// Browsercontrole van de Peil-statuspagina (ONTWERPTAAL §12/§13.2). Alleen ontwikkeling/CI.
//
//   python tests/ui/render_toestanden.py --out <map>
//   node tests/ui/statuspagina.mjs --in <map> [--rapport <bestand.json>] [--schermafdrukken <map>]
//
// Per toestand × breedte (320/360/390/768/1440) × thema (licht/donker), geserveerd met de echte beveiligingskoppen
// van de dienst (CSP default-src 'none'):
//   - axe-core: 0 overtredingen op WCAG 2.0/2.1/2.2 A + AA (en best practices);
//   - geen horizontale overloop (scrollWidth ≤ breedte), ook niet op 320 px;
//   - geen JavaScript op de pagina (geen <script>, geen on…-attributen), geen CSP-schending, geen extern verzoek;
//   - knoppen en invoervelden ≥ 44 px hoog (Peil: 44 px overal).
// Schermafdrukken (volledige pagina) voor 360/768/1440 × licht/donker van de toestanden met "schermafdruk": true.
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'node:module';
import { chromium } from 'playwright';

const require = createRequire(import.meta.url);
const AXE = fs.readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
const AXE_VERSION = require('axe-core/package.json').version;
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'];
const BREEDTES = [320, 360, 390, 768, 1440];
const SHOT_BREEDTES = new Set([360, 768, 1440]);
const THEMAS = [['licht', 'light'], ['donker', 'dark']];

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
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const BASIS = `http://127.0.0.1:${server.address().port}`;

const browser = await chromium.launch();
const BROWSER = `chromium ${browser.version()}`;
const resultaten = [];
let fouten = 0;
for (const { naam, schermafdruk } of meta.toestanden) {
  for (const breedte of BREEDTES) {
    for (const [thema, scheme] of THEMAS) {
      const context = await browser.newContext({ viewport: { width: breedte, height: 900 }, colorScheme: scheme, deviceScaleFactor: 1 });
      const page = await context.newPage();
      const console_ = [];
      const extern = [];
      page.on('console', (m) => { if (m.type() === 'error' || /Content Security Policy|Refused to/i.test(m.text())) console_.push(m.text()); });
      page.on('request', (r) => { if (!r.url().startsWith(`${BASIS}/${naam}`)) extern.push(r.url()); });
      const antwoord = await page.goto(`${BASIS}/${naam}`, { waitUntil: 'load' });
      const csp = antwoord.headers()['content-security-policy'] || '';
      const dom = await page.evaluate(() => {
        const knoppen = [...document.querySelectorAll('button, input:not([type=hidden]), .knop')]
          .map((el) => ({ el: el.id || el.name || el.textContent.trim().slice(0, 30), h: el.getBoundingClientRect().height }))
          .filter((k) => k.h < 44);
        const handlers = [...document.querySelectorAll('*')].flatMap((el) => [...el.attributes].map((a) => a.name)).filter((n) => n.startsWith('on'));
        return {
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
          scripts: document.querySelectorAll('script').length,
          handlers,
          kleineDoelen: knoppen,
          h1: document.querySelectorAll('h1').length,
          lang: document.documentElement.lang,
        };
      });
      await page.evaluate(AXE);
      const axe = await page.evaluate(async (tags) => {
        const r = await window.axe.run(document, { runOnly: { type: 'tag', values: tags }, resultTypes: ['violations', 'incomplete'] });
        return {
          violations: r.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => n.target.join(' ')) })),
          incomplete: r.incomplete.map((v) => v.id),
        };
      }, TAGS);
      const overloop = dom.scrollWidth > breedte;
      const problemen = [];
      if (axe.violations.length) problemen.push(`axe: ${axe.violations.map((v) => `${v.id} (${v.nodes.join(', ')})`).join('; ')}`);
      if (overloop) problemen.push(`horizontale overloop: scrollWidth ${dom.scrollWidth} > ${breedte}`);
      if (dom.scripts || dom.handlers.length) problemen.push(`JavaScript op de pagina: ${dom.scripts} script, ${dom.handlers.join(',')}`);
      if (console_.length) problemen.push(`console/CSP: ${console_.join(' | ')}`);
      if (extern.length) problemen.push(`externe verzoeken: ${extern.join(', ')}`);
      if (!csp.includes("default-src 'none'")) problemen.push(`CSP zonder default-src 'none': ${csp}`);
      if (dom.kleineDoelen.length) problemen.push(`doelen < 44 px: ${JSON.stringify(dom.kleineDoelen)}`);
      if (dom.h1 !== 1 || dom.lang !== 'nl') problemen.push(`structuur: ${dom.h1} h1, lang=${dom.lang}`);
      let afdruk = null;
      if (SHOTS && schermafdruk && SHOT_BREEDTES.has(breedte)) {
        afdruk = `${naam}-${breedte}-${thema}.png`;
        await page.screenshot({ path: path.join(SHOTS, afdruk), fullPage: true });
      }
      fouten += problemen.length ? 1 : 0;
      resultaten.push({
        toestand: naam, breedte, thema,
        axe_overtredingen: axe.violations.length, axe_onvolledig: axe.incomplete,
        overloop, scroll_breedte: dom.scrollWidth, problemen, schermafdruk: afdruk,
      });
      console.log(`${problemen.length ? 'FOUT' : 'ok  '} ${naam.padEnd(24)} ${String(breedte).padStart(4)} ${thema.padEnd(6)} axe ${axe.violations.length}${afdruk ? ` · ${afdruk}` : ''}${problemen.length ? `\n     ${problemen.join('\n     ')}` : ''}`);
      await context.close();
    }
  }
}
await browser.close();
server.close();

const rapport = {
  peil: meta.peil,
  axe_core: AXE_VERSION,
  axe_tags: TAGS,
  browser: BROWSER,
  breedtes: BREEDTES,
  themas: THEMAS.map(([t]) => t),
  toestanden: meta.toestanden.map((t) => t.naam),
  controles: resultaten.length,
  mislukt: fouten,
  resultaten,
};
if (RAPPORT) fs.writeFileSync(RAPPORT, `${JSON.stringify(rapport, null, 1)}\n`);
console.log(`\n${resultaten.length} controles, ${fouten} mislukt (axe-core ${AXE_VERSION}, ${rapport.browser})`);
process.exit(fouten ? 1 : 0);
