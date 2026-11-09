"""Simulator van de Hub voor de dienst-E2E. ALLEEN voor tests; geen productiecode.

Biedt wat zoeken-en-persoonlijke-ai van de Hub verwacht, in het formaat van de echte Hub (``ai-server-manager``):
- ``GET  /api/v1/auth/jwks``         JWKS met één Ed25519-sleutel
- ``POST /api/v1/services/enroll``   eenmalig token (``SIM_ENROLL_TOKEN``) → service-token + JWKS (extra velden → 422)
- ``POST /api/v1/events/inbox``      ontvangt events (alleen met het uitgegeven service-token; dedupe op event_id)
Plus testhulpmiddelen onder ``/sim/*`` (JWT uitgeven, inbox en alle verzoeken lezen).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Header, HTTPException, Request

KEY = Ed25519PrivateKey.generate()
RAW = KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
X = base64.urlsafe_b64encode(RAW).rstrip(b"=").decode()
KID = (
    base64.urlsafe_b64encode(
        hashlib.sha256(
            json.dumps(
                {"crv": "Ed25519", "kty": "OKP", "x": X},
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).digest()
    )
    .rstrip(b"=")
    .decode()
)
JWKS = {
    "keys": [
        {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": X,
            "kid": KID,
            "use": "sig",
            "alg": "EdDSA",
        }
    ]
}
ENROLL_FIELDS = {
    "app_id",
    "enrollment_token",
    "pairing_code",
    "version",
    "base_url",
    "public_key_jwk",
}

STATE: dict[str, Any] = {
    "enroll_token": os.environ.get("SIM_ENROLL_TOKEN", ""),
    "used": False,
    "service_token": "svc_" + secrets.token_urlsafe(32),
    "enroll_requests": [],
    "inbox": [],
    "requests": [],
}

app = FastAPI(title="sim-hub")


@app.middleware("http")
async def record(request: Request, call_next: Any) -> Any:
    if not request.url.path.startswith("/sim/"):
        body = (await request.body()).decode(errors="replace")
        STATE["requests"].append({"method": request.method, "path": request.url.path, "body": body})
    return await call_next(request)


@app.get("/api/v1/auth/jwks")
def jwks() -> dict[str, Any]:
    return JWKS


@app.post("/api/v1/services/enroll")
async def enroll(request: Request) -> dict[str, Any]:
    body = await request.json()
    STATE["enroll_requests"].append({k: v for k, v in body.items() if k not in ("enrollment_token", "pairing_code")})
    if set(body) - ENROLL_FIELDS:
        raise HTTPException(422, "extra velden")
    if STATE["used"] or not STATE["enroll_token"] or body.get("enrollment_token") != STATE["enroll_token"]:
        raise HTTPException(401, "token ongeldig")
    STATE["used"] = True
    return {
        "service_id": str(uuid.uuid4()),
        "app_id": body["app_id"],
        "service_token": STATE["service_token"],
        "token_type": "Bearer",
        "hub_issuer": "hub",
        "jwks": JWKS,
        "jwks_url": "/api/v1/auth/jwks",
        "events_inbox_url": "/api/v1/events/inbox",
    }


@app.post("/api/v1/events/inbox", status_code=202)
async def inbox(request: Request, authorization: str = Header(default="")) -> dict[str, str]:
    if authorization != f"Bearer {STATE['service_token']}":
        raise HTTPException(401)
    event = await request.json()
    if any(e["event_id"] == event["event_id"] for e in STATE["inbox"]):
        return {"status": "duplicaat"}
    STATE["inbox"].append(event)
    return {"status": "ontvangen"}


@app.post("/sim/token")
async def token(request: Request) -> dict[str, str]:
    body = await request.json()
    now = int(time.time())
    claims = {
        "iss": "hub",
        "aud": "zoeken-en-persoonlijke-ai",
        "sub": body.get("sub", "e2e-user"),
        "roles": body.get("roles", []),
        "iat": now,
        "nbf": now,
        "exp": now + 100,
        "jti": str(uuid.uuid4()),
    }
    return {"token": jwt.encode(claims, KEY, algorithm="EdDSA", headers={"kid": KID})}


@app.get("/sim/inbox")
def sim_inbox() -> list[dict[str, Any]]:
    return list(STATE["inbox"])


@app.get("/sim/requests")
def sim_requests() -> list[dict[str, Any]]:
    return list(STATE["requests"])


@app.get("/sim/enroll-requests")
def sim_enroll_requests() -> list[dict[str, Any]]:
    return list(STATE["enroll_requests"])


@app.get("/sim/service-token")
def sim_service_token() -> dict[str, str]:
    return {"token": STATE["service_token"]}
