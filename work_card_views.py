"""Pure Telegram work-card views and sanitized task journal rendering.

Only Host-observed events and trusted worker reports are displayed. The view
never performs work, asks a model, or consumes Telegram callback authority.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

_PHASE_LABELS = {
    "queued": "📥 Поручение принято",
    "coordinator": "🧠 Составляю план",
    "developer": "💻 Разработчик работает с GitHub",
    "revision": "🔁 Учитываю уточнения",
    "verify": "🔎 Проверяю ветку и коммит",
    "local_test": "🧪 Проверяю код",
    "review": "📝 Проверяю результат",
    "completed": "✅ Готово",
    "needs_review": "⚠️ Нужны уточнения",
    "failed": "❌ Задача не завершена",
    "stopped": "⏹ Задача остановлена",
    "interrupted": "⚠️ Прервана",
}
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[a-f0-9]{40}$", re.I)
_FILENAME = re.compile(r"(?<![\w./\\-])([A-Za-z0-9][\w.-]{0,70}\.(?:py|js|ts|tsx|jsx|json|md|html|css|yml|yaml|go|rs|java|sh|ps1))(?![\w./\\-])", re.I)
_TESTS_RU = re.compile(r"(?iu)\b(\d{1,5})\s*(?:unittest[\s-]*)?тест(?:а|ов|ы)?\b.{0,65}?\b(?:пройдены|пройдено|прошли|успешн\w*)")
_TESTS_EN = re.compile(r"(?i)\b(\d{1,5})\s+(?:unittest\s+)?tests?\s+(?:passed|successful|succeeded)\b")
_TESTS_RAN = re.compile(r"(?i)\bRan\s+(\d{1,5})\s+tests?\b.{0,150}?\bOK\b", re.S)


def _plain(value: Any, limit: int) -> str:
    value = str(value or "")
    return " ".join(value.split())[:limit]


def _h(value: Any, limit: int) -> str:
    return html.escape(_plain(value, limit), quote=True)


def _worker_test_count(text: str) -> int | None:
    for rx in (_TESTS_RU, _TESTS_EN, _TESTS_RAN):
        match = rx.search(text)
        if match:
            return int(match.group(1))
    return None


def _files(text: str) -> list[str]:
    names: list[str] = []
    for m in _FILENAME.finditer(text[:3500]):
        if m.group(1) not in names:
            names.append(m.group(1))
        if len(names) >= 6:
            break
    return names


def github_commit_url(result: dict[str, Any]) -> str | None:
    repo = result.get("repository", "")
    sha = result.get("commit_sha", "")
    if (isinstance(repo, str) and isinstance(sha, str) and
            _REPO.fullmatch(repo) and _SHA.fullmatch(sha) and
            result.get("remote_verified") is True):
        return "https://github.com/" + repo + "/commit/" + sha
    return None


def work_keyboard(work_id: int, detail: bool, result: dict[str, Any]) -> dict[str, Any]:
    action = "c" if detail else "d"
    label = "Свернуть" if detail else "Подробнее"
    rows = [[
        {"text": label, "callback_data": f"wp:{action}:{int(work_id)}"},
        {"text": "Журнал", "callback_data": f"wp:j:{int(work_id)}"},
    ]]
    link = github_commit_url(result)
    if link:
        rows.append([{"text": "GitHub", "url": link}])
    return {"inline_keyboard": rows}


def render_work_card(record: dict[str, Any], *, detail: bool = False) -> str:
    work_id = int(record["work_id"])
    phase = str(record.get("phase", "queued"))
    if phase not in _PHASE_LABELS:
        phase = "queued"
    project = _h(record.get("project"), 60)
    task = _h(record.get("task"), 170)
    result = record.get("result")
    if not isinstance(result, dict):
        result = {}
    worker = _plain(result.get("developer_summary", ""), 2200)
    tests = _worker_test_count(worker)
    files = _files(worker)
    lines = [f"<b>Задача #{work_id} · {project}</b>", _PHASE_LABELS[phase], "",
             f"<i>{task}</i>"]
    if not detail:
        if phase == "completed":
            if files:
                lines.append("\nФайлы: " + ", ".join(f"<code>{_h(name, 75)}</code>" for name in files[:3]))
            elif worker:
                lines.append("\n" + _h(worker, 180))
            if tests is not None:
                lines.append(f"✅ {tests} тестов пройдены")
            if result.get("remote_verified"):
                lines.append("✅ Коммит подтверждён на GitHub")
        elif phase in ("needs_review", "failed", "stopped", "interrupted"):
            note = record.get("note", "")
            if note:
                lines.append("\n" + _h(note, 220))
        elif phase != "queued":
            lines.append("\nМожно продолжать общение с Ragna.")
    else:
        lines.extend(["", "<b>Подробности</b>"])
        if files:
            lines.append("<b>Файлы:</b>")
            lines.extend("• <code>" + _h(name, 75) + "</code>" for name in files)
        if worker:
            lines.extend(["", "<b>Результат разработчика:</b>", _h(worker, 1150)])
        if tests is not None:
            lines.append(f"\n✅ {tests} тестов пройдены")
        if result.get("remote_verified"):
            lines.append("✅ Коммит подтверждён на GitHub")
        elif phase not in ("queued", "coordinator", "developer"):
            lines.append("Коммит не подтверждён")
        if result.get("branch"):
            lines.append("Ветка: <code>" + _h(result["branch"], 120) + "</code>")
        if result.get("commit_sha"):
            lines.append("SHA: <code>" + _h(result["commit_sha"], 40) + "</code>")
        if result.get("local_check"):
            lines.extend(["", "<b>Проверки:</b>", _h(result["local_check"], 450)])
        pending = result.get("pending_amendments", 0)
        if type(pending) is int and pending:
            lines.append(f"\n⚠️ {pending} уточнений ещё не применены")
        note = record.get("note", "")
        if note and phase in ("failed", "stopped", "interrupted", "needs_review"):
            lines.append("\n" + _h(note, 200))
    return "\n".join(lines)[:3500]


def journal_event(phase: str, note: str = "") -> dict[str, str]:
    label = _PHASE_LABELS.get(phase, phase)
    return {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "phase": str(phase)[:32],
            "label": label,
            "note": _plain(note, 280)}


def render_work_journal(record: dict[str, Any]) -> bytes:
    """No raw prompts, credentials, logs, or model/tool transcripts."""
    result = record.get("result", {})
    if not isinstance(result, dict):
        result = {}
    parts = [
        "AgentRelay — журнал задачи #" + str(record["work_id"]),
        "Проект: " + _plain(record.get("project"), 100),
        "Поручение: " + _plain(record.get("task"), 250),
        "Состояние: " + _plain(record.get("phase"), 40),
        "",
        "События AgentRelay:",
    ]
    for event in record.get("events", [])[-40:]:
        if not isinstance(event, dict):
            continue
        parts.append(f"[{_plain(event.get('at'), 35)}] "
                     f"{_plain(event.get('label'), 90)}"
                     + (" — " + _plain(event.get("note"), 180)
                        if event.get("note") else ""))
    if result:
        parts.extend([
            "", "Отчёт доверенного Developer:",
            _plain(result.get("developer_summary"), 1400) or "(нет отчёта)",
            "", "Подтверждение AgentRelay:",
            "Репозиторий: " + _plain(result.get("repository"), 120),
            "Ветка: " + _plain(result.get("branch"), 130),
            "Коммит: " + _plain(result.get("commit_sha"), 40),
            "Удалённый SHA: " + (
                "подтверждён" if result.get("remote_verified") else "не подтверждён"),
            "Локальная проверка: " + _plain(result.get("local_check"), 500),
            "", "Вывод Coordinator:",
            _plain(result.get("coordinator_summary"), 800) or "(нет отчёта)",
        ])
    return ("\n".join(parts) + "\n").encode("utf-8")
