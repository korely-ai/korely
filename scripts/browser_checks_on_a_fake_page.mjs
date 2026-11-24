// The logic of the browser checks (browser_sign_in.mjs: the way in and the
// visit of the pages), run on a page that answers as the dashboard's gate
// does, with no browser and no package:
//
//   node scripts/browser_checks_on_a_fake_page.mjs
//
// `make dashboard-test` runs it, and so `make test-all`, the release's suite
// and the tag's CI. Before it, only a real browser on a real install ran
// this logic, once a release: a check switched off (the "sent to" of a page
// the gate sends elsewhere) or a branch made always true (the one of an
// install with a writer) passed every test, which read the scripts as text
// (review of 15 Nov 2026). Each case below fails on one of those, or on a
// gate that answers after the first wait.
//
// The page keeps a clock: the gate asks the server when the login is sent
// (a request in flight) and answers `gate` ms later, sending the browser to
// /setup when no model writes and Skip for now was not pressed
// (dashboard/src/App.tsx, RequireWriter). Waits move the clock.

import { signIn, visitPages } from "./browser_sign_in.mjs";

const SKIP_LABEL = "Skip for now: run without a model.";

function fakePage({ configured, skipWorks = true, loginWorks = true, gate = 0,
                    sendsAway = {}, shows = {}, errorsOn = [], errors = [] }) {
  let now = 0;
  let path = "/";
  let skipped = false;
  let answerAt = null;               // when the gate's read is answered
  const handlers = {};
  const fire = (event) => (handlers[event] || []).forEach((fn) => fn({}));
  const gated = () => !configured && !skipped && path !== "/login" && path !== "/setup";
  const answer = () => {
    answerAt = null;
    fire("requestfinished");
    if (gated()) path = "/setup";
  };
  const tick = (ms) => {
    now += ms;
    if (answerAt !== null && now >= answerAt) answer();
  };
  const ask = () => {               // the gate's read of the server
    fire("request");
    answerAt = now + gate;
    if (gate === 0) answer();
  };
  return {
    url: () => "http://x" + path,
    on: (event, fn) => { (handlers[event] = handlers[event] || []).push(fn); },
    goto: async (u) => {
      path = new URL(u).pathname;
      if (sendsAway[path]) path = sendsAway[path];
      else if (gated()) path = "/setup";
      if (errorsOn.includes(path)) errors.push(`[pageerror] on ${path}`);
    },
    fill: async () => {},
    click: async () => {
      if (!loginWorks) return;
      path = "/";
      ask();
    },
    waitForURL: async (pred, { timeout = 30000 } = {}) => {
      for (let waited = 0; ; waited += 100) {
        if (pred(new URL("http://x" + path))) return;
        if (waited >= timeout) throw new Error(`timeout ${timeout}ms`);
        tick(100);
      }
    },
    waitForLoadState: async () => {},
    waitForTimeout: async (ms) => tick(ms),
    evaluate: async (fn) => {
      globalThis.document = { body: { innerText: shows[path] || "a page" } };
      try { return fn(); } finally { delete globalThis.document; }
    },
    getByRole: (_role, { name }) => ({
      click: async () => {
        if (path !== "/setup" || !name.test(SKIP_LABEL)) throw new Error("no such button");
        if (skipWorks) { skipped = true; path = "/"; }
      },
    }),
  };
}

const way_in = [
  // [name, page, WRITER, the way in passes]
  ["no writer: the first screen, then Skip for now", { configured: false }, "none", true],
  ["a writer: no first screen", { configured: true }, "configured", true],
  ["said none, and no first screen came", { configured: true }, "none", false],
  ["said configured, and the first screen came", { configured: false }, "configured", false],
  ["Skip for now leads nowhere", { configured: false, skipWorks: false }, "none", false],
  ["the login fails", { configured: true, loginWorks: false }, "configured", false],
  ["said configured, and a slow gate sends to the first screen", { configured: false, gate: 4300 },
   "configured", false],
  ["no writer, and a slow gate shows the first screen", { configured: false, gate: 4300 }, "none", true],
];

const ROUTES = ["/", "/keys", "/facts"];
const pages = [
  // [name, page, the pages it calls broken]
  ["every page loads", {}, []],
  ["a page the gate sends to /setup", { sendsAway: { "/keys": "/setup" } }, ["/keys"]],
  ["a page sent back to the login", { sendsAway: { "/facts": "/login" } }, ["/facts"]],
  ["a page that shows a missing field", { shows: { "/keys": "created undefined" } }, ["/keys"]],
  ["a page that logs an error", { errorsOn: ["/"] }, ["/"]],
];

let bad = 0;
const say = (ok, line) => { if (!ok) bad++; console.log(`${ok ? "ok  " : "FAIL"} ${line}`); };

for (const [name, opts, writer, want] of way_in) {
  const r = await signIn(fakePage(opts), { base: "http://x", user: "u", pass: "p", writer });
  say(r.ok === want, `${name}: ${JSON.stringify(r.steps.map((s) => [s.name, s.ok]))}`);
}
try {
  await signIn(fakePage({ configured: true }), { base: "http://x", user: "u", pass: "p" });
  say(false, "a run that does not say WRITER is taken");
} catch (e) {
  say(/WRITER/.test(e.message), `a run that does not say WRITER is refused: ${e.message}`);
}
for (const [name, opts, want] of pages) {
  const errors = [];
  const page = fakePage({ configured: true, ...opts, errors });
  const broken = await visitPages(page, { base: "http://x", routes: ROUTES, errors, log: () => {} });
  say(JSON.stringify(broken) === JSON.stringify(want), `${name}: broken ${JSON.stringify(broken)}`);
}
console.log(bad ? `${bad} failed` : "the browser checks' logic holds on a fake page");
process.exit(bad ? 1 : 0);
