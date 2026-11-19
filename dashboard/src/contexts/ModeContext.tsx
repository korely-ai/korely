// Which product this screen serves (one codebase, 8 Oct 2026): an install of
// one's own, or the hosted service (KORELY_MODE=hosted on the server). Read
// once at start (App.tsx): GET /health says the mode, and on the hosted service
// GET /hosted/config what its sign-in needs.
// The pages that differ ask `useMode()`; the hosted service's own pieces live
// in dashboard/src/hosted/, and reach the screen through lib/cloud.ts: a build
// without them (the public repository) is the screen of an install of one's
// own, whatever the server says.
import { createContext, useContext } from "react";
import { API_BASE } from "@/config/backendUrl";
import type { CloudConfig, CloudScreen } from "@/lib/cloud";

export interface Mode {
  hosted: boolean;
  /** GET /hosted/config, kept whole for the Cloud's pieces (hosted/config.ts). */
  config: CloudConfig | null;
  /** What a key of this install begins with and where its MCP door is, as
   *  GET /health says them (services/keys.minted_prefix,
   *  mcp/server.published_path). The snippets wrote both by hand, and a
   *  change on the server would not have reached them (audit of 17 Oct
   *  2026). Empty when the server did not answer. */
  keyPrefix: string;
  mcpPath: string;
}

export const ModeContext = createContext<Mode>({
  hosted: false,
  config: null,
  keyPrefix: "",
  mcpPath: "",
});

export function useMode(): Mode {
  return useContext(ModeContext);
}

/** `cloud`: the Cloud's pieces of this build (lib/cloud.ts), null in a build
 *  without them. Passed in, not imported: those pieces read this context. */
export async function loadMode(cloud: CloudScreen | null): Promise<Mode> {
  let health: { mode?: unknown; key_prefix?: unknown; mcp_path?: unknown } | null = null;
  try {
    const res = await fetch(`${API_BASE}/health`, { headers: { Accept: "application/json" } });
    health = res.ok ? await res.json() : null;
  } catch {
    // Unreachable: the screen of an install of one's own, whose login page
    // says the server cannot be reached when it tries.
    health = null;
  }
  const text = (v: unknown) => (typeof v === "string" ? v : "");
  const keyPrefix = text(health?.key_prefix);
  const mcpPath = text(health?.mcp_path);
  // Only a server that says it is the hosted service is asked /hosted/config,
  // so an install of one's own never sees a 404 for it.
  const config = health?.mode === "hosted" && cloud ? await cloud.loadConfig() : null;
  return { hosted: config !== null, config, keyPrefix, mcpPath };
}
