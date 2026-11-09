# ADR-0002 — Authenticatie, JWKS en koppelen met de Hub

Status: aanvaard, **deels vervangen door ADR-0011** (platformcontract 1.2) · Datum: 2026-11-09 · Fase: P0 · Contract:
1.1 §3, §4

> **Vervangen in 1.2 (ADR-0011):** punt 1 (antwoordcontrole, opslag in `/appdata/secrets/hub-link.json` i.p.v. de
> vault), punt 2 (formulier alleen zolang nooit gekoppeld, met verplichte vingerafdruk, 5/15 min per bron-IP; geen
> lokale ontkoppelcode meer), punt 4 (geen JWKS-cache: gepinde sleutels), punt 5 (inkomend service-token geweigerd; de
> Hub meldt zich met een dienst-JWT `token_use: hub_service`). Blijft geldig: punt 3 (aangevuld met `nbf`, `jti`,
> `token_use`), 6 en 7 (aangevuld met de regels van §3.2). De open punten hieronder zijn in 1.2 opgelost.

## Besluit
1. **Enroll-formaat = de echte Hub** (`ai-server-manager/apps/api/src/hub/routers/services.py`, `extra=forbid`):
   verzoek `{"app_id", "enrollment_token" | "pairing_code", "version", "base_url"?, "public_key_jwk"}` (alleen
   `kty, crv, x, kid`); antwoord `{"service_id", "service_token", "jwks", "policy_public_key", …}`.
   400/401/403/409/410/422 = definitief geweigerd (token wordt niet opnieuw geprobeerd), 404 = endpoint ontbreekt,
   429/5xx/netwerk = later opnieuw (backoff tot 15 min). Zonder Hub: eerlijk *niet gekoppeld*.
2. **Koppelcode op de statuspagina**: alleen als niet gekoppeld; CSRF (double-submit, SameSite=Strict) + Origin;
   throttle 10/10 min; vaste meldingen. Ontkoppelen op de pagina vereist de **lokale ontkoppelcode** uit de app-log.
3. **Hub-JWT**: `alg=EdDSA`, `iss=hub`, `aud=zoeken-en-persoonlijke-ai`, verplicht `exp, iat, iss, aud, sub`;
   `exp-iat ≤ 120 s`, 30 s klokspeling, onbekende rollen genegeerd.
4. **JWKS fail-closed**: vers 300 s; bij mislukt verversen hooguit 900 s oude set; daarna elke JWT 401
   `jwks_niet_beschikbaar` (EX16). Onbekende `kid` → hooguit elke 30 s verversen.
5. **Service-token** (gedeeld geheim uit de enrollment, versleuteld in de vault, alleen SHA-256 in de status) geeft
   principal `service:hub` **zonder gebruikerscontext**: geen zoeken, geen bronbeheer; alleen connector-lezen.
6. **Rol `kind` wint altijd**: wie `kind` heeft, krijgt kinderprofiel-beperkingen, ook met `admin` erbij.
7. Hub-adres: alleen http(s), geen inloggegevens/query, geen loopback (behalve met `SEARCH_AI_ALLOW_LOOPBACK_HUB`
   voor ontwikkeling). Uitgaande calls negeren proxy-omgevingsvariabelen (`trust_env=False`).

## Open punten (contract_issues)
- De Hub bewaart alleen de hash van het service-token; hoe de Hub zich met dat token tegenover een dienst
  authenticeert is niet vastgelegd. Gekozen: inkomend service-token = het eigen token (zoals de zusterdiensten).
- JWKS over HTTP op het LAN kan door een LAN-aanvaller worden vervalst; voorstel: HTTPS of ondertekende JWKS-rotatie.
