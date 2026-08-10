"""Cold-path maintenance for ambiguous and duplicate memory entries."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable

from .storage import GrowthStore


DecisionCaller = Callable[[str, str, str], Awaitable[list[dict[str, Any]]]]


class MaintenancePipeline:
    """Review queued conflicts and merge clearly duplicate automatic entries."""

    def __init__(
        self,
        store: GrowthStore,
        llm_caller: DecisionCaller | None,
        *,
        similarity_threshold: float = 0.72,
        max_items: int = 20,
    ) -> None:
        self.store = store
        self.llm_caller = llm_caller
        self.similarity_threshold = max(0.6, min(0.95, similarity_threshold))
        self.max_items = max(1, min(50, max_items))

    async def run(self, run_id: str) -> dict[str, Any]:
        """Run one bounded maintenance pass.

        Args:
            run_id: Identifier used to claim queue items and charge LLM budget.

        Returns:
            Aggregate counters and a bounded list of decisions. A missing LLM
            caller leaves work deferred instead of pretending it succeeded.
        """
        queued = self.store.maintenance_queue(limit=self.max_items)
        pairs = self._similar_pairs()
        report: dict[str, Any] = {
            "processed": 0,
            "merged": 0,
            "archived": 0,
            "ignored": 0,
            "failed": 0,
            "deferred": 0,
            "similar_pairs": len(pairs),
            "decisions": [],
        }
        if not self.llm_caller:
            report["deferred"] = len(queued) + len(pairs)
            if report["deferred"]:
                report["error"] = "maintenance provider not configured"
            return report

        await self._process_queue(run_id, report)
        await self._merge_similar(run_id, pairs, report)
        return report

    async def _process_queue(self, run_id: str, report: dict[str, Any]) -> None:
        """Review queued owner conflicts with compare-and-set updates."""
        for item in self.store.claim_maintenance_queue(run_id, limit=self.max_items):
            try:
                decision = await self._decide(
                    {
                        "type": "conflict",
                        "existing": item["existing_content"],
                        "proposed": item["conflicting_content"],
                        "kind": item["kind"],
                        "scope_type": item["scope_type"],
                        "scope_key": item["scope_key"],
                    },
                    run_id,
                    "replace|merge|ignore",
                )
                action = decision.get("action", "ignore")
                entry = (
                    self.store._db()
                    .execute(
                        "SELECT * FROM entries WHERE entry_id=?", (item["entry_id"],)
                    )
                    .fetchone()
                )
                if not entry or entry["source_kind"] == "manual":
                    action = "ignore"
                    decision = {
                        "action": action,
                        "reason": "manual entry takes precedence",
                    }
                if action in {"replace", "merge"}:
                    content = str(
                        decision.get("merged_content") or item["conflicting_content"]
                    ).strip()[:4000]
                    if not content:
                        raise ValueError("maintenance decision has empty content")
                    payload = dict(entry)
                    try:
                        payload["triggers"] = json.loads(
                            payload.get("triggers_json") or "[]"
                        )
                    except (TypeError, ValueError):
                        payload["triggers"] = []
                    try:
                        payload["evidence_dates"] = json.loads(
                            payload.get("evidence_dates_json") or "[]"
                        )
                    except (TypeError, ValueError):
                        payload["evidence_dates"] = []
                    payload.update(
                        {
                            "content": content,
                            "status": "active",
                            "trust_level": "owner_explicit",
                            "confidence": 1.0,
                            "evidence_count": max(
                                1, int(payload.get("evidence_count", 0)) + 1
                            ),
                            "source_kind": "maintenance",
                        }
                    )
                    self.store.save_entry(
                        payload,
                        actor_key="maintenance",
                        reason=f"{action} queue={item['queue_id']}",
                    )
                    report["processed"] += 1
                    if action == "merge":
                        report["merged"] += 1
                elif action == "ignore":
                    report["ignored"] += 1
                else:
                    raise ValueError("unsupported maintenance decision")
                self.store.finish_maintenance_item(
                    item["queue_id"],
                    run_id,
                    "succeeded" if action != "ignore" else "ignored",
                    decision,
                )
                self._record_decision(
                    report, {"queue_id": item["queue_id"], **decision}
                )
            except Exception as exc:
                attempts = int(item.get("attempts", 1))
                status = "failed" if attempts >= 3 else "deferred"
                report["failed" if status == "failed" else "deferred"] += 1
                decision = {"action": status, "reason": f"{type(exc).__name__}: {exc}"}
                self.store.finish_maintenance_item(
                    item["queue_id"], run_id, status, decision
                )
                self._record_decision(
                    report, {"queue_id": item["queue_id"], **decision}
                )

    async def _merge_similar(
        self,
        run_id: str,
        pairs: list[tuple[dict[str, Any], dict[str, Any]]],
        report: dict[str, Any],
    ) -> None:
        """Ask the LLM whether bounded automatic-entry pairs should merge."""
        seen: set[str] = set()
        for left, right in pairs:
            if left["entry_id"] in seen or right["entry_id"] in seen:
                continue
            current = [
                self.store._db()
                .execute("SELECT * FROM entries WHERE entry_id=?", (left["entry_id"],))
                .fetchone(),
                self.store._db()
                .execute("SELECT * FROM entries WHERE entry_id=?", (right["entry_id"],))
                .fetchone(),
            ]
            if any(
                not row or row["status"] == "archived" or row["source_kind"] == "manual"
                for row in current
            ):
                continue
            try:
                decision = await self._decide(
                    {
                        "type": "similar",
                        "left": current[0]["content"],
                        "right": current[1]["content"],
                        "kind": current[0]["kind"],
                        "scope_type": current[0]["scope_type"],
                        "scope_key": current[0]["scope_key"],
                    },
                    run_id,
                    "merge|keep_both|ignore",
                )
                action = decision.get("action", "ignore")
                if action == "merge":
                    merged_content = str(decision.get("merged_content", "")).strip()[
                        :4000
                    ]
                    if not merged_content:
                        raise ValueError("merge decision has empty content")
                    survivor, loser = current
                    rank = {
                        "owner_explicit": 4,
                        "owner_correction": 3,
                        "repeated_observation": 2,
                        "model_inference": 1,
                    }
                    if (
                        rank.get(str(loser["trust_level"]), 0),
                        int(loser["evidence_count"]),
                    ) > (
                        rank.get(str(survivor["trust_level"]), 0),
                        int(survivor["evidence_count"]),
                    ):
                        survivor, loser = loser, survivor
                    payload = dict(survivor)
                    try:
                        payload["triggers"] = json.loads(
                            payload.get("triggers_json") or "[]"
                        )
                    except (TypeError, ValueError):
                        payload["triggers"] = []
                    try:
                        survivor_dates = json.loads(
                            payload.get("evidence_dates_json") or "[]"
                        )
                    except (TypeError, ValueError):
                        survivor_dates = []
                    try:
                        loser_dates = json.loads(loser["evidence_dates_json"] or "[]")
                    except (TypeError, ValueError):
                        loser_dates = []
                    payload.update(
                        {
                            "content": merged_content,
                            "evidence_count": int(survivor["evidence_count"])
                            + int(loser["evidence_count"]),
                            "evidence_dates": sorted(
                                set(survivor_dates) | set(loser_dates)
                            ),
                            "source_kind": "maintenance",
                        }
                    )
                    self.store.save_entry(
                        payload,
                        actor_key="maintenance",
                        reason=f"merge similar entry {loser['entry_id']}",
                    )
                    self.store.archive_entry(
                        loser["entry_id"], reason=f"merged into {survivor['entry_id']}"
                    )
                    report["processed"] += 1
                    report["merged"] += 1
                    report["archived"] += 1
                    seen.update((survivor["entry_id"], loser["entry_id"]))
                elif action in {"keep_both", "ignore"}:
                    report["ignored"] += 1
                else:
                    raise ValueError("unsupported similarity decision")
                self._record_decision(
                    report,
                    {"entry_ids": [left["entry_id"], right["entry_id"]], **decision},
                )
                pair_key = "|".join(
                    sorted(
                        (
                            f"{current[0]['entry_id']}:{current[0]['content_hash'] or ''}",
                            f"{current[1]['entry_id']}:{current[1]['content_hash'] or ''}",
                        )
                    )
                )
                self.store.audit(
                    "maintenance",
                    "maintenance_similar_decision",
                    "entries",
                    pair_key,
                    decision,
                )
            except Exception as exc:
                report["failed"] += 1
                self._record_decision(
                    report,
                    {
                        "entry_ids": [left["entry_id"], right["entry_id"]],
                        "action": "failed",
                        "reason": f"{type(exc).__name__}: {exc}",
                    },
                )

    async def _decide(
        self, payload: dict[str, Any], run_id: str, actions: str
    ) -> dict[str, Any]:
        """Request and validate one maintenance decision."""
        assert self.llm_caller is not None
        prompt = (
            "请维护成长记忆，只根据输入判断，不新增输入没有的事实。\n"
            f"输入={json.dumps(payload, ensure_ascii=False)}\n"
            f"action 只能是 {actions}。如果需要 merge，merged_content 必须是一句可执行、"
            "不超过 4000 字的合并内容。只输出一个 JSON 数组，例如 "
            '[{"action":"ignore","reason":"内容相同"}]。'
        )
        result = await self.llm_caller(
            prompt,
            "You are a conservative memory maintenance reviewer. Return one JSON array only.",
            run_id,
        )
        if isinstance(result, dict):
            decision = result
        elif isinstance(result, list) and result and isinstance(result[0], dict):
            decision = result[0]
        else:
            raise ValueError("maintenance output is not one JSON decision")
        action = str(decision.get("action", "")).strip().lower()
        if action not in set(actions.split("|")):
            raise ValueError("maintenance action is invalid")
        decision["action"] = action
        return decision

    def _similar_pairs(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Find bounded same-scope automatic pairs using text similarity."""
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for entry in self.store.maintenance_entries(limit=100):
            groups.setdefault(
                (entry["scope_type"], entry["scope_key"], entry["kind"]), []
            ).append(entry)
        pairs: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        for entries in groups.values():
            for index, left in enumerate(entries):
                left_text = re.sub(r"[\W_]+", "", str(left["content"]).casefold())
                if not left_text:
                    continue
                for right in entries[index + 1 :]:
                    right_text = re.sub(r"[\W_]+", "", str(right["content"]).casefold())
                    if not right_text:
                        continue
                    pair_key = "|".join(
                        sorted(
                            (
                                f"{left['entry_id']}:{left.get('content_hash', '')}",
                                f"{right['entry_id']}:{right.get('content_hash', '')}",
                            )
                        )
                    )
                    if (
                        self.store._db()
                        .execute(
                            "SELECT 1 FROM audit_log WHERE action=? AND object_id=? LIMIT 1",
                            ("maintenance_similar_decision", pair_key),
                        )
                        .fetchone()
                    ):
                        continue
                    similarity = SequenceMatcher(None, left_text, right_text).ratio()
                    if left_text in right_text or right_text in left_text:
                        similarity = 1.0
                    if similarity >= self.similarity_threshold:
                        pairs.append((similarity, left, right))
        pairs.sort(key=lambda item: item[0], reverse=True)
        return [(left, right) for _score, left, right in pairs[: self.max_items]]

    @staticmethod
    def _record_decision(report: dict[str, Any], decision: dict[str, Any]) -> None:
        """Keep reports useful without allowing unbounded database growth."""
        decisions = report.setdefault("decisions", [])
        if len(decisions) < 50:
            decisions.append(decision)
