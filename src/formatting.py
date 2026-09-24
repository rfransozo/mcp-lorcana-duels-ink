"""Shared response-shaping helpers: pagination envelopes and markdown rendering.

Every tool that returns data goes through here so the JSON envelope and the
markdown style stay identical across the whole server (DRY).
"""

import json
from typing import Any, Callable, Iterable, Optional

from .models import ResponseFormat


def paginate(
    items: list[dict],
    total: Optional[int],
    offset: int,
    key: str = "items",
) -> dict:
    """Build the standard pagination envelope.

    Args:
        items: The page of results already sliced to the requested limit.
        total: Total matches server-side. None when the API does not report it.
        offset: The offset these items start at.
        key: Name of the list field in the envelope (e.g. "cards", "decks").

    Returns:
        dict with total, count, offset, <key>, has_more and next_offset.
    """
    count = len(items)
    known_total = total if total is not None else offset + count
    has_more = known_total > offset + count
    return {
        "total": known_total,
        "count": count,
        "offset": offset,
        key: items,
        "has_more": has_more,
        "next_offset": offset + count if has_more else None,
    }


def as_json(payload: Any) -> str:
    """Serialise a payload as indented JSON, for JSON shown inside markdown."""
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def as_compact_json(payload: Any) -> str:
    """Serialise a payload with no whitespace - what response_format='json' returns.

    Indentation is for people reading markdown. A four-player Coconut state
    came back as 104,584 characters, over what an MCP client shows inline with
    the clock at 0:29, and close to a third of it was spaces and newlines.
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)


def render(
    payload: dict,
    response_format: ResponseFormat,
    markdown: Callable[[dict], str],
    json_view: Optional[Callable[[dict], Any]] = None,
) -> str:
    """Return either the markdown rendering or the JSON envelope.

    `json_view` reshapes the payload for JSON only - the markdown keeps
    reading the full one.
    """
    if response_format == ResponseFormat.JSON:
        return as_compact_json(json_view(payload) if json_view else payload)
    return markdown(payload)


def pagination_footer(payload: dict) -> str:
    """One-line 'showing X of Y' footer with the next_offset hint."""
    total = payload.get("total", 0)
    count = payload.get("count", 0)
    offset = payload.get("offset", 0)
    line = f"_Showing {count} of {total} (offset {offset})._"
    if payload.get("has_more"):
        line += f" Pass `offset={payload['next_offset']}` for the next page."
    return line


def bullet(label: str, value: Any) -> str:
    """Render a markdown bullet, skipping empty values."""
    if value is None or value == "" or value == []:
        return ""
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return f"- **{label}**: {value}"


def join_lines(parts: Iterable[str]) -> str:
    """Join rendered fragments, dropping the empty ones."""
    return "\n".join(p for p in parts if p)


def empty_result(what: str, hint: str = "") -> str:
    """Consistent 'nothing found' message that tells the agent what to try next."""
    msg = f"No {what} found."
    if hint:
        msg += f" {hint}"
    return msg
