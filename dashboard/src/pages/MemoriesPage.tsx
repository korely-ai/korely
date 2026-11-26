import { useState } from "react";
import { useSearchParams } from "react-router";
import {
  Database,
  Search,
  X,
  User,
  Plug,
  Sparkles,
  ChevronRight,
  Trash2,
  Eraser,
  RefreshCw,
  Bot,
} from "lucide-react";
import { PageHeader, EmptyState, ProjectScopeBanner } from "@/components/PageShell";
import {
  errorMessage,
  memoriesApi,
  type Memory,
  type SearchHit,
  type MemoryWithFacts,
  type MemoryHistory,
  type HistoryEvent,
  type CategoryCount,
} from "@/lib/agentsApi";
import { familyColor } from "@/lib/families";
import { cn } from "@/lib/cn";
import { DOCS } from "@/lib/docsLinks";
import { useLatest } from "@/lib/latest";
import { useLoad } from "@/lib/useLoad";
import { ErrorNote, LoadNotes, Loaded } from "@/components/LoadState";
import { CopyButton } from "@/components/CopyButton";
import { ModalShell } from "@/components/Modal";
import { closedLabel } from "@/lib/factPages";
import { useProjectScope } from "@/contexts/ProjectContext";

type Row = Memory & { score?: number };

const RANGES = [
  { label: "All", days: 0 },
  { label: "1d", days: 1 },
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
] as const;

// One page of the list; "Show more" reads the next one, so the list and the
// total above it agree once everything is shown (audit of 17 Oct 2026: it
// listed 50 under a larger total, and said nothing).
const PAGE = 50;

function relTime(iso: string | null) {
  if (!iso) return "—";
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return `${Math.floor(d / 30)}mo ago`;
}
function fullTime(iso: string | null) {
  return iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";
}

/** What the list shows: a page of the list under its total, or a search's
 *  hits (`total` null: a search has no total). */
interface View {
  rows: Row[];
  total: number | null;
}

/** The pages "Show more" read after `of`, the view they continue. */
interface More {
  of: View;
  rows: Row[];
  total: number;
}

export function MemoriesPage() {
  // The project chosen at the top, or every project (contexts/ProjectContext.tsx).
  const scope = useProjectScope();
  const [query, setQuery] = useState("");
  // The search that was run ("" = the list): typing does not read.
  const [asked, setAsked] = useState("");

  const [days, setDays] = useState(0); // 0 = all time
  const [family, setFamily] = useState<string | null>(null);

  // `/memories?open=mem_...` opens one memory: the Audit log links its
  // memories here.
  const [params] = useSearchParams();
  // Only a memory's public id opens the drawer: the value comes from a link
  // anybody can write (audit of 20 Oct 2026; the request paths encode it as
  // well).
  const [openId, setOpenId] = useState<string | null>(() => {
    const asked = params.get("open");
    return asked && /^mem_[0-9A-Za-z_-]+$/.test(asked) ? asked : null;
  });

  const toRow = (m: Memory | SearchHit): Row => ({
    ...m,
    score: "score" in m ? (m as SearchHit).score : undefined,
  });

  // The list and a search are one read (lib/useLoad.ts): whichever started
  // last fills the rows, and a failure is said as one. A failed read used to
  // leave the rows of the read before, or none, under the error, so the page
  // said "No memories" when it could not read them (audit of 17 Oct 2026).
  const view = useLoad<View>(
    () =>
      asked
        ? memoriesApi
            .search(scope, asked, 25)
            .then((hits) => ({ rows: hits.map(toRow), total: null }))
        : memoriesApi
            .list(scope, PAGE, 0, undefined, family, days)
            .then((d) => ({ rows: d.memories.map(toRow), total: d.total })),
    [scope, asked, family, days],
    asked ? "Search failed." : "Could not load your memories.",
  );

  // Category chips come from the whole scope (independent of the filters).
  // A failed read left only "All", as if there were no categories.
  const cats = useLoad<CategoryCount[]>(
    () => memoriesApi.categories(scope),
    [scope],
    "The categories could not be read.",
  );

  // The next pages, under the same filters; they belong to the view they
  // continue, and a newer view (a filter, a search, Refresh) drops them.
  const ticket = useLatest();
  const [more, setMore] = useState<More | null>(null);
  const [moreBusy, setMoreBusy] = useState(false);
  // What a "Show more" or a delete answered when it did not happen.
  const [actionError, setActionError] = useState<string | null>(null);
  // Deleted here since the view was read; the server hides them from every
  // later read.
  const [gone, setGone] = useState<ReadonlySet<string>>(() => new Set());

  const base = view.data;
  const pages = base && more?.of === base ? more : null;
  const loaded = base ? [...base.rows, ...(pages?.rows ?? [])] : [];
  const rows = loaded.filter((r) => !gone.has(r.id));
  const searching = base?.total === null;
  const total = (pages?.total ?? base?.total ?? 0) - (loaded.length - rows.length);

  const loadMore = () => {
    if (!base || searching) return;
    const current = ticket();
    const of = base;
    const before = pages?.rows ?? [];
    setMoreBusy(true);
    setActionError(null);
    memoriesApi
      .list(scope, PAGE, rows.length, undefined, family, days)
      .then((d) => {
        if (!current()) return;
        setMore({ of, rows: [...before, ...d.memories.map(toRow)], total: d.total });
      })
      .catch((e) => current() && setActionError(errorMessage(e) ?? "Could not load more memories."))
      .finally(() => current() && setMoreBusy(false));
  };

  const runSearch = () => {
    const q = query.trim();
    if (q === asked) view.reload();
    else setAsked(q);
  };
  // A filter shows the list: the search does not take filters.
  const pickFamily = (f: string | null) => {
    setAsked("");
    setFamily(f);
  };
  const pickDays = (d: number) => {
    setAsked("");
    setDays(d);
  };

  // Delete forgets: hidden from every view, the row kept. Erase removes it
  // from the database, as DELETE /v1/memories/{id}?erase=true does.
  const handleDelete = async (id: string, erase = false) => {
    const ask = erase
      ? "Erase this memory for good? Its text, vector and the facts only it stated are deleted from the database. This can't be undone."
      : "Delete this memory? It's hidden from every view, and its text stays in the database: erase it to remove it. This can't be undone.";
    if (!confirm(ask)) return;
    setActionError(null);
    try {
      await memoriesApi.remove(id, erase);
    } catch (e) {
      setActionError(errorMessage(e) ?? "The memory was not deleted. Try again.");
      return;
    }
    setGone((g) => new Set(g).add(id));
    if (openId === id) setOpenId(null);
  };

  return (
    <div>
      <PageHeader
        title="Memories"
        subtitle="What your agents have stored. Click any memory for the typed facts Korely pulled from it and its full timeline."
        docs={DOCS.memories}
      />

      <ProjectScopeBanner />

      {/* search */}
      <div className="mb-4 flex items-center gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && runSearch()}
            placeholder="Search memories by meaning…"
            className="w-full rounded-md border border-input bg-background py-2 pl-9 pr-9 text-sm outline-hidden focus:ring-2 focus:ring-ring"
          />
          {query && (
            <button
              type="button"
              onClick={() => {
                setQuery("");
                setAsked("");
              }}
              aria-label="Clear the search"
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>
        <button
          type="button"
          onClick={runSearch}
          className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary-hover"
        >
          Search
        </button>
      </div>

      {/* filter bar: categories (left) · date range + refresh (right) */}
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <CatChip label="All" active={family === null} onClick={() => pickFamily(null)} />
          {(cats.data ?? []).map((c) => (
            <CatChip
              key={c.family}
              label={c.family}
              color={familyColor(c.family)}
              count={c.count}
              active={family === c.family}
              onClick={() => pickFamily(family === c.family ? null : c.family)}
            />
          ))}
        </div>
        <div className="flex items-center gap-2">
          <div className="inline-flex rounded-lg border border-border bg-secondary/40 p-1">
            {RANGES.map((r) => (
              <button
                key={r.days}
                type="button"
                onClick={() => pickDays(r.days)}
                className={cn(
                  "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                  days === r.days
                    ? "bg-background text-foreground shadow-xs"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {r.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={view.reload}
            title="Refresh"
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </button>
        </div>
      </div>

      {actionError && <ErrorNote className="mb-4">{actionError}</ErrorNote>}

      {/* The list and the categories, each cause said once (LoadNotes): a
          429 or a proxy's page drew the same sentence twice (QA of 14 Nov
          2026). */}
      <LoadNotes className="mb-4" reads={[{ load: view }, { load: cats }]} />

      <Loaded load={view} quiet>
        {() => (
          <>
            {!searching && (
              <p className="mb-3 text-xs text-muted-foreground">
                {rows.length < total
                  ? `Showing ${rows.length.toLocaleString()} of ${total.toLocaleString()} memories`
                  : `${total.toLocaleString()} memories`}
                {family ? ` · ${family}` : ""}
                {days ? ` · last ${days}d` : ""}
              </p>
            )}

            {rows.length === 0 ? (
              <EmptyState
                icon={<Database className="h-6 w-6" />}
                title={searching ? "No matches" : "No memories"}
              >
                {searching
                  ? "Try different words."
                  : family || days
                    ? "No memories match this filter. Clear it to see everything."
                    : "When your agents call POST /v1/memories, what they store shows up here."}
              </EmptyState>
            ) : (
              <MemoryTable rows={rows} onOpen={setOpenId} onDelete={handleDelete} />
            )}

            {!searching && rows.length < total && (
              <button
                type="button"
                onClick={loadMore}
                disabled={moreBusy}
                className="mt-3 w-full rounded-md border border-border px-3 py-2 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
              >
                {moreBusy ? "Loading…" : `Show ${Math.min(PAGE, total - rows.length).toLocaleString()} more`}
              </button>
            )}
          </>
        )}
      </Loaded>

      {openId && (
        <MemoryDrawer
          key={openId}
          id={openId}
          onClose={() => setOpenId(null)}
          onDelete={handleDelete}
        />
      )}
    </div>
  );
}

/**
 * Who wrote a memory: the agent (`agent_id`, the name the Agents page lists),
 * and under it the tool it came through when one said its name. The column
 * showed only the tool, so a memory written over REST by an agent read "—"
 * (all five memories of an account).
 */
export function WrittenBy({ memory }: { memory: Pick<Memory, "agent_id" | "client"> }) {
  const { agent_id: agent, client } = memory;
  if (!agent && !client) return <span className="text-xs text-muted-foreground/60">—</span>;
  return (
    <span className="flex max-w-40 flex-col items-start gap-0.5">
      {agent && (
        <span
          title={`agent_id: ${agent}`}
          className="inline-flex max-w-full items-center gap-1 rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground"
        >
          <Bot className="h-3 w-3 shrink-0" />
          <span className="truncate">{agent}</span>
        </span>
      )}
      {client && (
        <span title={`Through ${client}`} className="inline-flex max-w-full items-center gap-1 text-[11px] text-muted-foreground">
          <Plug className="h-3 w-3 shrink-0" />
          <span className="truncate">via {client}</span>
        </span>
      )}
    </span>
  );
}

function MemoryTable({
  rows,
  onOpen,
  onDelete,
}: {
  rows: Row[];
  onOpen: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead className="bg-secondary/50 text-left text-xs uppercase tracking-wider text-muted-foreground">
          <tr>
            <th className="px-4 py-2.5 font-medium">Time</th>
            <th className="hidden px-4 py-2.5 font-medium sm:table-cell">End user</th>
            <th className="hidden px-4 py-2.5 font-medium lg:table-cell">Written by</th>
            <th className="px-4 py-2.5 font-medium">Memory</th>
            <th className="hidden px-4 py-2.5 font-medium md:table-cell">Categories</th>
            <th className="px-4 py-2.5 text-right font-medium">Action</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map((r) => (
            <tr
              key={r.id}
              onClick={() => onOpen(r.id)}
              className="cursor-pointer transition-colors hover:bg-secondary/40"
            >
              <td className="whitespace-nowrap px-4 py-3 text-muted-foreground" title={fullTime(r.created_at)}>
                {relTime(r.created_at)}
              </td>
              <td className="hidden px-4 py-3 sm:table-cell">
                {r.user_id ? (
                  <span className="inline-flex max-w-40 items-center gap-1 truncate rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
                    <User className="h-3 w-3 shrink-0" />
                    <span className="truncate">{r.user_id}</span>
                  </span>
                ) : (
                  <span className="text-xs text-muted-foreground/60">—</span>
                )}
              </td>
              <td className="hidden px-4 py-3 lg:table-cell">
                <WrittenBy memory={r} />
              </td>
              {/* w-full: the memory takes the room the other columns
                  leave, which fit their content; max-w-0 lets it be cut.
                  Without w-full the table gave the room to the others and
                  cut the memory to ten letters at 1280 px (QA of 14 Nov
                  2026). */}
              <td className="w-full max-w-0 px-4 py-3">
                <div className="flex items-center gap-2">
                  {/* The row opens on a click; this button is the same door
                      for the keyboard, which cannot reach a <tr> (audit of
                      17 Oct 2026). */}
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      onOpen(r.id);
                    }}
                    className="truncate text-left"
                  >
                    {r.content}
                  </button>
                  {r.score !== undefined && (
                    <span className="shrink-0 rounded bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                      {r.score.toFixed(2)}
                    </span>
                  )}
                </div>
              </td>
              <td className="hidden px-4 py-3 md:table-cell">
                <CategoryCells families={r.categories ?? []} />
              </td>
              <td className="px-4 py-3 text-right">
                <div className="flex items-center justify-end gap-1">
                  <button
                    type="button"
                    title="Delete memory"
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(r.id);
                    }}
                    className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                  <ChevronRight className="h-4 w-4 text-muted-foreground/50" />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CatChip({
  label,
  color,
  count,
  active,
  onClick,
}: {
  label: string;
  color?: string;
  count?: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium capitalize transition-colors",
        active
          ? "border-foreground/30 bg-secondary text-foreground"
          : "border-border text-muted-foreground hover:border-foreground/20 hover:text-foreground",
      )}
    >
      {color && <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />}
      {label}
      {count !== undefined && <span className="tabular-nums text-muted-foreground">{count}</span>}
    </button>
  );
}

function CategoryCells({ families }: { families: string[] }) {
  if (families.length === 0) return <span className="text-xs text-muted-foreground/50">—</span>;
  const shown = families.slice(0, 2);
  const extra = families.length - shown.length;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {shown.map((f) => (
        <span
          key={f}
          className="inline-flex items-center gap-1 rounded bg-secondary px-1.5 py-0.5 text-[11px] capitalize text-muted-foreground"
        >
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: familyColor(f) }} />
          {f}
        </span>
      ))}
      {extra > 0 && (
        <span className="rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">+{extra}</span>
      )}
    </div>
  );
}

function MemoryDrawer({
  id,
  onClose,
  onDelete,
}: {
  id: string;
  onClose: () => void;
  onDelete: (id: string, erase?: boolean) => void;
}) {
  const [tab, setTab] = useState<"details" | "timeline">("details");
  // The page keys the drawer by id, so a drawer never shows another memory's
  // answer while its own is out.
  const detail = useLoad<MemoryWithFacts>(
    () => memoriesApi.get(id),
    [id],
    "Could not load this memory.",
  );
  const history = useLoad<MemoryHistory>(
    () => memoriesApi.history(id),
    [id],
    "Could not load the timeline.",
  );

  // Escape is ModalShell's: a second listener here closed the drawer twice,
  // and with a dialog open over it, both at once (audit of 17 Oct 2026).

  return (
    <ModalShell label="Memory details" onClose={onClose} className="fixed inset-0 z-40">
      <div className="absolute inset-0 bg-background/60 backdrop-blur-xs" onClick={onClose} />
      <aside className="absolute right-0 top-0 flex h-full w-full max-w-md flex-col border-l border-border bg-card shadow-xl">
        {/* header + tabs */}
        <div className="border-b border-border">
          <div className="flex items-center justify-between px-5 py-3.5">
            <p className="text-sm font-semibold">Memory details</p>
            <div className="flex items-center gap-1">
              <button
                onClick={() => onDelete(id)}
                title="Delete memory"
                className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
              >
                <Trash2 className="h-4 w-4" />
              </button>
              <button
                onClick={() => onDelete(id, true)}
                title="Erase memory for good"
                className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-destructive/10 hover:text-destructive"
              >
                <Eraser className="h-4 w-4" />
              </button>
              <button type="button" onClick={onClose} aria-label="Close" className="rounded p-1.5 text-muted-foreground hover:text-foreground">
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>
          <div className="flex px-2">
            <Tab active={tab === "details"} onClick={() => setTab("details")}>
              Details
            </Tab>
            <Tab active={tab === "timeline"} onClick={() => setTab("timeline")}>
              Timeline
            </Tab>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {tab === "details" ? (
            <Loaded load={detail}>
              {(d) => (
                <DetailsTab
                  detail={d}
                  families={[...new Set(d.facts.filter((f) => !f.closed).map((f) => f.predicate_family))]}
                />
              )}
            </Loaded>
          ) : (
            <Loaded load={history}>{(h) => <TimelineTab history={h} />}</Loaded>
          )}
        </div>
      </aside>
    </ModalShell>
  );
}

function Tab({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`relative px-3 py-2 text-sm font-medium transition-colors ${
        active ? "text-foreground" : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {children}
      {active && <span className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-foreground" />}
    </button>
  );
}

// A tick only when the id was copied (components/CopyButton.tsx; audit of
// 17 Oct 2026: it showed one with no clipboard at all).
function CopyId({ id }: { id: string }) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-md border border-border bg-secondary/40 px-3 py-2">
      <code className="truncate font-mono text-xs text-muted-foreground">{id}</code>
      <CopyButton
        text={id}
        label="Copy id"
        className="shrink-0 text-muted-foreground hover:text-foreground"
      />
    </div>
  );
}

function DetailsTab({ detail, families }: { detail: MemoryWithFacts; families: string[] }) {
  return (
    <div>
      <CopyId id={detail.id} />

      <p className="mt-3 whitespace-pre-wrap wrap-anywhere text-sm leading-relaxed">{detail.content}</p>

      {families.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {families.map((fam) => (
            <span
              key={fam}
              className="inline-flex items-center gap-1.5 rounded-full border border-border px-2 py-0.5 text-[11px] text-muted-foreground"
            >
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: familyColor(fam) }} />
              {fam}
            </span>
          ))}
        </div>
      )}

      {/* created / updated */}
      <div className="mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-md border border-border bg-border">
        <Meta label="Created" value={fullTime(detail.created_at)} />
        <Meta label="Updated" value={fullTime(detail.updated_at)} />
      </div>

      {/* scope */}
      <div className="mt-3 flex flex-wrap gap-1.5">
        {detail.user_id && (
          <Chip>
            <User className="h-3 w-3" />
            {detail.user_id}
          </Chip>
        )}
        {detail.agent_id && <Chip>agent: {detail.agent_id}</Chip>}
        {detail.run_id && <Chip>run: {detail.run_id}</Chip>}
        {detail.client && (
          <Chip>
            <Plug className="h-3 w-3" />
            via {detail.client}
          </Chip>
        )}
      </div>

      {detail.metadata && Object.keys(detail.metadata).length > 0 && (
        <div className="mt-4">
          <SectionLabel>Metadata</SectionLabel>
          <pre className="overflow-x-auto rounded-md border border-border bg-background px-2.5 py-2 text-[11px] leading-relaxed text-muted-foreground">
            {JSON.stringify(detail.metadata, null, 2)}
          </pre>
        </div>
      )}

      <div className="mt-5">
        <SectionLabel>
          <Sparkles className="h-3 w-3" />
          Facts extracted ({detail.facts.length})
        </SectionLabel>
        {detail.facts.length === 0 ? (
          <p className="rounded-md border border-dashed border-border px-3 py-3 text-xs text-muted-foreground">
            No typed facts from this memory. Korely extracts facts from statements
            about an end user (preferences, places, work…); chit-chat yields none.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {detail.facts.map((f) => {
              const stale = f.closed;
              return (
                <li key={f.id} className={`text-xs leading-relaxed ${stale ? "opacity-55" : ""}`}>
                  <span
                    className="mr-1 inline-block h-2 w-2 rounded-full align-middle"
                    style={{ background: familyColor(f.predicate_family) }}
                  />
                  <span className="font-medium">{f.subject}</span>{" "}
                  <span className="font-mono text-muted-foreground">{f.predicate}</span>{" "}
                  <span className={`font-medium ${stale ? "line-through" : ""}`}>{f.object}</span>
                  {stale && <span className="ml-1 text-[10px] text-destructive">{closedLabel(f)}</span>}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

const EVENT_META: Record<HistoryEvent["event"], { label: string; color: string }> = {
  created: { label: "Created", color: "hsl(var(--primary))" },
  updated: { label: "Updated", color: "hsl(38 92% 50%)" },
  fact_extracted: { label: "Learned a fact", color: "hsl(142 71% 45%)" },
  // One of this memory's own facts stopped being true: "Superseded a fact"
  // read as if this memory had superseded another (functional test of 28 Oct
  // 2026). The fact is named under the label.
  fact_invalidated: { label: "Its fact stopped being true", color: "hsl(var(--destructive))" },
  deleted: { label: "Deleted", color: "hsl(var(--destructive))" },
};

function TimelineTab({ history }: { history: MemoryHistory }) {
  return (
    <div>
      <SectionLabel>What this memory taught the store</SectionLabel>
      <ol className="relative ml-1 border-l border-border">
        {history.events.map((e, i) => {
          const meta = EVENT_META[e.event] ?? { label: e.event, color: "hsl(var(--muted-foreground))" };
          const strike = e.event === "fact_invalidated";
          return (
            <li key={i} className="relative pb-4 pl-5 last:pb-0">
              <span
                className="absolute left-[-5px] top-1 h-2.5 w-2.5 rounded-full ring-2 ring-card"
                style={{ background: meta.color }}
              />
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs font-medium">{meta.label}</span>
                <span className="shrink-0 text-[11px] text-muted-foreground" title={fullTime(e.at)}>
                  {relTime(e.at)}
                </span>
              </div>
              {e.fact && (
                <p className={`mt-0.5 text-xs text-muted-foreground ${strike ? "line-through" : ""}`}>
                  {e.fact}
                </p>
              )}
            </li>
          );
        })}
      </ol>
      <p className="mt-4 text-[11px] text-muted-foreground">
        A memory's life: when it was stored, every typed fact it produced, and
        when one of those facts stopped being true, superseded by a later
        memory or ended.
      </p>
    </div>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-2 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
      {children}
    </p>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-card px-3 py-2">
      <p className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</p>
      <p className="mt-0.5 text-xs">{value}</p>
    </div>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1 rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
      {children}
    </span>
  );
}
