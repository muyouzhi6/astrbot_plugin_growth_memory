from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import astrbot_plugin_growth_memory.main as plugin_main
from astrbot_plugin_growth_memory.main import GrowthMemory, TargetCaptureFilter
from astrbot_plugin_growth_memory.prototype.growth_memory_core import (
    CaptureItemKind,
    LearningTarget,
    TargetChatType,
    TargetMatcher,
)
from astrbot_plugin_growth_memory.runtime import (
    BoundedCapture,
    CaptureGate,
    LearningPipeline,
    RuntimeSnapshot,
)
from astrbot_plugin_growth_memory.storage import GrowthStore


class FakeEvent:
    def __init__(self, text="hello", *, group_id="", sender="10001", message_id="m1"):
        self.message_str = text
        self.unified_msg_origin = (
            f"aiocqhttp:{'group' if group_id else 'private'}:{group_id or sender}"
        )
        self.message_obj = SimpleNamespace(
            self_id="bot", group_id=group_id, message_id=message_id, message=[]
        )
        self._sender = sender
        self.sent = []

    def get_platform_name(self):
        return "aiocqhttp"

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return "tester"

    async def send(self, text):
        self.sent.append(text)


class FakeContext:
    def __init__(self):
        self.registered_web_apis = []

    def register_web_api(self, route, handler, methods, desc):
        self.registered_web_apis.append((route, handler, methods, desc))


class FakeWebRequest:
    def __init__(self, method, path, body=None, username="admin"):
        self.method = method
        self.path = path
        self.username = username
        self._body = body

    async def json(self, default=None):
        return self._body if self._body is not None else default


class FakeLLMContext:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    async def llm_generate(self, **kwargs):
        self.calls += 1
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(role="assistant", completion_text=output)


def ready_anchor(store: GrowthStore, sender_key="aiocqhttp:user:10001"):
    target = store.upsert_target(
        {"platform": "aiocqhttp", "chat_type": "private", "peer_id": "10001"}
    )
    question = store.create_message(
        target["target_id"],
        direction="inbound",
        sender_key=sender_key,
        sender_name="owner",
        text="以后画图不要偏黄",
        session_id="s1",
        source="platform_inbound",
    )
    answer = store.create_message(
        target["target_id"],
        direction="outbound",
        sender_key="aiocqhttp:bot",
        sender_name="bot",
        text="知道了",
        session_id="s1",
        source="agent_final",
    )
    anchor = store.create_anchor(
        target["target_id"], question["row_id"], "2000-01-01T00:00:00Z"
    )
    store.update_anchor(
        anchor["anchor_id"],
        request_state="built",
        answer_state="generated",
        answer_row_id=answer["row_id"],
    )
    return anchor


class PluginRuntimeTests(unittest.TestCase):
    def test_target_capture_filter_is_default_off_and_exact(self):
        target = LearningTarget("aiocqhttp", "bot", TargetChatType.GROUP, "12345")
        gate = CaptureGate(
            RuntimeSnapshot(
                TargetMatcher((target,), capture_enabled=True), capture_enabled=True
            )
        )
        TargetCaptureFilter.gate = gate
        wrong_account = FakeEvent(group_id="12345")
        wrong_account.message_obj.self_id = "other-bot"
        self.assertFalse(TargetCaptureFilter().filter(wrong_account, None))
        self.assertTrue(TargetCaptureFilter().filter(FakeEvent(group_id="12345"), None))

    def test_bounded_capture_evicts_context_before_critical(self):
        queue = BoundedCapture(2)
        queue.admit(CaptureItemKind.ANCHOR_OPEN, "a0", "anchor0")
        queue.admit(CaptureItemKind.ANCHOR_OPEN, "a1", "anchor1")
        self.assertTrue(
            queue.admit(CaptureItemKind.ANCHOR_OPEN, "a2", "anchor2").critical_overflow
        )
        self.assertTrue(queue.degraded)

    def test_storage_target_schedule_entry_and_version(self):
        with tempfile.TemporaryDirectory() as td:
            store = GrowthStore(Path(td) / "db.sqlite")
            store.open()
            store.upsert_target(
                {"platform": "aiocqhttp", "chat_type": "private", "peer_id": "12345"}
            )
            self.assertEqual(store.upsert_schedule("03:00")["local_time"], "03:00")
            entry = store.save_entry(
                {
                    "scope_type": "task",
                    "scope_key": "drawing",
                    "kind": "behavior_rule",
                    "content": "避免偏黄画面",
                    "trust_level": "manual",
                    "status": "active",
                }
            )
            self.assertEqual(entry["version"], 1)
            entry2 = store.save_entry({**entry, "content": "避免偏黄和复古滤镜"})
            self.assertEqual(entry2["version"], 2)
            self.assertEqual(store.counts()["learning_targets"], 1)

    def test_anchor_lifecycle_and_no_provider_defers(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                context = FakeContext()
                plugin = GrowthMemory(
                    context,
                    {
                        "capture_enabled": True,
                        "owner_identities": ["aiocqhttp:user:10001"],
                    },
                )
                plugin.data_dir = Path(td)
                plugin.store = GrowthStore(Path(td) / "db.sqlite")
                plugin.capture = BoundedCapture(8)
                plugin.gate = CaptureGate()
                TargetCaptureFilter.gate = plugin.gate
                plugin.writer = __import__(
                    "astrbot_plugin_growth_memory.runtime", fromlist=["CaptureWriter"]
                ).CaptureWriter(plugin.store, plugin.capture)
                plugin.pipeline = __import__(
                    "astrbot_plugin_growth_memory.runtime",
                    fromlist=["LearningPipeline"],
                ).LearningPipeline(
                    plugin.context,
                    plugin.store,
                    plugin.config,
                    lambda: plugin.gate.snapshot,
                )
                plugin.store.open()
                plugin.store.upsert_target(
                    {
                        "platform": "aiocqhttp",
                        "chat_type": "private",
                        "peer_id": "10001",
                    }
                )
                plugin._refresh_snapshot()
                await plugin.writer.start()
                await plugin.pipeline.start()
                event = FakeEvent(message_id="question")
                await plugin.capture_message(event)
                await plugin.on_waiting_llm_request(event)
                await asyncio.sleep(0.2)
                self.assertEqual(
                    len(
                        plugin.store._db()
                        .execute("SELECT * FROM trigger_anchors")
                        .fetchall()
                    ),
                    1,
                )
                await plugin.on_agent_done(
                    event,
                    None,
                    SimpleNamespace(role="assistant", completion_text="final"),
                )
                await asyncio.sleep(0.2)
                anchor = (
                    plugin.store._db()
                    .execute("SELECT * FROM trigger_anchors")
                    .fetchone()
                )
                self.assertEqual(anchor["answer_state"], "generated")
                plugin.store.update_anchor(
                    anchor["anchor_id"], context_close_at="2000-01-01T00:00:00Z"
                )
                result = await plugin.pipeline.run_due(force=True)
                self.assertEqual(result["deferred"], 1)
                await plugin.terminate()

        asyncio.run(run())

    def test_delayed_anchor_keeps_completion_after_delivery_hook(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                plugin = GrowthMemory(
                    FakeContext(),
                    {
                        "capture_enabled": True,
                        "owner_identities": ["aiocqhttp:user:10001"],
                    },
                )
                plugin.store = GrowthStore(Path(td) / "db.sqlite")
                plugin.capture = BoundedCapture(8)
                plugin.gate = CaptureGate()
                TargetCaptureFilter.gate = plugin.gate
                plugin.writer = __import__(
                    "astrbot_plugin_growth_memory.runtime", fromlist=["CaptureWriter"]
                ).CaptureWriter(plugin.store, plugin.capture)
                plugin.pipeline = LearningPipeline(
                    plugin.context,
                    plugin.store,
                    plugin.config,
                    lambda: plugin.gate.snapshot,
                )
                plugin.store.open()
                plugin.store.upsert_target(
                    {
                        "platform": "aiocqhttp",
                        "chat_type": "private",
                        "peer_id": "10001",
                    }
                )
                plugin._refresh_snapshot()
                await plugin.writer.start()
                event = FakeEvent(message_id="delayed-question")
                await plugin.on_waiting_llm_request(event)
                await plugin.on_agent_done(
                    event,
                    None,
                    SimpleNamespace(role="assistant", completion_text="final"),
                )
                await plugin.after_message_sent(event)
                await asyncio.sleep(0.2)
                anchor = (
                    plugin.store._db()
                    .execute("SELECT * FROM trigger_anchors")
                    .fetchone()
                )
                self.assertEqual(anchor["answer_state"], "generated")
                await plugin.terminate()

        asyncio.run(run())

    def test_reviewer_cannot_promote_or_overwrite_manual_entry(self):
        self.assertEqual(
            LearningPipeline._server_trust("behavior_rule", "task", ["普通聊天"], 1, 1),
            "model_inference",
        )
        proposal_template = {
            "scope_type": "task",
            "scope_key": "drawing",
            "kind": "behavior_rule",
            "content": "模型试图改写人工规则",
            "triggers": ["画图"],
            "conflict_key": "drawing.color",
            "signal_type": "owner_explicit",
            "confidence": 1.0,
        }

        async def run():
            with tempfile.TemporaryDirectory() as td:
                store = GrowthStore(Path(td) / "db.sqlite")
                store.open()
                manual = store.save_entry(
                    {
                        **proposal_template,
                        "content": "人工规则保持不变",
                        "status": "active",
                        "trust_level": "manual",
                        "source_kind": "manual",
                    }
                )
                ready_anchor(store)
                proposal = json.dumps(
                    {**proposal_template, "target_entry_id": manual["entry_id"]},
                    ensure_ascii=False,
                )
                pipeline = LearningPipeline(
                    FakeLLMContext([f"[{proposal}]", f"[{proposal}]"]),
                    store,
                    {
                        "extractor_provider_id": "extract",
                        "reviewer_provider_id": "review",
                    },
                    lambda: RuntimeSnapshot(
                        TargetMatcher(),
                        owner_identities=frozenset({"aiocqhttp:user:10001"}),
                    ),
                )
                result = await pipeline.run_due(force=True)
                self.assertEqual(result["processed"], 1)
                current = next(
                    item
                    for item in store.entries()
                    if item["entry_id"] == manual["entry_id"]
                )
                self.assertEqual(current["content"], "人工规则保持不变")
                self.assertEqual(current["version"], 1)
                self.assertEqual(len(store.entries()), 1)

        asyncio.run(run())

    def test_anchor_hook_state_is_pruned_after_ttl(self):
        plugin = GrowthMemory(FakeContext(), {})
        plugin._anchors["old"] = SimpleNamespace(anchor_id="a")
        plugin._anchor_state_started_at["old"] = 0
        plugin._pending_completions["old"] = ("assistant", "text", "aiocqhttp", "s")
        plugin._prune_anchor_state()
        self.assertNotIn("old", plugin._anchors)
        self.assertNotIn("old", plugin._pending_completions)

    def test_plugin_reload_cleans_routes_and_tasks(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                plugin = GrowthMemory(FakeContext(), {})
                plugin.data_dir = Path(td)
                plugin.store = GrowthStore(Path(td) / "db.sqlite")
                plugin.capture = BoundedCapture(32)
                plugin.gate = CaptureGate()
                TargetCaptureFilter.gate = plugin.gate
                plugin.writer = __import__(
                    "astrbot_plugin_growth_memory.runtime", fromlist=["CaptureWriter"]
                ).CaptureWriter(plugin.store, plugin.capture)
                plugin.pipeline = LearningPipeline(
                    plugin.context,
                    plugin.store,
                    plugin.config,
                    lambda: plugin.gate.snapshot,
                    lambda: plugin.capture.degraded,
                )
                plugin.store.open()
                plugin._seed()
                plugin._refresh_snapshot()
                await plugin.writer.start()
                await plugin.pipeline.start()
                plugin._register_web_api()
                self.assertTrue(plugin.pipeline._ticker)
                await plugin.terminate()
                self.assertFalse(plugin.context.registered_web_apis)

        asyncio.run(run())

    def test_two_stage_learning_commits_owner_rule_with_budget(self):
        proposal = json.dumps(
            [
                {
                    "scope_type": "task",
                    "scope_key": "drawing",
                    "kind": "behavior_rule",
                    "content": "绘图避免偏黄画面",
                    "triggers": ["画图"],
                    "signal_type": "owner_explicit",
                    "confidence": 0.98,
                }
            ],
            ensure_ascii=False,
        )

        async def run():
            with tempfile.TemporaryDirectory() as td:
                store = GrowthStore(Path(td) / "db.sqlite")
                store.open()
                ready_anchor(store)
                context = FakeLLMContext([proposal, proposal])
                snapshot = RuntimeSnapshot(
                    TargetMatcher(),
                    owner_identities=frozenset({"aiocqhttp:user:10001"}),
                )
                pipeline = LearningPipeline(
                    context,
                    store,
                    {
                        "extractor_provider_id": "extract",
                        "reviewer_provider_id": "review",
                    },
                    lambda: snapshot,
                )
                result = await pipeline.run_due(force=True)
                self.assertEqual(result["processed"], 1)
                self.assertEqual(context.calls, 2)
                self.assertEqual(store.entries()[0]["status"], "active")
                budget = store._db().execute("SELECT * FROM daily_budget").fetchone()
                self.assertEqual(budget["request_count"], 2)

        asyncio.run(run())

    def test_reviewer_failure_does_not_rerun_extractor(self):
        proposal = json.dumps(
            [
                {
                    "scope_type": "owner",
                    "kind": "profile_fact",
                    "content": "喜欢简洁回答",
                    "signal_type": "owner_explicit",
                    "confidence": 0.9,
                }
            ],
            ensure_ascii=False,
        )

        async def run():
            with tempfile.TemporaryDirectory() as td:
                store = GrowthStore(Path(td) / "db.sqlite")
                store.open()
                ready_anchor(store)
                context = FakeLLMContext(
                    [proposal, RuntimeError("review down"), RuntimeError("review down")]
                )
                pipeline = LearningPipeline(
                    context,
                    store,
                    {
                        "extractor_provider_id": "extract",
                        "reviewer_provider_id": "review",
                    },
                    lambda: RuntimeSnapshot(TargetMatcher()),
                )
                first = await pipeline.run_due(force=True)
                self.assertEqual(first["deferred"], 1)
                self.assertEqual(context.calls, 3)
                second = await pipeline.run_due(force=True)
                self.assertEqual(second["processed"], 0)
                self.assertEqual(context.calls, 3)
                stages = (
                    store._db()
                    .execute("SELECT stage,status FROM learning_batches ORDER BY stage")
                    .fetchall()
                )
                self.assertEqual(
                    {(row["stage"], row["status"]) for row in stages},
                    {("extract", "succeeded"), ("review", "deferred")},
                )

        asyncio.run(run())

    def test_daily_budget_stops_reviewer_request(self):
        proposal = '[{"scope_type":"owner","kind":"profile_fact","content":"x"}]'

        async def run():
            with tempfile.TemporaryDirectory() as td:
                store = GrowthStore(Path(td) / "db.sqlite")
                store.open()
                ready_anchor(store)
                context = FakeLLMContext([proposal])
                pipeline = LearningPipeline(
                    context,
                    store,
                    {"extractor_provider_id": "extract", "daily_request_budget": 1},
                    lambda: RuntimeSnapshot(TargetMatcher()),
                )
                result = await pipeline.run_due(force=True)
                self.assertEqual(result["deferred"], 1)
                self.assertEqual(context.calls, 1)

        asyncio.run(run())

    def test_schedule_catch_up_is_idempotent(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                store = GrowthStore(Path(td) / "db.sqlite")
                store.open()
                store.upsert_schedule("03:00")
                pipeline = LearningPipeline(
                    FakeLLMContext([]),
                    store,
                    {},
                    lambda: RuntimeSnapshot(TargetMatcher()),
                )
                now = datetime(2026, 8, 8, 4, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
                first = await pipeline.run_due(now=now)
                second = await pipeline.run_due(now=now)
                self.assertEqual(first["scheduled"], 1)
                self.assertEqual(second["scheduled"], 0)
                run_kind = (
                    store._db()
                    .execute("SELECT run_kind FROM learning_runs")
                    .fetchone()[0]
                )
                self.assertEqual(run_kind, "catch_up")

        asyncio.run(run())

    def test_schedule_timezone_is_applied(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                store = GrowthStore(Path(td) / "db.sqlite")
                store.open()
                store.upsert_schedule("03:00", timezone="UTC")
                pipeline = LearningPipeline(
                    FakeLLMContext([]),
                    store,
                    {},
                    lambda: RuntimeSnapshot(TargetMatcher()),
                )
                now = datetime(2026, 8, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
                result = await pipeline.run_due(now=now)
                self.assertEqual(result["scheduled"], 0)

        asyncio.run(run())

    def test_invalid_runtime_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            GrowthMemory._validate_runtime_updates({"daily_request_budget": 0})
        with self.assertRaises(ValueError):
            GrowthMemory._validate_runtime_updates({"capture_enabled": "false"})

    def test_web_api_post_update_and_rollback(self):
        async def run():
            original_request = plugin_main.request
            try:
                with tempfile.TemporaryDirectory() as td:
                    plugin = GrowthMemory(FakeContext(), {})
                    plugin.store = GrowthStore(Path(td) / "db.sqlite")
                    plugin.store.open()
                    plugin._seed()
                    plugin._refresh_snapshot()

                    plugin_main.request = FakeWebRequest(
                        "POST",
                        "/api/plug/astrbot_plugin_growth_memory/targets",
                        {
                            "platform": "aiocqhttp",
                            "chat_type": "group",
                            "peer_id": "12345",
                        },
                    )
                    response = await plugin.web_api()
                    self.assertEqual(response.status_code, 201)

                    plugin_main.request = FakeWebRequest(
                        "POST",
                        "/api/plug/astrbot_plugin_growth_memory/schedules",
                        {"local_time": "04:30", "timezone": "Asia/Shanghai"},
                    )
                    schedule_response = await plugin.web_api()
                    schedule_id = schedule_response.data["schedule_id"]
                    self.assertEqual(schedule_response.status_code, 201)

                    plugin_main.request = FakeWebRequest(
                        "POST",
                        f"/api/plug/astrbot_plugin_growth_memory/schedules/{schedule_id}",
                        {"enabled": False},
                    )
                    toggled = await plugin.web_api(schedule_id=schedule_id)
                    self.assertFalse(toggled.data["enabled"])

                    plugin_main.request = FakeWebRequest(
                        "POST",
                        "/api/plug/astrbot_plugin_growth_memory/entries",
                        {
                            "scope_type": "task",
                            "scope_key": "drawing",
                            "kind": "behavior_rule",
                            "content": "避免偏黄",
                            "status": "active",
                        },
                    )
                    created = await plugin.web_api()
                    entry_id = created.data["entry_id"]

                    plugin_main.request = FakeWebRequest(
                        "POST",
                        f"/api/plug/astrbot_plugin_growth_memory/entries/{entry_id}",
                        {"content": "避免偏黄和复古滤镜"},
                    )
                    updated = await plugin.web_api(entry_id=entry_id)
                    self.assertEqual(updated.data["kind"], "behavior_rule")
                    self.assertEqual(updated.data["version"], 2)

                    plugin_main.request = FakeWebRequest(
                        "POST",
                        f"/api/plug/astrbot_plugin_growth_memory/entries/{entry_id}/rollback",
                        {"version": 1},
                    )
                    rolled = await plugin.web_api(entry_id=entry_id)
                    self.assertEqual(rolled.data["content"], "避免偏黄")
                    self.assertEqual(rolled.data["version"], 3)
            finally:
                plugin_main.request = original_request

        asyncio.run(run())

    def test_runtime_flags_survive_reload_and_schedule_cap(self):
        with tempfile.TemporaryDirectory() as td:
            store = GrowthStore(Path(td) / "db.sqlite")
            store.open()
            store.set_runtime_flag("capture_enabled", False, "admin")
            store.close()
            reopened = GrowthStore(Path(td) / "db.sqlite")
            reopened.open()
            self.assertFalse(reopened.runtime_flags()["capture_enabled"])
            for hour in range(8):
                reopened.upsert_schedule(f"{hour:02d}:00")
            with self.assertRaises(ValueError):
                reopened.upsert_schedule("08:00")

    def test_retention_keeps_uncommitted_anchor_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            store = GrowthStore(Path(td) / "db.sqlite")
            store.open()
            anchor = ready_anchor(store)
            unrelated = store.create_message(
                store.targets()[0]["target_id"],
                direction="inbound",
                sender_key="aiocqhttp:user:10002",
                sender_name="other",
                text="old",
                session_id="s1",
                source="platform_inbound",
            )
            store._db().execute(
                "UPDATE conversation_messages SET expires_at='2000-01-01T00:00:00Z'"
            )
            store._db().commit()
            store.cleanup_expired_messages()
            self.assertIsNotNone(
                store._db()
                .execute(
                    "SELECT row_id FROM conversation_messages WHERE row_id=?",
                    (anchor["question_row_id"],),
                )
                .fetchone()
            )
            self.assertIsNone(
                store._db()
                .execute(
                    "SELECT row_id FROM conversation_messages WHERE row_id=?",
                    (unrelated["row_id"],),
                )
                .fetchone()
            )

    def test_injection_is_independent_from_learning_target(self):
        class FakeTextPart:
            def __init__(self, text):
                self.text = text
                self.temporary = False

            def mark_as_temp(self):
                self.temporary = True
                return self

        async def run():
            original_text_part = plugin_main.TextPart
            try:
                with tempfile.TemporaryDirectory() as td:
                    plugin = GrowthMemory(FakeContext(), {"capture_enabled": True})
                    plugin.store = GrowthStore(Path(td) / "db.sqlite")
                    plugin.store.open()
                    plugin.store.save_entry(
                        {
                            "scope_type": "task",
                            "scope_key": "drawing",
                            "kind": "behavior_rule",
                            "content": "绘图避免偏黄",
                            "triggers": ["画图"],
                            "status": "active",
                            "trust_level": "manual",
                            "confidence": 1,
                            "visibility": "behavior_only",
                        }
                    )
                    plugin._refresh_snapshot()
                    plugin_main.TextPart = FakeTextPart
                    request = SimpleNamespace(extra_user_content_parts=[])
                    await plugin.on_llm_request(FakeEvent(text="帮我画图"), request)
                    self.assertEqual(len(request.extra_user_content_parts), 1)
                    self.assertTrue(request.extra_user_content_parts[0].temporary)
            finally:
                plugin_main.TextPart = original_text_part

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
