# ADR-0016 — Contract 1.3 en Peil 1.1.1 toepassen

**Status:** aanvaard. **Datum:** 2026-11-10. **Contract:** `contracts 1.3` (kopie op `ai-server-manager@0a7143b`),
`CONTRACT_1.3_CONFORMITEIT.md` hoofdstuk 5 (C1.3-ZPA-01 … -14) en hoofdstuk 0 (C1.3-ALG-01 … -05).
**Ontwerptaal:** Peil 1.1.1 (ONTWERPTAAL §13.2, §14, §16; COMPONENTEN §29). **Bouwt voort op:** ADR-0011 … ADR-0015.

## Context

Contract 1.3 en Peil 1.1.1 zijn op 2026-11-10 vastgesteld in `ai-server-manager`. De conformiteitslijst noemt voor deze
repo dertien punten met moment **NU** (C1.3-ZPA-01 … -13; -13 pas na de validator C1.3-ASF-08) en één voor P5
(C1.3-ZPA-14). Deze ADR legt vast hoe ze zijn gebouwd, welke keuzes daarbij zijn gemaakt en wat open blijft. Niets is op
een echte NAS, met echte accounts of op echte hardware gevalideerd.

## Besluiten

### 1. Kopieën en drift (C1.3-ALG-01, C1.3-ZPA-12)

`docs/platform-v2/PLATFORM_CONTRACTEN.md` en `CONTRACT_1.3_CONFORMITEIT.md` zijn byte-gelijke kopieën van de bron op
commit `0a7143b`. `apps/api/tests/unit/test_contract_kopie.py` controleert offline de sha256 van beide en vergelijkt ze
met de bron op die commit; CI leest de commit uit de test en haalt de bron verplicht op. `CONTRACT_1.2_CONFORMITEIT.md`
blijft als historie staan.

De Peil-kopie (`design/ontwerptaal/statuspagina.{html,css}`) is 1.1.1, gepind op dezelfde commit. De drifttest volgt de
pinregel E08: fout bij een afwijking van de gepinde commit en bij **dezelfde versie met andere inhoud** op de werkbranch;
een **nieuwere** versie op de werkbranch geeft alleen een waarschuwing.

### 2. Statuspagina: het antwoord op de POST is de pagina met de echte HTTP-status (C1.3-ZPA-02)

Contract 1.3 §3.7 vraagt HTTP 409 voor `koppelen_bezig` en `al_gekoppeld` en 403 voor een geweigerde herkomst, en zegt
dat een melding bij een echte poging van deze browser hoort, nooit bij alleen een query-parameter. De 1.2-pagina deed
POST → 303 → `GET /?melding=…&poging=…`; daarmee is de status van de POST altijd 303 en kan een info-melding uit de URL
komen. Gekozen: **de POST krijgt direct de pagina** met de melding van dít verzoek en de vaste HTTP-status, zoals de
executor (referentie in `ai-server-manager`). `GET /` negeert `?melding=`; er is geen poging-sleutel meer.

| Melding | HTTP | Wanneer |
|---|---|---|
| `herkomst_geweigerd` | 403 | `Origin` ontbreekt, is `null`, wijkt af van `<schema>://<Host>`, of `Sec-Fetch-Site` is `same-site`/`cross-site` |
| `host_niet_toegestaan` | 403 | `Host` is geen IP-adres in de vorm van §3.2, geen naam zonder punt en geen naam onder `.local`/`.lan`/`.internal`/`.home.arpa` |
| `te_veel_pogingen` | 429 (+ `Retry-After: 900`) | meer dan 5 per bron-IP of 20 voor de hele dienst per 15 min |
| `ongeldig_formulier` | 403 | formuliertoken of cookie ontbreekt of klopt niet |
| `koppelstatus_onleesbaar`, `al_gekoppeld`, `koppelen_bezig` | 409 | de toestand staat koppelen niet toe |
| `hub_adres_ongeldig`, `koppelcode_ongeldig`, `vingerafdruk_ongeldig`, `hub_weigerde`, `hub_vingerafdruk_onjuist` | 422 | foute invoer (ook volgens de Hub) |
| `hub_onbereikbaar`, `hub_antwoord_ongeldig`, `enroll_endpoint_ontbreekt`, `hub_incompatibel` | 503 | de Hub (afhankelijkheid) kon het niet afhandelen |
| `rate_limited` | 429 | de Hub vraagt te wachten |
| `gekoppeld`, `gekoppeld_onbevestigd` | 200 | geslaagd (melding met rol `status`) |

Volgorde: herkomst → **telt als poging** → formuliertoken → onleesbaar → al gekoppeld → lopende poging → invoer → Hub.
De strengste lezing van "elke formulier-POST die de herkomstcontrole doorstaat, telt mee" is gekozen: ook een verlopen
formulier telt. Een geweigerde herkomst telt niet (die gaat nergens heen en een andere site mag de limiet van de
gebruiker niet opmaken). Elke afgehandelde POST geeft precies één logregel
(`statuspagina: koppelpoging afgehandeld (code …, HTTP …, Trace-ID …)`); de koppellogica logt bij een koppelcode niets
extra. Gevolg: na een POST vraagt de browser bij herladen om opnieuw te verzenden; dat telt dan als nieuwe poging en
geeft hooguit `al_gekoppeld` of `hub_weigerde`. Dat is eerlijk en veilig.

**Host-beperking (K1 van het contract):** `GET /` via een domeinnaam die koppelen niet toelaat toont het formulier nog,
met een blijvende melding *Koppelen kan niet via dit adres* (zonder live-rol). Zo weet de beheerder vóór het invullen
waarom het niet lukt.

### 3. Koppelbestand (C1.3-ZPA-04, -06)

- Schrijft `schema_version: "1.3"`, leest elke `1.x`, bewaart onbekende velden.
- Onleesbaar = fail-closed, nu ook bij een **verkeerde eigenaar of rechten** (eigenaar ≠ het eigen proces, of bits voor
  groep/anderen). Inode, mtime, grootte, rechten en eigenaar bepalen wanneer het bestand opnieuw gelezen wordt.
- **Vervangen (RV6):** alleen de eerste schrijfactie van een poging met een **nieuw env-token** (`pending`, of
  `last_error` bij `token_ongeldig`) vervangt een onleesbaar bestand, vóór het verzoek aan de Hub. Het origineel blijft
  eerst bewaard als `hub-link.json.onleesbaar-<UTC-tijd>` (harde koppeling, dus ook zonder leesrecht; anders een kopie
  0600); het nieuwe bestand krijgt `unreadable_replaced_at`. Zolang `first_linked_at` leeg is, blijft de toestand voor de
  statuspagina "onleesbaar" (geen formulier, geen vertrouwen). Een configuratiefout (`hub_adres_ongeldig`,
  `vingerafdruk_ongeldig`) is geen poging en vervangt niets.
- Vaste codes van 1.3: `hub_incompatibel` (413/422 **en andere onverwachte 4xx**, definitief), `token_ongeldig`,
  `vingerafdruk_ongeldig`, `hub_adres_ongeldig`, `overzetten_onvolledig`. Oude 1.2-namen in een bestaand bestand
  (`ongeldig_hub_adres`, `ongeldige_vingerafdruk`, `migratie_onvolledig`) worden bij het lezen omgezet.
  `sleutelrotatie_ongeldig` blijft een eigen code (open lijst).
- Extra velden: `token_rotation_requested_at` (een rotatieverzoek overleeft een herstart) en
  `key_rotation_blocked_sequence` (een geweigerde rotatie blijft zichtbaar). Ze staan niet in de witte lijst.
- `rotation_sequence` uit het enroll-antwoord (ontbreekt bij een Hub 1.2 → 0; een ander type of < 0 → ongeldig
  antwoord).

### 4. Adressen (C1.3-ZPA-08)

`validate_hub_url`: IPv4 alleen als `a.b.c.d` (verkorte, decimale, hexadecimale en octale vormen geweigerd), IPv6 alleen
tussen `[]` en zonder zone-ID, één punt aan het eind van een naam telt niet mee, hooguit 512 tekens. `base_url` gaat
alleen mee als hij aan dezelfde regels voldoet, anders `null`. **Bij elke verbinding** lost `GuardedBackend` (een
httpcore-netwerklaag in `GuardedTransport`) de naam zelf op, weigert als één opgelost adres verboden is (loopback zonder
ontwikkelvlag, link-local, `0.0.0.0`/`::`, multicast, IPv4 in IPv6) en verbindt alleen met een gecontroleerd adres: geen
tweede DNS-vraag tussen controle en verbinding. TLS gebruikt de hostnaam voor SNI en certificaat.

### 5. JWT (C1.3-ZPA-07)

`aud` moet precies één string zijn (PyJWT accepteert ook een lijst; die wordt nu `invalid_token`). Een JWS-header zonder
`kid` is `unknown_key`. Ook een POST met een onleesbare body (die eerst authenticeert) verbruikt de `jti`.

### 6. Events (C1.3-ZPA-05)

Vaste `service.*`-namen (`service.hub_link.linked`, `…unlinked`, `…revoked`, `service.token.rotated`,
`service.key_rotation.applied`, `service.key_rotation.rejected`) met `entity: {type: "service", id: <app_id>}`; alleen de
domeinen `search` en `voice` plus `app.health.changed`. De oude namen worden niet meer gestuurd (het contract staat één
release beide toe; deze dienst heeft nog geen release, dus er is geen overgang nodig). De worker controleert de
inboxgrenzen vóór verzending; een event dat de inbox zou weigeren, gaat niet over het LAN maar direct naar dead-letter
met de reden. `payload.ernst` (vijf woorden) bij `revoked` en `key_rotation.rejected`: `hoog`.

### 7. Connector-health (C1.3-ZPA-10)

Vaste namen `database`, `worker`, `jobs`, `hub_link` (was `hub_koppeling`) plus eigen namen; totaal = slechtste check.
De worker schrijft na elke ronde een hartslag (`service_state.worker_heartbeat`); ouder dan `max_age` (6 × het
werkinterval, minstens 120 s) → `unknown` met `meting_verouderd` en de tijd van de laatste meting. `discover` noemt
`limits.max_age_s`. Twee keuzes:

- **Niet-gebouwde functies** (ingest, spraak) zijn geen gezondheidscheck meer (ze stonden als `unknown` in de checks,
  maar telden niet mee). Ze staan als `capability_unavailable` in `discover.functions`, zoals "Nog niet beschikbaar"
  (fase, telt niet) op de statuspagina. Anders zou het totaal tot P5 nooit `healthy` kunnen zijn.
- **Nog geen share** is een open instelling (een gate, ONTWERPTAAL §4.1), geen gezondheid: dan is er geen check
  `leeswortels`; `discover.instellingen.leeswortels = niet_ingesteld`. Een ingestelde share die genegeerd wordt, is wél
  een check (`degraded`). Op de statuspagina: "Niet ingesteld" (gate, telt niet mee) of "Beperkt" (telt mee).

### 8. API (C1.3-ZPA-11, foutcatalogus)

Statische test `apps/api/tests/unit/test_idempotency_routes.py` met een uitzonderingslijst (formulier-POST van de
statuspagina; `propose`/`apply`/`rollback` zolang ze 501 geven). `POST /voice/v1/sessions` (501) vraagt nu ook een
`Idempotency-Key`: het contract kent geen uitzondering voor een 501-route buiten de connector. Foutcatalogus:
`idempotency_key_reused` (422, was 409 `idempotency_key_conflict`), ongeldige cursor en onbekend itemtype →
422 `validation_error`, tijdslimiet van een zoekopdracht → 503 `dependency_unavailable` met `details.reason`
`zoeken_tijdslimiet` en `Retry-After`, 412 met `details.huidige_etag`.

### 9. Image (C1.3-ZPA-09)

Bij een start als root leegt de bewaker eerst de **bounding set** (`PR_CAPBSET_DROP` voor elke capability tot
`cap_last_cap`; `EINVAL` = onbekend bij de kernel, overslaan) en stopt hij met een Nederlandse melding als dat niet lukt;
daarna `setgroups([])`, gid en uid 568 en `no_new_privs`. De healthcheck doet hetzelfde als eerste handeling en heeft een
controlevlag `--controle`. De rooktest controleert per proces `CapEff`/`CapPrm`/`CapAmb` 0, `NoNewPrivs 1`, en bij een
root-start `CapBnd 0`; de healthcheck apart. Met "Custom User 568" mag `CapBnd` de Docker-standaardset zijn.

### 10. Peil 1.1.1 op de statuspagina

- Renderer in één doorgang (E07), byte-gelijk getest met de referentie `bouw/sjabloon.py` op de gepinde commit.
- Tonen: `niet-gekoppeld` (ook in de controles), `nog-niet` voor P5-functies, `gate` voor bronnen en een niet ingestelde
  share, `verouderd` voor een oude hartslag; `reden_code` per rij; `melding_rol` `alert`/`status`/leeg; het blok
  `koppelstatus_onleesbaar` met `melding_code` en `melding_pad` zonder trace-ID; `koppelen_bezig` (geen formulier, met
  starttijd); `bron` noemt dat het totaal ook de connector-gezondheid meeneemt.
- **Gereed** vraagt de bevestiging door de Hub (`confirmed_at`, contract 1.3 §3.7). `gereed_uitleg` vervangt de
  standaardzin van het sjabloon: die zegt "gekoppeld" (zonder bevestiging) en gebruikt "in orde", dat volgens
  ONTWERPTAAL §11.1 niet als woord voor Gezond hoort.
- Totaalregel E10: een verse Beperkt/Storing in het dienstblok maakt het totaal hooguit Beperkt; gate, nog-niet en
  verouderd in het dienstblok tellen niet mee.
- Controles: 320, 360, 390, 768, **1024** en 1440 px, licht en donker; reflow op 320 px ook met **DejaVu Sans**
  (`PEIL_LETTER`, E12); axe-tags tot en met `wcag22aa`.

## Niet in deze ronde (met reden)

- **C1.3-ZPA-13 (manifest):** de validator van `appstore-en-fork` kent `healthcheck.monitor` en de kop 1.3 nog niet
  (C1.3-ASF-08 staat open). Het manifest heeft al geen `HUB_*` in parameters of `custom_app.env` (conform).
- **C1.3-ZPA-14 (één producent, Δ-ZK-1/-2):** fase P5, na BG-06.
- **Log-sink en heartbeat naar de Hub (§6.1):** optioneel; niet gebouwd.

## Gevolgen

- Contractmeldingen in het eindrapport: de keuze voor directe POST-antwoorden (geen PRG), het weglaten van niet-gebouwde
  functies en een niet ingestelde share uit de connector-health, `hub_incompatibel` voor andere onverwachte 4xx, en de
  eigen `gereed_uitleg`.
- **Achter een HTTPS-proxy** (later, B10/B13) stuurt de browser `Origin: https://…`, terwijl de dienst zelf HTTP ziet
  en proxy-koppen niet vertrouwt: de exacte `Origin`-controle weigert dan het formulier. Dat is de veilige kant;
  koppelen gaat dan via een koppeltoken (env-enrollment, aanbevolen) of via de Hub. Een vertrouwde proxy instellen
  vraagt een eigen ADR.
- **NIET GEVALIDEERD:** welk bron-IP-adres de dienst achter de poortdoorsturing van TrueNAS ziet; het gedrag van een
  echte schermlezer, telefoon of tv; de bounding set en de healthcheck op een echte TrueNAS (alleen in Docker op de
  ontwikkelmachine getest); de echte Hub (alleen de simulator, die 1.3 volgt).
