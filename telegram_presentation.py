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


_FENCE_LINE = re.compile(r"^[ \t]*(\x60{3,}|~{3,})(.*)$")


def utf16_size(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _plain_chunks(text: str, limit: int) -> list[str]:
    chunks = []
    while text:
        units = 0
        end = boundary = 0
        for i, char in enumerate(text):
            size = 2 if ord(char) > 0xFFFF else 1
            if units + size > limit:
                break
            units += size
            end = i + 1
            if char.isspace():
                boundary = end
        if end == len(text):
            chunks.append(text)
            break
        cut = boundary if boundary >= end // 2 and boundary else end
        chunks.append(text[:cut])
        text = text[cut:]
    return chunks


def _markdown_blocks(text: str) -> list[tuple[str, bool]]:
    blocks = []
    lines = []
    fence = None
    for line in text.splitlines(keepends=True):
        match = _FENCE_LINE.match(line.rstrip("\r\n"))
        if fence is not None:
            lines.append(line)
            if (match and match[1][0] == fence[0]
                    and len(match[1]) >= len(fence) and not match[2].strip()):
                # Only the existing renderer's triple-backtick syntax is supported.
                blocks.append(("".join(lines), fence == chr(96) * 3 and match[1] == fence))
                lines, fence = [], None
            continue
        if match:
            if lines:
                blocks.append(("".join(lines), True))
            lines = [line]
            fence = match[1]
            continue
        lines.append(line)
        if not line.strip():
            blocks.append(("".join(lines), True))
            lines = []
    if lines:
        blocks.append(("".join(lines), fence is None))
    return blocks


def delivery_chunks(text: str, limit: int = 3800) -> list[tuple[str, bool]]:
    """Lossless source chunks; oversized Markdown blocks degrade to literal text.

    Reserve space for peer commands. Keep paragraphs and complete fences
    together when they fit; never reinterpret a split code block as prose.
    """
    if not 2 <= limit <= 3900:
        raise ValueError("invalid Telegram chunk limit")
    output = []
    current = ""
    for block, formatted in _markdown_blocks(text):
        if not formatted or utf16_size(block) > limit:
            if current:
                output.append((current, True))
                current = ""
            output.extend((part, False) for part in _plain_chunks(block, limit))
            continue
        if current and utf16_size(current + block) > limit:
            output.append((current, True))
            current = ""
        current += block
    if current:
        output.append((current, True))
    return output
