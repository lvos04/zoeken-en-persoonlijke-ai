"""Kleine Nederlandse statuspagina op ``GET /`` (contract 1.3 §1/§13) in de ontwerptaal **Peil 1.1.1** (ONTWERPTAAL
§13.2) + koppelen met koppelcode (contract 1.3 §3.7).

Vormgeving: het framework-vrije Peil-sjabloon ``design/ontwerptaal/statuspagina.html`` (versie-gepinde kopie, O-14):
CSS en iconen inline, **geen JavaScript**, geen externe bron; past binnen ``default-src 'none'``.

Veiligheid (§3.7):
- Geen geheimen, geen bronnamen, geen paden en geen zoekinhoud op de pagina; alleen de witte lijst van de koppelstatus
  (§3.5), versie, gezondheid en totaalaantallen.
- Het koppelformulier (Hub-adres, koppelcode, **Hub-vingerafdruk**, alle drie verplicht) bestaat **alleen zolang de dienst
  nog nooit gekoppeld is** en het koppelbestand leesbaar is. Daarna: alleen status en "ontkoppelen of opnieuw koppelen
  gaat via de Hub". Er is hier geen ontkoppelen, geen lokale ontkoppelcode en geen herkoppelen na intrekking.
- **Herkomst** van elke formulier-POST, naast de double-submit-cookie (``SameSite=Strict``, HttpOnly): de pagina's sturen
  ``Referrer-Policy: same-origin``; ``Origin`` moet exact ``<schema>://<Host>`` zijn (ontbreekt of ``null`` → 403);
  ``Sec-Fetch-Site`` alleen ``same-origin`` of ``none``; de ``Host`` moet een IP-adres in de vorm van §3.2, een naam
  zonder punt of een naam onder ``.local``/``.lan``/``.internal``/``.home.arpa`` zijn (DNS-rebinding, anders 403
  ``host_niet_toegestaan``). Een geweigerd verzoek gaat niet naar de Hub en verandert niets.
- **Elke** POST die de herkomstcontrole doorstaat telt als poging: max. 5 per 15 min per bron-IP (adres van de
  TCP-verbinding; nooit ``X-Forwarded-For``) en 20 per 15 min voor de hele dienst (429), ook bij een verlopen formulier,
  ongeldige invoer, een lopende poging (409 ``koppelen_bezig``), een onleesbaar koppelbestand (409) of een dienst die al
  eens gekoppeld is (409 ``al_gekoppeld``).
- **Meldingen** horen bij dít verzoek: de POST krijgt direct de pagina terug met de melding en de echte HTTP-status
  (geen redirect, geen ``?melding=`` in de URL). ``GET /`` toont nooit een melding over een poging; alleen een
  toestand die bij het laden al bestaat (onleesbaar koppelbestand, adres zonder koppelmogelijkheid), zonder live-rol.
- Invoer als geheel controleren, nooit eerst afkappen: koppelcode ≤ 32, vingerafdruk ≤ 64, Hub-adres ≤ 512 tekens.
- **Log:** elke afgehandelde POST geeft precies één regel met code, HTTP-status en Trace-ID, zonder het ingevulde
  Hub-adres en zonder geheimen.
- **Gereed** = ``/readyz`` in orde én gekoppeld en bevestigd; het totaal is nooit groen zonder Gereed (§3.7, §13).
"""

from __future__ import annotations

import hmac
import html
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select

from . import APP_ID, __version__, audit, events
from .errors import TRACE_HEADER, trace_id_for
from .health import connector_health, readiness
from .hubclient import HubUrlError, canonical_host, validate_hub_url
from .hublink import ERROR_MESSAGES, is_unreadable, public_view
from .peil import PeilTemplate, PeilTemplateError, default_dir, load_template, render, strip_comments
from .search.models import IndexSource
from .search.paths import validate_read_roots
from .security.pins import normalize_fingerprint, normalize_pairing_code
from .timeutil import utcnow

log = logging.getLogger("search_ai.status_page")
router = APIRouter(include_in_schema=False)

CSRF_COOKIE = "sai_csrf"
SERVICE_NAME = "Zoeken en persoonlijke AI"
# Ontwerpreview (docs/ontwerp/PEIL_REVIEW.md): kort en feitelijk zoals het Peil-prototype; "alle schermen staan in de
# Hub" staat al in het koppelblok en de voet van het sjabloon.
SERVICE_DESCRIPTION = (
    "Persoonlijke zoekmachine voor je eigen bestanden op de NAS; spraak en persoonlijke AI volgen in fase P5. "
    f"Dienst-ID in de Hub: {APP_ID}."
)
PAIRING_CODE_VALID = "60 minuten"  # contract §3.4: koppelcode verloopt 60 min na aanmaak in de Hub
LINK_FILE = "/appdata/secrets/hub-link.json"
WINDOW_S = 900.0
MONTHS = ("jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec")
ALLOWED_HOST_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa")  # §3.7 punt 4


@dataclass(frozen=True)
class Notice:
    toon: str  # "fout" | "info" (klassen melding-fout / melding-info van het sjabloon)
    titel: str
    tekst: str
    http: int = 200  # HTTP-status van het antwoord op de POST met deze melding
    veld: str | None = None  # "hub" | "koppelcode" | "vingerafdruk": het formulierveld met de fout
    veldfout: str | None = None
    pad: str | None = None  # Peil 1.1.1 melding_pad: pad in mono op een eigen regel (tekst eindigt op ":")


_FAILED = "Koppelen is mislukt"
_NOTHING_SENT = "Er is niets naar de Hub gestuurd."
NOTICES: dict[str, Notice] = {
    # na een geslaagde poging van dít verzoek (rol status)
    "gekoppeld": Notice(
        "info",
        "Gekoppeld met de Hub",
        "De Hub heeft de koppeling bevestigd. Vergelijk de vingerafdruk hieronder met die in de Hub.",
    ),
    "gekoppeld_onbevestigd": Notice(
        "info",
        "Gekoppeld, nog niet bevestigd",
        "De koppeling is opgeslagen, maar de Hub heeft haar nog niet bevestigd. De dienst probeert dat vanzelf opnieuw. "
        "Vergelijk de vingerafdruk hieronder met die in de Hub.",
    ),
    # weigeringen vóór de Hub (rol alert)
    "herkomst_geweigerd": Notice(
        "fout",
        "Formulier geweigerd",
        "Dit formulier kwam niet van deze pagina (andere herkomst). "
        + _NOTHING_SENT
        + " Open de statuspagina van deze "
        "dienst zelf en vul het formulier daar in.",
        403,
    ),
    "host_niet_toegestaan": Notice(
        "fout",
        "Koppelen kan niet via dit adres",
        "Open deze pagina via het IP-adres van de NAS, of koppel via de Hub. " + _NOTHING_SENT,
        403,
    ),
    "ongeldig_formulier": Notice(
        "fout",
        "Het formulier is verlopen",
        "Het formulier is ouder dan een uur, of je browser stuurde de beveiligingscookie van deze pagina niet mee. "
        + _NOTHING_SENT
        + " Laad de pagina opnieuw en vul het formulier nog eens in.",
        403,
    ),
    "te_veel_pogingen": Notice(
        "fout",
        "Te veel pogingen",
        "In het afgelopen kwartier is te vaak geprobeerd te koppelen. " + _NOTHING_SENT + " Wacht een kwartier en "
        "probeer het dan opnieuw.",
        429,
    ),
    "al_gekoppeld": Notice(
        "fout",
        "Koppelen kan hier niet",
        "Deze dienst is al eens gekoppeld. Ontkoppelen of opnieuw koppelen gaat via de Hub, niet via deze pagina.",
        409,
    ),
    "koppelen_bezig": Notice(
        "fout",
        "Er loopt al een koppelpoging",
        "Deze poging is niet naar de Hub gestuurd. Wacht tot de lopende poging klaar is en laad de pagina daarna "
        "opnieuw.",
        409,
    ),
    # Na een koppelpoging op een onleesbaar koppelbestand is onbekend of de dienst ooit gekoppeld was, dus nooit "al eens
    # gekoppeld" (ADR-0015 Q-04). Zonder poging staat dezelfde tekst als blijvende toestand op de pagina (zonder rol).
    "koppelstatus_onleesbaar": Notice(
        "fout",
        "Koppelstatus onleesbaar",
        "Het koppelbestand van deze dienst is onleesbaar. Uit veiligheid weigert de dienst daarom elke Hub-aanvraag; er "
        "gaat niets naar de Hub. Het bestand staat in:",
        409,
        pad=LINK_FILE,
    ),
    "hub_adres_ongeldig": Notice(
        "fout",
        _FAILED,
        "Het adres van de Hub is ongeldig. " + _NOTHING_SENT,
        422,
        "hub",
        "Gebruik http(s)://<NAS-adres>:<poort>, zonder pad en geen localhost.",
    ),
    "koppelcode_ongeldig": Notice(
        "fout",
        _FAILED,
        "De koppelcode heeft niet de goede vorm. " + _NOTHING_SENT,
        422,
        "koppelcode",
        "12 tekens, zoals de Hub ze toont (XXXX-XXXX-XXXX).",
    ),
    "vingerafdruk_ongeldig": Notice(
        "fout",
        _FAILED,
        "De vingerafdruk van de Hub heeft niet de goede vorm. " + _NOTHING_SENT,
        422,
        "vingerafdruk",
        "16 tekens, zoals de Hub ze toont (XXXX-XXXX-XXXX-XXXX).",
    ),
    # uitkomst van de Hub
    "hub_onbereikbaar": Notice(
        "fout",
        _FAILED,
        "De Hub is niet bereikbaar op dit adres. Controleer het adres en of de Hub draait, en probeer het opnieuw.",
        503,
        "hub",
        "Geen antwoord van een Hub op dit adres.",
    ),
    # Statuspagina: de gebruiker vulde een koppelcode in (geen koppeltoken), dus de tekst noemt alleen de koppelcode
    # (zoals het Peil-prototype). ERROR_MESSAGES blijft de algemene tekst voor last_error in de API.
    "hub_weigerde": Notice(
        "fout",
        _FAILED,
        "De Hub weigerde de koppelcode: verlopen, al gebruikt of ongeldig. Maak in de Hub een nieuwe koppelcode en "
        "probeer het opnieuw.",
        422,
        "koppelcode",
        "Deze koppelcode is verlopen, al gebruikt of ongeldig. Maak een nieuwe in de Hub.",
    ),
    "hub_antwoord_ongeldig": Notice(
        "fout",
        _FAILED,
        ERROR_MESSAGES["hub_antwoord_ongeldig"] + " Controleer of dit adres de Hub is en probeer het opnieuw.",
        503,
    ),
    "hub_vingerafdruk_onjuist": Notice(
        "fout",
        _FAILED,
        ERROR_MESSAGES["hub_vingerafdruk_onjuist"],
        422,
        "vingerafdruk",
        "Deze vingerafdruk hoort niet bij de Hub op dit adres. Vergelijk hem met de Hub.",
    ),
    "enroll_endpoint_ontbreekt": Notice(
        "fout",
        _FAILED,
        "Op dit adres draait geen Hub die koppelen met een koppelcode kent. Controleer het adres; is het de Hub, werk "
        "de Hub dan bij en probeer het opnieuw.",
        503,
        "hub",
        "Op dit adres draait geen Hub die koppelen kent.",
    ),
    "hub_incompatibel": Notice(
        "fout",
        _FAILED,
        "De Hub weigerde het verzoek als onverenigbaar: deze dienst en de Hub passen niet bij elkaar. Werk de Hub of "
        "deze dienst bij en probeer het daarna opnieuw.",
        503,
    ),
    "rate_limited": Notice(
        "fout",
        _FAILED,
        "De Hub vraagt even te wachten (te veel pogingen). Probeer het over een paar minuten opnieuw.",
        429,
    ),
}
# Blijvende toestand bij het laden (geen poging): eigen tekst, geen live-rol (Peil 1.1.1 E05).
HOST_NOTICE = Notice(
    "info",
    "Koppelen kan niet via dit adres",
    "Je opent deze pagina via een domeinnaam. Koppelen op deze pagina kan alleen via het IP-adres van de NAS, een naam "
    "zonder punt of een naam onder .local, .lan, .internal of .home.arpa (bescherming tegen DNS-rebinding). Open deze "
    "pagina via het IP-adres van de NAS, of koppel via de Hub.",
)
FIELD_LINK = {
    "hub": ("hub", "Naar het veld Adres van de Hub", "Naar het Hub-adres (fout)"),
    "koppelcode": ("koppelcode", "Naar het veld Koppelcode", "Naar de koppelcode (fout)"),
    "vingerafdruk": ("vingerafdruk", "Naar het veld Vingerafdruk van de Hub", "Naar de vingerafdruk (fout)"),
}
CHECK_NAMES = {
    "database": "Eigen database",
    "migraties": "Databaseschema",
    "pgvector": "pgvector (vectorzoeken)",
    "geheimen": "Sleutels in /appdata/secrets",
}
CHECK_FAILED = {
    "database": "De ingebouwde database antwoordt niet; zie de app-log.",
    "migraties": "Het databaseschema staat niet op de verwachte versie; zie de app-log.",
    "pgvector": "De extensie pgvector ontbreekt in de database.",
    "geheimen": "Een of meer sleutelbestanden ontbreken; zie de app-log.",
}
STATUS_NL = {"draft": "concept", "active": "actief", "paused": "gepauzeerd", "revoked": "ingetrokken"}
# Peil 1.1.1 (E03/E10): tonen die meetellen in het totaal en hun gewicht; 'verouderd' als eigen controle = Onbekend.
SEVERITY = {"gezond": 0, "onbekend": 1, "beperkt": 2, "storing": 3}
AS_TOTAL = {
    "gezond": "gezond",
    "onbekend": "onbekend",
    "niet-gekoppeld": "onbekend",
    "niet-beschikbaar": "onbekend",
    "verouderd": "onbekend",
    "beperkt": "beperkt",
    "storing": "storing",
}
# Dienstblok (E10): een verse Storing of Beperkt maakt het totaal hooguit Beperkt; niet ingesteld (gate), nog niet
# beschikbaar, beleid, feit en verouderd tellen niet mee; niet-beschikbaar (capability_unavailable) telt als Onbekend.
SERVICE_BLOCK_TOTAL = {"gezond": "gezond", "beperkt": "beperkt", "storing": "beperkt", "niet-beschikbaar": "onbekend"}
TOTAL_LABEL = {"gezond": "Alles gezond", "onbekend": "Deels onbekend", "beperkt": "Beperkt", "storing": "Storing"}
# Begin van de toelichting bij "Niet gereed": dezelfde toon als de pil (nooit "Deels onbekend" naast een amber pil).
NOT_READY_PREFIX = {"onbekend": "Deels onbekend", "beperkt": "Beperkt", "storing": "Storing"}
LABEL = {"gezond": "Gezond", "onbekend": "Onbekend", "beperkt": "Beperkt", "storing": "Storing"}
SOURCE_TEXT = "/readyz, /healthz en de connector-gezondheid (wat de Hub ziet) van deze dienst"
# Peil 1.1.1 {{gereed_uitleg}}: preciezer dan de standaardzin van het sjabloon, want "Gereed" vraagt in contract 1.3
# §3.7 ook de bevestiging door de Hub (confirmed_at), en zonder het woord "in orde" (ONTWERPTAAL §11.1: Gezond).
GEREED_UITLEG = (
    "Gereed betekent: de eigen controles van /readyz zijn gezond én de Hub heeft de koppeling bevestigd. /readyz kijkt "
    "zelf niet naar de koppeling."
)
WORKER = "Achtergrondtaken (worker)"


class PairThrottle:
    """Max. ``per_ip`` pogingen per bron-IP en ``total`` voor de hele dienst per ``window_s`` (in het geheugen; deze
    dienst heeft één API-proces, §3.7)."""

    def __init__(self, per_ip: int = 5, total: int = 20, window_s: float = WINDOW_S, max_ips: int = 10_000) -> None:
        self.per_ip, self.total, self.window_s, self.max_ips = per_ip, total, window_s, max_ips
        self._ips: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, q: deque[float], now: float) -> None:
        while q and now - q[0] >= self.window_s:
            q.popleft()

    def allow(self, ip: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._ips) > self.max_ips:
                for k in [k for k, q in self._ips.items() if not q or now - q[-1] >= self.window_s]:
                    del self._ips[k]
            q = self._ips.setdefault(ip, deque())
            self._trim(q, now)
            self._trim(self._all, now)
            if len(q) >= self.per_ip or len(self._all) >= self.total:
                return False
            q.append(now)
            self._all.append(now)
            return True


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    # contract 1.3 §3.7 punt 1: same-origin, nooit no-referrer (dan stuurt elke browser Origin: null)
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


# ---------------------------------------------------------------------------------------------------- herkomst
def host_allowed(host_header: str) -> bool:
    """§3.7 punt 4 (DNS-rebinding): de ``Host`` (zonder poort) is een IP-adres in de vorm van §3.2 (IPv6 tussen ``[]``),
    een naam zonder punt, of een naam onder ``.local``, ``.lan``, ``.internal`` of ``.home.arpa``."""
    raw = (host_header or "").strip()
    if not raw or len(raw) > 300 or any(ch.isspace() or ord(ch) < 32 for ch in raw):
        return False
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            return False
        host, rest, bracketed = raw[1:end], raw[end + 1 :], True
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return False
    else:
        host, _, port = raw.partition(":")
        if port and not port.isdigit():
            return False
        bracketed = False
    try:
        name, ip = canonical_host(host, bracketed)
    except HubUrlError:
        return False
    if ip is not None:
        return True
    return "." not in name or name.endswith(ALLOWED_HOST_SUFFIXES)


def origin_refusal(request: Request) -> str | None:
    """Herkomstcontrole van §3.7 (punten 2–4); ``None`` = doorgelaten, anders de vaste code."""
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if not origin or origin == "null" or origin != f"{request.url.scheme}://{host}":
        return "herkomst_geweigerd"
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return "herkomst_geweigerd"
    if not host_allowed(host):
        return "host_niet_toegestaan"
    return None


# ---------------------------------------------------------------------------------------------------- weergave
@dataclass(frozen=True)
class PageInput:
    """Alles wat de pagina toont. ``doc`` is het koppelbestand (``read_safe``); de pagina gebruikt daarvan alleen de
    witte lijst van §3.5 (``public_view``) plus ``corrupt``, ``unreadable_replaced_at``, ``confirmed_at`` en
    ``pending.started_at`` (nooit het geheim of zijn hash)."""

    ready: bool
    checks: list[dict[str, Any]]
    doc: dict[str, Any]
    per_status: dict[str, int] | None
    roots: int
    root_warnings: int
    csrf: str
    now: datetime
    # melding van dít verzoek (alleen bij een POST): code uit NOTICES + Trace-ID
    melding: str | None = None
    trace_id: str | None = None
    # controles van de connector-gezondheid (wat de Hub ziet): elke afwijking wordt een regel, zodat de pagina nooit
    # beter is dan wat de Hub ziet (ONTWERPTAAL §1 punt 8)
    health: list[dict[str, Any]] | None = None
    # het adres waarmee de pagina geopend is staat koppelen niet toe (§3.7 punt 4): blijvende melding zonder rol
    host_blocked: bool = False


def may_pair(doc: dict[str, Any]) -> bool:
    """Koppelen via de statuspagina alleen zolang de dienst nog **nooit** gekoppeld is en het koppelbestand leesbaar is
    (§3.5/§3.7; ook na het vervangen van een onleesbaar bestand blijft het "onleesbaar" tot een nieuwe koppeling)."""
    return doc.get("state") == "nooit_gekoppeld" and not doc.get("first_linked_at") and not is_unreadable(doc)


def fmt_time(value: datetime | str | None) -> tuple[str, str]:
    """``("9 nov 2026, 11:56", ISO)`` in de tijdzone van de container (TrueNAS zet ``TZ``); UTC staat er dan bij."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return "onbekend", ""
    if value is None:
        return "onbekend", ""
    local = value.astimezone()
    text = f"{local.day} {MONTHS[local.month - 1]} {local.year}, {local:%H:%M}"
    if local.utcoffset() == timedelta(0):
        text += " UTC"
    return text, local.isoformat(timespec="seconds")


def _short(text: str) -> str:
    return text.split(", ", 1)[1] if ", " in text else text


def _row(naam: str, toon: str, label: str, reden: str = "", code: str | None = None) -> dict[str, str]:
    return {"naam": naam, "toon": toon, "label": label, "reden": reden, "reden_code": code or ""}


def _readiness_rows(checks: list[dict[str, Any]]) -> list[dict[str, str]]:
    by_name = {str(c.get("name")): str(c.get("status")) for c in checks}
    rows = []
    for name, label in CHECK_NAMES.items():
        status = by_name.get(name)
        if status == "ok":
            rows.append(_row(label, "gezond", "Gezond"))
        elif status is None:  # bv. migraties/pgvector zonder database: niet gemeten, dus nooit groen
            rows.append(_row(label, "onbekend", "Onbekend", "Niet te controleren zonder database."))
        else:
            rows.append(_row(label, "storing", "Storing", CHECK_FAILED[name]))
    return rows


def _hub_row(link: dict[str, Any], unreadable: bool, shown_code: str | None = None) -> dict[str, str] | None:
    """Regel "Koppeling met de Hub" in de controles; ``None`` als de dienst gekoppeld is zonder melding (dan staat de
    neutrale pil "Gekoppeld" in het koppelblok: verbonden is geen gezondheid, dus nooit groen).

    ``shown_code``: de foutcode van de melding bovenaan; dezelfde laatste melding wordt dan niet nog eens herhaald."""
    state = link.get("state")
    err = link.get("last_error") or {}
    code = err.get("code")
    herhaal = bool(code) and code != shown_code
    laatste = f" Laatste melding: {err.get('message_nl')}" if herhaal else ""
    naam = "Koppeling met de Hub"
    if unreadable:
        reden = "Onleesbaar koppelbestand; de dienst weigert elke Hub-aanvraag."
        return _row(naam, "storing", "Onleesbaar", reden, "koppelstatus_onleesbaar")
    if state == "gekoppeld":
        return _row(naam, "beperkt", "Beperkt", laatste.strip(), code) if herhaal else None
    if state == "ontkoppeld":
        reden = "Ontkoppeld door de Hub; de dienst weigert elke Hub-aanvraag." + laatste
        return _row(naam, "niet-gekoppeld", "Ontkoppeld", reden, code if herhaal else None)
    if state == "ingetrokken":
        reden = "Ingetrokken door de Hub; de dienst weigert elke opdracht van de Hub."
        return _row(naam, "niet-gekoppeld", "Ingetrokken", reden, "ingetrokken_door_hub")
    reden = "Tot de koppeling voert de dienst niets uit voor de Hub."
    if link.get("koppelen_bezig"):
        reden += " Er loopt een koppelpoging."
    return _row(naam, "niet-gekoppeld", "Niet gekoppeld", reden + laatste, code if herhaal else None)


HEALTH_ROWS = {
    # naam in de connector-gezondheid → (regelnaam, reden); afwijkingen worden een regel in de controles
    "hub_sleutels": ("Gepinde Hub-sleutels", "De gepinde sleutels van de Hub zijn ongeldig of een rotatie is mislukt."),
    "jobs": ("Gebeurtenissen naar de Hub", "Er staan gebeurtenissen vast of lopen achter; zie de Hub."),
    "intrekking_opgeschoond": (
        "Opschonen na intrekking",
        "Een ingetrokken bron is nog niet volledig opgeschoond; zie de app-log.",
    ),
    "worker": (WORKER, "De worker meldt dat een stap mislukte; zie de app-log."),
}
HEALTH_TOON = {"degraded": "beperkt", "down": "storing"}


def _health_rows(health: list[dict[str, Any]] | None, linked: bool) -> list[dict[str, str]]:
    rows = []
    for c in health or []:
        name, status, code = str(c.get("name")), str(c.get("status")), c.get("reason_code")
        if name == "worker" and status == "unknown":
            # §6.2: een verouderde meting is "laatst bekend" (Peil-toon verouderd, telt als Onbekend), nooit actueel
            if code == "meting_verouderd":
                tijd, _ = fmt_time(c.get("observed_at"))
                reden = f"Laatste hartslag {_short(tijd)}; daarna niets meer gemeten. Zie de app-log."
                row = _row(WORKER, "verouderd", "Verouderd", reden, "meting_verouderd")
                row["tijd_bron"] = str(c.get("observed_at") or "")
                rows.append(row)
            else:
                reden = "Nog geen hartslag van de worker gemeten; zie de app-log."
                rows.append(_row(WORKER, "onbekend", "Onbekend", reden, code or "geen_hartslag"))
            continue
        toon = HEALTH_TOON.get(status)
        if toon is None:
            continue
        if name == "hub_link" and linked and code == "niet_bevestigd":
            reden = "De Hub heeft de koppeling nog niet bevestigd; de dienst probeert het opnieuw."
            rows.append(_row("Koppeling met de Hub", toon, LABEL[toon], reden, code))
        elif name in HEALTH_ROWS and not (name == "hub_sleutels" and code == "sleutelrotatie_ongeldig"):
            naam, reden = HEALTH_ROWS[name]  # een mislukte rotatie staat al als laatste melding bij de koppeling
            rows.append(_row(naam, toon, LABEL[toon], reden, code or name))
    return rows


def _sources_summary(per_status: dict[str, int] | None) -> str:
    if per_status is None:
        return "Aantal bronnen onbekend (database niet bereikbaar)"
    if not sum(per_status.values()):
        return "Nog geen bronnen"
    return ", ".join(f"{n} {STATUS_NL.get(st, st)}" for st, n in sorted(per_status.items()))


SHARE_HINT = "Een share toevoegen: in TrueNAS als Host Path met Read Only onder /bronnen."


def _shares(n: int) -> str:
    return f"{n} geldige share" if n == 1 else f"{n} geldige shares"


def _service_rows(inp: PageInput) -> list[dict[str, str]]:
    # Namen in gewone taal, zonder interne taak-ID's zoals B07/B15 (ontwerpreview, docs/ontwerp/PEIL_REVIEW.md).
    # "Gekoppelde shares" (de leeswortels van ADR-0004): gezond/beperkt is een verse meting (telt mee); geen share is
    # een instelling die nog open staat (gate "Niet ingesteld", telt niet mee, nooit groen; ONTWERPTAAL §4.1 regel 12).
    if inp.roots and not inp.root_warnings:
        roots = _row("Gekoppelde shares", "gezond", "Gezond", f"{_shares(inp.roots)}. {SHARE_HINT}")
    elif inp.root_warnings:
        reden = f"{_shares(inp.roots)}, {inp.root_warnings} genegeerd (zie de app-log). {SHARE_HINT}"
        roots = _row("Gekoppelde shares", "beperkt", "Beperkt", reden, "ongeldige_leeswortel")
    else:
        roots = _row("Gekoppelde shares", "gate", "Niet ingesteld", f"Nog geen share gekoppeld. {SHARE_HINT}")
    return [
        _row(
            "Bronnen",
            "gate",
            "Alleen met toestemming",
            f"{_sources_summary(inp.per_status)}. Zonder toestemming van de eigenaar wordt niets gelezen; indexdata "
            "verlaat de NAS niet.",
        ),
        roots,
        _row(
            "Indexeren en semantisch zoeken",
            "nog-niet",
            "Nog niet beschikbaar",
            "Volgt in fase P5. Er wordt nu niets geïndexeerd.",
            "capability_unavailable",
        ),
        _row(
            "Spraak en persoonlijke AI",
            "nog-niet",
            "Nog niet beschikbaar",
            "Volgt in fase P5. Spraak en een lokaal taalmodel zijn optioneel en staan standaard uit; er wordt niets "
            "opgenomen. Typen in de Hub blijft altijd mogelijk.",
            "capability_unavailable",
        ),
    ]


def _safe_hub_url(url: Any) -> str:
    """Alleen http(s) als link (de opgeslagen koppeling is bij het koppelen al gecontroleerd; dit is een vangnet)."""
    if isinstance(url, str) and urlsplit(url).scheme in ("http", "https") and urlsplit(url).netloc:
        return url
    return ""


def _worst(tonen: list[str]) -> str:
    return max(tonen, key=SEVERITY.__getitem__) if tonen else "gezond"


def _toelichting(
    *,
    failing: list[str],
    unreadable: bool,
    ready: bool,
    state: str,
    linked: bool,
    confirmed: bool,
    bezig: bool,
    worst: str,
    rows_beperkt: bool,
) -> str:
    """Toelichting naast de totaalpil; bij "Niet gereed" met hetzelfde woord als de toon van de pil (Peil §11.1)."""
    prefix = NOT_READY_PREFIX.get(worst, "Deels onbekend")
    if failing:
        uitleg = f"Storing in: {', '.join(failing)}."
        return uitleg + (" Ook nog niet gekoppeld met de Hub." if not linked else "")
    if unreadable:
        return "Storing — de koppelstatus is onleesbaar."
    if not ready:
        return f"{prefix} — de eigen controles zijn niet compleet."
    if state == "nooit_gekoppeld":
        return f"{prefix} — " + ("er loopt een koppelpoging." if bezig else "wacht op koppeling met de Hub.")
    if state == "ontkoppeld":
        return f"{prefix} — ontkoppeld door de Hub."
    if state == "ingetrokken":
        return f"{prefix} — de Hub heeft de koppeling ingetrokken."
    if not linked:  # onbekende toestand in het koppelbestand (vangnet): nooit "Gereed en gekoppeld"
        return f"{prefix} — koppelstatus onbekend."
    if not confirmed:
        return f"{prefix} — gekoppeld, maar de Hub heeft de koppeling nog niet bevestigd."
    if worst == "gezond":
        return "Gereed en gekoppeld met de Hub."
    if worst == "beperkt" and not rows_beperkt:
        return "Gereed, maar beperkt: zie het blok hieronder."  # E10: uit het dienstblok, hooguit Beperkt
    return "Gereed; een of meer controles vragen aandacht (zie hieronder)."


def _gereed(*, ok: bool, ready: bool, unreadable: bool, state: str, linked: bool) -> str:
    eigen = "de eigen controles van /readyz zijn gezond"
    if ok:
        return "Ja"
    if not ready:
        return "Nee — /readyz meldt een storing of een controle die niet kon worden uitgevoerd"
    if unreadable:
        return f"Nee — de koppelstatus is onleesbaar ({eigen})"
    if state == "ontkoppeld":
        return f"Nee — ontkoppeld door de Hub ({eigen})"
    if state == "ingetrokken":
        return f"Nee — de Hub heeft de koppeling ingetrokken ({eigen})"
    if linked:
        return f"Nee — de Hub heeft de koppeling nog niet bevestigd ({eigen})"
    return f"Nee — wacht op koppeling met de Hub ({eigen})"


def _notice_data(inp: PageInput, unreadable: bool, form: bool) -> tuple[dict[str, Any], str, Notice | None]:
    """Melding van dít verzoek (rol ``alert`` bij een fout, ``status`` bij info) of een blijvende toestand (geen rol,
    E05). Geeft (plekken, rol, melding)."""
    notice = NOTICES.get(inp.melding or "")
    rol = ""
    if notice is not None:
        rol = "alert" if notice.toon == "fout" else "status"
    elif unreadable:
        notice = NOTICES["koppelstatus_onleesbaar"]
    elif inp.host_blocked and form:
        notice = HOST_NOTICE
    if notice is None:
        return {}, rol, None
    data: dict[str, Any] = {
        "melding": True,
        "melding_toon": notice.toon,
        "melding_icoon": "storing" if notice.toon == "fout" else "info",
        "melding_titel": notice.titel,
        "melding_tekst": notice.tekst,
        "melding_rol": rol,
    }
    if notice.pad:
        data["melding_pad"] = notice.pad
    if rol == "alert" and inp.trace_id:
        data.update(trace_id=inp.trace_id, melding_code=inp.melding)
    elif notice is NOTICES["koppelstatus_onleesbaar"]:
        data["melding_code"] = "koppelstatus_onleesbaar"  # blijvende toestand: code zonder trace-ID (E06)
    if notice.veld and form and rol == "alert":
        doel, link_tekst, spring = FIELD_LINK[notice.veld]
        veld = "hub_adres" if notice.veld == "hub" else notice.veld
        data.update(
            melding_link=True,
            melding_link_doel=doel,
            melding_link_tekst=link_tekst,
            spring_doel=doel,
            spring_tekst=spring,
            **{f"fout_{veld}": notice.veldfout},
        )
    return data, rol, notice


def page_data(inp: PageInput) -> dict[str, Any]:
    """Plekken van het Peil-sjabloon voor deze dienst (zuivere functie: ook voor de schermafdrukken en UI-tests)."""
    link = public_view(inp.doc)
    unreadable = is_unreadable(inp.doc)
    state = str(link.get("state"))
    form = may_pair(inp.doc)
    tijd, iso_ = fmt_time(inp.now)

    linked = state == "gekoppeld" and not unreadable
    confirmed = linked and bool(inp.doc.get("confirmed_at"))
    bezig = bool(link.get("koppelen_bezig")) and not unreadable
    shown = inp.melding if inp.melding in NOTICES and NOTICES[inp.melding].toon == "fout" else None

    rows = _readiness_rows(inp.checks)
    hub = _hub_row(link, unreadable, shown)
    if hub:
        rows.append(hub)
    rows += _health_rows(inp.health, linked)
    for r in rows:
        bron_tijd = r.pop("tijd_bron", "")
        t, i = fmt_time(bron_tijd) if bron_tijd else (tijd, iso_)
        r.update(tijd=_short(t), iso=i)
    service_rows = _service_rows(inp)
    later = sum(1 for r in service_rows if r["toon"] == "nog-niet")
    open_ = sum(1 for r in service_rows if r["toon"] == "gate" and r["label"] == "Niet ingesteld")
    totals = [AS_TOTAL[r["toon"]] for r in rows]
    totals += [SERVICE_BLOCK_TOTAL[r["toon"]] for r in service_rows if r["toon"] in SERVICE_BLOCK_TOTAL]
    worst = _worst(totals)

    # Totaal = slechtste van gezondheid en gereedheid; "Niet gereed" is nooit groen (ONTWERPTAAL §13.2, contract §3.7).
    gereed_ok = inp.ready and confirmed
    if gereed_ok:
        label = TOTAL_LABEL[worst]
    else:
        label = "Niet gereed"
        worst = "onbekend" if worst == "gezond" else worst
    uitleg = _toelichting(
        failing=[r["naam"] for r in rows[: len(CHECK_NAMES)] if r["toon"] == "storing"],
        unreadable=unreadable,
        ready=inp.ready,
        state=state,
        linked=linked,
        confirmed=confirmed,
        bezig=bezig,
        worst=worst,
        rows_beperkt=any(AS_TOTAL[r["toon"]] == "beperkt" for r in rows),
    )
    if gereed_ok and (later or open_):
        # ONTWERPTAAL §4.3: "Nog niet beschikbaar" en "Niet ingesteld" verslechteren het totaal niet, maar staan wel in
        # de telling
        delen = []
        if later:
            delen.append(f"{later} {'functie is' if later == 1 else 'functies zijn'} nog niet beschikbaar")
        if open_:
            delen.append(f"{open_} {'instelling staat' if open_ == 1 else 'instellingen staan'} nog open")
        zin = " en ".join(delen)
        uitleg += f" {zin[0].upper()}{zin[1:]}."

    data: dict[str, Any] = {
        "voorbeeld": False,
        "dienst_naam": SERVICE_NAME,
        "dienst_omschrijving": SERVICE_DESCRIPTION,
        "spring_doel": "inhoud",
        "spring_tekst": "Naar de inhoud",
        "totaal_toon": worst,
        "totaal_label": label,
        "totaal_toelichting": uitleg,
        "checks": rows,
        "gereed": _gereed(ok=gereed_ok, ready=inp.ready, unreadable=unreadable, state=state, linked=linked),
        "versie": __version__,
        "bron": SOURCE_TEXT,
        "gereed_uitleg": GEREED_UITLEG,
        "bijgewerkt": tijd,
        "bijgewerkt_iso": iso_,
        "gekoppeld": linked,
        "ontkoppeld": state == "ontkoppeld" and not unreadable,
        "ingetrokken": state == "ingetrokken" and not unreadable,
        "koppelstatus_onleesbaar": unreadable,
        "koppelformulier": form,
        "koppelen_bezig": bezig,
        "koppelcode_geldig": PAIRING_CODE_VALID,
        "csrf_token": inp.csrf,
        "dienst_blok_titel": "Wat deze versie doet",
        "dienst_blok": service_rows,
    }
    started = (inp.doc.get("pending") or {}).get("started_at") if bezig else None
    if started:
        sinds, sinds_iso = fmt_time(str(started))
        if sinds_iso:
            data.update(koppelen_bezig_sinds=_short(sinds), koppelen_bezig_iso=sinds_iso)
    if linked:
        sinds, _ = fmt_time(link.get("linked_at"))
        data.update(
            gekoppeld_sinds=sinds,
            hub_adres=_safe_hub_url(link.get("hub_url")),
            hub_vingerafdruk=link.get("keyset_fingerprint") or "onbekend",
        )
    melding, rol, notice = _notice_data(inp, unreadable, form)
    data.update(melding)
    staat = {"gekoppeld": "gekoppeld", "ontkoppeld": "ontkoppeld", "ingetrokken": "ingetrokken"}.get(
        state, "niet gekoppeld"
    )
    if unreadable:
        staat = "koppelstatus onleesbaar"
    elif bezig and not linked:
        staat = "koppelen bezig"
    titel = f"{label.lower()}, {staat}"
    if rol == "alert" and notice is not None and notice.titel.lower() not in titel:  # niets dubbel in de titel
        titel = f"{notice.titel} · {titel}"
    data["titel_toestand"] = titel
    return data


def render_page(template: PeilTemplate, inp: PageInput) -> str:
    return strip_comments(render(template.body, page_data(inp)))


# ---------------------------------------------------------------------------------------------------- HTTP
def _template(app: FastAPI) -> PeilTemplate:
    tpl: PeilTemplate | None = getattr(app.state, "peil_template", None)
    if tpl is None:
        tpl = load_template(app.state.settings.peil_dir or default_dir())
        app.state.peil_template = tpl
    return tpl


def _fallback(request: Request, exc: Exception) -> Response:
    """Sjabloon onbruikbaar (bouwfout): eerlijke, minimale pagina met code + Trace-ID; de API werkt los hiervan."""
    tid = html.escape(trace_id_for(request))
    log.error("statuspagina-sjabloon onbruikbaar: %s", exc)
    # Zonder sjabloon geen Peil-kleuren: alleen systeemkleuren (licht/donker via color-scheme), systeemfont, ≥ 14 px,
    # 44 px-doelen en een selecteerbaar Trace-ID-blok (Foutpaneel op paginaniveau: wat, waarom, wat je kunt doen).
    body = (
        '<!doctype html>\n<html lang="nl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark"><meta name="robots" content="noindex">'
        f"<title>{SERVICE_NAME} — statuspagina niet beschikbaar</title>"
        "<style>html{background:Canvas;color:CanvasText}"
        'body{margin:0;font:400 1rem/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans",Arial,'
        "sans-serif}main{max-width:47.5rem;margin:0 auto;padding:1.5rem 1rem 3rem;overflow-wrap:break-word}"
        "h1{font-size:1.5rem;line-height:2rem}"
        'code{font-family:ui-monospace,"Cascadia Mono","Roboto Mono","DejaVu Sans Mono",Menlo,Consolas,'
        "monospace;font-size:max(.9375em,.875rem);overflow-wrap:anywhere}.selecteer{display:block;margin-top:.25rem;"
        "padding:.5rem .625rem;border:2px dashed;border-radius:.25rem;user-select:all;word-break:break-all}"
        "ul{list-style:none;padding:0;display:flex;flex-wrap:wrap;gap:0 1rem}"
        "a{display:inline-flex;align-items:center;min-height:2.75rem;color:LinkText;text-underline-offset:3px}"
        ":focus-visible{outline:3px solid;outline-offset:2px;box-shadow:0 0 0 2px Canvas}</style></head>"
        f"<body><main><h1>{SERVICE_NAME}</h1>"
        "<p><strong>De statuspagina is niet beschikbaar.</strong> Het sjabloon van de statuspagina in deze image ontbreekt "
        "of is gewijzigd, dus de dienst toont hem niet. De dienst zelf (API, /healthz en /readyz) werkt los hiervan. "
        "Werk de app bij naar een andere image-versie, of meld de fout met de code en de Trace-ID hieronder.</p>"
        "<p>Code <code>peil_sjabloon_ongeldig</code> · Trace-ID (selecteer en kopieer)</p>"
        f'<code class="selecteer">{tid}</code>'
        '<ul><li><a href="/healthz">/healthz (proces leeft)</a></li>'
        '<li><a href="/readyz">/readyz (afhankelijkheden)</a></li></ul>'
        "</main></body></html>"
    )
    return _with_headers(HTMLResponse(body, status_code=500))


def _csrf_token(request: Request) -> str:
    tok = request.cookies.get(CSRF_COOKIE)
    if tok and 20 <= len(tok) <= 100:
        return tok
    return secrets.token_urlsafe(24)


def _with_headers(resp: Response, csrf: str | None = None) -> Response:
    for k, v in SECURITY_HEADERS.items():
        resp.headers[k] = v
    if csrf is not None:
        resp.set_cookie(CSRF_COOKIE, csrf, httponly=True, samesite="strict", path="/", max_age=3600)
    return resp


def _db_facts(request: Request) -> tuple[dict[str, int] | None, list[dict[str, Any]] | None]:
    """Aantal bronnen per status en de connector-gezondheid; ``None`` als de database dat niet kan geven (eerlijk
    "onbekend", nooit 0)."""
    state = request.app.state
    if not state.db.ping():
        return None, None
    try:
        with state.db.sessions() as s:
            counts = {
                str(st): int(n)
                for st, n in s.execute(select(IndexSource.status, func.count()).group_by(IndexSource.status))
            }
            return counts, list(connector_health(request, s)["checks"])
    except Exception:  # bv. migraties nog niet klaar
        log.info("statuspagina: bronnen of connector-gezondheid niet leesbaar (database nog niet klaar)")
        return None, None


def _page_input(request: Request, csrf: str, melding: str | None = None, trace_id: str | None = None) -> PageInput:
    state = request.app.state
    ready, checks = readiness(request)
    roots, root_warnings = validate_read_roots(state.settings.read_roots, state.settings.data_dir)
    per_status, health = _db_facts(request)
    return PageInput(
        ready=ready,
        checks=checks,
        doc=state.hub_link.read_safe(),
        per_status=per_status,
        roots=len(roots),
        root_warnings=len(root_warnings),
        csrf=csrf,
        now=utcnow(),
        melding=melding,
        trace_id=trace_id,
        health=health,
        host_blocked=not host_allowed(request.headers.get("host", "")),
    )


@router.get("/", response_class=HTMLResponse)
def status_page(request: Request) -> Response:
    """Toont de stand van nu. Een melding over een poging hoort alleen bij het antwoord op die POST (§3.7); een
    ``?melding=`` in de URL heeft geen effect."""
    try:
        tpl = _template(request.app)
    except PeilTemplateError as exc:
        return _fallback(request, exc)
    csrf = _csrf_token(request)
    return _with_headers(HTMLResponse(render_page(tpl, _page_input(request, csrf))), csrf)


def _audit_pair(request: Request, ok: bool, code: str) -> None:
    state = request.app.state
    tid = trace_id_for(request)
    try:
        with state.db.sessions() as s:
            audit.record(
                s,
                domain="platform",
                action="hub.pair" if ok else "hub.pair_failed",
                entity_type="hub_link",
                entity_id="hub",
                principal=None,
                trace_id=tid,
                actor_override=("user", "statuspagina"),
                message_nl="Gekoppeld met de Hub via de statuspagina."
                if ok
                else "Koppelen via de statuspagina mislukt.",
                details={"code": code},
            )
            if ok:
                events.emit_service(
                    s,
                    "service.hub_link.linked",
                    actor_type="user",
                    actor_id="statuspagina",
                    trace_id=tid,
                    payload={"via": "koppelcode"},
                )
            s.commit()
    except Exception:  # de koppeling zelf staat al vast in hub-link.json; het auditspoor mag haar niet ongedaan maken
        log.warning("auditspoor van het koppelen kon niet worden vastgelegd (database niet beschikbaar)")


def _answer(request: Request, code: str) -> Response:
    """Antwoord op een POST: de pagina met de melding van dít verzoek en de HTTP-status van die melding (§3.7), plus
    precies één logregel met code en Trace-ID (nooit het ingevulde Hub-adres, nooit geheimen)."""
    notice = NOTICES[code]
    tid = trace_id_for(request)
    level = logging.INFO if notice.toon == "info" else logging.WARNING
    log.log(level, "statuspagina: koppelpoging afgehandeld (code %s, HTTP %s, Trace-ID %s)", code, notice.http, tid)
    try:
        tpl = _template(request.app)
    except PeilTemplateError as exc:
        return _fallback(request, exc)
    csrf = _csrf_token(request)
    body = render_page(tpl, _page_input(request, csrf, melding=code, trace_id=tid))
    resp = _with_headers(HTMLResponse(body, status_code=notice.http), csrf)
    resp.headers[TRACE_HEADER] = tid
    if notice.http == 429:
        resp.headers["Retry-After"] = str(int(WINDOW_S))
    return resp


@router.post("/koppelen")
def pair(
    request: Request,
    csrf: str = Form(""),
    hub_adres: str = Form(""),
    koppelcode: str = Form(""),
    hub_vingerafdruk: str = Form(""),
) -> Response:
    """Veldnamen zoals het Peil-sjabloon: ``hub_adres``, ``koppelcode``, ``hub_vingerafdruk`` (+ ``csrf``).

    Volgorde (§3.7): herkomst (Origin, Sec-Fetch-Site, Host) → telt als poging (5 per IP / 20 per dienst per 15 min) →
    formuliertoken → onleesbaar koppelbestand → al eens gekoppeld → lopende poging → invoer → Hub."""
    state = request.app.state
    refused = origin_refusal(request)
    if refused is not None:
        return _answer(request, refused)
    client_ip = request.client.host if request.client else "onbekend"
    if not state.pair_throttle.allow(client_ip):
        return _answer(request, "te_veel_pogingen")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not cookie or not csrf or not hmac.compare_digest(cookie, csrf):
        return _answer(request, "ongeldig_formulier")
    doc = state.hub_link.read_safe()
    if is_unreadable(doc):
        return _answer(request, "koppelstatus_onleesbaar")
    if doc.get("state") != "nooit_gekoppeld" or doc.get("first_linked_at"):
        return _answer(request, "al_gekoppeld")
    if doc.get("pending") is not None:
        return _answer(request, "koppelen_bezig")
    # Invoer als geheel controleren, nooit eerst afkappen: een te lange invoer is ongeldig (Hub-adres ≤ 512, koppelcode
    # ≤ 32, vingerafdruk ≤ 64), ook als het begin ervan toevallig een geldige waarde is.
    try:
        url = validate_hub_url(hub_adres, state.settings.allow_loopback_hub)
    except HubUrlError:
        return _answer(request, "hub_adres_ongeldig")
    code = normalize_pairing_code(koppelcode)
    if code is None:
        return _answer(request, "koppelcode_ongeldig")
    fingerprint = normalize_fingerprint(hub_vingerafdruk)
    if fingerprint is None:
        return _answer(request, "vingerafdruk_ongeldig")
    outcome = state.enrollment.enroll_with_code(url, code, fingerprint)
    if outcome.code != "koppelen_bezig":
        _audit_pair(request, outcome.ok, outcome.code)
    if outcome.ok:
        return _answer(request, "gekoppeld" if outcome.detail.get("confirmed") else "gekoppeld_onbevestigd")
    return _answer(request, outcome.code if outcome.code in NOTICES else "hub_onbereikbaar")
