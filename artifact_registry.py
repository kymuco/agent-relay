"""NA-06.7.1/2: bounded artifact declarations and immutable Git repository locators.

An artifact is observed metadata, not proof of downloaded bytes, a new grant,
or a local file path. Source adapters can be added without changing task identity.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener

_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_MAX = 5
_MAX_BODY = 4 * 1024 * 1024
_SOURCE = "github_repository"
_UNAVAILABLE = ("not_authorized", "verification_incomplete", "metadata_unavailable",
                "not_found", "not_regular_file")


def parse_artifact_projects(raw: str, projects: Mapping[str, str]) -> frozenset[str]:
    if not raw.strip():
        return frozenset()
    aliases = [part.strip() for part in raw.split(",")]
    if len(set(aliases)) != len(aliases) or any(alias not in projects for alias in aliases):
        raise ValueError("WORK_ARTIFACT_PROJECTS requires unique configured GitHub project aliases")
    return frozenset(aliases)


def _path(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 180:
        return False
    if any(ord(c) < 32 or ord(c) == 127 or c in "\\:%?#" for c in value):
        return False
    # Repository-relative, canonical; no hidden credentials or VCS metadata.
    parts = value.split("/")
    return (len(parts) <= 12 and all(p and p not in (".", "..") and p.strip() == p
            and p.casefold() not in (".git", ".ssh", ".aws", ".netrc", ".npmrc",
                                      "id_rsa", "id_ed25519")
            and not p.casefold().startswith(".env")
            for p in parts))


def _valid_repo(repo: Any) -> bool:
    return isinstance(repo, str) and bool(_REPO.fullmatch(repo)) and all(
        part not in (".", "..") for part in repo.split("/"))


def parse_artifact_requests(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > _MAX:
        raise ValueError("artifacts must be a bounded array")
    result = []
    seen = set()
    for item in value:
        if (type(item) is not dict or set(item) != {"source", "path", "required", "delivery"}
                or item["source"] != _SOURCE or item["delivery"] != "link"
                or type(item["required"]) is not bool or not _path(item["path"])):
            raise ValueError("invalid repository artifact declaration")
        if item["path"] in seen:
            raise ValueError("duplicate artifact path")
        seen.add(item["path"])
        result.append(dict(item))
    return result


def merge_required(previous: list[dict], current: list[dict]) -> list[dict]:
    """A subsequent repair may not silently drop or downgrade a required file."""
    merged = {r["path"]: dict(r) for r in parse_artifact_requests(current)}
    for item in parse_artifact_requests(previous):
        if item["required"]:
            if item["path"] in merged:
                merged[item["path"]]["required"] = True
            else:
                merged[item["path"]] = dict(item)
    if len(merged) > _MAX:
        # Prefer required outputs over optional ones; never silently drop a required output.
        required = [r for r in merged.values() if r["required"]]
        if len(required) > _MAX:
            raise ValueError("too many required artifacts across revisions")
        merged = {r["path"]: r for r in (required + [r for r in merged.values()
                                                     if not r["required"]])[:_MAX]}
    return list(merged.values())


def _artifact_id(repo: str, sha: str, path: str) -> str:
    return hashlib.sha256(json.dumps([_SOURCE, repo.lower(), sha, path],
                                   ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:24]


def _record(request: dict, repo: str, sha: str, *, reason: str = "",
            blob: str = "", size: int | None = None) -> dict:
    return {**request, "artifact_id": _artifact_id(repo, sha, request["path"]),
            "repository": repo, "commit_sha": sha,
            "filename": request["path"].rsplit("/", 1)[-1],
            "status": "unavailable" if reason else "available",
            "reason": reason, "git_blob_sha": blob, "size": size,
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _tree_entries(repo: str, tree_sha: str, *, permitted: Callable[[], bool]) -> dict | None:
    """Read Git tree metadata only; never fetch blob contents or signed download URLs."""
    from github_ci import _NoRedirect
    if not _valid_repo(repo) or not _SHA.fullmatch(tree_sha) or not permitted():
        return None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "AgentRelay-Artifacts",
               "X-GitHub-Api-Version": "2022-11-28"}
    token = os.getenv("WORK_CI_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request("https://api.github.com/repos/" + repo + "/git/trees/" + tree_sha
                      + "?recursive=1", headers=headers, method="GET")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=8) as response:
            raw = response.read(_MAX_BODY + 1)
        if len(raw) > _MAX_BODY or not permitted():
            return None
        data = json.loads(raw)
        if (type(data) is not dict or data.get("sha") != tree_sha
                or data.get("truncated") is not False or not isinstance(data.get("tree"), list)):
            return None
        entries = {}
        for entry in data["tree"]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                    or entry["path"] in entries):
                return None
            entries[entry["path"]] = entry
        return entries
    except HTTPError as exc:
        exc.close()
        return None
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def register_artifacts(requests: list[dict], repo: str, sha: str, *,
                       enabled: bool, verified_final: bool,
                       permitted: Callable[[], bool]) -> list[dict] | None:
    """None means stopped. Required unavailable results can never count as delivered."""
    from feedback_loop import read_commit_identity
    requests = parse_artifact_requests(requests)
    if not _valid_repo(repo) or not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ValueError("invalid artifact repository or commit")
    if not permitted():
        return None
    if not requests:
        return []
    reason = "" if enabled and verified_final else (
        "not_authorized" if not enabled else "verification_incomplete")
    entries = None
    if not reason:
        identity = read_commit_identity(repo, sha, permitted=permitted)
        if not permitted():
            return None
        if identity is not None:
            entries = _tree_entries(repo, identity["tree"], permitted=permitted)
        if not permitted():
            return None
        if entries is None:
            reason = "metadata_unavailable"
    records = []
    for item in requests:
        if not permitted():
            return None
        denial = reason
        blob, size = "", None
        if not denial:
            entry = entries.get(item["path"])
            if entry is None:
                denial = "not_found"
            elif (entry.get("type") != "blob" or entry.get("mode") not in ("100644", "100755")
                  or not isinstance(entry.get("sha"), str) or not _SHA.fullmatch(entry["sha"])
                  or type(entry.get("size")) is not int or entry["size"] < 0):
                denial = "not_regular_file"
            else:
                blob, size = entry["sha"], entry["size"]
        records.append(_record(item, repo, sha, reason=denial, blob=blob, size=size))
    return records


def artifact_records(data: Any, repo: str, sha: str) -> list[dict]:
    """Validate persisted evidence; derive identity/filename, never trust saved URLs."""
    if (not _valid_repo(repo) or not isinstance(sha, str) or not _SHA.fullmatch(sha)
            or not isinstance(data, list) or len(data) > _MAX):
        return []
    result, seen = [], set()
    for item in data:
        if not isinstance(item, dict) or item.get("repository") != repo or item.get("commit_sha") != sha:
            return []
        try:
            request = parse_artifact_requests([{k: item[k] for k in
                          ("source", "path", "required", "delivery")}])[0]
            stamp = item["observed_at"]
            if not isinstance(stamp, str) or len(stamp) > 50 or datetime.fromisoformat(stamp).tzinfo is None:
                return []
        except (KeyError, ValueError, TypeError):
            return []
        path = request["path"]
        identifier = _artifact_id(repo, sha, path)
        if (path in seen or item.get("artifact_id") != identifier
                or item.get("filename") != path.rsplit("/", 1)[-1]):
            return []
        status, reason, blob, size = (item.get(k) for k in
                                      ("status", "reason", "git_blob_sha", "size"))
        if status == "available":
            if (reason != "" or not isinstance(blob, str) or not _SHA.fullmatch(blob)
                    or type(size) is not int or size < 0):
                return []
        elif status == "unavailable":
            if reason not in _UNAVAILABLE or blob != "" or size is not None:
                return []
        else:
            return []
        seen.add(path)
        result.append({**request, "artifact_id": identifier, "repository": repo,
                       "commit_sha": sha, "filename": path.rsplit("/", 1)[-1],
                       "status": status, "reason": reason, "git_blob_sha": blob,
                       "size": size, "observed_at": stamp})
    return result


def withhold_artifacts(records: list[dict]) -> list[dict]:
    return [dict(item, status='unavailable', reason='verification_incomplete',
                 git_blob_sha='', size=None) if item['status'] == 'available' else dict(item)
            for item in records]


def artifact_url(record: dict) -> str | None:
    records = artifact_records([record], record.get("repository"), record.get("commit_sha"))
    if not records or records[0]["status"] != "available":
        return None
    item = records[0]
    return ("https://github.com/" + item["repository"] + "/blob/" + item["commit_sha"]
            + "/" + quote(item["path"], safe="/"))


def result_artifacts(result: Mapping[str, Any]) -> list[dict]:
    return artifact_records(result.get("artifacts"), result.get("repository"),
                            result.get("commit_sha")) if result.get("remote_verified") is True else []


def artifact_label(record: dict, *, include_url: bool = False) -> str:
    if record["status"] != "available":
        return record["path"] + " · недоступен (" + record["reason"] + ")" + (
            " · обязательный" if record["required"] else "")
    label = record["path"] + " · " + str(record["size"]) + " Б · GitHub"
    url = artifact_url(record)
    return label + (" · " + url if include_url and url else "")
