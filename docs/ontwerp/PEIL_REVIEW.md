# Ontwerpreview — statuspagina van zoeken-en-persoonlijke-ai (Peil 1.1.0)

**Wat:** onafhankelijke ontwerpreview achteraf (contract 1.2 §13, werkwijze) van `GET /`, in alle toestanden.
**Door:** de design-agent. **Datum:** 9 november 2026. **Repo:** `lvos04/zoeken-en-persoonlijke-ai`, branch
`ccr-c133d7a0-kuz1cv`.

**Getoetst tegen (bindend):**

- `ai-server-manager/docs/platform-v2/ONTWERPTAAL.md` 1.1.0;
- `design/ontwerptaal/COMPONENTEN.md` §4, §5, §12, §13, §14 en §25;
- ADR-0015 en de besluiten van de gebruiker (productnaam "TrueNAS Home Platform");
- de prototypes `statuspagina.html` en `statuspagina-gekoppeld.html` met hun `shots/`, allemaal op commit `172ee87f`.

## 1. Samenvatting

De statuspagina gebruikt het Peil-sjabloon correct en eerlijk:

- de status staat bovenaan;
- het totaal is nooit groen zolang de dienst niet gereed of niet gekoppeld is;
- "Gekoppeld" is een neutrale pil;
- onbekend en niet gemeten zijn gestippeld grijs, met een reden erbij;
- elke status heeft een icoon, een woord en een vorm.

Gemeten op de **echte dienst** in 12 toestanden, op 320/360/390/768/1440 px, in licht en donker:

- axe-core: 0 overtredingen;
- alle tekst ≥ 7:1 (AAA);
- randen, iconen en focus ≥ 3:1;
- geen overloop op 320 px, ook niet met ruimere tekstafstand;
- doelen ≥ 44 px;
- de skiplink is de eerste tabstop en de focus is nooit bedekt.

De review vond **14 punten in deze repo**. Alle 14 zijn hersteld (§5). Het ging vooral om tekst en eerlijkheid:

- interne taak-ID's stonden op de pagina;
- er stonden woorden die Peil vervangen heeft;
- het feit "Gereed" gaf soms de verkeerde reden;
- de telling van functies die nog niet beschikbaar zijn ontbrak;
- er was een noodpagina zonder kleurschema.

Daarnaast zijn er **8 punten in het sjabloon zelf** (§6). Die kunnen hier niet worden opgelost: de kopie is byte voor
byte vastgezet (O-14). Ze gaan als voorstel naar de bron in `ai-server-manager`.

**Niet gevalideerd:** een echte schermlezer (NVDA/TalkBack), een echte Android-telefoon in fel licht, een tv en de echte
NAS (§8).

## 2. Werkwijze

1. **Echte dienst lokaal, met testdata** (`make ui-echt` = `tests/ui/echte_dienst.py` + `echte_dienst.mjs`):
   - API en worker van deze repo, met eigen PostgreSQL-databases (pgvector);
   - twee Hub-simulators (`tests/e2e/sim_hub.py`);
   - nep-shares en twee testbronnen (1 met toestemming, 1 concept).

   Chromium bereikt elke toestand zoals een gebruiker:

   - het formulier invullen met het toetsenbord en met Enter verzenden;
   - een koppelcode uit de Hub-simulator;
   - ontkoppelen met een dienst-JWT en intrekken door de Hub;
   - een dienst waarvan de database niet bestaat;
   - een onleesbaar `hub-link.json`;
   - een gewijzigde sjabloonkopie.

2. **Matrix:** 12 toestanden × 320/360/390/768/1440 × licht/donker = 120 controles, plus forced colors (12 controles).
   De volledige set schermafdrukken staat in `build/`. Een selectie van 64 staat in
   [`docs/screenshots/peil/statuspagina/`](../screenshots/peil/statuspagina/LEESMIJ.md).

3. **Controles per pagina** (`tests/ui/peilcontrole.mjs`, afgeleid van de Peil-referentie `bouw/controle.cjs`):

   | Onderdeel | Wat er getoetst wordt |
   |---|---|
   | axe-core 4.14.0 | WCAG 2.0/2.1/2.2 A+AA en best practices |
   | Contrast | in de DOM, met alle vlakken samengesteld: tekst ≥ 4,5:1, iconen/randen/veldranden/knopvlakken ≥ 3:1 |
   | Tekst | ≥ 14 px |
   | Doelen | ≥ 44 × 44 px; inline links in lopende tekst zijn uitgezonderd (WCAG 2.5.8) |
   | Overloop | geen overloop van de pagina, geen inhoud buiten zijn eigen vak, geen ellips; ook met tekstafstand (WCAG 1.4.12) op 320 px |
   | Toetsenbord | Tab-doorloop: skiplink eerst, tweekleurige ring ≥ 3:1, element zichtbaar en niet bedekt |
   | Structuur | één h1, één main, geen kopsprongen, geen dubbele id's, geen kapotte `aria-*`/`href="#…"`/`use` |
   | Status | nooit alleen kleur (icoon + woord), niet-gezonde woorden nooit in de groene stijl |
   | Woorden | alleen woorden uit de Peil-woordenlijst (§11.1), geen uitroeptekens |
   | Veiligheid | geen JavaScript, CSP `default-src 'none'` zonder schending, geen extern verzoek |

   **Negatieve controle:** een opzettelijk foute pagina geeft wel FOUT. Gemeld werden: grijze tekst 2,55:1, veldrand
   1,46:1, links van 21 px hoog, een pil zonder icoon, "Niet gekoppeld" in groen, "in orde" en een uitroepteken,
   focus zonder ring, en de axe-meldingen `color-contrast` en `target-size`.

4. **Skills gebruikt:** design-critique, accessibility-review, ux-copy, design-system, design-handoff en dataviz. De
   dataviz-skill is doorlopen vóór de keuze van de vorm: **geen grafiek**. De pagina toont een paar losse aantallen
   ("1 actief, 1 concept") en controles met een status. Dat is tekst, geen grafiek, meter of stattegel (ONTWERPTAAL
   §10, "één huidige waarde → geen grafiek"). Het sjabloon heeft ook geen grafiekplek.

5. **Vergeleken** met de prototype-afdrukken `statuspagina-*-{360,390,768,1440}.png` en
   `statuspagina-gekoppeld-*.png`.

## 3. Meetuitkomst (`CONTROLE.json`, eindstand)

| Meting | Licht | Donker | Eis |
|---|---|---|---|
| axe-overtredingen (132 controles) | 0 | 0 | 0 |
| Laagste tekstcontrast (statuspagina) | 8,34:1 (link) | 9,32:1 (knoptekst) | ≥ 4,5:1 (Peil-doel ≥ 7:1) |
| Laagste tekstcontrast (noodpagina, systeemkleuren) | 9,4:1 | 7,84:1 | ≥ 4,5:1 |
| Laagste niet-tekstcontrast | 4,86:1 (stippelrand Trace-ID-blok op het foutvlak) | 5,69:1 | ≥ 3:1 |
| Laagste focusring | 7,79:1 | 8,04:1 (noodpagina 7,84:1) | ≥ 3:1, 3 px + halo |
| Kleinste lettermaat | 14 px | 14 px | ≥ 14 px |
| Horizontale overloop en inhoud buiten zijn vak (ook 320 px en met tekstafstand) | 0 | 0 | 0 |
| Forced colors: stippelrand van Onbekend | blijft gestippeld (12 van 12) | | |

**Toetsenbord na een fout:**

- de eerste tabstop is "Naar de koppelcode (fout)";
- Enter zet de focus op `#koppelcode`, met `aria-invalid="true"` en `aria-describedby="code-hulp code-fout"`;
- de velden zijn na de fout leeg (er wordt niets teruggezet);
- de paginatitel noemt de fout: "Koppelen is mislukt · niet gereed, niet gekoppeld".

Een schermlezer leest die titel bij het laden. Dat is belangrijk, omdat een `role="alert"` die er al bij het laden
staat niet overal wordt voorgelezen.

**Inline links** (uitzondering WCAG 2.5.8, toegestaan in Peil §6): "Naar het veld Koppelcode" in de melding en "Hub
openen" in het koppelblok, beide 22 px hoog in lopende tekst.

## 4. Vergelijking met de Peil-prototypes

| Onderdeel | Prototype | Deze dienst | Oordeel |
|---|---|---|---|
| Kop | "Onderdeel van TrueNAS Home Platform" + h1 + één korte zin | idem; zin was 3 regels met dubbele boodschap (P-10) | hersteld |
| Totaal niet gekoppeld | "Niet gereed" (stippel) + "Deels onbekend — wacht op koppeling met de Hub." | identiek | goed |
| Totaal gekoppeld | pil zonder vlak + toelichting | "Alles gezond", maar zonder telling van wat nog niet beschikbaar is (P-07) | hersteld |
| Controles | naam · pil · tijd · reden | idem; op 320–390 px valt de tijd soms onder de naam (S-03, ook in het prototype) | sjabloon |
| Koppelblok gekoppeld | neutrale pil `link-2`, Sinds/Hub/Vingerafdruk, "Hub openen" | identiek | goed |
| Foutmelding | "De Hub weigerde de koppelcode: verlopen, al gebruikt of ongeldig. Maak in de Hub een nieuwe koppelcode en probeer het opnieuw." | noemde ook "het koppeltoken" (P-04) | hersteld |
| Veldfout | rood, `octagon-x`, onder het veld, `aria-invalid` | identiek | goed |
| Dienstblok | "Wat deze dienst doet", namen in gewone taal | taak-ID's "(B07)", "(B15)" en "Leeswortels" (P-01, P-03) | hersteld |
| Nog niet beschikbaar | §4.3: `hourglass`, neutrale pil | toon onbekend (`circle-help`, stippel): het sjabloon kent geen `hourglass` (S-02) | sjabloon |
| Voet | /healthz, /readyz (44 px), versie | identiek | goed |

## 5. Bevindingen en reparaties in deze repo

Ernst: **Hoog** (misleidend of ontoegankelijk), **Middel** (wijkt af van een bindende Peil-regel), **Laag** (helderheid of
consistentie). Alleen uiterlijk, teksten en toegankelijkheid. Er is geen functie, route, veldnaam of contractgedrag
gewijzigd.

| # | Bevinding | Ernst | Regel | Reparatie | Getoetst door |
|---|---|---|---|---|---|
| P-01 | Interne taak-ID's op de pagina: "Bronnen (B07)", "Indexeren en semantisch zoeken (B07)", "Spraak en persoonlijke AI (B15)". Voor de gebruiker betekenisloos. | Middel | §11 (toon, gewone taal), §1.8 | Namen zonder ID | `test_review_geen_interne_ids_en_alleen_peil_woorden` (8 toestanden) |
| P-02 | "de eigen controles van /readyz zijn **in orde**": "In orde" is door Peil vervangen door *Gezond*. | Middel | §11.1 vaste woorden | "… zijn gezond" | idem + woordcontrole in `peilcontrole.mjs` |
| P-03 | "Leeswortels" is vakjargon (ADR-0004). Zonder share stond er de pil "Geen", geen Peil-woord. | Middel | §11.1, §4.1 regel 12 | "Gekoppelde shares", "2 geldige shares", "Niet ingesteld" (onbekend-stijl, nooit groen); de hint "Een share toevoegen: in TrueNAS als Host Path met Read Only onder /bronnen." | `test_review_geen_share_is_niet_ingesteld_en_nooit_groen` |
| P-04 | De melding `hub_weigerde` zei "De Hub weigerde het **koppeltoken of** de koppelcode", maar de gebruiker vulde alleen een koppelcode in. | Middel | §11.2, prototype | Tekst van het prototype; `ERROR_MESSAGES` blijft de algemene tekst voor `last_error` in de API | `test_review_hub_weigerde_noemt_de_koppelcode_en_herhaalt_niet` |
| P-05 | Dezelfde fout stond drie keer op de pagina: melding, veldfout en "Laatste melding" bij de controle. | Laag | §2 (status eerst, rustig) | De controle herhaalt de laatste melding niet als de melding bovenaan dezelfde code toont; zonder melding blijft ze staan | idem |
| P-06 | "Gereed: Nee — wacht op koppeling met de Hub" ook bij *ontkoppeld*, *ingetrokken* en *koppelstatus onleesbaar*. De dienst wacht dan niet. | Middel | §1.1 eerlijk | De echte reden per toestand | `test_review_gereed_noemt_de_echte_reden` (4 toestanden) |
| P-07 | "Alles gezond", terwijl twee functies nog niet beschikbaar zijn, zonder dat te tellen. | Middel | §4.3 ("staan wel in de telling") | "Gereed en gekoppeld met de Hub. 2 functies zijn nog niet beschikbaar." Het totaal blijft Gezond, want nog niet beschikbaar verslechtert het niet. | `test_review_nog_niet_beschikbaar_staat_in_de_telling` |
| P-08 | Meldingen herhaalden hun titel ("Te veel pogingen. Te veel pogingen …") of misten *wat je kunt doen* (`hub_antwoord_ongeldig`, `enroll_endpoint_ontbreekt` met "aanmelden" in plaats van "koppelen"). | Laag | §11 (wat, waarom, wat je kunt doen), C-14 | Elke melding volgt dat patroon en zegt of er iets naar de Hub is gestuurd | `test_elke_melding_rendert_met_vaste_tekst` |
| P-09 | De toelichting bij ontkoppeld/ingetrokken herhaalde "Opnieuw koppelen gaat via de Hub", dat ook al in het koppelblok staat. | Laag | §2 | Korter: "Deels onbekend — ontkoppeld door de Hub." | `test_ontkoppeld`, `test_ingetrokken` |
| P-10 | De kop-omschrijving liep over 3 tot 4 regels; "Alle schermen staan in de Hub" stond 3 keer op de pagina. | Laag | §2, prototype | "Persoonlijke zoekmachine voor je eigen bestanden op de NAS; spraak en persoonlijke AI volgen in fase P5. Dienst-ID in de Hub: zoeken-en-persoonlijke-ai." | schermafdrukken |
| P-11 | Koppelstatus onleesbaar: de melding zei niet *waarom* de dienst weigert of *waar* je opnieuw koppelt; de paginatitel herhaalde "koppelstatus onleesbaar". | Laag | §11.2, C-14 | De melding legt de fail-closed-weigering uit + "Instellingen → Diensten"; een korte reden bij de controle; de titel zonder herhaling | `test_review_paginatitel_herhaalt_niets` |
| P-12 | De noodpagina zonder sjabloon (500 `peil_sjabloon_ongeldig`) had geen kleurschema, code van 13 px, geen selecteerbaar Trace-ID-blok en geen 44 px-links. | Middel | §5 (≥ 14 px), §13.2 (Trace-ID in een selecteerbaar blok), §6 | Alleen systeemkleuren (`Canvas`/`CanvasText`/`LinkText`, licht en donker, geen eigen hex), systeemfont, code ≥ 14 px, Trace-ID-blok, links van 44 px, tweekleurige focus. Tekst volgens wat/waarom/wat je kunt doen. | `test_gewijzigde_kopie_geeft_eerlijke_500_en_de_api_werkt_door`; live: 0 axe, licht 9,4:1 / donker 7,84:1 |
| P-13 | De browsercontrole mat alleen de hoogte van knoppen en velden. Er was geen gemeten contrast, focus, woordcontrole of "nooit groen", en ze draaide niet tegen de echte dienst. | Middel | §12 testmatrix | `peilcontrole.mjs` voor beide routes; `make ui-echt`; CI-job `statuspagina-ui` draait ook de review tegen de echte dienst (met PostgreSQL) | negatieve controle (§2) |
| P-14 | Bij een onleesbare koppelstatus stond "(code koppelstatus_onleesbaar)" in de melding. Met ruimere tekstafstand op 320 px liep die code buiten het meldingvak: lokaal 25 px, maar nog binnen de pagina; op de CI-runner, met een ander lettertype, ook 4 px buiten de pagina. Gevonden in de eerste CI-run van de review (run 42662368284). | Middel | WCAG 1.4.12, §6 (geen overloop op 320 px) | De code staat alleen nog bij de controle "Koppeling met de Hub" (breder vak). De controle meldt nu ook inhoud buiten zijn eigen vak, in beide routes. Ook getoetst met DejaVu Sans en Liberation Sans: geen overloop. | `make ui` (90 controles) en `make ui-echt` (132); CI |

## 6. Bevindingen in het sjabloon (bron: `ai-server-manager/design/ontwerptaal/statuspagina.html`)

Deze punten gelden voor alle vijf statuspagina's. Hier zijn ze niet te repareren: de kopie is byte-gelijk en CI faalt
bij drift (O-14). Voorstel voor Peil 1.1.x/1.2.0:

| # | Bevinding | Ernst | Voorstel |
|---|---|---|---|
| S-01 | Er is geen koppelblok voor een **onleesbare koppelstatus**. De kaart "Koppeling met de Hub" blijft dan leeg (alleen de kop). De dienst vangt dit op met een melding bovenaan. | Middel | Blok `<!--ALS:koppelstatus_onleesbaar-->`: pil storing + `link-2-off` + vaste tekst "Opnieuw koppelen gaat via de Hub". |
| S-02 | Er is geen toon voor **Nog niet beschikbaar** (`hourglass`, neutraal) en geen toon voor **Niet beschikbaar** (`circle-slash`), hoewel §4.3 die voorschrijft. Deze dienst gebruikt daarom toon onbekend (nooit groen, wel het verkeerde icoon). | Middel | Tonen `later` (hourglass, `muted`, `border-strong`) en `niet-beschikbaar` (circle-slash, stippel) in de vaste lijst, met symbolen. |
| S-03 | In `.checks` heeft de naam `flex: 1 1 10rem`. Op 320–390 px valt de tijd onder de naam zodra de pil breed is (Onbekend, Niet gekoppeld, Ingetrokken), zodat de rijen ongelijk worden. Dit zit ook in het prototype ("TrueNAS-schrijfsleutel"). | Laag | `.checks .naam{flex:1 1 8rem}` + `.checks .tijd{margin-left:auto}`, of een raster naam/pil/tijd. |
| S-04 | `role="alert"` staat vast op elke melding, ook op de info-melding (*Gekoppeld met de Hub*) en op blijvende toestanden. C-14: alert alleen bij een fout die net is ontstaan; info is `role="status"`. | Laag | `role` als plek (`{{melding_rol}}`: `alert` bij een fout na verzenden, anders `status`). |
| S-05 | De plek `reden` is alleen tekst. Codes (`capability_unavailable`, `hub_weigerde`) kunnen daardoor niet in mono, terwijl §5/§11.2 "Code `x`" voorschrijven. | Laag | Optionele plek `{{reden_code}}` → `<code>`. |
| S-06 | De vaste tekst "Bron: /readyz en /healthz van deze dienst" is smaller dan wat het totaal meeneemt (ook de connector-gezondheid). | Laag | Plek `{{bron}}`. |
| S-07 | "Hub openen" is een actie, maar staat als inline link van 22 px aan het eind van een zin. Dat mag (WCAG 2.5.8), maar het is klein voor een duim. | Laag | Losse secundaire knop of link van 44 px (zoals de voetlinks). |
| S-08 | In forced colors krijgt ook de Gezond-pil een rand (`CanvasText`). Dat is aanvaardbaar: vorm en woord blijven, en Onbekend houdt zijn stippel. | Info | Geen. |

## 7. Wat goed is (behouden)

- **Eerlijk:**
  - het totaal is in geen enkele toestand groen zolang de dienst niet gereed is (getoetst op 12 toestanden);
  - "Gekoppeld" is neutraal;
  - schema en pgvector zonder database zijn "Onbekend — niet te controleren zonder database", nooit groen of 0;
  - het aantal bronnen zonder database is "onbekend", nooit 0.
- **Één betekenis per vorm:**
  - gezondheid = ronde pil;
  - toestemming = hoekige gate-tag met slot;
  - fout = rood vlak met `octagon-x`;
  - info = blauw.
- **Formulier volgens Peil:**
  - het label staat boven het veld, met hulptekst via `aria-describedby`;
  - een veldfout staat bij het veld;
  - de skiplink en de melding wijzen naar het foute veld;
  - `autocomplete=off`;
  - waarden worden na een fout niet teruggezet.
- **Robuust:** geen JavaScript; werkt binnen `default-src 'none'`; leesbaar op 320 px en bij ruimere tekstafstand.

## 8. Niet gevalideerd / open

- **NIET GEVALIDEERD:**
  - echte schermlezer (NVDA, TalkBack);
  - echte Android-telefoon in fel licht;
  - tv;
  - de pagina achter de poortdoorsturing van een echte TrueNAS (bron-IP, `TZ`, Custom App).

  NAS-gegevens D01–D18 zijn onbekend. In de tests zijn alle adressen en codes fictief.
- **Forced colors:** Playwright emuleert alleen de mediaquery, niet het systeempalet. De stippelrand is getoetst, het
  echte Windows-hoogcontrast niet.
- **Sjabloonpunten S-01 t/m S-07:** voorstel voor de bron in `ai-server-manager`. Elders wijzigen mag niet (eigen repo).

## 9. Handoff: toestanden en plekken

Voor de andere vier statuspagina's (home-control-hub, appstore-en-fork, jelly-leon-platform, executor). De volgorde is
steeds: kop → melding → Status → Koppeling met de Hub → Koppelen → dienstblok → voet.

| Toestand | `totaal_toon` · label | Toelichting (begin) | Koppelblok | Melding (`role=alert`) | Skiplink | `<title>` (na "Dienstnaam — ") |
|---|---|---|---|---|---|---|
| nooit gekoppeld | onbekend · Niet gereed | "Deels onbekend — wacht op koppeling met de Hub." | `koppelformulier` | — | Naar de inhoud | niet gereed, niet gekoppeld |
| veldfout / Hub weigert | onbekend · Niet gereed | idem | `koppelformulier` + `fout_<veld>` | fout + code + Trace-ID + link naar het veld | Naar de <veld> (fout) | Koppelen is mislukt · niet gereed, niet gekoppeld |
| verlopen formulier / te veel pogingen | (volgt de controles) · Niet gereed | idem | `koppelformulier` | fout + code + Trace-ID | Naar de inhoud | <titel melding> · … |
| gekoppeld | gezond · Alles gezond (of beperkt · Beperkt) | "Gereed en gekoppeld met de Hub. n functies zijn nog niet beschikbaar." | `gekoppeld` (neutrale pil) | info alleen direct na koppelen | Naar de inhoud | alles gezond, gekoppeld |
| ontkoppeld / ingetrokken | onbekend · Niet gereed | "Deels onbekend — ontkoppeld door de Hub." | `ontkoppeld` / `ingetrokken` | — | Naar de inhoud | niet gereed, ontkoppeld |
| eigen controle faalt | storing · Niet gereed | "Storing in: Eigen database. Ook nog niet gekoppeld met de Hub." | volgens de koppeling | — | Naar de inhoud | niet gereed, … |
| koppelstatus onleesbaar | storing · Niet gereed | "Storing — de koppelstatus is onleesbaar." | (geen blok, S-01) | fout zonder Trace-ID | Naar de inhoud | niet gereed, koppelstatus onleesbaar |
| sjabloon onbruikbaar | — (500, noodpagina) | — | — | — | — | statuspagina niet beschikbaar |

Woorden op pillen (vaste lijst in `peilcontrole.mjs`): Gezond · Beperkt · Storing · Onbekend · Alles gezond · Deels
onbekend · Niet gereed · Gekoppeld · Niet gekoppeld · Ontkoppeld · Ingetrokken · Onleesbaar · Nog niet beschikbaar ·
Niet beschikbaar · Niet ingesteld · Alleen met toestemming.

## 10. Reproduceren

```sh
cd tests/ui && npm ci && cd ../..        # Playwright 1.65.0 + axe-core 4.14.0 (package-lock.json)
make ui                                  # toestanden uit de renderer, zonder database (build/statuspagina)
make ui-echt                             # echte dienst + simulators + PostgreSQL → docs/screenshots/peil/statuspagina
```

Vereist voor `ui-echt`: PostgreSQL met pgvector op `127.0.0.1:5432` (gebruiker `postgres`, databases `zoek_ui_echt_*`;
andere server via `SEARCH_AI_TEST_ADMIN_URL`).
