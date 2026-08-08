from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .runtime import (
    AnchorHandle,
    BoundedCapture,
    CaptureGate,
    CaptureWriter,
    LearningPipeline,
    RuntimeSnapshot,
    event_identity,
    normalize_text,
    render_injection,
)
from .storage import GrowthStore, now_iso
from .prototype.growth_memory_core import (
    CaptureItemKind,
    Entry,
    EntryKind,
    EntryStatus,
    LearningTarget,
    ScopeType,
    TargetChatType,
    TargetMatcher,
    TrustLevel,
    Visibility,
)

try:  # AstrBot is intentionally optional for offline tests.
    from astrbot.api import logger
    from astrbot.api.event import filter
    from astrbot.api.star import Star, StarTools
    from astrbot.core.agent.message import TextPart
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # pragma: no cover - only used by local import/compile tests
    import logging

    logger = logging.getLogger("growth_memory")

    class Star:  # type: ignore[no-redef]
        def __init__(self, context: Any):
            self.context = context

    class _Decorators:
        class CustomFilter:
            def __init__(self, raise_error: bool = True, **kwargs: Any):
                self.raise_error = raise_error

            def filter(self, event: Any, cfg: Any = None) -> bool:
                return False

        class PermissionType:
            ADMIN = "admin"

        EventMessageType = type("EventMessageType", (), {"ALL": "all"})

        def __getattr__(self, _name: str):
            return lambda *args, **kwargs: lambda fn: fn

        def custom_filter(self, _cls: Any, *args: Any, **kwargs: Any):
            return lambda fn: fn

        def permission_type(self, _kind: Any):
            return lambda fn: fn

    filter = _Decorators()  # type: ignore[assignment]

    class _Response:
        def __init__(self, data: Any, status_code: int = 200):
            self.data, self.status_code = data, status_code

    def json_response(data: Any = None, **kwargs: Any) -> _Response:
        return _Response(data or {}, kwargs.get("status_code", 200))

    def error_response(message: str, **kwargs: Any) -> _Response:
        return _Response(
            {"status": "error", "message": message}, kwargs.get("status_code", 400)
        )

    request = type(
        "Request",
        (),
        {
            "method": "GET",
            "path": "",
            "username": "offline",
            "path_params": {},
            "json": None,
        },
    )()
    TextPart = None
    StarTools = None


PLUGIN_NAME = "astrbot_plugin_growth_memory"
ANCHOR_STATE_TTL_SECONDS = 60 * 60
RUNTIME_SETTING_KEYS = {
    "owner_identities",
    "capture_enabled",
    "extractor_provider_id",
    "reviewer_provider_id",
    "daily_request_budget",
    "daily_input_token_budget",
    "injection_token_budget",
}
RUNTIME_SETTING_LIMITS = {
    "daily_request_budget": (1, 32),
    "daily_input_token_budget": (2000, 64000),
    "injection_token_budget": (128, 2400),
}


class TargetCaptureFilter(getattr(filter, "CustomFilter", object)):  # type: ignore[misc]
    gate: CaptureGate | None = None

    def filter(self, event: Any, cfg: Any = None) -> bool:
        return bool(self.gate and self.gate.matches(event))


class GrowthMemory(Star):
    def __init__(self, context: Any, config: dict[str, Any]):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        data_dir = None
        if StarTools is not None:
            try:
                data_dir = Path(StarTools.get_data_dir())
            except Exception:
                data_dir = None
        self.data_dir = data_dir or Path(__file__).resolve().parent / "data"
        self.store = GrowthStore(self.data_dir / "growth_memory.db")
        self.capture = BoundedCapture(
            int(self.config.get("capture_buffer_capacity", 2048) or 2048)
        )
        self.gate = CaptureGate()
        TargetCaptureFilter.gate = self.gate
        self.writer = CaptureWriter(self.store, self.capture)
        self.pipeline = LearningPipeline(
            self.context,
            self.store,
            self.config,
            lambda: self.gate.snapshot,
            lambda: self.capture.degraded,
        )
        self._snapshot = RuntimeSnapshot(TargetMatcher(capture_enabled=False))
        self._anchors: dict[str, AnchorHandle] = {}
        self._anchor_futures: dict[str, asyncio.Future[Any]] = {}
        self._anchor_state_started_at: dict[str, float] = {}
        self._pending_completions: dict[str, tuple[str, str, str, str]] = {}
        self._routes: list[tuple[str, list[str]]] = []

    async def initialize(self) -> None:
        self.store.open()
        self._load_runtime_flags()
        self._seed()
        self._refresh_snapshot()
        await self.writer.start()
        await self.pipeline.start()
        self._register_web_api()
        logger.info("[%s] initialized: %s", PLUGIN_NAME, self.store.counts())

    def _load_runtime_flags(self) -> None:
        flags = self.store.runtime_flags()
        defaults = {
            "owner_identities": self.config.get("owner_identities", []),
            "capture_enabled": self.config.get("capture_enabled", True),
            "extractor_provider_id": self.config.get("extractor_provider_id", ""),
            "reviewer_provider_id": self.config.get("reviewer_provider_id", ""),
            "daily_request_budget": self.config.get("daily_request_budget", 8),
            "daily_input_token_budget": self.config.get(
                "daily_input_token_budget", 16000
            ),
            "injection_token_budget": self.config.get("injection_token_budget", 800),
        }
        for key, value in defaults.items():
            if key not in flags:
                self.store.set_runtime_flag(key, value, "config_seed")
                flags[key] = value
        self.config.update(
            {key: value for key, value in flags.items() if key in RUNTIME_SETTING_KEYS}
        )

    def _seed(self) -> None:
        if not self.store.schedules():
            self.store.upsert_schedule("03:00")
        if self.store.targets():
            return
        for value in self.config.get("initial_learning_targets", []) or []:
            if isinstance(value, dict):
                try:
                    self.store.upsert_target(value, source="config")
                except ValueError as exc:
                    logger.warning("[%s] ignored target: %s", PLUGIN_NAME, exc)

    def _refresh_snapshot(self) -> None:
        targets = []
        target_ids = {}
        for row in self.store.targets():
            target = LearningTarget(
                platform=row["platform"],
                account_id=row["account_id"],
                chat_type=TargetChatType(row["chat_type"]),
                peer_id=row["peer_id"],
                enabled=bool(row["enabled"]),
            )
            targets.append(target)
            target_ids[target.key] = row["target_id"]
        entries = []
        for row in self.store.entries():
            try:
                entries.append(
                    Entry(
                        entry_id=row["entry_id"],
                        scope_type=ScopeType(row["scope_type"]),
                        scope_key=row.get("scope_key", ""),
                        kind=EntryKind(row["kind"]),
                        content=row["content"],
                        triggers=tuple(json.loads(row.get("triggers_json") or "[]")),
                        conflict_key=row.get("conflict_key", ""),
                        status=EntryStatus(row["status"]),
                        trust=TrustLevel(row.get("trust_level", "model_inference")),
                        confidence=float(row.get("confidence", 0)),
                        evidence_count=int(row.get("evidence_count", 0)),
                        evidence_days=int(row.get("evidence_days", 0)),
                        priority=int(row.get("priority", 0)),
                        visibility=Visibility(row.get("visibility", "public")),
                        updated_at=row.get("updated_at", ""),
                        version=int(row.get("version", 1)),
                    )
                )
            except (TypeError, ValueError):
                continue
        owner_values = set()
        for value in self.config.get("owner_identities", []) or []:
            raw = str(value).strip()
            if not raw:
                continue
            parts = raw.split(":", 1)
            owner_values.add(raw if len(parts) != 2 else f"{parts[0]}:user:{parts[1]}")
        owners = frozenset(owner_values)
        capture_enabled = bool(self.config.get("capture_enabled", True))
        self._snapshot = RuntimeSnapshot(
            TargetMatcher(tuple(targets), capture_enabled=capture_enabled),
            tuple(entries),
            owners,
            capture_enabled,
        )
        self.gate.replace(self._snapshot)
        self._target_ids = target_ids

    @staticmethod
    def _validate_runtime_updates(updates: dict[str, Any]) -> None:
        if "owner_identities" in updates:
            values = updates["owner_identities"]
            if (
                not isinstance(values, list)
                or len(values) > 20
                or any(
                    not isinstance(value, str)
                    or not value.startswith("aiocqhttp:")
                    or not value.removeprefix("aiocqhttp:").isdigit()
                    or not 5 <= len(value.removeprefix("aiocqhttp:")) <= 20
                    for value in values
                )
            ):
                raise ValueError(
                    "owner_identities must contain at most 20 aiocqhttp:QQ entries"
                )
        if "capture_enabled" in updates and not isinstance(
            updates["capture_enabled"], bool
        ):
            raise ValueError("capture_enabled must be boolean")
        for key in ("extractor_provider_id", "reviewer_provider_id"):
            if key in updates and (
                not isinstance(updates[key], str) or len(updates[key]) > 200
            ):
                raise ValueError(f"{key} must be a string of at most 200 characters")
        for key, (minimum, maximum) in RUNTIME_SETTING_LIMITS.items():
            if key in updates and (
                isinstance(updates[key], bool)
                or not isinstance(updates[key], int)
                or not minimum <= updates[key] <= maximum
            ):
                raise ValueError(f"{key} must be between {minimum} and {maximum}")

    def _target_for_event(self, event: Any) -> str | None:
        platform, account, chat_type, peer, *_ = event_identity(event)
        key = f"{platform}:{account or '*'}:{chat_type}:{peer}"
        wildcard = f"{platform}:*:{chat_type}:{peer}"
        return self._target_ids.get(key) or self._target_ids.get(wildcard)

    def _event_key(self, event: Any) -> str:
        obj = getattr(event, "message_obj", None)
        msg_id = str(getattr(obj, "message_id", "") or "")
        return (
            msg_id
            or f"{event_identity(event)[-1]}:{hash(getattr(event, 'message_str', ''))}"
        )

    def _is_management(self, event: Any) -> bool:
        text = str(getattr(event, "message_str", "") or "").strip().lstrip("/")
        return (
            text.split(maxsplit=1)[0] in {"进化", "停止进化", "进化状态"}
            if text
            else False
        )

    def _inbound_operation(self, event: Any, target_id: str, key: str) -> Any:
        text, components = normalize_text(event)
        platform, _account, _chat_type, _peer, sender, session = event_identity(event)
        sender_name = str(getattr(event, "get_sender_name", lambda: "")() or "")[:80]

        def operation() -> dict[str, Any] | None:
            if not text and not components:
                return None
            existing = self.store.message_by_platform_id(target_id, key)
            if existing:
                return existing
            row = self.store.create_message(
                target_id,
                direction="inbound",
                sender_key=f"{platform}:user:{sender}",
                sender_name=sender_name,
                text=text,
                session_id=session,
                source="platform_inbound",
                platform_message_id=key,
            )
            self.store.close_mature_anchors(target_id, int(row["message_seq"]))
            return row

        return operation

    @staticmethod
    def _consume_future(future: asyncio.Future[Any]) -> None:
        if future.cancelled():
            return
        try:
            future.result()
        except Exception:
            logger.exception("[%s] capture writer operation failed", PLUGIN_NAME)

    def _prune_anchor_state(self) -> None:
        """Drop abandoned hook state so long-running bots do not retain each turn."""
        deadline = time.monotonic() - ANCHOR_STATE_TTL_SECONDS
        expired = [
            origin
            for origin, started_at in self._anchor_state_started_at.items()
            if started_at < deadline
        ]
        for origin in expired:
            future = self._anchor_futures.pop(origin, None)
            if future and not future.done():
                future.cancel()
            self._anchors.pop(origin, None)
            self._pending_completions.pop(origin, None)
            self._anchor_state_started_at.pop(origin, None)

    def _remember_anchor(self, origin: str, handle: AnchorHandle) -> None:
        self._anchors[origin] = handle
        self._anchor_state_started_at[origin] = time.monotonic()

    def _on_anchor_resolved(self, origin: str, future: asyncio.Future[Any]) -> None:
        self._anchor_futures.pop(origin, None)
        if future.cancelled():
            return
        try:
            handle = future.result()
        except Exception:
            logger.exception("[%s] anchor operation failed", PLUGIN_NAME)
            self._pending_completions.pop(origin, None)
            self._anchor_state_started_at.pop(origin, None)
            return
        if not handle:
            self._pending_completions.pop(origin, None)
            self._anchor_state_started_at.pop(origin, None)
            return
        self._remember_anchor(origin, handle)
        completion = self._pending_completions.pop(origin, None)
        if completion:
            self._queue_answer(handle, *completion)

    def _queue_answer(
        self,
        handle: AnchorHandle,
        role: str,
        text: str,
        platform: str,
        session: str,
    ) -> None:
        if role != "assistant" or not text:
            future = self.writer.submit(
                CaptureItemKind.ANSWER_FINAL,
                f"answer-error:{handle.anchor_id}",
                lambda: self.store.update_anchor(
                    handle.anchor_id,
                    answer_state="error",
                    request_state="failed",
                    status="retryable",
                ),
                anchor_id=handle.anchor_id,
            )
            if future:
                future.add_done_callback(self._consume_future)
            return

        close_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        def save_answer() -> dict[str, Any]:
            existing = (
                self.store._db()
                .execute(
                    "SELECT answer_row_id FROM trigger_anchors WHERE anchor_id=?",
                    (handle.anchor_id,),
                )
                .fetchone()
            )
            if existing and existing["answer_row_id"]:
                row = (
                    self.store._db()
                    .execute(
                        "SELECT * FROM conversation_messages WHERE row_id=?",
                        (existing["answer_row_id"],),
                    )
                    .fetchone()
                )
                if row:
                    return dict(row)
            row = self.store.create_message(
                handle.target_id,
                direction="outbound",
                sender_key=f"{platform}:bot",
                sender_name="",
                text=text,
                session_id=session,
                source="agent_final",
                delivery_state="unknown",
            )
            self.store.update_anchor(
                handle.anchor_id,
                answer_row_id=row["row_id"],
                answer_state="generated",
                answer_source="agent_done",
                status="open",
                context_close_at=close_at,
            )
            return row

        future = self.writer.submit(
            CaptureItemKind.ANSWER_FINAL,
            f"answer:{handle.anchor_id}",
            save_answer,
            anchor_id=handle.anchor_id,
        )
        if future:
            future.add_done_callback(self._consume_future)

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.custom_filter(TargetCaptureFilter, priority=10**9)
    async def capture_message(self, event: Any) -> None:
        self._prune_anchor_state()
        if self._is_management(event):
            return
        target_id = self._target_for_event(event)
        if not target_id:
            return
        key = self._event_key(event)
        future = self.writer.submit(
            CaptureItemKind.CONTEXT,
            key,
            self._inbound_operation(event, target_id, key),
        )
        if future:
            future.add_done_callback(self._consume_future)

    @filter.on_waiting_llm_request(priority=10**9)
    async def on_waiting_llm_request(self, event: Any) -> None:
        self._prune_anchor_state()
        if self._is_management(event) or not self.gate.matches(event):
            return
        target_id = self._target_for_event(event)
        if not target_id:
            return
        key = self._event_key(event)
        inbound = self._inbound_operation(event, target_id, key)

        def ensure_anchor() -> AnchorHandle | None:
            row = inbound()
            if not row:
                return None
            anchor = self.store.create_anchor(target_id, row["row_id"], now_iso())
            self.store.update_anchor(anchor["anchor_id"], request_state="built")
            return AnchorHandle(anchor["anchor_id"], target_id, row["row_id"])

        future = self.writer.submit(
            CaptureItemKind.ANCHOR_OPEN,
            f"anchor:{key}",
            ensure_anchor,
            anchor_id=f"pending:{key}",
        )
        if future is None:
            return
        origin = str(getattr(event, "unified_msg_origin", "") or key)
        self._anchor_futures[origin] = future
        self._anchor_state_started_at[origin] = time.monotonic()
        future.add_done_callback(lambda done: self._on_anchor_resolved(origin, done))

    @filter.on_llm_request(priority=10**9)
    async def on_llm_request(self, event: Any, req: Any) -> None:
        text, _ids, _tokens = render_injection(
            self._snapshot,
            event,
            int(self.config.get("injection_token_budget", 800) or 800),
        )
        if text and hasattr(req, "extra_user_content_parts") and TextPart is not None:
            part = TextPart(text=text)
            if hasattr(part, "mark_as_temp"):
                part = part.mark_as_temp()
            req.extra_user_content_parts.append(part)

    @filter.on_agent_done(priority=10**9)
    async def on_agent_done(self, event: Any, run_context: Any, response: Any) -> None:
        self._prune_anchor_state()
        origin = str(getattr(event, "unified_msg_origin", "") or self._event_key(event))
        handle = self._anchors.get(origin)
        role = str(getattr(response, "role", ""))
        text = str(getattr(response, "completion_text", "") or "").strip()
        platform, _account, _chat, _peer, _sender, session = event_identity(event)
        if handle:
            self._queue_answer(handle, role, text, platform, session)
            return
        if origin in self._anchor_futures:
            # The capture worker can finish after the agent. Preserve the result
            # without making this chat hook wait on storage.
            self._pending_completions[origin] = (role, text, platform, session)

    @filter.on_decorating_result(priority=-(10**9))
    async def on_decorating_result(self, event: Any) -> None:
        return

    @filter.after_message_sent(priority=-(10**9))
    async def after_message_sent(self, event: Any) -> None:
        origin = str(getattr(event, "unified_msg_origin", "") or self._event_key(event))
        handle = self._anchors.pop(origin, None)
        # The writer can still be creating the anchor when delivery is observed.
        # Keep a pending completion alive until _on_anchor_resolved can bind it.
        anchor_pending = origin in self._anchor_futures
        if handle or not anchor_pending:
            self._anchor_state_started_at.pop(origin, None)
            self._pending_completions.pop(origin, None)
        if handle:
            future = self.writer.submit(
                CaptureItemKind.DELIVERY_OBSERVED,
                f"delivery:{handle.anchor_id}",
                lambda: self.store.update_anchor(
                    handle.anchor_id, delivery_state="attempted_unknown"
                ),
                anchor_id=handle.anchor_id,
            )
            if future:
                future.add_done_callback(self._consume_future)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("进化")
    async def enable_learning(self, event: Any) -> None:
        await self._command_target(event, True)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("停止进化")
    async def disable_learning(self, event: Any) -> None:
        await self._command_target(event, False)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("进化状态")
    async def learning_status(self, event: Any) -> None:
        target_id = self._target_for_event(event)
        counts = self.store.counts()
        await event.send(
            f"成长记忆: target={'未开启' if not target_id else target_id}, anchors={counts['trigger_anchors']}, entries={counts['entries']}, degraded={self.capture.degraded}, last_error={self.pipeline.last_error or 'none'}"
        )

    async def _command_target(self, event: Any, enabled: bool) -> None:
        platform, account, chat_type, peer, *_ = event_identity(event)
        if platform != "aiocqhttp" or not peer:
            await event.send("成长记忆目前只支持 QQ 私聊和群聊")
            return
        row = self.store.upsert_target(
            {
                "platform": platform,
                "account_id": account,
                "chat_type": chat_type,
                "peer_id": peer,
                "enabled": enabled,
                "label": "command",
            },
            source="command",
        )
        self.store.audit(
            "admin",
            "enable_target" if enabled else "disable_target",
            "learning_target",
            row["target_id"],
            {"target_key": row["target_key"]},
        )
        self._refresh_snapshot()
        await event.send(
            ("已开启" if enabled else "已停止") + f"当前会话学习: {row['target_key']}"
        )

    def _register_web_api(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            return
        routes = [
            ("/astrbot_plugin_growth_memory/state", ["GET"]),
            ("/astrbot_plugin_growth_memory/targets", ["GET", "POST"]),
            ("/astrbot_plugin_growth_memory/targets/<target_id>", ["POST"]),
            ("/astrbot_plugin_growth_memory/entries", ["GET", "POST"]),
            ("/astrbot_plugin_growth_memory/entries/<entry_id>", ["POST"]),
            (
                "/astrbot_plugin_growth_memory/entries/<entry_id>/versions",
                ["GET"],
            ),
            ("/astrbot_plugin_growth_memory/entries/<entry_id>/rollback", ["POST"]),
            ("/astrbot_plugin_growth_memory/schedules", ["GET", "POST"]),
            ("/astrbot_plugin_growth_memory/schedules/<schedule_id>", ["POST"]),
            ("/astrbot_plugin_growth_memory/runs", ["GET"]),
            ("/astrbot_plugin_growth_memory/run-now", ["POST"]),
            ("/astrbot_plugin_growth_memory/settings", ["GET", "POST"]),
        ]
        for route, methods in routes:
            self.context.register_web_api(
                route, self.web_api, methods, "成长记忆管理 API"
            )
            self._routes.append((route, methods))

    async def web_api(self, **path_params: Any) -> Any:
        method = str(getattr(request, "method", "GET")).upper()
        path = str(getattr(request, "path", ""))
        username = str(getattr(request, "username", "") or "")
        if not username:
            return error_response("dashboard authentication required", status_code=401)
        try:
            if path.endswith("/state"):
                budget_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
                return json_response(
                    {
                        "plugin": PLUGIN_NAME,
                        "snapshot": {
                            "targets": len(self.store.targets()),
                            "entries": len(self.store.entries()),
                            "capture_enabled": self._snapshot.capture_enabled,
                        },
                        "counts": self.store.counts(),
                        "targets": self.store.targets(),
                        "schedules": self.store.schedules(),
                        "budget": self.store.daily_budget(budget_date),
                        "queue_depth": self.capture.size,
                        "degraded": self.capture.degraded,
                        "last_error": self.pipeline.last_error,
                    }
                )
            if "/targets" in path:
                if method == "GET":
                    return json_response(self.store.targets())
                if method == "POST" and not path_params.get("target_id"):
                    body = await request.json(default={})
                    row = self.store.upsert_target(body or {}, source="page")
                    self.store.audit(
                        username, "upsert", "learning_target", row["target_id"]
                    )
                    self._refresh_snapshot()
                    return json_response(row, status_code=201)
                tid = path_params.get("target_id")
                body = await request.json(default={})
                if method == "POST":
                    row = next(
                        (x for x in self.store.targets() if x["target_id"] == tid), None
                    )
                    if not row:
                        return error_response("target not found", status_code=404)
                    row.update(body or {})
                    row["target_id"] = tid
                    result = self.store.upsert_target(row, source="page")
                    self.store.audit(username, "update", "learning_target", tid)
                    self._refresh_snapshot()
                    return json_response(result)
            if "/entries" in path:
                if path.endswith("/versions") and method == "GET":
                    entry_id = str(path_params.get("entry_id") or "")
                    if not any(
                        row["entry_id"] == entry_id
                        for row in self.store.entries(include_archived=True)
                    ):
                        return error_response("entry not found", status_code=404)
                    return json_response(self.store.entry_versions(entry_id))
                if path.endswith("/rollback") and method == "POST":
                    body = await request.json(default={})
                    version = int((body or {}).get("version", 0))
                    row = self.store.rollback_entry(
                        str(path_params.get("entry_id")), version, username
                    )
                    self.store.audit(
                        username,
                        "rollback",
                        "entry",
                        row["entry_id"],
                        {"version": version},
                    )
                    self._refresh_snapshot()
                    return json_response(row)
                if method == "GET":
                    return json_response(self.store.entries(include_archived=True))
                body = await request.json(default={})
                if not isinstance(body, dict):
                    return error_response("JSON object required")
                if method == "POST":
                    if path_params.get("entry_id"):
                        current = next(
                            (
                                item
                                for item in self.store.entries(include_archived=True)
                                if item["entry_id"] == path_params["entry_id"]
                            ),
                            None,
                        )
                        if not current:
                            return error_response("entry not found", status_code=404)
                        current["triggers"] = json.loads(
                            current.get("triggers_json") or "[]"
                        )
                        current.update(body)
                        current["entry_id"] = path_params["entry_id"]
                        body = current
                    if (
                        body.get("scope_type") in {"global", "owner", "task"}
                        and body.get("kind") == "behavior_rule"
                    ):
                        body["trust_level"] = "manual"
                    row = self.store.save_entry(body, actor_key=username)
                    self.store.audit(username, "save", "entry", row["entry_id"])
                    self._refresh_snapshot()
                    return json_response(
                        row,
                        status_code=201 if not path_params.get("entry_id") else 200,
                    )
            if "/schedules" in path:
                schedule_id = path_params.get("schedule_id")
                if schedule_id and method == "POST":
                    body = await request.json(default={})
                    if not isinstance(body, dict):
                        return error_response("JSON object required")
                    current = next(
                        (
                            item
                            for item in self.store.schedules()
                            if item["schedule_id"] == schedule_id
                        ),
                        None,
                    )
                    if not current:
                        return error_response("schedule not found", status_code=404)
                    if body.get("action") == "delete":
                        self.store.delete_schedule(schedule_id)
                        self.store.audit(username, "delete", "schedule", schedule_id)
                        return json_response({"ok": True})
                    if "local_time" in body or "timezone" in body:
                        row = self.store.update_schedule(
                            schedule_id,
                            str(body.get("local_time", current["local_time"])),
                            str(body.get("timezone", current["timezone"])),
                            bool(body.get("enabled", current["enabled"])),
                        )
                        self.store.audit(username, "update", "schedule", schedule_id)
                        return json_response(row)
                    if not isinstance(body.get("enabled"), bool):
                        return error_response("enabled boolean required")
                    self.store.set_schedule_enabled(schedule_id, body["enabled"])
                    self.store.audit(
                        username,
                        "toggle",
                        "schedule",
                        schedule_id,
                        {"enabled": body["enabled"]},
                    )
                    return json_response(
                        next(
                            item
                            for item in self.store.schedules()
                            if item["schedule_id"] == schedule_id
                        )
                    )
                if method == "GET":
                    return json_response(self.store.schedules())
                body = await request.json(default={})
                row = self.store.upsert_schedule(
                    str(body.get("local_time", "")),
                    str(body.get("timezone", "Asia/Shanghai")),
                    bool(body.get("enabled", True)),
                )
                self.store.audit(username, "upsert", "schedule", row["schedule_id"])
                return json_response(row, status_code=201)
            if path.endswith("/runs") and method == "GET":
                return json_response(
                    [
                        dict(r)
                        for r in self.store._db()
                        .execute(
                            "SELECT * FROM learning_runs ORDER BY created_at DESC LIMIT 50"
                        )
                        .fetchall()
                    ]
                )
            if path.endswith("/run-now") and method == "POST":
                value = await self.pipeline.run_due(force=True)
                return json_response(value, status_code=202)
            if path.endswith("/settings"):
                if method == "GET":
                    return json_response(
                        {
                            k: self.config.get(k)
                            for k in (
                                "owner_identities",
                                "capture_enabled",
                                "extractor_provider_id",
                                "reviewer_provider_id",
                                "daily_request_budget",
                                "daily_input_token_budget",
                                "injection_token_budget",
                            )
                        }
                    )
                body = await request.json(default={})
                if not isinstance(body, dict):
                    return error_response("JSON object required")
                allowed = RUNTIME_SETTING_KEYS
                updates = {k: v for k, v in body.items() if k in allowed}
                self._validate_runtime_updates(updates)
                for key, value in updates.items():
                    self.store.set_runtime_flag(key, value, username)
                self.config.update(updates)
                self._refresh_snapshot()
                self.store.audit(
                    username,
                    "update",
                    "settings",
                    "runtime",
                    {k: body[k] for k in body if k in allowed},
                )
                return json_response({"ok": True})
            return error_response("route not found", status_code=404)
        except ValueError as exc:
            return error_response(str(exc), status_code=422)
        except Exception:
            logger.exception("[%s] web api error", PLUGIN_NAME)
            return error_response("internal error", status_code=500)

    async def terminate(self) -> None:
        await self.pipeline.stop()
        await self.writer.stop()
        self._anchor_futures.clear()
        self._anchors.clear()
        self._anchor_state_started_at.clear()
        self._pending_completions.clear()
        if hasattr(self.context, "registered_web_apis"):
            self.context.registered_web_apis[:] = [
                item
                for item in self.context.registered_web_apis
                if not item[0].startswith("/astrbot_plugin_growth_memory/")
            ]
        self.store.close()


__all__ = ["GrowthMemory", "TargetCaptureFilter"]
