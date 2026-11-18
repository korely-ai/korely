// Facts read a page at a time, for the screens that show a total next to a
// list (audit of 17 Oct 2026: Facts listed 100 under "250 facts", and the
// graph drew 100 and replayed a cut history, with nothing saying so).
//
// GET /admin/facts takes `offset` and pages in a stable order (the freshest
// confirmation first, the id last: services/facts/service.py
// retrieve_agent_facts), and `FactsQuery` names it.
import {
  factsApi,
  type Fact,
  type FactsList,
  type FactsQuery,
  type ProjectScope,
} from "@/lib/agentsApi";

/** The most facts the server returns in one page (retrieve_agent_facts). */
export const FACTS_PAGE_MAX = 200;

export function factsPage(
  scope: ProjectScope,
  q: FactsQuery,
  limit: number,
  offset: number,
): Promise<FactsList> {
  return factsApi.list(scope, { ...q, limit, offset });
}

/** What a closed fact is called: "superseded" when a newer fact took its
 *  place (`invalidated_by`), "ended" when it closed on its own, forgotten or
 *  stopped. Every closed fact read "superseded" on the Facts page and in the
 *  graph (audit of 17 Oct 2026). */
export function closedLabel(f: Pick<Fact, "invalidated_by">): string {
  return f.invalidated_by ? "superseded" : "ended";
}

/** A date of the API as the screen writes dates, `2026-03-15` (UTC, as the
 *  graph's slider reads them); null when there is none or it does not parse,
 *  so a fact never reads "from null". */
export function factDay(s: string | null | undefined): string | null {
  const t = s ? Date.parse(s) : NaN;
  return Number.isNaN(t) ? null : new Date(t).toISOString().slice(0, 10);
}

/** What "known on" means, where a fact shows it. */
export const KNOWN_ON =
  "The day Korely learned it had ended: from that date the graph's \"Known then\" clock draws it closed.";

/** The dates a fact shows: since when it is true, when it closed, and, when
 *  Korely learned that end on a later day, the day it did: the date the
 *  graph's "Known then" clock sees it closed. The dashboard showed none of
 *  them, only "ended" or "superseded" (29 Oct 2026). */
export function factDates(f: Pick<Fact, "valid_from" | "invalid_at" | "closed_known_at">): {
  from: string | null;
  ended: string | null;
  knownOn: string | null;
} {
  const ended = factDay(f.invalid_at);
  const known = factDay(f.closed_known_at);
  return { from: factDay(f.valid_from), ended, knownOn: ended && known && known > ended ? known : null };
}
