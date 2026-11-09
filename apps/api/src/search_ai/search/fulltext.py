"""Full-text zoeken (PostgreSQL ``tsvector``) met de gebruikers-ACL in de ``WHERE`` — dus vóór ranking.

P0 levert alleen de full-text-route die de ACL bewijst; de hybride zoekmachine (B07-003: fuzzy + pgvector, filters
op type/bron/datum/score) en de ingest die de index vult (B07-002) volgen in P5. Zolang de ingest er niet is, is de
index leeg en zegt het antwoord dat eerlijk (``ingest: capability_unavailable``) in plaats van iets te verzinnen.
Semantisch zoeken zonder geïnstalleerd lokaal embeddingmodel → ``501 capability_unavailable``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from psycopg.errors import QueryCanceled
from sqlalchemy import Float, and_, cast, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..errors import ApiError
from ..pagination import decode_int_cursor, encode_cursor
from ..security.auth import Principal
from ..timeutil import iso, utcnow
from . import acl
from .models import Chunk, DocumentRef, IndexSource, SearchQuery
from .retention import keep_days

TS_CONFIG = "simple"
HEADLINE_OPTS = "MaxFragments=1,MaxWords=30,MinWords=8,StartSel=«,StopSel=»,FragmentDelimiter= … "
MAX_QUERY_LEN = 300


def search(
    session: Session,
    principal: Principal,
    q: str,
    *,
    source_id: uuid.UUID | None,
    kind: str | None,
    limit: int,
    cursor: str | None,
    statement_timeout_ms: int = 5000,
) -> dict[str, Any]:
    query = q.strip()
    if not query:
        raise ApiError(422, "lege_zoekvraag", "Vul een zoekterm in.")
    if len(query) > MAX_QUERY_LEN:
        raise ApiError(422, "zoekvraag_te_lang", f"De zoekvraag is te lang (max. {MAX_QUERY_LEN} tekens).")
    offset = decode_int_cursor(cursor, maximum=10_000)

    tsq = func.websearch_to_tsquery(TS_CONFIG, query)
    score = cast(func.ts_rank_cd(Chunk.tsv, tsq), Float).label("score")
    stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.source_id,
            Chunk.ordinal,
            Chunk.start_ms,
            Chunk.end_ms,
            DocumentRef.title,
            DocumentRef.uri,
            DocumentRef.mime_type,
            DocumentRef.source_mtime,
            IndexSource.kind,
            IndexSource.name,
            score,
            func.ts_headline(TS_CONFIG, Chunk.text, tsq, HEADLINE_OPTS).label("snippet"),
        )
        # join op (id, source_id): de samengestelde sleutels (migratie 0002) houden chunk en document bij één bron
        .join(DocumentRef, and_(DocumentRef.id == Chunk.document_id, DocumentRef.source_id == Chunk.source_id))
        .join(IndexSource, IndexSource.id == DocumentRef.source_id)
        # ACL vóór ranking: alleen rijen die deze gebruiker mag zien komen bij ts_rank/ORDER BY/LIMIT.
        .where(acl.document_condition(principal))
        .where(Chunk.tsv.op("@@")(tsq))
    )
    if source_id is not None:
        stmt = stmt.where(Chunk.source_id == source_id)
    if kind is not None:
        stmt = stmt.where(IndexSource.kind == kind)
    # tijdlimiet (B07 §3) alleen voor deze transactie; een te zware zoekvraag geeft 503, nooit een half antwoord
    session.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(int(statement_timeout_ms))})
    try:
        rows = session.execute(stmt.order_by(score.desc(), Chunk.id).offset(offset).limit(limit + 1)).all()
        readable_count = int(
            session.scalar(select(func.count()).select_from(acl.readable_sources(principal).subquery())) or 0
        )
    except OperationalError as exc:
        session.rollback()
        if isinstance(exc.orig, QueryCanceled):
            raise ApiError(
                503,
                "zoeken_tijdslimiet",
                "De zoekopdracht duurde te lang en is afgebroken; maak de zoekvraag specifieker.",
                {"timeout_ms": int(statement_timeout_ms)},
            ) from exc
        raise
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for r in rows:
        item: dict[str, Any] = {
            "chunk_id": str(r.id),
            "document_id": str(r.document_id),
            "bron": {"id": str(r.source_id), "naam": r.name, "soort": r.kind},
            "titel": r.title,
            "uri": r.uri,
            "mime_type": r.mime_type,
            "bron_gewijzigd": iso(r.source_mtime),
            "fragment": r.snippet,
            "score": round(float(r.score), 6),
        }
        # Tijdcodes alleen als de bron ze heeft (B07-004): nooit verzinnen.
        if r.start_ms is not None:
            item["tijdcode"] = {"start_ms": r.start_ms, "eind_ms": r.end_ms}
        items.append(item)

    days = keep_days(session, "search_query")
    if days > 0:
        session.add(
            SearchQuery(
                principal_sub=principal.subject,
                roles=sorted(principal.roles),
                query_text=query,
                mode="fulltext",
                filters={"source_id": str(source_id) if source_id else None, "kind": kind},
                result_count=len(items),
                source_ids=sorted({i["bron"]["id"] for i in items}),
                expires_at=utcnow() + timedelta(days=days),
            )
        )
        session.commit()
    return {
        "mode": "fulltext",
        "items": items,
        "next_cursor": encode_cursor(offset + limit) if has_more else None,
        "doorzochte_bronnen": readable_count,
        "capabilities": {
            "fulltext": "beschikbaar",
            "semantic": "capability_unavailable",
            "ingest": "capability_unavailable",
        },
        "melding_nl": (
            None
            if readable_count
            else "Er zijn (nog) geen bronnen die jij mag doorzoeken. Een beheerder moet een bron selecteren, de "
            "eigenaar moet toestemming geven en jou toegang geven."
        ),
        "zoekgeschiedenis_bewaard": days > 0,
        "observed_at": iso(utcnow()),
    }
