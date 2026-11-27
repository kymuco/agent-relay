"""Read-only Actions archive metadata for successful exact-SHA runs.

No archive download, signed redirect, workflow dispatch or rerun. Run attempt
one only: GitHub's per-run artifact list does not identify producing attempts.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from github_ci import _NoRedirect, ci_record

_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_BODY = 4 * 1024 * 1024
_MAX_RUNS = 20
_MAX_ARTIFACTS = 100


def _positive(value: Any) -> bool:
    return type(value) is int and 0 < value <= 2**63-1


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _get(repo: str, run_id: int, *, artifacts: bool, permitted: Callable[[], bool]) -> dict | None:
    if (not isinstance(repo, str) or not _REPO.fullmatch(repo)
            or any(part in (".", "..") for part in repo.split("/"))
            or not _positive(run_id) or not permitted()):
        return None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "AgentRelay-ActionsArtifacts",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("WORK_CI_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    url = "https://api.github.com/repos/" + repo + "/actions/runs/" + str(run_id)
    if artifacts:
        url += "/artifacts?per_page=100"
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers=headers, method="GET"), timeout=8) as response:
            raw = response.read(_MAX_BODY + 1)
        if len(raw) > _MAX_BODY or not permitted():
            return None
        value = json.loads(raw)
        return value if type(value) is dict else None
    except HTTPError as exc:
        exc.close()
        return None
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def _repository_id(value: Any, repo: str) -> int | None:
    if (not isinstance(value, dict) or not isinstance(value.get("full_name"), str)
            or value["full_name"].casefold() != repo.casefold() or not _positive(value.get("id"))):
        return None
    return value["id"]


def collect_actions_artifacts(repo: str, branch: str, sha: str, ci: dict | None, *,
                              permitted: Callable[[], bool]) -> tuple[str, dict[str, list[dict]]] | None:
    """Enumerate only already observed successful runs; fail closed on partial results.

    Result is (reason, exact-name candidates); None means stopped. Expired
    candidates are retained for unambiguous reporting, not made downloadable.
    """
    if not permitted():
        return None
    observed = ci_record(ci, sha)
    if not observed or observed["status"] != "passed":
        return "actions_ci_not_passed", {}
    if len(observed["run_ids"]) > _MAX_RUNS:
        return "metadata_unavailable", {}
    by_name, seen_ids = {}, set()
    for run_id in observed["run_ids"]:
        if not permitted():
            return None
        run = _get(repo, run_id, artifacts=False, permitted=permitted)
        if not permitted():
            return None
        if (not run or run.get("id") != run_id or run.get("head_sha") != sha
                or run.get("head_branch") != branch or run.get("status") != "completed"
                or run.get("conclusion") != "success"):
            return "actions_run_mismatch", {}
        repo_id = _repository_id(run.get("repository"), repo)
        if repo_id is None or _repository_id(run.get("head_repository"), repo) != repo_id:
            return "actions_run_mismatch", {}
        # Artifact lists are per run, not per attempt. Do not risk selecting
        # a stale failed-attempt artifact after an external rerun.
        if type(run.get("run_attempt")) is not int or run["run_attempt"] != 1:
            return "actions_attempt_ambiguous", {}
        started = timestamp(run.get("run_started_at"))
        if started is None:
            return "metadata_unavailable", {}
        data = _get(repo, run_id, artifacts=True, permitted=permitted)
        if not permitted():
            return None
        if not data:
            return "metadata_unavailable", {}
        total, entries = data.get("total_count"), data.get("artifacts")
        if (type(total) is not int or not 0 <= total <= _MAX_ARTIFACTS
                or not isinstance(entries, list) or len(entries) != total):
            return "metadata_unavailable", {}
        for item in entries:
            if not isinstance(item, dict):
                return "metadata_unavailable", {}
            aid, name, size, expired = (item.get(k) for k in ("id", "name", "size_in_bytes", "expired"))
            created, expires = timestamp(item.get("created_at")), timestamp(item.get("expires_at"))
            digest = item.get("digest")
            linked = item.get("workflow_run")
            if (not _positive(aid) or aid in seen_ids or not isinstance(name, str)
                    or not 1 <= len(name) <= 255 or type(size) is not int or size < 0
                    or type(expired) is not bool or created is None or expires is None
                    or created < started or expires <= created
                    or (digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)))):
                return "metadata_unavailable", {}
            # The per-run endpoint establishes association. When GitHub also
            # provides the nested run identity, independently cross-check it.
            if linked is not None and (not isinstance(linked, dict) or linked.get("id") != run_id
                    or linked.get("repository_id") != repo_id or linked.get("head_repository_id") != repo_id
                    or linked.get("head_branch") != branch or linked.get("head_sha") != sha):
                return "actions_run_mismatch", {}
            seen_ids.add(aid)
            by_name.setdefault(name, []).append({
                "github_artifact_id": aid, "run_id": run_id, "size": size,
                "expires_at": expires.isoformat(timespec="seconds"),
                "created_at": created.isoformat(timespec="seconds"),
                "digest": digest or "",
                "expired": expired or expires <= datetime.now(timezone.utc),
            })
        # Detect an external rerun racing the artifact listing.
        after = _get(repo, run_id, artifacts=False, permitted=permitted)
        if not permitted():
            return None
        if (not after or any(after.get(k) != run.get(k) for k in
                            ("id", "head_sha", "head_branch", "status", "conclusion", "run_attempt", "run_started_at"))
                or _repository_id(after.get("repository"), repo) != repo_id
                or _repository_id(after.get("head_repository"), repo) != repo_id):
            return "actions_run_mismatch", {}
    return ("", by_name) if permitted() else None
