"""Datamodel B07 — persoonlijke zoekmachine.

Uitgangspunten (B07 §2, B07-001, I14/I25, 02_DATA_EIGENAARSCHAP):
- **Primaire gegevens blijven bij de bron** (bestanden, Immich, Jellyfin, logs). Deze tabellen bevatten alleen
  afgeleide indexdata (verwijzingen, tekstfragmenten, embeddings) plus toestemming, rechten en administratie.
- Niets wordt geïndexeerd zonder actieve toestemming (``privacy_consent``) met expliciete scopes
  (``connector_permission``); een bron zonder toestemming heeft status ``draft`` en levert niets op.
- Gebruikers-ACL (``access_scope``) wordt in de query vóór ranking toegepast (zie ``acl.py``).
- Intrekken verwijdert alle afgeleide data van de bron (``document_ref`` → ``chunk`` → ``embedding`` en
  ``transcript_ref`` via ``ON DELETE CASCADE``) en legt een gecontroleerde purge vast in ``index_run``.
- Elke entiteit heeft een stabiele UUID, tijden, eigenaar/actor en waar van toepassing versie en herkomst.
- Geheimen alleen als ``vault://``-referentie. De scope ``ai.external`` bestaat bewust niet in de database: in deze
  versie verlaat geen inhoud de NAS (ook niet per ongeluk via een latere codewijziging zonder migratie).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, Timestamps, new_id, utcnow
from .vector import Vector

SOURCE_KINDS = ("files", "immich", "jellyfin", "logs")
SOURCE_STATUSES = ("draft", "active", "paused", "revoked")
CLASSIFICATIONS = ("internal", "sensitive")
PURGE_STATUSES = ("none", "pending", "done", "failed")
# Scopes die toestemming kan geven. 'ai.external' ontbreekt bewust (zie module-docstring).
DB_SCOPES = ("metadata.read", "content.read", "embeddings.local", "transcripts.read")
ROLE_NAMES = ("admin", "operator", "viewer", "kind")
RUN_KINDS = ("scan", "ingest", "purge", "reindex")
RUN_STATUSES = ("queued", "running", "succeeded", "failed", "capability_unavailable")
RETENTION_SCOPES = (
    "search_query",
    "index_run",
    "audit_event",
    "idempotency_record",
    "voice_session",
    "voice_transcript",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ",".join(f"'{v}'" for v in values) + ")"


class IndexSource(Timestamps, Base):
    """Een door een beheerder geselecteerde bron (share/map, Immich, Jellyfin, logs).

    ``allowed_paths`` is de lees-padenallowlist van deze bron: alleen paden binnen deze lijst (en binnen de
    systeembrede leeswortels) kunnen ooit worden gelezen. ``exclude_patterns`` sluit daarbinnen extra uit; een vaste
    lijst gevoelige patronen (sleutels, .ssh, .zfs-snapshots, …) geldt altijd.
    """

    __tablename__ = "index_source"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    owner_sub: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    classification: Mapped[str] = mapped_column(String(20), nullable=False, default="sensitive")
    kid_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allowed_paths: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    exclude_patterns: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(200), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    purge_status: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    purge_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_index_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("kind", SOURCE_KINDS), name="kind"),
        CheckConstraint(_in("status", SOURCE_STATUSES), name="status"),
        CheckConstraint(_in("classification", CLASSIFICATIONS), name="classification"),
        CheckConstraint(_in("purge_status", PURGE_STATUSES), name="purge_status"),
        CheckConstraint("credential_ref IS NULL OR credential_ref LIKE 'vault://%'", name="credential_is_vault_ref"),
        CheckConstraint("jsonb_typeof(allowed_paths) = 'array'", name="allowed_paths_array"),
        CheckConstraint("jsonb_typeof(exclude_patterns) = 'array'", name="exclude_patterns_array"),
        CheckConstraint("status <> 'revoked' OR revoked_at IS NOT NULL", name="revoked_has_time"),
        CheckConstraint("NOT (kid_allowed AND classification = 'sensitive')", name="kid_not_sensitive"),
        Index("ix_index_source_status", "status"),
    )


class PrivacyConsent(Base):
    """Toestemming van de gegevenseigenaar, gebonden aan een exacte momentopname van bron + scopes.

    Wijzigt de bron (paden, uitsluitingen, adres) of de gevraagde scopes, dan past ``snapshot_sha256`` niet meer en is
    nieuwe toestemming nodig (zelfde principe als een gebonden goedkeuring in de Hub).
    """

    __tablename__ = "privacy_consent"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_source.id", ondelete="CASCADE"), nullable=True
    )
    subject_sub: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    scope_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    consent_text_version: Mapped[str] = mapped_column(String(40), nullable=False)
    granted_by: Mapped[str] = mapped_column(String(200), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("purpose", ("search_index", "voice_transcripts", "voice_context")), name="purpose"),
        CheckConstraint("jsonb_typeof(scopes) = 'array'", name="scopes_array"),
        CheckConstraint("purpose <> 'search_index' OR source_id IS NOT NULL", name="index_consent_has_source"),
        Index(
            "uq_privacy_consent_active_source",
            "source_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL AND source_id IS NOT NULL"),
        ),
    )


class ConnectorPermission(Base):
    """Eén toegekende scope per bron, altijd gekoppeld aan de toestemming waaruit hij volgt."""

    __tablename__ = "connector_permission"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_source.id", ondelete="CASCADE"), nullable=False
    )
    consent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("privacy_consent.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(40), nullable=False)
    granted_by: Mapped[str] = mapped_column(String(200), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("scope", DB_SCOPES), name="scope"),
        Index(
            "uq_connector_permission_active",
            "source_id",
            "scope",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class AccessScope(Base):
    """Gebruikers-ACL per bron: wie (gebruiker ``sub`` of rol) mag resultaten uit deze bron zien."""

    __tablename__ = "access_scope"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_source.id", ondelete="CASCADE"), nullable=False
    )
    principal_type: Mapped[str] = mapped_column(String(10), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(200), nullable=False)
    permission: Mapped[str] = mapped_column(String(20), nullable=False, default="read")
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("principal_type", ("user", "role")), name="principal_type"),
        CheckConstraint("permission = 'read'", name="permission"),
        CheckConstraint(f"principal_type <> 'role' OR {_in('principal_id', ROLE_NAMES)}", name="role_known"),
        Index(
            "uq_access_scope_active",
            "source_id",
            "principal_type",
            "principal_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class DocumentRef(Timestamps, Base):
    """Verwijzing naar één origineel bij de bron (nooit een kopie van het origineel)."""

    __tablename__ = "document_ref"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_source.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_etag: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    classification: Mapped[str] = mapped_column(String(20), nullable=False, default="sensitive")
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, default="source")
    owner_sub: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kid_safe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parental_rating: Mapped[str | None] = mapped_column(String(20), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        CheckConstraint(_in("classification", CLASSIFICATIONS), name="classification"),
        CheckConstraint(_in("visibility", ("source", "owner")), name="visibility"),
        CheckConstraint("visibility <> 'owner' OR owner_sub IS NOT NULL", name="owner_visibility_has_owner"),
        Index("ix_document_ref_source_id", "source_id"),
    )


class Chunk(Base):
    """Tekstfragment van een document; ``tsv`` voor PostgreSQL-full-text. Tijdcodes alleen als de bron ze heeft."""

    __tablename__ = "chunk"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_ref.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_source.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple'::regconfig, coalesce(text, ''))", persisted=True)
    )
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal"),
        CheckConstraint("(start_ms IS NULL) = (end_ms IS NULL)", name="timecode_pair"),
        CheckConstraint("start_ms IS NULL OR (start_ms >= 0 AND end_ms >= start_ms)", name="timecode_order"),
        Index("ix_chunk_source_id", "source_id"),
        Index("ix_chunk_tsv", "tsv", postgresql_using="gin"),
    )


class Embedding(Base):
    """Vector per chunk en model. Model, versie en dimensie staan in de rij (metadata); dimensie wordt afgedwongen."""

    __tablename__ = "embedding"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunk.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_source.id", ondelete="CASCADE"), nullable=False
    )
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    model_version: Mapped[str] = mapped_column(String(60), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("chunk_id", "model_name", "model_version"),
        CheckConstraint("dimension BETWEEN 1 AND 16000", name="dimension_range"),
        CheckConstraint("vector_dims(vector) = dimension", name="dimension_matches"),
        Index("ix_embedding_source_id", "source_id"),
        Index("ix_embedding_model", "model_name", "model_version"),
    )


class IndexRun(Base):
    """Eén scan/ingest/purge/reindex-run met tellingen en (bij purge) de controle achteraf."""

    __tablename__ = "index_run"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_source.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    triggered_by: Mapped[str] = mapped_column(String(200), nullable=False)
    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(_in("kind", RUN_KINDS), name="kind"),
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        Index("ix_index_run_source_created", "source_id", "created_at"),
    )


class SearchQuery(Base):
    """Zoekgeschiedenis. Standaard **uit** (retentie 0 dagen): dan wordt er niets opgeslagen."""

    __tablename__ = "search_query"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    principal_sub: Mapped[str] = mapped_column(String(200), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(_in("mode", ("fulltext", "semantic", "hybrid")), name="mode"),
        Index("ix_search_query_expires_at", "expires_at"),
        Index("ix_search_query_principal", "principal_sub", "created_at"),
    )


class TranscriptRef(Base):
    """Bestaande ondertitel/transcript bij een mediabron (B07-004). Zonder transcript: géén tijdcodes."""

    __tablename__ = "transcript_ref"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_ref.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_source.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    external_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    has_timecodes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("document_id", "external_ref"),
        CheckConstraint(_in("kind", ("subtitle", "transcript", "asr")), name="kind"),
        CheckConstraint(_in("origin", ("existing", "asr_opt_in")), name="origin"),
        CheckConstraint("kind <> 'asr' OR origin = 'asr_opt_in'", name="asr_needs_opt_in"),
        Index("ix_transcript_ref_source_id", "source_id"),
    )


class Retention(Base):
    """Bewaartermijn per soort gegevens (optioneel per bron). ``keep_days = 0`` betekent: niet bewaren."""

    __tablename__ = "retention"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_source.id", ondelete="CASCADE"), nullable=True
    )
    keep_days: Mapped[int] = mapped_column(Integer, nullable=False)
    note_nl: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(200), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        CheckConstraint(_in("scope", RETENTION_SCOPES), name="scope"),
        CheckConstraint("keep_days BETWEEN 0 AND 3650", name="keep_days_range"),
        UniqueConstraint("scope", "source_id", postgresql_nulls_not_distinct=True),
    )
