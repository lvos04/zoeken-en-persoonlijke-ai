"""Koppelen met de Hub en de levenscyclus van die koppeling (contract 1.2 §3.2–§3.8, §4.5).

Twee manieren van koppelen:
1. **Eenmalig enrollment-token** uit de omgeving (``HUB_URL`` + ``HUB_ENROLLMENT_TOKEN`` [+ ``HUB_KEY_FINGERPRINT``]),
   gezet door de Hub bij installatie via de API of door de beheerder in het TrueNAS-formulier. De worker koppelt.
2. **Koppelcode + vingerafdruk** op de statuspagina, alleen zolang de dienst nog **nooit** gekoppeld is (§3.7).

Verzoek ``POST {HUB_URL}/api/v1/services/enroll`` exact volgens §3.3: ``{"app_id", "enrollment_token" | "pairing_code",
"version", "base_url", "public_key_jwk": {kty, crv, x, kid=RFC 7638-thumbprint}}``. Het antwoord wordt volledig
gecontroleerd (alle velden, types, ``schema_version`` major 1, Ed25519-sleutels met ``use``/``alg``/unieke ``kid``,
zelf berekende sleutelset-vingerafdruk) vóór er iets wordt opgeslagen. Opslag: ``/appdata/secrets/hub-link.json``
(``hublink.py``), nooit de database. Daarna bevestigt de dienst met ``GET /api/v1/services/self``.

Deze module praat alleen met de Hub en het koppelbestand; audit en events legt de aanroeper vast.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from . import APP_ID, __version__
from .config import Settings
from .db import Database
from .hubclient import HubClient, HubUrlError, validate_hub_url
from .hublink import HubLinkStore, empty_doc, error, public_view
from .models import ServiceState
from .security.auth import sha256_hex
from .security.pins import (
    PinError,
    apply_rotation,
    keyset_fingerprint,
    make_pin,
    normalize_fingerprint,
    normalize_pairing_code,
    pinned_keys,
    pins_fingerprint,
    validate_hub_jwk,
    verify_rotation_statement,
)
from .security.vault import Vault
from .timeutil import iso, utcnow

log = logging.getLogger("search_ai.enrollment")

ENROLL_PATH = "/api/v1/services/enroll"
SELF_PATH = "/api/v1/services/self"
ROTATE_PATH = "/api/v1/services/self/rotate"
EVENTS_INBOX_PATH = "/api/v1/events/inbox"
KEY_ROTATIONS_PATH = "/api/v1/auth/key-rotations"
ROTATION_HEADER = "Hub-Token-Rotation"
SERVICE_TOKEN = re.compile(r"^svc_[A-Za-z0-9_-]{43}$")
ENROLLMENT_TOKEN = re.compile(r"^enr_[A-Za-z0-9_-]{43}$")

# Antwoordvelden van §3.3 met hun type (bool apart: in Python is ``True`` ook een ``int``).
RESPONSE_FIELDS: dict[str, type] = {
    "schema_version": str,
    "service_id": str,
    "app_id": str,
    "service_token": str,
    "token_type": str,
    "hub_issuer": str,
    "jwks": dict,
    "policy_public_key": dict,
    "keyset_fingerprint": str,
    "jwks_url": str,
    "events_inbox_url": str,
    "key_rotation_url": str,
    "enrollment_replayed": bool,
}


@dataclass
class Outcome:
    ok: bool
    code: str
    retryable: bool = False
    retry_after_s: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class InvalidResponse(ValueError):
    pass


class FingerprintMismatch(ValueError):
    pass


def _uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise InvalidResponse("service_id is geen UUID") from exc


def parse_enroll_response(data: Any, expected_fingerprint: str | None) -> dict[str, Any]:
    """Alle controles van §3.3 vóór opslaan. ``FingerprintMismatch`` of ``InvalidResponse`` bij elke afwijking."""
    if not isinstance(data, dict):
        raise InvalidResponse("antwoord is geen object")
    for name, typ in RESPONSE_FIELDS.items():
        value = data.get(name)
        if typ is bool:
            if not isinstance(value, bool):
                raise InvalidResponse(f"{name} ontbreekt of is geen boolean")
        elif not isinstance(value, typ) or isinstance(value, bool):
            raise InvalidResponse(f"{name} ontbreekt of heeft een verkeerd type")
    if data["schema_version"].split(".")[0] != "1":
        raise InvalidResponse("andere hoofdversie van het schema")
    if data["app_id"] != APP_ID:
        raise InvalidResponse("app_id wijkt af")
    service_id = _uuid(data["service_id"])
    if not SERVICE_TOKEN.fullmatch(data["service_token"]):
        raise InvalidResponse("service_token past niet in het patroon")
    if data["token_type"] != "Bearer" or data["hub_issuer"] != "hub":  # noqa: S105 - vaste waarden
        raise InvalidResponse("token_type of hub_issuer wijkt af")
    keys = data["jwks"].get("keys")
    if not isinstance(keys, list) or not keys:
        raise InvalidResponse("jwks heeft geen sleutels")
    try:
        jwt_keys = [validate_hub_jwk(k) for k in keys]
        policy = validate_hub_jwk(data["policy_public_key"])
        computed = keyset_fingerprint(jwt_keys, policy)
    except PinError as exc:
        raise InvalidResponse(f"ongeldige Hub-sleutel: {exc}") from exc
    kids = [k["kid"] for k in jwt_keys]
    if len(set(kids)) != len(kids):
        raise InvalidResponse("kid niet uniek binnen jwks.keys")
    if normalize_fingerprint(data["keyset_fingerprint"]) != computed:
        raise FingerprintMismatch("keyset_fingerprint in het antwoord ≠ zelf berekend")
    if expected_fingerprint is not None and normalize_fingerprint(expected_fingerprint) != computed:
        raise FingerprintMismatch("vingerafdruk ≠ verwachte vingerafdruk")
    return {
        "service_id": service_id,
        "service_token": data["service_token"],
        "jwt_keys": jwt_keys,
        "policy": policy,
        "fingerprint": computed,
        "replayed": data["enrollment_replayed"],
    }


def _retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("Retry-After", "").strip()
    try:
        return float(min(3600, max(1, int(raw))))
    except ValueError:
        return 60.0


class EnrollmentService:
    def __init__(
        self,
        settings: Settings,
        store: HubLinkStore,
        identity_jwk: dict[str, str],
        hub: HubClient,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.store = store
        self.identity_jwk = {k: identity_jwk[k] for k in ("kty", "crv", "x", "kid")}
        self.hub = hub
        self.now = now
        # gezet door elk geauthenticeerd Hub-antwoord met ``Hub-Token-Rotation: required`` (§3.8)
        self.rotation_required = False
        self._poll_lock = threading.Lock()

    # ------------------------------------------------------------------ status
    def link(self) -> dict[str, Any]:
        return self.store.read_safe()

    def public_state(self) -> dict[str, Any]:
        return public_view(self.store.read_safe())

    def _iso(self) -> str:
        return str(iso(self.now()))

    # ------------------------------------------------------------------ herstart (§3.6 stap 5)
    def on_api_start(self) -> None:
        """Het API-proces doet koppelcode-pogingen synchroon; na een herstart is zo'n geheim weg."""

        def fn(doc: dict[str, Any]) -> dict[str, Any] | None:
            pending = doc.get("pending")
            if not pending or pending.get("kind") != "koppelcode":
                return None
            doc["pending"] = None
            doc["last_error"] = error("koppelen_onderbroken", self._iso())
            return doc

        self.store.update(fn)

    def on_worker_start(self) -> None:
        token = self.settings.enrollment_token
        env_hash = sha256_hex(token) if token else None

        def fn(doc: dict[str, Any]) -> dict[str, Any] | None:
            pending = doc.get("pending")
            if not pending or pending.get("kind") != "token" or pending.get("secret_sha256") == env_hash:
                return None  # zelfde env-token: de worker herhaalt stap 2 (toegestane herhaling bij de Hub)
            doc["pending"] = None
            doc["last_error"] = error("koppelen_onderbroken", self._iso())
            return doc

        self.store.update(fn)

    # ------------------------------------------------------------------ koppelen
    def env_token_open(self) -> bool:
        token = self.settings.enrollment_token
        if not token:
            return False
        return sha256_hex(token) not in (self.store.read_safe().get("used_enrollment_token_sha256") or [])

    def enroll_with_env_token(self) -> Outcome | None:
        """Voor de worker. ``None`` als er niets te doen is (geen token, of dit token is al afgehandeld)."""
        s = self.settings
        token = s.enrollment_token
        if not token:
            return None
        token_hash = sha256_hex(token)
        doc = self.store.read_safe()
        if token_hash in (doc.get("used_enrollment_token_sha256") or []):
            return None
        if not ENROLLMENT_TOKEN.fullmatch(token):
            # een token dat de Hub sowieso met 422 weigert: niet versturen, wel definitief afhandelen
            return self._definitive("token", token_hash, Outcome(False, "hub_weigerde"))
        try:
            hub_url = validate_hub_url(s.hub_url or "", s.allow_loopback_hub)
        except HubUrlError:
            self._note_config_error("ongeldig_hub_adres")
            return Outcome(False, "ongeldig_hub_adres")
        expected = None
        if s.hub_key_fingerprint:
            expected = normalize_fingerprint(s.hub_key_fingerprint)
            if expected is None:
                self._note_config_error("ongeldige_vingerafdruk")
                return Outcome(False, "ongeldige_vingerafdruk")
        pending = doc.get("pending") or {}
        started = pending.get("started_at") if pending.get("secret_sha256") == token_hash else None
        if started:
            try:
                age = (self.now() - datetime.fromisoformat(str(started).replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                age = 0.0
            if age > s.enroll_give_up_s:
                return self._definitive("token", token_hash, Outcome(False, "token_verlopen_na_60_min"))
        return self._attempt("token", token, token_hash, hub_url, expected)

    def enroll_with_code(self, hub_url: str, code: str, fingerprint: str) -> Outcome:
        """Statuspagina (§3.7). Invoer is al gecontroleerd door de aanroeper; hier nog één keer, voor de zekerheid."""
        url = validate_hub_url(hub_url, self.settings.allow_loopback_hub)
        normalized = normalize_pairing_code(code)
        expected = normalize_fingerprint(fingerprint)
        if normalized is None or expected is None:
            raise ValueError("koppelcode of vingerafdruk ongeldig")
        return self._attempt("koppelcode", normalized, sha256_hex(normalized), url, expected)

    def _note_config_error(self, code: str) -> None:
        def fn(doc: dict[str, Any]) -> dict[str, Any] | None:
            if (doc.get("last_error") or {}).get("code") == code:
                return None
            doc["last_error"] = error(code, self._iso())
            return doc

        self.store.update(fn)

    def _attempt(self, kind: str, secret: str, secret_hash: str, hub_url: str, expected: str | None) -> Outcome:
        now = self._iso()

        def set_pending(doc: dict[str, Any]) -> dict[str, Any]:
            p = doc.get("pending")
            if p and p.get("secret_sha256") == secret_hash and p.get("kind") == kind:
                p["attempts"] = int(p.get("attempts") or 0) + 1
                p["hub_url"] = hub_url
            else:
                doc["pending"] = {
                    "kind": kind,
                    "secret_sha256": secret_hash,
                    "hub_url": hub_url,
                    "started_at": now,
                    "attempts": 1,
                }
            return doc

        self.store.update(set_pending)
        body: dict[str, Any] = {
            "app_id": APP_ID,
            "enrollment_token" if kind == "token" else "pairing_code": secret,
            "version": __version__,
            "base_url": self.settings.public_url,
            "public_key_jwk": dict(self.identity_jwk),
        }
        try:
            resp = self.hub.request("POST", hub_url, ENROLL_PATH, json=body, attempts=1)
        except httpx.HTTPError:
            return self._retry(kind, secret_hash, Outcome(False, "hub_onbereikbaar", retryable=True))
        status = resp.status_code
        if status == 429:
            outcome = Outcome(False, "rate_limited", retryable=True, retry_after_s=_retry_after(resp))
            return self._retry(kind, secret_hash, outcome)
        if 300 <= status < 400 or status >= 500:  # redirects volgen we nooit: storing
            return self._retry(kind, secret_hash, Outcome(False, "hub_onbereikbaar", retryable=True))
        if status == 404:
            return self._definitive(kind, secret_hash, Outcome(False, "enroll_endpoint_ontbreekt"))
        if status != 200:  # 401 invalid_enrollment, 413, 422 en andere 4xx: definitief voor dit geheim
            return self._definitive(kind, secret_hash, Outcome(False, "hub_weigerde", detail={"http": status}))
        try:
            parsed = parse_enroll_response(resp.json(), expected)
        except FingerprintMismatch as exc:
            log.warning("koppelen geweigerd: %s", exc)
            return self._definitive(kind, secret_hash, Outcome(False, "hub_vingerafdruk_onjuist"))
        except (InvalidResponse, ValueError) as exc:
            log.warning("koppelen geweigerd: ongeldig antwoord van de Hub (%s)", exc)
            return self._definitive(kind, secret_hash, Outcome(False, "hub_antwoord_ongeldig"))
        self._store_link(kind, secret_hash, hub_url, parsed)
        log.info("gekoppeld met de Hub op %s via %s (vingerafdruk %s)", hub_url, kind, parsed["fingerprint"])
        confirmed = self.confirm()
        return Outcome(
            True,
            "gekoppeld",
            detail={"replayed": parsed["replayed"], "confirmed": confirmed.ok, "fingerprint": parsed["fingerprint"]},
        )

    def _store_link(self, kind: str, secret_hash: str, hub_url: str, parsed: dict[str, Any]) -> None:
        now = self._iso()

        def fn(doc: dict[str, Any]) -> dict[str, Any]:
            old_pins = doc.get("pins") if doc.get("state") == "gekoppeld" else None
            new = empty_doc(APP_ID) if doc.get("corrupt") else doc
            new.pop("corrupt", None)
            pins = {
                "jwt": [make_pin(k, source="enroll", pinned_at=now) for k in parsed["jwt_keys"]],
                "policy": [make_pin(parsed["policy"], source="enroll", pinned_at=now)],
                "keyset_fingerprint": parsed["fingerprint"],
                "rotation_sequence": 0,
            }
            token = parsed["service_token"]
            new.update(
                state="gekoppeld",
                hub_url=hub_url,
                service_id=parsed["service_id"],
                via=kind,
                first_linked_at=new.get("first_linked_at") or now,
                linked_at=now,
                confirmed_at=None,
                service_token=token,
                service_token_last4=token[-4:],
                pins=pins,
                pending=None,
                last_error=None,
            )
            if old_pins:
                new["previous_pins"] = old_pins
            if kind == "token":
                used = list(new.get("used_enrollment_token_sha256") or [])
                if secret_hash not in used:
                    used.append(secret_hash)
                new["used_enrollment_token_sha256"] = used
            return new

        self.store.update(fn)

    def _clear_pending(self, doc: dict[str, Any], secret_hash: str) -> bool:
        pending = doc.get("pending")
        if pending and pending.get("secret_sha256") == secret_hash:
            doc["pending"] = None
            return True
        return False

    def _definitive(self, kind: str, secret_hash: str, outcome: Outcome) -> Outcome:
        """§3.6 stap 4: ``pending: null``, ``last_error``, env-token als afgehandeld; koppeling ongemoeid."""
        now = self._iso()

        def fn(doc: dict[str, Any]) -> dict[str, Any]:
            self._clear_pending(doc, secret_hash)
            doc["last_error"] = error(outcome.code, now)
            if kind == "token":
                used = list(doc.get("used_enrollment_token_sha256") or [])
                if secret_hash not in used:
                    used.append(secret_hash)
                doc["used_enrollment_token_sha256"] = used
            return doc

        self.store.update(fn)
        log.warning("koppelen met de Hub definitief mislukt: %s", outcome.code)
        return outcome

    def _retry(self, kind: str, secret_hash: str, outcome: Outcome) -> Outcome:
        """Storing: env-token blijft ``pending`` (de worker probeert het later opnieuw); een koppelcode niet (het
        geheim wordt niet bewaard), de gebruiker kan hem opnieuw invoeren."""
        now = self._iso()

        def fn(doc: dict[str, Any]) -> dict[str, Any]:
            if kind == "koppelcode":
                self._clear_pending(doc, secret_hash)
            doc["last_error"] = error(outcome.code, now)
            return doc

        self.store.update(fn)
        log.warning("koppelen met de Hub lukte nog niet: %s", outcome.code)
        if kind == "koppelcode":
            outcome.retryable = False
        return outcome

    # ------------------------------------------------------------------ dienst → Hub met het service-token
    def _authed(self, method: str, path: str, token: str, *, json: Any = None) -> httpx.Response:
        doc = self.store.read_safe()
        resp = self.hub.request(
            method, str(doc.get("hub_url")), path, json=json, headers={"Authorization": f"Bearer {token}"}, attempts=1
        )
        if resp.headers.get(ROTATION_HEADER, "").strip().lower() == "required":
            self.rotation_required = True
        return resp

    def authed_request(self, method: str, path: str, *, json: Any = None) -> httpx.Response | None:
        """Voor andere modules (events): ``None`` als de dienst niet gekoppeld is. Een 401 → ``check_revoked``."""
        doc = self.store.read_safe()
        token = doc.get("service_token")
        if doc.get("state") != "gekoppeld" or not token:
            return None
        resp = self._authed(method, path, str(token), json=json)
        if resp.status_code == 401:
            self.check_revoked()
        return resp

    def confirm(self) -> Outcome:
        """§3.6 stap 3: bevestigen met ``GET /api/v1/services/self`` (zet ``confirmed_at``)."""
        doc = self.store.read_safe()
        token = doc.get("service_token")
        if doc.get("state") != "gekoppeld" or not token:
            return Outcome(False, "niet_gekoppeld")
        try:
            resp = self._authed("GET", SELF_PATH, str(token))
        except httpx.HTTPError:
            return Outcome(False, "hub_onbereikbaar", retryable=True)
        if resp.status_code == 401:
            self._mark_revoked(str(token))
            return Outcome(False, "ingetrokken_door_hub")
        if resp.status_code != 200:
            return Outcome(False, "hub_onbereikbaar", retryable=True, detail={"http": resp.status_code})
        try:
            data = resp.json()
            if _uuid(data.get("service_id")) != doc.get("service_id") or data.get("app_id") != APP_ID:
                raise InvalidResponse("services/self hoort bij een andere registratie")
        except (ValueError, AttributeError) as exc:
            log.warning("bevestigen bij de Hub mislukt: %s", exc)
            return Outcome(False, "hub_antwoord_ongeldig", retryable=True)
        hub_fp = normalize_fingerprint(str(data.get("keyset_fingerprint", "")))
        if hub_fp and hub_fp != (doc.get("pins") or {}).get("keyset_fingerprint"):
            log.warning("de Hub meldt een andere sleutelset-vingerafdruk; vertrouwen verandert alleen via rotatie")
        now = self._iso()

        def fn(d: dict[str, Any]) -> dict[str, Any] | None:
            if d.get("state") != "gekoppeld" or d.get("service_token") != token:
                return None
            if d.get("confirmed_at"):
                return None
            d["confirmed_at"] = now
            return d

        self.store.update(fn)
        return Outcome(True, "bevestigd")

    def check_revoked(self) -> Outcome:
        """§3.8: na een 401 van de Hub eerst ``services/self``; alleen als ook dat 401 geeft → ``ingetrokken``.
        Netwerkfouten, 5xx en 429 zijn nooit een intrekking."""
        doc = self.store.read_safe()
        token = doc.get("service_token")
        if doc.get("state") != "gekoppeld" or not token:
            return Outcome(False, "niet_gekoppeld")
        try:
            resp = self._authed("GET", SELF_PATH, str(token))
        except httpx.HTTPError:
            return Outcome(False, "hub_onbereikbaar", retryable=True)
        if resp.status_code == 401:
            self._mark_revoked(str(token))
            return Outcome(True, "ingetrokken_door_hub")
        return Outcome(False, "niet_ingetrokken", detail={"http": resp.status_code})

    def _mark_revoked(self, token: str) -> None:
        now = self._iso()

        def fn(doc: dict[str, Any]) -> dict[str, Any] | None:
            if doc.get("state") != "gekoppeld" or doc.get("service_token") != token:
                return None  # intussen geroteerd of opnieuw gekoppeld: dit 401 gold een oud token
            doc.update(
                state="ingetrokken",
                previous_pins=doc.get("pins"),
                pins=None,
                service_token=None,
                service_token_last4=None,
                confirmed_at=None,
                last_error=error("ingetrokken_door_hub", now),
            )
            return doc

        self.store.update(fn)
        log.warning("de Hub heeft de koppeling ingetrokken; elke Hub-JWT wordt vanaf nu geweigerd")

    def rotate_service_token(self) -> Outcome:
        """§3.8: ``POST /api/v1/services/self/rotate``, nieuw token atomair schrijven, bevestigen met
        ``services/self``. Mislukt het halverwege, dan vraagt de Hub opnieuw rotatie (``Hub-Token-Rotation``)."""
        doc = self.store.read_safe()
        old = doc.get("service_token")
        if doc.get("state") != "gekoppeld" or not old:
            return Outcome(False, "niet_gekoppeld")
        try:
            resp = self._authed("POST", ROTATE_PATH, str(old))
        except httpx.HTTPError:
            return Outcome(False, "hub_onbereikbaar", retryable=True)
        if resp.status_code == 401:
            return Outcome(False, self.check_revoked().code)
        if resp.status_code != 200:
            return Outcome(False, "hub_onbereikbaar", retryable=True, detail={"http": resp.status_code})
        try:
            data = resp.json()
            new = data.get("service_token")
            if _uuid(data.get("service_id")) != doc.get("service_id") or not SERVICE_TOKEN.fullmatch(str(new)):
                raise InvalidResponse("rotatie-antwoord ongeldig")
        except (ValueError, AttributeError) as exc:
            log.warning("tokenrotatie: %s", exc)
            return Outcome(False, "hub_antwoord_ongeldig", retryable=True)

        def fn(d: dict[str, Any]) -> dict[str, Any] | None:
            if d.get("state") != "gekoppeld" or d.get("service_token") != old:
                return None
            d.update(service_token=new, service_token_last4=str(new)[-4:], confirmed_at=None)
            return d

        self.store.update(fn)
        confirmed = self.confirm()
        if confirmed.ok:
            self.rotation_required = False
        log.info("service-token geroteerd (bevestigd: %s)", confirmed.ok)
        return Outcome(True, "geroteerd", detail={"confirmed": confirmed.ok})

    # ------------------------------------------------------------------ sleutelrotatie (§4.5)
    def poll_key_rotations(self) -> Outcome:
        """``GET /api/v1/auth/key-rotations?after=<sequence>`` (zonder authenticatie: de handtekening is het bewijs).
        Statements worden in volgorde gecontroleerd tegen de **op dat moment** gepinde sleutels; het eerste ongeldige
        statement stopt het toepassen."""
        if not self._poll_lock.acquire(blocking=False):
            return Outcome(False, "al_bezig")
        try:
            doc = self.store.read_safe()
            if doc.get("state") != "gekoppeld" or not doc.get("pins"):
                return Outcome(False, "niet_gekoppeld")
            seq = int(doc["pins"].get("rotation_sequence") or 0)
            try:
                resp = self.hub.request("GET", str(doc["hub_url"]), f"{KEY_ROTATIONS_PATH}?after={seq}")
            except httpx.HTTPError:
                return Outcome(False, "hub_onbereikbaar", retryable=True)
            if resp.status_code != 200:
                return Outcome(False, "hub_onbereikbaar", retryable=True, detail={"http": resp.status_code})
            try:
                items = resp.json().get("items")
                if not isinstance(items, list):
                    raise ValueError("items ontbreekt")
            except (ValueError, AttributeError):
                return Outcome(False, "hub_antwoord_ongeldig", retryable=True)
            if not items:
                return Outcome(True, "geen_rotaties")
            return self._apply_rotations(items, seq)
        finally:
            self._poll_lock.release()

    def _apply_rotations(self, items: list[Any], seq: int) -> Outcome:
        now = self._iso()
        result: dict[str, Any] = {"applied": 0, "error": None}

        def fn(doc: dict[str, Any]) -> dict[str, Any] | None:
            pins = doc.get("pins")
            if doc.get("state") != "gekoppeld" or not pins or int(pins.get("rotation_sequence") or 0) != seq:
                return None  # intussen gewijzigd door een ander proces: volgende poll
            for item in sorted(
                (i for i in items if isinstance(i, dict) and isinstance(i.get("sequence"), int)),
                key=lambda i: int(i["sequence"]),
            ):
                if int(item["sequence"]) <= int(pins.get("rotation_sequence") or 0):
                    continue
                try:
                    rot = verify_rotation_statement(
                        item.get("statement"), pins, int(pins.get("rotation_sequence") or 0) + 1
                    )
                    if rot.sequence != item["sequence"]:
                        raise PinError("sequence van het item ≠ sequence in het statement")
                    pins = apply_rotation(pins, rot, now=now)
                    result["applied"] += 1
                except PinError as exc:
                    result["error"] = str(exc)
                    break
            if result["applied"] == 0 and result["error"] is None:
                return None
            doc["pins"] = pins
            if result["error"] is not None:
                doc["last_error"] = error("sleutelrotatie_ongeldig", now)
            return doc

        self.store.update(fn)
        if result["error"] is not None:
            log.error("sleutelrotatie van de Hub geweigerd (incident): %s", result["error"])
            return Outcome(False, "sleutelrotatie_ongeldig", detail=result)
        log.info("sleutelrotatie toegepast: %s statement(s)", result["applied"])
        return Outcome(True, "rotatie_toegepast", detail=result)

    # ------------------------------------------------------------------ ontkoppelen (§3.8)
    def unlink(self) -> dict[str, Any]:
        def fn(doc: dict[str, Any]) -> dict[str, Any] | None:
            if doc.get("state") != "gekoppeld":
                return None
            doc.update(
                state="ontkoppeld",
                previous_pins=doc.get("pins"),
                pins=None,
                service_token=None,
                service_token_last4=None,
                confirmed_at=None,
                pending=None,
            )
            return doc

        return public_view(self.store.update(fn))


# ------------------------------------------------------------------ overzetting uit de 1.1-opslag (§3.5)
LEGACY_STATE_KEYS = ("hub_link", "hub_jwks", "hub_policy_key")
LEGACY_TOKEN_REF = "vault://hub/service-token"  # noqa: S105 - referentie, geen geheim


def _legacy_doc(
    state: dict[str, Any], jwks: Any, policy: Any, token: str | None, allow_loopback: bool
) -> dict[str, Any]:
    doc = empty_doc(APP_ID)
    used = [h for h in (state.get("used_token_sha256"), state.get("rejected_token_sha256")) if isinstance(h, str)]
    doc["used_enrollment_token_sha256"] = list(dict.fromkeys(used))
    was_linked = state.get("status") == "gekoppeld" or state.get("reason") == "ontkoppeld"
    first = state.get("enrolled_at") or state.get("unpaired_at")
    if not was_linked:
        return doc
    doc.update(first_linked_at=first or str(iso(utcnow())), hub_url=state.get("hub_url"), via=state.get("via"))
    if state.get("status") != "gekoppeld":
        doc["state"] = "ontkoppeld"
        return doc
    try:
        validate_hub_url(str(state.get("hub_url") or ""), allow_loopback)
        service_id = _uuid(state.get("service_id"))
        jwt_keys = [validate_hub_jwk(k) for k in (jwks or {}).get("keys") or []]
        policy_jwk = validate_hub_jwk((policy or {}).get("jwk"))
        if not jwt_keys or not token or not SERVICE_TOKEN.fullmatch(token):
            raise InvalidResponse("onvolledig")
        fp = keyset_fingerprint(jwt_keys, policy_jwk)
    except (InvalidResponse, PinError, HubUrlError, AttributeError, TypeError):
        doc.update(state="ontkoppeld", last_error=error("migratie_onvolledig", str(iso(utcnow()))))
        return doc
    at = str(state.get("enrolled_at") or iso(utcnow()))
    doc.update(
        state="gekoppeld",
        service_id=service_id,
        linked_at=at,
        confirmed_at=None,  # de worker bevestigt het overgezette token met services/self
        service_token=token,
        service_token_last4=token[-4:],
        pins={
            "jwt": [make_pin(k, source="migratie_1.1", pinned_at=at) for k in jwt_keys],
            "policy": [make_pin(policy_jwk, source="migratie_1.1", pinned_at=at)],
            "keyset_fingerprint": fp,
            "rotation_sequence": 0,
        },
    )
    return doc


def migrate_legacy_link(store: HubLinkStore, db: Database, vault: Vault, allow_loopback: bool = False) -> str:
    """Eenmalig: een koppeling uit de database (1.1: ``service_state`` + vault) naar ``hub-link.json``, daarna uit
    de database verwijderen. Bestaat het bestand al, dan wordt alleen de oude database-kopie gewist (bv. na een
    database-herstel): een herstel brengt nooit een oude koppeling terug."""
    with db.sessions() as s:
        rows = {k: s.get(ServiceState, k) for k in LEGACY_STATE_KEYS}
        try:
            token = vault.get(s, LEGACY_TOKEN_REF)
        except Exception:  # onleesbaar (andere vault-sleutel): niet overnemen, wel wissen
            token = None
        has_token_row = _vault_row_exists(s, LEGACY_TOKEN_REF)
        if not any(rows.values()) and not has_token_row:
            return "niets"
        result = "gewist"
        if not store.path.exists():
            state = dict(rows["hub_link"].value) if rows["hub_link"] is not None else {}
            jwks = (rows["hub_jwks"].value or {}).get("jwks") if rows["hub_jwks"] is not None else None
            policy = rows["hub_policy_key"].value if rows["hub_policy_key"] is not None else None
            new = _legacy_doc(state, jwks, policy, token, allow_loopback)
            store.update(lambda _doc: new)
            result = f"overgezet:{new['state']}"
        for row in rows.values():
            if row is not None:
                s.delete(row)
        vault.delete(s, LEGACY_TOKEN_REF)
        s.commit()
    log.info("koppeling uit de vorige versie: %s (database-kopie verwijderd)", result)
    return result


def _vault_row_exists(session: Session, ref: str) -> bool:
    from .models import VaultSecret

    return session.get(VaultSecret, ref) is not None


# ------------------------------------------------------------------ achtergrondpoll bij een onbekende kid (§4.5)
class KeyRotationTrigger:
    """Een JWT met een onbekende ``kid`` krijgt direct 401; daarnaast start dit hooguit 1× per 30 s een poll op de
    achtergrond (het verzoek wacht daar niet op)."""

    def __init__(
        self, enrollment: EnrollmentService, min_interval_s: float, clock: Callable[[], float] = time.monotonic
    ):
        self.enrollment, self.min_interval_s, self.clock = enrollment, min_interval_s, clock
        self._last: float | None = None
        self._lock = threading.Lock()
        self.started = 0

    def __call__(self) -> None:
        with self._lock:
            now = self.clock()
            if self._last is not None and now - self._last < self.min_interval_s:
                return
            self._last = now
            self.started += 1
        threading.Thread(target=self._run, name="sleutelrotatie-poll", daemon=True).start()

    def _run(self) -> None:
        try:
            self.enrollment.poll_key_rotations()
        except Exception:  # nooit de API laten vallen
            log.exception("achtergrondpoll van sleutelrotaties mislukt")


def pinned_kids(doc: dict[str, Any]) -> dict[str, list[str]]:
    pins = doc.get("pins") if doc.get("state") == "gekoppeld" else None
    return {p: sorted(pinned_keys(pins, p)) for p in ("jwt", "policy")}


def pins_ok(doc: dict[str, Any]) -> bool:
    pins = doc.get("pins")
    if doc.get("state") != "gekoppeld" or not pins:
        return False
    try:
        return (
            bool(pinned_keys(pins, "jwt"))
            and bool(pinned_keys(pins, "policy"))
            and (pins_fingerprint(pins) == pins.get("keyset_fingerprint"))
        )
    except (PinError, KeyError, TypeError):
        return False
