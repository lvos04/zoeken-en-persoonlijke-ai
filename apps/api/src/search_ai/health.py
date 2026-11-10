"""``/healthz`` (proces leeft), ``/readyz`` (afhankelijkheden ok) en de connector-health (contract 1.3 §5/§6).

``/readyz`` hangt niet van de koppeling met de Hub af (§5): een niet-gekoppelde dienst is ready; de statuspagina toont
het verschil ("Gereed", §3.7)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import APP_ID, __version__
from .enrollment import pins_ok
from .hublink import is_unreadable, public_view
from .models import OutboxEvent, ServiceState
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
    # koppelstatus alleen als witte lijst (contract 1.3 §3.5); 'niet gekoppeld' maakt de dienst niet 'niet klaar'
    link = public_view(request.app.state.hub_link.read_safe())
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "checks": checks, "hub_link": link},
        status_code=200 if ok else 503,
    )


# ------------------------------------------------------------------ connector-health (contract 1.3 §6, §6.2)
SEVERITY = {"healthy": 0, "unknown": 1, "degraded": 2, "down": 3}
DEFAULT_MAX_AGE_S = 900  # §6.2: standaard max_age
WORKER_HEARTBEAT_KEY = "worker_heartbeat"
OUTBOX_STALL = timedelta(hours=1)  # een wachtrij die zo lang niet afneemt → degraded (§6 regel 3)


def worker_max_age_s(settings: Any) -> int:
    """Hoe oud de laatste hartslag van de worker mag zijn (in ``discover`` als ``limits.max_age_s.worker``)."""
    return int(max(120.0, 6 * float(getattr(settings, "worker_interval_s", 10.0))))


def max_age_limits(settings: Any) -> dict[str, int]:
    """``limits.max_age_s`` voor ``discover`` (§6.2): per check of itemtype; de rest volgt de standaard (900 s)."""
    return {
        "health": DEFAULT_MAX_AGE_S,
        "worker": worker_max_age_s(settings),
        "jobs": DEFAULT_MAX_AGE_S,
        "search.source": DEFAULT_MAX_AGE_S,
        "search.index_run": DEFAULT_MAX_AGE_S,
        "search.privacy_summary": DEFAULT_MAX_AGE_S,
    }


def _check(
    name: str, status: str, detail: Any = None, reason: str | None = None, observed_at: str | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "observed_at": observed_at or iso(utcnow()),
        "detail": detail or {},
        "reason_code": reason,
    }


def worst(checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """De slechtste check (§6 regel 1: healthy < unknown < degraded < down); bij gelijkspel de eerste."""
    bad = [c for c in checks if c["status"] != "healthy"]
    return max(bad, key=lambda c: SEVERITY[c["status"]]) if bad else None


def _parse_time(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def worker_check(session: Session, settings: Any) -> dict[str, Any]:
    """``worker``: ``observed_at`` = de laatste hartslag; ouder dan ``max_age`` → ``unknown`` + ``meting_verouderd``
    (§6.2: de oude waarde nooit als actueel doorgeven, wel als laatst bekend)."""
    row = session.get(ServiceState, WORKER_HEARTBEAT_KEY)
    value = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    beat = _parse_time(value.get("laatste_hartslag"))
    max_age = worker_max_age_s(settings)
    if beat is None:
        return _check("worker", "unknown", {"max_age_s": max_age}, "geen_hartslag")
    age = (utcnow() - beat).total_seconds()
    detail = {"laatste_hartslag": iso(beat), "max_age_s": max_age, "leeftijd_s": int(max(0.0, age))}
    if age > max_age:
        return _check("worker", "unknown", detail, "meting_verouderd", observed_at=iso(beat))
    failed = int(value.get("stappen_mislukt") or 0)
    if failed:
        return _check("worker", "degraded", {**detail, "stappen_mislukt": failed}, "stap_mislukt", iso(beat))
    return _check("worker", "healthy", detail, None, observed_at=iso(beat))


def connector_health(request: Request, session: Session) -> dict[str, Any]:
    """``GET /connector/v1/health`` (§6, 1.3): ``status`` is de **slechtste** van alle checks; vaste namen
    ``database``, ``worker``, ``jobs``, ``hub_link`` (+ eigen namen); elke check met ``observed_at``.

    Functies die in deze versie niet gebouwd zijn (ingest, semantisch zoeken, spraak) zijn geen gezondheidscheck: ze
    staan als ``capability_unavailable`` in ``discover`` (een fase, zoals "Nog niet beschikbaar" op de statuspagina)."""
    state = request.app.state
    now = iso(utcnow())
    checks: list[dict[str, Any]] = []
    db_ok = state.db.ping()
    checks.append(_check("database", "healthy" if db_ok else "down", None, None if db_ok else "database_onbereikbaar"))
    if not db_ok:
        return {"status": "down", "observed_at": now, "reason_code": "database_onbereikbaar", "checks": checks}

    current = state.db.current_revision()
    at_head = current == state.alembic_head
    checks.append(
        _check(
            "migraties",
            "healthy" if at_head else "down",
            {"huidig": current, "verwacht": state.alembic_head},
            None if at_head else "schema_niet_actueel",
        )
    )
    vec = state.db.vector_extension()
    checks.append(_check("pgvector", "healthy" if vec else "down", {"versie": vec}, None if vec else "ontbreekt"))

    link = state.hub_link.read_safe()
    lstate = str(link.get("state"))
    coupled = lstate == "gekoppeld" and not is_unreadable(link)
    err_code = (link.get("last_error") or {}).get("code")
    if coupled and link.get("confirmed_at"):
        hub_status, hub_reason = "healthy", None
    elif coupled:
        hub_status, hub_reason = "degraded", "niet_bevestigd"
    else:
        hub_status, hub_reason = "unknown", "koppelstatus_onleesbaar" if is_unreadable(link) else lstate
    checks.append(_check("hub_link", hub_status, {"state": lstate}, hub_reason))
    # gepinde Hub-sleutels (§4.5): er wordt nooit een JWKS opgehaald; zonder koppeling is dit 'unknown', nooit groen
    if not coupled:
        keys_status, keys_reason = "unknown", "niet_gekoppeld"
    elif not pins_ok(link):
        keys_status, keys_reason = "down", "pins_ongeldig"
    elif err_code == "sleutelrotatie_ongeldig":
        keys_status, keys_reason = "degraded", "sleutelrotatie_ongeldig"
    else:
        keys_status, keys_reason = "healthy", None
    pins = link.get("pins") or {}
    checks.append(
        _check(
            "hub_sleutels",
            keys_status,
            {
                "jwt": len(pins.get("jwt") or []) if coupled else 0,
                "policy": len(pins.get("policy") or []) if coupled else 0,
                "rotation_sequence": int(pins.get("rotation_sequence") or 0) if coupled else 0,
            },
            keys_reason,
        )
    )

    worker = worker_check(session, state.settings)
    checks.append(worker)
    dead = session.scalar(select(func.count()).where(OutboxEvent.delivery_status == "dead_letter")) or 0
    pending = session.scalar(select(func.count()).where(OutboxEvent.delivery_status == "pending")) or 0
    stalled = (
        session.scalar(
            select(func.count()).where(
                OutboxEvent.delivery_status == "pending", OutboxEvent.timestamp_utc < utcnow() - OUTBOX_STALL
            )
        )
        or 0
    )
    # Niet gekoppeld: events wachten terecht in de outbox (§7, RV12); dat is geen vastgelopen wachtrij.
    jobs_status = "degraded" if dead or (coupled and stalled) else "healthy"
    checks.append(
        _check(
            "jobs",
            jobs_status,
            {
                "wachtrij": int(pending),
                "bezig": 0,  # de worker werkt de outbox per ronde af; tussen twee rondes loopt er niets
                "laatste_job_klaar": (worker["detail"] or {}).get("laatste_hartslag"),
                "dead_letter": int(dead),
                "vast_ouder_dan_1u": int(stalled) if coupled else 0,
            },
            "dead_letter" if dead else ("wachtrij_neemt_niet_af" if jobs_status != "healthy" else None),
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
    bad = worst(checks)
    return {
        "status": bad["status"] if bad else "healthy",
        "observed_at": now,
        "reason_code": (bad["reason_code"] or bad["name"]) if bad else None,
        "checks": checks,
    }
