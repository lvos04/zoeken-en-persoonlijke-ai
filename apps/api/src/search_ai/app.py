"""FastAPI-app-fabriek."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from sqlalchemy.orm import Session

from . import APP_ID, CONNECTOR_PREFIX, SEARCH_PREFIX, VOICE_PREFIX, __version__, audit
from .config import Settings
from .connector import router as connector_router
from .db import Database
from .deps import get_session, trace_id
from .enrollment import JWKS_PATH, EnrollmentService, stored_jwks
from .errors import TRACE_HEADER, error_response, install_handlers, trace_id_for
from .health import router as health_router
from .hubclient import HubClient
from .idempotency import idempotency_key, run_idempotent
from .migrations import head_revision
from .search.paths import validate_read_roots
from .search.router import router as search_router
from .security.auth import Authenticator, JwtPolicy, Principal, require_admin
from .security.jwks import JwksCache
from .security.secrets import ensure_secrets, load_identity, load_vault_key, public_jwk
from .security.vault import Vault
from .status_page import Throttle, new_unpair_code
from .status_page import router as status_router
from .voice.router import router as voice_router

log = logging.getLogger("search_ai")

MAX_BODY_BYTES = 1024 * 1024  # 1 MiB: geen enkel verzoek van deze API is groter (geen uploads)


class HubLinkCache:
    """Leest de koppelstatus (die ook de worker kan schrijven) met een korte TTL en houdt de JWKS-cache in lijn."""

    def __init__(
        self, db: Database, enrollment: EnrollmentService, hub: HubClient, jwks: JwksCache, ttl_s: float
    ) -> None:
        self.db, self.enrollment, self.hub, self.jwks, self.ttl_s = db, enrollment, hub, jwks, ttl_s
        self._lock = threading.Lock()
        self._loaded_at: float | None = None
        self._hash: str | None = None
        self._hub_url: str | None = None
        self._enrolled_at: str | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._loaded_at = None

    def _refresh(self) -> None:
        now = time.monotonic()
        if self._loaded_at is not None and now - self._loaded_at < self.ttl_s:
            return
        try:
            with self.db.sessions() as s:
                st = self.enrollment.state(s)
                doc = stored_jwks(s)
        except Exception:  # database weg → fail-closed: geen service-token geldig
            self._hash = None
            self._loaded_at = None
            return
        self._loaded_at = now
        if st.get("status") != "gekoppeld":
            if self._hub_url is not None:
                self.jwks.set_fetcher(None)
                self.jwks.clear()
            self._hash, self._hub_url, self._enrolled_at = None, None, None
            return
        hub_url = st.get("hub_url")
        if hub_url != self._hub_url or st.get("enrolled_at") != self._enrolled_at:
            self.jwks.clear()
            url = str(hub_url)
            self.jwks.set_fetcher(lambda: self.hub.get_json(url, JWKS_PATH))
            if doc is not None:
                try:
                    self.jwks.seed(doc)
                except ValueError:
                    log.warning("opgeslagen JWKS is ongeldig; wordt opnieuw opgehaald")
            self._hub_url, self._enrolled_at = hub_url, st.get("enrolled_at")
        self._hash = st.get("service_token_sha256")

    def service_token_hash(self) -> str | None:
        with self._lock:
            self._refresh()
            return self._hash


def _enrollment_router() -> APIRouter:
    r = APIRouter(prefix=SEARCH_PREFIX, tags=["koppeling"])

    @r.get("/enrollment", summary="Koppelstatus met de Hub (admin)")
    def enrollment_state(
        request: Request, session: Session = Depends(get_session), _p: Principal = Depends(require_admin)
    ) -> dict[str, Any]:
        state = request.app.state.enrollment.public_state(session)
        js = request.app.state.jwks.status()
        state["jwks"] = {"available": js.available, "keys": js.keys, "age_s": js.age_s, "stale": js.stale}
        return state

    @r.post("/enrollment/unpair", summary="Ontkoppelen van de Hub (admin)")
    def unpair(
        request: Request,
        session: Session = Depends(get_session),
        principal: Principal = Depends(require_admin),
        key: uuid.UUID = Depends(idempotency_key),
        tid: str = Depends(trace_id),
    ) -> Any:
        def action() -> tuple[int, Any, dict[str, str]]:
            request.app.state.enrollment.unpair(session)
            audit.record(
                session,
                domain="platform",
                action="hub.unpair",
                entity_type="hub_link",
                entity_id="hub",
                principal=principal,
                trace_id=tid,
                message_nl="Ontkoppeld van de Hub door een beheerder.",
            )
            return 200, request.app.state.enrollment.public_state(session), {}

        resp = run_idempotent(request, session, principal, key, {"unpair": True}, action)
        request.app.state.link.invalidate()
        return resp

    return r


def create_app(settings: Settings | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    created = ensure_secrets(settings.secrets_dir)
    if created:
        log.info("eerste start: geheimen aangemaakt in %s: %s", settings.secrets_dir, ", ".join(created))
    _roots, root_warnings = validate_read_roots(settings.read_roots, settings.data_dir)
    for w in root_warnings:
        log.warning("%s", w)
    vault = Vault(load_vault_key(settings.secrets_dir))
    identity = public_jwk(load_identity(settings.secrets_dir))
    db = Database(settings.database_url)
    hub = HubClient(settings, transport=transport)
    enrollment = EnrollmentService(settings, vault, identity, hub)
    jwks = JwksCache(None, settings.jwks_ttl_s, settings.jwks_max_stale_s, settings.jwks_min_refresh_interval_s)
    link = HubLinkCache(db, enrollment, hub, jwks, settings.link_cache_ttl_s)
    policy = JwtPolicy(settings.jwt_issuer, settings.jwt_audience, settings.jwt_max_lifetime_s, settings.jwt_leeway_s)
    unpair_code = new_unpair_code()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.warning(
            "Lokale ontkoppelcode (alleen geldig tot de volgende herstart; alleen nodig om een verloren Hub te "
            "vervangen): %s",
            unpair_code,
        )
        yield
        db.dispose()

    app = FastAPI(
        title="Zoeken en persoonlijke AI",
        version=__version__,
        description=(
            "B07 persoonlijke zoekmachine (PostgreSQL + pgvector) en B15 spraak/persoonlijke AI voor TrueNAS Home "
            "Platform V2. Bronselectie en toestemming (B07-001) met lees-padenallowlist, gebruikers-ACL vóór ranking, "
            "kinderprofiel-beperkingen en intrekking met gecontroleerde purge. Niet-gebouwde functies geven "
            "`501 capability_unavailable`; nooit gesimuleerd succes."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.vault = vault
    app.state.db = db
    app.state.hub = hub
    app.state.enrollment = enrollment
    app.state.jwks = jwks
    app.state.link = link
    app.state.auth = Authenticator(jwks, policy, link.service_token_hash)
    app.state.alembic_head = head_revision(settings)
    app.state.unpair_code_sha256 = hashlib.sha256(unpair_code.encode()).hexdigest()
    app.state.pair_throttle = Throttle()
    app.state.app_id = APP_ID

    install_handlers(app)

    @app.middleware("http")
    async def trace_and_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        tid = trace_id_for(request)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            raw = request.headers.get("content-length")
            if raw is None and "chunked" in request.headers.get("transfer-encoding", "").lower():
                return error_response(request, 411, "length_required", "Content-Length is verplicht.")
            if raw is not None and (not raw.isdigit() or int(raw) > MAX_BODY_BYTES):
                return error_response(
                    request,
                    413,
                    "payload_too_large",
                    "Het verzoek is te groot (maximaal 1 MiB).",
                    {"max": MAX_BODY_BYTES},
                )
        response = await call_next(request)
        response.headers.setdefault(TRACE_HEADER, tid)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path.startswith((SEARCH_PREFIX, VOICE_PREFIX, CONNECTOR_PREFIX)):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    for r in (
        health_router,
        status_router,
        connector_router,
        _enrollment_router(),
        search_router,
        voice_router,
    ):
        app.include_router(r)
    return app
