"""Rendert de statuspagina per toestand met de echte renderer (``search_ai.status_page.render_page``) en het echte
Peil-sjabloon, voor de browsercontrole (``statuspagina.mjs``: axe-core, overloop op 320 px, CSP, schermafdrukken).

Alle gegevens zijn fictief (simulator): het Hub-adres eindigt op ``.invalid``, de vingerafdruk en de Trace-ID zijn
testwaarden. Er wordt geen database, Hub of NAS aangesproken.

    python tests/ui/render_toestanden.py --out <map>
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from search_ai.peil import load_template
from search_ai.status_page import SECURITY_HEADERS, PageInput, render_page

NOW = datetime(2026, 11, 9, 10, 56, tzinfo=UTC)
OK = [{"name": n, "status": "ok"} for n in ("database", "migraties", "pgvector", "geheimen")]
CSRF = "voorbeeld-csrf-token-0123456789abcdef"
TRACE = "3d8a1f4e-6b2c-4e95-a7d0-91c5b8e2f364"
HUB = "http://een-lange-nas-naam-voor-de-overlooptest.voorbeeld.invalid:30400"
FP = "MEGG-8GFG-2E4Y-K2RQ"


def _never(**extra: Any) -> dict[str, Any]:
    doc = {"state": "nooit_gekoppeld", "pending": None, "last_error": None, "first_linked_at": None}
    doc.update(extra)
    return doc


def _linked(**extra: Any) -> dict[str, Any]:
    doc = {
        "state": "gekoppeld",
        "hub_url": HUB,
        "service_id": "00000000-0000-4000-8000-000000000001",
        "via": "koppelcode",
        "first_linked_at": "2026-11-08T19:14:00+00:00",
        "linked_at": "2026-11-08T19:14:00+00:00",
        "confirmed_at": "2026-11-08T19:14:01+00:00",
        "service_token_last4": "AbCd",
        "pins": {"keyset_fingerprint": FP},
        "pending": None,
        "last_error": None,
    }
    doc.update(extra)
    return doc


def _health(**afwijkend: tuple[str, str | None]) -> list[dict[str, Any]]:
    """Connector-gezondheid zoals de dienst hem meet (alles gezond, behalve ``afwijkend``: naam → (status, code))."""
    namen = (
        "database",
        "migraties",
        "pgvector",
        "hub_link",
        "hub_sleutels",
        "worker",
        "jobs",
        "intrekking_opgeschoond",
    )
    out = []
    for n in namen:
        status, code = afwijkend.get(n, ("healthy", None))
        out.append({"name": n, "status": status, "reason_code": code, "observed_at": "2026-11-09T10:55:00+00:00"})
    return out


# naam → (PageInput, schermafdruk ja/nee). De vijf toestanden van de opdracht krijgen schermafdrukken; de andere lopen
# mee in axe/overloop/CSP en in de unit-tests. Peil 1.1.1: ook koppelen bezig, onleesbaar als blijvende toestand (zonder
# rol), de herkomstweigering, een adres zonder koppelmogelijkheid, een verouderde worker en een beperkt dienstblok.
def toestanden() -> dict[str, tuple[PageInput, bool]]:
    base: dict[str, Any] = {"per_status": {}, "roots": 1, "root_warnings": 0, "csrf": CSRF, "now": NOW}
    gezond = _health()
    onleesbaar = {**_never(), "state": "ontkoppeld", "corrupt": True}
    return {
        "niet-gekoppeld": (PageInput(ready=True, checks=OK, doc=_never(), health=gezond, **base), True),
        "koppelen-mislukt": (
            PageInput(
                ready=True, checks=OK, doc=_never(), melding="hub_weigerde", trace_id=TRACE, health=gezond, **base
            ),
            True,
        ),
        "veldfout-hub-adres": (
            PageInput(ready=True, checks=OK, doc=_never(), melding="hub_adres_ongeldig", trace_id=TRACE, **base),
            False,
        ),
        "gekoppeld": (
            PageInput(
                ready=True,
                checks=OK,
                doc=_linked(),
                health=gezond,
                **{**base, "per_status": {"active": 2, "paused": 1}},
            ),
            True,
        ),
        "gekoppeld-melding": (
            PageInput(ready=True, checks=OK, doc=_linked(), health=gezond, melding="gekoppeld", trace_id=TRACE, **base),
            False,
        ),
        "ingetrokken": (
            PageInput(
                ready=True,
                checks=OK,
                doc={
                    **_linked(state="ingetrokken", pins=None, service_token_last4=None, confirmed_at=None),
                    "last_error": {"code": "ingetrokken_door_hub", "at": "2026-11-09T09:00:00+00:00"},
                },
                **base,
            ),
            True,
        ),
        "niet-gereed": (
            PageInput(
                ready=False,
                checks=[{"name": "database", "status": "fout"}, {"name": "geheimen", "status": "ok"}],
                doc=_never(),
                **{**base, "per_status": None, "roots": 0},
            ),
            True,
        ),
        "gekoppeld-beperkt": (
            PageInput(
                ready=True,
                checks=OK,
                doc=_linked(confirmed_at=None),
                health=_health(hub_link=("degraded", "niet_bevestigd"), jobs=("degraded", "dead_letter")),
                **base,
            ),
            False,
        ),
        "ontkoppeld": (
            PageInput(ready=True, checks=OK, doc=_linked(state="ontkoppeld", pins=None, confirmed_at=None), **base),
            False,
        ),
        "koppelstatus-onleesbaar": (
            PageInput(ready=True, checks=OK, doc=onleesbaar, **{**base, "root_warnings": 2}),
            False,
        ),
        # POST uit een oud tabblad op een onleesbaar koppelbestand → melding met code en Trace-ID (rol alert)
        "koppelen-op-onleesbaar": (
            PageInput(ready=True, checks=OK, doc=onleesbaar, melding="koppelstatus_onleesbaar", trace_id=TRACE, **base),
            False,
        ),
        "koppelen-bezig": (
            PageInput(
                ready=True,
                checks=OK,
                doc=_never(
                    pending={"kind": "token", "hub_url": HUB, "started_at": "2026-11-09T10:54:00+00:00", "attempts": 2}
                ),
                health=gezond,
                **base,
            ),
            False,
        ),
        "herkomst-geweigerd": (
            PageInput(
                ready=True, checks=OK, doc=_never(), melding="herkomst_geweigerd", trace_id=TRACE, health=gezond, **base
            ),
            False,
        ),
        "adres-zonder-koppelen": (
            PageInput(ready=True, checks=OK, doc=_never(), health=gezond, host_blocked=True, **base),
            False,
        ),
        "worker-verouderd": (
            PageInput(
                ready=True,
                checks=OK,
                doc=_linked(),
                health=[
                    *[c for c in gezond if c["name"] != "worker"],
                    {
                        "name": "worker",
                        "status": "unknown",
                        "reason_code": "meting_verouderd",
                        "observed_at": "2026-11-09T10:41:00+00:00",
                    },
                ],
                **base,
            ),
            False,
        ),
        "shares-beperkt": (
            PageInput(ready=True, checks=OK, doc=_linked(), health=gezond, **{**base, "root_warnings": 1}),
            False,
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    out = ap.parse_args().out
    out.mkdir(parents=True, exist_ok=True)
    tpl = load_template()
    index = []
    for name, (inp, shot) in toestanden().items():
        (out / f"{name}.html").write_text(render_page(tpl, inp), encoding="utf-8")
        index.append({"naam": name, "schermafdruk": shot})
    meta = {"headers": SECURITY_HEADERS, "toestanden": index, "peil": tpl.provenance.version}
    (out / "toestanden.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(index)} toestanden in {out}")


if __name__ == "__main__":
    main()
