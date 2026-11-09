# Testrapport P0 — zoeken-en-persoonlijke-ai 0.1.0 (2026-11-09)

Omgeving: Linux-bouwomgeving (4 CPU, gedeeld met andere bouwers), Python 3.12.3 (venv), gedeelde PostgreSQL 16.15 +
pgvector 0.6.0 op 127.0.0.1:5432 (databases `zoek_test_api`, `zoek_test_backup`, `zoek_test_e2e`, `zoek_dev_mig`),
Docker met buildx. **Geen** echte NAS, Hub, Immich, Jellyfin, STT/TTS of accounts: die worden gesimuleerd (nep-Hub,
Hub-simulator) of zijn niet gebouwd. De ingest (B07-002) bestaat nog niet; waar tests een gevulde index nodig hebben,
schrijft de test rijen rechtstreeks in de database en dat staat er expliciet bij (`simulate_ingest`).

| Commando | Uitkomst |
|---|---|
| `cd apps/api && ruff check . && ruff format --check . && mypy` | geen bevindingen (39 bronbestanden, mypy ok) |
| `ruff check tests/e2e deploy/allinone && ruff format --check tests/e2e deploy/allinone` | geen bevindingen |
| `cd apps/api && pytest -q` (unit + integratie, PostgreSQL + pgvector) | **78 geslaagd** (42 unit, 36 integratie) |
| `pytest -q tests/e2e -p no:cacheprovider` (API + worker als echte processen, Hub-simulator in eigen proces) | **4 geslaagd** |
| `python -m search_ai migrate` → `migrate --down base` → `migrate` | op/neer/op zonder fouten; drifttest model ↔ migratie: geen verschil |
| `pip-audit -r apps/api/requirements.lock` | geen bekende kwetsbaarheden |
| `python -m search_ai openapi --out docs/openapi/zoeken-en-persoonlijke-ai.openapi.json` (2×) | deterministisch, 30 routes |
| `jl-manifest validate deploy/manifest/zoeken-en-persoonlijke-ai.manifest.yaml` (validator van appstore-en-fork, alleen gelezen) | GELDIG — 0 fouten, 1 verwachte melding `digest_pending_release` |
| `docker compose -f deploy/dev/compose.yaml config -q` | geldig (alleen ontwikkeling) |
| `docker buildx build -f deploy/allinone/Dockerfile -t zoeken-en-persoonlijke-ai:dev .` | gebouwd (± 437 MB) |
| `deploy/allinone/smoke-test.sh zoeken-en-persoonlijke-ai:dev` | **21 geslaagd, 0 mislukt** |

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

## Wat de rooktest van de image controleert
Start als root op een root-map met een **alleen-lezen share onder `/bronnen`** → healthcheck *healthy*; `/healthz`,
`/readyz` (met pgvector); **pgvector 0.8.7** in de ingebouwde database; statuspagina *niet gekoppeld*; 401 zonder token;
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
