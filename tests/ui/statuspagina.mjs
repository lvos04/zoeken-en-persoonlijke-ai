// Browsercontrole van de Peil-statuspagina (ONTWERPTAAL §12/§13.2) op toestanden uit de echte renderer. Alleen
// ontwikkeling/CI; draait zonder database. De ontwerpreview tegen de echte dienst is echte_dienst.py/.mjs.
//
//   python tests/ui/render_toestanden.py --out <map>
//   node tests/ui/statuspagina.mjs --in <map> [--rapport <bestand.json>] [--schermafdrukken <map>]
//
// Per toestand × breedte (320/360/390/768/1024/1440) × thema (licht/donker), geserveerd met de echte beveiligingskoppen
// van de dienst (CSP default-src 'none'), de controles van peilcontrole.mjs:
//   - axe-core: 0 overtredingen op WCAG 2.0/2.1/2.2 A + AA (en best practices);
//   - contrast gemeten in de DOM: tekst ≥ 4,5:1, iconen/randen/veldranden ≥ 3:1; tekst ≥ 14 px;
//   - geen horizontale overloop (ook niet op 320 px), geen inhoud buiten zijn vak, geen afkappen, ook met
//     tekstafstand (WCAG 1.4.12) op 320 px; doelen ≥ 44 × 44 px (behalve inline links);
//   - focusdoorloop met Tab: skiplink eerst, tweekleurige ring ≥ 3:1, nooit bedekt;
//   - status nooit alleen kleur, onbekend/niet gereed nooit groen, alleen woorden uit de Peil-woordenlijst;
//   - geen JavaScript (geen <script>, geen on…-attributen), geen CSP-schending, geen extern verzoek.
// Schermafdrukken (volledige pagina) voor 360/768/1440 × licht/donker van de toestanden met "schermafdruk": true.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { chromium } from 'playwright';
import { AXE_TAGS, AXE_VERSION, BREEDTES, LETTER, THEMAS, controleer, reflow320 } from './peilcontrole.mjs';

const SHOT_BREEDTES = new Set([360, 768, 1440]);
const arg = (naam) => { const i = process.argv.indexOf(naam); return i > 0 ? process.argv[i + 1] : undefined; };
const IN = arg('--in');
if (!IN) { console.error('gebruik: node statuspagina.mjs --in <map> [--rapport <json>] [--schermafdrukken <map>]'); process.exit(2); }
const RAPPORT = arg('--rapport');
const SHOTS = arg('--schermafdrukken');
const meta = JSON.parse(fs.readFileSync(path.join(IN, 'toestanden.json'), 'utf8'));
if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true });

const server = http.createServer((req, res) => {
  const naam = decodeURIComponent((req.url || '/').slice(1).split('?')[0]);
  const bestand = path.join(IN, `${naam}.html`);
  if (!meta.toestanden.some((t) => t.naam === naam) || !fs.existsSync(bestand)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', ...meta.headers });
  res.end(fs.readFileSync(bestand));
});
await new Promise((r) => { server.listen(0, '127.0.0.1', r); });
const BASIS = `http://127.0.0.1:${server.address().port}`;

const browser = await chromium.launch();
const BROWSER = `chromium ${browser.version()}`;
const resultaten = [];
let fouten = 0;
for (const { naam, schermafdruk } of meta.toestanden) {
  for (const breedte of BREEDTES) {
    for (const [thema, scheme] of THEMAS) {
      const context = await browser.newContext({ viewport: { width: breedte, height: 900 }, colorScheme: scheme, deviceScaleFactor: 1, hasTouch: breedte < 1024 });
      const page = await context.newPage();
      const console_ = [];
      const extern = [];
      page.on('console', (m) => { if (m.type() === 'error' || /Content Security Policy|Refused to/i.test(m.text())) console_.push(m.text()); });
      page.on('request', (r) => { if (!r.url().startsWith(`${BASIS}/${naam}`)) extern.push(r.url()); });
      const antwoord = await page.goto(`${BASIS}/${naam}`, { waitUntil: 'load' });
      const csp = antwoord.headers()['content-security-policy'] || '';
      const { problemen, meting } = await controleer(page);
      if (console_.length) problemen.push(`console/CSP: ${console_.join(' | ')}`);
      if (extern.length) problemen.push(`externe verzoeken: ${extern.join(', ')}`);
      if (!csp.includes("default-src 'none'")) problemen.push(`CSP zonder default-src 'none': ${csp}`);
      if (breedte === 320 && thema === 'licht') {
        // WCAG 1.4.12 tekstafstand en het lettertype van de CI-runner (E12): geen overloop, geen inhoud buiten zijn vak
        const r = await reflow320(page);
        problemen.push(...r.problemen);
        meting.reflow_320 = r.meting;
      }
      let afdruk = null;
      if (SHOTS && schermafdruk && SHOT_BREEDTES.has(breedte)) {
        afdruk = `${naam}-${breedte}-${thema}.png`;
        await page.screenshot({ path: path.join(SHOTS, afdruk), fullPage: true });
      }
      fouten += problemen.length ? 1 : 0;
      resultaten.push({ toestand: naam, breedte, thema, problemen, ...meting, overloop: meting.scroll_breedte > breedte, schermafdruk: afdruk });
      console.log(`${problemen.length ? 'FOUT' : 'ok  '} ${naam.padEnd(24)} ${String(breedte).padStart(4)} ${thema.padEnd(6)} axe ${meting.axe_overtredingen} · tekst ≥ ${meting.tekst_min} · ui ≥ ${meting.ui_min}${afdruk ? ` · ${afdruk}` : ''}${problemen.length ? `\n     ${problemen.join('\n     ')}` : ''}`);
      await context.close();
    }
  }
}
await browser.close();
server.close();

const rapport = {
  peil: meta.peil,
  axe_core: AXE_VERSION,
  axe_tags: AXE_TAGS,
  browser: BROWSER,
  breedtes: BREEDTES,
  lettertype_reflow: LETTER,
  themas: THEMAS.map(([t]) => t),
  toestanden: meta.toestanden.map((t) => t.naam),
  controles: resultaten.length,
  mislukt: fouten,
  resultaten,
};
if (RAPPORT) fs.writeFileSync(RAPPORT, `${JSON.stringify(rapport, null, 1)}\n`);
console.log(`\n${resultaten.length} controles, ${fouten} mislukt (axe-core ${AXE_VERSION}, ${rapport.browser})`);
process.exit(fouten ? 1 : 0);
