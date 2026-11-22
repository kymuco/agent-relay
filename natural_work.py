"""Conservative natural-language work commands for an addressed, trusted human.

No LLM-generated intent here. Only explicit imperative, addressed user text and
an unambiguous allowlisted project can initiate write-capable background work.
Everything else remains ordinary conversation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

_START = re.compile(
    r"^(?:пожалуйста[\s,]+)?"
    r"(?:создай|сделай|реализуй|разработай|напиши|исправь|добавь|"
    r"обнови|доработай|поправь)\b",
    re.I,
)
_AMEND = re.compile(
    r"^(?:пожалуйста[\s,]+)?"
    r"(?:учти|не забудь|добавь ещё|добавь еще|добавь также|"
    r"и ещё|и еще|ещё|еще|уточнение:|измени требование|"
    r"проверь также|предусмотри также)\b",
    re.I,
)
# Do not treat questions, hypotheticals, pasted logs, or negated requests as execution.
_GUARD = re.compile(r"^(?:а если|а что если|можно ли|как|почему|"
                    r"не (?:надо|нужно|делай)|давай обсудим|"
                    r"расскажи|поясни)\b", re.I)


@dataclass(frozen=True)
class NaturalWorkIntent:
    kind: str
    project: str
    instruction: str


def _word_present(message: str, term: str) -> bool:
    return bool(re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])",
                          message, re.I))


def route_natural_work(text: str, *, bot_username: str,
                       aliases: tuple[str, ...], project_repos: Mapping[str, str],
                       active_project: str | None) -> NaturalWorkIntent | None:
    if not isinstance(text, str) or len(text) > 2500 or "\n" in text:
        return None
    text = text.strip()
    prefixes = (bot_username, "Рагна", "Ragna", *aliases)
    addressed = None
    for name in prefixes:
        name = name.strip().lstrip("@")
        if not 3 <= len(name) <= 40:
            continue
        m = re.match(r"^@?" + re.escape(name) + r"(?!\w)[\s,:!—–-]*",
                     text, re.I)
        if m and (addressed is None or m.end() > addressed.end()):
            addressed = m
    if addressed is None:
        return None
    body = text[addressed.end():].strip()
    if not body or body.startswith("/") or _GUARD.match(body):
        return None
    if active_project and _AMEND.match(body):
        # Explicitly naming a DIFFERENT project is never an amendment.
        other = [alias for alias, name in project_repos.items()
                 if alias != active_project and (
                     _word_present(body, alias) or _word_present(body, name.rsplit("/", 1)[-1]))]
        if other:
            return None
        return NaturalWorkIntent("amend", active_project, body)
    if not _START.match(body):
        return None
    matches = [alias for alias, name in project_repos.items()
               if _word_present(body, alias) or
               _word_present(body, name.rsplit("/", 1)[-1]) or
               _word_present(body, name)]
    if len(matches) != 1:
        return None  # Never guess which repository the user meant.
    return NaturalWorkIntent("start", matches[0], body)
