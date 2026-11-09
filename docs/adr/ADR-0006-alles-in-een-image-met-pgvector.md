# ADR-0006 — Alles-in-één image met PostgreSQL 16 + pgvector 0.8.7 als standaard Custom App

Status: aanvaard · Datum: 2026-11-09 · Fase: P0 · Contract: 1.1 §1, §2, §10 · Excel: I14-14-02

## Besluit
1. Eén image (`deploy/allinone/Dockerfile`) naar het werkende MSO-voorbeeld: PostgreSQL 16 (alleen unix-socket
   `/tmp/search-ai-pg`, geen wachtwoord, geen TCP), migraties, API + statuspagina en worker onder een kleine
   procesbewaker met dumb-init; één datamap `/appdata`, één poort 30450, healthcheck op `/readyz` (database,
   migraties, pgvector, geheimen).
2. **pgvector uit de officiële image** `pgvector/pgvector:0.8.7-pg16-bookworm` (via `mirror.gcr.io`), op
   index-digest `sha256:0dd3149a…dc4f`. pgvector v0.8.7 is getagd op 2026-10-01 (> 2 weken oud); de image bevat
   PostgreSQL 16.15. De Dockerfile controleert dat `vector.control` versie 0.8.7 meldt. Python-runtime:
   `python:3.12.16-slim-bookworm` op digest (zelfde Debian-basis, glibc-compatibel).
3. Start als root alleen om `/appdata` en de eigen submappen aan 568:568 te geven; daarna alles als 568. Geen
   setuid/setgid-bestanden. `/bronnen` wordt nooit gewijzigd.
4. First-start-init maakt `/appdata/secrets/{vault.key, identity.ed25519}` (0600, nooit overschrijven).
5. Vóór openstaande migraties een `pg_dump -Fc` naar `/appdata/backups` (nieuwste 3); mislukt een migratie, dan stopt
   de app met een NL-melding en blijft de oude database ongewijzigd.
6. De sandbox-CA staat nooit in de Dockerfile: alleen een optionele BuildKit-secret `ca_bundle`.
7. `deploy/dev/compose.yaml` is uitsluitend voor ontwikkeling/CI.

## Gevolgen
- Lokaal ontwikkelen kan tegen elke PostgreSQL ≥ 16 met pgvector (de gedeelde ontwikkel-PostgreSQL heeft 0.6.0); de
  migratie gebruikt alleen functies die in beide versies bestaan (`vector`, `vector_dims`).
