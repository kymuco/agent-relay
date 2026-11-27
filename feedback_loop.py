"""NA-06.6: evidence-driven continuation, not a new authority or retry policy."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from github_ci import _NoRedirect
from local_diagnostics import bounded_excerpt

_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_STATES = ("evaluating", "resolved", "not_needed", "developer_finished",
           "developer_blocked", "identity_unavailable", "remote_changed",
           "no_progress", "verification_weakened", "nonrepairable",
           "human_amendments_pending", "ambiguous")
_MAX_BODY = 1024 * 1024


def parse_feedback_projects(raw: str, projects: Mapping[str, str]) -> frozenset[str]:
    if not raw.strip():
        return frozenset()
    aliases = [part.strip() for part in raw.split(",")]
    if len(set(aliases)) != len(aliases) or any(alias not in projects for alias in aliases):
        raise ValueError("WORK_FEEDBACK_PROJECTS requires unique configured GitHub project aliases")
    return frozenset(aliases)


def read_commit_identity(repo: str, sha: str, *, permitted: Callable[[], bool]) -> dict | None:
    """Read exact GitHub Git commit metadata; no clone, no diff, no code execution."""
    if not _REPO.fullmatch(repo) or any(p in (".", "..") for p in repo.split("/")) or not _SHA.fullmatch(sha):
        raise ValueError("invalid commit identity")
    if not permitted():
        return None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "AgentRelay-Feedback",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("WORK_CI_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request("https://api.github.com/repos/" + repo + "/git/commits/" + sha,
                      headers=headers, method="GET")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=8) as response:
            raw = response.read(_MAX_BODY + 1)
        if len(raw) > _MAX_BODY or not permitted():
            return None
        value = json.loads(raw)
        if type(value) is not dict or value.get("sha") != sha:
            return None
        tree, parents = value.get("tree"), value.get("parents")
        if not isinstance(tree, dict) or not isinstance(parents, list):
            return None
        tree_sha = tree.get("sha")
        if not isinstance(tree_sha, str) or not _SHA.fullmatch(tree_sha):
            return None
        ids = [p.get("sha") if isinstance(p, dict) else None for p in parents]
        if any(not isinstance(p, str) or not _SHA.fullmatch(p) for p in ids):
            return None
        return {"sha": sha, "tree": tree_sha, "parents": ids}
    except HTTPError as exc:
        exc.close()
        return None
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def repairable(verification: dict | None, ci: dict | None, execution: dict | None) -> bool:
    # Pending, missing, cancelled, timeout and unavailable evidence are not a
    # confirmed repairable failure. No automatic rerun of an ambiguous effect.
    return bool(verification and verification.get("admission") == "admitted"
                and ((ci and ci.get("status") == "failed")
                     or (execution and execution.get("status") in ("failed", "no_tests"))))


def preserves_checks(before: dict, after: dict) -> bool:
    if after.get("admission") != "admitted":
        return False
    checks = {"cloud": {"cloud"}, "local": {"local"}, "hybrid": {"cloud", "local"}}
    old, new = checks.get(before.get("route"), set()), checks.get(after.get("route"), set())
    return bool(old and old <= new and
                ("local" not in old or before.get("preset") == after.get("preset")))


def feedback_record(data: Any, sha: str) -> dict | None:
    if (not isinstance(data, dict) or not isinstance(sha, str) or not _SHA.fullmatch(sha)
            or data.get("commit_sha") != sha or data.get("status") not in _STATES):
        return None
    count, commits, summary = data.get("attempts"), data.get("commits"), data.get("summary")
    if (type(count) is not int or count < 0 or not isinstance(commits, list)
            or not 1 <= len(commits) <= 12 or len(commits) > count + 1
            or any(not isinstance(v, str) or not _SHA.fullmatch(v) for v in commits)
            or len(set(commits)) != len(commits) or commits[-1] != sha
            or not isinstance(summary, str) or len(summary) > 1200):
        return None
    return {"commit_sha": sha, "status": data["status"], "attempts": count,
            "commits": list(commits), "summary": summary}


def feedback_label(data: Any, sha: str, *, detail=False) -> str:
    record = feedback_record(data, sha)
    if not record:
        return ""
    label = "Feedback Loop: " + record["status"] + " · попыток: " + str(record["attempts"])
    if detail and record["summary"]:
        label += " · " + record["summary"]
    if detail and len(record["commits"]) > 1:
        label += " · SHA: " + " → ".join(v[:12] for v in record["commits"])
    return label


def feedback_context(evidence: dict, output: str) -> dict:
    """Small transient model context; no log replay or command authority."""
    from work_feedback import result_record
    clean = result_record(evidence, 0)
    clean["developer_summary"] = clean["developer_summary"][:1200]
    return {"observed": clean, "failure_excerpt": bounded_excerpt(output)}
