import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { ChevronDown, ChevronRight, ScrollText, Waypoints } from "lucide-react";
import { PageHeader, EmptyState, HelpNote, ProjectScopeBanner } from "@/components/PageShell";
import { auditApi, type AuditEvent, type AuditReadItems } from "@/lib/agentsApi";
import { useLatest } from "@/lib/latest";
import { DOCS } from "@/lib/docsLinks";
import { useProjectScope } from "@/contexts/ProjectContext";

// The Audit log (bussola 2e, 2026-10-01): the trail of one end user in time
// order, for the demo question "who wrote this memory, and what did the agent
// know when it decided?". Every event says who acted (a key, over REST or
// MCP), what it did, and for a read what it returned, by id. The ids become
// text only when an event is opened, read from the live tables: the trail
// itself holds no content, so a memory erased since shows as erased.

// The filter offers the verbs the server says the trail records (GET
// /admin/audit, `actions`), named as the badges name them. A copy of seven
// of them lived here, and fact_invalidate, tenant_create, key_change,
// setting_change and login could not be picked (audit of 17 Oct 2026).
function actionName(action: string) {
  return action.split("_").join(" ");
}

const PAGE = 100;

function fullTime(iso: string) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
}

function readCount(e: AuditEvent) {
  const m = Number(e.meta?.memories_read ?? e.read?.memories?.length ?? 0);
  const f = Number(e.meta?.facts_read ?? e.read?.facts?.length ?? 0);
  return { m, f };
}

function readSummary(e: AuditEvent) {
  const { m, f } = readCount(e);
  const parts = [];
  if (m) parts.push(`${m} ${m === 1 ? "memory" : "memories"}`);
  if (f) parts.push(`${f} ${f === 1 ? "fact" : "facts"}`);
  return parts.length ? `read ${parts.join(", ")}` : "read nothing";
}

function badge(action: string, result: string) {
  if (result !== "ok") return "bg-red-500/10 text-red-600 dark:text-red-400";
  if (action === "read") return "bg-secondary text-muted-foreground";
  if (action === "erase") return "bg-amber-500/10 text-amber-700 dark:text-amber-400";
  // A memory forgotten and a fact closed are kept (out of every read, in
  // history): a colour of their own, never the erasure's (1.0.23).
  if (action === "forget" || action === "fact_invalidate") {
    return "bg-orange-500/10 text-orange-700 dark:text-orange-400";
  }
  return "bg-blue-500/10 text-blue-700 dark:text-blue-400";
}

export function AuditPage() {
  // The project chosen at the top, as GET /v1/audit reads it for a key of it,
  // or every project (contexts/ProjectContext.tsx).
  const scope = useProjectScope();
  const [userId, setUserId] = useState("");
  const [applied, setApplied] = useState("");
  const [action, setAction] = useState("");
  const [oldestFirst, setOldestFirst] = useState(false);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [actions, setActions] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [open, setOpen] = useState<number | null>(null);

  // A filter changed while "Load older" was on its way: that page belongs to
  // the old filter and must not be appended to the new one.
  const ticket = useLatest();
  const load = useCallback((offset: number) => {
    const current = ticket();
    setLoading(true);
    setError(false);
    auditApi
      .list(scope, { user_id: applied || undefined, action: action || undefined, limit: PAGE, offset })
      .then((p) => {
        if (!current()) return;
        setEvents((prev) => (offset === 0 ? p.events : [...prev, ...p.events]));
        setTotal(p.total);
        setActions(p.actions ?? []);
      })
      .catch(() => current() && setError(true))
      .finally(() => current() && setLoading(false));
  }, [scope, applied, action, ticket]);

  useEffect(() => {
    setOpen(null);
    load(0);
  }, [load]);

  const shown = oldestFirst ? [...events].reverse() : events;

  return (
    <div>
      <PageHeader
        title="Audit log"
        subtitle="Who did what, and what each read returned. Pick an end user to see their trail in time order: with the date of an event, the graph shows what the agent could know then."
        docs={DOCS.audit}
      />

      <ProjectScopeBanner
        oneProjectNote="Events that belong to no project, such as a sign-in, are under All projects."
      />

      <HelpNote title="What is recorded">
        <p className="mb-2">
          Every call to the API and to MCP: the key that made it, the end user and the memory it
          touched, and for a read the memories and facts it returned, by id (the first 100 of each,
          the full count next to them).
        </p>
        <p className="text-muted-foreground">
          Never the content. The text you see when you open an event is read now from the memories
          and facts themselves, so an erased memory shows as erased here too.
        </p>
      </HelpNote>

      <form
        className="mb-4 flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          setApplied(userId.trim());
        }}
      >
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          End user
          <input
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            placeholder="all end users"
            className="w-56 rounded-md border border-border bg-background px-2.5 py-1.5 font-mono text-sm text-foreground"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Action
          <select
            value={action}
            onChange={(e) => setAction(e.target.value)}
            className="rounded-md border border-border bg-background px-2.5 py-1.5 text-sm text-foreground"
          >
            <option value="">Everything</option>
            {actions.map((a) => (
              <option key={a} value={a}>
                {actionName(a)}
              </option>
            ))}
          </select>
        </label>
        <button
          type="submit"
          className="rounded-md border border-border px-3 py-1.5 text-sm transition-colors hover:bg-secondary"
        >
          Show
        </button>
        <label className="ml-auto flex items-center gap-2 text-sm text-muted-foreground">
          <input type="checkbox" checked={oldestFirst} onChange={(e) => setOldestFirst(e.target.checked)} />
          Oldest first
        </label>
      </form>

      {error ? (
        <EmptyState icon={<ScrollText className="h-6 w-6" />} title="The audit log could not be read">
          Try again in a moment; if it keeps failing, the API log says why.
        </EmptyState>
      ) : !loading && events.length === 0 ? (
        <EmptyState icon={<ScrollText className="h-6 w-6" />} title="Nothing recorded yet">
          {applied ? (
            <>No event touched <code className="font-mono">{applied}</code> with these filters.</>
          ) : (
            "Events appear here as soon as an agent calls the API or MCP."
          )}
        </EmptyState>
      ) : (
        <>
          <p className="mb-2 text-xs text-muted-foreground">
            {total.toLocaleString()} events{applied ? ` for ${applied}` : ""}, newest loaded first
          </p>
          <ul className="space-y-1.5">
            {shown.map((e, i) => (
              <EventRow
                key={`${e.ts}-${i}`}
                event={e}
                open={open === i}
                onToggle={() => setOpen(open === i ? null : i)}
                onUser={(u) => {
                  setUserId(u);
                  setApplied(u);
                }}
              />
            ))}
          </ul>
          {events.length < total && (
            <button
              type="button"
              disabled={loading}
              onClick={() => load(events.length)}
              className="mt-3 rounded-md border border-border px-3 py-1.5 text-sm transition-colors hover:bg-secondary disabled:opacity-50"
            >
              {loading ? "Loading…" : "Load older events"}
            </button>
          )}
        </>
      )}
    </div>
  );
}

function EventRow({
  event: e,
  open,
  onToggle,
  onUser,
}: {
  event: AuditEvent;
  open: boolean;
  onToggle: () => void;
  onUser: (u: string) => void;
}) {
  const hasRead = Boolean(e.read && ((e.read.memories?.length ?? 0) || (e.read.facts?.length ?? 0)));
  const day = e.ts.slice(0, 10);
  return (
    <li className="rounded-lg border border-border bg-card">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-left xl:flex-nowrap"
      >
        {open ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
        )}
        <span className="shrink-0 text-xs tabular-nums text-muted-foreground xl:w-44" title={e.ts}>
          {fullTime(e.ts)}
        </span>
        <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium uppercase ${badge(e.action, e.result)}`}>
          {e.result === "ok" ? actionName(e.action) : `${actionName(e.action)} ${e.result}`}
        </span>
        <code className="min-w-0 break-all font-mono text-xs xl:shrink-0 xl:break-normal">{e.endpoint}</code>
        <span className="shrink-0 rounded border border-border px-1.5 text-[11px] uppercase text-muted-foreground">
          {e.actor}
        </span>
        {e.user_id && <code className="truncate font-mono text-xs text-muted-foreground">{e.user_id}</code>}
        <span className="ml-auto shrink-0 text-xs text-muted-foreground">
          {e.read ? readSummary(e) : e.target_id ?? ""}
        </span>
      </button>
      {open && (
        <div className="border-t border-border px-4 py-3 text-sm">
          <dl className="mb-3 grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 text-xs">
            {e.user_id && (
              <>
                <dt className="text-muted-foreground">End user</dt>
                <dd className="flex items-center gap-3">
                  <button type="button" className="font-mono underline underline-offset-2" onClick={() => onUser(e.user_id!)}>
                    {e.user_id}
                  </button>
                  {/* What the agent knew that day (`known_as_of`), the question an
                      audit asks, and what was true that day as known now. */}
                  <Link
                    to={`/graph?user_id=${encodeURIComponent(e.user_id)}&known_as_of=${day}`}
                    className="inline-flex items-center gap-1 text-muted-foreground hover:text-foreground"
                  >
                    <Waypoints className="h-3.5 w-3.5" /> what Korely knew on {day}
                  </Link>
                  <Link
                    to={`/graph?user_id=${encodeURIComponent(e.user_id)}&as_of=${day}`}
                    className="inline-flex items-center gap-1 text-muted-foreground hover:text-foreground"
                  >
                    what was true on {day}
                  </Link>
                </dd>
              </>
            )}
            {e.target_id && (
              <>
                <dt className="text-muted-foreground">Acted on</dt>
                <dd>
                  <IdLink id={e.target_id} />
                </dd>
              </>
            )}
            {e.api_key_id && (
              <>
                <dt className="text-muted-foreground">Key</dt>
                <dd className="font-mono">{e.api_key_id}</dd>
              </>
            )}
            {e.ip && (
              <>
                <dt className="text-muted-foreground">From</dt>
                <dd className="font-mono">{e.ip}</dd>
              </>
            )}
            {e.meta?.as_of != null && (
              <>
                <dt className="text-muted-foreground">Asked as of</dt>
                <dd className="font-mono">{String(e.meta.as_of)}</dd>
              </>
            )}
            {e.meta?.known_as_of != null && (
              <>
                <dt className="text-muted-foreground">Asked as known on</dt>
                <dd className="font-mono">{String(e.meta.known_as_of)}</dd>
              </>
            )}
          </dl>
          {hasRead ? <WhatItRead event={e} /> : e.read ? <p className="text-xs text-muted-foreground">The read returned nothing.</p> : null}
        </div>
      )}
    </li>
  );
}

function IdLink({ id }: { id: string }) {
  if (id.startsWith("mem_")) {
    return (
      <Link to={`/memories?open=${id}`} className="font-mono text-xs underline underline-offset-2">
        {id}
      </Link>
    );
  }
  return <code className="font-mono text-xs">{id}</code>;
}

function WhatItRead({ event: e }: { event: AuditEvent }) {
  const [items, setItems] = useState<AuditReadItems | null>(null);
  const [failed, setFailed] = useState(false);
  const mems = e.read?.memories ?? [];
  const facts = e.read?.facts ?? [];
  const { m, f } = readCount(e);

  useEffect(() => {
    auditApi.read(mems, facts).then(setItems).catch(() => setFailed(true));
    // Once per event: `mems` and `facts` are read from it.
  }, [e.ts]);

  if (failed) return <p className="text-xs text-muted-foreground">What it read could not be loaded.</p>;
  if (!items) return <p className="text-xs text-muted-foreground">Loading what it read…</p>;
  return (
    <div className={`grid gap-4 ${mems.length && facts.length ? "md:grid-cols-2" : ""}`}>
      {mems.length > 0 && (
        <div>
          <p className="mb-1.5 text-xs font-medium">
            Memories{m > mems.length ? ` (first ${mems.length} of ${m})` : ""}
          </p>
          <ul className="space-y-1">
            {mems.map((id) => {
              const it = items.memories[id];
              return (
                <li key={id} className="text-xs">
                  <IdLink id={id} />{" "}
                  {it ? (
                    <span className={it.deleted ? "italic text-muted-foreground" : "text-muted-foreground"}>
                      {it.deleted ? "(deleted) " : ""}
                      {it.text}
                    </span>
                  ) : (
                    <span className="italic text-muted-foreground">erased since</span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {facts.length > 0 && (
        <div>
          <p className="mb-1.5 text-xs font-medium">
            Facts{f > facts.length ? ` (first ${facts.length} of ${f})` : ""}
          </p>
          <ul className="space-y-1">
            {facts.map((id) => {
              const it = items.facts[id];
              return (
                <li key={id} className="text-xs">
                  <code className="font-mono">{id}</code>{" "}
                  {it ? (
                    <span className="text-muted-foreground">
                      {it.line}
                      {it.invalid_at ? ` (no longer true since ${it.invalid_at.slice(0, 10)})` : ""}
                    </span>
                  ) : (
                    <span className="italic text-muted-foreground">erased since</span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
