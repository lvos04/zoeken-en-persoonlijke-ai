"""Gepinde Hub-sleutels, thumbprint en sleutelset-vingerafdruk (contract 1.3 §4.5).

- **Vertrouwensset** = de gepinde sleutels in ``/appdata/secrets/hub-link.json`` (``pins.jwt`` voor JWT's,
  ``pins.policy`` voor ``ExecutionRequest``s). Een via HTTP opgehaalde JWKS voegt **nooit** vertrouwen toe; deze dienst
  haalt de JWKS daarom helemaal niet meer op.
- **Thumbprint** = RFC 7638 over ``{"crv","kty","x"}``.
- **Sleutelset-vingerafdruk** = SHA-256 over
  ``"jl-hub-keyset-v1\\njwt:<thumbprints gesorteerd>\\npolicy:<thumbprints gesorteerd>"``, eerste 80 bit als
  Crockford-base32, ``XXXX-XXXX-XXXX-XXXX``. Sinds 1.3 tellen **alle** gepinde policy-sleutels mee (tijdens een
  policy-rotatie zijn dat er twee); met één policy-sleutel is de uitkomst gelijk aan 1.2.
- **Rotatie** alleen via een met een op dat moment gepinde sleutel van hetzelfde doel ondertekend statement
  (``GET /api/v1/auth/key-rotations``), in volgorde, zonder gaten; nooit de laatste sleutel van een doel intrekken.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jwt.api_jws import PyJWS

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
KEYSET_PREFIX = "jl-hub-keyset-v1"
PURPOSES = ("jwt", "policy")
ROTATION_TYP = "hub-key-rotation+jws"
_FINGERPRINT = re.compile(r"^[0-9A-HJKMNP-TV-Z]{16}$")
_PAIRING = re.compile(r"^[0-9A-HJKMNP-TV-Z]{12}$")
MAX_KID = 200


class PinError(ValueError):
    """Ongeldige sleutel of ongeldig statement (de boodschap is voor de log, niet voor gebruikers)."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", value):
        raise PinError("geen base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise PinError("geen base64url") from exc


def _x_bytes(x: Any) -> bytes:
    raw = b64url_decode(x)
    if len(raw) != 32 or b64url(raw) != x:
        raise PinError("x moet 32 bytes in canonieke base64url zijn")
    return raw


def jwk_thumbprint(jwk: Mapping[str, Any]) -> str:
    """RFC 7638-thumbprint van een publieke Ed25519-JWK (zonder ``d``)."""
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or "d" in jwk:
        raise PinError("alleen een publieke Ed25519-JWK (kty=OKP, crv=Ed25519, zonder d)")
    x = jwk.get("x")
    _x_bytes(x)
    canonical = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": x}, separators=(",", ":"), sort_keys=True)
    return b64url(hashlib.sha256(canonical.encode("utf-8")).digest())


def _crockford(data: bytes) -> str:
    bits = "".join(f"{byte:08b}" for byte in data)
    bits += "0" * (-len(bits) % 5)
    return "".join(CROCKFORD[int(bits[i : i + 5], 2)] for i in range(0, len(bits), 5))


def _sorted_bytes(thumbprints: Iterable[str]) -> list[str]:
    return sorted(thumbprints, key=lambda t: t.encode("utf-8"))  # gesorteerd op bytewaarde (§4.5)


def fingerprint_from_thumbprints(jwt_thumbprints: Iterable[str], policy_thumbprints: Iterable[str]) -> str:
    """§4.5 (1.3): beide lijsten gesorteerd op bytewaarde; elke gepinde policy-sleutel telt mee."""
    jwt_list = _sorted_bytes(jwt_thumbprints)
    policy_list = _sorted_bytes(policy_thumbprints)
    if not jwt_list:
        raise PinError("minstens één JWT-sleutel nodig")
    if not policy_list:
        raise PinError("minstens één policy-sleutel nodig")
    text = f"{KEYSET_PREFIX}\njwt:{','.join(jwt_list)}\npolicy:{','.join(policy_list)}"
    code = _crockford(hashlib.sha256(text.encode("utf-8")).digest()[:10])
    return "-".join(code[i : i + 4] for i in range(0, 16, 4))


def keyset_fingerprint(
    jwt_keys: Iterable[Mapping[str, Any]], policy_keys: Mapping[str, Any] | Iterable[Mapping[str, Any]]
) -> str:
    """Vingerafdruk over JWK's. ``policy_keys`` is één JWK (enroll-antwoord) of een lijst (na een policy-rotatie)."""
    policies = [policy_keys] if isinstance(policy_keys, Mapping) else list(policy_keys)
    return fingerprint_from_thumbprints((jwk_thumbprint(k) for k in jwt_keys), (jwk_thumbprint(k) for k in policies))


def normalize_code(value: str) -> str:
    """Invoer van een mens vergelijkbaar maken: hoofdletters, alleen letters/cijfers, ``O→0`` en ``I/L→1``."""
    cleaned = "".join(ch for ch in value.upper() if ch.isascii() and ch.isalnum())
    return cleaned.replace("O", "0").replace("I", "1").replace("L", "1")


def normalize_fingerprint(value: str) -> str | None:
    """``XXXX-XXXX-XXXX-XXXX`` na normalisatie, of ``None`` als het geen geldige vingerafdruk is."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    code = normalize_code(value)
    if not _FINGERPRINT.fullmatch(code):
        return None
    return "-".join(code[i : i + 4] for i in range(0, 16, 4))


def normalize_pairing_code(value: str) -> str | None:
    """Koppelcode (§3.3): ≤ 32 tekens, na normalisatie precies 12 Crockford-tekens → ``XXXX-XXXX-XXXX``."""
    if not isinstance(value, str) or len(value) > 32:
        return None
    code = normalize_code(value)
    if not _PAIRING.fullmatch(code):
        return None
    return f"{code[:4]}-{code[4:8]}-{code[8:]}"


def validate_hub_jwk(jwk: Any) -> dict[str, str]:
    """Een Hub-sleutel uit het enroll-antwoord of een rotatie-statement (§3.3): Ed25519, zonder ``d``,
    ``use:"sig"``, ``alg:"EdDSA"`` en een niet-lege ``kid``. Geeft de genormaliseerde JWK terug."""
    if not isinstance(jwk, Mapping):
        raise PinError("JWK is geen object")
    if "d" in jwk:
        raise PinError("JWK bevat een privésleutel (d)")
    if jwk.get("use") != "sig" or jwk.get("alg") != "EdDSA":
        raise PinError("JWK moet use=sig en alg=EdDSA hebben")
    kid = jwk.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > MAX_KID:
        raise PinError("JWK mist een geldige kid")
    jwk_thumbprint(jwk)
    return {"kty": "OKP", "crv": "Ed25519", "x": str(jwk["x"]), "kid": kid, "use": "sig", "alg": "EdDSA"}


def make_pin(jwk: Mapping[str, Any], *, source: str, pinned_at: str) -> dict[str, Any]:
    return {
        "kid": str(jwk["kid"]),
        "jwk": {"kty": "OKP", "crv": "Ed25519", "x": str(jwk["x"])},
        "thumbprint": jwk_thumbprint(jwk),
        "pinned_at": pinned_at,
        "source": source,
    }


def pins_fingerprint(pins: Mapping[str, Any]) -> str:
    """Vingerafdruk over de **hele** gepinde set (contract 1.3 §4.5): na een policy-rotatie met alleen ``add`` zijn er
    tijdelijk twee policy-sleutels en tellen ze allebei mee (in 1.2 telde hier de laatst gepinde, ADR-0011)."""
    policy_pins = pins.get("policy") or []
    if not policy_pins:
        raise PinError("geen gepinde policy-sleutel")
    return fingerprint_from_thumbprints(
        (p["thumbprint"] for p in pins.get("jwt") or []), (p["thumbprint"] for p in policy_pins)
    )


@dataclass(frozen=True)
class PinnedKey:
    kid: str
    thumbprint: str
    key: Ed25519PublicKey


def pinned_keys(pins: Mapping[str, Any] | None, purpose: str) -> dict[str, PinnedKey]:
    """``{kid: sleutel}`` voor één doel; ongeldige pins worden overgeslagen (en dus nooit vertrouwd)."""
    out: dict[str, PinnedKey] = {}
    for pin in (pins or {}).get(purpose) or []:
        try:
            jwk = pin["jwk"]
            thumb = jwk_thumbprint(jwk)
            if thumb != pin.get("thumbprint"):
                continue
            out[str(pin["kid"])] = PinnedKey(
                str(pin["kid"]), thumb, Ed25519PublicKey.from_public_bytes(_x_bytes(jwk["x"]))
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ------------------------------------------------------------------ rotatie-statements (§4.5)
@dataclass(frozen=True)
class Rotation:
    sequence: int
    purpose: str
    add: list[dict[str, str]]
    retire: list[str]


def verify_rotation_statement(statement: Any, pins: Mapping[str, Any], expected_sequence: int) -> Rotation:
    """Controleert één statement tegen de **huidige** pins. Elke afwijking → ``PinError`` (dan stopt het toepassen)."""
    if not isinstance(statement, str) or statement.count(".") != 2 or len(statement) > 64 * 1024:
        raise PinError("statement is geen compacte JWS")
    try:
        header = jwt.get_unverified_header(statement)
    except jwt.PyJWTError as exc:
        raise PinError("JWS-header onleesbaar") from exc
    if header.get("alg") != "EdDSA" or header.get("typ") != ROTATION_TYP:
        raise PinError("verkeerde alg of typ in de JWS-header")
    kid = header.get("kid")
    try:
        body = json.loads(b64url_decode(statement.split(".")[1]))
    except (ValueError, PinError) as exc:
        raise PinError("payload onleesbaar") from exc
    if not isinstance(body, dict):
        raise PinError("payload is geen object")
    purpose = body.get("purpose")
    if purpose not in PURPOSES:
        raise PinError("onbekend doel")
    signer = pinned_keys(pins, purpose).get(kid) if isinstance(kid, str) else None
    if signer is None:
        raise PinError("statement niet ondertekend met een gepinde sleutel van hetzelfde doel")
    try:
        payload = json.loads(PyJWS().decode(statement, key=signer.key, algorithms=["EdDSA"]))
    except (jwt.PyJWTError, ValueError) as exc:
        raise PinError("handtekening ongeldig") from exc
    if not isinstance(payload, dict) or payload != body:
        raise PinError("payload ongeldig")
    if payload.get("type") != "hub_key_rotation" or payload.get("iss") != "hub":
        raise PinError("verkeerd type of iss")
    seq = payload.get("sequence")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq != expected_sequence:
        raise PinError(f"sequence {seq!r} ≠ verwacht {expected_sequence} (gat of volgorde)")
    if not isinstance(payload.get("issued_at"), str):
        raise PinError("issued_at ontbreekt")
    add_raw, retire_raw = payload.get("add"), payload.get("retire")
    if not isinstance(add_raw, list) or not isinstance(retire_raw, list):
        raise PinError("add en retire moeten lijsten zijn")
    add = [validate_hub_jwk(k) for k in add_raw]
    retire = [str(k) for k in retire_raw if isinstance(k, str)]
    if len(retire) != len(retire_raw):
        raise PinError("retire bevat geen kid")
    current = pinned_keys(pins, purpose)
    kids = [k["kid"] for k in add]
    if len(set(kids)) != len(kids) or any(k in current for k in kids):
        raise PinError("dubbele kid in add")
    if any(k not in current for k in retire):
        raise PinError("retire noemt een sleutel die niet gepind is")
    if not (set(current) - set(retire)) and not add:
        raise PinError("de laatste sleutel van een doel mag niet worden ingetrokken")
    return Rotation(sequence=seq, purpose=str(purpose), add=add, retire=retire)


def apply_rotation(pins: Mapping[str, Any], rot: Rotation, *, now: str) -> dict[str, Any]:
    new = {k: list(v) if isinstance(v, list) else v for k, v in pins.items()}
    kept = [p for p in new.get(rot.purpose) or [] if p.get("kid") not in rot.retire]
    kept += [make_pin(k, source=f"rotation:{rot.sequence}", pinned_at=now) for k in rot.add]
    new[rot.purpose] = kept
    new["rotation_sequence"] = rot.sequence
    new["keyset_fingerprint"] = pins_fingerprint(new)
    return new
