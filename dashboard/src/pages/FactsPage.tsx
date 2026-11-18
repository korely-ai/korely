import { useState, type ReactNode } from "react";
import { Network, User, Clock, Pencil, Trash2, Check, X } from "lucide-react";
import { PageHeader, EmptyState, ProjectScopeBanner } from "@/components/PageShell";
import { FactFilters } from "@/components/FactFilters";
import { errorMessage, type Fact, type FactsList, type FactsQuery } from "@/lib/agentsApi";
import { KNOWN_ON, closedLabel, factDates, factsPage } from "@/lib/factPages";
import { familyColor, useFamilies } from "@/lib/families";
import { useFactEditing } from "@/lib/useFactEditing";
import { useLatest } from "@/lib/latest";
import { useLoad, type Load } from "@/lib/useLoad";
import { ErrorNote, Loaded } from "@/components/LoadState";
import { DOCS } from "@/lib/docsLinks";
import { useProjectScope } from "@/contexts/ProjectContext";

// One page of the list; "Show more" reads the next one, so the list and the
// total above it agree once everything is shown (audit of 17 Oct 2026).
const PAGE = 100;

/** The pages "Show more" read after the first, and the first they follow. */
interface More {
  of: FactsList;
  facts: Fact[];
  total: number;
}

/**
 * What the Facts page draws for its first page (lib/useLoad.ts): a failure
 * is the server's sentence with Try again, and the count and the empty state
 * come only from an answer. The read was caught by hand into an empty list,
 * so a read that failed drew "0 facts" and "No facts yet" under the error,
 * as if the account had none, the finding
 * the Memories page had fixed on 17 Oct. `children` draws the facts.
 */
export function FactsShown({
  load,
  shown,
  total,
  children,
}: {
  load: Load<unknown>;
  shown: number;
  total: number;
  children: () => ReactNode;
}) {
  return (
    <Loaded load={load} className="mb-4">
      {() => (
        <>
          <p className="mb-3 text-xs text-muted-foreground">
            {shown < total
              ? `Showing ${shown.toLocaleString()} of ${total.toLocaleString()} facts`
              : `${total.toLocaleString()} facts`}
          </p>
          {shown === 0 ? (
            <EmptyState icon={<Network className="h-6 w-6" />} title="No facts yet">
              As your agents add memories, Korely extracts typed facts here
              (preferences, places, work, health, and more) and keeps a bi-temporal
              history so you can ask "what was true on a date".
            </EmptyState>
          ) : (
            children()
          )}
        </>
      )}
    </Loaded>
  );
}

export function FactsPage() {
  // The project chosen at the top, or every project (contexts/ProjectContext.tsx).
  const scope = useProjectScope();
  const [endUser, setEndUser] = useState("");
  const [family, setFamily] = useState("");
  const [showInvalidated, setShowInvalidated] = useState(false);
  const families = useFamilies();

  const query = (): FactsQuery => ({
    user_id: endUser.trim() || undefined,
    predicate_family: family || undefined,
    include_invalidated: showInvalidated,
  });

  // The first page is one read (lib/useLoad.ts): the filters read again on
  // every keystroke and only the newest answer lands. A failed read is said
  // with the server's sentence and Try again, and the list is not drawn: it
  // was caught by hand into an empty list, so a read that failed drew
  // "0 facts" and "No facts yet" under the error, as if the account had none,
  // the finding the Memories page had fixed on 17 Oct.
  const view = useLoad<FactsList>(
    () => factsPage(scope, query(), PAGE, 0),
    [scope, endUser, family, showInvalidated],
    "Could not load facts.",
  );

  // The next pages, under the same filters: they belong to the first page
  // they continue, and a newer read (a filter, an edit) drops them.
  const ticket = useLatest();
  const [more, setMore] = useState<More | null>(null);
  const [moreBusy, setMoreBusy] = useState(false);
  // A failed "Show more" belongs to the first page it continued, as the
  // pages do: a filter changed or a fact forgotten since reads a new first
  // page, and the note goes with the old one.
  const [moreError, setMoreError] = useState<{ of: FactsList; text: string } | null>(null);
  const base = view.data;
  const pages = base && more?.of === base ? more : null;
  const moreFailed = base && moreError?.of === base ? moreError.text : null;
  const facts = base ? [...base.facts, ...(pages?.facts ?? [])] : [];
  const total = pages?.total ?? base?.total ?? 0;

  const loadMore = () => {
    if (!base) return;
    const current = ticket();
    const of = base;
    const before = pages?.facts ?? [];
    setMoreBusy(true);
    setMoreError(null);
    factsPage(scope, query(), PAGE, facts.length)
      .then((d) => {
        if (!current()) return;
        setMore({ of, facts: [...before, ...d.facts], total: d.total });
      })
      .catch((e) => current() && setMoreError({ of, text: errorMessage(e) ?? "Could not load more facts." }))
      .finally(() => current() && setMoreBusy(false));
  };

  const { editing, draft, setDraft, busy, err, startEdit, cancel, save, forget } =
    useFactEditing(view.reload);

  return (
    <div>
      <PageHeader
        title="Facts"
        subtitle="The typed knowledge graph your agents build automatically. Subject → predicate → object, with full history. Correct or forget any fact: both keep the bi-temporal record."
        docs={DOCS.facts}
      />

      <ProjectScopeBanner />

      <FactFilters
        className="mb-5"
        endUser={endUser}
        onEndUser={setEndUser}
        onApply={view.reload}
        family={family}
        onFamily={setFamily}
        families={families}
        history={showInvalidated}
        onHistory={setShowInvalidated}
        historyLabel="Show history (closed facts)"
      />

      {(moreFailed || err) && <ErrorNote className="mb-4">{moreFailed || err}</ErrorNote>}

      <FactsShown load={view} shown={facts.length} total={total}>
        {() => (
          <>
              <ul className="space-y-2">
                {facts.map((f) => {
                  const stale = !!f.invalid_at;
                  const dates = factDates(f);
                  const isEditing = editing === f.id;
                  const isBusy = busy === f.id;
                  return (
                    <li
                      key={f.id}
                      className={`group rounded-lg border border-border bg-card px-4 py-3 ${stale ? "opacity-60" : ""}`}
                    >
                      <div className="mb-1.5 flex flex-wrap items-center gap-2">
                        <span className="inline-flex items-center gap-1 rounded bg-secondary px-1.5 py-0.5 text-[11px] capitalize text-muted-foreground">
                          <span className="h-2 w-2 rounded-full" style={{ background: familyColor(f.predicate_family) }} />
                          {f.predicate_family}
                        </span>
                        {/* An id without spaces breaks inside the chip: whole, it
                            pushed the page 110 px past a phone's 390. */}
                        {f.user_id && (
                          <span className="inline-flex min-w-0 max-w-full items-center gap-1 rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
                            <User className="h-3 w-3 shrink-0" />
                            <span className="min-w-0 break-all">{f.user_id}</span>
                          </span>
                        )}
                        {dates.from && (
                          <span className="rounded bg-secondary px-1.5 py-0.5 text-[11px] tabular-nums text-muted-foreground">
                            from {dates.from}
                          </span>
                        )}
                        {stale && (
                          <span className="inline-flex items-center gap-1 rounded bg-destructive/10 px-1.5 py-0.5 text-[11px] tabular-nums text-destructive">
                            <Clock className="h-3 w-3" />
                            {closedLabel(f)}{dates.ended && ` ${dates.ended}`}
                            {dates.knownOn && (
                              <span title={KNOWN_ON}>
                                · known on {dates.knownOn}
                              </span>
                            )}
                          </span>
                        )}

                        {!stale && !isEditing && (
                          <span className="hover-reveal ml-auto flex items-center gap-1 transition-opacity">
                            <button
                              type="button"
                              title="Correct (supersede with a new value)"
                              disabled={isBusy}
                              onClick={() => startEdit(f)}
                              className="rounded p-1 text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
                            >
                              <Pencil className="h-3.5 w-3.5" />
                            </button>
                            <button
                              type="button"
                              title="Forget (invalidate, keep in history)"
                              disabled={isBusy}
                              onClick={() => forget(f)}
                              className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          </span>
                        )}
                      </div>

                      {isEditing ? (
                        <div className="flex flex-wrap items-center gap-1.5">
                          <input
                            value={draft.subject}
                            onChange={(e) => setDraft((d) => ({ ...d, subject: e.target.value }))}
                            className="w-40 rounded border border-input bg-background px-2 py-1 text-sm outline-hidden focus:ring-2 focus:ring-ring"
                            placeholder="subject"
                          />
                          <input
                            value={draft.predicate}
                            onChange={(e) => setDraft((d) => ({ ...d, predicate: e.target.value }))}
                            className="w-32 rounded border border-input bg-background px-2 py-1 font-mono text-xs outline-hidden focus:ring-2 focus:ring-ring"
                            placeholder="predicate"
                          />
                          <input
                            value={draft.object}
                            onChange={(e) => setDraft((d) => ({ ...d, object: e.target.value }))}
                            className="w-44 rounded border border-input bg-background px-2 py-1 text-sm outline-hidden focus:ring-2 focus:ring-ring"
                            placeholder="object"
                          />
                          <button
                            type="button"
                            title="Save correction"
                            disabled={isBusy}
                            onClick={() => save(f)}
                            className="rounded p-1 text-emerald-700 dark:text-emerald-400 hover:bg-secondary disabled:opacity-50"
                          >
                            <Check className="h-4 w-4" />
                          </button>
                          <button
                            type="button"
                            title="Cancel"
                            onClick={cancel}
                            className="rounded p-1 text-muted-foreground hover:bg-secondary"
                          >
                            <X className="h-4 w-4" />
                          </button>
                        </div>
                      ) : (
                        <p className={`break-words text-sm leading-relaxed ${stale ? "line-through" : ""}`}>
                          <span className="font-medium">{f.subject}</span>{" "}
                          <span className="font-mono text-muted-foreground">{f.predicate}</span>{" "}
                          <span className="font-medium">{f.object}</span>
                        </p>
                      )}
                    </li>
                  );
                })}
              </ul>

            {facts.length < total && (
              <button
                type="button"
                onClick={loadMore}
                disabled={moreBusy}
                className="mt-3 w-full rounded-md border border-border px-3 py-2 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
              >
                {moreBusy ? "Loading…" : `Show ${Math.min(PAGE, total - facts.length).toLocaleString()} more`}
              </button>
            )}
          </>
        )}
      </FactsShown>
    </div>
  );
}
