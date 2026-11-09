# Zoeken en persoonlijke AI installeren als standaard TrueNAS Custom App

*Zoeken en persoonlijke AI* is de dienst van het TrueNAS Home Platform voor de persoonlijke zoekmachine (B07) en
— later — spraak/persoonlijke AI (B15). Het is **één image** waarin alles zit: een eigen PostgreSQL 16 met pgvector
(alleen via een unix-socket), de API met een kleine statuspagina en een worker. Je installeert hem met het **gewone
Custom App-formulier** van TrueNAS: geen YAML, geen *Install via YAML*, geen docker compose, geen terminal, geen
wachtwoorden. Alle schermen (bronnen kiezen, toestemming, zoeken) staan in de Hub; deze app heeft alleen een statuspagina.

> **Normaal installeer je deze app niet zelf.** Na de eerste installatie van de Hub (`ai-server-manager`) en de
> executor installeert het platform deze app via de TrueNAS-API met precies dezelfde formulierwaarden, en geeft het
> `HUB_URL` en een eenmalig koppeltoken mee. Dit document is voor handmatige installatie en voor controle.

> **Verificatiestatus: NIET GEVALIDEERD (echte NAS).** De image is in een Linux/Docker-bouwomgeving gebouwd en met een
> rooktest gestart (zie `docs/TESTRAPPORT.md`). Het TrueNAS-formulier zelf is niet op echte TrueNAS-hardware
> doorlopen; de veldnamen volgen het standaard Custom App-formulier van TrueNAS SCALE 24.10/25.x en kunnen per versie
> iets afwijken. Jouw TrueNAS-versie (D01), poolnaam/dataset (D02) en NAS-adres (D03) zijn onbekend: vul je eigen
> waarden in, nooit de voorbeelden.

## Wat je nodig hebt

| Wat | Waarde |
|---|---|
| Image | `ghcr.io/lvos04/zoeken-en-persoonlijke-ai` |
| Tag | de vaste versie uit de release, bv. `0.1.0` (**nooit** `latest`) |
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
| | Tag | de releaseversie, bv. `0.1.0` |
| | Pull Policy | *Pull the image if it is not already present on host* |
| Container Configuration | Timezone | jouw tijdzone |
| | Restart Policy | *Unless Stopped* |
| | Environment Variables | leeg laten (zie *Optioneel*) |
| Security Context | Privileged | **uit** |
| | Capabilities | geen toevoegen |
| | Custom User | **uit** (de app start als root alleen om de rechten van `/appdata` te regelen en draait daarna als 568) |
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

Kies **Install**. De eerste start duurt ongeveer 10–30 seconden (database aanmaken, pgvector installeren, migraties).
Daarna staat de app op *Running* en na de healthcheck (`/readyz`) op *Healthy*.

**Een share koppelen betekent nog niet dat hij doorzocht wordt.** Pas als een beheerder in de Hub een bron kiest
(bronwizard) én de eigenaar van de gegevens toestemming geeft, wordt die map gebruikt. Een niet-geselecteerde share
wordt nooit gelezen en de index verlaat de NAS niet.

Niet doen: geen Docker-socket koppelen, geen extra host-paden zoals `/`, `/mnt`, `/etc` of `/var/run`, geen *Host
Network*, geen *Privileged*, geen schrijfbare share-koppelingen. Er is geen enkele reden voor in deze app.

## 3. Koppelen met de Hub

- **Geïnstalleerd door het platform:** de Hub geeft `HUB_URL` en een eenmalig koppeltoken mee; de app koppelt
  zichzelf bij de eerste start. Het token is daarna waardeloos.
- **Handmatig geïnstalleerd:** open `http://<NAS-adres>:30450`. De statuspagina toont *niet gekoppeld*. Maak in de
  Hub-UI een koppelcode voor `zoeken-en-persoonlijke-ai` en vul op de statuspagina het Hub-adres
  (`http://<NAS-adres>:<Hub-poort>`, geen `localhost`) en de koppelcode in.

Ontkoppelen (alleen nodig als de Hub opnieuw is geïnstalleerd): in de Hub, of op de statuspagina met de **lokale
ontkoppelcode** die bij elke start in de app-log staat (**Apps** → `zoeken-en-persoonlijke-ai` → **Logs**).

## Optioneel (omgevingsvariabelen)

| Variabele | Wanneer |
|---|---|
| `HUB_URL` | Hub-adres als je het koppeltoken handmatig meegeeft (normaal zet het platform dit) |
| `HUB_ENROLLMENT_TOKEN` | eenmalig koppeltoken uit de Hub (vervalt na 60 min of na gebruik; geen blijvend geheim) |
| `SEARCH_AI_PUBLIC_URL` | adres waarop de Hub deze app bereikt, als dat afwijkt van wat de Hub zelf weet |

Zet hier **nooit** wachtwoorden of API-sleutels. Credentials van Immich/Jellyfin voer je later in de Hub-UI in; de app
bewaart ze versleuteld in `/appdata` (`vault.key` in `/appdata/secrets`).

Spraak (STT/TTS) en een lokaal taalmodel zijn **aparte, optionele apps die standaard uit staan**; deze app neemt
nooit audio op.

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
| `FOUT: geen schrijfrechten op /appdata/...` | *Custom User* staat aan met een andere gebruiker: zet het uit, of geef gebruiker 568 schrijfrechten. |
| `share /bronnen/...: LET OP: schrijfbaar` | Zet bij die opslagregel **Read Only** aan; anders weigert de app toestemming voor die map. |
| `geen shares gekoppeld onder /bronnen` | Geen fout: zonder shares is er niets te doorzoeken. |
| `oude postmaster.pid ... verwijderd` | Geen fout: de vorige stop was onverwacht (stroom/kill). |
| Poort bezet bij opslaan | Kies een andere vrije **Host Port**; de Container Port blijft `30450`. |
| Statuspagina: *Loopback-adressen ... wijzen naar deze container zelf* | Gebruik het LAN-adres van de NAS i.p.v. `localhost`. |
| Image kan niet worden opgehaald (`denied`) | Het GHCR-pakket staat nog op privé (eenmalig openbaar maken in GitHub → Packages). |
