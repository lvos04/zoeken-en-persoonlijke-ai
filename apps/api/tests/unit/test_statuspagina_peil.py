"""Statuspagina in Peil (ONTWERPTAAL §13.2) volgens contract 1.2 §3.7 — rendering per toestand, zonder database.

Toestanden komen uit ``tests/ui/render_toestanden.py`` (dezelfde invoer als de browsercontrole en de schermafdrukken):
niet gekoppeld · koppelen mislukt · gekoppeld · ingetrokken · niet gereed (+ gekoppeld-beperkt, ontkoppeld,
koppelstatus onleesbaar).
"""

from __future__ import annotations

import importlib.util
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

from search_ai.peil import load_template, render, strip_comments
from search_ai.status_page import NOTICES, AttemptLog, PageInput, page_data, render_page
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
    assert page.count("<h1>") == 1 and "Peil statuspagina.css 1.1.0" in page
    assert "ontkoppelcode" not in page.lower() and 'action="/ontkoppelen"' not in page
    for token in ("service_token", "svc_", "secret_sha256", "AbCd"):  # alleen de witte lijst van §3.5
        assert token not in page


@pytest.mark.parametrize("name", ALLE)
def test_koppelformulier_alleen_als_nooit_gekoppeld(name: str) -> None:
    page = _page(name)
    inp, _ = _fixtures().toestanden()[name]
    form = inp.doc.get("state") == "nooit_gekoppeld" and not inp.doc.get("first_linked_at")
    assert ('action="/koppelen"' in page) is form
    assert ('name="csrf"' in page) is form
    if form:
        for field in ("hub_adres", "koppelcode", "hub_vingerafdruk"):  # drie verplichte velden (§3.7)
            assert re.search(rf'<input [^>]*name="{field}"[^>]*\srequired', page), field
        assert "Vingerafdruk van de Hub" in page and "Geldig 60 minuten" in page
    else:
        assert "via de Hub" in page


# ------------------------------------------------------------------ per toestand
def test_niet_gekoppeld() -> None:
    page = _page("niet-gekoppeld")
    assert "<title>Zoeken en persoonlijke AI — niet gereed, niet gekoppeld</title>" in page
    totaal = _totaal(page)
    assert "pil-onbekend" in totaal and "Niet gereed" in totaal and "wacht op koppeling met de Hub" in totaal
    assert "pil-gezond" not in totaal  # eerste signaal nooit groen als niet gereed
    assert 'class="melding' not in page
    assert (
        '<span class="pil pil-onbekend"><svg aria-hidden="true"><use href="#i-niet-gekoppeld"/></svg>Niet gekoppeld'
        in page
    )


def test_koppelen_mislukt_toont_code_en_kopieerbare_trace_id() -> None:
    page = _page("koppelen-mislukt")
    trace = _fixtures().TRACE
    assert '<div class="melding melding-fout" role="alert" aria-labelledby="melding-titel"' in page
    assert "<strong>Koppelen is mislukt</strong>" in page
    assert "Code <code>hub_weigerde</code> · Trace-ID (selecteer en kopieer)" in page
    assert f'<code class="selecteer">{trace}</code>' in page
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
    assert f"<code>{_fixtures().FP}</code>" in page and f'<a href="{_fixtures().HUB}">Hub openen</a>' in page
    assert "Ontkoppelen of opnieuw koppelen gaat via de Hub." in page
    assert not re.search(r"pil-gezond[^<]*<svg[^>]*><use[^>]*/></svg>Gekoppeld", page)
    # alles gemeten en goed: dan pas groen
    assert "Alles gezond" in _totaal(page) and "pil-gezond" in _totaal(page)
    assert "2 actief, 1 gepauzeerd" in page


def test_gekoppeld_maar_beperkt_is_nooit_alles_gezond() -> None:
    page = _page("gekoppeld-beperkt")
    totaal = _totaal(page)
    assert "pil-beperkt" in totaal and "Alles gezond" not in totaal
    assert "nog niet bevestigd" in page and "(code niet_bevestigd)" in page and "(code dead_letter)" in page


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


def test_koppelstatus_onleesbaar_is_storing_zonder_formulier() -> None:
    page = _page("koppelstatus-onleesbaar")
    assert "Koppelstatus onleesbaar" in page and "(code koppelstatus_onleesbaar)" in page
    assert "pil-storing" in _totaal(page) and 'action="/koppelen"' not in page
    assert "2 genegeerd (zie de app-log)" in page


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


def test_onbekende_melding_wordt_niet_weerspiegeld() -> None:
    data = page_data(_inp(melding="<script>alert(1)</script>"))
    assert "melding" not in data and "trace_id" not in data


@pytest.mark.parametrize("state", ["nooit_gekoppeld", "ontkoppeld", "ingetrokken"])
def test_succesmelding_uit_de_url_spreekt_de_koppelstatus_nooit_tegen(state: str) -> None:
    for code in ("gekoppeld", "gekoppeld_onbevestigd"):
        assert "melding" not in page_data(_inp(doc={"state": state}, melding=code))
    assert page_data(_inp(doc=_fixtures()._linked(), melding="gekoppeld"))["melding_titel"] == "Gekoppeld met de Hub"


def test_trace_id_alleen_bij_een_foutmelding() -> None:
    assert "trace_id" not in page_data(_inp(melding="gekoppeld", trace_id="t"))
    data = page_data(_inp(melding="te_veel_pogingen", trace_id="t-1"))
    assert data["trace_id"] == "t-1" and data["melding_code"] == "te_veel_pogingen"


def test_hub_adres_wordt_geescaped_en_alleen_http_s_is_een_link() -> None:
    tpl = load_template()
    doc = {
        "state": "gekoppeld",
        "hub_url": 'http://nas"><script>x</script>:30400',
        "linked_at": "2026-11-08T19:14:00+00:00",
        "pins": {"keyset_fingerprint": "MEGG-8GFG-2E4Y-K2RQ"},
    }
    page = render_page(tpl, _inp(doc=doc))
    assert "<script>" not in page and "&lt;script&gt;" in page
    page = render_page(tpl, _inp(doc={**doc, "hub_url": "javascript:alert(1)"}))
    assert "javascript:" not in page


@pytest.mark.parametrize("code", sorted(NOTICES))
def test_elke_melding_rendert_met_vaste_tekst(code: str) -> None:
    notice = NOTICES[code]
    doc = _fixtures()._linked() if notice.toon == "info" else {"state": "nooit_gekoppeld"}
    page = render_page(load_template(), _inp(doc=doc, melding=code, trace_id="0f0e0d0c-0000-4000-8000-000000000000"))
    assert f"<strong>{notice.titel}</strong>" in page
    assert ("0f0e0d0c-0000-4000-8000-000000000000" in page) is (notice.toon == "fout")
    if notice.veld:
        veld = {"hub": "hub-fout", "koppelcode": "code-fout", "vingerafdruk": "vinger-fout"}[notice.veld]
        assert f'id="{veld}"' in page


def test_tijd_in_nederlandse_notatie() -> None:
    data = page_data(_inp())
    assert data["bijgewerkt"] == "9 nov 2026, 10:56 UTC" or re.fullmatch(r"9 nov 2026, \d\d:\d\d", data["bijgewerkt"])
    assert data["checks"][0]["iso"].startswith("2026-11-09T")


# ------------------------------------------------------------------ poging-sleutel voor de Trace-ID
def test_attempt_log() -> None:
    log = AttemptLog(ttl_s=10, max_items=3)
    key = log.put("hub_weigerde", "trace-1", now=0)
    assert log.get(key, "hub_weigerde", now=1) == "trace-1"
    assert log.get(key, "te_veel_pogingen", now=1) is None  # andere melding: geen Trace-ID
    assert log.get("kort", "hub_weigerde", now=1) is None
    assert log.get("<script>alert(1)</script>xxxxxxxxxx", "hub_weigerde", now=1) is None
    assert log.get(key, "hub_weigerde", now=10) is None  # verlopen
    keys = [log.put("x", f"t{i}", now=20) for i in range(5)]
    assert log.get(keys[0], "x", now=21) is None and log.get(keys[-1], "x", now=21) == "t4"  # begrensd


# ------------------------------------------------------------------ renderer = referentie (sjabloon.mjs)
def test_render_semantiek() -> None:
    tpl = "<!--ALS:a-->A{{x}}<!--/ALS:a--><!--NIET:a-->N<!--/NIET:a--><!--ELK:l-->[{{n}}<!--ALS:r-->:{{r}}<!--/ALS:r-->]<!--/ELK:l-->"
    assert render(tpl, {"a": True, "x": "<&\"'>", "l": [{"n": 1}, {"n": 2, "r": "y"}]}) == (
        "A&lt;&amp;&quot;&#x27;&gt;[1][2:y]"
    )
    assert render(tpl, {"a": False, "l": []}) == "N"
    assert strip_comments("a<!-- uitleg\n met regels -->\nb") == "ab"


REFERENTIE = REPO_ROOT.parent / "ai-server-manager" / "design" / "ontwerptaal" / "bouw" / "sjabloon.mjs"


@pytest.mark.parametrize("name", ALLE)
def test_zelfde_uitkomst_als_de_referentie_renderer(name: str, tmp_path: Path) -> None:
    """Lokaal (naastgelegen ai-server-manager + node): de Python-renderer geeft byte voor byte wat ``sjabloon.mjs``
    geeft, op het echte sjabloon met de echte gegevens van deze toestand."""
    node = shutil.which("node")
    if not node or not REFERENTIE.exists():
        pytest.skip("referentie-renderer (ai-server-manager/design/ontwerptaal/bouw/sjabloon.mjs) of node ontbreekt")
    import json

    inp, _ = _fixtures().toestanden()[name]
    data = page_data(inp)
    tpl = load_template()
    (tmp_path / "data.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "tpl.html").write_text(tpl.body, encoding="utf-8")
    script = (
        f"import {{ render }} from {json.dumps(REFERENTIE.as_uri())}; import fs from 'node:fs';"
        f"process.stdout.write(render(fs.readFileSync({json.dumps(str(tmp_path / 'tpl.html'))}, 'utf8'),"
        f" JSON.parse(fs.readFileSync({json.dumps(str(tmp_path / 'data.json'))}, 'utf8'))));"
    )
    out = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, check=True, text=True)
    ours = render(tpl.body, data)
    # enige bekende verschil: JS escapet ' als &#39;, Python als &#x27; (zelfde teken)
    assert ours.replace("&#x27;", "&#39;") == out.stdout
