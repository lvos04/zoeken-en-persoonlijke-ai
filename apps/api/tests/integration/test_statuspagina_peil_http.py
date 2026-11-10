"""Statuspagina in Peil 1.1.1 via HTTP (echte app, PostgreSQL + pgvector, Hub-simulator): GET / en POST /koppelen
volgens contract 1.3 §3.7 (C1.3-ZPA-01, C1.3-ZPA-02).

- herkomst: ``Referrer-Policy: same-origin``; ``Origin`` exact ``<schema>://<Host>`` (ontbreekt/``null``/vreemd → 403);
  ``Sec-Fetch-Site`` ``same-site``/``cross-site`` → 403; ``Host``-beperking tegen DNS-rebinding (403
  ``host_niet_toegestaan``); een geweigerd verzoek gaat niet naar de Hub en verandert niets;
- elke POST die de herkomstcontrole doorstaat telt als poging (ook ongeldige invoer, al gekoppeld, koppelen bezig);
- de melding hoort bij dít verzoek: de POST krijgt de pagina met de echte HTTP-status, de vaste code en de Trace-ID
  van díe poging (dezelfde als in ``X-Trace-Id``, het auditspoor en de app-log); ``?melding=`` doet niets;
- 409 ``koppelen_bezig``, 409 ``al_gekoppeld``, 409 ``koppelstatus_onleesbaar``; invoer nooit eerst afkappen;
- precies één logregel per afgehandelde poging, zonder het ingevulde Hub-adres;
- "Gereed"/"Alles gezond" alleen als de koppeling bevestigd is én de connector-gezondheid gezond is;
- een gewijzigde of ontbrekende sjabloonkopie geeft een eerlijke 500 met code + Trace-ID; de API werkt gewoon door.
"""

from __future__ import annotations

import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from search_ai import events
from search_ai.models import AuditEvent, OutboxEvent
from search_ai.worker import Worker
from tests.conftest import HUB_URL, PAIR_CODE, REPO_ROOT, FakeHub

pytestmark = pytest.mark.integration

ENROLL = "/api/v1/services/enroll"
EIGEN = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}  # zoals een browser op de eigen pagina


def _csrf(client: Any) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text)
    assert m, "geen koppelformulier"
    return m.group(1)


def _totaal(page: str) -> str:
    m = re.search(r'<p class="totaal">(.*?)</p>', page, flags=re.S)
    assert m
    return m.group(1)


def _melding(page: str) -> str | None:
    m = re.search(r'<p id="melding-titel"><strong>([^<]+)</strong>', page)
    return m.group(1) if m else None


def _code(page: str) -> str | None:
    m = re.search(r'<p class="meta">Code <code>([a-z_]+)</code></p>', page)
    return m.group(1) if m else None


def _enroll_calls(fake_hub: FakeHub) -> int:
    return sum(1 for r in fake_hub.all_requests if r["path"] == ENROLL)


def _data(client: Any, fake_hub: FakeHub, **over: str) -> dict[str, str]:
    data = {
        "csrf": _csrf(client),
        "hub_adres": HUB_URL,
        "koppelcode": PAIR_CODE,
        "hub_vingerafdruk": fake_hub.fingerprint(),
    }
    data.update(over)
    return data


def _post(client: Any, data: dict[str, str], headers: dict[str, str] | None = None) -> Any:
    return client.post("/koppelen", data=data, headers=EIGEN if headers is None else headers, follow_redirects=False)


# ------------------------------------------------------------------ GET
def test_get_is_peil_1_1_1_zonder_js_met_strikte_kopregels(client: Any) -> None:
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["Referrer-Policy"] == "same-origin"  # §3.7 punt 1: nooit no-referrer
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Frame-Options"] == "DENY"
    assert "Peil statuspagina.css 1.1.1" in r.text and "<script" not in r.text.lower()
    assert re.search(r'<form method="post" action="/koppelen" autocomplete="off" novalidate>', r.text)
    assert 'name="hub_vingerafdruk" required' in r.text and "(verplicht)" in r.text
    assert "Niet gereed" in _totaal(r.text) and "pil-gezond" not in _totaal(r.text)
    assert "sai_csrf" in r.headers["set-cookie"] and "samesite=strict" in r.headers["set-cookie"].lower()
    assert 'role="' not in r.text  # geen melding, dus ook geen live-regio


@pytest.mark.parametrize(
    "code", ["gekoppeld", "al_gekoppeld", "hub_weigerde", "te_veel_pogingen", "koppelstatus_onleesbaar", "x"]
)
def test_melding_uit_de_url_doet_niets(client: Any, code: str) -> None:
    """§3.7: een melding hoort bij een echte poging, nooit bij alleen een query-parameter (ADR-0015 Q-01)."""
    for url in (f"/?melding={code}", f"/?melding={code}&poging={'A' * 22}"):
        page = client.get(url).text
        assert _melding(page) is None and 'class="selecteer"' not in page, url
        assert 'action="/koppelen"' in page


def test_get_via_een_domeinnaam_meldt_dat_koppelen_hier_niet_kan(client: Any) -> None:
    page = client.get("/", headers={"Host": "zoeken.voorbeeld.nl"}).text
    assert _melding(page) == "Koppelen kan niet via dit adres" and 'role="' not in page  # toestand, geen live-regio
    assert "IP-adres van de NAS" in page and 'action="/koppelen"' in page
    assert _melding(client.get("/", headers={"Host": "192.168.1.5:30450"}).text) is None


# ------------------------------------------------------------------ POST: herkomst (§3.7 punten 2–5)
@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({}, "herkomst_geweigerd"),  # geen Origin
        ({"Origin": "null", "Sec-Fetch-Site": "same-origin"}, "herkomst_geweigerd"),
        ({"Origin": "http://andere-app.lan:30420", "Sec-Fetch-Site": "same-origin"}, "herkomst_geweigerd"),
        ({"Origin": "https://testserver", "Sec-Fetch-Site": "same-origin"}, "herkomst_geweigerd"),  # ander schema
        ({"Origin": "http://testserver:30450", "Sec-Fetch-Site": "same-origin"}, "herkomst_geweigerd"),
        ({"Origin": "http://testserver", "Sec-Fetch-Site": "same-site"}, "herkomst_geweigerd"),
        ({"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"}, "herkomst_geweigerd"),
        (
            {"Origin": "http://zoeken.voorbeeld.nl", "Host": "zoeken.voorbeeld.nl", "Sec-Fetch-Site": "same-origin"},
            "host_niet_toegestaan",
        ),
        ({"Origin": "http://10.1.2.3.nip.io:30450", "Host": "10.1.2.3.nip.io:30450"}, "host_niet_toegestaan"),
        ({"Origin": "http://127.1:30450", "Host": "127.1:30450"}, "host_niet_toegestaan"),  # geen canonieke IP-vorm
    ],
)
def test_geweigerde_herkomst_is_403_gaat_niet_naar_de_hub_en_telt_niet(
    client: Any, fake_hub: FakeHub, headers: dict[str, str], code: str
) -> None:
    data = _data(client, fake_hub)
    for _ in range(7):  # meer dan de pogingenlimiet: een geweigerde herkomst telt niet als poging
        r = _post(client, data, headers)
        assert r.status_code == 403 and _code(r.text) == code, r.text[:200]
        assert r.headers["X-Trace-Id"] in r.text and 'role="alert"' in r.text
    assert _enroll_calls(fake_hub) == 0 and client.app.state.hub_link.read()["state"] == "nooit_gekoppeld"
    r = _post(client, data)  # dezelfde invoer vanaf de eigen pagina koppelt wél
    assert r.status_code == 200 and _melding(r.text) == "Gekoppeld met de Hub" and _enroll_calls(fake_hub) == 1


@pytest.mark.parametrize(
    "host", ["192.168.1.5:30450", "nas", "nas:30450", "nas.local", "nas.lan.", "box.home.arpa", "[fd00::5]:30450"]
)
def test_toegestane_hosts_komen_door_de_herkomstcontrole(client: Any, fake_hub: FakeHub, host: str) -> None:
    data = _data(client, fake_hub, koppelcode="KORT")
    r = _post(client, data, {"Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 422 and _code(r.text) == "koppelcode_ongeldig"  # verder dan de herkomst: de invoer


def test_zonder_cookie_of_met_een_ander_formuliertoken_403(client: Any, fake_hub: FakeHub) -> None:
    data = _data(client, fake_hub)
    client.cookies.clear()
    r = _post(client, data)
    assert r.status_code == 403 and _code(r.text) == "ongeldig_formulier" and r.headers["X-Trace-Id"] in r.text
    _csrf(client)
    r = _post(client, {**data, "csrf": "verkeerd-maar-lang-genoeg-123456"})
    assert r.status_code == 403 and _code(r.text) == "ongeldig_formulier" and _enroll_calls(fake_hub) == 0


# ------------------------------------------------------------------ POST: pogingen, toestand en invoer
def test_elke_post_na_de_herkomst_telt_ook_ongeldige_invoer(client: Any, fake_hub: FakeHub) -> None:
    data = _data(client, fake_hub)
    for _ in range(5):
        assert _post(client, {**data, "koppelcode": "KORT"}).status_code == 422
    r = _post(client, data)  # zesde poging, nu met geldige invoer: toch te veel
    assert r.status_code == 429 and _code(r.text) == "te_veel_pogingen" and r.headers["Retry-After"] == "900"
    assert _enroll_calls(fake_hub) == 0


def test_al_gekoppeld_is_409_en_telt_mee(paired_client: Any, fake_hub: FakeHub) -> None:
    client = paired_client
    client.get("/")
    csrf = client.cookies.get("sai_csrf")
    data = {"csrf": csrf, "hub_adres": HUB_URL, "koppelcode": PAIR_CODE, "hub_vingerafdruk": fake_hub.fingerprint()}
    calls = _enroll_calls(fake_hub)
    for _ in range(5):
        r = _post(client, data)
        assert r.status_code == 409 and _code(r.text) == "al_gekoppeld" and 'action="/koppelen"' not in r.text
    assert _post(client, data).status_code == 429  # RV7: ook een al gekoppelde dienst telt de pogingen
    assert _enroll_calls(fake_hub) == calls


def test_koppelen_bezig_is_409_en_gaat_niet_naar_de_hub(client: Any, fake_hub: FakeHub) -> None:
    data = _data(client, fake_hub)
    client.app.state.hub_link.update(
        lambda d: {
            **d,
            "pending": {
                "kind": "token",
                "secret_sha256": "a" * 64,
                "hub_url": HUB_URL,
                "started_at": "2026-11-10T08:00:00Z",
                "attempts": 1,
            },
        }
    )
    page = client.get("/").text
    assert "Koppelen bezig" in page and 'action="/koppelen"' not in page  # Peil 1.1.1 E02: geen formulier
    r = _post(client, data)
    assert r.status_code == 409 and _code(r.text) == "koppelen_bezig" and _enroll_calls(fake_hub) == 0


def test_poging_op_een_onleesbaar_koppelbestand_409_en_niets_vervangen(client: Any, fake_hub: FakeHub) -> None:
    data = _data(client, fake_hub)
    store = client.app.state.hub_link
    store.path.write_text("{kapot", encoding="utf-8")
    store.path.chmod(0o600)
    page = client.get("/").text
    assert _melding(page) == "Koppelstatus onleesbaar" and 'role="' not in page  # blijvende toestand: geen rol
    assert "<code>/appdata/secrets/hub-link.json</code>" in page and _code(page) == "koppelstatus_onleesbaar"
    assert 'class="selecteer"' not in page and 'action="/koppelen"' not in page
    r = _post(client, data)
    assert r.status_code == 409 and _code(r.text) == "koppelstatus_onleesbaar" and 'role="alert"' in r.text
    assert r.headers["X-Trace-Id"] in r.text and "al eens gekoppeld" not in r.text
    assert _enroll_calls(fake_hub) == 0 and store.path.read_text(encoding="utf-8") == "{kapot"


@pytest.mark.parametrize(
    ("veld", "waarde", "melding"),
    [
        # het begin (32 tekens) is na normalisatie een geldige koppelcode; de hele invoer niet (§3.3: ≤ 32 tekens)
        ("koppelcode", PAIR_CODE + "-" * (32 - len(PAIR_CODE)) + "ZZZZ", "koppelcode_ongeldig"),
        ("hub_vingerafdruk", None, "vingerafdruk_ongeldig"),
        ("hub_adres", HUB_URL + "/" + "a" * 520, "hub_adres_ongeldig"),  # > 512 tekens
        ("hub_adres", "http://127.1:30400", "hub_adres_ongeldig"),  # geen canonieke IP-vorm
    ],
)
def test_te_lange_of_ongeldige_invoer_is_422_en_nooit_afgekapt(
    client: Any, fake_hub: FakeHub, veld: str, waarde: str | None, melding: str
) -> None:
    fp = fake_hub.fingerprint()
    data = _data(client, fake_hub)
    data[veld] = waarde if waarde is not None else fp + " " * (64 - len(fp)) + "X"
    r = _post(client, data)
    assert r.status_code == 422 and _code(r.text) == melding and 'aria-invalid="true"' in r.text
    assert _enroll_calls(fake_hub) == 0


# ------------------------------------------------------------------ POST: uitkomst van de Hub
def test_mislukte_poging_toont_code_en_trace_id_van_die_poging(client: Any, fake_hub: FakeHub) -> None:
    r = _post(client, _data(client, fake_hub, hub_vingerafdruk="MEGG-8GFG-2E4Y-K2RQ"))
    assert r.status_code == 422
    trace = r.headers["X-Trace-Id"]
    page = r.text
    assert '<div class="melding melding-fout" role="alert" aria-labelledby="melding-titel"' in page
    assert _code(page) == "hub_vingerafdruk_onjuist" and f'<code class="selecteer">{trace}</code>' in page
    assert 'aria-describedby="vinger-hulp vinger-fout" aria-invalid="true"' in page
    assert HUB_URL not in page  # het ingevulde Hub-adres van een mislukte poging komt nergens terug (§3.7)
    with client.app.state.db.sessions() as s:
        audit = s.scalars(select(AuditEvent).where(AuditEvent.action == "hub.pair_failed")).all()
    assert [str(a.trace_id) for a in audit] == [trace]
    assert trace not in client.get("/").text  # een volgende GET toont de stand van nu, niet de oude poging


def test_na_koppelen_neutraal_zonder_formulier_en_pas_alles_gezond_met_hartslag(client: Any, fake_hub: FakeHub) -> None:
    r = _post(client, _data(client, fake_hub))
    assert r.status_code == 200
    page = r.text
    assert '<div class="melding melding-info" role="status" aria-labelledby="melding-titel"' in page
    assert "<strong>Gekoppeld met de Hub</strong>" in page and 'class="selecteer"' not in page
    assert 'action="/koppelen"' not in page and 'name="csrf"' not in page
    assert "pil-neutraal" in page and fake_hub.fingerprint() in page
    assert f'<a class="knop knop-secundair" href="{HUB_URL}">Hub openen</a>' in page
    # zonder hartslag van de worker is de connector-gezondheid onbekend: nooit "Alles gezond"
    assert "Alles gezond" not in _totaal(client.get("/").text)
    Worker(client.app.state.settings).heartbeat(0)
    totaal = _totaal(client.get("/").text)
    assert "Alles gezond" in totaal and "pil-gezond" in totaal


def test_gekoppeld_maar_niet_bevestigd_is_niet_gereed(client: Any, fake_hub: FakeHub) -> None:
    fake_hub.override = lambda r: (
        __import__("httpx").Response(503, json={}) if r.url.path == "/api/v1/services/self" else None
    )
    r = _post(client, _data(client, fake_hub))
    assert r.status_code == 200 and _melding(r.text) == "Gekoppeld, nog niet bevestigd"
    Worker(client.app.state.settings).heartbeat(0)
    page = client.get("/").text
    assert "Niet gereed" in _totaal(page) and "Nee — de Hub heeft de koppeling nog niet bevestigd" in page


def test_een_logregel_per_poging_zonder_hub_adres(
    client: Any, fake_hub: FakeHub, caplog: pytest.LogCaptureFixture
) -> None:
    """§3.7: elke afgehandelde poging geeft één regel in de app-log met code en Trace-ID, zonder het ingevulde Hub-adres
    van een mislukte poging en zonder geheimen."""
    caplog.set_level("INFO", logger="search_ai")
    data = _data(client, fake_hub, hub_vingerafdruk="MEGG-8GFG-2E4Y-K2RQ")
    for headers, verwacht in ((EIGEN, "hub_vingerafdruk_onjuist"), ({}, "herkomst_geweigerd")):
        caplog.clear()
        r = _post(client, data, headers)
        trace = r.headers["X-Trace-Id"]
        regels = [rec.getMessage() for rec in caplog.records if rec.name.startswith("search_ai")]
        assert regels == [
            f"statuspagina: koppelpoging afgehandeld (code {verwacht}, HTTP {r.status_code}, Trace-ID {trace})"
        ]
        assert not any(HUB_URL in m or PAIR_CODE in m for m in regels)
    caplog.clear()
    r = _post(client, {**data, "hub_vingerafdruk": fake_hub.fingerprint()})
    regels = [rec.getMessage() for rec in caplog.records if rec.name.startswith("search_ai")]
    assert len(regels) == 1 and r.headers["X-Trace-Id"] in regels[0] and "code gekoppeld" in regels[0]


def test_dead_letter_in_de_outbox_maakt_de_pagina_beperkt(paired_client: Any) -> None:
    client = paired_client
    Worker(client.app.state.settings).heartbeat(0)
    assert "Alles gezond" in _totaal(client.get("/").text)
    with client.app.state.db.sessions() as s:
        events.emit_service(
            s, "service.hub_link.linked", actor_type="user", actor_id="test", trace_id=str(uuid.uuid4()), payload={}
        )
        s.flush()
        s.execute(update(OutboxEvent).values(delivery_status="dead_letter"))
        s.commit()
    page = client.get("/").text
    totaal = _totaal(page)
    assert "Beperkt" in totaal and "Alles gezond" not in totaal
    assert "Gebeurtenissen naar de Hub" in page and "(code <code>dead_letter</code>)" in page


def test_gewijzigde_kopie_geeft_eerlijke_500_en_de_api_werkt_door(
    app_factory: Callable[..., Any], tmp_path: Path
) -> None:
    peil = tmp_path / "peil"
    shutil.copytree(REPO_ROOT / "design" / "ontwerptaal", peil)
    raw = (peil / "statuspagina.html").read_bytes()
    (peil / "statuspagina.html").write_bytes(raw.replace(b"<h1>{{dienst_naam}}</h1>", b"<h1>HANDWERK</h1>"))
    client = app_factory(peil_dir=peil)
    r = client.get("/")
    assert r.status_code == 500 and "peil_sjabloon_ongeldig" in r.text and "HANDWERK" not in r.text
    assert str(uuid.UUID(r.headers["X-Trace-Id"])) in r.text
    # ontwerpreview: licht/donker via systeemkleuren, selecteerbare Trace-ID, wat je kunt doen
    assert '<meta name="color-scheme" content="light dark">' in r.text and 'class="selecteer"' in r.text
    assert "Werk de app bij naar een andere image-versie" in r.text and r.text.startswith(
        '<!doctype html>\n<html lang="nl">'
    )
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Referrer-Policy"] == "same-origin"
    assert client.get("/readyz").status_code == 200 and client.get("/healthz").status_code == 200
