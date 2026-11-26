import { useState } from "react";
import { Activity, AlertTriangle, Bot, Cpu, Download, FolderTree, Layers } from "lucide-react";

import { EmptyState, HelpNote, PageHeader } from "@/components/PageShell";
import { LoadNote } from "@/components/LoadState";
import { useLoad } from "@/lib/useLoad";
import { errorMessage, usageApi, type UsageReport } from "@/lib/agentsApi";

/**
 * What the model provider was asked to do, and by whom.
 *
 * This page used to be billing: plans, a quota bar, a checkout button and a
 * customer portal. All of that priced a hosted product. On a machine you own,
 * the model bill arrives at your own provider account, and the question that
 * matters is not "how much of my plan is left" but "which of my agents is
 * spending the money". Nobody else can answer that one for you.
 *
 * Two things on this page are deliberately blank rather than zero. Without a
 * rate card in LLM_RATES there is no cost, because nobody has said what a token
 * costs here, and zero would read as a free month. A model the rate card does
 * not mention is named instead of being priced at nothing.
 */

const RANGES = [7, 30, 90];

/** The name a row of `by_project` is shown with: the project's, or what
 *  the row holds when it has none. */
function projectLabel(r: { project_id: string | null; name: string | null }): string {
  if (r.project_id === null) return "(no project)";
  return r.name ?? r.project_id;
}

/** The window as a CSV file, saved by the browser: the report's numbers,
 *  one row per project and model (GET /admin/usage/export). */
async function saveCsv(days: number): Promise<void> {
  const blob = await usageApi.exportCsv(days);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `korely-model-usage-${days}d.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function n(x: number): string {
  return x.toLocaleString();
}

function Money({
  cost,
  currency,
}: {
  cost: number | null;
  currency: string | null;
}) {
  if (cost === null || !currency) {
    return <span className="text-muted-foreground">—</span>;
  }
  return (
    <span>
      {cost.toFixed(cost < 1 ? 4 : 2)} {currency}
    </span>
  );
}

function Table({
  head,
  rows,
  currency,
}: {
  head: string;
  rows: { key: string; calls: number; tin: number; tout: number; cost: number | null }[];
  currency: string | null;
}) {
  if (rows.length === 0) return null;
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-[13px]">
        <thead>
          <tr className="border-b border-border text-left text-muted-foreground">
            <th className="px-4 py-2.5 font-medium">{head}</th>
            <th className="px-4 py-2.5 text-right font-medium">calls</th>
            <th className="px-4 py-2.5 text-right font-medium">in</th>
            <th className="px-4 py-2.5 text-right font-medium">out</th>
            <th className="px-4 py-2.5 text-right font-medium">cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-b border-border/60 last:border-0">
              <td className="px-4 py-2.5 font-mono text-[12px]">{r.key}</td>
              <td className="px-4 py-2.5 text-right tabular-nums">{n(r.calls)}</td>
              <td className="px-4 py-2.5 text-right tabular-nums text-muted-foreground">
                {n(r.tin)}
              </td>
              <td className="px-4 py-2.5 text-right tabular-nums">{n(r.tout)}</td>
              <td className="px-4 py-2.5 text-right tabular-nums">
                <Money cost={r.cost} currency={currency} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function UsagePage() {
  const [days, setDays] = useState(30);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  // The screen's one read (lib/useLoad.ts): its failure was an orange line
  // with no way to read again, unlike every other page (QA of 14 Nov 2026).
  const read = useLoad(() => usageApi.report(days), [days], "Could not load usage.");
  const report: UsageReport | null = read.data;
  const loading = read.loading && read.data === null && !read.error;

  const empty = !loading && report !== null && report.totals.calls === 0;

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <PageHeader
        title="Model usage"
        subtitle="What your agents asked the model to do, and what it cost, in the whole account and project by project. Everything on this page stays on this machine."
      />

      <div className="mb-6 flex flex-wrap items-center gap-1">
        {RANGES.map((d) => (
          <button
            key={d}
            onClick={() => setDays(d)}
            className={
              d === days
                ? "rounded-md bg-secondary px-3 py-1.5 text-[13px] font-medium"
                : "rounded-md px-3 py-1.5 text-[13px] text-muted-foreground hover:bg-secondary/50"
            }
          >
            {d}d
          </button>
        ))}
        <button
          onClick={() => {
            setSaving(true);
            setSaveError(null);
            saveCsv(days)
              .catch((e) => setSaveError(errorMessage(e) ?? "Could not download the CSV."))
              .finally(() => setSaving(false));
          }}
          disabled={saving || empty || report === null}
          className="ml-auto flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[13px] hover:bg-secondary/50 disabled:opacity-50"
        >
          <Download className="h-3.5 w-3.5" />
          {saving ? "Preparing…" : "Download CSV"}
        </button>
      </div>
      {saveError && (
        <p className="mb-6 text-[13px] text-amber-700 dark:text-amber-400">{saveError}</p>
      )}

      <LoadNote load={read} className="mb-6" />

      {loading && <p className="text-[13px] text-muted-foreground">Loading…</p>}

      {empty && (
        <EmptyState icon={<Activity className="h-5 w-5" />} title="Nothing yet">
          No model call in this window. Write a memory and the extraction that
          follows will appear here.
        </EmptyState>
      )}

      {report && !empty && (
        <div className="space-y-8">
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            {[
              ["calls", n(report.totals.calls), ""],
              ["tokens in", n(report.totals.prompt_tokens),
               report.totals.cached_tokens
                 ? `${n(report.totals.cached_tokens)} from the provider's cache`
                 : ""],
              ["tokens out", n(report.totals.completion_tokens), ""],
            ].map(([label, value, sub]) => (
              <div key={label} className="rounded-lg border border-border px-4 py-3">
                <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
                  {label}
                </div>
                <div className="mt-1 text-[18px] tabular-nums">{value}</div>
                {sub && <div className="mt-0.5 text-[11px] text-muted-foreground tabular-nums">{sub}</div>}
              </div>
            ))}
            <div className="rounded-lg border border-border px-4 py-3">
              <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
                cost
              </div>
              <div className="mt-1 text-[18px] tabular-nums">
                <Money cost={report.totals.cost} currency={report.currency} />
              </div>
            </div>
          </div>

          {!report.rates_configured && (
            <HelpNote title="Why the cost column is empty">
              Nobody has told this install what a token costs, so it does not
              guess. Put a rate card in <code>LLM_RATES</code> and the same
              numbers get priced. The example is the default Global writer at
              Google's list price of October 2026; add a line for each model
              this install calls:
              <pre className="mt-2 overflow-x-auto rounded bg-secondary/50 p-3 text-[11px]">
{`LLM_RATES={"currency":"USD","models":{
  "gemini-3.1-flash-lite":{"input_per_million":0.25,"output_per_million":1.50}}}`}
              </pre>
              It lives in the environment and not in the database on purpose: a
              price changes, and a price written next to the tokens would move
              last quarter's numbers the day your provider moves theirs.
            </HelpNote>
          )}

          {report.models_without_a_rate.length > 0 && (
            <p className="flex items-start gap-2 text-[13px] text-amber-700 dark:text-amber-400">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <span>
                No rate for {report.models_without_a_rate.join(", ")}. Those
                calls are counted but not priced, so the total is lower than
                what you will actually be billed.
              </span>
            </p>
          )}

          <section>
            <h2 className="mb-3 flex items-center gap-2 text-[15px] font-medium">
              <FolderTree className="h-4 w-4 text-muted-foreground" /> By project
            </h2>
            <Table
              head="project"
              currency={report.currency}
              rows={report.by_project.map((r) => ({
                key: projectLabel(r),
                calls: r.calls,
                tin: r.prompt_tokens,
                tout: r.completion_tokens,
                cost: r.cost,
              }))}
            />
            <p className="mt-2 text-[12px] text-muted-foreground">
              {report.by_project.some((r) => r.project_id === null) && (
                <>
                  <code>(no project)</code> holds the calls made for the whole
                  account: the vocabulary of its relations, a search across
                  projects, and every call made before 1.0.41.{" "}
                </>
              )}
              The calls and tokens add up to the totals above; each cost is
              rounded on its own, so the costs add up to the total only
              approximately. The CSV has them by project and model.
            </p>
          </section>

          <section>
            <h2 className="mb-3 flex items-center gap-2 text-[15px] font-medium">
              <Bot className="h-4 w-4 text-muted-foreground" /> By agent
            </h2>
            <Table
              head="agent_id"
              currency={report.currency}
              rows={[
                ...report.by_agent.map((r) => ({
                  key: r.agent_id ?? "(no agent_id)",
                  calls: r.calls,
                  tin: r.prompt_tokens,
                  tout: r.completion_tokens,
                  cost: r.cost,
                })),
                ...(report.other_agents
                  ? [
                      {
                        key: `(${n(report.other_agents.agents)} other agents)`,
                        calls: report.other_agents.calls,
                        tin: report.other_agents.prompt_tokens,
                        tout: report.other_agents.completion_tokens,
                        cost: report.other_agents.cost,
                      },
                    ]
                  : []),
              ]}
            />
            {report.calls_without_an_agent_id > 0 && (
              <p className="mt-2 text-[12px] text-muted-foreground">
                {n(report.calls_without_an_agent_id)} call
                {report.calls_without_an_agent_id === 1 ? "" : "s"} arrived
                without an <code>agent_id</code>. Pass one when you write a
                memory and they will be attributed here.
              </p>
            )}
          </section>

          <section>
            <h2 className="mb-3 flex items-center gap-2 text-[15px] font-medium">
              <Layers className="h-4 w-4 text-muted-foreground" /> By stage
            </h2>
            <Table
              head="stage"
              currency={report.currency}
              rows={report.by_stage.map((r) => ({
                key: r.stage,
                calls: r.calls,
                tin: r.prompt_tokens,
                tout: r.completion_tokens,
                cost: r.cost,
              }))}
            />
          </section>

          <section>
            <h2 className="mb-3 flex items-center gap-2 text-[15px] font-medium">
              <Cpu className="h-4 w-4 text-muted-foreground" /> By model
            </h2>
            <Table
              head="model"
              currency={report.currency}
              rows={report.by_model.map((r) => ({
                key: r.model,
                calls: r.calls,
                tin: r.prompt_tokens,
                tout: r.completion_tokens,
                cost: r.cost,
              }))}
            />
          </section>
        </div>
      )}
    </div>
  );
}
