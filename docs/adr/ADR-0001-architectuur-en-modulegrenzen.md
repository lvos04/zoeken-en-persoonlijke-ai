# ADR-0001 — Architectuur, modulegrenzen en API-prefixen

Status: aanvaard · Datum: 2026-11-09 · Fase: P0 · Contract: 1.1 §1, §2, §5, §6, §10

## Context
Deze repo levert B07 (persoonlijke zoekmachine met pgvector) en B15 (spraak/persoonlijke AI). De repo was leeg op de
docs na. Contract §1: alle schermen staan in de Hub-UI; nieuwe diensten zijn API-diensten met een kleine statuspagina.
B07 §3 noemt routes als `GET /api/v1/search?q=` — dat zijn **Hub**-routes; contract §5 geeft diensten een eigen prefix.

## Besluit
1. Eén Python-pakket `search_ai` (FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16 + pgvector) met modules
   `search` (B07) en `voice` (B15) plus de gedeelde platformkern (auth, koppelen, connector, outbox, audit).
2. Prefixen: `/search/v1/*` (B07), `/voice/v1/*` (B15), `/connector/v1/*` (contract §6), `/healthz`, `/readyz`,
   statuspagina op `/`. De Hub vertaalt zijn `/api/v1/search…` naar deze dienstroutes:
   `GET /api/v1/search?q=` → `GET /search/v1/search`, `GET /api/v1/search/sources` → `GET /search/v1/sources`,
   `POST /api/v1/search/reindex` → `POST /search/v1/reindex`, `DELETE /api/v1/search/sources/{id}/index` →
   `DELETE /search/v1/sources/{id}/index`.
3. Fase P0 levert de scaffold, B07-001 en het datamodel van B07 + B15. Alles wat niet gebouwd is (ingest B07-002,
   semantisch/hybride zoeken B07-003, mediamomenten B07-004, vraag-en-antwoord B07-005, opschoonrapport/export
   B07-006, alle B15-logica) geeft `501 capability_unavailable` of `unknown` — nooit gesimuleerd succes.
4. Containerpoort/hostpoort-voorstel 30450, Custom App-naam `zoeken-en-persoonlijke-ai`, env-prefix `SEARCH_AI_`.
5. Een eenvoudige full-text-zoekroute bestaat al in P0, uitsluitend om de ACL-vóór-ranking aantoonbaar te maken; de
   index is leeg zolang de ingest ontbreekt en het antwoord zegt dat (`ingest: capability_unavailable`).

## Gevolgen
- De Hub moet de vertaling van zijn zoekroutes naar `/search/v1` doen (gemeld als contract_issue).
- STT (faster-whisper), TTS (Piper) en Ollama worden aparte, standaard uitgeschakelde apps; deze dienst neemt nooit
  audio op en heeft geen audio-opslag.
