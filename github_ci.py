"""NA-06.2: read-only GitHub Actions evidence for an exact verified commit.

No workflow dispatch, reruns, repository changes or arbitrary URLs. A snapshot
of visible runs is not proof that every required workflow was triggered.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_RUNS = 100
_MAX_BODY = 4 * 1024 * 1024
_LABELS = {
    "passed": "наблюдаемые workflow успешно завершены",
    "failed": "есть неуспешные workflow",
    "pending": "workflow ещё выполняются",
    "incomplete": "не все workflow завершились успешно (отмена, пропуск или иной итог)",
    "not_configured": "GitHub API не показывает зарегистрированных workflow",
    "not_run": "запуски для этого SHA и ветки не обнаружены",
    "unavailable": "результаты недоступны или не могут быть проверены",
}
_FAILURE = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
_ACTIVE = frozenset({"queued", "in_progress", "requested", "waiting", "pending"})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(repo: str, suffix: str, query: dict[str, Any], *, timeout: float) -> dict:
    if not _REPO.fullmatch(repo) or any(p in (".", "..") for p in repo.split("/")):
        raise ValueError("invalid repository")
    if suffix not in ("runs", "workflows"):
        raise ValueError("invalid CI endpoint")
    url = "https://api.github.com/repos/" + repo + "/actions/" + suffix + "?" + urlencode(query)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "AgentRelay-CI",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("WORK_CI_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, headers=headers, method="GET")
    with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(_MAX_BODY + 1)
    if len(raw) > _MAX_BODY:
        raise ValueError("oversized CI response")
    value = json.loads(raw)
    if type(value) is not dict:
        raise ValueError("invalid CI payload")
    return value


def _observe_runs(data: dict, sha: str, branch: str) -> tuple[str, list[int]]:
    total, runs = data.get("total_count"), data.get("workflow_runs")
    if (type(total) is not int or not 0 <= total <= _MAX_RUNS
            or not isinstance(runs, list) or len(runs) != total):
        raise ValueError("incomplete CI enumeration")
    ids = []
    states = []
    for run in runs:
        if (not isinstance(run, dict) or run.get("head_sha") != sha
                or run.get("head_branch") != branch
                or type(run.get("id")) is not int or run["id"] <= 0
                or run["id"] in ids):
            raise ValueError("CI run identity mismatch")
        ids.append(run["id"])
        status, conclusion = run.get("status"), run.get("conclusion")
        if not isinstance(status, str):
            raise ValueError("invalid CI status")
        if status in _ACTIVE and conclusion is None:
            states.append("pending")
        elif status == "completed" and isinstance(conclusion, str) and conclusion:
            states.append("passed" if conclusion == "success" else
                          "failed" if conclusion in _FAILURE else "incomplete")
        else:
            raise ValueError("unrecognized CI state")
    for state in ("failed", "pending", "incomplete", "passed"):
        if state in states:
            return state, sorted(ids)
    return "not_run", []


def _record(status: str, sha: str, ids: list[int]) -> dict:
    return {"status": status, "commit_sha": sha, "run_count": len(ids),
            "run_ids": ids, "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def observe_ci(repo: str, branch: str, sha: str, *, wait_seconds: int,
               permitted: Callable[[], bool]) -> dict | None:
    """Return bounded evidence; None means stopped. No external writes."""
    if not _REPO.fullmatch(repo) or not _SHA.fullmatch(sha) or not branch:
        raise ValueError("invalid CI identity")
    if not 0 <= wait_seconds <= 300:
        raise ValueError("invalid CI wait")
    deadline = time.monotonic() + wait_seconds
    # At least one observation, at most 61; socket reads have a separate timeout.
    for _ in range(61):
        if not permitted():
            return None
        try:
            state, ids = _observe_runs(_get(repo, "runs",
                {"head_sha": sha, "branch": branch, "per_page": _MAX_RUNS},
                timeout=8), sha, branch)
            if not permitted():
                return None
            if state == "not_run":
                workflows = _get(repo, "workflows", {"per_page": 1}, timeout=8)
                count = workflows.get("total_count")
                if type(count) is not int or count < 0:
                    raise ValueError("invalid workflow count")
                if count == 0:
                    state = "not_configured"
            if not permitted():
                return None
        except (OSError, ValueError, TypeError, RecursionError):
            return _record("unavailable", sha, []) if permitted() else None
        if state not in ("pending", "not_run") or time.monotonic() >= deadline:
            return _record(state, sha, ids)
        until = min(deadline, time.monotonic() + 5)
        while time.monotonic() < until:
            if not permitted():
                return None
            time.sleep(min(.25, max(0, until - time.monotonic())))
    return _record(state, sha, ids) if permitted() else None


def ci_record(data: Any, sha: str) -> dict | None:
    """Bound persisted/rendered evidence, reject mismatched or malformed identity."""
    if (not isinstance(data, dict) or not isinstance(sha, str) or not _SHA.fullmatch(sha)
            or data.get("commit_sha") != sha or not isinstance(data.get("status"), str)
            or data["status"] not in _LABELS):
        return None
    ids, count, stamp = data.get("run_ids"), data.get("run_count"), data.get("observed_at")
    if (type(count) is not int or not isinstance(ids, list) or count != len(ids)
            or count > _MAX_RUNS or any(type(i) is not int or i <= 0 for i in ids)
            or len(set(ids)) != count or not isinstance(stamp, str) or len(stamp) > 50):
        return None
    if (data["status"] in ("passed", "failed", "pending", "incomplete")) != bool(ids):
        return None
    try:
        observed = datetime.fromisoformat(stamp)
        if observed.tzinfo is None:
            return None
    except ValueError:
        return None
    return {"status": data["status"], "commit_sha": sha, "run_count": count,
            "run_ids": list(ids), "observed_at": observed.isoformat(timespec="seconds")}


def ci_label(data: Any, sha: str) -> str:
    value = ci_record(data, sha)
    if value is None:
        return ""
    return "GitHub CI: " + _LABELS[value["status"]] + (
        f" ({value['run_count']} запусков)" if value["run_count"] else "")
