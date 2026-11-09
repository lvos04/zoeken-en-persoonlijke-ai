# Schermafdrukken — statuspagina in Peil 1.1.0

Gemaakt met `make ui` (`tests/ui/render_toestanden.py` + `tests/ui/statuspagina.mjs`): de **echte renderer** van de
dienst (`search_ai.status_page.render_page`) met het versie-gepinde Peil-sjabloon, geserveerd met de echte
beveiligingskoppen (CSP `default-src 'none'`), in Chromium (Playwright 1.65.0). Volledige pagina, apparaatschaal 1.

**Gegevens zijn fictief (simulator):** Hub-adres op `.invalid` (bewust lang, voor de overlooptest), vingerafdruk,
Trace-ID en tijd (9 nov 2026, 10:56 UTC) zijn testwaarden. Geen echte NAS, Hub of database.

| Toestand | Wat je ziet |
|---|---|
| `niet-gekoppeld` | Totaal *Niet gereed* (onbekend, gestippeld), koppelblok *Niet gekoppeld*, formulier met drie verplichte velden |
| `koppelen-mislukt` | Melding *Koppelen is mislukt* met code `hub_weigerde` en selecteerbare Trace-ID; veldfout bij de koppelcode |
| `gekoppeld` | *Alles gezond*; neutrale pil *Gekoppeld* (nooit groen), Hub-adres en vingerafdruk; geen formulier |
| `ingetrokken` | *Niet gereed*; *Ingetrokken* — opnieuw koppelen gaat via de Hub; geen formulier |
| `niet-gereed` | *Niet gereed* met storing in de eigen database; schema/pgvector *Onbekend* (niet gemeten), nooit groen |

Bestanden: `<toestand>-<360|768|1440>-<licht|donker>.png` (30 stuks). `CONTROLE.json` is het rapport van dezelfde run:
8 toestanden (ook `gekoppeld-beperkt`, `ontkoppeld`, `koppelstatus-onleesbaar`) × 320/360/390/768/1440 × licht/donker
= 80 controles op axe-core 4.14.0 (WCAG 2.0/2.1/2.2 A+AA en best practices), horizontale overloop, JavaScript,
CSP-schendingen, externe verzoeken en doelgrootte ≥ 44 px.

Niet gedaan: echte schermlezer, telefoon en tv (ONTWERPTAAL §15 punt 7), en de ontwerpreview achteraf door de
design-agent — **NIET GEVALIDEERD**.
