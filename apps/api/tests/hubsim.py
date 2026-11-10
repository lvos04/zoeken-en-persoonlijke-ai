"""Hub-simulator volgens platformcontract 1.2 (§3.3–§3.8, §4.2–§4.5). ALLEEN voor tests; geen productiecode.

Eén framework-vrije kern (``HubSim.handle``) die zowel in-proces (``httpx.MockTransport``, integratietests) als in een eigen
proces (``tests/e2e/sim_hub.py``, dienst-E2E) draait. Gedrag zoals het contract het vastlegt:

- ``POST /api/v1/services/enroll``: exact schema (onbekende velden, ``use``/``alg`` in de JWK, ``kid`` ≠ thumbprint,
  beide/geen geheim, koppelcode ≠ 12 Crockford-tekens → ``422 validation_error``); onbekend/verlopen/gebruikt/verkeerde
  ``app_id`` → ``401 invalid_enrollment`` (één code, geen orakel); na 10 mislukte pogingen per bron-IP in 15 min →
  ``429 rate_limited`` + ``Retry-After``; toegestane herhaling (zelfde geheim, zelfde sleutel, eerder token nooit gebruikt)
  → nieuw token met ``enrollment_replayed: true``.
- ``GET /api/v1/services/self``, ``POST /api/v1/services/self/rotate`` (oude tokens tot 10 min na het eerste gebruik van
  het nieuwe, max. 24 uur), ``Hub-Token-Rotation: required`` bij een rotatieverzoek of als een ouder token wordt gebruikt
  terwijl het nieuwste nooit gebruikt is.
- ``POST /api/v1/events/inbox`` (Bearer service-token, dedupe op ``event_id``).
- ``GET /api/v1/auth/key-rotations?after=n`` (zonder authenticatie) en ``GET /api/v1/auth/jwks`` (alleen diagnose).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.api_jws import PyJWS

APP_ID = "zoeken-en-persoonlijke-ai"
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ENROLL_FIELDS = {"app_id", "enrollment_token", "pairing_code", "version", "base_url", "public_key_jwk"}
REQUIRED_FIELDS = {"app_id", "version", "base_url", "public_key_jwk"}
APP_ID_RE = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$")
TOKEN_RE = re.compile(r"^enr_[A-Za-z0-9_-]{43}$")
BASE_URL_RE = re.compile(r"^https?://[^\s/?#]+(/[^\s?#]*)?$")
GRANT_TTL_S = 3600
ROTATION_GRACE_S = 600
ROTATION_CAP_S = 24 * 3600
RATE_WINDOW_S = 900
RATE_MAX_FAILURES = 10

Response = tuple[int, dict[str, str], Any]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def raw_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def thumbprint_of_x(x: str) -> str:
    canonical = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": x}, separators=(",", ":"), sort_keys=True)
    return b64url(hashlib.sha256(canonical.encode()).digest())


def hub_jwk(key: Ed25519PrivateKey) -> dict[str, str]:
    """Zoals de Hub (``ai-server-manager`` ``keys.public_jwk``): ``kid = sha256(raw)[:16]``, met ``use``/``alg``."""
    raw = raw_public(key)
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": b64url(raw),
        "kid": hashlib.sha256(raw).hexdigest()[:16],
        "use": "sig",
        "alg": "EdDSA",
    }


def fingerprint(jwt_keys: list[dict[str, str]], policy: dict[str, str] | list[dict[str, str]]) -> str:
    """Onafhankelijke implementatie van §4.5 (1.3: alle policy-sleutels, gesorteerd; geen code van de dienst)."""
    thumbs = sorted(thumbprint_of_x(k["x"]) for k in jwt_keys)
    policies = sorted(thumbprint_of_x(k["x"]) for k in ([policy] if isinstance(policy, dict) else policy))
    text = f"jl-hub-keyset-v1\njwt:{','.join(thumbs)}\npolicy:{','.join(policies)}"
    digest = hashlib.sha256(text.encode()).digest()[:10]
    n = int.from_bytes(digest, "big")
    code = "".join(CROCKFORD[(n >> (75 - 5 * i)) & 31] for i in range(16))
    return "-".join(code[i : i + 4] for i in range(0, 16, 4))


def normalize_code(value: str) -> str:
    cleaned = "".join(ch for ch in value.upper() if ch.isascii() and ch.isalnum())
    return cleaned.replace("O", "0").replace("I", "1").replace("L", "1")


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def error(status: int, code: str, message: str = "", headers: dict[str, str] | None = None) -> Response:
    body = {"code": code, "message": message or code, "details": {}, "trace_id": str(uuid.uuid4())}
    return status, headers or {}, body


@dataclass
class Grant:
    app_id: str
    kind: str
    created_at: float
    used_at: float | None = None
    used_with_key_thumbprint: str | None = None
    token_hash: str | None = None


@dataclass
class TokenRec:
    service_id: str
    created_at: float
    first_used_at: float | None = None
    revoked_at: float | None = None
    expires_at: float | None = None
    superseded_at: float | None = None


@dataclass
class Service:
    service_id: str
    app_id: str
    version: str
    enrolled_at: float
    status: str = "active"
    rotation_requested: bool = False


@dataclass
class HubSim:
    jwt_key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    policy_key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    clock: Callable[[], float] = time.time
    extra_jwt_keys: list[Ed25519PrivateKey] = field(default_factory=list)
    grants: dict[str, Grant] = field(default_factory=dict)
    services: dict[str, Service] = field(default_factory=dict)  # app_id → registratie
    tokens: dict[str, TokenRec] = field(default_factory=dict)  # sha256(token) → token
    failures: dict[str, list[float]] = field(default_factory=dict)
    rotations: list[dict[str, Any]] = field(default_factory=list)
    inbox: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    enroll_requests: list[dict[str, Any]] = field(default_factory=list)
    issued: list[str] = field(default_factory=list)  # uitgegeven service-tokens (alleen voor tests)
    inbox_status: int = 202
    lose_enroll_response: bool = False  # simuleer: Hub verwerkt het verzoek, het antwoord gaat verloren

    # ------------------------------------------------------------------ sleutels
    def jwks(self) -> dict[str, Any]:
        return {"keys": [hub_jwk(k) for k in [self.jwt_key, *self.extra_jwt_keys]]}

    def policy_jwk(self) -> dict[str, str]:
        return hub_jwk(self.policy_key)

    def fingerprint(self) -> str:
        return fingerprint(self.jwks()["keys"], self.policy_jwk())

    def kid(self, key: Ed25519PrivateKey | None = None) -> str:
        return hub_jwk(key or self.jwt_key)["kid"]

    def _jwt(self, claims: dict[str, Any], key: Ed25519PrivateKey | None, kid: str | None) -> str:
        return jwt.encode(claims, key or self.jwt_key, algorithm="EdDSA", headers={"kid": kid or self.kid(key)})

    def base_claims(self, lifetime: int = 60, iat_offset: int = 0) -> dict[str, Any]:
        now = int(self.clock()) + iat_offset
        return {"iss": "hub", "aud": APP_ID, "iat": now, "nbf": now, "exp": now + lifetime, "jti": str(uuid.uuid4())}

    def user_jwt(
        self,
        roles: list[str] | None = None,
        sub: str = "user-1",
        *,
        key: Ed25519PrivateKey | None = None,
        kid: str | None = None,
        lifetime: int = 60,
        iat_offset: int = 0,
        drop: tuple[str, ...] = (),
        **overrides: Any,
    ) -> str:
        claims = {**self.base_claims(lifetime, iat_offset), "sub": sub, "roles": roles or [], "token_use": "user"}
        claims.update(overrides)
        for name in drop:
            claims.pop(name, None)
        return self._jwt(claims, key, kid)

    def service_jwt(
        self,
        service_id: str | None = None,
        *,
        key: Ed25519PrivateKey | None = None,
        drop: tuple[str, ...] = (),
        **overrides: Any,
    ) -> str:
        sid = service_id or (self.services[APP_ID].service_id if APP_ID in self.services else str(uuid.uuid4()))
        claims = {
            **self.base_claims(),
            "sub": "hub",
            "roles": ["service"],
            "token_use": "hub_service",
            "service_id": sid,
        }
        claims.update(overrides)
        for name in drop:
            claims.pop(name, None)
        return self._jwt(claims, key, None)

    # ------------------------------------------------------------------ beheer (wat de Hub-UI doet)
    def add_enrollment_token(self, token: str | None = None, app_id: str = APP_ID) -> str:
        token = token or "enr_" + secrets.token_urlsafe(32)
        self.grants[sha(token)] = Grant(app_id, "token", self.clock())
        return token

    def add_pairing_code(self, code: str | None = None, app_id: str = APP_ID) -> str:
        raw = normalize_code(code) if code else "".join(secrets.choice(CROCKFORD) for _ in range(12))
        self.grants[sha(raw)] = Grant(app_id, "koppelcode", self.clock())
        return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"

    def revoke(self, app_id: str = APP_ID) -> None:
        svc = self.services[app_id]
        svc.status = "revoked"
        for rec in self.tokens.values():
            if rec.service_id == svc.service_id and rec.revoked_at is None:
                rec.revoked_at = self.clock()

    def request_rotation(self, app_id: str = APP_ID) -> None:
        self.services[app_id].rotation_requested = True

    def rotation_statement(
        self,
        sequence: int,
        purpose: str,
        add: list[dict[str, str]],
        retire: list[str],
        signer: Ed25519PrivateKey,
        *,
        typ: str = "hub-key-rotation+jws",
        **overrides: Any,
    ) -> str:
        payload = {
            "type": "hub_key_rotation",
            "iss": "hub",
            "sequence": sequence,
            "issued_at": iso(self.clock()),
            "purpose": purpose,
            "add": add,
            "retire": retire,
            **overrides,
        }
        return PyJWS().encode(
            json.dumps(payload).encode(), signer, algorithm="EdDSA", headers={"kid": self.kid(signer), "typ": typ}
        )

    def current_token(self, app_id: str = APP_ID) -> str | None:
        svc = self.services.get(app_id)
        if svc is None:
            return None
        for tok in reversed(self.issued):
            rec = self.tokens.get(sha(tok))
            if rec and rec.service_id == svc.service_id and rec.revoked_at is None:
                return tok
        return None

    # ------------------------------------------------------------------ verzoeken
    def handle(
        self, method: str, path: str, query: str, headers: dict[str, str], body: bytes, client_ip: str
    ) -> Response:
        self.requests.append(
            {"method": method, "path": path, "query": query, "body": body.decode(errors="replace"), "ip": client_ip}
        )
        q = {k: v[-1] for k, v in parse_qs(query).items()}
        if method == "GET" and path == "/api/v1/auth/jwks":
            return 200, {}, self.jwks()
        if method == "GET" and path == "/api/v1/auth/key-rotations":
            try:
                after = int(q.get("after", "0"))
            except ValueError:
                return error(422, "validation_error")
            return 200, {}, {"items": [r for r in self.rotations if r["sequence"] > after]}
        if method == "POST" and path == "/api/v1/services/enroll":
            return self._enroll(body, client_ip)
        auth = headers.get("authorization", "")
        if path in ("/api/v1/services/self", "/api/v1/services/self/rotate", "/api/v1/events/inbox"):
            token = auth[7:] if auth.startswith("Bearer ") else ""
            rec = self._valid(token)
            if rec is None:
                return error(401, "unauthorized")
            hdrs = self._use(token, rec)
            if method == "GET" and path == "/api/v1/services/self":
                svc = self._svc(rec)
                return (
                    200,
                    hdrs,
                    {
                        "service_id": svc.service_id,
                        "app_id": svc.app_id,
                        "status": svc.status,
                        "version": svc.version,
                        "enrolled_at": iso(svc.enrolled_at),
                        "keyset_fingerprint": self.fingerprint(),
                    },
                )
            if method == "POST" and path == "/api/v1/services/self/rotate":
                return self._rotate(token, rec)
            if method == "POST" and path == "/api/v1/events/inbox":
                event = json.loads(body)
                if any(e["event_id"] == event["event_id"] for e in self.inbox):
                    return 202, hdrs, {"status": "duplicaat"}
                self.inbox.append(event)
                return self.inbox_status, hdrs, {"status": "ontvangen"}
        return error(404, "not_found")

    def _svc(self, rec: TokenRec) -> Service:
        return next(s for s in self.services.values() if s.service_id == rec.service_id)

    def _valid(self, token: str) -> TokenRec | None:
        rec = self.tokens.get(sha(token)) if token else None
        now = self.clock()
        if rec is None or rec.revoked_at is not None or (rec.expires_at is not None and rec.expires_at <= now):
            return None
        return rec

    def _use(self, token: str, rec: TokenRec) -> dict[str, str]:
        now = self.clock()
        if rec.first_used_at is None:
            rec.first_used_at = now
            # eerste gebruik van het nieuwe token: oudere tokens nog 10 min geldig (binnen de bovengrens van 24 uur)
            for other in self.tokens.values():
                if other is not rec and other.service_id == rec.service_id and other.superseded_at is not None:
                    cap = other.expires_at or now + ROTATION_CAP_S
                    other.expires_at = min(cap, now + ROTATION_GRACE_S)
        svc = self._svc(rec)
        newest = self.tokens.get(sha(self.current_token(svc.app_id) or ""))
        stale = newest is not None and newest is not rec and newest.first_used_at is None
        return {"Hub-Token-Rotation": "required"} if svc.rotation_requested or stale else {}

    def _issue(self, svc: Service) -> str:
        token = "svc_" + secrets.token_urlsafe(32)
        self.tokens[sha(token)] = TokenRec(svc.service_id, self.clock())
        self.issued.append(token)
        return token

    def _rotate(self, token: str, rec: TokenRec) -> Response:
        now = self.clock()
        svc = self._svc(rec)
        for other_token in list(self.issued):
            other = self.tokens[sha(other_token)]
            if other.service_id != svc.service_id or other.revoked_at is not None:
                continue
            if other is not rec and other.first_used_at is None:
                other.revoked_at = now  # eerder uitgegeven, nooit gebruikte nieuwe tokens
                continue
            other.superseded_at = other.superseded_at or now
            other.expires_at = min(other.expires_at or now + ROTATION_CAP_S, now + ROTATION_CAP_S)
        new = self._issue(svc)
        svc.rotation_requested = False
        return (
            200,
            {},
            {"service_id": svc.service_id, "service_token": new, "oude_tokens_geldig_tot": iso(now + ROTATION_CAP_S)},
        )

    def _fail(self, ip: str) -> Response:
        self.failures.setdefault(ip, []).append(self.clock())
        return error(401, "invalid_enrollment", "Koppelen geweigerd.")

    def _enroll(self, body: bytes, ip: str) -> Response:
        now = self.clock()
        recent = [t for t in self.failures.get(ip, []) if now - t < RATE_WINDOW_S]
        self.failures[ip] = recent
        if len(recent) >= RATE_MAX_FAILURES:
            retry = int(RATE_WINDOW_S - (now - recent[0])) + 1
            return error(429, "rate_limited", "Te veel pogingen.", {"Retry-After": str(retry)})
        try:
            data = json.loads(body)
        except ValueError:
            return error(422, "validation_error")
        if not isinstance(data, dict):
            return error(422, "validation_error")
        self.enroll_requests.append({k: v for k, v in data.items() if k not in ("enrollment_token", "pairing_code")})
        problem = self._schema_problem(data)
        if problem:
            return error(422, "validation_error", problem)
        if data.get("enrollment_token") is not None:
            secret_hash = sha(data["enrollment_token"])
        else:
            secret_hash = sha(normalize_code(data["pairing_code"]))
        grant = self.grants.get(secret_hash)
        thumb = thumbprint_of_x(data["public_key_jwk"]["x"])
        if grant is None or grant.app_id != data["app_id"] or now - grant.created_at > GRANT_TTL_S:
            return self._fail(ip)
        replayed = False
        if grant.used_at is not None:
            old = self.tokens.get(grant.token_hash or "")
            if grant.used_with_key_thumbprint != thumb or old is None or old.first_used_at is not None:
                return self._fail(ip)
            old.revoked_at = now  # ongebruikt token van de eerdere poging intrekken
            replayed = True
            svc = self.services[data["app_id"]]
        else:
            grant.used_at, grant.used_with_key_thumbprint = now, thumb
            svc = self.services.get(data["app_id"])  # type: ignore[assignment]
            if svc is None:
                svc = Service(str(uuid.uuid4()), data["app_id"], data["version"], now)
                self.services[data["app_id"]] = svc
            else:  # opnieuw koppelen: service_id blijft, alle oude tokens vervallen
                for rec in self.tokens.values():
                    if rec.service_id == svc.service_id and rec.revoked_at is None:
                        rec.revoked_at = now
                svc.enrolled_at, svc.status = now, "active"
        svc.version, svc.rotation_requested = data["version"], False
        token = self._issue(svc)
        grant.token_hash = sha(token)
        self.failures.pop(ip, None)
        body_out = {
            "schema_version": "1.2",
            "service_id": svc.service_id,
            "app_id": svc.app_id,
            "service_token": token,
            "token_type": "Bearer",
            "hub_issuer": "hub",
            "jwks": self.jwks(),
            "policy_public_key": self.policy_jwk(),
            "keyset_fingerprint": self.fingerprint(),
            "jwks_url": "/api/v1/auth/jwks",
            "events_inbox_url": "/api/v1/events/inbox",
            "key_rotation_url": "/api/v1/auth/key-rotations",
            "enrollment_replayed": replayed,
        }
        if self.lose_enroll_response:
            self.lose_enroll_response = False
            return error(502, "service_unavailable", "antwoord kwijt (simulatie)")
        return 200, {"Cache-Control": "no-store"}, body_out

    @staticmethod
    def _schema_problem(data: dict[str, Any]) -> str | None:
        if set(data) - ENROLL_FIELDS:
            return "onbekende velden: " + ", ".join(sorted(set(data) - ENROLL_FIELDS))
        if REQUIRED_FIELDS - set(data):
            return "verplichte velden ontbreken: " + ", ".join(sorted(REQUIRED_FIELDS - set(data)))
        if (data.get("enrollment_token") is None) == (data.get("pairing_code") is None):
            return "precies één van enrollment_token of pairing_code"
        app_id, version, base = data["app_id"], data["version"], data["base_url"]
        if not isinstance(app_id, str) or not (1 <= len(app_id) <= 40) or not APP_ID_RE.fullmatch(app_id):
            return "app_id ongeldig"
        if not isinstance(version, str) or not (1 <= len(version) <= 64) or not VERSION_RE.fullmatch(version):
            return "version ongeldig"
        if base is not None and (not isinstance(base, str) or len(base) > 512 or not BASE_URL_RE.fullmatch(base)):
            return "base_url ongeldig"
        tok = data.get("enrollment_token")
        if tok is not None and (not isinstance(tok, str) or not TOKEN_RE.fullmatch(tok)):
            return "enrollment_token ongeldig"
        code = data.get("pairing_code")
        if code is not None:
            norm = normalize_code(code) if isinstance(code, str) and len(code) <= 32 else ""
            if len(norm) != 12 or any(ch not in CROCKFORD for ch in norm):
                return "pairing_code moet 12 Crockford-tekens zijn"
        jwk = data["public_key_jwk"]
        if not isinstance(jwk, dict) or set(jwk) - {"kty", "crv", "x", "kid"}:
            return "public_key_jwk: alleen kty, crv, x en kid"
        if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or not isinstance(jwk.get("x"), str):
            return "public_key_jwk: geen Ed25519"
        x = jwk["x"]
        try:
            raw = base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
        except ValueError:
            return "x ongeldig"
        if len(x) != 43 or len(raw) != 32 or b64url(raw) != x:
            return "x moet 32 bytes canoniek zijn"
        if "kid" in jwk and jwk["kid"] != thumbprint_of_x(x):
            return "kid moet de RFC 7638-thumbprint zijn"
        return None
