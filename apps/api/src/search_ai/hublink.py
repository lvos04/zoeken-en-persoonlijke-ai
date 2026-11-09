"""Koppelstatus met de Hub in ``/appdata/secrets/hub-link.json`` (contract 1.2 §3.5).

- Eén bestand, 0600, in ``/appdata/secrets`` (0700). Schrijven: nieuw bestand in dezelfde map (``O_CREAT|O_EXCL``, 0600)
  → ``fsync`` → ``rename`` → ``fsync`` van de map, onder een exclusieve vergrendeling op ``hub-link.lock``. Lezen-wijzigen-
  schrijven gebeurt altijd binnen die vergrendeling (API en worker schrijven allebei).
- Elk proces leest het bestand opnieuw zodra inode, mtime of grootte verandert (``stat`` bij elk gebruik), zodat een
  ontkoppeling, intrekking of rotatie door het ene proces direct voor het andere geldt.
- **Niet** in de database: een database-herstel brengt dus geen oude koppeling, oude pins of ingetrokken token terug, en
  DB-exports bevatten geen credentials. Het service-token staat onversleuteld in het bestand, net als ``vault.key``.
- Is het bestand onleesbaar, dan geldt de dienst als **niet gekoppeld** (fail-closed) zonder koppelformulier; alleen
  een nieuw ``HUB_ENROLLMENT_TOKEN`` (TrueNAS-beheerder) kan het herstellen.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.2"
FILE_NAME = "hub-link.json"
LOCK_NAME = "hub-link.lock"
STATES = ("nooit_gekoppeld", "gekoppeld", "ontkoppeld", "ingetrokken")
MAX_USED_TOKENS = 20

# Vaste codes voor ``last_error`` met een vaste Nederlandse melding (§3.5); nooit weerspiegelde invoer.
ERROR_MESSAGES = {
    "hub_onbereikbaar": "De Hub was niet bereikbaar. Er wordt later opnieuw geprobeerd.",
    "hub_weigerde": "De Hub weigerde het koppeltoken of de koppelcode (verlopen, al gebruikt of ongeldig). "
    "Maak een nieuwe in de Hub.",
    "hub_antwoord_ongeldig": "Het antwoord van de Hub was onvolledig of ongeldig; er is niets opgeslagen.",
    "hub_vingerafdruk_onjuist": "De vingerafdruk van de Hub klopt niet; er is niets opgeslagen. Vergelijk de "
    "vingerafdruk met die in de Hub.",
    "enroll_endpoint_ontbreekt": "Deze Hub-versie kent aanmelden niet.",
    "rate_limited": "De Hub vraagt even te wachten (te veel pogingen). Er wordt later opnieuw geprobeerd.",
    "koppelen_onderbroken": "Een koppelpoging is door een herstart onderbroken. Vul de koppelcode opnieuw in of "
    "maak een nieuw koppeltoken in de Hub.",
    "token_verlopen_na_60_min": "Koppelen is na 60 minuten gestopt. Maak een nieuw koppeltoken in de Hub.",
    "ingetrokken_door_hub": "De Hub heeft deze koppeling ingetrokken. Opnieuw koppelen gaat via de Hub.",
    "ongeldig_hub_adres": "Het Hub-adres (HUB_URL) is ongeldig. Gebruik http(s)://<NAS-adres>:<poort> zonder pad.",
    "ongeldige_vingerafdruk": "HUB_KEY_FINGERPRINT heeft geen geldig formaat (XXXX-XXXX-XXXX-XXXX).",
    "koppelstatus_onleesbaar": "De koppelstatus in /appdata/secrets is onleesbaar. Koppel opnieuw via de Hub.",
    "migratie_onvolledig": "De koppeling uit de vorige versie was onvolledig en is niet overgenomen. Koppel opnieuw "
    "via de Hub.",
    "sleutelrotatie_ongeldig": "Een sleutelrotatie van de Hub was ongeldig en is niet toegepast (zie de app-log).",
}

# Witte lijst van §3.5: alleen deze velden verlaten ooit de dienst (statuspagina, /readyz, /connector/v1/hub-link).
PUBLIC_FIELDS = (
    "state",
    "koppelen_bezig",
    "hub_url",
    "service_id",
    "via",
    "linked_at",
    "first_linked_at",
    "keyset_fingerprint",
    "service_token_last4",
    "last_error",
)


class HubLinkCorrupt(RuntimeError):
    """``hub-link.json`` bestaat maar is geen geldig koppelbestand."""


def empty_doc(app_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "nooit_gekoppeld",
        "app_id": app_id,
        "hub_url": None,
        "service_id": None,
        "via": None,
        "first_linked_at": None,
        "linked_at": None,
        "confirmed_at": None,
        "service_token": None,
        "service_token_last4": None,
        "pins": None,
        "previous_pins": None,
        "pending": None,
        "used_enrollment_token_sha256": [],
        "last_error": None,
    }


def _validate(doc: Any, app_id: str) -> dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("state") not in STATES or doc.get("app_id") != app_id:
        raise HubLinkCorrupt("geen geldig koppelbestand")
    if not str(doc.get("schema_version", "")).startswith("1."):
        raise HubLinkCorrupt("onbekende schemaversie")
    base = empty_doc(app_id)
    base.update(doc)
    if base["state"] == "gekoppeld" and (not base.get("service_token") or not isinstance(base.get("pins"), dict)):
        raise HubLinkCorrupt("gekoppeld zonder token of pins")
    if not isinstance(base.get("used_enrollment_token_sha256"), list):
        raise HubLinkCorrupt("used_enrollment_token_sha256 is geen lijst")
    return base


class HubLinkStore:
    """Leest en schrijft ``hub-link.json``. Thread-safe en veilig tussen processen (``flock``)."""

    def __init__(self, secrets_dir: Path, app_id: str) -> None:
        self.dir = Path(secrets_dir)
        self.path = self.dir / FILE_NAME
        self.lock_path = self.dir / LOCK_NAME
        self.app_id = app_id
        self._mutex = threading.RLock()
        self._sig: tuple[int, int, int] | None = None
        self._doc: dict[str, Any] | None = None
        self._corrupt = False

    # ------------------------------------------------------------------ lezen
    def _stat_sig(self) -> tuple[int, int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _load_locked(self) -> dict[str, Any]:
        sig = self._stat_sig()
        if sig is not None and sig == self._sig and self._doc is not None:
            if self._corrupt:
                raise HubLinkCorrupt("koppelbestand onleesbaar")
            return self._doc
        if sig is None:
            self._sig, self._doc, self._corrupt = None, empty_doc(self.app_id), False
            return self._doc
        try:
            doc = _validate(json.loads(self.path.read_text(encoding="utf-8")), self.app_id)
        except (OSError, ValueError, HubLinkCorrupt) as exc:
            self._sig, self._doc, self._corrupt = sig, empty_doc(self.app_id), True
            raise HubLinkCorrupt("koppelbestand onleesbaar") from exc
        self._sig, self._doc, self._corrupt = sig, doc, False
        return doc

    def read(self) -> dict[str, Any]:
        """Actuele toestand (kopie). ``HubLinkCorrupt`` als het bestand onleesbaar is (fail-closed)."""
        with self._mutex:
            return copy.deepcopy(self._load_locked())

    def read_safe(self) -> dict[str, Any]:
        """Zoals ``read``, maar een onleesbaar bestand geeft een fail-closed toestand met ``corrupt: True``."""
        try:
            return self.read()
        except HubLinkCorrupt:
            doc = empty_doc(self.app_id)
            doc.update(state="ontkoppeld", corrupt=True, last_error={"code": "koppelstatus_onleesbaar", "at": None})
            return doc

    # ------------------------------------------------------------------ schrijven
    @contextlib.contextmanager
    def _flock(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _write_locked(self, doc: dict[str, Any]) -> None:
        data = (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
        tmp = self.dir / f".{FILE_NAME}.{os.getpid()}.{threading.get_ident()}.tmp"
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()  # restant van een eerdere crash van dit proces/deze thread
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
        dfd = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def update(self, fn: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any]:
        """Lezen-wijzigen-schrijven onder de vergrendeling. ``fn`` krijgt een kopie; ``None`` = niets schrijven.

        Een onleesbaar bestand wordt aan ``fn`` gegeven als ``read_safe``-toestand (``corrupt: True``); alleen een
        nieuwe geslaagde koppeling (die ``corrupt`` weghaalt) mag het dan overschrijven.
        """
        with self._mutex, self._flock():
            try:
                current = copy.deepcopy(self._load_locked())
            except HubLinkCorrupt:
                current = self.read_safe()
            new = fn(copy.deepcopy(current))
            if new is None:
                return current
            if new.get("corrupt"):
                return current  # nooit een onleesbaar bestand stil vervangen door een afgeleide toestand
            new.pop("corrupt", None)
            new["schema_version"] = SCHEMA_VERSION
            new["used_enrollment_token_sha256"] = list(new.get("used_enrollment_token_sha256") or [])[-MAX_USED_TOKENS:]
            _validate(new, self.app_id)
            self._write_locked(new)
            self._sig = None  # volgende lezing haalt het nieuwe bestand op
            return copy.deepcopy(new)


def public_view(doc: dict[str, Any]) -> dict[str, Any]:
    """Alleen de witte lijst van §3.5 (nooit ``service_token`` of ``pending.secret_sha256``)."""
    pins = doc.get("pins") if doc.get("state") == "gekoppeld" else None
    err = doc.get("last_error")
    return {
        "state": doc.get("state"),
        "koppelen_bezig": doc.get("pending") is not None,
        "hub_url": doc.get("hub_url"),
        "service_id": doc.get("service_id"),
        "via": doc.get("via"),
        "linked_at": doc.get("linked_at"),
        "first_linked_at": doc.get("first_linked_at"),
        "keyset_fingerprint": (pins or {}).get("keyset_fingerprint"),
        "service_token_last4": doc.get("service_token_last4") if doc.get("state") == "gekoppeld" else None,
        "last_error": (
            {"code": err.get("code"), "at": err.get("at"), "message_nl": ERROR_MESSAGES.get(str(err.get("code")), "")}
            if isinstance(err, dict)
            else None
        ),
    }


def error(code: str, at: str | None) -> dict[str, Any]:
    if code not in ERROR_MESSAGES:
        raise ValueError(f"onbekende foutcode {code!r}")
    return {"code": code, "at": at}
