"""Statuspagina in Peil via HTTP (echte app, PostgreSQL + pgvector, Hub-simulator): GET / en POST /koppelen.

- foutweergave na een mislukte koppelpoging: vaste code + de Trace-ID van díe poging (dezelfde als in de
  ``X-Trace-Id``-kop en in het auditspoor), selecteerbaar; nooit een Trace-ID uit de URL;
- na het koppelen: neutrale pil "Gekoppeld", geen formulier;
- "Alles gezond" alleen als ook de connector-gezondheid (wat de Hub ziet) gezond is;
- een gewijzigde of ontbrekende sjabloonkopie geeft een eerlijke 500 met code + Trace-ID; de API werkt gewoon door.
"""

from __future__ import annotations

import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select, update

from search_ai import events
from search_ai.models import AuditEvent, OutboxEvent
from tests.conftest import HUB_URL, PAIR_CODE, REPO_ROOT, FakeHub

pytestmark = pytest.mark.integration


def _csrf(client: Any) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text)
    assert m, "geen koppelformulier"
    return m.group(1)


def _totaal(page: str) -> str:
    m = re.search(r'<p class="totaal">(.*?)</p>', page, flags=re.S)
    assert m
    return m.group(1)


def test_get_is_peil_zonder_js_met_strikte_csp(client: Any) -> None:
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "script-src" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Frame-Options"] == "DENY"
    assert "Peil statuspagina.css 1.1.0" in r.text and "<script" not in r.text.lower()
    assert 'name="hub_vingerafdruk"' in r.text and "Niet gereed" in _totaal(r.text)
    assert "pil-gezond" not in _totaal(r.text)  # nog niet gekoppeld → nooit groen
    assert "sai_csrf" in r.headers["set-cookie"] and "samesite=strict" in r.headers["set-cookie"].lower()


def test_mislukte_poging_toont_code_en_trace_id_van_die_poging(client: Any, fake_hub: FakeHub) -> None:
    csrf = _csrf(client)
    r = client.post(
        "/koppelen",
        data={"csrf": csrf, "hub_adres": HUB_URL, "koppelcode": PAIR_CODE, "hub_vingerafdruk": "MEGG-8GFG-2E4Y-K2RQ"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    query = parse_qs(urlsplit(loc).query)
    assert query["melding"] == ["hub_vingerafdruk_onjuist"] and len(query["poging"][0]) >= 22
    trace = r.headers["X-Trace-Id"]
    assert trace not in loc  # de Trace-ID staat niet in de URL
    page = client.get(loc).text
    assert "Code <code>hub_vingerafdruk_onjuist</code> · Trace-ID (selecteer en kopieer)" in page
    assert f'<code class="selecteer">{trace}</code>' in page
    assert 'aria-describedby="vinger-hulp vinger-fout" aria-invalid="true"' in page
    assert HUB_URL not in page  # het ingevulde Hub-adres van een mislukte poging komt nergens terug (§3.7)
    with client.app.state.db.sessions() as s:
        audit = s.scalars(select(AuditEvent).where(AuditEvent.action == "hub.pair_failed")).all()
    assert [str(a.trace_id) for a in audit] == [trace]
    # een verzonnen of andermans poging-sleutel toont geen Trace-ID
    assert trace not in client.get("/?melding=hub_vingerafdruk_onjuist&poging=" + "A" * 22).text
    assert trace not in client.get(f"/?melding=hub_weigerde&poging={query['poging'][0]}").text
    assert trace not in client.get(f"/?melding=hub_vingerafdruk_onjuist&poging={trace}").text


def test_csrf_fout_krijgt_ook_code_en_trace_id(client: Any) -> None:
    r = client.post("/koppelen", data={"csrf": "fout"}, follow_redirects=False)
    page = client.get(r.headers["location"]).text
    assert "Code <code>ongeldig_formulier</code>" in page and r.headers["X-Trace-Id"] in page


def test_na_koppelen_neutraal_zonder_formulier_en_alles_gezond(client: Any, fake_hub: FakeHub) -> None:
    csrf = _csrf(client)
    r = client.post(
        "/koppelen",
        data={"csrf": csrf, "hub_adres": HUB_URL, "koppelcode": PAIR_CODE, "hub_vingerafdruk": fake_hub.fingerprint()},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/?melding=gekoppeld"  # geen foutmelding → geen poging-sleutel
    page = client.get(r.headers["location"]).text
    assert '<div class="melding melding-info"' in page and "<strong>Gekoppeld met de Hub</strong>" in page
    assert 'action="/koppelen"' not in page and 'name="csrf"' not in page
    assert "pil-neutraal" in page and fake_hub.fingerprint() in page
    assert f'<a href="{HUB_URL}">Hub openen</a>' in page
    totaal = _totaal(page)
    assert "Alles gezond" in totaal and "pil-gezond" in totaal


def test_dead_letter_in_de_outbox_maakt_de_pagina_beperkt(paired_client: Any) -> None:
    client = paired_client
    assert "Alles gezond" in _totaal(client.get("/").text)
    with client.app.state.db.sessions() as s:
        events.emit(
            s,
            event_type="service.hub_link.paired",
            entity_type="hub_link",
            entity_id="hub",
            actor_type="user",
            actor_id="test",
            trace_id=str(uuid.uuid4()),
            payload={"via": "koppelcode"},
        )
        s.flush()
        s.execute(update(OutboxEvent).values(delivery_status="dead_letter"))
        s.commit()
    page = client.get("/").text
    totaal = _totaal(page)
    assert "Beperkt" in totaal and "Alles gezond" not in totaal
    assert "Gebeurtenissen naar de Hub" in page and "(code dead_letter)" in page


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
    assert client.get("/readyz").status_code == 200 and client.get("/healthz").status_code == 200


# ------------------------------------------------------------------ QA statuspagina (ADR-0015)
ENROLL = "/api/v1/services/enroll"


def _enroll_calls(fake_hub: FakeHub) -> int:
    return sum(1 for r in fake_hub.all_requests if r["path"] == ENROLL)


def _melding(page: str) -> str | None:
    m = re.search(r'<p id="melding-titel"><strong>([^<]+)</strong>', page)
    return m.group(1) if m else None


@pytest.mark.parametrize(
    "code",
    ["al_gekoppeld", "hub_weigerde", "hub_vingerafdruk_onjuist", "te_veel_pogingen", "koppelstatus_onleesbaar"],
)
def test_qa_foutmelding_uit_een_zelfgemaakte_link_verschijnt_niet(client: Any, code: str) -> None:
    """Een foutmelding hoort bij een echte, recente poging (met haar Trace-ID). Een gedeelde of zelfgemaakte link
    zonder geldige poging-sleutel laat de pagina niets beweren dat niet gebeurd is (bv. 'al eens gekoppeld')."""
    for url in (f"/?melding={code}", f"/?melding={code}&poging={'A' * 22}"):
        page = client.get(url).text
        assert _melding(page) is None and 'class="selecteer"' not in page, url
        assert 'action="/koppelen"' in page  # de dienst is nog nooit gekoppeld: het formulier blijft gewoon staan


@pytest.mark.parametrize("site", ["same-site", "cross-site"])
def test_qa_formulier_van_een_andere_app_op_hetzelfde_nas_adres_wordt_geweigerd(
    client: Any, fake_hub: FakeHub, site: str
) -> None:
    """Andere poort op hetzelfde NAS-adres = zelfde site: SameSite-cookies gaan mee, en met Referrer-Policy
    no-referrer is Origin 'null'. Sec-Fetch-Site verraadt dan de herkomst; zonder geldige herkomst gaat er niets weg."""
    csrf = _csrf(client)
    data = {"csrf": csrf, "hub_adres": HUB_URL, "koppelcode": PAIR_CODE, "hub_vingerafdruk": fake_hub.fingerprint()}
    r = client.post("/koppelen", data=data, headers={"Origin": "null", "Sec-Fetch-Site": site}, follow_redirects=False)
    assert r.status_code == 303 and "melding=ongeldig_formulier" in r.headers["location"]
    assert _enroll_calls(fake_hub) == 0 and client.app.state.hub_link.read()["state"] == "nooit_gekoppeld"
    # dezelfde poging vanaf de eigen pagina (same-origin; Origin null door no-referrer) koppelt wél
    r = client.post(
        "/koppelen", data=data, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"}, follow_redirects=False
    )
    assert r.headers["location"] == "/?melding=gekoppeld" and _enroll_calls(fake_hub) == 1


@pytest.mark.parametrize(
    ("veld", "waarde", "melding"),
    [
        # het begin (32 tekens) is na normalisatie een geldige koppelcode; de hele invoer niet (§3.3: ≤ 32 tekens)
        ("koppelcode", PAIR_CODE + "-" * (32 - len(PAIR_CODE)) + "ZZZZ", "koppelcode_ongeldig"),
        ("hub_vingerafdruk", None, "vingerafdruk_ongeldig"),
        ("hub_adres", HUB_URL + "0" * 10 + "/" + "a" * 500, "ongeldig_hub_adres"),
    ],
)
def test_qa_te_lange_invoer_wordt_nooit_afgekapt_tot_iets_geldigs(
    client: Any, fake_hub: FakeHub, veld: str, waarde: str | None, melding: str
) -> None:
    fp = fake_hub.fingerprint()
    data = {"csrf": _csrf(client), "hub_adres": HUB_URL, "koppelcode": PAIR_CODE, "hub_vingerafdruk": fp}
    data[veld] = waarde if waarde is not None else fp + " " * (64 - len(fp)) + "X"
    r = client.post("/koppelen", data=data, follow_redirects=False)
    assert parse_qs(urlsplit(r.headers["location"]).query)["melding"] == [melding]
    assert _enroll_calls(fake_hub) == 0


def test_qa_poging_op_een_onleesbaar_koppelbestand(client: Any, fake_hub: FakeHub) -> None:
    """Formulier nog open in een oud tabblad, koppelbestand intussen onleesbaar: niets naar de Hub, niets
    overschreven, en de melding zegt wat er is (niet 'al eens gekoppeld', want dat is onbekend)."""
    csrf = _csrf(client)
    store = client.app.state.hub_link
    store.path.write_text("{kapot", encoding="utf-8")
    data = {"csrf": csrf, "hub_adres": HUB_URL, "koppelcode": PAIR_CODE, "hub_vingerafdruk": fake_hub.fingerprint()}
    r = client.post("/koppelen", data=data, follow_redirects=False)
    assert "melding=koppelstatus_onleesbaar" in r.headers["location"] and _enroll_calls(fake_hub) == 0
    page = client.get(r.headers["location"]).text
    assert _melding(page) == "Koppelstatus onleesbaar" and "al eens gekoppeld" not in page
    assert f'<code class="selecteer">{r.headers["X-Trace-Id"]}</code>' in page
    assert 'action="/koppelen"' not in page and "pil-storing" in _totaal(page)
    assert store.path.read_text(encoding="utf-8") == "{kapot"  # nooit stil vervangen


def test_qa_trace_id_van_een_poging_staat_ook_in_de_app_log(
    client: Any, fake_hub: FakeHub, caplog: pytest.LogCaptureFixture
) -> None:
    """CUSTOM_APP.md belooft dat de Trace-ID van de pagina ook in de app-log (TrueNAS → Apps → Logs) staat; het
    ingevulde Hub-adres van een mislukte poging staat er niet in (§3.7)."""
    csrf = _csrf(client)
    caplog.set_level("INFO", logger="search_ai")
    data = {"csrf": csrf, "hub_adres": HUB_URL, "koppelcode": PAIR_CODE, "hub_vingerafdruk": "MEGG-8GFG-2E4Y-K2RQ"}
    r = client.post("/koppelen", data=data, follow_redirects=False)
    trace = r.headers["X-Trace-Id"]
    regels = [rec.getMessage() for rec in caplog.records if trace in rec.getMessage()]
    assert regels == [f"statuspagina: koppelen niet gelukt (code hub_vingerafdruk_onjuist, Trace-ID {trace})"]
    assert not any(HUB_URL in rec.getMessage() for rec in caplog.records)
    r = client.post("/koppelen", data={**data, "hub_vingerafdruk": fake_hub.fingerprint()}, follow_redirects=False)
    assert any(r.headers["X-Trace-Id"] in rec.getMessage() for rec in caplog.records)
