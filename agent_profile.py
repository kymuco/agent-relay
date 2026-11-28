"""NA-05: local agent identity, state namespace and conversation ownership.

Named profiles are optional. No credential contents or remote permissions are
stored. The ownership registry is local to one checkout/profile root.
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

PROFILE_ROOT = Path(".agent-relay-profiles")
STATE_FILES = {
    "state_file": ("STATE_FILE", ".agent-relay-state.json", "cursor.json"),
    "control_file": ("CONTROL_FILE", ".agent-relay-control.json", "control.json"),
    "work_state_file": ("WORK_STATE_FILE", ".agent-relay-work.json", "work.json"),
    "work_chat_state_file": ("WORK_CHAT_STATE_FILE", ".agent-relay-work-chats.json", "work-chats.json"),
    "work_progress_file": ("WORK_PROGRESS_FILE", ".agent-relay-progress.json", "progress.json"),
    "action_observation_file": ("ACTION_OBSERVATION_FILE", ".agent-relay-action-observations.json", "observations.json"),
    "action_claim_file": ("ACTION_CLAIM_FILE", ".agent-relay-action-claims.sqlite3", "claims.sqlite3"),
}


def profile_directory(profile: str) -> Path:
    if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", profile)
            or re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", profile)):
        raise ValueError("AGENT_PROFILE must be a safe lowercase identifier")
    root = PROFILE_ROOT.resolve()
    directory = root / profile
    if directory.resolve() != directory:
        raise ValueError("Profile directory must not be a symbolic link")
    return directory


def profile_auth_path(profile: str, value: str) -> str:
    if profile and value in ("", "auth_data.json"):
        return str(profile_directory(profile) / "auth_data.json")
    return value or "auth_data.json"


def namespace_config(config):
    if not config.agent_profile:
        return config
    directory = profile_directory(config.agent_profile)
    fields = {}
    for field, (env_name, legacy, filename) in STATE_FILES.items():
        path = directory / filename
        existing = getattr(config, field)
        if existing != legacy and Path(existing).resolve() != path:
            raise ValueError(env_name + " conflicts with the named profile state namespace")
        if path.resolve() != path:
            raise ValueError("Profile state path must not be a symbolic link")
        fields[field] = str(path)
    fields["auth_file"] = profile_auth_path(config.agent_profile, config.auth_file)
    return replace(config, **fields)


def profile_log_path(profile: str, value: str) -> str:
    if not profile or value.lower() in {"off", "none"}:
        return value
    path = profile_directory(profile) / "agentrelay.log"
    if value not in ("", "logs/agentrelay.log") and Path(value).resolve() != path:
        raise ValueError("LOG_FILE conflicts with the named profile state namespace")
    if path.resolve() != path:
        raise ValueError("Profile log path must not be a symbolic link")
    return str(path)


def display_name(configured: str, username: str) -> str:
    if not configured:
        return username
    if len(configured) > 40 or any(ord(c) < 32 or ord(c) == 127 for c in configured):
        raise ValueError("AGENT_NAME must be a single display name, at most 40 characters")
    return configured


def own_aliases(username: str, configured: tuple[str, ...], name: str,
                *, legacy: bool) -> tuple[str, ...]:
    values = list(configured)
    if name and name.casefold() != username.casefold():
        values.append(name)
    # Compatibility only for actual legacy Ragna installations, not every bot.
    if legacy and username.casefold() in {"ragnacobot", "ragnabot"}:
        values.extend(("Ragna", "Рагна"))
    return tuple(dict.fromkeys(values))


class ProfileLease:
    """One process per named profile; OS releases the lock when it exits."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "process.lock"
        if path.resolve() != path:
            raise ValueError("Profile lock path must not be a symbolic link")
        self.stream = path.open("a+b")
        try:
            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b"\0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise ValueError("Named agent profile is already running or cannot be locked") from exc

    def close(self):
        self.stream.close()


class ProfileOwnership:
    """Durably reject bot/chat ownership collisions inside one profile root."""

    def __init__(self, profile: str, bot_id: int, chat_id: int, main_id: str):
        directory = profile_directory(profile)
        self.path = directory.parent / "ownership.sqlite3"
        self.profile = profile
        if self.path.resolve() != self.path:
            raise ValueError("Profile ownership database must not be a symbolic link")
        directory.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("""CREATE TABLE IF NOT EXISTS profiles (
                profile TEXT PRIMARY KEY, bot_id INTEGER NOT NULL UNIQUE,
                chat_id INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversations (
                profile TEXT NOT NULL REFERENCES profiles(profile),
                lane TEXT NOT NULL, conversation_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY(profile, lane))""")
            try:
                db.execute("INSERT OR IGNORE INTO profiles VALUES (?, ?, ?)",
                           (profile, bot_id, chat_id))
                row = db.execute("SELECT bot_id, chat_id FROM profiles WHERE profile=?",
                                 (profile,)).fetchone()
                if row != (bot_id, chat_id):
                    raise ValueError("Named profile already belongs to a different bot/group")
                self._claim(db, "main", main_id)
            except sqlite3.IntegrityError as exc:
                raise ValueError("Conversation or bot belongs to another profile") from exc

    def _claim(self, db, lane: str, cid: str):
        if lane not in {"main", "coordinator", "developer", "mission"} or not isinstance(cid, str) or not cid:
            raise ValueError("Invalid profile conversation identity")
        db.execute("INSERT OR IGNORE INTO conversations VALUES (?, ?, ?)",
                   (self.profile, lane, cid))
        row = db.execute("SELECT conversation_id FROM conversations WHERE profile=? AND lane=?",
                         (self.profile, lane)).fetchone()
        if row != (cid,):
            raise ValueError("Conversation already belongs to another profile/lane or changed")

    def work_lanes(self) -> dict[str, str]:
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            return dict(db.execute("SELECT lane, conversation_id FROM conversations WHERE profile=? AND lane != ?",
                                   (self.profile, "main")).fetchall())

    def claim_lane(self, lane: str, cid: str):
        with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            self._claim(db, lane, cid)
