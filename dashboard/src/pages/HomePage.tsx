import { useState } from "react";
import {
  Database,
  Network,
  Search,
  PlusCircle,
  KeyRound,
  ArrowRight,
  ArrowUpRight,
  Download,
  Play,
  Waypoints,
  BookOpen,
} from "lucide-react";
import { Link } from "react-router";

import { useTarget } from "@/lib/target";
import { DOCS } from "@/lib/docsLinks";
import { PageHeader, ProjectScopeBanner } from "@/components/PageShell";
import { ActivityChart } from "@/components/ActivityChart";
import {
  errorMessage,
  usageApi,
  requestsApi,
  memoriesApi,
  keysApi,
  type RequestsSummary,
  type AgentKeyCreated,
} from "@/lib/agentsApi";
import { useLoad } from "@/lib/useLoad";
import { LoadNote, Loaded } from "@/components/LoadState";
import { CopyButton } from "@/components/CopyButton";
import { KeysEndingNote } from "@/components/KeyEnds";
import { endingSoon } from "@/lib/keyLife";
import { useAuth } from "@/contexts/AuthContext";
import { useMode } from "@/contexts/ModeContext";
import { useProjectScope } from "@/contexts/ProjectContext";
import { cloud } from "@/lib/cloud";

// This month's writes on the Cloud (hosted/usage.tsx); a build without the
// Cloud's pieces never shows the card. Chosen once, so the hooks run in the
// same order on every render.
const useWritesThisMonth: () => { value: string; sub?: string } =
  cloud?.useWritesThisMonth ?? (() => ({ value: "—" }));

// Mem0-style dashboard home. Every number is real — pulled from the account's
// own billing, namespaces, and request log. The activity chart is backed by a
// server-side daily aggregation (no synthetic series). First-run still hands a
// dev their first key in one click.

const RANGES = [
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
] as const;

export function HomePage() {
  const { user } = useAuth();
  const [days, setDays] = useState<number>(30);
  // The project chosen at the top, or every project (contexts/ProjectContext.tsx).
  // The memories and the calls are read in it; the model's calls and the
  // plan's writes are the whole account's, and their cards say so.
  const scope = useProjectScope();

  // On the hosted service the model's spend is the service's own cost, and
  // the card shows this month's writes against the plan instead.
  const { hosted } = useMode();
  const writes = useWritesThisMonth();

  // Each number is its own read (lib/useLoad.ts), so one that fails says so
  // on its own card. They used to be one Promise.allSettled whose rejections
  // were dropped: a failed summary read "0 Retrieval events", a failed key
  // list offered "Get your first API key", and a failed series left the chart
  // on "Loading…" for good (audit of 17 Oct 2026).
  const usage = useLoad(
    () => (hosted ? Promise.resolve(null) : usageApi.report(30)),
    [hosted],
    "The model usage could not be read.",
  );
  const summary = useLoad(
    () => requestsApi.summary(scope),
    [scope],
    "The request counts could not be read.",
  );
  // The count of the Memories page, every memory of the scope. The sum
  // over the agents left out every memory written without an agent_id,
  // and an install that never used one read "0 stored memories". A count,
  // not a read of one memory: nothing goes in the project's trail.
  const memoriesStored = useLoad(
    () => memoriesApi.count(scope),
    [scope],
    "The memories could not be counted.",
  );
  const keys = useLoad(keysApi.list, [], "Your API keys could not be read.");
  const hasKey = keys.data ? keys.data.some((x) => !x.revoked) : null;
  // The notice before a key ends (`warning_days`, the webhooks' own), for
  // the line that names the keys ending soon (lib/keyLife.ts).
  const lifetimes = useLoad(keysApi.lifetimes, [], "Whether an API key ends soon could not be read.");
  // The chart series, read again whenever the range changes; an answer for
  // a range picked earlier is dropped.
  const daily = useLoad(
    () => requestsApi.daily(scope, days),
    [scope, days],
    "The daily requests could not be read.",
  );

  // This card used to show a percentage of a plan. There is no plan, so it
  // shows what the model was actually asked to do.
  const report = usage.data;

  const firstName = user?.name?.split("@")[0] ?? "there";

  return (
    <div>
      <PageHeader
        title={`Welcome, ${firstName}`}
        subtitle={
          // The numbers below are the scope's, and the subtitle names it: they
          // were the whole account's under the name of the project chosen
          // (audit of 17 Oct 2026), then the account's whatever was chosen,
          // until 6 Nov 2026.
          scope.kind === "project"
            ? `${scope.name} at a glance: bi-temporal typed facts that supersede instead of going stale.`
            : "Your account at a glance, every project included: bi-temporal typed facts that supersede instead of going stale."
        }
      />

      <ProjectScopeBanner
        oneProjectNote={
          hosted
            ? "Writes this month is your plan's count, every project together."
            : "Model calls are the whole account's: a model call is not counted by project."
        }
      />

      {/* First-run: no key yet → get one in one click. */}
      {hasKey === false && <FirstKeyCard />}
      <LoadNote load={keys} className="mb-6" />
      <KeysEndingNote
        ending={endingSoon(keys.data ?? [], lifetimes.data?.warning_days ?? null, Date.now())}
        className="mb-6"
      />
      <LoadNote load={lifetimes} className="mb-6" />

      {/* Range selector — drives the activity chart. */}
      <div className="mb-4 flex items-center justify-end">
        <div className="inline-flex rounded-lg border border-border bg-secondary/40 p-1">
          {RANGES.map((r) => (
            <button
              key={r.days}
              type="button"
              onClick={() => setDays(r.days)}
              className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
                days === r.days
                  ? "bg-background text-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* Metric cards — all real. */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard
          icon={<Database className="h-4 w-4" />}
          label="Stored memories"
          value={memoriesStored.data?.toLocaleString() ?? null}
          sub="currently in memory"
          to="/memories"
          error={memoriesStored.error}
        />
        {hosted ? (
          <StatCard
            icon={<PlusCircle className="h-4 w-4" />}
            label="Writes this month"
            value={writes.value}
            sub={writes.sub}
            to="/usage"
            account
          />
        ) : (
          <StatCard
            icon={<PlusCircle className="h-4 w-4" />}
            label="Model calls, 30 days"
            value={report?.totals.calls.toLocaleString() ?? null}
            sub={
              report && report.totals.cost !== null && report.currency
                ? `${report.totals.cost.toFixed(2)} ${report.currency}`
                : report
                  ? `${(report.totals.prompt_tokens + report.totals.completion_tokens).toLocaleString()} tokens`
                  : undefined
            }
            to="/usage"
            error={usage.error}
            account
          />
        )}
        <StatCard
          icon={<Search className="h-4 w-4" />}
          label="Retrieval events"
          value={summary.data?.reads.toLocaleString() ?? null}
          sub="search · context · facts"
          to="/requests"
          error={summary.error}
        />
        <StatCard
          icon={<Network className="h-4 w-4" />}
          label="Write events"
          value={summary.data?.writes.toLocaleString() ?? null}
          sub="add · update · delete"
          to="/requests"
          error={summary.error}
        />
      </div>

      {/* Activity chart + endpoint breakdown — the two-panel row. */}
      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-5">
        <div className="rounded-lg border border-border bg-card p-5 lg:col-span-3">
          <div className="mb-3 flex items-center justify-between">
            <p className="text-sm font-medium">Requests</p>
            <Link
              to="/requests"
              className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
            >
              View requests <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          </div>
          <Loaded load={daily} className="my-16 text-center">
            {(d) => <ActivityChart buckets={d.buckets} />}
          </Loaded>
        </div>

        <div className="rounded-lg border border-border bg-card p-5 lg:col-span-2">
          <p className="mb-3 text-sm font-medium">Top endpoints</p>
          <Loaded load={summary}>{(s) => <EndpointBars summary={s} />}</Loaded>
        </div>
      </div>

      <ExplorePlatform />
    </div>
  );
}

/** `value` null: not read yet, or not read at all when `error` says why.
 *  `account`: a number of the whole account whichever project is shown,
 *  tagged as such. */
function StatCard({
  icon,
  label,
  value,
  sub,
  to,
  error,
  account = false,
}: {
  icon: React.ReactNode;
  label: string;
  value: string | null;
  sub?: string;
  to: string;
  error?: string | null;
  account?: boolean;
}) {
  return (
    <Link
      to={to}
      className="group rounded-lg border border-border bg-card p-4 transition-colors hover:border-foreground/20"
    >
      <div className="mb-2 flex items-center gap-1.5 text-muted-foreground">
        {icon}
        <span className="text-xs">{label}</span>
        {account && (
          <span
            title="The whole account, every project together"
            className="ml-auto rounded bg-secondary px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider"
          >
            Account
          </span>
        )}
      </div>
      <p className="text-2xl font-semibold tracking-tight">{error ? "—" : value ?? "—"}</p>
      {error ? (
        <p className="mt-0.5 text-[11px] text-destructive" title={error}>
          Could not be read
        </p>
      ) : (
        sub && <p className="mt-0.5 text-[11px] text-muted-foreground">{sub}</p>
      )}
    </Link>
  );
}

/** Horizontal bars of the most-called endpoints — real, from the log summary. */
function EndpointBars({ summary }: { summary: RequestsSummary }) {
  if (summary.total === 0) {
    return (
      <div className="flex h-[180px] items-center justify-center rounded-md border border-dashed border-border text-sm text-muted-foreground">
        No requests yet.
      </div>
    );
  }
  const top = summary.by_endpoint.slice(0, 6);
  const max = Math.max(...top.map((e) => e.count), 1);
  return (
    <ul className="space-y-2.5">
      {top.map((e) => (
        <li key={e.endpoint}>
          <div className="mb-1 flex items-center justify-between gap-2 text-xs">
            <span className="truncate font-mono text-foreground">{e.endpoint}</span>
            <span className="shrink-0 text-muted-foreground">{e.count.toLocaleString()}</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-secondary">
            <div
              className={`h-full rounded-full ${e.kind === "write" ? "bg-primary" : "bg-primary/40"}`}
              style={{ width: `${Math.max(4, Math.round((e.count / max) * 100))}%` }}
            />
          </div>
        </li>
      ))}
      <li className="flex items-center gap-3 pt-1 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full bg-primary" /> Writes
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full bg-primary/40" /> Reads
        </span>
      </li>
    </ul>
  );
}

/** One-click self-serve: mint the first key + show a ready curl. */
function FirstKeyCard() {
  const target = useTarget();
  const [key, setKey] = useState<AgentKeyCreated | null>(null);
  const [busy, setBusy] = useState(false);
  // The server's sentence when it refused (a key cap, a scope), else none.
  const [err, setErr] = useState<string | null>(null);
  const failed = "Couldn't create a key.";

  const mint = () => {
    setBusy(true);
    setErr(null);
    keysApi
      .bootstrap()
      .then((r) => {
        if (r.created && r.key) setKey(r.key);
        else setErr(failed);
      })
      .catch((e) => setErr(errorMessage(e) ?? failed))
      .finally(() => setBusy(false));
  };

  if (key) {
    const curl = `curl -X POST ${target.base}/v1/memories \\
  -H "Authorization: Bearer ${key.key}" \\
  -H "Content-Type: application/json" \\
  -d '{"content": "Maria prefers email and is allergic to peanuts.", "user_id": "maria"}'`;
    return (
      <div className="mb-6 rounded-lg border border-primary/30 bg-primary/5 p-5">
        <div className="mb-2 flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-primary" />
          <p className="text-sm font-medium">Your API key: copy it now, it's shown once</p>
        </div>
        <CopyBlock text={key.key} />
        <p className="mb-1.5 mt-4 text-sm font-medium">Store your first memory:</p>
        <CopyBlock text={curl} />
        <p className="mt-3 text-xs text-muted-foreground">
          Lost it? You can revoke and create new keys any time under{" "}
          <Link to="/keys" className="text-foreground underline-offset-2 hover:underline">
            API Keys
          </Link>
          .
        </p>
      </div>
    );
  }

  return (
    <div className="mb-6 rounded-lg border border-primary/30 bg-primary/5 p-5">
      <p className="text-sm font-medium">Get your first API key</p>
      <p className="mt-1 text-sm text-muted-foreground">
        One click mints a free <code className="font-mono text-xs">{target.prefix}</code> key and shows
        you a copy-paste request to store your first memory.
      </p>
      <button
        type="button"
        onClick={mint}
        disabled={busy}
        className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-60"
      >
        <KeyRound className="h-4 w-4" />
        {busy ? "Creating…" : "Create my key"}
      </button>
      {err && (
        <p className="mt-2 text-xs text-destructive">
          {err} Try the{" "}
          <Link to="/keys" className="underline">
            API Keys
          </Link>{" "}
          page.
        </p>
      )}
    </div>
  );
}

function CopyBlock({ text }: { text: string }) {
  return (
    <div className="relative">
      <pre className="overflow-x-auto rounded-md bg-secondary px-3 py-2 pr-10 font-mono text-[13px] text-foreground">
        <code>{text}</code>
      </pre>
      <CopyButton
        text={text}
        className="absolute right-2 top-2 rounded-md bg-secondary p-1.5 text-muted-foreground transition-colors hover:bg-background hover:text-foreground"
      />
    </div>
  );
}

/** "Explore the platform" — the Mem0-style card row, linking to real surfaces. */
function ExplorePlatform() {
  const cards: {
    icon: React.ReactNode;
    title: string;
    desc: string;
    to: string;
    external?: boolean;
    cta: string;
  }[] = [
    {
      icon: <Download className="h-5 w-5" />,
      title: "Install",
      desc: "Wire Korely into your agent with the SDK, the MCP connector, or raw REST.",
      to: "/install",
      cta: "Set up",
    },
    {
      icon: <Play className="h-5 w-5" />,
      title: "Playground",
      desc: "Store and recall memories live, and see exactly what your agent retrieves.",
      to: "/playground",
      cta: "Try it",
    },
    {
      icon: <Waypoints className="h-5 w-5" />,
      title: "Memory Graph",
      desc: "Watch typed facts form and supersede each other over time, with an as-of slider.",
      to: "/graph",
      cta: "Open",
    },
    {
      icon: <BookOpen className="h-5 w-5" />,
      title: "Documentation",
      desc: "Step-by-step guides and the full REST, SDK, and MCP reference.",
      to: DOCS.home.href,
      external: true,
      cta: "Read",
    },
  ];

  return (
    <div className="mt-8">
      <h2 className="text-lg font-semibold tracking-tight">Explore the platform</h2>
      <p className="mb-4 mt-0.5 text-sm text-muted-foreground">
        Get the most out of Korely: wire it in, see it in action, and ship faster.
      </p>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {cards.map((c) => {
          const inner = (
            <>
              <div className="mb-3 text-primary">{c.icon}</div>
              <p className="text-sm font-medium">{c.title}</p>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{c.desc}</p>
              <span className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-foreground">
                {c.cta}
                {c.external ? (
                  <ArrowUpRight className="h-3.5 w-3.5" />
                ) : (
                  <ArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
                )}
              </span>
            </>
          );
          const cls =
            "group flex flex-col rounded-lg border border-border bg-card p-5 transition-colors hover:border-foreground/20";
          return c.external ? (
            <a key={c.title} href={c.to} target="_blank" rel="noreferrer" className={cls}>
              {inner}
            </a>
          ) : (
            <Link key={c.title} to={c.to} className={cls}>
              {inner}
            </Link>
          );
        })}
      </div>
    </div>
  );
}
