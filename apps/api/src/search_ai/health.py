"""``/healthz`` (proces leeft), ``/readyz`` (afhankelijkheden ok) en de connector-health (contract §5/§6)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import APP_ID, __version__
from .models import OutboxEvent
from .search.models import IndexSource
from .search.paths import validate_read_roots
from .security.secrets import secrets_present
from .timeutil import iso, utcnow

router = APIRouter(tags=["gezondheid"])


@router.get("/healthz", summary="Proces leeft (zonder auth, zonder geheimen)")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "app_id": APP_ID, "version": __version__}


def readiness(request: Request) -> tuple[bool, list[dict[str, Any]]]:
    state = request.app.state
    checks: list[dict[str, Any]] = []
    db_ok = state.db.ping()
    checks.append({"name": "database", "status": "ok" if db_ok else "fout"})
    if db_ok:
        current = state.db.current_revision()
        at_head = current is not None and current == state.alembic_head
        checks.append(
            {
                "name": "migraties",
                "status": "ok" if at_head else "fout",
                "detail": {"huidig": current, "verwacht": state.alembic_head},
            }
        )
        vec = state.db.vector_extension()
        checks.append({"name": "pgvector", "status": "ok" if vec else "fout", "detail": {"versie": vec}})
    present = secrets_present(state.settings.secrets_dir)
    checks.append({"name": "geheimen", "status": "ok" if all(present.values()) else "fout", "detail": present})
    return all(c["status"] == "ok" for c in checks), checks


@router.get("/readyz", summary="Klaar voor verkeer: database, migraties, pgvector, geheimen (zonder auth)")
def readyz(request: Request) -> JSONResponse:
    ok, checks = readiness(request)
    return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)


def _check(name: str, status: str, detail: Any = None, reason: str | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "observed_at": iso(utcnow()), "detail": detail or {}, "reason_code": reason}


def connector_health(request: Request, session: Session) -> dict[str, Any]:
    state = request.app.state
    checks: list[dict[str, Any]] = []
    db_ok = state.db.ping()
    checks.append(_check("database", "healthy" if db_ok else "down"))
    if not db_ok:
        return {
            "status": "down",
            "observed_at": iso(utcnow()),
            "reason_code": "database_onbereikbaar",
            "checks": checks,
        }

    current = state.db.current_revision()
    checks.append(
        _check(
            "migraties",
            "healthy" if current == state.alembic_head else "degraded",
            {"huidig": current, "verwacht": state.alembic_head},
        )
    )
    vec = state.db.vector_extension()
    checks.append(_check("pgvector", "healthy" if vec else "down", {"versie": vec}, None if vec else "ontbreekt"))
    link = state.enrollment.state(session)
    coupled = link.get("status") == "gekoppeld"
    checks.append(
        _check(
            "hub_koppeling",
            "healthy" if coupled else "degraded",
            {"status": link.get("status")},
            None if coupled else link.get("reason"),
        )
    )
    js = state.jwks.status()
    if not coupled:
        jwks_status, jwks_reason = "unknown", "niet_gekoppeld"
    elif js.available and not js.stale:
        jwks_status, jwks_reason = "healthy", None
    elif js.available:
        jwks_status, jwks_reason = "degraded", "verouderd"
    elif js.age_s is None and js.last_error is None:
        jwks_status, jwks_reason = "unknown", "nog_niet_opgehaald"
    else:
        jwks_status, jwks_reason = "down", js.last_error or "niet_beschikbaar"
    checks.append(_check("hub_jwks", jwks_status, {"keys": js.keys, "age_s": js.age_s}, jwks_reason))

    dead = session.scalar(select(func.count()).where(OutboxEvent.delivery_status == "dead_letter")) or 0
    stale_pending = (
        session.scalar(
            select(func.count()).where(
                OutboxEvent.delivery_status == "pending", OutboxEvent.timestamp_utc < utcnow() - timedelta(hours=1)
            )
        )
        or 0
    )
    outbox_status = "degraded" if dead or (coupled and stale_pending) else "healthy"
    checks.append(
        _check(
            "outbox",
            outbox_status,
            {"dead_letter": dead, "pending_ouder_dan_1u": stale_pending},
            "dead_letter" if dead else ("achterstand" if outbox_status != "healthy" else None),
        )
    )
    roots, warnings = validate_read_roots(state.settings.read_roots, state.settings.data_dir)
    checks.append(
        _check(
            "leeswortels",
            "healthy" if roots and not warnings else ("degraded" if roots else "unknown"),
            {"geldig": len(roots), "waarschuwingen": len(warnings)},
            None if roots and not warnings else ("geen_leeswortels" if not roots else "ongeldige_leeswortel"),
        )
    )
    failed_purges = (
        session.scalar(select(func.count()).where(IndexSource.status == "revoked", IndexSource.purge_status != "done"))
        or 0
    )
    checks.append(
        _check(
            "intrekking_opgeschoond",
            "healthy" if not failed_purges else "degraded",
            {"open": failed_purges},
            "purge_open" if failed_purges else None,
        )
    )
    # Ingest/embeddings/spraak zijn in P0 niet gebouwd: eerlijk 'unknown', nooit 'healthy'.
    checks.append(_check("ingest", "unknown", {"capability": "search.ingest"}, "capability_unavailable"))
    checks.append(_check("spraak", "unknown", {"capability": "voice"}, "capability_unavailable"))

    service_checks = [
        c
        for c in checks
        if c["name"] in ("migraties", "pgvector", "hub_koppeling", "hub_jwks", "outbox", "intrekking_opgeschoond")
    ]
    bad = [c for c in service_checks if c["status"] not in ("healthy", "unknown")]
    status = "down" if any(c["status"] == "down" for c in service_checks if c["name"] == "pgvector") else None
    if status is None:
        status = "degraded" if bad or not coupled else "healthy"
    # Onbekend is nooit groen: een dienstcheck die 'unknown' is (bv. Hub-sleutels nog nooit opgehaald) maakt het
    # geheel 'unknown', niet 'healthy'. Niet-gebouwde functies (ingest, spraak) tellen hier niet mee: die staan
    # eerlijk als capability_unavailable in hun eigen check.
    if status == "healthy" and any(c["status"] == "unknown" for c in service_checks):
        status = "unknown"
    reason = next((c["reason_code"] or c["name"] for c in service_checks if c["status"] != "healthy"), None)
    return {
        "status": status,
        "observed_at": iso(utcnow()),
        "reason_code": reason if status != "healthy" else None,
        "checks": checks,
    }
