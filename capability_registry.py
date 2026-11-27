"""NA-06.1: Host-owned, immutable native capability descriptions.

A descriptor is not an execution grant. The catalog reflects configured runtime
availability, never proves connector permissions or reserves an executor.
Admission and dispatch remain separate and recheck current Host facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

DELEGATIONS = ("direct", "coordinated")
CATALOG_VERSION = 1


@dataclass(frozen=True)
class Argument:
    name: str
    kind: str
    required: bool = True
    maximum: int = 0
    choices: tuple[str, ...] = ()
    default: str | None = None

    def accepts(self, value: Any) -> bool:
        if self.kind == "integer":
            return type(value) is int and 1 <= value <= self.maximum
        if not isinstance(value, str) or not value.strip():
            return False
        if self.maximum and len(value) > self.maximum:
            return False
        return not self.choices or value in self.choices

    def schema(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": "integer" if self.kind == "integer" else "string"}
        if self.kind == "integer":
            result.update(minimum=1, maximum=self.maximum)
        elif self.maximum:
            result.update(minLength=1, maxLength=self.maximum)
        if self.kind == "project":
            result["description"] = "Exact configured project alias"
        if self.choices:
            result["enum"] = list(self.choices)
        if self.default is not None:
            result["default"] = self.default
        return result


@dataclass(frozen=True)
class Capability:
    id: str
    action: str
    description: str
    effect: str
    executor: str
    scope: str
    arguments: tuple[Argument, ...]
    constraints: tuple[str, ...]

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {arg.name: arg.schema() for arg in self.arguments},
            "required": [arg.name for arg in self.arguments if arg.required],
            "additionalProperties": False,
        }

    def valid_arguments(self, args: Any) -> bool:
        if type(args) is not dict:
            return False
        fields = {arg.name: arg for arg in self.arguments}
        if set(args) - fields.keys():
            return False
        return all((not arg.required if arg.name not in args else arg.accepts(args[arg.name]))
                   for arg in self.arguments)

    def supported(self, executor: str, *, runtime_ready: bool) -> bool:
        return runtime_ready and executor == self.executor


_WORK_ID = Argument("work_id", "integer", maximum=999999999)
_CAPABILITIES = (
    Capability("github.develop", "start_work",
               "Develop source code in one configured GitHub repository.",
               "write", "github", "project",
               (Argument("project", "project"), Argument("task", "string", maximum=2500),
                Argument("delegation", "string", required=False, choices=DELEGATIONS,
                         default="coordinated")),
               ("Explicit human authorization", "Isolated branch only; no merge",
                "One active task", "Automatic execution only for allowlisted projects")),
    Capability("work.status", "get_work_status", "Read exact retained task status.",
               "read", "github", "task", (_WORK_ID,),
               ("Exact known work_id", "Configured repository scope")),
    Capability("work.result", "get_work_result", "Read exact retained terminal task result.",
               "read", "github", "task", (_WORK_ID,),
               ("Exact known work_id", "Configured repository scope", "Terminal task only")),
    Capability("work.amend", "amend_work", "Append a human-authorized requirement to active work.",
               "write", "github", "task",
               (_WORK_ID, Argument("task", "string", maximum=1600)),
               ("Exact active task card reply", "Same project and task",
                "Automatic execution only for allowlisted projects")),
)
BY_ACTION: Mapping[str, Capability] = MappingProxyType({c.action: c for c in _CAPABILITIES})
BY_ID: Mapping[str, Capability] = MappingProxyType({c.id: c for c in _CAPABILITIES})
READ_ACTIONS = frozenset(c.action for c in _CAPABILITIES if c.effect == "read")
WRITE_ACTIONS = frozenset(c.action for c in _CAPABILITIES if c.effect == "write")


def catalog_snapshot(*, projects: Mapping[str, str], auto_projects: frozenset[str],
                     executor: str, mode: str, runtime_ready: bool,
                     work_active: bool, worker_busy: bool = False,
                     verification_by_project: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Copy safe configuration facts for the current eligible human turn."""
    if mode not in ("off", "observe", "execute"):
        raise ValueError("invalid native action mode")
    items = []
    if mode != "off":
        for cap in _CAPABILITIES:
            configured = cap.supported(executor, runtime_ready=runtime_ready) and bool(projects)
            state = "configured" if configured else "unavailable"
            if configured and cap.action == "start_work" and (work_active or worker_busy):
                state = "busy"
            if configured and cap.action == "amend_work" and not work_active:
                state = "no_active_task"
            items.append({
                "id": cap.id, "action": cap.action, "description": cap.description,
                "effect": cap.effect, "executor": cap.executor, "scope": cap.scope,
                "input_schema": cap.input_schema(), "constraints": list(cap.constraints),
                "availability": state,
            })
    return {
        "catalog_version": CATALOG_VERSION,
        "mode": mode,
        "execution_enabled": mode == "execute" and runtime_ready and executor == "github" and bool(projects),
        "projects": {alias: {"repository": repo, "write_policy":
                             "automatic" if alias in auto_projects else "not_auto_enabled",
                             **({"verification": verification_by_project[alias]}
                                if verification_by_project and alias in verification_by_project else {})}
                     for alias, repo in sorted(projects.items())} if mode != "off" else {},
        "capabilities": items,
    }
