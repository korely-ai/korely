import { NavLink } from "react-router";
import {
  LayoutDashboard,
  KeyRound,
  FolderTree,
  Database,
  Network,
  Waypoints,
  Bot,
  Webhook,
  Activity,
  ScrollText,
  Gauge,
  Settings,
  Download,
  Play,
  UserRound,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { usageApi } from "@/lib/agentsApi";
import { useLoad } from "@/lib/useLoad";
import { LoadNote } from "@/components/LoadState";
import { useMode } from "@/contexts/ModeContext";
import { cloud, useScreenParts } from "@/lib/cloud";

/** What the model was asked to do this month.
 *
 * This used to be a quota bar: tier, writes used, cap. There is no cap here
 * and no tier, and a bar that fills towards a limit nobody enforces is a bar
 * that misleads. What an operator actually wants at a glance is how much their
 * agents are spending, so that is what it shows.
 *
 * A failed read is said here (lib/useLoad.ts). It ended in
 * `.catch(() => setReport(null))`, and the card vanished as it does for an
 * install whose model was never called (audit of 17 Oct 2026).
 *
 * The whole account's, whichever project the switcher shows: a model call
 * is not filed by project (lib/agentsApi.ts usageApi), and the card says
 * "account" since the data pages follow the switcher (6 Nov 2026).
 */
function ModelUsage() {
  const usage = useLoad(() => usageApi.report(30), [], "The model usage could not be read.");
  if (usage.error) {
    return (
      <div className="border-t border-border px-3 py-3">
        <LoadNote load={usage} />
      </div>
    );
  }
  const report = usage.data;
  if (!report || report.totals.calls === 0) return null;
  const { calls, prompt_tokens, completion_tokens, cost } = report.totals;
  const tokens = prompt_tokens + completion_tokens;
  return (
    <NavLink
      to="/usage"
      className="block border-t border-border px-4 py-3 transition-colors hover:bg-secondary/40"
    >
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          account · model, 30 days
        </span>
        <span className="text-[11px] text-muted-foreground">
          {calls.toLocaleString()} calls
        </span>
      </div>
      <div className="text-[13px] text-foreground">
        {tokens.toLocaleString()} tokens
        {cost !== null && report.currency ? (
          <span className="text-muted-foreground">
            {" · "}
            {cost.toFixed(2)} {report.currency}
          </span>
        ) : null}
      </div>
    </NavLink>
  );
}
type NavItem = { label: string; to: string; icon: LucideIcon; end?: boolean };

// HOME, then SETUP, then ACTIVITY, then ACCOUNT — a Mem0-style information
// architecture. Pages are wired slice by slice; the
// shell renders them all.
const HOME: NavItem[] = [
  { label: "Dashboard", to: "/", icon: LayoutDashboard, end: true },
];

const SETUP: NavItem[] = [
  { label: "Install", to: "/install", icon: Download },
  { label: "Projects", to: "/projects", icon: FolderTree },
  { label: "API Keys", to: "/keys", icon: KeyRound },
  { label: "Playground", to: "/playground", icon: Play },
];

const ACTIVITY: NavItem[] = [
  { label: "Memory Graph", to: "/graph", icon: Waypoints },
  { label: "Agents", to: "/agents", icon: Bot },
  { label: "Memories", to: "/memories", icon: Database },
  { label: "Facts", to: "/facts", icon: Network },
  { label: "Webhooks", to: "/webhooks", icon: Webhook },
  { label: "Requests", to: "/requests", icon: Activity },
  { label: "Audit log", to: "/audit", icon: ScrollText },
  { label: "Usage", to: "/usage", icon: Gauge },
];

const ACCOUNT: NavItem[] = [
  { label: "Settings", to: "/settings", icon: Settings },
];

function NavGroup({
  title,
  items,
  onNavigate,
}: {
  title?: string;
  items: NavItem[];
  onNavigate?: () => void;
}) {
  return (
    <div className="mb-6">
      {title && (
        <p className="px-3 mb-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
          {title}
        </p>
      )}
      <ul className="space-y-0.5">
        {items.map(({ label, to, icon: Icon, end }) => (
          <li key={to}>
            <NavLink
              to={to}
              end={end}
              onClick={onNavigate}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors",
                  isActive
                    ? "bg-secondary text-foreground font-medium"
                    : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
                )
              }
            >
              <Icon className="h-4 w-4 shrink-0" />
              {label}
            </NavLink>
          </li>
        ))}
      </ul>
    </div>
  );
}


/**
 * The navigation: the shell's first column from the `md` breakpoint up, and
 * below it the content of the menu's drawer (components/DashboardLayout.tsx).
 * `onNavigate` closes the drawer when a page is chosen, the page already open
 * included.
 */
export function Sidebar({ onNavigate }: { onNavigate?: () => void } = {}) {
  // The hosted service: /usage is the plan and this month's use, and the
  // account has a page of its own.
  const { hosted } = useMode();
  // The Cloud's parts of the product, after Memories, on the hosted
  // service alone and for an account the server serves them (lib/cloud.ts,
  // useScreenParts).
  const { parts } = useScreenParts();
  const withParts = ACTIVITY.flatMap((i) =>
    i.to === "/memories"
      ? [i, ...parts.map((p) => ({ label: p.label, to: p.path, icon: p.icon }))]
      : [i],
  );
  const activity = hosted
    ? withParts.map((i) => (i.to === "/usage" ? { ...i, label: "Plan & usage" } : i))
    : ACTIVITY;
  const account = hosted
    ? [{ label: "Account", to: "/account", icon: UserRound }, ...ACCOUNT]
    : ACCOUNT;
  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-border bg-background">
      <div className="flex items-center gap-2 px-5 h-14 border-b border-border">
        <img src="/korely-mark.svg" alt="" className="h-6 w-6" />
        <span className="font-semibold tracking-tight">Korely</span>
        <span className="ml-1 rounded-full bg-secondary px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          Agents
        </span>
      </div>
      <nav className="flex-1 overflow-y-auto px-3 py-4">
        <NavGroup items={HOME} onNavigate={onNavigate} />
        <NavGroup title="Setup" items={SETUP} onNavigate={onNavigate} />
        <NavGroup title="Activity" items={activity} onNavigate={onNavigate} />
        <NavGroup title="Account" items={account} onNavigate={onNavigate} />
      </nav>
      {hosted && cloud ? <cloud.SidebarUsage /> : <ModelUsage />}
    </aside>
  );
}
