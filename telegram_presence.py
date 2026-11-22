"""Telegram typing presence and one-message work progress (no model calls).

Presence is ephemeral and never claims a background GitHub worker is typing.
Progress is updated from real Host state transitions, never model estimates.
"""
from __future__ import annotations

import html
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from relay_logging import LOG


class TypingPulse:
    """Send an initial typing indicator and refresh at most once per 4 seconds."""

    def __init__(self, api: Any, chat_id: int, *, thread_id: int | None = None,
                 permitted: Callable[[], bool] = lambda: True,
                 interval: float = 4.0, max_seconds: float = 180):
        self.api, self.chat_id, self.thread_id = api, chat_id, thread_id
        self.permitted = permitted
        self.interval, self.max_seconds = interval, max_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        until = time.monotonic() + self.max_seconds
        while not self._stop.is_set() and time.monotonic() < until:
            if not self.permitted():
                return
            data: dict[str, Any] = {"chat_id": self.chat_id, "action": "typing"}
            if self.thread_id is not None:
                data["message_thread_id"] = self.thread_id
            try:
                self.api.call("sendChatAction", data, timeout=8)
            except Exception as exc:
                LOG.debug("Typing status недоступен (%s)", type(exc).__name__)
                return
            if self._stop.wait(self.interval):
                return

    def start(self) -> "TypingPulse":
        self._thread = threading.Thread(
            target=self._loop, name="AgentRelayTyping", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def __enter__(self) -> "TypingPulse":
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.stop()


_PHASES = {
    "queued": "📥 Поручение принято",
    "coordinator": "🧠 Составляю план",
    "developer": "💻 Разработчик работает с GitHub",
    "revision": "🔁 Передаю уточнения",
    "verify": "🔎 Проверяю ветку и коммит",
    "local_test": "🧪 Проверяю код локально",
    "review": "📝 Проверяю итоговый отчёт",
    "completed": "✅ Коммит подтверждён",
    "needs_review": "⚠️ Есть неприменённые уточнения",
    "failed": "❌ Задача не завершена",
    "stopped": "⏹ Задача остановлена",
    "interrupted": "⚠️ Прервана при перезапуске",
}
_TERMINAL = frozenset({"completed", "needs_review", "failed", "stopped", "interrupted"})


def _safe_text(value: str, limit: int) -> str:
    return html.escape(" ".join(str(value).split())[:limit], quote=True)


def format_work_progress(work_id: int, project: str, task: str,
                         phase: str, *, note: str = "") -> str:
    """Native Telegram HTML, bounded; labels reflect actual Host events."""
    if phase not in _PHASES:
        raise ValueError("Unknown progress phase")
    lines = [
        f"<b>Задача #{int(work_id)} · {_safe_text(project, 55)}</b>",
        _PHASES[phase],
        "",
        f"<i>{_safe_text(task, 180)}</i>",
    ]
    if note.strip():
        lines.extend(["", _safe_text(note, 1200)])
    if phase not in _TERMINAL:
        lines.extend(["", "Можно продолжать общение с Ragna."])
    return "\n".join(lines)


class WorkProgress:
    """Store the one editable Telegram message id for the current work id.

    Never invent progress, resend a possibly delivered initial message, or
    change an unrelated message. Failed edits are logged, not retried blindly.
    """

    def __init__(self, api: Any, chat_id: int, state_file: str):
        self.api, self.chat_id = api, chat_id
        self.path = Path(state_file)
        self._lock = threading.RLock()
        self.record: dict[str, Any] | None = None
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if (type(data.get("work_id")) is int and data["work_id"] > 0
                        and type(data.get("message_id")) is int and data["message_id"] > 0
                        and data.get("chat_id") == self.chat_id
                        and isinstance(data.get("project"), str)
                        and isinstance(data.get("task"), str)
                        and data.get("phase") in _PHASES):
                    self.record = data
            except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
                LOG.warning("Не удалось восстановить Telegram progress ID")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as out:
            json.dump(self.record, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, self.path)

    def begin(self, work_id: int, project: str, task: str,
              *, reply_message_id: int, thread_id: int | None = None) -> bool:
        with self._lock:
            text = format_work_progress(work_id, project, task, "queued")
            payload: dict[str, Any] = {
                "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                "reply_parameters": {"message_id": reply_message_id,
                                     "allow_sending_without_reply": True},
            }
            if thread_id is not None:
                payload["message_thread_id"] = thread_id
            # Telegram may have delivered the message if the request times out;
            # don't generate a duplicate fallback message in that case.
            result = self.api.call("sendMessage", payload)
            mid = result.get("message_id") if isinstance(result, dict) else None
            if type(mid) is not int or mid < 1:
                LOG.warning("Telegram progress: missing message ID; edits disabled")
                return False
            self.record = {"work_id": work_id, "project": project,
                           "task": task[:250], "chat_id": self.chat_id,
                           "message_id": mid, "thread_id": thread_id,
                           "phase": "queued", "html": text}
            self._save()
            return True

    def update(self, work_id: int, phase: str, *, note: str = "") -> bool:
        with self._lock:
            record = self.record
            if record is None or record["work_id"] != work_id:
                return False
            # Terminal phases are immutable, preventing stale worker threads
            # from resurrecting progress after stop or completion.
            if record["phase"] in _TERMINAL:
                return False
            rendered = format_work_progress(work_id, record["project"],
                                            record["task"], phase, note=note)
            if rendered == record["html"]:
                return False
            try:
                self.api.call("editMessageText", {
                    "chat_id": self.chat_id,
                    "message_id": record["message_id"],
                    "text": rendered, "parse_mode": "HTML",
                })
            except Exception as exc:
                LOG.warning("Не удалось обновить Telegram прогресс (%s)",
                            type(exc).__name__)
                return False
            record["phase"] = phase
            record["html"] = rendered
            self._save()
            return True

    def interrupted(self, work_id: int) -> None:
        self.update(work_id, "interrupted",
                    note="Предыдущая задача не возобновлялась автоматически.")
