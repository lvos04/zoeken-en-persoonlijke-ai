# ADR-0003 — B07-001: consent-register, scopes, gebruikers-ACL vóór ranking en kinderprofiel

Status: aanvaard · Datum: 2026-11-09 · Fase: P0 (F0 van B07) · Excel: B07-001

## Context
Acceptatie: *Niet geselecteerde share indexeert niets en verlaat NAS niet.* B07-003: *per-user ACL before ranking*.
Contract §4: diensten met ACL (search-ai) dwingen rol/ACL zelf af op basis van de Hub-JWT. Het contract zegt niet
wie toestemming mag geven, of een beheerder alle inhoud mag doorzoeken, of hoe kinderprofielen worden beperkt.

## Besluit (veiligste interpretatie)
1. **Bronstatus** `draft → active ⇄ paused → revoked`. Een bron zonder actieve toestemming levert niets: geen lezen,
   geen index, geen zoekresultaten.
2. **Toestemming alleen door de gegevenseigenaar zelf** (`owner_sub`, geen kinderprofiel). Een beheerder kan een bron
   voorbereiden maar **niet namens een ander instemmen**. Gevolg: data van kinderen kan in P0 niet worden
   geïndexeerd (ouderlijke toestemming is een contractvraag).
3. **Gebonden toestemming**: `privacy_consent.snapshot_sha256` over (bron-id, soort, paden, uitsluitingen, adres,
   scopes, tekstversie). Paden/uitsluitingen/adres wijzigen kan alleen in `draft`/`paused`; daarna weigert `resume`
   (`toestemming_verouderd`) tot er nieuwe toestemming is. Precies één actieve toestemming per bron (unieke index).
4. **Scopes per bronsoort**: files/logs `metadata.read, content.read, embeddings.local`; Immich `metadata.read,
   embeddings.local` (nooit gezichtsdata of foto's); Jellyfin `metadata.read, transcripts.read, embeddings.local`.
   `ai.external` bestaat niet in de database-CHECK; aanvragen geeft 501 — indexdata verlaat de NAS niet.
5. **ACL vóór ranking**: `acl.document_condition(principal)` staat in de `WHERE` van elke zoekquery, vóór `ts_rank`,
   `ORDER BY` en `LIMIT` (en later vóór vectorafstand). Toegang per `user` (Hub-`sub`) of `role`. De eigenaar staat
   standaard in de ACL; **een beheerder ziet geen inhoud zonder ACL-regel** (beheren ≠ meelezen; zichzelf toevoegen
   wordt geaudit). Documenten met `visibility='owner'` alleen voor hun eigenaar. Bestandsnamen in de bronwizard alleen
   voor de eigenaar.
6. **Kinderprofiel**: alleen bronnen met `kid_allowed` (en nooit `classification='sensitive'`, ook in de database
   afgedwongen), alleen documenten met `kid_safe`; geen bronbeheer, geen toestemming, geen privacyoverzicht.
7. **Verlopen toestemming = intrekking** (worker), zodat er geen index blijft staan zonder geldige toestemming.
8. Zoekgeschiedenis wordt standaard niet bewaard (`retention.search_query = 0`).

## Alternatieven
- PostgreSQL Row Level Security als tweede verdedigingslinie: uitgesteld naar B07-003, omdat de ingebouwde database
  één superuser-rol heeft (RLS geldt niet voor superusers). Vereist een aparte niet-superuser querierol.
