"""Contract 1.2 §3.2/§3.3/§3.5/§3.6 zonder database: koppelbestand, adresregels, exact enroll-verzoek en de controles
op het antwoord van de Hub."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from search_ai.config import Settings
from search_ai.enrollment import EnrollmentService, FingerprintMismatch, InvalidResponse, parse_enroll_response
from search_ai.hubclient import HubClient, HubUrlError, validate_hub_url
from search_ai.hublink import PUBLIC_FIELDS, HubLinkCorrupt, HubLinkStore, empty_doc, public_view
from search_ai.security.auth import sha256_hex
from search_ai.security.pins import jwk_thumbprint
from search_ai.security.secrets import ensure_secrets, load_identity, public_jwk
from tests.conftest import ENV_TOKEN, HUB_URL, PAIR_CODE, FakeHub
from tests.hubsim import APP_ID


# ------------------------------------------------------------------ koppelbestand
def test_store_writes_atomically_0600_and_rereads_changes(tmp_path: Path) -> None:
    sec = tmp_path / "secrets"
    a, b = HubLinkStore(sec, APP_ID), HubLinkStore(sec, APP_ID)  # twee "processen"
    assert a.read()["state"] == "nooit_gekoppeld" and not a.path.exists()
    a.update(lambda d: {**d, "last_error": {"code": "hub_onbereikbaar", "at": "t"}})
    assert stat.S_IMODE(a.path.stat().st_mode) == 0o600
    assert b.read()["last_error"]["code"] == "hub_onbereikbaar"
    b.update(lambda d: {**d, "last_error": None})
    assert a.read()["last_error"] is None  # inode gewijzigd → opnieuw gelezen
    assert [p.name for p in sec.iterdir() if p.name.endswith(".tmp")] == []


def test_store_update_is_serialized(tmp_path: Path) -> None:
    store = HubLinkStore(tmp_path, APP_ID)

    def bump() -> None:
        for _ in range(25):
            store.update(lambda d: {**d, "used_enrollment_token_sha256": [*d["used_enrollment_token_sha256"], "x"]})

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.read()["used_enrollment_token_sha256"]) == 20  # hooguit de 20 laatste (§3.5)


def test_corrupt_file_is_fail_closed_and_never_silently_replaced(tmp_path: Path) -> None:
    store = HubLinkStore(tmp_path, APP_ID)
    store.path.write_text("{geen json", encoding="utf-8")
    with pytest.raises(HubLinkCorrupt):
        store.read()
    safe = store.read_safe()
    assert safe["state"] == "ontkoppeld" and safe["corrupt"] is True
    store.update(lambda d: {**d, "last_error": None})  # afgeleide toestand: niet schrijven
    assert store.path.read_text(encoding="utf-8") == "{geen json"
    # 'gekoppeld' zonder token is ook onleesbaar
    store.path.write_text(json.dumps({**empty_doc(APP_ID), "state": "gekoppeld"}), encoding="utf-8")
    assert store.read_safe()["corrupt"] is True


def test_public_view_is_whitelist_only() -> None:
    doc = {
        **empty_doc(APP_ID),
        "state": "gekoppeld",
        "service_token": "svc_" + "g" * 43,
        "service_token_last4": "gggg",
        "pins": {"jwt": [], "policy": [], "keyset_fingerprint": "MEGG-8GFG-2E4Y-K2RQ", "rotation_sequence": 0},
        "pending": {"kind": "token", "secret_sha256": "f" * 64, "hub_url": "x", "started_at": "t", "attempts": 1},
        "last_error": {"code": "rate_limited", "at": "t"},
    }
    view = public_view(doc)
    assert tuple(view) == PUBLIC_FIELDS
    text = json.dumps(view)
    assert "svc_" not in text and "f" * 64 not in text and view["koppelen_bezig"] is True
    assert view["keyset_fingerprint"] == "MEGG-8GFG-2E4Y-K2RQ" and view["last_error"]["message_nl"]


# ------------------------------------------------------------------ adresregels (§3.2)
@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.20:30400",
        "https://nas.lan:30400/",
        "http://[fd00::1]:30400",
        "http://10.0.0.5",
    ],
)
def test_hub_url_accepted(url: str) -> None:
    assert validate_hub_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://nas:30400",
        "http://nas:30400/api",  # pad
        "http://nas:30400?x=1",
        "http://nas:30400#a",
        "http://user:pw@nas:30400",
        "http://localhost:30400",
        "http://127.0.0.1:30400",
        "http://[::1]:30400",
        "http://[::ffff:127.0.0.1]:30400",
        "http://169.254.169.254",  # link-local (cloud-metadata)
        "http://[fe80::1]:30400",
        "http://0.0.0.0:30400",
        "http://[::]:30400",
        "http://224.0.0.1",
        "http://nas:99999",
        "http://nas:0",
        "http://nas :30400",
        "",
    ],
)
def test_hub_url_rejected(url: str) -> None:
    with pytest.raises(HubUrlError):
        validate_hub_url(url)


def test_loopback_only_with_dev_flag() -> None:
    assert validate_hub_url("http://127.0.0.1:30400", allow_loopback=True) == "http://127.0.0.1:30400"
    with pytest.raises(HubUrlError):
        validate_hub_url("http://169.254.1.1", allow_loopback=True)  # link-local nooit


# ------------------------------------------------------------------ enroll-verzoek en -antwoord (§3.3)
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


def test_enroll_request_has_exactly_the_contract_fields(tmp_path: Path) -> None:
    hub = FakeHub()
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    assert svc.enroll_with_env_token() is not None
    body = json.loads(next(r for r in hub.all_requests if r["path"] == "/api/v1/services/enroll")["body"])
    assert set(body) == {"app_id", "enrollment_token", "version", "base_url", "public_key_jwk"}
    assert body["app_id"] == APP_ID and body["base_url"] is None and body["enrollment_token"] == ENV_TOKEN
    jwk = body["public_key_jwk"]
    assert set(jwk) == {"kty", "crv", "x", "kid"} and jwk["kid"] == jwk_thumbprint(jwk)
    # koppelcode: zelfde velden, met pairing_code in plaats van enrollment_token
    hub2 = FakeHub()
    svc2 = _service(tmp_path / "b", hub2)
    assert svc2.enroll_with_code(HUB_URL, PAIR_CODE.lower(), hub2.fingerprint()).ok
    body2 = json.loads(next(r for r in hub2.all_requests if r["path"] == "/api/v1/services/enroll")["body"])
    assert set(body2) == {"app_id", "pairing_code", "version", "base_url", "public_key_jwk"}
    assert body2["pairing_code"] == PAIR_CODE


def _good(hub: FakeHub) -> dict[str, Any]:
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
    assert status == 200
    return dict(body)


def test_response_checks(tmp_path: Path) -> None:
    hub = FakeHub()
    good = _good(hub)
    assert parse_enroll_response(good, None)["fingerprint"] == hub.fingerprint()
    assert parse_enroll_response(good, hub.fingerprint().lower().replace("-", " "))["service_id"]
    bad_cases: list[dict[str, Any]] = [
        {k: v for k, v in good.items() if k != "key_rotation_url"},
        {**good, "enrollment_replayed": 1},
        {**good, "schema_version": "2.0"},
        {**good, "app_id": "home-control-hub"},
        {**good, "service_id": "geen-uuid"},
        {**good, "service_token": "svc_kort"},
        {**good, "token_type": "Basic"},
        {**good, "hub_issuer": "elders"},
        {**good, "jwks": {"keys": []}},
        {**good, "jwks": {"keys": [good["jwks"]["keys"][0], good["jwks"]["keys"][0]]}},  # kid niet uniek
        {**good, "jwks": {"keys": [{**good["jwks"]["keys"][0], "d": "geheim"}]}},
        {**good, "jwks": {"keys": [{k: v for k, v in good["jwks"]["keys"][0].items() if k != "use"}]}},
        {**good, "policy_public_key": {**good["policy_public_key"], "alg": "RS256"}},
    ]
    for case in bad_cases:
        with pytest.raises(InvalidResponse):
            parse_enroll_response(case, None)
    with pytest.raises(FingerprintMismatch):
        parse_enroll_response({**good, "keyset_fingerprint": "MEGG-8GFG-2E4Y-K2RQ"}, None)
    with pytest.raises(FingerprintMismatch):
        parse_enroll_response(good, "MEGG-8GFG-2E4Y-K2RQ")
    # onbekende extra velden in het antwoord worden genegeerd
    assert parse_enroll_response({**good, "nieuw_veld": 1}, None)


def test_wrong_fingerprint_stores_nothing_and_token_is_never_retried(tmp_path: Path) -> None:
    hub = FakeHub()
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN, hub_key_fingerprint="MEGG8GFG2E4YK2RQ")
    out = svc.enroll_with_env_token()
    assert out is not None and not out.ok and out.code == "hub_vingerafdruk_onjuist" and not out.retryable
    doc = svc.store.read()
    assert doc["state"] == "nooit_gekoppeld" and doc["service_token"] is None and doc["pins"] is None
    assert doc["pending"] is None and doc["used_enrollment_token_sha256"] == [sha256_hex(ENV_TOKEN)]
    assert svc.enroll_with_env_token() is None  # afgehandeld: nooit opnieuw


def test_invalid_fingerprint_format_blocks_tofu(tmp_path: Path) -> None:
    hub = FakeHub()
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN, hub_key_fingerprint="geen-vingerafdruk")
    out = svc.enroll_with_env_token()
    assert out is not None and out.code == "ongeldige_vingerafdruk"
    assert not hub.all_requests  # er gaat niets naar de Hub
    assert svc.store.read()["used_enrollment_token_sha256"] == []  # na het repareren van de instelling mag het token


def test_secret_file_permissions_on_disk(tmp_path: Path) -> None:
    hub = FakeHub()
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    out = svc.enroll_with_env_token()
    assert out is not None and out.ok, out
    st = os.stat(svc.store.path)
    assert stat.S_IMODE(st.st_mode) == 0o600 and st.st_uid == os.getuid()
    assert stat.S_IMODE(os.stat(svc.store.dir).st_mode) == 0o700
    doc = svc.store.read()
    assert doc["state"] == "gekoppeld" and doc["confirmed_at"] and doc["service_token"] == hub.issued_token


def test_redirect_is_never_followed(tmp_path: Path) -> None:
    hub = FakeHub()
    hub.override = lambda r: (
        httpx.Response(307, headers={"Location": "http://hub.test:30400/elders"})
        if r.url.path == "/api/v1/services/enroll"
        else None
    )
    svc = _service(tmp_path, hub, hub_url=HUB_URL, enrollment_token=ENV_TOKEN)
    out = svc.enroll_with_env_token()
    assert out is not None and out.code == "hub_onbereikbaar" and out.retryable
    assert [r["path"] for r in hub.all_requests] == ["/api/v1/services/enroll"]
    assert svc.store.read()["pending"]["kind"] == "token"  # blijft staan: later opnieuw
