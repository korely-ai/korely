/**
 * The README's demo, recorded from a real install: frames of the dashboard
 * telling one story, which scripts/make_demo.sh turns into docs/assets/demo.gif
 * and docs/assets/demo.mp4.
 *
 *   bash scripts/make_demo.sh
 *
 * does everything (install, write, record, convert, clean up). Run by hand,
 * against an install that is already up, it is
 *
 *   BASE=http://127.0.0.1:8124 OPU=<login> OPP=<password> KEY=<api key> OUT=<dir> \
 *     node make_demo.mjs
 *
 * from a directory where `npm ci` has been run on a copy of scripts/package.json
 * and scripts/package-lock.json (never in scripts/ itself).
 *
 * The story is the README's: customer c4821 is on the Priority Support plan
 * from 15 March 2026 and moves to the Basic plan on 20 June 2026. It is
 * written through /v1 with no model at all (POST /v1/facts, then
 * POST /v1/facts/{id}/forget with the date it stopped being true), so a
 * re-render costs nothing and gives the same picture every time. The two
 * memories are written too, with their timestamps, so the install is the one
 * the README describes; the vectors are local.
 *
 * Nothing on screen is staged: the dashboard is the one the install serves,
 * with its own data. What this adds is outside the app's state: a caption
 * band, a drawn cursor (headless Chromium has none) and the crop, which keeps
 * the sidebar and the header, where the operator's login is, out of frame.
 * The slider is moved the way a hand moves it, one step at a time through its
 * own values, and every date in a caption is read from the screen.
 *
 * Writes OUT/f0000.png..., OUT/frames.txt (the ffmpeg concat list, with each
 * frame's duration) and OUT/still-1.png..still-3.png, the three key moments.
 */
import { chromium } from 'playwright';
import { mkdirSync, writeFileSync, copyFileSync } from 'node:fs';
import { join } from 'node:path';

const BASE = process.env.BASE;
const OPU = process.env.OPU;
const OPP = process.env.OPP;
const KEY = process.env.KEY;
const OUT = process.env.OUT ?? '/out';
const DPR = Number(process.env.DPR ?? 2);
if (!BASE || !OPU || !OPP || !KEY) {
  console.error('make_demo.mjs: BASE, OPU, OPP and KEY are needed (see the header)');
  process.exit(2);
}

const USER = 'c4821';
const OLD = 'Priority Support plan';
const NEW = 'Basic plan';
const TARGET_DAY = '2026-04-01';

// The browser is 1120 wide so the dashboard lays out as on a laptop (the graph
// and its side panel side by side from 1024); the frame is the 880 to the
// right of the 240 px sidebar, below the 56 px header: README width at 1:1.
const VIEW = { width: 1120, height: 760 };
const SIDEBAR = 240;
const FRAME = { width: 880, height: 520 };
const BAND = 64;          // the caption band, at the bottom of the frame
const FPS = 15;           // motion frames; holds are one frame with a duration

mkdirSync(OUT, { recursive: true });

// ── the story, through /v1 ──────────────────────────────────────────────────
async function writeTheStory(request) {
  const h = { Authorization: `Bearer ${KEY}` };
  const call = async (method, path, data) => {
    const r = await request.fetch(BASE + path, { method, headers: h, data });
    if (!r.ok()) throw new Error(`${method} ${path} -> ${r.status()} ${await r.text()}`);
    return r.json();
  };
  const have = await call('GET', `/v1/facts?user_id=${USER}&include_invalidated=true`);
  if (have.total > 0) {
    console.log(`  ${USER} already has ${have.total} facts: nothing written`);
    return;
  }
  await call('POST', '/v1/memories', {
    content: 'Customer 4821 is on the Priority Support plan.', user_id: USER, timestamp: '2026-03-15',
  });
  await call('POST', '/v1/memories', {
    content: 'Customer 4821 moved to the Basic plan.', user_id: USER, timestamp: '2026-06-20',
  });
  const old = await call('POST', '/v1/facts', {
    user_id: USER, subject: USER, predicate: 'subscribes_to', object: OLD, valid_from: '2026-03-15',
  });
  await call('POST', `/v1/facts/${old.id}/forget`, { at: '2026-06-20' });
  await call('POST', '/v1/facts', {
    user_id: USER, subject: USER, predicate: 'subscribes_to', object: NEW, valid_from: '2026-06-20',
  });
  // The store's answer, checked before anything is filmed: the demo shows
  // what the API says, so the API is asked first.
  const april = await call('GET', `/v1/facts?user_id=${USER}&as_of=${TARGET_DAY}`);
  const objs = april.facts.map((f) => f.object);
  if (objs.length !== 1 || objs[0] !== OLD) {
    throw new Error(`as_of=${TARGET_DAY} answers ${JSON.stringify(objs)}, not ["${OLD}"]`);
  }
  console.log(`  wrote 2 memories and 2 facts; as_of=${TARGET_DAY} answers "${OLD}"`);
}

// ── the browser ─────────────────────────────────────────────────────────────
const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: VIEW, deviceScaleFactor: DPR, colorScheme: 'light', reducedMotion: 'no-preference',
});
await writeTheStory(ctx.request);

// Signed in through the API, so the login screen and its password are never
// on screen; the light theme, and past the setup screen an install with no
// model key opens on ("Skip for now", remembered in this browser).
const session = await ctx.request.post(BASE + '/admin/session', { data: { username: OPU, password: OPP } });
if (!session.ok()) throw new Error(`login -> ${session.status()}`);
const { token } = await session.json();
await ctx.addInitScript((t) => {
  localStorage.setItem('token', t);
  localStorage.setItem('korely_agent_theme', 'light');
  localStorage.setItem('korely.setup.skipped', '1');
}, token);

const page = await ctx.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));

// The caption band and the cursor: drawn over the page, never part of it.
async function decorate() {
  await page.evaluate(({ band }) => {
    if (document.getElementById('demo-cursor')) return;
    const c = document.createElement('div');
    c.id = 'demo-cursor';
    c.innerHTML = `<svg width="22" height="28" viewBox="0 0 22 28" xmlns="http://www.w3.org/2000/svg">
      <path d="M2 2 L2 22 L7.5 17 L11 25.5 L14.5 24 L11 15.8 L18.5 15.8 Z"
            fill="#111827" stroke="#ffffff" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
    Object.assign(c.style, {
      position: 'fixed', left: '0px', top: '0px', zIndex: 2147483647, pointerEvents: 'none',
      transformOrigin: '2px 2px', transition: 'none', filter: 'drop-shadow(0 1px 1.5px rgba(0,0,0,.25))',
    });
    document.body.appendChild(c);
    const b = document.createElement('div');
    b.id = 'demo-band';
    Object.assign(b.style, {
      position: 'fixed', height: band + 'px', zIndex: 2147483646, pointerEvents: 'none',
      display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '0 28px',
      background: '#0f172a', color: '#f8fafc', boxSizing: 'border-box',
      font: '600 20px/1.3 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif',
      letterSpacing: '-0.01em', textAlign: 'center',
    });
    document.body.appendChild(b);
  }, { band: BAND });
}

let clip = null;          // the frame, in page coordinates, for this scene
let cursor = { x: 0, y: 0, down: false };
const frames = [];

async function frameAt(top) {
  clip = { x: SIDEBAR, y: top, width: FRAME.width, height: FRAME.height };
  await page.evaluate(({ clip, band }) => {
    const b = document.getElementById('demo-band');
    Object.assign(b.style, {
      left: clip.x + 'px', top: clip.y + clip.height - band + 'px', width: clip.width + 'px',
    });
  }, { clip, band: BAND });
}

async function caption(text) {
  await page.evaluate((t) => { document.getElementById('demo-band').textContent = t; }, text);
}

async function drawCursor() {
  await page.evaluate(({ x, y, down }) => {
    const c = document.getElementById('demo-cursor');
    c.style.transform = `translate(${x - 2}px, ${y - 2}px) scale(${down ? 0.86 : 1})`;
  }, cursor);
}

async function shot(ms) {
  const name = `f${String(frames.length).padStart(4, '0')}.png`;
  await page.screenshot({ path: join(OUT, name), clip, animations: 'allow' });
  frames.push([name, ms]);
  return name;
}

const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2);

async function moveTo(x, y, ms = 650) {
  const from = { ...cursor };
  const n = Math.max(2, Math.round((ms / 1000) * FPS));
  for (let i = 1; i <= n; i++) {
    const k = ease(i / n);
    cursor.x = from.x + (x - from.x) * k;
    cursor.y = from.y + (y - from.y) * k;
    await drawCursor();
    await shot(1000 / FPS);
  }
}

// A real click where the drawn cursor is, then the real mouse goes back off
// frame so no hover state stays behind it.
async function click() {
  cursor.down = true;
  await drawCursor();
  await shot(1000 / FPS);
  await page.mouse.click(cursor.x, cursor.y);
  cursor.down = false;
  await drawCursor();
  await page.mouse.move(5, VIEW.height - 5);
}

async function centre(locator) {
  const b = await locator.boundingBox();
  if (!b) throw new Error(`not on screen: ${locator}`);
  return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
}

async function settle(ms = 600) {
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(ms);
}

// ── scene 1: the Facts page ─────────────────────────────────────────────────
console.log('  recording');
await page.goto(BASE + '/facts', { waitUntil: 'networkidle' });
await page.getByText('Show history (closed facts)').waitFor();
await settle(400);
await decorate();
await frameAt(80);
cursor = { x: SIDEBAR + 640, y: 470, down: false };
await drawCursor();
await page.mouse.move(5, VIEW.height - 5);

await caption('Customer c4821 moved from Priority Support to Basic on 20 June 2026.');
await shot(2600);

const filter = page.getByLabel('End user');
let p = await centre(filter);
await moveTo(p.x - 40, p.y + 4);
await click();
for (const ch of USER) {
  await page.keyboard.type(ch);
  await page.waitForTimeout(90);
  await shot(110);
}
await page.getByText(NEW, { exact: true }).waitFor();
await settle(300);
await caption('Today one fact is open: the Basic plan.');
await shot(2200);

p = await centre(page.getByRole('checkbox'));
await moveTo(p.x, p.y);
await click();
await page.getByText(OLD, { exact: true }).waitFor();
await settle(300);
// The old fact's own badge says when it ended: the caption reads it there.
const badge = page.locator('li', { hasText: OLD }).getByText(/^ended \d{4}-\d{2}-\d{2}/);
const ended = (await badge.innerText()).match(/\d{4}-\d{2}-\d{2}/)[0];
if (ended !== '2026-06-20') throw new Error(`the Facts page says the old plan ended ${ended}`);
const bb = await badge.boundingBox();
await moveTo(bb.x + 34, bb.y + bb.height + 14, 500);
await caption(`The old plan ended on ${ended}: closed, not overwritten.`);
const still1 = await shot(3200);

// ── scene 2: the graph, and its clock ───────────────────────────────────────
await page.goto(BASE + `/graph?user_id=${USER}&history=1`, { waitUntil: 'networkidle' });
const slider = page.locator('input[type=range]');
await slider.waitFor();
await settle(3200);       // the layout settles in about two seconds, then fits the view
await decorate();
await frameAt(240);
await caption('The same facts on the Memory Graph, which has a clock.');
cursor = { x: SIDEBAR + 600, y: 640, down: false };
await drawCursor();
await page.mouse.move(5, VIEW.height - 5);
await shot(1800);

// The customer's node: the circle of the node its label belongs to.
const label = page.locator('svg text', { hasText: new RegExp(`^${USER}$`) });
const node = await centre(label.locator('xpath=..').locator('circle').last());
await moveTo(node.x, node.y);
await click();
await page.getByText(/Facts \(\d+\)/).waitFor();
await settle(400);
await moveTo(cursor.x + 40, cursor.y + 90, 450);
await caption('Today: Basic plan. Priority Support is closed.');
const still2 = await shot(2800);

// The slider, through its own values. It snaps to its `step` from its `min`;
// the days asked for are the values whose date reads that day, or the
// nearest, and the caption says the date the screen shows.
const range = await slider.evaluate((el) => ({
  min: Number(el.min), max: Number(el.max), step: Number(el.step), value: Number(el.value),
}));
const dayOf = (ms) => new Date(ms).toISOString().slice(0, 10);
const valueFor = (day) => {
  const want = Date.parse(`${day}T00:00:00Z`);
  const k = Math.round((want - range.min) / range.step);
  const ks = [k - 1, k, k + 1].filter((i) => i >= 0);
  const exact = ks.map((i) => range.min + i * range.step).find((v) => dayOf(v) === day);
  return exact ?? Math.min(range.max, range.min + Math.max(0, k) * range.step);
};
const thumbAt = async (v) => {
  const b = await slider.boundingBox();
  const t = 16;           // Chromium's range thumb
  return { x: b.x + t / 2 + ((v - range.min) / (range.max - range.min)) * (b.width - t), y: b.y + b.height / 2 };
};
const setValue = (v) => slider.evaluate((el, v) => {
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  set.call(el, String(v));
  el.dispatchEvent(new Event('input', { bubbles: true }));
  return Number(el.value);
}, v);
const shownDay = () => page.locator('span.font-mono.tabular-nums').first().innerText();
const pretty = (day) => new Date(`${day}T00:00:00Z`).toLocaleDateString('en-GB', {
  day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC',
});

let value = range.value;
p = await thumbAt(value);
await moveTo(p.x, p.y);
cursor.down = true;
await drawCursor();
await caption('Put the clock back…');
await shot(1000 / FPS);

async function dragTo(target, ms) {
  const from = value;
  const n = Math.max(2, Math.round((ms / 1000) * FPS));
  for (let i = 1; i <= n; i++) {
    const want = from + (target - from) * ease(i / n);
    const k = Math.round((want - range.min) / range.step);
    value = await setValue(i === n ? target : range.min + k * range.step);
    const q = await thumbAt(value);
    cursor.x = q.x; cursor.y = q.y;
    await drawCursor();
    await page.waitForTimeout(25);
    await shot(1000 / FPS);
  }
}

const june20 = valueFor('2026-06-20');
const june19 = valueFor('2026-06-19');
await dragTo(june20, 1700);
await caption(`${pretty(await shownDay())}: Basic plan begins, Priority Support ends.`);
await shot(1700);
await dragTo(june19, 140);
await settle(250);
await caption(`${pretty(await shownDay())}: still Priority Support.`);
await shot(1900);
await dragTo(valueFor(TARGET_DAY), 1100);
cursor.down = false;
await drawCursor();
await settle(1200);       // the graph has redrawn with one fact
const day = await shownDay();
if (day !== TARGET_DAY) console.log(`  the slider cannot show ${TARGET_DAY} today: it shows ${day}`);
await moveTo(cursor.x + 30, cursor.y + 110, 500);

// What the side panel says on that day is what the caption says.
const panel = page.locator('aside').last();
const said = await panel.innerText();
if (!said.includes(OLD) || said.includes(NEW) || !said.includes('from 2026-03-15')) {
  throw new Error(`the panel on ${day} does not read "${OLD}" alone, from 2026-03-15:\n${said}`);
}
await caption(`As of ${pretty(day)}, the store answers: Priority Support plan.`);
const still3 = await shot(4800);

await browser.close();

// ffmpeg's concat list: each frame and how long it stays. The concat demuxer
// drops the last entry's duration, which would cut the final hold: the last
// frame goes in once more, for one frame's time.
const lines = ['ffconcat version 1.0'];
for (const [name, ms] of frames) lines.push(`file '${name}'`, `duration ${(ms / 1000).toFixed(4)}`);
lines.push(`file '${frames[frames.length - 1][0]}'`, 'duration 0.0333');
writeFileSync(join(OUT, 'frames.txt'), lines.join('\n') + '\n');
[still1, still2, still3].forEach((f, i) => copyFileSync(join(OUT, f), join(OUT, `still-${i + 1}.png`)));

const total = frames.reduce((s, [, ms]) => s + ms, 0);
console.log(`  ${frames.length} frames, ${(total / 1000).toFixed(1)} s`);
if (errors.length) {
  console.error('the page threw:\n  ' + errors.slice(0, 5).join('\n  '));
  process.exit(1);
}
