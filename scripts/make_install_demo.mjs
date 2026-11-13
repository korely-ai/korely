/**
 * The README's install recording, rendered: frames of a terminal playing a
 * real run of install.sh, then the browser opening the address it printed,
 * which scripts/make_install_demo.sh turns into docs/assets/install.gif and
 * docs/assets/install.mp4. scripts/make_terminal_demos.sh renders its
 * recordings (several typed commands, no browser part) with it too.
 *
 *   bash scripts/make_install_demo.sh
 *
 * does everything. Run by hand, with a recording and (for the browser part)
 * an install that is still up:
 *
 *   CAST=<run.cast> OUT=<dir> [BASE=http://127.0.0.1:8125 OPU=<login> OPP=<password>] \
 *     [FINAL=<seconds the last frame is held, default 7>] [BUSY=<seconds, default 20>] \
 *     [TITLE=<title bar>] \
 *     node make_install_demo.mjs
 *
 * from a directory where `npm ci` has been run on a copy of scripts/package.json
 * and scripts/package-lock.json (never in scripts/ itself).
 *
 * The terminal is xterm.js fed the bytes scripts/record_terminal.py recorded,
 * in their order: nothing is added to them or taken out. Only the clock is
 * changed, and the frame says so when it is: a pause longer than IDLE seconds
 * is played in HOLD seconds, with a badge in the title bar saying how long it
 * really was; a stretch of output longer than BUSY seconds (a download) is
 * played faster, with a badge saying how much. A pause the recording named
 * (a "wait:<why>" marker) says what it was in its badge. A command being typed or
 * pasted (from a "typing" or "pasted" marker to its "enter") is never sped up
 * and never shortened. The browser part is the
 * install's own dashboard, screenshotted under a drawn address bar: the login
 * typed in is the one the run printed.
 *
 * Writes OUT/f0000.png..., OUT/frames.txt (the ffmpeg concat list, with each
 * frame's duration), OUT/still-1.png (the pasted line), OUT/still-2.png (the
 * final printout) and OUT/timeline.json (what was shortened, and by how much,
 * and the frame on screen at each marker: scripts/terminal_demo_lib.sh's
 * demo_stack holds a pane on the one at its "start" marker).
 */
import { chromium } from 'playwright';
import { readFileSync, writeFileSync, copyFileSync, mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';

const CAST = process.env.CAST;
const OUT = process.env.OUT ?? '/out';
const BASE = process.env.BASE ?? '';
const OPU = process.env.OPU ?? '';
const OPP = process.env.OPP ?? '';
const DPR = Number(process.env.DPR ?? 2);
if (!CAST) {
  console.error('make_install_demo.mjs: CAST is needed (see the header)');
  process.exit(2);
}

const WIDTH = 880;        // README width, 1:1 in the GIF
const TITLE = 34;         // the window's title bar
const PAD = 14;
const IDLE = 5;           // a pause longer than this is shortened...
const HOLD = 1.8;         // ...to this, with a badge
const BUSY = Number(process.env.BUSY ?? 20);   // a stretch of output longer than this is sped up to it
const TICK = 1 / 15;      // the shortest frame
const FINAL = Number(process.env.FINAL ?? 7);   // the final printout, held to be read
mkdirSync(OUT, { recursive: true });

// ── the recording, and its clock ────────────────────────────────────────────
const [headerLine, ...lines] = readFileSync(CAST, 'utf8').trim().split('\n');
const header = JSON.parse(headerLine);
const events = lines.map((l) => { const [t, type, data] = JSON.parse(l); return { t, type, data }; });
const marker = (name) => events.find((e) => e.type === 'm' && e.data === name);
const enter = marker('enter');
if (!enter || !marker('done')) throw new Error(`${CAST} has no enter/done markers: not a record_terminal.py file`);

// Runs: stretches of events with no pause longer than IDLE. A command being
// typed or pasted, up to its Enter, is its own run and is never sped up.
const starts = (e) => e.type === 'm' && ['typing', 'pasted', 'enter'].includes(e.data);
const runs = [];
let run = null;
let prevT = 0;
let typed = false;
for (const e of events) {
  if (starts(e)) typed = e.data !== 'enter';
  if (!run || e.t - prevT > IDLE || starts(e)) {
    run = { from: e.t, to: e.t, events: [], typed: typed || e.t < enter.t };
    runs.push(run);
  }
  run.events.push(e);
  run.to = e.t;
  prevT = e.t;
}
const fmt = (s) => (s >= 90 ? `${Math.floor(s / 60)} min ${Math.round(s % 60)} s` : `${Math.round(s)} s`);
const badges = [];
let v = 0;
prevT = 0;
let why = '';   // a "wait:<why>" marker (record_terminal.py's "until") names the pause after it
for (const r of runs) {
  const gap = r.from - prevT;
  if (gap > IDLE) {
    const text = why ? `… ${fmt(gap)}: ${why}, shortened` : `… ${fmt(gap)} of waiting, shortened`;
    badges.push({ from: v, to: v + HOLD, text, real: gap });
    v += HOLD;
  } else {
    v += gap;
  }
  const length = r.to - r.from;
  const speed = !r.typed && length > BUSY && length / BUSY >= 1.5 ? length / BUSY : 1;
  const start = v;
  for (const e of r.events) e.v = start + (e.t - r.from) / speed;
  v = start + length / speed;
  if (speed > 1) badges.push({ from: start, to: v, text: `» ${Math.round(speed)}× faster (${fmt(length)})`, real: length });
  const last = r.events[r.events.length - 1];
  why = last.type === 'm' && String(last.data).startsWith('wait:') ? last.data.slice(5) : '';
  prevT = r.to;
}
const end = marker('done').v;

// Frame boundaries: every event, badge edge and marker, no two closer than
// TICK (an event inside a TICK shows on the next frame).
// A "start" marker (the shell's prompt up, nothing typed) is a frame of its
// own: scripts/terminal_demo_lib.sh's demo_stack holds a pane on it.
const start = marker('start');
const keep = new Set([...badges.flatMap((b) => [b.from, b.to]), enter.v, end, ...(start ? [start.v] : [])]);
const cands = [...new Set([0, ...events.map((e) => e.v), ...keep])].filter((x) => x <= end).sort((a, b) => a - b);
const bounds = [];
for (const b of cands) {
  if (!bounds.length || keep.has(b) || b - bounds[bounds.length - 1] >= TICK) bounds.push(b);
}

// ── the stage ───────────────────────────────────────────────────────────────
const require = createRequire(import.meta.url);
const XTERM_JS = require.resolve('@xterm/xterm/lib/xterm.js');
const XTERM_CSS = require.resolve('@xterm/xterm/css/xterm.css');

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: WIDTH, height: 1000 }, deviceScaleFactor: DPR });
const stage = await ctx.newPage();
await stage.setContent(`<!doctype html><html><head><style>
  html, body { margin: 0; background: #0b1020; }
  #stage { width: ${WIDTH}px; background: #0b1020; font-family: "Liberation Sans", sans-serif; }
  .bar { height: ${TITLE}px; display: flex; align-items: center; padding: 0 14px; box-sizing: border-box;
         background: #1f2937; color: #cbd5e1; font-size: 13px; position: relative; gap: 8px; }
  .dot { width: 12px; height: 12px; border-radius: 50%; }
  .title { position: absolute; left: 0; right: 0; text-align: center; pointer-events: none; }
  #badge { margin-left: auto; z-index: 1; background: #fbbf24; color: #111827; font-weight: 700;
           font-size: 13px; padding: 3px 10px; border-radius: 999px; visibility: hidden; }
  #term { padding: ${PAD}px; background: #111827; }
  #browser { display: none; background: #fff; }
  .url { margin: 0 auto; width: 60%; z-index: 1; background: #111827; color: #e5e7eb; border-radius: 6px;
         padding: 4px 12px; font-size: 13px; font-family: "Liberation Sans", sans-serif; }
  #page { display: block; width: ${WIDTH}px; }
</style></head><body><div id="stage">
  <div id="twin"><div class="bar"><span class="dot" style="background:#ef4444"></span><span class="dot" style="background:#f59e0b"></span><span class="dot" style="background:#22c55e"></span>
    <span class="title">${process.env.TITLE ?? `zsh — ${header.width}×${header.height}`}</span><span id="badge"></span></div>
    <div id="term"></div></div>
  <div id="browser"><div class="bar"><span class="dot" style="background:#ef4444"></span><span class="dot" style="background:#f59e0b"></span><span class="dot" style="background:#22c55e"></span>
    <span class="url" id="url"></span></div><img id="page" alt=""></div>
</div></body></html>`);
await stage.addStyleTag({ path: XTERM_CSS });
await stage.addScriptTag({ path: XTERM_JS });
await stage.evaluate(({ cols, rows, pad }) => {
  // The largest font at which the recorded width fits the stage.
  const c = document.createElement('canvas').getContext('2d');
  c.font = '100px "Liberation Mono"';
  const em = c.measureText('M').width / 100;
  const size = Math.floor(((document.getElementById('stage').clientWidth - 2 * pad) / cols / em) * 4) / 4;
  const term = new window.Terminal({
    cols, rows, fontFamily: '"Liberation Mono", monospace', fontSize: size, lineHeight: 1.12,
    cursorBlink: false, cursorInactiveStyle: 'block', scrollback: 2000, convertEol: false,
    theme: { background: '#111827', foreground: '#e5e7eb', cursor: '#e5e7eb' },
  });
  term.open(document.getElementById('term'));
  const frame = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  window.feed = (text) => new Promise((r) => term.write(text, () => frame().then(r)));
  window.badge = (text) => {
    const b = document.getElementById('badge');
    b.textContent = text ?? '';
    b.style.visibility = text ? 'visible' : 'hidden';
  };
  window.fontSize = size;
}, { cols: header.width, rows: header.height, pad: PAD });
await stage.evaluate(() => document.fonts.ready);
const box = await stage.locator('#stage').boundingBox();
const clip = { x: 0, y: 0, width: WIDTH, height: Math.ceil(box.height) };
console.log(`  terminal ${header.width}×${header.height} at ${await stage.evaluate(() => window.fontSize)} px, frame ${WIDTH}×${clip.height}`);

const frames = [];
async function shot(seconds) {
  const name = `f${String(frames.length).padStart(4, '0')}.png`;
  await stage.screenshot({ path: join(OUT, name), clip });
  frames.push([name, seconds]);
  return name;
}

// ── the terminal ────────────────────────────────────────────────────────────
console.log(`  ${events.length} events, ${bounds.length} frames, ${badges.length} shortened stretches`);
let fed = 0;
let still1 = null;
const out = events.filter((e) => e.type === 'o');
const shown = [];   // the frame on screen from bounds[i]
for (let i = 0; i < bounds.length; i++) {
  const at = bounds[i];
  let text = '';
  while (fed < out.length && out[fed].v <= at + 1e-9) text += out[fed++].data;
  if (text) await stage.evaluate((t) => window.feed(t), text);
  const b = badges.find((x) => x.from <= at + 1e-9 && at < x.to - 1e-9);
  await stage.evaluate((t) => window.badge(t), b ? b.text : null);
  const next = i + 1 < bounds.length ? bounds[i + 1] : at + FINAL;
  const name = await shot(Math.max(next - at, TICK));
  shown.push(name);
  if (at === enter.v) still1 = name;
}
const onScreen = (v) => shown[Math.max(0, bounds.findLastIndex((b) => b <= v + 1e-9))];
const still2 = frames[frames.length - 1][0];

// ── the browser, opening the address the run printed ────────────────────────
if (BASE && OPU && OPP) {
  console.log('  the browser');
  const dash = await ctx.newPage();
  await dash.setViewportSize({ width: WIDTH, height: clip.height - TITLE });
  const errors = [];
  dash.on('pageerror', (e) => errors.push(e.message));
  const show = async (seconds) => {
    const png = await dash.screenshot();
    await stage.evaluate((src) => new Promise((r) => {
      const img = document.getElementById('page');
      img.onload = () => r();
      img.src = src;
    }), `data:image/png;base64,${png.toString('base64')}`);
    return shot(seconds);
  };
  await stage.evaluate(({ url, height }) => {
    document.getElementById('twin').style.display = 'none';
    document.getElementById('browser').style.display = 'block';
    document.getElementById('url').textContent = url;
    document.getElementById('page').style.height = `${height}px`;
  }, { url: BASE, height: clip.height - TITLE });
  await dash.goto('about:blank');
  await show(0.6);
  await dash.goto(BASE, { waitUntil: 'networkidle' });
  const user = dash.locator('#username');
  await user.waitFor();
  await show(1.4);
  await user.click();
  for (const ch of OPU) {
    await dash.keyboard.type(ch);
    await show(0.12);
  }
  await dash.locator('#password').click();
  await dash.keyboard.insertText(OPP);   // pasted from the printout
  await show(0.9);
  await dash.locator('button[type=submit]').click();
  await dash.waitForURL((u) => !/login/.test(u.pathname), { timeout: 20000 }).catch(() => {});
  await dash.waitForLoadState('networkidle');
  await dash.locator('h1').first().waitFor();
  await dash.waitForTimeout(500);
  await show(4);
  if (errors.length) throw new Error('the dashboard threw:\n  ' + errors.slice(0, 5).join('\n  '));
}
await browser.close();

// ffmpeg's concat list, as scripts/make_demo.mjs writes it: the concat demuxer
// drops the last entry's duration, so the last frame goes in once more.
const list = ['ffconcat version 1.0'];
for (const [name, s] of frames) list.push(`file '${name}'`, `duration ${s.toFixed(4)}`);
list.push(`file '${frames[frames.length - 1][0]}'`, 'duration 0.0333');
writeFileSync(join(OUT, 'frames.txt'), list.join('\n') + '\n');
copyFileSync(join(OUT, still1), join(OUT, 'still-1.png'));
copyFileSync(join(OUT, still2), join(OUT, 'still-2.png'));
const total = frames.reduce((s, [, d]) => s + d, 0);
const real = events[events.length - 1].t;
writeFileSync(join(OUT, 'timeline.json'), JSON.stringify({
  recorded_seconds: real, played_seconds: total, frames: frames.length,
  shortened: badges.map((b) => ({ at: +b.from.toFixed(2), shown: +(b.to - b.from).toFixed(2), real: +b.real.toFixed(1), badge: b.text })),
  markers: events.filter((e) => e.type === 'm').map((e) => ({ at: +e.v.toFixed(3), marker: e.data, frame: onScreen(e.v) })),
}, null, 2) + '\n');
console.log(`  ${frames.length} frames, ${total.toFixed(1)} s played (the run took ${real.toFixed(0)} s)`);
