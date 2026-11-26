"""Durable one-shot claim for live native actions. Never replay this database."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from action_policy import Source


class DispatchClaims:
    def __init__(self, filename: str):
        self.path = Path(filename)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS action_claims (
                bot_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, update_id INTEGER NOT NULL,
                proposal_id TEXT NOT NULL, generation INTEGER NOT NULL,
                PRIMARY KEY(bot_id, chat_id, message_id),
                UNIQUE(bot_id, update_id)
            )""")

    def claim(self, source: Source, proposal_id: str) -> bool:
        # Durable before reservation. If a crash occurs in between, the
        # request is lost rather than replayed. No executor reads this table.
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            cursor = db.execute(
                "INSERT OR IGNORE INTO action_claims VALUES (?, ?, ?, ?, ?, ?)",
                (source.bot_id, source.chat_id, source.message_id,
                 source.update_id, proposal_id, source.generation),
            )
            return cursor.rowcount == 1
