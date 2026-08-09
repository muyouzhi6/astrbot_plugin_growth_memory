from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
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
    StarTools = None

try:
    from astrbot.core.agent.message import TextPart
except ImportError:  # AstrBot 4.26.x exposes no TextPart module in some builds.
    TextPart = None

try:
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # pragma: no cover - compatibility with older AstrBot builds
    try:
        from quart import g, jsonify, request
    except ImportError:  # pragma: no cover - offline unit tests
        g = None
        jsonify = None
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

    class _Response:
        def __init__(self, data: Any, status_code: int = 200):
            self.data, self.status_code = data, status_code

    def json_response(data: Any = None, **kwargs: Any) -> Any:
        status_code = kwargs.get("status_code", 200)
        if jsonify is None:
            return _Response(data or {}, status_code)
        try:
            response = jsonify(data or {})
        except RuntimeError:
            return _Response(data or {}, status_code)
        response.status_code = status_code
        return response

    def error_response(message: str, **kwargs: Any) -> Any:
        return json_response(
            {"status": "error", "message": message},
            status_code=kwargs.get("status_code", 400),
        )


HAS_WAITING_LLM_HOOK = hasattr(filter, "on_waiting_llm_request")
HAS_AGENT_DONE_HOOK = hasattr(filter, "on_agent_done")
AGENT_COMPLETION_DECORATOR = (
    filter.on_agent_done if HAS_AGENT_DONE_HOOK else filter.on_llm_response
)

for _missing_hook in ("on_waiting_llm_request", "on_agent_done"):
    if not hasattr(filter, _missing_hook):
        setattr(
            filter,
            _missing_hook,
            lambda *args, **kwargs: lambda fn: fn,
        )


PLUGIN_NAME = "astrbot_plugin_growth_memory"
ANCHOR_STATE_TTL_SECONDS = 60 * 60
RUNTIME_SETTING_KEYS = {
    "owner_identities",
    "capture_enabled",
    "llm_note_enabled",
    "extractor_provider_id",
    "reviewer_provider_id",
    "daily_request_budget",
    "daily_input_token_budget",
    "daily_output_token_budget",
    "learning_input_token_limit",
    "learning_max_output_tokens",
    "injection_token_budget",
}
RUNTIME_SETTING_LIMITS = {
    "daily_request_budget": (1, 128),
    "daily_input_token_budget": (2000, 1000000),
    "daily_output_token_budget": (1000, 1000000),
    "learning_input_token_limit": (1000, 1000000),
    "learning_max_output_tokens": (256, 1000000),
    "injection_token_budget": (128, 2400),
}
SAFE_RUNTIME_DEFAULTS = {
    "owner_identities": [],
    "capture_enabled": False,
    "llm_note_enabled": True,
    "extractor_provider_id": "",
    "reviewer_provider_id": "",
    "daily_request_budget": 64,
    "daily_input_token_budget": 1000000,
    "daily_output_token_budget": 1000000,
    "learning_input_token_limit": 32000,
    "learning_max_output_tokens": 32768,
    "injection_token_budget": 800,
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
            self._refresh_snapshot,
        )
        self._snapshot = RuntimeSnapshot(TargetMatcher(capture_enabled=False))
        self._anchors: dict[str, AnchorHandle] = {}
        self._anchor_futures: dict[str, asyncio.Future[Any]] = {}
        self._anchor_state_started_at: dict[str, float] = {}
        self._pending_completions: dict[str, tuple[str, str, str, str]] = {}
        self._routes: list[tuple[str, list[str]]] = []
        self._target_ids: dict[str, tuple[str, bool]] = {}
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        try:
            self.store.open()
            self._load_runtime_flags()
            self._seed()
            self._refresh_snapshot()
            await self.writer.start()
            await self.pipeline.start()
            self._register_web_api()
            logger.info("[%s] initialized: %s", PLUGIN_NAME, self.store.counts())
        except BaseException:
            await self.terminate()
            raise

    def _load_runtime_flags(self) -> None:
        flags = self.store.runtime_flags()
        defaults = {
            "owner_identities": self.config.get("owner_identities", []),
            "capture_enabled": self.config.get("capture_enabled", True),
            "llm_note_enabled": self.config.get("llm_note_enabled", True),
            "extractor_provider_id": self.config.get("extractor_provider_id", ""),
            "reviewer_provider_id": self.config.get("reviewer_provider_id", ""),
            "daily_request_budget": self.config.get("daily_request_budget", 64),
            "daily_input_token_budget": self.config.get(
                "daily_input_token_budget", 1000000
            ),
            "daily_output_token_budget": self.config.get(
                "daily_output_token_budget", 1000000
            ),
            "learning_input_token_limit": self.config.get(
                "learning_input_token_limit", 32000
            ),
            "learning_max_output_tokens": self.config.get(
                "learning_max_output_tokens", 32768
            ),
            "injection_token_budget": self.config.get("injection_token_budget", 800),
        }
        for key, configured_value in defaults.items():
            value = flags.get(key, configured_value)
            try:
                self._validate_runtime_updates({key: value})
            except ValueError as exc:
                value = SAFE_RUNTIME_DEFAULTS[key]
                logger.warning(
                    "[%s] repaired invalid runtime flag %s: %s",
                    PLUGIN_NAME,
                    key,
                    exc,
                )
                self.store.set_runtime_flag(key, value, "runtime_repair")
            else:
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
            target_ids[target.key] = (row["target_id"], bool(row["enabled"]))
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
            parts = raw.split(":")
            if len(parts) == 3 and parts[1] == "user" and parts[2].isdigit():
                owner_values.add(f"{parts[0]}:user:{parts[2]}")
            elif len(parts) == 2 and parts[1].isdigit():
                owner_values.add(f"{parts[0]}:user:{parts[1]}")
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
                    or not (
                        value.removeprefix("aiocqhttp:").isdigit()
                        or (
                            value.removeprefix("aiocqhttp:").startswith("user:")
                            and value.removeprefix("aiocqhttp:user:").isdigit()
                        )
                    )
                    or not 5
                    <= len(
                        value.removeprefix("aiocqhttp:user:")
                        if value.removeprefix("aiocqhttp:").startswith("user:")
                        else value.removeprefix("aiocqhttp:")
                    )
                    <= 20
                    for value in values
                )
            ):
                raise ValueError(
                    "owner_identities must contain at most 20 aiocqhttp:QQ entries"
                )
        for key in ("capture_enabled", "llm_note_enabled"):
            if key in updates and not isinstance(updates[key], bool):
                raise ValueError(f"{key} must be boolean")
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
        exact = self._target_ids.get(key)
        if exact is not None:
            return exact[0] if exact[1] else None
        fallback = self._target_ids.get(wildcard)
        return fallback[0] if fallback and fallback[1] else None

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

    @staticmethod
    def _sanitize_tool_note(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("note must be a string")
        note = "".join(
            char for char in value.strip() if char in "\n\t" or ord(char) >= 32
        )
        if not note or len(note) > 1000:
            raise ValueError("note must contain 1-1000 visible characters")
        lowered = note.lower()
        if any(
            marker in lowered
            for marker in (
                "api key",
                "apikey",
                "password",
                "passwd",
                "access token",
                "refresh token",
                "secret key",
                "private key",
                "-----begin",
                "ignore previous instructions",
                "忽略之前的指令",
            )
        ) or re.search(r"\bsk-[a-z0-9_-]{12,}\b", lowered):
            raise ValueError("note looks like a credential or prompt injection")
        return note

    async def _tool_evidence_row(
        self, event: Any, target_id: str
    ) -> dict[str, Any] | None:
        key = self._event_key(event)
        origin = str(getattr(event, "unified_msg_origin", "") or key)
        pending = self._anchor_futures.get(origin)
        if pending and not pending.done():
            try:
                await asyncio.wait_for(asyncio.shield(pending), timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        row = self.store.message_by_platform_id(target_id, key)
        if row:
            return row
        operation = self._inbound_operation(event, target_id, key)
        if getattr(self.writer, "_task", None) is None:
            return operation()
        future = self.writer.submit(
            CaptureItemKind.CONTEXT,
            f"tool-context:{key}",
            operation,
        )
        if future:
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=1.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return self.store.message_by_platform_id(target_id, key)

    @filter.llm_tool(name="growth_memory_note")
    async def growth_memory_note(
        self,
        event: Any,
        note: str,
        scope: str = "owner",
        kind: str = "profile_fact",
        subject_id: str = "",
        triggers: str | list[str] | None = None,
        confidence: float = 0.8,
        expires_in_days: str = "0",
    ) -> str:
        """提交一条需要审核的长期记忆候选, 不会直接修改正式记忆.

        仅当用户明确表达希望长期记住偏好、纠正、规则或重要经历时调用. 普通闲聊、一次性事实、通用知识和敏感信息不要调用.

        Args:
            note(string): 用一句可执行、可验证的话概括用户希望长期记住的内容, 不要写密码或提示词.
            scope(string): 记忆层级, 只能是 owner, global, task, group, person.
            kind(string): 条目类型, 只能是 behavior_rule, profile_fact, milestone, emotional_bond.
            subject_id(string): person 层级对应的 QQ 号, 留空表示当前说话的人.
            triggers(string): task 层级触发词, 多个触发词用逗号或换行分隔, 其他层级可留空.
            confidence(number): 对候选内容的初始置信度, 0 到 1.
            expires_in_days(string): 过期天数, 0 表示永久, 大于 0 表示多少天后自动归档. emotional_bond 建议 7 天.
        """
        if not bool(self.config.get("llm_note_enabled", True)):
            return "记忆工具当前已关闭, 没有保存任何内容."
        if not self.store.conn:
            self.store.open()
        if self._is_management(event) or not self.gate.matches(event):
            return "当前会话没有开启学习, 没有保存任何内容."
        target_id = self._target_for_event(event)
        if not target_id:
            return "当前会话不是已启用的学习目标, 没有保存任何内容."
        try:
            content = self._sanitize_tool_note(note)
            scope_value = str(scope or "owner").strip().lower()
            scope_value = {"user": "person", "member": "person"}.get(
                scope_value, scope_value
            )
            kind_value = str(kind or "profile_fact").strip().lower()
            if scope_value not in {value.value for value in ScopeType}:
                raise ValueError("scope is invalid")
            if kind_value not in {value.value for value in EntryKind}:
                raise ValueError("kind is invalid")
            if (
                scope_value == ScopeType.GLOBAL.value
                and kind_value != EntryKind.BEHAVIOR_RULE.value
            ):
                raise ValueError("global scope only accepts behavior_rule")
            if triggers is None:
                trigger_items = []
            elif isinstance(triggers, str):
                trigger_items = re.split(r"[,，\n\r]+", triggers)
            elif isinstance(triggers, list) and all(
                isinstance(value, str) for value in triggers
            ):
                # Keep direct Python callers and older tests compatible.
                trigger_items = triggers
            else:
                raise ValueError("triggers must be a comma-separated string")
            trigger_values = [
                value.strip()[:64] for value in trigger_items[:20] if value.strip()
            ]
            confidence_value = float(confidence)
            if not math.isfinite(confidence_value):
                raise ValueError("confidence is invalid")
            confidence_value = max(0.0, min(1.0, confidence_value))
            expires_days = int(str(expires_in_days or "0").strip())
            if expires_days < 0 or expires_days > 365:
                raise ValueError("expires_in_days must be between 0 and 365")
        except (TypeError, ValueError) as exc:
            return f"记忆候选未保存: {exc}"

        platform, _account, chat_type, peer, sender, _session = event_identity(event)
        owner_key = f"{platform}:user:{sender}"
        is_owner = owner_key in self._snapshot.owner_identities

        # 🆕 主人强意图快速通道
        strong_intent_pattern = r"(以后|永远|记住|不要再|一定要|必须|禁止)"
        has_strong_intent = re.search(strong_intent_pattern, content)

        if (
            is_owner
            and has_strong_intent
            and kind_value == EntryKind.BEHAVIOR_RULE.value
            and scope_value
            in {ScopeType.OWNER.value, ScopeType.TASK.value, ScopeType.GLOBAL.value}
        ):
            # 快速通道：跳过 Reviewer，立即生效
            if scope_value == ScopeType.TASK.value:
                if not trigger_values:
                    return "task 层级至少需要一个触发词, 没有保存任何内容."
                scope_key = trigger_values[0]
            else:
                scope_key = ""

            evidence = await self._tool_evidence_row(event, target_id)
            if not evidence:
                return "当前消息证据还未落账, 本次没有保存候选, 请稍后再试."

            expires_at = None
            if expires_days > 0:
                expires_at = (
                    datetime.now(timezone.utc) + timedelta(days=expires_days)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")

            conflict_key = self.pipeline._automatic_conflict_key(
                scope_value, scope_key, kind_value, content, ""
            )

            # 检查是否已存在相同冲突键的条目
            existing = (
                self.store._db()
                .execute(
                    "SELECT entry_id FROM entries WHERE conflict_key=? AND conflict_key!='' AND status!='archived'",
                    (conflict_key,),
                )
                .fetchone()
            )
            if existing:
                return "已记住"

            try:
                entry = self.store.save_entry(
                    {
                        "scope_type": scope_value,
                        "scope_key": scope_key,
                        "kind": kind_value,
                        "content": content,
                        "triggers": trigger_values,
                        "conflict_key": conflict_key,
                        "status": "active",
                        "trust_level": "owner_explicit",
                        "confidence": 1.0,
                        "evidence_count": 1,
                        "evidence_ids": [evidence["row_id"]],
                        "expires_at": expires_at,
                        "priority": 100,
                    },
                    actor_key="owner_explicit_fast",
                )

                self._refresh_snapshot()
                self.store.audit(
                    f"llm_tool_fast:{platform}:user:{sender}",
                    "save_entry_fast",
                    "entry",
                    entry["entry_id"],
                    {"trust": "owner_explicit", "scope": scope_value},
                )

                expire_hint = f" ({expires_days}天后过期)" if expires_days > 0 else ""
                return f"已记住{expire_hint}"
            except Exception as exc:
                logger.exception("[%s] fast path entry creation failed", PLUGIN_NAME)
                return f"写入失败: {exc}"

        # 原有审核流程
        if not is_owner and scope_value in {
            ScopeType.GLOBAL.value,
            ScopeType.OWNER.value,
            ScopeType.TASK.value,
        }:
            return "普通成员不能提交主人、全局或任务规则候选, 没有保存任何内容."
        if not is_owner and kind_value == EntryKind.BEHAVIOR_RULE.value:
            return "普通成员不能提交行为规则候选, 没有保存任何内容."
        if scope_value == ScopeType.GROUP.value:
            if chat_type != "group":
                return "group 层级只能在群聊中使用, 没有保存任何内容."
            scope_key = f"{platform}:group:{peer}"
        elif scope_value == ScopeType.PERSON.value:
            subject = str(subject_id or sender).strip()
            if not subject.isdigit() or (not is_owner and subject != sender):
                return "person 层级的 subject_id 不在当前权限范围内, 没有保存任何内容."
            participant = (
                self.store._db()
                .execute(
                    "SELECT 1 FROM conversation_messages WHERE target_id=? AND sender_key=? LIMIT 1",
                    (target_id, f"{platform}:user:{subject}"),
                )
                .fetchone()
            )
            if not participant and subject != sender:
                return "person 层级的 subject_id 不在当前会话证据中, 没有保存任何内容."
            scope_key = f"{platform}:user:{subject}"
        elif scope_value == ScopeType.TASK.value:
            if not trigger_values:
                return "task 层级至少需要一个触发词, 没有保存任何内容."
            scope_key = trigger_values[0]
        else:
            scope_key = ""

        evidence = await self._tool_evidence_row(event, target_id)
        if not evidence:
            return "当前消息证据还未落账, 本次没有保存候选, 请稍后再试."

        # 🆕 计算过期时间
        expires_at = None
        if expires_days > 0:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=expires_days)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

        proposal = {
            "scope_type": scope_value,
            "scope_key": scope_key,
            "kind": kind_value,
            "content": content,
            "triggers": trigger_values,
            "confidence": confidence_value,
            "evidence_ids": [evidence["row_id"]],
            "signal_type": "model_reflection",
            "conflict_key": self.pipeline._automatic_conflict_key(
                scope_value, scope_key, kind_value, content, ""
            ),
            "expires_at": expires_at,
        }
        candidate_key = json.dumps(
            {
                "target_id": target_id,
                "message_id": self._event_key(event),
                "proposal": proposal,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        candidate_id = hashlib.sha256(candidate_key.encode("utf-8")).hexdigest()
        row = self.store.create_tool_candidate(
            candidate_id,
            target_id,
            proposal,
            [evidence["row_id"]],
            confidence=confidence_value,
        )
        self.store.audit(
            f"llm_tool:{platform}:user:{sender}",
            "create_candidate",
            "candidate",
            candidate_id,
            {"target_id": target_id, "status": row["status"]},
        )
        if row.get("_duplicate") or row["status"] in {"committed", "rejected"}:
            return f"这条记忆候选之前已经处理过, 当前状态: {row['status']}."
        expire_hint = f", 设定 {expires_days} 天后过期" if expires_days > 0 else ""
        return f"已记录为待审核记忆候选 {candidate_id[:12]}, 将在下一次学习批次交给 Reviewer 筛选, 尚未写入正式记忆{expire_hint}."

    @filter.on_llm_request(priority=10**9)
    async def on_llm_request(self, event: Any, req: Any) -> None:
        if not HAS_WAITING_LLM_HOOK:
            await self.on_waiting_llm_request(event)
        text, entry_ids, tokens = render_injection(
            self._snapshot,
            event,
            int(self.config.get("injection_token_budget", 800) or 800),
        )
        if not text:
            return
        injected = False
        if hasattr(req, "extra_user_content_parts") and TextPart is not None:
            part = TextPart(text=text)
            if hasattr(part, "mark_as_temp"):
                part = part.mark_as_temp()
            req.extra_user_content_parts.append(part)
            injected = True
        elif hasattr(req, "system_prompt"):
            current = str(getattr(req, "system_prompt", "") or "").strip()
            req.system_prompt = f"{current}\n\n{text}".strip()
            injected = True
        elif hasattr(req, "prompt"):
            req.prompt = f"{getattr(req, 'prompt', '')}\n\n{text}".strip()
            injected = True
        if injected:
            session_id = event_identity(event)[-1] or self._event_key(event)
            try:
                self.store.record_injection(session_id, entry_ids, tokens)
            except Exception:
                logger.warning("[%s] failed to record injection audit", PLUGIN_NAME)

    @AGENT_COMPLETION_DECORATOR(priority=10**9)
    async def on_agent_done(self, event: Any, *args: Any) -> None:
        self._prune_anchor_state()
        origin = str(getattr(event, "unified_msg_origin", "") or self._event_key(event))
        handle = self._anchors.get(origin)
        response = args[-1] if args else None
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
            ("/astrbot_plugin_growth_memory/providers", ["GET"]),
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

    def _available_chat_providers(self) -> list[dict[str, str]]:
        getter = getattr(self.context, "get_all_providers", None)
        providers: Any = []
        if callable(getter):
            try:
                providers = getter() or []
            except Exception:
                logger.exception("[%s] failed to read chat providers", PLUGIN_NAME)
        if not providers:
            manager = getattr(self.context, "provider_manager", None)
            providers = getattr(manager, "provider_insts", []) or []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for provider in providers:
            try:
                meta = (
                    provider.meta()
                    if callable(getattr(provider, "meta", None))
                    else None
                )
                provider_id = str(getattr(meta, "id", "") or "").strip()
            except Exception:
                continue
            if not provider_id or provider_id in seen:
                continue
            seen.add(provider_id)
            result.append({"id": provider_id, "name": provider_id})
        return sorted(result, key=lambda item: item["id"].lower())

    async def web_api(self, **path_params: Any) -> Any:
        method = str(getattr(request, "method", "GET")).upper()
        path = str(getattr(request, "path", ""))
        web_context = globals().get("g")
        username = str(
            getattr(request, "username", "")
            or getattr(web_context, "username", "")
            or getattr(web_context, "user", "")
            or ""
        )
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
                            "entries": len(self._snapshot.entries),
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
            if path.endswith("/providers") and method == "GET":
                return json_response(self._available_chat_providers())
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
                                "llm_note_enabled",
                                "extractor_provider_id",
                                "reviewer_provider_id",
                                "daily_request_budget",
                                "daily_input_token_budget",
                                "daily_output_token_budget",
                                "learning_input_token_limit",
                                "learning_max_output_tokens",
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
        self._initialized = False


__all__ = ["GrowthMemory", "TargetCaptureFilter"]
