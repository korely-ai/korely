// The public site's addresses the screen links to, written once: every
// dashboard page has a "learn more" escape hatch into the docs at
// korely.ai/docs, and the Install, Home and Settings pages link to the site.
// Keep these pointing at pages that actually exist (no anchors that might 404
// visually); tests/test_dashboard_texts.py checks that every entry is used and
// that no page writes the site's address by hand (audit of 17 Oct 2026).

export const SITE = "https://korely.ai";
const BASE = `${SITE}/docs`;

/** Where people write to: account and billing questions (the address the
 *  hosted service's own refusals name), commercial support, feedback. */
export const SUPPORT_EMAIL = "support@korely.ai";
export const SALES_EMAIL = "info@korely.ai";
export const FEEDBACK_EMAIL = "feedback@korely.ai";

export interface DocsTarget {
  href: string;
  label: string;
}

export const DOCS = {
  home: { href: BASE, label: "Docs home" },
  playground: { href: `${BASE}/concepts/memory-model`, label: "How retrieval works" },
  keys: { href: `${BASE}/api-reference`, label: "API keys & auth" },
  install: { href: `${BASE}/surfaces`, label: "SDK · MCP · CLI" },
  sdk: { href: `${BASE}/surfaces/sdk`, label: "Full SDK reference" },
  cli: { href: `${BASE}/surfaces/cli`, label: "CLI" },
  api: { href: `${BASE}/api-reference`, label: "REST API reference" },
  graph: { href: `${BASE}/concepts/temporal-facts`, label: "How temporal facts work" },
  memories: { href: `${BASE}/concepts/memory-model`, label: "The memory model" },
  facts: { href: `${BASE}/concepts/temporal-facts`, label: "How temporal facts work" },
  agents: { href: `${BASE}/surfaces/sdk`, label: "Scoping: agents & users" },
  // The site has a page of its own for webhooks (korely.ai/docs/webhooks): this
  // opened the API index (audit of 17 Oct 2026).
  webhooks: { href: `${BASE}/webhooks`, label: "Webhooks reference" },
  requests: { href: `${BASE}/api-reference`, label: "API reference" },
  audit: { href: `${BASE}/api-reference/audit-log`, label: "The audit log" },
} satisfies Record<string, DocsTarget>;
