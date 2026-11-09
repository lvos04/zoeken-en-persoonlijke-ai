"""Testhulpmiddelen voor de B07-integratietests.

``simulate_ingest`` schrijft documentverwijzingen/chunks/embeddings rechtstreeks in de database, **als simulatie van de
toekomstige ingest (B07-002, nog niet gebouwd)**. Zo kan de ACL-vóór-ranking en de purge bij intrekking worden getoetst
zonder te doen alsof de ingest al bestaat. Productiecode schrijft deze rijen in P0 nooit.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy.orm import Session

from search_ai.search.models import Chunk, DocumentRef, Embedding, TranscriptRef


def simulate_ingest(
    session: Session,
    source_id: str | uuid.UUID,
    external_id: str,
    texts: list[str],
    *,
    title: str = "",
    visibility: str = "source",
    owner_sub: str | None = None,
    kid_safe: bool = False,
    classification: str = "sensitive",
    with_embedding: bool = True,
    timecodes: list[tuple[int, int]] | None = None,
) -> DocumentRef:
    sid = uuid.UUID(str(source_id))
    doc = DocumentRef(
        source_id=sid,
        external_id=external_id,
        title=title or external_id.rsplit("/", 1)[-1],
        uri=None,
        mime_type="text/plain",
        visibility=visibility,
        owner_sub=owner_sub,
        kid_safe=kid_safe,
        classification=classification,
        provenance={"adapter": "test-simulatie", "let_op": "geen echte ingest"},
    )
    session.add(doc)
    session.flush()
    for i, t in enumerate(texts):
        tc = timecodes[i] if timecodes else (None, None)
        ch = Chunk(
            document_id=doc.id,
            source_id=sid,
            ordinal=i,
            text=t,
            text_sha256=hashlib.sha256(t.encode()).hexdigest(),
            start_ms=tc[0],
            end_ms=tc[1],
        )
        session.add(ch)
        session.flush()
        if with_embedding:
            session.add(
                Embedding(
                    chunk_id=ch.id,
                    source_id=sid,
                    model_name="test-model",
                    model_version="0",
                    dimension=3,
                    vector=[0.1 * (i + 1), 0.2, 0.3],
                )
            )
    if timecodes:
        session.add(
            TranscriptRef(
                document_id=doc.id,
                source_id=sid,
                kind="subtitle",
                origin="existing",
                external_ref=external_id + ".srt",
                has_timecodes=True,
            )
        )
    session.commit()
    return doc


def counts(session: Session, source_id: str | uuid.UUID) -> dict[str, Any]:
    from search_ai.search.service import derived_counts

    return derived_counts(session, uuid.UUID(str(source_id)))
