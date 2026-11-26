// What lib/returns.ts does with an address a source sends a person back to:
// run in node by scripts/check_the_behaviour.mjs (`npm test`).
import { FakeClock, fakeBrowser, settle, type Check } from "./behaviour";
import { cloud, partsChunk } from "./cloud";
import { RETURN_WAIT_MS, lostAt, returnFirst, takeReturned, takeReturns, type ReturnsTo } from "./returns";
import { unknownAddress } from "./unknownAddress";

const PARTS: readonly ReturnsTo[] = [
  { path: "/sources", returns: ["oauth/callback"] },
  { path: "/files" },
];
const SECRET = "CODESENTINEL";

/** The address bar of a page opened at `pathname` + `search`: the address
 *  it shows now (each replaceState puts its own in place of it), and each
 *  one put. */
function bar(pathname: string, search: string) {
  const b = {
    where: { pathname, search },
    now: pathname + search,
    replaced: [] as string[],
    history: {
      state: null as unknown,
      replaceState(data: unknown, _unused: string, url: string) {
        b.history.state = data;
        b.now = url;
        b.replaced.push(url);
      },
    },
  };
  return b;
}

function at(pathname: string, search: string) {
  const b = fakeBrowser();
  takeReturns({ pathname, search }, b.history, PARTS);
  return b;
}

export const CHECKS: Record<string, Check> = {
  "an address a part declares leaves the address bar, its values held once": (t) => {
    const b = at("/sources/oauth/callback", `?code=${SECRET}&state=S1&scope=x`);
    t.deepEqual(b.scan(SECRET), [], "the code is nowhere the page keeps");
    t.equal(b.history.state, null, "the history entry keeps the state it had");
    const r = takeReturned("/sources");
    t.ok(r, "held for the part's page");
    t.equal(r?.params.get("code"), SECRET);
    t.equal(r?.params.get("state"), "S1");
    t.equal(takeReturned("/sources"), null, "given once");
  },
  "the address is the part's own path after, in the same history entry": (t) => {
    const replaced: string[] = [];
    const history = { state: { from: 1 }, replaceState: (_d: unknown, _u: string, url: string) => void replaced.push(url) };
    takeReturns({ pathname: "/sources/oauth/callback/", search: `?code=${SECRET}&state=S` }, history, PARTS);
    t.deepEqual(replaced, ["/sources/oauth/callback", "/sources"],
      "the query out first, then the part's path (a trailing slash taken as the same address)");
    t.ok(takeReturned("/sources"));
  },
  "an address under a part that it does not declare is not taken": (t) => {
    for (const path of ["/sources/oauth/callback/more", "/sources/oauth", "/sources/other/callback", "/files/oauth/callback"]) {
      const b = at(path, `?code=${SECRET}&state=S`);
      t.deepEqual(b.scan(SECRET), [], `${path}: nothing written`);
      t.equal(takeReturned("/sources"), null, `${path}: nothing held`);
      t.equal(takeReturned("/files"), null, `${path}: nothing held for another part`);
    }
  },
  "what is held is for its part alone": (t) => {
    at("/sources/oauth/callback", `?code=${SECRET}&state=S`);
    t.equal(takeReturned("/files"), null, "another part's page gets nothing");
    t.ok(takeReturned("/sources"), "and the part's own still gets it");
  },
  "the main chunk's step: a return's query leaves the bar before the parts are asked": async (t) => {
    const b = bar("/sources/oauth/callback", `?code=${SECRET}&state=S`);
    const seen: string[] = [];
    await returnFirst(b.where, b.history, () => {
      seen.push(b.now);
      return Promise.resolve(PARTS);
    });
    t.deepEqual(seen, ["/sources/oauth/callback"], "asked once, with the code already out of the bar");
    t.equal(b.now, "/sources", "then the part's path");
    t.equal(takeReturned("/sources")?.params.get("code"), SECRET, "then held for the part");
  },
  "the start asks this build's parts' chunk, and nothing in a build without it": async (t) => {
    if (cloud === null) {
      t.equal(partsChunk, null, "no Cloud's pieces: no chunk to ask");
      return;
    }
    t.ok(partsChunk, "the Cloud's pieces: their chunk");
    const asked = await partsChunk!();
    t.ok(asked.length > 0, "the parts themselves");
    t.deepEqual(asked, await cloud.loadParts(), "the very parts the build carries");
  },
  "an address that is no return asks for no part, and keeps its query": async (t) => {
    for (const [pathname, search] of [["/memories", `?project=${SECRET}`], ["/login", ""], ["/login", `?from=${SECRET}`],
                                       ["/sources/callback/more", `?code=${SECRET}`], ["/sources/oauthcallback", `?code=${SECRET}`]]) {
      const b = bar(pathname, search);
      let asked = 0;
      await returnFirst(b.where, b.history, () => {
        asked += 1;
        return Promise.resolve(PARTS);
      });
      t.equal(asked, 0, `${pathname}${search}: the parts' chunk is not asked`);
      t.deepEqual(b.replaced, [], `${pathname}${search}: the address is left as it is`);
      t.equal(takeReturned("/sources"), null, `${pathname}: nothing held`);
      t.equal(lostAt(pathname), false, `${pathname}: nothing lost`);
    }
  },
  "a parts' chunk that fails leaves no code in the bar, nothing held, and says the return was lost": async (t) => {
    const failing: [string, () => Promise<readonly ReturnsTo[]>][] = [
      ["fails", () => Promise.reject(new Error("offline"))],
      ["throws at once", () => {
        throw new Error("no chunk");
      }],
    ];
    for (const [why, load] of failing) {
      const b = bar("/sources/oauth/callback/", `?code=${SECRET}&state=S`);
      let settled = "no";
      await returnFirst(b.where, b.history, load).then(() => (settled = "resolved"), () => (settled = "rejected"));
      t.equal(settled, "resolved", `${why}: the start never fails`);
      t.equal(b.now, "/sources/oauth/callback", `${why}: the code is out of the bar`);
      t.equal(takeReturned("/sources"), null, `${why}: nothing held`);
      t.ok(lostAt("/sources/oauth/callback") && lostAt("/sources/oauth/callback/"), `${why}: lost, there`);
      t.equal(lostAt("/sources"), false, `${why}: and nowhere else`);
      t.equal(unknownAddress(true, lostAt(b.now)), "lost", `${why}: the screen says so there`);
    }
    takeReturns({ pathname: "/", search: "" }, bar("/", "").history, PARTS);
    t.equal(lostAt("/sources/oauth/callback"), false, "a start after forgets it");
  },
  "a build without parts takes the query out of the bar too, and loses nothing": async (t) => {
    const b = bar("/sources/oauth/callback", `?code=${SECRET}`);
    await returnFirst(b.where, b.history, null);
    t.deepEqual(b.replaced, ["/sources/oauth/callback"], "the code is out of the bar");
    t.equal(takeReturned("/sources"), null, "nothing held");
    t.equal(lostAt("/sources/oauth/callback"), false, "no part's return: the address goes home");
  },
  "a parts' chunk that does not come in time: the screen renders without it": async (t) => {
    const clock = new FakeClock();
    const b = bar("/sources/oauth/callback", `?code=${SECRET}&state=S`);
    let late: (parts: readonly ReturnsTo[]) => void = () => {};
    let done = false;
    const started = returnFirst(b.where, b.history, () => new Promise((give) => (late = give)), clock).then(() => {
      done = true;
    });
    await settle();
    clock.advance(RETURN_WAIT_MS - 1);
    await settle();
    t.equal(done, false, "it waits for the chunk until the time is up");
    t.equal(b.now, "/sources/oauth/callback", "with the code already out of the bar");
    clock.advance(1);
    await started;
    t.ok(lostAt("/sources/oauth/callback"), "then renders, and the return is lost");
    late(PARTS);
    await settle();
    t.equal(takeReturned("/sources"), null, "a chunk that comes after places nothing");
    t.equal(b.now, "/sources/oauth/callback", "and moves no address");
    takeReturns({ pathname: "/", search: "" }, bar("/", "").history, PARTS);
  },
  "a parts' chunk in time leaves no timer behind": async (t) => {
    const clock = new FakeClock();
    const b = bar("/sources/oauth/callback", `?code=${SECRET}`);
    await returnFirst(b.where, b.history, () => Promise.resolve(PARTS), clock);
    t.equal(clock.pending, 0, "the time limit is let go");
    t.equal(lostAt("/sources/oauth/callback"), false, "nothing lost");
    t.ok(takeReturned("/sources"), "held for the part");
  },
  "a return that sent nothing back is taken too, for the page to say so": async (t) => {
    const b = fakeBrowser();
    await returnFirst({ pathname: "/sources/oauth/callback", search: "" }, b.history, () => Promise.resolve(PARTS));
    const r = takeReturned("/sources");
    t.ok(r, "held for the part's page");
    t.equal(r?.params.toString(), "", "with nothing in it");
  },
};
