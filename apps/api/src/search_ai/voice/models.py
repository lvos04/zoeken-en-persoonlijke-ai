"""Datamodel B15 — spraak/persoonlijke AI (P0: alleen schema; logica volgt in P5).

Privacy by design (B15-002/B15-006):
- **Geen ruwe audio**: geen enkele tabel heeft een binaire kolom; ``tts_job`` bewaart alleen status, geen audio.
- Transcripten worden standaard niet bewaard (retentie ``voice_transcript`` = 0 dagen): ``transcript.expires_at``
  is verplicht en een sessie-einde ruimt ze op.
- Een herkende zin is **alleen tekst**: ``intent.decision`` kan nooit 'uitgevoerd' zijn. Acties lopen als
  ``ActionProposal`` via de policy-engine van de Hub (``proposal_ref``); er is geen uitvoerpad in deze dienst.
- Toestemming per apparaat en soort (microfoon, STT-verwerking, transcript bewaren, download) in ``audio_consent``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base, new_id, utcnow
from ..search.models import _in

SESSION_STATUSES = ("active", "ended", "expired", "revoked")
INTENT_DECISIONS = ("answered", "proposal_created", "denied", "needs_confirmation", "unknown", "capability_unavailable")
TTS_STATUSES = ("queued", "done", "failed", "skipped_muted", "capability_unavailable")
CONTEXT_KINDS = ("room", "profile", "device", "search_source")
AUDIO_CONSENT_KINDS = ("microphone", "stt_processing", "transcript_retention", "download")


class VoiceSession(Base):
    """Eén chat-/spraaksessie per gebruiker en apparaat, met vervaltijd (B15-005)."""

    __tablename__ = "voice_session"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    principal_sub: Mapped[str] = mapped_column(String(200), nullable=False)
    device_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    input_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="text")
    stt_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="none")
    tts_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("status", SESSION_STATUSES), name="status"),
        CheckConstraint(_in("input_mode", ("text", "push_to_talk")), name="input_mode"),
        CheckConstraint("status = 'active' OR ended_at IS NOT NULL", name="ended_has_time"),
        Index("ix_voice_session_principal", "principal_sub", "started_at"),
        Index("ix_voice_session_status_expires", "status", "expires_at"),
    )


class Transcript(Base):
    """Geschoonde tekst van een beurt (getypt, uit STT of het antwoord). Nooit audio."""

    __tablename__ = "transcript"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("voice_session.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    scrubbed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    stt_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "ordinal"),
        CheckConstraint(_in("role", ("user", "assistant")), name="role"),
        CheckConstraint(_in("origin", ("typed", "stt", "assistant")), name="origin"),
        CheckConstraint("origin <> 'stt' OR role = 'user'", name="stt_is_user"),
        CheckConstraint("stt_confidence IS NULL OR (stt_confidence >= 0 AND stt_confidence <= 1)", name="confidence"),
        Index("ix_transcript_expires_at", "expires_at"),
    )


class Intent(Base):
    """Uitkomst van de smalle intentrouter (B15-003). Kan een voorstel naar de Hub zijn, nooit een uitvoering."""

    __tablename__ = "intent"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("voice_session.id", ondelete="CASCADE"), nullable=False
    )
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transcript.id", ondelete="CASCADE"), nullable=True
    )
    intent_name: Mapped[str] = mapped_column(String(80), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    policy_reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    proposal_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(_in("decision", INTENT_DECISIONS), name="decision"),
        CheckConstraint("decision <> 'proposal_created' OR proposal_ref IS NOT NULL", name="proposal_has_ref"),
        Index("ix_intent_session_id", "session_id"),
    )


class ContextScope(Base):
    """Expliciet toegekende context (kamer, profiel, apparaat, zoekbron) met vervaltijd (B15-005)."""

    __tablename__ = "context_scope"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("voice_session.id", ondelete="CASCADE"), nullable=True
    )
    principal_sub: Mapped[str] = mapped_column(String(200), nullable=False)
    context_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    context_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    search_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("index_source.id", ondelete="CASCADE"), nullable=True
    )
    granted_by: Mapped[str] = mapped_column(String(200), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("context_kind", CONTEXT_KINDS), name="context_kind"),
        CheckConstraint("context_kind <> 'search_source' OR search_source_id IS NOT NULL", name="search_source_has_id"),
        Index("ix_context_scope_principal", "principal_sub"),
    )


class TtsJob(Base):
    """Uitspreken van een antwoord (B15-004). Alleen status: audio wordt nooit opgeslagen."""

    __tablename__ = "tts_job"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("voice_session.id", ondelete="CASCADE"), nullable=False
    )
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transcript.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="none")
    voice: Mapped[str | None] = mapped_column(String(80), nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("status", TTS_STATUSES), name="status"),
        Index("ix_tts_job_session_id", "session_id"),
    )


class AudioConsent(Base):
    """Toestemming per gebruiker, apparaat en soort; standaard niets toegestaan (opt-in)."""

    __tablename__ = "audio_consent"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    principal_sub: Mapped[str] = mapped_column(String(200), nullable=False)
    device_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_text_version: Mapped[str] = mapped_column(String(40), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("kind", AUDIO_CONSENT_KINDS), name="kind"),
        Index("ix_audio_consent_lookup", "principal_sub", "device_id", "kind"),
    )
