"""Kleine Nederlandse statuspagina op ``GET /`` (contract §1) + koppelen met koppelcode (contract 1.2 §3.7).

Veiligheid:
- Geen geheimen, geen bronnamen, geen paden en geen zoekinhoud op de pagina; alleen de witte lijst van de koppelstatus
  (§3.5), versie, gezondheid en totaalaantallen.
- Het koppelformulier (Hub-adres, koppelcode, **Hub-vingerafdruk**, alle drie verplicht) bestaat **alleen zolang de dienst
  nog nooit gekoppeld is**. Daarna: alleen status en "ontkoppelen of opnieuw koppelen gaat via de Hub". Er is hier geen
  ontkoppelen, geen lokale ontkoppelcode en geen herkoppelen na intrekking.
- Formulieren: CSRF-token (double-submit-cookie, SameSite=Strict, HttpOnly) + Origin-controle; geen JavaScript; strikte
  CSP. Max. 5 pogingen per 15 min per bron-IP (adres van de TCP-verbinding; nooit ``X-Forwarded-For``) en 20 per 15 min
  voor de hele dienst; daarboven gaat er niets naar de Hub.
- Meldingen komen uit een vaste lijst (geen weerspiegelde invoer); het Hub-adres van een mislukte poging wordt nergens
  bewaard.
"""

from __future__ import annotations

import hmac
import html
import logging
import secrets
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from . import APP_ID, __version__, audit, events
from .errors import trace_id_for
from .health import readiness
from .hubclient import HubUrlError, validate_hub_url
from .hublink import ERROR_MESSAGES, public_view
from .search.models import IndexSource
from .search.paths import validate_read_roots
from .security.pins import normalize_fingerprint, normalize_pairing_code

log = logging.getLogger("search_ai.status_page")
router = APIRouter(include_in_schema=False)

CSRF_COOKIE = "sai_csrf"
NOTICES = {
    "gekoppeld": ("ok", "Gekoppeld met de Hub."),
    "al_gekoppeld": (
        "fout",
        "Deze dienst is al eens gekoppeld. Ontkoppelen of opnieuw koppelen gaat via de Hub, niet via deze pagina.",
    ),
    "ongeldig_formulier": (
        "fout",
        "Het formulier is verlopen of ongeldig. Laad de pagina opnieuw en probeer het nog eens.",
    ),
    "te_veel_pogingen": ("fout", "Te veel pogingen. Wacht een kwartier en probeer het opnieuw."),
    "koppelen_bezig": (
        "fout",
        "Er loopt al een koppelpoging. Wacht tot die klaar is en laad de pagina daarna opnieuw.",
    ),
    "ongeldig_hub_adres": (
        "fout",
        "Het Hub-adres is ongeldig. Gebruik http(s)://<NAS-adres>:<poort>, zonder pad en geen localhost.",
    ),
    "koppelcode_ongeldig": ("fout", "De koppelcode is ongeldig: 12 tekens, zoals de Hub ze toont (XXXX-XXXX-XXXX)."),
    "vingerafdruk_ongeldig": (
        "fout",
        "De Hub-vingerafdruk is ongeldig: 16 tekens, zoals de Hub ze toont (XXXX-XXXX-XXXX-XXXX).",
    ),
    "hub_onbereikbaar": ("fout", "De Hub is niet bereikbaar op dit adres. Controleer het adres en of de Hub draait."),
    **{
        code: ("fout", ERROR_MESSAGES[code])
        for code in (
            "hub_weigerde",
            "hub_antwoord_ongeldig",
            "hub_vingerafdruk_onjuist",
            "enroll_endpoint_ontbreekt",
            "rate_limited",
        )
    },
}

STATE_NL = {
    "nooit_gekoppeld": ("onb", "nog niet gekoppeld"),
    "gekoppeld": ("ok", "gekoppeld"),
    "ontkoppeld": ("let", "ontkoppeld"),
    "ingetrokken": ("fout", "ingetrokken"),
}
STATE_TEXT = {
    "nooit_gekoppeld": "Deze dienst is nog niet gekoppeld met de Hub.",
    "gekoppeld": "Gekoppeld met de Hub.",
    "ontkoppeld": "Ontkoppeld door de Hub. Opnieuw koppelen gaat via de Hub (nieuw koppeltoken).",
    "ingetrokken": "Ingetrokken door de Hub. Elke Hub-aanvraag wordt geweigerd; opnieuw koppelen gaat via de Hub.",
}
STATUS_NL = {"draft": "concept", "active": "actief", "paused": "gepauzeerd", "revoked": "ingetrokken"}
WINDOW_S = 900.0


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
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}

STYLE = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1b1f24}
main{max-width:760px;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:.5rem 0}h2{font-size:1.1rem;margin:1.2rem 0 .5rem}
.card{background:#fff;border:1px solid #d9dde3;border-radius:8px;padding:12px 14px;margin:10px 0}
.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.85rem;font-weight:600}
.ok{background:#e3f4e8;color:#14532d}.let{background:#fff4d6;color:#713f12}.fout{background:#fde2e2;color:#7f1d1d}
.onb{background:#e8eaee;color:#374151}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:6px 4px;border-bottom:1px solid #eef0f3;
vertical-align:top}small,.muted{color:#5b6470}
label{display:block;margin:.5rem 0 .2rem;font-weight:600}input{width:100%;box-sizing:border-box;padding:8px;
border:1px solid #b8bfc8;border-radius:6px;font-size:1rem}
button{margin-top:.8rem;padding:9px 16px;border:0;border-radius:6px;background:#1d4ed8;color:#fff;font-size:1rem;
min-height:44px}
@media (prefers-color-scheme: dark){body{background:#111418;color:#e6e8eb}.card{background:#1a1e24;border-color:#2c323b}
td,th{border-color:#262b33}small,.muted{color:#9aa3ad}input{background:#0f1216;color:#e6e8eb;border-color:#3a414b}}
"""


def _tag(status: str) -> str:
    cls = {
        "ok": "ok",
        "gekoppeld": "ok",
        "healthy": "ok",
        "ready": "ok",
        "degraded": "let",
        "down": "fout",
        "fout": "fout",
        "not_ready": "fout",
    }.get(status, "onb")
    label = {
        "healthy": "gezond",
        "degraded": "beperkt",
        "down": "storing",
        "unknown": "onbekend",
        "ready": "klaar",
        "not_ready": "niet klaar",
        "capability_unavailable": "niet beschikbaar",
    }.get(status, status)
    return f'<span class="tag {cls}">{html.escape(label)}</span>'


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


def _redirect(notice: str) -> Response:
    return _with_headers(RedirectResponse(f"/?melding={notice}", status_code=303))


def _link_card(link: dict[str, Any], corrupt: bool) -> str:
    cls, label = STATE_NL.get(str(link.get("state")), ("onb", "onbekend"))
    rows = [
        f'<span class="tag {cls}">{html.escape(label)}</span><br><small>'
        f"{html.escape(STATE_TEXT.get(str(link.get('state')), ''))}</small>"
    ]
    if link.get("state") == "gekoppeld":
        rows.append(
            f"<br><small>Hub: {html.escape(str(link.get('hub_url')))} · gekoppeld op "
            f"{html.escape(str(link.get('linked_at')))} · token …{html.escape(str(link.get('service_token_last4') or ''))}"
            "</small>"
        )
    if link.get("keyset_fingerprint"):
        rows.append(
            "<br><small>Hub-vingerafdruk (vergelijk met de Hub-UI): "
            f"<b>{html.escape(str(link.get('keyset_fingerprint')))}</b></small>"
        )
    if link.get("koppelen_bezig"):
        rows.append("<br><small>Er loopt een koppelpoging.</small>")
    err = link.get("last_error")
    if isinstance(err, dict) and err.get("code"):
        rows.append(f"<br><small>Laatste melding: {html.escape(str(err.get('message_nl') or ''))}</small>")
    if corrupt:
        rows.append(f"<br><small>{html.escape(ERROR_MESSAGES['koppelstatus_onleesbaar'])}</small>")
    return "".join(rows)


def _may_pair(doc: dict[str, Any]) -> bool:
    """Koppelen via de statuspagina alleen zolang de dienst nog **nooit** gekoppeld is (§3.7)."""
    return doc.get("state") == "nooit_gekoppeld" and not doc.get("first_linked_at") and not doc.get("corrupt")


@router.get("/", response_class=HTMLResponse)
def status_page(request: Request, melding: str | None = None) -> Response:
    state = request.app.state
    csrf = _csrf_token(request)
    ready, checks = readiness(request)
    doc = state.hub_link.read_safe()
    link = public_view(doc)
    per_status: dict[str, int] = {}
    if state.db.ping():
        try:
            with state.db.sessions() as s:
                per_status = {
                    str(st): int(n)
                    for st, n in s.execute(select(IndexSource.status, func.count()).group_by(IndexSource.status))
                }
        except Exception:  # bv. migraties nog niet klaar: toon wat wél bekend is
            per_status = {}
    roots, root_warnings = validate_read_roots(state.settings.read_roots, state.settings.data_dir)

    parts: list[str] = []
    if melding in NOTICES:
        cls, text = NOTICES[melding]
        parts.append(
            f'<div class="card"><span class="tag {cls}">{"Gelukt" if cls == "ok" else "Let op"}</span> '
            f"{html.escape(text)}</div>"
        )
    parts.append(
        '<div class="card"><table>'
        f"<tr><th>Dienst</th><td>{APP_ID} · versie {html.escape(__version__)}</td></tr>"
        f"<tr><th>Gereed</th><td>{_tag('ready' if ready else 'not_ready')} "
        + " ".join(f"<small>{html.escape(c['name'])}: {html.escape(c['status'])}</small>" for c in checks)
        + "</td></tr>"
        f"<tr><th>Koppeling met de Hub</th><td>{_link_card(link, bool(doc.get('corrupt')))}</td></tr></table></div>"
    )
    total = sum(per_status.values())
    summary = (
        ", ".join(f"{n} {STATUS_NL.get(st, st)}" for st, n in sorted(per_status.items()))
        if total
        else "nog geen bronnen"
    )
    parts.append(
        '<div class="card"><h2>Persoonlijke zoekmachine (B07)</h2>'
        f"<p><small>Bronnen: {html.escape(summary)}. Zonder toestemming van de eigenaar wordt niets gelezen; "
        "indexdata verlaat de NAS niet.</small></p>"
        f"<p><small>Leeswortels: {len(roots)} geldig"
        + (f", {len(root_warnings)} genegeerd (zie app-log)" if root_warnings else "")
        + ". Koppel shares als Host Path met <b>Read Only</b> onder /bronnen.</small></p>"
        f"<p>{_tag('capability_unavailable')} <small>Indexeren (ingest) en semantisch zoeken volgen in een latere "
        "versie.</small></p></div>"
    )
    parts.append(
        '<div class="card"><h2>Spraak en persoonlijke AI (B15)</h2>'
        f"<p>{_tag('capability_unavailable')} <small>Spraak (STT/TTS) en een lokaal taalmodel zijn optionele apps die "
        "standaard uit staan. Er wordt niets opgenomen; typen in de Hub blijft altijd mogelijk.</small></p></div>"
    )
    if _may_pair(doc):
        parts.append(
            '<div class="card"><h2>Koppelen met de Hub</h2>'
            f'<p class="muted">Maak in de Hub (Diensten) een koppelcode voor <b>{APP_ID}</b>. De Hub toont de code en '
            "de vingerafdruk samen; vul beide hier in. Gebruik het LAN-adres van de NAS met de poort van de Hub. "
            "Aanbevolen is koppelen via de Hub zelf (koppeltoken in de app-instellingen): dan is dit formulier niet "
            "nodig.</p>"
            '<form method="post" action="/koppelen" autocomplete="off">'
            f'<input type="hidden" name="csrf" value="{html.escape(csrf)}">'
            '<label for="hub_url">Hub-adres</label>'
            '<input id="hub_url" name="hub_url" type="url" required maxlength="500" '
            'placeholder="http://&lt;NAS-adres&gt;:&lt;poort&gt;">'
            '<label for="code">Koppelcode</label>'
            '<input id="code" name="code" required maxlength="32" autocomplete="one-time-code" '
            'placeholder="XXXX-XXXX-XXXX">'
            '<label for="fingerprint">Hub-vingerafdruk</label>'
            '<input id="fingerprint" name="fingerprint" required maxlength="64" placeholder="XXXX-XXXX-XXXX-XXXX">'
            '<button type="submit">Koppelen</button></form></div>'
        )
    else:
        parts.append(
            '<div class="card"><h2>Koppeling beheren</h2>'
            '<p class="muted">Ontkoppelen of opnieuw koppelen gaat via de Hub (Diensten). Op deze pagina kan dat niet: '
            "zo kan niemand op het netwerk de koppeling overnemen.</p></div>"
        )
    body = (
        '<!doctype html><html lang="nl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Zoeken en persoonlijke AI</title><style>{STYLE}</style></head><body><main>"
        "<h1>Zoeken en persoonlijke AI</h1>"
        '<p class="muted">Dienst van het TrueNAS Home Platform: persoonlijke zoekmachine en (later) spraak. '
        "Alle schermen staan in de Hub; dit is alleen de statuspagina.</p>" + "".join(parts) + "</main></body></html>"
    )
    return _with_headers(HTMLResponse(body), csrf)


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
    hub_url: str = Form(""),
    code: str = Form(""),
    fingerprint: str = Form(""),
) -> Response:
    state = request.app.state
    if not _form_ok(request, csrf):
        return _redirect("ongeldig_formulier")
    client_ip = request.client.host if request.client else "onbekend"
    if not state.pair_throttle.allow(client_ip):
        return _redirect("te_veel_pogingen")
    if not _may_pair(state.hub_link.read_safe()):
        return _redirect("al_gekoppeld")
    try:
        url = validate_hub_url(hub_url[:500], state.settings.allow_loopback_hub)
    except HubUrlError:
        return _redirect("ongeldig_hub_adres")
    if normalize_pairing_code(code[:32]) is None:
        return _redirect("koppelcode_ongeldig")
    if normalize_fingerprint(fingerprint[:64]) is None:
        return _redirect("vingerafdruk_ongeldig")
    outcome = state.enrollment.enroll_with_code(url, code[:32], fingerprint[:64])
    _audit_pair(request, outcome.ok, outcome.code)
    return _redirect("gekoppeld" if outcome.ok else outcome.code if outcome.code in NOTICES else "hub_onbereikbaar")
