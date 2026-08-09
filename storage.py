from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_targets(
 target_id TEXT PRIMARY KEY, target_key TEXT UNIQUE NOT NULL, platform TEXT NOT NULL,
 account_id TEXT NOT NULL DEFAULT '', chat_type TEXT NOT NULL CHECK(chat_type IN ('private','group')),
 peer_id TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
 source TEXT NOT NULL DEFAULT 'page', next_message_seq INTEGER NOT NULL DEFAULT 1,
 next_anchor_seq INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_messages(
 row_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, message_seq INTEGER NOT NULL,
 session_id TEXT NOT NULL, platform_message_id TEXT, direction TEXT NOT NULL,
 sender_key TEXT NOT NULL, sender_name TEXT NOT NULL DEFAULT '', normalized_text TEXT NOT NULL DEFAULT '',
 components_json TEXT NOT NULL DEFAULT '[]', reply_to_message_id TEXT, content_hash TEXT NOT NULL,
 content_source TEXT NOT NULL, delivery_state TEXT NOT NULL DEFAULT 'not_applicable',
 occurred_at TEXT NOT NULL, captured_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 UNIQUE(target_id,message_seq), FOREIGN KEY(target_id) REFERENCES learning_targets(target_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_messages_target_seq ON conversation_messages(target_id,message_seq);
CREATE TABLE IF NOT EXISTS trigger_anchors(
 anchor_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, question_row_id TEXT NOT NULL,
 answer_row_id TEXT, anchor_seq INTEGER NOT NULL, context_close_at TEXT NOT NULL,
 request_state TEXT NOT NULL, answer_state TEXT NOT NULL, answer_source TEXT,
 delivery_state TEXT NOT NULL DEFAULT 'unknown', status TEXT NOT NULL DEFAULT 'open',
 claimed_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(target_id,question_row_id), UNIQUE(target_id,anchor_seq),
 FOREIGN KEY(target_id) REFERENCES learning_targets(target_id) ON DELETE CASCADE,
 FOREIGN KEY(question_row_id) REFERENCES conversation_messages(row_id) ON DELETE RESTRICT);
CREATE INDEX IF NOT EXISTS idx_anchors_ready ON trigger_anchors(status,context_close_at,target_id,anchor_seq);
CREATE TABLE IF NOT EXISTS entries(
 entry_id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_key TEXT NOT NULL DEFAULT '',
 kind TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
 triggers_json TEXT NOT NULL DEFAULT '[]', conflict_key TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL, trust_level TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0,
 priority INTEGER NOT NULL DEFAULT 0, visibility TEXT NOT NULL DEFAULT 'public',
 evidence_count INTEGER NOT NULL DEFAULT 0, evidence_days INTEGER NOT NULL DEFAULT 0,
 evidence_dates_json TEXT NOT NULL DEFAULT '[]', expires_at TEXT, version INTEGER NOT NULL DEFAULT 1,
 source_kind TEXT NOT NULL DEFAULT 'manual', content_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_entries_runtime ON entries(status,scope_type,scope_key,priority DESC);
CREATE TABLE IF NOT EXISTS entry_versions(
 version_id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL, version INTEGER NOT NULL,
 snapshot_json TEXT NOT NULL, mutation_kind TEXT NOT NULL, actor_key TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(entry_id,version),
 FOREIGN KEY(entry_id) REFERENCES entries(entry_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS evidence(
 evidence_id TEXT PRIMARY KEY, source_message_row_id TEXT, source_session_id TEXT NOT NULL,
 actor_key TEXT NOT NULL, target_scope_type TEXT NOT NULL, target_scope_key TEXT NOT NULL DEFAULT '',
 signal_type TEXT NOT NULL, excerpt TEXT NOT NULL, excerpt_hash TEXT NOT NULL,
 observed_at TEXT NOT NULL, expires_at TEXT);
CREATE TABLE IF NOT EXISTS candidates(
 candidate_id TEXT PRIMARY KEY, run_id TEXT, target_id TEXT, proposal_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', confidence REAL NOT NULL DEFAULT 0,
 proposed_by TEXT NOT NULL, rejection_reason TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL DEFAULT 0, review_lease_until TEXT, review_run_id TEXT,
 last_error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidate_evidence(candidate_id TEXT NOT NULL,evidence_id TEXT NOT NULL,PRIMARY KEY(candidate_id,evidence_id));
CREATE TABLE IF NOT EXISTS learning_schedules(
 schedule_id TEXT PRIMARY KEY, timezone TEXT NOT NULL, local_time TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, next_run_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(timezone,local_time));
CREATE TABLE IF NOT EXISTS learning_runs(
 run_id TEXT PRIMARY KEY, slot_key TEXT UNIQUE NOT NULL, run_kind TEXT NOT NULL,
 cutoff_at TEXT NOT NULL, status TEXT NOT NULL, request_count INTEGER NOT NULL DEFAULT 0,
 input_tokens_estimated INTEGER NOT NULL DEFAULT 0, output_tokens_actual INTEGER NOT NULL DEFAULT 0,
 started_at TEXT, completed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_batches(
 batch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, target_id TEXT NOT NULL, stage TEXT NOT NULL,
 batch_index INTEGER NOT NULL, dedupe_key TEXT UNIQUE NOT NULL, input_refs_json TEXT NOT NULL,
 output_json TEXT, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 not_before TEXT NOT NULL, lease_until TEXT, last_error_code TEXT NOT NULL DEFAULT '', last_error_hash TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(run_id,target_id,stage,batch_index));
CREATE TABLE IF NOT EXISTS staged_proposals(
 proposal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, batch_id TEXT NOT NULL, target_id TEXT NOT NULL,
 proposal_json TEXT NOT NULL, proposal_hash TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'pending',
 reviewer_decision_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(batch_id,proposal_hash));
CREATE TABLE IF NOT EXISTS entry_evidence_batches(
 entry_id TEXT NOT NULL, batch_id TEXT NOT NULL, evidence_count INTEGER NOT NULL,
 evidence_dates_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
 PRIMARY KEY(entry_id,batch_id));
CREATE TABLE IF NOT EXISTS entry_evidence_messages(
 entry_id TEXT NOT NULL, message_row_id TEXT NOT NULL, observed_date TEXT NOT NULL,
 batch_id TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(entry_id,message_row_id));
CREATE INDEX IF NOT EXISTS idx_entry_evidence_messages_batch
 ON entry_evidence_messages(entry_id,batch_id);
CREATE TABLE IF NOT EXISTS target_checkpoints(
 target_id TEXT PRIMARY KEY, committed_anchor_seq INTEGER NOT NULL DEFAULT 0,
 last_successful_run_id TEXT, last_successful_at TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily_budget(
 budget_date TEXT PRIMARY KEY, request_count INTEGER NOT NULL DEFAULT 0,
 input_tokens_estimated INTEGER NOT NULL DEFAULT 0, output_tokens_actual INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runtime_flags(key TEXT PRIMARY KEY,value_json TEXT NOT NULL,actor_key TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS injection_audit(audit_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,entry_ids_json TEXT NOT NULL,estimated_tokens INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_injection_audit_time ON injection_audit(created_at);
CREATE TABLE IF NOT EXISTS audit_log(audit_id TEXT PRIMARY KEY,actor_key TEXT NOT NULL,action TEXT NOT NULL,object_type TEXT NOT NULL,object_id TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def future_iso(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class GrowthStore:
    """Small SQLite service. All writes are serialized by the caller's worker lock."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def open(self) -> None:
        if self.conn is not None:
            return
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        columns = {
            str(row[1])
            for row in self.conn.execute("PRAGMA table_info(entries)").fetchall()
        }
        if "evidence_days" not in columns:
            self.conn.execute(
                "ALTER TABLE entries ADD COLUMN evidence_days INTEGER NOT NULL DEFAULT 0"
            )
        if "evidence_dates_json" not in columns:
            self.conn.execute(
                "ALTER TABLE entries ADD COLUMN evidence_dates_json TEXT NOT NULL DEFAULT '[]'"
            )
        candidate_columns = {
            str(row[1])
            for row in self.conn.execute("PRAGMA table_info(candidates)").fetchall()
        }
        for name, definition in (
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("review_lease_until", "TEXT"),
            ("review_run_id", "TEXT"),
            ("last_error_code", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in candidate_columns:
                self.conn.execute(
                    f"ALTER TABLE candidates ADD COLUMN {name} {definition}"
                )
        self.conn.commit()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _db(self) -> sqlite3.Connection:
        self.open()
        assert self.conn is not None
        return self.conn

    def targets(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._db()
            .execute("SELECT * FROM learning_targets ORDER BY created_at")
            .fetchall()
        ]

    def upsert_target(
        self, target: dict[str, Any], source: str = "page"
    ) -> dict[str, Any]:
        db = self._db()
        stamp = now_iso()
        target_id = str(target.get("target_id") or uuid.uuid4())
        platform = str(target.get("platform", "aiocqhttp")).strip().lower()
        account_id = str(target.get("account_id", "")).strip()
        chat_type = str(target.get("chat_type", "")).strip().lower()
        peer_id = str(target.get("peer_id", "")).strip()
        if (
            platform != "aiocqhttp"
            or chat_type not in {"private", "group"}
            or not peer_id.isdigit()
            or not 5 <= len(peer_id) <= 20
        ):
            raise ValueError(
                "target must be aiocqhttp, private/group and a 5-20 digit peer_id"
            )
        key = f"{platform}:{account_id or '*'}:{chat_type}:{peer_id}"
        db.execute(
            """INSERT INTO learning_targets(target_id,target_key,platform,account_id,chat_type,peer_id,label,enabled,source,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(target_key) DO UPDATE SET label=excluded.label,enabled=excluded.enabled,source=excluded.source,updated_at=excluded.updated_at""",
            (
                target_id,
                key,
                platform,
                account_id,
                chat_type,
                peer_id,
                str(target.get("label", ""))[:80],
                1 if target.get("enabled", True) else 0,
                source,
                stamp,
                stamp,
            ),
        )
        db.commit()
        return dict(
            db.execute(
                "SELECT * FROM learning_targets WHERE target_key=?", (key,)
            ).fetchone()
        )

    def set_target_enabled(self, target_id: str, enabled: bool) -> None:
        self._db().execute(
            "UPDATE learning_targets SET enabled=?,updated_at=? WHERE target_id=?",
            (int(enabled), now_iso(), target_id),
        )
        self._db().commit()

    def schedules(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._db()
            .execute("SELECT * FROM learning_schedules ORDER BY local_time")
            .fetchall()
        ]

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        db = self._db()
        db.execute(
            "UPDATE learning_schedules SET enabled=?,updated_at=? WHERE schedule_id=?",
            (int(enabled), now_iso(), schedule_id),
        )
        db.commit()

    def update_schedule(
        self, schedule_id: str, local_time: str, timezone: str, enabled: bool
    ) -> dict[str, Any]:
        self._validate_schedule(local_time, timezone)
        with self._lock:
            db = self._db()
            if not db.execute(
                "SELECT 1 FROM learning_schedules WHERE schedule_id=?", (schedule_id,)
            ).fetchone():
                raise ValueError("schedule not found")
            try:
                db.execute(
                    "UPDATE learning_schedules SET timezone=?,local_time=?,enabled=?,updated_at=? WHERE schedule_id=?",
                    (timezone, local_time, int(enabled), now_iso(), schedule_id),
                )
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ValueError("schedule time already exists") from exc
            return dict(
                db.execute(
                    "SELECT * FROM learning_schedules WHERE schedule_id=?",
                    (schedule_id,),
                ).fetchone()
            )

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._lock:
            db = self._db()
            if (
                int(db.execute("SELECT COUNT(*) FROM learning_schedules").fetchone()[0])
                <= 1
            ):
                raise ValueError(
                    "at least one learning schedule is required; disable it instead"
                )
            deleted = db.execute(
                "DELETE FROM learning_schedules WHERE schedule_id=?", (schedule_id,)
            ).rowcount
            db.commit()
            return bool(deleted)

    @staticmethod
    def _validate_schedule(local_time: str, timezone: str) -> None:
        if not isinstance(timezone, str) or not timezone.strip():
            raise ValueError("timezone must not be empty")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("invalid timezone") from exc
        if (
            not isinstance(local_time, str)
            or len(local_time) != 5
            or local_time[2] != ":"
        ):
            raise ValueError("local_time must be HH:MM")
        hour, minute = local_time.split(":")
        if not (
            hour.isdigit()
            and minute.isdigit()
            and 0 <= int(hour) < 24
            and 0 <= int(minute) < 60
        ):
            raise ValueError("invalid local_time")

    def upsert_schedule(
        self,
        local_time: str,
        timezone: str = "Asia/Shanghai",
        enabled: bool = True,
        schedule_id: str | None = None,
    ) -> dict[str, Any]:
        self._validate_schedule(local_time, timezone)
        db = self._db()
        stamp = now_iso()
        sid = schedule_id or str(uuid.uuid4())
        existing = db.execute(
            "SELECT schedule_id FROM learning_schedules WHERE timezone=? AND local_time=?",
            (timezone, local_time),
        ).fetchone()
        if not existing:
            count = int(
                db.execute("SELECT COUNT(*) FROM learning_schedules").fetchone()[0]
            )
            if count >= 8:
                raise ValueError("at most 8 learning schedules are allowed")
        db.execute(
            """INSERT INTO learning_schedules(schedule_id,timezone,local_time,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?)
          ON CONFLICT(timezone,local_time) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at""",
            (sid, timezone, local_time, int(enabled), stamp, stamp),
        )
        db.commit()
        return dict(
            db.execute(
                "SELECT * FROM learning_schedules WHERE timezone=? AND local_time=?",
                (timezone, local_time),
            ).fetchone()
        )

    def create_message(
        self,
        target_id: str,
        *,
        direction: str,
        sender_key: str,
        sender_name: str,
        text: str,
        session_id: str,
        source: str,
        platform_message_id: str | None = None,
        delivery_state: str = "not_applicable",
    ) -> dict[str, Any]:
        with self._lock:
            db = self._db()
            stamp = now_iso()
            row_id = str(uuid.uuid4())
            original = text
            digest = __import__("hashlib").sha256(original.encode("utf-8")).hexdigest()
            if len(original) > 4000:
                marker = f"\n[truncated length={len(original)} sha256={digest[:16]}]\n"
                text = original[:1950] + marker + original[-1950:]
            row = db.execute(
                "SELECT COALESCE(MAX(message_seq),0)+1 FROM conversation_messages WHERE target_id=?",
                (target_id,),
            ).fetchone()
            seq = int(row[0])
            db.execute(
                "INSERT INTO conversation_messages(row_id,target_id,message_seq,session_id,platform_message_id,direction,sender_key,sender_name,normalized_text,content_hash,content_source,delivery_state,occurred_at,captured_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    target_id,
                    seq,
                    session_id,
                    platform_message_id,
                    direction,
                    sender_key,
                    sender_name,
                    text,
                    digest,
                    source,
                    delivery_state,
                    stamp,
                    stamp,
                    future_iso(14),
                ),
            )
            db.commit()
            return dict(
                db.execute(
                    "SELECT * FROM conversation_messages WHERE row_id=?", (row_id,)
                ).fetchone()
            )

    def message_window(
        self, target_id: str, question_seq: int, radius: int = 10
    ) -> list[dict[str, Any]]:
        lo = max(1, question_seq - radius)
        hi = question_seq + radius
        return [
            dict(r)
            for r in self._db()
            .execute(
                "SELECT * FROM conversation_messages WHERE target_id=? AND message_seq BETWEEN ? AND ? ORDER BY message_seq",
                (target_id, lo, hi),
            )
            .fetchall()
        ]

    def message_by_platform_id(
        self, target_id: str, platform_message_id: str
    ) -> dict[str, Any] | None:
        row = (
            self._db()
            .execute(
                "SELECT * FROM conversation_messages WHERE target_id=? AND platform_message_id=? "
                "AND direction='inbound' ORDER BY message_seq LIMIT 1",
                (target_id, platform_message_id),
            )
            .fetchone()
        )
        return dict(row) if row else None

    def create_anchor(
        self, target_id: str, question_row_id: str, close_at: str
    ) -> dict[str, Any]:
        with self._lock:
            db = self._db()
            stamp = now_iso()
            seq = int(
                db.execute(
                    "SELECT COALESCE(MAX(anchor_seq),0)+1 FROM trigger_anchors WHERE target_id=?",
                    (target_id,),
                ).fetchone()[0]
            )
            aid = str(uuid.uuid4())
            db.execute(
                "INSERT OR IGNORE INTO trigger_anchors(anchor_id,target_id,question_row_id,anchor_seq,context_close_at,request_state,answer_state,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    target_id,
                    question_row_id,
                    seq,
                    close_at,
                    "preparing",
                    "missing",
                    "open",
                    stamp,
                    stamp,
                ),
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM trigger_anchors WHERE target_id=? AND question_row_id=?",
                (target_id, question_row_id),
            ).fetchone()
            return dict(row)

    def update_anchor(self, anchor_id: str, **fields: Any) -> None:
        if not fields:
            return
        with self._lock:
            fields["updated_at"] = now_iso()
            db = self._db()
            clause = ",".join(f"{k}=?" for k in fields)
            db.execute(
                f"UPDATE trigger_anchors SET {clause} WHERE anchor_id=?",
                (*fields.values(), anchor_id),
            )
            db.commit()

    def close_mature_anchors(self, target_id: str, current_message_seq: int) -> None:
        with self._lock:
            db = self._db()
            stamp = now_iso()
            db.execute(
                "UPDATE trigger_anchors SET context_close_at=?,updated_at=? "
                "WHERE target_id=? AND status='open' AND answer_state='generated' "
                "AND answer_row_id IN (SELECT row_id FROM conversation_messages "
                "WHERE target_id=? AND message_seq<=?)",
                (stamp, stamp, target_id, target_id, current_message_seq - 10),
            )
            db.commit()

    def ready_anchors(self, cutoff: str, limit: int = 100) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._db()
            .execute(
                "SELECT * FROM trigger_anchors WHERE status IN ('open','retryable') AND request_state='built' AND answer_state='generated' AND context_close_at<=? ORDER BY target_id,anchor_seq LIMIT ?",
                (cutoff, limit),
            )
            .fetchall()
        ]

    def entries(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        where = (
            ""
            if include_archived
            else "WHERE status != 'archived' AND (expires_at IS NULL OR expires_at>?)"
        )
        params = () if include_archived else (now_iso(),)
        return [
            dict(r)
            for r in self._db()
            .execute(f"SELECT * FROM entries {where} ORDER BY updated_at DESC", params)
            .fetchall()
        ]

    def create_tool_candidate(
        self,
        candidate_id: str,
        target_id: str,
        proposal: dict[str, Any],
        evidence_ids: list[str],
        *,
        confidence: float,
    ) -> dict[str, Any]:
        """Persist an LLM-suggested note without activating an entry."""
        if not candidate_id or not target_id or not isinstance(proposal, dict):
            raise ValueError("candidate fields are invalid")
        evidence = list(dict.fromkeys(str(value) for value in evidence_ids if value))[
            :50
        ]
        if not evidence:
            raise ValueError("candidate requires evidence")
        with self._lock:
            db = self._db()
            stamp = now_iso()
            existing = db.execute(
                "SELECT 1 FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            db.execute(
                "INSERT OR IGNORE INTO candidates(candidate_id,run_id,target_id,proposal_json,status,confidence,proposed_by,rejection_reason,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    None,
                    target_id,
                    json.dumps(proposal, ensure_ascii=False),
                    "pending",
                    max(0.0, min(1.0, float(confidence))),
                    "llm_tool",
                    "",
                    stamp,
                    stamp,
                ),
            )
            db.executemany(
                "INSERT OR IGNORE INTO candidate_evidence(candidate_id,evidence_id) VALUES(?,?)",
                [(candidate_id, value) for value in evidence],
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if not row:
                raise RuntimeError("candidate persistence failed")
            result = dict(row)
            result["_duplicate"] = bool(existing)
            return result

    def pending_tool_candidate_count(self) -> int:
        return int(
            self._db()
            .execute(
                "SELECT COUNT(*) FROM candidates WHERE proposed_by='llm_tool' AND status IN ('pending','deferred')"
            )
            .fetchone()[0]
        )

    def claim_tool_candidates(
        self, run_id: str, *, limit: int = 20, lease_seconds: int = 300
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._lock:
            db = self._db()
            stamp = now_iso()
            db.execute(
                "UPDATE candidates SET status='pending',review_lease_until=NULL,review_run_id=NULL,updated_at=? "
                "WHERE proposed_by='llm_tool' AND status='reviewing' AND review_lease_until IS NOT NULL AND review_lease_until<=?",
                (stamp, stamp),
            )
            rows = db.execute(
                "SELECT * FROM candidates WHERE proposed_by='llm_tool' AND status IN ('pending','deferred') "
                "ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
            if not rows:
                db.commit()
                return []
            lease_until = (
                datetime.now(timezone.utc) + timedelta(seconds=max(30, lease_seconds))
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            ids = [str(row["candidate_id"]) for row in rows]
            db.executemany(
                "UPDATE candidates SET status='reviewing',attempts=attempts+1,review_lease_until=?,review_run_id=?,updated_at=? "
                "WHERE candidate_id=? AND proposed_by='llm_tool' AND status IN ('pending','deferred')",
                [(lease_until, run_id, stamp, candidate_id) for candidate_id in ids],
            )
            db.commit()
            claimed = db.execute(
                "SELECT * FROM candidates WHERE review_run_id=? AND status='reviewing' ORDER BY created_at",
                (run_id,),
            ).fetchall()
            return [dict(row) for row in claimed]

    def candidate_evidence_ids(self, candidate_id: str) -> list[str]:
        return [
            str(row[0])
            for row in self._db()
            .execute(
                "SELECT evidence_id FROM candidate_evidence WHERE candidate_id=? ORDER BY evidence_id",
                (candidate_id,),
            )
            .fetchall()
        ]

    def finish_tool_candidate(
        self,
        candidate_id: str,
        run_id: str,
        status: str,
        *,
        reason: str = "",
    ) -> bool:
        if status not in {"committed", "rejected", "deferred"}:
            raise ValueError("invalid candidate status")
        with self._lock:
            updated = (
                self._db()
                .execute(
                    "UPDATE candidates SET status=?,rejection_reason=?,last_error_code=?,review_lease_until=NULL,updated_at=? "
                    "WHERE candidate_id=? AND proposed_by='llm_tool' AND status='reviewing' AND review_run_id=?",
                    (
                        status,
                        reason[:400],
                        reason[:120] if status == "deferred" else "",
                        now_iso(),
                        candidate_id,
                        run_id,
                    ),
                )
                .rowcount
            )
            self._db().commit()
            return bool(updated)

    def entry_versions(self, entry_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._db()
            .execute(
                "SELECT version,mutation_kind,actor_key,reason,created_at FROM entry_versions "
                "WHERE entry_id=? ORDER BY version DESC",
                (entry_id,),
            )
            .fetchall()
        ]

    def save_entry(
        self, data: dict[str, Any], actor_key: str = "manual", reason: str = ""
    ) -> dict[str, Any]:
        db = self._db()
        stamp = now_iso()
        eid = str(data.get("entry_id") or uuid.uuid4())
        current = db.execute(
            "SELECT version FROM entries WHERE entry_id=?", (eid,)
        ).fetchone()
        version = int(current[0]) + 1 if current else 1
        content = str(data.get("content", "")).strip()[:4000]
        if not content:
            raise ValueError("content must not be empty")
        scope_type = str(data.get("scope_type", "owner"))
        kind = str(data.get("kind", "profile_fact"))
        status = str(data.get("status", "active"))
        trust_level = str(data.get("trust_level", data.get("trust", "manual")))
        visibility = str(data.get("visibility", "public"))
        if scope_type not in {"global", "owner", "task", "group", "person"}:
            raise ValueError("invalid scope_type")
        if kind not in {"behavior_rule", "profile_fact", "milestone"}:
            raise ValueError("invalid kind")
        if status not in {"draft", "trial", "active", "suspended", "archived"}:
            raise ValueError("invalid status")
        if trust_level not in {
            "model_inference",
            "repeated_observation",
            "owner_correction",
            "owner_explicit",
            "manual",
        }:
            raise ValueError("invalid trust_level")
        if visibility not in {"public", "owner_only", "behavior_only"}:
            raise ValueError("invalid visibility")
        if kind == "behavior_rule" and trust_level not in {
            "owner_correction",
            "owner_explicit",
            "manual",
        }:
            raise ValueError("untrusted evidence cannot create behavior rules")
        if (
            scope_type in {"global", "task"}
            and status in {"trial", "active"}
            and trust_level not in {"owner_correction", "owner_explicit", "manual"}
        ):
            raise ValueError(
                "untrusted evidence cannot activate global or task entries"
            )
        raw_triggers = data.get("triggers", [])
        if not isinstance(raw_triggers, list) or any(
            not isinstance(value, str) for value in raw_triggers
        ):
            raise ValueError("triggers must be a list of strings")
        triggers = json.dumps(
            [value.strip()[:64] for value in raw_triggers[:20] if value.strip()],
            ensure_ascii=False,
        )
        raw_evidence_dates = data.get("evidence_dates")
        if raw_evidence_dates is None:
            try:
                raw_evidence_dates = json.loads(
                    str(data.get("evidence_dates_json", "[]"))
                )
            except (TypeError, ValueError):
                raw_evidence_dates = []
        if not isinstance(raw_evidence_dates, list) or any(
            not isinstance(value, str) for value in raw_evidence_dates
        ):
            raise ValueError("evidence_dates must be a list of strings")
        evidence_dates = sorted(
            {value.strip()[:10] for value in raw_evidence_dates if value.strip()}
        )
        digest = __import__("hashlib").sha256(content.encode("utf-8")).hexdigest()
        params = (
            eid,
            scope_type,
            str(data.get("scope_key", ""))[:120],
            kind,
            str(data.get("title", ""))[:120],
            content,
            triggers,
            str(data.get("conflict_key", ""))[:120],
            status,
            trust_level,
            max(0, min(1, float(data.get("confidence", 1)))),
            int(data.get("priority", 0)),
            visibility,
            int(data.get("evidence_count", 0)),
            int(data.get("evidence_days", len(evidence_dates))),
            json.dumps(evidence_dates, ensure_ascii=False),
            data.get("expires_at"),
            version,
            str(data.get("source_kind", "manual")),
            digest,
            stamp,
            stamp,
        )
        db.execute(
            """INSERT INTO entries(entry_id,scope_type,scope_key,kind,title,content,triggers_json,conflict_key,status,trust_level,confidence,priority,visibility,evidence_count,evidence_days,evidence_dates_json,expires_at,version,source_kind,content_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(entry_id) DO UPDATE SET scope_type=excluded.scope_type,scope_key=excluded.scope_key,kind=excluded.kind,title=excluded.title,content=excluded.content,triggers_json=excluded.triggers_json,conflict_key=excluded.conflict_key,status=excluded.status,trust_level=excluded.trust_level,confidence=excluded.confidence,priority=excluded.priority,visibility=excluded.visibility,evidence_count=excluded.evidence_count,evidence_days=excluded.evidence_days,evidence_dates_json=excluded.evidence_dates_json,expires_at=excluded.expires_at,version=excluded.version,source_kind=excluded.source_kind,content_hash=excluded.content_hash,updated_at=excluded.updated_at""",
            params,
        )
        db.execute(
            "INSERT INTO entry_versions(entry_id,version,snapshot_json,mutation_kind,actor_key,reason,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                eid,
                version,
                json.dumps(
                    dict(data, entry_id=eid, version=version), ensure_ascii=False
                ),
                "upsert",
                actor_key,
                reason,
                stamp,
            ),
        )
        db.commit()
        return dict(
            db.execute("SELECT * FROM entries WHERE entry_id=?", (eid,)).fetchone()
        )

    def register_entry_evidence(
        self,
        entry_id: str,
        batch_id: str,
        evidence_messages: list[tuple[str, str]],
        base_count: int = 0,
        base_dates: list[str] | None = None,
    ) -> tuple[int, list[str]]:
        messages = {
            str(message_id): str(observed_date)[:10]
            for message_id, observed_date in evidence_messages
            if message_id and observed_date
        }
        with self._lock:
            db = self._db()
            if (
                not db.execute(
                    "SELECT 1 FROM entry_evidence_batches WHERE entry_id=? LIMIT 1",
                    (entry_id,),
                ).fetchone()
                and not db.execute(
                    "SELECT 1 FROM entry_evidence_messages WHERE entry_id=? LIMIT 1",
                    (entry_id,),
                ).fetchone()
                and (base_count > 0 or base_dates)
            ):
                db.execute(
                    "INSERT OR IGNORE INTO entry_evidence_batches(entry_id,batch_id,evidence_count,evidence_dates_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        entry_id,
                        f"legacy:{entry_id}",
                        max(0, int(base_count)),
                        json.dumps(sorted(set(base_dates or [])), ensure_ascii=False),
                        now_iso(),
                    ),
                )
            db.execute(
                "INSERT OR IGNORE INTO entry_evidence_batches(entry_id,batch_id,evidence_count,evidence_dates_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    entry_id,
                    batch_id,
                    0,
                    "[]",
                    now_iso(),
                ),
            )
            db.executemany(
                "INSERT OR IGNORE INTO entry_evidence_messages(entry_id,message_row_id,observed_date,batch_id,created_at) "
                "VALUES(?,?,?,?,?)",
                [
                    (entry_id, message_id, date, batch_id, now_iso())
                    for message_id, date in messages.items()
                ],
            )
            legacy_rows = db.execute(
                "SELECT evidence_count,evidence_dates_json FROM entry_evidence_batches WHERE entry_id=?",
                (entry_id,),
            ).fetchall()
            message_rows = db.execute(
                "SELECT observed_date FROM entry_evidence_messages WHERE entry_id=?",
                (entry_id,),
            ).fetchall()
            db.commit()
        total = sum(int(row["evidence_count"]) for row in legacy_rows) + len(
            message_rows
        )
        all_dates: set[str] = set()
        for row in legacy_rows:
            try:
                all_dates.update(json.loads(row["evidence_dates_json"] or "[]"))
            except (TypeError, ValueError):
                continue
        all_dates.update(str(row["observed_date"]) for row in message_rows)
        return total, sorted(all_dates)

    def archive_expired_entries(self) -> int:
        db = self._db()
        cursor = db.execute(
            "UPDATE entries SET status='archived',updated_at=? "
            "WHERE status!='archived' AND expires_at IS NOT NULL AND expires_at<=?",
            (now_iso(), now_iso()),
        )
        db.commit()
        return int(cursor.rowcount)

    def archive_stale_drafts(self, cutoff: str) -> int:
        db = self._db()
        cursor = db.execute(
            "UPDATE entries SET status='archived',updated_at=? "
            "WHERE source_kind IN ('scheduled','llm_tool') AND status='draft' AND updated_at<?",
            (now_iso(), cutoff),
        )
        db.commit()
        return int(cursor.rowcount)

    def reserve_learning_budget(
        self,
        budget_date: str,
        input_tokens: int,
        max_requests: int,
        max_tokens: int,
        run_id: str,
        *,
        max_output_tokens: int | None = None,
        planned_output_tokens: int = 0,
    ) -> bool:
        with self._lock:
            db = self._db()
            row = db.execute(
                "SELECT request_count,input_tokens_estimated,output_tokens_actual "
                "FROM daily_budget WHERE budget_date=?",
                (budget_date,),
            ).fetchone()
            requests = int(row[0]) if row else 0
            tokens = int(row[1]) if row else 0
            output_tokens = int(row[2]) if row else 0
            output_exhausted = (
                max_output_tokens is not None
                and output_tokens + max(0, planned_output_tokens) > max_output_tokens
            )
            if (
                requests + 1 > max_requests
                or tokens + input_tokens > max_tokens
                or output_exhausted
            ):
                return False
            stamp = now_iso()
            db.execute(
                "INSERT INTO daily_budget(budget_date,request_count,input_tokens_estimated,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(budget_date) DO UPDATE SET "
                "request_count=excluded.request_count,input_tokens_estimated=excluded.input_tokens_estimated,updated_at=excluded.updated_at",
                (budget_date, requests + 1, tokens + input_tokens, stamp),
            )
            db.execute(
                "UPDATE learning_runs SET request_count=request_count+1,"
                "input_tokens_estimated=input_tokens_estimated+?,updated_at=? WHERE run_id=?",
                (input_tokens, stamp, run_id),
            )
            db.commit()
            return True

    def record_learning_output(
        self, budget_date: str, run_id: str, output_tokens: int
    ) -> None:
        if output_tokens <= 0:
            return
        with self._lock:
            db = self._db()
            stamp = now_iso()
            db.execute(
                "UPDATE daily_budget SET output_tokens_actual=output_tokens_actual+?,updated_at=? WHERE budget_date=?",
                (output_tokens, stamp, budget_date),
            )
            db.execute(
                "UPDATE learning_runs SET output_tokens_actual=output_tokens_actual+?,updated_at=? WHERE run_id=?",
                (output_tokens, stamp, run_id),
            )
            db.commit()

    def daily_budget(self, budget_date: str) -> dict[str, Any]:
        row = (
            self._db()
            .execute("SELECT * FROM daily_budget WHERE budget_date=?", (budget_date,))
            .fetchone()
        )
        return (
            dict(row)
            if row
            else {
                "budget_date": budget_date,
                "request_count": 0,
                "input_tokens_estimated": 0,
                "output_tokens_actual": 0,
            }
        )

    def record_injection(
        self,
        session_id: str,
        entry_ids: list[str] | tuple[str, ...],
        estimated_tokens: int,
    ) -> str:
        audit_id = str(uuid.uuid4())
        with self._lock:
            self._db().execute(
                "INSERT INTO injection_audit(audit_id,session_id,entry_ids_json,estimated_tokens,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    audit_id,
                    str(session_id)[:500],
                    json.dumps(list(dict.fromkeys(entry_ids)), ensure_ascii=False),
                    max(0, int(estimated_tokens)),
                    now_iso(),
                ),
            )
            self._db().commit()
        return audit_id

    def cleanup_injection_audit(self, cutoff: str) -> int:
        with self._lock:
            cursor = self._db().execute(
                "DELETE FROM injection_audit WHERE created_at<?", (cutoff,)
            )
            self._db().commit()
            return int(cursor.rowcount)

    def rollback_entry(
        self, entry_id: str, version: int, actor_key: str
    ) -> dict[str, Any]:
        row = (
            self._db()
            .execute(
                "SELECT snapshot_json FROM entry_versions WHERE entry_id=? AND version=?",
                (entry_id, version),
            )
            .fetchone()
        )
        if not row:
            raise ValueError("entry version not found")
        snapshot = json.loads(row[0])
        if not isinstance(snapshot, dict):
            raise ValueError("invalid entry version snapshot")
        snapshot["entry_id"] = entry_id
        return self.save_entry(
            snapshot,
            actor_key=actor_key,
            reason=f"rollback from version {version}",
        )

    def audit(
        self,
        actor: str,
        action: str,
        object_type: str,
        object_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        db = self._db()
        db.execute(
            "INSERT INTO audit_log(audit_id,actor_key,action,object_type,object_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                actor,
                action,
                object_type,
                object_id,
                json.dumps(payload or {}, ensure_ascii=False),
                now_iso(),
            ),
        )
        db.commit()

    def runtime_flags(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for row in self._db().execute("SELECT key,value_json FROM runtime_flags"):
            try:
                result[row["key"]] = json.loads(row["value_json"])
            except (TypeError, ValueError):
                continue
        return result

    def set_runtime_flag(self, key: str, value: Any, actor_key: str) -> None:
        self._db().execute(
            "INSERT INTO runtime_flags(key,value_json,actor_key,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,actor_key=excluded.actor_key,updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False), actor_key, now_iso()),
        )
        self._db().commit()

    def cleanup_expired_messages(self) -> int:
        db = self._db()
        cursor = db.execute(
            "DELETE FROM conversation_messages WHERE expires_at<? AND row_id NOT IN ("
            "SELECT question_row_id FROM trigger_anchors WHERE status NOT IN ('committed','cancelled') "
            "UNION SELECT answer_row_id FROM trigger_anchors WHERE status NOT IN ('committed','cancelled') AND answer_row_id IS NOT NULL)",
            (now_iso(),),
        )
        db.commit()
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return int(cursor.rowcount)

    def cancel_stale_anchors(self, cutoff: str, limit: int = 500) -> int:
        """Cancel requests that never produced a final answer after the hook TTL."""
        if limit <= 0:
            return 0
        with self._lock:
            db = self._db()
            rows = db.execute(
                "SELECT anchor_id FROM trigger_anchors "
                "WHERE status IN ('open','retryable') "
                "AND answer_state IN ('missing','error') "
                "AND created_at<? ORDER BY created_at LIMIT?",
                (cutoff, limit),
            ).fetchall()
            if not rows:
                return 0
            stamp = now_iso()
            db.executemany(
                "UPDATE trigger_anchors SET status='cancelled',request_state='aborted',"
                "answer_state='aborted',updated_at=? WHERE anchor_id=?",
                [(stamp, row["anchor_id"]) for row in rows],
            )
            db.commit()
            return len(rows)

    def counts(self) -> dict[str, int]:
        db = self._db()
        counts = {
            name: int(db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in (
                "learning_targets",
                "conversation_messages",
                "trigger_anchors",
                "entries",
                "learning_runs",
                "learning_batches",
                "injection_audit",
            )
        }
        counts["pending_anchors"] = int(
            db.execute(
                "SELECT COUNT(*) FROM trigger_anchors "
                "WHERE status IN ('open','retryable')"
            ).fetchone()[0]
        )
        counts["pending_tool_candidates"] = self.pending_tool_candidate_count()
        return counts
