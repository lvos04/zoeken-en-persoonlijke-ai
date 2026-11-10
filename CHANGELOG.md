# Wijzigingen

## 0.1.0 — nog niet uitgebracht (fase P0)

Eerste versie van de dienst *Zoeken en persoonlijke AI* (TrueNAS Home Platform V2).

- **Dienstkern:** FastAPI + SQLAlchemy 2 + Alembic op een ingebouwde PostgreSQL 16 met pgvector 0.8.7; authenticatie
  met Hub-JWT (JWKS, gecachet, fail-closed) en service-token; koppelen met eenmalig enrollment-token of koppelcode;
  foutformaat met trace-id; Idempotency-Key; healthz/readyz; connectorcontract; outbox naar de Hub; NL-statuspagina.
- **B07-001 Bronselectie en toestemming:** lees-padenallowlist (leeswortels, paden per bron, vaste uitsluitingen,
  geen symlinks naar buiten), consent-register met scopes gebonden aan een momentopname, toestemming alleen door de
  gegevenseigenaar, gebruikers-ACL vóór ranking, kinderprofiel-beperkingen, intrekken met gecontroleerde purge van
  index en embeddings (originelen ongemoeid).
- **Datamodel B07 en B15** als migratie 0001 (logica van B07-002…006 en B15 volgt in P5).
- **Installatie:** één alles-in-één image als standaard TrueNAS Custom App (`deploy/truenas/CUSTOM_APP.md`), manifest
  v1, CI met rooktest van de image, release met SBOM en digest.

- **Review P0 (onafhankelijk):** migratie 0002 met samengestelde bron-sleutels (chunk/transcript/embedding kunnen
  niet aan een andere bron hangen dan hun ouder); vectorzoeker met gebruikers-ACL vóór de afstands-ranking (intern en
  getest, ook met HNSW; de API voor semantisch zoeken blijft 501); intrekken wist ook bewaarde zoekgeschiedenis en
  trekt spraakcontext op de bron in; de back-up vóór een upgrade bevat geen indexinhoud, zoekgeschiedenis of
  transcripten meer; verzoeklimiet (429) en tijdlimiet (503) op zoeken; database-socket alleen voor 568; rooktest
  toetst pgvector echt en databehoud na herstart, harde kill en nieuwe container (ADR-0008).
- **P0 afmaken (CI groen, ADR-0009):** de rooktest bekijkt `/appdata/secrets` (bewust `0700/568`) via een
  alleen-lezen wegwerpcontainer in plaats van als host-gebruiker, zodat hij ook op de niet-root GitHub-runner werkt;
  testmappen zijn deterministisch van root; de bewaker geeft een duidelijke Nederlandse fout als de app als andere
  gebruiker dan root of 568 start (*Custom User*), in plaats van een Engelse `initdb`-melding.

- **QA P0-afronding (ADR-0010):** een bronpad dat onder de leeswortel via een symlink loopt, wordt geweigerd en nooit
  gelezen (kon een niet-geselecteerde share openen); vervaldatum zonder tijdzone → 422 i.p.v. 500; geen lege namen
  of eigenaar; cursor alleen een geheel getal ≥ 0; connector-health `unknown` (niet `healthy`) zolang de
  Hub-sleutels onbekend zijn; kinderprofiel nooit de beheerweergave; de image zet zelf no-new-privileges (het
  TrueNAS-formulier kan dat niet) en de rooktest start zonder die vlag.

- **Platformcontract 1.2 (ADR-0011):** koppeling in `/appdata/secrets/hub-link.json` (0600, atomair, `flock`) in
  plaats van de vault in de database, met eenmalige overzetting; enroll exact volgens §3.3 met volledige
  antwoordcontrole, sleutelset-vingerafdruk (`HUB_KEY_FINGERPRINT` of op de statuspagina) en bevestiging met
  `services/self`; herhaling na een herstart, `429` met `Retry-After`, nooit redirects volgen; gepinde Hub-sleutels
  (geen JWKS-cache meer) met ondertekende sleutelrotatie; de Hub meldt zich met een dienst-JWT (`token_use:
  hub_service`), het eigen service-token wordt inkomend geweigerd; vaste `reason`-waarden; `jti` verplicht met een
  persistente replaycache (migratie 0003); intrekking pas na 401 op `services/self`; tokenrotatie op verzoek van de
  Hub; `GET /connector/v1/hub-link` + `POST /connector/v1/hub-link/unlink` vervangen `/search/v1/enrollment*`;
  statuspagina koppelt alleen zolang nooit gekoppeld (met vingerafdruk, 5 per 15 min per bron-IP), geen lokale
  ontkoppelcode meer; bodygrens telt ook chunked verzoeken (`413`, `details.max_bytes`); geen root-PID 1 meer
  (`dumb-init` weg, de bewaker is PID 1 en ruimt zombies op), rooktest controleert elk proces; manifest volgens §9;
  Custom App-instructie met tag + digest en registry-login alleen als terugval.

- **QA contract 1.2 (ADR-0012):** zonder geldige authenticatie altijd 401, ook bij een onleesbare JSON-body (was
  422/400); hooguit één koppelpoging tegelijk via de statuspagina, en een koppelcode vervangt nooit een koppeling die er
  intussen is; de bewaker volgt als root nooit een symbolische koppeling (in `/appdata` → duidelijke fout; socketmap in
  `/tmp` → opnieuw aangemaakt), zodat een 568-proces root niet buiten `/appdata` kan laten chownen; extra negatieve
  tests (verlopen/hergebruikt token, `X-Forwarded-For`, gewisselde Hub-sleutel, koppelen na intrekking) en rooktest
  secties 7/7b; na een 413 wordt de rest van de body begrensd weggelezen (4 MiB, 2 s), zodat de client de 413 krijgt in
  plaats van een verbindingsreset; interoptest tegen de echte Hub (`tests/interop`, alleen lokaal).

- **Statuspagina in Peil (ZA9, ADR-0013):** `GET /` rendert het framework-vrije statuspagina-sjabloon van de
  ontwerptaal Peil 1.1.0 als versie-gepinde kopie met herkomstkop (`design/ontwerptaal/`, O-14); de dienst en de
  image-bouw controleren de sha256, CI faalt bij drift met de bron in `ai-server-manager`. Geen JavaScript, CSP
  `default-src 'none'`. Koppelformulier alleen zolang nooit gekoppeld, met verplicht vingerafdrukveld (veldnamen van het
  sjabloon: `hub_adres`, `koppelcode`, `hub_vingerafdruk`); geen ontkoppelen of ontkoppelcode. Foutmelding met vaste
  code en de Trace-ID van die poging (selecteerbaar; niet uit de URL), veldfout met `aria-invalid`. Eerste signaal
  nooit groen als de dienst niet gereed of niet gekoppeld is; *Alles gezond* alleen als ook de connector-gezondheid
  gezond is. Browsercontrole (axe-core 0 overtredingen, geen overloop op 320 px, geen CSP-schending) in een eigen
  CI-job en schermafdrukken in `docs/screenshots/peil/statuspagina/`.
- **Ontwerpreview statuspagina (ADR-0014, `docs/ontwerp/PEIL_REVIEW.md`):** review door de design-agent tegen de echte
  dienst. Doorgelopen zijn 12 toestanden × 320/360/390/768/1440 × licht/donker, bereikt via het formulier en via
  Hub-acties in de simulator. Uitkomst: 0 axe-overtredingen, tekst ≥ 7:1, geen overloop.
  - Hersteld (alleen tekst en weergave):
    - geen taak-ID's (B07/B15) en geen "in orde" meer op de pagina;
    - "Gekoppelde shares" met "Niet ingesteld" in plaats van "Leeswortels"/"Geen";
    - *Gereed* noemt per toestand de echte reden;
    - "Alles gezond" telt de functies die nog niet beschikbaar zijn;
    - de melding `hub_weigerde` noemt alleen de koppelcode, en een fout staat niet meer drie keer op de pagina;
    - meldingen volgen wat, waarom en wat je kunt doen;
    - de paginatitel herhaalt niets;
    - de noodpagina zonder sjabloon heeft systeemkleuren (licht en donker), ≥ 14 px en een selecteerbare Trace-ID;
    - geen code zonder breekpunt meer in de melding; die liep bij ruimere tekstafstand buiten het vak (gevonden door
      CI).
  - Nieuw: `tests/ui/peilcontrole.mjs` toetst contrast in de DOM, 44 px, focus, vaste woorden en "nooit groen";
    `make ui-echt` en een CI-stap draaien de review tegen de echte dienst.
- **QA statuspagina (ADR-0015):** onafhankelijke controle na ZA9 en de ontwerpreview. Alle CI-stappen waren groen;
  zeven bevindingen in code en documenten zijn hersteld.
  - Een foutmelding verschijnt alleen bij een echte poging van het afgelopen kwartier (geldige poging-sleutel). Een
    zelfgemaakte link toont niets meer, dus ook geen *al eens gekoppeld* naast het formulier.
  - Een formulier-POST met `Sec-Fetch-Site` anders dan `same-origin` wordt geweigerd. Zo kan een andere app op
    hetzelfde NAS-adres niet posten (zelfde site, `Origin: null`).
  - De invoer wordt als geheel gecontroleerd en nooit eerst afgekapt (koppelcode > 32 tekens is ongeldig).
  - Een poging op een onleesbaar koppelbestand krijgt een eigen melding met code en Trace-ID en gaat niet naar de Hub.
  - De toelichting bij *Niet gereed* volgt de toon van de pil.
  - Code en Trace-ID van elke poging staan in de app-log.
  - `CUSTOM_APP.md` noemt de echte meldingen.
  - `EVIDENCE.json`: `validatie` is altijd een lijstwaarde (A4); de bewakende test controleert per Excel-blad.
  - Acht punten in het Peil-sjabloon zelf gaan als voorstel naar `ai-server-manager`.

- **Contract 1.3 en Peil 1.1.1 (ADR-0016):** alle punten die de conformiteitslijst voor deze repo als NU markeert
  (C1.3-ZPA-01 … -12); -13 (manifest) wacht op de validator van appstore-en-fork, -14 hoort bij P5 (bijgewerkt:
  -13 is gebouwd in de QA-ronde hieronder).
  - Kopieën van `PLATFORM_CONTRACTEN.md` 1.3 en `CONTRACT_1.3_CONFORMITEIT.md` met drifttest (bron `0a7143b`).
  - Statuspagina: `Referrer-Policy: same-origin`; exacte `Origin` (`null` → 403), `Sec-Fetch-Site`, `Host`-beperking
    tegen DNS-rebinding (403 `host_niet_toegestaan`). De POST krijgt direct de pagina met de melding en de echte
    HTTP-status (409 `koppelen_bezig`/`al_gekoppeld`, 422 bij foute invoer, 429 bij te veel pogingen); elke POST na de
    herkomstcontrole telt als poging; precies één logregel per poging. *Gereed* vraagt de bevestiging door de Hub.
  - Peil 1.1.1: renderer in één doorgang, nieuwe tonen (niet-gekoppeld, nog-niet, verouderd, gate), meldingsrol,
    onleesbaar-blok, koppelen bezig, totaalregel E10; controles ook op 1024 px en met DejaVu Sans.
  - Koppelbestand 1.3: schema 1.3, verkeerde eigenaar of rechten = onleesbaar, vervangen alleen bij de eerste
    schrijfactie van een poging met een nieuw env-token (origineel bewaard), vaste codes van 1.3, `rotation_sequence`.
  - Adressen: alleen canonieke IP-vormen; `base_url` alleen als hij geldig is; verboden bereiken ook na
    DNS-resolutie bij elke verbinding.
  - JWT: `aud` precies één string, geen `kid` = `unknown_key`, elke mutatie verbruikt de `jti`.
  - Sleutels: vingerafdruk over alle policy-sleutels (testvector `HN69-2PHK-XV2D-X85W`).
  - Events: vaste `service.*`-namen en -entiteit, alleen eigen domeinen, inboxgrenzen vóór verzending.
  - Connector: vaste checknamen (`database`, `worker`, `jobs`, `hub_link`), hartslag van de worker, totaal =
    slechtste check, `limits.max_age_s`, `meting_verouderd`.
  - API: foutcatalogus (o.a. 422 `idempotency_key_reused`) en een statische Idempotency-Key-test.
  - Image: bounding set leeg bij een start als root; healthcheck met controlevlag; rooktest controleert het apart.

- **QA contract 1.3 (onafhankelijke review, ADR-0017):**
  - **C1.3-ZPA-13 gebouwd:** de validator van appstore-en-fork is klaar (C1.3-ASF-08). Het manifest heeft nu:
    - de kop 1.3;
    - een monitorplan op `/readyz`;
    - licentie GPL-3.0;
    - `digest_pending_release` met reden in `accepted_findings`.

    CI valideert het met `--strict` (validator op een vaste commit). Negatief getoetst: `HUB_URL` in de env geeft
    `reserved_env`.
  - **Retry-After (§5):** na een 429/503 met `Retry-After` gaat er nooit eerder iets naar de Hub. Dat geldt voor de
    Hub-client, voor koppelen (503), voor de sleutelpoll en voor de outbox (de ronde stopt).
  - **Outbox (§7, RV12):** een 401 die een intrekking blijkt, telt geen poging.
  - **Sleutelrotatie-events (§7):** `service.key_rotation.applied` komt er ook als de achtergrondpoll van de API de
    rotatie toepast. `service.key_rotation.rejected` komt één keer per geblokkeerde reeks.
  - **Image:** `SEARCH_AI_UID`/`SEARCH_AI_GID` = 0 wordt geweigerd, zodat nooit alles als root blijft draaien.
  - **Interop:** de test tegen de echte Hub-code (contract 1.3) controleert ook schema 1.3, `rotation_sequence` en de
    `service.*`-events in de inbox van de Hub.

Niet gebouwd (eerlijk `capability_unavailable`): ingest (B07-002), semantisch/hybride zoeken (B07-003),
mediamomenten (B07-004), vraag-en-antwoord (B07-005), opschoonrapport/export (B07-006), alle spraakfuncties (B15).
Validatie op een echte NAS: **NIET GEVALIDEERD**.
