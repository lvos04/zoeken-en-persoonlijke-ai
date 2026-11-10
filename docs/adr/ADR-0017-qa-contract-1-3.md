# ADR-0017 — QA contract 1.3 (onafhankelijke review)

**Status:** aanvaard. **Datum:** 2026-11-10. **Contract:** `contracts 1.3` (kopie op `ai-server-manager@0a7143b`,
inhoud gelijk aan de werkbranch), `CONTRACT_1.3_CONFORMITEIT.md` hoofdstuk 5 (C1.3-ZPA-01 … -14).
**Ontwerptaal:** Peil 1.1.1. **Bouwt voort op:** ADR-0016.

## Context

Dit is een onafhankelijke QA-ronde na de bouwronde van contract 1.3 en Peil 1.1.1 (ADR-0016). Eerst is alles opnieuw
gedraaid zoals CI het doet:
- lint en typen;
- unit- en integratietests;
- drift van de contract- en Peil-kopie, met de bron online opgehaald;
- dienst-E2E;
- beide UI-routes;
- OpenAPI-export, actionlint en shellcheck;
- de alles-in-één image met de rooktest.

Alles was groen. De kopieën zijn byte-gelijk aan de bron:
- contract `15b30cf1…`;
- conformiteitslijst `f0a2352d…`;
- Peil-sjabloon `d352028f…` en `0f996358…`, gelijk aan de werkbranch.

Daarna zijn de code, de NU-punten en de documenten gelezen. Ook is de interoptest gedraaid tegen de **echte** Hub-code
met contract 1.3 (`ai-server-manager@5e21775`, eigen venv, hun werkmap niet aangeraakt).

## Bevindingen

| # | Bevinding | Gevolg |
|---|---|---|
| Q13-01 | **C1.3-ZPA-13 was niet langer geblokkeerd.** De validator van `appstore-en-fork` (C1.3-ASF-08, commit `1090fc9`, op de branch `cca4403`) kent `healthcheck.monitor`, `accepted_findings` en de kop 1.3. Het manifest gaf daar in `--strict` twee bevindingen: `healthcheck_monitor_missing` en de niet geaccepteerde `digest_pending_release`. | Het NU-punt stond open, met een verouderde reden. |
| Q13-02 | **§5 Retry-After:** `HubClient` probeerde een 429/503 na 0,25–1,5 s opnieuw, ook als de Hub `Retry-After` stuurde. Dat gold voor de sleutelpoll (3 pogingen). Bij koppelen gaf een 503 met `Retry-After` een backoff van 5 s, en de outbox probeerde na 5 s opnieuw en stuurde in dezelfde ronde de volgende events. | In strijd met "bij 429 of 503 nooit eerder dan `Retry-After`" (§5). |
| Q13-03 | **§7/RV12:** gaf de inbox 401 en bleek dat een intrekking (`services/self` ook 401), dan telde het event toch een poging. | In strijd met "niet gekoppeld = niet pushen en geen pogingen tellen". jelly-leon-platform doet dit wel goed. |
| Q13-04 | **§7 (C1.3-ZPA-05):** de API pollt sleutelrotaties op de achtergrond bij een onbekende `kid`. Paste die poll een rotatie als eerste toe, dan kwam er geen `service.key_rotation.applied` en geen auditregel: de worker zag daarna niets meer. Daarnaast gaf een geblokkeerde rotatie bij **elke** poll (elke 10 min) een nieuw `service.key_rotation.rejected`. | Een rotatie kon zonder melding gebeuren, of één incident gaf een stroom events. |
| Q13-05 | **§2.2/§10:** de bewaker en de healthcheck lazen de doelgebruiker uit `SEARCH_AI_UID`/`SEARCH_AI_GID` (alleen voor ontwikkeling). Met waarde 0 werd de "wissel" `setuid(0)`, en dan draaide alles als root. | De garantie "na de eigenaarschapsstap alles als 568" hing aan een omgevingsvariabele. |
| Q13-06 | De interoptest controleerde alleen contract 1.2. | Er was geen bewijs dat de echte Hub 1.3 en deze dienst samenwerken. |
| Q13-07 | `.env.example` noemde nog contract 1.2. De ADR-0016 en de CHANGELOG zeiden dat ZPA-13 op de validator wacht. | De documentatie liep achter. |

## Besluiten

1. **ZPA-13 gebouwd** (Q13-01). Het manifest heeft nu:
   - de kop "platformcontract 1.3";
   - `healthcheck.monitor: {pad: /readyz, verwachte_status: 200, timeout_s: 10, interval_s: 60}`;
   - `provenance.license: GPL-3.0`;
   - alleen `digest_pending_release` in `accepted_findings`, met een reden: vóór de eerste release is er geen digest,
     en de vertaling blokkeert installeren dan met `digest_not_pinned`.

   CI installeert de validator op de **vaste commit** `cca4403` (met de hashes van zijn lock) en draait
   `jl-manifest validate --strict`. De unit-test eist de validator in CI (`MANIFEST_VALIDATOR_VERPLICHT=1`) en toetst
   ook negatief: `HUB_URL` in de env geeft `reserved_env`. Een vaste commit en niet de branch-kop: zo breekt een
   latere validatorwijziging deze CI niet onverwacht, en een nieuwe regel volgt met een bewuste pin-update.
2. **Retry-After** (Q13-02). `retry_after_s()` leest de waarde (seconden of HTTP-datum, alleen bij 429/503, hooguit
   1 uur). Een korte wachttijd (≤ 8 s) wacht de client binnen de aanroep af. Bij een langere krijgt de aanroeper het
   antwoord terug, en die plant zelf:
   - de worker koppelt niet eerder opnieuw;
   - de sleutelpoll pollt niet eerder (de 15-minutengrens blijft een bovengrens);
   - het outbox-event wacht minstens die tijd, en de ronde stopt.
3. **Intrekking telt geen poging** (Q13-03). Blijkt een 401 van de inbox een intrekking, dan stopt de ronde zonder
   `mark_failed`. Het event blijft `pending` met 0 pogingen tot een nieuwe koppeling. Een 401 zonder intrekking
   (`services/self` geeft 200) telt wel een poging, anders zou een kapotte Hub elke 10 s opnieuw worden aangeroepen.
4. **Rotatie-events uit elk proces** (Q13-04). `key_rotation_record()` bepaalt in één functie wat er vastgelegd wordt:
   - De worker gebruikt die functie, en de achtergrondpoll van de API ook, via `on_outcome` van `KeyRotationTrigger`.
     De API legt dan zelf de audit en het event vast, met actor `api`.
   - Een geweigerde rotatie wordt één keer per geblokkeerde reeks gemeld (`nieuw_geblokkeerd`). De blokkade zelf
     blijft zichtbaar in `key_rotation_blocked_sequence`, in `last_error` en in de connector-health.
5. **Nooit root als doelgebruiker** (Q13-05). `SEARCH_AI_UID`/`SEARCH_AI_GID` = 0 geeft in de bewaker een Nederlandse
   fout (de start stopt). De healthcheck geeft dan exit 1, vóór elke wissel of controle.
6. **Interop met contract 1.3** (Q13-06). De interoptest (alleen lokaal) controleert nu ook:
   - schema 1.3 in het koppelbestand;
   - `rotation_sequence` uit het enroll-antwoord van de echte Hub;
   - `service.hub_link.linked` (payload `{via: token}`) en `service.token.rotated`, met entiteit `service/<app_id>`, in
     de inbox van de echte Hub.

## Bewust niet veranderd (met reden)

- **Paginering over een momentopname (§5, K5) voor `GET /search/v1/search`.** De conformiteitslijst noemt dit niet voor
  deze repo. In P0 kan de index niet veranderen: er is geen ingest (B07-002), dus de index is in productie leeg en een
  intrekking wist niets. Daarom kan een volgende pagina niet verschuiven. De momentopname (`snapshot_id`, hetzelfde
  totaal, 409 `snapshot_changed`) hoort bij het vullen van de index in P5 (B07-002/B07-003). Gemeld als
  contractpunt voor de volgende conformiteitslijst.
- **Log-sink en heartbeat naar de Hub (§6.1):** blijven optioneel en niet gebouwd (ADR-0016).
- **C1.3-ZPA-14:** blijft P5, na BG-06.

## Gevolgen

- Alle NU-punten C1.3-ZPA-01 … -13 zijn gebouwd en getest. ZPA-13 is gevalideerd met de echte validator; een echte
  installatie op een NAS is **NIET GEVALIDEERD**.
- Elke reparatie heeft een test die zonder de reparatie faalt:
  - 6 van 8 nieuwe integratietests faalden op de oude code; de 2 andere zijn positieve controles;
  - de unit-test van `HubClient` kon de oude code niet eens importeren;
  - de 3 tests voor root als doelgebruiker faalden.
- **NIET GEVALIDEERD:** een echte NAS, echte accounts en echte hardware. De interop draaide lokaal tegen de Hub-code,
  niet tegen een geïnstalleerde Hub.
