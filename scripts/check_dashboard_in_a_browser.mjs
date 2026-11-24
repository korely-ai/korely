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
import { signIn, visitPages } from './browser_sign_in.mjs';

// From the environment, as check_modals_in_a_browser.mjs reads them: on the
// command line the password is in `ps` (audit of 17 Oct 2026).
const BASE = process.env.BASE ?? process.argv[2];  // not URL: that shadows the global one
const USER = process.env.OPU ?? process.argv[3];
const PASS = process.env.OPP ?? process.argv[4];
// What the install has, for the first screen (browser_sign_in.mjs).
const WRITER = process.env.WRITER;

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

// The login as a person makes it, the first screen included.
const way_in = await signIn(page, { base: BASE, user: USER, pass: PASS, writer: WRITER });

const page_url = page.url();
const stored = await page.evaluate(() => ({
  token: (localStorage.getItem('token') || '').slice(0, 16),
  who: localStorage.getItem('korely_operator'),
}));

console.log('--- login ---');
for (const s of way_in.steps) console.log(`  ${s.ok ? 'ok  ' : 'FAIL'} ${s.name}${s.detail ? '   ' + s.detail : ''}`);
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
const broken = await visitPages(page, { base: BASE, routes: ROUTES, errors });

if (http_notok.length) {
  console.log('--- answers that are not 2xx ---');
  http_notok.slice(0, 10).forEach(l => console.log('  ' + l));
}

await browser.close();

const loggedIn = way_in.ok && stored.token.length > 0;
const ok = loggedIn && broken.length === 0;
console.log('');
if (!loggedIn) console.log('the login does NOT succeed as a person makes it.');
else if (broken.length) console.log(`logged in, but ${broken.length} pages break: ${broken.join(', ')}`);
else console.log('logged in, and every page loads without errors.');
process.exit(ok ? 0 : 1);
