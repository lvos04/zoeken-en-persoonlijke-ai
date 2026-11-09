"""Kleine Nederlandse statuspagina op ``GET /`` (contract §1) in de ontwerptaal **Peil** (ONTWERPTAAL §13.2) +
koppelen met koppelcode (contract 1.2 §3.7).

Vormgeving: het framework-vrije Peil-sjabloon ``design/ontwerptaal/statuspagina.html`` (versie-gepinde kopie, O-14):
CSS en iconen inline, **geen JavaScript**, geen externe bron; past binnen ``default-src 'none'``.

Veiligheid:
- Geen geheimen, geen bronnamen, geen paden en geen zoekinhoud op de pagina; alleen de witte lijst van de koppelstatus
  (§3.5), versie, gezondheid en totaalaantallen.
- Het koppelformulier (Hub-adres, koppelcode, **Hub-vingerafdruk**, alle drie verplicht) bestaat **alleen zolang de dienst
  nog nooit gekoppeld is**. Daarna: alleen status en "ontkoppelen of opnieuw koppelen gaat via de Hub". Er is hier geen
  ontkoppelen, geen lokale ontkoppelcode en geen herkoppelen na intrekking.
- Formulieren: CSRF-token (double-submit-cookie, SameSite=Strict, HttpOnly) + Origin-controle; strikte CSP. Max. 5
  pogingen per 15 min per bron-IP (adres van de TCP-verbinding; nooit ``X-Forwarded-For``) en 20 per 15 min voor de hele
  dienst; daarboven gaat er niets naar de Hub.
- Meldingen komen uit een vaste lijst (geen weerspiegelde invoer); het Hub-adres van een mislukte poging wordt nergens
  bewaard en ook niet teruggezet in het formulier.
- Foutweergave met code + Trace-ID (selecteer en kopieer): na de redirect (POST → 303 → GET) zoekt de pagina de
  Trace-ID van de poging op met een willekeurige, kortlevende sleutel (``poging``). De Trace-ID komt dus nooit uit de URL.
- Eerste signaal is nooit groen als de dienst niet gereed is: het totaal is het slechtste van de eigen controles
  (``/readyz``) en de koppeling met de Hub.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from . import APP_ID, __version__, audit, events
from .errors import trace_id_for
from .health import connector_health, readiness
from .hubclient import HubUrlError, validate_hub_url
from .hublink import ERROR_MESSAGES, public_view
from .peil import PeilTemplate, PeilTemplateError, default_dir, load_template, render, strip_comments
from .search.models import IndexSource
from .search.paths import validate_read_roots
from .security.pins import normalize_fingerprint, normalize_pairing_code
from .timeutil import utcnow

log = logging.getLogger("search_ai.status_page")
router = APIRouter(include_in_schema=False)

CSRF_COOKIE = "sai_csrf"
SERVICE_NAME = "Zoeken en persoonlijke AI"
SERVICE_DESCRIPTION = (
    f"Persoonlijke zoekmachine en, later, spraak en persoonlijke AI (dienst {APP_ID} in de Hub). Alle schermen staan in "
    "de Hub; dit is alleen de statuspagina."
)
PAIRING_CODE_VALID = "60 minuten"  # contract 1.2 §3.6: koppelcode verloopt 60 min na aanmaak in de Hub
WINDOW_S = 900.0
MONTHS = ("jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec")


@dataclass(frozen=True)
class Notice:
    toon: str  # "fout" | "info" (klassen melding-fout / melding-info van het sjabloon)
    titel: str
    tekst: str
    veld: str | None = None  # "hub" | "koppelcode" | "vingerafdruk": het formulierveld met de fout
    veldfout: str | None = None


_FAILED = "Koppelen is mislukt"
NOTICES: dict[str, Notice] = {
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
    "al_gekoppeld": Notice(
        "fout",
        "Koppelen kan hier niet",
        "Deze dienst is al eens gekoppeld. Ontkoppelen of opnieuw koppelen gaat via de Hub, niet via deze pagina.",
    ),
    "ongeldig_formulier": Notice(
        "fout",
        "Het formulier is verlopen",
        "Het formulier is verlopen of ongeldig. Laad de pagina opnieuw en probeer het nog eens.",
    ),
    "te_veel_pogingen": Notice(
        "fout",
        "Te veel pogingen",
        "Te veel pogingen. Wacht een kwartier en probeer het opnieuw. Er is niets naar de Hub gestuurd.",
    ),
    "koppelen_bezig": Notice(
        "fout",
        "Er loopt al een koppelpoging",
        "Er loopt al een koppelpoging. Wacht tot die klaar is en laad de pagina daarna opnieuw.",
    ),
    "ongeldig_hub_adres": Notice(
        "fout",
        _FAILED,
        "Het adres van de Hub is ongeldig. Er is niets naar de Hub gestuurd.",
        "hub",
        "Gebruik http(s)://<NAS-adres>:<poort>, zonder pad en geen localhost.",
    ),
    "koppelcode_ongeldig": Notice(
        "fout",
        _FAILED,
        "De koppelcode heeft niet de goede vorm. Er is niets naar de Hub gestuurd.",
        "koppelcode",
        "12 tekens, zoals de Hub ze toont (XXXX-XXXX-XXXX).",
    ),
    "vingerafdruk_ongeldig": Notice(
        "fout",
        _FAILED,
        "De vingerafdruk van de Hub heeft niet de goede vorm. Er is niets naar de Hub gestuurd.",
        "vingerafdruk",
        "16 tekens, zoals de Hub ze toont (XXXX-XXXX-XXXX-XXXX).",
    ),
    "hub_onbereikbaar": Notice(
        "fout",
        _FAILED,
        "De Hub is niet bereikbaar op dit adres. Controleer het adres en of de Hub draait, en probeer het opnieuw.",
        "hub",
        "Geen antwoord van een Hub op dit adres.",
    ),
    "hub_weigerde": Notice(
        "fout",
        _FAILED,
        ERROR_MESSAGES["hub_weigerde"],
        "koppelcode",
        "Deze koppelcode is verlopen, al gebruikt of ongeldig. Maak een nieuwe in de Hub.",
    ),
    "hub_antwoord_ongeldig": Notice("fout", _FAILED, ERROR_MESSAGES["hub_antwoord_ongeldig"]),
    "hub_vingerafdruk_onjuist": Notice(
        "fout",
        _FAILED,
        ERROR_MESSAGES["hub_vingerafdruk_onjuist"],
        "vingerafdruk",
        "Deze vingerafdruk hoort niet bij de Hub op dit adres. Vergelijk hem met de Hub.",
    ),
    "enroll_endpoint_ontbreekt": Notice(
        "fout",
        _FAILED,
        ERROR_MESSAGES["enroll_endpoint_ontbreekt"] + " Controleer of dit adres echt de Hub is.",
        "hub",
        "Op dit adres draait geen Hub die koppelen kent.",
    ),
    "rate_limited": Notice(
        "fout",
        _FAILED,
        "De Hub vraagt even te wachten (te veel pogingen). Probeer het over een paar minuten opnieuw.",
    ),
}
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
SEVERITY = {"gezond": 0, "onbekend": 1, "beperkt": 2, "storing": 3}
TOTAL_LABEL = {"gezond": "Alles gezond", "onbekend": "Deels onbekend", "beperkt": "Beperkt", "storing": "Storing"}
LABEL = {"gezond": "Gezond", "onbekend": "Onbekend", "beperkt": "Beperkt", "storing": "Storing"}


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


class AttemptLog:
    """Trace-ID van recente koppelpogingen voor de foutweergave na de redirect. Alleen in het geheugen, hooguit
    ``max_items`` en ``ttl_s``; de sleutel is willekeurig (niet te raden) en verschijnt nooit op de pagina."""

    KEY = re.compile(r"[A-Za-z0-9_-]{22,64}")

    def __init__(self, ttl_s: float = WINDOW_S, max_items: int = 256) -> None:
        self.ttl_s, self.max_items = ttl_s, max_items
        self._items: OrderedDict[str, tuple[str, str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, code: str, trace_id: str, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        key = secrets.token_urlsafe(16)
        with self._lock:
            self._items[key] = (code, trace_id, now)
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)
        return key

    def get(self, key: str | None, code: str, now: float | None = None) -> str | None:
        if not key or not self.KEY.fullmatch(key):
            return None
        now = time.monotonic() if now is None else now
        with self._lock:
            item = self._items.get(key)
            if item is None or now - item[2] >= self.ttl_s:
                self._items.pop(key, None)
                return None
        return item[1] if item[0] == code else None


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


# ---------------------------------------------------------------------------------------------------- weergave
@dataclass(frozen=True)
class PageInput:
    """Alles wat de pagina toont. ``doc`` is het koppelbestand (``read_safe``); de pagina gebruikt daarvan alleen de
    witte lijst van §3.5 (``public_view``) plus ``corrupt``."""

    ready: bool
    checks: list[dict[str, Any]]
    doc: dict[str, Any]
    per_status: dict[str, int] | None
    roots: int
    root_warnings: int
    csrf: str
    now: datetime
    melding: str | None = None
    trace_id: str | None = None
    # controles van de connector-gezondheid (wat de Hub ziet); alleen afwijkingen worden een regel, zodat de pagina
    # nooit "Alles gezond" zegt terwijl de Hub "Beperkt" of "Storing" toont (ONTWERPTAAL §1 punt 8)
    health: list[dict[str, Any]] | None = None


def may_pair(doc: dict[str, Any]) -> bool:
    """Koppelen via de statuspagina alleen zolang de dienst nog **nooit** gekoppeld is (§3.7)."""
    return doc.get("state") == "nooit_gekoppeld" and not doc.get("first_linked_at") and not doc.get("corrupt")


def fmt_time(value: datetime | str | None) -> tuple[str, str]:
    """``("9 nov 2026, 11:56", ISO)`` in de tijdzone van de container (TrueNAS zet ``TZ``); UTC staat er dan bij."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return "onbekend", ""
    if value is None:
        return "onbekend", ""
    local = value.astimezone()
    text = f"{local.day} {MONTHS[local.month - 1]} {local.year}, {local:%H:%M}"
    if local.utcoffset() == timedelta(0):
        text += " UTC"
    return text, local.isoformat(timespec="seconds")


def _readiness_rows(checks: list[dict[str, Any]], tijd: str, iso_: str) -> list[dict[str, str]]:
    by_name = {str(c.get("name")): str(c.get("status")) for c in checks}
    rows = []
    for name, label in CHECK_NAMES.items():
        status = by_name.get(name)
        if status == "ok":
            toon, reden = "gezond", ""
        elif status is None:  # bv. migraties/pgvector zonder database: niet gemeten, dus nooit groen
            toon, reden = "onbekend", "Niet te controleren zonder database."
        else:
            toon, reden = "storing", CHECK_FAILED[name]
        rows.append({"naam": label, "toon": toon, "label": LABEL[toon], "tijd": tijd, "iso": iso_, "reden": reden})
    return rows


def _hub_row(link: dict[str, Any], corrupt: bool) -> dict[str, str] | None:
    """Regel "Koppeling met de Hub" in de controles; ``None`` als de dienst gekoppeld is zonder melding (dan staat de
    neutrale pil "Gekoppeld" in het koppelblok: verbonden is geen gezondheid, dus nooit groen)."""
    state = link.get("state")
    err = link.get("last_error") or {}
    err_text = f" Laatste melding: {err.get('message_nl')} (code {err.get('code')})" if err.get("code") else ""
    if corrupt:
        reden = f"{ERROR_MESSAGES['koppelstatus_onleesbaar']} (code koppelstatus_onleesbaar)"
        return {"toon": "storing", "label": "Onleesbaar", "reden": reden}
    if state == "gekoppeld":
        return {"toon": "beperkt", "label": "Beperkt", "reden": err_text.strip()} if err_text else None
    if state == "ontkoppeld":
        reden = "Ontkoppeld door de Hub; de dienst weigert elke Hub-aanvraag." + err_text
        return {"toon": "onbekend", "label": "Ontkoppeld", "reden": reden}
    if state == "ingetrokken":
        reden = "Ingetrokken door de Hub; de dienst weigert elke opdracht van de Hub."
        return {"toon": "onbekend", "label": "Ingetrokken", "reden": reden}
    reden = "Tot de koppeling voert de dienst niets uit voor de Hub."
    if link.get("koppelen_bezig"):
        reden += " Er loopt een koppelpoging."
    return {"toon": "onbekend", "label": "Niet gekoppeld", "reden": reden + err_text}


HEALTH_ROWS = {
    # naam in de connector-gezondheid → (regelnaam, reden); alleen afwijkingen (degraded/down) worden getoond
    "hub_sleutels": ("Gepinde Hub-sleutels", "De gepinde sleutels van de Hub zijn ongeldig of een rotatie is mislukt."),
    "outbox": ("Gebeurtenissen naar de Hub", "Er staan gebeurtenissen vast of lopen achter; zie de Hub."),
    "intrekking_opgeschoond": (
        "Opschonen na intrekking",
        "Een ingetrokken bron is nog niet volledig opgeschoond; zie de app-log.",
    ),
}
HEALTH_TOON = {"degraded": "beperkt", "down": "storing"}


def _health_rows(health: list[dict[str, Any]] | None, linked: bool) -> list[dict[str, str]]:
    rows = []
    for c in health or []:
        name, status, code = str(c.get("name")), str(c.get("status")), c.get("reason_code")
        toon = HEALTH_TOON.get(status)
        if toon is None:
            continue
        if name == "hub_koppeling" and linked and code == "niet_bevestigd":
            naam, reden = (
                "Koppeling met de Hub",
                "De Hub heeft de koppeling nog niet bevestigd; de dienst probeert het opnieuw.",
            )
        elif name in HEALTH_ROWS and not (name == "hub_sleutels" and code == "sleutelrotatie_ongeldig"):
            naam, reden = HEALTH_ROWS[name]  # een mislukte rotatie staat al als laatste melding bij de koppeling
        else:
            continue
        rows.append({"naam": naam, "toon": toon, "label": LABEL[toon], "reden": f"{reden} (code {code or name})"})
    return rows


def _sources_summary(per_status: dict[str, int] | None) -> str:
    if per_status is None:
        return "Aantal bronnen onbekend (database niet bereikbaar)"
    if not sum(per_status.values()):
        return "Nog geen bronnen"
    return ", ".join(f"{n} {STATUS_NL.get(st, st)}" for st, n in sorted(per_status.items()))


def _service_rows(inp: PageInput) -> list[dict[str, str]]:
    if inp.roots and not inp.root_warnings:
        roots = ("gezond", "Gezond", f"{inp.roots} geldig.")
    elif inp.roots:
        roots = ("beperkt", "Beperkt", f"{inp.roots} geldig, {inp.root_warnings} genegeerd (zie de app-log).")
    else:
        roots = ("onbekend", "Geen", "Nog geen share gekoppeld.")
    return [
        {
            "naam": "Bronnen (B07)",
            "toon": "gate",
            "label": "Alleen met toestemming",
            "reden": f"{_sources_summary(inp.per_status)}. Zonder toestemming van de eigenaar wordt niets gelezen; "
            "indexdata verlaat de NAS niet.",
        },
        {
            "naam": "Leeswortels",
            "toon": roots[0],
            "label": roots[1],
            "reden": f"{roots[2]} Koppel shares als Host Path met Read Only onder /bronnen.",
        },
        {
            "naam": "Indexeren en semantisch zoeken (B07)",
            "toon": "onbekend",
            "label": "Nog niet beschikbaar",
            "reden": "Volgt in fase P5. Er wordt nu niets geïndexeerd (code capability_unavailable).",
        },
        {
            "naam": "Spraak en persoonlijke AI (B15)",
            "toon": "onbekend",
            "label": "Nog niet beschikbaar",
            "reden": "Volgt in fase P5. Spraak en een lokaal taalmodel zijn optioneel en staan standaard uit; er wordt "
            "niets opgenomen. Typen in de Hub blijft altijd mogelijk (code capability_unavailable).",
        },
    ]


def _safe_hub_url(url: Any) -> str:
    """Alleen http(s) als link (de opgeslagen koppeling is bij het koppelen al gecontroleerd; dit is een vangnet)."""
    if isinstance(url, str) and urlsplit(url).scheme in ("http", "https") and urlsplit(url).netloc:
        return url
    return ""


def page_data(inp: PageInput) -> dict[str, Any]:
    """Plekken van het Peil-sjabloon voor deze dienst (zuivere functie: ook voor de schermafdrukken en UI-tests)."""
    link = public_view(inp.doc)
    corrupt = bool(inp.doc.get("corrupt"))
    state = str(link.get("state"))
    form = may_pair(inp.doc)
    tijd, iso_ = fmt_time(inp.now)
    tijd_kort = tijd.split(", ", 1)[1] if ", " in tijd else tijd

    rows = _readiness_rows(inp.checks, tijd_kort, iso_)
    hub = _hub_row(link, corrupt)
    if hub:
        rows.append({"naam": "Koppeling met de Hub", "tijd": tijd_kort, "iso": iso_, **hub})
    linked = state == "gekoppeld" and not corrupt
    rows += [{**r, "tijd": tijd_kort, "iso": iso_} for r in _health_rows(inp.health, linked)]
    worst = max((r["toon"] for r in rows), key=SEVERITY.__getitem__)
    failing = [r["naam"] for r in rows[: len(CHECK_NAMES)] if r["toon"] == "storing"]

    # Totaal = slechtste van gezondheid en gereedheid; "Niet gereed" is nooit groen (ONTWERPTAAL §13.2).
    if not inp.ready or not linked:
        label = "Niet gereed"
        if worst == "gezond":  # kan niet: niet gereed zonder storing en zonder onbekend → toch niet groen
            worst = "onbekend"
    else:
        label = TOTAL_LABEL[worst]
    if failing:
        uitleg = f"Storing in: {', '.join(failing)}."
        if not linked:
            uitleg += " Ook nog niet gekoppeld met de Hub."
    elif corrupt:
        uitleg = "Storing — de koppelstatus is onleesbaar."
    elif not inp.ready:
        uitleg = "Deels onbekend — de eigen controles zijn niet compleet."
    elif state == "nooit_gekoppeld":
        uitleg = "Deels onbekend — wacht op koppeling met de Hub."
    elif state == "ontkoppeld":
        uitleg = "Deels onbekend — ontkoppeld door de Hub. Opnieuw koppelen gaat via de Hub."
    elif state == "ingetrokken":
        uitleg = "Deels onbekend — de Hub heeft de koppeling ingetrokken. Opnieuw koppelen gaat via de Hub."
    elif worst == "gezond":
        uitleg = "Gereed en gekoppeld met de Hub."
    else:
        uitleg = "Gereed; een of meer controles vragen aandacht (zie hieronder)."
    if inp.ready and linked:
        gereed = "Ja"
    elif inp.ready:
        gereed = "Nee — wacht op koppeling met de Hub (de eigen controles van /readyz zijn in orde)"
    else:
        gereed = "Nee — /readyz meldt een storing of een controle die niet kon worden uitgevoerd"

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
        "gereed": gereed,
        "versie": __version__,
        "bijgewerkt": tijd,
        "bijgewerkt_iso": iso_,
        "gekoppeld": linked,
        "ontkoppeld": state == "ontkoppeld" and not corrupt,
        "ingetrokken": state == "ingetrokken" and not corrupt,
        "koppelformulier": form,
        "koppelcode_geldig": PAIRING_CODE_VALID,
        "csrf_token": inp.csrf,
        "dienst_blok_titel": "Wat deze versie doet",
        "dienst_blok": _service_rows(inp),
    }
    if linked:
        sinds, _ = fmt_time(link.get("linked_at"))
        data.update(
            gekoppeld_sinds=sinds,
            hub_adres=_safe_hub_url(link.get("hub_url")),
            hub_vingerafdruk=link.get("keyset_fingerprint") or "onbekend",
        )

    notice = NOTICES.get(inp.melding or "")
    if notice is not None and notice.toon == "info" and not linked:
        notice = None  # een succesmelding uit de URL mag nooit iets beweren dat de koppelstatus tegenspreekt
    if notice is None and corrupt:
        tekst = f"{ERROR_MESSAGES['koppelstatus_onleesbaar']} (code koppelstatus_onleesbaar)"
        notice = Notice("fout", "Koppelstatus onleesbaar", tekst)
    if notice is not None:
        data.update(
            melding=True,
            melding_toon=notice.toon,
            melding_icoon="storing" if notice.toon == "fout" else "info",
            melding_titel=notice.titel,
            melding_tekst=notice.tekst,
        )
        if notice.toon == "fout" and inp.trace_id:
            data.update(trace_id=inp.trace_id, melding_code=inp.melding)
        if notice.veld and form:
            doel, link_tekst, spring = FIELD_LINK[notice.veld]
            data.update(
                melding_link=True,
                melding_link_doel=doel,
                melding_link_tekst=link_tekst,
                spring_doel=doel,
                spring_tekst=spring,
                **{f"fout_{'hub_adres' if notice.veld == 'hub' else notice.veld}": notice.veldfout},
            )
    staat = {"gekoppeld": "gekoppeld", "ontkoppeld": "ontkoppeld", "ingetrokken": "ingetrokken"}.get(
        state, "niet gekoppeld"
    )
    if corrupt:
        staat = "koppelstatus onleesbaar"
    titel = f"{label.lower()}, {staat}"
    if notice is not None and notice.toon == "fout":
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
    tid = trace_id_for(request)
    log.error("statuspagina-sjabloon onbruikbaar: %s", exc)
    body = (
        '<!doctype html><html lang="nl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{SERVICE_NAME} — statuspagina niet beschikbaar</title></head><body><main><h1>{SERVICE_NAME}</h1>"
        "<p>De statuspagina is niet beschikbaar: het sjabloon ontbreekt of is gewijzigd. De API, /healthz en /readyz "
        "werken los hiervan.</p>"
        f"<p>Code <code>peil_sjabloon_ongeldig</code> · Trace-ID (selecteer en kopieer) <code>{tid}</code></p>"
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


def _form_ok(request: Request, token: str) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    if not cookie or not token or not hmac.compare_digest(cookie, token):
        return False
    origin = request.headers.get("origin")
    if origin and origin != "null":
        host = request.headers.get("host", "")
        if urlsplit(origin).netloc != host:
            return False
    return True


def _redirect(request: Request, notice: str) -> Response:
    url = f"/?melding={notice}"
    if NOTICES[notice].toon == "fout":
        url += f"&poging={request.app.state.pair_attempts.put(notice, trace_id_for(request))}"
    return _with_headers(RedirectResponse(url, status_code=303))


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


@router.get("/", response_class=HTMLResponse)
def status_page(request: Request, melding: str | None = None, poging: str | None = None) -> Response:
    state = request.app.state
    try:
        tpl = _template(request.app)
    except PeilTemplateError as exc:
        return _fallback(request, exc)
    csrf = _csrf_token(request)
    ready, checks = readiness(request)
    roots, root_warnings = validate_read_roots(state.settings.read_roots, state.settings.data_dir)
    code = melding if melding in NOTICES else None
    per_status, health = _db_facts(request)
    inp = PageInput(
        ready=ready,
        checks=checks,
        doc=state.hub_link.read_safe(),
        per_status=per_status,
        roots=len(roots),
        root_warnings=len(root_warnings),
        csrf=csrf,
        now=utcnow(),
        melding=code,
        trace_id=state.pair_attempts.get(poging, code) if code else None,
        health=health,
    )
    return _with_headers(HTMLResponse(render_page(tpl, inp)), csrf)


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
                events.emit(
                    s,
                    event_type="service.hub_link.paired",
                    entity_type="hub_link",
                    entity_id="hub",
                    actor_type="user",
                    actor_id="statuspagina",
                    trace_id=tid,
                    payload={"via": "koppelcode"},
                )
            s.commit()
    except Exception:  # de koppeling zelf staat al vast in hub-link.json; het auditspoor mag haar niet ongedaan maken
        log.warning("auditspoor van het koppelen kon niet worden vastgelegd (database niet beschikbaar)")


@router.post("/koppelen")
def pair(
    request: Request,
    csrf: str = Form(""),
    hub_adres: str = Form(""),
    koppelcode: str = Form(""),
    hub_vingerafdruk: str = Form(""),
) -> Response:
    """Veldnamen zoals het Peil-sjabloon: ``hub_adres``, ``koppelcode``, ``hub_vingerafdruk`` (+ ``csrf``)."""
    state = request.app.state
    if not _form_ok(request, csrf):
        return _redirect(request, "ongeldig_formulier")
    client_ip = request.client.host if request.client else "onbekend"
    if not state.pair_throttle.allow(client_ip):
        return _redirect(request, "te_veel_pogingen")
    if not may_pair(state.hub_link.read_safe()):
        return _redirect(request, "al_gekoppeld")
    try:
        url = validate_hub_url(hub_adres[:500], state.settings.allow_loopback_hub)
    except HubUrlError:
        return _redirect(request, "ongeldig_hub_adres")
    if normalize_pairing_code(koppelcode[:32]) is None:
        return _redirect(request, "koppelcode_ongeldig")
    if normalize_fingerprint(hub_vingerafdruk[:64]) is None:
        return _redirect(request, "vingerafdruk_ongeldig")
    outcome = state.enrollment.enroll_with_code(url, koppelcode[:32], hub_vingerafdruk[:64])
    _audit_pair(request, outcome.ok, outcome.code)
    if outcome.ok:
        confirmed = bool(outcome.detail.get("confirmed"))
        return _redirect(request, "gekoppeld" if confirmed else "gekoppeld_onbevestigd")
    return _redirect(request, outcome.code if outcome.code in NOTICES else "hub_onbereikbaar")
