# Herkomst van `design/ontwerptaal/` (O-14)

Versie-gepinde kopie van het statuspagina-sjabloon van de ontwerptaal **Peil 1.1.0** (ONTWERPTAAL §13.2 en §14).
Bron van waarheid: `lvos04/ai-server-manager`, map `design/ontwerptaal/`, commit
`172ee87f15406edbe1a72dda30908abde7488e94` ("Ontwerptaal Peil 1.1.0 als bron van waarheid (contract 1.2 §13)").

| Bestand | Gebruik in deze dienst | sha256 van het bronbestand |
|---|---|---|
| `statuspagina.html` | sjabloon van `GET /` (CSS en iconen inline, geen JS); gerenderd door `search_ai/peil.py` | `be766cc4bf6962772a8dfc77996da34040fa94698403a9625e33545fa9ee62c8` |
| `statuspagina.css` | dezelfde CSS los (staat ook inline in het sjabloon); alleen voor controle en hergebruik | `b1e3f789cbeaa2c928088cdac482cc8d6d11c7a5007c5d9b79005df66dd1b5af` |

- Beide bestanden hebben een **herkomstkop van 2 regels** (versie, bronpad, commit, sha256); de rest is byte-gelijk
  aan de bron. Niet met de hand wijzigen.
- De dienst controleert bij het laden dat de inhoud de sha256 uit de kop heeft en dat de versie 1.1.0 is; anders toont
  `GET /` een eerlijke 500 (`peil_sjabloon_ongeldig`). De image-bouw doet dezelfde controle.
- **CI** (`.github/workflows/ci.yml`, stap *Peil-bron op de gepinde commit ophalen*) haalt de bronbestanden op de commit
  uit de kop op en `apps/api/tests/unit/test_peil_kopie.py` faalt bij elke afwijking (drift) of als de bron niet op te
  halen is.
- Niet gekopieerd: de rest van de ontwerptaal (`ontwerptaal.css`, tokens, prototypes, schermafdrukken, bouwscripts):
  deze dienst heeft alleen een statuspagina.

## Controleren

```sh
cd design/ontwerptaal
for f in statuspagina.html statuspagina.css; do echo "$(tail -n +3 "$f" | sha256sum | cut -d' ' -f1)  $f"; done
```

De uitkomst moet gelijk zijn aan de tabel. Tegen de bron: `git -C ../ai-server-manager show
172ee87f15406edbe1a72dda30908abde7488e94:design/ontwerptaal/statuspagina.html | sha256sum`.

## Bijwerken naar een nieuwe Peil-versie

1. Nieuwe bronbestanden op de nieuwe commit ophalen; kop opnieuw schrijven (versie, commit, sha256); tabel hierboven.
2. `PEIL_VERSION` in `apps/api/src/search_ai/peil.py` aanpassen; `make test ui` (axe, 320 px, schermafdrukken).
3. Notitie in `docs/adr/` en `CHANGELOG.md`.
