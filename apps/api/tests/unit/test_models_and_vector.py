"""Datamodel B07/B15: verplichte tabellen, privacy-invarianten in het schema en het pgvector-type."""

from __future__ import annotations

import math

import pytest
from sqlalchemy import LargeBinary

from search_ai.models import all_models
from search_ai.search.models import DB_SCOPES
from search_ai.search.vector import from_pg, to_pg

B07_TABLES = {
    "index_source",
    "connector_permission",
    "document_ref",
    "chunk",
    "embedding",
    "index_run",
    "search_query",
    "access_scope",
    "transcript_ref",
    "privacy_consent",
    "retention",
}
B15_TABLES = {"voice_session", "transcript", "intent", "context_scope", "tts_job", "audio_consent"}


def test_all_required_tables_exist() -> None:
    tables = set(all_models().metadata.tables)
    assert B07_TABLES <= tables
    assert B15_TABLES <= tables


def test_no_raw_audio_column_anywhere_in_b15() -> None:
    """B15-006: geen ruwe audio-opslag — geen enkele binaire kolom in de spraaktabellen."""
    md = all_models().metadata
    for name in B15_TABLES:
        for col in md.tables[name].columns:
            assert not isinstance(col.type, LargeBinary), f"{name}.{col.name} is binair"
            assert "audio" not in col.name or col.name == "audio_ref", f"{name}.{col.name}"


def test_external_ai_scope_not_storable() -> None:
    """'Verlaat NAS niet': de scope ai.external bestaat niet in de database-CHECK."""
    assert "ai.external" not in DB_SCOPES
    ck = [c for c in all_models().metadata.tables["connector_permission"].constraints if c.name and "scope" in str(c.name)]
    assert ck and "ai.external" not in str(ck[0].sqltext)  # type: ignore[attr-defined]


def test_embedding_has_model_and_dimension_metadata() -> None:
    cols = all_models().metadata.tables["embedding"].columns
    assert {"model_name", "model_version", "dimension", "vector", "metadata"} <= set(cols.keys())


def test_intent_can_never_record_execution() -> None:
    from search_ai.voice.models import INTENT_DECISIONS

    assert "executed" not in INTENT_DECISIONS and "uitgevoerd" not in INTENT_DECISIONS


def test_vector_text_roundtrip() -> None:
    assert to_pg([1, 0.5, -2.25]) == "[1.0,0.5,-2.25]"
    assert from_pg("[1,0.5,-2.25]") == [1.0, 0.5, -2.25]
    with pytest.raises(ValueError):
        to_pg([])
    with pytest.raises(ValueError):
        to_pg([math.nan])
    with pytest.raises(ValueError):
        from_pg("1,2")
