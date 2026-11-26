// What the Home page says the Playground is (HomePage.tsx, ExplorePlatform)
// and what the Playground says when nothing matched (PlaygroundPage.tsx,
// NothingMatched), drawn with React's own server renderer: run in node by
// scripts/check_the_behaviour.mjs (`npm test`). The QA round of 24 Nov 2026:
// the card said "Store and recall memories live", the page only searches
// (it is the read path, by design: memories are written by the agent with
// its key), and on an empty project it answered "Store some memories first"
// with no way to learn how.
import type { ReactElement } from "react";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router";
import type { Check } from "@/lib/behaviour";
import { ExplorePlatform } from "@/pages/HomePage";
import { NothingMatched, RetrievedShown } from "@/pages/PlaygroundPage";
import type { SearchHit } from "@/lib/agentsApi";

const html = (el: ReactElement) =>
  renderToString(<MemoryRouter>{el}</MemoryRouter>).replace(/<!-- -->/g, "");
const text = (s: string) => s.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

// The card that links to /playground, as a person reads it.
const playgroundCard = () => {
  const page = html(<ExplorePlatform />);
  const card = page.match(/<a[^>]*href="\/playground"[^>]*>([\s\S]*?)<\/a>/);
  if (!card) throw new Error("no card links to /playground");
  return text(card[1]);
};

export const CHECKS: Record<string, Check> = {
  "the Playground card says the page retrieves, as the page does": (t) => {
    t.equal(
      playgroundCard(),
      "Playground Ask a question and see exactly what your agent retrieves: the memories and facts its key gets back. Try it",
    );
  },
  "nothing matched: the page links to Install, where the agent's first write is shown": (t) => {
    const out = html(<NothingMatched searched="memories" />);
    t.match(out, /<a[^>]*href="\/install"[^>]*>Install<\/a>/);
    t.equal(
      text(out),
      "No memories matched If this project has none yet, your agent writes them with its key: Install shows the first write. Otherwise broaden the query.",
    );
  },
  "what is searched is named in the empty state, a part's kind too when a part adds one": (t) => {
    t.match(text(html(<NothingMatched searched="memories and notes" />)), /^No memories and notes matched /);
  },
  "the page's search with no hits draws the empty state that links to Install": (t) => {
    let listed = 0;
    const list = () => {
      listed += 1;
      return <ul>the hits</ul>;
    };
    for (const hits of [[], null] as (SearchHit[] | null)[]) {
      const out = html(
        <RetrievedShown loading={false} error={null} hits={hits} searched="memories">{list}</RetrievedShown>,
      );
      t.equal(out, html(<NothingMatched searched="memories" />));
    }
    t.equal(listed, 0, "nothing is listed");
  },
  "a failed search is said as one, with no Install link and nothing listed": (t) => {
    const out = html(
      <RetrievedShown loading={false} error="The search did not run." hits={null} searched="memories">
        {() => <ul>the hits</ul>}
      </RetrievedShown>,
    );
    t.ok(out.includes("The search failed") && out.includes("The search did not run."), out);
    t.ok(!out.includes('href="/install"') && !out.includes("the hits"), out);
  },
  "hits are listed, and no empty state is drawn": (t) => {
    const hit = { id: "m1", content: "x", score: 1 } as SearchHit;
    const out = html(
      <RetrievedShown loading={false} error={null} hits={[hit]} searched="memories">
        {(found) => <ul>{found.length} hit</ul>}
      </RetrievedShown>,
    );
    t.ok(out.includes("1 hit") && !out.includes("matched"), out);
  },
};
