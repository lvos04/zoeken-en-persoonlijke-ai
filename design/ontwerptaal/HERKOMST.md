# Herkomst van `design/ontwerptaal/` (O-14, Peil 1.1.1 E08)

Versie-gepinde kopie van het statuspagina-sjabloon van de ontwerptaal **Peil 1.1.1** (ONTWERPTAAL §13.2, §14 en §16).
Bron van waarheid: `lvos04/ai-server-manager`, map `design/ontwerptaal/`, branch `ccr-c133d7a0-kuz1cv`, gepind op commit
`0a7143bc0a94ed5317a9655fa785e73a1fb07ba1` (de inhoud van beide bestanden is daar gelijk aan commit `3e559e7`, "Peil
1.1.1: statuspagina-sjabloon en renderer in één doorgang (E01-E10)").

| Bestand | Gebruik in deze dienst | sha256 van het bronbestand |
|---|---|---|
| `statuspagina.html` | sjabloon van `GET /` en van het antwoord op `POST /koppelen` (CSS en iconen inline, geen JS); gerenderd door `search_ai/peil.py` | `d352028f798934d5efb96610c42a61712e3948b8c9eec8a87f34035b89f8c80b` |
| `statuspagina.css` | dezelfde CSS los (staat ook inline in het sjabloon); alleen voor controle en hergebruik | `0f9963583a32d91232e7cfb1b7fe8a8b7612cf8349d4fcc8edc3ca545e32e91b` |

- Beide bestanden hebben een **herkomstkop van 2 regels** (versie, bronpad, commit, sha256); de rest is byte-gelijk
  aan de bron. Niet met de hand wijzigen.
- De dienst controleert bij het laden dat de inhoud de sha256 uit de kop heeft, dat de versie 1.1.1 is en dat het
  sjabloon goed genest is; anders toont `GET /` een eerlijke 500 (`peil_sjabloon_ongeldig`). De image-bouw doet dezelfde
  controle.
- **Renderer in één doorgang** (E07): `search_ai/peil.py` leest het sjabloon in een boom en schrijft die uit; een
  ingevulde waarde wordt nooit opnieuw als sjabloon gelezen. `apps/api/tests/unit/test_statuspagina_peil.py` vergelijkt
  de uitvoer per toestand byte voor byte met de referentie `bouw/sjabloon.py` op de gepinde commit.
- **Pin- en driftregel** (E08, `apps/api/tests/unit/test_peil_kopie.py`): CI faalt bij (1) een afwijking van de bron op
  de gepinde commit en (2) dezelfde versie met andere inhoud op de werkbranch van de bron. Een **nieuwere** versie op de
  werkbranch geeft alleen een waarschuwing. CI (`.github/workflows/ci.yml`, stap *Peil-bron … ophalen*) haalt beide op;
  zonder bron faalt de test (`PEIL_BRON_VERPLICHT=1`).
- Niet gekopieerd: de rest van de ontwerptaal (`ontwerptaal.css`, tokens, prototypes, schermafdrukken, bouwscripts):
  deze dienst heeft alleen een statuspagina.

## Controleren

```sh
cd design/ontwerptaal
for f in statuspagina.html statuspagina.css; do echo "$(tail -n +3 "$f" | sha256sum | cut -d' ' -f1)  $f"; done
```

De uitkomst moet gelijk zijn aan de tabel. Tegen de bron: `git -C ../ai-server-manager show
0a7143bc0a94ed5317a9655fa785e73a1fb07ba1:design/ontwerptaal/statuspagina.html | sha256sum`.

## Bijwerken naar een nieuwe Peil-versie

1. Nieuwe bronbestanden op de nieuwe commit ophalen; kop opnieuw schrijven (versie, commit, sha256); tabel hierboven.
2. `PEIL_VERSION` in `apps/api/src/search_ai/peil.py` aanpassen; `make test ui ui-echt` (axe, 320–1440 px, lettertype
   van de CI-runner, schermafdrukken).
3. Notitie in `docs/adr/` en `CHANGELOG.md`.

## Vorige versies

- Peil 1.1.0 (commit `172ee87f15406edbe1a72dda30908abde7488e94`), van 2026-11-09 tot de overstap in ADR-0016.
