"""Basisschema: platform (koppeling, idempotency, outbox, vault, audit), B07 zoeken en B15 spraak.

Revision ID: 0001
Revises:
Create Date: 2026-11-09

- Installeert de pgvector-extensie (``CREATE EXTENSION vector``); de alles-in-één image levert pgvector 0.8.7 mee.
- ``chunk.tsv`` is een gegenereerde ``tsvector`` met GIN-index (full-text, B07-003).
- ``embedding.vector`` is ``vector`` zonder vaste dimensie; model/versie/dimensie per rij (CHECK op ``vector_dims``).
  Een ANN-index (HNSW) per model volgt in B07-003 als aparte migratie.
- Zet de standaard-bewaartermijnen: zoekgeschiedenis en spraaktranscripten 0 dagen (= niet bewaren).
- Downgrade verwijdert alle tabellen (alleen afgeleide data en administratie; primaire gegevens staan bij de bron).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType


class _Vector(UserDefinedType):  # type: ignore[type-arg]
    """Lokale kopie van het pgvector-type: migraties hangen niet af van (latere) app-code."""

    cache_ok = True

    def get_col_spec(self, **_kw: object) -> str:
        return "VECTOR"


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "audio_consent",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("principal_sub", sa.String(length=200), nullable=False),
        sa.Column("device_id", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("consent_text_version", sa.String(length=40), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('microphone','stt_processing','transcript_retention','download')",
            name=op.f("ck_audio_consent_kind"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audio_consent")),
    )
    op.create_index("ix_audio_consent_lookup", "audio_consent", ["principal_sub", "device_id", "kind"], unique=False)
    op.create_table(
        "audit_event",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("domain", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=60), nullable=False),
        sa.Column("entity_id", sa.String(length=120), nullable=False),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("trace_id", sa.UUID(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("message_nl", sa.Text(), nullable=False),
        sa.CheckConstraint("domain IN ('platform','search','voice')", name=op.f("ck_audit_event_domain")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
    )
    op.create_index("ix_audit_event_at", "audit_event", ["at"], unique=False)
    op.create_index("ix_audit_event_entity", "audit_event", ["entity_type", "entity_id"], unique=False)
    op.create_table(
        "idempotency_record",
        sa.Column("principal", sa.String(length=200), nullable=False),
        sa.Column("key", sa.UUID(), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("principal", "key", name=op.f("pk_idempotency_record")),
    )
    op.create_index("ix_idempotency_record_created_at", "idempotency_record", ["created_at"], unique=False)
    op.create_table(
        "index_source",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("owner_sub", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("classification", sa.String(length=20), nullable=False),
        sa.Column("kid_allowed", sa.Boolean(), nullable=False),
        sa.Column("allowed_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exclude_patterns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("credential_ref", sa.String(length=200), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("updated_by", sa.String(length=200), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=200), nullable=True),
        sa.Column("purge_status", sa.String(length=20), nullable=False),
        sa.Column("purge_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_index_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "NOT (kid_allowed AND classification = 'sensitive')", name=op.f("ck_index_source_kid_not_sensitive")
        ),
        sa.CheckConstraint("classification IN ('internal','sensitive')", name=op.f("ck_index_source_classification")),
        sa.CheckConstraint(
            "credential_ref IS NULL OR credential_ref LIKE 'vault://%%'",
            name=op.f("ck_index_source_credential_is_vault_ref"),
        ),
        sa.CheckConstraint("jsonb_typeof(allowed_paths) = 'array'", name=op.f("ck_index_source_allowed_paths_array")),
        sa.CheckConstraint(
            "jsonb_typeof(exclude_patterns) = 'array'", name=op.f("ck_index_source_exclude_patterns_array")
        ),
        sa.CheckConstraint("kind IN ('files','immich','jellyfin','logs')", name=op.f("ck_index_source_kind")),
        sa.CheckConstraint(
            "purge_status IN ('none','pending','done','failed')", name=op.f("ck_index_source_purge_status")
        ),
        sa.CheckConstraint(
            "status <> 'revoked' OR revoked_at IS NOT NULL", name=op.f("ck_index_source_revoked_has_time")
        ),
        sa.CheckConstraint("status IN ('draft','active','paused','revoked')", name=op.f("ck_index_source_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_index_source")),
    )
    op.create_index("ix_index_source_status", "index_source", ["status"], unique=False)
    op.create_table(
        "outbox_event",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("entity_type", sa.String(length=60), nullable=False),
        sa.Column("entity_id", sa.String(length=120), nullable=False),
        sa.Column("timestamp_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.UUID(), nullable=False),
        sa.Column("correlation_id", sa.UUID(), nullable=True),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("classification", sa.String(length=20), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("delivery_status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "classification IN ('public','internal','sensitive')", name=op.f("ck_outbox_event_classification")
        ),
        sa.CheckConstraint(
            "delivery_status IN ('pending','delivered','dead_letter')", name=op.f("ck_outbox_event_delivery_status")
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_outbox_event")),
        sa.UniqueConstraint("event_id", name=op.f("uq_outbox_event_event_id")),
    )
    op.create_index("ix_outbox_event_pending", "outbox_event", ["delivery_status", "next_attempt_at"], unique=False)
    op.create_table(
        "search_query",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("principal_sub", sa.String(length=200), nullable=False),
        sa.Column("roles", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("source_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode IN ('fulltext','semantic','hybrid')", name=op.f("ck_search_query_mode")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_query")),
    )
    op.create_index("ix_search_query_expires_at", "search_query", ["expires_at"], unique=False)
    op.create_index("ix_search_query_principal", "search_query", ["principal_sub", "created_at"], unique=False)
    op.create_table(
        "service_state",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_service_state")),
    )
    op.create_table(
        "vault_secret",
        sa.Column("ref", sa.String(length=200), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("last4", sa.String(length=4), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ref", name=op.f("pk_vault_secret")),
    )
    op.create_table(
        "voice_session",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("principal_sub", sa.String(length=200), nullable=False),
        sa.Column("device_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("input_mode", sa.String(length=20), nullable=False),
        sa.Column("stt_provider", sa.String(length=40), nullable=False),
        sa.Column("tts_enabled", sa.Boolean(), nullable=False),
        sa.Column("muted", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=60), nullable=True),
        sa.CheckConstraint("input_mode IN ('text','push_to_talk')", name=op.f("ck_voice_session_input_mode")),
        sa.CheckConstraint("status = 'active' OR ended_at IS NOT NULL", name=op.f("ck_voice_session_ended_has_time")),
        sa.CheckConstraint("status IN ('active','ended','expired','revoked')", name=op.f("ck_voice_session_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_voice_session")),
    )
    op.create_index("ix_voice_session_principal", "voice_session", ["principal_sub", "started_at"], unique=False)
    op.create_index("ix_voice_session_status_expires", "voice_session", ["status", "expires_at"], unique=False)
    op.create_table(
        "access_scope",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("principal_type", sa.String(length=10), nullable=False),
        sa.Column("principal_id", sa.String(length=200), nullable=False),
        sa.Column("permission", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=200), nullable=True),
        sa.CheckConstraint("permission = 'read'", name=op.f("ck_access_scope_permission")),
        sa.CheckConstraint(
            "principal_type <> 'role' OR principal_id IN ('admin','operator','viewer','kind')",
            name=op.f("ck_access_scope_role_known"),
        ),
        sa.CheckConstraint("principal_type IN ('user','role')", name=op.f("ck_access_scope_principal_type")),
        sa.ForeignKeyConstraint(
            ["source_id"], ["index_source.id"], name=op.f("fk_access_scope_source_id_index_source"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_scope")),
    )
    op.create_index(
        "uq_access_scope_active",
        "access_scope",
        ["source_id", "principal_type", "principal_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "context_scope",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("principal_sub", sa.String(length=200), nullable=False),
        sa.Column("context_kind", sa.String(length=20), nullable=False),
        sa.Column("context_ref", sa.String(length=200), nullable=False),
        sa.Column("search_source_id", sa.UUID(), nullable=True),
        sa.Column("granted_by", sa.String(length=200), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "context_kind <> 'search_source' OR search_source_id IS NOT NULL",
            name=op.f("ck_context_scope_search_source_has_id"),
        ),
        sa.CheckConstraint(
            "context_kind IN ('room','profile','device','search_source')", name=op.f("ck_context_scope_context_kind")
        ),
        sa.ForeignKeyConstraint(
            ["search_source_id"],
            ["index_source.id"],
            name=op.f("fk_context_scope_search_source_id_index_source"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["voice_session.id"],
            name=op.f("fk_context_scope_session_id_voice_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_scope")),
    )
    op.create_index("ix_context_scope_principal", "context_scope", ["principal_sub"], unique=False)
    op.create_table(
        "document_ref",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=1024), nullable=False),
        sa.Column("uri", sa.String(length=2048), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("source_etag", sa.String(length=200), nullable=True),
        sa.Column("source_mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("classification", sa.String(length=20), nullable=False),
        sa.Column("visibility", sa.String(length=10), nullable=False),
        sa.Column("owner_sub", sa.String(length=200), nullable=True),
        sa.Column("kid_safe", sa.Boolean(), nullable=False),
        sa.Column("parental_rating", sa.String(length=20), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("classification IN ('internal','sensitive')", name=op.f("ck_document_ref_classification")),
        sa.CheckConstraint(
            "visibility <> 'owner' OR owner_sub IS NOT NULL", name=op.f("ck_document_ref_owner_visibility_has_owner")
        ),
        sa.CheckConstraint("visibility IN ('source','owner')", name=op.f("ck_document_ref_visibility")),
        sa.ForeignKeyConstraint(
            ["source_id"], ["index_source.id"], name=op.f("fk_document_ref_source_id_index_source"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_ref")),
        sa.UniqueConstraint("source_id", "external_id", name=op.f("uq_document_ref_source_id_external_id")),
    )
    op.create_index("ix_document_ref_source_id", "document_ref", ["source_id"], unique=False)
    op.create_table(
        "index_run",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verification", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("triggered_by", sa.String(length=200), nullable=False),
        sa.Column("trace_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('scan','ingest','purge','reindex')", name=op.f("ck_index_run_kind")),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','capability_unavailable')",
            name=op.f("ck_index_run_status"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["index_source.id"], name=op.f("fk_index_run_source_id_index_source"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_index_run")),
    )
    op.create_index("ix_index_run_source_created", "index_run", ["source_id", "created_at"], unique=False)
    op.create_table(
        "privacy_consent",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("subject_sub", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.String(length=40), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scope_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("consent_text_version", sa.String(length=40), nullable=False),
        sa.Column("granted_by", sa.String(length=200), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=200), nullable=True),
        sa.Column("revoke_reason", sa.String(length=200), nullable=True),
        sa.CheckConstraint("jsonb_typeof(scopes) = 'array'", name=op.f("ck_privacy_consent_scopes_array")),
        sa.CheckConstraint(
            "purpose <> 'search_index' OR source_id IS NOT NULL",
            name=op.f("ck_privacy_consent_index_consent_has_source"),
        ),
        sa.CheckConstraint(
            "purpose IN ('search_index','voice_transcripts','voice_context')", name=op.f("ck_privacy_consent_purpose")
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["index_source.id"],
            name=op.f("fk_privacy_consent_source_id_index_source"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_privacy_consent")),
    )
    op.create_index(
        "uq_privacy_consent_active_source",
        "privacy_consent",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL AND source_id IS NOT NULL"),
    )
    op.create_table(
        "retention",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("keep_days", sa.Integer(), nullable=False),
        sa.Column("note_nl", sa.String(length=300), nullable=False),
        sa.Column("updated_by", sa.String(length=200), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope IN ('search_query','index_run','audit_event','idempotency_record','voice_session','voice_transcript')",
            name=op.f("ck_retention_scope"),
        ),
        sa.CheckConstraint("keep_days BETWEEN 0 AND 3650", name=op.f("ck_retention_keep_days_range")),
        sa.ForeignKeyConstraint(
            ["source_id"], ["index_source.id"], name=op.f("fk_retention_source_id_index_source"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retention")),
        sa.UniqueConstraint(
            "scope", "source_id", name=op.f("uq_retention_scope_source_id"), postgresql_nulls_not_distinct=True
        ),
    )
    op.create_table(
        "transcript",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("scrubbed", sa.Boolean(), nullable=False),
        sa.Column("stt_confidence", sa.Float(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("origin <> 'stt' OR role = 'user'", name=op.f("ck_transcript_stt_is_user")),
        sa.CheckConstraint("origin IN ('typed','stt','assistant')", name=op.f("ck_transcript_origin")),
        sa.CheckConstraint("role IN ('user','assistant')", name=op.f("ck_transcript_role")),
        sa.CheckConstraint(
            "stt_confidence IS NULL OR (stt_confidence >= 0 AND stt_confidence <= 1)",
            name=op.f("ck_transcript_confidence"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["voice_session.id"],
            name=op.f("fk_transcript_session_id_voice_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcript")),
        sa.UniqueConstraint("session_id", "ordinal", name=op.f("uq_transcript_session_id_ordinal")),
    )
    op.create_index("ix_transcript_expires_at", "transcript", ["expires_at"], unique=False)
    op.create_table(
        "chunk",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple'::regconfig, coalesce(text, ''))", persisted=True),
            nullable=False,
        ),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("start_ms", sa.BigInteger(), nullable=True),
        sa.Column("end_ms", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("(start_ms IS NULL) = (end_ms IS NULL)", name=op.f("ck_chunk_timecode_pair")),
        sa.CheckConstraint(
            "start_ms IS NULL OR (start_ms >= 0 AND end_ms >= start_ms)", name=op.f("ck_chunk_timecode_order")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document_ref.id"], name=op.f("fk_chunk_document_id_document_ref"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["index_source.id"], name=op.f("fk_chunk_source_id_index_source"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunk")),
        sa.UniqueConstraint("document_id", "ordinal", name=op.f("uq_chunk_document_id_ordinal")),
    )
    op.create_index("ix_chunk_source_id", "chunk", ["source_id"], unique=False)
    op.create_index("ix_chunk_tsv", "chunk", ["tsv"], unique=False, postgresql_using="gin")
    op.create_table(
        "connector_permission",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("consent_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.String(length=40), nullable=False),
        sa.Column("granted_by", sa.String(length=200), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=200), nullable=True),
        sa.CheckConstraint(
            "scope IN ('metadata.read','content.read','embeddings.local','transcripts.read')",
            name=op.f("ck_connector_permission_scope"),
        ),
        sa.ForeignKeyConstraint(
            ["consent_id"],
            ["privacy_consent.id"],
            name=op.f("fk_connector_permission_consent_id_privacy_consent"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["index_source.id"],
            name=op.f("fk_connector_permission_source_id_index_source"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_permission")),
    )
    op.create_index(
        "uq_connector_permission_active",
        "connector_permission",
        ["source_id", "scope"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "intent",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("transcript_id", sa.UUID(), nullable=True),
        sa.Column("intent_name", sa.String(length=80), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("decision", sa.String(length=30), nullable=False),
        sa.Column("policy_reason_code", sa.String(length=80), nullable=True),
        sa.Column("proposal_ref", sa.String(length=200), nullable=True),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision <> 'proposal_created' OR proposal_ref IS NOT NULL", name=op.f("ck_intent_proposal_has_ref")
        ),
        sa.CheckConstraint(
            "decision IN ('answered','proposal_created','denied','needs_confirmation','unknown','capability_unavailable')",
            name=op.f("ck_intent_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["voice_session.id"], name=op.f("fk_intent_session_id_voice_session"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["transcript_id"], ["transcript.id"], name=op.f("fk_intent_transcript_id_transcript"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intent")),
    )
    op.create_index("ix_intent_session_id", "intent", ["session_id"], unique=False)
    op.create_table(
        "transcript_ref",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("external_ref", sa.String(length=1024), nullable=False),
        sa.Column("has_timecodes", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind <> 'asr' OR origin = 'asr_opt_in'", name=op.f("ck_transcript_ref_asr_needs_opt_in")),
        sa.CheckConstraint("kind IN ('subtitle','transcript','asr')", name=op.f("ck_transcript_ref_kind")),
        sa.CheckConstraint("origin IN ('existing','asr_opt_in')", name=op.f("ck_transcript_ref_origin")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document_ref.id"],
            name=op.f("fk_transcript_ref_document_id_document_ref"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["index_source.id"],
            name=op.f("fk_transcript_ref_source_id_index_source"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcript_ref")),
        sa.UniqueConstraint("document_id", "external_ref", name=op.f("uq_transcript_ref_document_id_external_ref")),
    )
    op.create_index("ix_transcript_ref_source_id", "transcript_ref", ["source_id"], unique=False)
    op.create_table(
        "tts_job",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("transcript_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("voice", sa.String(length=80), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','done','failed','skipped_muted','capability_unavailable')",
            name=op.f("ck_tts_job_status"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["voice_session.id"], name=op.f("fk_tts_job_session_id_voice_session"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["transcript_id"], ["transcript.id"], name=op.f("fk_tts_job_transcript_id_transcript"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tts_job")),
    )
    op.create_index("ix_tts_job_session_id", "tts_job", ["session_id"], unique=False)
    op.create_table(
        "embedding",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("chunk_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("model_version", sa.String(length=60), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("vector", _Vector(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("dimension BETWEEN 1 AND 16000", name=op.f("ck_embedding_dimension_range")),
        sa.CheckConstraint("vector_dims(vector) = dimension", name=op.f("ck_embedding_dimension_matches")),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["chunk.id"], name=op.f("fk_embedding_chunk_id_chunk"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["index_source.id"], name=op.f("fk_embedding_source_id_index_source"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedding")),
        sa.UniqueConstraint(
            "chunk_id", "model_name", "model_version", name=op.f("uq_embedding_chunk_id_model_name_model_version")
        ),
    )
    op.create_index("ix_embedding_model", "embedding", ["model_name", "model_version"], unique=False)
    op.create_index("ix_embedding_source_id", "embedding", ["source_id"], unique=False)

    retention = sa.table(
        "retention",
        sa.column("id", sa.UUID()),
        sa.column("scope", sa.String()),
        sa.column("source_id", sa.UUID()),
        sa.column("keep_days", sa.Integer()),
        sa.column("note_nl", sa.String()),
        sa.column("updated_by", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    defaults = [
        ("search_query", 0, "Zoekgeschiedenis wordt standaard niet bewaard."),
        ("voice_transcript", 0, "Spraaktranscripten worden standaard niet bewaard."),
        ("voice_session", 30, "Sessiegegevens (zonder inhoud) 30 dagen."),
        ("index_run", 180, "Index- en purge-runs 180 dagen (bewijs van opschonen)."),
        ("audit_event", 365, "Auditspoor 365 dagen."),
        ("idempotency_record", 7, "Idempotency-records 7 dagen."),
    ]
    now = datetime.now(UTC)
    op.bulk_insert(
        retention,
        [
            {
                "id": uuid.uuid4(),
                "scope": scope,
                "source_id": None,
                "keep_days": days,
                "note_nl": note,
                "updated_by": "system:migratie-0001",
                "updated_at": now,
            }
            for scope, days, note in defaults
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_embedding_source_id", table_name="embedding")
    op.drop_index("ix_embedding_model", table_name="embedding")
    op.drop_table("embedding")
    op.drop_index("ix_tts_job_session_id", table_name="tts_job")
    op.drop_table("tts_job")
    op.drop_index("ix_transcript_ref_source_id", table_name="transcript_ref")
    op.drop_table("transcript_ref")
    op.drop_index("ix_intent_session_id", table_name="intent")
    op.drop_table("intent")
    op.drop_index(
        "uq_connector_permission_active",
        table_name="connector_permission",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.drop_table("connector_permission")
    op.drop_index("ix_chunk_tsv", table_name="chunk", postgresql_using="gin")
    op.drop_index("ix_chunk_source_id", table_name="chunk")
    op.drop_table("chunk")
    op.drop_index("ix_transcript_expires_at", table_name="transcript")
    op.drop_table("transcript")
    op.drop_table("retention")
    op.drop_index(
        "uq_privacy_consent_active_source",
        table_name="privacy_consent",
        postgresql_where=sa.text("revoked_at IS NULL AND source_id IS NOT NULL"),
    )
    op.drop_table("privacy_consent")
    op.drop_index("ix_index_run_source_created", table_name="index_run")
    op.drop_table("index_run")
    op.drop_index("ix_document_ref_source_id", table_name="document_ref")
    op.drop_table("document_ref")
    op.drop_index("ix_context_scope_principal", table_name="context_scope")
    op.drop_table("context_scope")
    op.drop_index("uq_access_scope_active", table_name="access_scope", postgresql_where=sa.text("revoked_at IS NULL"))
    op.drop_table("access_scope")
    op.drop_index("ix_voice_session_status_expires", table_name="voice_session")
    op.drop_index("ix_voice_session_principal", table_name="voice_session")
    op.drop_table("voice_session")
    op.drop_table("vault_secret")
    op.drop_table("service_state")
    op.drop_index("ix_search_query_principal", table_name="search_query")
    op.drop_index("ix_search_query_expires_at", table_name="search_query")
    op.drop_table("search_query")
    op.drop_index("ix_outbox_event_pending", table_name="outbox_event")
    op.drop_table("outbox_event")
    op.drop_index("ix_index_source_status", table_name="index_source")
    op.drop_table("index_source")
    op.drop_index("ix_idempotency_record_created_at", table_name="idempotency_record")
    op.drop_table("idempotency_record")
    op.drop_index("ix_audit_event_entity", table_name="audit_event")
    op.drop_index("ix_audit_event_at", table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_index("ix_audio_consent_lookup", table_name="audio_consent")
    op.drop_table("audio_consent")
    op.execute("DROP EXTENSION IF EXISTS vector")
