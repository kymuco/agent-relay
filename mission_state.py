"""NA-07.3: durable mission intent and owner grant, no execution authority.

A mission is a bounded, local, explicitly authorized planning envelope. No
historical work or scheduler observation is implicitly bound to a mission.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from typing import Mapping

from agent_events import EventJournal


class MissionRejected(ValueError):
    """A validly addressed request cannot be admitted."""


@dataclass(frozen=True)
class MissionPolicy:
    owners: frozenset[int]
    projects: Mapping[str, str]
    chat_id: int
    generation: int


@dataclass(frozen=True)
class MissionRequest:
    project: str
    minutes: int
    max_decisions: int
    max_tasks: int
    max_worker_turns: int
    goal: str
    criteria: tuple[str, ...]

    def validate(self):
        if (not isinstance(self.project, str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", self.project)
                or not all(type(value) is int and low <= value <= high
                           for value, low, high in (
                               (self.minutes, 1, 1440), (self.max_decisions, 1, 100),
                               (self.max_tasks, 1, 30), (self.max_worker_turns, 1, 200)))
                or not isinstance(self.goal, str) or not 1 <= len(self.goal.strip()) <= 1200
                or not isinstance(self.criteria, tuple) or not 1 <= len(self.criteria) <= 8
                or any(not isinstance(c, str) or not 1 <= len(c.strip()) <= 400
                       for c in self.criteria)):
            raise MissionRejected("invalid_mission_request")


def parse_request(text: str) -> MissionRequest:
    parts = text.split("|")
    if len(parts) != 3:
        raise MissionRejected("expected_limits_goal_criteria")
    words = parts[0].split()
    if len(words) != 5 or any(not re.fullmatch(r"[0-9]{1,4}", w) for w in words[1:]):
        raise MissionRejected("invalid_mission_limits")
    result = MissionRequest(words[0], *(int(w) for w in words[1:]),
                            parts[1].strip(), tuple(c.strip() for c in parts[2].split(";")))
    result.validate()
    return result


class MissionStore:
    """One active mission per journal; immutable grant, separate lifecycle."""

    def __init__(self, journal: EventJournal, *, clock=time.time):
        self.journal = journal
        self.clock = clock
        with closing(journal._connect()) as db, db:
            journal._check_identity(db)
            db.execute("""CREATE TABLE IF NOT EXISTS missions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                mission_id TEXT NOT NULL UNIQUE,
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                grant_body TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','revoked','expired','invalidated')),
                ended_at INTEGER, end_reason TEXT,
                UNIQUE(chat_id, message_id))""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_mission
                          ON missions(status) WHERE status='active'""")

    @staticmethod
    def _row(row):
        if row is None:
            return None
        body, status, ended, reason = row
        return {**json.loads(body), "status": status, "ended_at": ended,
                "end_reason": reason, "execution_enabled": False,
                "used_decisions": 0, "used_tasks": 0, "used_worker_turns": 0}

    def _refresh(self, db, policy: MissionPolicy, now: int):
        row = db.execute("SELECT mission_id, grant_body FROM missions WHERE status='active'").fetchone()
        if row is None:
            return
        mission_id, body = row
        grant = json.loads(body)
        reason = None
        status = "invalidated"
        if grant["generation"] != policy.generation:
            reason = "control_generation_changed"
        elif grant["owner_user_id"] not in policy.owners or grant["chat_id"] != policy.chat_id:
            reason = "owner_or_chat_changed"
        elif policy.projects.get(grant["project"]) != grant["repository"]:
            reason = "project_scope_changed"
        elif now < grant["created_at"]:
            reason = "clock_moved_before_creation"
        elif now >= grant["expires_at"]:
            status, reason = "expired", "deadline_reached"
        if reason:
            db.execute("UPDATE missions SET status=?, ended_at=?, end_reason=? WHERE mission_id=?",
                       (status, now, reason, mission_id))

    def create(self, request: MissionRequest, *, owner_user_id: int, message_id: int,
               policy: MissionPolicy) -> dict:
        request.validate()
        if (type(owner_user_id) is not int or owner_user_id not in policy.owners
                or type(message_id) is not int or message_id <= 0):
            raise MissionRejected("owner_not_allowed")
        repository = policy.projects.get(request.project)
        if not repository:
            raise MissionRejected("project_not_allowed")
        now = int(self.clock())
        with closing(self.journal._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.journal._check_identity(db)
            self._refresh(db, policy, now)
            # A replay cannot create a new grant even after expiry or revocation.
            if db.execute("SELECT 1 FROM missions WHERE chat_id=? AND message_id=?",
                          (policy.chat_id, message_id)).fetchone():
                raise MissionRejected("source_already_used")
            if db.execute("SELECT 1 FROM missions WHERE status='active'").fetchone():
                raise MissionRejected("mission_already_active")
            grant = {
                "schema_version": 1, "mission_id": uuid.uuid4().hex,
                "agent_id": self.journal.agent_id, "owner_user_id": owner_user_id,
                "chat_id": policy.chat_id, "message_id": message_id,
                "generation": policy.generation, "project": request.project,
                "repository": repository, "goal": request.goal.strip(),
                "criteria": [c.strip() for c in request.criteria],
                "created_at": now, "expires_at": now + request.minutes * 60,
                "max_decisions": request.max_decisions, "max_tasks": request.max_tasks,
                "max_worker_turns": request.max_worker_turns,
                "scope": "isolated_branches_only", "merge_allowed": False,
                "peer_delegation_allowed": False,
            }
            body = json.dumps(grant, ensure_ascii=False, sort_keys=True)
            db.execute("""INSERT INTO missions(mission_id, chat_id, message_id, grant_body, status)
                          VALUES (?, ?, ?, ?, 'active')""",
                       (grant["mission_id"], policy.chat_id, message_id, body))
            return self._row((body, "active", None, None))

    def latest(self, policy: MissionPolicy) -> dict | None:
        """Refresh expiry/authority against current Host facts; no work binding."""
        with closing(self.journal._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.journal._check_identity(db)
            self._refresh(db, policy, int(self.clock()))
            return self._row(db.execute(
                "SELECT grant_body, status, ended_at, end_reason FROM missions ORDER BY seq DESC LIMIT 1"
            ).fetchone())

    def revoke(self, mission_id: str, *, owner_user_id: int, policy: MissionPolicy) -> bool:
        if type(owner_user_id) is not int or owner_user_id not in policy.owners:
            raise MissionRejected("owner_not_allowed")
        if not isinstance(mission_id, str) or not re.fullmatch(r"[0-9a-f]{32}", mission_id):
            raise MissionRejected("invalid_mission_id")
        with closing(self.journal._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self.journal._check_identity(db)
            now = int(self.clock())
            self._refresh(db, policy, now)
            return db.execute("""UPDATE missions SET status='revoked', ended_at=?,
                                 end_reason='owner_revoked' WHERE mission_id=? AND status='active'""",
                              (now, mission_id)).rowcount == 1
