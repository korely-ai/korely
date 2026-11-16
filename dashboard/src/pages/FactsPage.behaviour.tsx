// What the Facts page and the Memory Graph draw for their read (FactsPage.tsx,
// FactsShown; components/GraphShown.tsx): run in node by
// scripts/check_the_behaviour.mjs (`npm test`), drawn with renderToString.
// Pre-tester check of 15 Nov 2026: a read that failed drew "0 facts" and "No
// facts yet", and "No graph yet ... Add a memory or two", under the error, as
// if the account were empty.
import type { ReactElement } from "react";
import { renderToString } from "react-dom/server";
import type { Check } from "@/lib/behaviour";
import type { Load } from "@/lib/useLoad";
import { FactsShown } from "@/pages/FactsPage";
import { GraphShown } from "@/components/GraphShown";

const SAID = "The database did not answer in time: try again in a moment.";
const LIST = "<ul>the facts</ul>";
const load = (over: Partial<Load<unknown>>): Load<unknown> => ({
  data: null,
  error: null,
  loading: false,
  reload: () => {},
  refused: false,
  waitUntil: null,
  ...over,
});
const failed = load({ error: SAID });
const refused = load({ error: "This account cannot read the facts.", refused: true });
const answered = load({ data: { facts: [], total: 0 } });
// Without React's markers between text nodes: what a person reads.
const html = (el: ReactElement) => renderToString(el).replace(/<!-- -->/g, "");

/** What `shown` draws, and how many times it asked for the list. */
function drawn(page: "facts" | "graph", l: Load<unknown>, shown: number, total = shown) {
  let asked = 0;
  const list = () => {
    asked += 1;
    return <ul>the facts</ul>;
  };
  const out = html(
    page === "facts" ? (
      <FactsShown load={l} shown={shown} total={total}>{list}</FactsShown>
    ) : (
      <GraphShown load={l} shown={shown}>{list}</GraphShown>
    ),
  );
  return { out, asked };
}

const EMPTY = { facts: "No facts yet", graph: "No graph yet" } as const;

export const CHECKS: Record<string, Check> = {};
for (const page of ["facts", "graph"] as const) {
  Object.assign(CHECKS, {
    [`${page}: a failed read says the server's sentence with Try again, and nothing of an empty account`]: (t) => {
      const { out, asked } = drawn(page, failed, 0);
      t.ok(out.includes(SAID), out);
      t.ok(out.includes("Try again"), out);
      t.ok(!out.includes(EMPTY[page]), out);
      t.ok(!out.includes("Add a memory") && !out.includes("0 facts"), out);
      t.equal(asked, 0, "the list is not drawn");
    },
    [`${page}: a refused read says so with no Try again, and nothing of an empty account`]: (t) => {
      const { out, asked } = drawn(page, refused, 0);
      t.ok(out.includes("This account cannot read the facts."), out);
      t.ok(!out.includes("Try again") && !out.includes(EMPTY[page]), out);
      t.equal(asked, 0);
    },
    [`${page}: only an answer with no facts draws the empty state`]: (t) => {
      const { out, asked } = drawn(page, answered, 0);
      t.ok(out.includes(EMPTY[page]), out);
      t.ok(!out.includes("Try again"), out);
      t.equal(asked, 0);
    },
    [`${page}: an answer with facts draws them, and a read still out draws neither`]: (t) => {
      const some = drawn(page, answered, 3, 250);
      t.ok(some.out.includes(LIST) && !some.out.includes(EMPTY[page]), some.out);
      t.equal(some.asked, 1);
      const reading = drawn(page, load({ loading: true }), 0);
      t.ok(reading.out.includes("Loading…") && !reading.out.includes(EMPTY[page]), reading.out);
      t.equal(reading.asked, 0);
    },
  } satisfies Record<string, Check>);
}

Object.assign(CHECKS, {
  "facts: the count says what is shown of the total, from an answer only": (t) => {
    t.ok(drawn("facts", answered, 3, 250).out.includes("Showing 3 of 250 facts"));
    t.ok(drawn("facts", answered, 0).out.includes("0 facts"));
    t.ok(drawn("facts", answered, 2, 2).out.includes(">2 facts<"));
  },
} satisfies Record<string, Check>);
