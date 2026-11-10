"""Contract 1.3 §5 (C1.3-ZPA-11): statische test op ``Idempotency-Key``.

Elke muterende route (POST/PUT/PATCH/DELETE) van de app moet de afhankelijkheid ``idempotency_key`` hebben, tenzij ze met
reden in ``UITZONDERINGEN`` staat. De lijst volgt de uitzonderingen van §5:
- formulier-POST's van een statuspagina (een browserformulier kan geen header meesturen; bescherming via het
  formuliertoken, de herkomstcontrole, de pogingenlimiet, ``koppelen_bezig`` en de eenmaligheid van de koppelcode, RV10);
- ``POST /connector/v1/propose|apply|rollback`` **alleen zolang** ze 501 ``capability_unavailable`` geven.
Een nieuwe muterende route zonder sleutel en zonder reden laat deze test falen; andere uitzonderingen vragen een ADR
en een contractmelding voor de volgende versie. Let op §4.6: ook zonder ``Idempotency-Key`` verbruikt een POST de
``jti`` (zie ``tests/integration/test_jwt_1_3.py``).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from search_ai import connector
from search_ai.app import create_app
from search_ai.config import Settings
from search_ai.idempotency import idempotency_key

MUTATIES = {"POST", "PUT", "PATCH", "DELETE"}
UITZONDERINGEN = {
    ("POST", "/koppelen"): "statuspagina-formulier (§3.7/§5, RV10): browserformulier kan geen header meesturen",
    ("POST", "/connector/v1/propose"): "501 capability_unavailable (§5): deze dienst voert geen acties uit",
    ("POST", "/connector/v1/apply"): "501 capability_unavailable (§5): deze dienst voert geen acties uit",
    ("POST", "/connector/v1/rollback"): "501 capability_unavailable (§5): deze dienst voert geen acties uit",
}
ALLEEN_ZOLANG_501 = {
    "/connector/v1/propose": connector.propose,
    "/connector/v1/apply": connector.apply,
    "/connector/v1/rollback": connector.rollback,
}


def _heeft_idempotency_key(dep: Dependant) -> bool:
    if dep.call is idempotency_key:
        return True
    return any(_heeft_idempotency_key(sub) for sub in dep.dependencies)


def _plat(routes: list[Any]) -> list[APIRoute]:
    """Alle ``APIRoute``'s, ook binnen ingesloten routers (FastAPI houdt die als aparte knoop bij)."""
    out: list[APIRoute] = []
    for r in routes:
        if isinstance(r, APIRoute):
            out.append(r)
        elif hasattr(r, "original_router"):
            out.extend(_plat(list(r.original_router.routes)))
        elif hasattr(r, "routes"):
            out.extend(_plat(list(r.routes)))
    return out


@pytest.fixture(scope="module")
def routes(tmp_path_factory: pytest.TempPathFactory) -> list[APIRoute]:
    data = Path(tmp_path_factory.mktemp("appdata"))
    app = create_app(Settings(data_dir=data, database_url="postgresql+psycopg://niemand@127.0.0.1:1/geen"))
    found = _plat(list(app.routes))
    assert len(found) > 20, "routes niet gevonden (andere FastAPI-structuur?)"
    return found


def _muterend(routes: list[APIRoute]) -> list[tuple[str, str, APIRoute]]:
    return [(m, r.path, r) for r in routes for m in sorted(r.methods & MUTATIES)]


def test_er_zijn_muterende_routes(routes: list[APIRoute]) -> None:
    paden = {(m, p) for m, p, _r in _muterend(routes)}
    assert ("POST", "/search/v1/sources") in paden and ("POST", "/koppelen") in paden


def test_elke_muterende_route_vraagt_idempotency_key_of_staat_met_reden_in_de_lijst(routes: list[APIRoute]) -> None:
    zonder = [
        (m, p)
        for m, p, r in _muterend(routes)
        if not _heeft_idempotency_key(r.dependant) and (m, p) not in UITZONDERINGEN
    ]
    assert zonder == [], f"muterende route(s) zonder Idempotency-Key en zonder reden: {zonder}"


def test_uitzonderingen_bestaan_nog_en_hebben_een_reden(routes: list[APIRoute]) -> None:
    paden = {(m, p): r for m, p, r in _muterend(routes)}
    for sleutel, reden in UITZONDERINGEN.items():
        assert sleutel in paden, f"verouderde uitzondering {sleutel}: route bestaat niet meer"
        assert reden.strip()
        assert not _heeft_idempotency_key(paden[sleutel].dependant), f"{sleutel} vraagt de sleutel al: haal hem weg"


def test_connector_uitzonderingen_alleen_zolang_ze_501_geven() -> None:
    for pad, fn in ALLEEN_ZOLANG_501.items():
        bron = inspect.getsource(fn)
        assert "raise capability_unavailable(" in bron, f"{pad} geeft geen 501 meer: dan vervalt de uitzondering"


def test_negatief_een_nieuwe_route_zonder_sleutel_wordt_gevonden(routes: list[APIRoute]) -> None:
    """Zelftest: een (nep)route zonder sleutel en zonder reden komt in de foutlijst."""
    from fastapi import FastAPI

    app = FastAPI()

    @app.post("/search/v1/nieuw")
    def nieuw() -> None:  # pragma: no cover - wordt niet aangeroepen
        return None

    extra = [r for r in app.routes if isinstance(r, APIRoute)]
    zonder = [(m, p) for m, p, r in _muterend(extra) if not _heeft_idempotency_key(r.dependant)]
    assert zonder == [("POST", "/search/v1/nieuw")]
