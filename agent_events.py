"""NA-07.1: local durable Host event journal. Events are facts, not grants.

The WorkState JSON outbox is the source until SQLite commit succeeds.
No consumer, model call, network delivery or action dispatch lives here.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

STATUS_EVENTS = {
    "queued": "work.accepted", "running": "work.started",
    "completed": "work.completed", "failed": "work.failed",
    "needs_review": "work.needs_review", "interrupted": "work.interrupted",
    "stopped": "work.stopped",
}


def transition_event(agent_id: str, record: Mapping[str, Any]) -> dict:
    """Capture persisted Host status, not a claim of successful verification."""
    status = record["status"]
    payload = {"project": record["project"], "status": status,
               "stage": record["stage"], "repository": record.get("repository", ""),
               "branch": record.get("branch", ""), "remote_verified": False}
    result = record.get("result")
    if isinstance(result, dict) and status not in ("queued", "running"):
        sha = result.get("commit_sha")
        if (result.get("remote_verified") is True and isinstance(sha, str)
                and re.fullmatch(r"[0-9a-f]{40}", sha)
                and payload["repository"] and result.get("repository") == payload["repository"]
                and result.get("branch") and
                (not payload["branch"] or result["branch"] == payload["branch"])):
            payload.update(remote_verified=True, commit_sha=sha, branch=result["branch"])
            ci = result.get("ci")
            if isinstance(ci, dict) and ci.get("commit_sha") == sha:
                payload["ci_status"] = ci.get("status")
    return {"schema_version": 1, "event_id": uuid.uuid4().hex,
            "event_type": STATUS_EVENTS[status], "source": "host",
            "agent_id": agent_id, "work_id": record["id"],
            "occurred_at": datetime.now(timezone.utc).isoformat(), "payload": payload}


class EventJournal:
    """Append-only local journal; stable event IDs, ordered cursor, no live handles."""

    def __init__(self, filename: str | Path, agent_id: str):
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("Event journal requires agent identity")
        self.path = Path(filename)
        if self.path.is_symlink():
            raise ValueError("Event journal must not be a symbolic link")
        self.agent_id = agent_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS identity (slot INTEGER PRIMARY KEY CHECK(slot=1), agent_id TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO identity VALUES (1, ?)", (agent_id,))
            self._check_identity(db)
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE, body TEXT NOT NULL)""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=0.1)
        try:
            db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            db.close()
            raise
        return db

    def _check_identity(self, db):
        if db.execute("SELECT agent_id FROM identity WHERE slot=1").fetchone() != (self.agent_id,):
            raise ValueError("Event journal identity mismatch")

    def validate(self, event: dict) -> None:
        if (not isinstance(event, dict) or event.get("schema_version") != 1
                or event.get("agent_id") != self.agent_id or event.get("source") != "host"
                or not isinstance(event.get("event_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", event["event_id"])
                or type(event.get("work_id")) is not int or event["work_id"] <= 0
                or not isinstance(event.get("payload"), dict)
                or event["payload"].get("status") not in STATUS_EVENTS
                or event.get("event_type") != STATUS_EVENTS[event["payload"]["status"]]):
            raise ValueError("Invalid Host event")

    def append(self, events: list[dict]) -> None:
        if not events:
            return
        with closing(self._connect()) as db, db:
            self._check_identity(db)
            for event in events:
                self.validate(event)
                body = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                old = db.execute("SELECT body FROM events WHERE event_id=?", (event["event_id"],)).fetchone()
                if old is None:
                    db.execute("INSERT INTO events(event_id, body) VALUES (?, ?)", (event["event_id"], body))
                elif old[0] != body:
                    raise ValueError("Event identity collision")

    def read(self, *, after: int = 0, limit: int = 100) -> list[dict]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid event cursor")
        with closing(self._connect()) as db:
            self._check_identity(db)
            rows = db.execute("SELECT seq, body FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                              (after, limit)).fetchall()
        return [{"seq": seq, **json.loads(body)} for seq, body in rows]
