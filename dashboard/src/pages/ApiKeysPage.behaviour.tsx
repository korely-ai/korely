// What the API Keys page says of the keys it lists (ApiKeysPage.tsx,
// GroupedKeys): run in node by scripts/check_the_behaviour.mjs (`npm test`),
// drawn with renderToString. The page lists the whole account's keys
// whichever project the switcher picks; up to 1.0.39 nothing said so, and
// with a new, empty project picked Default's keys read as its own (found on
// 24 Nov 2026).
import type { ReactElement } from "react";
import { renderToString } from "react-dom/server";
import type { Check } from "@/lib/behaviour";
import type { AgentKey, AgentProject } from "@/lib/agentsApi";
import { GroupedKeys } from "@/pages/ApiKeysPage";

const html = (el: ReactElement) => renderToString(el).replace(/<!-- -->/g, "");
const project = (id: string, name: string, is_default = false): AgentProject => ({
  id, name, slug: name.toLowerCase(), is_default, archived: false, key_count: 0,
  description: null, region: null, processing_region: "eu", created_at: "2026-11-24T08:00:00Z",
});
const DEFAULT = project("p-0", "Default", true);
const QA = project("p-1", "QA");
const SHOP = project("p-2", "Shop");
const key = (id: string, p: AgentProject): AgentKey => ({
  id, name: `key of ${p.name}`, masked: "••••abcd", tier: "free", scopes: ["read", "write"],
  created_at: "2026-11-24T08:00:00Z", last_used_at: null, revoked: false,
  project_id: p.id, project_name: p.name,
});
const drawn = (keys: AgentKey[], active: string | null) =>
  html(
    <GroupedKeys
      keys={keys}
      projects={[DEFAULT, QA, SHOP]}
      activeProjectId={active}
      onRevoke={() => {}}
      onMove={() => {}}
      onRotate={() => {}}
      warningDays={null}
    />,
  );
const PICKED = "· picked in the switcher";
const NONE_FOR = (name: string) => `No key reads <span class="font-medium text-foreground">${name}</span> yet`;

export const CHECKS: Record<string, Check> = {
  "with a new project picked that has no key, the page says the keys listed read other projects": (t) => {
    const out = drawn([key("k-0", DEFAULT)], QA.id);
    t.ok(out.includes(NONE_FOR("QA")), out);
    t.ok(!out.includes(PICKED), "no group is marked as the picked project's");
  },
  "the group of the picked project says it is the one picked, and only that group": (t) => {
    const out = drawn([key("k-0", DEFAULT), key("k-1", QA), key("k-2", SHOP)], SHOP.id);
    t.equal(out.split(PICKED).length - 1, 1, out);
    t.ok(/Shop<span[^>]*>· picked in the switcher<\/span>/.test(out), out);
    t.ok(!out.includes("No key reads"), out);
  },
  "with every project picked, no group is marked and nothing is said missing": (t) => {
    const out = drawn([key("k-0", DEFAULT), key("k-1", QA)], null);
    t.ok(!out.includes(PICKED) && !out.includes("No key reads"), out);
  },
};
