"""Bewaartermijnen (tabel ``retention``). Standaard: zoekgeschiedenis en spraaktranscripten **niet** bewaren."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Retention

DEFAULT_KEEP_DAYS = {
    "search_query": 0,
    "voice_transcript": 0,
    "voice_session": 30,
    "index_run": 180,
    "audit_event": 365,
    "idempotency_record": 7,
}


def keep_days(session: Session, scope: str, source_id: uuid.UUID | None = None) -> int:
    """Bronspecifieke regel wint van de algemene; ontbreekt alles, dan de (privacyvriendelijke) standaard."""
    if source_id is not None:
        row = session.scalars(
            select(Retention).where(Retention.scope == scope, Retention.source_id == source_id)
        ).first()
        if row is not None:
            return int(row.keep_days)
    row = session.scalars(select(Retention).where(Retention.scope == scope, Retention.source_id.is_(None))).first()
    if row is not None:
        return int(row.keep_days)
    return DEFAULT_KEEP_DAYS.get(scope, 0)
