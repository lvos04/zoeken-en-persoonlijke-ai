"""Statuspagina in Peil 1.1.1 (ONTWERPTAAL §13.2) volgens contract 1.3 §3.7 — rendering per toestand, zonder database.

Toestanden komen uit ``tests/ui/render_toestanden.py`` (dezelfde invoer als de browsercontrole en de schermafdrukken):
niet gekoppeld · koppelen mislukt · gekoppeld · ingetrokken · niet gereed (+ gekoppeld-beperkt, ontkoppeld,
koppelstatus onleesbaar, koppelen bezig, herkomst geweigerd, adres zonder koppelen, worker verouderd, shares beperkt).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from search_ai.peil import PeilTemplateError, load_template, parse, render, split_copy, strip_comments
from search_ai.status_page import NOTICES, PageInput, host_allowed, page_data, render_page
from tests.conftest import REPO_ROOT

OK = [{"name": n, "status": "ok"} for n in ("database", "migraties", "pgvector", "geheimen")]
NOW = datetime(2026, 11, 9, 10, 56, tzinfo=UTC)


@cache
def _fixtures() -> ModuleType:
    spec = importlib.util.spec_from_file_location("render_toestanden", REPO_ROOT / "tests/ui/render_toestanden.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["render_toestanden"] = mod
    spec.loader.exec_module(mod)
    return mod


def _page(name: str) -> str:
    inp, _ = _fixtures().toestanden()[name]
    return render_page(load_template(), inp)


def _inp(**kw: Any) -> PageInput:
    base: dict[str, Any] = {
        "ready": True,
        "checks": OK,
        "doc": {"state": "nooit_gekoppeld", "pending": None, "last_error": None},
        "per_status": {},
        "roots": 1,
        "root_warnings": 0,
        "csrf": "c" * 32,
        "now": NOW,
    }
    base.update(kw)
    return PageInput(**base)


def _totaal(page: str) -> str:
    m = re.search(r'<p class="totaal">(.*?)</p>', page, flags=re.S)
    assert m, "geen totaalregel"
    return m.group(1)


ALLE = list(_fixtures().toestanden())


# ------------------------------------------------------------------ voor elke toestand
@pytest.mark.parametrize("name", ALLE)
def test_geen_js_geen_externe_bron_alles_ingevuld(name: str) -> None:
    page = _page(name)
    assert page.startswith('<!doctype html>\n<html lang="nl">')
    assert "<script" not in page.lower() and not re.search(r"\son[a-z]+\s*=", page, flags=re.I)
    assert "{{" not in page and "<!--" not in page  # alle plekken gevuld, geen uitleg-commentaar naar de browser
    assert not re.search(r"\s(src|srcset|action|formaction)=\"(?!/koppelen\")", page)
    hrefs = set(re.findall(r'href="([^"]*)"', page))
    toegestaan = {"/healthz", "/readyz", _fixtures().HUB}
    assert all(h.startswith("#") or h in toegestaan for h in hrefs), hrefs
    assert page.count("<h1>") == 1 and "Peil statuspagina.css 1.1.1" in page
    assert "ontkoppelcode" not in page.lower() and 'action="/ontkoppelen"' not in page
    for token in ("service_token", "svc_", "secret_sha256", "AbCd"):  # alleen de witte lijst van §3.5
        assert token not in page


@pytest.mark.parametrize("name", ALLE)
def test_koppelformulier_alleen_als_nooit_gekoppeld(name: str) -> None:
    page = _page(name)
    inp, _ = _fixtures().toestanden()[name]
    form = (
        inp.doc.get("state") == "nooit_gekoppeld"
        and not inp.doc.get("first_linked_at")
        and not inp.doc.get("corrupt")
        and not inp.doc.get("pending")  # Peil 1.1.1 E02: koppelen bezig verbergt het formulier
    )
    assert ('action="/koppelen"' in page) is form
    assert ('name="csrf"' in page) is form
    if form:
        for field in ("hub_adres", "koppelcode", "hub_vingerafdruk"):  # drie verplichte velden (§3.7)
            assert re.search(rf'<input [^>]*name="{field}"[^>]*\srequired', page), field
        assert "Vingerafdruk van de Hub" in page and "Geldig 60 minuten" in page
        assert " novalidate>" in page and page.count("(verplicht)") == 3  # Peil 1.1.1 E09
    else:  # de pagina zegt hoe het verder gaat: via de Hub, met een nieuw koppeltoken, of wachten op de poging
        assert "via de Hub" in page or "nieuw koppeltoken" in page or "Koppelen bezig" in page


# ------------------------------------------------------------------ per toestand
def test_niet_gekoppeld() -> None:
    page = _page("niet-gekoppeld")
    assert "<title>Zoeken en persoonlijke AI — niet gereed, niet gekoppeld</title>" in page
    totaal = _totaal(page)
    assert "pil-onbekend" in totaal and "Niet gereed" in totaal and "wacht op koppeling met de Hub" in totaal
    assert "pil-gezond" not in totaal  # eerste signaal nooit groen als niet gereed
    assert 'class="melding' not in page and 'role="' not in page
    # Peil 1.1.1 E04: "Niet gekoppeld" overal met link-2-off, ook in de controles
    assert page.count('<use href="#i-niet-gekoppeld"/></svg>Niet gekoppeld</span>') == 2
    assert '<span class="pil pil-niet-gekoppeld">' in page


def test_koppelen_mislukt_toont_code_en_kopieerbare_trace_id() -> None:
    page = _page("koppelen-mislukt")
    trace = _fixtures().TRACE
    assert '<div class="melding melding-fout" role="alert" aria-labelledby="melding-titel"' in page
    assert "<strong>Koppelen is mislukt</strong>" in page
    assert '<p class="meta">Code <code>hub_weigerde</code></p>' in page  # E06: code los van de trace-ID
    assert f'Trace-ID (selecteer en kopieer)</p><code class="selecteer">{trace}</code>' in page
    # veldfout bij de koppelcode, gekoppeld via aria-describedby; skiplink en melding wijzen naar het veld
    assert 'aria-describedby="code-hulp code-fout" aria-invalid="true"' in page
    assert '<p class="veldfout" id="code-fout">' in page and "verlopen, al gebruikt of ongeldig" in page
    assert (
        '<a class="spring" href="#koppelcode">' in page and '<a href="#koppelcode">Naar het veld Koppelcode</a>' in page
    )
    assert "<title>Zoeken en persoonlijke AI — Koppelen is mislukt · " in page
    assert 'action="/koppelen"' in page  # formulier blijft: nog nooit gekoppeld
    assert "pil-gezond" not in _totaal(page)


def test_gekoppeld_is_neutraal_nooit_groen_en_zonder_formulier() -> None:
    page = _page("gekoppeld")
    assert (
        '<span class="pil pil-neutraal"><svg aria-hidden="true"><use href="#i-gekoppeld"/></svg>Gekoppeld</span>'
        in page
    )
    hub = _fixtures().HUB
    assert (
        f"<code>{_fixtures().FP}</code>" in page
        and f'<a class="knop knop-secundair" href="{hub}">Hub openen</a>' in page
    )
    assert "Ontkoppelen of opnieuw koppelen gaat via de Hub." in page
    assert not re.search(r"pil-gezond[^<]*<svg[^>]*><use[^>]*/></svg>Gekoppeld", page)
    # alles gemeten en goed (ook bevestigd en de connector-gezondheid): dan pas groen
    assert "Alles gezond" in _totaal(page) and "pil-gezond" in _totaal(page)
    assert "2 actief, 1 gepauzeerd" in page
    # bron van het totaal en de uitleg van Gereed (Peil 1.1.1, contract 1.3 §3.7)
    assert "connector-gezondheid (wat de Hub ziet)" in page and "de Hub heeft de koppeling bevestigd" in page


def test_melding_na_koppelen_heeft_rol_status() -> None:
    page = _page("gekoppeld-melding")
    assert '<div class="melding melding-info" role="status" aria-labelledby="melding-titel"' in page
    assert 'class="selecteer"' not in page  # geen trace-ID bij een info-melding


def test_gekoppeld_maar_beperkt_is_nooit_alles_gezond() -> None:
    page = _page("gekoppeld-beperkt")
    totaal = _totaal(page)
    assert "Alles gezond" not in totaal and "Niet gereed" in totaal  # niet bevestigd = niet gereed (§3.7)
    assert "pil-beperkt" in totaal
    assert "nog niet bevestigd" in page and "(code <code>niet_bevestigd</code>)" in page
    assert "(code <code>dead_letter</code>)" in page


def test_ingetrokken() -> None:
    page = _page("ingetrokken")
    assert '<use href="#i-niet-gekoppeld"/></svg>Ingetrokken</span>' in page
    assert "De Hub heeft de koppeling ingetrokken" in page and "Opnieuw koppelen gaat via de Hub" in page
    totaal = _totaal(page)
    assert "Niet gereed" in totaal and "pil-gezond" not in totaal
    assert "Gekoppeld</span>" not in page


def test_ontkoppeld() -> None:
    page = _page("ontkoppeld")
    assert '<use href="#i-niet-gekoppeld"/></svg>Ontkoppeld</span>' in page
    assert "Niet gereed" in _totaal(page)


def test_niet_gereed_is_storing_en_nooit_groen() -> None:
    page = _page("niet-gereed")
    totaal = _totaal(page)
    assert "pil-storing" in totaal and "Niet gereed" in totaal and "Storing in: Eigen database." in totaal
    assert "Niet te controleren zonder database." in page  # migraties/pgvector: onbekend, niet groen
    assert page.count("Niet te controleren zonder database.") == 2
    assert "Aantal bronnen onbekend (database niet bereikbaar)" in page
    assert "Nee — /readyz meldt een storing" in page


def test_koppelstatus_onleesbaar_is_storing_zonder_formulier_en_zonder_rol() -> None:
    """Peil 1.1.1 E01/E05/E06: eigen koppelblok (pil Storing "Onleesbaar"), melding zonder live-rol met code en pad,
    geen trace-ID en geen formulier."""
    page = _page("koppelstatus-onleesbaar")
    assert (
        '<span class="pil pil-storing"><svg aria-hidden="true"><use href="#i-storing"/></svg>Onleesbaar</span>' in page
    )
    assert "Hier staat bewust geen formulier" in page and 'action="/koppelen"' not in page
    assert '<div class="melding melding-fout" style=' in page and 'role="' not in page
    assert "<p><code>/appdata/secrets/hub-link.json</code></p>" in page
    assert '<p class="meta">Code <code>koppelstatus_onleesbaar</code></p>' in page and 'class="selecteer"' not in page
    assert "pil-storing" in _totaal(page)
    assert "2 genegeerd (zie de app-log)" in page


def test_koppelen_op_onleesbaar_is_een_alert_met_trace_id() -> None:
    page = _page("koppelen-op-onleesbaar")
    assert 'role="alert"' in page and _fixtures().TRACE in page and "al eens gekoppeld" not in page


def test_koppelen_bezig_verbergt_het_formulier_en_noemt_de_starttijd() -> None:
    page = _page("koppelen-bezig")
    assert '<use href="#i-nog-niet"/></svg>Koppelen bezig</span>' in page
    assert re.search(r'gestart om <time datetime="2026-11-09T\d\d:54:00[^"]*">\d\d:54(?: UTC)?</time>', page)
    assert 'action="/koppelen"' not in page and "Maak in de Hub een koppelcode" not in page
    assert "Niet gereed" in _totaal(page) and "er loopt een koppelpoging" in _totaal(page)
    assert "<title>Zoeken en persoonlijke AI — niet gereed, koppelen bezig</title>" in page


def test_herkomst_geweigerd_en_adres_zonder_koppelen() -> None:
    page = _page("herkomst-geweigerd")
    assert 'role="alert"' in page and "<code>herkomst_geweigerd</code>" in page
    page = _page("adres-zonder-koppelen")
    assert "<strong>Koppelen kan niet via dit adres</strong>" in page and 'role="' not in page
    assert 'action="/koppelen"' in page


def test_verouderde_worker_is_laatst_bekend_en_nooit_alles_gezond() -> None:
    page = _page("worker-verouderd")
    assert (
        '<span class="pil pil-verouderd"><svg aria-hidden="true"><use href="#i-verouderd"/></svg>Verouderd</span>'
        in page
    )
    assert re.search(r'<time datetime="2026-11-09T\d\d:41:00[^"]*">\d\d:41(?: UTC)?</time>', page)  # tijd van de meting
    assert "(code <code>meting_verouderd</code>)" in page
    totaal = _totaal(page)
    assert "Deels onbekend" in totaal and "Alles gezond" not in totaal


def test_beperkt_dienstblok_maakt_het_totaal_hooguit_beperkt() -> None:
    """Peil 1.1.1 E10: een verse Beperkt/Storing in het dienstblok maakt het totaal hooguit Beperkt."""
    page = _page("shares-beperkt")
    totaal = _totaal(page)
    assert "pil-beperkt" in totaal and "Gereed, maar beperkt" in totaal
    storing = page_data(_inp(doc=_fixtures()._linked(), root_warnings=3, roots=0))
    assert storing["totaal_toon"] == "beperkt"


def test_dienstblok_tonen_volgen_peil_1_1_1() -> None:
    data = page_data(_inp(doc=_fixtures()._linked(), roots=0))
    rijen = {r["naam"]: r for r in data["dienst_blok"]}
    assert rijen["Bronnen"]["toon"] == "gate"
    assert (rijen["Gekoppelde shares"]["toon"], rijen["Gekoppelde shares"]["label"]) == ("gate", "Niet ingesteld")
    for naam in ("Indexeren en semantisch zoeken", "Spraak en persoonlijke AI"):
        assert (rijen[naam]["toon"], rijen[naam]["reden_code"]) == ("nog-niet", "capability_unavailable")
    # niet ingesteld en nog niet beschikbaar tellen niet mee, maar staan wel in de telling
    assert data["totaal_label"] == "Alles gezond"
    assert data["totaal_toelichting"] == (
        "Gereed en gekoppeld met de Hub. 2 functies zijn nog niet beschikbaar en 1 instelling staat nog open."
    )


# ------------------------------------------------------------------ eerlijk en veilig
@pytest.mark.parametrize("state", ["nooit_gekoppeld", "ontkoppeld", "ingetrokken", "gekoppeld"])
@pytest.mark.parametrize(
    "checks",
    [
        [{"name": "database", "status": "fout"}, {"name": "geheimen", "status": "ok"}],
        [*OK[:3], {"name": "geheimen", "status": "fout"}],
        [{"name": "database", "status": "ok"}, {"name": "geheimen", "status": "ok"}],  # schema/pgvector niet gemeten
    ],
)
def test_eerste_signaal_nooit_groen_als_niet_gereed(state: str, checks: list[dict[str, str]]) -> None:
    data = page_data(_inp(ready=False, checks=checks, doc={"state": state, "first_linked_at": "x"}))
    assert data["totaal_toon"] != "gezond" and data["totaal_label"] == "Niet gereed"
    assert data["gereed"].startswith("Nee")


@pytest.mark.parametrize("state", ["nooit_gekoppeld", "ontkoppeld", "ingetrokken"])
def test_niet_gekoppeld_is_nooit_groen(state: str) -> None:
    data = page_data(_inp(doc={"state": state}))
    assert data["totaal_toon"] == "onbekend" and data["totaal_label"] == "Niet gereed"


def test_gekoppeld_zonder_bevestiging_is_niet_gereed() -> None:
    data = page_data(_inp(doc=_fixtures()._linked(confirmed_at=None)))
    assert data["totaal_label"] == "Niet gereed" and data["totaal_toon"] == "onbekend"
    assert data["gereed"].startswith("Nee — de Hub heeft de koppeling nog niet bevestigd")


def test_onbekende_melding_wordt_niet_weerspiegeld() -> None:
    data = page_data(_inp(melding="<script>alert(1)</script>"))
    assert "melding" not in data and "trace_id" not in data


def test_meldingsrol_alleen_bij_dit_verzoek() -> None:
    """Peil 1.1.1 E05: alert = fout van dít verzoek, status = info na dít verzoek, leeg = bestaande toestand."""
    assert page_data(_inp(melding="te_veel_pogingen", trace_id="t-1"))["melding_rol"] == "alert"
    assert page_data(_inp(doc=_fixtures()._linked(), melding="gekoppeld"))["melding_rol"] == "status"
    assert page_data(_inp(doc={"state": "nooit_gekoppeld", "corrupt": True}))["melding_rol"] == ""
    assert page_data(_inp(host_blocked=True))["melding_rol"] == ""


def test_trace_id_alleen_bij_een_foutmelding_van_dit_verzoek() -> None:
    assert "trace_id" not in page_data(_inp(doc=_fixtures()._linked(), melding="gekoppeld", trace_id="t"))
    data = page_data(_inp(melding="te_veel_pogingen", trace_id="t-1"))
    assert data["trace_id"] == "t-1" and data["melding_code"] == "te_veel_pogingen"
    zonder = page_data(_inp(doc={"state": "nooit_gekoppeld", "corrupt": True}))
    assert "trace_id" not in zonder and zonder["melding_code"] == "koppelstatus_onleesbaar"


def test_hub_adres_wordt_geescaped_en_alleen_http_s_is_een_link() -> None:
    tpl = load_template()
    doc = {
        "state": "gekoppeld",
        "hub_url": 'http://nas"><script>x</script>:30400',
        "linked_at": "2026-11-08T19:14:00+00:00",
        "confirmed_at": "2026-11-08T19:14:01+00:00",
        "pins": {"keyset_fingerprint": "MEGG-8GFG-2E4Y-K2RQ"},
    }
    page = render_page(tpl, _inp(doc=doc))
    assert "<script>" not in page and "&lt;script&gt;" in page
    page = render_page(tpl, _inp(doc={**doc, "hub_url": "javascript:alert(1)"}))
    assert "javascript:" not in page


@pytest.mark.parametrize("code", sorted(NOTICES))
def test_elke_melding_rendert_met_vaste_tekst_en_status(code: str) -> None:
    notice = NOTICES[code]
    doc = _fixtures()._linked() if notice.toon == "info" else {"state": "nooit_gekoppeld"}
    page = render_page(load_template(), _inp(doc=doc, melding=code, trace_id="0f0e0d0c-0000-4000-8000-000000000000"))
    assert f"<strong>{notice.titel}</strong>" in page
    assert ("0f0e0d0c-0000-4000-8000-000000000000" in page) is (notice.toon == "fout")
    assert notice.http in (200, 403, 409, 422, 429, 503)
    if notice.toon == "info":
        assert notice.http == 200
    if notice.veld:
        veld = {"hub": "hub-fout", "koppelcode": "code-fout", "vingerafdruk": "vinger-fout"}[notice.veld]
        assert f'id="{veld}"' in page


def test_http_status_per_melding_volgt_contract_1_3() -> None:
    assert NOTICES["koppelen_bezig"].http == 409 and NOTICES["al_gekoppeld"].http == 409
    assert NOTICES["herkomst_geweigerd"].http == 403 and NOTICES["host_niet_toegestaan"].http == 403
    assert NOTICES["te_veel_pogingen"].http == 429 and NOTICES["koppelstatus_onleesbaar"].http == 409


def test_tijd_in_nederlandse_notatie() -> None:
    data = page_data(_inp())
    assert data["bijgewerkt"] == "9 nov 2026, 10:56 UTC" or re.fullmatch(r"9 nov 2026, \d\d:\d\d", data["bijgewerkt"])
    assert data["checks"][0]["iso"].startswith("2026-11-09T")


# ------------------------------------------------------------------ Host-beperking (§3.7 punt 4)
@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("192.168.1.5", True),
        ("192.168.1.5:30450", True),
        ("[fd00::5]:30450", True),
        ("[fd00::5]", True),
        ("nas", True),
        ("nas:30450", True),
        ("NAS.LAN", True),
        ("nas.local.", True),
        ("box.home.arpa", True),
        ("svc.internal:1", True),
        ("testserver", True),
        ("zoeken.voorbeeld.nl", False),
        ("10.1.2.3.nip.io", False),
        ("local", True),
        ("evil.local.example.com", False),
        ("127.1", False),
        ("0x7f000001", False),
        ("fd00::5", False),  # IPv6 zonder [ ]
        ("[nas]", False),
        ("nas:abc", False),
        ("", False),
        ("na s", False),
    ],
)
def test_host_beperking(host: str, ok: bool) -> None:
    assert host_allowed(host) is ok


# ------------------------------------------------------------------ ontwerpreview (docs/ontwerp/PEIL_REVIEW.md)
WOORDEN_BUITEN_PEIL = re.compile(r"\b(in orde|actueel|verminderd|verstoord|let op|gelukt)\b|!", re.I)


@pytest.mark.parametrize("name", ALLE)
def test_review_geen_interne_ids_en_alleen_peil_woorden(name: str) -> None:
    """Geen taak-ID's (B07, B15) en geen woorden die Peil vervangen heeft (§11.1), in geen enkele toestand."""
    page = _page(name)
    tekst = re.sub(r"<style>.*?</style>|<[^>]+>", " ", page, flags=re.S)
    assert not re.search(r"\bB\d\d\b", tekst), re.findall(r"\bB\d\d\b", tekst)
    assert not WOORDEN_BUITEN_PEIL.search(tekst), WOORDEN_BUITEN_PEIL.findall(tekst)
    assert "Leeswortels" not in tekst and "Gekoppelde shares" in tekst


@pytest.mark.parametrize(
    ("doc", "verwacht"),
    [
        ({"state": "nooit_gekoppeld"}, "Nee — wacht op koppeling met de Hub"),
        ({"state": "ontkoppeld", "first_linked_at": "x"}, "Nee — ontkoppeld door de Hub"),
        ({"state": "ingetrokken", "first_linked_at": "x"}, "Nee — de Hub heeft de koppeling ingetrokken"),
        ({"state": "ontkoppeld", "corrupt": True}, "Nee — de koppelstatus is onleesbaar"),
        ({"state": "nooit_gekoppeld", "unreadable_replaced_at": "t"}, "Nee — de koppelstatus is onleesbaar"),
    ],
)
def test_review_gereed_noemt_de_echte_reden(doc: dict[str, Any], verwacht: str) -> None:
    gereed = page_data(_inp(doc=doc))["gereed"]
    assert gereed.startswith(verwacht) and gereed.endswith("(de eigen controles van /readyz zijn gezond)")


def test_na_vervangen_onleesbaar_bestand_blijft_de_pagina_onleesbaar_tot_een_koppeling() -> None:
    """§3.5 (RV6): zolang ``first_linked_at`` leeg is na het vervangen: geen formulier, geen vertrouwen."""
    data = page_data(_inp(doc={"state": "nooit_gekoppeld", "unreadable_replaced_at": "2026-11-10T08:00:00Z"}))
    assert data["koppelstatus_onleesbaar"] and not data["koppelformulier"]


def test_review_hub_weigerde_noemt_de_koppelcode_en_herhaalt_niet() -> None:
    doc = {"state": "nooit_gekoppeld", "last_error": {"code": "hub_weigerde"}}  # tekst komt uit de vaste lijst
    data = page_data(_inp(doc=doc, melding="hub_weigerde", trace_id="t-1"))
    assert (
        data["melding_tekst"].startswith("De Hub weigerde de koppelcode") and "koppeltoken" not in data["melding_tekst"]
    )
    hub = next(r for r in data["checks"] if r["naam"] == "Koppeling met de Hub")
    assert "Laatste melding" not in hub["reden"]  # staat al in de melding bovenaan
    # zonder melding (bv. later opnieuw geladen) blijft de laatste melding wél bij de controle staan
    hub = next(r for r in page_data(_inp(doc=doc))["checks"] if r["naam"] == "Koppeling met de Hub")
    assert "Laatste melding: De Hub weigerde" in hub["reden"] and hub["reden_code"] == "hub_weigerde"


def test_review_paginatitel_herhaalt_niets() -> None:
    titel = page_data(_inp(doc={"state": "ontkoppeld", "corrupt": True}))["titel_toestand"]
    assert titel == "niet gereed, koppelstatus onleesbaar"
    titel = page_data(_inp(melding="hub_weigerde", trace_id="t"))["titel_toestand"]
    assert titel == "Koppelen is mislukt · niet gereed, niet gekoppeld"


@pytest.mark.parametrize(
    ("doc", "eind"),
    [
        ({"state": "ontkoppeld", "first_linked_at": "x"}, "ontkoppeld door de Hub."),
        ({"state": "ingetrokken", "first_linked_at": "x"}, "de Hub heeft de koppeling ingetrokken."),
        ({"state": "nooit_gekoppeld"}, "wacht op koppeling met de Hub."),
    ],
)
def test_qa_toelichting_bij_niet_gereed_volgt_de_toon(doc: dict[str, Any], eind: str) -> None:
    """Ontkoppeld met vastgelopen gebeurtenissen: pil 'beperkt', dus ook de toelichting 'Beperkt', niet 'Deels onbekend'."""
    vast = [{"name": "jobs", "status": "degraded", "reason_code": "dead_letter"}]
    data = page_data(_inp(doc=doc, health=vast))
    assert (data["totaal_toon"], data["totaal_label"]) == ("beperkt", "Niet gereed")
    assert data["totaal_toelichting"] == f"Beperkt — {eind}"
    data = page_data(_inp(doc=doc))
    assert (data["totaal_toon"], data["totaal_toelichting"]) == ("onbekend", f"Deels onbekend — {eind}")


def test_qa_onbekende_koppeltoestand_is_nooit_gereed_en_gekoppeld() -> None:
    data = page_data(_inp(doc={"state": "iets_anders", "first_linked_at": "x"}))
    assert data["totaal_label"] == "Niet gereed" and data["totaal_toon"] == "onbekend"
    # vangnet: nooit "Gereed en gekoppeld" of "Gereed; …" bij een toestand die de dienst niet kent
    assert data["totaal_toelichting"] == "Deels onbekend — koppelstatus onbekend." and data["gereed"].startswith("Nee")


# ------------------------------------------------------------------ renderer in één doorgang (Peil 1.1.1, E07)
def test_render_semantiek() -> None:
    tpl = "<!--ALS:a-->A{{x}}<!--/ALS:a--><!--NIET:a-->N<!--/NIET:a--><!--ELK:l-->[{{n}}<!--ALS:r-->:{{r}}<!--/ALS:r-->]<!--/ELK:l-->"
    assert render(tpl, {"a": True, "x": "<&\"'>", "l": [{"n": 1}, {"n": 2, "r": "y"}]}) == (
        "A&lt;&amp;&quot;&#39;&gt;[1][2:y]"
    )
    assert render(tpl, {"a": False, "l": []}) == "N"
    assert strip_comments("a<!-- uitleg\n met regels -->\nb") == "ab"
    # waar = niet leeg: 0, lege tekst en een lege lijst zijn onwaar; True/False als tekst zoals JavaScript
    assert render("<!--ALS:x-->ja<!--/ALS:x-->", {"x": 0}) == "" and render("{{b}}", {"b": True}) == "true"


def test_een_doorgang_een_waarde_wordt_nooit_opnieuw_als_sjabloon_gelezen() -> None:
    """1.1.0 vulde een bronnaam ``{{csrf_token}}`` in met de CSRF-waarde; 1.1.1 laat zo'n waarde letterlijk staan."""
    out = render("<p>{{naam}}</p><i>{{csrf_token}}</i>", {"naam": "{{csrf_token}}", "csrf_token": "GEHEIM"})
    assert out == "<p>{{csrf_token}}</p><i>GEHEIM</i>"
    out = render("{{a}}", {"a": "<!--ALS:b-->x<!--/ALS:b-->", "b": True})
    assert out == "&lt;!--ALS:b--&gt;x&lt;!--/ALS:b--&gt;"


def test_verkeerd_genest_sjabloon_wordt_geweigerd() -> None:
    for kapot in ("<!--ALS:a-->x", "<!--ALS:a-->x<!--/NIET:a-->", "x<!--/ELK:l-->"):
        with pytest.raises(PeilTemplateError):
            parse(kapot)


def _referentie_python() -> ModuleType | None:
    """``bouw/sjabloon.py`` van de ontwerptaal op de gepinde commit: uit ``PEIL_BRON_DIR`` (CI) of de naastgelegen
    checkout van ai-server-manager."""
    prov, _ = split_copy((REPO_ROOT / "design" / "ontwerptaal" / "statuspagina.html").read_bytes())
    bron: bytes | None = None
    env = os.environ.get("PEIL_BRON_DIR")
    if env and (Path(env) / "sjabloon.py").exists():
        bron = (Path(env) / "sjabloon.py").read_bytes()
    else:
        sibling = REPO_ROOT.parent / "ai-server-manager"
        git = shutil.which("git")
        if git and (sibling / ".git").exists():
            r = subprocess.run(
                [git, "-C", str(sibling), "show", f"{prov.commit}:design/ontwerptaal/bouw/sjabloon.py"],
                capture_output=True,
                check=False,
            )
            bron = r.stdout if r.returncode == 0 else None
    if bron is None:
        return None
    mod = ModuleType("peil_referentie_sjabloon")
    exec(compile(bron, "sjabloon.py", "exec"), mod.__dict__)  # noqa: S102 - eigen bron van het platform, gepinde commit
    return mod


@pytest.mark.parametrize("name", ALLE)
def test_zelfde_uitkomst_als_de_referentie_renderer(name: str) -> None:
    """De Python-renderer geeft byte voor byte wat de referentie ``bouw/sjabloon.py`` (Peil 1.1.1, byte-gelijk getest met
    ``sjabloon.mjs``) geeft, op het echte sjabloon met de echte gegevens van deze toestand."""
    ref = _referentie_python()
    if ref is None:
        if os.environ.get("PEIL_BRON_VERPLICHT") == "1":
            pytest.fail("referentie-renderer bouw/sjabloon.py niet beschikbaar (PEIL_BRON_DIR)")
        pytest.skip("referentie-renderer (ai-server-manager/design/ontwerptaal/bouw/sjabloon.py) ontbreekt")
    inp, _ = _fixtures().toestanden()[name]
    data = json.loads(json.dumps(page_data(inp)))  # zoals de referentie het als JSON krijgt
    tpl = load_template()
    assert render(tpl.body, data) == ref.render(tpl.body, data)
