# ADR-0008 — Review P0: geen restdata na intrekken, vector-ACL, samenhang bron-id en limieten

Status: aanvaard · Datum: 2026-11-09 · Fase: P0 (onafhankelijke review) · Excel: B07-001, B07-002, B07-003, B07-006,
I14-14-02, I14-14-03, EX44

## Aanleiding
Een onafhankelijke review van P0 vond vier plekken waar het P0-ontwerp zijn eigen belofte niet helemaal waarmaakte:

1. **Back-up vóór upgrade bevatte de afgeleide index.** `pg_dump` in `/appdata/backups` nam `chunk`, `embedding`,
   `document_ref` en `transcript_ref` mee. Een bron die daarna werd ingetrokken, bleef dus tot drie upgrades lang in
   een back-up staan — in strijd met "intrekken laat geen restdata achter" (B07-006, EX44). Hetzelfde gold voor
   bewaarde zoekgeschiedenis en (straks) spraaktranscripten (B15-006: "no audio in backup", sessie wissen = weg).
2. **Bewaarde zoekgeschiedenis bleef na intrekken naar de bron verwijzen** (`search_query.source_ids`, alleen bij
   retentie > 0). Dat is afgeleide informatie over de inhoud van de bron ("in bron X stond iets over *belasting*").
3. **Geen databasegarantie dat afgeleide rijen bij dezelfde bron horen als hun ouder.** De ACL filtert op
   `document_ref.source_id`, de purge en (straks) de vectorzoeker werken ook met `chunk.source_id`/`embedding.source_id`.
   Een fout in een toekomstige ingest had een embedding aan een andere bron kunnen hangen dan zijn document.
4. **Geen verzoek- of tijdlimiet op zoeken**, terwijl B07 §3 beide eist.

Daarnaast ontbrak een toets voor de B07-003-acceptatie (*gebruiker B kan niet in een privébestand van A zoeken via de
vectorindex*), en toetste de rooktest pgvector alleen op het versienummer.

## Besluit
1. **Back-up zonder indexinhoud.** `pre_upgrade_backup` gebruikt `--exclude-table-data` voor `document_ref`, `chunk`,
   `embedding`, `transcript_ref`, `search_query`, `transcript`, `intent` en `idempotency_record`
   (`migrations.BACKUP_SCHEMA_ONLY_TABLES`). Structuur, bronnen, toestemming, ACL, audit en koppeling blijven in de
   back-up. Na herstel uit zo'n back-up is de index leeg en wordt hij opnieuw opgebouwd (de index is derivatief,
   B07 §5). Getoetst met een echte `pg_dump`/`pg_restore`.
2. **Intrekken/opschonen wist ook bewaarde zoekgeschiedenis** die naar de bron verwijst en trekt B15-context
   (`context_scope.search_source_id`) op die bron in. Het bewijs (`index_run.verification`) bevat alleen
   `zoekgeschiedenis_opgeschoond: true/false`, geen aantal: een aantal zou de eigenaar vertellen hoe vaak anderen in
   zijn bron zochten. De worker-nacontrole (`sweep_revoked`) kijkt ook naar zoekgeschiedenis.
   Een integratietest doorzoekt na intrekken **alle tabellen** (`row_to_json`) op de geïndexeerde inhoud en de
   bestandsnaam.
3. **Migratie 0002: samengestelde sleutels** `chunk(document_id, source_id) → document_ref(id, source_id)`,
   `transcript_ref(document_id, source_id) → document_ref(id, source_id)` en
   `embedding(chunk_id, source_id) → chunk(id, source_id)`. Bestaande bron-verwisselde rijen (alleen door een fout
   mogelijk) worden vooraf verwijderd; getoetst met een vooruit-migratie met data en terug.
4. **Vector-ACL vóór ranking** in `search/semantic.py` (`nearest_statement`/`nearest_chunks`): ACL, model, versie en
   dimensie in dezelfde `WHERE`, join op `(id, source_id)`, sortering alleen op cosinusafstand zodat een HNSW-index
   (`(vector::vector(dim)) vector_cosine_ops`, partieel per model) de volgorde kan leveren. De API
   (`mode=semantic|hybrid`) blijft **501 capability_unavailable**: er is geen lokaal embeddingmodel (optionele app,
   standaard uit) om de zoekvraag te vectoriseren. De functie wordt in P0 alleen door tests aangeroepen, met vaste
   testvectoren; ook met een echte HNSW-index (plan gecontroleerd met `EXPLAIN`).
5. **Limieten op zoeken:** per gebruiker standaard 60 zoekopdrachten per 60 s per API-proces (`429 rate_limited` met
   `Retry-After`) en een `statement_timeout` van 5 s per zoektransactie (`503 zoeken_tijdslimiet`, nooit een half
   antwoord). Instelbaar via `SEARCH_AI_SEARCH_RATE_LIMIT` en `SEARCH_AI_SEARCH_STATEMENT_TIMEOUT_MS` (geen verplichte
   formuliervelden).
6. **Database-socket 0700** (`unix_socket_permissions`): alleen gebruiker 568 (de app) kan de ingebouwde database
   bereiken; de rooktest toont dat een andere gebruiker in de container `Permission denied` krijgt.
7. **Rooktest van de image** toetst nu pgvector echt (HNSW-index bouwen en KNN-zoekvraag via de index), het
   behoud van databasegegevens na herstart, harde kill en een **nieuwe container op dezelfde `/appdata`** (zoals
   TrueNAS bij een update doet), en de socketrechten. Daarnaast zijn alle unit-, integratie- en E2E-tests één keer
   gedraaid tegen een PostgreSQL die uit de gebouwde image zelf is gestart (PG 16.15 + pgvector 0.8.7).

## Grenzen (bewust niet opgelost in P0)
- **Fysiek wissen** is niet gegarandeerd: verwijderde rijen staan tot `VACUUM`/hergebruik nog in databasepagina's, en
  ZFS-snapshots van de `/appdata`-dataset (door de gebruiker of TrueNAS gemaakt) bevatten de index zoals hij toen
  was. Dat valt buiten wat de app kan wissen; de Custom App-instructie zegt dit eerlijk.
- **Herstel van een oude back-up/snapshot** zet ook oude toestemmingen terug. Voordat ingest (B07-002) bestaat,
  levert dat geen data op; vóór B07-002 moet worden vastgelegd dat na een herstel elke toestemming opnieuw door de
  eigenaar wordt bevestigd of tegen het toestemmingsregister van de Hub wordt vergeleken (follow-up).
- **Wie de eigenaar van een share is**, bepaalt in P0 de beheerder bij het aanmaken van de bron (`owner_sub`, geaudit).
  Een beheerder kan zichzelf dus als eigenaar van andermans share opgeven. Op een thuis-NAS kan de beheerder die
  share ook rechtstreeks lezen; de zoek-ACL beschermt daar niet tegen. De Hub moet `owner_sub` straks afleiden uit
  het data-eigenaarschapsregister (02_DATA_EIGENAARSCHAP) in plaats van vrije invoer (contractpunt).
- RLS met een aparte niet-superuser querierol blijft een follow-up (ADR-0003); de app verbindt nog als de enige
  (superuser-)rol van de ingebouwde database, alleen via de 0700-socket.
