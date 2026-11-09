# ADR-0015 — QA statuspagina (contract 1.2 §3.7, Peil)

**Status:** aanvaard. **Datum:** 2026-11-09. **Contract:** 1.2 §3.5, §3.7, §5, §11 (conformiteitspunt A4), §13.
**Ontwerptaal:** Peil 1.1.0 (ONTWERPTAAL §1, §4.3, §11.1, §13.2). **Bouwt voort op:** ADR-0013 en ADR-0014.

## Context

Een onafhankelijke QA-ronde na ZA9 (statuspagina in Peil, ADR-0013) en de ontwerpreview (ADR-0014). Alles is opnieuw
gedraaid zoals CI het doet: lint, typen, unit, integratie, drift van de Peil-kopie, E2E, beide UI-routes, de image en
de rooktest. Alles was groen. Daarna zijn de code en de documenten gelezen. Dat leverde zeven bevindingen op die geen
test ving:

| # | Bevinding | Gevolg |
|---|---|---|
| Q-01 | Een foutmelding kwam uit de URL (`?melding=…`), ook zonder poging. Een gedeelde of zelfgemaakte link toonde dan bijvoorbeeld *Deze dienst is al eens gekoppeld* naast het koppelformulier van een dienst die nog nooit gekoppeld was. | De pagina beweerde iets wat niet waar is (ONTWERPTAAL §1 punt 1). |
| Q-02 | `POST /koppelen` kortte de invoer af vóór de controle (`koppelcode[:32]`, `hub_vingerafdruk[:64]`, `hub_adres[:500]`). Een te lange koppelcode waarvan het begin een geldige code is, ging zo toch naar de Hub. | Invoer die volgens §3.3 ongeldig is (koppelcode > 32 tekens), werd geaccepteerd. |
| Q-03 | De Origin-controle accepteert `Origin: null`. Dat moet, omdat de eigen pagina `Referrer-Policy: no-referrer` zet en de eigen formulier-POST daardoor `null` stuurt. Maar een andere app op hetzelfde NAS-adres kan dat ook: een andere poort is dezelfde site, dus SameSite-cookies gaan mee, en de cookies van die app gelden ook voor deze poort. | De CSRF-bescherming hing in dat geval alleen aan de double-submit-cookie. |
| Q-04 | Een koppelpoging op een onleesbaar koppelbestand, uit een oud tabblad, gaf *al eens gekoppeld*. Of de dienst ooit gekoppeld was, is dan onbekend. | De melding was niet eerlijk. |
| Q-05 | De toelichting bij *Niet gereed* begon altijd met *Deels onbekend*, ook als de pil *Beperkt* (amber) was, bijvoorbeeld bij ontkoppeld met vastgelopen gebeurtenissen. Een onbekende koppeltoestand kreeg de toelichting *Gereed; …*. | Pil en woord spraken elkaar tegen (Peil §11.1). |
| Q-06 | `CUSTOM_APP.md` beloofde dat de Trace-ID van de pagina ook in de app-log staat. Dat was niet zo: de Trace-ID stond alleen in het auditspoor en in `X-Trace-Id`. Ook noemde het document een melding (*Loopback-adressen …*) die de statuspagina niet meer toont. | De documentatie klopte niet met de dienst. |
| Q-07 | In `EVIDENCE.json` stond bij vijf taken met status *Niet gestart* een lege `validatie` (B07-005, B15-001, B15-003, B15-004, B15-007). Een lege waarde is geen lijstwaarde (A4). De bewakende test liet dat toe, en liet ook toe dat elke status en elke validatiewaarde bij elk Excel-blad werd gebruikt. | Afwijking van §11/A4. De test van de contracteigenaar (`ai-server-manager`) eist altijd een lijstwaarde. |

## Besluiten

1. **Een foutmelding hoort bij een echte, recente poging** (Q-01). `GET /` toont een foutmelding alleen als de
   poging-sleutel uit de redirect geldig is en bij dezelfde code hoort. Zo'n sleutel is willekeurig, staat alleen in
   het geheugen van dit proces en vervalt na 15 minuten. Een link zonder geldige sleutel toont geen melding; de pagina
   toont dan gewoon de toestand. Een melding mag de koppelstatus bovendien nooit tegenspreken:
   - *gekoppeld* alleen als de koppeling bestaat (was al zo, ADR-0013);
   - *al eens gekoppeld* nooit naast het formulier;
   - *koppelstatus onleesbaar* alleen als het koppelbestand onleesbaar is.

   Gevolg: na een herstart van de dienst is een oude foutmelding weg. Dat is eerlijk: de pagina toont dan de stand van
   nu.
2. **Invoer als geheel controleren, nooit eerst afkappen** (Q-02). De grenzen staan in de controles zelf: koppelcode
   ≤ 32 tekens (§3.3), vingerafdruk ≤ 64, Hub-adres ≤ 500. De totale grootte is begrensd door de bodygrens van 1 MiB
   (§5, vóór routering). Daarna gaan de genormaliseerde waarden naar de Hub.
3. **`Sec-Fetch-Site` als extra herkomstcontrole** (Q-03). Een formulier-POST met `Sec-Fetch-Site` anders dan
   `same-origin` (of `none`) wordt geweigerd met `ongeldig_formulier`, en er gaat niets naar de Hub. Moderne browsers
   sturen die header altijd mee; een andere app op hetzelfde NAS-adres geeft `same-site`, een andere site
   `cross-site`. Zonder de header (oude browser, curl) gelden de double-submit-cookie en de Origin-controle zoals
   voorheen. Dit is een aanscherping van de *Origin-controle* uit §3.7, geen nieuw mechanisme. Het restrisico uit
   §3.7 blijft: wie op het LAN zit, kan een nooit gekoppelde dienst zelf aan een eigen "Hub" koppelen.
4. **Koppelpoging op een onleesbaar koppelbestand** (Q-04). Die gaat niet naar de Hub en overschrijft niets. De poging
   krijgt de eigen melding `koppelstatus_onleesbaar`, met code en Trace-ID. Zonder poging toont de pagina dezelfde
   vaste tekst zonder code: daar staat de code bij de controle *Koppeling met de Hub* (P-14). De melding staat nu in de
   vaste lijst `NOTICES`, niet meer los in de code.
5. **De toelichting volgt de toon van de pil** (Q-05). Bij *Niet gereed* begint de toelichting met *Deels onbekend*,
   *Beperkt* of *Storing*, net als de pil. Een onbekende koppeltoestand (vangnet) geeft *Deels onbekend —
   koppelstatus onbekend*, nooit *Gereed*.
6. **Trace-ID in de app-log** (Q-06). Elke afgehandelde poging op de statuspagina schrijft één logregel met de code
   en de Trace-ID: `statuspagina: koppelen niet gelukt (code …, Trace-ID …)`. Het ingevulde Hub-adres van een
   mislukte poging staat er niet in (§3.7). Bij een geslaagde poging stond het adres al in de log. `CUSTOM_APP.md`
   noemt nu de echte meldingen.
7. **`validatie` is altijd een lijstwaarde** (Q-07). De vijf taken krijgen wat hun acceptatie nodig heeft:
   `NIET GEVALIDEERD (echte NAS)`, `(hardware)` of beide. Dat is waar en verzint niets. De status blijft
   *Niet gestart*, en de uitleg staat in `opmerking`. De test `test_evidence_contract.py` controleert nu per Excel-blad:
   - Testmatrix (`EX..`): `Sim-geslaagd`/`Sim-mislukt`;
   - werkpakketten en installatiestappen (`B..-…`, `I..-..-..`): `Sim-getest`/`Unit-getest`;
   - besluiten (`D..`): hun eigen statussen;
   - overal mag `NIET GEVALIDEERD (…)`; `validatie` is nooit leeg, en `Gereed` en `Geslaagd` komen nooit voor.

   Deze ronde vervangt de keuze uit de vorige test, "leeg mag bij Niet gestart".

## Gevolgen

- Tests: 18 nieuwe tests voor de statuspagina (6 unit, 12 HTTP). Op de oude `status_page.py` falen er 15; dat is
  gecontroleerd. De andere 3 bewaken grenzen die al klopten (te lang Hub-adres, een melding die niet bij de toestand
  past). Daarnaast is de bewakende EVIDENCE-test strenger; op de oude EVIDENCE.json faalt hij op de vijf lege waarden.
- `make ui`: een tiende toestand, `koppelen-op-onleesbaar` (code + Trace-ID in de melding), op 320–1440 px in licht
  en donker. Uitkomst: axe 0, geen overloop, ook niet met ruimere tekstafstand.
- Het Peil-sjabloon is niet gewijzigd (O-14); de kopie blijft byte-gelijk.
- Voorstel voor Peil (S-09, naar `ai-server-manager`):
  - De referentie-renderer vult een plek ook binnen een waarde die al is ingevuld. Een waarde met `{{x}}` erin wordt
    daardoor nog een keer vervangen. In deze dienst komt elke waarde uit een vaste tekst of uit gecontroleerde data,
    en de render blijft byte-gelijk aan de referentie.
  - Voorstel: in de referentie de accolades in waarden escapen (`&#123;`), of in één doorgang vullen.
- **NIET GEVALIDEERD:**
  - of TrueNAS achter de poortdoorsturing het echte bron-IP doorgeeft;
  - het gedrag van een echte schermlezer, telefoon of tv;
  - de echte NAS.
