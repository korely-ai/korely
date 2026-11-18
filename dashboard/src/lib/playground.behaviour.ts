// What the Playground asks the one search for and keeps of its answer
// (lib/playground.ts): run in node by scripts/check_the_behaviour.mjs
// (`npm test`). The parts here are stand-ins: a part of the screen that
// adds a kind of result to the search, and one that adds none.
import type { Check } from "./behaviour";
import type { PartSearch, ScreenPart } from "./cloud";
import type { SearchHit } from "./agentsApi";
import { kindsToAsk, partOf, searchedFor, searchesOf, whatTheAgentGets } from "./playground";

const Found = () => null;
const PAGES: PartSearch = { kind: "pages", type: "page", label: "pages", Found };
const NOTES: PartSearch = { kind: "notes", type: "note", label: "notes", Found };

function part(name: string, search?: PartSearch): ScreenPart {
  return { part: name, path: `/${name}`, label: name, icon: (() => null) as never, Page: Found,
           ...(search ? { search } : {}) };
}

function hit(id: string, user_id: string | null, type?: string): SearchHit {
  return { id, user_id, content: id, score: 0.5, ...(type ? { type } : {}) } as SearchHit;
}

export const CHECKS: Record<string, Check> = {
  "with no part that searches, the memories alone, asked as always": (t) => {
    const none = searchesOf([part("plain")]);
    t.deepEqual(none, []);
    t.equal(kindsToAsk(none), undefined, "no `types`: the request of before");
    t.equal(searchedFor(none), "memories");
  },
  "a part that searches adds its kind after the memories": (t) => {
    const s = searchesOf([part("plain"), part("pages", PAGES)]);
    t.deepEqual(s, [PAGES]);
    t.deepEqual(kindsToAsk(s), ["memories", "pages"]);
    t.equal(searchedFor(s), "memories and pages");
    t.deepEqual(kindsToAsk(searchesOf([part("pages", PAGES), part("notes", NOTES)])),
      ["memories", "pages", "notes"]);
    t.equal(searchedFor([PAGES, NOTES]), "memories, pages and notes");
  },
  "a result is drawn by the part its type names, a memory by the page": (t) => {
    t.equal(partOf(hit("m", "maria", "memory"), [PAGES]), undefined);
    t.equal(partOf(hit("m", "maria"), [PAGES]), undefined, "asked with no `types`");
    t.equal(partOf(hit("p", null, "page"), [PAGES]), PAGES);
    t.equal(partOf(hit("p", null, "page"), []), undefined, "a part not served");
  },
  "for an end user, a memory of another is left out, a part's result is kept": (t) => {
    const hits = [hit("mine", "maria", "memory"), hit("theirs", "bo", "memory"),
                  hit("shared", null, "page"), hit("hers", "maria", "page")];
    t.deepEqual(whatTheAgentGets(hits, "maria", [PAGES]).map((h) => h.id),
      ["mine", "shared", "hers"]);
    t.deepEqual(whatTheAgentGets(hits, "", [PAGES]).map((h) => h.id),
      ["mine", "theirs", "shared", "hers"], "no end user: everything, in its order");
    t.deepEqual(whatTheAgentGets([hit("a", "maria"), hit("b", "bo")], "maria", []).map((h) => h.id),
      ["a"], "the memories alone, as before");
  },
};
