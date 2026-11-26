"""NA-06.4: operator-selected test presets for freshly verified Git worktrees.

This is NOT a sandbox. Opt in only for trusted repositories: Python tests
execute arbitrary repository code with the Host user's OS permissions.
No command strings from a model, dependency installation, or automatic repairs.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from local_workspace import verify_prepared_workspace, workspace_record

_PRESETS = frozenset({"unittest", "pytest"})
_FALLBACK = frozenset({"not_configured", "not_run", "unavailable"})
_STATUSES = frozenset({"passed", "failed", "no_tests", "timeout", "output_limit", "unavailable"})
_OUTPUT_LIMIT = 1024 * 1024
# Trusted launcher: ensure zero-test unittest discovery cannot return success.
_UNITTEST = """import os, sys, unittest
sys.path.insert(0, os.getcwd())
suite = unittest.defaultTestLoader.discover('.')
result = unittest.TextTestRunner(verbosity=1).run(suite)
print('AGENTRELAY_UNITTEST_RESULT:%d:%d' % (result.testsRun, int(result.wasSuccessful())), flush=True)
sys.exit(5 if result.testsRun == 0 else 0 if result.wasSuccessful() else 1)
"""
_MARKER = re.compile(rb"(?m)^AGENTRELAY_UNITTEST_RESULT:([0-9]{1,8}):([01])\r?$")


def parse_local_tests(raw: str, projects: Mapping[str, Path]) -> dict[str, str]:
    if not raw.strip():
        return {}
    result = {}
    for entry in raw.split(";"):
        alias, sep, preset = entry.partition("=")
        alias, preset = alias.strip(), preset.strip()
        if not sep or alias not in projects or alias in result or preset not in _PRESETS:
            raise ValueError("WORK_LOCAL_TESTS requires unique configured alias=unittest or alias=pytest")
        result[alias] = preset
    return result


def should_run_local(ci: dict | None) -> bool:
    return ci is None or ci.get("status") in _FALLBACK


def python_executable() -> Path:
    raw = os.getenv("WORK_PYTHON", "").strip()
    candidate = Path(raw).expanduser() if raw else Path(sys.executable)
    # Do not resolve the interpreter symlink: that can bypass a Linux venv.
    if not candidate.is_absolute() or not candidate.is_file():
        raise ValueError("WORK_PYTHON must be an existing absolute Python executable")
    if os.name == "nt" and candidate.suffix.lower() != ".exe":
        raise ValueError("WORK_PYTHON must be an executable, not a shell script")
    return candidate


def _record(status: str, workspace: dict, preset: str, code: int | None,
            count: int | None, elapsed: float) -> dict:
    return {"status": status, "commit_sha": workspace["commit_sha"],
            "workspace_id": workspace["workspace_id"], "preset": preset,
            "exit_code": code, "tests_run": count, "elapsed_ms": min(36000000, max(0, int(elapsed*1000))),
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def execution_record(data: Any, workspace: Any, sha: str) -> dict | None:
    prepared = workspace_record(workspace, sha)
    if (not prepared or prepared["status"] != "prepared" or not isinstance(data, dict)
            or data.get("commit_sha") != sha or data.get("workspace_id") != prepared["workspace_id"]
            or not isinstance(data.get("preset"), str) or data["preset"] not in _PRESETS
            or not isinstance(data.get("status"), str) or data["status"] not in _STATUSES):
        return None
    code, count, ms, stamp = (data.get(k) for k in ("exit_code", "tests_run", "elapsed_ms", "observed_at"))
    if ((code is not None and (type(code) is not int or abs(code) > 2**32))
            or (count is not None and (type(count) is not int or not 0 <= count <= 99999999))
            or type(ms) is not int or not 0 <= ms <= 36000000
            or not isinstance(stamp, str) or len(stamp) > 50):
        return None
    if data["status"] == "passed" and (code != 0 or
            (data["preset"] == "unittest" and (count is None or count <= 0))):
        return None
    if data["status"] == "no_tests" and (code != 5 or count != 0):
        return None
    if data["status"] == "failed" and (code is None or code == 0):
        return None
    try:
        observed = datetime.fromisoformat(stamp)
        if observed.tzinfo is None:
            return None
    except ValueError:
        return None
    return {**{k: data[k] for k in ("status", "commit_sha", "workspace_id", "preset")},
            "exit_code": code, "tests_run": count, "elapsed_ms": ms,
            "observed_at": observed.isoformat(timespec="seconds")}


def execution_label(data: Any, workspace: Any, sha: str) -> str:
    value = execution_record(data, workspace, sha)
    if value is None:
        return ""
    labels = {"passed": "успешно", "failed": "ошибка", "no_tests": "тесты не найдены",
              "timeout": "таймаут", "output_limit": "превышен лимит вывода",
              "unavailable": "исполнение не подтверждено"}
    count = f" · тестов: {value['tests_run']}" if value["tests_run"] is not None else ""
    code = f" · exit={value['exit_code']}" if value["exit_code"] is not None else ""
    return "Local Runner (" + value["preset"] + "): " + labels[value["status"]] + count + code


def run_local_tests(source: Path, root: Path, repo: str, sha: str, workspace: dict,
                    preset: str, *, timeout: int, permitted: Callable[[], bool]) -> dict | None:
    from local_codex import _child_environment, _kill_tree
    prepared = workspace_record(workspace, sha)
    if not prepared or prepared["status"] != "prepared" or preset not in _PRESETS:
        raise ValueError("invalid local execution scope")
    if not permitted():
        return None
    started = time.monotonic()

    def result(status, code=None, count=None):
        return _record(status, prepared, preset, code, count, time.monotonic()-started)

    directory = verify_prepared_workspace(source, root, repo, sha,
                                          prepared["workspace_id"], permitted=permitted)
    if not permitted():
        return None
    if directory is None:
        return result("unavailable")
    try:
        python = python_executable()
        args = [str(python), "-I", "-B"]
        args += ["-c", _UNITTEST] if preset == "unittest" else ["-m", "pytest", "-q", "-p", "no:cacheprovider"]
        env = _child_environment()
        env.pop("CODEX_HOME", None)
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        with tempfile.TemporaryFile(mode="w+b") as output:
            if not permitted():
                return None
            proc = subprocess.Popen(args, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.STDOUT, shell=False,
                                    creationflags=flags, start_new_session=os.name != "nt")
            deadline = time.monotonic() + timeout
            try:
                while proc.poll() is None:
                    if not permitted():
                        return None
                    if time.monotonic() >= deadline:
                        return result("timeout")
                    if os.fstat(output.fileno()).st_size > _OUTPUT_LIMIT:
                        return result("output_limit")
                    time.sleep(.05)
                if not permitted():
                    return None
                if os.fstat(output.fileno()).st_size > _OUTPUT_LIMIT:
                    return result("output_limit")
                output.seek(0)
                raw = output.read(_OUTPUT_LIMIT + 1)
                if len(raw) > _OUTPUT_LIMIT:
                    return result("output_limit")
                code = proc.returncode
                count = None
                if preset == "unittest":
                    markers = _MARKER.findall(raw)
                    if len(markers) != 1:
                        return result("failed", code) if code else result("unavailable", code)
                    count = int(markers[0][0])
                    success = markers[0][1] == b"1"
                    if code == 0 and (not success or count == 0):
                        return result("unavailable", code, count)
                if code == 5 and (preset == "pytest" or count == 0):
                    return result("no_tests", code, 0)
                return result("passed" if code == 0 else "failed", code, count)
            finally:
                if proc.poll() is None:
                    _kill_tree(proc)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=8)
    except (OSError, ValueError, subprocess.SubprocessError):
        return result("unavailable") if permitted() else None
