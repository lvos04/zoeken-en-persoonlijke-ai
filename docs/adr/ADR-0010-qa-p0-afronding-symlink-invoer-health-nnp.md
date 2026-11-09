# ADR-0010 — QA P0-afronding: symlink in het bronpad, invoervalidatie, 'onbekend' in health en no-new-privileges

Status: aanvaard · Datum: 2026-11-09 · Fase: P0 (afronding, onafhankelijke QA) · Contract: 1.1 §1, §5, §10 ·
Excel: B07-001, I25-25-01, I14-14-02, EX16, EX36

## Aanleiding
Bij de onafhankelijke QA van de P0-afronding zijn alle CI-stappen opnieuw lokaal gedraaid (allemaal groen) en is de
code tegen contract 1.1 en de acceptatie van B07-001 gelegd. Daarbij kwamen vijf echte gaten naar boven. Elk is eerst
gereproduceerd (met een test die op de vorige code faalt) en daarna gerepareerd.

1. **Symlink in het bronpad (hoog, B07-001).** `PathGuard.check_real` vergeleek het echte pad van een bestand alleen
   met het *opgeloste* bronpad. Liep het gekozen bronpad zelf via een symlink in een bovenliggende map
   (`/bronnen/a/map-naar-b/sub`, waarbij `map-naar-b` een symlink naar share B is), dan werd het pad bij kiezen
   geaccepteerd en las de voorvertoning share B. Een gebruiker met schrijfrecht op share A (bv. via SMB) kon zo een
   niet-geselecteerde share — of een map buiten `/bronnen` — binnen een geselecteerd pad laten vallen. Dat is in strijd
   met de acceptatie *niet geselecteerde share indexeert niets*. Alleen het geval "het bronpad is zelf een symlink"
   werd al afgevangen.
2. **Vervaldatum zonder tijdzone (middel).** `expires_at` zonder tijdzone bij toestemming gaf een interne fout (500:
   vergelijking van een naïeve met een tijdzonebewuste datetime).
3. **Lege tekst (laag).** Naam, eigenaar (`owner_sub`) en ACL-gebruiker van alleen spaties werden na `strip()` als lege
   tekst opgeslagen.
4. **Cursor-typen (middel).** `true`/`false` in een cursor telde als offset 1/0 (in Python is `bool` een `int`) en gaf
   bij `GET /connector/v1/events` een databasefout (500); `-1` werd daar ook geaccepteerd.
5. **'Onbekend' telde als gezond (laag).** Connector-health gaf `healthy` terwijl de Hub-sleutels (JWKS) nog nooit waren
   opgehaald (Hub zonder `jwks` in het enroll-antwoord): een dienstcheck `unknown` telde stil als goed.
6. **Kinderprofiel als eigenaar (laag).** `source_view` gaf een kinderprofiel dat als eigenaar van een bron staat de
   beheerweergave (paden, toegangslijst, toestemming), terwijl details/voorvertoning/runs dat al weigerden.
7. **No-new-privileges alleen van buitenaf (middel, contract §10).** De rooktest startte de image met
   `--security-opt no-new-privileges:true`, maar het standaard TrueNAS Custom App-formulier kan die vlag niet zetten
   (zie contract 1.2-punten in `OVERDRACHT/STATUS.md`). Zonder de vlag draaiden alle processen met `NoNewPrivs: 0`
   (gemeten in de container van de vorige image).

## Besluit
1. `symlink_in_path()` zoekt het eerste padonderdeel **onder de leeswortel** dat een symlink is (buiten elke wortel:
   fail-closed). Een bronpad met zo'n onderdeel wordt bij kiezen geweigerd (`422 pad_via_symlink`), bij lezen
   overgeslagen (`check_real` → `symlink_in_bronpad`, voorvertoning `via_symlink: true`, `bereikbaar: false`) en krijgt
   geen toestemming (`409 bron_niet_klaar`, probleem `via_symlink`) — ook als de map pas ná het kiezen door een symlink
   is vervangen. De leeswortel zelf telt niet mee: die kiest de beheerder van de container, niet een sharegebruiker.
   Shares onder `/bronnen/<naam>` zijn koppelpunten (Host Path), geen symlinks, dus dit raakt geen geldige installatie.
2. `expires_at` is `AwareDatetime`: zonder tijdzone → `422 validation_error` (contract §5: ISO-8601 met tijdzone).
3. Naam, eigenaar en ACL-gebruiker: witruimte eraf en minimaal één teken (`422`).
4. Eén helper `decode_int_cursor` voor alle offset-/volgnummercursors: alleen een geheel getal ≥ 0 (en bij zoeken
   ≤ 10 000), anders `400 invalid_cursor`.
5. Connector-health: is het geheel anders `healthy`, maar staat er een dienstcheck op `unknown`, dan is het geheel
   `unknown` met de `reason_code` van die check. Niet-gebouwde functies (ingest, spraak) tellen niet mee; die staan
   eerlijk als `capability_unavailable` in hun eigen check. De health-check haalt zelf niets op bij de Hub.
6. Kinderprofiel krijgt nooit de beheerweergave van een bron, ook niet als eigenaar.
7. De bewaker zet na de gebruikerswissel zelf `PR_SET_NO_NEW_PRIVS` (prctl) voor zichzelf en elk kindproces (zoals de
   Hub, `appstore-en-fork` en `jelly-leon-platform`) en meldt het als dat niet lukt. De rooktest start de image nu
   standaard **zonder** `--security-opt` (zoals het formulier) en controleert dat elk proces van 568 `NoNewPrivs: 1`
   heeft; stap 4 start ook mét de vlag, om te tonen dat beide werken.

## Gevolgen
- Geen wijziging in de OpenAPI-export (zelfde schema's), geen migratie, geen nieuwe omgevingsvariabelen.
- Regressietests: `apps/api/tests/unit/test_paths.py::test_selected_path_through_symlinked_ancestor_is_refused_and_never_walked`,
  `apps/api/tests/integration/test_qa_p0_afronding.py` (13 tests; 10 falen op de vorige code) en
  `apps/api/tests/unit/test_deploy_artifacts.py::test_supervisor_sets_no_new_privs_for_itself_and_children`.
- Validatie blijft **Sim-getest**; op een echte TrueNAS (Read Only Host Path, formulier zonder no-new-privileges) is
  dit **NIET GEVALIDEERD (echte NAS)**.
- Bewust niet veranderd (al vastgelegd of contract 1.2): een mislukte `pg_dump` vóór een migratie laat de app
  doorstarten, net als de Hub (ADR-0006); koppelen via de statuspagina heeft één globale verzoeklimiet en JWKS gaat
  over HTTP op het LAN (contract 1.2: limiet per bron, pinning/HTTPS).
