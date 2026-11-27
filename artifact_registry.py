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
_ACTIONS = "github_actions"
_ACTION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,119}")
_UNAVAILABLE = ("not_authorized", "verification_incomplete", "metadata_unavailable",
                "not_found", "not_regular_file", "actions_ci_not_passed",
                "actions_run_mismatch", "actions_attempt_ambiguous", "ambiguous_name", "expired")


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


def _request_key(item: dict) -> tuple[str, str]:
    return item["source"], item["path"] if item["source"] == _SOURCE else item["name"]


def parse_artifact_requests(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > _MAX:
        raise ValueError("artifacts must be a bounded array")
    result = []
    seen = set()
    for item in value:
        if (type(item) is not dict or item.get("delivery") != "link"
                or type(item.get("required")) is not bool):
            raise ValueError("invalid artifact declaration")
        source = item.get("source")
        if source == _SOURCE:
            if set(item) != {"source", "path", "required", "delivery"} or not _path(item["path"]):
                raise ValueError("invalid repository artifact declaration")
        elif source == _ACTIONS:
            if (set(item) != {"source", "name", "required", "delivery"}
                    or not isinstance(item["name"], str) or not _ACTION_NAME.fullmatch(item["name"])
                    or item["name"].strip() != item["name"]):
                raise ValueError("invalid Actions artifact declaration")
        else:
            raise ValueError("unknown artifact source")
        key = _request_key(item)
        if key in seen:
            raise ValueError("duplicate artifact locator")
        seen.add(key)
        result.append(dict(item))
    return result


def merge_required(previous: list[dict], current: list[dict]) -> list[dict]:
    """A subsequent repair may not silently drop or downgrade a required file."""
    merged = {_request_key(r): dict(r) for r in parse_artifact_requests(current)}
    for item in parse_artifact_requests(previous):
        if item["required"]:
            key = _request_key(item)
            if key in merged:
                merged[key]["required"] = True
            else:
                merged[key] = dict(item)
    if len(merged) > _MAX:
        # Prefer required outputs over optional ones; never silently drop a required output.
        required = [r for r in merged.values() if r["required"]]
        if len(required) > _MAX:
            raise ValueError("too many required artifacts across revisions")
        merged = {_request_key(r): r for r in (required + [r for r in merged.values()
                                                     if not r["required"]])[:_MAX]}
    return list(merged.values())


def _artifact_id(repo: str, sha: str, path: str, source: str = _SOURCE) -> str:
    return hashlib.sha256(json.dumps([source, repo.lower(), sha, path],
                                   ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:24]


def _record(request: dict, repo: str, sha: str, *, reason: str = "",
            blob: str = "", size: int | None = None) -> dict:
    return {**request, "artifact_id": _artifact_id(repo, sha, request["path"]),
            "repository": repo, "commit_sha": sha,
            "filename": request["path"].rsplit("/", 1)[-1],
            "status": "unavailable" if reason else "available",
            "reason": reason, "git_blob_sha": blob, "size": size,
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _actions_record(request: dict, repo: str, sha: str, *, reason: str = "",
                    metadata: dict | None = None) -> dict:
    data = metadata or {}
    return {**request, "artifact_id": _artifact_id(repo, sha, request["name"], _ACTIONS),
            "repository": repo, "commit_sha": sha, "filename": request["name"] + ".zip",
            "status": "unavailable" if reason else "available", "reason": reason,
            "github_artifact_id": data.get("github_artifact_id"), "run_id": data.get("run_id"),
            "size": data.get("size"), "expires_at": data.get("expires_at"),
            "created_at": data.get("created_at"), "digest": data.get("digest", ""),
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
                       permitted: Callable[[], bool], branch: str = '',
                       ci: dict | None = None) -> list[dict] | None:
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
    repo_reason = reason
    actions_reason = reason
    actions_entries = {}
    if not reason and any(r["source"] == _SOURCE for r in requests):
        identity = read_commit_identity(repo, sha, permitted=permitted)
        if not permitted():
            return None
        if identity is not None:
            entries = _tree_entries(repo, identity["tree"], permitted=permitted)
        if not permitted():
            return None
        if entries is None:
            repo_reason = "metadata_unavailable"
    if not reason and any(r["source"] == _ACTIONS for r in requests):
        from github_actions_artifacts import collect_actions_artifacts
        observed = collect_actions_artifacts(repo, branch, sha, ci, permitted=permitted)
        if observed is None or not permitted():
            return None
        actions_reason, actions_entries = observed
    records = []
    for item in requests:
        if not permitted():
            return None
        if item["source"] == _ACTIONS:
            denial = actions_reason
            meta = None
            if not denial:
                candidates = actions_entries.get(item["name"], [])
                if not candidates:
                    denial = "not_found"
                elif len(candidates) != 1:
                    denial = "ambiguous_name"
                elif candidates[0]["expired"]:
                    denial = "expired"
                else:
                    meta = candidates[0]
            records.append(_actions_record(item, repo, sha, reason=denial, metadata=meta))
            continue
        denial = repo_reason
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
            locator = "name" if item.get("source") == _ACTIONS else "path"
            request = parse_artifact_requests([{k: item[k] for k in
                          ("source", locator, "required", "delivery")}])[0]
            stamp = item["observed_at"]
            if not isinstance(stamp, str) or len(stamp) > 50 or datetime.fromisoformat(stamp).tzinfo is None:
                return []
        except (KeyError, ValueError, TypeError):
            return []
        key = _request_key(request)
        identifier = _artifact_id(repo, sha, key[1], key[0])
        filename = key[1] + ".zip" if key[0] == _ACTIONS else key[1].rsplit("/", 1)[-1]
        if (key in seen or item.get("artifact_id") != identifier
                or item.get("filename") != filename):
            return []
        if key[0] == _ACTIONS:
            clean = _validate_actions_record(item, request, repo, sha, identifier, stamp)
            if clean is None:
                return []
            seen.add(key)
            result.append(clean)
            continue
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
        seen.add(key)
        result.append({**request, "artifact_id": identifier, "repository": repo,
                       "commit_sha": sha, "filename": filename,
                       "status": status, "reason": reason, "git_blob_sha": blob,
                       "size": size, "observed_at": stamp})
    return result


def _validate_actions_record(item, request, repo, sha, identifier, stamp):
    from github_actions_artifacts import _positive, timestamp, _DIGEST
    status, reason, aid, run_id, size, expires, created, digest = (item.get(k) for k in
        ("status", "reason", "github_artifact_id", "run_id", "size", "expires_at", "created_at", "digest"))
    if status == "available":
        expiry, creation = timestamp(expires), timestamp(created)
        if (reason != "" or not _positive(aid) or not _positive(run_id)
                or type(size) is not int or size < 0 or expiry is None or creation is None
                or expiry <= creation or not isinstance(digest, str)
                or (digest and not _DIGEST.fullmatch(digest))):
            return None
    elif status == "unavailable":
        if (reason not in _UNAVAILABLE or any(v is not None for v in (aid, run_id, size, expires, created))
                or digest != ""):
            return None
    else:
        return None
    return {**request, "artifact_id": identifier, "repository": repo, "commit_sha": sha,
            "filename": request["name"] + ".zip", "status": status, "reason": reason,
            "github_artifact_id": aid, "run_id": run_id, "size": size,
            "expires_at": expires, "created_at": created, "digest": digest, "observed_at": stamp}


def withhold_artifacts(records: list[dict]) -> list[dict]:
    outputs = []
    for item in records:
        result = dict(item)
        if item["status"] == "available":
            result.update(status="unavailable", reason="verification_incomplete", size=None)
            if item["source"] == _ACTIONS:
                result.update(github_artifact_id=None, run_id=None, expires_at=None, created_at=None, digest="")
            else:
                result["git_blob_sha"] = ""
        outputs.append(result)
    return outputs


def artifact_url(record: dict) -> str | None:
    records = artifact_records([record], record.get("repository"), record.get("commit_sha"))
    if not records or records[0]["status"] != "available":
        return None
    item = records[0]
    if item["source"] == _ACTIONS:
        from github_actions_artifacts import timestamp
        if timestamp(item["expires_at"]) <= datetime.now(timezone.utc):
            return None
        return ("https://github.com/" + item["repository"] + "/actions/runs/" + str(item["run_id"])
                + "/artifacts/" + str(item["github_artifact_id"]))
    return ("https://github.com/" + item["repository"] + "/blob/" + item["commit_sha"]
            + "/" + quote(item["path"], safe="/"))


def bound_artifact_records(data: Any, repo: str, sha: str, ci: Any) -> list[dict]:
    """Reject available Actions metadata disconnected from successful Host CI evidence."""
    from github_ci import ci_record
    values = artifact_records(data, repo, sha)
    observed = ci_record(ci, sha)
    if any(item["source"] == _ACTIONS and item["status"] == "available"
           and (not observed or observed["status"] != "passed"
                or item["run_id"] not in observed["run_ids"]) for item in values):
        return []
    return values


def result_artifacts(result: Mapping[str, Any]) -> list[dict]:
    return bound_artifact_records(result.get("artifacts"), result.get("repository"),
                                  result.get("commit_sha"), result.get("ci")) if result.get("remote_verified") is True else []


def artifact_label(record: dict, *, include_url: bool = False) -> str:
    source, locator = _request_key(record)
    if record["status"] != "available":
        return locator + " · недоступен (" + record["reason"] + ")" + (
            " · обязательный" if record["required"] else "")
    url = artifact_url(record)
    if source == _ACTIONS:
        if not url:
            return locator + " · срок хранения истёк"
        label = (record["filename"] + " · " + str(record["size"]) + " Б · GitHub Actions · run "
                 + str(record["run_id"]) + " · до " + record["expires_at"])
    else:
        label = locator + " · " + str(record["size"]) + " Б · GitHub"
    return label + (" · " + url if include_url and url else "")
