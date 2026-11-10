"""FastAPI-app-fabriek."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

from . import APP_ID, CONNECTOR_PREFIX, SEARCH_PREFIX, VOICE_PREFIX, __version__, audit, events
from .bodylimit import MAX_BODY_BYTES, BodyLimitMiddleware
from .config import Settings
from .connector import router as connector_router
from .db import Database
from .enrollment import (
    EnrollmentService,
    KeyRotationTrigger,
    Outcome,
    key_rotation_record,
    migrate_legacy_link,
)
from .errors import TRACE_HEADER, install_handlers, trace_id_for
from .health import router as health_router
from .hubclient import HubClient
from .hublink import HubLinkStore
from .migrations import head_revision
from .ratelimit import RateLimiter
from .search.paths import validate_read_roots
from .search.router import router as search_router
from .security.auth import Authenticator, JwtPolicy, ReplayCache
from .security.secrets import ensure_secrets, load_identity, load_vault_key, public_jwk
from .security.vault import Vault
from .status_page import PairThrottle
from .status_page import router as status_router
from .voice.router import router as voice_router

log = logging.getLogger("search_ai")

__all__ = ["MAX_BODY_BYTES", "create_app"]


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
    store = HubLinkStore(settings.secrets_dir, APP_ID)
    enrollment = EnrollmentService(settings, store, identity, hub)

    def record_key_rotation(outcome: Outcome) -> None:
        """Achtergrondpoll van de API (onbekende ``kid``): een toegepaste of geweigerde rotatie krijgt dezelfde
        audit-regel en hetzelfde ``service.key_rotation.*``-event als in de worker (§7)."""
        rec = key_rotation_record(outcome)
        if rec is None:
            return
        tid = str(uuid.uuid4())
        try:
            with db.sessions() as s:
                audit.record(
                    s,
                    domain="platform",
                    action=rec.action,
                    entity_type="hub_link",
                    entity_id="hub",
                    principal=None,
                    trace_id=tid,
                    message_nl=rec.message_nl,
                    details=rec.details,
                )
                events.emit_service(
                    s, rec.event_type, actor_type="system", actor_id="api", trace_id=tid, payload=rec.payload
                )
                s.commit()
        except Exception:  # de pins staan al vast in hub-link.json; het auditspoor mag dat niet ongedaan maken
            log.warning("auditspoor voor %s kon niet worden vastgelegd", rec.action)

    trigger = KeyRotationTrigger(enrollment, settings.key_rotation_min_interval_s, on_outcome=record_key_rotation)
    policy = JwtPolicy(settings.jwt_issuer, settings.jwt_audience, settings.jwt_max_lifetime_s, settings.jwt_leeway_s)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            migrate_legacy_link(store, db, vault, settings.allow_loopback_hub)
        except Exception:  # database nog niet klaar: de worker en de volgende start proberen het opnieuw
            log.warning("overzetten van de oude koppeling is uitgesteld (database niet beschikbaar)")
        enrollment.on_api_start()
        yield
        db.dispose()

    app = FastAPI(
        title="Zoeken en persoonlijke AI",
        version=__version__,
        description=(
            "B07 persoonlijke zoekmachine (PostgreSQL + pgvector) en B15 spraak/persoonlijke AI voor TrueNAS Home "
            "Platform V2. Bronselectie en toestemming (B07-001) met lees-padenallowlist, gebruikers-ACL vóór ranking, "
            "kinderprofiel-beperkingen en intrekking met gecontroleerde purge. Authenticatie alleen met Hub-JWT's "
            "die zijn ondertekend met een bij het koppelen gepinde sleutel (platformcontract 1.3 §4). Niet-gebouwde "
            "functies geven `501 capability_unavailable`; nooit gesimuleerd succes."
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
    app.state.hub_link = store
    app.state.enrollment = enrollment
    app.state.key_rotation_trigger = trigger
    app.state.auth = Authenticator(store.read_safe, policy, ReplayCache(db.engine, settings.jwt_leeway_s), trigger)
    app.state.alembic_head = head_revision(settings)
    app.state.pair_throttle = PairThrottle()
    app.state.search_limiter = RateLimiter(max(1, settings.search_rate_limit), settings.search_rate_window_s)
    app.state.app_id = APP_ID

    install_handlers(app)

    @app.middleware("http")
    async def trace_and_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        tid = trace_id_for(request)
        response = await call_next(request)
        response.headers.setdefault(TRACE_HEADER, tid)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path.startswith((SEARCH_PREFIX, VOICE_PREFIX, CONNECTOR_PREFIX)):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # buitenste laag: telt de body vóór routering en authenticatie (contract 1.3 §5)
    app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)

    for r in (health_router, status_router, connector_router, search_router, voice_router):
        app.include_router(r)
    return app
