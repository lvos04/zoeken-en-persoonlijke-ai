"""Contract 1.3 §4.5: thumbprint, sleutelset-vingerafdruk (testvectoren van het contract, ook over twee
policy-sleutels), normalisatie en ondertekende sleutelrotatie (in volgorde, zonder gaten, per doel, nooit de laatste
sleutel)."""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from search_ai.security.pins import (
    PinError,
    apply_rotation,
    jwk_thumbprint,
    keyset_fingerprint,
    make_pin,
    normalize_fingerprint,
    normalize_pairing_code,
    pinned_keys,
    pins_fingerprint,
    validate_hub_jwk,
    verify_rotation_statement,
)
from tests.hubsim import HubSim, fingerprint, hub_jwk

# Testvectoren uit PLATFORM_CONTRACTEN.md 1.3 §4.5 (publieke sleutels; alleen voor tests)
X_JWT = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"  # RFC 8037 A.1
X_POLICY = "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg"
X_JWT2 = "Kay64UG8yvCyLhqU000LxzYeUm0L_hLIl5S8kyKWbdc"


def _okp(x: str) -> dict[str, str]:
    return {"kty": "OKP", "crv": "Ed25519", "x": x}


def test_contract_thumbprint_vectors() -> None:
    assert jwk_thumbprint(_okp(X_JWT)) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
    assert jwk_thumbprint(_okp(X_POLICY)) == "1IG2tMH7J2wbJZnOf8LJzQitKf7LMvoAElsuDMVM54Y"
    assert jwk_thumbprint(_okp(X_JWT2)) == "AkXIZFzonb59ZmGwyKgi3H3BwMi6amevqdKQiLZhdtc"


def test_contract_fingerprint_vectors_and_order_independence() -> None:
    assert keyset_fingerprint([_okp(X_JWT)], _okp(X_POLICY)) == "MEGG-8GFG-2E4Y-K2RQ"
    assert keyset_fingerprint([_okp(X_JWT), _okp(X_JWT2)], _okp(X_POLICY)) == "CYRC-JS8C-14AN-JR17"
    assert keyset_fingerprint([_okp(X_JWT2), _okp(X_JWT)], _okp(X_POLICY)) == "CYRC-JS8C-14AN-JR17"
    # onafhankelijke implementatie in de simulator (andere bit-extractie) geeft hetzelfde
    assert fingerprint([_okp(X_JWT), _okp(X_JWT2)], _okp(X_POLICY)) == "CYRC-JS8C-14AN-JR17"


def test_contract_1_3_vector_with_two_policy_keys_in_any_order() -> None:
    """Nieuw in 1.3: één jwt-sleutel en twee policy-sleutels (A6EH…Mbg én Kay6…bdc) → HN69-2PHK-XV2D-X85W."""
    for policies in ([_okp(X_POLICY), _okp(X_JWT2)], [_okp(X_JWT2), _okp(X_POLICY)]):
        assert keyset_fingerprint([_okp(X_JWT)], policies) == "HN69-2PHK-XV2D-X85W"
        assert fingerprint([_okp(X_JWT)], policies) == "HN69-2PHK-XV2D-X85W"
    # één policy-sleutel in een lijst = de 1.2-uitkomst
    assert keyset_fingerprint([_okp(X_JWT)], [_okp(X_POLICY)]) == "MEGG-8GFG-2E4Y-K2RQ"
    # negatief: alleen de laatst gepinde policy-sleutel (het 1.2-gedrag van deze dienst) geeft iets anders
    assert keyset_fingerprint([_okp(X_JWT)], _okp(X_JWT2)) != "HN69-2PHK-XV2D-X85W"
    with pytest.raises(PinError):
        keyset_fingerprint([_okp(X_JWT)], [])


def test_pins_fingerprint_counts_every_pinned_policy_key() -> None:
    """Na een policy-rotatie met alleen ``add`` (twee policy-sleutels) telt elke gepinde sleutel mee, nooit alleen de
    laatst gepinde (C1.3-ZPA-03)."""
    pins = {
        "jwt": [make_pin({**_okp(X_JWT), "kid": "j1"}, source="enroll", pinned_at="t")],
        "policy": [
            make_pin({**_okp(X_JWT2), "kid": "p2"}, source="rotation:1", pinned_at="t"),
            make_pin({**_okp(X_POLICY), "kid": "p1"}, source="enroll", pinned_at="t"),
        ],
    }
    assert pins_fingerprint(pins) == "HN69-2PHK-XV2D-X85W"
    pins["policy"].reverse()
    assert pins_fingerprint(pins) == "HN69-2PHK-XV2D-X85W"


def test_fingerprint_normalization() -> None:
    assert normalize_fingerprint("megg 8gfg 2e4y k2rq") == "MEGG-8GFG-2E4Y-K2RQ"
    assert normalize_fingerprint("CYRC-JS8C-I4AN-JR17") == "CYRC-JS8C-14AN-JR17"  # I → 1
    assert normalize_fingerprint("CYRC-JS8C-l4AN-JR17") == "CYRC-JS8C-14AN-JR17"  # l → 1
    assert normalize_fingerprint("MEGG-8GFG-2E4Y-K2RU") is None  # U staat niet in Crockford
    assert normalize_fingerprint("MEGG-8GFG-2E4Y") is None
    assert normalize_fingerprint("x" * 100) is None


def test_pairing_code_normalization() -> None:
    assert normalize_pairing_code("kq4m-7zp2-9hxt") == "KQ4M-7ZP2-9HXT"
    assert normalize_pairing_code("KQ4M 7ZP2 9HXO") == "KQ4M-7ZP2-9HX0"  # O → 0
    assert normalize_pairing_code("KQ4M-7ZP2-9HX") is None  # 11 tekens
    assert normalize_pairing_code("KQ4M-7ZP2-9HXTU") is None  # 13 tekens
    assert normalize_pairing_code("KQ4M-7ZP2-9HXU") is None  # U niet toegestaan
    assert normalize_pairing_code("K" * 33) is None


@pytest.mark.parametrize(
    "jwk",
    [
        {**_okp(X_JWT), "kid": "a", "use": "sig"},  # alg ontbreekt
        {**_okp(X_JWT), "kid": "a", "alg": "EdDSA"},  # use ontbreekt
        {**_okp(X_JWT), "use": "sig", "alg": "EdDSA"},  # kid ontbreekt
        {**_okp(X_JWT), "kid": "a", "use": "enc", "alg": "EdDSA"},
        {**_okp(X_JWT), "kid": "a", "use": "sig", "alg": "EdDSA", "d": "geheim"},
        {"kty": "OKP", "crv": "X25519", "x": X_JWT, "kid": "a", "use": "sig", "alg": "EdDSA"},
        {"kty": "OKP", "crv": "Ed25519", "x": X_JWT + "=", "kid": "a", "use": "sig", "alg": "EdDSA"},
        {"kty": "OKP", "crv": "Ed25519", "x": X_JWT[:-1], "kid": "a", "use": "sig", "alg": "EdDSA"},
        "geen object",
    ],
)
def test_hub_jwk_rules(jwk: Any) -> None:
    with pytest.raises(PinError):
        validate_hub_jwk(jwk)


def _pins(sim: HubSim) -> dict[str, Any]:
    pins = {
        "jwt": [make_pin(k, source="enroll", pinned_at="t") for k in sim.jwks()["keys"]],
        "policy": [make_pin(sim.policy_jwk(), source="enroll", pinned_at="t")],
        "rotation_sequence": 0,
    }
    pins["keyset_fingerprint"] = pins_fingerprint(pins)
    return pins


def test_rotation_applies_in_order_and_updates_fingerprint() -> None:
    sim = HubSim()
    pins = _pins(sim)
    assert pins["keyset_fingerprint"] == sim.fingerprint()
    new = Ed25519PrivateKey.generate()
    stmt = sim.rotation_statement(1, "jwt", [hub_jwk(new)], [], sim.jwt_key)
    rot = verify_rotation_statement(stmt, pins, 1)
    pins2 = apply_rotation(pins, rot, now="t2")
    assert sorted(pinned_keys(pins2, "jwt")) == sorted([sim.kid(), sim.kid(new)])
    assert pins2["rotation_sequence"] == 1 and pins2["keyset_fingerprint"] != pins["keyset_fingerprint"]
    # tweede statement: oude sleutel intrekken, ondertekend met de nieuwe (die nu gepind is)
    stmt2 = sim.rotation_statement(2, "jwt", [], [sim.kid()], new)
    pins3 = apply_rotation(pins2, verify_rotation_statement(stmt2, pins2, 2), now="t3")
    assert list(pinned_keys(pins3, "jwt")) == [sim.kid(new)]
    assert pins3["keyset_fingerprint"] == fingerprint([hub_jwk(new)], sim.policy_jwk())


def test_rotation_rejections() -> None:
    sim = HubSim()
    pins = _pins(sim)
    new = hub_jwk(Ed25519PrivateKey.generate())
    stranger = Ed25519PrivateKey.generate()
    cases = {
        "gat in de volgorde": (sim.rotation_statement(2, "jwt", [new], [], sim.jwt_key), 1),
        "verkeerd doel ondertekend": (sim.rotation_statement(1, "jwt", [new], [], sim.policy_key), 1),
        "policy met jwt-sleutel": (sim.rotation_statement(1, "policy", [new], [], sim.jwt_key), 1),
        "niet gepinde ondertekenaar": (sim.rotation_statement(1, "jwt", [new], [], stranger), 1),
        "laatste sleutel intrekken": (sim.rotation_statement(1, "jwt", [], [sim.kid()], sim.jwt_key), 1),
        "onbekende kid intrekken": (sim.rotation_statement(1, "jwt", [], ["bestaat-niet"], sim.jwt_key), 1),
        "verkeerd typ": (sim.rotation_statement(1, "jwt", [new], [], sim.jwt_key, typ="JWT"), 1),
        "verkeerd type": (sim.rotation_statement(1, "jwt", [new], [], sim.jwt_key, type="iets"), 1),
        "andere iss": (sim.rotation_statement(1, "jwt", [new], [], sim.jwt_key, iss="elders"), 1),
        "sleutel met d": (sim.rotation_statement(1, "jwt", [{**new, "d": "x"}], [], sim.jwt_key), 1),
        "geen jws": ("geen.jws", 1),
    }
    for name, (stmt, expected) in cases.items():
        with pytest.raises(PinError):
            verify_rotation_statement(stmt, pins, expected)
        assert name  # (naam alleen voor de leesbaarheid bij een fout)
    # vervalste handtekening: payload aangepast na ondertekenen
    good = sim.rotation_statement(1, "jwt", [new], [], sim.jwt_key)
    head, body, sig = good.split(".")
    forged_body = sim.rotation_statement(1, "jwt", [new], [sim.kid()], sim.jwt_key).split(".")[1]
    with pytest.raises(PinError):
        verify_rotation_statement(f"{head}.{forged_body}.{sig}", pins, 1)
    assert body


def test_policy_rotation_with_only_add_changes_fingerprint_over_both_keys() -> None:
    """Een policy-rotatie die alleen toevoegt: daarna staan er twee policy-sleutels en is de vingerafdruk die over
    beide (zoals de Hub hem toont), niet over de laatste alleen."""
    sim = HubSim()
    pins = _pins(sim)
    new = Ed25519PrivateKey.generate()
    stmt = sim.rotation_statement(1, "policy", [hub_jwk(new)], [], sim.policy_key)
    pins2 = apply_rotation(pins, verify_rotation_statement(stmt, pins, 1), now="t")
    assert len(pinned_keys(pins2, "policy")) == 2
    assert pins2["keyset_fingerprint"] == fingerprint(sim.jwks()["keys"], [sim.policy_jwk(), hub_jwk(new)])
    assert pins2["keyset_fingerprint"] != fingerprint(sim.jwks()["keys"], hub_jwk(new))


def test_replace_key_in_one_statement_is_allowed() -> None:
    """Toevoegen + de oude intrekken in één statement laat het doel nooit zonder sleutel (§4.5)."""
    sim = HubSim()
    pins = _pins(sim)
    new = Ed25519PrivateKey.generate()
    stmt = sim.rotation_statement(1, "policy", [hub_jwk(new)], [sim.kid(sim.policy_key)], sim.policy_key)
    pins2 = apply_rotation(pins, verify_rotation_statement(stmt, pins, 1), now="t")
    assert list(pinned_keys(pins2, "policy")) == [sim.kid(new)]


def test_tampered_pin_is_never_trusted() -> None:
    sim = HubSim()
    pins = _pins(sim)
    pins["jwt"][0]["jwk"]["x"] = hub_jwk(Ed25519PrivateKey.generate())["x"]  # thumbprint klopt niet meer
    assert pinned_keys(pins, "jwt") == {}
