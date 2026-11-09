"""Platformkern (contract §3–§7) tegen PostgreSQL: gezondheid, koppelen, auth, idempotency, connector, events."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from search_ai.models import all_models
from tests.conftest import ENV_TOKEN, HUB_URL, PAIR_CODE, FakeHub, idem

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
    page = client.get("/")
    assert page.status_code == 200 and "niet gekoppeld" in page.text and 'lang="nl"' in page.text
    assert "default-src 'none'" in page.headers["Content-Security-Policy"]
    assert "Koppelcode" in page.text and "Spraak" in page.text


def test_unauthenticated_and_error_format(client: Any) -> None:
    r = client.get("/search/v1/sources")
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == "unauthorized" and body["message"] and uuid.UUID(body["trace_id"])
    assert r.headers["X-Trace-Id"] == body["trace_id"]
    # niet gekoppeld → ook een geldig ogende JWT wordt geweigerd (geen JWKS = fail-closed)
    assert client.get("/connector/v1/discover", headers={"Authorization": "Bearer a.b.c"}).status_code == 401


def test_enroll_with_env_token_uses_real_hub_format(app_factory: Callable[..., Any], fake_hub: FakeHub) -> None:
    from search_ai.worker import Worker

    client = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    w = Worker(client.app.state.settings, transport=fake_hub.transport)
    w.enroll_step(0)
    req = fake_hub.enroll_requests[-1]
    assert set(req) == {"app_id", "enrollment_token", "version", "public_key_jwk"}
    assert req["app_id"] == "zoeken-en-persoonlijke-ai" and "d" not in req["public_key_jwk"]
    with client.app.state.db.sessions() as s:
        st = client.app.state.enrollment.public_state(s)
    assert st["status"] == "gekoppeld" and st["service_token_last4"] == fake_hub.issued_token[-4:]
    # het token is eenmalig: na ontkoppelen wordt hetzelfde omgevingstoken niet opnieuw geprobeerd
    with client.app.state.db.sessions() as s:
        client.app.state.enrollment.unpair(s)
        s.commit()
        assert client.app.state.enrollment.enroll_with_env_token(s) is None


def test_enroll_rejected_and_hub_offline(app_factory: Callable[..., Any], fake_hub: FakeHub) -> None:
    client = app_factory(hub_url=HUB_URL, enrollment_token="enr_verkeerd-token-000000000000")
    with client.app.state.db.sessions() as s:
        out = client.app.state.enrollment.enroll_with_env_token(s)
        s.commit()
    assert out is not None and not out.ok and out.reason == "hub_weigerde_token" and not out.retryable
    fake_hub.up = False
    with client.app.state.db.sessions() as s:
        out = client.app.state.enrollment.enroll_with_code(s, HUB_URL, PAIR_CODE)
        s.commit()
    assert out.reason == "hub_onbereikbaar" and out.retryable
    with client.app.state.db.sessions() as s:
        assert client.app.state.enrollment.public_state(s)["status"] == "koppelen_mislukt"


def test_pair_via_status_page_with_csrf_and_unpair_code(client: Any, fake_hub: FakeHub, caplog: Any) -> None:
    page = client.get("/")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    # zonder geldig CSRF-token → geweigerd
    r = client.post("/koppelen", data={"csrf": "fout", "hub_url": HUB_URL, "code": PAIR_CODE}, follow_redirects=False)
    assert r.status_code == 303 and "ongeldig_formulier" in r.headers["location"]
    # loopback-Hub geweigerd (wijst naar de container zelf)
    r = client.post("/koppelen", data={"csrf": csrf, "hub_url": "http://127.0.0.1:30400", "code": PAIR_CODE}, follow_redirects=False)
    assert "ongeldig_hub_adres" in r.headers["location"]
    r = client.post("/koppelen", data={"csrf": csrf, "hub_url": HUB_URL, "code": PAIR_CODE}, follow_redirects=False)
    assert "melding=gekoppeld" in r.headers["location"]
    assert fake_hub.enroll_requests[-1]["pairing_code"] == PAIR_CODE
    assert "gekoppeld" in client.get("/").text
    # ontkoppelen vereist de lokale ontkoppelcode
    page = client.get("/")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
    r = client.post("/ontkoppelen", data={"csrf": csrf, "unpair_code": "XXXX-XXXX-XXXX"}, follow_redirects=False)
    assert "ontkoppelcode_onjuist" in r.headers["location"]


def test_jwt_roles_and_fail_closed_when_hub_disappears(paired_client: Any, auth: Auth, fake_hub: FakeHub) -> None:
    client = paired_client
    assert client.get("/connector/v1/discover", headers=auth("viewer")).status_code == 200
    assert client.get("/search/v1/enrollment", headers=auth("viewer")).status_code == 403
    assert client.get("/search/v1/enrollment", headers=auth("admin")).json()["status"] == "gekoppeld"
    # JWKS veroudert en de Hub is weg: na ttl + max_stale wordt elke JWT geweigerd
    fake_hub.up = False
    jwks = client.app.state.jwks
    jwks._fetched_at = jwks._clock() - (jwks.ttl_s + jwks.max_stale_s + 1)  # noqa: SLF001 - tijdsprong in de test
    r = client.get("/connector/v1/discover", headers=auth("admin"))
    assert r.status_code == 401 and r.json()["details"]["reason"] == "jwks_niet_beschikbaar"
    health = client.get("/connector/v1/health", headers={"Authorization": f"Bearer {fake_hub.issued_token}"}).json()
    assert {c["name"]: c["status"] for c in health["checks"]}["hub_jwks"] == "down"
    assert health["status"] == "degraded"


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


def test_connector_contract(paired_client: Any, auth: Auth, fake_hub: FakeHub, shares: Any) -> None:
    svc = {"Authorization": f"Bearer {fake_hub.issued_token}"}
    d = paired_client.get("/connector/v1/discover", headers=svc).json()
    assert d["app_id"] == "zoeken-en-persoonlijke-ai" and d["schema_version"] == "1.0"
    assert d["capabilities"] == ["read", "stream"] and "apply" in d["unavailable_capabilities"]
    for path in ("/connector/v1/propose", "/connector/v1/apply", "/connector/v1/rollback"):
        r = paired_client.post(path, headers=svc)
        assert r.status_code == 501 and r.json()["code"] == "capability_unavailable"
    paired_client.post(
        "/search/v1/sources",
        json={"kind": "files", "name": "Docs", "allowed_paths": [str(shares.a)]},
        headers=idem(auth("admin", sub="beheer")),
    )
    items = paired_client.get("/connector/v1/items", params={"type": "search.source"}, headers=svc).json()
    assert len(items["items"]) == 1 and "allowed_paths" not in items["items"][0]
    r = paired_client.get("/connector/v1/items", params={"type": "search.document"}, headers=svc)
    assert r.status_code == 403
    r = paired_client.get("/connector/v1/items", params={"type": "voice.session"}, headers=svc)
    assert r.status_code == 501
    r = paired_client.get("/connector/v1/items", params={"type": "search.source"}, headers=auth("kind"))
    assert r.status_code == 403
    health = paired_client.get("/connector/v1/health", headers=svc).json()
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
