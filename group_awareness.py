"""Bounded group context and one-hop peer handoff for AgentRelay.

Nothing in this module calls ChatGPT. Observed chat text is untrusted context,
not a command or instruction. The buffer is process-local and not persisted.
"""
from __future__ import annotations

import re
from collections import deque
from typing import Any

_HANDOFF = re.compile(r'\[\[handoff:([A-Za-z][A-Za-z0-9_]{4,31})\]\]', re.IGNORECASE)
_MAX_NAME = 64
_MAX_ENTRY = 600


def trusted_sender(message: dict[str, Any], allowed_users: frozenset[int],
                   peers: frozenset[str], peer_ids: frozenset[int], own_id: int) -> bool:
    """Require a numeric identity, and both numeric ID and username for bots."""
    if message.get('sender_chat'):
        return False
    sender = message.get('from') or {}
    identity = sender.get('id')
    if type(identity) is not int or identity <= 0 or identity == own_id:
        return False
    if sender.get('is_bot'):
        return identity in peer_ids and str(sender.get('username') or '').lower() in peers
    return identity in allowed_users


def addressed_by_alias(text: str, aliases: tuple[str, ...]) -> bool:
    """Match a distinct name, not a substring of a word or @username."""
    for alias in aliases:
        alias = alias.strip()
        if len(alias) < 3 or len(alias) > 40:
            continue
        if re.search(r'(?<![\w@])' + re.escape(alias) + r'(?!\w)', text, re.IGNORECASE):
            return True
    return False


class GroupAwareness:
    def __init__(self, limit: int = 30) -> None:
        if not 0 <= limit <= 100:
            raise ValueError('GROUP_CONTEXT_LIMIT must be between 0 and 100')
        self.entries: deque[tuple[int | None, str]] = deque(maxlen=max(limit, 1))
        self.limit = limit

    def remember(self, message: dict[str, Any]) -> None:
        if not self.limit:
            return
        text = message.get('text') or message.get('caption')
        if not isinstance(text, str) or not text.strip():
            return
        sender = message.get('from') or {}
        name = str(sender.get('first_name') or sender.get('username') or sender.get('id'))[:_MAX_NAME]
        kind = 'bot' if sender.get('is_bot') else 'human'
        message_id = message.get('message_id')
        identity = message_id if type(message_id) is int else None
        self.entries.append((identity, f'{kind} {name}: {self._short(text)}'))

    def remember_own_reply(self, username: str, text: str) -> None:
        if self.limit and text.strip():
            self.entries.append((None, f'bot {username[:_MAX_NAME]}: {self._short(text)}'))

    def recent_text(self, *, exclude_message_id: int | None = None) -> str:
        if not self.limit:
            return ''
        items = [line for message_id, line in self.entries
                 if message_id is None or message_id != exclude_message_id]
        return '\n'.join(items[-self.limit:])

    @staticmethod
    def _short(text: str) -> str:
        return ' '.join(text.split())[:_MAX_ENTRY]


def explicit_handoff(text: str, peers: frozenset[str], *, from_bot: bool) -> str:
    """Use Telegram's command-mention delivery for bounded peer handoffs.

    Handoff markers or a leading @Peer from human-triggered responses route
    through /relay@Peer. Replies to bots cannot request another hop.
    """
    chosen = None
    for match in _HANDOFF.finditer(text):
        if not from_bot and chosen is None and match.group(1).lower() in peers:
            chosen = match.group(1)
    clean = _HANDOFF.sub('', text).strip()
    if from_bot:
        return clean
    leading_peer = re.match(r'^@([A-Za-z][A-Za-z0-9_]{4,31})\b(?:\s+|$)', clean, re.I)
    if leading_peer and leading_peer.group(1).lower() in peers:
        chosen = chosen or leading_peer.group(1)
        # Avoid repeating the username after /relay@Peer.
        if chosen.lower() == leading_peer.group(1).lower():
            clean = clean[leading_peer.end():].strip()
    if chosen:
        return f'/relay@{chosen} {clean}'.strip()
    return clean
