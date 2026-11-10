"""Koppelstatus met de Hub in ``/appdata/secrets/hub-link.json`` (contract 1.3 §3.5).

- Eén bestand, 0600, in ``/appdata/secrets`` (0700). Schrijven: nieuw bestand in dezelfde map (``O_CREAT|O_EXCL``, 0600)
  → ``fsync`` → ``rename`` → ``fsync`` van de map, onder een exclusieve vergrendeling op ``hub-link.lock``. Lezen-wijzigen-
  schrijven gebeurt altijd binnen die vergrendeling (API en worker schrijven allebei).
- Elk proces leest het bestand opnieuw zodra inode, mtime, grootte, rechten of eigenaar veranderen (``stat`` bij elk
  gebruik), zodat een ontkoppeling, intrekking of rotatie door het ene proces direct voor het andere geldt.
- **Niet** in de database: een database-herstel brengt dus geen oude koppeling, oude pins of ingetrokken token terug, en
  DB-exports bevatten geen credentials. Het service-token staat onversleuteld in het bestand, net als ``vault.key``.
- **Onleesbaar** (geen geldige JSON, een andere major van ``schema_version``, ontbrekende verplichte velden, verkeerde
  eigenaar of rechten): fail-closed. De dienst vertrouwt geen Hub-JWT (401 ``not_linked``), toont geen koppelformulier en
  meldt ``koppelstatus_onleesbaar``. Een gewone schrijfactie vervangt zo'n bestand **nooit**. Alleen de eerste
  schrijfactie van een poging met een **nieuw env-token** (``update(..., replace_unreadable=True)``) vervangt het: eerst
  blijft het origineel bewaard als ``hub-link.json.onleesbaar-<UTC-tijd>`` (0600), daarna komt er een nieuw bestand met
  ``unreadable_replaced_at``. Zolang ``first_linked_at`` daarna leeg is, blijft de toestand voor de statuspagina
  "onleesbaar" (``is_unreadable``): geen formulier, geen vertrouwen (RV6).
- Lezers accepteren elke ``1.x`` en bewaren onbekende velden; deze dienst schrijft ``schema_version: "1.3"``.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.3"
FILE_NAME = "hub-link.json"
BACKUP_PREFIX = FILE_NAME + ".onleesbaar-"
_SCHEMA = re.compile(r"1\.[0-9]{1,4}")
LOCK_NAME = "hub-link.lock"
STATES = ("nooit_gekoppeld", "gekoppeld", "ontkoppeld", "ingetrokken")
MAX_USED_TOKENS = 20

# Vaste codes voor ``last_error`` met een vaste Nederlandse melding (§3.5, open lijst); nooit weerspiegelde invoer.
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
    # nieuw in contract 1.3
    "hub_incompatibel": "De Hub weigerde het verzoek als onverenigbaar (deze dienst en de Hub passen niet bij elkaar). "
    "Werk de Hub of deze dienst bij en maak daarna een nieuw koppeltoken.",
    "token_ongeldig": "Het koppeltoken (HUB_ENROLLMENT_TOKEN) heeft niet de juiste vorm. Maak een nieuw koppeltoken in "
    "de Hub.",
    "vingerafdruk_ongeldig": "HUB_KEY_FINGERPRINT heeft geen geldig formaat (XXXX-XXXX-XXXX-XXXX). Pas de "
    "app-instellingen aan.",
    "hub_adres_ongeldig": "Het Hub-adres (HUB_URL) is ongeldig. Gebruik http(s)://<NAS-adres>:<poort> zonder pad.",
    "overzetten_onvolledig": "De koppeling uit de vorige versie was onvolledig en is niet overgenomen. Koppel opnieuw "
    "via de Hub.",
    "koppelstatus_onleesbaar": "De koppelstatus in /appdata/secrets is onleesbaar. Koppel opnieuw via de Hub.",
    # eigen code van deze dienst (open lijst): een ondertekende sleutelrotatie was ongeldig (§4.5)
    "sleutelrotatie_ongeldig": "Een sleutelrotatie van de Hub was ongeldig en is niet toegepast (zie de app-log).",
}
# Codes uit contract 1.2 die in een bestaand koppelbestand kunnen staan → de vaste 1.3-naam (alleen bij het lezen).
LEGACY_CODES = {
    "ongeldig_hub_adres": "hub_adres_ongeldig",
    "ongeldige_vingerafdruk": "vingerafdruk_ongeldig",
    "migratie_onvolledig": "overzetten_onvolledig",
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
    """Verplichte velden en vorm (§3.5). Onbekende velden blijven staan (extra velden mogen)."""
    if not isinstance(doc, dict):
        raise HubLinkCorrupt("geen object")
    if not isinstance(doc.get("schema_version"), str) or not _SCHEMA.fullmatch(doc["schema_version"]):
        raise HubLinkCorrupt("schema_version ontbreekt of heeft een andere hoofdversie")
    if doc.get("state") not in STATES or doc.get("app_id") != app_id:
        raise HubLinkCorrupt("state of app_id ontbreekt of is ongeldig")
    base = empty_doc(app_id)
    base.update(doc)
    if base["state"] == "gekoppeld":
        pins = base.get("pins")
        if (
            not isinstance(base.get("service_token"), str)
            or not base["service_token"]
            or not isinstance(base.get("service_id"), str)
            or not isinstance(base.get("hub_url"), str)
            or not isinstance(pins, dict)
            or not isinstance(pins.get("jwt"), list)
            or not isinstance(pins.get("policy"), list)
        ):
            raise HubLinkCorrupt("gekoppeld zonder token, service_id, hub_url of pins")
    if not isinstance(base.get("used_enrollment_token_sha256"), list):
        raise HubLinkCorrupt("used_enrollment_token_sha256 is geen lijst")
    if base.get("pending") is not None and not isinstance(base["pending"], dict):
        raise HubLinkCorrupt("pending is geen object")
    if base.get("last_error") is not None and not isinstance(base["last_error"], dict):
        raise HubLinkCorrupt("last_error is geen object")
    return base


def is_unreadable(doc: dict[str, Any]) -> bool:
    """Toestand "onleesbaar" voor de statuspagina (§3.5/§3.7): het bestand is onleesbaar, of het is na een onleesbaar
    bestand vervangen en de dienst is daarna nog niet (opnieuw) gekoppeld."""
    return bool(doc.get("corrupt")) or (bool(doc.get("unreadable_replaced_at")) and not doc.get("first_linked_at"))


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


class HubLinkStore:
    """Leest en schrijft ``hub-link.json``. Thread-safe en veilig tussen processen (``flock``)."""

    def __init__(self, secrets_dir: Path, app_id: str) -> None:
        self.dir = Path(secrets_dir)
        self.path = self.dir / FILE_NAME
        self.lock_path = self.dir / LOCK_NAME
        self.app_id = app_id
        self._mutex = threading.RLock()
        self._sig: tuple[int, ...] | None = None
        self._doc: dict[str, Any] | None = None
        self._corrupt = False

    # ------------------------------------------------------------------ lezen
    def _stat_sig(self) -> tuple[int, ...] | None:
        """Inode, mtime, grootte, rechten en eigenaar: ook een chmod/chown wordt direct opgemerkt (§3.5)."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode, st.st_uid)

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
            self._check_owner_and_mode()
            doc = _validate(json.loads(self.path.read_text(encoding="utf-8")), self.app_id)
        except (OSError, ValueError, HubLinkCorrupt) as exc:
            self._sig, self._doc, self._corrupt = sig, empty_doc(self.app_id), True
            raise HubLinkCorrupt("koppelbestand onleesbaar") from exc
        self._sig, self._doc, self._corrupt = sig, doc, False
        return doc

    def _check_owner_and_mode(self) -> None:
        """Verkeerde eigenaar of rechten = onleesbaar (§3.5): het bestand hoort bij deze gebruiker en is 0600."""
        st = self.path.stat()
        if st.st_uid != os.geteuid():
            raise HubLinkCorrupt("koppelbestand heeft een andere eigenaar")
        if st.st_mode & 0o077:
            raise HubLinkCorrupt("koppelbestand is leesbaar of schrijfbaar voor anderen")

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

    def _keep_unreadable_original(self) -> str:
        """Bewaart het onleesbare origineel als ``hub-link.json.onleesbaar-<UTC-tijd>`` (0600) vóór het vervangen.
        Een harde koppeling bewaart de bytes zonder ze te hoeven lezen (ook bij een verkeerde eigenaar)."""
        backup = self.dir / f"{BACKUP_PREFIX}{_utc_stamp()}"
        try:
            os.link(self.path, backup)
        except OSError:
            data = self.path.read_bytes()  # geen harde koppeling mogelijk: kopiëren (faalt bij geen leesrecht)
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        with contextlib.suppress(OSError):
            os.chmod(backup, 0o600)
        return backup.name

    def update(
        self, fn: Callable[[dict[str, Any]], dict[str, Any] | None], *, replace_unreadable: bool = False
    ) -> dict[str, Any]:
        """Lezen-wijzigen-schrijven onder de vergrendeling. ``fn`` krijgt een kopie; ``None`` = niets schrijven.

        Een onleesbaar bestand wordt aan ``fn`` gegeven als ``read_safe``-toestand (``corrupt: True``) en wordt dan
        nooit geschreven. Alleen met ``replace_unreadable=True`` (de eerste schrijfactie van een poging met een nieuw
        env-token, §3.5/RV6) blijft het origineel eerst bewaard en krijgt ``fn`` een nieuw document met
        ``unreadable_replaced_at``.
        """
        with self._mutex, self._flock():
            try:
                current = copy.deepcopy(self._load_locked())
            except HubLinkCorrupt:
                current = self.read_safe()
            base = copy.deepcopy(current)
            if current.get("corrupt") and replace_unreadable:
                base = empty_doc(self.app_id)
                base["unreadable_replaced_at"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            new = fn(base)
            if new is None:
                return current
            if new.get("corrupt"):
                return current  # nooit een onleesbaar bestand stil vervangen door een afgeleide toestand
            new.pop("corrupt", None)
            new["schema_version"] = SCHEMA_VERSION
            new["used_enrollment_token_sha256"] = list(new.get("used_enrollment_token_sha256") or [])[-MAX_USED_TOKENS:]
            _validate(new, self.app_id)
            if current.get("corrupt") and self.path.exists():
                kept = self._keep_unreadable_original()
                new["unreadable_original"] = kept  # extra veld (niet in de witte lijst): waar het origineel staat
            self._write_locked(new)
            self._sig = None  # volgende lezing haalt het nieuwe bestand op
            return copy.deepcopy(new)


def public_view(doc: dict[str, Any]) -> dict[str, Any]:
    """Alleen de witte lijst van §3.5 (nooit ``service_token`` of ``pending.secret_sha256``; ook nooit de extra velden
    zoals ``unreadable_replaced_at`` of ``token_rotation_requested_at``)."""
    pins = doc.get("pins") if doc.get("state") == "gekoppeld" else None
    err = doc.get("last_error")
    if isinstance(err, dict):
        err = {**err, "code": LEGACY_CODES.get(str(err.get("code")), err.get("code"))}
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
