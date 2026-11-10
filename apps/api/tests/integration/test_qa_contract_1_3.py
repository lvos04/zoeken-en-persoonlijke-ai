"""QA contract 1.3 (ADR-0017): gaten die de onafhankelijke review vond, met tests die zonder de reparatie falen.

- §5: bij 429 of 503 **nooit eerder** opnieuw dan ``Retry-After`` — ook bij het koppelen (503), het pollen van
  sleutelrotaties en het afleveren van de outbox (een 429/503 stopt de ronde).
- §7/RV12: een 401 van de inbox die een intrekking blijkt (``services/self`` ook 401) telt geen poging: het event
  wacht in de outbox op een nieuwe koppeling.
- §7 (C1.3-ZPA-05): ``service.key_rotation.applied`` ook als de achtergrondpoll van de **API** (onbekende ``kid``) de
  rotatie als eerste toepast; ``service.key_rotation.rejected`` één keer per geblokkeerde reeks, niet bij elke poll.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from search_ai.models import AuditEvent, OutboxEvent
from search_ai.timeutil import utcnow
from search_ai.worker import Worker
from tests.conftest import ENV_TOKEN, HUB_URL, FakeHub, idem
from tests.hubsim import hub_jwk

pytestmark = pytest.mark.integration

Auth = Callable[..., Mapping[str, str]]
ENROLL = "/api/v1/services/enroll"
INBOX = "/api/v1/events/inbox"
KEY_ROTATIONS = "/api/v1/auth/key-rotations"


def _worker(client: Any, fake_hub: FakeHub) -> Worker:
    return Worker(client.app.state.settings, transport=fake_hub.transport)


def _respond(path: str, status: int, headers: dict[str, str] | None = None) -> Any:
    def _o(req: httpx.Request) -> httpx.Response | None:
        if req.url.path == path:
            return httpx.Response(status, json={"code": "x"}, headers=headers or {})
        return None

    return _o


def _calls(fake_hub: FakeHub, path: str) -> int:
    return sum(1 for r in fake_hub.all_requests if r["path"] == path)


def _events(client: Any, event_type: str) -> list[OutboxEvent]:
    with client.app.state.db.sessions() as s:
        return list(s.scalars(select(OutboxEvent).where(OutboxEvent.event_type == event_type)))


def _audit(client: Any, action: str) -> int:
    with client.app.state.db.sessions() as s:
        return len(list(s.scalars(select(AuditEvent).where(AuditEvent.action == action))))


def _new_source(client: Any, auth: Auth, shares: Any, name: str) -> None:
    r = client.post(
        "/search/v1/sources",
        json={"kind": "files", "name": name, "allowed_paths": [str(shares.a)]},
        headers=idem(auth("admin", sub="beheer")),
    )
    assert r.status_code == 201, r.text


def _join_background_poll() -> None:
    for t in threading.enumerate():
        if t.name == "sleutelrotatie-poll":
            t.join(10)


# ------------------------------------------------------------------ §5 Retry-After
def test_enroll_503_met_retry_after_wacht_minstens_die_tijd(app_factory: Callable[..., Any], fake_hub: FakeHub) -> None:
    client = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    fake_hub.override = _respond(ENROLL, 503, {"Retry-After": "600"})
    w = _worker(client, fake_hub)
    w.startup()
    w.enroll_step(1000.0)
    assert w.state.next_enroll_at >= 1600.0  # zonder de reparatie: backoff van ±5 s
    doc = client.app.state.hub_link.read()
    assert doc["last_error"]["code"] == "hub_onbereikbaar" and doc["pending"]["kind"] == "token"
    w.enroll_step(1599.0)
    assert _calls(fake_hub, ENROLL) == 1


def test_enroll_503_zonder_retry_after_houdt_de_gewone_backoff(
    app_factory: Callable[..., Any], fake_hub: FakeHub
) -> None:
    client = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    fake_hub.override = _respond(ENROLL, 503)
    w = _worker(client, fake_hub)
    w.startup()
    w.enroll_step(1000.0)
    assert 1000.0 + 3.9 <= w.state.next_enroll_at <= 1000.0 + 6.1  # start 5 s met ±20 % jitter (§3.3)


def test_sleutelpoll_met_503_en_retry_after_pollt_niet_eerder(paired_client: Any, fake_hub: FakeHub) -> None:
    client = paired_client
    w = _worker(client, fake_hub)
    fake_hub.override = _respond(KEY_ROTATIONS, 503, {"Retry-After": "1800"})
    w.state.next_key_poll_at = 0
    w.link_step(100.0)
    assert w.state.next_key_poll_at >= 1900.0  # anders na hooguit 15 min (900 s)
    assert _calls(fake_hub, KEY_ROTATIONS) == 1  # 1800 s is langer dan binnen één aanroep gewacht wordt


def test_inbox_429_met_retry_after_stopt_de_ronde_en_wacht(
    paired_client: Any, fake_hub: FakeHub, auth: Auth, shares: Any
) -> None:
    client = paired_client
    w = _worker(client, fake_hub)
    w.push_step()  # het event van de koppeling eerst afleveren
    _new_source(client, auth, shares, "Een")
    _new_source(client, auth, shares, "Twee")
    with client.app.state.db.sessions() as s:
        pending = [e for e in s.scalars(select(OutboxEvent)) if e.delivery_status == "pending"]
    assert len(pending) >= 2
    before = _calls(fake_hub, INBOX)
    fake_hub.override = _respond(INBOX, 429, {"Retry-After": "120"})
    w.push_step()
    assert _calls(fake_hub, INBOX) - before == 1  # de rest van de ronde gaat niet meer naar de Hub
    with client.app.state.db.sessions() as s:
        failed = [e for e in s.scalars(select(OutboxEvent)) if e.attempts == 1]
    assert len(failed) == 1 and failed[0].delivery_status == "pending"
    assert failed[0].next_attempt_at >= utcnow() + timedelta(seconds=115)  # niet de 5 s-backoff


# ------------------------------------------------------------------ §7/RV12: intrekking telt geen poging
def test_401_van_de_inbox_door_intrekking_telt_geen_poging(
    paired_client: Any, fake_hub: FakeHub, auth: Auth, shares: Any
) -> None:
    client = paired_client
    w = _worker(client, fake_hub)
    w.push_step()
    _new_source(client, auth, shares, "Docs")
    fake_hub.sim.revoke()  # inbox én services/self geven nu 401
    w.push_step()
    doc = client.app.state.hub_link.read()
    assert doc["state"] == "ingetrokken"
    with client.app.state.db.sessions() as s:
        waiting = [e for e in s.scalars(select(OutboxEvent)) if e.delivery_status == "pending"]
    assert waiting and all(e.attempts == 0 and e.last_error is None for e in waiting)
    assert _audit(client, "hub.revoked") == 1  # wel één incident


def test_401_zonder_intrekking_telt_wel_een_poging(
    paired_client: Any, fake_hub: FakeHub, auth: Auth, shares: Any
) -> None:
    client = paired_client
    w = _worker(client, fake_hub)
    w.push_step()
    _new_source(client, auth, shares, "Docs")
    fake_hub.override = _respond(INBOX, 401)  # services/self blijft 200: geen intrekking
    w.push_step()
    assert client.app.state.hub_link.read()["state"] == "gekoppeld"
    with client.app.state.db.sessions() as s:
        tried = [e for e in s.scalars(select(OutboxEvent)) if e.attempts == 1]
    assert len(tried) == 1 and tried[0].last_error == "http_401"


# ------------------------------------------------------------------ §7: service.key_rotation.* uit elk proces
def test_rotatie_via_de_achtergrondpoll_van_de_api_wordt_vastgelegd(paired_client: Any, fake_hub: FakeHub) -> None:
    client = paired_client
    sim = fake_hub.sim
    new = Ed25519PrivateKey.generate()
    sim.rotations.append(
        {"sequence": 1, "statement": sim.rotation_statement(1, "jwt", [hub_jwk(new)], [], sim.jwt_key)}
    )
    # een JWT met de nieuwe sleutel: 401 unknown_key en een achtergrondpoll in de API, die de rotatie toepast
    r = client.get("/connector/v1/discover", headers={"Authorization": f"Bearer {sim.user_jwt(['admin'], key=new)}"})
    assert r.status_code == 401 and r.json()["details"]["reason"] == "unknown_key"
    _join_background_poll()
    assert client.app.state.hub_link.read()["pins"]["rotation_sequence"] == 1
    applied = _events(client, "service.key_rotation.applied")
    assert len(applied) == 1 and applied[0].actor_id == "api" and applied[0].payload["rotation_sequence"] == 1
    assert applied[0].entity_type == "service" and applied[0].entity_id == "zoeken-en-persoonlijke-ai"
    assert _audit(client, "hub.key_rotated") == 1
    # de worker ziet daarna niets nieuws: geen tweede event
    w = _worker(client, fake_hub)
    w.state.next_key_poll_at = 0
    w.link_step(1.0)
    assert len(_events(client, "service.key_rotation.applied")) == 1


def test_geweigerde_rotatie_wordt_een_keer_gemeld_niet_bij_elke_poll(paired_client: Any, fake_hub: FakeHub) -> None:
    client = paired_client
    sim = fake_hub.sim
    stranger = Ed25519PrivateKey.generate()
    sim.rotations.append(
        {"sequence": 1, "statement": sim.rotation_statement(1, "jwt", [hub_jwk(stranger)], [], stranger)}
    )
    w = _worker(client, fake_hub)
    for now in (1.0, 2000.0, 4000.0):  # drie polls op dezelfde blokkade
        w.state.next_key_poll_at = 0
        w.link_step(now)
    doc = client.app.state.hub_link.read()
    assert doc["key_rotation_blocked_sequence"] == 1 and doc["last_error"]["code"] == "sleutelrotatie_ongeldig"
    rejected = _events(client, "service.key_rotation.rejected")
    assert len(rejected) == 1 and rejected[0].payload == {"rotation_sequence": 1, "ernst": "hoog"}
    assert _audit(client, "hub.key_rotation_rejected") == 1
    # ook de achtergrondpoll van de API meldt dezelfde blokkade niet opnieuw
    client.app.state.key_rotation_trigger()
    _join_background_poll()
    assert len(_events(client, "service.key_rotation.rejected")) == 1
