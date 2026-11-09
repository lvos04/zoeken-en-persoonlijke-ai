"""Auth (contract §4): Hub-JWT via JWKS, fail-closed, rollen (kind wint), redactie en vault."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from search_ai.errors import ApiError
from search_ai.redact import looks_like_secret, redact
from search_ai.security.auth import Authenticator, JwtPolicy, Principal, sha256_hex, validate_jwt
from search_ai.security.jwks import JwksCache, JwksUnavailable, UnknownKey, parse_jwks
from search_ai.security.secrets import ensure_secrets, load_identity, public_jwk, secrets_present
from search_ai.security.vault import Vault
from tests.conftest import HubKeys


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _cache(keys: HubKeys, clock: Clock | None = None, fail: bool = False) -> JwksCache:
    def fetch() -> Any:
        if fail:
            raise RuntimeError("hub weg")
        return keys.jwks()

    return JwksCache(fetch, ttl_s=300, max_stale_s=900, min_refresh_interval_s=0, clock=clock or time.monotonic)


def _reason(exc: pytest.ExceptionInfo[ApiError]) -> str:
    return str(exc.value.details["reason"])


def test_valid_jwt_and_role_filtering() -> None:
    keys = HubKeys()
    p = validate_jwt(keys.token(["viewer", "superuser"], sub="leon"), _cache(keys), JwtPolicy())
    assert p.subject == "leon" and p.roles == frozenset({"viewer"}) and p.is_user


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"aud": "home-control-hub"}, "verkeerde_audience"),
        ({"iss": "elders"}, "verkeerde_issuer"),
        ({"lifetime": 600}, "levensduur_te_lang"),
        ({"iat_offset": -400, "lifetime": 60}, "token_verlopen"),
    ],
)
def test_jwt_rejections(kwargs: dict[str, Any], reason: str) -> None:
    keys = HubKeys()
    with pytest.raises(ApiError) as exc:
        validate_jwt(keys.token(["admin"], **kwargs), _cache(keys), JwtPolicy())
    assert exc.value.status_code == 401 and _reason(exc) == reason


def test_jwt_forged_alg_and_key() -> None:
    keys = HubKeys()
    hs = jwt.encode({"sub": "x", "aud": "zoeken-en-persoonlijke-ai", "iss": "hub"}, "geheim" * 8, algorithm="HS256")
    with pytest.raises(ApiError) as exc:
        validate_jwt(hs, _cache(keys), JwtPolicy())
    assert _reason(exc) == "algoritme_niet_toegestaan"
    other = HubKeys(Ed25519PrivateKey.generate())
    forged = other.token(["admin"], kid=keys.kid)  # juiste kid, verkeerde sleutel
    with pytest.raises(ApiError) as exc:
        validate_jwt(forged, _cache(keys), JwtPolicy())
    assert _reason(exc) == "token_ongeldig"
    with pytest.raises(ApiError) as exc:
        validate_jwt(other.token(["admin"]), _cache(keys), JwtPolicy())
    assert _reason(exc) == "onbekende_sleutel"


def test_jwks_fail_closed_after_max_stale() -> None:
    keys = HubKeys()
    clock = Clock()
    state = {"fail": False}

    def fetch() -> Any:
        if state["fail"]:
            raise RuntimeError("hub weg")
        return keys.jwks()

    cache = JwksCache(fetch, ttl_s=300, max_stale_s=900, min_refresh_interval_s=0, clock=clock)
    assert cache.get_key(keys.kid)
    state["fail"] = True
    clock.t += 600  # verouderd maar binnen max_stale → nog bruikbaar
    assert cache.get_key(keys.kid)
    assert cache.status().stale is True
    clock.t += 700  # ruim voorbij ttl + max_stale → weigeren
    with pytest.raises(JwksUnavailable):
        cache.get_key(keys.kid)
    with pytest.raises(UnknownKey):
        _cache(keys).get_key("bestaat-niet")


def test_no_hub_means_no_access() -> None:
    keys = HubKeys()
    auth = Authenticator(JwksCache(None), JwtPolicy(), lambda: None)
    with pytest.raises(ApiError) as exc:
        auth.authenticate(f"Bearer {keys.token(['admin'])}")
    assert _reason(exc) == "jwks_niet_beschikbaar"
    with pytest.raises(ApiError) as exc:
        auth.authenticate("Bearer svc_" + "a" * 40)
    assert _reason(exc) == "niet_gekoppeld"
    with pytest.raises(ApiError):
        auth.authenticate(None)


def test_service_token_constant_time_and_no_user_context() -> None:
    token = "svc_" + "b" * 40
    auth = Authenticator(JwksCache(None), JwtPolicy(), lambda: sha256_hex(token))
    p = auth.authenticate(f"Bearer {token}")
    assert p.kind == "service" and not p.is_user and p.roles == frozenset({"service"})
    with pytest.raises(ApiError):
        auth.authenticate("Bearer svc_" + "c" * 40)


def test_kid_role_always_restrictive() -> None:
    p = Principal(kind="user", subject="kind-1", roles=frozenset({"admin", "kind"}))
    assert p.is_kid is True and p.is_admin is False


def test_parse_jwks_ignores_foreign_keys() -> None:
    keys = HubKeys()
    doc = {"keys": [{"kty": "RSA", "n": "x", "e": "AQAB"}, {"kty": "OKP", "crv": "X25519", "x": "a"}, keys.jwk]}
    assert list(parse_jwks(doc)) == [keys.kid]
    with pytest.raises(ValueError):
        parse_jwks({"geen": "keys"})


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
    assert jwk["kty"] == "OKP" and "d" not in jwk


def test_vault_refs_validated() -> None:
    v = Vault(b"k" * 32)
    with pytest.raises(ValueError):
        v.check_ref("geen-ref")
    assert v.check_ref("vault://immich/api-key") == "vault://immich/api-key"
    assert v.request_hmac(b"x") != v.request_hmac(b"y")
