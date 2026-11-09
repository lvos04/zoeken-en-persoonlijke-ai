"""Gedeelde fixtures: een Hub-simulator volgens contract 1.2 (``tests/hubsim.py``, via httpx.MockTransport), een
test-PostgreSQL met pgvector en nep-shares op schijf.

Integratietests gebruiken de gedeelde ontwikkel-PostgreSQL (``SEARCH_AI_TEST_ADMIN_URL``, standaard
127.0.0.1:5432) met een eigen database ``zoek_test_api``. Er worden nooit echte apps, NAS'en of accounts aangesproken.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, text

from search_ai.config import Settings
from tests.hubsim import HubSim, b64url

ADMIN_DB_URL = os.environ.get("SEARCH_AI_TEST_ADMIN_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/postgres")
TEST_DB_NAME = os.environ.get("SEARCH_AI_TEST_DB_NAME", "zoek_test_api")
REPO_ROOT = Path(__file__).resolve().parents[3]

# vaste testwaarden (alleen de simulator kent ze); formaat zoals contract 1.2 §3.4
ENV_TOKEN = "enr_" + b64url(hashlib.sha256(b"zoek-test-env-token").digest())
PAIR_CODE = "KQ4M-7ZP2-9HXT"
HUB_URL = "http://hub.test:30400"
HUB_HOST = "hub.test"


def recreate_database(name: str) -> str:
    eng = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{name}"'))
    eng.dispose()
    return ADMIN_DB_URL.rsplit("/", 1)[0] + "/" + name


class FreshAuth(Mapping[str, str]):
    """``Authorization``-header die bij **elke** lezing een nieuwe Hub-JWT (nieuwe ``jti``) maakt — zoals de echte Hub
    per mutatie een nieuwe JWT haalt (contract 1.2 §4.6). Zo kan een test ``headers=admin`` vaker gebruiken; replaytests
    gebruiken bewust één vaste tokenstring."""

    def __init__(self, mint: Callable[[], str]) -> None:
        self._mint = mint

    def __getitem__(self, key: str) -> str:
        if key != "Authorization":
            raise KeyError(key)
        return f"Bearer {self._mint()}"

    def __iter__(self) -> Iterator[str]:
        return iter(("Authorization",))

    def __len__(self) -> int:
        return 1


@dataclass
class FakeHub:
    """Hub-simulator achter ``httpx.MockTransport``. Registreert **elk** uitgaand verzoek (voor de 'verlaat NAS
    niet'-toets) en kan offline gaan of een antwoord vervangen."""

    sim: HubSim = field(default_factory=HubSim)
    up: bool = True
    all_requests: list[dict[str, Any]] = field(default_factory=list)
    override: Callable[[httpx.Request], httpx.Response | None] | None = None

    def __post_init__(self) -> None:
        self.sim.add_enrollment_token(ENV_TOKEN)
        self.sim.add_pairing_code(PAIR_CODE)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.all_requests.append(
            {"host": url.host, "path": url.path, "method": request.method, "body": request.content.decode()}
        )
        if url.host != HUB_HOST:
            raise httpx.ConnectError("onbekende host", request=request)
        if not self.up:
            raise httpx.ConnectError("hub offline", request=request)
        if self.override is not None:
            replaced = self.override(request)
            if replaced is not None:
                return replaced
        status, headers, body = self.sim.handle(
            request.method,
            url.path,
            url.query.decode(),
            {k.lower(): v for k, v in request.headers.items()},
            request.content,
            "192.0.2.10",
        )
        return httpx.Response(status, json=body, headers=headers)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # gemak voor tests
    @property
    def inbox(self) -> list[dict[str, Any]]:
        return self.sim.inbox

    @property
    def enroll_requests(self) -> list[dict[str, Any]]:
        return self.sim.enroll_requests

    @property
    def issued_token(self) -> str:
        tok = self.sim.current_token()
        assert tok, "nog geen service-token uitgegeven"
        return tok

    def fingerprint(self) -> str:
        return self.sim.fingerprint()

    def user(self, *roles: str, sub: str = "user-1", **kw: Any) -> Mapping[str, str]:
        return FreshAuth(lambda: self.sim.user_jwt(list(roles), sub=sub, **kw))

    def service(self, **kw: Any) -> Mapping[str, str]:
        return FreshAuth(lambda: self.sim.service_jwt(**kw))


@pytest.fixture
def fake_hub() -> FakeHub:
    return FakeHub()


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
    "TRUNCATE service_state, idempotency_record, outbox_event, vault_secret, audit_event, jwt_replay, index_source, "
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


def pair(client: Any, fake_hub: FakeHub) -> None:
    """Koppelt via de koppelcode + vingerafdruk (zoals de statuspagina) met de simulator."""
    outcome = client.app.state.enrollment.enroll_with_code(HUB_URL, PAIR_CODE, fake_hub.fingerprint())
    assert outcome.ok, outcome.code


@pytest.fixture
def paired_client(client: Any, fake_hub: FakeHub) -> Any:
    """App die via de koppelcode met de Hub-simulator gekoppeld én bevestigd is."""
    pair(client, fake_hub)
    assert client.app.state.hub_link.read()["confirmed_at"]
    return client


@pytest.fixture
def auth(fake_hub: FakeHub) -> Callable[..., Mapping[str, str]]:
    """Gebruikers-JWT (``token_use: user``) van de Hub-simulator; elke lezing een nieuwe ``jti``."""

    def _auth(*roles: str, sub: str = "user-1") -> Mapping[str, str]:
        return fake_hub.user(*roles, sub=sub)

    return _auth


@pytest.fixture
def svc(fake_hub: FakeHub) -> Callable[..., Mapping[str, str]]:
    """Dienst-JWT van de Hub (``token_use: hub_service``); elke lezing een nieuwe ``jti``."""

    def _svc(**kw: Any) -> Mapping[str, str]:
        return fake_hub.service(**kw)

    return _svc


def idem(headers: Mapping[str, str] | None = None, key: str | None = None, **extra: str) -> dict[str, str]:
    return {**(headers or {}), "Idempotency-Key": key or str(uuid.uuid4()), **extra}
