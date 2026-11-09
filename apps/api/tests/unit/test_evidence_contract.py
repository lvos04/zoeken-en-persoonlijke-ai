"""Contract 1.2 §11 (conformiteitspunt A4): ``docs/platform-v2/EVIDENCE.json`` gebruikt alleen de vaste Excel-statussen
en validatiewaarden, en elke test die als bewijs wordt genoemd bestaat echt.

- ``status`` uit de Excel-lijst; ``Gereed`` en ``Geslaagd`` worden in deze bouwomgeving nooit gezet.
- ``validatie``: één of meer waarden uit de vaste lijst, gescheiden door ``"; "``; toelichting hoort in ``opmerking``.
  Nooit leeg en nooit vrije tekst (A4), ook niet bij ``Niet gestart``: dan noemt ``validatie`` wat nog ontbreekt
  (``NIET GEVALIDEERD (…)``), wat waar is en niets verzint (QA statuspagina, ADR-0015).
- Per Excel-blad een eigen lijst: Testmatrix (``EX01``–``EX50``) heeft ``Sim-geslaagd``/``Sim-mislukt`` als
  simulatorresultaat, werkpakketten en installatiestappen ``Sim-getest``/``Unit-getest``; besluiten (``D01``–``D18``)
  hun eigen statussen.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from tests.conftest import REPO_ROOT

EVIDENCE = REPO_ROOT / "docs" / "platform-v2" / "EVIDENCE.json"
EXCEL_TAKEN = REPO_ROOT / "docs" / "platform-v2" / "EXCEL_TAKEN.json"
NOOIT = {"Gereed", "Geslaagd"}
NIET_GEVALIDEERD = {
    "NIET GEVALIDEERD (echte NAS)",
    "NIET GEVALIDEERD (hardware)",
    "NIET GEVALIDEERD (account)",
    "NIET GEVALIDEERD (Mac/Xcode)",
}
# (patroon van de ID, toegestane statussen, toegestane validatiewaarden) per Excel-blad (§11)
BLADEN = [
    (
        re.compile(r"EX\d{2}"),
        {"Niet gestart", "Bezig", "Mislukt", "Geblokkeerd", "N.v.t."},
        {"Sim-geslaagd", "Sim-mislukt"} | NIET_GEVALIDEERD,
    ),
    (re.compile(r"D\d{2}"), {"Open", "Ingevuld", "Getest", "Geblokkeerd", "N.v.t."}, NIET_GEVALIDEERD),
    (
        re.compile(r"(B\d{2}-\d{3}|I\d{2}-\d{2}-\d{2})"),
        {"Niet gestart", "Bezig", "Geblokkeerd", "N.v.t."},
        {"Sim-getest", "Unit-getest"} | NIET_GEVALIDEERD,
    ),
]
VELDEN = {"id", "status", "validatie", "bewijs", "blokkade", "opmerking", "repo", "bijgewerkt"}
TEST_REF = re.compile(r"((?:apps/api/)?tests/[\w/.-]+\.py)::(\w+)")


def _items() -> list[dict[str, Any]]:
    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data
    return data


@pytest.mark.parametrize("item", _items(), ids=lambda e: str(e.get("id")))
def test_item_volgt_paragraaf_11(item: dict[str, Any]) -> None:
    assert set(item) == VELDEN
    assert item["repo"] == "zoeken-en-persoonlijke-ai"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["bijgewerkt"])
    blad = next(((s, v) for p, s, v in BLADEN if p.fullmatch(item["id"])), None)
    assert blad is not None, f"onbekende soort ID: {item['id']}"
    statussen, validaties = blad
    assert item["status"] in statussen and item["status"] not in NOOIT, (item["id"], item["status"])
    waarden = item["validatie"].split("; ")
    assert all(w in validaties for w in waarden), (item["id"], item["validatie"])  # ook nooit leeg
    assert len(waarden) == len(set(waarden))


@pytest.mark.parametrize("item", _items(), ids=lambda e: str(e.get("id")))
def test_genoemde_tests_bestaan(item: dict[str, Any]) -> None:
    for path, name in TEST_REF.findall(item["bewijs"]):
        candidates = [REPO_ROOT / path, REPO_ROOT / "apps" / "api" / path]
        file = next((c for c in candidates if c.exists()), None)
        assert file is not None, f"{item['id']}: {path} bestaat niet"
        assert re.search(rf"^def {re.escape(name)}\(", file.read_text(encoding="utf-8"), flags=re.M), (
            f"{item['id']}: {path}::{name} bestaat niet (meer)"
        )


def test_ids_uniek_en_uit_de_excel() -> None:
    ids = [e["id"] for e in _items()]
    assert len(ids) == len(set(ids))
    excel = {t["ID"] for t in json.loads(EXCEL_TAKEN.read_text(encoding="utf-8"))}
    eigen = [i for i in ids if re.fullmatch(r"B\d{2}-\d{3}", i)]
    assert set(eigen) <= excel, set(eigen) - excel
