# Schermafdrukken — statuspagina in Peil 1.1.0, van de echte dienst

Gemaakt met `make ui-echt` (`tests/ui/echte_dienst.py` + `tests/ui/echte_dienst.mjs`) voor de ontwerpreview
([`docs/ontwerp/PEIL_REVIEW.md`](../../../ontwerp/PEIL_REVIEW.md)). De **echte dienst** draait lokaal: API en worker
van zoeken-en-persoonlijke-ai met eigen PostgreSQL-databases (pgvector), naast twee Hub-simulators. Chromium
(Playwright 1.65.0) bereikt elke toestand zoals een gebruiker dat doet: het koppelformulier invullen en verzenden, of
een Hub-actie via de simulator. Volledige pagina, apparaatschaal 1, tijdzone Europe/Amsterdam.

**Gegevens zijn fictief (simulator):** Hub-adres `http://127.0.0.1:<poort>`, vingerafdruk, koppelcode, Trace-ID,
testbronnen en nep-shares. Geen echte NAS, Hub of account.

| Toestand | Hoe bereikt | Afdrukken |
|---|---|---|
| `niet-gekoppeld` | verse dienst, nog nooit gekoppeld | 320/360/390/768/1440 × licht/donker |
| `veldfout-koppelcode` | formulier met koppelcode `KORT` verzonden | 390 × licht/donker |
| `koppelen-mislukt` | geldige vorm, maar de Hub kent de code niet (`hub_weigerde`) | 320/360/390/768/1440 × licht/donker |
| `gekoppeld-melding` | koppelcode uit de Hub-simulator; redirect met de melding *Gekoppeld met de Hub* | 390 × licht/donker |
| `gekoppeld` | daarna, met twee testbronnen (1 actief, 1 concept) | 320/360/390/768/1440 × licht/donker |
| `ontkoppeld` | tweede dienst, koppelt zelf met het token uit de omgeving; daarna ontkoppelt de Hub | 390 × licht/donker |
| `ingetrokken` | de Hub trekt de koppeling in; de worker merkt het bij de volgende controle | 320/360/390/768/1440 × licht/donker |
| `niet-gereed` | dienst waarvan de eigen database niet bestaat | 320/360/390/768/1440 × licht/donker |
| `formulier-verlopen` | formulier verzonden zonder geldige beveiligingscookie | 390 × licht/donker |
| `te-veel-pogingen` | zes pogingen binnen een kwartier | 390 × licht/donker |
| `koppelstatus-onleesbaar` | onleesbaar `hub-link.json` in `/appdata/secrets` | 390 × licht/donker |
| `sjabloon-ongeldig` | gewijzigde sjabloonkopie: eerlijke 500 met code en Trace-ID | 390 × licht/donker |

De volledige set (alle 12 toestanden × 320/360/390/768/1440 × licht/donker, plus forced colors) maakt
`make ui-echt` in `build/echte-dienst/schermafdrukken/`. In git staan de afdrukken als palet-PNG (`pngquant`, kwaliteit
90–100). De tokenkleuren blijven exact (steekproef: `#EEF1F5`, `#FBE4E4`), alleen de anti-aliasing heeft minder
tussentinten. Contrast is in de DOM gemeten, niet op deze afbeeldingen.

`CONTROLE.json` is het rapport van dezelfde run: 12 toestanden × 5 breedtes × 2 thema's = 120 controles plus 12 met
forced colors. Elke controle omvat:

- axe-core 4.14.0 (WCAG 2.0/2.1/2.2 A+AA en best practices);
- contrast in de DOM en de kleinste lettermaat;
- doelgrootte, overloop en afkappen;
- de Tab-doorloop met focusring en bedekking;
- vaste woorden, en "onbekend nooit groen";
- CSP en externe verzoeken;
- tekstafstand op 320 px.

Uitkomst: **132 controles, 0 mislukt**.

Niet gedaan: een echte schermlezer (NVDA/TalkBack), een echte telefoon of tv en de echte NAS. Die zijn **NIET
GEVALIDEERD**.
