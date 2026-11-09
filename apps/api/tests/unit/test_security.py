"""Auth (contract 1.2 §4.2–§4.7): gepinde sleutels, ``token_use``, vaste ``reason``-waarden in de vaste volgorde,
rollen (kind wint), redactie en vault."""

from __future__ import annotations

import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from search_ai.errors import ApiError
from search_ai.hublink import empty_doc
from search_ai.redact import looks_like_secret, redact
from search_ai.security.auth import REASON_MESSAGES, Authenticator, JwtPolicy, Principal, RequestInfo
from search_ai.security.pins import make_pin, pins_fingerprint
from search_ai.security.secrets import ensure_secrets, load_identity, public_jwk, secrets_present
from search_ai.security.vault import Vault
from tests.hubsim import APP_ID, HubSim

SERVICE_ID = str(uuid.uuid4())


def linked_doc(sim: HubSim, state: str = "gekoppeld") -> dict[str, Any]:
    doc = empty_doc(APP_ID)
    pins = {
        "jwt": [make_pin(k, source="enroll", pinned_at="2026-11-09T00:00:00Z") for k in sim.jwks()["keys"]],
        "policy": [make_pin(sim.policy_jwk(), source="enroll", pinned_at="2026-11-09T00:00:00Z")],
        "rotation_sequence": 0,
    }
    pins["keyset_fingerprint"] = pins_fingerprint(pins)
    doc.update(
        state=state,
        service_id=SERVICE_ID,
        service_token="svc_" + "a" * 43 if state == "gekoppeld" else None,
        pins=pins if state == "gekoppeld" else None,
    )
    return doc


def _auth(doc: dict[str, Any], unknown: list[int] | None = None) -> Authenticator:
    return Authenticator(lambda: doc, JwtPolicy(), None, (lambda: unknown.append(1)) if unknown is not None else None)


def _reason(fn: Any, *args: Any) -> str:
    with pytest.raises(ApiError) as exc:
        fn(*args)
    assert exc.value.status_code == 401 and exc.value.code == "unauthorized"
    assert exc.value.message == REASON_MESSAGES[exc.value.details["reason"]]
    return str(exc.value.details["reason"])


GET = RequestInfo("GET", "/connector/v1/discover", None)


def test_valid_user_and_service_jwt() -> None:
    sim = HubSim()
    auth = _auth(linked_doc(sim))
    p = auth.authenticate(f"Bearer {sim.user_jwt(['viewer', 'superuser'], sub='leon')}", GET)
    assert p.subject == "leon" and p.roles == frozenset({"viewer"}) and p.is_user and p.token_use == "user"
    s = auth.authenticate(f"Bearer {sim.service_jwt(SERVICE_ID)}", GET)
    assert s.kind == "service" and s.subject == "hub" and s.roles == frozenset({"service"}) and not s.is_user
    assert s.audit_id == "service:hub" and uuid.UUID(str(s.token_id))


def test_state_checked_first() -> None:
    sim = HubSim()
    tok = f"Bearer {sim.user_jwt(['admin'])}"
    for state, reason in (("nooit_gekoppeld", "not_linked"), ("ontkoppeld", "not_linked"), ("ingetrokken", "revoked")):
        auth = _auth(linked_doc(sim, state))
        assert _reason(auth.authenticate, tok, GET) == reason
        # ook een service-token of rommel geeft eerst de koppelstatus (volgorde §4.7)
        assert _reason(auth.authenticate, "Bearer svc_" + "b" * 43, GET) == reason
        assert _reason(auth.authenticate, None, GET) == reason


def test_service_token_never_accepted_inbound() -> None:
    doc = linked_doc(HubSim())
    assert _reason(_auth(doc).authenticate, f"Bearer {doc['service_token']}", GET) == "service_token_not_accepted"
    assert _reason(_auth(doc).authenticate, "Bearer svc_" + "c" * 43, GET) == "service_token_not_accepted"


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer ", "Bearer a.b", "Bearer x.y.z", "Token a.b.c"])
def test_garbage_is_invalid_token(header: str | None) -> None:
    assert _reason(_auth(linked_doc(HubSim())).authenticate, header, GET) == "invalid_token"


def test_unknown_key_triggers_background_poll_and_compares_the_key_itself() -> None:
    sim = HubSim()
    calls: list[int] = []
    auth = _auth(linked_doc(sim), calls)
    other = Ed25519PrivateKey.generate()
    assert _reason(auth.authenticate, f"Bearer {sim.user_jwt(['admin'], key=other)}", GET) == "unknown_key"
    assert calls == [1]
    # gepinde kid, maar ondertekend met een andere sleutel → handtekening ongeldig (vergelijking op x, niet op kid)
    forged = sim.user_jwt(["admin"], key=other, kid=sim.kid())
    assert _reason(auth.authenticate, f"Bearer {forged}", GET) == "invalid_token"
    assert calls == [1]  # geen extra poll: de kid was wel bekend


def test_alg_none_and_hs256_rejected() -> None:
    sim = HubSim()
    auth = _auth(linked_doc(sim))
    claims = {**sim.base_claims(), "sub": "x", "roles": ["admin"], "token_use": "user"}
    hs = jwt.encode(claims, "geheim" * 8, algorithm="HS256", headers={"kid": sim.kid()})
    assert _reason(auth.authenticate, f"Bearer {hs}", GET) == "invalid_token"
    none = jwt.encode(claims, None, algorithm="none", headers={"kid": sim.kid()})  # type: ignore[arg-type]
    assert _reason(auth.authenticate, f"Bearer {none}", GET) == "invalid_token"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"aud": "home-control-hub"}, "invalid_token"),
        ({"iss": "elders"}, "invalid_token"),
        ({"lifetime": 600}, "invalid_token"),  # exp - iat > 120 s
        ({"iat_offset": -400, "lifetime": 60}, "token_expired"),
        ({"nbf": 9_999_999_999}, "invalid_token"),
        ({"drop": ("nbf",)}, "invalid_token"),
        ({"drop": ("sub",)}, "invalid_token"),
        ({"roles": "admin"}, "invalid_token"),
        ({"drop": ("token_use",)}, "token_use_invalid"),
        ({"token_use": "iets"}, "token_use_invalid"),
        ({"roles": ["admin", "service"]}, "token_use_invalid"),
        ({"drop": ("jti",)}, "jti_missing"),
        ({"jti": ""}, "jti_missing"),
        ({"jti": "geen-uuid"}, "invalid_token"),
    ],
)
def test_user_jwt_rejections(kwargs: dict[str, Any], reason: str) -> None:
    sim = HubSim()
    roles = kwargs.pop("roles", ["admin"])
    tok = sim.user_jwt(roles, **kwargs)
    assert _reason(_auth(linked_doc(sim)).authenticate, f"Bearer {tok}", GET) == reason


@pytest.mark.parametrize(
    "overrides",
    [{"sub": "iemand"}, {"roles": ["service", "admin"]}, {"roles": ["admin"]}, {"service_id": str(uuid.uuid4())}],
)
def test_hub_service_jwt_rejections(overrides: dict[str, Any]) -> None:
    sim = HubSim()
    tok = sim.service_jwt(overrides.pop("service_id", SERVICE_ID), **overrides)
    assert _reason(_auth(linked_doc(sim)).authenticate, f"Bearer {tok}", GET) == "token_use_invalid"


def test_order_claims_before_token_use_before_jti() -> None:
    sim = HubSim()
    auth = _auth(linked_doc(sim))
    # verlopen én geen token_use én geen jti → claims gaan voor
    tok = sim.user_jwt(["admin"], iat_offset=-400, drop=("token_use", "jti"))
    assert _reason(auth.authenticate, f"Bearer {tok}", GET) == "token_expired"
    # geen token_use én geen jti → token_use gaat voor
    assert _reason(auth.authenticate, f"Bearer {sim.user_jwt(['admin'], drop=('token_use', 'jti'))}", GET) == (
        "token_use_invalid"
    )


def test_mutation_without_replay_cache_is_fail_closed() -> None:
    sim = HubSim()
    with pytest.raises(ApiError) as exc:
        _auth(linked_doc(sim)).authenticate(
            f"Bearer {sim.user_jwt(['admin'])}", RequestInfo("POST", "/search/v1/sources", str(uuid.uuid4()))
        )
    assert exc.value.status_code == 503 and exc.value.code == "replay_check_unavailable"


def test_kid_role_always_restrictive() -> None:
    p = Principal(kind="user", subject="kind-1", roles=frozenset({"admin", "kind"}))
    assert p.is_kid is True and p.is_admin is False


def test_redaction_and_secret_detection() -> None:
    out = redact({"api_key": "abc", "token_last4": "1234", "nested": {"tekst": "Bearer eyJhbGciOi.eyJzdWIi.sig"}})
    assert out["api_key"] == "***" and out["token_last4"] == "1234"
    assert "eyJ" not in str(out)
    assert looks_like_secret("svc_" + "a1" * 20) and not looks_like_secret("gewone zin over de keuken")


def test_secrets_first_start_never_overwrites(tmp_path: Any) -> None:
    d = tmp_path / "secrets"
    assert sorted(ensure_secrets(d)) == ["identity.ed25519", "vault.key"]
    before = (d / "vault.key").read_bytes()
    assert ensure_secrets(d) == []
    assert (d / "vault.key").read_bytes() == before
    assert oct((d / "vault.key").stat().st_mode & 0o777) == "0o600"
    assert all(secrets_present(d).values())
    jwk = public_jwk(load_identity(d))
    assert set(jwk) == {"kty", "crv", "x", "kid"}  # geen d, geen use/alg (contract 1.2 §3.3)


def test_vault_refs_validated() -> None:
    v = Vault(b"k" * 32)
    with pytest.raises(ValueError):
        v.check_ref("geen-ref")
    assert v.check_ref("vault://immich/api-key") == "vault://immich/api-key"
    assert v.request_hmac(b"x") != v.request_hmac(b"y")
