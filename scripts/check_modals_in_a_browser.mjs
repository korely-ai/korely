// The modals, measured in a real browser.
//
//   docker run --rm --network host -v "$PWD/scripts":/src:ro -w /tmp \
//     -e BASE=http://127.0.0.1:8123 -e OPU=operator -e OPP=... \
//     mcr.microsoft.com/playwright:v1.56.0-noble \
//     sh -c 'cp /src/package*.json /src/check_modals_in_a_browser.mjs . && npm ci --silent && node check_modals_in_a_browser.mjs'
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

const BASE = process.env.BASE, U = process.env.OPU, P = process.env.OPP;
const b = await chromium.launch();
const page = await b.newPage();
const outcomes = [];
const say = (name, ok, detail = "") =>
  outcomes.push({ name, ok, detail });

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
await page.goto(BASE + "/login", { waitUntil: "networkidle" });
await page.fill("#username", U);
await page.fill("#password", P);
await page.click('button[type="submit"]');
await page.waitForLoadState("networkidle");
await page.waitForTimeout(800);
console.log("after login we are on:", page.url());
console.log("message on screen:", (await page.evaluate(() => document.body.innerText)).slice(0, 200).replace(/\n/g, " | "));

await page.goto(BASE + "/keys", { waitUntil: "networkidle" });
await page.waitForTimeout(800);
console.log("on /keys we are on:", page.url());
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
} catch (e) { say("the field can be typed into", false, String(e).slice(0, 80)); }
try {
  await page.click('[role="dialog"] button[aria-label="Close"]', { timeout: 4000 });
  await page.waitForTimeout(300);
  const closed = await page.evaluate(() => !document.querySelector('[role="dialog"]'));
  say("it closes with the X", closed);
} catch (e) { say("it closes with the X", false, String(e).slice(0, 80)); }

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

await b.close();
let broken = 0;
for (const e of outcomes) {
  console.log(`  ${e.ok ? "ok  " : "FAIL"} ${e.name}${e.ok ? "" : "   " + e.detail}`);
  if (!e.ok) broken++;
}
console.log(`\n${outcomes.length - broken} passed, ${broken} failed`);
process.exit(broken ? 1 : 0);
