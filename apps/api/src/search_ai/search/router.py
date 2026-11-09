"""Zoek-API ``/search/v1`` (contract §5). De Hub-UI toont de schermen; deze dienst levert alleen de API.

B07 noemt Hub-routes ``/api/v1/search…``; die zet de Hub om naar deze dienstroutes (ADR-0001).
Alle mutaties vereisen ``Idempotency-Key``; wijzigen van een bestaande bron vereist ``If-Match`` (ETag).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import SEARCH_PREFIX
from ..deps import check_if_match, etag, get_session, if_match, trace_id
from ..errors import capability_unavailable, conflict, forbidden, not_found
from ..idempotency import idempotency_key, run_idempotent
from ..pagination import MAX_LIMIT, offset_page
from ..security.auth import Principal, current_principal, require_admin, require_user_context
from ..timeutil import iso, utcnow
from . import acl, fulltext, service
from .models import AccessScope, IndexRun, IndexSource

router = APIRouter(prefix=SEARCH_PREFIX, tags=["zoeken (B07)"])

SourceKind = Literal["files", "immich", "jellyfin", "logs"]
Classification = Literal["internal", "sensitive"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceCreate(Strict):
    kind: SourceKind
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    owner_sub: str | None = Field(default=None, min_length=1, max_length=200, description="Gegevenseigenaar (Hub-sub)")
    allowed_paths: list[str] = Field(default_factory=list, max_length=50)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=50)
    base_url: str | None = Field(default=None, max_length=500)
    classification: Classification = "sensitive"
    kid_allowed: bool = False


class SourcePatch(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    allowed_paths: list[str] | None = Field(default=None, max_length=50)
    exclude_patterns: list[str] | None = Field(default=None, max_length=50)
    base_url: str | None = Field(default=None, max_length=500)
    classification: Classification | None = None
    kid_allowed: bool | None = None


class ConsentIn(Strict):
    scopes: list[str] = Field(min_length=1, max_length=10)
    consent_text_version: str = Field(max_length=40)
    bevestiging: Literal[True] = Field(description="Moet true zijn: de eigenaar heeft de toestemmingstekst gelezen")
    expires_at: datetime | None = None


class RevokeIn(Strict):
    reden: str = Field(default="", max_length=200)


class AccessIn(Strict):
    principal_type: Literal["user", "role"]
    principal_id: str = Field(min_length=1, max_length=200)


class ReindexIn(Strict):
    source_id: uuid.UUID


def _load(session: Session, source_id: uuid.UUID, *, lock: bool = False) -> IndexSource:
    stmt = select(IndexSource).where(IndexSource.id == source_id)
    if lock:
        stmt = stmt.with_for_update()
    src = session.scalars(stmt).first()
    if src is None:
        raise not_found("Bron")
    return src


def _visible_source(session: Session, principal: Principal, source_id: uuid.UUID) -> IndexSource:
    """Beheerders zien alles; de eigenaar zijn eigen bron; anderen alleen bronnen die ze mogen doorzoeken."""
    src = _load(session, source_id)
    if principal.is_admin or (principal.subject == src.owner_sub and not principal.is_kid):
        return src
    readable = session.scalar(acl.readable_sources(principal).where(IndexSource.id == source_id))
    if readable is None:
        raise not_found("Bron")  # geen bestaan lekken
    return src


def _view(session: Session, src: IndexSource, principal: Principal, request: Request) -> dict[str, Any]:
    return service.source_view(session, src, principal, request.app.state.settings)


# ------------------------------------------------------------------ toestemmingstekst en bronnen
@router.get("/consent-text", summary="Actuele toestemmingstekst en scopes per bronsoort")
def consent_text(_p: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {
        "version": service.CONSENT_TEXT_VERSION,
        "text_nl": service.CONSENT_TEXT_NL,
        "scopes": service.SCOPE_TITLES_NL,
        "scopes_per_kind": {k: list(v) for k, v in service.SCOPES_PER_KIND.items()},
        "niet_beschikbaar": {"ai.external": "Externe AI-verwerking: indexdata verlaat de NAS niet."},
    }


@router.get("/sources", summary="Bronnen (beheerder: alle; anderen: eigen en doorzoekbare)")
def list_sources(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_user_context),
) -> dict[str, Any]:
    stmt = select(IndexSource).order_by(IndexSource.created_at, IndexSource.id)
    if not principal.is_admin:
        readable = IndexSource.id.in_(acl.readable_sources(principal))
        stmt = stmt.where(readable if principal.is_kid else or_(readable, IndexSource.owner_sub == principal.subject))
    items = [_view(session, s, principal, request) for s in session.scalars(stmt)]
    return {**offset_page(items, cursor, limit), "observed_at": iso(utcnow())}


@router.post("/sources", status_code=201, summary="Bron selecteren (concept; zonder toestemming wordt niets gelezen)")
def create_source(
    body: SourceCreate,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
    key: uuid.UUID = Depends(idempotency_key),
    tid: str = Depends(trace_id),
) -> JSONResponse:
    def action() -> tuple[int, Any, dict[str, str]]:
        src = service.create_source(session, request.app.state.settings, principal, body.model_dump(), tid)
        session.flush()
        return 201, _view(session, src, principal, request), {"ETag": etag(src.version)}

    return run_idempotent(request, session, principal, key, body.model_dump(), action)


@router.get("/sources/{source_id}", summary="Eén bron (met ETag)")
def get_source(
    source_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_user_context),
) -> JSONResponse:
    src = _visible_source(session, principal, source_id)
    return JSONResponse(_view(session, src, principal, request), headers={"ETag": etag(src.version)})


@router.patch("/sources/{source_id}", summary="Bron wijzigen (If-Match verplicht; paden alleen in concept/gepauzeerd)")
def patch_source(
    source_id: uuid.UUID,
    body: SourcePatch,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
    key: uuid.UUID = Depends(idempotency_key),
    given: str | None = Depends(if_match),
    tid: str = Depends(trace_id),
) -> JSONResponse:
    payload = body.model_dump(exclude_unset=True)

    def action() -> tuple[int, Any, dict[str, str]]:
        src = _load(session, source_id, lock=True)
        check_if_match(src.version, given)
        result = service.update_source(session, request.app.state.settings, principal, src, payload, tid)
        session.flush()
        return 200, {"bron": _view(session, src, principal, request), **result}, {"ETag": etag(src.version)}

    return run_idempotent(request, session, principal, key, {"id": str(source_id), **payload}, action)


@router.get(
    "/sources/{source_id}/preview", summary="Bronwizard: wat zou er gelezen worden? (alleen namen, geen inhoud)"
)
def preview_source(
    source_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_user_context),
) -> dict[str, Any]:
    src = _load(session, source_id)
    is_owner = principal.subject == src.owner_sub and not principal.is_kid
    if not (principal.is_admin or is_owner):
        raise not_found("Bron")
    if src.kind not in service.PATH_KINDS:
        raise capability_unavailable(
            f"preview.{src.kind}", "Voorvertoning voor API-bronnen (Immich/Jellyfin) volgt met de adapters (B07-002)."
        )
    if src.status == "revoked":
        raise conflict("bron_ingetrokken", "Deze bron is ingetrokken.")
    settings = request.app.state.settings
    from .paths import preview

    result = preview(service.guard_for(settings, src), settings.preview_max_files)
    return {
        "source_id": str(src.id),
        "status": src.status,
        "paden": result.paths,
        "bestanden": result.files,
        "bytes": result.bytes,
        "overgeslagen": {
            "uitgesloten": result.skipped_excluded,
            "symlink": result.skipped_symlink,
            "speciaal": result.skipped_special,
            "onleesbaar": result.unreadable,
        },
        "afgekapt": result.truncated,
        # Bestandsnamen alleen voor de eigenaar zelf; een beheerder ziet tellingen (beheren ≠ meelezen).
        "voorbeeld": result.sample if is_owner else [],
        "voorbeeld_verborgen": not is_owner,
        "vaste_uitsluitingen": [*service.guard_for(settings, src).all_excludes[:12], "…"],
        "inhoud_gelezen": False,
        "observed_at": iso(utcnow()),
    }


# ------------------------------------------------------------------ toestemming, pauzeren, intrekken
@router.post("/sources/{source_id}/consent", summary="Toestemming geven (alleen de gegevenseigenaar zelf)")
def give_consent(
    source_id: uuid.UUID,
    body: ConsentIn,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_user_context),
    key: uuid.UUID = Depends(idempotency_key),
    tid: str = Depends(trace_id),
) -> JSONResponse:
    payload = body.model_dump(mode="json")

    def action() -> tuple[int, Any, dict[str, str]]:
        src = _load(session, source_id, lock=True)
        if principal.subject != src.owner_sub and not principal.is_admin:
            raise not_found("Bron")
        service.grant_consent(
            session,
            request.app.state.settings,
            principal,
            src,
            body.scopes,
            body.consent_text_version,
            body.expires_at,
            tid,
        )
        session.flush()
        return 200, _view(session, src, principal, request), {"ETag": etag(src.version)}

    return run_idempotent(request, session, principal, key, {"id": str(source_id), **payload}, action)


def _simple_transition(name: str) -> Any:
    def endpoint(
        source_id: uuid.UUID,
        request: Request,
        session: Session = Depends(get_session),
        principal: Principal = Depends(require_admin),
        key: uuid.UUID = Depends(idempotency_key),
        tid: str = Depends(trace_id),
    ) -> JSONResponse:
        def action() -> tuple[int, Any, dict[str, str]]:
            src = _load(session, source_id, lock=True)
            getattr(service, name)(session, principal, src, tid)
            session.flush()
            return 200, _view(session, src, principal, request), {"ETag": etag(src.version)}

        return run_idempotent(request, session, principal, key, {"id": str(source_id), "op": name}, action)

    return endpoint


router.add_api_route(
    "/sources/{source_id}/pause",
    _simple_transition("pause"),
    methods=["POST"],
    summary="Bron pauzeren (niet doorzoekbaar, niet indexeren)",
)
router.add_api_route(
    "/sources/{source_id}/resume",
    _simple_transition("resume"),
    methods=["POST"],
    summary="Bron hervatten (alleen met toestemming voor exact deze selectie)",
)


@router.post("/sources/{source_id}/revoke", summary="Toestemming intrekken: alle indexdata van de bron verwijderen")
def revoke_source(
    source_id: uuid.UUID,
    request: Request,
    body: RevokeIn | None = None,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_user_context),
    key: uuid.UUID = Depends(idempotency_key),
    tid: str = Depends(trace_id),
) -> JSONResponse:
    reden = body.reden if body else ""

    def action() -> tuple[int, Any, dict[str, str]]:
        src = _load(session, source_id, lock=True)
        if not (principal.is_admin or (principal.subject == src.owner_sub and not principal.is_kid)):
            raise not_found("Bron")
        run = service.revoke(session, request.app.state.settings, principal, src, reden, tid)
        session.flush()
        return (
            200,
            {"bron": _view(session, src, principal, request), "opschonen": service.run_dict(run)},
            {"ETag": etag(src.version)},
        )

    return run_idempotent(request, session, principal, key, {"id": str(source_id), "reden": reden}, action)


@router.delete("/sources/{source_id}/index", summary="Indexdata van een bron verwijderen (toestemming blijft)")
def purge_index(
    source_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
    key: uuid.UUID = Depends(idempotency_key),
    tid: str = Depends(trace_id),
) -> JSONResponse:
    def action() -> tuple[int, Any, dict[str, str]]:
        src = _load(session, source_id, lock=True)
        run = service.purge_source(session, principal, src, tid, reason="handmatig_opschonen")
        from .. import audit

        audit.record(
            session,
            domain="search",
            action="index.purged",
            entity_type="index_source",
            entity_id=str(src.id),
            principal=principal,
            trace_id=tid,
            message_nl=f"Indexdata van bron '{src.name}' verwijderd (handmatig).",
            details={"index_run": str(run.id), "status": run.status},
        )
        session.flush()
        return 200, service.run_dict(run), {}

    return run_idempotent(request, session, principal, key, {"id": str(source_id), "op": "purge"}, action)


# ------------------------------------------------------------------ ACL
@router.get("/sources/{source_id}/access", summary="Toegangslijst (ACL) van een bron")
def list_access(
    source_id: uuid.UUID, session: Session = Depends(get_session), principal: Principal = Depends(require_admin)
) -> dict[str, Any]:
    src = _load(session, source_id)
    return {"items": [service.access_dict(a) for a in service.acl_entries(session, src.id)]}


@router.post("/sources/{source_id}/access", status_code=201, summary="Leesrecht geven aan gebruiker of rol")
def add_access(
    source_id: uuid.UUID,
    body: AccessIn,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
    key: uuid.UUID = Depends(idempotency_key),
    tid: str = Depends(trace_id),
) -> JSONResponse:
    def action() -> tuple[int, Any, dict[str, str]]:
        src = _load(session, source_id, lock=True)
        entry = service.add_access(session, principal, src, body.principal_type, body.principal_id, tid)
        session.flush()
        return 201, service.access_dict(entry), {}

    return run_idempotent(request, session, principal, key, {"id": str(source_id), **body.model_dump()}, action)


@router.delete("/sources/{source_id}/access/{access_id}", summary="Leesrecht intrekken")
def remove_access(
    source_id: uuid.UUID,
    access_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
    key: uuid.UUID = Depends(idempotency_key),
    tid: str = Depends(trace_id),
) -> JSONResponse:
    def action() -> tuple[int, Any, dict[str, str]]:
        src = _load(session, source_id, lock=True)
        entry = session.get(AccessScope, access_id)
        if entry is None or entry.source_id != src.id or entry.revoked_at is not None:
            raise not_found("Toegangsregel")
        service.remove_access(session, principal, src, entry, tid)
        session.flush()
        return 200, {"id": str(access_id), "ingetrokken": True}, {}

    return run_idempotent(request, session, principal, key, {"id": str(source_id), "access": str(access_id)}, action)


# ------------------------------------------------------------------ runs, herindexeren, zoeken, privacy
@router.get("/sources/{source_id}/runs", summary="Index-/purge-runs van een bron")
def list_runs(
    source_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_user_context),
) -> dict[str, Any]:
    src = _load(session, source_id)
    if not (principal.is_admin or (principal.subject == src.owner_sub and not principal.is_kid)):
        raise not_found("Bron")
    runs = session.scalars(select(IndexRun).where(IndexRun.source_id == src.id).order_by(IndexRun.created_at.desc()))
    return offset_page([service.run_dict(r) for r in runs], cursor, limit)


@router.post(
    "/reindex",
    summary="Herindexeren (ingest volgt in B07-002; geeft nu eerlijk 501)",
    responses={409: {"description": "geen toestemming"}, 501: {"description": "capability_unavailable"}},
)
def reindex(
    body: ReindexIn,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
    _key: uuid.UUID = Depends(idempotency_key),
) -> None:
    src = _load(session, body.source_id)
    if src.status != "active" or service.active_consent(session, src.id) is None:
        raise conflict(
            "geen_toestemming",
            "Deze bron heeft geen actieve toestemming; er wordt niets gelezen of geïndexeerd.",
            {"status": src.status},
        )
    raise capability_unavailable(
        "search.ingest", "Indexeren (B07-002) is in deze versie nog niet gebouwd; er wordt niets gelezen of gemeld."
    )


@router.get(
    "/search",
    summary="Zoeken (full-text; ACL vóór ranking). Semantisch: 501 zonder lokaal model",
    responses={
        429: {"description": "rate_limited: te veel zoekopdrachten per gebruiker (met Retry-After)"},
        501: {"description": "capability_unavailable: semantisch/hybride zoeken zonder lokaal model"},
        503: {"description": "zoeken_tijdslimiet: de zoekopdracht duurde te lang en is afgebroken"},
    },
)
def search(
    request: Request,
    q: str = Query(min_length=1, max_length=300),
    mode: Literal["fulltext", "semantic", "hybrid"] = "fulltext",
    source_id: uuid.UUID | None = None,
    kind: SourceKind | None = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_user_context),
) -> dict[str, Any]:
    request.app.state.search_limiter.check(principal.audit_id)
    if mode != "fulltext":
        raise capability_unavailable(
            f"search.{mode}",
            "Semantisch/hybride zoeken vereist een lokaal embeddingmodel (optionele app, standaard uit) en volgt in "
            "B07-003.",
        )
    return fulltext.search(
        session,
        principal,
        q,
        source_id=source_id,
        kind=kind,
        limit=limit,
        cursor=cursor,
        statement_timeout_ms=request.app.state.settings.search_statement_timeout_ms,
    )


@router.get("/privacy/coverage", summary="Privacydekking: per bron toestemming, scopes, toegang en afgeleide data")
def privacy_coverage(
    request: Request, session: Session = Depends(get_session), principal: Principal = Depends(require_user_context)
) -> dict[str, Any]:
    if principal.is_kid:
        raise forbidden("kinderprofiel", "Een kinderprofiel heeft geen toegang tot het privacyoverzicht.")
    return service.coverage(session, principal, request.app.state.settings)


@router.get("/retention", summary="Bewaartermijnen (standaard: zoekgeschiedenis en transcripten niet bewaren)")
def retention_view(session: Session = Depends(get_session), _p: Principal = Depends(require_admin)) -> dict[str, Any]:
    from .retention import DEFAULT_KEEP_DAYS, keep_days

    return {"items": [{"scope": s, "keep_days": keep_days(session, s)} for s in DEFAULT_KEEP_DAYS]}
