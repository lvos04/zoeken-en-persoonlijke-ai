"""Cursor-paginering (contract §5: ``cursor``/``limit``, max 200)."""

from __future__ import annotations

import base64
import json
from typing import Any

from .errors import ApiError

MAX_LIMIT = 200


def encode_cursor(value: Any) -> str:
    raw = json.dumps({"k": value}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str | None) -> Any:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(cursor + pad))
        return data["k"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(400, "invalid_cursor", "De cursor is ongeldig; begin opnieuw zonder cursor.") from exc


def offset_page(items: list[Any], cursor: str | None, limit: int) -> dict[str, Any]:
    start = decode_cursor(cursor) or 0
    if not isinstance(start, int) or start < 0:
        raise ApiError(400, "invalid_cursor", "De cursor is ongeldig; begin opnieuw zonder cursor.")
    chunk = items[start : start + limit]
    nxt = encode_cursor(start + limit) if start + limit < len(items) else None
    return {"items": chunk, "next_cursor": nxt}
