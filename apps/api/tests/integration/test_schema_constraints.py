"""Privacy- en integriteitsregels die de database zelf afdwingt (ook als app-code later fouten maakt)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from search_ai.search.models import Chunk, ConnectorPermission, DocumentRef, Embedding, IndexSource, PrivacyConsent
from tests.integration.helpers import simulate_ingest

pytestmark = pytest.mark.integration


@pytest.fixture
def session(clean_db: str) -> Any:
    eng = create_engine(clean_db)
    with Session(eng) as s:
        yield s
    eng.dispose()


def _source(s: Session, **kw: Any) -> IndexSource:
    src = IndexSource(
        kind="files", name="t", owner_sub="u", created_by="t", updated_by="t", allowed_paths=["/bronnen/x"], **kw
    )
    s.add(src)
    s.flush()
    return src


def test_vector_roundtrip_and_dimension_check(session: Session) -> None:
    src = _source(session)
    doc = simulate_ingest(session, src.id, "/bronnen/x/a.txt", ["tekst"], with_embedding=True)
    emb = session.scalars(select(Embedding).where(Embedding.source_id == src.id)).one()
    assert emb.vector == pytest.approx([0.1, 0.2, 0.3]) and emb.dimension == 3
    chunk = session.scalars(select(Chunk).where(Chunk.document_id == doc.id)).one()
    session.add(
        Embedding(chunk_id=chunk.id, source_id=src.id, model_name="m", model_version="1", dimension=4, vector=[1, 2, 3])
    )
    with pytest.raises(IntegrityError, match="dimension_matches"):
        session.flush()
    session.rollback()


def test_external_ai_scope_rejected_by_database(session: Session) -> None:
    src = _source(session)
    c = PrivacyConsent(
        source_id=src.id,
        subject_sub="u",
        purpose="search_index",
        scopes=["metadata.read"],
        snapshot_sha256="0" * 64,
        consent_text_version="v",
        granted_by="u",
    )
    session.add(c)
    session.flush()
    session.add(ConnectorPermission(source_id=src.id, consent_id=c.id, scope="ai.external", granted_by="u"))
    with pytest.raises(IntegrityError, match="ck_connector_permission_scope"):
        session.flush()
    session.rollback()


def test_one_active_consent_per_source(session: Session) -> None:
    src = _source(session)
    for _ in range(2):
        session.add(
            PrivacyConsent(
                source_id=src.id,
                subject_sub="u",
                purpose="search_index",
                scopes=[],
                snapshot_sha256="0" * 64,
                consent_text_version="v",
                granted_by="u",
            )
        )
    with pytest.raises(IntegrityError, match="uq_privacy_consent_active_source"):
        session.flush()
    session.rollback()


def test_kid_never_on_sensitive_source(session: Session) -> None:
    with pytest.raises(IntegrityError, match="kid_not_sensitive"):
        _source(session, kid_allowed=True, classification="sensitive")
    session.rollback()


def test_no_invented_timecodes_half_pair(session: Session) -> None:
    src = _source(session)
    doc = DocumentRef(source_id=src.id, external_id="/bronnen/x/film.mkv", title="film")
    session.add(doc)
    session.flush()
    session.add(Chunk(document_id=doc.id, source_id=src.id, ordinal=0, text="x", text_sha256="0" * 64, start_ms=5))
    with pytest.raises(IntegrityError, match="timecode_pair"):
        session.flush()
    session.rollback()


def test_cascade_removes_all_derived_rows(session: Session) -> None:
    src = _source(session)
    simulate_ingest(session, src.id, "/bronnen/x/a.srt", ["a", "b"], timecodes=[(0, 10), (10, 20)])
    session.delete(session.get(IndexSource, src.id))
    session.commit()
    for model in (DocumentRef, Chunk, Embedding):
        assert session.scalars(select(model).where(model.source_id == src.id)).first() is None
    assert uuid.UUID(str(src.id))
