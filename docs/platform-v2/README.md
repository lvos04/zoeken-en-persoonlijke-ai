# TrueNAS Home Platform V2 — documentatie voor `zoeken-en-persoonlijke-ai`

Bron: uitvoeringspakket `TrueNAS_Home_Platform_V2_VOLLEDIG_2026-11-09.zip` (peildatum 9 november 2026).

**Rol van deze repo (vast besluit):** B07 persoonlijke zoekmachine (pgvector) + B15 spraak/persoonlijke AI.

Hier staan alleen de eigen B-, I-, A- en promptdocumenten plus de gedeelde architectuur (`00_START`, `01_ARCHITECTUUR`, `05_TESTS`). Het volledige pakket en de Excel staan in `lvos04/ai-server-manager` onder `docs/platform-v2/`.

Onbekende NAS-gegevens (D01–D18) worden nooit met voorbeeldwaarden ingevuld; ze worden via de onboarding-wizard in de web-UI vastgelegd.

## Bestanden

- `00_START/BESLUITEN_EN_ONBEKENDEN.md`
- `00_START/INHOUDSOPGAVE.md`
- `00_START/INHOUD_CONTROLE.json`
- `00_START/LEES_MIJ_EERST.md`
- `00_START/SHA256SUMS.txt`
- `01_ARCHITECTUUR/01_PROJECTCHARTER.md`
- `01_ARCHITECTUUR/02_DATA_EIGENAARSCHAP.md`
- `01_ARCHITECTUUR/03_RISICO_AUTONOMIE.md`
- `01_ARCHITECTUUR/04_GEGEVENS_EN_STORAGE.md`
- `01_ARCHITECTUUR/05_OPERATIES_EN_UPDATES.md`
- `01_ARCHITECTUUR/06_FASERING_EN_GATES.md`
- `01_ARCHITECTUUR/07_API_EN_EVENTSCHEMA.md`
- `01_ARCHITECTUUR/08_UI_EN_INTERACTIES.md`
- `02_BOUWPLANNEN/B07_PERSOONLIJKE_ZOEKMACHINE.md`
- `02_BOUWPLANNEN/B15_SPRAAK_PERSOONLIJKE_AI.md`
- `03_INSTALLATIE/I14_PGVECTOR.md`
- `03_INSTALLATIE/I15_IMMICH.md`
- `03_INSTALLATIE/I25_SEARCH.md`
- `04_BESTAANDE_APPS/A10_IMMICH.md`
- `05_TESTS/GEBRUIKERSWENSEN_TRACEERBAARHEID.md`
- `05_TESTS/INTEGRALE_E2E_TESTMATRIX.md`
- `06_TEMPLATES/PROMPTS_PER_APP/PROMPT_B07_PERSOONLIJKE_ZOEKMACHINE.md`
- `06_TEMPLATES/PROMPTS_PER_APP/PROMPT_B15_SPRAAK_PERSOONLIJKE_AI.md`

## Contract (bindend)

- `PLATFORM_CONTRACTEN.md` — platformcontract `contracts 1.2`, byte-gelijke kopie van `lvos04/ai-server-manager`
  (bron van waarheid; bij verschil wint die repo)
- `CONTRACT_1.2_CONFORMITEIT.md` — wat elke repo voor 1.2 moet veranderen (hier: A1–A7, ZA1–ZA9; zie ADR-0011, ZA9 in ADR-0013)

## Bewijs en inventaris (deze repo)

- `EVIDENCE.json` — status/validatie per Excel-ID (contract §11; nooit 'Gereed'/'Geslaagd' zonder echte NAS-run)
- `DELTA_MATRIX.md` — inventaris bestaand / onvolledig / ontbrekend
