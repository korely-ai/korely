/**
 * Drive the dashboard with a real browser and report what it did.
 *
 *   OPERATOR_PASSWORD=<password> bash scripts/acceptance_browser.sh <base-url> <username>
 *
 * That starts the Playwright image and installs the one package this needs,
 * Playwright, from scripts/package-lock.json with `npm ci`, inside the
 * container. Run by hand it is
 *
 *   BASE=<base-url> OPU=<username> OPP=<password> node check_dashboard_in_a_browser.mjs
 *
 * (the three may also be its arguments, where `ps` shows the password)
 * from a directory where `npm ci` has been run on a copy of scripts/package.json
 * and scripts/package-lock.json, never in scripts/ itself: node_modules does not
 * belong in the repository (check_modals_in_a_browser.mjs has the command).
 *
 * The acceptance suite checked that the page is served and that every endpoint
 * it names exists. Neither of those is the same as the page being able to talk
 * to them, and on 2026-09-12 it could not: `vite.config.ts` baked
 * `http://localhost:8000` into the bundle as the API base, so a dashboard
 * served from a customer's machine asked THEIR browser to call their own
 * laptop. Every check was green. The login was broken.
 *
 * curl cannot find that, because curl is not the thing that was wrong. This
 * can, and it found it in one run.
 *
 * Exits non-zero when the login does not end up logged in.
 */
import { chromium } from 'playwright';

// From the environment, as check_modals_in_a_browser.mjs reads them: on the
// command line the password is in `ps` (audit of 17 Oct 2026).
const BASE = process.env.BASE ?? process.argv[2];  // not URL: that shadows the global one
const USER = process.env.OPU ?? process.argv[3];
const PASS = process.env.OPP ?? process.argv[4];

const browser = await chromium.launch();
const page = await browser.newPage();

const errors = [];        // only real page errors: a broken page, not a warning
const http_notok = [];

page.on('pageerror', e => errors.push(`[pageerror] ${e.message}`));
page.on('console', m => {
  if (m.type() === 'error') errors.push(`[console] ${m.text()}`);
});
page.on('requestfailed', r => {
  errors.push(`[requestfailed] ${r.method()} ${r.url()} :: ${r.failure()?.errorText}`);
});
page.on('response', r => {
  const u = r.url();
  if ((u.includes('/admin/') || u.includes('/v1/')) && r.status() >= 400) {
    http_notok.push(`${r.request().method()} ${u.replace(BASE, '')} -> ${r.status()}`);
  }
});

await page.goto(BASE + '/login', { waitUntil: 'networkidle' });
await page.fill('#username', USER);
await page.fill('#password', PASS);
await page.click('button[type=submit]');
await page.waitForTimeout(4000);

const page_url = page.url();
const stored = await page.evaluate(() => ({
  token: (localStorage.getItem('token') || '').slice(0, 16),
  who: localStorage.getItem('korely_operator'),
}));

console.log('--- login ---');
console.log(`  ended on ${page_url}`);
console.log(`  token ${stored.token ? 'yes' : 'NO'}`);
const shown = (await page.locator('[role=alert]').allTextContents()).join(' | ');
if (shown) console.log(`  error shown: ${shown}`);

// Every page in the sidebar, because an endpoint that exists and answers 200
// in a shape the screen does not expect is a blank page with a green test
// behind it. That is exactly what /admin/projects was.
const ROUTES = ['/', '/install', '/projects', '/keys', '/playground', '/graph',
                '/agents', '/memories', '/facts', '/webhooks', '/requests', '/audit',
                '/usage', '/settings'];

console.log('--- the pages ---');
const broken = [];
for (const route of ROUTES) {
  const before = errors.length;
  await page.goto(BASE + route, { waitUntil: 'networkidle' }).catch(() => {});
  await page.waitForTimeout(1200);
  // What a page shows, not only whether it threw. The three defects found by
  // looking at a screenshot today were all silent: an empty column, a switcher
  // with nothing in it, an account with no project. None of them raises, and
  // none of the six hundred tests saw them. These are the shapes that leak
  // onto a page when a field is missing, and they are cheap to look for.
  const leaked = await page.evaluate(() => {
    const t = document.body.innerText || "";
    const bad = [];
    // "from null" and "Invalid Date": a fact's date the API did not have.
    for (const needle of ["undefined", "NaN", "[object Object]", "null null", "from null", "Invalid Date"]) {
      if (t.includes(needle)) bad.push(needle);
    }
    return bad;
  }).catch(() => []);
  if (leaked.length) {
    broken.push(route);
    console.log(`  BROKEN ${route}`);
    console.log(`         the page shows: ${leaked.join(", ")}`);
    continue;
  }

  const mine = errors.slice(before);
  if (mine.length) {
    broken.push(route);
    console.log(`  BROKEN ${route}`);
    mine.slice(0, 2).forEach(e => console.log(`         ${e.slice(0, 160)}`));
  } else {
    console.log(`  ok     ${route}`);
  }
}

if (http_notok.length) {
  console.log('--- answers that are not 2xx ---');
  http_notok.slice(0, 10).forEach(l => console.log('  ' + l));
}

await browser.close();

const loggedIn = !page_url.endsWith('/login') && stored.token.length > 0;
const ok = loggedIn && broken.length === 0;
console.log('');
if (!loggedIn) console.log('the login does NOT succeed.');
else if (broken.length) console.log(`logged in, but ${broken.length} pages break: ${broken.join(', ')}`);
else console.log('logged in, and every page loads without errors.');
process.exit(ok ? 0 : 1);
