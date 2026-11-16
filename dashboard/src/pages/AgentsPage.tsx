import { useState } from "react";
import { Link } from "react-router";
import { Bot, Database, Network, Users, X, ChevronRight } from "lucide-react";
import { PageHeader, EmptyState, HelpNote, ProjectScopeBanner } from "@/components/PageShell";
import {
  namespacesApi,
  memoriesApi,
  type AgentNamespace,
  type Memory,
  type ProjectScope,
} from "@/lib/agentsApi";
import { useProjectScope } from "@/contexts/ProjectContext";
import { DOCS } from "@/lib/docsLinks";
import { ModalShell } from "@/components/Modal";
import { useMode } from "@/contexts/ModeContext";
import { cloud } from "@/lib/cloud";
import { useLoad } from "@/lib/useLoad";
import { Loaded } from "@/components/LoadState";

export function AgentsPage() {
  const { hosted } = useMode();
  // The project chosen at the top, or every project (contexts/ProjectContext.tsx).
  const scope = useProjectScope();
  // The page holds the agents with the most memories; `total` is all of them
  // (the server pages the list since the audit of 20 Oct 2026). One read
  // (lib/useLoad.ts): a failure says the server's sentence with Try again,
  // where it said "Agents unavailable. Try again in a moment." with no way
  // to (pre-tester check of 15 Nov 2026).
  const list = useLoad(() => namespacesApi.list(scope), [scope], "The agents could not be read.");
  const [selected, setSelected] = useState<AgentNamespace | null>(null);

  return (
    <div>
      <PageHeader
        title="Agents"
        subtitle="One agent = one app or surface (a support bot, a coding assistant). It's a label you pass on each call, not a key."
        docs={DOCS.agents}
      />

      <ProjectScopeBanner />

      <HelpNote title="What's an agent, exactly?">
        <p className="mb-2">
          <span className="text-foreground">An agent is a label, not something
          you create.</span> It's a string, the{" "}
          <code className="font-mono text-xs">agent_id</code>, that you attach to a
          memory to say which app it belongs to (e.g.{" "}
          <code className="font-mono text-xs">support-bot</code>). One API key can
          write to any agent; one agent can be shared by many keys.
        </p>
        <p className="mb-2">
          Two more labels scope each memory:{" "}
          <code className="font-mono text-xs">user_id</code> (your end customer,{" "}
          <span className="text-foreground">{hosted ? "unlimited on every plan" : "unlimited"}</span>) and{" "}
          <code className="font-mono text-xs">run_id</code> (a single session).
        </p>
        <p className="text-muted-foreground">
          {hosted && cloud ? <cloud.AgentCaps /> : <>This install puts no cap on agents. </>} Need
          staging vs prod? Use{" "}
          <Link to="/projects" className="text-foreground underline underline-offset-2 hover:no-underline">
            Projects
          </Link>
          .
        </p>
      </HelpNote>

      <Loaded load={list}>
        {({ agents, total }) => agents.length === 0 ? (
          <EmptyState icon={<Bot className="h-6 w-6" />} title="No agents yet">
            Pass an <code className="font-mono">agent_id</code> when your code calls{" "}
            <code className="font-mono">add(...)</code> and each distinct namespace
            shows up here with what it remembers.
          </EmptyState>
        ) : (
          <>
            <p className="mb-2 text-xs text-muted-foreground">
              Click an agent to see what it remembers.
              {total > agents.length &&
                ` Showing the ${agents.length.toLocaleString()} with the most memories of ${total.toLocaleString()}.`}
            </p>
            <ul className="space-y-2">
              {agents.map((a, i) => (
                <li key={a.agent_id ?? `__none_${i}`}>
                  <button
                    type="button"
                    onClick={() => setSelected(a)}
                    className="group flex w-full items-center justify-between rounded-lg border border-border bg-card px-4 py-3 text-left transition-colors hover:border-foreground/20"
                  >
                    {/* min-w-0 and wrap-anywhere: a name with no spaces ran out
                        of the row on a phone (QA of 14 Nov 2026). */}
                    <div className="flex min-w-0 items-center gap-2.5">
                      <Bot className="h-4 w-4 shrink-0 text-muted-foreground" />
                      {a.agent_id ? (
                        <code className="min-w-0 wrap-anywhere font-mono text-sm font-medium">{a.agent_id}</code>
                      ) : (
                        <span className="text-sm italic text-muted-foreground">
                          Default namespace
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-4 text-xs text-muted-foreground">
                      <Stat icon={<Database className="h-3.5 w-3.5" />} value={a.memories} label="memories" />
                      <Stat icon={<Users className="h-3.5 w-3.5" />} value={a.end_users} label="users" />
                      <Stat icon={<Network className="h-3.5 w-3.5" />} value={a.facts} label="facts" />
                      <ChevronRight className="h-4 w-4 text-muted-foreground/50 transition-transform group-hover:translate-x-0.5" />
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </Loaded>

      {selected && (
        <AgentModal agent={selected} scope={scope} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function Stat({ icon, value, label }: { icon: React.ReactNode; value: number; label: string }) {
  return (
    <span className="inline-flex items-center gap-1" title={label}>
      {icon}
      <span className="tabular-nums">{value.toLocaleString()}</span>
    </span>
  );
}

function AgentModal({
  agent,
  scope,
  onClose,
}: {
  agent: AgentNamespace;
  scope: ProjectScope;
  onClose: () => void;
}) {
  // agent_id is null for the default namespace → memoriesApi maps null to the
  // "__none__" sentinel so we scope to no-agent memories, not all.
  const recent = useLoad<Memory[]>(
    () => memoriesApi.list(scope, 20, 0, agent.agent_id ?? null).then((d) => d.memories),
    [scope, agent.agent_id],
    "The memories could not be read.",
  );

  const name = agent.agent_id ?? "Default namespace";

  return (
    <ModalShell label="Agent details" onClose={onClose}>
      <div className="absolute inset-0" onClick={onClose} />
      <div
        className="flex max-h-[80vh] w-full max-w-lg flex-col rounded-lg border border-border bg-card shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-start justify-between gap-3 border-b border-border p-5">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <Bot className="h-4 w-4 text-muted-foreground" />
              {agent.agent_id ? (
                <code className="min-w-0 wrap-anywhere font-mono text-sm font-medium">{agent.agent_id}</code>
              ) : (
                <span className="text-sm italic text-muted-foreground">Default namespace</span>
              )}
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {agent.agent_id
                ? "Memories your code wrote under this agent_id."
                : "Memories written without an agent_id."}
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="shrink-0 text-muted-foreground hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* stats */}
        <div className="grid grid-cols-3 divide-x divide-border border-b border-border">
          <ModalStat label="Memories" value={agent.memories} />
          <ModalStat label="End users" value={agent.end_users} />
          <ModalStat label="Facts" value={agent.facts} />
        </div>

        {/* recent memories */}
        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          <p className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
            Recent memories
          </p>
          <Loaded load={recent}>
            {(memories) => memories.length === 0 ? (
              <p className="text-sm text-muted-foreground">No memories stored under {name} yet.</p>
            ) : (
              <ul className="space-y-2">
                {memories.map((m) => (
                  <li key={m.id} className="rounded-md border border-border bg-background px-3 py-2.5">
                    <p className="line-clamp-2 text-sm">{m.content}</p>
                    <div className="mt-1.5 flex items-center gap-3 text-[11px] text-muted-foreground">
                      {m.user_id && (
                        <span className="inline-flex items-center gap-1">
                          <Users className="h-3 w-3" />
                          {m.user_id}
                        </span>
                      )}
                      {m.created_at && <span>{new Date(m.created_at).toLocaleDateString()}</span>}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Loaded>
          {recent.data && recent.data.length > 0 && (
            <Link
              to="/memories"
              className="mt-3 inline-flex items-center gap-1 text-xs text-foreground underline-offset-2 hover:underline"
            >
              Browse all memories <ChevronRight className="h-3.5 w-3.5" />
            </Link>
          )}
        </div>
      </div>
    </ModalShell>
  );
}

function ModalStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="px-4 py-3 text-center">
      <p className="text-lg font-semibold tabular-nums">{value.toLocaleString()}</p>
      <p className="text-[11px] text-muted-foreground">{label}</p>
    </div>
  );
}
