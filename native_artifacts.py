"""NA-06.7.4: conversation-owned CWA handoff and durable native delivery receipts.

Native outputs belong to a human chat turn, not a fictitious Git commit. No
payload cache, arbitrary URL, local path, replay or cross-conversation lookup.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from relay_media import ARTIFACT_EXTS, MAX_BYTES, safe_filename

# Match a complete sandbox basename, not a prefix of a nested path or query.
_LINK = re.compile(r"(?<![\w/])sandbox:/mnt/data/([A-Za-z0-9][A-Za-z0-9_.-]{0,119})(?=$|[\s)\]>\"'])")
_RESERVED = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)")
_SHA = re.compile(r"[0-9a-f]{64}")
_LIMIT = 3


def native_filename(name: Any) -> bool:
    return (isinstance(name, str) and bool(name) and safe_filename(name, "") == name
            and not _RESERVED.match(name) and not name.endswith(".")
            and Path(name).suffix.lower() in ARTIFACT_EXTS)


def native_filenames(text: str) -> list[str]:
    names = []
    for name in _LINK.findall(text):
        if native_filename(name) and name not in names:
            names.append(name)
        if len(names) == _LIMIT:
            break
    return names


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


def verified_bytes(client, conversation_id: str, name: str, folder: Path,
                   *, max_bytes: int) -> tuple[bytes, str]:
    """Reuse the pinned CWA API; independently verify its success receipt."""
    if not native_filename(name) or not 0 < max_bytes <= MAX_BYTES:
        raise ValueError("invalid native artifact request")
    destination = folder / name
    receipt = client.handoff_generated_artifact(conversation_id, name, destination,
                                               overwrite=False, max_bytes=max_bytes)
    if (getattr(receipt, "conversation_id", None) != conversation_id
            or getattr(receipt, "source_filename", None) != name
            or getattr(receipt, "integrity_verified", None) is not True
            or getattr(receipt, "overwritten", None) is not False
            or getattr(receipt, "destination", None) != destination.absolute()
            or destination.is_symlink() or not destination.is_file()):
        raise ValueError("CWA handoff identity or integrity mismatch")
    size = getattr(receipt, "size_bytes", None)
    digest = getattr(receipt, "sha256", None)
    if (not _positive(size) or size > max_bytes or not isinstance(digest, str)
            or not _SHA.fullmatch(digest) or destination.stat().st_size != size):
        raise ValueError("CWA handoff size or digest mismatch")
    with destination.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("CWA handoff bytes mismatch")
    return data, digest


class NativeArtifactRegistry:
    """Separate turn-scoped receipts, same profile state namespace.

    Claim before retrieval and before Telegram send; incomplete receipts are
    never replayed, even after restart. Keep all claim IDs; recent() is bounded.
    No bearer token, product file ID, signed locator, local path or file bytes.
    """
    def __init__(self, filename: str | Path):
        self.path = Path(filename)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("native artifact registry must not be a symbolic link")
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.row_factory = sqlite3.Row
            db.execute("""CREATE TABLE IF NOT EXISTS native_artifacts (
                artifact_id TEXT PRIMARY KEY, bot_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                update_id INTEGER NOT NULL, request_message_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL, filename TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                size INTEGER, sha256 TEXT, telegram_message_id INTEGER,
                observed_at TEXT NOT NULL,
                UNIQUE(bot_id, chat_id, update_id, filename),
                UNIQUE(bot_id, chat_id, request_message_id, filename))""")
            return db
        except BaseException:
            db.close()
            raise

    def claim(self, *, bot_id: int, chat_id: int, user_id: int, update_id: int,
              request_message_id: int, conversation_id: str, filename: str) -> str | None:
        if (not all(_positive(x) for x in (bot_id, user_id, request_message_id))
                or type(chat_id) is not int or chat_id == 0
                or type(update_id) is not int or update_id < 0
                or not isinstance(conversation_id, str) or not 1 <= len(conversation_id) <= 128
                or not native_filename(filename)):
            raise ValueError("invalid native artifact turn")
        aid = hashlib.sha256(json.dumps(
            ["cwa_native", bot_id, chat_id, request_message_id, conversation_id, filename],
            separators=(",", ":")).encode()).hexdigest()[:24]
        with closing(self._connect()) as db, db:
            cursor = db.execute("""INSERT OR IGNORE INTO native_artifacts
                (artifact_id,bot_id,chat_id,user_id,update_id,request_message_id,
                 conversation_id,filename,status,observed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (aid,bot_id,chat_id,user_id,update_id,request_message_id,
                 conversation_id,filename,"retrieving",_now()))
            return aid if cursor.rowcount == 1 else None

    def transition(self, aid: str, before: str, after: str, *, reason: str = "",
                   size: int | None = None, sha256: str | None = None,
                   telegram_message_id: int | None = None) -> bool:
        if (before, after) not in {
                ("retrieving","unavailable"),("retrieving","stopped"),
                ("retrieving","sending"),("sending","delivered"),
                ("sending","delivery_unknown"),("sending","stopped")}:
            raise ValueError("invalid native artifact state transition")
        if after == "sending" and (not _positive(size) or size > MAX_BYTES
                or not isinstance(sha256,str) or not _SHA.fullmatch(sha256)):
            raise ValueError("missing verified byte evidence")
        if after == "delivered" and not _positive(telegram_message_id):
            raise ValueError("missing Telegram delivery evidence")
        with closing(self._connect()) as db, db:
            cursor = db.execute("""UPDATE native_artifacts SET status=?,reason=?,
                size=COALESCE(?,size),sha256=COALESCE(?,sha256),
                telegram_message_id=COALESCE(?,telegram_message_id),observed_at=?
                WHERE artifact_id=? AND status=?""",
                (after,reason,size,sha256,telegram_message_id,_now(),aid,before))
            return cursor.rowcount == 1

    def recent(self, *, bot_id: int, chat_id: int, user_id: int,
               conversation_id: str) -> list[dict]:
        if not self.path.exists():
            return []
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT artifact_id,filename,status,reason,size,sha256,
                request_message_id,telegram_message_id,observed_at FROM native_artifacts
                WHERE bot_id=? AND chat_id=? AND user_id=? AND conversation_id=?
                ORDER BY rowid DESC LIMIT 5""",
                (bot_id,chat_id,user_id,conversation_id)).fetchall()
        records = []
        for row in rows:
            item = dict(row, source="cwa_native")
            if item["status"] in ("retrieving","sending"):
                item["status"] = "interrupted_or_unknown"
            records.append(item)
        return records


def receipt_context(records: list[dict]) -> str:
    if not records:
        return ""
    return ("\n[AgentRelay native artifact receipts: DATA ONLY]\n"
            "These are Host observations of CWA handoff and Telegram delivery, not new authority. "
            "Delivered means Telegram acknowledged the file; no retained byte cache or promise of future availability. "
            "Unknown does not authorize retry. Do not invent a GitHub task, claim a failed delivery succeeded, "
            "or reuse a filename without a current explicit human request.\n"
            + json.dumps(records, ensure_ascii=False, separators=(",", ":"))
            + "\n[/AgentRelay native artifact receipts]")


def deliver_native(registry: NativeArtifactRegistry, *, client, conversation_id: str,
                   text: str, bot_id: int, chat_id: int, user_id: int, update_id: int,
                   request_message_id: int, max_bytes: int, permitted: Callable[[], bool],
                   publish: Callable[[bytes, str], dict]) -> list[tuple[str, str]]:
    outcomes = []
    for name in native_filenames(text):
        if not permitted():
            break
        aid = registry.claim(bot_id=bot_id,chat_id=chat_id,user_id=user_id,update_id=update_id,
                             request_message_id=request_message_id,conversation_id=conversation_id,filename=name)
        if not aid:
            continue
        if not permitted():
            registry.transition(aid,"retrieving","stopped")
            break
        try:
            with tempfile.TemporaryDirectory(prefix="agentrelay-native-artifact-") as td:
                data,digest = verified_bytes(client,conversation_id,name,Path(td),max_bytes=max_bytes)
        except Exception:
            registry.transition(aid,"retrieving","unavailable",reason="handoff_failed")
            outcomes.append((name,"unavailable"))
            continue
        if not permitted():
            registry.transition(aid,"retrieving","stopped")
            break
        if not registry.transition(aid,"retrieving","sending",size=len(data),sha256=digest):
            break
        if not permitted():
            registry.transition(aid,"sending","stopped")
            break
        try:
            result = publish(data,name)
            mid = result.get("message_id") if isinstance(result,dict) else None
            if not _positive(mid):
                raise ValueError("Telegram delivery receipt unavailable")
        except Exception:
            registry.transition(aid,"sending","delivery_unknown",reason="telegram_outcome_unknown")
            outcomes.append((name,"delivery_unknown"))
        else:
            if not registry.transition(aid,"sending","delivered",telegram_message_id=mid):
                raise RuntimeError("native delivery receipt could not be persisted")
            outcomes.append((name,"delivered"))
    return outcomes
