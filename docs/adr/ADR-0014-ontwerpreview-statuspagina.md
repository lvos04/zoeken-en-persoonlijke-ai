# ADR-0014 — Ontwerpreview van de statuspagina tegen de echte dienst

**Status:** aanvaard. **Datum:** 2026-11-09. **Contract:** 1.2 §13 (werkwijze: ontwerpreview achteraf door de
design-agent). **Ontwerptaal:** Peil 1.1.0 (ONTWERPTAAL §1, §4.3, §11, §12, §13.2). **Bouwt voort op:** ADR-0013.
**Verslag:** [`docs/ontwerp/PEIL_REVIEW.md`](../ontwerp/PEIL_REVIEW.md).

## Context

ADR-0013 zette `GET /` op het Peil-sjabloon en toetste het in de browser. Die toets had drie beperkingen:

- de toestanden kwamen uit de renderer, niet uit de echte dienst;
- de controle mat alleen axe, overloop en de hoogte van knoppen;
- gemeten contrast, focusdoorloop, woordgebruik en "onbekend nooit groen" ontbraken.

De ontwerpreview achteraf stond nog open.

## Besluiten

1. **Review tegen de echte dienst, herhaalbaar.**
   - `tests/ui/echte_dienst.py` start de API en worker van deze repo, met eigen databases `zoek_ui_echt_*` op de
     gedeelde of de CI-PostgreSQL, naast twee Hub-simulators.
   - `echte_dienst.mjs` brengt elke toestand tot stand zoals een gebruiker dat doet: via het formulier, of via een
     Hub-actie in de simulator.
   - Er zijn 12 toestanden, waaronder de noodpagina zonder sjabloon. `make ui-echt` lokaal; in CI draait dezelfde
     review in de job `statuspagina-ui`, die daarvoor een PostgreSQL-service krijgt.
   - De simulator kan nu ook een koppelcode maken (`/sim/pairing-code`), zoals de Hub-UI. Dat is alleen testcode.
2. **Eén set Peil-controles voor beide routes** (`tests/ui/peilcontrole.mjs`, afgeleid van de referentie
   `bouw/controle.cjs` in ai-server-manager):
   - axe-core met WCAG 2.2 AA en best practices;
   - contrast in de DOM (tekst ≥ 4,5:1, niet-tekst ≥ 3:1) en tekst ≥ 14 px;
   - doelen ≥ 44 × 44 px, met de inline-uitzondering van WCAG 2.5.8;
   - overloop, afkappen en tekstafstand (1.4.12);
   - Tab-doorloop: skiplink eerst, ring ≥ 3:1 met halo, niet bedekt;
   - structuur;
   - status nooit alleen kleur, en niet-gezonde woorden nooit groen;
   - alleen woorden uit de Peil-woordenlijst.

   Een negatieve controle bewijst dat de controles werkelijk falen.
3. **Alleen weergave en tekst repareren, het sjabloon niet aanraken.** De dertien bevindingen in deze repo zijn
   hersteld in `status_page.py`. Dat zijn de teksten, de namen in het dienstblok, het feit *Gereed* per toestand, de
   telling van functies die nog niet beschikbaar zijn (§4.3) en de noodpagina. Er is geen functie, route, veldnaam of
   contractgedrag gewijzigd.

   Bevindingen in het sjabloon zelf (S-01 t/m S-08) gaan als voorstel naar de bron in `ai-server-manager`. De kopie
   blijft byte-gelijk (O-14).
4. **Noodpagina zonder sjabloon met systeemkleuren.**
   - Bij `peil_sjabloon_ongeldig` zijn er geen Peil-kleuren beschikbaar. Er komt geen eigen noodontwerp en er zijn
     geen hardgecodeerde kleuren.
   - De pagina gebruikt `color-scheme: light dark` met `Canvas`/`CanvasText`/`LinkText`, systeemfont, ≥ 14 px,
     links van 44 px, een tweekleurige focus en een selecteerbare Trace-ID.
   - Zo is ze leesbaar in licht en donker. Gemeten: 9,4:1 en 7,84:1, axe 0.
5. **Schermafdrukken in git als palet-PNG.**
   - De volledige set (12 toestanden × 10 combinaties + forced colors) blijft in `build/`.
   - In `docs/screenshots/peil/statuspagina/` staan er 64. De vijf hoofdtoestanden hebben alle tien combinaties; de
     overige toestanden alleen 390 px, licht en donker.
   - Ze zijn verkleind met `pngquant` (kwaliteit 90–100). Tokenkleuren blijven exact (steekproef). Contrast wordt in de
     DOM gemeten, niet op de afbeelding.
   - Zo blijft de repo klein: 5,4 MB voor 64 afdrukken, tegen 6,8 MB voor de 30 afdrukken van eerst.
6. **`make ui` schrijft naar `build/`**, niet meer naar `docs/`. De afdrukken in `docs` komen alleen uit de review
   tegen de echte dienst, zodat er niet twee versies van dezelfde toestand naast elkaar staan.

## Gevolgen

- **Reproduceerbaar:** elke wijziging aan de statuspagina wordt in CI getoetst op de echte dienst, in alle toestanden,
  met de Peil-regels.
- **CI-tijd:** de job `statuspagina-ui` duurt een paar minuten langer (lokaal ongeveer 4 minuten voor de echte dienst).
- **Open in de bron (ai-server-manager):**
  - S-01: koppelblok voor een onleesbare koppelstatus;
  - S-02: tonen `hourglass` en `circle-slash`;
  - S-03: uitlijning van de tijd op 320–390 px;
  - S-04: `role` als plek;
  - S-05: code in mono in de reden;
  - S-06: bron als plek;
  - S-07: "Hub openen" als doel van 44 px.
- **NIET GEVALIDEERD:** echte schermlezer, telefoon, tv en NAS.
