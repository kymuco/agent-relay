"""NA-07.2: durable observation of potential cognition triggers.

This is deliberately not an executable wake queue. Historical decisions never
authorize a model call or worker. Mission binding and live admission are future
stages. Cursor and observations commit together in the event journal database.
"""
from __future__ import annotations

import json
from contextlib import closing

from agent_events import EventJournal

_RULES = {
    "work.accepted": ("ignore", "reservation_only"),
    "work.started": ("ignore", "execution_in_progress"),
    "work.completed": ("candidate", "evaluate_result"),
    "work.failed": ("candidate", "reconsider_plan"),
    "work.needs_review": ("candidate", "review_required"),
    "work.interrupted": ("recovery_required", "reconcile_before_continuing"),
    "work.stopped": ("ignore", "operator_stop"),
}


class DecisionScheduler:
    """Classify Host events in order, exactly once per local journal.

    A candidate means the event type could justify cognition in a future
    mission. It does not mean the task is current, authorized or successful.
    """

    def __init__(self, journal: EventJournal):
        self.journal = journal
        with closing(journal._connect()) as db, db:
            journal._check_identity(db)
            db.execute("""CREATE TABLE IF NOT EXISTS decision_cursor (
                slot INTEGER PRIMARY KEY CHECK(slot=1), last_seq INTEGER NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO decision_cursor VALUES (1, 0)")
            db.execute("""CREATE TABLE IF NOT EXISTS decision_observations (
                event_seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
                decision TEXT NOT NULL, reason TEXT NOT NULL)""")

    def tick(self, *, limit: int = 100) -> int:
        """Consume a bounded batch; return number observed, never dispatch."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Invalid decision batch size")
        with closing(self.journal._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.journal._check_identity(db)
            cursor = db.execute("SELECT last_seq FROM decision_cursor WHERE slot=1").fetchone()[0]
            rows = db.execute(
                "SELECT seq, event_id, body FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                (cursor, limit)).fetchall()
            for seq, event_id, body in rows:
                event = json.loads(body)
                self.journal.validate(event)
                if event["event_id"] != event_id:
                    raise ValueError("Journal event identity mismatch")
                decision, reason = _RULES[event["event_type"]]
                db.execute("INSERT INTO decision_observations VALUES (?, ?, ?, ?)",
                           (seq, event_id, decision, reason))
                cursor = seq
            if rows:
                db.execute("UPDATE decision_cursor SET last_seq=? WHERE slot=1", (cursor,))
            return len(rows)

    def read(self, *, after: int = 0, limit: int = 100) -> list[dict]:
        if (type(after) is not int or after < 0 or type(limit) is not int
                or not 1 <= limit <= 1000):
            raise ValueError("Invalid decision cursor")
        with closing(self.journal._connect()) as db:
            self.journal._check_identity(db)
            rows = db.execute(
                """SELECT d.event_seq, d.event_id, d.decision, d.reason, e.body
                   FROM decision_observations d JOIN events e ON e.seq=d.event_seq
                   WHERE d.event_seq>? ORDER BY d.event_seq LIMIT ?""",
                (after, limit)).fetchall()
        return [{"seq": seq, "event_id": event_id, "decision": decision,
                 "reason": reason, "event_type": json.loads(body)["event_type"],
                 "mode": "observe", "execution_enabled": False}
                for seq, event_id, decision, reason, body in rows]
