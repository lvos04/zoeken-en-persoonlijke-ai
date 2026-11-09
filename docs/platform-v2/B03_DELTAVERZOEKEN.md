# B03 — wijzigingsverzoeken aan zoeken-en-persoonlijke-ai

Stand: 2026-11-09 (B03 2.0, inclusief de Emby-besluiten van de gebruiker). Bron van waarheid:
`lvos04/ai-server-manager` → `docs/platform-v2/02_BOUWPLANNEN/B03_UNIVERSELE_MEDIASERVER.md` (hoofdstuk 18
"Deltaverzoeken", §10.16 "Lokale verwerking en optionele modellen") en `B03_BESLUITEN.md`; ADR-voorstel ADR-B03-13
"Optionele model-apps: buiten de image, één producent per soort".

Dit is een **verzoekenlijst, geen bouwwerk**: in deze ronde is geen code gewijzigd. Zoeken (B07/B15) is eigenaar en
beslist; een aanvaard verzoek krijgt een ID in de eigen B07- of B15-reeks (nog niet toegekend). Tot dan werkt Jelly Leon
met de terugval uit de tabel. Alles hieronder valt in P5 (lokale AI): optioneel, standaard uit, met een werkende
klassieke terugval.

## 1. Overzicht van de verzoeken

| ID | Wat zoeken zou moeten doen | Nodig voor (B03) | Besluit | Status 2026-11-09 | Zonder dit (terugval) |
|---|---|---|---|---|---|
| Δ-ZK-1 | B07 `transcript_ref.origin` krijgt de waarde `machine_translation`, zodat machinevertalingen uit Jelly Leon (F3) alleen met herkomst in de index komen | F3 → B07 (B03-049; P5) | geen eigen BG; volgt BG-19 (opslag en label "Machinevertaling"; open, golf 3) en BG-20 (automatisch vertalen standaard uit; open, P5) | open | machinevertalingen gaan niet naar B07; B07 sluit gemarkeerde externe sporen uit |
| Δ-ZK-2 | één canonieke itemtekst en gedeelde vectoren met `text_sha256`, zodat er één producent van itemvectoren voor mediametadata is | F2 P5 (B03-038; JLT-18) | **BG-06 open** (vóór P5); advies: voorlopig geen embeddings; komen ze er, dan maakt Jelly Leon ze en leest zoeken ze, met hetzelfde model | open | geen embeddings in Jelly Leon; aanbevelingen klassiek |
| Δ-CONTRACT-MODEL (eigenaar: ai-server-manager; raakt zoeken) | contract legt vast welke optionele app embeddings en spraakherkenning levert, met **één** ASR-producent voor B03-008, B07-004, F4 (STT-taal) en F6 (transcripten); model-apps standaard zonder GPU of alleen op de UHD 770, nooit op de A310 | B03-059 (JLT-32), B03-079, B03-038; afstemming met B07-004 | **BG-06 open** (vóór P5); advies: één spraakproducent = Jelly Leon | open | klassieke terugval; taal "onbekend" |
| Δ-CONTRACT-MT (eigenaar: ai-server-manager; raakt zoeken alleen indirect) | optionele app `mt` (vertaalmotor, standaard uit) in de apptabel; eventueel samen met het embeddingmodel geleverd | F3 3C (P5) | BG-18 open (vóór P5) | open | geen vertaling; sync en kwaliteitscontrole werken wel |

## 2. Wat dit voor zoeken betekent (gemeten in deze repo, 2026-11-09)

- **Δ-ZK-1:** `transcript_ref.origin` staat nu op `existing` of `asr_opt_in`
  (`apps/api/src/search_ai/search/models.py:379`, migratie `0001_basisschema.py:545-550`, kolom `String(20)`). De nieuwe
  waarde past in de kolom maar vraagt een nieuwe migratie van de controle-constraint. Let op: het plan noemt in het
  F3-hoofdstuk één keer `machine_translated`; hoofdstuk 18 (bindend) zegt `machine_translation`.
- **Δ-ZK-2:** de bronsoort `jellyfin` heeft nu de mogelijkheden `metadata.read`, `transcripts.read` en
  `embeddings.local` (`apps/api/src/search_ai/search/service.py:81-82`). Valt BG-06 op "Jelly Leon maakt de
  itemvectoren", dan leest zoeken die via een gescopete leesroute in plaats van ze zelf te maken; nooit beide.
- **Δ-CONTRACT-MODEL:** B07-004 ("Media scènes en ondertitels") plant nu "off-peak under A310 resource budget". Volgens
  ADR-B03-13 en het contractvoorstel draaien model-apps nooit op de A310 (alleen CPU of de UHD 770) en is er één
  ASR-producent; B07-004 moet daarop worden afgestemd zodra BG-06 valt.
- **Adresregels voor model-apps** (ADR-B03-13): alleen loopback, RFC 1918 of ULA (niet `100.64.0.0/10`), IP-controle per
  verbinding, geen redirects; het geheim van de app in de geheimenmap; nooit een betaalde API.

## 3. Emby en de HARDE EIS

De gebruiker gebruikt nu Emby; de kijkgeschiedenis gaat via B03-015 naar Jelly Leon. Zoeken is daarvoor geen bron en
geen bestemming: zoeken krijgt geen kijkgedrag (B03 §10.9 "geen kijkgedrag naar de Hub, B07 of MediaGuard") en bewaart
geen kijkgeschiedenis. Zoeken heeft geen Emby-bron (bronsoorten `files`, `immich`, `jellyfin`, `logs`) en dit plan voegt
er geen toe; de bronsoort `jellyfin` (metadata en ondertitels) wijst naar Jelly Leon. Het verwijderen van Emby raakt
zoeken dus niet. De HARDE EIS van de gebruiker (kijkgeschiedenis en MediaGuard-voortgang blijven altijd beschikbaar)
legt zoeken geen eigen releasevoorwaarde op, behalve dat zoeken nooit kijkgeschiedenis opslaat, kopieert of wist.

## 4. Rijen in `EXCEL_TAKEN.json` van deze repo

Volgens `EXCEL_TAKEN_PER_REPO.json` (alleen de rijen waarvoor zoeken zelf iets levert):

- **Werkpakketten** (start *Niet gestart*): B03-038 (optionele embeddings, P2; Δ-ZK-2), B03-049 (clientweergave, Hub-kaart
  en connector-itemtypen; Δ-ZK-1), B03-059 (audiotaal met lokale STT, P2; Δ-CONTRACT-MODEL), B03-079 (lokale AI voor
  segmentdetectie, P2; Δ-CONTRACT-MODEL).
- **Testen** (start *Niet gestart*): JLT-18 (embeddings uit, aan en onbereikbaar), JLT-32 (verkeerde talen en ontbrekend
  dialoogspoor, met en zonder STT). Testrijen staan in het tasks.json-formaat zoals de EX-rijen (Soort `Testen`, Fase
  `P6 - E2E`).
- Nooit *Gereed* of *Geslaagd* in deze bouwomgeving. `EVIDENCE.json` is in deze ronde niet aangeraakt.

## 5. Besluiten

| Besluit | Inhoud | Stand |
|---|---|---|
| BG-06 | optionele lokale modellen: welke apps, één producent per soort (embeddings, spraak) | open — vóór P5 |
| BG-18 | vertaalmotor en licentie (`mt`, standaard uit) | open — vóór P5 |
| BG-19 | opslag en label van machinevertalingen | open — golf 3 |
| BG-20 | automatisch vertalen en publiceren | open — P5 |
| BG-37 | MovieLens en Pioneer One alleen lokaal; eenmalige evaluatie op de echte kijkhistorie (raakt de NDCG-drempel van B03-038) | genomen (2026-11-09) |
| HARDE EIS | kijkgeschiedenis en MediaGuard-voortgang altijd beschikbaar | genomen (2026-11-09); voor zoeken: nooit kijkgeschiedenis opslaan |
