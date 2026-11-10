# Schermafdrukken — statuspagina in Peil 1.1.1, van de echte dienst

Gemaakt met `make ui-echt` (`tests/ui/echte_dienst.py` + `tests/ui/echte_dienst.mjs`) voor de ontwerpreview
([`docs/ontwerp/PEIL_REVIEW.md`](../../../ontwerp/PEIL_REVIEW.md), §11 voor Peil 1.1.1 en contract 1.3). De **echte
dienst** draait lokaal: API en worker van zoeken-en-persoonlijke-ai met eigen PostgreSQL-databases (pgvector), naast
twee Hub-simulators. Chromium (Playwright 1.65.0) bereikt elke toestand zoals een gebruiker dat doet: het
koppelformulier invullen en verzenden, of een Hub-actie via de simulator. Volledige pagina, apparaatschaal 1, tijdzone
Europe/Amsterdam.

Sinds contract 1.3 krijgt een POST op `/koppelen` direct de pagina met de melding en de echte HTTP-status. Zo'n
antwoord is één keer echt opgehaald (elke POST telt als poging) en daarna per breedte en thema met precies dezelfde
bytes en koppen getoond.

**Gegevens zijn fictief (simulator):** Hub-adres `http://127.0.0.1:<poort>`, vingerafdruk, koppelcode, Trace-ID,
testbronnen en nep-shares. Geen echte NAS, Hub of account.

| Toestand | Hoe bereikt | HTTP | Afdrukken |
|---|---|---|---|
| `niet-gekoppeld` | verse dienst, nog nooit gekoppeld | 200 | 320/360/390/768/1024/1440 × licht/donker |
| `veldfout-koppelcode` | formulier met koppelcode `KORT` verzonden | 422 | 390 × licht/donker |
| `koppelen-mislukt` | geldige vorm, maar de Hub kent de code niet (`hub_weigerde`) | 422 | 320/360/390/768/1024/1440 × licht/donker |
| `adres-zonder-koppelen` | de pagina geopend via een domeinnaam (`zoeken.voorbeeld.nl`, in Chromium naar 127.0.0.1 gezet) | 200 | 390 × licht/donker |
| `host-geweigerd` | het formulier via die domeinnaam verzonden (`host_niet_toegestaan`) | 403 | 390 × licht/donker |
| `gekoppeld-melding` | koppelcode uit de Hub-simulator; antwoord met de melding *Gekoppeld met de Hub* (rol `status`) | 200 | 390 × licht/donker |
| `gekoppeld` | daarna, met twee testbronnen, bevestigd en met hartslag van de worker (*Alles gezond*) | 200 | 320/360/390/768/1024/1440 × licht/donker |
| `ontkoppeld` | tweede dienst, koppelt zelf met het token uit de omgeving; daarna ontkoppelt de Hub | 200 | 390 × licht/donker |
| `ingetrokken` | de Hub trekt de koppeling in; de worker merkt het bij de volgende controle | 200 | 320/360/390/768/1024/1440 × licht/donker |
| `niet-gereed` | dienst waarvan de eigen database niet bestaat | 200 | 320/360/390/768/1024/1440 × licht/donker |
| `formulier-verlopen` | formulier verzonden zonder beveiligingscookie | 403 | 390 × licht/donker |
| `te-veel-pogingen` | zes pogingen binnen een kwartier | 429 | 390 × licht/donker |
| `koppelen-bezig` | env-token naar een Hub-adres waar niets luistert: de poging loopt, geen formulier | 200 | 390 × licht/donker |
| `koppelen-bezig-poging` | een POST terwijl die poging loopt (`koppelen_bezig`) | 409 | 390 × licht/donker |
| `koppelstatus-onleesbaar` | onleesbaar `hub-link.json` in `/appdata/secrets`: Storing, geen formulier, melding zonder rol | 200 | 390 × licht/donker |
| `sjabloon-ongeldig` | gewijzigde sjabloonkopie: eerlijke 500 met code en Trace-ID | 500 | 390 × licht/donker |

De volledige set (alle 16 toestanden × 320/360/390/768/1024/1440 × licht/donker, plus forced colors) maakt
`make ui-echt` in `build/echte-dienst/schermafdrukken/`. In git staan de afdrukken als palet-PNG (`pngquant`, kwaliteit
90–100). De tokenkleuren blijven exact, alleen de anti-aliasing heeft minder tussentinten. Contrast is in de DOM
gemeten, niet op deze afbeeldingen.

`CONTROLE.json` is het rapport van dezelfde run: 16 toestanden × 6 breedtes × 2 thema's = 192 controles plus 16 met
forced colors. Elke controle omvat:

- axe-core 4.14.0 (WCAG 2.0/2.1/2.2 A+AA en best practices);
- contrast in de DOM en de kleinste lettermaat;
- doelgrootte, overloop en afkappen;
- de Tab-doorloop met focusring en bedekking;
- vaste woorden, en "onbekend nooit groen";
- CSP, `Referrer-Policy: same-origin` en externe verzoeken;
- op 320 px: tekstafstand (WCAG 1.4.12) en het lettertype van de CI-runner (DejaVu Sans, Peil 1.1.1 E12).

Daarnaast de stappen met de negatieve herkomsttests in Chromium: een formulier van een andere poort op hetzelfde adres
(`Sec-Fetch-Site: same-site`), van een andere site (`cross-site`), uit een sandbox-iframe (`Origin: null`), via een
domeinnaam (Host) en zonder cookie. Elk krijgt 403 en er gaat niets naar de Hub.

Uitkomst: **208 controles, 0 mislukt**; laagste tekstcontrast 8,34:1 (licht) en 7,84:1 (donker, noodpagina zonder
sjabloon), niet-tekst ≥ 4,86:1.

Niet gedaan: een echte schermlezer (NVDA/TalkBack), een echte telefoon of tv en de echte NAS. Die zijn **NIET
GEVALIDEERD**.
