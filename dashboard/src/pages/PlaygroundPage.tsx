import { useState } from "react";
import { Link } from "react-router";
import { Play, Search, Network, Database, Bot, Users } from "lucide-react";
import { PageHeader, HelpNote, EmptyState, ProjectScopeBanner } from "@/components/PageShell";
import {
  memoriesApi,
  factsApi,
  errorMessage,
  type SearchHit,
  type Fact,
} from "@/lib/agentsApi";
import { useProjectScope } from "@/contexts/ProjectContext";
import { useScreenParts } from "@/lib/cloud";
import { DOCS } from "@/lib/docsLinks";
import { familyColor } from "@/lib/families";
import { kindsToAsk, partOf, searchedFor, searchesOf, whatTheAgentGets } from "@/lib/playground";

/**
 * Playground — type a question, see exactly what Korely would hand your agent
 * for it: the memories ranked by meaning and, if you scope to an end user, that
 * user's active typed facts. Search is by meaning alone: the keyword half
 * (HYBRID_SEARCH) left on 12 Oct 2026, and this page said "semantic +
 * keyword" until the audit of 17 Oct 2026. No model generation happens here; this
 * is the read path your LLM reasons over. It makes the retrieval visible, which
 * is the whole pitch: Korely returns the data, your agent does the thinking.
 * A part of the product the account is served may add its kind of result to
 * the one search (lib/playground.ts, lib/cloud.ts PartSearch): asked with the
 * memories, merged by rank, and drawn by the part.
 */
export function PlaygroundPage() {
  const [query, setQuery] = useState("");
  const [endUser, setEndUser] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [facts, setFacts] = useState<Fact[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [ran, setRan] = useState(false);
  // A failed read is said as one, never as "nothing matched" (audit of 17
  // Oct 2026): the server answers 503 precisely so that its own failure is
  // never read as "you have no memories".
  const [hitsError, setHitsError] = useState<string | null>(null);
  const [factsError, setFactsError] = useState<string | null>(null);
  // What a key of the project chosen at the top would get back: the agent's
  // key reads one project, so the Playground reads that one; with All
  // projects, the whole account, which no key reads (the banner says so).
  const scope = useProjectScope();
  const searches = searchesOf(useScreenParts().parts);
  const searched = searchedFor(searches);

  const run = (e: React.FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    setLoading(true);
    setRan(true);
    const eu = endUser.trim();
    Promise.allSettled([
      // Scope the search to the end user server-side, so the ranking and limit
      // are exactly what that user's agent would get back from search/get_context.
      memoriesApi.search(scope, q, 10, eu || undefined, kindsToAsk(searches)),
      eu ? factsApi.list(scope, { user_id: eu, limit: 50 }) : Promise.resolve(null),
    ])
      .then(([h, f]) => {
        // The server already scopes to the end user; a memory that slips
        // through is left out (whatTheAgentGets), keeping the "what this
        // user's agent would see" promise honest.
        setHits(h.status === "fulfilled" ? whatTheAgentGets(h.value, eu, searches) : null);
        setHitsError(h.status === "rejected"
          ? errorMessage(h.reason) ?? "The search did not run. Try again in a moment."
          : null);
        setFacts(f.status === "fulfilled" && f.value ? f.value.facts : null);
        setFactsError(f.status === "rejected"
          ? errorMessage(f.reason) ?? "The facts could not be read. Try again in a moment."
          : null);
      })
      .finally(() => setLoading(false));
  };

  return (
    <div>
      <PageHeader
        title="Playground"
        subtitle="Type a question and see what Korely retrieves for it in the project chosen at the top: the memories and facts an agent with that project's key would get back. No answer is generated here; that's your LLM's job."
        docs={DOCS.playground}
      />

      <ProjectScopeBanner />

      <HelpNote title="What this shows (and why there's no chat bubble)">
        <p className="mb-2">
          Korely is a <span className="text-foreground">retrieval</span> layer,
          not a chatbot. The Playground runs the read path (a search by
          meaning over your {searched}, plus a scoped end user's active typed
          facts) and shows you the ranked results. That's what you'd inject into your
          own model's prompt.
        </p>
        <p>
          Scope to an <code className="font-mono text-xs">end user</code> to see
          their facts too: only the facts valid <em>now</em> appear (superseded
          ones drop out automatically: that's the bi-temporal part).
        </p>
      </HelpNote>

      <form onSubmit={run} className="mb-6 space-y-3">
        <div className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2">
          <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="e.g. how do I reach this customer?"
            className="w-full bg-transparent text-sm outline-hidden placeholder:text-muted-foreground"
          />
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <div className="flex flex-1 items-center gap-2 rounded-lg border border-border bg-card px-3 py-2">
            <Users className="h-4 w-4 shrink-0 text-muted-foreground" />
            <input
              value={endUser}
              onChange={(e) => setEndUser(e.target.value)}
              placeholder="scope to an end user (optional, e.g. maria)"
              className="w-full bg-transparent text-sm outline-hidden placeholder:text-muted-foreground"
            />
          </div>
          <button
            type="submit"
            disabled={loading || !query.trim()}
            className="inline-flex items-center justify-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:bg-primary-hover disabled:opacity-60"
          >
            <Play className="h-4 w-4" />
            {loading ? "Retrieving…" : "Retrieve"}
          </button>
        </div>
      </form>

      {!ran ? (
        <EmptyState icon={<Play className="h-6 w-6" />} title="Run a query to see retrieval">
          Type a question above. You'll see the {searched} ranked by relevance and,
          if you scope to an end user, their active facts.
        </EmptyState>
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
          <section className="lg:col-span-3">
            <h2 className="mb-2 flex items-center gap-1.5 text-sm font-medium">
              <Database className="h-4 w-4 text-muted-foreground" />
              Retrieved {searched}
              {hits && <span className="text-muted-foreground">· {hits.length}</span>}
            </h2>
            <RetrievedShown loading={loading} error={hitsError} hits={hits} searched={searched}>
              {(found) => (
                <ul className="space-y-2">
                  {found.map((h) => {
                    const part = partOf(h, searches);
                    return part ? (
                      <part.Found key={`${h.type}:${h.id}`} hit={h} />
                    ) : (
                      <MemoryHit key={h.id} hit={h} />
                    );
                  })}
                </ul>
              )}
            </RetrievedShown>
          </section>

          <aside className="lg:col-span-2">
            <h2 className="mb-2 flex items-center gap-1.5 text-sm font-medium">
              <Network className="h-4 w-4 text-muted-foreground" />
              Active facts
            </h2>
            {!endUser.trim() ? (
              <p className="rounded-lg border border-dashed border-border bg-card/40 px-4 py-6 text-center text-sm text-muted-foreground">
                Scope to an end user above to see their typed facts.
              </p>
            ) : loading ? (
              <p className="text-sm text-muted-foreground">Loading facts…</p>
            ) : factsError ? (
              <p className="rounded-lg border border-dashed border-border bg-card/40 px-4 py-6 text-center text-sm text-muted-foreground">
                {factsError}
              </p>
            ) : !facts || facts.length === 0 ? (
              <p className="rounded-lg border border-dashed border-border bg-card/40 px-4 py-6 text-center text-sm text-muted-foreground">
                No active facts for <code className="font-mono">{endUser.trim()}</code> yet.
              </p>
            ) : (
              <ul className="space-y-1.5">
                {facts
                  .filter((f) => !f.invalid_at)
                  .map((f) => (
                    <FactRow key={f.id} fact={f} />
                  ))}
              </ul>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}

/** What the Playground draws for the memories of a search, as the page
 *  holds it: retrieving, a search that failed (said as one, never as
 *  nothing matched: audit of 17 Oct 2026), nothing matched
 *  (`NothingMatched`), or what was found, drawn by `list`. Its own
 *  component so that what each state draws is checked as the page draws it
 *  (PlaygroundPage.behaviour.tsx), as FactsPage's FactsShown is. */
export function RetrievedShown({
  loading,
  error,
  hits,
  searched,
  children: list,
}: {
  loading: boolean;
  error: string | null;
  hits: SearchHit[] | null;
  searched: string;
  children: (hits: SearchHit[]) => React.ReactNode;
}) {
  if (loading) return <p className="text-sm text-muted-foreground">Retrieving…</p>;
  if (error) {
    return (
      <EmptyState icon={<Database className="h-5 w-5" />} title="The search failed">
        {error}
      </EmptyState>
    );
  }
  if (!hits || hits.length === 0) return <NothingMatched searched={searched} />;
  return <>{list(hits)}</>;
}

/** A search that found nothing. The page only reads: memories are written by
 *  the agent with its key (POST /v1/memories, the SDK, the MCP), and the
 *  dashboard has no write of its own, so the way to the first memory is the
 *  Install page. Up to 1.0.37 this said "Store some memories first" with no
 *  way to learn how (QA of 24 Nov 2026). */
export function NothingMatched({ searched }: { searched: string }) {
  return (
    <EmptyState icon={<Database className="h-5 w-5" />} title={`No ${searched} matched`}>
      If this project has none yet, your agent writes them with its key:{" "}
      <Link to="/install" className="text-foreground underline underline-offset-2">
        Install
      </Link>{" "}
      shows the first write. Otherwise broaden the query.
    </EmptyState>
  );
}

function MemoryHit({ hit }: { hit: SearchHit }) {
  const pct = Math.round(Math.max(0, Math.min(1, hit.score)) * 100);
  return (
    <li className="rounded-lg border border-border bg-card p-3">
      <div className="mb-1.5 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {hit.agent_id && (
            <span className="inline-flex items-center gap-1">
              <Bot className="h-3 w-3" />
              {hit.agent_id}
            </span>
          )}
          {hit.user_id && (
            <span className="inline-flex items-center gap-1">
              <Users className="h-3 w-3" />
              {hit.user_id}
            </span>
          )}
        </div>
        <span
          className="shrink-0 font-mono text-[11px] text-muted-foreground"
          title="relevance score"
        >
          {pct}%
        </span>
      </div>
      <p className="text-sm leading-relaxed">{hit.content}</p>
    </li>
  );
}

function FactRow({ fact }: { fact: Fact }) {
  return (
    <li className="flex items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-sm">
      <span
        className="h-2 w-2 shrink-0 rounded-full"
        style={{ backgroundColor: familyColor(fact.predicate_family) }}
        title={fact.predicate_family}
      />
      <span className="truncate">
        <span className="font-medium">{fact.subject}</span>{" "}
        <span className="text-muted-foreground">{fact.predicate}</span>{" "}
        <span className="font-medium">{fact.object}</span>
      </span>
    </li>
  );
}
