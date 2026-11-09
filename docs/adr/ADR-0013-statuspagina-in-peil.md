# ADR-0013 — Statuspagina in de ontwerptaal Peil (ZA9, O-14)

Status: aanvaard · Datum: 2026-11-09 · Contract: `contracts 1.2` §3.7 en §13 · Ontwerptaal: Peil **1.1.0**
(`ai-server-manager/docs/platform-v2/ONTWERPTAAL.md` §11.2, §12, §13.2, §14; ADR-0015 van de Hub) · Sluit ZA9 uit
`CONTRACT_1.2_CONFORMITEIT.md`.

## Context
`GET /` was een eigen HTML-pagina met eigen CSS (ADR-0011 liet ZA9 open: de ontwerptaal bestond nog niet).
Inmiddels staan `ONTWERPTAAL.md` en `design/ontwerptaal/` in `lvos04/ai-server-manager` (commit
`172ee87f15406edbe1a72dda30908abde7488e94`). §13.2 schrijft voor alle vijf statuspagina's één framework-vrij sjabloon
voor: `statuspagina.html` (CSS en lucide-symbolen inline, geen JS, past in `default-src 'none'`), met vaste opbouw,
plekken (`{{x}}`, `ALS`/`NIET`/`ELK`), toonwaarden uit een vaste lijst en een versie-gepinde kopie per repo (O-14) die
CI tegen de bron controleert.

## Besluiten

1. **Kopie, niet nabouwen.** `design/ontwerptaal/statuspagina.html` en `statuspagina.css` zijn byte-gelijke kopieën
   van de bron op commit `172ee87f…`, met een herkomstkop van twee regels: versie `1.1.0`, bronpad, commit en de
   sha256 van het bronbestand (`HERKOMST.md`). De dienst rendert **het sjabloon zelf** (geen eigen HTML of CSS meer).
   - `search_ai/peil.py` leest de kopie, controleert bij het laden dat de inhoud precies de sha256 uit de kop heeft
     en weigert een andere versie dan 1.1.0. De renderer doet hetzelfde als de referentie `bouw/sjabloon.mjs`
     (eerst `ELK`, dan `ALS`, dan `NIET`, dan tekstplekken, altijd `html.escape`); een test vergelijkt beide byte voor
     byte op alle toestanden (lokaal, met de naastgelegen bron en Node). Na het vullen gaat het uitleg-commentaar
     van het sjabloon niet mee naar de browser; vóór het vullen strippen kan niet (de uitleg noemt zelf de plekken).
   - In de image staat de kopie in `/usr/local/lib/search-ai/peil/` (`SEARCH_AI_PEIL_DIR`); de bouw controleert de
     sha256 al (`load_template` in de Dockerfile), de rooktest ook via `GET /`.
   - Is de kopie toch onbruikbaar (ontbreekt, gewijzigd), dan geeft `GET /` een eerlijke, minimale 500 met code
     `peil_sjabloon_ongeldig` en Trace-ID; API, `/healthz` en `/readyz` werken door. Nooit een eigen noodontwerp.
2. **Drift-controle in CI (O-14, §14).** Stap *Peil-bron op de gepinde commit ophalen* leest de commit uit de kop,
   haalt beide bronbestanden van `raw.githubusercontent.com/lvos04/ai-server-manager/<commit>/…` en zet
   `PEIL_BRON_DIR` + `PEIL_BRON_VERPLICHT=1`; `tests/unit/test_peil_kopie.py` faalt bij elk verschil, en ook als de bron
   niet op te halen is (nooit stil overslaan in CI). Lokaal gebruikt de test `git show <commit>:…` in de naastgelegen
   checkout. Een nieuwere Peil-versie in de bron laat CI **niet** falen: de kopie is bewust gepind; bijwerken = nieuwe
   kopie + nieuwe kop + ADR-notitie. (De bron staat op de werkbranch van `ai-server-manager`, niet op `main`; een
   commit-SHA is onveranderlijk, een branchnaam niet.)
3. **Koppelen volgens contract 1.2 §3.7 in het sjabloon.** Precies één koppeltoestand: `koppelformulier` (alleen als
   `first_linked_at` leeg is en het koppelbestand leesbaar), `gekoppeld`, `ontkoppeld` of `ingetrokken`. Het
   formulier heeft de drie verplichte velden van het sjabloon; de **veldnamen** van `POST /koppelen` volgen het
   sjabloon: `hub_adres`, `koppelcode`, `hub_vingerafdruk` (was `hub_url`, `code`, `fingerprint`; alleen de
   statuspagina zelf post hierheen). Geen ontkoppelen, geen ontkoppelcode. CSRF, Origin-controle, throttle (5 per bron-IP
   en 20 per dienst per 15 min) en "één poging tegelijk" blijven ongewijzigd. Bij een koppelstatus die onleesbaar is,
   toont het koppelblok geen van de vier (het sjabloon kent die toestand niet); de melding en de controle-regel zeggen
   dan "Koppelstatus onleesbaar" met de code.
4. **Foutweergave met code + kopieerbare Trace-ID zonder JS en zonder weerspiegelde invoer.** De pagina houdt
   Post/Redirect/Get aan. Bij een foutmelding bewaart de dienst de Trace-ID van díe poging (dezelfde als de
   `X-Trace-Id`-kop en het auditspoor `hub.pair_failed`) in het geheugen onder een willekeurige sleutel
   (`secrets.token_urlsafe(16)`, 15 min, hooguit 256 stuks) en stuurt door naar `/?melding=<code>&poging=<sleutel>`.
   De Trace-ID komt dus nooit uit de URL; een verzonnen of verlopen sleutel, of een sleutel van een andere melding,
   toont geen Trace-ID. Meldingen komen uit de vaste lijst `NOTICES` (titel, tekst, eventueel veld + veldfout); de
   veldfout zet `aria-invalid`, `aria-describedby`, een link in de melding en de skiplink naar het veld. Het ingevulde
   Hub-adres van een mislukte poging komt nergens terug, ook niet in het formulier. Gekozen boven het POST-antwoord
   direct renderen (dan dubbele POST bij verversen) en boven de Trace-ID in de URL (weerspiegelde invoer).
5. **Eerste signaal nooit groen als niet gereed (§13.2).** Totaal = slechtste van de eigen controles (`/readyz`:
   database, schema, pgvector, sleutels) en de koppeling. Niet gemeten (schema/pgvector zonder database) is
   *Onbekend*, nooit *Gezond*. Zolang de dienst niet gekoppeld, ontkoppeld of ingetrokken is: label **Niet gereed**,
   toon *Onbekend* ("Deels onbekend — wacht op koppeling met de Hub"), ook al geeft `/readyz` 200 (dat blijft zo: de
   TrueNAS-gezondheidscontrole mag een ongekoppelde dienst niet herstarten). Het feit *Gereed* zegt dat expliciet.
   *Alles gezond* alleen als alles gemeten en goed is **én** de connector-gezondheid (wat de Hub ziet) geen afwijking
   heeft: afwijkingen daarvan (koppeling niet bevestigd, outbox vast, opschonen na intrekking open, gepinde sleutels
   ongeldig) worden een eigen regel met code, zodat de statuspagina nooit groener is dan de Hub.
6. **Toonwaarden en woorden uit Peil.** Alleen `gezond | beperkt | storing | onbekend | gate`. "Gekoppeld" is de
   neutrale pil van het sjabloon, nooit groen; er is dan geen groene regel "Koppeling met de Hub". Bronnen (B07) zijn
   `gate` ("Alleen met toestemming": beleid, geen gezondheid). Ingest en spraak zijn "Nog niet beschikbaar — volgt in
   fase P5" met `capability_unavailable`, als *Onbekend* (het sjabloon heeft geen `hourglass`-symbool); ze tellen niet
   mee in het totaal (ONTWERPTAAL §4.3). Tijden in nl-NL ("9 nov 2026, 11:56") in de tijdzone van de container; is
   dat UTC, dan staat "UTC" erbij.
7. **Browsercontrole in Node, rest in Python.** `tests/ui/` gebruikt dezelfde versies als de ontwerptaal-controle
   (Playwright 1.65.0, axe-core 4.14.0, `package-lock.json`). `render_toestanden.py` rendert de toestanden met de echte
   renderer; `statuspagina.mjs` serveert ze met de echte beveiligingskoppen en toetst per toestand × 320/360/390/768/
   1440 × licht/donker: axe (WCAG 2.0/2.1/2.2 A+AA en best practices) 0 overtredingen, geen horizontale overloop, geen
   `<script>`/`on…`, geen CSP-schending, geen extern verzoek, knoppen/velden ≥ 44 px. axe wordt via het
   DevTools-protocol ingespoten (valt niet onder de CSP), zodat de pagina zelf met `default-src 'none'` getest wordt.
   Eigen CI-job `statuspagina-ui`; schermafdrukken (360/768/1440 × licht/donker) in
   `docs/screenshots/peil/statuspagina/` met `CONTROLE.json`.

## Gevolgen
- ZA9 is gebouwd. Geen eigen CSS meer in `status_page.py`; een Peil-update is een nieuwe kopie, geen codewijziging.
- Contractvraag (gemeld): §13.2 noemt "gereed" zonder te zeggen of een ongekoppelde dienst gereed is; `/readyz` en
  de statuspagina verschillen daarom bewust (zie besluit 5). Het sjabloon kent geen toestand voor een onleesbaar
  koppelbestand en geen `hourglass` voor "Nog niet beschikbaar" (besluiten 3 en 6).
- Niet gedaan: echte schermlezer-, telefoon- en tv-test (ONTWERPTAAL §15 punt 7) en een test op een echte NAS:
  **NIET GEVALIDEERD**. De ontwerpreview achteraf door de design-agent (contract §13 werkwijze) is nog niet gebeurd.
- Aanvulling 2026-11-09: de ontwerpreview achteraf is gedaan tegen de echte dienst; zie ADR-0014 en
  `docs/ontwerp/PEIL_REVIEW.md`. `make ui` schrijft sindsdien naar `build/`; de afdrukken in `docs/` komen uit
  `make ui-echt`.
