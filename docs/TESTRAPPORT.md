# Testrapport P0 — zoeken-en-persoonlijke-ai 0.1.0 (2026-11-09)

Omgeving: Linux-bouwomgeving (4 CPU, gedeeld met andere bouwers), Python 3.12.3 (venv), gedeelde PostgreSQL 16.15 +
pgvector 0.6.0 op 127.0.0.1:5432 (databases `zoek_test_api`, `zoek_test_backup`, `zoek_test_mig0002`, `zoek_test_e2e`,
`zoek_dev_mig`) en — bij de review — een PostgreSQL 16.15 + pgvector 0.8.7 uit de gebouwde image zelf,
Docker met buildx. **Geen** echte NAS, Hub, Immich, Jellyfin, STT/TTS of accounts: die worden gesimuleerd (nep-Hub,
Hub-simulator) of zijn niet gebouwd. De ingest (B07-002) bestaat nog niet; waar tests een gevulde index nodig hebben,
schrijft de test rijen rechtstreeks in de database en dat staat er expliciet bij (`simulate_ingest`).

| Commando | Uitkomst |
|---|---|
| `cd apps/api && ruff check . && ruff format --check . && mypy` | geen bevindingen (41 bronbestanden, mypy ok) |
| `ruff check tests/e2e deploy/allinone && ruff format --check tests/e2e deploy/allinone` | geen bevindingen |
| `cd apps/api && pytest -q` (unit + integratie, gedeelde PostgreSQL 16 + pgvector 0.6.0) | **87 geslaagd** (44 unit, 43 integratie) |
| idem tegen een PostgreSQL die uit de gebouwde image zelf is gestart (PG 16.15 + pgvector 0.8.7, poort 55433) | **87 geslaagd** |
| `pytest -q tests/e2e -p no:cacheprovider` (API + worker als echte processen, Hub-simulator in eigen proces) | **4 geslaagd** (ook tegen de PostgreSQL uit de image) |
| `python -m search_ai migrate` → `--down 0001` → `--down base` → `migrate` | op/neer/op zonder fouten (revisie 0002); drifttest model ↔ migratie: geen verschil |
| `pip-audit -r apps/api/requirements.lock` | geen bekende kwetsbaarheden |
| `python -m search_ai openapi --out docs/openapi/zoeken-en-persoonlijke-ai.openapi.json` (2× en vergeleken) | deterministisch, geen drift na commit |
| `actionlint .github/workflows/*.yml` (actionlint-py 1.7.12) | geen bevindingen |
| `jl-manifest validate deploy/manifest/zoeken-en-persoonlijke-ai.manifest.yaml` (validator van appstore-en-fork, alleen gelezen) | GELDIG — 0 fouten, 1 verwachte melding `digest_pending_release` |
| `docker compose -f deploy/dev/compose.yaml config -q` | geldig (alleen ontwikkeling) |
| `nice -n 10 docker buildx build --network host --secret id=ca_bundle,… -f deploy/allinone/Dockerfile .` | gebouwd |
| `deploy/allinone/smoke-test.sh zoeken-en-persoonlijke-ai:review` | **32 geslaagd, 0 mislukt** |

## B07-001 — wat de tests aantonen (Sim-getest)
- *Niet geselecteerde share indexeert niets*: de bronwizard ziet alleen share A; share B, symlinks naar B, `.ssh`,
  `.zfs`-snapshots en sleutelbestanden komen nooit als kandidaat voor (`test_paths.py`,
  `test_unselected_share_indexes_nothing_and_never_leaves_nas`, E2E `test_02`). Een bron zonder toestemming levert
  geen zoekresultaten op, ook niet als er (gesimuleerd) toch indexdata zou staan; herindexeren weigert met 409.
- *Verlaat NAS niet*: al het uitgaande verkeer gaat alleen naar de Hub (koppelen, JWKS, events) en bevat geen
  bestandsnamen, paden of inhoud; `ai.external` kan niet worden aangevraagd (501) en bestaat niet in de database.
- Consent-register: alleen de eigenaar zelf, gebonden aan een momentopname; scopes per bronsoort; verlopen toestemming
  wordt intrekking.
- Gebruikers-ACL vóór ranking: een document van B met een veel hogere score verschijnt niet bij A; beheerder zonder
  ACL ziet niets; rol-ACL werkt en is intrekbaar; `visibility=owner`-documenten alleen voor de eigenaar.
- Kinderprofiel: alleen `kid_allowed` + niet-gevoelig + `kid_safe`; `kind` wint van `admin`.
- Intrekken: documenten, chunks, embeddings en transcriptverwijzingen verwijderd en gecontroleerd (0/0/0/0),
  originele bestanden (checksum + mtime) ongewijzigd, idempotente herhaling, event zonder inhoud, nacontrole door de
  worker.

## Review P0 (onafhankelijk) — extra aandachtspunten (Sim-getest)
`apps/api/tests/integration/test_review_p0_qa.py`, `test_review_p0.py::test_pre_upgrade_backup_with_real_pg_dump`:
- **ACL vóór ranking met limiet/paginering:** 30 hoger scorende documenten van B veranderen niets aan A's
  `limit=1`-resultaat en geven A geen `next_cursor`; B bladert door precies zijn eigen 30 treffers.
- **ACL op de vectorindex (B07-003-acceptatie):** B vindt A's privébestand niet, ook niet met `limit=1` terwijl dat de
  exacte match is; beheerder zonder ACL, service-token en onbekende gebruiker krijgen niets; ander model/andere
  dimensie telt niet mee; kinderprofiel alleen `kid_safe`. Ook met een echte HNSW-index (`EXPLAIN` toont de index).
  Let op: de vectorzoeker wordt alleen door tests aangeroepen met vaste testvectoren; de API voor semantisch zoeken
  blijft `501` omdat er geen lokaal embeddingmodel is.
- **Kinderprofiel ziet nergens iets geblokkeerds** (ook met extra rollen `admin`/`operator`): zoeken, bronnenlijst,
  details, voorvertoning, runs, privacy, bewaartermijnen, connector, vectorindex; geen beheerhandelingen.
- **Intrekken laat geen restdata achter:** na intrekken staan de geïndexeerde tekst en de bestandsnaam in **geen
  enkele tabel** meer (alle tabellen doorzocht); bewaarde zoekgeschiedenis met deze bron is weg, die van een andere
  bron blijft; spraakcontext op de bron ingetrokken; originelen (checksum + mtime) ongewijzigd. De back-up vóór een
  upgrade bevat de bron en toestemming, maar niet de indexinhoud of zoekgeschiedenis (echte `pg_dump`/`pg_restore`).
- **Samengestelde bron-sleutels:** chunk/embedding/transcript met een andere bron dan hun ouder → `IntegrityError`;
  vooruit-migratie 0001 → 0002 met data houdt geldige rijen en verwijdert bron-verwisselde rijen.
- **Limieten:** 4e zoekopdracht binnen het venster → `429 rate_limited` met `Retry-After` (andere gebruiker niet
  geraakt); `statement_timeout` per zoektransactie; een te zware zoekvraag → `503 zoeken_tijdslimiet`.

## Wat de rooktest van de image controleert
Start als root op een lege root-map met een **alleen-lezen share onder `/bronnen`** → healthcheck *healthy*;
`/healthz`, `/readyz` (met pgvector); **pgvector 0.8.7** in de ingebouwde database en **pgvector werkt echt** (HNSW-
index bouwen, KNN-zoekvraag via de index, in een teruggedraaide transactie); migratie 0002 actief; database-socket
0700/568 en een andere gebruiker krijgt `Permission denied`; databasegegevens blijven na herstart, harde kill en in een
**nieuwe container op dezelfde `/appdata`** (geen nieuwe database); statuspagina *niet gekoppeld*; 401 zonder token;
geen proces als root behalve dumb-init/bewaker; PostgreSQL, API en worker als 568; geen TCP-poort voor PostgreSQL;
geheimen 0600/568; ontkoppelcode in de log; share als alleen-lezen gemeld en **ongewijzigd** (eigenaar, rechten,
inhoud, tijden); geen setuid-bestanden; herstart met behoud van geheimen; harde kill en herstart; start als 568;
duidelijke NL-fout bij verkeerde rechten.

## Validatieniveau
- **Sim-getest:** B07-001, koppelen (enrollment-token en koppelcode in het formaat van de echte Hub), fail-closed
  bij uitval van de Hub (EX16-deel voor deze dienst), EX44 (opschonen bij intrekking) voor zover deze dienst.
- **Unit-/integratiegetest:** datamodel B07/B15 en migratie, padenallowlist, auth/JWKS.
- **NIET GEVALIDEERD (echte NAS):** installatie via het TrueNAS-formulier, echte shares als Read Only Host Path op
  TrueNAS, echte Hub-koppeling, alle externe bronnen (Immich, Jellyfin), prestaties/resourcebudget.
- **Niet gebouwd:** ingest, semantisch/hybride zoeken, mediamomenten, vraag-en-antwoord, opschoonrapport/export en
  alle spraakfuncties (eerlijk `capability_unavailable`).
- De GitHub Actions-workflows zijn hier niet op GitHub zelf gedraaid; hun stappen zijn lokaal één voor één
  uitgevoerd (tabel hierboven).
