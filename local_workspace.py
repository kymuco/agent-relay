"""NA-06.3: materialize an independently verified commit in a new Git worktree.

No project scripts, main checkout changes, automatic cleanup, branch creation
or push. Git metadata is shared with the trusted operator-configured clone;
a worktree is not an execution security sandbox.
"""
from __future__ import annotations

import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

_SHA = re.compile(r"[0-9a-f]{40}")
_ID = re.compile(r"bot-[1-9][0-9]{0,19}-work-[1-9][0-9]{0,19}-[0-9a-f]{32}")
_FILTER_KEY = re.compile(r"filter\.([A-Za-z0-9_.-]+)\.(?:smudge|clean|process|required)", re.I)
_REASONS = frozenset({"origin_mismatch", "source_not_root", "unsafe_filter_config",
                      "fetch_failed", "checkout_failed", "identity_mismatch",
                      "workspace_unavailable"})


def validate_root(value: str, projects: Iterable[Path]) -> Path:
    """Only an existing dedicated operator directory outside all project roots."""
    candidate = Path(value).expanduser()
    if not value or not candidate.is_absolute() or not candidate.is_dir():
        raise ValueError("WORK_WORKSPACE_ROOT must be an existing absolute directory")
    root = candidate.resolve(strict=True)
    if root != candidate.absolute() or root == Path(root.anchor):
        raise ValueError("WORK_WORKSPACE_ROOT must not be a symlink or filesystem root")
    for project in projects:
        source = project.resolve(strict=True)
        if root == source or root.is_relative_to(source) or source.is_relative_to(root):
            raise ValueError("WORK_WORKSPACE_ROOT must be separate from project directories")
    return root


def _record(status: str, sha: str, identity: str, reason: str = "") -> dict:
    return {"status": status, "commit_sha": sha, "workspace_id": identity,
            "reason": reason, "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def workspace_record(data: Any, sha: str) -> dict | None:
    """Persist only bounded Host facts, never local absolute paths or raw logs."""
    if (not isinstance(data, dict) or not isinstance(sha, str) or not _SHA.fullmatch(sha)
            or data.get("commit_sha") != sha
            or data.get("status") not in ("prepared", "unavailable")):
        return None
    identity, reason, stamp = data.get("workspace_id"), data.get("reason"), data.get("observed_at")
    if (not isinstance(identity, str) or not _ID.fullmatch(identity)
            or not isinstance(reason, str) or not isinstance(stamp, str) or len(stamp) > 50):
        return None
    if (data["status"] == "prepared" and reason) or (
            data["status"] == "unavailable" and reason not in _REASONS):
        return None
    try:
        observed = datetime.fromisoformat(stamp)
        if observed.tzinfo is None:
            return None
    except ValueError:
        return None
    return {"status": data["status"], "commit_sha": sha, "workspace_id": identity,
            "reason": reason, "observed_at": observed.isoformat(timespec="seconds")}


def workspace_label(data: Any, sha: str) -> str:
    record = workspace_record(data, sha)
    if record is None:
        return ""
    if record["status"] == "prepared":
        return "Local Workspace: подготовлен точный SHA (код не запускался) · " + record["workspace_id"]
    return "Local Workspace: не подтверждён (" + record["reason"] + ")"


class _Stopped(Exception):
    pass


def prepare_workspace(source: Path, root: Path, repo: str, sha: str, *,
                      bot_id: int, work_id: int,
                      permitted: Callable[[], bool]) -> dict | None:
    """One new detached worktree, checked by exact SHA; no retry on ambiguity.

    A stop fences the next Git command. An already-started command may finish;
    partial directories and worktree metadata are retained for manual review.
    """
    from github_work import _github_origin_matches
    from local_codex import _child_environment

    identity = f"bot-{bot_id}-work-{work_id}-{uuid.uuid4().hex}"
    if not _ID.fullmatch(identity) or not _SHA.fullmatch(sha):
        raise ValueError("invalid workspace identity")
    reason = "workspace_unavailable"
    try:
        root = validate_root(str(root), (source,))
        source = source.resolve(strict=True)
        dest = root / identity
        hooks = root / (identity + ".disabled-hooks")
        options = ["-c", "core.hooksPath=" + str(hooks), "-c", "core.fsmonitor=false",
                   "-c", "core.symlinks=false", "-c", "submodule.recurse=false",
                   "-c", "fetch.recurseSubmodules=false", "-c", "gc.auto=0",
                   "-c", "maintenance.auto=false"]
        env = {**_child_environment(), "GIT_TERMINAL_PROMPT": "0"}

        def git(directory: Path, *args: str, allow_missing: bool = False) -> str:
            if not permitted():
                raise _Stopped()
            proc = subprocess.run(["git", "-C", str(directory), *options, *args],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="strict", timeout=90, env=env, check=False)
            if not permitted():
                raise _Stopped()
            if allow_missing and proc.returncode == 1 and not proc.stderr:
                return ""
            if proc.returncode != 0:
                raise RuntimeError("workspace Git command failed")
            return proc.stdout.strip()

        reason = "source_not_root"
        if Path(git(source, "rev-parse", "--show-toplevel")).resolve(strict=True) != source:
            return _record("unavailable", sha, identity, reason)
        reason = "origin_mismatch"
        if not _github_origin_matches(git(source, "remote", "get-url", "origin"), repo):
            return _record("unavailable", sha, identity, reason)

        # Disable configured checkout filters without altering Git config.
        # LFS files remain pointers. Refuse unusual driver names.
        reason = "unsafe_filter_config"
        keys = git(source, "config", "--name-only", "--get-regexp",
                   r"^filter\..*\.(smudge|clean|process|required)$", allow_missing=True)
        drivers = set()
        for key in keys.splitlines():
            match = _FILTER_KEY.fullmatch(key)
            if not match:
                return _record("unavailable", sha, identity, reason)
            drivers.add(match.group(1))
        if len(drivers) > 64:
            return _record("unavailable", sha, identity, reason)
        for driver in sorted(drivers):
            for key, value in (("smudge", ""), ("clean", ""), ("process", ""), ("required", "false")):
                options.extend(("-c", f"filter.{driver}.{key}={value}"))

        reason = "fetch_failed"
        git(source, "fetch", "--no-tags", "--no-recurse-submodules",
            "--no-write-fetch-head", "--refmap=", "origin", sha)
        reason = "identity_mismatch"
        if git(source, "rev-parse", "--verify", sha + "^{commit}") != sha:
            return _record("unavailable", sha, identity, reason)

        reason = "checkout_failed"
        if not permitted():
            raise _Stopped()
        if hooks.exists() or hooks.is_symlink() or root.resolve(strict=True) != root:
            raise ValueError("unsafe workspace directory")
        dest.mkdir(exist_ok=False)
        if dest.resolve(strict=True) != dest:
            raise ValueError("unsafe destination")
        git(source, "worktree", "add", "--detach", "--no-guess-remote", str(dest), sha)
        reason = "identity_mismatch"
        if (Path(git(dest, "rev-parse", "--show-toplevel")).resolve(strict=True) != dest
                or git(dest, "rev-parse", "--verify", "HEAD") != sha
                or git(dest, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD"
                or git(dest, "status", "--porcelain", "--untracked-files=all")):
            return _record("unavailable", sha, identity, reason)
        return _record("prepared", sha, identity) if permitted() else None
    except _Stopped:
        return None
    except (OSError, ValueError, RuntimeError, UnicodeError, subprocess.SubprocessError):
        return _record("unavailable", sha, identity, reason) if permitted() else None
