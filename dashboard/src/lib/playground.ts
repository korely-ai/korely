// What the Playground asks the one search for, and what it shows of the
// answer (pages/PlaygroundPage.tsx): the memories, and the kinds the parts
// of the screen the signed-in account is served add to the search
// (lib/cloud.ts, PartSearch). On an install of one's own, or for an account
// served no part, there are none, and the Playground asks for the memories
// as it always did: no `types`, the same request and the same answer. Until
// 3 Nov 2026, on an account served a part the search reaches, the
// Playground asked the memories alone, so what the part keeps was never
// among its results, while the part's own page found it.
import type { PartSearch, ScreenPart } from "./cloud";
import type { SearchHit } from "./agentsApi";

/** The kinds the served parts add to the search, in their order. */
export function searchesOf(parts: readonly ScreenPart[]): PartSearch[] {
  return parts.flatMap((p) => (p.search ? [p.search] : []));
}

/** The `types` to ask for: none with no part's kind (the memories alone,
 *  the request of before), else the memories and each part's kind. */
export function kindsToAsk(searches: readonly PartSearch[]): string[] | undefined {
  return searches.length ? ["memories", ...searches.map((s) => s.kind)] : undefined;
}

/** The part whose kind of result `hit` is, or none: a memory. */
export function partOf(hit: SearchHit, searches: readonly PartSearch[]): PartSearch | undefined {
  return hit.type === undefined ? undefined : searches.find((s) => s.type === hit.type);
}

/** What the agent of `endUser` would get back: the server already scopes
 *  the search to them, and a memory of another end user that slipped
 *  through is left out, keeping the page's promise honest. A part's result
 *  is kept as the server answered it: the part's walls are the server's to
 *  apply (a result of no end user may be the project's, which an end
 *  user's search finds too). */
export function whatTheAgentGets(
  hits: readonly SearchHit[],
  endUser: string,
  searches: readonly PartSearch[],
): SearchHit[] {
  if (!endUser) return [...hits];
  return hits.filter((h) => partOf(h, searches) !== undefined || h.user_id === endUser);
}

/** What the page says it searched: "memories", or "memories and <each
 *  part's label>". */
export function searchedFor(searches: readonly PartSearch[]): string {
  const all = ["memories", ...searches.map((s) => s.label)];
  return all.length === 1 ? all[0] : `${all.slice(0, -1).join(", ")} and ${all[all.length - 1]}`;
}
