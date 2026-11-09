# Wijzigingen

## 0.1.0 — nog niet uitgebracht (fase P0)

Eerste versie van de dienst *Zoeken en persoonlijke AI* (TrueNAS Home Platform V2).

- **Dienstkern:** FastAPI + SQLAlchemy 2 + Alembic op een ingebouwde PostgreSQL 16 met pgvector 0.8.7; authenticatie
  met Hub-JWT (JWKS, gecachet, fail-closed) en service-token; koppelen met eenmalig enrollment-token of koppelcode;
  foutformaat met trace-id; Idempotency-Key; healthz/readyz; connectorcontract; outbox naar de Hub; NL-statuspagina.
- **B07-001 Bronselectie en toestemming:** lees-padenallowlist (leeswortels, paden per bron, vaste uitsluitingen,
  geen symlinks naar buiten), consent-register met scopes gebonden aan een momentopname, toestemming alleen door de
  gegevenseigenaar, gebruikers-ACL vóór ranking, kinderprofiel-beperkingen, intrekken met gecontroleerde purge van
  index en embeddings (originelen ongemoeid).
- **Datamodel B07 en B15** als migratie 0001 (logica van B07-002…006 en B15 volgt in P5).
- **Installatie:** één alles-in-één image als standaard TrueNAS Custom App (`deploy/truenas/CUSTOM_APP.md`), manifest
  v1, CI met rooktest van de image, release met SBOM en digest.

- **Review P0 (onafhankelijk):** migratie 0002 met samengestelde bron-sleutels (chunk/transcript/embedding kunnen
  niet aan een andere bron hangen dan hun ouder); vectorzoeker met gebruikers-ACL vóór de afstands-ranking (intern en
  getest, ook met HNSW; de API voor semantisch zoeken blijft 501); intrekken wist ook bewaarde zoekgeschiedenis en
  trekt spraakcontext op de bron in; de back-up vóór een upgrade bevat geen indexinhoud, zoekgeschiedenis of
  transcripten meer; verzoeklimiet (429) en tijdlimiet (503) op zoeken; database-socket alleen voor 568; rooktest
  toetst pgvector echt en databehoud na herstart, harde kill en nieuwe container (ADR-0008).
- **P0 afmaken (CI groen, ADR-0009):** de rooktest bekijkt `/appdata/secrets` (bewust `0700/568`) via een
  alleen-lezen wegwerpcontainer in plaats van als host-gebruiker, zodat hij ook op de niet-root GitHub-runner werkt;
  testmappen zijn deterministisch van root; de bewaker geeft een duidelijke Nederlandse fout als de app als andere
  gebruiker dan root of 568 start (*Custom User*), in plaats van een Engelse `initdb`-melding.

- **QA P0-afronding (ADR-0010):** een bronpad dat onder de leeswortel via een symlink loopt, wordt geweigerd en nooit
  gelezen (kon een niet-geselecteerde share openen); vervaldatum zonder tijdzone → 422 i.p.v. 500; geen lege namen
  of eigenaar; cursor alleen een geheel getal ≥ 0; connector-health `unknown` (niet `healthy`) zolang de
  Hub-sleutels onbekend zijn; kinderprofiel nooit de beheerweergave; de image zet zelf no-new-privileges (het
  TrueNAS-formulier kan dat niet) en de rooktest start zonder die vlag.

Niet gebouwd (eerlijk `capability_unavailable`): ingest (B07-002), semantisch/hybride zoeken (B07-003),
mediamomenten (B07-004), vraag-en-antwoord (B07-005), opschoonrapport/export (B07-006), alle spraakfuncties (B15).
Validatie op een echte NAS: **NIET GEVALIDEERD**.
