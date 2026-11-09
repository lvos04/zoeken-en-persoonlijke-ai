"""QA-review contract 1.2: negatieve scenario's die nog ontbraken of niet klopten (ADR-0012).

- Zonder geldige authenticatie altijd 401, ook als de body onleesbare JSON is (§4.7; FastAPI leest de body vóór de
  afhankelijkheden). Met geldige authenticatie blijft het 422 en wordt de ``jti`` niet verbruikt.
- Twee gelijktijdige koppelpogingen via de statuspagina: alleen de eerste koppelt; de tweede vervangt nooit een
  koppeling die er intussen is (§3.7).
- Verlopen (> 60 min) en hergebruikte enrollment-tokens met de echte regels van de Hub-simulator (§3.3/§3.6).
- Rate-limit van de statuspagina telt het adres van de TCP-verbinding, nooit ``X-Forwarded-For`` (§3.7).
- Na intrekking geen koppelformulier en geen koppelen via de statuspagina (§3.7).
- Gewisselde Hub-sleutel zonder ondertekende rotatie of nieuwe koppeling: nooit vertrouwd; herkoppelen met de oude
  ``HUB_KEY_FINGERPRINT`` slaat niets op en laat de bestaande koppeling staan (§4.5, §3.3).
"""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text

from search_ai.security.auth import sha256_hex
from search_ai.worker import Worker
from tests.conftest import ENV_TOKEN, HUB_URL, PAIR_CODE, FakeHub, idem

pytestmark = pytest.mark.integration

Auth = Callable[..., Mapping[str, str]]
ENROLL = "/api/v1/services/enroll"
JSON = {"Content-Type": "application/json"}


def enroll_calls(fake_hub: FakeHub) -> int:
    return sum(1 for r in fake_hub.all_requests if r["path"] == ENROLL)


def _csrf(client: Any) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text)
    assert m, "geen koppelformulier"
    return m.group(1)


# ------------------------------------------------------------------ §4.7: authenticatie vóór body-fouten
# 422-pad (json_invalid) en 400-pad (ongeldige UTF-8: FastAPI kan de body niet ontleden)
@pytest.mark.parametrize("body", [b"{kapot", b'{"kind": ', b"\xff\xfe\x00{", b"\x80abc", b'{"a":"\xff"}'])
def test_unreadable_body_without_valid_auth_is_401_not_422(paired_client: Any, body: bytes) -> None:
    for path in ("/search/v1/sources", "/connector/v1/hub-link/unlink", "/voice/v1/sessions"):
        r = paired_client.post(path, content=body, headers={**JSON, **idem()})
        assert r.status_code == 401, (path, r.status_code, r.text)
        assert r.json()["code"] == "unauthorized" and r.json()["details"] == {"reason": "invalid_token"}
        r = paired_client.post(path, content=body, headers={**JSON, **idem(), "Authorization": "Bearer svc_x"})
        assert r.status_code == 401 and r.json()["details"]["reason"] == "service_token_not_accepted"


def test_unreadable_body_when_not_linked_is_401_not_linked(client: Any) -> None:
    r = client.post("/search/v1/sources", content=b"{kapot", headers={**JSON, **idem()})
    assert r.status_code == 401 and r.json()["details"]["reason"] == "not_linked"


def test_unreadable_body_with_valid_auth_stays_422_and_keeps_the_jti(
    paired_client: Any, fake_hub: FakeHub, shares: Any
) -> None:
    token = fake_hub.sim.user_jwt(["admin"], sub="beheer")
    hdr = {"Authorization": f"Bearer {token}"}
    r = paired_client.post("/search/v1/sources", content=b"{kapot", headers={**JSON, **idem(hdr)})
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    with paired_client.app.state.db.sessions() as s:
        assert s.execute(text("SELECT count(*) FROM jwt_replay")).scalar() == 0  # niets uitgevoerd, jti niet verbruikt
    body = {"kind": "files", "name": "Docs", "allowed_paths": [str(shares.a)]}
    assert paired_client.post("/search/v1/sources", json=body, headers=idem(hdr)).status_code == 201


def test_status_page_form_errors_are_unaffected(client: Any) -> None:
    # de statuspagina heeft geen Hub-JWT nodig: een kapotte formulierbody blijft gewoon een formulierfout
    r = client.post("/koppelen", data={"csrf": "x"}, follow_redirects=False)
    assert r.status_code == 303 and "ongeldig_formulier" in r.headers["location"]


# ------------------------------------------------------------------ §3.7: gelijktijdig koppelen
def test_concurrent_code_pairings_only_one_reaches_the_hub(client: Any, fake_hub: FakeHub) -> None:
    second = fake_hub.sim.add_pairing_code()
    at_hub, release = threading.Event(), threading.Event()

    def hold_first(req: httpx.Request) -> httpx.Response | None:
        if req.url.path == ENROLL:
            at_hub.set()
            assert release.wait(10)  # de eerste poging staat bij de Hub tot de tweede is afgehandeld
        return None

    fake_hub.override = hold_first
    enrollment = client.app.state.enrollment
    results: list[Any] = []
    first = threading.Thread(
        target=lambda: results.append(enrollment.enroll_with_code(HUB_URL, PAIR_CODE, fake_hub.fingerprint()))
    )
    first.start()
    assert at_hub.wait(10)
    # tweede poging via de statuspagina terwijl de eerste loopt: gaat niet naar de Hub
    csrf = _csrf(client)
    r = client.post(
        "/koppelen",
        data={"csrf": csrf, "hub_url": HUB_URL, "code": second, "fingerprint": fake_hub.fingerprint()},
        follow_redirects=False,
    )
    assert "koppelen_bezig" in r.headers["location"] and enroll_calls(fake_hub) == 1
    release.set()
    first.join(20)
    fake_hub.override = None
    assert [(o.ok, o.code) for o in results] == [(True, "gekoppeld")]
    doc = client.app.state.hub_link.read()
    assert doc["state"] == "gekoppeld" and doc["pending"] is None and doc["confirmed_at"]
    assert fake_hub.sim._valid(doc["service_token"]) is not None  # geen tweede uitgifte die het token intrekt
    assert 'action="/koppelen"' not in client.get("/").text


def test_code_attempt_never_replaces_a_link_made_meanwhile(app_factory: Callable[..., Any], fake_hub: FakeHub) -> None:
    """Vangnet naast de vergrendeling: koppelt de worker (env-token) terwijl een koppelcode-poging bij de Hub is, dan
    vervangt die poging de koppeling niet (§3.7: de statuspagina koppelt alleen een nooit gekoppelde dienst)."""
    client = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN, hub_key_fingerprint=fake_hub.fingerprint())
    store = client.app.state.hub_link

    def env_link_meanwhile(req: httpx.Request) -> httpx.Response | None:
        if req.url.path == ENROLL and b"pairing_code" in req.content:
            fake_hub.override = None
            Worker(client.app.state.settings, transport=fake_hub.transport).enroll_step(0.0)
            assert store.read()["via"] == "token"
        return None

    fake_hub.override = env_link_meanwhile
    outcome = client.app.state.enrollment.enroll_with_code(HUB_URL, PAIR_CODE, fake_hub.fingerprint())
    assert not outcome.ok and outcome.code == "al_gekoppeld"
    doc = store.read()
    assert doc["state"] == "gekoppeld" and doc["via"] == "token" and doc["pending"] is None
    assert doc["service_token"] != fake_hub.sim.issued[-1]  # het token van de koppelcode-uitgifte is niet opgeslagen


# ------------------------------------------------------------------ §3.3/§3.6: verlopen en hergebruikt token
def test_expired_enrollment_token_is_refused_by_the_hub_and_never_retried(
    app_factory: Callable[..., Any], fake_hub: FakeHub
) -> None:
    token = fake_hub.sim.add_enrollment_token()
    fake_hub.sim.grants[sha256_hex(token)].created_at -= 61 * 60  # aangemaakt in de Hub, 61 minuten geleden
    client = app_factory(hub_url=HUB_URL, enrollment_token=token, hub_key_fingerprint=fake_hub.fingerprint())
    w = Worker(client.app.state.settings, transport=fake_hub.transport)
    w.run_once()
    doc = client.app.state.hub_link.read()
    assert doc["state"] == "nooit_gekoppeld" and doc["service_token"] is None and doc["pins"] is None
    assert doc["last_error"]["code"] == "hub_weigerde" and doc["pending"] is None
    assert sha256_hex(token) in doc["used_enrollment_token_sha256"]
    w.state.next_enroll_at = 0
    w.run_once()
    Worker(client.app.state.settings, transport=fake_hub.transport).run_once()  # ook niet na een herstart
    assert enroll_calls(fake_hub) == 1


def test_reused_enrollment_token_on_a_fresh_install_is_refused(
    app_factory: Callable[..., Any], fake_hub: FakeHub, tmp_path: Any
) -> None:
    first = app_factory(hub_url=HUB_URL, enrollment_token=ENV_TOKEN, hub_key_fingerprint=fake_hub.fingerprint())
    Worker(first.app.state.settings, transport=fake_hub.transport).run_once()
    linked = first.app.state.hub_link.read()
    assert linked["state"] == "gekoppeld" and linked["confirmed_at"]  # het service-token is gebruikt
    # zelfde token in een nieuwe installatie (lege /appdata): geen toegestane herhaling meer → 401 invalid_enrollment
    again = app_factory(
        hub_url=HUB_URL,
        enrollment_token=ENV_TOKEN,
        hub_key_fingerprint=fake_hub.fingerprint(),
        data_dir=tmp_path / "nieuwe-appdata",
    )
    Worker(again.app.state.settings, transport=fake_hub.transport).run_once()
    doc = again.app.state.hub_link.read()
    assert doc["state"] == "nooit_gekoppeld" and doc["last_error"]["code"] == "hub_weigerde"
    assert sha256_hex(ENV_TOKEN) in doc["used_enrollment_token_sha256"]
    # de herhaalpoging trok het (gebruikte) token van de eerste installatie niet in
    assert fake_hub.sim._valid(linked["service_token"]) is not None
    assert first.get("/connector/v1/discover", headers=fake_hub.user("viewer")).status_code == 200
    assert again.get("/connector/v1/discover", headers=fake_hub.user("viewer")).json()["details"]["reason"] == (
        "not_linked"
    )


# ------------------------------------------------------------------ §3.7: rate-limit en na intrekking
def test_status_page_rate_limit_ignores_x_forwarded_for(client: Any, fake_hub: FakeHub) -> None:
    csrf = _csrf(client)
    locations = []
    for i in range(7):
        r = client.post(
            "/koppelen",
            data={"csrf": csrf, "hub_url": HUB_URL, "code": "X", "fingerprint": fake_hub.fingerprint()},
            headers={"X-Forwarded-For": f"10.9.9.{i}", "X-Real-IP": f"10.8.8.{i}", "Forwarded": f"for=10.7.7.{i}"},
            follow_redirects=False,
        )
        locations.append(r.headers["location"])
    assert locations[:5] == ["/?melding=koppelcode_ongeldig"] * 5
    assert locations[5:] == ["/?melding=te_veel_pogingen"] * 2  # elke poging telt voor hetzelfde TCP-adres
    assert enroll_calls(fake_hub) == 0


def test_no_pairing_on_the_status_page_after_revocation(paired_client: Any, fake_hub: FakeHub) -> None:
    client = paired_client
    csrf = _csrf_after_link(client)
    fake_hub.sim.revoke()
    w = Worker(client.app.state.settings, transport=fake_hub.transport)
    w.state.next_self_check_at = 0
    w.link_step(1.0)
    assert client.app.state.hub_link.read()["state"] == "ingetrokken"
    page = client.get("/").text
    assert 'action="/koppelen"' not in page and "ingetrokken" in page
    calls = enroll_calls(fake_hub)
    code = fake_hub.sim.add_pairing_code()
    r = client.post(
        "/koppelen",
        data={"csrf": csrf, "hub_url": HUB_URL, "code": code, "fingerprint": fake_hub.fingerprint()},
        follow_redirects=False,
    )
    assert "al_gekoppeld" in r.headers["location"] and enroll_calls(fake_hub) == calls
    # ook rechtstreeks via de koppellogica: een koppelcode vervangt nooit een (ingetrokken) koppeling
    outcome = client.app.state.enrollment.enroll_with_code(HUB_URL, code, fake_hub.fingerprint())
    assert not outcome.ok and outcome.code == "al_gekoppeld"
    assert client.app.state.hub_link.read()["state"] == "ingetrokken"


def _csrf_after_link(client: Any) -> str:
    """Na het koppelen staat er geen formulier meer; het CSRF-cookie blijft wel (zoals een open tabblad)."""
    client.get("/")
    return str(client.cookies.get("sai_csrf"))


# ------------------------------------------------------------------ §4.5: gewisselde Hub-sleutel
def test_swapped_hub_key_is_never_trusted_without_rotation_or_relink(
    app_factory: Callable[..., Any], paired_client: Any, fake_hub: FakeHub
) -> None:
    client = paired_client
    sim = fake_hub.sim
    before = client.app.state.hub_link.read()
    old_fp = fake_hub.fingerprint()
    old_kid = sim.kid()
    sim.jwt_key = Ed25519PrivateKey.generate()  # bv. de Hub opnieuw geïnstalleerd: nieuwe sleutel, geen rotatie
    new_token = sim.user_jwt(["admin"])
    r = client.get("/connector/v1/discover", headers={"Authorization": f"Bearer {new_token}"})
    assert r.status_code == 401 and r.json()["details"]["reason"] == "unknown_key"
    forged = sim.user_jwt(["admin"], kid=old_kid)  # gepinde kid, andere sleutel
    r = client.get("/connector/v1/discover", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401 and r.json()["details"]["reason"] == "invalid_token"
    r = client.post("/connector/v1/hub-link/unlink", headers=idem({"Authorization": f"Bearer {sim.service_jwt()}"}))
    assert r.status_code == 401 and client.app.state.hub_link.read()["state"] == "gekoppeld"  # geen ontkoppeling
    # opnieuw koppelen met de vingerafdruk van de oude sleutelset: de Hub antwoordt met de nieuwe → niets opgeslagen
    token = sim.add_enrollment_token()
    relink = app_factory(
        hub_url=HUB_URL, enrollment_token=token, hub_key_fingerprint=old_fp, data_dir=client.app.state.settings.data_dir
    )
    w = Worker(relink.app.state.settings, transport=fake_hub.transport)
    w.startup()
    w.enroll_step(0.0)
    after = client.app.state.hub_link.read()
    assert after["last_error"]["code"] == "hub_vingerafdruk_onjuist" and after["pending"] is None
    assert after["pins"] == before["pins"] and after["service_token"] == before["service_token"]
    assert sha256_hex(token) in after["used_enrollment_token_sha256"]
    # de nieuwe sleutel blijft onbekend tot een ondertekende rotatie of een geslaagde nieuwe koppeling
    r = client.get("/connector/v1/hub-link", headers=fake_hub.user("viewer"))
    assert r.status_code == 401 and r.json()["details"]["reason"] == "unknown_key"
    assert uuid.UUID(after["service_id"]) == uuid.UUID(before["service_id"])
    # de Hub zelf zag een geslaagde herkoppeling en trok het oude service-token in: de volgende controle zet de dienst
    # fail-closed op 'ingetrokken' (nooit open toegang, nooit de nieuwe sleutel vertrouwen)
    w.state.next_self_check_at = 0
    w.link_step(1.0)
    assert client.app.state.hub_link.read()["state"] == "ingetrokken"
    r = client.get("/connector/v1/discover", headers=fake_hub.user("admin"))
    assert r.status_code == 401 and r.json()["details"]["reason"] == "revoked"
