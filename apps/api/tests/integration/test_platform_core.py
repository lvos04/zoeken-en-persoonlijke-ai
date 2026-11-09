"""Platformkern (contract §3–§7) tegen PostgreSQL: gezondheid, koppelen, auth, idempotency, connector, events."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from search_ai.models import all_models
from tests.conftest import ENV_TOKEN, HUB_URL, FakeHub, idem

pytestmark = pytest.mark.integration

Auth = Callable[..., dict[str, str]]


def test_schema_has_no_drift_against_models(database_url: str) -> None:
    eng = create_engine(database_url)
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), all_models().metadata)
    eng.dispose()
    assert diff == []


def test_pgvector_dimension_enforced_and_roundtrip(database_url: str) -> None:
    eng = create_engine(database_url)
    with eng.connect() as c:
        assert c.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'")).scalar()
        assert c.execute(text("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector")).scalar() == pytest.approx(1.0)
    eng.dispose()


def test_health_ready_and_status_page_not_paired(client: Any) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    r = client.get("/readyz")
    assert r.status_code == 200, r.text
    names = {c["name"]: c["status"] for c in r.json()["checks"]}
    assert names == {"database": "ok", "migraties": "ok", "pgvector": "ok", "geheimen": "ok"}
    assert r.json()["hub_link"]["state"] == "nooit_gekoppeld"
    page = client.get("/")
    assert page.status_code == 200 and "niet gekoppeld" in page.text and 'lang="nl"' in page.text
    assert "default-src 'none'" in page.headers["Content-Security-Policy"]
    assert "Koppelcode" in page.text and "Hub-vingerafdruk" in page.text and "Spraak" in page.text


def test_unauthenticated_and_error_format(client: Any) -> None:
    r = client.get("/search/v1/sources")
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == "unauthorized" and body["message"] and uuid.UUID(body["trace_id"])
    assert r.headers["X-Trace-Id"] == body["trace_id"]
    assert body["details"]["reason"] == "not_linked"
    # niet gekoppeld → ook een geldig ogende JWT wordt geweigerd (geen gepinde sleutels = fail-closed)
    r = client.get("/connector/v1/discover", headers={"Authorization": "Bearer a.b.c"})
    assert r.status_code == 401 and r.json()["details"]["reason"] == "not_linked"


def test_enroll_with_env_token_follows_contract_1_2(app_factory: Callable[..., Any], fake_hub: FakeHub) -> None:
    from search_ai.worker import Worker

    client = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN, hub_key_fingerprint=fake_hub.fingerprint())
    w = Worker(client.app.state.settings, transport=fake_hub.transport)
    w.run_once()
    req = fake_hub.enroll_requests[-1]
    assert set(req) == {"app_id", "version", "base_url", "public_key_jwk"}  # (geheim zelf niet bewaard in de sim)
    assert req["app_id"] == "zoeken-en-persoonlijke-ai" and set(req["public_key_jwk"]) == {"kty", "crv", "x", "kid"}
    st = client.app.state.enrollment.public_state()
    assert st["state"] == "gekoppeld" and st["service_token_last4"] == fake_hub.issued_token[-4:]
    assert st["keyset_fingerprint"] == fake_hub.fingerprint() and st["via"] == "token"
    doc = client.app.state.hub_link.read()
    assert doc["confirmed_at"] and doc["first_linked_at"] == doc["linked_at"]
    # niets van de koppeling staat in de database (een DB-herstel brengt haar dus nooit terug)
    with client.app.state.db.sessions() as s:
        assert s.execute(text("SELECT count(*) FROM vault_secret")).scalar() == 0
        assert s.execute(text("SELECT count(*) FROM service_state WHERE key LIKE 'hub%'")).scalar() == 0
    # eenmalig: na ontkoppelen wordt hetzelfde omgevingstoken niet opnieuw geprobeerd
    client.app.state.enrollment.unlink()
    assert client.app.state.enrollment.enroll_with_env_token() is None


def test_jwt_roles_and_pins_survive_hub_outage(paired_client: Any, auth: Auth, svc: Auth, fake_hub: FakeHub) -> None:
    client = paired_client
    assert client.get("/connector/v1/discover", headers=auth("viewer")).status_code == 200
    r = client.get("/connector/v1/hub-link", headers=auth("viewer"))
    assert r.status_code == 200 and r.json()["state"] == "gekoppeld"
    # gepinde sleutels: de Hub tijdelijk weg verandert niets aan het vertrouwen (en is nooit een intrekking)
    fake_hub.up = False
    assert client.get("/connector/v1/discover", headers=auth("admin")).status_code == 200
    health = client.get("/connector/v1/health", headers=svc()).json()
    checks = {c["name"]: c["status"] for c in health["checks"]}
    assert checks["hub_sleutels"] == "healthy" and checks["hub_koppeling"] == "healthy"
    assert health["status"] == "healthy"
    assert not any(r["path"] == "/api/v1/auth/jwks" for r in fake_hub.all_requests)  # nooit een JWKS opgehaald


def test_idempotency_required_replay_and_conflict(paired_client: Any, auth: Auth, shares: Any) -> None:
    admin = auth("admin", sub="beheer")
    body = {"kind": "files", "name": "Docs", "allowed_paths": [str(shares.a)]}
    r = paired_client.post("/search/v1/sources", json=body, headers=admin)
    assert r.status_code == 400 and r.json()["code"] == "idempotency_key_required"
    key = str(uuid.uuid4())
    r1 = paired_client.post("/search/v1/sources", json=body, headers=idem(admin, key=key))
    r2 = paired_client.post("/search/v1/sources", json=body, headers=idem(admin, key=key))
    assert r1.status_code == r2.status_code == 201 and r1.json()["id"] == r2.json()["id"]
    assert r2.headers["Idempotent-Replay"] == "true"
    assert len(paired_client.get("/search/v1/sources", headers=admin).json()["items"]) == 1
    r3 = paired_client.post("/search/v1/sources", json={**body, "name": "Anders"}, headers=idem(admin, key=key))
    assert r3.status_code == 409 and r3.json()["code"] == "idempotency_key_conflict"


def test_connector_contract(paired_client: Any, auth: Auth, svc: Auth, shares: Any) -> None:
    d = paired_client.get("/connector/v1/discover", headers=svc()).json()
    assert d["app_id"] == "zoeken-en-persoonlijke-ai" and d["schema_version"] == "1.0"
    assert d["capabilities"] == ["read", "stream"] and "apply" in d["unavailable_capabilities"]
    for path in ("/connector/v1/propose", "/connector/v1/apply", "/connector/v1/rollback"):
        r = paired_client.post(path, headers=svc())
        assert r.status_code == 501 and r.json()["code"] == "capability_unavailable"
    paired_client.post(
        "/search/v1/sources",
        json={"kind": "files", "name": "Docs", "allowed_paths": [str(shares.a)]},
        headers=idem(auth("admin", sub="beheer")),
    )
    items = paired_client.get("/connector/v1/items", params={"type": "search.source"}, headers=svc()).json()
    assert len(items["items"]) == 1 and "allowed_paths" not in items["items"][0]
    r = paired_client.get("/connector/v1/items", params={"type": "search.document"}, headers=svc())
    assert r.status_code == 403
    r = paired_client.get("/connector/v1/items", params={"type": "voice.session"}, headers=svc())
    assert r.status_code == 501
    r = paired_client.get("/connector/v1/items", params={"type": "search.source"}, headers=auth("kind"))
    assert r.status_code == 403
    health = paired_client.get("/connector/v1/health", headers=svc()).json()
    checks = {c["name"]: c["status"] for c in health["checks"]}
    assert checks["ingest"] == "unknown" and checks["spraak"] == "unknown" and checks["pgvector"] == "healthy"
    assert health["status"] == "healthy"


def test_events_pushed_to_hub_inbox(paired_client: Any, auth: Auth, fake_hub: FakeHub, shares: Any) -> None:
    from search_ai.worker import Worker

    paired_client.post(
        "/search/v1/sources",
        json={"kind": "files", "name": "Docs", "allowed_paths": [str(shares.a)]},
        headers=idem(auth("admin", sub="beheer")),
    )
    w = Worker(paired_client.app.state.settings, transport=fake_hub.transport)
    w.push_step()
    types = [e["event_type"] for e in fake_hub.inbox]
    assert "search.source.created" in types
    ev = fake_hub.inbox[0]
    assert ev["source_app"] == "zoeken-en-persoonlijke-ai" and ev["schema_version"] == "1.0"
    assert set(ev) >= {"event_id", "source_event_id", "entity", "timestamp_utc", "trace_id", "actor", "classification"}
    w.push_step()  # niets dubbel
    assert len(fake_hub.inbox) == len(types)


def test_voice_capabilities_are_honest(paired_client: Any, auth: Auth) -> None:
    st = paired_client.get("/voice/v1/status", headers=auth("viewer")).json()
    assert set(st["capabilities"].values()) == {"capability_unavailable"}
    assert st["privacy"]["ruwe_audio_opslaan"] is False
    r = paired_client.post("/voice/v1/sessions", headers=auth("viewer"))
    assert r.status_code == 501
