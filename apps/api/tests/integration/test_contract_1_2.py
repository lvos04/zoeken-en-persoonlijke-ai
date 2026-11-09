"""Contract 1.2 tegen PostgreSQL + de Hub-simulator: koppelen (incl. herhaling na een crash en alle foutcodes van §3.3),
herstart halverwege, statuspagina (§3.7), ontkoppelen/intrekken/rotatie (§3.8), sleutelrotatie (§4.5), replaycache
(§4.6), bodygrens (§5) en de overzetting van de 1.1-koppeling uit de database (§3.5)."""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine, select, text

from search_ai.enrollment import migrate_legacy_link
from search_ai.models import AuditEvent, JwtReplay, OutboxEvent, ServiceState
from search_ai.security.auth import sha256_hex
from search_ai.timeutil import iso, utcnow
from search_ai.worker import Worker
from tests.conftest import ENV_TOKEN, HUB_URL, PAIR_CODE, FakeHub, idem, pair
from tests.hubsim import hub_jwk

pytestmark = pytest.mark.integration

Auth = Callable[..., Mapping[str, str]]
ENROLL = "/api/v1/services/enroll"


def worker(client: Any, fake_hub: FakeHub) -> Worker:
    return Worker(client.app.state.settings, transport=fake_hub.transport)


def enroll_calls(fake_hub: FakeHub) -> int:
    return sum(1 for r in fake_hub.all_requests if r["path"] == ENROLL)


def respond(path: str, status: int, body: Any = None, headers: dict[str, str] | None = None) -> Any:
    def _o(req: httpx.Request) -> httpx.Response | None:
        if req.url.path == path:
            return httpx.Response(status, json=body or {"code": "x"}, headers=headers or {})
        return None

    return _o


def audit_actions(client: Any) -> list[str]:
    with client.app.state.db.sessions() as s:
        return [a.action for a in s.scalars(select(AuditEvent).order_by(AuditEvent.at))]


# ------------------------------------------------------------------ koppelen met het env-token (§3.3/§3.6)
def test_allowed_replay_after_lost_answer_and_worker_restart(
    app_factory: Callable[..., Any], fake_hub: FakeHub
) -> None:
    client = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN, hub_key_fingerprint=fake_hub.fingerprint())
    fake_hub.sim.lose_enroll_response = True  # de Hub verwerkt het verzoek, maar het antwoord gaat verloren
    w = worker(client, fake_hub)
    w.run_once()
    doc = client.app.state.hub_link.read()
    assert doc["state"] == "nooit_gekoppeld" and doc["pending"]["kind"] == "token"
    assert doc["pending"]["secret_sha256"] == sha256_hex(ENV_TOKEN) and ENV_TOKEN not in json.dumps(doc)
    assert doc["last_error"]["code"] == "hub_onbereikbaar"
    # 'herstart' van de worker: zelfde env-token → stap 2 opnieuw, de Hub staat de herhaling toe
    w2 = worker(client, fake_hub)
    w2.run_once()
    doc = client.app.state.hub_link.read()
    assert doc["state"] == "gekoppeld" and doc["pending"] is None and doc["confirmed_at"]
    assert len(fake_hub.sim.issued) == 2  # eerste (ongebruikte) token is door de Hub ingetrokken
    assert fake_hub.sim._valid(fake_hub.sim.issued[0]) is None and doc["service_token"] == fake_hub.sim.issued[1]
    assert "hub.pair" in audit_actions(client)


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "hub_weigerde"), (413, "hub_weigerde"), (422, "hub_weigerde"), (404, "enroll_endpoint_ontbreekt")],
)
def test_definitive_failures_are_never_retried(
    app_factory: Callable[..., Any], fake_hub: FakeHub, status: int, code: str
) -> None:
    client = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    fake_hub.override = respond(ENROLL, status)
    w = worker(client, fake_hub)
    w.run_once()
    doc = client.app.state.hub_link.read()
    assert doc["last_error"]["code"] == code and doc["pending"] is None
    assert doc["used_enrollment_token_sha256"] == [sha256_hex(ENV_TOKEN)]
    w.state.next_enroll_at = 0
    w.run_once()
    worker(client, fake_hub).run_once()  # ook niet na een herstart
    assert enroll_calls(fake_hub) == 1


def test_unknown_token_gets_401_invalid_enrollment_from_the_sim(
    app_factory: Callable[..., Any], fake_hub: FakeHub
) -> None:
    other = "enr_" + "Q" * 43
    client = app_factory(hub_url=HUB_URL, enrollment_token=other)
    worker(client, fake_hub).run_once()
    doc = client.app.state.hub_link.read()
    assert doc["last_error"]["code"] == "hub_weigerde" and sha256_hex(other) in doc["used_enrollment_token_sha256"]
    page = client.get("/").text
    assert "De Hub weigerde het koppeltoken" in page


def test_rate_limited_waits_for_retry_after(app_factory: Callable[..., Any], fake_hub: FakeHub) -> None:
    client = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    fake_hub.override = respond(ENROLL, 429, {"code": "rate_limited"}, {"Retry-After": "600"})
    w = worker(client, fake_hub)
    w.startup()
    w.enroll_step(1000.0)
    assert w.state.next_enroll_at >= 1600.0
    doc = client.app.state.hub_link.read()
    assert doc["last_error"]["code"] == "rate_limited" and doc["pending"]["kind"] == "token"
    assert doc["used_enrollment_token_sha256"] == []  # 429 is geen definitieve weigering
    w.enroll_step(1599.0)
    assert enroll_calls(fake_hub) == 1  # niet eerder dan Retry-After


def test_sim_rate_limit_after_ten_failures_from_one_ip(fake_hub: FakeHub) -> None:
    for _ in range(10):
        status, _h, body = fake_hub.sim.handle(
            "POST",
            ENROLL,
            "",
            {},
            json.dumps(
                {
                    "app_id": "zoeken-en-persoonlijke-ai",
                    "enrollment_token": "enr_" + "W" * 43,
                    "version": "0.1.0",
                    "base_url": None,
                    "public_key_jwk": {k: v for k, v in hub_jwk(Ed25519PrivateKey.generate()).items() if k == "x"}
                    | {"kty": "OKP", "crv": "Ed25519"},
                }
            ).encode(),
            "192.0.2.99",
        )
        assert status == 401 and body["code"] == "invalid_enrollment"
    status, headers, body = fake_hub.sim.handle("POST", ENROLL, "", {}, b"{}", "192.0.2.99")
    assert status == 429 and body["code"] == "rate_limited" and int(headers["Retry-After"]) > 0


def test_storing_backs_off_and_gives_up_after_60_minutes(app_factory: Callable[..., Any], fake_hub: FakeHub) -> None:
    client = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    fake_hub.up = False
    w = worker(client, fake_hub)
    w.startup()
    w.enroll_step(0.0)
    first = w.state.next_enroll_at
    assert 3.9 <= first <= 6.1  # start 5 s (± jitter)
    w.enroll_step(first)
    assert w.state.next_enroll_at - first >= 7.9  # verdubbeld
    # 61 minuten na de eerste poging: definitief, zonder nog een verzoek
    client.app.state.hub_link.update(
        lambda d: {**d, "pending": {**d["pending"], "started_at": iso(utcnow() - timedelta(minutes=61))}}
    )
    fake_hub.up = True
    calls = enroll_calls(fake_hub)
    w.enroll_step(10_000.0)
    doc = client.app.state.hub_link.read()
    assert doc["last_error"]["code"] == "token_verlopen_na_60_min" and doc["pending"] is None
    assert enroll_calls(fake_hub) == calls and sha256_hex(ENV_TOKEN) in doc["used_enrollment_token_sha256"]


def test_restart_interrupts_pending_code_and_foreign_pending_token(
    app_factory: Callable[..., Any], fake_hub: FakeHub
) -> None:
    client = app_factory()
    store = client.app.state.hub_link
    store.update(
        lambda d: {
            **d,
            "pending": {
                "kind": "koppelcode",
                "secret_sha256": "a" * 64,
                "hub_url": HUB_URL,
                "started_at": "t",
                "attempts": 1,
            },
        }
    )
    client.app.state.enrollment.on_api_start()  # zoals bij een herstart van het API-proces
    doc = store.read()
    assert doc["pending"] is None and doc["last_error"]["code"] == "koppelen_onderbroken"
    # pending env-token, maar na de herstart een ander (of geen) token → onderbroken
    store.update(
        lambda d: {
            **d,
            "pending": {
                "kind": "token",
                "secret_sha256": "b" * 64,
                "hub_url": HUB_URL,
                "started_at": "t",
                "attempts": 1,
            },
            "last_error": None,
        }
    )
    w = worker(client, fake_hub)
    w.startup()
    doc = store.read()
    assert doc["pending"] is None and doc["last_error"]["code"] == "koppelen_onderbroken"


def test_new_env_token_relinks_and_failed_attempt_keeps_existing_link(
    app_factory: Callable[..., Any], fake_hub: FakeHub
) -> None:
    client = app_factory()
    pair(client, fake_hub)
    before = client.app.state.hub_link.read()
    # mislukte nieuwe poging (onbekend token): bestaande koppeling ongemoeid
    bad = app_factory(hub_url=HUB_URL, enrollment_token="enr_" + "Z" * 43, data_dir=client.app.state.settings.data_dir)
    worker(bad, fake_hub).run_once()
    after = client.app.state.hub_link.read()
    assert after["state"] == "gekoppeld" and after["service_token"] == before["service_token"]
    assert after["pins"] == before["pins"] and after["last_error"]["code"] == "hub_weigerde"
    # nieuw, geldig env-token: opnieuw koppelen ook al is de dienst gekoppeld (§3.8); service_id blijft gelijk
    fake_hub.sim.jwt_key = Ed25519PrivateKey.generate()  # bv. een opnieuw geïnstalleerde Hub
    token = fake_hub.sim.add_enrollment_token()
    good = app_factory(hub_url=HUB_URL, enrollment_token=token, data_dir=client.app.state.settings.data_dir)
    worker(good, fake_hub).run_once()
    new = client.app.state.hub_link.read()
    assert new["state"] == "gekoppeld" and new["service_token"] != before["service_token"]
    assert new["previous_pins"] == before["pins"] and new["service_id"] == before["service_id"]
    assert new["first_linked_at"] == before["first_linked_at"] and new["via"] == "token"
    assert client.get("/connector/v1/discover", headers=fake_hub.user("admin")).status_code == 200


def test_link_confirmed_later_when_hub_drops_after_enroll(app_factory: Callable[..., Any], fake_hub: FakeHub) -> None:
    client = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    fake_hub.override = lambda r: (
        httpx.Response(503, json={"code": "service_unavailable"}) if r.url.path == "/api/v1/services/self" else None
    )
    w = worker(client, fake_hub)
    w.run_once()
    doc = client.app.state.hub_link.read()
    assert doc["state"] == "gekoppeld" and doc["confirmed_at"] is None
    health = client.get("/connector/v1/health", headers=fake_hub.service()).json()
    assert {c["name"]: c["status"] for c in health["checks"]}["hub_koppeling"] == "degraded"
    fake_hub.override = None
    w.state.next_confirm_at = 0
    w.link_step(100.0)
    assert client.app.state.hub_link.read()["confirmed_at"]


# ------------------------------------------------------------------ statuspagina (§3.7)
def _csrf(client: Any) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text)
    assert m, "geen koppelformulier"
    return m.group(1)


def _post(client: Any, csrf: str, **form: str) -> str:
    # veldnamen van het Peil-sjabloon (hub_adres, koppelcode, hub_vingerafdruk)
    data = {
        "csrf": csrf,
        "hub_adres": form.get("hub_url", HUB_URL),
        "koppelcode": form.get("code", PAIR_CODE),
        "hub_vingerafdruk": form.get("fingerprint", ""),
    }
    r = client.post("/koppelen", data=data, follow_redirects=False)
    assert r.status_code == 303
    return str(r.headers["location"])


def test_status_page_pairing_rules(client: Any, fake_hub: FakeHub) -> None:
    csrf = _csrf(client)
    fp = fake_hub.fingerprint()
    assert "ongeldig_formulier" in _post(client, "fout", fingerprint=fp)
    r = client.post(
        "/koppelen",
        data={"csrf": csrf, "hub_adres": HUB_URL, "koppelcode": PAIR_CODE, "hub_vingerafdruk": fp},
        headers={"Origin": "http://elders.example"},
        follow_redirects=False,
    )
    assert "ongeldig_formulier" in r.headers["location"]
    assert "ongeldig_hub_adres" in _post(client, csrf, hub_url="http://169.254.169.254", fingerprint=fp)
    assert "koppelcode_ongeldig" in _post(client, csrf, code="KORT", fingerprint=fp)
    assert enroll_calls(fake_hub) == 0  # ongeldige invoer gaat nooit naar de Hub
    # vingerafdruk is verplicht en moet kloppen; bij een fout wordt niets bewaard (ook het Hub-adres niet)
    csrf = _csrf(client)
    assert "vingerafdruk_ongeldig" in _post(client, csrf)
    loc = _post(client, csrf, fingerprint="MEGG-8GFG-2E4Y-K2RQ")
    assert "hub_vingerafdruk_onjuist" in loc
    doc = client.app.state.hub_link.read()
    assert doc["state"] == "nooit_gekoppeld" and doc["hub_url"] is None and doc["pins"] is None
    assert doc["pending"] is None and doc["last_error"]["code"] == "hub_vingerafdruk_onjuist"


def test_status_page_rate_limit_per_source_ip(client: Any, fake_hub: FakeHub) -> None:
    csrf = _csrf(client)
    for _ in range(5):
        assert "koppelcode_ongeldig" in _post(client, csrf, code="X", fingerprint=fake_hub.fingerprint())
    assert "te_veel_pogingen" in _post(client, csrf, fingerprint=fake_hub.fingerprint())
    assert enroll_calls(fake_hub) == 0


def test_pair_throttle_per_ip_and_total() -> None:
    from search_ai.status_page import PairThrottle

    t = PairThrottle()
    assert all(t.allow("10.0.0.1", now=0) for _ in range(5)) and not t.allow("10.0.0.1", now=1)
    assert all(t.allow(f"10.0.1.{i}", now=2) for i in range(15)) and not t.allow("10.0.2.1", now=3)  # 20 totaal
    assert t.allow("10.0.0.1", now=900)  # venster van 15 min voorbij


def test_after_first_link_status_page_never_pairs_again(client: Any, fake_hub: FakeHub, svc: Auth) -> None:
    csrf = _csrf(client)
    assert "melding=gekoppeld" in _post(client, csrf, fingerprint=fake_hub.fingerprint())
    page = client.get("/").text
    assert 'action="/koppelen"' not in page and "via de Hub" in page and fake_hub.fingerprint() in page
    assert "ontkoppelcode" not in page.lower()
    calls = enroll_calls(fake_hub)
    assert "al_gekoppeld" in _post(client, csrf, fingerprint=fake_hub.fingerprint())
    assert client.post("/ontkoppelen", data={"csrf": csrf}).status_code in (404, 405)
    # ook na ontkoppelen door de Hub: geen formulier (first_linked_at blijft staan)
    assert client.post("/connector/v1/hub-link/unlink", headers=idem(svc())).status_code == 200
    page = client.get("/").text
    assert 'action="/koppelen"' not in page and "ontkoppeld" in page
    assert "al_gekoppeld" in _post(client, csrf, fingerprint=fake_hub.fingerprint())
    assert enroll_calls(fake_hub) == calls
    assert "hub.pair" in audit_actions(client)


# ------------------------------------------------------------------ connector hub-link + ontkoppelen (§3.8/§6)
def test_hub_link_endpoint_is_whitelist_with_pinned_kids(
    paired_client: Any, auth: Auth, svc: Auth, fake_hub: FakeHub
) -> None:
    body = paired_client.get("/connector/v1/hub-link", headers=svc()).json()
    assert body["state"] == "gekoppeld" and body["keyset_fingerprint"] == fake_hub.fingerprint()
    assert body["pinned_kids"] == {"jwt": [fake_hub.sim.kid()], "policy": [fake_hub.sim.kid(fake_hub.sim.policy_key)]}
    assert body["rotation_sequence"] == 0 and body["service_token_last4"] == fake_hub.issued_token[-4:]
    assert fake_hub.issued_token not in json.dumps(body) and "pending" not in body and "service_token" not in body
    assert paired_client.get("/connector/v1/hub-link", headers=auth("viewer")).status_code == 200
    assert paired_client.get("/connector/v1/hub-link", headers=auth("kind")).status_code == 403
    ready = paired_client.get("/readyz").json()
    assert ready["hub_link"]["state"] == "gekoppeld" and fake_hub.issued_token not in json.dumps(ready)


def test_unlink_by_hub_then_every_hub_jwt_refused(paired_client: Any, auth: Auth, svc: Auth) -> None:
    client = paired_client
    assert client.post("/connector/v1/hub-link/unlink", headers=svc()).status_code == 400  # Idempotency-Key verplicht
    assert client.post("/connector/v1/hub-link/unlink", headers=idem(auth("viewer"))).status_code == 403
    r = client.post("/connector/v1/hub-link/unlink", headers=idem(svc()))
    assert r.status_code == 200 and r.json()["state"] == "ontkoppeld"
    doc = client.app.state.hub_link.read()
    assert doc["service_token"] is None and doc["pins"] is None and doc["previous_pins"]
    for hdr in (auth("admin"), svc()):
        r = client.get("/connector/v1/discover", headers=hdr)
        assert r.status_code == 401 and r.json()["details"]["reason"] == "not_linked"
    # herhaalde unlink (antwoord kwijt bij de Hub) → 401 not_linked: telt voor de Hub als geslaagd
    r = client.post("/connector/v1/hub-link/unlink", headers=idem(svc()))
    assert r.status_code == 401 and r.json()["details"]["reason"] == "not_linked"
    with client.app.state.db.sessions() as s:
        types = list(s.scalars(select(OutboxEvent.event_type)))
    assert "service.hub_link.unlinked" in types and "hub.unlink" in audit_actions(client)


def test_unlink_by_admin_user(paired_client: Any, auth: Auth) -> None:
    r = paired_client.post("/connector/v1/hub-link/unlink", headers=idem(auth("admin", sub="beheer")))
    assert r.status_code == 200 and r.json()["state"] == "ontkoppeld"


# ------------------------------------------------------------------ intrekken (§3.8, EX16)
def test_revocation_detected_only_when_services_self_also_401(
    paired_client: Any, auth: Auth, fake_hub: FakeHub, shares: Any
) -> None:
    client = paired_client
    w = worker(client, fake_hub)
    # 401 op de inbox, maar services/self is 200 → géén intrekking
    paired_client.post(
        "/search/v1/sources",
        json={"kind": "files", "name": "Docs", "allowed_paths": [str(shares.a)]},
        headers=idem(auth("admin", sub="beheer")),
    )
    fake_hub.override = respond("/api/v1/events/inbox", 401)
    w.push_step()
    assert client.app.state.hub_link.read()["state"] == "gekoppeld"
    # netwerkfout, 5xx en 429 op services/self zijn nooit een intrekking
    for status in (500, 503, 429):
        fake_hub.override = respond("/api/v1/services/self", status)
        assert client.app.state.enrollment.check_revoked().code == "niet_ingetrokken"
    fake_hub.override = None
    fake_hub.up = False
    assert client.app.state.enrollment.check_revoked().code == "hub_onbereikbaar"
    fake_hub.up = True
    assert client.app.state.hub_link.read()["state"] == "gekoppeld"
    # echt ingetrokken in de Hub → bij de volgende controle fail-closed, eigen data blijft, één incident
    fake_hub.sim.revoke()
    w.state.next_self_check_at = 0
    w.link_step(50.0)
    doc = client.app.state.hub_link.read()
    assert doc["state"] == "ingetrokken" and doc["last_error"]["code"] == "ingetrokken_door_hub"
    assert doc["service_token"] is None and doc["pins"] is None
    r = client.get("/search/v1/sources", headers=auth("admin", sub="beheer"))
    assert r.status_code == 401 and r.json()["details"]["reason"] == "revoked"
    w.state.next_self_check_at = 0
    w.link_step(60.0)
    w.push_step()
    assert audit_actions(client).count("hub.revoked") == 1
    with client.app.state.db.sessions() as s:
        assert s.execute(text("SELECT count(*) FROM index_source")).scalar() == 1  # eigen data blijft
        assert "service.hub_link.revoked" in list(s.scalars(select(OutboxEvent.event_type)))
    assert "ingetrokken" in client.get("/").text


# ------------------------------------------------------------------ rotatie service-token (§3.8)
def test_rotation_requested_by_hub(paired_client: Any, fake_hub: FakeHub, auth: Auth) -> None:
    client = paired_client
    w = worker(client, fake_hub)
    old = client.app.state.hub_link.read()["service_token"]
    fake_hub.sim.request_rotation()
    w.state.next_self_check_at = 0
    w.link_step(10.0)
    doc = client.app.state.hub_link.read()
    assert (
        doc["service_token"] != old and doc["confirmed_at"] and doc["service_token_last4"] == doc["service_token"][-4:]
    )
    assert not w.enrollment.rotation_required and "hub.token_rotated" in audit_actions(client)
    # oud token nog maximaal 10 min na het eerste gebruik van het nieuwe geldig (bovengrens 24 uur)
    rec_old = fake_hub.sim.tokens[sha256_hex(old)]
    assert rec_old.expires_at is not None and rec_old.expires_at - fake_hub.sim.clock() <= 600 + 1
    assert client.get("/connector/v1/discover", headers=auth("viewer")).status_code == 200


def test_interrupted_rotation_is_redone(paired_client: Any, fake_hub: FakeHub) -> None:
    client = paired_client
    w = worker(client, fake_hub)
    old = client.app.state.hub_link.read()["service_token"]
    original = fake_hub.handler

    def crash_after_issue(req: httpx.Request) -> httpx.Response | None:
        if req.url.path == "/api/v1/services/self/rotate":
            fake_hub.override = None
            original(req)  # de Hub geeft een nieuw token uit …
            raise httpx.ReadTimeout("antwoord kwijt", request=req)  # … maar de dienst krijgt het nooit
        return None

    fake_hub.override = crash_after_issue
    w.enrollment.rotation_required = True
    w.link_step(10.0)
    assert client.app.state.hub_link.read()["service_token"] == old
    lost = fake_hub.sim.issued[-1]
    # volgende aanroep met het oude token: het nieuwste is nooit gebruikt → Hub-Token-Rotation: required
    w.enrollment.rotation_required = False
    w.state.next_self_check_at = 0
    w.state.next_rotation_at = 0
    w.link_step(20.0)
    doc = client.app.state.hub_link.read()
    assert doc["service_token"] not in (old, lost) and doc["confirmed_at"]
    assert fake_hub.sim._valid(lost) is None  # het nooit ontvangen token is bij de nieuwe rotatie ingetrokken


# ------------------------------------------------------------------ sleutelrotatie (§4.5)
def test_key_rotation_polling(paired_client: Any, fake_hub: FakeHub, svc: Auth) -> None:
    client = paired_client
    sim = fake_hub.sim
    w = worker(client, fake_hub)
    w.link_step(0.0)  # lege lijst → niets
    assert client.app.state.hub_link.read()["pins"]["rotation_sequence"] == 0
    new = Ed25519PrivateKey.generate()
    # een JWT met de nieuwe sleutel wordt vóór de rotatie geweigerd (en zet een achtergrondpoll in gang)
    r = client.get("/connector/v1/discover", headers={"Authorization": f"Bearer {sim.user_jwt(['admin'], key=new)}"})
    assert r.status_code == 401 and r.json()["details"]["reason"] == "unknown_key"
    assert client.app.state.key_rotation_trigger.started == 1
    for t in threading.enumerate():
        if t.name == "sleutelrotatie-poll":
            t.join(10)  # de achtergrondpoll (lege lijst) is klaar vóór we een statement publiceren
    sim.rotations.append(
        {"sequence": 1, "statement": sim.rotation_statement(1, "jwt", [hub_jwk(new)], [], sim.jwt_key)}
    )
    w.state.next_key_poll_at = 0
    w.link_step(1.0)
    doc = client.app.state.hub_link.read()
    assert doc["pins"]["rotation_sequence"] == 1 and len(doc["pins"]["jwt"]) == 2
    sim.extra_jwt_keys.append(new)  # de Hub publiceert nu beide JWT-sleutels
    assert doc["pins"]["keyset_fingerprint"] == fake_hub.fingerprint()
    r = client.get("/connector/v1/discover", headers={"Authorization": f"Bearer {sim.user_jwt(['admin'], key=new)}"})
    assert r.status_code == 200
    assert client.get("/connector/v1/hub-link", headers=svc()).json()["rotation_sequence"] == 1
    # een ongeldig statement (niet ondertekend door een gepinde sleutel) wordt niet toegepast → incident
    stranger = Ed25519PrivateKey.generate()
    sim.rotations.append(
        {"sequence": 2, "statement": sim.rotation_statement(2, "jwt", [hub_jwk(stranger)], [], stranger)}
    )
    w.state.next_key_poll_at = 0
    w.link_step(2.0)
    doc = client.app.state.hub_link.read()
    assert doc["pins"]["rotation_sequence"] == 1 and doc["last_error"]["code"] == "sleutelrotatie_ongeldig"
    health = client.get("/connector/v1/health", headers=svc()).json()
    assert {c["name"]: c["status"] for c in health["checks"]}["hub_sleutels"] == "degraded"
    assert "hub.key_rotation_rejected" in audit_actions(client)


def test_unknown_kid_background_poll_is_rate_limited(paired_client: Any, fake_hub: FakeHub) -> None:
    trigger = paired_client.app.state.key_rotation_trigger
    other = Ed25519PrivateKey.generate()
    for _ in range(5):
        tok = fake_hub.sim.user_jwt(["admin"], key=other)
        assert (
            paired_client.get("/connector/v1/discover", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
        )
    assert trigger.started == 1  # hooguit 1× per 30 s


# ------------------------------------------------------------------ replaycache (§4.6)
def test_jti_replay_rules(paired_client: Any, fake_hub: FakeHub, shares: Any) -> None:
    client = paired_client
    token = fake_hub.sim.user_jwt(["admin"], sub="beheer")
    hdr = {"Authorization": f"Bearer {token}"}
    body = {"kind": "files", "name": "Docs", "allowed_paths": [str(shares.a)]}
    key = str(uuid.uuid4())
    r1 = client.post("/search/v1/sources", json=body, headers=idem(hdr, key=key))
    assert r1.status_code == 201
    # exacte herhaling (zelfde jti, methode, pad, Idempotency-Key) → het opgeslagen resultaat
    r2 = client.post("/search/v1/sources", json=body, headers=idem(hdr, key=key))
    assert r2.status_code == 201 and r2.headers["Idempotent-Replay"] == "true" and r2.json() == r1.json()
    # dezelfde jti voor een andere mutatie → token_replayed
    r3 = client.post("/search/v1/sources", json={**body, "name": "Anders"}, headers=idem(hdr))
    assert r3.status_code == 401 and r3.json()["details"]["reason"] == "token_replayed"
    r4 = client.post(f"/search/v1/sources/{r1.json()['id']}/pause", headers=idem(hdr, key=key))
    assert r4.status_code == 401 and r4.json()["details"]["reason"] == "token_replayed"
    # lezen mag binnen de levensduur vaker
    for _ in range(3):
        assert client.get("/search/v1/sources", headers=hdr).status_code == 200
    with client.app.state.db.sessions() as s:
        rows = list(s.scalars(select(JwtReplay)))
    assert len(rows) == 1 and rows[0].idempotency_key == key and token not in json.dumps([r.path for r in rows])
    # verlopen rijen ruimt de worker op
    with client.app.state.db.sessions() as s:
        s.execute(text("UPDATE jwt_replay SET expires_at = now() - interval '1 second'"))
        s.commit()
    w = worker(client, fake_hub)
    w.housekeeping_step(0.0)
    assert w.state.stats["expired_replay"] == 1


def test_replay_check_unavailable_is_fail_closed(paired_client: Any, auth: Auth, shares: Any) -> None:
    client = paired_client
    broken = create_engine("postgresql+psycopg://postgres@127.0.0.1:1/bestaat_niet", pool_pre_ping=False)
    client.app.state.auth.replay.engine = broken
    r = client.post(
        "/search/v1/sources",
        json={"kind": "files", "name": "Docs", "allowed_paths": [str(shares.a)]},
        headers=idem(auth("admin", sub="beheer")),
    )
    assert r.status_code == 503 and r.json()["code"] == "replay_check_unavailable"
    assert client.get("/search/v1/sources", headers=auth("admin", sub="beheer")).status_code == 200  # lezen kan
    with client.app.state.db.sessions() as s:
        assert s.execute(text("SELECT count(*) FROM index_source")).scalar() == 0  # er is niets uitgevoerd
    broken.dispose()


# ------------------------------------------------------------------ bodygrens (§5)
def _chunks(n: int) -> Iterator[bytes]:
    block = b"x" * 65536
    sent = 0
    while sent < n:
        part = block[: min(len(block), n - sent)]
        sent += len(part)
        yield part


def test_body_limit_counts_chunked_before_auth(client: Any) -> None:
    r = client.post(
        "/search/v1/sources", content=_chunks(1024 * 1024 + 1), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"
    assert r.json()["details"] == {"max_bytes": 1048576} and r.headers["X-Trace-Id"] == r.json()["trace_id"]
    r = client.post("/search/v1/sources", content=_chunks(1024 * 1024), headers={"Content-Type": "application/json"})
    assert r.status_code in (401, 422) and r.json()["code"] != "payload_too_large"  # precies 1 MiB mag
    r = client.post("/koppelen", content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413  # ook de statuspagina


# ------------------------------------------------------------------ overzetting uit de 1.1-opslag (§3.5)
def _legacy_rows(client: Any, fake_hub: FakeHub, *, with_policy: bool = True, status: str = "gekoppeld") -> str:
    state = client.app.state
    token = "svc_" + "L" * 43
    # vorm zoals 1.1 hem schreef (enrollment.py van vóór contract 1.2): na ontkoppelen een nieuwe dict zonder enrolled_at
    value: dict[str, Any] = (
        {
            "status": "gekoppeld",
            "reason": "gekoppeld",
            "hub_url": HUB_URL,
            "via": "token",
            "service_id": str(uuid.uuid4()),
            "enrolled_at": "2026-11-01T10:00:00Z",
            "used_token_sha256": "c" * 64,
        }
        if status == "gekoppeld"
        else {
            "status": "niet_gekoppeld",
            "reason": "ontkoppeld",
            "previous_hub_url": HUB_URL,
            "unpaired_at": "2026-11-02T10:00:00Z",
            "rejected_token_sha256": "c" * 64,
        }
    )
    with state.db.sessions() as s:
        s.add(ServiceState(key="hub_link", value=value))
        s.add(ServiceState(key="hub_jwks", value={"jwks": fake_hub.sim.jwks(), "received_at": "t", "source": "enroll"}))
        if with_policy:
            s.add(ServiceState(key="hub_policy_key", value={"jwk": fake_hub.sim.policy_jwk(), "received_at": "t"}))
        state.vault.put(s, "vault://hub/service-token", token)
        s.commit()
    return token


def _legacy_left(client: Any) -> int:
    with client.app.state.db.sessions() as s:
        return int(
            s.execute(
                text(
                    "SELECT (SELECT count(*) FROM service_state WHERE key LIKE 'hub%') + (SELECT count(*) FROM vault_secret)"
                )
            ).scalar()
            or 0
        )


def test_legacy_link_moves_to_file_and_db_copy_is_removed(client: Any, fake_hub: FakeHub, auth: Auth) -> None:
    token = _legacy_rows(client, fake_hub)
    state = client.app.state
    assert migrate_legacy_link(state.hub_link, state.db, state.vault).startswith("overgezet:gekoppeld")
    doc = state.hub_link.read()
    assert doc["state"] == "gekoppeld" and doc["service_token"] == token and doc["confirmed_at"] is None
    assert (
        doc["pins"]["keyset_fingerprint"] == fake_hub.fingerprint()
        and doc["pins"]["jwt"][0]["source"] == "migratie_1.1"
    )
    assert doc["first_linked_at"] == "2026-11-01T10:00:00Z" and doc["used_enrollment_token_sha256"] == ["c" * 64]
    assert _legacy_left(client) == 0
    assert client.get("/connector/v1/discover", headers=auth("viewer")).status_code == 200
    # database-herstel van vóór de overzetting: de oude kopie wordt gewist, het bestand blijft leidend
    _legacy_rows(client, fake_hub)
    assert migrate_legacy_link(state.hub_link, state.db, state.vault) == "gewist"
    assert state.hub_link.read()["service_token"] == token and _legacy_left(client) == 0


def test_incomplete_or_unpaired_legacy_never_reopens_the_form(
    app_factory: Callable[..., Any], fake_hub: FakeHub, tmp_path: Any
) -> None:
    client = app_factory()
    state = client.app.state
    _legacy_rows(client, fake_hub, with_policy=False)
    assert migrate_legacy_link(state.hub_link, state.db, state.vault) == "overgezet:ontkoppeld"
    doc = state.hub_link.read()
    assert doc["last_error"]["code"] == "migratie_onvolledig" and doc["service_token"] is None
    assert 'action="/koppelen"' not in client.get("/").text
    other = app_factory(data_dir=tmp_path / "andere-appdata")
    _legacy_rows(other, fake_hub, status="niet_gekoppeld")
    assert migrate_legacy_link(other.app.state.hub_link, other.app.state.db, other.app.state.vault) == (
        "overgezet:ontkoppeld"
    )
    assert other.app.state.hub_link.read()["first_linked_at"] == "2026-11-02T10:00:00Z"
