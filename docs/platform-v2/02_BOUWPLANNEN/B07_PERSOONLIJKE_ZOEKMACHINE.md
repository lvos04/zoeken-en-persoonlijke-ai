# Persoonlijke AI zoekmachine — volledige bouwspecificatie

**Type/eigenaar:** Nieuwe Hub-module  
**Doel:** Doorzoek je eigen bestanden, Immich metadata, Jellyfin bibliotheek en logs in één lokale zoeklaag, opt-in per bron.

**Afhankelijkheden:** Hub, pgvector, Immich, Jellyfin, consent system.

**Bron:** https://github.com/pgvector/pgvector

## 1. Concrete schermen en gebruikersreizen
Zoeken; Filters; Zoekresultaat; Bronnen en rechten; Indexstatus; Privacy; Opschonen; AI-citaten

**Per scherm minimaal:** responsief (360/390/768/1440 px), status leeg/laden/storing/offline, gedocumenteerde navigatie, zoek/filter/paginering, rolcontrole, datum van laatste update, bronherkomst, fout in het Nederlands, toegankelijk toetsenbordgebruik en zichtbaar auditspoor voor wijzigingen.

## 2. Datamodel en levensduur
index_source, connector_permission, document_ref, chunk, embedding, index_run, search_query, access_scope, transcript_ref, privacy_consent, retention

Dwing database-migraties met forward-schema-tests af; time-series met retentie/aggregatie; primaire gegevens alleen bij bron. Entiteiten krijgen stable ID, versie, eigenaar, timestamps en provenance waar toepasselijk. Secrets alleen als vault-reference.

## 3. API en integratie-contracten
GET /api/v1/search?q=; GET /api/v1/search/sources; POST /api/v1/search/reindex; DELETE /api/v1/search/sources/{id}/index

Bij alle endpoints: authenticatie, rol, inputvalidatie, idempotency voor mutaties, paginering of cursors voor lijsten, rate limits, time-outs, versie/capability-detectie en machineleesbare foutcodes. De genoemde uitbreidingsroutes zijn ontwerpeisen, niet automatisch bestaande endpoints. API-contracten moet de bouwer in OpenAPI vastleggen en e2e verifiëren.

## 4. Bouwfasen (verplichte volgorde)

### F0 - Inventaris & onderzoek
**Fase-entry:** voorafgaande kritieke gates geslaagd; bestaande gegevens en huidige release vastgelegd; rollback plan bekend.

#### B07-001 — Bronselectie en toestemming
**Status bij start:** Bouwen; **impact:** Hoog.
**Gewenst gedrag en grenzen:** Geen automatische full-disk index; eerst media, documenten, Immich metadata en logs per bron opt-in.
**Implementatie in code/diensten:** Consent registry with scopes, readable path allowlist and user ACL.
**Schermen/bediening:** Bronwizard en privacydekking.
**Acceptatie / concrete toets:** Niet geselecteerde share indexeert niets en verlaat NAS niet.
**Foutafhandeling en bewijs:** een externe storing mag niet als succes worden gelogd; leg testscenario, gebruikte app/image-versie, screenshots, redacted request/response, timestamps, audit/rollback en incidentlink vast.

**Fase-exit:** alle in deze fase genoemde checks aantoonbaar geslaagd, code review, back-up-/rollback-gate, een gebruikersreis op mobiel en een negatieve test voor permissions. Bij een blocker niets forceren: status `Geblokkeerd` + reden + eigenaar.

### F1 - Kern en contracten
**Fase-entry:** voorafgaande kritieke gates geslaagd; bestaande gegevens en huidige release vastgelegd; rollback plan bekend.

#### B07-002 — Adapter-gedreven ingest
**Status bij start:** Bouwen; **impact:** Middel.
**Gewenst gedrag en grenzen:** Doorzoek toegestane lokale bestanden en API-indexen, cursor en mtime/incremental sync.
**Implementatie in code/diensten:** Read-only mounts and API tokens; hash/ETag, rate limiting, secret redaction, file exclusion.
**Schermen/bediening:** Indexstatus met laatste run en mislukte bestanden.
**Acceptatie / concrete toets:** Verwijderde bron verwijdert gekoppelde embeddings zonder originele data te wijzigen.
**Foutafhandeling en bewijs:** een externe storing mag niet als succes worden gelogd; leg testscenario, gebruikte app/image-versie, screenshots, redacted request/response, timestamps, audit/rollback en incidentlink vast.

**Fase-exit:** alle in deze fase genoemde checks aantoonbaar geslaagd, code review, back-up-/rollback-gate, een gebruikersreis op mobiel en een negatieve test voor permissions. Bij een blocker niets forceren: status `Geblokkeerd` + reden + eigenaar.

### F2 - Functionaliteit
**Fase-entry:** voorafgaande kritieke gates geslaagd; bestaande gegevens en huidige release vastgelegd; rollback plan bekend.

#### B07-003 — Hybride zoekmachine
**Status bij start:** Bouwen; **impact:** Hoog.
**Gewenst gedrag en grenzen:** Klassieke exacte term/fuzzy zoekresultaten plus semantische embeddings alleen als zinnig.
**Implementatie in code/diensten:** PostgreSQL full-text and pgvector optional, embedding model version and dimension; per-user ACL before ranking.
**Schermen/bediening:** Zoekbalk met filters type/bron/datum/score en bronlink.
**Acceptatie / concrete toets:** Gebruiker B kan niet in private file van A zoeken via vector index.
**Foutafhandeling en bewijs:** een externe storing mag niet als succes worden gelogd; leg testscenario, gebruikte app/image-versie, screenshots, redacted request/response, timestamps, audit/rollback en incidentlink vast.

#### B07-004 — Media scènes en ondertitels
**Status bij start:** Bouwen; **impact:** Middel.
**Gewenst gedrag en grenzen:** Benut bestaande subtitle/transcript metadata, video ASR opt-in, zonder files te muteren.
**Implementatie in code/diensten:** Chunk timecodes and source ID; schedule off-peak under A310 resource budget.
**Schermen/bediening:** ‘Zoek moment’ met timecode en speler deep link.
**Acceptatie / concrete toets:** Geen timestamps verzonnen voor media zonder transcript.
**Foutafhandeling en bewijs:** een externe storing mag niet als succes worden gelogd; leg testscenario, gebruikte app/image-versie, screenshots, redacted request/response, timestamps, audit/rollback en incidentlink vast.

**Fase-exit:** alle in deze fase genoemde checks aantoonbaar geslaagd, code review, back-up-/rollback-gate, een gebruikersreis op mobiel en een negatieve test voor permissions. Bij een blocker niets forceren: status `Geblokkeerd` + reden + eigenaar.

### F3 - Integraties en veiligheid
**Fase-entry:** voorafgaande kritieke gates geslaagd; bestaande gegevens en huidige release vastgelegd; rollback plan bekend.

#### B07-005 — Vraag-en-antwoord met citaten
**Status bij start:** Bouwen; **impact:** Middel.
**Gewenst gedrag en grenzen:** Persoonlijke AI antwoordt enkel op toegestane passages met bron en onzekerheid.
**Implementatie in code/diensten:** RAG pipeline context policies, prompt-injection filtering, citations, no implicit write tool.
**Schermen/bediening:** Antwoordkaart met waarom en bronnen openen.
**Acceptatie / concrete toets:** Malicious text in bestand kan geen admin API triggeren.
**Foutafhandeling en bewijs:** een externe storing mag niet als succes worden gelogd; leg testscenario, gebruikte app/image-versie, screenshots, redacted request/response, timestamps, audit/rollback en incidentlink vast.

**Fase-exit:** alle in deze fase genoemde checks aantoonbaar geslaagd, code review, back-up-/rollback-gate, een gebruikersreis op mobiel en een negatieve test voor permissions. Bij een blocker niets forceren: status `Geblokkeerd` + reden + eigenaar.

### F4 - Automatisering
**Fase-entry:** voorafgaande kritieke gates geslaagd; bestaande gegevens en huidige release vastgelegd; rollback plan bekend.

#### B07-006 — Opschonen en portabiliteit
**Status bij start:** Bouwen; **impact:** Middel.
**Gewenst gedrag en grenzen:** Index herbouwbaar, retentie, privacyrapport en dataset-export.
**Implementatie in code/diensten:** Delete indexed content on permission revoke, checksum purge verification, migration with model changes.
**Schermen/bediening:** Privacy panel verwijder mijn indexdata.
**Acceptatie / concrete toets:** Re-index na restore geeft dezelfde documentset, geen dubbele chunks.
**Foutafhandeling en bewijs:** een externe storing mag niet als succes worden gelogd; leg testscenario, gebruikte app/image-versie, screenshots, redacted request/response, timestamps, audit/rollback en incidentlink vast.

**Fase-exit:** alle in deze fase genoemde checks aantoonbaar geslaagd, code review, back-up-/rollback-gate, een gebruikersreis op mobiel en een negatieve test voor permissions. Bij een blocker niets forceren: status `Geblokkeerd` + reden + eigenaar.

## 5. Niet-regressie, randgevallen en verbodsbepalingen
Geen duplicatie van Immich gezicht-/albumfeatures; geen volledige bestands-cloud vervangen. Indexdata is derivatief en krijgt eigen opslagexport, niet gelijk aan primaire brondata.

## 6. Oplevering door bouwer
Opleveren: (1) broncode en tests, (2) migration scripts en versiecompatibiliteit, (3) Compose/TrueNAS app manifest met gepinde images, (4) .env.example zonder geheimen, (5) OpenAPI/connector schemas, (6) test- en auditrapport, (7) restore bewijs, (8) release notes, (9) gebruikershandleiding, (10) screenshots web/mobiel. Markeer niet-geteste hardware/echte accounts als `NIET GEVALIDEERD`.

## 7. Proof of Done
Alle acceptatiecriteria in Excel zijn gekoppeld aan testbewijs en codecommit; bij stroomonderbreking, uitval van afhankelijke app, NAS-herstart, gewijzigde credentials en incompatibele versie degradeert de service aantoonbaar veilig. Geen directe writes aan de database van een andere app.
