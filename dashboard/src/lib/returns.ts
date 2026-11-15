/**
 * Where a source outside the screen sends a person back with values that
 * must not stay in the address bar (13 Nov 2026). The first such source is
 * an OAuth consent: the source sends the browser back to an address of the
 * screen with a one-time `code` and a `state` in the query (RFC 6749 4.1.2),
 * and RFC 9700 4.2.4 asks that they leak nowhere: not into the history, not
 * into the next page's Referer.
 *
 * A part of the screen declares those addresses, under its own path
 * (lib/cloud.ts, ScreenPart `returns`), and every one of them ends in
 * `/callback` (`ReturnAt`). The parts are a lazy chunk of their own
 * (lib/cloud.ts, `partsChunk`), so the start works in two steps, both
 * before anything renders (main.tsx, `returnFirst`): before the session is
 * read (so a login that an expired session asks for carries no `code` in
 * its `from`, App.tsx RequireAuth), before the router reads the address.
 *
 * 1. Here, in the main chunk, naming no part: an address that ends in
 *    `/callback` may be a source's return (one that sent nothing back too:
 *    the page says so). Its query leaves the address bar at once
 *    (`history.replaceState`: the same history entry, nothing added) and is
 *    kept in this module's memory. Only then is the parts' chunk asked, so
 *    a slow or failed chunk leaves no code in the bar. Any other address,
 *    with a query or not, asks for nothing: a page of the core that reads
 *    its query keeps it. The shape is all this step knows, and it knows it
 *    before the session: any address of that shape, opened by anybody,
 *    asks the parts' chunk.
 * 2. With the parts: on an address a part declares, what was kept is held
 *    for the part's page, and the part's own path takes the place of the
 *    address the source sent. On any other, it is dropped, and the address
 *    without its query goes home as any unknown address does (App.tsx). The
 *    page takes what was sent back once (`takeReturned`); a reload finds it
 *    gone, as the address no longer carries it.
 *
 * The chunk has RETURN_WAIT_MS to come. One that fails, that cannot even be
 * asked, or that does not come in time places nothing: the screen renders,
 * and on the address of the return it says that what the source sent back
 * could not be finished (`lostAt`, App.tsx UnknownAddress), with a way to
 * load the screen again there, which lands on the part's page (step 2, with
 * nothing in the return, so the page says so) once the chunk comes.
 *
 * An address under a part that the part does not declare is no page of the
 * screen: App.tsx routes a part's own path alone.
 */

/** The last part of every address a source sends a person back to. */
const RETURN_END = "callback";

/** How long the start waits for the parts' chunk on a source's return
 *  before it renders the screen without it (milliseconds). */
export const RETURN_WAIT_MS = 15_000;

/** An address under a part's path that a source sends a person back to:
 *  it ends in `/callback`, the one shape the main chunk knows. */
export type ReturnAt = `${string}/${typeof RETURN_END}`;

/** What a source sent back to a part's page. */
export interface Returned {
  /** The part's path. */
  part: string;
  /** Which of its returns. */
  at: string;
  params: URLSearchParams;
}

/** A part and the addresses under it a source sends people back to. */
export interface ReturnsTo {
  path: string;
  returns?: readonly ReturnAt[];
}

/** Time, as the page's timers keep it. */
export interface Clock {
  set(fn: () => void, ms: number): number;
  clear(id: number): void;
}

const PAGE_CLOCK: Clock = {
  set: (fn, ms) => globalThis.setTimeout(fn, ms) as unknown as number,
  clear: (id) => globalThis.clearTimeout(id),
};

/** What step 1 kept of an address that may be a source's return: its path
 *  (without a trailing slash) and its query, until step 2 places it. */
let kept: { path: string; params: URLSearchParams } | null = null;
let held: Returned | null = null;
/** The path of a return whose parts' chunk did not come: nothing placed. */
let lost: string | null = null;

type Where = { pathname: string; search: string };
type History = { state: unknown; replaceState(data: unknown, unused: string, url: string): void };

const withoutSlash = (pathname: string): string =>
  pathname.length > 1 && pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;

/** Step 1, naming no part: an address ending in `/callback` loses its query
 *  from the address bar, kept here. Whether it did. */
function keepReturn(where: Where, history: History): boolean {
  lost = null;
  const path = withoutSlash(where.pathname);
  if (path.split("/").pop() !== RETURN_END) return false;
  kept = { path, params: new URLSearchParams(where.search) };
  history.replaceState(history.state, "", path);
  return true;
}

/** Step 2, with the parts: what step 1 kept, held for the part that
 *  declares its address, whose path the address bar shows; dropped on any
 *  other. */
function placeReturn(history: History, parts: readonly ReturnsTo[]): void {
  const was = kept;
  kept = null;
  if (was === null) return;
  for (const part of parts) {
    for (const at of part.returns ?? []) {
      if (was.path !== `${part.path}/${at}`) continue;
      held = { part: part.path, at, params: was.params };
      history.replaceState(history.state, "", part.path);
      return;
    }
  }
}

/** The parts `load` gives within RETURN_WAIT_MS of `clock`; null when it
 *  throws, fails or does not come in time. */
function partsInTime(load: () => Promise<readonly ReturnsTo[]>, clock: Clock): Promise<readonly ReturnsTo[] | null> {
  return new Promise((done) => {
    const timer = clock.set(() => done(null), RETURN_WAIT_MS);
    new Promise<readonly ReturnsTo[]>((asked) => asked(load()))
      .then(done, () => done(null))
      .finally(() => clock.clear(timer));
  });
}

/** The start of the screen (main.tsx), before anything renders: an address
 *  that may be a source's return loses its query, and only then are the
 *  parts asked (`load`, their chunk; null in a build without them, where no
 *  address is any part's return) to say whose return it is. Any other
 *  address asks nothing. Never fails: a chunk that throws, fails or does
 *  not come within RETURN_WAIT_MS places nothing, and the return is lost
 *  (`lostAt`). */
export async function returnFirst(
  where: Where,
  history: History,
  load: (() => Promise<readonly ReturnsTo[]>) | null,
  clock: Clock = PAGE_CLOCK,
): Promise<void> {
  if (!keepReturn(where, history)) return;
  const parts = load ? await partsInTime(load, clock) : [];
  if (parts === null) {
    lost = kept?.path ?? null;
    kept = null;
    return;
  }
  placeReturn(history, parts);
}

/** `returnFirst` with the parts already at hand: both steps at once. */
export function takeReturns(where: Where, history: History, parts: readonly ReturnsTo[]): void {
  if (keepReturn(where, history)) placeReturn(history, parts);
}

/** What a source sent back to `part`'s page, given once: null after, and on
 *  every other part's page. */
export function takeReturned(part: string): Returned | null {
  if (held?.part !== part) return null;
  const taken = held;
  held = null;
  return taken;
}

/** Whether `pathname` is the address of a return the start could not
 *  finish (the parts' chunk did not come): the screen says so there. */
export function lostAt(pathname: string): boolean {
  return lost !== null && withoutSlash(pathname) === lost;
}
