# zoeken-en-persoonlijke-ai

Dienst van het **TrueNAS Home Platform V2**: de persoonlijke zoekmachine (**B07**, PostgreSQL 16 + pgvector) en
— later — spraak/persoonlijke AI (**B15**). API-dienst met alleen een kleine Nederlandse statuspagina; alle schermen
staan in de Hub-UI van `ai-server-manager`.

**Stand (fase P0):** scaffold, koppelen met de Hub, connectorcontract en **B07-001 Bronselectie en toestemming**
(lees-padenallowlist, consent-register met scopes, gebruikers-ACL vóór ranking, kinderprofiel-beperkingen, intrekken
met gecontroleerde purge). Datamodel B07 + B15 staat in migratie 0001; de logica van B07-002…006 en B15 volgt in P5.
Niet-gebouwde functies geven eerlijk `501 capability_unavailable`. **Niet gevalideerd op een echte NAS.**
Na de onafhankelijke review van P0 (ADR-0008): samengestelde bron-sleutels (migratie 0002), een vectorzoeker met
ACL vóór ranking (alleen intern en getest; de API voor semantisch zoeken blijft 501 zonder lokaal model), intrekken
zonder restdata (ook zoekgeschiedenis en back-ups) en een verzoek- en tijdlimiet op zoeken.

**Platformcontract 1.3** (kopie in [`docs/platform-v2/PLATFORM_CONTRACTEN.md`](docs/platform-v2/PLATFORM_CONTRACTEN.md)
met drifttest, conformiteitslijst [`docs/platform-v2/CONTRACT_1.3_CONFORMITEIT.md`](docs/platform-v2/CONTRACT_1.3_CONFORMITEIT.md),
keuzes in [`docs/adr/ADR-0016-contract-1-3-en-peil-1-1-1.md`](docs/adr/ADR-0016-contract-1-3-en-peil-1-1-1.md); de
1.2-keuzes staan in ADR-0011 en ADR-0012): koppeling in `/appdata/secrets/hub-link.json` met gepinde Hub-sleutels en
een vingerafdruk over alle sleutels, alleen Hub-JWT's (`aud` als één string, `token_use`, `jti` per mutatie),
ontkoppelen/opnieuw koppelen alleen via de Hub, vaste `service.*`-events, connector-health met hartslag van de worker
(totaal = slechtste check), en geen root-proces in de container (ook PID 1 niet; bounding set leeg). Na een 429/503
met `Retry-After` gaat er nooit eerder iets naar de Hub. QA-ronde: [`docs/adr/ADR-0017-qa-contract-1-3.md`](docs/adr/ADR-0017-qa-contract-1-3.md).
Getest tegen een Hub-simulator in CI, en lokaal (interop, niet in CI) tegen de Hub-code van `ai-server-manager` met
contract 1.3. **Niet** tegen een op een NAS geïnstalleerde Hub.

**Statuspagina in Peil 1.1.1** ([`docs/adr/ADR-0013-statuspagina-in-peil.md`](docs/adr/ADR-0013-statuspagina-in-peil.md),
ADR-0016): `GET /` rendert het framework-vrije statuspagina-sjabloon van de ontwerptaal Peil 1.1.1 (versie-gepinde
kopie met herkomstkop in [`design/ontwerptaal/`](design/ontwerptaal/HERKOMST.md); CI faalt bij drift met de bron in
`ai-server-manager`). Geen JavaScript, CSP `default-src 'none'`, `Referrer-Policy: same-origin`, eerste signaal nooit
groen als de dienst niet gereed is. Het koppelformulier weigert een andere herkomst (`Origin`, `Sec-Fetch-Site`) en een
domeinnaam die DNS-rebinding mogelijk maakt (`Host`); de POST krijgt direct de pagina met de melding en de echte
HTTP-status. Ontwerpreview tegen de echte dienst ([`docs/ontwerp/PEIL_REVIEW.md`](docs/ontwerp/PEIL_REVIEW.md)): 16
toestanden × 320/360/390/768/1024/1440 × licht/donker, axe-core 0 overtredingen, reflow ook met DejaVu Sans, en de
negatieve herkomsttests in Chromium; schermafdrukken in
[`docs/screenshots/peil/statuspagina/`](docs/screenshots/peil/statuspagina/).

## Installeren
Uitsluitend als **standaard TrueNAS Custom App** — zie [`deploy/truenas/CUSTOM_APP.md`](deploy/truenas/CUSTOM_APP.md)
(image `ghcr.io/lvos04/zoeken-en-persoonlijke-ai`, poort 30450, opslag `/appdata`, shares optioneel als Read Only Host
Path onder `/bronnen/<naam>`). Normaal installeert het platform de app zelf via de TrueNAS-API. Geen YAML, geen
compose, geen terminal.

## Ontwikkelen
```
make venv            # .venv met de gepinde afhankelijkheden
make lint test e2e   # ruff + mypy, unit/integratie (PostgreSQL + pgvector op 127.0.0.1:5432), dienst-E2E
make ui              # statuspagina in de browser (renderer, zonder database): Peil-controles, axe-core, 320 px, CSP
make ui-echt         # ontwerpreview tegen de echte dienst (PostgreSQL + Hub-simulators): alle toestanden, afdrukken
make openapi         # docs/openapi/zoeken-en-persoonlijke-ai.openapi.json
make image smoke     # alles-in-één image bouwen en de rooktest draaien
```
`deploy/dev/compose.yaml` is alleen voor ontwikkeling.

## Documentatie
- Keuzes: [`docs/adr/`](docs/adr/) · Testrapport: [`docs/TESTRAPPORT.md`](docs/TESTRAPPORT.md)
- Pakketdocs, delta-matrix en bewijs: [`docs/platform-v2/`](docs/platform-v2/) (`EVIDENCE.json`, `DELTA_MATRIX.md`)
- Manifest: [`deploy/manifest/zoeken-en-persoonlijke-ai.manifest.yaml`](deploy/manifest/zoeken-en-persoonlijke-ai.manifest.yaml)
  (contract 1.3 §9, monitorplan op `/readyz`; CI valideert het met de validator van appstore-en-fork in `--strict`)

## Licentie

Licentie: **GPL-3.0** (zie [`LICENSE`](LICENSE); besluit van de eigenaar, 2026-11-09).
