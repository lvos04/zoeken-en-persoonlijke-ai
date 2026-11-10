# Zoeken en persoonlijke AI installeren als standaard TrueNAS Custom App

*Zoeken en persoonlijke AI* is de dienst van het TrueNAS Home Platform voor de persoonlijke zoekmachine (B07) en
— later — spraak/persoonlijke AI (B15). Het is **één image** waarin alles zit: een eigen PostgreSQL 16 met pgvector
(alleen via een unix-socket), de API met een kleine statuspagina en een worker. Je installeert hem met het **gewone
Custom App-formulier** van TrueNAS: geen YAML, geen *Install via YAML*, geen docker compose, geen terminal, geen
wachtwoorden. Alle schermen (bronnen kiezen, toestemming, zoeken) staan in de Hub; deze app heeft alleen een statuspagina.

> **Normaal installeer je deze app niet zelf.** Na de eerste installatie van de Hub (`ai-server-manager`) en de
> executor installeert het platform deze app via de TrueNAS-API met precies dezelfde formulierwaarden, en geeft het
> alleen `HUB_URL`, `HUB_KEY_FINGERPRINT` en een eenmalig koppeltoken (`HUB_ENROLLMENT_TOKEN`) mee
> (platformcontract 1.3 §3.2). Dit document is voor handmatige installatie en voor controle.

> **Verificatiestatus: NIET GEVALIDEERD (echte NAS).** De image is in een Linux/Docker-bouwomgeving gebouwd en met een
> rooktest gestart (zie `docs/TESTRAPPORT.md`). Het TrueNAS-formulier zelf is niet op echte TrueNAS-hardware
> doorlopen; de veldnamen volgen het standaard Custom App-formulier van TrueNAS SCALE 24.10/25.x en kunnen per versie
> iets afwijken. Jouw TrueNAS-versie (D01), poolnaam/dataset (D02) en NAS-adres (D03) zijn onbekend: vul je eigen
> waarden in, nooit de voorbeelden.

## Wat je nodig hebt

| Wat | Waarde |
|---|---|
| Image | `ghcr.io/lvos04/zoeken-en-persoonlijke-ai` (publiek pakket; geen registry-login nodig, zie *Image ophalen*) |
| Tag | versie **met digest** uit de release: `<versie>@sha256:<64 hex>` uit `release/digests.json`, bv. `0.1.0@sha256:…` (**nooit** `latest`) |
| Poort | container `30450` → host `30450` (voorstel; de Hub controleert vooraf op conflicten) |
| Opslag | één dataset → `/appdata` (database, geheimen, back-ups, afgeleide index) |
| Shares om te doorzoeken | optioneel: per share een extra Host Path **met Read Only** → `/bronnen/<naam>` |
| Omgevingsvariabelen | geen verplichte; zie *Optioneel* |

## 1. Dataset maken

**Datasets** → kies je pool → **Add Dataset**:

- Name: `zoeken-en-persoonlijke-ai` (bijvoorbeeld onder je apps-dataset; gebruik je eigen poolnaam)
- Dataset Preset: **Apps**

Rechten hoef je niet aan te passen: bij de eerste start maakt de app gebruiker `568` (apps) eigenaar van deze dataset
en van zijn eigen submappen (`postgres`, `secrets`, `backups`). Shares onder `/bronnen` worden **nooit** aangepast.

## 2. Custom App aanmaken

**Apps** → **Discover Apps** → **Custom App**. Vul in:

| Sectie | Veld | Waarde |
|---|---|---|
| Application Name | | `zoeken-en-persoonlijke-ai` |
| Image Configuration | Repository | `ghcr.io/lvos04/zoeken-en-persoonlijke-ai` |
| | Tag | `<versie>@sha256:<digest>` uit `release/digests.json` van de release (zie hieronder) |
| | Pull Policy | *Pull the image if it is not already present on host* |
| Container Configuration | Timezone | jouw tijdzone |
| | Restart Policy | *Unless Stopped* |
| | Environment Variables | leeg laten (zie *Optioneel*) |
| Security Context | Privileged | **uit** |
| | Capabilities | geen toevoegen |
| | Custom User | **uit** (aanbevolen: de app start als root alleen om de rechten van `/appdata` te regelen en draait daarna als 568, ook het hoofdproces) — of **aan met User ID en Group ID `568`** als de dataset al van 568 is |
| Network Configuration | Host Network | **uit** |
| | Ports → Add | Container Port `30450`, Host Port `30450`, Protocol TCP |
| Storage Configuration | Add → Type | **Host Path** (of ixVolume) |
| | Mount Path | `/appdata` |
| | Host Path | het pad van de dataset uit stap 1 |
| Storage Configuration (optioneel, per share) | Add → Type | **Host Path** |
| | Mount Path | `/bronnen/<naam>`, bv. `/bronnen/documenten` |
| | Host Path | de share (dataset) die je doorzoekbaar wilt maken |
| | **Read Only** | **aan** (verplicht: zonder alleen-lezen weigert de app toestemming voor die map) |
| Resources (optioneel) | CPUs / Memory | 1 CPU / 768 MiB voor P0 (niet gemeten op een echte NAS) |
| GPU | | geen |

### Tag met digest

Vul bij **Tag** de versie mét digest in, precies zoals in `release/digests.json` van de GitHub-release staat:
`<versie>@sha256:<64 hex>` (bijvoorbeeld `0.1.0@sha256:` gevolgd door 64 tekens). Het formulier maakt daar
`ghcr.io/lvos04/zoeken-en-persoonlijke-ai:<versie>@sha256:<digest>` van: TrueNAS haalt dan precies die image op, ook als
iemand de versietag later zou verplaatsen.

> **NIET GEVALIDEERD (echte NAS):** of het TrueNAS-formulier een tag met `@` accepteert en of TrueNAS daarmee nog
> updates herkent, is niet op een echte NAS getest (contract 1.3 §2.2). Weigert het formulier de tag, vul dan alleen
> de versie in (`0.1.0`); het platform controleert na installatie de digest (readback via de TrueNAS-API) en stopt bij
> een afwijking. Er is in deze bouwronde nog geen release gemaakt; tot dan bestaat er geen digest om in te vullen.

### Image ophalen (publiek; registry-login alleen als terugval)

De GHCR-pakketten worden publiek (besluit 3 van de gebruiker, na een scan van de volledige historie). Dan is er
**geen** registry-login nodig. Zolang het pakket nog privé is, lukt ophalen niet (`denied`). Alleen als terugval: stel in
TrueNAS (**Apps** → **Configuration** → **Manage Container Image Registries**) een registry `ghcr.io` in met een token dat
alleen pakketten mag **lezen**. Dat token is een geheim van jou in TrueNAS: nooit in dit formulier, een
omgevingsvariabele, een manifest of een repo. Het gedrag van TrueNAS met GHCR-tokens is **NIET GEVALIDEERD**.

Kies **Install**. De eerste start duurt ongeveer 10–30 seconden (database aanmaken, pgvector installeren, migraties).
Daarna staat de app op *Running* en na de healthcheck (`/readyz`) op *Healthy*.

**Een share koppelen betekent nog niet dat hij doorzocht wordt.** Pas als een beheerder in de Hub een bron kiest
(bronwizard) én de eigenaar van de gegevens toestemming geeft, wordt die map gebruikt. Een niet-geselecteerde share
wordt nooit gelezen en de index verlaat de NAS niet. Mappen en bestanden die via een symbolische koppeling
(symlink) naar een andere plek wijzen — bijvoorbeeld naar een andere share — worden nooit gelezen; kies in de
bronwizard altijd de echte map.

Niet doen: geen Docker-socket koppelen, geen extra host-paden zoals `/`, `/mnt`, `/etc` of `/var/run`, geen *Host
Network*, geen *Privileged*, geen schrijfbare share-koppelingen. Er is geen enkele reden voor in deze app.

## 3. Koppelen met de Hub

- **Geïnstalleerd door het platform:** de Hub geeft `HUB_URL`, `HUB_KEY_FINGERPRINT` en een eenmalig koppeltoken mee;
  de app koppelt zichzelf bij de eerste start, controleert de vingerafdruk van de Hub en bewaart de koppeling in
  `/appdata/secrets/hub-link.json` (alleen leesbaar voor 568, niet in de database). Het token is daarna waardeloos.
- **Handmatig geïnstalleerd, aanbevolen:** maak in de Hub-UI (Diensten) een koppeltoken voor
  `zoeken-en-persoonlijke-ai` en vul bij **Environment Variables** `HUB_URL` (`http://<NAS-adres>:<Hub-poort>`, geen
  `localhost`, geen pad), `HUB_ENROLLMENT_TOKEN` en `HUB_KEY_FINGERPRINT` (de vingerafdruk die de Hub naast het token
  toont) in. Het token vervalt na 60 minuten of na gebruik.
- **Handmatig, zonder formulierwijziging:** open `http://<NAS-adres>:30450`. Zolang de app **nog nooit** gekoppeld is,
  toont de statuspagina een formulier met Hub-adres, koppelcode en Hub-vingerafdruk (alle drie verplicht; de Hub-UI
  toont koppelcode en vingerafdruk samen). Hooguit 5 pogingen per kwartier per bron-IP-adres en 20 per kwartier voor
  de hele app; er loopt hooguit één koppelpoging tegelijk. Welk bron-IP-adres de app achter de poortdoorsturing van
  TrueNAS ziet (jouw apparaat of één gateway-adres voor iedereen), is **NIET GEVALIDEERD (echte NAS)**; in het
  laatste geval delen alle apparaten de limiet van 5. Elke verzending telt mee, ook een verlopen formulier of een
  verkeerd ingevuld veld.
- **Open de statuspagina via het IP-adres van de NAS** (of een naam zonder punt, of een naam onder `.local`, `.lan`,
  `.internal` of `.home.arpa`). Via een andere domeinnaam (bijvoorbeeld een eigen domein of een publieke naam die naar
  de NAS wijst) toont de pagina *Koppelen kan niet via dit adres* en weigert hij het formulier: zo kan een website die
  je bezoekt de app niet via DNS-rebinding aan een eigen "Hub" koppelen (contract 1.3 §3.7). Gebruik dan het IP-adres,
  of koppel met een koppeltoken (hierboven).

**Ontkoppelen of opnieuw koppelen gaat alleen via de Hub** (of met een nieuw `HUB_ENROLLMENT_TOKEN` in dit formulier:
wie het formulier kan wijzigen is TrueNAS-beheerder). Op de statuspagina kan dat niet meer, en er is geen lokale
ontkoppelcode meer in de app-log: zo kan niemand op het netwerk een gekoppelde dienst aan een andere "Hub" hangen.
Vergelijk na het koppelen de vingerafdruk op de statuspagina met die in de Hub-UI.

## Optioneel (omgevingsvariabelen)

| Variabele | Wanneer |
|---|---|
| `HUB_URL` | Hub-adres als je het koppeltoken handmatig meegeeft (normaal zet het platform dit): `http(s)://<host>[:<poort>]` zonder pad; een IP-adres alleen als `a.b.c.d` (geen verkorte vorm zoals `127.1`), IPv6 tussen `[ ]` |
| `HUB_ENROLLMENT_TOKEN` | eenmalig koppeltoken uit de Hub (`enr_…`, vervalt na 60 min of na gebruik; geen blijvend geheim) |
| `HUB_KEY_FINGERPRINT` | vingerafdruk van de Hub-sleutels (`XXXX-XXXX-XXXX-XXXX`, niet geheim); ingevuld ⇒ de app koppelt alleen als hij klopt. Zonder deze waarde vertrouwt de app de eerste Hub (*trust on first use*) en toont hij de vingerafdruk ter vergelijking |
| `ENROLLMENT_TOKEN` | **verouderd** (oude naam van `HUB_ENROLLMENT_TOKEN`): wordt in deze release nog gelezen met een waarschuwing in de log |
| `SEARCH_AI_PUBLIC_URL` | adres waarop de Hub deze app bereikt, als dat afwijkt van wat de Hub zelf weet. Alleen informatief: voldoet het niet aan de adresregels, dan stuurt de app het niet mee (de Hub gebruikt altijd zijn eigen register) |

Zet hier **nooit** wachtwoorden of API-sleutels. Credentials van Immich/Jellyfin voer je later in de Hub-UI in; de app
bewaart ze versleuteld in `/appdata` (`vault.key` in `/appdata/secrets`).

Spraak (STT/TTS) en een lokaal taalmodel zijn **aparte, optionele apps die standaard uit staan**; deze app neemt
nooit audio op.

## Bewaking (monitorplan)

Het manifest geeft een monitorplan (platformcontract 1.3 §9): **HTTP** op `/readyz` van deze app (de poort uit stap 2),
verwachte status **200**, time-out **10 s**, elke **60 s**. `/readyz` vraagt geen aanmelding en toont geen geheimen; de
koppeling met de Hub telt er bewust niet in mee (een nog niet gekoppelde app is wél klaar). De Hub maakt hier een
monitorvoorstel van; jij neemt het over in Uptime Kuma en bevestigt het per monitor. Dat de monitor op jouw NAS werkt,
is **NIET GEVALIDEERD** (geen echte NAS in deze bouwomgeving).

## Updaten en terugdraaien

- **Updaten:** app → **Edit** → Tag naar de nieuwe versie → **Update**. Bij openstaande databasewijzigingen maakt de
  app eerst een back-up (`/appdata/backups/voor-upgrade-*.dump`, de nieuwste 3 blijven) en voert dan de migraties uit.
  Mislukt een migratie, dan blijft de oude database ongewijzigd en meldt de log dat. Een ZFS-snapshot vooraf blijft
  verstandig.
- **Wat staat er in die back-up?** Bronnen, toestemmingen, toegangslijsten, koppeling en auditspoor. **Niet** de inhoud
  van de zoekindex (tekstfragmenten, embeddings, documentverwijzingen), de zoekgeschiedenis of spraaktranscripten:
  zo blijft een ingetrokken bron ook niet in een back-up achter. Na een herstel uit deze back-up is de index leeg en
  moet hij opnieuw worden opgebouwd.
- **Terugdraaien:** zolang een release geen databasewijziging heeft (zie de releasenotes, veld
  `tag_only_rollback_safe`), kun je de Tag terugzetten. Bij een schemawijziging: zet de ZFS-snapshot van vóór de
  update terug, of gebruik de back-up uit `/appdata/backups`. De index is afgeleide data en kan altijd opnieuw worden
  opgebouwd uit de bronnen; originele bestanden worden nooit door deze app gewijzigd.
- **Privacy en snapshots:** intrekken wist de indexdata van een bron in de database van de app. **ZFS-snapshots** van
  de `/appdata`-dataset die jij of TrueNAS eerder maakte, bevatten de index nog zoals hij toen was; die kan de app niet
  wissen. Bewaar zulke snapshots niet langer dan nodig als je een bron om privacyredenen intrekt. Een herstelde oude
  snapshot zet ook oude toestemmingen terug: controleer daarna in de Hub de bronnen en toestemmingen.

## Problemen

| Melding in de app-log | Oplossing |
|---|---|
| `FOUT: /appdata bestaat niet` | Opslagregel ontbreekt of Mount Path is niet precies `/appdata`. |
| `FOUT: de app draait als gebruiker <uid>:<gid> ...` | *Custom User* staat aan met een andere gebruiker dan 568: zet het uit (aanbevolen), of zet User ID en Group ID allebei op `568`. |
| `FOUT: geen schrijfrechten op /appdata/...` | De app draait als 568 maar de dataset is niet van 568: zet *Custom User* uit (dan regelt de app de rechten zelf), of geef gebruiker 568 schrijfrechten op de dataset. |
| `share /bronnen/...: LET OP: schrijfbaar` | Zet bij die opslagregel **Read Only** aan; anders weigert de app toestemming voor die map. |
| `geen shares gekoppeld onder /bronnen` | Geen fout: zonder shares is er niets te doorzoeken. |
| `oude postmaster.pid ... verwijderd` | Geen fout: de vorige stop was onverwacht (stroom/kill). |
| Poort bezet bij opslaan | Kies een andere vrije **Host Port**; de Container Port blijft `30450`. |
| Statuspagina: *Het adres van de Hub is ongeldig* (bij het veld: *Gebruik http(s)://<NAS-adres>:<poort>, zonder pad en geen localhost*) | Vul het LAN-adres van de NAS met de poort van de Hub in, zonder pad. `localhost` en `127.0.0.1` wijzen naar deze container zelf. |
| Statuspagina: *Koppelstatus onleesbaar* | Het koppelbestand `/appdata/secrets/hub-link.json` is beschadigd, of het heeft een verkeerde eigenaar of verkeerde rechten; de app weigert uit veiligheid elke Hub-aanvraag en toont geen formulier. Koppel opnieuw via de Hub: die geeft een nieuw koppeltoken mee (of vul het zelf in bij `HUB_ENROLLMENT_TOKEN` in dit formulier). Het onleesbare bestand blijft daarbij bewaard als `hub-link.json.onleesbaar-<tijd>`. |
| Statuspagina: *Koppelen kan niet via dit adres* / code `host_niet_toegestaan` | Je opende de pagina via een domeinnaam die koppelen niet toelaat (bescherming tegen DNS-rebinding). Open hem via het IP-adres van de NAS, of koppel met een koppeltoken. |
| Statuspagina: *Formulier geweigerd* / code `herkomst_geweigerd` | Het formulier kwam niet van deze pagina zelf (een andere app of website, of een browser die de herkomst niet meestuurt). Open `http://<NAS-adres>:30450` en vul het formulier daar in. |
| Statuspagina: *Er loopt al een koppelpoging* / *Koppelen bezig* | Een koppeltoken uit de app-instellingen wordt nog afgehandeld (bijvoorbeeld omdat de Hub even niet bereikbaar is). Wacht tot die klaar is en laad de pagina opnieuw. |
| Image kan niet worden opgehaald (`denied`) | Het GHCR-pakket staat nog op privé (wordt publiek na de scan; tot dan alleen de registry-terugval hierboven). |
| Formulier weigert de tag met `@sha256:` | Vul alleen de versie in; het platform controleert de digest na installatie (NIET GEVALIDEERD op een echte NAS). |
| Statuspagina: *De vingerafdruk van de Hub klopt niet* | Er is niets opgeslagen. Vergelijk de vingerafdruk met de Hub-UI; bij twijfel niet koppelen. |
| Statuspagina: *ingetrokken* of *ontkoppeld* | De Hub heeft de koppeling beëindigd. Opnieuw koppelen gaat via de Hub (nieuw koppeltoken). |
| Statuspagina: melding met **Code** en **Trace-ID** | Selecteer en kopieer de Trace-ID; dezelfde ID staat in de app-log (regel *statuspagina: koppelpoging afgehandeld (code …, HTTP …, Trace-ID …)*) en in het auditspoor van die poging. Een melding hoort alleen bij het antwoord op jouw verzending; laad je de pagina opnieuw, dan zie je de stand van nu. |
| Statuspagina: *Niet gereed — Deels onbekend, wacht op koppeling* | Geen storing: de app werkt, maar doet niets tot hij gekoppeld is en de Hub de koppeling heeft bevestigd. `/readyz` geeft dan gewoon 200, zodat TrueNAS de app niet herstart. |
| Statuspagina: *Achtergrondtaken (worker): Verouderd* of *Onbekend* | De worker heeft al een tijd geen hartslag gegeven. Kijk in de app-log; een herstart van de app start de worker opnieuw. |
| Statuspagina: code `peil_sjabloon_ongeldig` (HTTP 500) | Het sjabloon in de image ontbreekt of is gewijzigd. De API werkt door. Installeer dezelfde versie opnieuw (tag + digest). |
| `HUB_KEY_FINGERPRINT heeft geen geldig formaat` | Vul de vingerafdruk exact over uit de Hub-UI (16 tekens in groepen van 4). |
| `FOUT: de app kon haar rechten niet afgeven` | Bij een start als root leegt de app eerst de *bounding set* van capabilities en wisselt dan naar 568 (contract 1.3 §2.2). Lukt dat niet, dan start de app bewust niet. Start de app opnieuw; blijft dit gebeuren, meld het met de log. |
