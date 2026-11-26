"""NA-02: dry-run policy decisions, never execution grants.

The model proposes intent. Host facts determine scope. An allow decision is
only a simulation of configured policy, not proof of the user's intent or
permission to dispatch. No executor, confirmation endpoint, or replay API.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from action_bridge import Observation
from capability_registry import BY_ACTION, READ_ACTIONS, WRITE_ACTIONS

POLICY_VERSION = 1


@dataclass(frozen=True)
class Source:
    bot_id: int
    chat_id: int
    update_id: int
    message_id: int
    user_id: int
    generation: int
    request_sha256: str


@dataclass(frozen=True)
class Decision:
    decision: str
    reason: str
    proposal_id: str
    project: str | None = None
    repository: str | None = None
    work_id: int | None = None
    executor: str | None = None
    delegation: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "policy_version": POLICY_VERSION,
            "decision": self.decision,
            "reason": self.reason,
            "proposal_id": self.proposal_id,
            "project": self.project,
            "repository": self.repository,
            "work_id": self.work_id,
            "executor": self.executor,
            "delegation": self.delegation,
            "execution_enabled": False,
        }


def request_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evaluate(
    observation: Observation,
    *,
    source: Source,
    authorized: bool,
    generation_current: bool,
    projects: Mapping[str, str],
    auto_projects: frozenset[str],
    executor: str,
    work: Mapping[str, Any] | None,
    bound_card: Mapping[str, Any] | None,
) -> Decision:
    """Evaluate only a parsed proposal against current Host-owned facts.

    A read can target the exact current work or the exact replied-to card.
    Amendments require the same active work and an exact Reply binding.
    Auto policy is explicitly project-scoped; unknown actions are denied.
    Task strings are not a machine-enforced sandbox or an intent proof.
    """
    action = observation.action
    args = observation.arguments or {}
    project = None
    work_id = None
    repository = None
    delegation = None

    def result(decision: str, reason: str) -> Decision:
        identity = {
            "version": POLICY_VERSION,
            "source": vars(source),
            "action": action,
            "arguments": args,
            "project": project,
            "repository": repository,
            "work_id": work_id,
            "executor": executor,
            "delegation": delegation,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return Decision(decision, reason, digest, project, repository, work_id, executor, delegation)

    if not authorized:
        return result("deny", "unauthorized_source")
    if not generation_current:
        return result("deny", "stale_generation")
    capability = BY_ACTION.get(action) if isinstance(action, str) else None
    if observation.status != "proposed" or capability is None:
        return result("deny", "invalid_or_unknown_action")
    if not capability.supported(executor, runtime_ready=work is not None):
        return result("deny", "executor_unavailable")
    if not capability.valid_arguments(args):
        return result("deny", "invalid_arguments")
    current_id = work.get("id")
    active = work.get("status") in {"queued", "running"}
    if action == "start_work":
        delegation = args.get("delegation", "coordinated")
        project = args["project"]
        if bound_card is not None:
            return result("deny", "start_from_task_reply")
    else:
        work_id = args["work_id"]
        if bound_card is not None:
            if bound_card.get("work_id") != work_id:
                return result("deny", "task_reply_mismatch")
            project = bound_card.get("project")
        elif current_id == work_id:
            project = work.get("project")
        else:
            return result("deny", "unknown_task")
    if not isinstance(project, str) or project not in projects:
        return result("deny", "project_not_allowed")
    repository = projects[project]
    if action in READ_ACTIONS:
        if action == "get_work_result":
            phase = bound_card.get("phase") if bound_card is not None else work.get("status")
            if phase in {"queued", "running", "coordinator", "developer",
                         "revision", "verify", "local_test", "ci", "review", "idle"}:
                return result("deny", "result_not_ready")
        return result("allow", "bounded_read")
    if action == "start_work" and active:
        return result("deny", "executor_busy")
    if action == "amend_work":
        if not active or current_id != work_id or work.get("project") != project:
            return result("deny", "task_not_active")
        if bound_card is None:
            return result("ask", "exact_task_reply_required")
    if project not in auto_projects:
        return result("ask", "project_not_auto_enabled")
    return result("allow", "configured_isolated_branch_policy")
