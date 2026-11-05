import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import {
  Waypoints, Clock, User as UserIcon, FileText, X, Pencil, Trash2, Check,
  Maximize2, Minimize2, ZoomIn, ZoomOut, LocateFixed, Pause, Play, Scan,
} from "lucide-react";
import { PageHeader, EmptyState, HelpNote } from "@/components/PageShell";
import { FactFilters } from "@/components/FactFilters";
import type { GNode, GEdge } from "@/lib/graphTypes";
import type { GraphHandle } from "@/lib/graphFocus";
import { FactGraph } from "@/components/FactGraph";

// three.js is most of the weight, so the 3D view is fetched only when asked for.
const FactGraph3D = lazy(() => import("@/components/FactGraph3D"));
import { memoriesApi, type Fact, type FactsQuery, type Memory } from "@/lib/agentsApi";
import { FACTS_PAGE_MAX, KNOWN_ON, closedLabel, factDates, factsPage } from "@/lib/factPages";
import { FAMILY_COLORS, familyColor, useFamilies } from "@/lib/families";
import { cn } from "@/lib/cn";
import { useDismiss } from "@/lib/useDismiss";
import { useFactEditing } from "@/lib/useFactEditing";
import { useLatest } from "@/lib/latest";
import { DOCS } from "@/lib/docsLinks";

const DAY = 86_400_000;

const toolButton =
  "inline-flex h-8 items-center justify-center rounded-md border border-border bg-card/80 text-[11px] text-muted-foreground backdrop-blur hover:text-foreground";

// The graph reads its facts a page at a time up to this many, the freshest
// first, and says so when there are more. It drew the first 100 in silence
// until the audit of 17 Oct 2026, and time travel replayed a cut history.
const GRAPH_MAX = 1_000;

/** The facts the graph draws, and how many the filters match in all; null
 *  when a newer read started meanwhile. */
async function readGraph(
  q: FactsQuery,
  current: () => boolean,
): Promise<{ facts: Fact[]; total: number } | null> {
  const facts: Fact[] = [];
  let total = 0;
  for (;;) {
    const d = await factsPage(q, FACTS_PAGE_MAX, facts.length);
    if (!current()) return null;
    facts.push(...d.facts);
    total = d.total;
    if (d.facts.length === 0 || facts.length >= Math.min(total, GRAPH_MAX)) break;
  }
  return { facts: facts.slice(0, GRAPH_MAX), total };
}

function fmtDate(ms: number): string {
  const d = new Date(ms);
  return d.toISOString().slice(0, 10);
}

// `?as_of=2026-04-01` read the way the API reads it: a date alone is midnight UTC.
function linkedDay(s: string | null): number | null {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const ms = Date.parse(`${s}T00:00:00Z`);
  return Number.isNaN(ms) ? null : ms;
}

// The two clocks of a fact (services/facts/temporal.py): "true" replays what
// was true on the date, as the store knows it now (`as_of`); "known" replays
// what the store knew on the date (`known_as_of`): a fact it had not learned
// yet is not there, and an end it had not learned yet is not drawn. A move
// told in October and made in September: on 20 September "true" shows the new
// address, "known" the old one.
type Clock = "true" | "known";

const at = (s: string | null | undefined, missing: number) => {
  const t = s ? Date.parse(s) : NaN;
  return Number.isNaN(t) ? missing : t;
};

function shownAt(f: Fact, t: number, clock: Clock): boolean {
  if (at(f.valid_from, -Infinity) > t) return false; // not yet true then
  return clock === "true" || at(f.created_at, -Infinity) <= t; // not yet learned then
}

function endedAt(f: Fact, t: number, clock: Clock): boolean {
  if (!f.invalid_at || at(f.invalid_at, Infinity) > t) return false;
  return clock === "true" || at(f.closed_known_at ?? f.created_at, -Infinity) <= t;
}

export function GraphPage() {
  const [facts, setFacts] = useState<Fact[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // A link can open the graph on one end user at one date,
  // `/graph?user_id=c4821&as_of=2026-04-01`: the demo's "show me April" is that
  // link, and the slider stays there to move. Read once, on arrival.
  const [params] = useSearchParams();
  const linkedAsOf = useRef<number | null>(
    linkedDay(params.get("known_as_of")) ?? linkedDay(params.get("as_of")),
  );
  const [clock, setClock] = useState<Clock>(params.get("known_as_of") ? "known" : "true");
  const [endUser, setEndUser] = useState(params.get("user_id") ?? "");
  const [family, setFamily] = useState("");
  const families = useFamilies();
  const [showHistory, setShowHistory] = useState(
    linkedAsOf.current !== null || params.get("history") === "1",
  );

  const [asOf, setAsOf] = useState<number | null>(null); // ms; null = now
  const [selected, setSelected] = useState<string | null>(null);
  const [view3d, setView3d] = useState(false);
  // Show only the selected node and its strongest neighbours (the rest is not drawn).
  const [focusOnly, setFocusOnly] = useState(false);
  const [layoutRunning, setLayoutRunning] = useState(false);
  const view = useRef<GraphHandle | null>(null);

  // Enlarged: the browser's fullscreen where it has one, and the page's own
  // full-window layer where it has not (a phone's Safari); Esc closes either.
  const stage = useRef<HTMLDivElement | null>(null);
  const [enlarged, setEnlarged] = useState(false);
  // Esc is listened to by the shared hook, as everywhere else (the layer covers
  // the window, so a press outside it cannot happen).
  useDismiss(stage, enlarged, () => setEnlarged(false));
  useEffect(() => {
    if (!enlarged) return;
    const root = document.documentElement;
    const overflow = root.style.overflow;
    root.style.overflow = "hidden";
    void stage.current?.requestFullscreen?.().catch(() => {});
    const onChange = () => !document.fullscreenElement && setEnlarged(false);
    document.addEventListener("fullscreenchange", onChange);
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
      root.style.overflow = overflow;
      if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
    };
  }, [enlarged]);
  const [mem, setMem] = useState<Map<string, Memory | "loading" | "error">>(new Map());

  // The filters read again on every keystroke, and a refetch after an edit
  // can cross a reload: only the newest answer lands.
  const ticket = useLatest();
  const load = useCallback(() => {
    const current = ticket();
    setLoading(true);
    setError(null);
    setSelected(null);
    readGraph(
      {
        user_id: endUser.trim() || undefined,
        predicate_family: family || undefined,
        include_invalidated: showHistory,
      },
      current,
    )
      .then((d) => {
        if (!d) return;
        setFacts(d.facts);
        setTotal(d.total);
        // Back to "now" on every reload, except the first one when a link
        // asked for a date.
        setAsOf(linkedAsOf.current);
        linkedAsOf.current = null;
      })
      .catch(() => current() && setError("Could not load the graph."))
      .finally(() => current() && setLoading(false));
  }, [endUser, family, showHistory, ticket]);

  useEffect(() => { load(); }, [load]);

  // Re-fetch facts without dropping the selected node or the as_of scrubber —
  // used after a forget/correct so the side panel stays put.
  const refetch = useCallback(() => {
    const current = ticket();
    readGraph(
      {
        user_id: endUser.trim() || undefined,
        predicate_family: family || undefined,
        include_invalidated: showHistory,
      },
      current,
    )
      .then((d) => {
        if (!d) return;
        setFacts(d.facts);
        setTotal(d.total);
      })
      .catch(() => current() && setError("Could not refresh the graph."));
  }, [endUser, family, showHistory, ticket]);

  const factEdit = useFactEditing(refetch);

  // Timeline bounds: earliest fact start → now. Only meaningful with history on.
  const [minMs, maxMs] = useMemo(() => {
    const now = Date.now();
    let lo = now;
    facts.forEach((f) => {
      const t = f.valid_from ? Date.parse(f.valid_from) : f.created_at ? Date.parse(f.created_at) : now;
      if (!Number.isNaN(t) && t < lo) lo = t;
      const learned = at(f.created_at, now);
      if (learned < lo) lo = learned;
    });
    return [Math.min(lo, now - DAY), now];
  }, [facts]);

  const asOfMs = asOf ?? maxMs;

  // Facts → graph at the chosen instant. A fact "exists" once valid_from ≤ asOf;
  // it's closed (superseded or ended) once invalid_at ≤ asOf. Scrubbing the slider literally
  // replays the knowledge as it was on that date — the bi-temporal moat.
  const { nodes, edges, endUsers } = useMemo(() => {
    const nodeMap = new Map<string, GNode>();
    const subjects = new Set(facts.map((f) => f.subject));
    const es: GEdge[] = [];
    const users = new Set<string>();

    const touch = (label: string, kind: "entity" | "value", seen: number, subjectType?: string) => {
      const existing = nodeMap.get(label);
      if (existing) {
        existing.degree += 1;
        if (seen > 0) existing.lastSeen = Math.max(existing.lastSeen ?? 0, seen);
        if (kind === "entity") existing.kind = "entity";
        if (subjectType && subjectType !== "unknown") existing.subjectType = subjectType;
        return;
      }
      nodeMap.set(label, { id: label, label, kind, subjectType, degree: 1, lastSeen: seen > 0 ? seen : undefined });
    };

    facts.forEach((f) => {
      if (!shownAt(f, asOfMs, clock)) return; // not yet true, or not yet known
      const invalidated = endedAt(f, asOfMs, clock);
      if (f.user_id) users.add(f.user_id);
      const seen = Math.max(at(f.valid_from, 0), at(f.created_at, 0));
      touch(f.subject, "entity", seen, f.subject_type);
      touch(f.object, subjects.has(f.object) ? "entity" : "value", seen);
      es.push({
        id: f.id,
        source: f.subject,
        target: f.object,
        predicate: f.predicate,
        family: f.predicate_family,
        invalidated,
      });
    });

    return { nodes: [...nodeMap.values()], edges: es, endUsers: users.size };
  }, [facts, asOfMs, clock]);

  // Facts touching the selected node (at this instant), for the side panel.
  const selectedFacts = useMemo(() => {
    if (!selected) return [];
    return facts
      .filter((f) => shownAt(f, asOfMs, clock) && (f.subject === selected || f.object === selected))
      .map((f) => ({
        ...f,
        closed: endedAt(f, asOfMs, clock),
        dates: factDates(f),
      }));
  }, [selected, facts, asOfMs, clock]);

  // Lazily fetch the memories that produced the selected node's facts.
  const memIds = useMemo(
    () => [...new Set(selectedFacts.map((f) => f.source_memory_id).filter(Boolean) as string[])],
    [selectedFacts],
  );
  const fetching = useRef<Set<string>>(new Set());
  useEffect(() => {
    memIds.forEach((id) => {
      if (mem.has(id) || fetching.current.has(id)) return;
      fetching.current.add(id);
      setMem((m) => new Map(m).set(id, "loading"));
      memoriesApi
        .get(id)
        .then((data) => setMem((m) => new Map(m).set(id, data)))
        .catch(() => setMem((m) => new Map(m).set(id, "error")))
        .finally(() => fetching.current.delete(id));
    });
  }, [memIds, mem]);

  const selectedNode = nodes.find((n) => n.id === selected) || null;

  return (
    <div>
      <PageHeader
        title="Memory Graph"
        subtitle="What your agents know, as a graph. Entities and values are nodes; every typed fact is a relation. Drag the time slider to replay what was true on any date."
        docs={DOCS.graph}
      />

      <HelpNote title="How to read this graph">
        <ul className="ml-4 list-disc space-y-1">
          <li><span className="text-foreground">Filled dots are entities</span> (people, orgs, places, e.g. "Maria", "Northwind"). <span className="text-foreground">Hollow dots are values</span> ("Berlin", "50 euro").</li>
          <li>Each <span className="text-foreground">line is a typed fact</span> (subject → predicate → object), colored by family (see the legend below the graph).</li>
          <li>A <span className="text-foreground">dashed line is a closed fact</span>: true once, then replaced by a newer one or ended.</li>
          <li><span className="text-foreground">Time travel</span>: turn it on, then drag the slider and the graph redraws as it was on that date (a 50€ fact flips back to live, its 60€ successor disappears).</li>
          <li><span className="text-foreground">True then or known then</span>: "True then" shows what was true on that date as Korely knows it today; "Known then" shows what Korely knew on that date. A move made in September and told in October: on 20 September the first shows the new address, the second the old one.</li>
          <li><span className="text-foreground">Click a node</span> to see its facts and the memories that produced them. Filter by end user to focus on one customer.</li>
        </ul>
      </HelpNote>

      {/* filters */}
      <FactFilters
        className="mb-4"
        endUser={endUser}
        onEndUser={setEndUser}
        onApply={load}
        family={family}
        onFamily={setFamily}
        families={families}
        history={showHistory}
        onHistory={setShowHistory}
        historyLabel="Time travel (load closed facts)"
      />

      {/* as_of scrubber — only meaningful once history is loaded */}
      {showHistory && facts.length > 0 && (
        <div className="mb-4 flex items-center gap-3 rounded-lg border border-border bg-card px-4 py-2.5">
          <Clock className="h-4 w-4 shrink-0 text-muted-foreground" />
          <div className="inline-flex shrink-0 rounded-md border border-input p-0.5" role="group"
               aria-label="Which clock">
            {(["true", "known"] as const).map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => setClock(c)}
                aria-pressed={clock === c}
                title={c === "true"
                  ? "What was true on this date, as Korely knows it today"
                  : "What Korely knew on this date"}
                className={`rounded px-2 py-0.5 text-xs ${clock === c
                  ? "bg-secondary text-foreground"
                  : "text-muted-foreground hover:text-foreground"}`}
              >
                {c === "true" ? "True then" : "Known then"}
              </button>
            ))}
          </div>
          <input
            type="range"
            min={minMs}
            max={maxMs}
            step={Math.max(DAY, Math.round((maxMs - minMs) / 240))}
            value={asOfMs}
            onChange={(e) => setAsOf(Number(e.target.value))}
            className="h-1.5 flex-1 cursor-pointer accent-primary"
          />
          <span className="w-24 shrink-0 text-right font-mono text-xs tabular-nums">
            {fmtDate(asOfMs)}
          </span>
          <button
            onClick={() => setAsOf(null)}
            className="shrink-0 rounded border border-input px-2 py-0.5 text-xs text-muted-foreground hover:bg-secondary"
          >
            Now
          </button>
        </div>
      )}

      {error && (
        <p className="mb-4 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>
      )}

      {!loading && facts.length < total && (
        <p className="mb-4 rounded-md bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-400" role="status">
          Drawing the {facts.length.toLocaleString()} freshest of {total.toLocaleString()} facts
          {showHistory ? ", so time travel replays these only" : ""}. Filter by end user or
          family to see the rest.
        </p>
      )}

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : facts.length === 0 ? (
        <EmptyState icon={<Waypoints className="h-6 w-6" />} title="No graph yet">
          As your agents store memories, Korely extracts typed facts and links
          them here. This is the differentiator: a temporal knowledge graph, not
          a flat list. Add a memory or two and it fills in.
        </EmptyState>
      ) : (
        <div
          ref={stage}
          className={cn(enlarged && "fixed inset-0 z-50 flex flex-col bg-background p-3")}
        >
        <div
          className={cn(
            "grid grid-cols-1 gap-4 lg:grid-cols-[1fr_320px]",
            enlarged && "min-h-0 flex-1 grid-rows-[minmax(0,1fr)_auto] gap-3 lg:grid-rows-1",
          )}
        >
          {/* graph canvas */}
          <div
            className={cn(
              "relative flex min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-card",
              !enlarged && "h-[68vh] min-h-[420px]",
            )}
          >
            <div className="absolute right-3 top-3 z-10 flex flex-wrap justify-end gap-1.5">
              <div className="inline-flex overflow-hidden rounded-md border border-border bg-card/80 text-[11px] backdrop-blur">
                {[false, true].map((is3d) => (
                  <button
                    key={String(is3d)}
                    type="button"
                    onClick={() => setView3d(is3d)}
                    aria-pressed={view3d === is3d}
                    className={`px-2.5 py-1.5 ${view3d === is3d ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground"}`}
                  >
                    {is3d ? "3D" : "2D"}
                  </button>
                ))}
              </div>
              <button
                type="button"
                onClick={() => setFocusOnly((x) => !x)}
                aria-pressed={focusOnly}
                title="Focus: draw only the selected node and its strongest neighbours"
                className={cn(toolButton, "gap-1.5 px-2", focusOnly && "bg-accent text-foreground")}
              >
                <Scan className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">Focus</span>
              </button>
              {!view3d && (
                <button
                  type="button"
                  onClick={() => view.current?.toggleLayout()}
                  title={layoutRunning ? "Pause the layout" : "Let the layout settle again"}
                  className={cn(toolButton, "gap-1.5 px-2")}
                >
                  {layoutRunning ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                  <span className="hidden sm:inline">{layoutRunning ? "Pause" : "Resume"}</span>
                </button>
              )}
              <button type="button" onClick={() => view.current?.zoomIn()} aria-label="Zoom in" title="Zoom in" className={cn(toolButton, "w-8")}>
                <ZoomIn className="h-3.5 w-3.5" />
              </button>
              <button type="button" onClick={() => view.current?.zoomOut()} aria-label="Zoom out" title="Zoom out" className={cn(toolButton, "w-8")}>
                <ZoomOut className="h-3.5 w-3.5" />
              </button>
              <button type="button" onClick={() => view.current?.reset()} aria-label="Reset the view" title="Reset the view" className={cn(toolButton, "w-8")}>
                <LocateFixed className="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={() => setEnlarged((x) => !x)}
                aria-label={enlarged ? "Leave full screen" : "Full screen"}
                title={enlarged ? "Leave full screen (Esc)" : "Full screen"}
                className={cn(toolButton, "w-8")}
              >
                {enlarged ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
              </button>
            </div>
            <div className="min-h-0 w-full flex-1">
              {view3d ? (
                <Suspense fallback={<p className="p-4 text-sm text-muted-foreground">Loading the 3D view…</p>}>
                  <FactGraph3D
                    ref={view}
                    nodes={nodes}
                    edges={edges}
                    selectedId={selected}
                    onSelect={setSelected}
                    familyColors={FAMILY_COLORS}
                    focusOnly={focusOnly}
                  />
                </Suspense>
              ) : (
                <FactGraph
                  ref={view}
                  nodes={nodes}
                  edges={edges}
                  selectedId={selected}
                  onSelect={setSelected}
                  familyColors={FAMILY_COLORS}
                  focusOnly={focusOnly}
                  onLeaveFocus={() => setFocusOnly(false)}
                  onLayoutRunning={setLayoutRunning}
                />
              )}
            </div>
            {/* legend */}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border px-3 py-2 text-[11px] text-muted-foreground">
              <span>{nodes.length} nodes</span>
              <span>{edges.length} facts</span>
              <span>{endUsers} end {endUsers === 1 ? "user" : "users"}</span>
              <span className="text-border">|</span>
              {families.map((f) => (
                <span key={f.name} className="inline-flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full" style={{ background: familyColor(f.name) }} />
                  {f.name}
                </span>
              ))}
              <span className="ml-auto inline-flex items-center gap-1">
                <svg width="16" height="6"><line x1="0" y1="3" x2="16" y2="3" stroke="currentColor" strokeOpacity="0.3" /></svg>
                closed (fainter)
              </span>
            </div>
          </div>

          {/* detail panel */}
          <aside className={cn("rounded-xl border border-border bg-card p-4 lg:max-h-[68vh] lg:overflow-y-auto", enlarged && "max-h-[42vh] overflow-y-auto lg:max-h-none", enlarged && !selectedNode && "hidden lg:block")}>
            {!selectedNode ? (
              <div className="flex h-full min-h-40 flex-col items-center justify-center text-center text-sm text-muted-foreground">
                <Waypoints className="mb-2 h-5 w-5" />
                Click any node to see its facts and the memories that mention it.
              </div>
            ) : (
              <div>
                <div className="mb-3 flex items-start justify-between gap-2">
                  <div>
                    <p className="text-sm font-semibold leading-tight">{selectedNode.label}</p>
                    <div className="mt-1 flex flex-wrap gap-1">
                      <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                        {selectedNode.kind}
                      </span>
                      {selectedNode.subjectType && selectedNode.subjectType !== "unknown" && (
                        <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          {selectedNode.subjectType}
                        </span>
                      )}
                    </div>
                  </div>
                  <button type="button" onClick={() => setSelected(null)} aria-label="Close" className="text-muted-foreground hover:text-foreground">
                    <X className="h-4 w-4" />
                  </button>
                </div>

                <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Facts ({selectedFacts.length})
                </p>
                {factEdit.err && (
                  <p className="mb-2 text-[11px] text-destructive">{factEdit.err}</p>
                )}
                <ul className="mb-4 space-y-1.5">
                  {selectedFacts.map((f) => {
                    const isEditing = factEdit.editing === f.id;
                    const isBusy = factEdit.busy === f.id;
                    if (isEditing) {
                      return (
                        <li key={f.id} className="space-y-1 rounded-md border border-border p-1.5">
                          <input
                            value={factEdit.draft.subject}
                            onChange={(e) => factEdit.setDraft((d) => ({ ...d, subject: e.target.value }))}
                            className="w-full rounded border border-input bg-background px-1.5 py-1 text-xs outline-hidden focus:ring-1 focus:ring-ring"
                            placeholder="subject"
                          />
                          <input
                            value={factEdit.draft.predicate}
                            onChange={(e) => factEdit.setDraft((d) => ({ ...d, predicate: e.target.value }))}
                            className="w-full rounded border border-input bg-background px-1.5 py-1 font-mono text-[11px] outline-hidden focus:ring-1 focus:ring-ring"
                            placeholder="predicate"
                          />
                          <input
                            value={factEdit.draft.object}
                            onChange={(e) => factEdit.setDraft((d) => ({ ...d, object: e.target.value }))}
                            className="w-full rounded border border-input bg-background px-1.5 py-1 text-xs outline-hidden focus:ring-1 focus:ring-ring"
                            placeholder="object"
                          />
                          <div className="flex justify-end gap-1">
                            <button type="button" disabled={isBusy} onClick={() => factEdit.save(f)} className="rounded p-1 text-emerald-500 hover:bg-secondary disabled:opacity-50" title="Save correction">
                              <Check className="h-3.5 w-3.5" />
                            </button>
                            <button type="button" onClick={factEdit.cancel} className="rounded p-1 text-muted-foreground hover:bg-secondary" title="Cancel">
                              <X className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </li>
                      );
                    }
                    return (
                      <li key={f.id} className={`group flex items-start gap-1 text-xs leading-relaxed ${f.closed ? "opacity-55" : ""}`}>
                        <span className="mt-1 inline-block h-2 w-2 shrink-0 rounded-full" style={{ background: familyColor(f.predicate_family) }} />
                        <span className="flex-1">
                          <span className="font-medium">{f.subject}</span>{" "}
                          <span className="font-mono text-muted-foreground">{f.predicate}</span>{" "}
                          <span className={`font-medium ${f.closed ? "line-through" : ""}`}>{f.object}</span>
                          {(f.dates.from || f.closed) && (
                            <span className="block text-[10px] tabular-nums text-muted-foreground">
                              {/* A line breaks between the dates, never inside one. */}
                              {f.dates.from && <span className="whitespace-nowrap">from {f.dates.from}</span>}
                              {f.closed && f.dates.from && " · "}
                              {f.closed && (
                                <span className="whitespace-nowrap text-destructive">
                                  {closedLabel(f)}{f.dates.ended && ` ${f.dates.ended}`}
                                </span>
                              )}
                              {f.closed && f.dates.knownOn && " · "}
                              {f.closed && f.dates.knownOn && (
                                <span className="whitespace-nowrap" title={KNOWN_ON}>known on {f.dates.knownOn}</span>
                              )}
                            </span>
                          )}
                        </span>
                        {!f.closed && (
                          <span className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
                            <button type="button" disabled={isBusy} onClick={() => factEdit.startEdit(f)} className="rounded p-0.5 text-muted-foreground hover:text-foreground disabled:opacity-50" title="Correct">
                              <Pencil className="h-3 w-3" />
                            </button>
                            <button type="button" disabled={isBusy} onClick={() => factEdit.forget(f)} className="rounded p-0.5 text-muted-foreground hover:text-destructive disabled:opacity-50" title="Forget">
                              <Trash2 className="h-3 w-3" />
                            </button>
                          </span>
                        )}
                      </li>
                    );
                  })}
                </ul>

                <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                  Mentioned in {memIds.length} {memIds.length === 1 ? "memory" : "memories"}
                </p>
                <ul className="space-y-2">
                  {memIds.map((id) => {
                    const m = mem.get(id);
                    return (
                      <li key={id} className="rounded-md border border-border bg-background px-2.5 py-2 text-xs">
                        {m === "loading" || m === undefined ? (
                          <span className="text-muted-foreground">Loading…</span>
                        ) : m === "error" ? (
                          <span className="text-muted-foreground">Memory unavailable</span>
                        ) : (
                          <div>
                            <div className="mb-1 flex items-center gap-1.5 text-[10px] text-muted-foreground">
                              <FileText className="h-3 w-3" />
                              {m.user_id && (
                                <span className="inline-flex items-center gap-0.5">
                                  <UserIcon className="h-2.5 w-2.5" />
                                  {m.user_id}
                                </span>
                              )}
                              {m.created_at && <span>· {fmtDate(Date.parse(m.created_at))}</span>}
                            </div>
                            <p className="leading-relaxed">
                              {m.content.length > 180 ? m.content.slice(0, 179) + "…" : m.content}
                            </p>
                          </div>
                        )}
                      </li>
                    );
                  })}
                  {memIds.length === 0 && (
                    <li className="text-xs text-muted-foreground">
                      No source memory linked (e.g. a fact written directly via POST /v1/facts).
                    </li>
                  )}
                </ul>
              </div>
            )}
          </aside>
        </div>
        </div>
      )}
    </div>
  );
}
