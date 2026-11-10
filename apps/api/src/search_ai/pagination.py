"""Cursor-paginering (contract §5: ``cursor``/``limit``, max 200)."""

from __future__ import annotations

import base64
import json
from typing import Any

from .errors import ApiError

MAX_LIMIT = 200


def invalid_cursor() -> ApiError:
    """Foutcatalogus (contract 1.3 §5): een ongeldige queryparameter is een schemafout → 422 ``validation_error``."""
    return ApiError(
        422,
        "validation_error",
        "De cursor is ongeldig; begin opnieuw zonder cursor.",
        {"fouten": [{"veld": "cursor", "probleem": "ongeldige cursor", "type": "invalid_cursor"}]},
    )


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
        raise invalid_cursor() from exc


def decode_int_cursor(cursor: str | None, maximum: int | None = None) -> int:
    """Cursor met een geheel getal ≥ 0 (offset of volgnummer); ontbreekt hij → 0.

    ``True``/``False`` zijn in Python ook ``int``; die (en elk ander type) geven 400 i.p.v. een stille offset of een
    databasefout (QA P0-afronding).
    """
    value = decode_cursor(cursor)
    if value is None:
        return 0
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise invalid_cursor()
    return value


def offset_page(items: list[Any], cursor: str | None, limit: int) -> dict[str, Any]:
    start = decode_int_cursor(cursor)
    chunk = items[start : start + limit]
    nxt = encode_cursor(start + limit) if start + limit < len(items) else None
    return {"items": chunk, "next_cursor": nxt}
