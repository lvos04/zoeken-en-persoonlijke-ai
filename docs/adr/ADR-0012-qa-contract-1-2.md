# ADR-0012 — QA contract 1.2: authenticatie vóór body-fouten, één koppelpoging tegelijk, geen links volgen als root, 413 zonder reset

Status: aanvaard · Datum: 2026-11-09 · Contract: `contracts 1.2` (kopie byte-gelijk aan `lvos04/ai-server-manager`,
sha256 `6a9d29bd…f424`) · Aanvulling op ADR-0011 · Onafhankelijke review van de bouwronde contract 1.2.

## Context
Een onafhankelijke review heeft alle CI-stappen opnieuw gedraaid (alles groen) en de code naast
`PLATFORM_CONTRACTEN.md` §1–§13 en elk punt van `CONTRACT_1.2_CONFORMITEIT.md` (A1–A7, ZA1–ZA9) gelegd. ZA1–ZA8 zijn
gebouwd en getest; ZA9 is terecht niet gedaan (§13: `ONTWERPTAAL.md` en `design/ontwerptaal/` ontbreken nog in
`ai-server-manager`). Vier afwijkingen zijn eerst gereproduceerd en daarna gerepareerd.

## Bevindingen en besluiten

1. **Onleesbare body zonder authenticatie gaf 422 of 400 in plaats van 401** (§4.7: "diensten zonder geldige auth →
   401"). FastAPI leest en ontleedt een JSON-body vóór de afhankelijkheden; `POST /search/v1/sources` met `{kapot` en
   zonder `Authorization` gaf `422 validation_error` (ongeldige UTF-8: `400 bad_request`). Er werd niets uitgevoerd en
   er lekte geen data, maar het antwoord verschilde van een gewone weigering.
   **Besluit:** de foutafhandeling voor `RequestValidationError` en HTTP 400 authenticeert eerst als er voor het
   verzoek nog geen principal is vastgesteld en het pad onder `/search/v1`, `/voice/v1` of `/connector/v1` valt
   (`errors.auth_error_first`). Zonder geldige authenticatie volgt dan de gewone 401 met de vaste `reason` en de
   controlevolgorde van §4.7. Met geldige authenticatie blijft het 422/400. Dit gebeurt zonder methode-informatie,
   zodat de replaycache (`jti`) niet wordt verbruikt: er is niets uitgevoerd. Gekozen boven een eigen ASGI-laag met
   dubbele authenticatielogica, omdat er zo één authenticatiepad blijft.

2. **Twee gelijktijdige koppelpogingen via de statuspagina koppelden allebei** (§3.7: het formulier bestaat alleen zolang
   de dienst nog nooit gekoppeld is). Beide verzoeken passeerden de controle `_may_pair` voordat er één was opgeslagen.
   De tweede overschreef daarna de net gemaakte koppeling: met twee codes van dezelfde Hub, of met de code van een
   tweede "Hub" op het LAN.
   **Besluit:** (a) in het (enige) API-proces loopt hooguit één koppelcode-poging tegelijk. Een tweede krijgt de vaste
   melding `koppelen_bezig` en gaat niet naar de Hub (een tweede uitgifte bij dezelfde Hub zou het token van de eerste
   intrekken, §3.6). (b) Als vangnet slaat een koppelcode-poging het antwoord alleen op zolang `state` nog
   `nooit_gekoppeld` is en `first_linked_at` leeg is, binnen dezelfde `flock`. Anders geldt `al_gekoppeld` en blijft
   alles ongewijzigd. Dat dekt ook de wedloop met het env-token van de worker (een ander proces). Een env-token mag
   altijd opnieuw koppelen (§3.8) en wint dus.

3. **De bewaker volgde als root symbolische koppelingen** (§2.2: na de eigenaarschapsstap draait niets meer als root).
   Bij elke start chownt/chmodt de bewaker als root `/appdata/{postgres,secrets,backups,logs}` en de socketmap
   `/tmp/search-ai-pg`. `p.stat()`, `os.walk`, `os.chown` en `os.chmod` volgden een link. Een proces dat als 568 draaide,
   kon zo'n map vervangen door een link naar bijvoorbeeld `/usr/local/lib`. Bij de volgende start chownde root dan
   code van de image aan 568, en daarmee kon 568 weer root worden. `/tmp` blijft bij `docker restart` staan, en de
   socketmap is van 568.
   **Besluit:** een link als eigen submap van `/appdata` geeft een duidelijke Nederlandse fout. Er wordt dan niets
   aangeraakt en er start geen database (de dataset is van de gebruiker: geen stille reparatie). Een link of
   niet-map als socketmap wordt verwijderd en opnieuw als gewone map gemaakt (dat is onze eigen tijdelijke map). De
   chown daarvan volgt geen links.

4. **413 kwam bij de client vaak niet aan, maar als verbindingsreset** (gevonden doordat CI-run 42643295437 faalde op
   `test_04_body_limit_over_real_http` bij een commit zonder runtime-wijziging). Na de 413 sloot de dienst de
   verbinding terwijl er nog ongelezen body in de ontvangstbuffer zat. TCP stuurt dan een reset, en die kan het
   antwoord bij de client wissen (RFC 9112 §9.6). Lokaal: 70 van de 200 chunked verzoeken van 1,06 MiB kregen
   `ConnectionResetError` in plaats van 413. Voor de Hub betekent dat "niet bereikbaar" (opnieuw proberen) in plaats van
   een definitieve 413 (§3.3).
   **Besluit:** vóór de 413 leest de laag de rest van de body en gooit hem weg ("lingering close"). Niets wordt bewaard
   of verwerkt (wat al gebufferd was, wordt gewist), en het lezen is begrensd tot 4 MiB extra en 2 s. Een te grote
   `Content-Length` wordt nog steeds geweigerd zonder dat er iets bij de app komt. Alleen als de opgegeven lengte
   binnen grens + 4 MiB valt, wordt de body eerst weggelezen; ver daarboven volgt de 413 meteen, zonder te lezen.
   Interpretatie van "direct geweigerd" (§5): niet verwerken en niet bufferen, wel begrensd weglezen. Uitkomst: 200 van
   de 200 goed.

## Kleinere punten
- `deploy/truenas/CUSTOM_APP.md` zei "5 pogingen per kwartier per apparaat". Het is per bron-IP-adres van de
  TCP-verbinding, plus 20 voor de hele app. Welk adres de container achter de poortdoorsturing ziet, is NIET
  GEVALIDEERD (echte NAS).
- Ontbrekende negatieve tests toegevoegd (`apps/api/tests/integration/test_qa_contract_1_2.py`): een verlopen
  (> 60 min) en een hergebruikt enrollment-token met de echte regels van de Hub-simulator. Hergebruik in een nieuwe
  installatie wordt geweigerd en trekt het gebruikte token van de eerste installatie niet in. Verder: de rate-limit
  negeert `X-Forwarded-For`/`X-Real-IP`/`Forwarded`; geen koppelen via de statuspagina na intrekking; een gewisselde
  Hub-sleutel wordt zonder rotatie nooit vertrouwd, en herkoppelen met de oude `HUB_KEY_FINGERPRINT` slaat niets op.
  De rooktest controleert de link-gevallen in de echte image (secties 7 en 7b).

## Interop met de echte Hub
`ai-server-manager` heeft H1–H4 inmiddels op zijn branch. `tests/interop/test_real_hub.py` start de echte Hub-API (eigen
venv uit `git archive`, eigen database) en deze dienst als processen. De test doorloopt de hele levenscyclus:
verkeerde vingerafdruk geweigerd, koppelen en bevestigen, gebruikers-JWT en `jti`-replay, `key-rotations`, rotatie op
verzoek van de beheerder, dienst-JWT met ontkoppelen, opnieuw koppelen, intrekken. De test hoort niet in de CI (hij
vraagt een checkout van een andere repo) en wordt overgeslagen zonder `SEARCH_AI_REAL_HUB_PYTHON`.

Daarbij viel een **contractpunt** op (gemeld aan de contracteigenaar, niet hier op te lossen). Bij een nieuwe
geslaagde enrollment trekt de Hub meteen alle oude tokens van de registratie in (§3.6, "Bestaat de dienst al …"). Dat
gebeurt ook als de dienst het antwoord daarna weigert (verkeerde vingerafdruk) of een gelijktijdige koppelcode-poging
verliest. "Mislukt het, dan blijft een bestaande koppeling ongewijzigd" (§3.8) geldt dan alleen lokaal: bij de volgende
controle wordt de bestaande koppeling `ingetrokken` (fail-closed, veilig, maar wel een herkoppeling). Voorstel: oude
tokens pas intrekken bij het eerste gebruik van het nieuwe token, zoals bij rotatie.

## Gevolgen
- Clients die zonder token een kapotte body sturen, krijgen nu 401 in plaats van 422/400. De Hub stuurt altijd een
  token, dus voor de Hub verandert niets.
- Twee mensen die tegelijk via de statuspagina koppelen: de tweede ziet "Er loopt al een koppelpoging" en laadt de
  pagina opnieuw (daarna is er geen formulier meer).
- Een dataset waarin iemand `postgres`, `secrets`, `backups` of `logs` door een link heeft vervangen, start niet meer.
  De app-log noemt de map.
- Een te groot verzoek kost de dienst hooguit 4 MiB en 2 s extra lezen (zonder geheugen), en de client krijgt de 413
  dan betrouwbaar.
- Niet gevalideerd: alles is Sim-getest (Hub-simulator, lokale Docker), plus één lokale interop-run tegen de echte
  Hub-code (be7d593). Niets is getest op een echte NAS.
