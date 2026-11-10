"""Authenticatie en rollen (contract 1.3 §4.2–§4.7).

Alleen **Hub-JWT's** (EdDSA, ondertekend met een bij de koppeling **gepinde** sleutel, §4.5):

1. ``token_use: "user"`` — gebruikerscontext (``sub`` + ``roles`` ⊂ {admin, operator, viewer, kind}). Alleen hiermee kan
   worden gezocht: de ACL (B07-001) werkt per gebruiker en rol. Rol ``kind`` wint altijd.
2. ``token_use: "hub_service"`` — de Hub zelf (``sub: "hub"``, ``roles: ["service"]``, eigen ``service_id``): principal
   ``service:hub`` met rol ``service``, **zonder** gebruikerscontext (geen zoeken, geen bronbeheer; ADR-0002).

Een inkomend ``Bearer svc_…`` (het eigen service-token) wordt **niet** meer geaccepteerd (§4.3): dat token gaat bij elke
aanroep naar de Hub over het LAN en mag een afluisteraar niet tot "Hub" maken.

Controlevolgorde (§4.7): (1 MiB-laag) → koppelstatus → tokensoort → handtekening/``kid`` → claims → ``token_use`` →
``jti`` → schemavalidatie van de body; rol (403) en ``Idempotency-Key`` pas daarna (RV9). Elke weigering is
``401 {code: "unauthorized", details: {reason}}`` met een vaste ``reason`` (1.3: geen header/ander schema/lege waarde →
``invalid_token``; een JWS-header zonder ``kid`` → ``unknown_key``; ``aud`` moet precies één string zijn, een lijst — ook
met de eigen ``app_id`` — is ``invalid_token``). **Elke** POST/PUT/PATCH/DELETE verbruikt de ``jti``, ook als de body
onleesbaar is of de route niets wijzigt; een al gebruikte ``jti`` → ``token_replayed`` (persistente replaycache in
PostgreSQL, gedeeld door alle processen); kan die niet worden geraadpleegd → ``503 replay_check_unavailable``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from ..errors import ApiError, forbidden
from .pins import pinned_keys

KNOWN_ROLES = frozenset({"admin", "operator", "viewer", "kind"})
SERVICE_ROLE = "service"
KID_ROLE = "kind"
MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_USE_USER = "user"  # noqa: S105 - claimwaarde, geen geheim
TOKEN_USE_HUB_SERVICE = "hub_service"  # noqa: S105 - claimwaarde, geen geheim

REASON_MESSAGES = {
    "not_linked": "Niet geautoriseerd: deze dienst is niet (meer) aan de Hub gekoppeld.",
    "revoked": "Niet geautoriseerd: de Hub heeft de koppeling van deze dienst ingetrokken.",
    "service_token_not_accepted": "Niet geautoriseerd: een service-token wordt hier niet geaccepteerd; de Hub meldt "
    "zich met een dienst-JWT.",
    "unknown_key": "Niet geautoriseerd: het token is ondertekend met een sleutel die deze dienst niet vertrouwt.",
    "token_expired": "Niet geautoriseerd: het token is verlopen.",
    "invalid_token": "Niet geautoriseerd: ontbrekend of ongeldig token.",
    "token_use_invalid": "Niet geautoriseerd: dit token is niet voor dit gebruik bedoeld.",
    "jti_missing": "Niet geautoriseerd: het token mist een jti.",
    "token_replayed": "Niet geautoriseerd: dit token is al gebruikt voor een andere wijziging.",
}


def unauthorized(reason: str) -> ApiError:
    if reason not in REASON_MESSAGES:
        raise ValueError(f"onbekende reason {reason!r}")
    return ApiError(401, "unauthorized", REASON_MESSAGES[reason], {"reason": reason}, {"WWW-Authenticate": "Bearer"})


@dataclass(frozen=True)
class Principal:
    kind: str  # "user" | "service"
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    token_id: str | None = None
    token_use: str | None = None

    @property
    def audit_id(self) -> str:
        return f"{self.kind}:{self.subject}"

    @property
    def actor_type(self) -> str:
        return "user" if self.kind == "user" else "system"

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @property
    def is_kid(self) -> bool:
        """Kinderprofiel: de meest beperkende rol wint."""
        return KID_ROLE in self.roles

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles and not self.is_kid

    def has_any(self, roles: set[str] | frozenset[str]) -> bool:
        return bool(self.roles & roles)


@dataclass
class JwtPolicy:
    issuer: str = "hub"
    audience: str = "zoeken-en-persoonlijke-ai"
    max_lifetime_s: int = 120
    leeway_s: int = 30


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RequestInfo:
    method: str
    path: str
    idempotency_key: str | None


class ReplayCache:
    """Persistente replaycache op ``(iss, jti)`` tot ``exp + 30 s`` (§4.6), in de eigen PostgreSQL."""

    def __init__(self, engine: Any, leeway_s: int = 30) -> None:
        self.engine = engine
        self.leeway_s = leeway_s

    def check_mutation(self, iss: str, jti: uuid.UUID, exp: float, req: RequestInfo) -> None:
        from ..models import JwtReplay

        expires = datetime.fromtimestamp(float(exp), UTC) + timedelta(seconds=self.leeway_s)
        try:
            with self.engine.begin() as conn:
                won = conn.execute(
                    insert(JwtReplay)
                    .values(
                        iss=iss,
                        jti=jti,
                        method=req.method,
                        path=req.path[:500],
                        idempotency_key=req.idempotency_key,
                        expires_at=expires,
                    )
                    .on_conflict_do_nothing(index_elements=["iss", "jti"])
                    .returning(JwtReplay.jti)
                ).first()
                row = None
                if won is None:
                    row = conn.execute(
                        select(JwtReplay.method, JwtReplay.path, JwtReplay.idempotency_key).where(
                            JwtReplay.iss == iss, JwtReplay.jti == jti
                        )
                    ).first()
        except SQLAlchemyError as exc:
            raise ApiError(
                503,
                "replay_check_unavailable",
                "De controle op hergebruikte tokens is niet beschikbaar; er is niets uitgevoerd. Probeer het later opnieuw.",
            ) from exc
        if won is not None:
            return
        # exacte herhaling (zelfde jti, methode, pad en Idempotency-Key): de idempotency-laag antwoordt
        if (
            row is not None
            and req.idempotency_key
            and (row.method, row.path, row.idempotency_key) == (req.method, req.path[:500], req.idempotency_key)
        ):
            return
        raise unauthorized("token_replayed")


def _same_uuid(a: Any, b: Any) -> bool:
    try:
        return isinstance(a, str) and isinstance(b, str) and uuid.UUID(a) == uuid.UUID(b)
    except ValueError:
        return False


class Authenticator:
    """In ``app.state.auth``. Leest de koppelstatus (met pins) bij elk verzoek uit ``hub-link.json``."""

    def __init__(
        self,
        link: Callable[[], dict[str, Any]],
        policy: JwtPolicy,
        replay: ReplayCache | None,
        on_unknown_kid: Callable[[], None] | None = None,
    ) -> None:
        self._link = link
        self.policy = policy
        self.replay = replay
        self.on_unknown_kid = on_unknown_kid

    def authenticate(self, authorization: str | None, req: RequestInfo | None = None) -> Principal:
        # 1. koppelstatus
        doc = self._link()
        state = doc.get("state")
        if state == "ingetrokken":
            raise unauthorized("revoked")
        if state != "gekoppeld":
            raise unauthorized("not_linked")
        # 2. tokensoort
        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise unauthorized("invalid_token")
        if token.startswith("svc_"):
            raise unauthorized("service_token_not_accepted")
        if token.count(".") != 2 or len(token) > 8192:
            raise unauthorized("invalid_token")
        # 3. handtekening/kid (alleen gepinde sleutels; vergelijking op de sleutel zelf)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise unauthorized("invalid_token") from exc
        kid = header.get("kid")
        if header.get("alg") != "EdDSA":
            raise unauthorized("invalid_token")
        if not isinstance(kid, str) or not kid:
            raise unauthorized("unknown_key")  # §4.7 (1.3): een JWS-header zonder kid
        key = pinned_keys(doc.get("pins"), "jwt").get(kid)
        if key is None:
            if self.on_unknown_kid is not None:
                self.on_unknown_kid()  # poll op de achtergrond; dit verzoek wacht daar niet op
            raise unauthorized("unknown_key")
        p = self.policy
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=["EdDSA"],
                audience=p.audience,
                issuer=p.issuer,
                leeway=p.leeway_s,
                options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise unauthorized("token_expired") from exc
        except jwt.PyJWTError as exc:
            raise unauthorized("invalid_token") from exc
        # 4. claims (1.3 §4.2: aud is precies één string, gelijk aan de eigen app_id; PyJWT accepteert ook een lijst)
        if not isinstance(claims.get("aud"), str) or claims["aud"] != p.audience:
            raise unauthorized("invalid_token")
        exp, iat = claims["exp"], claims["iat"]
        if not isinstance(exp, int | float) or not isinstance(iat, int | float) or exp - iat > p.max_lifetime_s:
            raise unauthorized("invalid_token")
        sub, raw_roles = claims.get("sub"), claims.get("roles")
        if not isinstance(sub, str) or not sub or len(sub) > 200:
            raise unauthorized("invalid_token")
        if not isinstance(raw_roles, list) or not all(isinstance(r, str) for r in raw_roles):
            raise unauthorized("invalid_token")
        # 5. token_use
        use = claims.get("token_use")
        if use == TOKEN_USE_USER:
            if SERVICE_ROLE in raw_roles:
                raise unauthorized("token_use_invalid")
            principal = Principal("user", sub, frozenset(r for r in raw_roles if r in KNOWN_ROLES), token_use=use)
        elif use == TOKEN_USE_HUB_SERVICE:
            if (
                sub != "hub"
                or raw_roles != [SERVICE_ROLE]
                or not _same_uuid(claims.get("service_id"), doc.get("service_id"))
            ):
                raise unauthorized("token_use_invalid")
            principal = Principal("service", "hub", frozenset({SERVICE_ROLE}), token_use=use)
        else:
            raise unauthorized("token_use_invalid")
        # 6. jti (+ replaycache bij mutaties)
        jti = claims.get("jti")
        if not isinstance(jti, str) or not jti:
            raise unauthorized("jti_missing")
        try:
            jti_uuid = uuid.UUID(jti)
        except ValueError as exc:
            raise unauthorized("invalid_token") from exc
        if req is not None and req.method in MUTATIONS:
            if self.replay is None:
                raise ApiError(
                    503, "replay_check_unavailable", "De controle op hergebruikte tokens is niet beschikbaar."
                )
            self.replay.check_mutation(p.issuer, jti_uuid, float(exp), req)
        return Principal(principal.kind, principal.subject, principal.roles, str(jti_uuid), principal.token_use)


def current_principal(request: Request) -> Principal:
    auth: Authenticator = request.app.state.auth
    key = request.headers.get("Idempotency-Key")
    info = RequestInfo(request.method, request.url.path, key.strip()[:200] if key else None)
    principal = auth.authenticate(request.headers.get("Authorization"), info)
    request.state.principal = principal
    return principal


def require_roles(*roles: str, allow_kid: bool = False) -> Callable[..., Principal]:
    allowed = frozenset(roles)

    def _dep(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has_any(allowed):
            raise forbidden("rol_ontbreekt", f"Hiervoor is een van deze rollen nodig: {', '.join(sorted(allowed))}.")
        if principal.is_kid and not allow_kid:
            raise forbidden("kinderprofiel", "Een kinderprofiel mag deze handeling niet uitvoeren.")
        return principal

    return _dep


def require_user_context(principal: Principal = Depends(current_principal)) -> Principal:
    """Zoeken en andere ACL-gebonden reads vereisen een gebruiker (``token_use: user``), nooit de Hub als dienst."""
    if not principal.is_user:
        raise forbidden(
            "gebruikerscontext_vereist",
            "Zoeken kan alleen namens een gebruiker (Hub-JWT met gebruikerscontext); de Hub als dienst heeft geen "
            "eigen zoekrechten.",
        )
    if not principal.has_any(KNOWN_ROLES):
        raise forbidden("rol_ontbreekt", "Er is een gebruikersrol nodig.")
    return principal


# Veelgebruikte combinaties (least privilege)
require_admin = require_roles("admin")
require_reader = require_roles("admin", "operator", "viewer")
require_connector_reader = require_roles("admin", "operator", "viewer", SERVICE_ROLE)
require_admin_or_service = require_roles("admin", SERVICE_ROLE)
