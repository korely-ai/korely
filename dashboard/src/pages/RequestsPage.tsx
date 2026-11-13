import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, X, ListFilter, ChevronDown, Check } from "lucide-react";
import { PageHeader, EmptyState, HelpNote, ProjectScopeBanner } from "@/components/PageShell";
import {
  requestsApi,
  type RequestRow,
  type RequestsSummary,
} from "@/lib/agentsApi";
import { DOCS } from "@/lib/docsLinks";
import { cn } from "@/lib/cn";
import { ModalShell } from "@/components/Modal";
import { useMode } from "@/contexts/ModeContext";
import { useLoad } from "@/lib/useLoad";
import { useDismiss } from "@/lib/useDismiss";
import { Loaded } from "@/components/LoadState";
import { useProjectScope } from "@/contexts/ProjectContext";

// The Requests page, Mem0-style: keep the Total/Write/Read rollup, but make the
// granular per-endpoint types (ADD / SEARCH / LIST / CONTEXT …) first-class —
// they're the same data the backend already logs (tool_name). A timeline bar
// chart sits below the rollup; clicking a row opens a detail modal.

// Write or read is the server's (`kind` on every row, the same rule as the
// summary's counts): guessing it here from the endpoint's suffix showed a
// fact forgotten or corrected as a read, next to a summary that counted it as
// a write (audit of 17 Oct 2026).
const actionOf = (ep: string) => (ep.includes(":") ? ep.split(":").pop()! : ep);
const resourceOf = (ep: string) => (ep.includes(":") ? ep.split(":")[0] : "");
const typeLabel = (ep: string) => actionOf(ep).replace(/_/g, " ").toUpperCase();

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmtDay(iso: string) {
  const [, m, d] = iso.split("-");
  return `${Number(d)} ${MONTHS[Number(m) - 1] ?? ""}`;
}
function relTime(iso: string) {
  if (!iso) return "";
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const days = Math.floor(h / 24);
  if (days < 30) return `${days}d ago`;
  return `${Math.floor(days / 30)}mo ago`;
}
function fullTime(iso: string) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
}

type Kind = "all" | "write" | "read";
const RANGES = [
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
] as const;

export function RequestsPage() {
  const { hosted } = useMode();
  // The project chosen at the top, or every project (contexts/ProjectContext.tsx):
  // the calls are read off the trail, where each one carries its project.
  const scope = useProjectScope();
  const [summary, setSummary] = useState<RequestsSummary | null>(null);
  const [rows, setRows] = useState<RequestRow[]>([]);
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const [kind, setKind] = useState<Kind>("all");
  const [endpoint, setEndpoint] = useState<string | null>(null); // granular type filter
  const [selected, setSelected] = useState<RequestRow | null>(null);

  useEffect(() => {
    Promise.all([requestsApi.summary(scope), requestsApi.list(scope, 200)])
      .then(([s, l]) => {
        setSummary(s);
        setRows(l.requests);
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }, [scope]);

  // The series, read again whenever the range changes (lib/useLoad.ts): only
  // the newest range's answer lands, and a failed read says so. It ended in
  // `.catch(() => setDaily(null))`, and the chart stayed on "Loading…" for
  // good (audit of 17 Oct 2026, as on the Dashboard).
  const daily = useLoad(
    () => requestsApi.daily(scope, days),
    [scope, days],
    "The daily requests could not be read.",
  );

  // Switching the read/write rollup clears a granular type that no longer fits.
  const pickKind = (k: Kind) => {
    setKind(k);
    const chip = (summary?.by_endpoint ?? []).find((e) => e.endpoint === endpoint);
    if (endpoint && k !== "all" && chip && chip.kind !== k) setEndpoint(null);
  };

  const chips = useMemo(
    () => (summary?.by_endpoint ?? []).filter((e) => kind === "all" || e.kind === kind),
    [summary, kind],
  );

  const filteredRows = useMemo(
    () =>
      rows.filter(
        (r) =>
          (kind === "all" || r.kind === kind) &&
          (!endpoint || r.endpoint === endpoint),
      ),
    [rows, kind, endpoint],
  );

  const bars = useMemo(
    () =>
      (daily.data?.buckets ?? []).map((b) => ({
        date: b.date,
        value: kind === "write" ? b.writes : kind === "read" ? b.reads : b.writes + b.reads,
      })),
    [daily.data, kind],
  );

  const empty = !loading && (!summary || summary.total === 0);

  return (
    <div>
      <PageHeader
        title="Requests"
        subtitle="Every API call your agents make, by type and over time. Counted in requests, never tokens."
        docs={DOCS.requests}
      />

      <ProjectScopeBanner
        oneProjectNote={hosted ? "Your plan counts the calls of every project together." : undefined}
      />

      <HelpNote title="Types, reads & writes">
        <p className="mb-2">
          Each call has a <span className="text-foreground">type</span>:{" "}
          <code>ADD</code>, <code>SEARCH</code>, <code>LIST</code>,{" "}
          <code>CONTEXT</code>, <code>CREATE</code> … the same granular events
          you'd expect. <span className="text-foreground">Writes</span> change
          memory: memories added, edited or imported, facts written or
          corrected, forgets and erasures;{" "}
          <span className="text-foreground">reads</span> (search / list /
          context / facts) just retrieve.{" "}
          {hosted ? (
            <>
              Your plan counts both: the month's writes and reads against its
              allowance (a memory counts one write per 6,000 characters), with
              limits per minute, hour and day. Plan &amp; usage has the numbers.
            </>
          ) : (
            <>
              Nothing here is metered: the only ceiling is the per-minute limit
              an operator can set (<code>RATE_LIMIT_PER_MINUTE</code>, off by
              default), and it counts both alike.
            </>
          )}
        </p>
        <p>
          Total / Writes / Reads is the rollup; the chips below it are the
          per-type detail. We record the call's type and time; per-call latency
          and status aren't tracked yet.
        </p>
      </HelpNote>

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : error ? (
        <EmptyState icon={<Activity className="h-6 w-6" />} title="Requests unavailable">
          Try again in a moment.
        </EmptyState>
      ) : empty ? (
        <EmptyState icon={<Activity className="h-6 w-6" />} title="No requests yet">
          Calls to the memory API show up here, by type and over time.
        </EmptyState>
      ) : (
        <div className="space-y-5">
          {/* Total / Writes / Reads — kept, now clickable filters. */}
          <div className="grid grid-cols-3 gap-3">
            <StatFilter
              label="Total requests"
              value={summary!.total}
              active={kind === "all"}
              onClick={() => pickKind("all")}
            />
            <StatFilter
              label="Writes"
              value={summary!.writes}
              tone="write"
              active={kind === "write"}
              onClick={() => pickKind("write")}
            />
            <StatFilter
              label="Reads"
              value={summary!.reads}
              tone="read"
              active={kind === "read"}
              onClick={() => pickKind("read")}
            />
          </div>

          {/* Timeline. */}
          <div className="rounded-lg border border-border bg-card p-5">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div>
                <p className="text-sm font-medium">Activity</p>
                <p className="text-xs text-muted-foreground">
                  {kind === "all" ? "All requests" : kind === "write" ? "Writes" : "Reads"}
                  {endpoint ? ` · ${typeLabel(endpoint)}` : ""} · last {days} days
                </p>
              </div>
              <div className="inline-flex rounded-lg border border-border bg-secondary/40 p-1">
                {RANGES.map((r) => (
                  <button
                    key={r.days}
                    type="button"
                    onClick={() => setDays(r.days)}
                    className={cn(
                      "rounded-md px-3 py-1 text-xs font-medium transition-colors",
                      days === r.days
                        ? "bg-background text-foreground shadow-xs"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
            </div>
            <Loaded load={daily} className="my-12 text-center">
              {() => <BarTimeline bars={bars} />}
            </Loaded>
          </div>

          {/* Granular type filter — collected into one tidy menu. */}
          <div className="flex items-center gap-2">
            <TypeFilterMenu options={chips} value={endpoint} onChange={setEndpoint} />
            {endpoint && (
              <button
                type="button"
                onClick={() => setEndpoint(null)}
                className="text-xs text-muted-foreground transition-colors hover:text-foreground"
              >
                Clear
              </button>
            )}
          </div>

          {/* Table. */}
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-secondary/50 text-left text-xs uppercase tracking-wider text-muted-foreground">
                <tr>
                  <th className="px-4 py-2.5 font-medium">Time</th>
                  <th className="px-4 py-2.5 font-medium">Type</th>
                  <th className="px-4 py-2.5 font-medium" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {filteredRows.length === 0 ? (
                  <tr>
                    <td colSpan={3} className="px-4 py-8 text-center text-sm text-muted-foreground">
                      No calls match this filter.
                    </td>
                  </tr>
                ) : (
                  filteredRows.map((r, i) => (
                    <tr
                      key={i}
                      onClick={() => setSelected(r)}
                      className="cursor-pointer transition-colors hover:bg-secondary/40"
                    >
                      <td className="px-4 py-2.5 text-muted-foreground" title={fullTime(r.at)}>
                        {relTime(r.at)}
                      </td>
                      <td className="px-4 py-2.5">
                        <TypeBadge endpoint={r.endpoint} kind={r.kind} />
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <span className="text-xs text-muted-foreground">Details →</span>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-muted-foreground">
            Showing the {rows.length.toLocaleString()} most recent calls
            {kind !== "all" || endpoint ? ` · ${filteredRows.length.toLocaleString()} match the filter` : ""}.
          </p>
        </div>
      )}

      {selected && <RequestModal row={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function StatFilter({
  label,
  value,
  tone,
  active,
  onClick,
}: {
  label: string;
  value: number;
  tone?: "write" | "read";
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-lg border bg-card p-4 text-left transition-colors",
        active ? "border-foreground/30 ring-1 ring-foreground/10" : "border-border hover:border-foreground/20",
      )}
    >
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={cn("mt-1 text-2xl font-semibold tabular-nums", tone === "write" && "text-primary")}>
        {value.toLocaleString()}
      </p>
    </button>
  );
}

/** One tidy filter button + popover menu, instead of a sprawling chip row. */
function TypeFilterMenu({
  options,
  value,
  onChange,
}: {
  options: { endpoint: string; count: number; kind: string }[];
  value: string | null;
  onChange: (endpoint: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useDismiss(ref, open, () => setOpen(false));

  const activeLabel = value
    ? `${typeLabel(value)}${resourceOf(value) ? ` · ${resourceOf(value)}` : ""}`
    : "All types";

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="inline-flex items-center gap-2 rounded-md border border-border bg-background px-3 py-1.5 text-sm transition-colors hover:bg-secondary"
      >
        <ListFilter className="h-4 w-4 text-muted-foreground" />
        <span className="font-medium">{activeLabel}</span>
        {value && (
          <span className="rounded-full bg-primary/15 px-1.5 text-[10px] font-medium text-primary">1</span>
        )}
        <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
      </button>

      {open && (
        <div className="absolute left-0 top-full z-50 mt-1.5 w-64 overflow-hidden rounded-lg border border-border bg-card shadow-lg">
          <ul className="max-h-80 overflow-y-auto py-1">
            <li>
              <button
                type="button"
                aria-current={value === null}
                onClick={() => {
                  onChange(null);
                  setOpen(false);
                }}
                className={cn(
                  "flex w-full items-center justify-between px-3 py-2 text-left text-sm transition-colors hover:bg-secondary",
                  value === null && "bg-secondary/60",
                )}
              >
                <span className="font-medium">All types</span>
                {value === null && <Check className="h-4 w-4 text-primary" />}
              </button>
            </li>
            {options.map((o) => {
              const active = value === o.endpoint;
              return (
                <li key={o.endpoint}>
                  <button
                    type="button"
                    aria-current={active}
                    onClick={() => {
                      onChange(o.endpoint);
                      setOpen(false);
                    }}
                    className={cn(
                      "flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-secondary",
                      active && "bg-secondary/60",
                    )}
                  >
                    <span className="flex min-w-0 items-center gap-2">
                      <span
                        className={cn(
                          "h-1.5 w-1.5 shrink-0 rounded-full",
                          o.kind === "write" ? "bg-primary" : "bg-primary/40",
                        )}
                      />
                      <span className="font-medium">{typeLabel(o.endpoint)}</span>
                      {resourceOf(o.endpoint) && (
                        <span className="truncate text-xs text-muted-foreground">
                          {resourceOf(o.endpoint)}
                        </span>
                      )}
                    </span>
                    <span className="flex shrink-0 items-center gap-2">
                      <span className="tabular-nums text-xs text-muted-foreground">
                        {o.count.toLocaleString()}
                      </span>
                      {active && <Check className="h-3.5 w-3.5 text-primary" />}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

function TypeBadge({ endpoint, kind }: { endpoint: string; kind: "write" | "read" }) {
  const resource = resourceOf(endpoint);
  return (
    <span className="inline-flex items-center gap-2">
      <span
        className={cn(
          "rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
          kind === "write" ? "bg-primary/15 text-primary" : "bg-secondary text-muted-foreground",
        )}
      >
        {typeLabel(endpoint)}
      </span>
      {resource && <span className="font-mono text-xs text-muted-foreground">{resource}</span>}
    </span>
  );
}

/** Dependency-free bar histogram, one bar per day, with a hover tooltip. */
function BarTimeline({ bars }: { bars: { date: string; value: number }[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(1, ...bars.map((b) => b.value));
  const total = bars.reduce((s, b) => s + b.value, 0);
  const n = bars.length;
  const labelEvery = Math.max(1, Math.ceil(n / 6));

  if (total === 0) {
    return (
      <div className="flex h-44 items-center justify-center rounded-md border border-dashed border-border text-sm text-muted-foreground">
        No requests in this range yet.
      </div>
    );
  }

  return (
    <div>
      <div className="flex h-44 items-end gap-px">
        {bars.map((b, i) => {
          const h = (b.value / max) * 100;
          return (
            <div
              key={b.date}
              className="group relative flex h-full flex-1 items-end"
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
            >
              <div
                className={cn(
                  "w-full rounded-t-sm transition-colors",
                  hover === i ? "bg-primary" : "bg-primary/55",
                )}
                style={{ height: `${b.value > 0 ? Math.max(h, 2) : 0}%` }}
              />
              {hover === i && (
                <div className="pointer-events-none absolute -top-9 left-1/2 z-10 -translate-x-1/2 whitespace-nowrap rounded-md border border-border bg-card px-2 py-1 text-[11px] shadow-md">
                  <span className="font-medium">{b.value.toLocaleString()}</span>{" "}
                  <span className="text-muted-foreground">· {fmtDay(b.date)}</span>
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div className="mt-1.5 flex justify-between text-[10px] text-muted-foreground">
        {bars.map((b, i) =>
          i % labelEvery === 0 || i === n - 1 ? <span key={b.date}>{fmtDay(b.date)}</span> : null,
        )}
      </div>
    </div>
  );
}

function RequestModal({ row, onClose }: { row: RequestRow; onClose: () => void }) {
  const kind = row.kind;
  return (
    <ModalShell label="Request details" onClose={onClose}>
      <div className="absolute inset-0" onClick={onClose} />
      <div
        className="w-full max-w-md rounded-lg border border-border bg-card p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold">Request</h2>
            <TypeBadge endpoint={row.endpoint} kind={row.kind} />
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <dl className="divide-y divide-border rounded-md border border-border">
          <Field label="Type" value={`${typeLabel(row.endpoint)}${resourceOf(row.endpoint) ? ` (${resourceOf(row.endpoint)})` : ""}`} />
          <Field label="Endpoint" value={<code className="font-mono text-xs">{row.endpoint}</code>} />
          <Field
            label="Counts as"
            value={
              <span className={cn("text-xs font-medium", kind === "write" ? "text-primary" : "text-muted-foreground")}>
                {kind === "write" ? "Write (changes memory)" : "Read (retrieves only)"}
              </span>
            }
          />
          <Field label="Requested at" value={fullTime(row.at)} />
          <Field label="When" value={relTime(row.at)} />
        </dl>

        <p className="mt-3 text-xs text-muted-foreground">
          Per-call latency and status aren't recorded yet: we log the call's type
          and time.
        </p>
      </div>
    </ModalShell>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 px-3 py-2.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-right text-sm">{value}</dd>
    </div>
  );
}
