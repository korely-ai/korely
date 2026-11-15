// Where the snippets on the screen send their requests, and what a key looks
// like (one codebase, 8 Oct 2026). On an install of one's own: this install's
// address and an SDK that must be told the address. On the hosted service:
// the service's address and an SDK that finds the service from the key alone.
// The key's prefix and the MCP door's path are the server's own (GET /health,
// ModeContext): written here by hand they could drift from it (audit of 17
// Oct 2026).
import { BACKEND_URL } from "@/config/backendUrl";
import { useMode } from "@/contexts/ModeContext";
import { cloud } from "@/lib/cloud";

export interface Target {
  base: string;
  prefix: string;
  /** The SDK snippets pass the address. */
  sdkNeedsAddress: boolean;
  mcp: string;
}

export function useTarget(): Target {
  const { hosted, config, keyPrefix, mcpPath } = useMode();
  const base = hosted && cloud ? cloud.apiBase(config, BACKEND_URL) : BACKEND_URL;
  return { base, prefix: keyPrefix, sdkNeedsAddress: !hosted, mcp: `${base}${mcpPath}` };
}
