// The way in a person takes, for the two checks in a real browser
// (check_dashboard_in_a_browser.mjs, check_modals_in_a_browser.mjs): the
// login, then the first screen of an install that no model writes for yet;
// and the visit of the pages after it (`visitPages`, below). Nothing here
// imports Playwright: scripts/browser_checks_on_a_fake_page.mjs runs both on
// a page that answers as the dashboard's gate does, in `make dashboard-test`.
//
// Until a writer is configured, the dashboard sends the first login to
// /setup, and every page behind its gate back there, until a model is saved
// or "Skip for now" is pressed (dashboard/src/App.tsx, RequireWriter; the
// skip is kept in the browser, lib/setupSkipped.ts). The checks went to
// /keys straight after the login: on the release's install, which had no
// model key, they landed on /setup, never found "Create key" (the
// acceptance of 1.0.21 to 1.0.23), and the page loop called /setup, met
// fourteen times, fourteen pages that load. A person who installs without
// a key presses Skip for now, the product's own way on; so does this, and
// says it saw the screen. With a writer there is no first screen, and
// meeting one is a failure.
//
// WRITER is what the install has, as acceptance.sh asked the server
// (GET /admin/models): "configured" or "none". Anything else is refused, so
// a run that forgot to say is not taken for either.

const SKIP = /^Skip for now/;

// The page the way in ends on reads the server as it opens, after a move of
// the router that loads no document, so Playwright's "networkidle", a state
// of the document's load, is already there: a check that went on at once
// aborted those reads with its next page.goto, and they were counted
// against that page (net::ERR_ABORTED, 15 Nov 2026). So the requests are
// counted here, and the way in ends when none has been open for half a
// second (Playwright's own measure of idle is 500 ms), ten at most.
function requestsInFlight(page) {
  let open = 0;
  page.on("request", () => { open++; });
  page.on("requestfinished", () => { open--; });
  page.on("requestfailed", () => { open--; });
  return async () => {
    let quiet = 0;
    for (let waited = 0; waited < 10000 && quiet < 500; waited += 100) {
      await page.waitForTimeout(100);
      quiet = open > 0 ? 0 : quiet + 100;
    }
  };
}

export async function signIn(page, { base, user, pass, writer }) {
  if (writer !== "configured" && writer !== "none") {
    throw new Error(`WRITER is ${JSON.stringify(writer)}: say "configured" or "none"`);
  }
  const settled = requestsInFlight(page);
  const steps = [];
  const say = (name, ok, detail = "") => steps.push({ name, ok, detail });
  await page.goto(base + "/login", { waitUntil: "networkidle" });
  await page.fill("#username", user);
  await page.fill("#password", pass);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.endsWith("/login"), { timeout: 10000 }).catch(() => {});
  await page.waitForLoadState("networkidle");
  const after = new URL(page.url()).pathname;
  say("the login leaves the login page", after !== "/login", `on ${after}`);
  if (after === "/login") return { ok: false, steps };

  // The gate asks the server first and shows "Loading..." meanwhile, and
  // sends the browser to /setup only once it has the answer. So the place is
  // read once the reads are over: read before, a gate that answered after
  // the four seconds was taken for no first screen.
  await page.waitForURL((u) => u.pathname === "/setup", { timeout: 4000 }).catch(() => {});
  await settled();
  const onSetup = new URL(page.url()).pathname === "/setup";
  if (writer === "configured") {
    say("an install with a writer shows no first screen", !onSetup, `on ${page.url()}`);
    return { ok: !onSetup, steps };
  }
  say("an install with no writer opens on the first screen", onSetup, `on ${page.url()}`);
  if (!onSetup) return { ok: false, steps };
  try {
    await page.getByRole("button", { name: SKIP }).click({ timeout: 4000 });
    await page.waitForURL((u) => u.pathname !== "/setup", { timeout: 4000 });
    await settled();
    say("Skip for now leads on", true, `to ${new URL(page.url()).pathname}`);
  } catch (e) {
    say("Skip for now leads on", false, String(e).slice(0, 120));
    return { ok: false, steps };
  }
  return { ok: true, steps };
}

// The pages after the way in, one by one, as check_dashboard_in_a_browser.mjs
// visits them: a page is broken when it shows a shape a missing field leaves,
// when the gate sent the browser elsewhere (to /setup, to /login: the screen
// met instead is not the page), or when the page logged an error. `errors`
// is the list the caller's listeners fill. Here, and not in that script, so
// that scripts/browser_checks_on_a_fake_page.mjs runs it without a browser.
export async function visitPages(page, { base, routes, errors, log = console.log }) {
  const broken = [];
  for (const route of routes) {
    const before = errors.length;
    await page.goto(base + route, { waitUntil: "networkidle" }).catch(() => {});
    await page.waitForTimeout(1200);
    // What a page shows, not only whether it threw. The three defects found by
    // looking at a screenshot were all silent: an empty column, a switcher
    // with nothing in it, an account with no project. None of them raises, and
    // none of the six hundred tests of the day saw them. These are the shapes
    // that leak onto a page when a field is missing, and they are cheap to
    // look for.
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
      log(`  BROKEN ${route}`);
      log(`         the page shows: ${leaked.join(", ")}`);
      continue;
    }
    const landed = new URL(page.url()).pathname;
    if (landed !== route) {
      broken.push(route);
      log(`  BROKEN ${route}`);
      log(`         sent to ${landed}`);
      continue;
    }
    const mine = errors.slice(before);
    if (mine.length) {
      broken.push(route);
      log(`  BROKEN ${route}`);
      mine.slice(0, 2).forEach((e) => log(`         ${e.slice(0, 160)}`));
    } else {
      log(`  ok     ${route}`);
    }
  }
  return broken;
}
