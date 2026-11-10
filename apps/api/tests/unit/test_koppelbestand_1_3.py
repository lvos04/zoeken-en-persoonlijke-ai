"""Contract 1.3 §3.3/§3.5/§3.6 (C1.3-ZPA-04, C1.3-ZPA-06) zonder database: ``rotation_sequence`` uit het enroll-antwoord,
``schema_version`` 1.3, elke ``1.x`` lezen, onleesbaar = fail-closed (ook bij een verkeerde eigenaar of rechten), het
onleesbare bestand pas vervangen bij de eerste schrijfactie van een poging met een nieuw env-token (origineel bewaard),
de vaste ``last_error``-codes met hun HTTP-uitkomst en de extra velden."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from search_ai.config import Settings
from search_ai.enrollment import EnrollmentService, InvalidResponse, parse_enroll_response
from search_ai.hubclient import HubClient
from search_ai.hublink import (
    BACKUP_PREFIX,
    ERROR_MESSAGES,
    HubLinkCorrupt,
    HubLinkStore,
    empty_doc,
    is_unreadable,
    public_view,
)
from search_ai.security.auth import sha256_hex
from search_ai.security.secrets import ensure_secrets, load_identity, public_jwk
from tests.conftest import ENV_TOKEN, HUB_URL, FakeHub
from tests.hubsim import APP_ID

VASTE_CODES = {
    "hub_onbereikbaar",
    "hub_weigerde",
    "hub_antwoord_ongeldig",
    "hub_vingerafdruk_onjuist",
    "enroll_endpoint_ontbreekt",
    "rate_limited",
    "koppelen_onderbroken",
    "token_verlopen_na_60_min",
    "ingetrokken_door_hub",
    "hub_incompatibel",
    "token_ongeldig",
    "vingerafdruk_ongeldig",
    "hub_adres_ongeldig",
    "overzetten_onvolledig",
    "koppelstatus_onleesbaar",
}


def _service(tmp_path: Path, hub: FakeHub, **kw: Any) -> EnrollmentService:
    sec = tmp_path / "secrets"
    ensure_secrets(sec)
    settings = Settings(data_dir=tmp_path, **kw)
    return EnrollmentService(
        settings,
        HubLinkStore(sec, APP_ID),
        public_jwk(load_identity(sec)),
        HubClient(settings, transport=hub.transport, sleep=lambda _s: None),
    )


def _write_raw(store: HubLinkStore, text: str, mode: int = 0o600) -> None:
    store.dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text(text, encoding="utf-8")
    os.chmod(store.path, mode)


# ------------------------------------------------------------------ vaste codes en schema
def test_alle_vaste_codes_van_1_3_hebben_een_nl_melding() -> None:
    assert set(ERROR_MESSAGES) >= VASTE_CODES
    assert all(ERROR_MESSAGES[c].strip() for c in VASTE_CODES)


def test_oude_codes_uit_1_2_worden_als_1_3_getoond() -> None:
    doc = {**empty_doc(APP_ID), "last_error": {"code": "ongeldig_hub_adres", "at": "t"}}
    view = public_view(doc)
    assert view["last_error"]["code"] == "hub_adres_ongeldig"
    assert view["last_error"]["message_nl"] == ERROR_MESSAGES["hub_adres_ongeldig"]


def test_schrijft_schema_1_3_en_leest_elke_1_x_met_extra_velden(tmp_path: Path) -> None:
    store = HubLinkStore(tmp_path / "secrets", APP_ID)
    store.update(lambda d: {**d, "last_error": None})
    assert json.loads(store.path.read_text(encoding="utf-8"))["schema_version"] == "1.3"
    for versie in ("1.2", "1.9", "1.1000"):
        _write_raw(store, json.dumps({**empty_doc(APP_ID), "schema_version": versie, "toekomstig_veld": {"x": 1}}))
        doc = store.read()
        assert doc["state"] == "nooit_gekoppeld" and doc["toekomstig_veld"] == {"x": 1}
    # extra velden blijven na een schrijfactie staan (oudere lezers negeren ze)
    store.update(lambda d: {**d, "last_error": None})
    assert store.read()["toekomstig_veld"] == {"x": 1}


@pytest.mark.parametrize(
    "inhoud",
    [
        "{geen json",
        json.dumps({**empty_doc(APP_ID), "schema_version": "2.0"}),
        json.dumps({**empty_doc(APP_ID), "schema_version": 1.2}),
        json.dumps({k: v for k, v in empty_doc(APP_ID).items() if k != "schema_version"}),
        json.dumps({k: v for k, v in empty_doc(APP_ID).items() if k != "state"}),
        json.dumps({**empty_doc(APP_ID), "state": "gekoppeld"}),  # gekoppeld zonder token/pins
        json.dumps({**empty_doc(APP_ID), "app_id": "home-control-hub"}),
        json.dumps({**empty_doc(APP_ID), "pending": "x"}),
        json.dumps(["geen", "object"]),
    ],
)
def test_onleesbaar_is_fail_closed(tmp_path: Path, inhoud: str) -> None:
    store = HubLinkStore(tmp_path / "secrets", APP_ID)
    _write_raw(store, inhoud)
    with pytest.raises(HubLinkCorrupt):
        store.read()
    safe = store.read_safe()
    assert safe["corrupt"] is True and safe["state"] != "gekoppeld" and is_unreadable(safe)
    assert public_view(safe)["last_error"]["code"] == "koppelstatus_onleesbaar"


def test_verkeerde_rechten_zijn_onleesbaar_en_worden_direct_opgemerkt(tmp_path: Path) -> None:
    store = HubLinkStore(tmp_path / "secrets", APP_ID)
    store.update(lambda d: {**d, "last_error": None})
    assert store.read()["state"] == "nooit_gekoppeld"
    os.chmod(store.path, 0o644)  # leesbaar voor anderen
    with pytest.raises(HubLinkCorrupt, match="onleesbaar"):
        store.read()
    os.chmod(store.path, 0o600)
    assert store.read()["state"] == "nooit_gekoppeld"


def test_andere_eigenaar_is_onleesbaar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = HubLinkStore(tmp_path / "secrets", APP_ID)
    store.update(lambda d: {**d, "last_error": None})
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(store.path).st_uid + 1)
    store._sig = None
    with pytest.raises(HubLinkCorrupt):
        store.read()


# ------------------------------------------------------------------ vervangen van een onleesbaar bestand (RV6)
def test_gewone_schrijfactie_vervangt_een_onleesbaar_bestand_nooit(tmp_path: Path) -> None:
    store = HubLinkStore(tmp_path / "secrets", APP_ID)
    _write_raw(store, "{kapot")
    store.update(lambda d: {**d, "last_error": {"code": "hub_onbereikbaar", "at": "t"}})
    assert store.path.read_text(encoding="utf-8") == "{kapot"
    assert not [p for p in store.dir.iterdir() if p.name.startswith(BACKUP_PREFIX)]


def test_nieuw_env_token_vervangt_onleesbaar_bestand_vooraf_met_behoud_van_het_origineel(tmp_path: Path) -> None:
    """Eerste schrijfactie van de poging (``pending``) vóór het verzoek aan de Hub; origineel 0600 bewaard."""
    hub = FakeHub()
    gezien: list[dict[str, Any]] = []

    def kijk(req: httpx.Request) -> httpx.Response | None:
        if req.url.path == "/api/v1/services/enroll":
            gezien.append(json.loads(svc.store.path.read_text(encoding="utf-8")))  # stand vóór het antwoord
        return None

    hub.override = kijk
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    _write_raw(svc.store, "{kapot")
    out = svc.enroll_with_env_token()
    assert out is not None and out.ok, out
    # vóór het verzoek stond er al een nieuw bestand met pending en unreadable_replaced_at
    assert gezien and gezien[0]["pending"]["kind"] == "token" and gezien[0]["unreadable_replaced_at"]
    backups = [p for p in svc.store.dir.iterdir() if p.name.startswith(BACKUP_PREFIX)]
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "{kapot"
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    doc = svc.store.read()
    assert doc["state"] == "gekoppeld" and doc["first_linked_at"] and not is_unreadable(doc)
    assert doc["unreadable_replaced_at"] and "unreadable_replaced_at" not in public_view(doc)


def test_geweigerd_token_na_vervangen_wordt_na_herstart_niet_opnieuw_geprobeerd(tmp_path: Path) -> None:
    hub = FakeHub()
    hub.override = lambda r: (
        httpx.Response(401, json={"code": "invalid_enrollment"}) if r.url.path == "/api/v1/services/enroll" else None
    )
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    _write_raw(svc.store, "{kapot")
    out = svc.enroll_with_env_token()
    assert out is not None and out.code == "hub_weigerde"
    doc = svc.store.read()
    assert sha256_hex(ENV_TOKEN) in doc["used_enrollment_token_sha256"]
    # nog nooit gekoppeld na het vervangen: voor de statuspagina blijft het "onleesbaar" (geen formulier)
    assert is_unreadable(doc) and doc["state"] == "nooit_gekoppeld" and not doc["first_linked_at"]
    # "herstart": een nieuwe dienst met hetzelfde token probeert het niet opnieuw
    svc2 = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    assert svc2.enroll_with_env_token() is None


def test_configfout_vervangt_een_onleesbaar_bestand_niet(tmp_path: Path) -> None:
    """``hub_adres_ongeldig``/``vingerafdruk_ongeldig``: geen poging, geen Hub, niets vervangen (§3.5)."""
    hub = FakeHub()
    svc = _service(tmp_path, hub, hub_url="http://127.1:30400", enrollment_token=ENV_TOKEN)
    _write_raw(svc.store, "{kapot")
    out = svc.enroll_with_env_token()
    assert out is not None and out.code == "hub_adres_ongeldig" and not hub.all_requests
    assert svc.store.path.read_text(encoding="utf-8") == "{kapot"


def test_token_in_verkeerde_vorm_is_token_ongeldig_en_afgehandeld(tmp_path: Path) -> None:
    hub = FakeHub()
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token="enr_te_kort")
    out = svc.enroll_with_env_token()
    assert out is not None and out.code == "token_ongeldig" and not hub.all_requests
    assert sha256_hex("enr_te_kort") in svc.store.read()["used_enrollment_token_sha256"]


# ------------------------------------------------------------------ HTTP-uitkomst per code (§3.3, RV6)
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "hub_weigerde"),
        (404, "enroll_endpoint_ontbreekt"),
        (413, "hub_incompatibel"),
        (422, "hub_incompatibel"),
        (400, "hub_incompatibel"),
    ],
)
def test_definitieve_weigering_per_http_status(tmp_path: Path, status: int, code: str) -> None:
    hub = FakeHub()
    hub.override = lambda r: httpx.Response(status, json={}) if r.url.path == "/api/v1/services/enroll" else None
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    out = svc.enroll_with_env_token()
    assert out is not None and out.code == code and not out.retryable
    doc = svc.store.read()
    assert doc["last_error"]["code"] == code and doc["pending"] is None
    assert sha256_hex(ENV_TOKEN) in doc["used_enrollment_token_sha256"]


@pytest.mark.parametrize(
    ("status", "code"), [(429, "rate_limited"), (503, "hub_onbereikbaar"), (500, "hub_onbereikbaar")]
)
def test_tijdelijke_storing_blijft_pending(tmp_path: Path, status: int, code: str) -> None:
    hub = FakeHub()
    hub.override = lambda r: (
        httpx.Response(status, json={}, headers={"Retry-After": "7"})
        if r.url.path == "/api/v1/services/enroll"
        else None
    )
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    out = svc.enroll_with_env_token()
    assert out is not None and out.code == code and out.retryable
    assert svc.store.read()["pending"]["kind"] == "token"


# ------------------------------------------------------------------ rotation_sequence en base_url (§3.3)
def test_rotation_sequence_uit_het_enroll_antwoord(tmp_path: Path) -> None:
    hub = FakeHub()
    hub.sim.rotations = [{"sequence": n, "statement": "x"} for n in (1, 2, 3)]  # Hub staat op reeks 3
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    out = svc.enroll_with_env_token()
    assert out is not None and out.ok, out
    assert svc.store.read()["pins"]["rotation_sequence"] == 3
    # de dienst vraagt alleen statements na die reeks
    svc.poll_key_rotations()
    assert any(r["path"] == "/api/v1/auth/key-rotations" for r in hub.all_requests)
    assert "after=3" in next(r for r in hub.sim.requests if r["path"] == "/api/v1/auth/key-rotations")["query"]


def test_hub_1_2_zonder_rotation_sequence_geeft_0(tmp_path: Path) -> None:
    hub = FakeHub()
    hub.sim.hub_version = "1.2"
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    out = svc.enroll_with_env_token()
    assert out is not None and out.ok, out
    assert svc.store.read()["pins"]["rotation_sequence"] == 0


@pytest.mark.parametrize("waarde", [-1, "3", 1.5, True, None])
def test_ongeldige_rotation_sequence_is_een_ongeldig_antwoord(waarde: Any) -> None:
    hub = FakeHub()
    status, _h, body = hub.sim.handle(
        "POST",
        "/api/v1/services/enroll",
        "",
        {},
        json.dumps(
            {
                "app_id": APP_ID,
                "enrollment_token": ENV_TOKEN,
                "version": "0.1.0",
                "base_url": None,
                "public_key_jwk": {k: v for k, v in hub.sim.policy_jwk().items() if k in ("kty", "crv", "x")},
            }
        ).encode(),
        "192.0.2.1",
    )
    assert status == 200 and body["schema_version"] == "1.3"
    assert parse_enroll_response(body, None)["rotation_sequence"] == 0
    with pytest.raises(InvalidResponse):
        parse_enroll_response({**body, "rotation_sequence": waarde}, None)


@pytest.mark.parametrize(
    ("public_url", "verwacht"),
    [
        (None, None),
        ("http://192.168.1.5:30450", "http://192.168.1.5:30450"),
        ("http://127.0.0.1:30450", None),  # loopback: nooit sturen
        ("http://0x7f000001:30450", None),  # geen canonieke IP-vorm
        ("http://nas.lan:30450?x=1", None),
    ],
)
def test_base_url_alleen_als_hij_geldig_is(tmp_path: Path, public_url: str | None, verwacht: str | None) -> None:
    hub = FakeHub()
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN, public_url=public_url)
    out = svc.enroll_with_env_token()
    assert out is not None and out.ok, out  # nooit een weigering om base_url
    body = json.loads(next(r for r in hub.all_requests if r["path"] == "/api/v1/services/enroll")["body"])
    assert body["base_url"] == verwacht


# ------------------------------------------------------------------ extra velden (§3.5)
def test_rotatieverzoek_overleeft_een_herstart(tmp_path: Path) -> None:
    hub = FakeHub()
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    assert (out := svc.enroll_with_env_token()) is not None and out.ok
    hub.sim.request_rotation()
    svc.check_revoked()  # geauthenticeerde aanroep → Hub-Token-Rotation: required
    assert svc.store.read()["token_rotation_requested_at"]
    svc2 = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)  # "herstart"
    assert svc2.rotation_required is False and svc2.rotation_pending() is True
    rotated = svc2.rotate_service_token()
    assert rotated.ok and svc2.store.read()["token_rotation_requested_at"] is None
    assert svc2.rotation_pending() is False


def test_geweigerde_sleutelrotatie_blijft_zichtbaar(tmp_path: Path) -> None:
    hub = FakeHub()
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    assert (out := svc.enroll_with_env_token()) is not None and out.ok
    hub.sim.rotations = [{"sequence": 1, "statement": "geen.geldige.jws"}]
    res = svc.poll_key_rotations()
    assert res.code == "sleutelrotatie_ongeldig"
    doc = svc.store.read()
    assert doc["key_rotation_blocked_sequence"] == 1 and doc["last_error"]["code"] == "sleutelrotatie_ongeldig"
    assert "key_rotation_blocked_sequence" not in public_view(doc)
