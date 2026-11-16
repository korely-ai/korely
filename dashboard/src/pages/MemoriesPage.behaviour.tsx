// Who wrote a memory, as the Memories list says it (MemoriesPage.tsx,
// WrittenBy): run in node by scripts/check_the_behaviour.mjs (`npm test`),
// drawn with renderToString. Pre-tester check of 15 Nov 2026: the column
// "Written by" showed the tool only, so the five memories an agent wrote
// over REST in a real account all read "—".
import type { ReactElement } from "react";
import { renderToString } from "react-dom/server";
import type { Check } from "@/lib/behaviour";
import { WrittenBy } from "@/pages/MemoriesPage";

// Without React's markers between text nodes: what a person reads.
const html = (el: ReactElement) => renderToString(el).replace(/<!-- -->/g, "");
const text = (el: ReactElement) => html(el).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

export const CHECKS: Record<string, Check> = {
  "the agent is who wrote it, and the tool it came through is said under it": (t) => {
    const both = text(<WrittenBy memory={{ agent_id: "support-bot", client: "Claude Code" }} />);
    t.equal(both, "support-bot via Claude Code");
    t.equal(text(<WrittenBy memory={{ agent_id: "support-bot", client: null }} />), "support-bot");
  },
  "a memory written over REST by no agent through no named tool reads a dash": (t) => {
    t.equal(text(<WrittenBy memory={{ agent_id: null, client: null }} />), "—");
    t.equal(text(<WrittenBy memory={{ agent_id: null, client: "cursor" }} />), "via cursor");
  },
};
