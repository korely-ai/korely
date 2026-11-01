// The Cloud's pieces of the screen (dashboard/src/hosted/), when this tree
// carries them: the screen's side of korely_agent/core/extensions.py. The
// public repository has no hosted/ folder (the decision of 28 Oct 2026):
// there `cloud` is null, `import.meta.glob` finds nothing to bundle,
// and the screen is the one of an install of one's own. Only a server that
// says it is the hosted service, and a build that has the pieces, switch them
// on (contexts/ModeContext.tsx).
import type { ComponentType, ReactNode } from "react";

/** What GET /hosted/config answers, read only by the Cloud's own pieces. */
export type CloudConfig = object;

export interface CloudScreen {
  /** GET /hosted/config, asked only once /health says `"mode": "hosted"`. */
  loadConfig(): Promise<CloudConfig | null>;
  /** The address the Install page's snippets carry. */
  apiBase(config: CloudConfig | null, fallback: string): string;
  /** The sign-in provider, the login, the plan and the account: lazy
   *  chunks, never downloaded by the screen of an install of one's own. */
  AuthProvider: ComponentType<{ children: ReactNode }>;
  LoginPage: ComponentType;
  PlanPage: ComponentType;
  AccountPage: ComponentType;
  /** This month's use against the plan, in the sidebar. */
  SidebarUsage: ComponentType;
  /** The named agents each plan holds, on the Agents page. */
  AgentCaps: ComponentType;
  /** This month's writes, for the home page's card. */
  useWritesThisMonth(): { value: string; sub?: string };
}

const found = import.meta.glob<CloudScreen>("../hosted/index.ts", {
  eager: true,
  import: "screen",
});

export const cloud: CloudScreen | null = found["../hosted/index.ts"] ?? null;
