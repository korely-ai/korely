import { useCallback, useEffect, useState } from "react";
import { Network, User, Clock, Pencil, Trash2, Check, X } from "lucide-react";
import { PageHeader, EmptyState, ProjectScopeBanner } from "@/components/PageShell";
import { FactFilters } from "@/components/FactFilters";
import { type Fact, type FactsQuery } from "@/lib/agentsApi";
import { KNOWN_ON, closedLabel, factDates, factsPage } from "@/lib/factPages";
import { familyColor, useFamilies } from "@/lib/families";
import { useFactEditing } from "@/lib/useFactEditing";
import { useLatest } from "@/lib/latest";
import { DOCS } from "@/lib/docsLinks";
import { useProjectScope } from "@/contexts/ProjectContext";

// One page of the list; "Show more" reads the next one, so the list and the
// total above it agree once everything is shown (audit of 17 Oct 2026).
const PAGE = 100;

export function FactsPage() {
  // The project chosen at the top, or every project (contexts/ProjectContext.tsx).
  const scope = useProjectScope();
  const [facts, setFacts] = useState<Fact[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [endUser, setEndUser] = useState("");
  const [family, setFamily] = useState("");
  const [showInvalidated, setShowInvalidated] = useState(false);
  const families = useFamilies();

  // The filters read again on every keystroke: only the newest answer lands.
  const ticket = useLatest();
  const query = useCallback(
    (): FactsQuery => ({
      user_id: endUser.trim() || undefined,
      predicate_family: family || undefined,
      include_invalidated: showInvalidated,
    }),
    [endUser, family, showInvalidated],
  );
  const load = useCallback(() => {
    const current = ticket();
    setLoading(true);
    setError(null);
    factsPage(scope, query(), PAGE, 0)
      .then((d) => {
        if (!current()) return;
        setFacts(d.facts);
        setTotal(d.total);
      })
      .catch(() => current() && setError("Could not load facts."))
      .finally(() => current() && setLoading(false));
  }, [scope, query, ticket]);

  // The next page, under the same filters: a filter changed meanwhile takes a
  // newer ticket, and this page is then dropped.
  const loadMore = () => {
    const current = ticket();
    setMore(true);
    factsPage(scope, query(), PAGE, facts.length)
      .then((d) => {
        if (!current()) return;
        setFacts((prev) => [...prev, ...d.facts]);
        setTotal(d.total);
      })
      .catch(() => current() && setError("Could not load more facts."))
      .finally(() => current() && setMore(false));
  };

  useEffect(() => {
    load();
  }, [load]);

  const { editing, draft, setDraft, busy, err, startEdit, cancel, save, forget } =
    useFactEditing(load);

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
        onApply={load}
        family={family}
        onFamily={setFamily}
        families={families}
        history={showInvalidated}
        onHistory={setShowInvalidated}
        historyLabel="Show history (closed facts)"
      />

      {(error || err) && (
        <p className="mb-4 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error || err}
        </p>
      )}

      {!loading && (
        <p className="mb-3 text-xs text-muted-foreground">
          {facts.length < total
            ? `Showing ${facts.length.toLocaleString()} of ${total.toLocaleString()} facts`
            : `${total.toLocaleString()} facts`}
        </p>
      )}

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : facts.length === 0 ? (
        <EmptyState icon={<Network className="h-6 w-6" />} title="No facts yet">
          As your agents add memories, Korely extracts typed facts here
          (preferences, places, work, health, and more) and keeps a bi-temporal
          history so you can ask "what was true on a date".
        </EmptyState>
      ) : (
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
                  {f.user_id && (
                    <span className="inline-flex items-center gap-1 rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
                      <User className="h-3 w-3" />
                      {f.user_id}
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
                    <span className="ml-auto flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
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
                      className="rounded p-1 text-emerald-500 hover:bg-secondary disabled:opacity-50"
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
                  <p className={`text-sm leading-relaxed ${stale ? "line-through" : ""}`}>
                    <span className="font-medium">{f.subject}</span>{" "}
                    <span className="font-mono text-muted-foreground">{f.predicate}</span>{" "}
                    <span className="font-medium">{f.object}</span>
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {!loading && facts.length < total && (
        <button
          type="button"
          onClick={loadMore}
          disabled={more}
          className="mt-3 w-full rounded-md border border-border px-3 py-2 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-50"
        >
          {more ? "Loading…" : `Show ${Math.min(PAGE, total - facts.length).toLocaleString()} more`}
        </button>
      )}
    </div>
  );
}
