// Ontwerpreview van de statuspagina tegen de ECHTE dienst (gestart door echte_dienst.py). Alleen ontwikkeling.
//
//   node tests/ui/echte_dienst.mjs --config <config.json> [--rapport <json>] [--schermafdrukken <map>]
//                                  [--alle-schermafdrukken <map>]
//
// Doorloopt in Chromium elke toestand van GET / zoals een gebruiker hem bereikt (formulier invullen en verzenden,
// Hub-acties via de simulator) en toetst elke toestand op 320/360/390/768/1440 × licht/donker met peilcontrole.mjs
// (axe-core WCAG 2.2 AA + best practices, contrast in de DOM, 44 px, overloop, focusdoorloop, vaste woorden,
// onbekend nooit groen, status nooit alleen kleur) plus: tekstafstand (WCAG 1.4.12) op 320 px, forced colors en de
// beveiligingskoppen van de dienst (CSP default-src 'none', geen JS, geen extern verzoek).
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { chromium } from 'playwright';
import { AXE_TAGS, AXE_VERSION, BREEDTES, OVERLOOP_BINNEN, THEMAS, controleer } from './peilcontrole.mjs';

const arg = (naam) => { const i = process.argv.indexOf(naam); return i > 0 ? process.argv[i + 1] : undefined; };
const cfg = JSON.parse(fs.readFileSync(arg('--config'), 'utf8'));
const RAPPORT = arg('--rapport');
const SHOTS = arg('--schermafdrukken');
const ALLE = arg('--alle-schermafdrukken');
for (const d of [SHOTS, ALLE]) if (d) fs.mkdirSync(d, { recursive: true });
const { a: A, b: B, c: C, d: D, e: E } = cfg.diensten;
const SIM_A = cfg.sims.a;
const SIM_B = cfg.sims.b;

const browser = await chromium.launch();
const BROWSER = `chromium ${browser.version()}`;
const resultaten = [];
const stappen = [];
let fouten = 0;
const wacht = (ms) => new Promise((r) => { setTimeout(r, ms); });
async function tot(fn, ms = 30000) { const eind = Date.now() + ms; while (Date.now() < eind) { if (await fn()) return true; await wacht(300); } throw new Error('time-out'); }
const json = async (url, init) => (await fetch(url, init)).json();
// de poging-sleutel in een URL is kortlevend en willekeurig; in het rapport staat hij niet
const stap = (naam, uitkomst) => {
  const schoon = JSON.parse(JSON.stringify(uitkomst).replace(/poging=[A-Za-z0-9_-]+/g, 'poging=…'));
  stappen.push({ naam, ...schoon });
  console.log(`stap ${naam}: ${JSON.stringify(schoon)}`);
};

// Toestanden met alle tien schermafdrukken in docs; de rest alleen op 390 px.
const VOLLEDIG = new Set(['niet-gekoppeld', 'koppelen-mislukt', 'gekoppeld', 'ingetrokken', 'niet-gereed']);

async function meet(naam, url, { status = 200, bevat = [], bevatNiet = [] } = {}) {
  for (const breedte of BREEDTES) {
    for (const [thema, scheme] of THEMAS) {
      const context = await browser.newContext({ viewport: { width: breedte, height: 900 }, colorScheme: scheme, deviceScaleFactor: 1, hasTouch: breedte < 1024 });
      const page = await context.newPage();
      const console_ = []; const extern = [];
      // de eigen, verwachte HTTP-status (bv. de eerlijke 500 zonder sjabloon) is geen consolefout van de pagina
      const verwacht = `the server responded with a status of ${status} `;
      page.on('console', (m) => { if ((m.type() === 'error' || /Content Security Policy|Refused to/i.test(m.text())) && !m.text().includes(verwacht)) console_.push(m.text()); });
      page.on('request', (r) => { const u = new URL(r.url()); if (`${u.protocol}//${u.host}` !== new URL(url).origin) extern.push(r.url()); });
      const antwoord = await page.goto(url, { waitUntil: 'load' });
      const kop = antwoord.headers();
      const html = await page.content();
      const { problemen, meting } = await controleer(page);
      if (antwoord.status() !== status) problemen.push(`HTTP ${antwoord.status()} (verwacht ${status})`);
      if (!(kop['content-security-policy'] || '').includes("default-src 'none'")) problemen.push(`CSP zonder default-src 'none': ${kop['content-security-policy']}`);
      if (console_.length) problemen.push(`console/CSP: ${console_.join(' | ')}`);
      if (extern.length) problemen.push(`externe verzoeken: ${extern.join(', ')}`);
      for (const t of bevat) if (!html.includes(t)) problemen.push(`verwacht op de pagina: "${t}"`);
      for (const t of bevatNiet) if (html.includes(t)) problemen.push(`mag niet op de pagina: "${t}"`);
      if (breedte === 320 && thema === 'licht') {
        // WCAG 1.4.12 tekstafstand: geen overloop en geen afgesneden tekst bij ruimere afstanden
        await page.addStyleTag({ content: '*{line-height:1.5!important;letter-spacing:.12em!important;word-spacing:.16em!important}p{margin-bottom:2em!important}' });
        const o = await page.evaluate(() => document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth);
        const vak = await page.evaluate(OVERLOOP_BINNEN);
        meting.tekstafstand_overloop = o;
        meting.tekstafstand_buiten_vak = vak;
        if (o > 0) problemen.push(`overloop bij tekstafstand (1.4.12): ${o}px`);
        if (vak.length) problemen.push(`inhoud buiten zijn vak bij tekstafstand (1.4.12): ${vak.join(', ')}`);
        await page.reload({ waitUntil: 'load' });
      }
      const bestand = `${naam}-${breedte}-${thema}.png`;
      if (ALLE) await page.screenshot({ path: path.join(ALLE, bestand), fullPage: true });
      const naarDocs = SHOTS && (VOLLEDIG.has(naam) || breedte === 390);
      if (naarDocs) fs.copyFileSync(path.join(ALLE, bestand), path.join(SHOTS, bestand));
      fouten += problemen.length ? 1 : 0;
      resultaten.push({ toestand: naam, breedte, thema, http: antwoord.status(), problemen, ...meting, schermafdruk: naarDocs ? bestand : null });
      console.log(`${problemen.length ? 'FOUT' : 'ok  '} ${naam.padEnd(24)} ${String(breedte).padStart(4)} ${thema.padEnd(6)} axe ${meting.axe_overtredingen} · tekst ≥ ${meting.tekst_min} · ui ≥ ${meting.ui_min}${problemen.length ? `\n     ${problemen.join('\n     ')}` : ''}`);
      await context.close();
    }
  }
  // forced colors (Windows hoog contrast): stippelrand van Onbekend blijft zichtbaar en gestippeld
  const fc = await browser.newContext({ viewport: { width: 390, height: 900 }, forcedColors: 'active' });
  const p = await fc.newPage();
  await p.goto(url);
  const stippel = await p.evaluate(() => [...document.querySelectorAll('.pil-onbekend')].map((e) => getComputedStyle(e).borderTopStyle));
  if (ALLE) await p.screenshot({ path: path.join(ALLE, `${naam}-390-forced-colors.png`), fullPage: true });
  if (stippel.some((s) => s !== 'dashed')) { fouten += 1; console.log(`FOUT ${naam} forced colors: ${stippel}`); }
  resultaten.push({ toestand: naam, breedte: 390, thema: 'forced-colors', problemen: stippel.some((s) => s !== 'dashed') ? ['stippelrand weg in forced colors'] : [], onbekend_randen: stippel });
  await fc.close();
}

// Formulier invullen en verzenden zoals een gebruiker (390 px, toetsenbord); daarna: waar landt de focus?
async function koppelen(basis, velden) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 900 }, hasTouch: true });
  const p = await ctx.newPage();
  await p.goto(`${basis}/`);
  await p.fill('#hub', velden.hub);
  await p.fill('#koppelcode', velden.code);
  await p.fill('#vingerafdruk', velden.fp);
  await Promise.all([p.waitForURL((u) => u.search.includes('melding=')), p.press('#vingerafdruk', 'Enter')]);
  const url = p.url();
  const leeg = await p.evaluate(() => [...document.querySelectorAll('form input:not([type=hidden])')].every((i) => i.value === ''));
  await p.keyboard.press('Tab');
  const skip = await p.evaluate(() => ({ tekst: document.activeElement.textContent.trim(), href: document.activeElement.getAttribute('href') }));
  await p.keyboard.press('Enter');
  const naSkip = await p.evaluate(() => ({ id: document.activeElement.id, invalid: document.activeElement.getAttribute('aria-invalid'), beschreven: document.activeElement.getAttribute('aria-describedby') }));
  await ctx.close();
  return { url, velden_leeg_na_fout: leeg, skiplink: skip, focus_na_skiplink: naSkip };
}

try {
  // 1. A: nog nooit gekoppeld
  await meet('niet-gekoppeld', `${A}/`, { bevat: ['Niet gekoppeld', 'name="hub_vingerafdruk"'], bevatNiet: ['pil-gezond"><svg aria-hidden="true"><use href="#i-gezond"/></svg>Alles gezond'] });

  // 2. A: koppelcode in de verkeerde vorm → veldfout bij de koppelcode
  const vorm = await koppelen(A, { hub: SIM_A, code: 'KORT', fp: cfg.fingerprint_a });
  stap('veldfout-koppelcode', vorm);
  await meet('veldfout-koppelcode', vorm.url, { bevat: ['aria-invalid="true"', 'koppelcode_ongeldig'] });

  // 3. A: geldige vorm, maar de Hub kent de code niet → Hub weigert
  const weiger = await koppelen(A, { hub: SIM_A, code: 'ABCD-EFGH-JKMN', fp: cfg.fingerprint_a });
  stap('koppelen-mislukt', weiger);
  await meet('koppelen-mislukt', weiger.url, { bevat: ['hub_weigerde', 'class="selecteer"'] });

  // 4. A: koppelcode uit de Hub (simulator) → gekoppeld; daarna testbronnen (één met toestemming, één concept)
  const { code } = await json(`${SIM_A}/sim/pairing-code`, { method: 'POST' });
  const goed = await koppelen(A, { hub: SIM_A, code, fp: cfg.fingerprint_a });
  stap('koppelen-gelukt', goed);
  await meet('gekoppeld-melding', goed.url, { bevat: ['Gekoppeld met de Hub', 'pil-neutraal'] });
  const token = async (roles, sub) => (await json(`${SIM_A}/sim/token`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ roles, sub }) })).token;
  // elke mutatie met een nieuwe Hub-JWT (nieuwe jti), zoals de Hub dat doet (contract 1.2 §4.6)
  const bron = async (naam, pad, eigenaar) => json(`${A}/search/v1/sources`, { method: 'POST', headers: { authorization: `Bearer ${await token(['admin'], 'beheer')}`, 'idempotency-key': randomUUID(), 'content-type': 'application/json' }, body: JSON.stringify({ kind: 'files', name: naam, allowed_paths: [pad], owner_sub: eigenaar }) });
  const docs = await bron('Documenten', `${cfg.bronnen}/documenten`, 'gebruiker-a');
  await bron("Foto's 2026", `${cfg.bronnen}/fotos-2026`, 'gebruiker-b');
  const toestemming = await fetch(`${A}/search/v1/sources/${docs.id}/consent`, { method: 'POST', headers: { authorization: `Bearer ${await token(['viewer'], 'gebruiker-a')}`, 'idempotency-key': randomUUID(), 'content-type': 'application/json' }, body: JSON.stringify({ scopes: ['metadata.read', 'content.read'], consent_text_version: cfg.toestemming_versie, bevestiging: true }) });
  stap('testbronnen', { bron: docs.id ? 'aangemaakt' : docs, toestemming: toestemming.status });
  // wacht tot de Hub de koppeling bevestigd heeft (anders toont de pagina terecht "Beperkt: nog niet bevestigd")
  await tot(async () => !(await (await fetch(`${A}/`)).text()).includes('nog niet bevestigd'));
  await meet('gekoppeld', `${A}/`, { bevat: ['pil-neutraal', 'Hub openen'], bevatNiet: ['name="koppelcode"'] });

  // 5. B: koppelt zelf met het token uit de omgeving; daarna ontkoppelt de Hub (dienst-JWT)
  await tot(async () => (await json(`${B}/readyz`)).hub_link?.state === 'gekoppeld');
  const dienstJwt = (await json(`${SIM_B}/sim/service-jwt`, { method: 'POST' })).token;
  const los = await fetch(`${B}/connector/v1/hub-link/unlink`, { method: 'POST', headers: { authorization: `Bearer ${dienstJwt}`, 'idempotency-key': randomUUID() } });
  stap('ontkoppelen-door-hub', { http: los.status });
  await meet('ontkoppeld', `${B}/`, { bevat: ['Ontkoppeld'], bevatNiet: ['name="koppelcode"'] });

  // 6. A: de Hub trekt de koppeling in; de worker merkt dat bij de volgende controle
  await fetch(`${SIM_A}/sim/revoke`, { method: 'POST' });
  await tot(async () => (await json(`${A}/readyz`)).hub_link?.state === 'ingetrokken');
  stap('intrekken-door-hub', { state: 'ingetrokken' });
  await meet('ingetrokken', `${A}/`, { bevat: ['Ingetrokken'], bevatNiet: ['name="koppelcode"'] });

  // 7. C: eigen database onbereikbaar → niet gereed; verlopen formulier; te veel pogingen
  await meet('niet-gereed', `${C}/`, { bevat: ['Niet gereed'] });
  const zonderCookie = await fetch(`${C}/koppelen`, { method: 'POST', redirect: 'manual', headers: { 'content-type': 'application/x-www-form-urlencoded' }, body: 'csrf=x&hub_adres=x&koppelcode=x&hub_vingerafdruk=x' });
  stap('formulier-verlopen', { http: zonderCookie.status, location: zonderCookie.headers.get('location')?.replace(/poging=[^&]+/, 'poging=…') });
  await meet('formulier-verlopen', `${C}${zonderCookie.headers.get('location')}`, { bevat: ['ongeldig_formulier'] });
  const eerste = await fetch(`${C}/`);
  const csrf = /sai_csrf=([^;]+)/.exec(eerste.headers.get('set-cookie'))[1];
  let laatste = null;
  for (let i = 0; i < 6; i += 1) {
    const r = await fetch(`${C}/koppelen`, { method: 'POST', redirect: 'manual', headers: { cookie: `sai_csrf=${csrf}`, 'content-type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ csrf, hub_adres: SIM_A, koppelcode: 'KORT', hub_vingerafdruk: cfg.fingerprint_a }).toString() });
    laatste = r.headers.get('location');
  }
  stap('te-veel-pogingen', { location: laatste.replace(/poging=[^&]+/, 'poging=…') });
  await meet('te-veel-pogingen', `${C}${laatste}`, { bevat: ['te_veel_pogingen'] });

  // 8. D: onleesbaar koppelbestand; E: onbruikbaar sjabloon (eerlijke 500)
  await meet('koppelstatus-onleesbaar', `${D}/`, { bevat: ['koppelstatus_onleesbaar'], bevatNiet: ['name="koppelcode"'] });
  await meet('sjabloon-ongeldig', `${E}/`, { status: 500, bevat: ['peil_sjabloon_ongeldig'] });
} catch (exc) {
  fouten += 1;
  console.error(`FOUT in de doorloop: ${exc.stack || exc}`);
  stappen.push({ naam: 'doorloop', fout: String(exc) });
}
await browser.close();

const rapport = {
  soort: 'echte dienst (lokaal, testdata, Hub-simulator)',
  axe_core: AXE_VERSION, axe_tags: AXE_TAGS, browser: BROWSER, breedtes: BREEDTES, themas: THEMAS.map(([t]) => t),
  toestanden: [...new Set(resultaten.map((r) => r.toestand))], stappen,
  controles: resultaten.length, mislukt: fouten, resultaten,
};
if (RAPPORT) fs.writeFileSync(RAPPORT, `${JSON.stringify(rapport, null, 1)}\n`);
console.log(`\n${resultaten.length} controles in ${rapport.toestanden.length} toestanden, ${fouten} mislukt (axe-core ${AXE_VERSION}, ${BROWSER})`);
process.exit(fouten ? 1 : 0);
