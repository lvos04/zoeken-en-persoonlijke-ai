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

## Contract 1.2 (2026-11-09, ADR-0011)
Eigen Ubuntu 26.04-machine (22 CPU, 14,5 GiB RAM, gedeeld met andere bouwers), Python 3.12 (venv), gedeelde
PostgreSQL 16 + pgvector 0.8.7 op 127.0.0.1:5432, Docker Engine met buildx; alles als gewone gebruiker (uid 1000).
Geen echte NAS, Hub of accounts: de Hub is een simulator (`apps/api/tests/hubsim.py`) die §3.3–§3.8 en §4.5 exact
volgt — in-proces voor de integratietests en als eigen proces voor de dienst-E2E. De echte Hub had H1–H4 op de
peildatum nog niet gecommit; tegen de echte Hub is dus **niet** getest.

| Commando | Uitkomst |
|---|---|
| `cmp` / `sha256sum` van `docs/platform-v2/PLATFORM_CONTRACTEN.md` tegen `ai-server-manager` 8b54f39 | byte-gelijk (`6a9d29bd…f424`); `CONTRACT_1.2_CONFORMITEIT.md` ook |
| `cd apps/api && ruff check . && ruff format --check . && mypy` | geen bevindingen (43 bronbestanden) |
| `ruff check tests/e2e deploy/allinone && ruff format --check tests/e2e deploy/allinone` · `shellcheck deploy/allinone/smoke-test.sh` · `actionlint .github/workflows/*.yml` | geen bevindingen |
| `cd apps/api && pytest -q tests/unit` | **175 geslaagd** (o.a. testvectoren §4.5 `MEGG-8GFG-2E4Y-K2RQ`/`CYRC-JS8C-14AN-JR17`, elke `reason` van §4.7 en de controlevolgorde, exacte enroll-velden, antwoordcontroles, adresregels, rotatie-statements, koppelbestand, §11-controle van EVIDENCE.json) |
| `cd apps/api && pytest -q tests/integration` | **83 geslaagd** (o.a. `test_contract_1_2.py`: herhaling na een verloren antwoord, 401/404/413/422 definitief, 429 + `Retry-After`, backoff en stop na 60 min, herstart halverwege, opnieuw koppelen, statuspagina-regels en rate-limit, `hub-link`/`unlink`, intrekking alleen na 401 op `services/self`, tokenrotatie ook na een onderbroken rotatie, sleutelrotatie, replaycache en 503, chunked > 1 MiB → 413, overzetting uit 1.1) |
| `pytest -q tests/e2e -p no:cacheprovider` (API + worker als echte processen, Hub-simulator in eigen proces) | **7 geslaagd** (env-token + `HUB_KEY_FINGERPRINT`, B07-001-flow, dienst-JWT en replay, bodygrens over echte HTTP met chunked, tokenrotatie, Hub bevroren met SIGSTOP ≠ intrekking, intrekking fail-closed) |
| `python -m search_ai migrate` → `--down 0002` → `--down base` → `migrate` (database `zoek_ci_mig`) | op/neer/op zonder fouten (revisie 0003, pgvector 0.8.7) |
| `python -m search_ai openapi --out …` + `git diff --exit-code docs/openapi/` | geen drift; `/connector/v1/hub-link` en `/hub-link/unlink` nieuw, `/search/v1/enrollment*` weg |
| `pip-audit -r apps/api/requirements.lock` | geen bekende kwetsbaarheden |
| `jl-manifest validate [--strict]` met de **gecommitte** validator van `appstore-en-fork` 837fa61 (M1–M6; `git archive` naar een tijdelijke map, hun werkmap niet aangeraakt) | GELDIG — 0 fouten; alleen `digest_pending_release` (in `--strict` daardoor exit 1: er is bewust nog geen release). Met de oude velden gaf dezelfde validator 2× `deprecated_field` |
| `nice -n 10 flock …/docker-build.lock docker buildx build --load -f deploy/allinone/Dockerfile -t zoeken-en-persoonlijke-ai:dev .` | gebouwd (zonder CA/proxy) |
| `deploy/allinone/smoke-test.sh zoeken-en-persoonlijke-ai:dev` (als uid 1000, zonder `--security-opt`/`--cap-drop`) | **43 geslaagd, 0 mislukt** |
| negatieve controle: de nieuwe rooktestfuncties tegen een container met `python` als root-PID 1 | alle drie falen zoals bedoeld (`Uid 0`, `CapEff 00000000a80425fb`, `NoNewPrivs 0`; PID 1 geen bewaker; zombie blijft staan) |
| GitHub Actions `ci`, run 42638210849 (commit 2f79922, branch `ccr-c133d7a0-kuz1cv`) | **groen**: job `python` (258 + 7 geslaagd, pip-audit schoon) en job `image` (rooktest 43 geslaagd, 0 mislukt, o.a. "elk proces (ook PID 1): Uid 568, CapEff 0 en NoNewPrivs 1") |

Wat de rooktest sinds 1.2 extra controleert: **elk** proces in de container, ook PID 1, heeft `Uid 568`,
`CapEff 0000000000000000` en `NoNewPrivs 1` (alleen `docker exec`-sessies van buitenaf zoals de healthcheck tellen niet
mee); PID 1 is de bewaker zelf; een verweesd proces wordt opgeruimd (geen zombie); `docker restart` stopt netjes via
SIGTERM op PID 1; er staat **geen** ontkoppelcode meer in de log en zonder koppeling bestaat er geen `hub-link.json`.

## QA contract 1.2 (onafhankelijke review, 2026-11-09, ADR-0012)
Zelfde machine (Ubuntu 26.04, uid 1000, gedeelde PostgreSQL 16 + pgvector 0.8.7, Docker Engine). Eerst alle
CI-stappen ongewijzigd opnieuw gedraaid op commit 86b08d1: ruff/mypy/actionlint/shellcheck schoon, **258 geslaagd**
(unit + integratie), E2E **7 geslaagd**, migraties op/neer/op, geen OpenAPI-drift, pip-audit schoon. De contractkopie
en de conformiteitslijst zijn byte-gelijk aan `ai-server-manager` (sha256 `6a9d29bd…f424` en `6daedffb…7f59`). Daarna
is de code naast §1–§13 en ZA1–ZA9 gelegd. Gevonden en gerepareerd (elk eerst gereproduceerd):

| Bevinding | Reparatie | Test (faalt op de vorige code) |
|---|---|---|
| Onleesbare JSON-body **zonder** authenticatie gaf `422 validation_error` (ongeldige UTF-8: `400`) in plaats van 401 (§4.7): FastAPI ontleedt de body vóór de afhankelijkheden | Bij een body-fout op `/search/v1`, `/voice/v1` en `/connector/v1` wordt eerst geauthenticeerd als er nog geen principal is. Zonder geldige auth volgt 401 met de vaste `reason`; met auth blijft het 422/400 en wordt de `jti` niet verbruikt | `test_qa_contract_1_2.py::test_unreadable_body_without_valid_auth_is_401_not_422` (5 varianten), `::test_unreadable_body_when_not_linked_is_401_not_linked`, `::test_unreadable_body_with_valid_auth_stays_422_and_keeps_the_jti` |
| Twee **gelijktijdige** koppelpogingen via de statuspagina slaagden allebei; de tweede verving de net gemaakte koppeling (§3.7) | Hooguit één koppelcode-poging tegelijk (`koppelen_bezig`, gaat niet naar de Hub); een koppelcode slaat alleen op zolang de dienst nog nooit gekoppeld is (binnen de `flock`), ook bij een wedloop met het env-token van de worker | `::test_concurrent_code_pairings_only_one_reaches_the_hub`, `::test_code_attempt_never_replaces_a_link_made_meanwhile`, `::test_no_pairing_on_the_status_page_after_revocation` |
| De bewaker volgde als **root** symbolische koppelingen in `/appdata/{postgres,secrets,backups,logs}` en in de socketmap `/tmp/search-ai-pg` (die bij `docker restart` blijft en van 568 is). In de echte image gereproduceerd met de vorige bewaker: een link `backups → /usr/local/lib` maakte `/usr/local/lib`, inclusief `search_ai/app.py`, eigendom van 568, waarna 568 bij de volgende start als root code kon laten draaien (§2.2) | Link als eigen submap van `/appdata` → Nederlandse fout, niets aangeraakt; link of niet-map als socketmap → vervangen door een gewone map, chown zonder links te volgen | `test_deploy_artifacts.py::test_supervisor_refuses_symlinked_own_dirs` (4×), `::test_supervisor_socket_dir_link_is_replaced_never_followed`, rooktest secties 7 en 7b |
| Na een 413 sloot de dienst met ongelezen body in de buffer: TCP-reset wiste de 413 bij de client (RFC 9112 §9.6). Gevonden doordat CI-run 42643295437 faalde op `test_04_body_limit_over_real_http`; lokaal 70 van 200 chunked verzoeken `ConnectionResetError` | Vóór de 413 de rest van de body begrensd weglezen en weggooien (4 MiB extra, 2 s), niets bufferen of verwerken; ver boven de grens meteen 413 zonder te lezen | `test_bodylimit.py` (6 tests; 3 falen op de vorige code), lokale herhaling 200× via echte HTTP: 200 van 200 een 413 (zonder reparatie 130/200) |
| `CUSTOM_APP.md`: "5 pogingen per kwartier per apparaat" | Per bron-IP-adres + 20 voor de hele app; welk adres de container achter de poortdoorsturing ziet, is NIET GEVALIDEERD (echte NAS) | — |

Ontbrekende negatieve tests toegevoegd (de code was al goed): een verlopen enrollment-token (> 60 min, regels van de
simulator) wordt definitief afgehandeld en nooit opnieuw geprobeerd, ook niet na een herstart
(`::test_expired_enrollment_token_is_refused_by_the_hub_and_never_retried`). Een hergebruikt token in een nieuwe
installatie wordt geweigerd en trekt het gebruikte token van de eerste installatie niet in
(`::test_reused_enrollment_token_on_a_fresh_install_is_refused`). De rate-limit negeert `X-Forwarded-For`, `X-Real-IP`
en `Forwarded` (`::test_status_page_rate_limit_ignores_x_forwarded_for`). Een gewisselde Hub-sleutel wordt zonder
ondertekende rotatie nooit vertrouwd (`unknown_key`; gepinde `kid` met een andere sleutel → `invalid_token`).
Herkoppelen met de oude `HUB_KEY_FINGERPRINT` slaat niets op; trekt de Hub daarbij het oude token in, dan wordt de
dienst fail-closed `ingetrokken` (`::test_swapped_hub_key_is_never_trusted_without_rotation_or_relink`).

| Commando | Uitkomst |
|---|---|
| `cd apps/api && ruff check . && ruff format --check . && mypy` · `ruff check/format --check tests/e2e deploy/allinone` · `shellcheck deploy/allinone/smoke-test.sh` · `actionlint .github/workflows/*.yml` | geen bevindingen (mypy: 43 bronbestanden) |
| `cd apps/api && pytest -q` (unit + integratie) | **284 geslaagd** (258 + 26 nieuw) |
| dezelfde nieuwe tests met de reparaties teruggedraaid (`git stash` van de broncode) | **12 falen** zoals bedoeld (body-fouten, gelijktijdig koppelen, koppelen na intrekking, symlinks); de overige nieuwe tests dekken gedrag dat al klopte |
| `pytest -q tests/e2e -p no:cacheprovider` (na de 413-reparatie 3× achter elkaar) | **7 geslaagd** (3×) |
| `python -m search_ai migrate` → `--down base` → `migrate` (database `zoek_qa_mig`) · OpenAPI-export + `git diff --exit-code docs/openapi/` · `pip-audit -r apps/api/requirements.lock` | op/neer/op zonder fouten (revisie 0003) · geen drift · geen bekende kwetsbaarheden |
| `nice -n 10 flock …/docker-build.lock docker buildx build --load -f deploy/allinone/Dockerfile -t zoeken-en-persoonlijke-ai:qa .` | gebouwd |
| `deploy/allinone/smoke-test.sh zoeken-en-persoonlijke-ai:qa` (als uid 1000, zonder `--security-opt`/`--cap-drop`) | **52 geslaagd, 0 mislukt** (43 + 9 nieuw in secties 7/7b) |
| negatieve controle: image met de vorige `supervisor.py` op een `/appdata` met `backups → /usr/local/lib` | `/usr/local/lib` en `search_ai/app.py` werden eigendom van 568 (lek bevestigd); met de nieuwe bewaker: fout in de log, eigenaar blijft 0 |
| GitHub Actions `ci`, run 42642241110 (commit aaf7722, branch `ccr-c133d7a0-kuz1cv`) | **groen**: job `python` (278 + 7 geslaagd, pip-audit schoon) en job `image` (rooktest 52 geslaagd, 0 mislukt) |
| GitHub Actions `ci`, run 42643295437 (commit 8f1a916, alleen interoptest en docs) | **rood**: `test_04_body_limit_over_real_http` kreeg een verbindingsreset i.p.v. 413 (zie de bevinding over de 413 hierboven; daarna gerepareerd) |
| GitHub Actions `ci`, run 42644401633 (commit 27eb591, na de 413-reparatie) | **groen**: job `python` (284 + 7 geslaagd, pip-audit schoon) en job `image` (rooktest 52 geslaagd, 0 mislukt) |

**Interop tegen de echte Hub (lokaal, niet in CI).** `ai-server-manager` levert inmiddels H1–H4 (enroll 1.2,
`services/self`, `self/rotate`, dienst-JWT, `key-rotations`). De Hub-API is uit `git archive be7d593` (remote-branch,
hun werkmap niet aangeraakt) in een eigen venv gestart, met een eigen database (`zoek_interop_hub`). Deze dienst
draaide als echte processen (API + worker, database `zoek_interop_dienst`): `tests/interop/test_real_hub.py`
(`SEARCH_AI_REAL_HUB_PYTHON=… pytest -q tests/interop`, of `make interop HUB_PY=…`).

| Stap | Uitkomst |
|---|---|
| eerste beheerder met de setupcode uit `hub init`; enrollment-token + `keyset_fingerprint` uit de Hub | 201 |
| env-token met een **verkeerde** `HUB_KEY_FINGERPRINT` | `hub_vingerafdruk_onjuist`, niets opgeslagen, token afgehandeld |
| env-token met de juiste vingerafdruk | gekoppeld en bevestigd (`services/self`); gepinde `kid` = JWKS van de Hub; de Hub ziet het token als gebruikt |
| gebruikers-JWT van de Hub (`/api/v1/auth/service-jwt`) | `discover`/`hub-link` 200; dezelfde `jti` voor een tweede mutatie → 401 `token_replayed`; `svc_`-token inkomend → 401 `service_token_not_accepted` |
| `key-rotations` (`{"items":[]}`) | geen fout, `rotation_sequence` 0 |
| rotatie aangevraagd door de beheerder (`POST /api/v1/services/{id}/rotate`) | de dienst roteerde zelf en bevestigde het nieuwe token |
| dienst-JWT van de Hub (`token_use: hub_service`, gemaakt met `hub.keys.KeyRing` van deze Hub) | `hub-link` 200, `unlink` 200 → `ontkoppeld`; daarna 401 `not_linked`, ook een herhaalde `unlink` |
| nieuw env-token, daarna `POST /api/v1/services/{id}/revoke` | opnieuw gekoppeld (zelfde `service_id`), daarna fail-closed `ingetrokken` → 401 `revoked` |
| `pytest -q tests/interop` | **1 geslaagd** (14,8 s); zonder `SEARCH_AI_REAL_HUB_PYTHON` overgeslagen |

Niet getoetst tegen de echte Hub: de Hub-route `POST /api/v1/services/{id}/unlink`. Die vraagt een gemeten dienstadres
(D03 + inventaris via TrueNAS), en dat bestaat hier niet. De dienst-JWT is daarom met de sleutelring van de Hub zelf
gemaakt. Een echte NAS is ook hier niet gebruikt.

Niet gedaan (terecht, op dat moment): ZA9 (CSS uit de ontwerptaaltokens). `ONTWERPTAAL.md` en `design/ontwerptaal/`
ontbraken toen nog in `ai-server-manager`; inmiddels gebouwd, zie hieronder (ADR-0013). Niet getest: echte NAS en echte
Hub.

## Statuspagina in Peil (ZA9, 2026-11-09, ADR-0013)
Zelfde machine (Ubuntu 26.04, uid 1000, gedeelde PostgreSQL 16 + pgvector 0.8.7, Docker Engine, Node 22, Chromium
158 uit Playwright 1.65.0). `GET /` gebruikt nu het Peil-sjabloon 1.1.0 (kopie van `ai-server-manager` @
`172ee87f…`, sha256 html `be766cc4…62c8`, css `b1e3f789…b5af`).

| Commando | Uitkomst |
|---|---|
| `make lint` (ruff + format + mypy in `apps/api`; ruff op `tests/e2e tests/interop tests/ui deploy/allinone`) · `shellcheck deploy/allinone/smoke-test.sh` · `actionlint .github/workflows/*.yml` | geen bevindingen (mypy: 44 bronbestanden) |
| `cd apps/api && pytest -q` (unit + integratie) | **368 geslaagd** (284 + 84 nieuw) |
| `pytest -q tests/unit/test_peil_kopie.py` met `PEIL_BRON_DIR` = bron van `raw.githubusercontent.com/lvos04/ai-server-manager/172ee87f…` en `PEIL_BRON_VERPLICHT=1` (zoals CI) | **9 geslaagd**; met één byte verschil in de bron: **1 faalt** (drift herkend); zonder bron: **2 falen** (nooit stil overslaan) |
| `tests/unit/test_statuspagina_peil.py::test_zelfde_uitkomst_als_de_referentie_renderer` (8 toestanden, lokaal met `bouw/sjabloon.mjs` + Node) | byte-gelijk aan de referentie-renderer (op `&#x27;`/`&#39;` na) |
| `pytest -q tests/e2e -p no:cacheprovider` | **7 geslaagd** |
| OpenAPI-export + `git diff --exit-code docs/openapi/` | geen drift (de statuspagina staat niet in het schema) |
| `make ui` (8 toestanden × 320/360/390/768/1440 × licht/donker, axe-core 4.14.0 met WCAG 2.0/2.1/2.2 A+AA en best practices) | **80 controles, 0 mislukt**: 0 axe-overtredingen, geen horizontale overloop (ook 320 px), geen `<script>`/`on…`, geen CSP-schending, geen extern verzoek, knoppen/velden ≥ 44 px; 30 schermafdrukken + `CONTROLE.json` in `docs/screenshots/peil/statuspagina/` |
| negatieve controle van `statuspagina.mjs` (pagina met een 900 px breed blok, een veld zonder label en een externe afbeelding) | **FOUT** gemeld: axe `image-alt` + `label`, overloop 933 > 320, CSP-schending, extern verzoek, doel < 44 px |
| `flock …/docker-build.lock nice -n 10 docker buildx build --load -f deploy/allinone/Dockerfile -t zoeken-en-persoonlijke-ai:dev .` | gebouwd (de bouw controleert de sha256 van de kopie) |
| `deploy/allinone/smoke-test.sh zoeken-en-persoonlijke-ai:dev` | **55 geslaagd, 0 mislukt** (52 + 3 nieuw: Peil 1.1.0, verplicht vingerafdrukveld, geen JavaScript) |
| GitHub Actions `ci`, run 42654438103 (commit 9e812dc, branch `ccr-c133d7a0-kuz1cv`) | **groen**: job `python` (drift-stap 9 geslaagd tegen de bron op `172ee87f…`; 360 geslaagd + 8 overgeslagen — de vergelijking met de referentie-renderer draait alleen lokaal; E2E 7 geslaagd), job `statuspagina-ui` (80 controles, 0 mislukt, Chromium 158 op de runner) en job `image` (rooktest 55 geslaagd, 0 mislukt) |
| na de reparatie "succesmelding uit de URL nooit in strijd met de koppelstatus" (commit ba3fba4): `cd apps/api && pytest -q` | **371 geslaagd** (3 nieuw: nooit gekoppeld, ontkoppeld, ingetrokken) |

Wat de nieuwe tests aantonen (Sim-getest): rendering per toestand (niet gekoppeld, koppelen mislukt, gekoppeld,
ingetrokken, niet gereed, plus gekoppeld-beperkt, ontkoppeld en een onleesbaar koppelbestand); formulier alleen zolang
nooit gekoppeld, drie verplichte velden; geen ontkoppelen/ontkoppelcode; foutmelding met code + de Trace-ID van díe
poging (gelijk aan `X-Trace-Id` en het auditspoor; verzonnen/verlopen/andere sleutel → geen Trace-ID); het ingevulde
Hub-adres komt nergens terug; eerste signaal nooit groen als niet gereed of niet gekoppeld; *Alles gezond* wordt
*Beperkt* zodra de outbox vastloopt; een gewijzigde kopie → 500 `peil_sjabloon_ongeldig`, API werkt door.

Niet getest: echte schermlezer, telefoon en tv; ontwerpreview door de design-agent; echte NAS — **NIET GEVALIDEERD**.

## B07-001 — wat de tests aantonen (Sim-getest)
- *Niet geselecteerde share indexeert niets*: de bronwizard ziet alleen share A; share B, symlinks naar B (ook een
  bronpad dat zelf via een symlink naar B loopt, ADR-0010), `.ssh`,
  `.zfs`-snapshots en sleutelbestanden komen nooit als kandidaat voor (`test_paths.py`,
  `test_unselected_share_indexes_nothing_and_never_leaves_nas`, E2E `test_02`). Een bron zonder toestemming levert
  geen zoekresultaten op, ook niet als er (gesimuleerd) toch indexdata zou staan; herindexeren weigert met 409.
- *Verlaat NAS niet*: al het uitgaande verkeer gaat alleen naar de Hub (koppelen, bevestigen, sleutelrotaties, events)
  en bevat geen
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
geen enkel proces als root, **ook PID 1 niet** (de bewaker is PID 1; contract 1.2); elk proces met `Uid 568`,
`CapEff 0` en `NoNewPrivs 1` door de image zelf (start zonder `--security-opt`/`--cap-drop`, zoals het
TrueNAS-formulier; ADR-0010/0011); zombies opgeruimd; geen TCP-poort voor PostgreSQL;
`secrets/` 0700/568 en geheimen 0600/568 (bekeken via een alleen-lezen wegwerpcontainer, zodat de test als elke gebruiker werkt); géén ontkoppelcode in de log; share als alleen-lezen gemeld en **ongewijzigd** (eigenaar, rechten,
inhoud, tijden); geen setuid-bestanden; herstart met behoud van geheimen; harde kill en herstart; start als 568;
duidelijke NL-fout bij verkeerde rechten (568 op een root-dataset, die onaangeroerd blijft) en bij een verkeerde
gebruiker (*Custom User* 1000:1000 → NL-fout vóór `initdb`).

## Validatieniveau
- **Sim-getest:** B07-001, koppelen volgens contract 1.2 (env-token met vingerafdruk en koppelcode op de statuspagina,
  tegen de Hub-simulator; **niet** tegen de echte Hub), intrekking fail-closed (EX16-deel voor deze dienst), EX44
  (opschonen bij intrekking) voor zover deze dienst.
- **Unit-/integratiegetest:** datamodel B07/B15 en migraties, padenallowlist, auth met gepinde sleutels, vingerafdruk.
- **NIET GEVALIDEERD (echte NAS):** installatie via het TrueNAS-formulier (ook of het formulier een tag met
  `@sha256:` accepteert), echte shares als Read Only Host Path op TrueNAS, echte Hub-koppeling (de Hub had H1–H4 nog
  niet uitgeleverd), alle externe bronnen (Immich, Jellyfin), prestaties/resourcebudget.
- **Niet gebouwd:** ingest, semantisch/hybride zoeken, mediamomenten, vraag-en-antwoord, opschoonrapport/export en
  alle spraakfuncties (eerlijk `capability_unavailable`).
- GitHub Actions: vóór *P0 afmaken* was de job `image` rood (zie boven); de runs na de reparatie staan in de
  tabel *P0 afmaken*.
