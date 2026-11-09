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
| `deploy/allinone/smoke-test.sh zoeken-en-persoonlijke-ai:review` | 32 geslaagd, 0 mislukt — **alleen als root** gedraaid; als gewone gebruiker faalde deze versie (zie *P0 afmaken* hieronder) |

## P0 afmaken — CI groen (2026-11-09, ADR-0009)
Eigen Ubuntu 26.04-machine (22 CPU, 14,5 GiB RAM, gedeeld), Python 3.12 (venv), gedeelde PostgreSQL 16.15 +
pgvector 0.8.7 op 127.0.0.1:5432, Docker Engine met buildx; tests als gewone gebruiker (uid 1000, groep docker).

De GitHub-job `image` faalde bij elke push (runs 42601471254, 42602000622, 42605815492): de rooktest las
`/appdata/secrets/vault.key` op de host als niet-root runner, terwijl `secrets/` bewust `0700/568` is. Lokaal als
uid 1000 exact gereproduceerd (`Permission denied`, daarna afbreken door `set -e`). Reparatie in de test, niet in de
rechten van de image; plus een duidelijke NL-fout als de app als andere gebruiker dan root/568 start.

| Commando | Uitkomst |
|---|---|
| `deploy/allinone/smoke-test.sh zoeken-en-persoonlijke-ai:ci` op de oude versie, als uid 1000 | **gereproduceerd**: `FOUT geheimen 0600 en eigenaar 568`, `sha256sum: … Permission denied`, exit 1 |
| `nice -n 10 flock …/docker-build.lock docker buildx build --load -f deploy/allinone/Dockerfile -t zoeken-en-persoonlijke-ai:ci .` | gebouwd (zonder CA/proxy) |
| `deploy/allinone/smoke-test.sh zoeken-en-persoonlijke-ai:ci` (nieuwe versie, als uid 1000) | **37 geslaagd, 0 mislukt** |
| `shellcheck deploy/allinone/smoke-test.sh` | geen bevindingen |
| `cd apps/api && ruff check . && ruff format --check . && mypy` | geen bevindingen |
| `cd apps/api && pytest -q` (gedeelde PostgreSQL 16.15 + pgvector 0.8.7) | **92 geslaagd** (incl. 5 nieuwe unittests) |
| `pytest -q tests/e2e -p no:cacheprovider` | **4 geslaagd** |
| `python -m search_ai migrate` → `--down base` → `migrate` (database `zoek_ci_mig`) | op/neer/op zonder fouten (revisie 0002) |
| `python -m search_ai openapi --out …` + `git diff --exit-code docs/openapi/` | geen drift |
| `pip-audit -r apps/api/requirements.lock` | geen bekende kwetsbaarheden |
| `actionlint .github/workflows/*.yml` | geen bevindingen |
| GitHub Actions `ci`, run 42619280743 (commit ef10e18, branch `ccr-c133d7a0-kuz1cv`) | **groen**: job `python` (92 + 4 geslaagd) en job `image` (rooktest 37 geslaagd, 0 mislukt) |

Bijvangst: `test_pre_upgrade_backup_with_real_pg_dump` faalde lokaal doordat hier PostgreSQL-client 16 én 18 staan
(`pg_wrapper` koos 18 voor `pg_dump`, 16 voor `pg_restore`); de test gebruikt nu de client van de serverversie.

## QA P0-afronding (onafhankelijke review, 2026-11-09, ADR-0010)
Zelfde machine als hierboven (Ubuntu 26.04, uid 1000, gedeelde PostgreSQL 16.15 + pgvector 0.8.7, Docker Engine).
Eerst alle CI-stappen ongewijzigd opnieuw gedraaid op commit 9555051 (allemaal groen, rooktest 37/37), daarna de code
tegen contract 1.1 en de acceptatie van B07-001 gelegd. Gevonden en gerepareerd (elk eerst gereproduceerd):

| Bevinding | Reparatie | Test (faalt op de vorige code) |
|---|---|---|
| **Bronpad via een symlink** in een bovenliggende map (`/bronnen/a/map-naar-b/sub`) werd geaccepteerd; de voorvertoning las daarna de niet-geselecteerde share B (B07-001) | kiezen → `422 pad_via_symlink`; lezen slaat zo'n pad over; toestemming → `409 bron_niet_klaar` (`via_symlink`), ook als de map pas later door een symlink is vervangen | `test_paths.py::test_selected_path_through_symlinked_ancestor_is_refused_and_never_walked`, `test_qa_p0_afronding.py::test_selected_path_via_symlink_never_opens_unselected_share` |
| `expires_at` zonder tijdzone → interne fout (500) | `AwareDatetime` → `422 validation_error` | `test_expires_at_without_timezone_is_422_not_500` |
| Naam/eigenaar/ACL-gebruiker van alleen spaties als lege tekst opgeslagen | witruimte eraf, minimaal 1 teken → `422` | `test_blank_name_or_owner_is_rejected`, `test_blank_patch_name_and_access_principal_are_rejected` |
| Cursor `true`/`false` telde als offset; bij `/connector/v1/events` databasefout (500), `-1` geaccepteerd | `decode_int_cursor`: alleen geheel getal ≥ 0 → anders `400 invalid_cursor` | `test_cursor_must_be_a_plain_non_negative_integer` |
| Connector-health `healthy` terwijl de Hub-sleutels nooit waren opgehaald | geheel `unknown` met `reason_code` van de check | `test_connector_health_unknown_while_hub_keys_never_fetched` |
| Kinderprofiel als eigenaar kreeg de beheerweergave (paden, ACL) | meest beperkende rol wint | `test_kid_as_owner_never_gets_admin_view` |
| Zonder `--security-opt` (zoals het TrueNAS-formulier) draaiden alle processen met `NoNewPrivs: 0` | bewaker zet zelf `PR_SET_NO_NEW_PRIVS`; rooktest start zonder de vlag en controleert elk proces van 568 | `test_deploy_artifacts.py::test_supervisor_sets_no_new_privs_for_itself_and_children`, rooktest-stap 1 en 4 |

| Commando | Uitkomst |
|---|---|
| `cd apps/api && ruff check . && ruff format --check . && mypy` | geen bevindingen (41 bronbestanden) |
| `ruff check tests/e2e deploy/allinone && ruff format --check tests/e2e deploy/allinone` | geen bevindingen |
| `cd apps/api && SEARCH_AI_TEST_ADMIN_URL=… pytest -q` | **107 geslaagd** (was 92; +1 unit paden, +13 integratie QA, +1 unit no-new-privileges) |
| `pytest -q tests/integration/test_qa_p0_afronding.py` tegen de bron van commit 9555051 | **10 mislukt, 3 geslaagd** (de 3 waren al goed: cursor `"5"`, `1.5`, `[1]`) |
| `pytest -q tests/e2e -p no:cacheprovider` | **4 geslaagd** |
| `python -m search_ai migrate` → `--down base` → `migrate` (database `zoek_ci_mig`) | op/neer/op zonder fouten (revisie 0002, pgvector 0.8.7) |
| `python -m search_ai openapi --out …` + `git diff --exit-code docs/openapi/` | geen drift (zelfde schema's) |
| `pip-audit -r apps/api/requirements.lock` | geen bekende kwetsbaarheden |
| `actionlint .github/workflows/*.yml` · `shellcheck deploy/allinone/smoke-test.sh` | geen bevindingen |
| manifest gevalideerd met de validator van `appstore-en-fork` (alleen gelezen, tijdelijke venv buiten de repos) | GELDIG — 0 fouten, 1 verwachte melding `digest_pending_release` |
| vorige image zonder `--security-opt`: `grep NoNewPrivs /proc/*/status` in de container | alle processen `0` (gereproduceerd) |
| `nice -n 10 flock …/docker-build.lock docker buildx build --load -f deploy/allinone/Dockerfile -t zoeken-en-persoonlijke-ai:qa-na .` | gebouwd |
| `deploy/allinone/smoke-test.sh zoeken-en-persoonlijke-ai:qa-na` (als uid 1000) | **39 geslaagd, 0 mislukt** (37 + 2 controles no-new-privileges) |
| GitHub Actions `ci`, run 42622755067 (commit eb6854c, branch `ccr-c133d7a0-kuz1cv`) | **groen**: job `python` (107 + 4 geslaagd, pip-audit schoon) en job `image` (rooktest 39 geslaagd, 0 mislukt, ook de controle no-new-privileges zonder `--security-opt`) |

## B07-001 — wat de tests aantonen (Sim-getest)
- *Niet geselecteerde share indexeert niets*: de bronwizard ziet alleen share A; share B, symlinks naar B (ook een
  bronpad dat zelf via een symlink naar B loopt, ADR-0010), `.ssh`,
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
geen proces als root behalve dumb-init/bewaker; PostgreSQL, API en worker als 568, **met `NoNewPrivs: 1` door de
image zelf** (start zonder `--security-opt`, zoals het TrueNAS-formulier; ADR-0010); geen TCP-poort voor PostgreSQL;
`secrets/` 0700/568 en geheimen 0600/568 (bekeken via een alleen-lezen wegwerpcontainer, zodat de test als elke gebruiker werkt); ontkoppelcode in de log; share als alleen-lezen gemeld en **ongewijzigd** (eigenaar, rechten,
inhoud, tijden); geen setuid-bestanden; herstart met behoud van geheimen; harde kill en herstart; start als 568;
duidelijke NL-fout bij verkeerde rechten (568 op een root-dataset, die onaangeroerd blijft) en bij een verkeerde
gebruiker (*Custom User* 1000:1000 → NL-fout vóór `initdb`).

## Validatieniveau
- **Sim-getest:** B07-001, koppelen (enrollment-token en koppelcode in het formaat van de echte Hub), fail-closed
  bij uitval van de Hub (EX16-deel voor deze dienst), EX44 (opschonen bij intrekking) voor zover deze dienst.
- **Unit-/integratiegetest:** datamodel B07/B15 en migratie, padenallowlist, auth/JWKS.
- **NIET GEVALIDEERD (echte NAS):** installatie via het TrueNAS-formulier, echte shares als Read Only Host Path op
  TrueNAS, echte Hub-koppeling, alle externe bronnen (Immich, Jellyfin), prestaties/resourcebudget.
- **Niet gebouwd:** ingest, semantisch/hybride zoeken, mediamomenten, vraag-en-antwoord, opschoonrapport/export en
  alle spraakfuncties (eerlijk `capability_unavailable`).
- GitHub Actions: vóór *P0 afmaken* was de job `image` rood (zie boven); de runs na de reparatie staan in de
  tabel *P0 afmaken*.
