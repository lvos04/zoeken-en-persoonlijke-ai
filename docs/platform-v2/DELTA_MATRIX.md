# Delta-matrix — zoeken-en-persoonlijke-ai (inventaris P0, 2026-11-09)

Uitgangssituatie: de repo bevatte alleen `README.md` en `docs/platform-v2/` (pakketdocs); geen code, tests, migraties
of images. Alles hieronder was dus **ontbrekend** bij de start van P0.

| Onderdeel | Bij start | Na P0 | Waar |
|---|---|---|---|
| Scaffold (FastAPI, SQLAlchemy 2, Alembic, PostgreSQL) | ontbrekend | bestaand | `apps/api/src/search_ai` |
| Auth (service-token + Hub-JWT via JWKS, fail-closed) | ontbrekend | bestaand | `security/auth.py`, `security/jwks.py` |
| Enrollment-client + koppelcode op statuspagina | ontbrekend | bestaand | `enrollment.py`, `status_page.py` |
| Foutformaat, Idempotency-Key, ETag, healthz/readyz | ontbrekend | bestaand | `errors.py`, `idempotency.py`, `health.py` |
| Connectorcontract §6 | ontbrekend | bestaand (read/stream; rest 501) | `connector.py` |
| Outbox + events naar Hub | ontbrekend | bestaand | `events.py`, `worker.py` |
| B07-001 bronselectie en toestemming | ontbrekend | bestaand (Sim-getest) | `search/{paths,acl,service,router}.py` |
| Datamodel B07 | ontbrekend | bestaand (migratie 0001) | `search/models.py` |
| Datamodel B15 | ontbrekend | bestaand (migratie 0001) | `voice/models.py` |
| B07-002 ingest | ontbrekend | ontbrekend (501) | P5 |
| B07-003 hybride zoeken | ontbrekend | onvolledig (full-text met ACL; vectorzoeker met ACL vóór ranking alleen intern/getest, API semantisch 501) | `search/fulltext.py`, `search/semantic.py`, P5 |
| B07-004 mediamomenten | ontbrekend | onvolledig (schema voor tijdcodes/transcripten) | P5 |
| B07-005 vraag-en-antwoord | ontbrekend | ontbrekend (501) | P5 |
| B07-006 opschonen/portabiliteit | ontbrekend | onvolledig (purge bij intrekking incl. zoekgeschiedenis; back-up zonder indexinhoud; rapport/export ontbreekt) | `search/service.py`, `migrations.py`, P5 |
| B15-001…007 spraak | ontbrekend | ontbrekend (status 501, datamodel aanwezig) | P5 |
| Alles-in-één image (PostgreSQL 16 + pgvector 0.8.7) | ontbrekend | bestaand | `deploy/allinone` |
| Custom App-instructie, manifest, dev-compose, `.env.example` | ontbrekend | bestaand | `deploy/`, `.env.example` |
| CI + release | ontbrekend | bestaand (niet op GitHub zelf gedraaid) | `.github/workflows` |
| OpenAPI-export | ontbrekend | bestaand | `docs/openapi/` |

Bestaande apps (Immich, Jellyfin) worden niet nagebouwd: hun adapters (B07-002) lezen straks alleen via hun eigen,
gescopeerde API's, read-only (02_DATA_EIGENAARSCHAP). Er is geen integriteit/rollback/monitoring van andere apps
gedupliceerd.

**Review P0 (onafhankelijk, 2026-11-09):** aangevuld zonder iets dubbel te bouwen: migratie 0002 (samengestelde
bron-sleutels), vectorzoeker met ACL vóór ranking (`search/semantic.py`, API blijft 501), opschonen van bewaarde
zoekgeschiedenis en B15-context bij intrekken, back-up zonder indexinhoud, verzoek- en tijdlimiet op zoeken,
database-socket 0700, uitgebreide rooktest. Zie `docs/adr/ADR-0008-review-p0-restdata-vector-acl-en-limieten.md`.
