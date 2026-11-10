// Ontwerpreview van de statuspagina tegen de ECHTE dienst (gestart door echte_dienst.py). Alleen ontwikkeling.
//
//   node tests/ui/echte_dienst.mjs --config <config.json> [--rapport <json>] [--schermafdrukken <map>]
//                                  [--alle-schermafdrukken <map>]
//
// Doorloopt in Chromium elke toestand van GET / en elk antwoord op POST /koppelen zoals een gebruiker hem bereikt
// (formulier invullen en verzenden, Hub-acties via de simulator) en toetst elke toestand op
// 320/360/390/768/1024/1440 × licht/donker met peilcontrole.mjs (axe-core WCAG 2.2 AA + best practices, contrast in de
// DOM, 44 px, overloop, focusdoorloop, vaste woorden, onbekend nooit groen, status nooit alleen kleur) plus: tekstafstand
// (WCAG 1.4.12) en het lettertype van de CI-runner op 320 px, forced colors en de beveiligingskoppen van de dienst.
//
// Contract 1.3 §3.7: de POST krijgt direct de pagina met de melding en de echte HTTP-status. Zo'n antwoord wordt één keer
// echt opgehaald (elke POST telt als poging) en daarna per breedte en thema met exact die bytes en koppen opnieuw
// getoond (page.route → fulfill). De negatieve herkomsttests lopen in de echte browser: een formulier van een andere app
// op hetzelfde NAS-adres (same-site), van een andere site (cross-site), uit een sandbox-iframe (Origin: null), via een
// domeinnaam die niet mag (Host, DNS-rebinding) en zonder cookie.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { chromium } from 'playwright';
import { AXE_TAGS, AXE_VERSION, BREEDTES, LETTER, THEMAS, controleer, reflow320 } from './peilcontrole.mjs';

const arg = (naam) => { const i = process.argv.indexOf(naam); return i > 0 ? process.argv[i + 1] : undefined; };
const cfg = JSON.parse(fs.readFileSync(arg('--config'), 'utf8'));
const RAPPORT = arg('--rapport');
const SHOTS = arg('--schermafdrukken');
const ALLE = arg('--alle-schermafdrukken');
for (const d of [SHOTS, ALLE]) if (d) fs.mkdirSync(d, { recursive: true });
const { a: A, b: B, c: C, d: D, e: E, f: F } = cfg.diensten;
const SIM_A = cfg.sims.a;
const SIM_B = cfg.sims.b;
const DOMEIN = 'zoeken.voorbeeld.nl'; // publieke-achtige naam: koppelen mag niet (DNS-rebinding, §3.7 punt 4)
const AANVALLER = 'aanvaller.voorbeeld';

// Een "andere app" op hetzelfde adres met een eigen poort: biedt formulieren die naar de dienst posten (zonder JS; de
// test klikt op de knop). Zelfde host, andere poort = same-site; via AANVALLER = cross-site; srcdoc in een sandbox =
// opaque origin (Origin: null).
const formulier = (doel) => `<form method="post" action="${doel}/koppelen"><input name="csrf" value="geraden-token-1234567890abcdef"><input name="hub_adres" value="${SIM_A}"><input name="koppelcode" value="ABCD-EFGH-JKMN"><input name="hub_vingerafdruk" value="${cfg.fingerprint_a}"><button id="verstuur">Verstuur</button></form>`;
const aanvaller = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  const doel = u.searchParams.get('doel') || A;
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
  if (u.pathname === '/sandbox') {
    const srcdoc = formulier(doel).replaceAll('"', '&quot;');
    res.end(`<!doctype html><title>x</title><iframe id="kader" sandbox="allow-forms" srcdoc="${srcdoc}"></iframe>`);
  } else {
    res.end(`<!doctype html><title>x</title>${formulier(doel)}`);
  }
});
await new Promise((r) => { aanvaller.listen(0, '127.0.0.1', r); });
const AANVALLER_POORT = aanvaller.address().port;

const browser = await chromium.launch({ args: [`--host-resolver-rules=MAP ${DOMEIN} 127.0.0.1, MAP ${AANVALLER} 127.0.0.1`] });
const BROWSER = `chromium ${browser.version()}`;
const resultaten = [];
const stappen = [];
let fouten = 0;
const wacht = (ms) => new Promise((r) => { setTimeout(r, ms); });
async function tot(fn, ms = 30000) { const eind = Date.now() + ms; while (Date.now() < eind) { if (await fn()) return true; await wacht(300); } throw new Error('time-out'); }
const json = async (url, init) => (await fetch(url, init)).json();
const enrolls = async (sim) => (await json(`${sim}/sim/enroll-requests`)).length;
const stap = (naam, uitkomst) => { stappen.push({ naam, ...uitkomst }); console.log(`stap ${naam}: ${JSON.stringify(uitkomst)}`); };
const verwachtStap = (naam, uitkomst, ok) => {
  stap(naam, { ...uitkomst, verwacht: ok });
  if (!ok) { fouten += 1; console.log(`FOUT stap ${naam}: niet zoals verwacht`); }
};
const code = (html) => (/<p class="meta">Code <code>([a-z_]+)<\/code><\/p>/.exec(html) || [])[1] || null;

// Toestanden met alle schermafdrukken in docs; de rest alleen op 390 px.
const VOLLEDIG = new Set(['niet-gekoppeld', 'koppelen-mislukt', 'gekoppeld', 'ingetrokken', 'niet-gereed']);

async function meetPagina(naam, page, antwoord, { status = 200, bevat = [], bevatNiet = [] } = {}, breedte, thema, console_, extern) {
  const kop = antwoord.headers();
  const html = await page.content();
  const { problemen, meting } = await controleer(page);
  if (antwoord.status() !== status) problemen.push(`HTTP ${antwoord.status()} (verwacht ${status})`);
  if (!(kop['content-security-policy'] || '').includes("default-src 'none'")) problemen.push(`CSP zonder default-src 'none': ${kop['content-security-policy']}`);
  if (kop['referrer-policy'] !== 'same-origin') problemen.push(`Referrer-Policy ${kop['referrer-policy']} (verwacht same-origin, §3.7)`);
  if (console_.length) problemen.push(`console/CSP: ${console_.join(' | ')}`);
  if (extern.length) problemen.push(`externe verzoeken: ${extern.join(', ')}`);
  for (const t of bevat) if (!html.includes(t)) problemen.push(`verwacht op de pagina: "${t}"`);
  for (const t of bevatNiet) if (html.includes(t)) problemen.push(`mag niet op de pagina: "${t}"`);
  if (breedte === 320 && thema === 'licht') {
    const r = await reflow320(page);
    problemen.push(...r.problemen);
    meting.reflow_320 = r.meting;
  }
  const bestand = `${naam}-${breedte}-${thema}.png`;
  if (ALLE) await page.screenshot({ path: path.join(ALLE, bestand), fullPage: true });
  const naarDocs = SHOTS && (VOLLEDIG.has(naam) || breedte === 390);
  if (naarDocs) fs.copyFileSync(path.join(ALLE, bestand), path.join(SHOTS, bestand));
  fouten += problemen.length ? 1 : 0;
  resultaten.push({ toestand: naam, breedte, thema, http: antwoord.status(), problemen, ...meting, schermafdruk: naarDocs ? bestand : null });
  console.log(`${problemen.length ? 'FOUT' : 'ok  '} ${naam.padEnd(24)} ${String(breedte).padStart(4)} ${thema.padEnd(6)} axe ${meting.axe_overtredingen} · tekst ≥ ${meting.tekst_min} · ui ≥ ${meting.ui_min}${problemen.length ? `\n     ${problemen.join('\n     ')}` : ''}`);
}

// Meet een toestand op elke breedte en elk thema. ``vast`` = een eerder echt opgehaald POST-antwoord dat met exact
// dezelfde status, koppen en bytes wordt getoond (geen nieuwe poging).
async function meet(naam, url, opties = {}, vast = null) {
  const status = vast ? vast.status : (opties.status || 200);
  for (const breedte of BREEDTES) {
    for (const [thema, scheme] of THEMAS) {
      const context = await browser.newContext({ viewport: { width: breedte, height: 900 }, colorScheme: scheme, deviceScaleFactor: 1, hasTouch: breedte < 1024 });
      const page = await context.newPage();
      if (vast) await page.route(url, (route) => route.fulfill({ status: vast.status, headers: vast.headers, body: vast.html }));
      const console_ = []; const extern = [];
      // de eigen, verwachte HTTP-status (bv. 409 of de eerlijke 500) is geen consolefout van de pagina
      const verwacht = `the server responded with a status of ${status} `;
      page.on('console', (m) => { if ((m.type() === 'error' || /Content Security Policy|Refused to/i.test(m.text())) && !m.text().includes(verwacht)) console_.push(m.text()); });
      page.on('request', (r) => { const u = new URL(r.url()); if (`${u.protocol}//${u.host}` !== new URL(url).origin) extern.push(r.url()); });
      const antwoord = await page.goto(url, { waitUntil: 'load' });
      await meetPagina(naam, page, antwoord, { ...opties, status }, breedte, thema, console_, extern);
      await context.close();
    }
  }
  // forced colors (Windows hoog contrast): stippelrand van Onbekend blijft zichtbaar en gestippeld
  const fc = await browser.newContext({ viewport: { width: 390, height: 900 }, forcedColors: 'active' });
  const p = await fc.newPage();
  if (vast) await p.route(url, (route) => route.fulfill({ status: vast.status, headers: vast.headers, body: vast.html }));
  await p.goto(url);
  const stippel = await p.evaluate(() => [...document.querySelectorAll('.pil-onbekend, .pil-niet-gekoppeld')].map((e) => getComputedStyle(e).borderTopStyle));
  if (ALLE) await p.screenshot({ path: path.join(ALLE, `${naam}-390-forced-colors.png`), fullPage: true });
  if (stippel.some((s) => s !== 'dashed')) { fouten += 1; console.log(`FOUT ${naam} forced colors: ${stippel}`); }
  resultaten.push({ toestand: naam, breedte: 390, thema: 'forced-colors', problemen: stippel.some((s) => s !== 'dashed') ? ['stippelrand weg in forced colors'] : [], onbekend_randen: stippel });
  await fc.close();
}

// Formulier invullen en verzenden zoals een gebruiker (390 px, toetsenbord). Geeft het echte POST-antwoord terug en waar
// de focus via de skiplink landt.
async function koppelen(basis, velden, { zonderCookie = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 900 }, hasTouch: true });
  const p = await ctx.newPage();
  await p.goto(`${basis}/`);
  await p.fill('#hub', velden.hub);
  await p.fill('#koppelcode', velden.code);
  await p.fill('#vingerafdruk', velden.fp);
  if (zonderCookie) await ctx.clearCookies();
  const [antwoord] = await Promise.all([
    p.waitForResponse((r) => r.url().endsWith('/koppelen') && r.request().method() === 'POST'),
    p.press('#vingerafdruk', 'Enter'),
  ]);
  const kop = await antwoord.request().allHeaders(); // zoals de browser hem echt verstuurde (ook Sec-Fetch-*)
  const verzoek = { origin: kop.origin ?? '(geen)', sfs: kop['sec-fetch-site'], cookie: Boolean(kop.cookie) };
  await p.waitForLoadState('load');
  const vast = { status: antwoord.status(), headers: await antwoord.allHeaders(), html: await antwoord.text() };
  delete vast.headers['set-cookie'];
  const leeg = await p.evaluate(() => [...document.querySelectorAll('form input:not([type=hidden])')].every((i) => i.value === ''));
  await p.keyboard.press('Tab');
  const skip = await p.evaluate(() => ({ tekst: document.activeElement.textContent.trim(), href: document.activeElement.getAttribute('href') }));
  await p.keyboard.press('Enter');
  const naSkip = await p.evaluate(() => ({ id: document.activeElement.id, invalid: document.activeElement.getAttribute('aria-invalid') }));
  await ctx.close();
  return { vast, uitkomst: { http: vast.status, code: code(vast.html), herkomst: verzoek, velden_leeg_na_antwoord: leeg, skiplink: skip, focus_na_skiplink: naSkip } };
}

// Een formulier van een andere herkomst (andere app op hetzelfde adres, andere site of sandbox) naar de dienst sturen.
async function vreemdFormulier(paginaUrl, { inKader = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 900 } });
  const p = await ctx.newPage();
  await p.goto(`${basisVan(A)}/`); // een echte cookie van de dienst: SameSite=Strict mag hem toch niet meesturen
  await p.goto(paginaUrl);
  const doel = inKader ? p.frameLocator('#kader').locator('#verstuur') : p.locator('#verstuur');
  const [antwoord] = await Promise.all([p.waitForResponse((r) => r.url().endsWith('/koppelen') && r.request().method() === 'POST'), doel.click()]);
  const kop = await antwoord.request().allHeaders();
  const verzoek = { origin: kop.origin ?? '(geen)', sfs: kop['sec-fetch-site'], cookie: Boolean(kop.cookie) };
  // in een kader kan de browser de body niet tonen (frame-ancestors 'none'); de status en de herkomst zeggen genoeg
  const html = await antwoord.text().catch(() => '');
  await ctx.close();
  return { http: antwoord.status(), code: code(html), herkomst: verzoek };
}
const basisVan = (u) => u;

try {
  // 1. A: nog nooit gekoppeld
  await meet('niet-gekoppeld', `${A}/`, { bevat: ['Niet gekoppeld', 'name="hub_vingerafdruk"', 'novalidate'], bevatNiet: ['pil-gezond"><svg aria-hidden="true"><use href="#i-gezond"/></svg>Alles gezond', 'role="'] });

  // 2. A: koppelcode in de verkeerde vorm → 422 met veldfout bij de koppelcode
  const vorm = await koppelen(A, { hub: SIM_A, code: 'KORT', fp: cfg.fingerprint_a });
  verwachtStap('veldfout-koppelcode', vorm.uitkomst, vorm.vast.status === 422 && vorm.uitkomst.code === 'koppelcode_ongeldig' && vorm.uitkomst.herkomst.sfs === 'same-origin');
  await meet('veldfout-koppelcode', `${A}/__antwoord/veldfout-koppelcode`, { bevat: ['aria-invalid="true"', 'role="alert"'] }, vorm.vast);

  // 3. A: geldige vorm, maar de Hub kent de code niet → 422 hub_weigerde
  const weiger = await koppelen(A, { hub: SIM_A, code: 'ABCD-EFGH-JKMN', fp: cfg.fingerprint_a });
  verwachtStap('koppelen-mislukt', weiger.uitkomst, weiger.vast.status === 422 && weiger.uitkomst.code === 'hub_weigerde');
  await meet('koppelen-mislukt', `${A}/__antwoord/koppelen-mislukt`, { bevat: ['hub_weigerde', 'class="selecteer"'] }, weiger.vast);

  // 4. A: negatieve herkomsttests in de echte browser; niets mag naar de Hub
  const voor = await enrolls(SIM_A);
  const poortA = new URL(A).port;
  const sameSite = await vreemdFormulier(`http://127.0.0.1:${AANVALLER_POORT}/?doel=${encodeURIComponent(A)}`);
  verwachtStap('herkomst-same-site', sameSite, sameSite.http === 403 && sameSite.code === 'herkomst_geweigerd' && sameSite.herkomst.sfs === 'same-site');
  const crossSite = await vreemdFormulier(`http://${AANVALLER}:${AANVALLER_POORT}/?doel=${encodeURIComponent(A)}`);
  verwachtStap('herkomst-cross-site', crossSite, crossSite.http === 403 && crossSite.code === 'herkomst_geweigerd' && crossSite.herkomst.sfs === 'cross-site' && !crossSite.herkomst.cookie);
  const sandbox = await vreemdFormulier(`http://${AANVALLER}:${AANVALLER_POORT}/sandbox?doel=${encodeURIComponent(A)}`, { inKader: true });
  verwachtStap('herkomst-origin-null', sandbox, sandbox.http === 403 && [null, 'herkomst_geweigerd'].includes(sandbox.code) && sandbox.herkomst.origin === 'null' && !sandbox.herkomst.cookie);
  await meet('adres-zonder-koppelen', `http://${DOMEIN}:${poortA}/`, { bevat: ['Koppelen kan niet via dit adres', 'action="/koppelen"'], bevatNiet: ['role="'] });
  const rebind = await koppelen(`http://${DOMEIN}:${poortA}`, { hub: SIM_A, code: 'ABCD-EFGH-JKMN', fp: cfg.fingerprint_a });
  verwachtStap('host-niet-toegestaan', rebind.uitkomst, rebind.vast.status === 403 && rebind.uitkomst.code === 'host_niet_toegestaan' && rebind.uitkomst.herkomst.origin === `http://${DOMEIN}:${poortA}`);
  await meet('host-geweigerd', `${A}/__antwoord/host-geweigerd`, { bevat: ['host_niet_toegestaan', 'role="alert"'] }, rebind.vast);
  const zonder = await koppelen(A, { hub: SIM_A, code: 'ABCD-EFGH-JKMN', fp: cfg.fingerprint_a }, { zonderCookie: true });
  verwachtStap('geen-cookie', zonder.uitkomst, zonder.vast.status === 403 && zonder.uitkomst.code === 'ongeldig_formulier');
  const na = await enrolls(SIM_A);
  verwachtStap('geweigerd-niets-naar-de-hub', { enroll_voor: voor, enroll_na: na }, voor === na);

  // 5. A: koppelcode uit de Hub (simulator) → gekoppeld (200, rol status); daarna testbronnen
  const { code: koppelcode } = await json(`${SIM_A}/sim/pairing-code`, { method: 'POST' });
  const goed = await koppelen(A, { hub: SIM_A, code: koppelcode, fp: cfg.fingerprint_a });
  verwachtStap('koppelen-gelukt', goed.uitkomst, goed.vast.status === 200 && goed.vast.html.includes('role="status"'));
  await meet('gekoppeld-melding', `${A}/__antwoord/gekoppeld-melding`, { bevat: ['Gekoppeld met de Hub', 'pil-neutraal'] }, goed.vast);
  const token = async (roles, sub) => (await json(`${SIM_A}/sim/token`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ roles, sub }) })).token;
  // elke mutatie met een nieuwe Hub-JWT (nieuwe jti), zoals de Hub dat doet (contract 1.3 §4.6)
  const bron = async (naam, pad, eigenaar) => json(`${A}/search/v1/sources`, { method: 'POST', headers: { authorization: `Bearer ${await token(['admin'], 'beheer')}`, 'idempotency-key': randomUUID(), 'content-type': 'application/json' }, body: JSON.stringify({ kind: 'files', name: naam, allowed_paths: [pad], owner_sub: eigenaar }) });
  const docs = await bron('Documenten', `${cfg.bronnen}/documenten`, 'gebruiker-a');
  await bron("Foto's 2026", `${cfg.bronnen}/fotos-2026`, 'gebruiker-b');
  const toestemming = await fetch(`${A}/search/v1/sources/${docs.id}/consent`, { method: 'POST', headers: { authorization: `Bearer ${await token(['viewer'], 'gebruiker-a')}`, 'idempotency-key': randomUUID(), 'content-type': 'application/json' }, body: JSON.stringify({ scopes: ['metadata.read', 'content.read'], consent_text_version: cfg.toestemming_versie, bevestiging: true }) });
  stap('testbronnen', { bron: docs.id ? 'aangemaakt' : docs, toestemming: toestemming.status });
  // wacht tot de Hub de koppeling bevestigd heeft en de worker een hartslag heeft gegeven
  await tot(async () => (await (await fetch(`${A}/`)).text()).includes('Alles gezond'));
  await meet('gekoppeld', `${A}/`, { bevat: ['pil-neutraal', 'Hub openen', 'Alles gezond'], bevatNiet: ['name="koppelcode"'] });
  // een tweede POST op een al gekoppelde dienst: 409 al_gekoppeld, niets naar de Hub (RV7)
  const ctxA = await browser.newContext();
  const pA = await ctxA.newPage();
  await pA.goto(`${A}/`);
  const csrfA = (await ctxA.cookies()).find((c) => c.name === 'sai_csrf').value;
  const al = await pA.request.post(`${A}/koppelen`, { headers: { origin: A }, form: { csrf: csrfA, hub_adres: SIM_A, koppelcode, hub_vingerafdruk: cfg.fingerprint_a } });
  verwachtStap('al-gekoppeld', { http: al.status(), code: code(await al.text()) }, al.status() === 409 && code(await al.text()) === 'al_gekoppeld');
  await ctxA.close();

  // 6. B: koppelt zelf met het token uit de omgeving; daarna ontkoppelt de Hub (dienst-JWT)
  await tot(async () => (await json(`${B}/readyz`)).hub_link?.state === 'gekoppeld');
  const dienstJwt = (await json(`${SIM_B}/sim/service-jwt`, { method: 'POST' })).token;
  const los = await fetch(`${B}/connector/v1/hub-link/unlink`, { method: 'POST', headers: { authorization: `Bearer ${dienstJwt}`, 'idempotency-key': randomUUID() } });
  stap('ontkoppelen-door-hub', { http: los.status });
  await meet('ontkoppeld', `${B}/`, { bevat: ['Ontkoppeld'], bevatNiet: ['name="koppelcode"'] });

  // 7. A: de Hub trekt de koppeling in; de worker merkt dat bij de volgende controle
  await fetch(`${SIM_A}/sim/revoke`, { method: 'POST' });
  await tot(async () => (await json(`${A}/readyz`)).hub_link?.state === 'ingetrokken');
  stap('intrekken-door-hub', { state: 'ingetrokken' });
  await meet('ingetrokken', `${A}/`, { bevat: ['Ingetrokken'], bevatNiet: ['name="koppelcode"'] });

  // 8. C: eigen database onbereikbaar → niet gereed; verlopen formulier; te veel pogingen
  await meet('niet-gereed', `${C}/`, { bevat: ['Niet gereed'] });
  const verlopen = await koppelen(C, { hub: SIM_A, code: 'ABCD-EFGH-JKMN', fp: cfg.fingerprint_a }, { zonderCookie: true });
  verwachtStap('formulier-verlopen', verlopen.uitkomst, verlopen.vast.status === 403 && verlopen.uitkomst.code === 'ongeldig_formulier');
  await meet('formulier-verlopen', `${C}/__antwoord/formulier-verlopen`, { bevat: ['ongeldig_formulier'] }, verlopen.vast);
  const ctxC = await browser.newContext();
  const pC = await ctxC.newPage();
  await pC.goto(`${C}/`);
  const csrfC = (await ctxC.cookies()).find((c) => c.name === 'sai_csrf').value;
  let laatste = null;
  for (let i = 0; i < 6; i += 1) {
    laatste = await pC.request.post(`${C}/koppelen`, { headers: { origin: C }, form: { csrf: csrfC, hub_adres: SIM_A, koppelcode: 'KORT', hub_vingerafdruk: cfg.fingerprint_a } });
  }
  const teVeel = { status: laatste.status(), headers: laatste.headers(), html: await laatste.text() };
  delete teVeel.headers['set-cookie'];
  await ctxC.close();
  verwachtStap('te-veel-pogingen', { http: teVeel.status, code: code(teVeel.html), retry_after: teVeel.headers['retry-after'] }, teVeel.status === 429 && code(teVeel.html) === 'te_veel_pogingen');
  await meet('te-veel-pogingen', `${C}/__antwoord/te-veel-pogingen`, { bevat: ['te_veel_pogingen'] }, teVeel);

  // 9. F: koppelen bezig (env-token, Hub onbereikbaar): geen formulier, POST → 409 koppelen_bezig
  await tot(async () => (await json(`${F}/readyz`)).hub_link?.koppelen_bezig === true);
  await meet('koppelen-bezig', `${F}/`, { bevat: ['Koppelen bezig'], bevatNiet: ['name="koppelcode"', 'role="'] });
  const ctxF = await browser.newContext();
  const pF = await ctxF.newPage();
  await pF.goto(`${F}/`);
  const csrfF = (await ctxF.cookies()).find((c) => c.name === 'sai_csrf').value;
  const bezig = await pF.request.post(`${F}/koppelen`, { headers: { origin: F }, form: { csrf: csrfF, hub_adres: SIM_A, koppelcode: 'ABCD-EFGH-JKMN', hub_vingerafdruk: cfg.fingerprint_a } });
  const bezigVast = { status: bezig.status(), headers: bezig.headers(), html: await bezig.text() };
  delete bezigVast.headers['set-cookie'];
  await ctxF.close();
  verwachtStap('koppelen-bezig-409', { http: bezigVast.status, code: code(bezigVast.html) }, bezigVast.status === 409 && code(bezigVast.html) === 'koppelen_bezig');
  await meet('koppelen-bezig-poging', `${F}/__antwoord/koppelen-bezig`, { bevat: ['koppelen_bezig', 'role="alert"'] }, bezigVast);

  // 10. D: onleesbaar koppelbestand; E: onbruikbaar sjabloon (eerlijke 500)
  await meet('koppelstatus-onleesbaar', `${D}/`, { bevat: ['koppelstatus_onleesbaar', '/appdata/secrets/hub-link.json'], bevatNiet: ['name="koppelcode"', 'role="'] });
  await meet('sjabloon-ongeldig', `${E}/`, { status: 500, bevat: ['peil_sjabloon_ongeldig'] });
} catch (exc) {
  fouten += 1;
  console.error(`FOUT in de doorloop: ${exc.stack || exc}`);
  stappen.push({ naam: 'doorloop', fout: String(exc) });
}
await browser.close();
aanvaller.close();

const rapport = {
  soort: 'echte dienst (lokaal, testdata, Hub-simulator)',
  axe_core: AXE_VERSION, axe_tags: AXE_TAGS, browser: BROWSER, breedtes: BREEDTES, lettertype_reflow: LETTER, themas: THEMAS.map(([t]) => t),
  toestanden: [...new Set(resultaten.map((r) => r.toestand))], stappen,
  controles: resultaten.length, mislukt: fouten, resultaten,
};
if (RAPPORT) fs.writeFileSync(RAPPORT, `${JSON.stringify(rapport, null, 1)}\n`);
console.log(`\n${resultaten.length} controles in ${rapport.toestanden.length} toestanden, ${fouten} mislukt (axe-core ${AXE_VERSION}, ${BROWSER})`);
process.exit(fouten ? 1 : 0);
