"""Connectorcontract ``/connector/v1/*`` (contract 1.3 §6).

Deze dienst levert in P0 ``read`` (bronnen, runs, privacydekking — **nooit documentinhoud** via de connector) en
``stream`` als polling-outbox. ``propose``/``apply``/``rollback`` geven eerlijk ``501 capability_unavailable``: deze dienst
voert geen acties uit voor de Hub (nooit gesimuleerd succes).

Koppelbeheer (alleen eigen diensten, §3.8/§6): ``GET /connector/v1/hub-link`` (witte lijst van §3.5 + gepinde ``kid``'s
en ``rotation_sequence``; dienst-JWT of gebruiker viewer+) en ``POST /connector/v1/hub-link/unlink`` (dienst-JWT of
gebruiker admin, ``Idempotency-Key``). Daarna weigert de dienst elke Hub-JWT met 401 ``not_linked``; een herhaalde
``unlink`` krijgt dus ook 401 ``not_linked`` — voor de Hub telt dat als geslaagd.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import APP_ID, CONNECTOR_PREFIX, SCHEMA_VERSION, __version__, audit, events
from .deps import get_session, trace_id
from .enrollment import pinned_kids
from .errors import ApiError, capability_unavailable, forbidden, not_found
from .events import envelope
from .health import connector_health, max_age_limits
from .hublink import public_view
from .idempotency import idempotency_key, run_idempotent
from .models import AuditEvent, OutboxEvent
from .pagination import MAX_LIMIT, decode_int_cursor, encode_cursor, offset_page
from .redact import redact
from .search import service as search_service
from .search.models import IndexRun, IndexSource
from .security.auth import Principal, require_admin_or_service, require_connector_reader
from .timeutil import iso, utcnow

router = APIRouter(prefix=CONNECTOR_PREFIX, tags=["connector"])

ITEM_TYPES: dict[str, dict[str, Any]] = {
    # Bronnamen kunnen persoonlijk zijn: alleen beheer/operator/Hub; een viewer ziet zijn bronnen via /search/v1.
    "search.source": {"roles": {"admin", "operator", "service"}, "title_nl": "Zoekbronnen (zonder paden)"},
    "search.index_run": {"roles": {"admin", "operator", "service"}, "title_nl": "Index- en purge-runs"},
    "search.privacy_summary": {
        "roles": {"admin", "operator", "service"},
        "title_nl": "Privacysamenvatting (aantallen per status)",
    },
}
UNBUILT_ITEM_TYPES = {
    "voice.session": "voice.sessions.read",
}
# Documenten/fragmenten zijn per gebruiker afgeschermd (ACL) en gaan nooit via de connector.
FORBIDDEN_ITEM_TYPES = {"search.document", "search.chunk", "search.embedding"}


@router.get("/discover", summary="Wat deze dienst kan (contract §6)")
def discover(request: Request, _p: Principal = Depends(require_connector_reader)) -> dict[str, Any]:
    return {
        "app_id": APP_ID,
        "source_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "capabilities": ["read", "stream"],
        "unavailable_capabilities": ["propose", "apply", "rollback"],
        "stream_mode": "polling via /connector/v1/events en push naar de Hub-inbox",
        "permissions": [
            "read:hub_link",
            "write:hub_link.unlink",
            "read:search.source",
            "read:search.index_run",
            "read:search.privacy_summary",
            "read:events",
            "read:evidence",
        ],
        "item_types": {k: v["title_nl"] for k, v in ITEM_TYPES.items()},
        "unbuilt_item_types": UNBUILT_ITEM_TYPES,
        "functions": {
            "search.fulltext": "beschikbaar (ACL vóór ranking; index leeg zolang ingest ontbreekt)",
            "search.ingest": "capability_unavailable",
            "search.semantic": "capability_unavailable",
            "search.qa_citations": "capability_unavailable",
            "voice": "capability_unavailable",
            "ai.external": "capability_unavailable (indexdata verlaat de NAS niet)",
        },
        "limits": {
            "max_page_size": MAX_LIMIT,
            "outbound_timeout_s": {"connect": 5, "read": 15},
            "max_retries": 3,
            # §6.2 (1.3): hoe oud een meting mag zijn; daarna 'unknown' met reason_code meting_verouderd
            "max_age_s": max_age_limits(request.app.state.settings),
        },
        "timestamp_utc": iso(utcnow()),
    }


@router.get("/health", summary="Gezondheid volgens het connectorcontract")
def health(
    request: Request, session: Session = Depends(get_session), _p: Principal = Depends(require_connector_reader)
) -> dict[str, Any]:
    return connector_health(request, session)


def _source_summary(session: Session, src: IndexSource) -> dict[str, Any]:
    return {
        "id": str(src.id),
        "kind": src.kind,
        "name": src.name,
        "status": src.status,
        "classification": src.classification,
        "kid_allowed": src.kid_allowed,
        "scopes": search_service.active_scopes(session, src.id),
        "purge_status": src.purge_status,
        "updated_at": iso(src.updated_at),
    }


@router.get("/items", summary="Eigen data, gepagineerd, met rolcontrole per type")
def items(
    type: str = Query(..., description="Itemtype, zie /discover"),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_connector_reader),
) -> dict[str, Any]:
    if type in FORBIDDEN_ITEM_TYPES:
        raise forbidden(
            "inhoud_niet_via_connector",
            "Documentinhoud is per gebruiker afgeschermd en gaat nooit via de connector; zoek via /search/v1/search.",
        )
    if type in UNBUILT_ITEM_TYPES:
        raise capability_unavailable(
            UNBUILT_ITEM_TYPES[type], "Dit itemtype is nog niet gebouwd; er wordt geen data verzonnen."
        )
    spec = ITEM_TYPES.get(type)
    if spec is None:
        raise ApiError(
            422,
            "validation_error",
            "Onbekend itemtype; zie /connector/v1/discover.",
            {"fouten": [{"veld": "type", "probleem": "onbekend itemtype", "type": "invalid_type"}]},
        )
    if not principal.has_any(spec["roles"]) or principal.is_kid:
        raise forbidden("rol_ontbreekt", "Geen toestemming voor dit itemtype.")
    data: list[Any]
    if type == "search.source":
        data = [
            _source_summary(session, s) for s in session.scalars(select(IndexSource).order_by(IndexSource.created_at))
        ]
    elif type == "search.index_run":
        data = [
            search_service.run_dict(r)
            for r in session.scalars(select(IndexRun).order_by(IndexRun.created_at.desc()).limit(1000))
        ]
    else:
        rows = session.execute(select(IndexSource.status, func.count()).group_by(IndexSource.status)).all()
        data = [
            {
                "per_status": {str(s): int(n) for s, n in rows},
                "verlaat_nas": False,
                "externe_ai": "capability_unavailable",
                "observed_at": iso(utcnow()),
            }
        ]
    now = iso(utcnow())
    return {"type": type, **offset_page(data, cursor, limit), "observed_at": now, "timestamp_utc": now}


@router.post(
    "/propose", summary="Voorstel (niet beschikbaar)", responses={501: {"description": "capability_unavailable"}}
)
def propose(_p: Principal = Depends(require_admin_or_service)) -> None:
    raise capability_unavailable("propose")


@router.post(
    "/apply", summary="Uitvoeren (niet beschikbaar)", responses={501: {"description": "capability_unavailable"}}
)
def apply(_p: Principal = Depends(require_admin_or_service)) -> None:
    raise capability_unavailable("apply", "Uitvoeren is in deze dienst niet beschikbaar; er wordt niets uitgevoerd.")


@router.post(
    "/rollback", summary="Terugdraaien (niet beschikbaar)", responses={501: {"description": "capability_unavailable"}}
)
def rollback(_p: Principal = Depends(require_admin_or_service)) -> None:
    raise capability_unavailable("rollback")


@router.get("/evidence/{evidence_id}", summary="Geredigeerd bewijs (audit-event of index-/purge-run)")
def evidence(
    evidence_id: str, session: Session = Depends(get_session), _p: Principal = Depends(require_admin_or_service)
) -> dict[str, Any]:
    try:
        eid = uuid.UUID(evidence_id)
    except ValueError as exc:
        raise not_found("Bewijs") from exc
    ev = session.get(AuditEvent, eid)
    if ev is not None:
        record = {
            "at": iso(ev.at),
            "domain": ev.domain,
            "action": ev.action,
            "entity_type": ev.entity_type,
            "entity_id": ev.entity_id,
            "actor": {"type": ev.actor_type, "id": ev.actor_id},
            "trace_id": str(ev.trace_id),
            "message_nl": ev.message_nl,
            "details": ev.details,
        }
        return {"id": evidence_id, "kind": "audit_event", "record": redact(record)}
    run = session.get(IndexRun, eid)
    if run is not None:
        return {"id": evidence_id, "kind": "index_run", "record": redact(search_service.run_dict(run))}
    raise not_found("Bewijs")


@router.get("/events", summary="Outbox pollen (contract §7)")
def poll_events(
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
    _p: Principal = Depends(require_admin_or_service),
) -> dict[str, Any]:
    after = decode_int_cursor(cursor)
    rows = list(
        session.scalars(select(OutboxEvent).where(OutboxEvent.seq > after).order_by(OutboxEvent.seq).limit(limit))
    )
    dead = session.scalar(select(func.count()).where(OutboxEvent.delivery_status == "dead_letter")) or 0
    return {
        "items": [envelope(r) for r in rows],
        "next_cursor": encode_cursor(rows[-1].seq) if rows else cursor,
        "dead_letter_count": dead,
    }


@router.get("/hub-link", summary="Koppelstatus met de Hub (witte lijst, gepinde sleutels; contract 1.2 §3.5/§6)")
def hub_link(request: Request, _p: Principal = Depends(require_connector_reader)) -> dict[str, Any]:
    doc = request.app.state.hub_link.read_safe()
    pins = doc.get("pins") if doc.get("state") == "gekoppeld" else None
    return {
        **public_view(doc),
        "pinned_kids": pinned_kids(doc),
        "rotation_sequence": int((pins or {}).get("rotation_sequence") or 0),
        "timestamp_utc": iso(utcnow()),
    }


@router.post(
    "/hub-link/unlink",
    summary="Ontkoppelen van de Hub (dienst-JWT of admin, Idempotency-Key; contract 1.2 §3.8)",
    responses={401: {"description": "niet (meer) gekoppeld: reason not_linked (telt voor de Hub als geslaagd)"}},
)
def hub_link_unlink(
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin_or_service),
    key: uuid.UUID = Depends(idempotency_key),
    tid: str = Depends(trace_id),
) -> JSONResponse:
    def action() -> tuple[int, Any, dict[str, str]]:
        state = request.app.state.enrollment.unlink()
        audit.record(
            session,
            domain="platform",
            action="hub.unlink",
            entity_type="hub_link",
            entity_id="hub",
            principal=principal,
            trace_id=tid,
            message_nl="Ontkoppeld van de Hub; Hub-JWT's worden vanaf nu geweigerd.",
            details={"door": principal.audit_id},
        )
        events.emit_service(
            session,
            "service.hub_link.unlinked",
            actor_type=principal.actor_type,
            actor_id=principal.audit_id,
            trace_id=tid,
            payload={"state": state["state"]},
        )
        return 200, state, {}

    return run_idempotent(request, session, principal, key, {"unlink": True}, action)
