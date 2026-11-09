# B15 — Spraakgestuurde persoonlijke AI (bouwplan)

**Doel:** via tekst en spraak de lokale eigen apps bedienen, met lokale verwerking als voorkeur en dezelfde risicopolicy als AI Server Manager. Geen cloud-voice afhankelijkheid verplicht.

## Schermen en datamodel
Chat met microfoonknop, transcript, broncitaten, privacy-indicator, modelkeuze, uitgevoerde actie-status, opnamegeschiedenis standaard uit. `voice_session`, `transcript`, `intent`, `context_scope`, `tts_job`, `audio_consent`.

## Fasen en functie-eisen

### F0 — B15-001: Lokale spraakinput
**Functioneel gedrag:** Microfoon vanuit telefoon/PWA, push-to-talk standaard, toestemming per browser.
**Techniek:** Capture locally and stream only to configured STT; do not background listen without opt-in.
**Acceptatie:** Denied mic permission shows typed fallback.
**Negatief scenario:** internet uitval, incorrecte spraakherkenning, verlopen sessie en geweigerde toestemming veroorzaken geen ongeautoriseerde apparaat-/serveractie.

### F1 — B15-002: STT privacy en tekstcontrole
**Functioneel gedrag:** Tekst uit spraak in het Nederlands herkend en getoond ter bevestiging.
**Techniek:** Use local STT service where feasible or opted-in allowed provider; scrub sensitive data, transcript retention=off default.
**Acceptatie:** Incorrect herkende ‘verwijder dataset’ blijft tekst en produceert geen action.
**Negatief scenario:** internet uitval, incorrecte spraakherkenning, verlopen sessie en geweigerde toestemming veroorzaken geen ongeautoriseerde apparaat-/serveractie.

### F2 — B15-003: Assistent-intentrouter
**Functioneel gedrag:** Afvragen info over apps, media, huis en netwerk; actie-aanvragen via risk engine.
**Techniek:** Narrow intent classifier + tool allowlist with source citations and user role; multi-turn but no implicit privilege.
**Acceptatie:** ‘Hoeveel verbruikt boiler’ returns my_house measured result; ‘zet boiler uit’ policy denial.
**Negatief scenario:** internet uitval, incorrecte spraakherkenning, verlopen sessie en geweigerde toestemming veroorzaken geen ongeautoriseerde apparaat-/serveractie.

### F3 — B15-004: TTS antwoord
**Functioneel gedrag:** Spreek Nederlands antwoord optioneel uit; mute en geen hard autoplay.
**Techniek:** Local TTS when available, caption transcript and speech interrupt control.
**Acceptatie:** Gemute apparaat speelt niets en text blijft zichtbaar.
**Negatief scenario:** internet uitval, incorrecte spraakherkenning, verlopen sessie en geweigerde toestemming veroorzaken geen ongeautoriseerde apparaat-/serveractie.

### F4 — B15-005: Context en devices
**Functioneel gedrag:** Huidige kamer/profiel/apparaat kan worden gebruikt met expliciete toestemming.
**Techniek:** Context source grants, per-device session, expiry and logout revoke.
**Acceptatie:** Kindprofiel of viewer kan geen admin doelen uitvoeren.
**Negatief scenario:** internet uitval, incorrecte spraakherkenning, verlopen sessie en geweigerde toestemming veroorzaken geen ongeautoriseerde apparaat-/serveractie.

### F5 — B15-006: Privacy/datatools
**Functioneel gedrag:** Opnames nooit standaard opslaan en geen permanente luisterstand.
**Techniek:** No raw audio storage, retention and audit events non-sensitive; downloads available only after consent.
**Acceptatie:** Deleting session clears transcripts, no audio in backup.
**Negatief scenario:** internet uitval, incorrecte spraakherkenning, verlopen sessie en geweigerde toestemming veroorzaken geen ongeautoriseerde apparaat-/serveractie.

### F6 — B15-007: Spraak voor problemen
**Functioneel gedrag:** Vraag ‘waarom download X mislukt’ en krijg bron/oorzaak, gevolgd door veilige herstel-preview.
**Techniek:** Hub incident and MediaGuard/MSO read APIs, in-app action preview.
**Acceptatie:** Geen herzoek zonder juiste scoped toestemming en MediaGuard noodstop.
**Negatief scenario:** internet uitval, incorrecte spraakherkenning, verlopen sessie en geweigerde toestemming veroorzaken geen ongeautoriseerde apparaat-/serveractie.

## Exit en herstel
Werkende PWA push-to-talk, Nederlandse STT/TTS (of expliciet onbekende hardware), veilige bevestiging bij R2/R3; geen automatische betaalde API, geen permanent microfoonproces. Bij uitval getypte chat behouden.
