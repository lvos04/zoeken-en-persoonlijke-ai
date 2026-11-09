# ADR-0007 — Datamodel B07 en B15 (migratie 0001)

Status: aanvaard · Datum: 2026-11-09 · Fase: P0 · Excel: I14-14-01, I25-25-02 (voorbereiding)

## Besluit
1. **B07** (`search/models.py`): `index_source`, `privacy_consent`, `connector_permission`, `access_scope`,
   `document_ref`, `chunk`, `embedding`, `index_run`, `search_query`, `transcript_ref`, `retention`. Primaire gegevens
   blijven bij de bron; alleen afgeleide data + toestemming/rechten/administratie. Stabiele UUID's, tijden,
   actor/eigenaar, `version` (ETag) en herkomst (`provenance`).
2. **chunk.tsv** = gegenereerde `tsvector` (`'simple'`) met GIN-index; Nederlandse stemming volgt in B07-003.
   Tijdcodes alleen als paar en oplopend (CHECK): zonder transcript geen verzonnen tijdcodes (B07-004).
3. **embedding.vector** = `vector` zonder vaste dimensie; `model_name`, `model_version`, `dimension` en `metadata`
   per rij, met CHECK `vector_dims(vector) = dimension`. Een ANN-index (HNSW) vereist een vaste dimensie en wordt in
   B07-003 per model als partiële expressie-index toegevoegd. Eigen SQLAlchemy-type zonder numpy.
4. **B15** (`voice/models.py`): `voice_session`, `transcript`, `intent`, `context_scope`, `tts_job`, `audio_consent`.
   Geen enkele binaire kolom (geen ruwe audio, B15-006); `transcript.expires_at` verplicht (standaard retentie 0);
   `intent.decision` kent geen 'uitgevoerd' — acties lopen als voorstel via de Hub-policy (B15-002/-003).
5. Database-invarianten als verdediging in de diepte: geen `ai.external`-scope, kinderprofiel nooit bij `sensitive`,
   één actieve toestemming per bron, `credential_ref` alleen `vault://…`.
6. Downgrade naar `base` verwijdert alle tabellen en de extensie; een drifttest vergelijkt model en migratie.
