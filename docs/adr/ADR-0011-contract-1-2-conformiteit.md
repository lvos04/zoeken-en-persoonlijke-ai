# ADR-0011 — Platformcontract 1.2 toegepast (koppelbestand, pinning, dienst-JWT, replaycache, geen root-PID 1)

Status: aanvaard · Datum: 2026-11-09 · Contract: `contracts 1.2` (kopie in `docs/platform-v2/PLATFORM_CONTRACTEN.md`,
byte-gelijk aan `lvos04/ai-server-manager` 8b54f39) · Conformiteitslijst: `docs/platform-v2/CONTRACT_1.2_CONFORMITEIT.md`
(punten A1–A7 en ZA1–ZA9) · Vervangt delen van ADR-0002 (zie daar).

## Context
Contract 1.2 legt het koppelen met de Hub exact vast (§3.3–§3.8), laat de Hub zich bij een dienst melden met een
eigen dienst-JWT in plaats van met het service-token van de dienst (§4.3), eist pinning van Hub-sleutels (§4.5), een
verplichte `jti` met replaycache (§4.6), vaste `reason`-waarden (§4.7), een bodygrens die ook chunked verzoeken telt
(§5) en dat na de eigenaarschapsstap **geen enkel proces, ook PID 1 niet**, nog als root draait (§2.2, R11). Deze dienst
had in 1.1 een vault-koppeling in de database, een JWKS-cache, een lokale ontkoppelcode en `dumb-init` als PID 1.

## Besluiten

1. **Koppelbestand** (`search_ai/hublink.py`, ZA2). `/appdata/secrets/hub-link.json` volgens §3.5: atomair schrijven
   (`O_CREAT|O_EXCL` 0600 → `fsync` → `rename` → `fsync` map) onder `flock` op `hub-link.lock` plus een thread-lock; elk
   proces leest opnieuw zodra inode, mtime of grootte verandert (`stat` bij elk gebruik). Witte lijst voor alles wat
   naar buiten gaat (statuspagina, `/readyz`, `GET /connector/v1/hub-link`). Een **onleesbaar** bestand is fail-closed:
   geen enkele Hub-JWT geldig (`not_linked`), géén koppelformulier (de dienst kan al gekoppeld zijn geweest), alleen een
   nieuw `HUB_ENROLLMENT_TOKEN` (TrueNAS-beheerder) kan het overschrijven.
2. **Overzetten uit 1.1** (§3.5). Bij `migrate` (de bewaker draait die vóór API en worker) en bij de start van API en
   worker: bestaat `hub-link.json` nog niet, dan wordt de oude koppeling (`service_state` + vault) overgezet; daarna
   worden de oude rijen **altijd** gewist — ook als het bestand al bestaat (database-herstel). Onvolledige oude
   koppelingen (geen policy-sleutel, geen `service_id`, ongeldig adres) worden `ontkoppeld` met
   `last_error: migratie_onvolledig` en openen het koppelformulier nooit opnieuw. Overgezette pins krijgen
   `source: migratie_1.1`; `confirmed_at` blijft leeg zodat de worker het token met `services/self` bevestigt.
   Gevolg: terug naar een image van vóór 1.2 betekent opnieuw koppelen via de Hub (die image leest de database).
3. **Enroll** (ZA1). Verzoek exact §3.3 (`base_url` altijd aanwezig, ook als `null`; JWK alleen `kty/crv/x/kid` met
   `kid` = thumbprint). Het antwoord wordt volledig gecontroleerd vóór opslaan; afwijking → niets opgeslagen,
   `hub_vingerafdruk_onjuist` of `hub_antwoord_ongeldig`, definitief voor dit geheim. Alleen `200` is succes. `3xx` en
   `5xx`/netwerk = storing (nooit een redirect volgen); `429` → niet vóór `Retry-After`; backoff 5 s ×2 tot 15 min met
   jitter; 60 min na `pending.started_at` → `token_verlopen_na_60_min`. Een ongeldig opgemaakte `HUB_KEY_FINGERPRINT`
   blokkeert het koppelen (geen stille *trust on first use*) zonder het token af te schrijven.
4. **Herstart halverwege** (§3.6 stap 5). De herstartregel is per proces uitgevoerd: het API-proces doet koppelcode-
   pogingen synchroon en zet bij zijn start een `pending` koppelcode op `koppelen_onderbroken`; de worker doet het
   env-token en houdt bij zijn start een `pending` token met dezelfde hash aan (toegestane herhaling bij de Hub) en
   breekt een ander af. Bij een storing tijdens een koppelcode-poging wordt `pending` direct gewist (het geheim wordt
   nergens bewaard); de gebruiker kan de code opnieuw invoeren.
5. **Pinning, geen JWKS** (ZA3, §4.5). Deze dienst haalt de JWKS **nooit** op (ook niet voor diagnose): vertrouwen
   komt alleen uit de pins. Een JWT met een onbekende `kid` → direct 401 `unknown_key` en hooguit 1× per 30 s een
   achtergrondpoll van `key-rotations` (thread in het API-proces, het verzoek wacht niet). De worker pollt elke 10 min
   (instelbaar 30–900 s). Hoewel de Hub nog `{"items":[]}` geeft, is de controle van statements volledig gebouwd
   (EdDSA, `typ: hub-key-rotation+jws`, ondertekend met een op dat moment gepinde sleutel van hetzelfde doel,
   `sequence` = huidige + 1, geldige JWK's, geen dubbele `kid`, geen onbekende `retire`); het eerste ongeldige
   statement stopt het toepassen (`last_error: sleutelrotatie_ongeldig`, incident-event, health `hub_sleutels`
   degraded).
6. **Authenticatie** (ZA3, §4.3–§4.7). Volgorde: koppelstatus → tokensoort → `kid`/handtekening → claims →
   `token_use` → `jti`. Keuzes waar de tabel niets zegt: geen of onleesbare `Authorization` → `invalid_token`; header
   zonder `kid` → `invalid_token` (geen geldige JWS volgens §4.2); `jti` die geen UUID is → `invalid_token`; `roles`
   geen lijst van tekst → `invalid_token`. `token_use: user` met rol `service` → `token_use_invalid`. Rechten van
   principal `service:hub` (rol `service`, nooit `admin`): connector lezen (`discover`, `health`, `items` voor
   bronnen/runs/privacysamenvatting, `events`, `evidence`), `propose`/`apply`/`rollback` (geven 501) en
   `hub-link`/`unlink`; geen zoeken, geen bronbeheer, geen toestemming (geen gebruikerscontext).
7. **Replaycache** (§4.6). Tabel `jwt_replay` (migratie 0003) op `(iss, jti)` tot `exp + 30 s`, gedeeld door alle
   processen, eigen transactie per mutatie (de `jti` is verbruikt, ook als het verzoek daarna faalt). Een exacte
   herhaling telt alleen met een niet-lege `Idempotency-Key`; de idempotency-laag geeft dan het opgeslagen resultaat.
   GET/HEAD worden niet vastgelegd. Database niet bereikbaar → `503 replay_check_unavailable`, er wordt niets
   uitgevoerd. De worker ruimt verlopen rijen op.
8. **Intrekken, ontkoppelen, rotatie** (§3.8, ZA6). Een 401 van de Hub leidt alleen tot `ingetrokken` als ook
   `GET /api/v1/services/self` 401 geeft; netwerkfouten, 5xx en 429 nooit. De worker controleert elke ≤ 4 min bij de
   Hub (`services/self`; instelbaar 1–240 s) zodat een rotatieverzoek binnen 5 min wordt opgemerkt en een intrekking
   wordt ontdekt zonder dat er events zijn. `Hub-Token-Rotation: required` (ook na een onderbroken rotatie) →
   `self/rotate`, nieuw token atomair schrijven, bevestigen. `POST /search/v1/enrollment/unpair` en
   `GET /search/v1/enrollment` zijn vervangen door `GET /connector/v1/hub-link` en `POST /connector/v1/hub-link/unlink`.
9. **Statuspagina** (ZA5, §3.7). Formulier alleen bij `state: nooit_gekoppeld` zonder `first_linked_at`; drie verplichte
   velden (Hub-adres met de regels van `HUB_URL`, koppelcode van 12 Crockford-tekens, vingerafdruk van 16); ongeldige
   invoer gaat nooit naar de Hub. Rate-limit in het geheugen (deze dienst heeft één API-proces): 5 per 15 min per
   TCP-bronadres (uvicorn met `proxy_headers=False`, nooit `X-Forwarded-For`) en 20 per 15 min voor de dienst; elke
   inzending met geldige CSRF telt mee. Lokale ontkoppelcode en `/ontkoppelen` zijn verwijderd. ZA9 (CSS uit de
   ontwerptaaltokens) is **niet** gedaan: §13 zegt dat de ombouw pas start als `ONTWERPTAAL.md` en
   `design/ontwerptaal/` in `ai-server-manager` staan; die ontbreken op de peildatum.
10. **Bodygrens** (ZA4, §5). Eigen ASGI-laag als buitenste middleware: te grote `Content-Length` → direct 413; anders
    de body tijdens het lezen tellen en bij > 1 MiB meteen `413 {"code":"payload_too_large","details":{"max_bytes":1048576}}`;
    een niet-numerieke `Content-Length` → 400. De body (≤ 1 MiB) wordt gebufferd en ongewijzigd doorgegeven.
11. **Geen root-PID 1** (ZA7, A6). `dumb-init` is verwijderd; de bewaker is PID 1, vangt SIGTERM/SIGINT als eerste
    handeling af, wisselt na de eigenaarschapsstap naar 568 (`setgroups([])`, `setgid`, `setuid`) met
    `PR_SET_NO_NEW_PRIVS` en ruimt verweesde zombies op (alleen processen die niet zijn eigen kinderen zijn, via
    `/proc`, zodat de exitstatus van eigen kinderen niet verloren gaat). De Docker-healthcheck start als `docker exec`
    met de gebruiker van de image (root) en zakt daarom als eerste naar 568 met `no_new_privs`. De rooktest draait
    zonder `--security-opt`/`--cap-drop` en controleert voor elk proces, ook PID 1, `Uid 568`, `CapEff 0` en
    `NoNewPrivs 1`; uitgezonderd zijn alleen `docker exec`-sessies van buitenaf (PPid 0), zoals de controle zelf en de
    healthcheck — dezelfde afbakening als de Hub. Een negatieve controle (root-PID 1 zonder opruimen) faalt op alle
    drie de nieuwe controles.
12. **Manifest §9** (ZA8). `custom_app.ports` en `network.joins_platform_network` zijn verwijderd, omdat de validator
    van `appstore-en-fork` (M1–M6) op de peildatum al gecommit is (837fa61) en het manifest dan geldig is (in
    `--strict` alleen `digest_pending_release`, want er is bewust nog geen release). CUSTOM_APP.md beschrijft de tag
    als `<versie>@sha256:<digest>` uit `release/digests.json` (NIET GEVALIDEERD op een echte NAS), de publieke image met
    registry-login alleen als terugval, en `HUB_KEY_FINGERPRINT`.

## Gevolgen
- Een Hub die tijdelijk onbereikbaar is, verandert niets: JWT's met gepinde sleutels blijven geldig. De vroegere
  fail-closed-na-900-s op een verouderde JWKS bestaat niet meer; fail-closed geldt bij intrekking, ontkoppeling,
  onbekende sleutels en een onleesbaar koppelbestand (EX16).
- Na `unlink` weigert de dienst ook gebruikers-JWT's (`not_linked`): zonder koppeling geen vertrouwde sleutels.
- Events die tijdens een ontkoppeling ontstaan, blijven `pending` en gaan na een nieuwe koppeling naar de nieuwe Hub.
- Bekend restpunt (buiten de conformiteitslijst, niet gewijzigd): FastAPI leest JSON-bodies vóór de afhankelijkheden;
  een **onleesbare** JSON-body zonder geldige auth krijgt daarom `422` in plaats van `401`. Er wordt niets uitgevoerd
  en er lekt geen data; strikter maken vraagt auth als middleware (voorstel voor een volgende ronde).

## Contractpunten (gemeld)
- Het enroll-antwoord bevat niet de actuele `rotation_sequence` van de Hub. Een dienst die koppelt nadat de Hub al
  sleutels heeft geroteerd, begint op 0 en kan de oude statements (ondertekend met inmiddels ingetrokken sleutels)
  niet controleren → incident, de Hub wacht dan eeuwig. Voorstel: `rotation_sequence` in het enroll-antwoord, of
  `key-rotations` alleen statements laten geven die na de koppeling vallen.
- De vingerafdruk is gedefinieerd over één policy-sleutel; na een policy-rotatie met alleen `add` zijn er tijdelijk
  twee. Gekozen: de laatst gepinde telt.
- "Nooit de laatste sleutel van een doel intrekken" is gelezen als: na het statement moet het doel minstens één
  sleutel hebben (vervangen in één statement mag).
- De `reason`-tabel kent geen waarde voor een ontbrekende `Authorization`; gekozen: `invalid_token`.
