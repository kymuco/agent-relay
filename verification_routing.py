"""NA-06.5.1: Developer chooses verification; Host admits configured effects.

A plan is untrusted worker data, never authority. No local fallback based on
CI status alone. Historical reports without a plan can only observe cloud CI.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

_ROUTES = ("cloud", "local", "hybrid", "none", "unavailable")
_LOCAL = ("local", "hybrid")
_PRESETS = ("unittest", "pytest")
_SHA = re.compile(r"[0-9a-f]{40}")
_REASONS = ("cloud_disabled", "local_not_authorized", "preset_not_authorized",
            "verification_unavailable", "missing_plan")


def capabilities(*, cloud_enabled: bool, local_preset: str | None,
                 workspace_enabled: bool, diagnostics_enabled: bool) -> dict:
    """Configuration facts for exactly one project. No paths, credentials or commands."""
    preset = local_preset if workspace_enabled and local_preset in _PRESETS else None
    return {"cloud_ci": bool(cloud_enabled), "local_presets": [preset] if preset else [],
            "codex_diagnostics": bool(preset and diagnostics_enabled)}


def parse_plan(value: Any) -> dict:
    if type(value) is not dict or not isinstance(value.get("route"), str):
        raise ValueError("invalid verification plan")
    route = value["route"]
    if route not in _ROUTES:
        raise ValueError("unknown verification route")
    expected = {"route", "reason", "preset"} if route in _LOCAL else {"route", "reason"}
    if set(value) != expected:
        raise ValueError("unexpected verification fields")
    reason = value.get("reason")
    if (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 600
            or len(reason) > 600 or any(ord(c) < 32 or ord(c) == 127 for c in reason)):
        raise ValueError("invalid verification reason")
    result = {"route": route, "reason": reason.strip()}
    if route in _LOCAL:
        preset = value.get("preset")
        # Unknown identifiers are denied by Host, never interpreted as commands.
        if not isinstance(preset, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", preset):
            raise ValueError("invalid verification preset")
        result["preset"] = preset
    return result


def admit_plan(plan: dict | None, sha: str, available: Mapping[str, Any]) -> dict:
    if not _SHA.fullmatch(sha):
        raise ValueError("invalid verification commit identity")
    if plan is None:
        return {"commit_sha": sha, "route": "unspecified", "reason": "",
                "admission": "legacy", "admission_reason": "missing_plan"}
    plan = parse_plan(plan)
    route = plan["route"]
    denial = ""
    if route == "unavailable":
        denial = "verification_unavailable"
    elif route in ("cloud", "hybrid") and available.get("cloud_ci") is not True:
        denial = "cloud_disabled"
    elif route in _LOCAL:
        presets = available.get("local_presets", [])
        if not presets:
            denial = "local_not_authorized"
        elif plan["preset"] not in presets or plan["preset"] not in _PRESETS:
            denial = "preset_not_authorized"
    return {"commit_sha": sha, **plan, "admission": "denied" if denial else "admitted",
            "admission_reason": denial}


def verification_record(value: Any, sha: str) -> dict | None:
    if (not isinstance(value, dict) or not isinstance(sha, str) or not _SHA.fullmatch(sha)
            or value.get("commit_sha") != sha):
        return None
    route, admission, reason = (value.get(k) for k in ("route", "admission", "admission_reason"))
    if route == "unspecified":
        if admission == "legacy" and reason == "missing_plan":
            return admit_plan(None, sha, {})
        return None
    if admission not in ("admitted", "denied"):
        return None
    if ((admission == "admitted" and (reason != "" or route == "unavailable"))
            or (admission == "denied" and reason not in _REASONS[:-1])):
        return None
    if (route == "unavailable") != (reason == "verification_unavailable"):
        return None
    try:
        data = {k: value[k] for k in ("route", "reason")}
        if route in _LOCAL:
            data["preset"] = value["preset"]
        plan = parse_plan(data)
    except (KeyError, ValueError, TypeError):
        return None
    if admission == "admitted" and route in _LOCAL and plan["preset"] not in _PRESETS:
        return None
    return {"commit_sha": sha, **plan, "admission": admission, "admission_reason": reason}


def wants_cloud(record: dict | None, *, enabled: bool) -> bool:
    return bool(enabled and record and record["route"] in ("cloud", "hybrid", "unspecified")
                and record["admission"] in ("admitted", "legacy"))


def wants_local(record: dict | None, ci: dict | None) -> bool:
    if not record or record["admission"] != "admitted":
        return False
    # Hybrid needs BOTH. Defer additional compute until cloud succeeds.
    return record["route"] == "local" or (
        record["route"] == "hybrid" and ci is not None and ci.get("status") == "passed")


def review_reason(record: dict | None, ci: dict | None, execution: dict | None) -> str:
    """No success inferred from absent/failed required checks."""
    if record is None:
        return ""
    if record["admission"] == "denied":
        return record["admission_reason"]
    route = record["route"]
    if route == "unspecified":
        # Compatibility for old reports: commit-only when CI is not enabled.
        # A missing plan never authorizes local work.
        return "cloud_not_passed" if ci is not None and ci.get("status") != "passed" else ""
    if route in ("cloud", "hybrid") and (ci is None or ci.get("status") != "passed"):
        return "cloud_not_passed"
    if route in _LOCAL and (execution is None or execution.get("status") != "passed"):
        return "local_not_passed"
    return ""


def verification_label(value: Any, sha: str, *, detail: bool = False) -> str:
    record = verification_record(value, sha)
    if not record:
        return ""
    if record["route"] == "unspecified":
        return "План проверки отсутствует; локальный запуск запрещён (коммит не равен проверке кода)."
    label = "План Developer: " + record["route"]
    if "preset" in record:
        label += " / " + record["preset"]
    if record["admission"] == "denied":
        label += " · Host отклонил (" + record["admission_reason"] + ")"
    elif record["route"] == "none":
        label += " · исполнение не запрошено"
    else:
        label += " · Host разрешил"
    if detail:
        label += " · Причина Developer: " + record["reason"]
    return label
