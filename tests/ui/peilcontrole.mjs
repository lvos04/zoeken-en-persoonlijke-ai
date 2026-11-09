// Peil-controles op één geladen statuspagina (ONTWERPTAAL §4, §11, §12 en §13.2). Gedeeld door statuspagina.mjs
// (toestanden uit de renderer) en echte_dienst.mjs (de echte dienst met testdata). Afgeleid van de referentie
// ai-server-manager/design/ontwerptaal/bouw/controle.cjs: contrast gemeten in de gerenderde DOM (ook vlakken en
// randen), doelgrootte, overloop, afkappen, lettermaat, focusdoorloop met bedekte focus, structuur en axe-core.
//
// Uitkomst: { problemen: string[], meting: {...} }. Elk probleem laat de controle falen.
import fs from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
export const AXE = fs.readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
export const AXE_VERSION = require('axe-core/package.json').version;
export const AXE_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'];
export const BREEDTES = [320, 360, 390, 768, 1440];
export const THEMAS = [['licht', 'light'], ['donker', 'dark']];

// Vaste woorden (ONTWERPTAAL §11.1): wat op een pil mag staan, en woorden die Peil vervangen heeft.
export const PIL_WOORDEN = new Set([
  'Gezond', 'Beperkt', 'Storing', 'Onbekend', 'Alles gezond', 'Deels onbekend', 'Niet gereed',
  'Gekoppeld', 'Niet gekoppeld', 'Ontkoppeld', 'Ingetrokken', 'Onleesbaar',
  'Nog niet beschikbaar', 'Niet beschikbaar', 'Niet ingesteld', 'Alleen met toestemming',
]);
// Niet-gezonde woorden: een pil met dit woord mag nooit de gezond-stijl (groen) hebben.
const NIET_GROEN = /onbekend|niet gereed|niet gekoppeld|ontkoppeld|ingetrokken|onleesbaar|beschikbaar|niet ingesteld|verouderd|laatst bekend|storing|beperkt/i;
const VERBODEN = [
  /\bin orde\b/i, /\bActueel\b/, /\bVerminderd\b/, /\bVerstoord\b/, /\bLet op\b/, /\bOffline\b/, /\bProbleem\b/,
  /\bNiet gezond\b/, /Laden…/, /\bGelukt\b/, /\bOK\b/, /\bDoorgaan\b/, /Weet je het zeker/, /snooz/i, /safe mode/i,
  /blast radius/i, /\brunbook\b/i, /\bhealthcheck\b/i, /\ballowlist\b/i, /Ctrl\+C/, /!/,
];

// ------------------------------------------------------------------------------------------- in de pagina
const IN_PAGINA = () => {
  const cvs = document.createElement('canvas'); cvs.width = cvs.height = 1;
  const ctx = cvs.getContext('2d', { willReadFrequently: true });
  const cache = new Map();
  const rgba = (s) => { if (cache.has(s)) return cache.get(s); ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = '#000'; ctx.fillStyle = s; ctx.fillRect(0, 0, 1, 1); const d = ctx.getImageData(0, 0, 1, 1).data; const v = [d[0], d[1], d[2], d[3] / 255]; cache.set(s, v); return v; };
  const over = (t, b) => { const a = t[3] + b[3] * (1 - t[3]); if (a === 0) return [0, 0, 0, 0]; return [0, 1, 2].map((i) => (t[i] * t[3] + b[i] * b[3] * (1 - t[3])) / a).concat([a]); };
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const ratio = (a, b) => { const x = lum(a); const y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const hex = (c) => `#${c.slice(0, 3).map((v) => Math.round(v).toString(16).padStart(2, '0')).join('')}`;
  const keten = (el) => { const c = []; for (let e = el; e && e.nodeType === 1; e = e.parentElement) c.push(e); return c; };
  const vlak = (el) => { let c = [255, 255, 255, 1]; for (const e of keten(el).reverse()) c = over(rgba(getComputedStyle(e).backgroundColor), c); return c; };
  const srOnly = (el) => keten(el).some((e) => { const cs = getComputedStyle(e); return cs.clip === 'rect(0px, 0px, 0px, 0px)' || (cs.position === 'absolute' && parseFloat(cs.width) <= 1 && parseFloat(cs.height) <= 1 && cs.overflow === 'hidden'); });
  const zichtbaar = (el) => { const r = el.getBoundingClientRect(); if (!r.width || !r.height) return false; if (getComputedStyle(el).visibility === 'hidden') return false; if (r.bottom < 0 && getComputedStyle(el).position === 'absolute') return false; return !srOnly(el); };
  const naam = (el) => { const id = el.id ? `#${el.id}` : ''; const cls = (el.getAttribute('class') || '').split(/\s+/).filter(Boolean).slice(0, 3).join('.'); return el.tagName.toLowerCase() + id + (cls ? `.${cls}` : ''); };
  const eigenTekst = (el) => [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join('').replace(/\s+/g, ' ').trim();

  // 1. tekstcontrast (eis 4,5:1; groot 3:1) en lettermaat
  const tekst = [];
  for (const el of document.querySelectorAll('body *')) {
    if (['SCRIPT', 'STYLE', 'svg', 'use', 'symbol', 'path', 'circle', 'line', 'rect'].includes(el.tagName)) continue;
    const t = eigenTekst(el); if (!t || !zichtbaar(el)) continue;
    const cs = getComputedStyle(el); const fg = rgba(cs.color); const B = vlak(el); const T = over(fg, B);
    const fs = parseFloat(cs.fontSize); const fw = parseInt(cs.fontWeight, 10);
    const groot = fs >= 24 || (fs >= 18.66 && fw >= 700);
    tekst.push({ el: naam(el), t: t.slice(0, 50), fs, eis: groot ? 3 : 4.5, r: +ratio(T, B).toFixed(2), fg: hex(T), bg: hex(B) });
  }
  // 2. niet-tekst (1.4.11): iconen, betekenisdragende randen, veldranden en knoppen ≥ 3:1 t.o.v. de omgeving
  const ui = [];
  for (const svg of document.querySelectorAll('body svg')) {
    if (!zichtbaar(svg) || svg.closest('[style*="display:none"]')) continue;
    const B = vlak(svg.parentElement); const kleur = over(rgba(getComputedStyle(svg).color), B);
    ui.push({ soort: 'icoon', el: naam(svg.parentElement), r: +ratio(kleur, B).toFixed(2), fg: hex(kleur), bg: hex(B) });
  }
  const rand = (el, soort) => {
    const cs = getComputedStyle(el); const buiten = vlak(el.parentElement);
    if (cs.borderTopStyle === 'none' || parseFloat(cs.borderTopWidth) === 0) return;
    const bc = over(rgba(cs.borderTopColor), buiten);
    if (bc[3] === 0 || rgba(cs.borderTopColor)[3] === 0) return; // transparante rand (Gezond heeft bewust geen vlak)
    ui.push({ soort, el: naam(el), stijl: cs.borderTopStyle, bw: parseFloat(cs.borderTopWidth), r: +ratio(bc, buiten).toFixed(2), fg: hex(bc), bg: hex(buiten) });
  };
  for (const el of document.querySelectorAll('.pil, .melding, .selecteer, .knop-secundair')) if (zichtbaar(el)) rand(el, 'rand');
  for (const el of document.querySelectorAll('input:not([type=hidden])')) if (zichtbaar(el)) rand(el, 'veldrand');
  for (const el of document.querySelectorAll('.knop:not(.knop-secundair)')) {
    if (!zichtbaar(el)) continue; const buiten = vlak(el.parentElement); const eigen = over(rgba(getComputedStyle(el).backgroundColor), buiten);
    ui.push({ soort: 'knopvlak', el: naam(el), r: +ratio(eigen, buiten).toFixed(2), fg: hex(eigen), bg: hex(buiten) });
  }
  // 3. doelgrootte (Peil: 44 × 44 px voor elk bedienbaar element; uitzondering: inline link in lopende tekst, WCAG 2.5.8)
  const doelen = []; const inline = [];
  for (const el of document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, summary')) {
    if (!zichtbaar(el)) continue;
    const r = el.getBoundingClientRect();
    const isInline = el.tagName === 'A' && getComputedStyle(el).display === 'inline' && eigenTekst(el.parentElement).length > 0;
    const item = { el: naam(el), t: (el.innerText || el.name || '').replace(/\s+/g, ' ').trim().slice(0, 40), w: Math.round(r.width), h: Math.round(r.height) };
    if (isInline) inline.push(item); else if (r.width < 44 || r.height < 44) doelen.push(item);
  }
  // 4. overloop en afkappen
  const se = document.scrollingElement;
  const afgekapt = [...document.querySelectorAll('body *')].filter((el) => getComputedStyle(el).textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + 1).map(naam);
  const uitstekend = [...document.querySelectorAll('.wrap *')].filter((el) => zichtbaar(el) && el.getBoundingClientRect().right > se.clientWidth + 0.5).map(naam).slice(0, 5);
  // 5. structuur
  const koppen = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map((e) => +e.tagName[1]);
  const sprong = []; for (let k = 1; k < koppen.length; k += 1) if (koppen[k] > koppen[k - 1] + 1) sprong.push(`h${koppen[k - 1]}→h${koppen[k]}`);
  const ids = {}; for (const e of document.querySelectorAll('[id]')) ids[e.id] = (ids[e.id] || 0) + 1;
  const kapot = [];
  for (const e of document.querySelectorAll('[aria-describedby],[aria-labelledby],[aria-controls]')) for (const a of ['aria-describedby', 'aria-labelledby', 'aria-controls']) for (const id of (e.getAttribute(a) || '').split(/\s+/).filter(Boolean)) if (!document.getElementById(id)) kapot.push(`${a}=${id}`);
  for (const a of document.querySelectorAll('a[href^="#"]')) { const id = a.getAttribute('href').slice(1); if (id && !document.getElementById(id)) kapot.push(`href=#${id}`); }
  for (const u of document.querySelectorAll('use')) { const id = (u.getAttribute('href') || '').slice(1); if (!document.getElementById(id)) kapot.push(`use=#${id}`); }
  // 6. status nooit alleen kleur, onbekend nooit groen, vaste woorden
  const pillen = [...document.querySelectorAll('.pil')].map((p) => {
    const use = p.querySelector('svg use'); const toon = [...p.classList].find((c) => c.startsWith('pil-')) || '';
    return { toon, woord: p.textContent.replace(/\s+/g, ' ').trim(), icoon: use ? use.getAttribute('href') : null, kleur: getComputedStyle(p).color };
  });
  const gezondKleur = (() => { const p = document.createElement('span'); p.className = 'pil pil-gezond'; document.body.append(p); const c = getComputedStyle(p).color; p.remove(); return c; })();
  // 7. zichtbare tekst voor de woordcontrole (geen code/trace-ID/paden)
  const kloon = document.body.cloneNode(true);
  for (const e of kloon.querySelectorAll('code, svg, style, .selecteer, input')) e.remove();
  return {
    tekst, ui, doelen, inline, afgekapt, uitstekend, pillen, gezondKleur,
    minFont: Math.min(...tekst.map((x) => x.fs)),
    scrollWidth: se.scrollWidth, clientWidth: se.clientWidth,
    structuur: { h1: document.querySelectorAll('h1').length, main: document.querySelectorAll('main').length, sprong, dubbel: Object.keys(ids).filter((k) => ids[k] > 1), kapot, lang: document.documentElement.lang, titel: document.title },
    scripts: document.querySelectorAll('script').length,
    handlers: [...document.querySelectorAll('*')].flatMap((el) => [...el.attributes].map((a) => a.name)).filter((n) => n.startsWith('on')),
    zichtbareTekst: kloon.textContent.replace(/\s+/g, ' ').trim(),
  };
};

const FOCUS = () => {
  const el = document.activeElement; if (!el || el === document.body) return null;
  const cvs = document.createElement('canvas'); cvs.width = cvs.height = 1; const ctx = cvs.getContext('2d');
  const rgba = (s) => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = '#000'; ctx.fillStyle = s; ctx.fillRect(0, 0, 1, 1); const d = ctx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const over = (t, b) => { const a = t[3] + b[3] * (1 - t[3]); return [0, 1, 2].map((i) => (t[i] * t[3] + b[i] * b[3] * (1 - t[3])) / (a || 1)).concat([a]); };
  const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
  const ratio = (a, b) => { const x = lum(a); const y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const vlak = (e) => { const l = []; for (let p = e; p && p.nodeType === 1; p = p.parentElement) l.push(rgba(getComputedStyle(p).backgroundColor)); let c = [255, 255, 255, 1]; for (const x of l.reverse()) c = over(x, c); return c; };
  const cs = getComputedStyle(el); const r = el.getBoundingClientRect();
  const ow = parseFloat(cs.outlineWidth); const heeft = cs.outlineStyle !== 'none' && ow > 0;
  const buiten = vlak(el.parentElement);
  const pts = [[r.left + r.width / 2, r.top + r.height / 2], [r.left + 2, r.top + 2], [r.right - 2, r.bottom - 2]];
  let inBeeld = 0; let zichtbaar = 0;
  for (const [x, y] of pts) { if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue; inBeeld += 1; const top = document.elementFromPoint(x, y); if (top && (top === el || el.contains(top) || top.contains(el))) zichtbaar += 1; }
  return {
    el: `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''} "${(el.innerText || el.name || '').replace(/\s+/g, ' ').trim().slice(0, 30)}"`,
    ring: heeft ? `${ow}px ${cs.outlineStyle}` : 'geen', ringR: heeft ? +ratio(over(rgba(cs.outlineColor), buiten), buiten).toFixed(2) : 0,
    halo: cs.boxShadow !== 'none', inBeeld, zichtbaar,
  };
};

// ------------------------------------------------------------------------------------------- per pagina
export async function controleer(page, { metFocus = true } = {}) {
  const problemen = [];
  const m = await page.evaluate(IN_PAGINA);
  const tekstFout = m.tekst.filter((x) => x.r < x.eis);
  if (tekstFout.length) problemen.push(`tekstcontrast < 4,5:1: ${tekstFout.slice(0, 5).map((x) => `${x.el} "${x.t}" ${x.r} (${x.fg} op ${x.bg})`).join('; ')}`);
  const uiFout = m.ui.filter((x) => x.r < 3);
  if (uiFout.length) problemen.push(`niet-tekstcontrast < 3:1: ${uiFout.slice(0, 5).map((x) => `${x.soort} ${x.el} ${x.r} (${x.fg} op ${x.bg})`).join('; ')}`);
  if (m.minFont < 14) problemen.push(`tekst kleiner dan 14 px: ${m.minFont}px`);
  if (m.doelen.length) problemen.push(`doelen < 44 × 44 px: ${JSON.stringify(m.doelen)}`);
  if (m.scrollWidth > m.clientWidth) problemen.push(`horizontale overloop: ${m.scrollWidth} > ${m.clientWidth} (${m.uitstekend.join(', ')})`);
  if (m.afgekapt.length) problemen.push(`afgekapt met ellips: ${m.afgekapt.join(', ')}`);
  const s = m.structuur;
  if (s.h1 !== 1 || s.main !== 1 || s.lang !== 'nl' || s.sprong.length || s.dubbel.length || s.kapot.length) problemen.push(`structuur: ${JSON.stringify(s)}`);
  if (m.scripts || m.handlers.length) problemen.push(`JavaScript op de pagina: ${m.scripts} script, ${m.handlers.join(',')}`);
  for (const p of m.pillen) {
    if (!p.icoon || !p.woord) problemen.push(`pil zonder icoon of woord (status alleen kleur): ${JSON.stringify(p)}`);
    if (!PIL_WOORDEN.has(p.woord)) problemen.push(`pil met een woord buiten de Peil-woordenlijst: "${p.woord}"`);
    if (NIET_GROEN.test(p.woord) && (p.toon === 'pil-gezond' || p.kleur === m.gezondKleur)) problemen.push(`niet-gezonde toestand in groen: "${p.woord}" (${p.toon})`);
    if (p.toon === 'pil-gezond' && !/^(Alles gezond|Gezond)$/.test(p.woord)) problemen.push(`groene pil met ander woord dan Gezond: "${p.woord}"`);
  }
  for (const re of VERBODEN) { const hit = m.zichtbareTekst.match(re); if (hit) problemen.push(`woord buiten de Peil-woordenlijst of toon: "${hit[0]}"`); }

  let focus = null;
  if (metFocus) {
    const stops = [];
    for (let i = 0; i < 60; i += 1) {
      await page.keyboard.press('Tab');
      const f = await page.evaluate(FOCUS);
      if (!f) break;
      if (stops.length && f.el === stops[0].el) break;
      stops.push(f);
    }
    focus = { aantal: stops.length, eerste: stops[0]?.el, ringMin: Math.min(...stops.map((f) => f.ringR)) };
    const zonder = stops.filter((f) => f.ring === 'geen' || f.ringR < 3 || !f.halo);
    if (zonder.length) problemen.push(`focusring ontbreekt of < 3:1: ${zonder.map((f) => `${f.el} ${f.ring} ${f.ringR}`).join('; ')}`);
    const bedekt = stops.filter((f) => f.inBeeld === 0 || f.zichtbaar === 0);
    if (bedekt.length) problemen.push(`focus buiten beeld of bedekt: ${bedekt.map((f) => f.el).join('; ')}`);
    if (stops.length && !/^a /.test(stops[0].el)) problemen.push(`eerste tabstop is geen skiplink: ${stops[0].el}`);
  }

  await page.evaluate(AXE);
  const axe = await page.evaluate(async (tags) => {
    const r = await window.axe.run(document, { runOnly: { type: 'tag', values: tags }, resultTypes: ['violations', 'incomplete'] });
    return { violations: r.violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => n.target.join(' ')) })), incomplete: r.incomplete.map((v) => v.id) };
  }, AXE_TAGS);
  if (axe.violations.length) problemen.push(`axe: ${axe.violations.map((v) => `${v.id} (${v.nodes.join(', ')})`).join('; ')}`);

  const minTekst = Math.min(...m.tekst.map((x) => x.r));
  const minUi = m.ui.length ? Math.min(...m.ui.map((x) => x.r)) : null;
  return {
    problemen,
    meting: {
      axe_overtredingen: axe.violations.length, axe_onvolledig: axe.incomplete,
      tekst_min: +minTekst.toFixed(2), tekst_min_waar: m.tekst.find((x) => x.r === minTekst)?.el,
      tekst_onder_7: m.tekst.filter((x) => x.r < 7).map((x) => `${x.el} ${x.r}`).slice(0, 6),
      ui_min: minUi, ui_min_waar: m.ui.find((x) => x.r === minUi)?.el,
      min_lettermaat: m.minFont, scroll_breedte: m.scrollWidth, inline_links: m.inline.map((x) => `${x.t} (${x.w}×${x.h})`),
      pillen: m.pillen.map((p) => `${p.toon}:${p.woord}`), titel: m.structuur.titel, focus,
    },
  };
}
