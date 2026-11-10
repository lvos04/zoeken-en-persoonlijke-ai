"""Contract 1.3 §4.2–§4.7 (C1.3-ZPA-07) tegen de echte app: ``aud`` precies één string, de reason-tabel (geen header,
ander schema, lege waarde, geen ``kid``), elke POST/PUT/PATCH/DELETE verbruikt de ``jti`` (ook bij een onleesbare body en
een route die niets wijzigt) en de controlevolgorde: authenticatie vóór rol, ``Idempotency-Key`` en body (RV9)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from jwt.api_jws import PyJWS

from tests.conftest import FakeHub, idem

SOURCES = "/search/v1/sources"


def _reason(r: Any) -> str:
    assert r.status_code == 401, (r.status_code, r.text)
    body = r.json()
    assert body["code"] == "unauthorized"
    return str(body["details"]["reason"])


def test_aud_als_lijst_is_invalid_token_ook_met_de_eigen_app_id(paired_client: Any, fake_hub: FakeHub) -> None:
    sim = fake_hub.sim
    for aud in (["zoeken-en-persoonlijke-ai"], ["zoeken-en-persoonlijke-ai", "home-control-hub"], "home-control-hub"):
        tok = sim.user_jwt(["admin"], aud=aud)
        r = paired_client.get("/connector/v1/discover", headers={"Authorization": f"Bearer {tok}"})
        assert _reason(r) == "invalid_token", aud
    ok = sim.user_jwt(["admin"])
    assert paired_client.get("/connector/v1/discover", headers={"Authorization": f"Bearer {ok}"}).status_code == 200


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic dXNlcjpwdw==", "Token abc", "bearer"],
)
def test_geen_header_ander_schema_of_lege_waarde_is_invalid_token(paired_client: Any, header: str | None) -> None:
    headers = {} if header is None else {"Authorization": header}
    assert _reason(paired_client.get("/connector/v1/discover", headers=headers)) == "invalid_token"


def test_jws_zonder_kid_is_unknown_key(paired_client: Any, fake_hub: FakeHub) -> None:
    sim = fake_hub.sim
    claims = {**sim.base_claims(), "sub": "user-1", "roles": ["admin"], "token_use": "user"}
    tok = PyJWS().encode(json.dumps(claims).encode(), sim.jwt_key, algorithm="EdDSA", headers={"typ": "JWT"})
    assert "kid" not in PyJWS().get_unverified_header(tok)
    assert _reason(paired_client.get("/connector/v1/discover", headers={"Authorization": f"Bearer {tok}"})) == (
        "unknown_key"
    )


def test_gepinde_kid_met_foute_handtekening_is_invalid_token(paired_client: Any, fake_hub: FakeHub) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    tok = fake_hub.sim.user_jwt(["admin"], key=Ed25519PrivateKey.generate(), kid=fake_hub.sim.kid())
    assert _reason(paired_client.get("/connector/v1/discover", headers={"Authorization": f"Bearer {tok}"})) == (
        "invalid_token"
    )


def test_elke_post_verbruikt_de_jti_ook_zonder_idempotency_key_en_bij_501(
    paired_client: Any, fake_hub: FakeHub
) -> None:
    sim = fake_hub.sim
    tok = sim.service_jwt()
    hdr = {"Authorization": f"Bearer {tok}"}
    r = paired_client.post("/connector/v1/propose", headers=hdr)
    assert r.status_code == 501
    assert _reason(paired_client.post("/connector/v1/propose", headers=hdr)) == "token_replayed"
    # lezen met dezelfde JWT mag binnen de levensduur vaker
    assert paired_client.get("/connector/v1/discover", headers=hdr).status_code == 200


def test_onleesbare_body_verbruikt_de_jti_en_geeft_met_geldige_auth_422(paired_client: Any, fake_hub: FakeHub) -> None:
    tok = fake_hub.sim.user_jwt(["admin"])
    hdr = idem({"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    r = paired_client.post(SOURCES, headers=hdr, content=b"{geen json")
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    # dezelfde JWT is verbruikt: een tweede mutatie (nu met een geldige body) → token_replayed
    hdr2 = idem({"Authorization": f"Bearer {tok}"})
    assert _reason(paired_client.post(SOURCES, headers=hdr2, json={"name": "x"})) == "token_replayed"


def test_zonder_auth_altijd_401_voor_rol_idempotency_key_en_body(paired_client: Any) -> None:
    # geen Idempotency-Key, onleesbare body, geen auth → 401 (niet 400/422)
    r = paired_client.post(SOURCES, headers={"Content-Type": "application/json"}, content=b"{kapot")
    assert _reason(r) == "invalid_token"
    r = paired_client.post(SOURCES, json={"onbekend": 1})
    assert _reason(r) == "invalid_token"


def test_rol_en_idempotency_key_pas_na_de_authenticatie(paired_client: Any, fake_hub: FakeHub) -> None:
    viewer = fake_hub.user("viewer")
    r = paired_client.post(SOURCES, headers=dict(viewer), json={})  # geldige auth, verkeerde rol, geen key
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    admin = fake_hub.user("admin")
    r = paired_client.post(SOURCES, headers=dict(admin), json={})  # geldige auth en rol, geen key
    assert r.status_code == 400 and r.json()["code"] == "idempotency_key_required"
    r = paired_client.post(SOURCES, headers={**dict(admin), "Idempotency-Key": "geen-uuid"}, json={})
    assert r.status_code == 400 and r.json()["code"] == "idempotency_key_invalid"
    r = paired_client.post(SOURCES, headers=idem(dict(admin)), json={"onbekend_veld": str(uuid.uuid4())})
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
