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
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert client.get("/readyz").status_code == 200 and client.get("/healthz").status_code == 200
