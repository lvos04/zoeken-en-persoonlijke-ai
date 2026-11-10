"""Contract 1.3 §6/§6.2/§7 tegen PostgreSQL (C1.3-ZPA-05, C1.3-ZPA-10): connector-health met vaste checknamen en
totaal = slechtste check, versheid (``observed_at``, ``max_age``, ``meting_verouderd``) en de vaste ``service.*``-events
die de (strengere) inbox van de simulator zonder weigering aanneemt."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import update

from search_ai import APP_ID
from search_ai.health import WORKER_HEARTBEAT_KEY, worker_max_age_s
from search_ai.models import OutboxEvent, ServiceState
from search_ai.timeutil import iso, utcnow
from search_ai.worker import Worker
from tests.conftest import ENV_TOKEN, HUB_URL, FakeHub, idem


def _health(client: Any, fake_hub: FakeHub) -> dict[str, Any]:
    r = client.get("/connector/v1/health", headers=fake_hub.service())
    assert r.status_code == 200, r.text
    return dict(r.json())


def _checks(h: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in h["checks"]}


def test_vaste_namen_observed_at_en_totaal_is_slechtste(paired_client: Any, fake_hub: FakeHub) -> None:
    Worker(paired_client.app.state.settings).heartbeat(0)
    h = _health(paired_client, fake_hub)
    checks = _checks(h)
    assert {"database", "worker", "jobs", "hub_link"} <= set(checks)
    assert all(c["observed_at"] and c["name"] and "detail" in c for c in h["checks"])
    assert h["status"] == "healthy" and h["reason_code"] is None and h["observed_at"]
    assert set(checks["jobs"]["detail"]) >= {"wachtrij", "bezig", "laatste_job_klaar"}
    # één dead-letter → jobs degraded → totaal degraded (slechtste), met de reden van die check
    with paired_client.app.state.db.sessions() as s:
        from search_ai import events

        events.emit_service(s, "service.token.rotated", actor_type="system", actor_id="t", trace_id=_tid())
        s.flush()
        s.execute(update(OutboxEvent).values(delivery_status="dead_letter"))
        s.commit()
    h = _health(paired_client, fake_hub)
    assert (
        _checks(h)["jobs"]["status"] == "degraded" and h["status"] == "degraded" and h["reason_code"] == "dead_letter"
    )


def test_verouderde_hartslag_is_unknown_met_meting_verouderd(paired_client: Any, fake_hub: FakeHub) -> None:
    settings = paired_client.app.state.settings
    oud = utcnow() - timedelta(seconds=worker_max_age_s(settings) + 30)
    with paired_client.app.state.db.sessions() as s:
        s.merge(ServiceState(key=WORKER_HEARTBEAT_KEY, value={"laatste_hartslag": iso(oud), "stappen_mislukt": 0}))
        s.commit()
    h = _health(paired_client, fake_hub)
    w = _checks(h)["worker"]
    assert w["status"] == "unknown" and w["reason_code"] == "meting_verouderd"
    assert w["observed_at"] == iso(oud) and w["detail"]["laatste_hartslag"] == iso(oud)  # laatst bekend, niet actueel
    assert h["status"] == "unknown" and h["reason_code"] == "meting_verouderd"  # onbekend is nooit groen


def test_discover_noemt_max_age_per_check_en_itemtype(paired_client: Any, fake_hub: FakeHub) -> None:
    d = paired_client.get("/connector/v1/discover", headers=fake_hub.service()).json()
    limits = d["limits"]["max_age_s"]
    assert limits["health"] == 900 and limits["worker"] == worker_max_age_s(paired_client.app.state.settings)
    assert {"search.source", "search.index_run", "search.privacy_summary"} <= set(limits)
    items = paired_client.get(
        "/connector/v1/items", params={"type": "search.privacy_summary"}, headers=fake_hub.service()
    ).json()
    assert items["observed_at"] and items["items"][0]["observed_at"]


def test_onbekend_itemtype_is_422_validation_error(paired_client: Any, fake_hub: FakeHub) -> None:
    r = paired_client.get("/connector/v1/items", params={"type": "bestaat.niet"}, headers=fake_hub.service())
    assert r.status_code == 422 and r.json()["code"] == "validation_error"


def test_service_events_komen_ongeweigerd_door_de_inbox(
    app_factory: Any, fake_hub: FakeHub, auth: Any, shares: Any
) -> None:
    """Koppelen via env-token, een bron maken en ontkoppelen: de worker pusht ``service.hub_link.linked`` en
    ``search.source.created`` met de juiste entiteit; de simulator (inboxgrenzen van §7) weigert niets."""
    client = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN, hub_key_fingerprint=fake_hub.fingerprint())
    w = Worker(client.app.state.settings, transport=fake_hub.transport)
    w.run_once()
    assert client.app.state.hub_link.read()["state"] == "gekoppeld"
    r = client.post(
        "/search/v1/sources",
        json={"kind": "files", "name": "Docs", "allowed_paths": [str(shares.a)]},
        headers=idem(auth("admin", sub="beheer")),
    )
    assert r.status_code == 201, r.text
    w.push_step()
    types = [e["event_type"] for e in fake_hub.inbox]
    assert "service.hub_link.linked" in types and "search.source.created" in types
    linked = next(e for e in fake_hub.inbox if e["event_type"] == "service.hub_link.linked")
    assert linked["entity"] == {"type": "service", "id": APP_ID} and linked["payload"]["via"] == "token"
    assert not any(t.startswith(("service.hub_link.paired", "service.hub_key.")) for t in types)  # oude namen
    assert fake_hub.sim.inbox_rejections == []


def test_ongeldig_event_gaat_niet_over_het_lan_maar_direct_naar_dead_letter(
    paired_client: Any, fake_hub: FakeHub
) -> None:
    with paired_client.app.state.db.sessions() as s:
        from search_ai import events

        ev = events.emit_service(s, "service.token.rotated", actor_type="system", actor_id="t", trace_id=_tid())
        s.flush()
        ev.entity_type = "Ongeldig Type"  # buiten de grenzen van de inbox (§7)
        s.commit()
    w = Worker(paired_client.app.state.settings, transport=fake_hub.transport)
    before = len(fake_hub.sim.requests)
    w.push_step()
    assert not any(r["path"] == "/api/v1/events/inbox" for r in fake_hub.sim.requests[before:])
    with paired_client.app.state.db.sessions() as s:
        row = s.query(OutboxEvent).one()
        assert row.delivery_status == "dead_letter" and str(row.last_error).startswith("ongeldig_event")


def _tid() -> str:
    import uuid

    return str(uuid.uuid4())


def test_geen_share_is_een_open_instelling_geen_gezondheidscheck(
    app_factory: Any, fake_hub: FakeHub, tmp_path: Any
) -> None:
    """Nog geen share is een gate (ONTWERPTAAL §4.1), zoals "Niet ingesteld" op de statuspagina: geen check die het
    totaal verslechtert, wel zichtbaar in discover. Een ingestelde share die genegeerd wordt, is wél een check."""
    from tests.conftest import pair

    client = app_factory(read_roots=())
    pair(client, fake_hub)
    Worker(client.app.state.settings).heartbeat(0)
    h = _health(client, fake_hub)
    assert "leeswortels" not in _checks(h) and h["status"] == "healthy"
    disc = client.get("/connector/v1/discover", headers=fake_hub.service()).json()
    assert disc["instellingen"]["leeswortels"] == "niet_ingesteld"
    kapot = app_factory(read_roots=("/etc",), data_dir=tmp_path / "ander")  # systeemmap: genegeerd met waarschuwing
    code = fake_hub.sim.add_pairing_code()
    assert kapot.app.state.enrollment.enroll_with_code(HUB_URL, code, fake_hub.fingerprint()).ok
    Worker(kapot.app.state.settings).heartbeat(0)
    h = _health(kapot, fake_hub)
    assert _checks(h)["leeswortels"]["status"] == "degraded" and h["status"] == "degraded"
