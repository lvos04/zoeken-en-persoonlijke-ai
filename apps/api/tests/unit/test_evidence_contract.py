"""Contract 1.3 §11 (C1.3-ALG-02, C1.3-ZPA-12): ``docs/platform-v2/EVIDENCE.json`` gebruikt alleen de vaste
Excel-statussen en validatiewaarden, en elke test die als bewijs wordt genoemd bestaat echt.

- ``status`` uit de Excel-lijst van het blad; ``Gereed`` en ``Geslaagd`` worden in deze bouwomgeving nooit gezet.
- ``validatie``: één of meer waarden uit de vaste lijst, gescheiden door ``"; "``; toelichting hoort in ``opmerking``.
  Nooit leeg en nooit vrije tekst. Nieuw in 1.3: ``Intel-iGPU-getest (ontwikkelmachine)`` en ``Niet getest`` (alleen bij
  status ``Niet gestart``).
- Elke ``NIET GEVALIDEERD (…)`` die in ``blokkade`` staat, staat ook in ``validatie`` (nieuw in 1.3).
- Per Excel-blad een eigen lijst: Testmatrix (``EX..``, ``JLT-..``) heeft ``Sim-geslaagd``/``Sim-mislukt`` als
  simulatorresultaat, werkpakketten en installatiestappen ``Sim-getest``/``Unit-getest``; besluiten (``D01``–``D18``)
  hun eigen statussen. Naast Excel-ID's mogen contractpunten (``C1.3-ZPA-<nn>`` uit ``CONTRACT_1.3_CONFORMITEIT.md``,
  ``C1.2-<code>``) en reviews (``REVIEW-zoeken-en-persoonlijke-ai-<onderwerp>``) een eigen regel hebben, met de
  statussen van het blad Werkpakketten.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from tests.conftest import REPO_ROOT

EVIDENCE = REPO_ROOT / "docs" / "platform-v2" / "EVIDENCE.json"
EXCEL_TAKEN = REPO_ROOT / "docs" / "platform-v2" / "EXCEL_TAKEN.json"
CONFORMITEIT = REPO_ROOT / "docs" / "platform-v2" / "CONTRACT_1.3_CONFORMITEIT.md"
NOOIT = {"Gereed", "Geslaagd"}
NIET_GEVALIDEERD = {
    "NIET GEVALIDEERD (echte NAS)",
    "NIET GEVALIDEERD (hardware)",
    "NIET GEVALIDEERD (account)",
    "NIET GEVALIDEERD (Mac/Xcode)",
}
ALTIJD = NIET_GEVALIDEERD | {"Intel-iGPU-getest (ontwikkelmachine)", "Niet getest"}
WERKPAKKET = {"Niet gestart", "Bezig", "Geblokkeerd", "N.v.t."}
# (patroon van de ID, toegestane statussen, toegestane validatiewaarden) per Excel-blad (§11)
BLADEN = [
    (
        re.compile(r"EX\d{2}|JLT-\d{2}"),
        {"Niet gestart", "Bezig", "Mislukt", "Geblokkeerd", "N.v.t."},
        {"Sim-geslaagd", "Sim-mislukt"} | ALTIJD,
    ),
    (re.compile(r"D\d{2}"), {"Open", "Ingevuld", "Getest", "Geblokkeerd", "N.v.t."}, ALTIJD),
    (
        re.compile(r"(B\d{2}-\d{3}|I\d{2}-\d{2}-\d{2})"),
        WERKPAKKET,
        {"Sim-getest", "Unit-getest"} | ALTIJD,
    ),
    # contractpunten en reviews (nieuw in 1.3): statussen van het blad Werkpakketten
    (
        re.compile(
            r"C1\.3-ZPA-\d{2}|C1\.3-ALG-\d{2}|C1\.2-[A-Z0-9]+|REVIEW-zoeken-en-persoonlijke-ai-[a-z0-9][a-z0-9.-]*"
        ),
        WERKPAKKET,
        {"Sim-getest", "Unit-getest"} | ALTIJD,
    ),
]
VELDEN = {"id", "status", "validatie", "bewijs", "blokkade", "opmerking", "repo", "bijgewerkt"}
TEST_REF = re.compile(r"((?:apps/api/)?tests/[\w/.-]+\.py)::(\w+)")
NG_IN_TEKST = re.compile(r"NIET GEVALIDEERD \([^)]*\)")


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
    assert item["validatie"].strip(), "een lege validatie is nooit toegestaan"
    waarden = item["validatie"].split("; ")
    assert all(w in validaties for w in waarden), (item["id"], item["validatie"])  # ook nooit leeg
    assert len(waarden) == len(set(waarden))
    if "Niet getest" in waarden:
        assert item["status"] == "Niet gestart", (item["id"], "'Niet getest' alleen bij status 'Niet gestart'")


@pytest.mark.parametrize("item", _items(), ids=lambda e: str(e.get("id")))
def test_niet_gevalideerd_in_blokkade_staat_ook_in_validatie(item: dict[str, Any]) -> None:
    waarden = item["validatie"].split("; ")
    for gevonden in NG_IN_TEKST.findall(item["blokkade"]):
        assert gevonden in NIET_GEVALIDEERD, f"{item['id']}: {gevonden!r} is geen waarde uit de vaste lijst"
        assert gevonden in waarden, f"{item['id']}: {gevonden!r} staat in blokkade maar niet in validatie"


def test_contractpunten_bestaan_in_de_conformiteitslijst() -> None:
    lijst = CONFORMITEIT.read_text(encoding="utf-8")
    for item in _items():
        if item["id"].startswith("C1.3-"):
            assert f"| {item['id']} |" in lijst, f"{item['id']} staat niet in CONTRACT_1.3_CONFORMITEIT.md"


def test_zelftest_inconsistente_blokkade_wordt_herkend() -> None:
    """Negatief: een blokkade met NIET GEVALIDEERD (echte NAS) zonder die waarde in validatie valt op."""
    item = {"validatie": "Sim-getest", "blokkade": "NIET GEVALIDEERD (echte NAS): geen NAS"}
    gevonden = NG_IN_TEKST.findall(item["blokkade"])
    assert gevonden and not all(g in item["validatie"].split("; ") for g in gevonden)


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
