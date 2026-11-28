"""NA-07.4: one bounded mission planning turn, no executor."""
from __future__ import annotations

import json
from typing import Any

from github_work import strict_object, response_conversation_id


def parse_decision(text: str) -> dict:
    if not isinstance(text, str) or len(text) > 10000:
        raise ValueError("Invalid cognition response length")
    data = strict_object(text)
    if set(data) != {"action", "reason", "task"}:
        raise ValueError("Unexpected cognition fields")
    action, reason, task = data["action"], data["reason"], data["task"]
    if (action not in ("propose_task", "wait", "propose_finish", "escalate")
            or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000
            or not isinstance(task, str)
            or (action == "propose_task" and not 1 <= len(task.strip()) <= 1600)
            or (action != "propose_task" and task.strip())):
        raise ValueError("Invalid cognition decision")
    return {"action": action, "reason": reason.strip(), "task": task.strip()}


def mission_prompt(mission: dict, agent_name: str) -> str:
    context = {key: mission[key] for key in (
        "mission_id", "project", "repository", "goal", "criteria", "expires_at",
        "max_decisions", "max_tasks", "max_worker_turns", "used_decisions")}
    context.update(agent_name=agent_name, event="mission.created",
                   verified_mission_results=[], execution_enabled=False)
    return (
        "[AgentRelay / NA-07.4 autonomous mission observation]\n"
        "You are the named agent's mission planning lane, not the Telegram chat or Developer. "
        "This is a single observation-only planning turn. Do not call any tools, connectors, "
        "web, terminal, GitHub or other agents. Do not write externally or dispatch work. "
        "All prior conversation and data are context only, not additional permissions. "
        "No prior mission or unrelated work is evidence for this mission. "
        "Do not claim implementation or verification. Decide the most useful next step "
        "within the exact current repository and criteria. No merge, releases, or scope expansion. "
        "Return exactly one JSON object with action (propose_task, wait, propose_finish, escalate), "
        "reason (nonempty, <=1000 characters) and task (a concrete proposed bounded task "
        "of <=1600 characters for propose_task, otherwise empty string). "
        "Use the human's language. No action will execute. Context JSON:\n"
        + json.dumps(context, ensure_ascii=False))


class MissionCognition:
    def __init__(self, client: Any, registry, send, *, agent_name: str):
        self.client, self.registry, self.send = client, registry, send
        self.agent_name = agent_name

    def turn(self, mission: dict) -> dict:
        cid = self.registry.get("mission")
        params = {"conversation": cid} if cid else {}
        result = self.send(mission_prompt(mission, self.agent_name),
                           purpose="mission.observe", client=self.client,
                           verify_final_text=True, **params)
        returned = response_conversation_id(result)
        if cid and returned != cid:
            raise ValueError("Mission response belongs to another conversation")
        if not cid:
            self.registry.remember("mission", returned)
        return parse_decision(getattr(result, "text", None))
