"""Small, opt-in Telegram Rich Message presentation adapter.

The model writes ordinary Markdown; Telegram parses it server-side. Never
retry a possibly-delivered sendRichMessage after a network/HTTP ambiguity.
"""
from __future__ import annotations

import re
from typing import Any

_MARKDOWN_STRUCTURE = re.compile(
    r"(?m)^(?:#{1,6}\s+\S|[-*+]\s+\S|\d+[.)]\s+\S|"
    r"[\t ]*\x60{3}[A-Za-z0-9_+-]*\s*$|>\s+\S|\|.+\|)",
)


def use_rich_message(text: str, *, mode: str, from_bot: bool = False) -> bool:
    if mode != "auto" or from_bot or not isinstance(text, str):
        return False
    if not (1 <= len(text.encode("utf-8")) <= 24000):
        return False
    if text.lstrip().lower().startswith("/relay@"):
        return False
    return bool(_MARKDOWN_STRUCTURE.search(text))


def rich_payload(text: str, *, chat_id: int, reply_message_id: int | None = None,
                 thread_id: int | None = None) -> dict[str, Any]:
    if not text or len(text.encode("utf-8")) > 24000:
        raise ValueError("Rich content exceeds safe size limit")
    payload: dict[str, Any] = {
        "chat_id": chat_id, "rich_message": {"markdown": text},
    }
    if reply_message_id is not None:
        payload["reply_parameters"] = {
            "message_id": reply_message_id,
            "allow_sending_without_reply": True,
        }
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    return payload
