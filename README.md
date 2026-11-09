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

## Installeren
Uitsluitend als **standaard TrueNAS Custom App** — zie [`deploy/truenas/CUSTOM_APP.md`](deploy/truenas/CUSTOM_APP.md)
(image `ghcr.io/lvos04/zoeken-en-persoonlijke-ai`, poort 30450, opslag `/appdata`, shares optioneel als Read Only Host
Path onder `/bronnen/<naam>`). Normaal installeert het platform de app zelf via de TrueNAS-API. Geen YAML, geen
compose, geen terminal.

## Ontwikkelen
```
make venv            # .venv met de gepinde afhankelijkheden
make lint test e2e   # ruff + mypy, unit/integratie (PostgreSQL + pgvector op 127.0.0.1:5432), dienst-E2E
make openapi         # docs/openapi/zoeken-en-persoonlijke-ai.openapi.json
make image smoke     # alles-in-één image bouwen en de rooktest draaien
```
`deploy/dev/compose.yaml` is alleen voor ontwikkeling.

## Documentatie
- Keuzes: [`docs/adr/`](docs/adr/) · Testrapport: [`docs/TESTRAPPORT.md`](docs/TESTRAPPORT.md)
- Pakketdocs, delta-matrix en bewijs: [`docs/platform-v2/`](docs/platform-v2/) (`EVIDENCE.json`, `DELTA_MATRIX.md`)
- Manifest: [`deploy/manifest/zoeken-en-persoonlijke-ai.manifest.yaml`](deploy/manifest/zoeken-en-persoonlijke-ai.manifest.yaml)
