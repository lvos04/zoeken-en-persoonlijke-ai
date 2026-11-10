r"""Outbox met event-envelope (contract 1.3 §7).

Events worden in dezelfde transactie als de wijziging vastgelegd. De worker pusht ze naar
``POST {HUB_URL}/api/v1/events/inbox`` (service-token) met backoff; na 10 mislukte pogingen → ``dead_letter``
(zichtbaar via de connector en de Hub). De Hub kan ook pollen via ``GET /connector/v1/events?cursor=``.
Geen exact-once-aanname: de Hub dedupliceert op ``event_id``. Payloads bevatten nooit documentinhoud of paden.

Regels van 1.3 (C1.3-ZPA-05):
- **Domeinen** van deze dienst: ``search`` en ``voice``; gedeeld alleen ``app.health.changed`` en de vaste
  ``service.*``-lijst (``SERVICE_EVENTS``, entiteit ``{type: "service", id: <app_id>}``). Een ander domein of een andere
  ``service.*``-naam is een programmeerfout (``ValueError`` bij ``emit``).
- **Grenzen van de Hub-inbox** worden vóór verzending gecontroleerd (``validate_envelope``), zodat een event niet pas
  als dead-letter opvalt: ``event_type`` precies drie segmenten ``^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$`` (≤ 128),
  ``entity.type`` ``^[a-z0-9_.-]{1,64}$``, ``entity.id`` 1–200, ``actor.id`` 1–128, ``source_event_id`` 1–200,
  ``payload`` ≤ 16 KiB als JSON (groter: ingekort met ``_ingekort: true``), ``timestamp_utc`` met tijdzone, geen
  onbekende velden.
- **Ernst** (optioneel): ``payload.ernst`` ∈ ``kritiek|hoog|middel|laag|info``.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import APP_ID, SCHEMA_VERSION
from .models import OutboxEvent
from .redact import redact
from .timeutil import iso, utcnow

MAX_ATTEMPTS = 10
CLASSIFICATIONS = ("public", "internal", "sensitive")
ACTOR_TYPES = ("system", "user", "ai", "flow")
OWN_DOMAINS = ("search", "voice")  # §7: domeinen van zoeken-en-persoonlijke-ai
SERVICE_EVENTS = frozenset(
    {
        "service.hub_link.linked",
        "service.hub_link.unlinked",
        "service.hub_link.revoked",
        "service.token.rotated",
        "service.key_rotation.applied",
        "service.key_rotation.rejected",
    }
)
SHARED_APP_EVENTS = frozenset({"app.health.changed"})
ERNST = ("kritiek", "hoog", "middel", "laag", "info")
EVENT_TYPE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+")
ENTITY_TYPE = re.compile(r"[a-z0-9_.-]{1,64}")
MAX_EVENT_TYPE = 128
MAX_ID = 200
MAX_ACTOR_ID = 128
MAX_PAYLOAD_BYTES = 16 * 1024
ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "source_app",
        "source_event_id",
        "event_type",
        "entity",
        "timestamp_utc",
        "trace_id",
        "correlation_id",
        "actor",
        "classification",
        "payload",
        "payload_ref",
    }
)


def check_event_type(event_type: str) -> None:
    """Vorm (drie segmenten, ≤ 128) én eigenaar (§7): alleen eigen domeinen, ``app.health.changed`` en de vaste
    ``service.*``-lijst. ``ValueError`` bij elke afwijking (programmeerfout, nooit een runtime-invoer)."""
    if len(event_type) > MAX_EVENT_TYPE or not EVENT_TYPE.fullmatch(event_type):
        raise ValueError(f"event_type {event_type!r} is niet <domein>.<object>.<gebeurtenis>")
    domain = event_type.split(".", 1)[0]
    if domain == "service":
        if event_type not in SERVICE_EVENTS:
            raise ValueError(f"{event_type!r} staat niet in de vaste service.*-lijst (§7)")
    elif domain == "app":
        if event_type not in SHARED_APP_EVENTS:
            raise ValueError(f"{event_type!r}: van het gedeelde domein 'app' alleen app.health.changed (§7)")
    elif domain not in OWN_DOMAINS:
        raise ValueError(f"{event_type!r}: domein {domain!r} hoort niet bij deze dienst (§7)")


def _bounded_actor_id(actor_id: str) -> str:
    """``actor.id`` is 1–128 tekens (§7); een langer ID wordt een vaste, niet-terug te rekenen verwijzing."""
    if 1 <= len(actor_id) <= MAX_ACTOR_ID:
        return actor_id
    prefix = actor_id.split(":", 1)[0][:20] if ":" in actor_id else "id"
    return f"{prefix}:sha256:{hashlib.sha256(actor_id.encode('utf-8')).hexdigest()[:40]}"


def _bounded_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` ≤ 16 KiB als JSON (§7); groter → alleen de sleutelnamen met ``_ingekort: true``."""
    if len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return payload
    return {"_ingekort": True, "sleutels": sorted(str(k) for k in payload)[:100]}


def emit(
    session: Session,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    actor_type: str,
    actor_id: str,
    trace_id: str,
    payload: dict[str, Any] | None = None,
    classification: str = "internal",
    correlation_id: str | None = None,
) -> OutboxEvent:
    if classification not in CLASSIFICATIONS:
        raise ValueError("ongeldige classificatie")
    check_event_type(event_type)
    if not ENTITY_TYPE.fullmatch(entity_type):
        raise ValueError(f"entity.type {entity_type!r} past niet in ^[a-z0-9_.-]{{1,64}}$")
    if not 1 <= len(str(entity_id)) <= MAX_ID:
        raise ValueError("entity.id moet 1–200 tekens zijn")
    clean = redact(payload or {})
    ernst = clean.get("ernst")
    if ernst is not None and ernst not in ERNST:
        raise ValueError(f"payload.ernst {ernst!r} is geen van {', '.join(ERNST)}")
    ev = OutboxEvent(
        event_id=uuid.uuid4(),
        event_type=event_type,
        entity_type=entity_type,
        entity_id=str(entity_id),
        trace_id=uuid.UUID(trace_id),
        correlation_id=uuid.UUID(correlation_id) if correlation_id else None,
        actor_type=actor_type if actor_type in ACTOR_TYPES else "system",
        actor_id=_bounded_actor_id(actor_id),
        classification=classification,
        payload=_bounded_payload(clean),
        timestamp_utc=utcnow(),
        next_attempt_at=utcnow(),
    )
    session.add(ev)
    return ev


def emit_service(
    session: Session,
    event_type: str,
    *,
    actor_type: str,
    actor_id: str,
    trace_id: str,
    payload: dict[str, Any] | None = None,
) -> OutboxEvent:
    """Een van de vaste ``service.*``-events (§7) met ``entity: {type: "service", id: <app_id>}``."""
    if event_type not in SERVICE_EVENTS:
        raise ValueError(f"{event_type!r} staat niet in de vaste service.*-lijst (§7)")
    return emit(
        session,
        event_type=event_type,
        entity_type="service",
        entity_id=APP_ID,
        actor_type=actor_type,
        actor_id=actor_id,
        trace_id=trace_id,
        payload=payload,
    )


def envelope(ev: OutboxEvent) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(ev.event_id),
        "source_app": APP_ID,
        "source_event_id": str(ev.seq),
        "event_type": ev.event_type,
        "entity": {"type": ev.entity_type, "id": ev.entity_id},
        "timestamp_utc": iso(ev.timestamp_utc),
        "trace_id": str(ev.trace_id),
        "correlation_id": str(ev.correlation_id) if ev.correlation_id else None,
        "actor": {"type": ev.actor_type, "id": ev.actor_id},
        "classification": ev.classification,
        "payload": ev.payload,
        "payload_ref": None,
    }


def validate_envelope(env: dict[str, Any]) -> list[str]:
    """Dezelfde grenzen als de Hub-inbox (§7), vóór verzending. Lege lijst = in orde."""
    problems: list[str] = []
    if set(env) != ENVELOPE_FIELDS:
        problems.append("onbekende of ontbrekende velden")
    if env.get("schema_version") != "1.0":
        problems.append("schema_version moet 1.0 zijn")
    if env.get("source_app") != APP_ID:
        problems.append("source_app wijkt af van de eigen app_id")
    try:
        check_event_type(str(env.get("event_type")))
    except ValueError as exc:
        problems.append(str(exc))
    sid = env.get("source_event_id")
    if not isinstance(sid, str) or not 1 <= len(sid) <= MAX_ID:
        problems.append("source_event_id moet 1–200 tekens zijn")
    entity = env.get("entity")
    if not isinstance(entity, dict) or set(entity) != {"type", "id"}:
        problems.append("entity moet precies {type, id} zijn")
    else:
        if not isinstance(entity.get("type"), str) or not ENTITY_TYPE.fullmatch(entity["type"]):
            problems.append("entity.type ongeldig")
        if not isinstance(entity.get("id"), str) or not 1 <= len(entity["id"]) <= MAX_ID:
            problems.append("entity.id moet 1–200 tekens zijn")
    actor = env.get("actor")
    if not isinstance(actor, dict) or set(actor) != {"type", "id"} or actor.get("type") not in ACTOR_TYPES:
        problems.append("actor moet {type: system|user|ai|flow, id} zijn")
    elif not isinstance(actor.get("id"), str) or not 1 <= len(actor["id"]) <= MAX_ACTOR_ID:
        problems.append("actor.id moet 1–128 tekens zijn")
    ts = env.get("timestamp_utc")
    if not isinstance(ts, str) or not (ts.endswith("Z") or "+" in ts[10:]):
        problems.append("timestamp_utc zonder tijdzone")
    if env.get("classification") not in CLASSIFICATIONS:
        problems.append("classification ongeldig")
    payload = env.get("payload")
    if not isinstance(payload, dict):
        problems.append("payload is geen object")
    elif len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        problems.append("payload groter dan 16 KiB")
    elif payload.get("ernst") is not None and payload.get("ernst") not in ERNST:
        problems.append("payload.ernst ongeldig")
    ref = env.get("payload_ref")
    if ref is not None and (not isinstance(ref, str) or len(ref) > 512):
        problems.append("payload_ref langer dan 512 tekens")
    return problems


def mark_invalid(ev: OutboxEvent, problems: list[str]) -> None:
    """Een event dat de inbox zou weigeren gaat niet over het LAN: direct ``dead_letter`` met de reden."""
    ev.delivery_status = "dead_letter"
    ev.last_error = ("ongeldig_event: " + "; ".join(problems))[:300]


def due_for_push(session: Session, limit: int = 50) -> list[OutboxEvent]:
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.delivery_status == "pending", OutboxEvent.next_attempt_at <= utcnow())
        .order_by(OutboxEvent.seq)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(stmt))


def mark_delivered(ev: OutboxEvent) -> None:
    ev.delivery_status = "delivered"
    ev.delivered_at = utcnow()
    ev.last_error = None


def mark_failed(ev: OutboxEvent, error: str, not_before_s: float | None = None) -> None:
    """Mislukte levering: backoff (×2 vanaf 5 s, met jitter), maar nooit vóór ``Retry-After`` van een 429/503 (§5)."""
    ev.attempts += 1
    ev.last_error = error[:300]
    if ev.attempts >= MAX_ATTEMPTS:
        ev.delivery_status = "dead_letter"
        return
    delay = min(3600.0, 5.0 * (2 ** (ev.attempts - 1))) * random.uniform(0.8, 1.2)  # noqa: S311 (jitter)
    if not_before_s is not None:
        delay = max(delay, float(not_before_s))
    ev.next_attempt_at = utcnow() + timedelta(seconds=delay)
