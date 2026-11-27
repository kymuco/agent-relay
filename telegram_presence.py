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
from github_ci import ci_record
from local_workspace import workspace_record
from local_execution import execution_record
from local_diagnostics import diagnostic_record
from verification_routing import verification_record
from work_card_views import (render_work_card, render_work_journal,
                             journal_event, work_keyboard)


class TypingPulse:
    """Send an initial typing indicator and refresh at most once per 4 seconds."""

    def __init__(self, api: Any, chat_id: int, *, thread_id: int | None = None,
                 permitted: Callable[[], bool] = lambda: True,
                 interval: float = 4.0, max_seconds: float = 180,
                 start_thread: Callable[[threading.Thread], None] | None = None):
        self.api, self.chat_id, self.thread_id = api, chat_id, thread_id
        self.permitted = permitted
        self.interval, self.max_seconds = interval, max_seconds
        self._start_thread = start_thread
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
        if self._start_thread is None:
            self._thread.start()
        else:
            self._start_thread(self._thread)
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
    "diagnostics": "🔎 Codex анализирует ошибку",
    "ci": "🧪 Проверяю GitHub Actions",
    "workspace": "📂 Получаю точный коммит локально",
    "review": "📝 Проверяю итоговый отчёт",
    "completed": "✅ Коммит подтверждён",
    "needs_review": "⚠️ Требуется проверка",
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
        lines.extend(["", "Можно продолжать общение с ассистентом."])
    return "\n".join(lines)


class WorkProgress:
    """One editable Telegram status card per task; callbacks reveal stored facts.

    Keeps a bounded history of verified message IDs across process restarts.
    A callback must reference exactly the original bot card and its work ID.
    Display-only interactions cannot start, cancel or modify any work.
    """

    HISTORY_LIMIT = 30

    def __init__(self, api: Any, chat_id: int, state_file: str, *, agent_name: str = ''):
        self.api, self.chat_id = api, chat_id
        self.agent_name = agent_name
        self.path = Path(state_file)
        self._lock = threading.RLock()
        self.record: dict[str, Any] | None = None
        self.history: dict[str, dict[str, Any]] = {}
        if self.path.is_file():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError("Invalid progress state")
                if isinstance(saved.get("records"), dict):
                    for key, item in saved["records"].items():
                        if self._valid(item) and key == str(item["work_id"]):
                            self.history[key] = item
                    last = saved.get("current_id")
                    self.record = self.history.get(str(last))
                elif self._valid(saved):
                    # Backward compatible with the pre-button card state.
                    self.record = saved
                    self.history[str(saved["work_id"])] = saved
            except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
                LOG.warning("Не удалось восстановить Telegram progress ID")

    def _valid(self, item: Any) -> bool:
        return (isinstance(item, dict)
                and type(item.get("work_id")) is int and item["work_id"] > 0
                and type(item.get("message_id")) is int and item["message_id"] > 0
                and item.get("chat_id") == self.chat_id
                and isinstance(item.get("project"), str)
                and isinstance(item.get("task"), str)
                and item.get("phase") in _PHASES)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        if len(self.history) > self.HISTORY_LIMIT:
            oldest = sorted(self.history, key=int)[:-self.HISTORY_LIMIT]
            for key in oldest:
                del self.history[key]
        payload = {
            "current_id": self.record["work_id"] if self.record else None,
            "records": self.history,
        }
        with temp.open("w", encoding="utf-8") as out:
            json.dump(payload, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, self.path)

    def _render(self, rec: dict[str, Any]) -> str:
        return render_work_card(rec, detail=rec.get("view") == "detail")

    def _markup(self, rec: dict[str, Any]) -> dict[str, Any]:
        return work_keyboard(rec["work_id"], rec.get("view") == "detail",
                             rec.get("result") or {},
                             phase=rec.get("phase", ""),
                             stop_pending=bool(rec.get("stop_confirm_user")))

    def begin(self, work_id: int, project: str, task: str,
              *, reply_message_id: int, thread_id: int | None = None,
              repo: str = "") -> bool:
        with self._lock:
            rec: dict[str, Any] = {
                "work_id": work_id, "project": project[:80],
                "agent_name": self.agent_name,
                "task": task[:250], "repository": repo[:130],
                "chat_id": self.chat_id, "message_id": 0,
                "thread_id": thread_id, "phase": "queued",
                "view": "compact", "note": "", "result": {},
                "events": [journal_event("queued")],
            }
            text = self._render(rec)
            payload: dict[str, Any] = {
                "chat_id": self.chat_id,
                "text": text, "parse_mode": "HTML",
                "reply_markup": self._markup(rec),
                "reply_parameters": {"message_id": reply_message_id,
                                     "allow_sending_without_reply": True},
            }
            if thread_id is not None:
                payload["message_thread_id"] = thread_id
            # An ambiguous initial send is not retried.
            result = self.api.call("sendMessage", payload)
            mid = result.get("message_id") if isinstance(result, dict) else None
            if type(mid) is not int or mid < 1:
                LOG.warning("Telegram progress: missing message ID; edits disabled")
                return False
            rec["message_id"] = mid
            rec["html"] = text
            self.record = rec
            self.history[str(work_id)] = rec
            self._save()
            return True

    def update(self, work_id: int, phase: str, *, note: str = "") -> bool:
        with self._lock:
            rec = self.record
            if rec is None or rec["work_id"] != work_id:
                return False
            if rec["phase"] in _TERMINAL:
                return False
            if phase not in _PHASES:
                return False
            # Render from a copy: don't change local state before the edit.
            target = dict(rec, phase=phase, note=note[:350])
            rendered = self._render(target)
            if rendered == rec.get("html"):
                return False
            try:
                self.api.call("editMessageText", {
                    "chat_id": self.chat_id,
                    "message_id": rec["message_id"],
                    "text": rendered, "parse_mode": "HTML",
                    "reply_markup": self._markup(target),
                })
            except Exception as exc:
                LOG.warning("Не удалось обновить Telegram прогресс (%s)",
                            type(exc).__name__)
                return False
            rec.update(phase=phase, note=note[:350], html=rendered)
            if phase in _TERMINAL:
                rec.pop("stop_confirm_user", None)
                rec.pop("stop_confirm_until", None)
            rec.setdefault("events", []).append(journal_event(phase, note))
            rec["events"] = rec["events"][-40:]
            self._save()
            return True

    def result(self, work_id: int, *, developer_summary: str,
               coordinator_summary: str, repository: str, branch: str,
               commit_sha: str, remote_verified: bool, local_check: str = "",
               pending_amendments: int = 0, delegation: str = "", ci=None,
               workspace=None, execution=None, diagnostic=None, verification=None) -> bool:
        """Save trusted worker report and Host verification as separate fields."""
        with self._lock:
            rec = self.record
            if rec is None or rec["work_id"] != work_id:
                return False
            rec["result"] = {
                "developer_summary": str(developer_summary)[:2300],
                "coordinator_summary": str(coordinator_summary)[:1000],
                "delegation": delegation if delegation in ("direct", "coordinated") else "",
                "repository": str(repository)[:130],
                "branch": str(branch)[:150],
                "commit_sha": str(commit_sha)[:40],
                "remote_verified": remote_verified is True,
                "local_check": str(local_check)[:750],
                "pending_amendments": pending_amendments,
            }
            observed = ci_record(ci, commit_sha) if remote_verified is True else None
            if observed is not None:
                rec["result"]["ci"] = observed
            prepared = workspace_record(workspace, commit_sha) if remote_verified is True else None
            if prepared is not None:
                rec["result"]["workspace"] = prepared
            tested = execution_record(execution, prepared, commit_sha) if remote_verified is True else None
            if tested is not None:
                rec["result"]["execution"] = tested
            diagnosed = diagnostic_record(diagnostic, tested, prepared, commit_sha) if remote_verified is True else None
            if diagnosed is not None:
                rec["result"]["diagnostic"] = diagnosed
            plan = verification_record(verification, commit_sha) if remote_verified is True else None
            if plan is not None:
                rec["result"]["verification"] = plan
            rec.setdefault("events", []).append(
                journal_event("review", "Отчёты и результаты сохранены"))
            rec["events"] = rec["events"][-40:]
            self._save()
            return True

    def card(self, work_id: int, message_id: int) -> dict[str, Any] | None:
        with self._lock:
            rec = self.history.get(str(work_id))
            if rec is None or rec.get("message_id") != message_id:
                return None
            return dict(rec)

    def open_view(self, work_id: int, message_id: int, *, detail: bool) -> bool:
        with self._lock:
            rec = self.history.get(str(work_id))
            if rec is None or rec["message_id"] != message_id:
                return False
            view = "detail" if detail else "compact"
            if rec.get("view", "compact") == view:
                return True
            target = dict(rec, view=view)
            rendered = self._render(target)
            try:
                self.api.call("editMessageText", {
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "text": rendered, "parse_mode": "HTML",
                    "reply_markup": self._markup(target),
                })
            except Exception as exc:
                LOG.warning("Не удалось раскрыть Telegram карточку (%s)",
                            type(exc).__name__)
                return False
            rec["view"] = view
            rec["html"] = rendered
            self._save()
            return True

    def journal_bytes(self, work_id: int, message_id: int) -> bytes | None:
        with self._lock:
            rec = self.history.get(str(work_id))
            if rec is None or rec["message_id"] != message_id:
                return None
            return render_work_journal(rec)

    def pending_amendments(self, work_id: int, count: int,
                           *, admitted: bool = False) -> bool:
        """Show exact WorkState queue count; record accepted changes as an event."""
        if type(count) is not int or count < 0 or count > 4:
            return False
        with self._lock:
            rec = self.record
            if (rec is None or rec["work_id"] != work_id
                    or rec["phase"] in _TERMINAL):
                return False
            rec["pending_changes"] = count
            if admitted:
                rec.setdefault("events", []).append(
                    journal_event("revision", "Получено уточнение к задаче"))
                rec["events"] = rec["events"][-40:]
            # Preserve factual queue state even if Telegram edit is ambiguous.
            self._save()
            rendered = self._render(rec)
            if rendered == rec.get("html"):
                return True
            try:
                self.api.call("editMessageText", {
                    "chat_id": self.chat_id,
                    "message_id": rec["message_id"],
                    "text": rendered, "parse_mode": "HTML",
                    "reply_markup": self._markup(rec),
                })
            except Exception as exc:
                LOG.warning("Не удалось показать очередь уточнений (%s)",
                            type(exc).__name__)
                return True
            rec["html"] = rendered
            self._save()
            return True

    def card_for_reply(self, message: dict[str, Any], *, bot_id: int) -> dict[str, Any] | None:
        """Only Reply to our real saved Telegram card ID selects a task."""
        reply = message.get("reply_to_message") or {}
        chat = message.get("chat") or {}
        quoted_bot = reply.get("from") or {}
        mid = reply.get("message_id")
        if (chat.get("id") != self.chat_id
                or type(mid) is not int
                or type(quoted_bot.get("id")) is not int
                or quoted_bot["id"] != bot_id):
            return None
        with self._lock:
            for rec in self.history.values():
                if rec.get("message_id") == mid:
                    return dict(rec)
        return None

    def request_stop_confirmation(self, work_id: int, message_id: int,
                                  user_id: int) -> bool:
        with self._lock:
            rec = self.history.get(str(work_id))
            if (rec is None or rec["message_id"] != message_id
                    or rec["phase"] in _TERMINAL):
                return False
            already = rec.get("stop_confirm_user")
            if (already is not None and already != user_id and
                    time.time() < rec.get("stop_confirm_until", 0)):
                return False
            # Durable before an ambiguous edit: if the button becomes visible,
            # pressing it still checks the authorized creator and short expiry.
            rec["stop_confirm_user"] = user_id
            rec["stop_confirm_until"] = time.time() + 60
            self._save()
            try:
                self.api.call("editMessageReplyMarkup", {
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "reply_markup": self._markup(rec),
                })
            except Exception as exc:
                LOG.warning("Не удалось показать подтверждение остановки (%s)",
                            type(exc).__name__)
                return False
            return True

    def stop_is_confirmed(self, work_id: int, message_id: int,
                          user_id: int) -> bool:
        with self._lock:
            rec = self.history.get(str(work_id))
            return bool(
                rec is not None and rec["message_id"] == message_id
                and rec["phase"] not in _TERMINAL
                and type(rec.get("stop_confirm_user")) is int
                and rec["stop_confirm_user"] == user_id
                and isinstance(rec.get("stop_confirm_until"), (int, float))
                and time.time() <= rec["stop_confirm_until"]
            )

    def cancel_stop_confirmation(self, work_id: int, message_id: int,
                                 user_id: int) -> bool:
        with self._lock:
            rec = self.history.get(str(work_id))
            if (rec is None or rec["message_id"] != message_id
                    or rec["phase"] in _TERMINAL
                    or rec.get("stop_confirm_user") != user_id):
                return False
            rec.pop("stop_confirm_user", None)
            rec.pop("stop_confirm_until", None)
            self._save()
            try:
                self.api.call("editMessageReplyMarkup", {
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "reply_markup": self._markup(rec),
                })
            except Exception as exc:
                LOG.warning("Не удалось закрыть подтверждение остановки (%s)",
                            type(exc).__name__)
                return False
            return True

    def interrupted(self, work_id: int) -> None:
        self.update(work_id, "interrupted",
                    note="Предыдущая задача не возобновлялась автоматически.")
