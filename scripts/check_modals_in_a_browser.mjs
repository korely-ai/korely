// The modals, measured in a real browser.
//
//   docker run --rm --network host -v "$PWD/scripts":/src:ro -w /tmp \
//     -e BASE=http://127.0.0.1:8123 -e OPU=operator -e OPP=... -e WRITER=none \
//     -e KORELY_API_KEY=... mcr.microsoft.com/playwright:v1.56.0-noble \
//     sh -c 'cp /src/package*.json /src/check_modals_in_a_browser.mjs /src/browser_sign_in.mjs . \
//            && npm ci --silent && node check_modals_in_a_browser.mjs'
//
// (scripts/acceptance_browser.sh runs it so, with acceptance.sh's values.)
//
// Playwright is the one package this needs, and it comes from
// scripts/package-lock.json: 1.56.0, the version the image's browsers are
// built for, installed by `npm ci` inside the container and thrown away with
// it. scripts/ is mounted read-only on purpose. The command that used to be
// here mounted it writable and ran `npm i` in it, and that is how 513 files of
// scripts/node_modules got committed and shipped inside the product image.
//
// It exists because of a repair that broke what it repaired.
//
// A tester had found that the page behind a modal stayed operable through the
// overlay, and aiming at a menu inside the dialog had hit one behind it,
// really moving a live API key from one project to another. The repair was to
// put `inert` on the application root. Except the dialog was inside the root,
// so it froze too: the form drew perfectly and could be neither filled in, nor
// confirmed, nor closed. Not even with the X. The only way out was Escape,
// which nobody guesses.
//
// That version compiled, passed the type check and passed the suite. The only
// way to notice was to open the dialog and try to use it.
//
// So this does not look at the DOM to see whether the attributes are in the
// right place: it measures what the browser does. `elementFromPoint` over a
// field, focus that sticks or does not, a real `fill` and a real `click`. They
// are truths of the browser, not of the specification.

import { chromium } from "playwright";
import { signIn } from "./browser_sign_in.mjs";

const BASE = process.env.BASE, U = process.env.OPU, P = process.env.OPP;
// The install's API key, for the one write this check makes (the drawers').
const KEY = process.env.KORELY_API_KEY;
// What the install has, for the first screen (browser_sign_in.mjs).
const WRITER = process.env.WRITER;
const b = await chromium.launch();
// A laptop's window, written out rather than left to Playwright's default
// (the same 1280 x 720): the dialogs are measured against it, and a dialog
// taller than it lost its X off the top (1.0.27 to 1.0.31).
const page = await b.newPage({ viewport: { width: 1280, height: 720 } });
const outcomes = [];
const say = (name, ok, detail = "") =>
  outcomes.push({ name, ok, detail });
// Why Playwright gave up: the error's first line, then each step of its call
// log once, in order. The detail used to be the error's first 80 characters,
// which ended at "waiting for loc", and for five releases the log never said
// "element is outside of the viewport", which was the whole answer.
const why = (e) => {
  const steps = String(e?.message ?? e).split("\n").map((l) => l.trim().replace(/^- /, ""));
  return [...new Set(steps.filter(Boolean))].join(" / ").slice(0, 1500);
};
// Every outcome, the count, and the exit status: at the end, or as soon as
// the way in fails, since nothing behind it can be reached.
async function verdict() {
  await b.close();
  let broken = 0;
  for (const e of outcomes) {
    console.log(`  ${e.ok ? "ok  " : "FAIL"} ${e.name}${e.ok ? "" : "   " + e.detail}`);
    if (!e.ok) broken++;
  }
  console.log(`\n${outcomes.length - broken} passed, ${broken} failed`);
  process.exit(broken ? 1 : 0);
}

// The sign-in's status, and the refusal's words when it refused: never the
// body of a success, which is the live session token (it went to stdout, and
// acceptance.sh kept stdout in /tmp/korely-browser.txt; functional test of 28
// Oct 2026).
page.on("response", async (r) => {
  if (r.url().includes("/admin/session")) {
    let said = "";
    if (!r.ok()) {
      try { said = (await r.text()).slice(0, 160); } catch {}
    }
    console.log("  POST /admin/session ->", r.status(), said);
  }
});
// The login as a person makes it: on an install with no writer, the first
// screen and its Skip for now, or /keys is that screen again.
const way_in = await signIn(page, { base: BASE, user: U, pass: P, writer: WRITER });
for (const s of way_in.steps) say(s.name, s.ok, s.detail);
console.log("after login we are on:", page.url());
if (!way_in.ok) await verdict();
console.log("message on screen:", (await page.evaluate(() => document.body.innerText)).slice(0, 200).replace(/\n/g, " | "));

await page.goto(BASE + "/keys", { waitUntil: "networkidle" });
await page.waitForTimeout(800);
console.log("on /keys we are on:", page.url());
const onKeys = new URL(page.url()).pathname === "/keys";
say("/keys opens the keys page", onKeys, `on ${page.url()}`);
if (!onKeys) await verdict();
const buttons = await page.evaluate(() =>
  [...document.querySelectorAll("button")].map((b) => b.textContent.trim()).filter(Boolean));
console.log("buttons on the page:", JSON.stringify(buttons.slice(0, 10)));
await page.getByRole("button", { name: /create key/i }).first().click();
await page.waitForTimeout(400);

const state = await page.evaluate(() => {
  const dlg = document.querySelector('[role="dialog"]');
  const root = document.getElementById("root");
  const inert = [...document.querySelectorAll("[inert]")].map((e) => e.tagName + "#" + (e.id || ""));
  const field = dlg?.querySelector("input");
  field?.focus();
  const r = field?.getBoundingClientRect();
  const overField = r ? document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2) : null;
  const close = dlg?.querySelector('button[aria-label="Close"]');
  const rc = close?.getBoundingClientRect();
  const pointX = rc ? document.elementFromPoint(rc.x + rc.width / 2, rc.y + rc.height / 2) : null;
  // The centre of the X lands on the icon's `path`, which is INSIDE the
  // button: that is right, the click bubbles up. What matters is that under the
  // pointer there is the button and not the BODY, which was the symptom of the
  // inert modal.
  const overX = pointX?.closest?.('button[aria-label="Close"]') ? "BUTTON" : (pointX?.tagName ?? null);
  return {
    dialogExists: !!dlg,
    dialogOutsideRoot: !!dlg && !root?.contains(dlg),
    inert,
    rootInert: root?.hasAttribute("inert") ?? false,
    focusInsideDialog: !!dlg && dlg.contains(document.activeElement),
    elementOverField: overField?.tagName ?? null,
    // Already a tag name: the old `overX?.tagName` read a property of a
    // string and was always null.
    elementOverX: overX,
    rowMenusReachable:
      [...document.querySelectorAll('select[aria-label="Move to project"]')]
        .filter((e) => !e.closest("[inert]")).length,
  };
});

say("the dialog exists", state.dialogExists);
say("and it sits outside the inert root", state.dialogOutsideRoot,
    `inert: ${state.inert.join(",")}`);
say("the page behind is inert", state.rootInert);
say("focus enters the dialog", state.focusInsideDialog);
say("the name field is clickable", state.elementOverField === "INPUT",
    `over the field there is: ${state.elementOverField}`);
// The X is not probed with elementFromPoint: further down there is a real
// click, which is the strong proof. A probe circling the same property only
// adds one more way of being red on its own account.
say("the row menus cannot be reached", state.rowMenusReachable === 0,
    `reachable: ${state.rowMenusReachable}`);

// and the real proof: fill in and close with the mouse
try {
  await page.fill('[role="dialog"] input', "browser-check", { timeout: 4000 });
  say("the field can be typed into", true);
} catch (e) { say("the field can be typed into", false, why(e)); }
try {
  await page.click('[role="dialog"] button[aria-label="Close"]', { timeout: 4000 });
  await page.waitForTimeout(300);
  const closed = await page.evaluate(() => !document.querySelector('[role="dialog"]'));
  say("it closes with the X", closed);
} catch (e) { say("it closes with the X", false, why(e)); }

// and the projects modal, which had been left behind last round
await page.goto(BASE + "/projects", { waitUntil: "networkidle" });
await page.getByRole("button", { name: /new project/i }).first().click();
await page.waitForTimeout(400);
const proj = await page.evaluate(() => {
  const dlg = document.querySelector('[role="dialog"]');
  const root = document.getElementById("root");
  const field = dlg?.querySelector("input");
  field?.focus();
  const r = field?.getBoundingClientRect();
  return {
    isDialog: !!dlg,
    outsideRoot: !!dlg && !root?.contains(dlg),
    rootInert: root?.hasAttribute("inert") ?? false,
    overField: r ? document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)?.tagName : null,
  };
});
say("the projects modal is a dialog too", proj.isDialog);
say("it too sits outside the root", proj.outsideRoot);
say("and makes the page behind inert", proj.rootInert);
say("and its field is clickable", proj.overField === "INPUT", `there is: ${proj.overField}`);

// The drawers below need a request and an agent, and a fresh install may have
// neither. An agent is not made, it is the agent_id a write carries (the
// Agents page says so), so the check writes one memory under an agent_id of
// its own with the install's key, as acceptance.sh's S2 does with two; the
// write is a request on the Requests page too.
const AGENT = "zz-browser-check";
let wrote;
try {
  const r = await fetch(BASE + "/v1/memories", {
    method: "POST",
    headers: { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ content: "The browser check opens this agent's details.", agent_id: AGENT }),
  });
  wrote = r.status;
} catch (e) { wrote = why(e); }
say("a memory is written under the check's own agent_id", wrote === 201, `POST /v1/memories -> ${wrote}`);

// The overlays a person closes with an X, each in a laptop's window and in a
// short one. The two drawers are laid out by their pages (ModalShell), and
// each panel lay under the clear layer that closes it: the X seemed to work
// because the layer closed the drawer, as did a press anywhere in the panel
// (1.0.31, measured 17 Nov 2026). A real click refuses a target that another
// element covers, so it is what tells. The short window is where a panel
// taller than the window loses its X or its foot, and where what is under
// the title scrolls: the X has to stay where it was.
const OVERLAYS = [
  { name: "the Create API key dialog", path: "/keys",
    open: () => page.getByRole("button", { name: /create key/i }).first().click({ timeout: 4000 }) },
  { name: "the request drawer", path: "/requests",
    open: () => page.locator("table tbody tr").first().click({ timeout: 4000 }) },
  { name: "the agent drawer", path: "/agents",
    open: () => page.locator("li button", { hasText: AGENT }).first().click({ timeout: 4000 }) },
];
const isOpen = () => page.evaluate(() => !!document.querySelector('[role="dialog"]'));
async function open(o) {
  await page.goto(BASE + o.path, { waitUntil: "networkidle" });
  await o.open();
  await page.waitForSelector('[role="dialog"]', { timeout: 4000 });
  await page.waitForTimeout(500);
}
// The open overlay's panel and its X. A Modal is its own panel; a ModalShell
// is the whole overlay, fixed over the window, and its panel is the child
// that holds the X.
const measure = () => page.evaluate(() => {
  const dlg = document.querySelector('[role="dialog"]');
  const x = dlg.querySelector('button[aria-label="Close"]');
  const box = getComputedStyle(dlg).position === "fixed"
    ? [...dlg.children].find((c) => c.contains(x)) : dlg;
  const p = box.getBoundingClientRect();
  const r = x.getBoundingClientRect();
  const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
  const there = document.elementFromPoint(cx, cy);
  return {
    top: Math.round(p.top), bottom: Math.round(p.bottom), window: innerHeight,
    x: [cx, cy], xOnTop: !!there && x.contains(there),
    there: there ? `${there.tagName} ${there.getAttribute("class") ?? ""}`.slice(0, 80) : "nothing",
  };
});

async function overlayIn(o, at) {
  const named = (what) => `${o.name} ${what} (${at})`;
  try {
    await open(o);
    const m = await measure();
    say(named("fits the window"), m.top >= 0 && m.bottom <= m.window,
        `top ${m.top}, bottom ${m.bottom}, window ${m.window}`);
    // A press on its own words, which nothing answers.
    await page.locator('[role="dialog"] :is(h2, p)').first().click({ timeout: 4000 });
    await page.waitForTimeout(300);
    say(named("stays open on a press inside it"), await isOpen());
  } catch (e) { say(named("stays open on a press inside it"), false, why(e)); }
  try {
    await open(o);
    await page.click('[role="dialog"] button[aria-label="Close"]', { timeout: 4000 });
    await page.waitForTimeout(300);
    say(named("closes with its own X"), !(await isOpen()));
  } catch (e) { say(named("closes with its own X"), false, why(e)); }
  // Scrolled to its end, then a press where the X is drawn, and only after
  // the browser says the X is on top there: Playwright's own click would
  // scroll the X back into view first. Scrolled by setting what a person's
  // wheel sets, each scroll box's scrollTop, because the wheel itself is
  // lost in the lower part of a short headless window, on a bare page too
  // (Chrome 157, 400 x 300: nothing at y 230, 100 px at y 150; 17 Nov 2026).
  try {
    await open(o);
    await page.evaluate(() => {
      const dlg = document.querySelector('[role="dialog"]');
      for (const e of [dlg, ...dlg.querySelectorAll("*")]) {
        if (/auto|scroll/.test(getComputedStyle(e).overflowY)) e.scrollTop = e.scrollHeight;
      }
    });
    await page.waitForTimeout(300);
    const m = await measure();
    if (!m.xOnTop) {
      say(named("keeps its X in sight when it scrolls"), false,
          `at the X's place (${m.x.map(Math.round).join(", ")}) there is ${m.there}`);
    } else {
      await page.mouse.click(...m.x);
      await page.waitForTimeout(300);
      say(named("keeps its X in sight when it scrolls"), !(await isOpen()),
          "a press where the X is drawn did not close it");
    }
  } catch (e) { say(named("keeps its X in sight when it scrolls"), false, why(e)); }
}

for (const [width, height] of [[1280, 720], [400, 300]]) {
  await page.setViewportSize({ width, height });
  for (const o of OVERLAYS) await overlayIn(o, `${width} x ${height}`);
}

await verdict();
