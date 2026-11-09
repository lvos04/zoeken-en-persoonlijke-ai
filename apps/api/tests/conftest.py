"""Gedeelde fixtures: Hub-sleutels/JWT's, een nep-Hub (httpx.MockTransport), een test-PostgreSQL met pgvector en
nep-shares op schijf.

Integratietests gebruiken de gedeelde ontwikkel-PostgreSQL (``SEARCH_AI_TEST_ADMIN_URL``, standaard
127.0.0.1:5432) met een eigen database ``zoek_test_api``. Er worden nooit echte apps, NAS'en of accounts aangesproken.
De nep-Hub volgt het verzoekformaat van de echte Hub (``ai-server-manager``, ``routers/services.py``).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine, text

from search_ai.config import Settings
from search_ai.security.secrets import public_jwk

ADMIN_DB_URL = os.environ.get("SEARCH_AI_TEST_ADMIN_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/postgres")
TEST_DB_NAME = os.environ.get("SEARCH_AI_TEST_DB_NAME", "zoek_test_api")
REPO_ROOT = Path(__file__).resolve().parents[3]

ENV_TOKEN = "enr_env-enroll-token-0123456789abcdef"  # testwaarde (nep-Hub)
PAIR_CODE = "KOPP-ELC0-DE42"
HUB_URL = "http://hub.test:30400"


def recreate_database(name: str) -> str:
    eng = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{name}"'))
    eng.dispose()
    return ADMIN_DB_URL.rsplit("/", 1)[0] + "/" + name


@dataclass
class HubKeys:
    private: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)

    @property
    def jwk(self) -> dict[str, str]:
        return public_jwk(self.private)

    @property
    def kid(self) -> str:
        return self.jwk["kid"]

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.jwk]}

    def token(
        self,
        roles: list[str] | None = None,
        sub: str = "user-1",
        aud: str = "zoeken-en-persoonlijke-ai",
        iss: str = "hub",
        lifetime: int = 60,
        iat_offset: int = 0,
        kid: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time()) + iat_offset
        claims = {
            "iss": iss,
            "aud": aud,
            "sub": sub,
            "roles": roles or [],
            "iat": now,
            "nbf": now,
            "exp": now + lifetime,
            "jti": str(uuid.uuid4()),
            **(extra or {}),
        }
        return jwt.encode(claims, self.private, algorithm="EdDSA", headers={"kid": kid or self.kid})


ENROLL_FIELDS = {"app_id", "enrollment_token", "pairing_code", "version", "base_url", "public_key_jwk"}


@dataclass
class FakeHub:
    """Nep-Hub voor httpx.MockTransport. Registreert **elk** uitgaand verzoek (voor de 'verlaat NAS niet'-toets)."""

    keys: HubKeys
    up: bool = True
    issued_token: str = field(default_factory=lambda: "svc_" + uuid.uuid4().hex + uuid.uuid4().hex)
    used: set[str] = field(default_factory=set)
    enroll_requests: list[dict[str, Any]] = field(default_factory=list)
    inbox: list[dict[str, Any]] = field(default_factory=list)
    all_requests: list[dict[str, Any]] = field(default_factory=list)
    inbox_status: int = 202
    jwks_calls: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.all_requests.append(
            {"host": url.host, "path": url.path, "method": request.method, "body": request.content.decode()}
        )
        if url.host != "hub.test":
            raise httpx.ConnectError("onbekende host", request=request)
        if not self.up:
            raise httpx.ConnectError("hub offline", request=request)
        if url.path == "/api/v1/auth/jwks":
            self.jwks_calls += 1
            return httpx.Response(200, json=self.keys.jwks())
        if url.path == "/api/v1/services/enroll" and request.method == "POST":
            body = json.loads(request.content)
            self.enroll_requests.append(body)
            if set(body) - ENROLL_FIELDS:  # de echte Hub heeft extra=forbid
                return httpx.Response(422, json={"code": "validation_error"})
            if bool(body.get("enrollment_token")) == bool(body.get("pairing_code")):
                return httpx.Response(422, json={"code": "validation_error"})
            secret = body.get("enrollment_token") or body.get("pairing_code")
            if secret not in {ENV_TOKEN, PAIR_CODE} or secret in self.used:
                return httpx.Response(401, json={"code": "invalid_enrollment"})
            self.used.add(secret)
            return httpx.Response(
                200,
                json={
                    "service_id": str(uuid.uuid4()),
                    "app_id": body["app_id"],
                    "service_token": self.issued_token,
                    "token_type": "Bearer",
                    "hub_issuer": "hub",
                    "jwks": self.keys.jwks(),
                    "jwks_url": "/api/v1/auth/jwks",
                    "policy_public_key": HubKeys().jwk,
                    "events_inbox_url": "/api/v1/events/inbox",
                },
            )
        if url.path == "/api/v1/events/inbox" and request.method == "POST":
            if request.headers.get("Authorization") != f"Bearer {self.issued_token}":
                return httpx.Response(401)
            self.inbox.append(json.loads(request.content))
            return httpx.Response(self.inbox_status)
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def hub_keys() -> HubKeys:
    return HubKeys()


@pytest.fixture
def fake_hub(hub_keys: HubKeys) -> FakeHub:
    return FakeHub(hub_keys)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "appdata"
    d.mkdir()
    return d


@dataclass
class Shares:
    """Twee nep-shares onder een eigen leeswortel: A (wordt geselecteerd) en B (niet)."""

    root: Path
    a: Path
    b: Path

    def checksums(self) -> dict[str, tuple[str, int]]:
        import hashlib

        out = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        return out


@pytest.fixture
def shares(tmp_path: Path) -> Shares:
    root = tmp_path / "bronnen"
    a = root / "documenten"
    b = root / "prive-b"
    (a / "werk").mkdir(parents=True)
    (a / ".ssh").mkdir()
    (a / ".zfs" / "snapshot" / "auto-1").mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "werk" / "offerte.txt").write_text("Offerte voor de verbouwing van de keuken.", encoding="utf-8")
    (a / "notities.md").write_text("Boodschappen: appels, peren.", encoding="utf-8")
    (a / ".ssh" / "id_ed25519").write_text("GEHEIM-SLEUTEL", encoding="utf-8")
    (a / "server.pem").write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    (a / ".zfs" / "snapshot" / "auto-1" / "notities.md").write_text("oude kopie", encoding="utf-8")
    (b / "dagboek.txt").write_text("Privé dagboek van B: niet indexeren.", encoding="utf-8")
    (b / "belasting.pdf").write_bytes(b"%PDF-1.4 nep")
    os.symlink(b / "dagboek.txt", a / "link-naar-b.txt")
    os.symlink(b, a / "map-naar-b")
    return Shares(root=root, a=a, b=b)


# ------------------------------------------------------------------ integratie (PostgreSQL + pgvector)
@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    try:
        url = recreate_database(TEST_DB_NAME)
    except Exception as exc:  # pragma: no cover - zonder Postgres worden integratietests overgeslagen
        pytest.skip(f"PostgreSQL niet beschikbaar: {type(exc).__name__}")
    from search_ai.migrations import upgrade

    upgrade(Settings(database_url=url))
    yield url


TRUNCATE = (
    "TRUNCATE service_state, idempotency_record, outbox_event, vault_secret, audit_event, index_source, "
    "privacy_consent, connector_permission, access_scope, document_ref, chunk, embedding, index_run, search_query, "
    "transcript_ref, voice_session, transcript, intent, context_scope, tts_job, audio_consent "
    "RESTART IDENTITY CASCADE"
)


@pytest.fixture
def clean_db(database_url: str) -> Iterator[str]:
    eng = create_engine(database_url)
    with eng.begin() as c:
        c.execute(text(TRUNCATE))
        c.execute(text("UPDATE retention SET keep_days = 0 WHERE scope IN ('search_query', 'voice_transcript')"))
        c.execute(text("DELETE FROM retention WHERE source_id IS NOT NULL"))
    eng.dispose()
    yield database_url


@pytest.fixture
def make_settings(data_dir: Path, clean_db: str, shares: Shares) -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "data_dir": data_dir,
            "database_url": clean_db,
            "hub_url": None,
            "enrollment_token": None,
            "jwks_min_refresh_interval_s": 0,
            "link_cache_ttl_s": 0,
            "read_roots": (str(shares.root),),
            # tijdelijke testmappen zijn schrijfbaar; de alleen-lezen-eis wordt apart getest
            "require_readonly_mounts": False,
        }
        base.update(overrides)
        return Settings(**base)

    return _make


@pytest.fixture
def app_factory(make_settings: Callable[..., Settings], fake_hub: FakeHub) -> Iterator[Callable[..., Any]]:
    from fastapi.testclient import TestClient

    from search_ai.app import create_app

    clients: list[TestClient] = []

    def _make(**overrides: Any) -> TestClient:
        app = create_app(make_settings(**overrides), transport=fake_hub.transport)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(app_factory: Callable[..., Any]) -> Any:
    return app_factory()


@pytest.fixture
def paired_client(client: Any) -> Any:
    """App die via de koppelcode met de nep-Hub gekoppeld is."""
    state = client.app.state
    with state.db.sessions() as s:
        outcome = state.enrollment.enroll_with_code(s, HUB_URL, PAIR_CODE)
        s.commit()
    assert outcome.ok, outcome.reason
    state.link.invalidate()
    return client


@pytest.fixture
def auth(hub_keys: HubKeys) -> Callable[..., dict[str, str]]:
    def _auth(*roles: str, sub: str = "user-1") -> dict[str, str]:
        return {"Authorization": f"Bearer {hub_keys.token(list(roles), sub=sub)}"}

    return _auth


def idem(headers: dict[str, str] | None = None, key: str | None = None, **extra: str) -> dict[str, str]:
    return {**(headers or {}), "Idempotency-Key": key or str(uuid.uuid4()), **extra}
