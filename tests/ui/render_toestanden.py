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


def _never() -> dict[str, Any]:
    return {"state": "nooit_gekoppeld", "pending": None, "last_error": None, "first_linked_at": None}


def _linked(**extra: Any) -> dict[str, Any]:
    doc = {
        "state": "gekoppeld",
        "hub_url": HUB,
        "service_id": "00000000-0000-4000-8000-000000000001",
        "via": "koppelcode",
        "first_linked_at": "2026-11-08T19:14:00+00:00",
        "linked_at": "2026-11-08T19:14:00+00:00",
        "service_token_last4": "AbCd",
        "pins": {"keyset_fingerprint": FP},
        "pending": None,
        "last_error": None,
    }
    doc.update(extra)
    return doc


# naam → (PageInput, schermafdruk ja/nee). De vijf toestanden van de opdracht krijgen schermafdrukken; de andere
# drie lopen mee in axe/overloop/CSP en in de unit-tests.
def toestanden() -> dict[str, tuple[PageInput, bool]]:
    base: dict[str, Any] = {"per_status": {}, "roots": 1, "root_warnings": 0, "csrf": CSRF, "now": NOW}
    return {
        "niet-gekoppeld": (PageInput(ready=True, checks=OK, doc=_never(), **base), True),
        "koppelen-mislukt": (
            PageInput(ready=True, checks=OK, doc=_never(), melding="hub_weigerde", trace_id=TRACE, **base),
            True,
        ),
        "veldfout-hub-adres": (
            PageInput(ready=True, checks=OK, doc=_never(), melding="ongeldig_hub_adres", trace_id=TRACE, **base),
            False,
        ),
        "gekoppeld": (
            PageInput(
                ready=True,
                checks=OK,
                doc=_linked(),
                **{**base, "per_status": {"active": 2, "paused": 1}},
            ),
            True,
        ),
        "ingetrokken": (
            PageInput(
                ready=True,
                checks=OK,
                doc={
                    **_linked(state="ingetrokken", pins=None, service_token_last4=None),
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
                doc=_linked(),
                health=[
                    {"name": "hub_koppeling", "status": "degraded", "reason_code": "niet_bevestigd"},
                    {"name": "outbox", "status": "degraded", "reason_code": "dead_letter"},
                ],
                **base,
            ),
            False,
        ),
        "ontkoppeld": (PageInput(ready=True, checks=OK, doc=_linked(state="ontkoppeld", pins=None), **base), False),
        "koppelstatus-onleesbaar": (
            PageInput(
                ready=True,
                checks=OK,
                doc={**_never(), "state": "ontkoppeld", "corrupt": True},
                **{**base, "root_warnings": 2},
            ),
            False,
        ),
        # QA (ADR-0015): koppelpoging uit een oud tabblad op een onleesbaar koppelbestand → melding met code in het
        # <code>-blok + Trace-ID (P-14: geen overloop op 320 px, ook niet met ruimere tekstafstand)
        "koppelen-op-onleesbaar": (
            PageInput(
                ready=True,
                checks=OK,
                doc={**_never(), "state": "ontkoppeld", "corrupt": True},
                melding="koppelstatus_onleesbaar",
                trace_id=TRACE,
                **base,
            ),
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
