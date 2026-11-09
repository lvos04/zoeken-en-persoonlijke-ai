# ADR-0005 — Intrekken = opschonen, met controle achteraf; originelen nooit wijzigen

Status: aanvaard · Datum: 2026-11-09 · Fase: P0 · Excel: B07-001, (voorbereiding B07-002/B07-006), EX44

## Besluit
1. Intrekken (eigenaar of beheerder; nooit een kinderprofiel) zet in **één transactie**: toestemming, scopes en
   ACL-regels ingetrokken, bron `revoked`, en verwijdert alle afgeleide data (`embedding`, `transcript_ref`, `chunk`,
   `document_ref` per `source_id`, naast `ON DELETE CASCADE`).
2. Daarna **telt** de dienst opnieuw; alleen bij 0/0/0/0 is de purge `succeeded` en krijgt de bron
   `purge_verified_at`. De run staat in `index_run` (kind `purge`) met tellingen vóór en na, en is als geredigeerd
   bewijs op te vragen via `/connector/v1/evidence/{run_id}`. Event `search.source.revoked` zonder inhoud of paden.
3. De worker doet een **nacontrole** (`sweep_revoked`): duikt er toch afgeleide data op bij een ingetrokken bron (bv.
   een racende ingest), dan wordt opnieuw gepurged.
4. `DELETE /search/v1/sources/{id}/index` verwijdert alleen de index (toestemming blijft), voor herindexeren.
5. **Originelen**: de dienst heeft alleen leesrechten (Read Only Host Path) en schrijft nooit in `/bronnen`; tests
   vergelijken checksums en mtimes vóór/na.
6. Checksum-gebaseerde purge-verificatie, privacyrapport en export (B07-006) volgen in P5.
