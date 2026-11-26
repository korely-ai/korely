import apiClient from "@/lib/apiClient";
import { apiError, apiErrorDetail } from "@/lib/apiError";
import { inScope, type ProjectScope } from "@/lib/projectScope";

// Every request path that carries a value, built by this one tag: each value
// is one path segment, encoded (encodeURIComponent), never a piece of the
// path. Interpolated raw, an id taken from a link (`/memories?open=`) such as
// `../users/<someone>/memories` was resolved by the browser to another route,
// and the memory drawer's Delete sent the account-wide erasure of that end
// user (audit of 20 Oct 2026).
// `.` and `..` encode to themselves and are still dot segments, so they are
// refused, as an empty segment is. tests/test_dashboard_paths_are_encoded.py
// fails on a request path written with a raw `${...}`.
export function apiPath(strings: TemplateStringsArray, ...values: (string | number)[]): string {
  return strings.reduce((out, piece, i) => {
    if (i === 0) return piece;
    const value = String(values[i - 1]);
    if (value === "" || value === "." || value === "..") {
      throw new Error("a path segment cannot be empty, '.' or '..'");
    }
    return out + encodeURIComponent(value) + piece;
  }, "");
}

// The backend's stable reason for a refusal (`project_name_taken`,
// `cannot_delete_default`, `endpoint_disabled`, or the status's own code),
// read by lib/apiError.ts like every other error on this screen. Branch on
// this, never on the message.
export function errorCode(e: unknown): string | null {
  return apiError(e)?.code || null;
}

// The server's sentence for a person: a plain refusal, a `{code, message}`
// pair's message, or the first field a 422 named. Null when there is none (a
// network failure, a proxy's page, the unhandled 500), and the page says its
// own sentence. It read a string `detail` only until the audit of 17 Oct 2026,
// so a 422 or a coded refusal showed the generic sentence instead.
export function errorMessage(e: unknown): string | null {
  return apiError(e)?.message ?? null;
}

/** The regions a refused clear would leave with no writer, the field
 *  `regions` of a 400 `confirmation_required` (api/admin.
 *  _ask_before_a_region_loses_its_writer): the page writes its own sentence
 *  from them, never from the API's message, which names `?confirm=true`. */
export function regionsLeftWithoutWriter(e: unknown): Region[] {
  const regions = apiErrorDetail(e)?.regions;
  return Array.isArray(regions)
    ? regions.filter((r): r is Region => r === "eu" || r === "global")
    : [];
}

// Masked view of a key — what list/revoke return. Never carries the secret.
export interface AgentKey {
  expires_at?: string | null;
  /** live | revoked | refused | expired, decided by the server so a listing
   *  and a refusal never disagree about why a key stopped working. `refused`:
   *  a kor_live_ key on an install of one's own, refused at every door since
   *  0.2.0. */
  state?: string;
  id: string;
  name: string | null;
  masked: string; // e.g. "kor_self_••••O5fP"
  tier: string;
  scopes: string[];
  created_at: string;
  last_used_at: string | null;
  revoked: boolean;
  // The project that scopes this key. null = the account's Default project.
  project_id: string | null;
  project_name: string | null;
  // The end users it is confined to inside its project, or null for the
  // whole project (services/user_scope.py).
  users?: string[] | null;
  /** Until when the secret a rotation replaced still works; null when none
   *  does (POST /admin/keys/{id}/rotate). */
  old_secret_expires_at?: string | null;
  /** Whether a rotation takes it (keys.rotates on the server): a live key a
   *  person made. A connected app's key renews its own secret. */
  rotates?: boolean;
}

// The "Only these end users" box: comma or newline separated, blanks dropped.
// Empty means the whole project (undefined: the field is not sent).
export function endUsersFrom(text: string): string[] | undefined {
  const list = text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
  return list.length ? list : undefined;
}

// Creation response — includes the full plaintext key exactly once.
export interface AgentKeyCreated extends AgentKey {
  key: string;
}

export interface KeyLifetimes {
  default_days: number;
  choices: { days: number | null; label: string }[];
  warning_days: number;
  /** The longest lifetime the API takes, in days (services/keys.py). */
  max_days: number;
  /** What Rotate offers for the old secret, in hours: 0 ends it at once. */
  old_secret_default_hours: number;
  old_secret_choices: { hours: number; label: string }[];
}

// The home page's first key. `key` carries the full plaintext only when
// `created` is true.
export interface BootstrapResult {
  created: boolean;
  key: AgentKeyCreated | null;
  existing_count: number;
}

const KEY_PAGE = 200;
const KEY_PAGES = 5;

export const keysApi = {
  // The API answers 200 keys at most, the live ones first. Page until a short
  // page, up to KEY_PAGES of them: the list shows the first 1,000 keys.
  list: async () => {
    const out: AgentKey[] = [];
    for (let page = 0; page < KEY_PAGES; page++) {
      const rows = await apiClient
        .get<AgentKey[]>("/admin/keys", { params: { limit: KEY_PAGE, offset: page * KEY_PAGE } })
        .then((r) => r.data);
      out.push(...rows);
      if (rows.length < KEY_PAGE) break;
    }
    return out;
  },

  // `projectId` is optional — omitting it scopes the key to the Default project.
  // `expiresInDays` of 0 means never. Omitted means the install's default,
  // which is finite: a credential with no end date is one nobody gets round to
  // rotating.
  // `scopes` left out: the install's default set (GET /admin/scopes).
  create: (
    name?: string,
    projectId?: string,
    expiresInDays?: number | null,
    scopes?: string[],
    users?: string[],
  ) =>
    apiClient
      .post<AgentKeyCreated>("/admin/keys", {
        name: name || null,
        ...(projectId ? { project_id: projectId } : {}),
        ...(scopes ? { scopes } : {}),
        ...(users && users.length ? { users } : {}),
        // null goes on the wire as null, not as 0. It used to be mapped to 0
        // because 0 was the spelling of "never"; 0 now means "expired now", and
        // sending it for the "Never expires" choice would mint a dead key.
        ...(expiresInDays === undefined ? {} : { expires_in_days: expiresInDays }),
      })
      .then((r) => r.data),

  lifetimes: () =>
    apiClient
      .get<KeyLifetimes>("/admin/keys/lifetimes")
      .then((r) => r.data),

  // What each scope opens, and the default set: read from the server, so the
  // screen offers exactly the scopes it serves.
  scopes: () =>
    apiClient
      .get<{ scopes: { name: string; opens: string }[]; default: string[] }>("/admin/scopes")
      .then((r) => r.data),

  revoke: (id: string) =>
    apiClient.post(apiPath`/admin/keys/${id}/revoke`).then((r) => r.data),

  // A new secret for the same key, in this answer only, the old one working
  // `oldSecretHours` more (0: it ends now, for a key that leaked). Left out,
  // the install's default overlap; the new secret keeps the key's lifetime.
  rotate: (id: string, oldSecretHours?: number) =>
    apiClient
      .post<AgentKeyCreated>(
        apiPath`/admin/keys/${id}/rotate`,
        oldSecretHours === undefined ? {} : { old_key_expires_in_hours: oldSecretHours },
      )
      .then((r) => r.data),

  // Re-point a key to a different project. Returns the updated masked key.
  move: (id: string, projectId: string) =>
    apiClient
      .post<AgentKey>(apiPath`/admin/keys/${id}/move`, { project_id: projectId })
      .then((r) => r.data),

  // The home page's "you have no key yet" card: the ordinary create call with
  // a name chosen for you. Not idempotent: a second call finds "first key"
  // taken and is refused with a 409, which the card says (audit of 17 Oct
  // 2026: this said "Idempotent"). It was POST /admin/keys/bootstrap, the
  // hosted product's self-serve signup, before there was one product.
  bootstrap: (): Promise<BootstrapResult> =>
    apiClient
      .post<AgentKeyCreated>("/admin/keys", { name: "first key" })
      .then((r) => ({ created: true, key: r.data, existing_count: 0 })),
};

// ── Projects (the org layer above agents — scopes keys + dashboard views) ───
// Where a project's memories are read by a model (services/regions.py).
export type Region = "eu" | "global";

export interface AgentProject {
  id: string;
  name: string;
  slug: string;
  is_default: boolean;
  archived: boolean;
  key_count: number;
  description: string | null;
  /** The project's own region; null follows this install's default provider. */
  region: Region | null;
  /** Where its memories are read now; null when that region has no provider. */
  processing_region: Region | null;
  created_at: string;
}

// Returned by DELETE /admin/projects/{id} (api/admin.py ProjectDeleted). A
// plain delete moves what the project held to Default and fills the `_moved`
// counters; `?erase=true` fills the `_deleted` ones, `erasure` and `audit_id`.
// The counters of the road not taken stay at zero. This declared fields the
// route had stopped sending (`keys_repointed`) until 2026-09-28.
// DELETE /admin/projects/{id}: api/admin.ProjectDeleted, field for field
// (tests/test_the_docs_say_what_the_code_does.py holds them equal).
export interface ProjectDeleteResult {
  deleted: boolean;
  keys_moved: number;
  /** The keys of apps connected over OAuth to it: revoked, never moved. */
  apps_disconnected: number;
  /** Erased: keys confined to end users it left with nobody, revoked. */
  keys_revoked: number;
  memories_moved: number;
  facts_moved: number;
  /** Facts the Default project already held: merged into that one. */
  facts_merged: number;
  aliases_moved: number;
  /** Aliases whose name the Default project had already decided otherwise. */
  aliases_not_moved: number;
  memories_deleted: number;
  facts_deleted: number;
  batches_deleted: number;
  erasure: "permanent" | null;
  audit_id: string | null;
  /** Default's region, where what moved is written from now on: the deleted
   *  project's own whenever live keys or data moved (a deletion that would
   *  change it is refused, 409 `project_region_differs`); a key that no
   *  longer works moves whatever the regions. Null when no provider serves
   *  it yet. */
  region: Region | null;
}

// ── Which project a read of the screen covers ─────────────────────────────
// One project, or every project said as such: lib/projectScope.ts, the only
// module that can make one. Every read of data that lives in a project below
// takes it, required, and sends it with `inScope`.
export type { ProjectScope } from "@/lib/projectScope";

export const projectsApi = {
  // `archived: true` adds the archived projects: the Projects page draws
  // them, with Unarchive. Without it the list is the live ones, as the
  // server answers by default (api/admin.py list_projects).
  list: (opts: { archived?: boolean } = {}) =>
    apiClient
      .get<{ projects: AgentProject[]; total: number }>("/admin/projects", {
        params: opts.archived ? { include_archived: true } : undefined,
      })
      .then((r) => r.data),

  create: (name: string, description?: string, region?: Region | null) =>
    apiClient
      .post<AgentProject>("/admin/projects", {
        name,
        ...(description ? { description } : {}),
        ...(region ? { region } : {}),
      })
      .then((r) => r.data),

  // `region: null` sends the project back to following the install.
  update: (
    id: string,
    patch: { name?: string; description?: string; region?: Region | null },
  ) =>
    apiClient
      .patch<AgentProject>(apiPath`/admin/projects/${id}`, patch)
      .then((r) => r.data),

  archive: (id: string) =>
    apiClient
      .post<AgentProject>(apiPath`/admin/projects/${id}/archive`)
      .then((r) => r.data),

  // Back from the archive: in the lists again, and it takes keys.
  unarchive: (id: string) =>
    apiClient
      .post<AgentProject>(apiPath`/admin/projects/${id}/unarchive`)
      .then((r) => r.data),

  // `erase: true` is the irreversible road: every memory, fact, alias and
  // batch job of the project is physically deleted. Without it they move to
  // Default. The screen asks for it in a second, separate step.
  remove: (id: string, opts: { erase?: boolean } = {}) =>
    apiClient
      .delete<ProjectDeleteResult>(apiPath`/admin/projects/${id}`, {
        params: opts.erase ? { erase: true } : undefined,
      })
      .then((r) => r.data),
};

// ── Memories (F3 agent store) ─────────────────────────────────────────────
export interface Memory {
  id: string;
  content: string;
  user_id: string | null; // the end user this memory belongs to
  agent_id: string | null;
  run_id: string | null;
  // The tool that wrote it (Claude Code, Cursor...): the X-Korely-Client
  // header, or the name an MCP client gave. A label, never a scope.
  client: string | null;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  categories?: string[]; // fact families this memory produced
}

// A category present in the account's memories — drives the filter chips.
export interface CategoryCount {
  family: string;
  count: number;
}

export interface SearchHit extends Memory {
  score: number;
  /** Said when the search was asked for `types` (`search`'s `kinds`):
   *  `memory`, or the type of a kind a part of the screen adds
   *  (lib/cloud.ts, PartSearch), whose result is that part's shape. */
  type?: string;
}

// Detail shape: the memory plus the typed facts Korely extracted from it.
/** A memory with every fact it asserted (GET /admin/memories/{id}), the
 *  closed ones too: `closed` says which ended or were superseded. */
export interface MemoryWithFacts extends Memory {
  facts: (Fact & { closed: boolean })[];
}

export interface MemoryList {
  memories: Memory[];
  total: number;
}

export const memoriesApi = {
  // agentId: undefined = all agents · null = the default (no agent_id) namespace
  // · "bot1" = that agent. family scopes to a category; days = recent window.
  list: (
    scope: ProjectScope,
    limit = 50,
    offset = 0,
    agentId?: string | null,
    family?: string | null,
    days?: number,
  ) =>
    apiClient
      .get<MemoryList>("/admin/memories", {
        params: {
          ...inScope(scope),
          limit,
          offset,
          ...(agentId !== undefined
            ? { agent_id: agentId === null ? "__none__" : agentId }
            : {}),
          ...(family ? { family } : {}),
          ...(days ? { days } : {}),
        },
      })
      .then((r) => r.data),

  // How many there are, read as a number: no memory is read, and nothing is
  // filed in the trail (GET /admin/memories/count). The Home page read one
  // memory with list(scope, 1, 0) for its total, a read in the project's
  // trail at every visit.
  count: (scope: ProjectScope) =>
    apiClient
      .get<{ total: number }>("/admin/memories/count", { params: inScope(scope) })
      .then((r) => r.data.total),

  categories: (scope: ProjectScope) =>
    apiClient
      .get<CategoryCount[]>("/admin/memories/categories", { params: inScope(scope) })
      .then((r) => r.data),

  // erase: gone for good (the row, its text, vector and the facts only it
  // stated), not only forgotten.
  remove: (id: string, erase = false) =>
    apiClient
      .delete<{ deleted: boolean; id: string; erasure?: string }>(apiPath`/admin/memories/${id}`, {
        params: erase ? { erase: true } : {},
      })
      .then((r) => r.data),

  // userId scopes the search to one end user — server-side, so the ranking and
  // limit reflect exactly what that user's agent would get back from
  // search/get_context. Omit it to search every end user of the scope.
  // `kinds`: the kinds of the one search (`types`), the memories alone when
  // omitted; sent once per kind, as the server reads a list in a query
  // (`types=a&types=b`), not axios's `types[]=`.
  search: (
    scope: ProjectScope,
    query: string,
    limit = 25,
    userId?: string | null,
    kinds?: readonly string[],
  ) =>
    apiClient
      .get<SearchHit[]>("/admin/memories/search", {
        params: {
          ...inScope(scope),
          query,
          limit,
          ...(userId ? { user_id: userId } : {}),
          ...(kinds ? { types: kinds } : {}),
        },
        ...(kinds ? { paramsSerializer: { indexes: null } } : {}),
      })
      .then((r) => r.data),

  get: (id: string) =>
    apiClient.get<MemoryWithFacts>(apiPath`/admin/memories/${id}`).then((r) => r.data),

  history: (id: string) =>
    apiClient.get<MemoryHistory>(apiPath`/admin/memories/${id}/history`).then((r) => r.data),
};

// Lifecycle timeline of one memory — the "Timeline" tab. Richer than a plain
// add/update/delete log: it includes the typed facts the memory produced.
export interface HistoryEvent {
  event: "created" | "updated" | "fact_extracted" | "fact_invalidated" | "deleted";
  at: string | null;
  fact?: string | null;
  fact_id?: string | null;
}

export interface MemoryHistory {
  id: string;
  events: HistoryEvent[];
}

// ── Billing (F4) ──────────────────────────────────────────────────────────
// There was a billing layer here: BillingStatus, SubscriptionState, checkout,
// the customer portal and plan changes. All of it priced a hosted plan. On a
// machine the customer owns and pays for there is nothing to bill, and a
// screen that shows a quota bar for a quota nobody enforces is a screen that
// lies. What replaced it is `usageApi` at the bottom: what the model provider
// was actually asked to do, and by which agent.

// ── Facts (the typed-graph moat) ──────────────────────────────────────────
export interface Fact {
  id: string;
  subject: string;
  subject_type: string;
  predicate: string;
  object: string;
  predicate_family: string;
  confidence: number | null;
  user_id: string | null;
  agent_id: string | null;
  valid_from: string | null;
  invalid_at: string | null;
  invalidated_by: string | null;
  source_memory_id: string | null;
  created_at: string | null;
  /** When the store learned the fact had ended (null while open): the second
   *  clock, next to `created_at`, when it learned the fact. */
  closed_known_at?: string | null;
}

export interface FactsList {
  facts: Fact[];
  total: number;
}

/** The filters of a facts read; the project is the read's `ProjectScope`. */
export interface FactsQuery {
  user_id?: string;
  entity?: string;
  predicate_family?: string;
  include_invalidated?: boolean;
  limit?: number;
  /** Where the page starts: the route pages in a stable order (the freshest
   *  confirmation first, the id last; services/facts/service.py). */
  offset?: number;
}

export interface FactCorrection {
  subject?: string;
  predicate?: string;
  object?: string;
}

export const factsApi = {
  list: (scope: ProjectScope, q: FactsQuery = {}) =>
    apiClient
      .get<FactsList>("/admin/facts", { params: { limit: 100, ...q, ...inScope(scope) } })
      .then((r) => r.data),

  // Forget = invalidate (drops from current, stays in history). Never destroys.
  forget: (id: string) =>
    apiClient
      .post<{ id: string; status: string; audit_id: string }>(apiPath`/admin/facts/${id}/forget`)
      .then((r) => r.data),

  // Correct = supersede: writes the corrected triple as a new fact, invalidates
  // the old one. Returns the new fact.
  correct: (id: string, patch: FactCorrection) =>
    apiClient.patch<Fact>(apiPath`/admin/facts/${id}`, patch).then((r) => r.data),
};

// ── Webhooks ──────────────────────────────────────────────────────────────
export interface Webhook {
  id: string;
  url: string;
  events: string[];
  /** False once a delivery spent its attempts with nothing accepted since:
   *  nothing is sent to it until it is enabled again. */
  active: boolean;
  /** Only on creation. A listing must never carry it: without the secret
   *  nobody can verify a delivery, and with it in a listing anybody can
   *  forge one. Optional here because the server is right not to send it. */
  secret?: string;
  /** Failed attempts since the last accepted delivery (the endpoint's own
   *  count; a success and Enable set it back to 0). */
  failure_count: number;
  last_delivery_at: string | null;
  /** What went wrong the last time. "N failed attempts" without this makes
   *  somebody go and guess at their own server. */
  last_error?: string | null;
  /** Made before 0.1.11, with a secret the Standard Webhooks libraries
   *  cannot verify: not sent since 0.2.0, each attempt refused and counted
   *  as a failure. `manage rotate-webhook-secret` gives it a spec secret and
   *  sends what is queued; or recreate it. */
  legacy_signature?: boolean;
  /** The project whose events it receives (and the account's); null: every project. */
  project_id: string | null;
  created_at: string | null;
}

export const webhooksApi = {
  list: () =>
    apiClient.get<Webhook[]>("/admin/webhooks").then((r) => r.data),

  // The events this server sends, the only ones worth offering.
  events: () =>
    apiClient
      .get<{ events: string[] }>("/admin/webhooks/events")
      .then((r) => r.data.events),

  create: (url: string, events: string[], projectId?: string | null) =>
    apiClient
      .post<Webhook>("/admin/webhooks", {
        url,
        events,
        ...(projectId ? { project_id: projectId } : {}),
      })
      .then((r) => r.data),

  remove: (id: string) =>
    apiClient.delete(apiPath`/admin/webhooks/${id}`).then((r) => r.data),

  // One delivery now. A 200 says the call ran, `delivered` whether the
  // receiver accepted it: a 500 from the receiver is `delivered: false`.
  test: (id: string) =>
    apiClient
      .post<WebhookTestResult>(apiPath`/admin/webhooks/${id}/test`)
      .then((r) => r.data),

  // Switch a disabled endpoint back on, URL and secret kept, its failures
  // forgotten (admin.py enable_webhook). The screen never called it until
  // the audit of 17 Oct 2026, and said to delete and recreate instead, which
  // changes the secret every receiver verifies with.
  enable: (id: string) =>
    apiClient.post<Webhook>(apiPath`/admin/webhooks/${id}/enable`).then((r) => r.data),
};

export interface WebhookTestResult {
  delivered: boolean;
  /** The receiver's HTTP status, null when it was not reached. */
  status: number | null;
  error: string | null;
  url: string;
}

// ── Requests log ──────────────────────────────────────────────────────────
export interface RequestRow {
  endpoint: string;
  at: string;
  /** The server's one classification (services/usage_report.kind_of): the
   *  same rule as the summary's counts next to the list. */
  kind: "write" | "read";
}

export interface EndpointCount {
  endpoint: string;
  count: number;
  kind: "write" | "read";
}

export interface RequestsSummary {
  by_endpoint: EndpointCount[];
  writes: number;
  reads: number;
  total: number;
}

// Daily writes/reads buckets for the activity chart. Contiguous series (zero
// days included), aggregated server-side from the real per-request log.
export interface DailyBucket {
  date: string; // YYYY-MM-DD (UTC)
  writes: number;
  reads: number;
}

export interface RequestsDaily {
  days: number;
  buckets: DailyBucket[];
  total_writes: number;
  total_reads: number;
}

export const requestsApi = {
  list: (scope: ProjectScope, limit = 50) =>
    apiClient
      .get<{ requests: RequestRow[]; total: number }>("/admin/requests", {
        params: { ...inScope(scope), limit },
      })
      .then((r) => r.data),

  summary: (scope: ProjectScope) =>
    apiClient
      .get<RequestsSummary>("/admin/requests/summary", { params: inScope(scope) })
      .then((r) => r.data),

  daily: (scope: ProjectScope, days = 30) =>
    apiClient
      .get<RequestsDaily>("/admin/requests/daily", { params: { ...inScope(scope), days } })
      .then((r) => r.data),
};

// ── Audit log (bussola 2e) ─────────────────────────────────────────────────
// The trail: who did what, and what each read returned, by id. Ids only in
// the events; `read` turns one event's ids into what they say now (a memory
// erased since comes back as null).

export interface AuditEvent {
  ts: string;
  actor: string; // rest | mcp | dashboard | manage | worker | oauth | install (services/audit.ACTORS)
  action: string; // read | write | fact_write | fact_invalidate | forget | erase | key_create | ...
  result: string; // ok | error; an error's token is meta.code
  endpoint: string;
  /** The key that made the call, by its id on the Keys page; null with no key (an operator). */
  api_key_id: string | null;
  user_id: string | null;
  target_id: string | null;
  meta: Record<string, unknown> | null;
  ip: string | null;
  project_id: string | null;
  read: { memories?: string[]; facts?: string[] } | null;
}

export interface AuditPage {
  events: AuditEvent[];
  total: number;
  /** Every verb the trail records (services/audit.ACTIONS): the filter's. */
  actions: string[];
}

export interface AuditFilters {
  user_id?: string;
  action?: string;
  limit?: number;
  offset?: number;
}

export interface AuditReadItems {
  memories: Record<string, { text: string; user_id: string | null; deleted: boolean } | null>;
  facts: Record<
    string,
    { line: string; user_id: string | null; valid_from: string | null; invalid_at: string | null } | null
  >;
}

export const auditApi = {
  list: (scope: ProjectScope, f: AuditFilters = {}) =>
    apiClient
      .get<AuditPage>("/admin/audit", {
        params: {
          ...inScope(scope),
          user_id: f.user_id || undefined,
          action: f.action || undefined,
          limit: f.limit ?? 100,
          offset: f.offset ?? 0,
        },
      })
      .then((r) => r.data),

  read: (memories: string[], facts: string[]) =>
    apiClient
      .get<AuditReadItems>("/admin/audit/read", {
        params: { memories: memories.join(","), facts: facts.join(",") },
      })
      .then((r) => r.data),
};

// ── Agents (agent_id namespaces) ──────────────────────────────────────────
export interface AgentNamespace {
  agent_id: string | null;
  memories: number;
  end_users: number;
  facts: number;
}

export const namespacesApi = {
  list: (scope: ProjectScope) =>
    apiClient
      .get<{ agents: AgentNamespace[]; total: number }>("/admin/namespaces", {
        params: inScope(scope),
      })
      .then((r) => r.data),
};

// ── Tiers (single source of truth, public) ────────────────────────────────


// ── What the model cost, and who caused it ────────────────────────────────
//
// The page the hosted product cannot have. There the model bill is the
// vendor's margin; here it arrives at the operator's own account, so "which of
// my agents burned four hundred euros last month" is their question.
//
// Tokens, never money, unless the operator has written a rate card into
// LLM_RATES. Without one `cost` is null everywhere, which is the honest shape:
// zero would read as "nothing was spent".

export interface UsageRow {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** The part of prompt_tokens the provider served from its prompt cache
   *  (0 before 0.1.21 and for a provider that does not say). */
  cached_tokens?: number;
  cost: number | null;
}

export interface UsageReport {
  since: string;
  days: number;
  currency: string | null;
  rates_configured: boolean;
  models_without_a_rate: string[];
  totals: UsageRow;
  by_stage: (UsageRow & { stage: string })[];
  by_agent: (UsageRow & { agent_id: string | null })[];
  /** One entry per project (1.0.41); `project_id` null holds the calls of
   *  no project (the account's vocabulary, a read across projects, the
   *  calls made before 1.0.41). The entries add up to `totals`. */
  by_project: (UsageRow & { project_id: string | null; name: string | null })[];
  /** The agents past the fifty with the most output, as one entry. */
  other_agents?: (UsageRow & { agents: number }) | null;
  by_model: (UsageRow & { model: string })[];
  calls_without_an_agent_id: number;
}

// The whole account, whichever project the screen shows, with each
// project's share of it in `by_project` (since 1.0.41 a model call is filed
// under the project it worked for). The export is the same window as a CSV
// file, one row per project and model.
export const usageApi = {
  report: (days = 30) =>
    apiClient
      .get<UsageReport>("/admin/usage", { params: { days } })
      .then((r) => r.data),
  exportCsv: (days = 30) =>
    apiClient
      .get<Blob>("/admin/usage/export", { params: { days }, responseType: "blob" })
      .then((r) => r.data),
};


// ── the model provider key ────────────────────────────────────────────────
//
// It used to live only in the install's .env, read once at boot, so a customer
// buying their own Google key meant an SSH session and a restart. Now it is a
// field. The key itself never comes back from the server: what comes back is
// whether one is set, where it came from, its last four characters, and what
// the provider last actually did.

export interface ProviderState {
  provider: string;
  source: "screen" | "env" | null;
  set: boolean;
  last_four: string | null;
  updated_at: string | null;
  updated_by: string | null;
  readable: boolean;
  /** False when the install has no SECRET_KEY, so there is nowhere safe to
   *  put a key and saving will refuse rather than store it in clear text. */
  can_store: boolean;
  last_call: { state: string; seconds_ago?: number; detail?: string };
  /** What the key in force does now (services/provider_key.uses): writes the
   *  memories of the projects in Global, computes the vectors, or neither. */
  uses: { writes: boolean; vectors: boolean };
}

export const providerApi = {
  get: () => apiClient.get<ProviderState>("/admin/provider").then((r) => r.data),
  save: (key: string) =>
    apiClient.put<ProviderState>("/admin/provider", { key }).then((r) => r.data),
  test: (key?: string) =>
    apiClient
      .post<{ ok: boolean; detail: string }>("/admin/provider/test",
        key ? { key } : {})
      .then((r) => r.data),
};

// ── Where the memories are written, chosen at first login ───────────────────

export interface ProviderOption {
  id: string;
  label: string;
  region: "eu" | "global";
  tagline: string;
  key_hint: string;
  /** Where the operator creates a key with this provider. */
  key_url: string;
  key_set: boolean;
  last_four: string | null;
  /** The model saved for this provider, if any. */
  model: string | null;
  /** model -> our measurement note, for the writers we measured. */
  measured: Record<string, string>;
  /** The provider we choose for its region: the first login goes straight to it. */
  recommended?: boolean;
}

export interface ModelsState {
  /** Something writes: the screen's choice, or the provider in `.env`. */
  configured: boolean;
  source: "screen" | "env" | null;
  /** The writer in force, the screen's choice or the one `.env` sets
   *  (`source`); `region` is null for a writer of `.env` at an address none
   *  of the known providers'. */
  active: {
    provider: string;
    label: string;
    region: "eu" | "global" | null;
    model: string;
    last_four: string;
  } | null;
  /** The provider saved for each region: what a project that chose it uses. */
  by_region: Record<
    Region,
    { provider: string; label: string; model: string; source: "screen" | "env" } | null
  >;
  /** Where a project that never chose a region writes: the region of the
   *  writer in force on an install of one's own (null with none), Global on
   *  the Cloud whichever provider was saved last (core/llm.default_region). */
  default_region: Region | null;
  providers: ProviderOption[];
  readable: boolean;
  can_store: boolean;
  updated_at: string | null;
  updated_by: string | null;
  env_provider: string;
  /** `ready`: search by meaning works (local vectors, or Gemini with a key). */
  embeddings: { provider: string; model: string; local: boolean; ready: boolean };
}

export interface ModelsCheck {
  ok: boolean;
  detail: string;
  models: string[];
  /** OpenRouter only: the most used this week, paid endpoints. */
  top: string[];
  /** The writer we suggest from this provider's list. */
  suggested: string | null;
}

export const modelsApi = {
  get: () => apiClient.get<ModelsState>("/admin/models").then((r) => r.data),
  check: (provider: string, key?: string) =>
    apiClient
      .post<ModelsCheck>(apiPath`/admin/models/${provider}/check`, key ? { key } : {})
      .then((r) => r.data),
  save: (provider: string, body: { key?: string; model: string }) =>
    apiClient.put<ModelsState>(apiPath`/admin/models/${provider}`, body).then((r) => r.data),
  /** The Cloud answers 400 `confirmation_required`, naming the regions left
   *  with no writer (`regionsLeftWithoutWriter`), until `confirm` is true
   *  (api/admin.clear_models). */
  clear: (confirm = false) =>
    apiClient
      .delete("/admin/models", confirm ? { params: { confirm: true } } : undefined)
      .then((r) => r.data),
};

// OAuth (api/oauth.py, 16 Oct 2026): an app (ChatGPT, Claude) asks to connect
// and the person signed in here answers, for one of their projects.
export interface OAuthRequest {
  client_name: string;
  // Who receives the answer (services/oauth._where_it_returns): a web host,
  // or, for an app's own address, the scheme whose app on this device opens
  // it. Exactly one of the two is set; never shown as each other.
  redirect_host: string | null;
  redirect_app_scheme: string | null;
  scopes: string[];
}

export const oauthApi = {
  show: (id: string) =>
    apiClient.get<OAuthRequest>(apiPath`/admin/oauth/requests/${id}`).then((r) => r.data),
  approve: (id: string, projectId: string, scopes: string[], users?: string[]) =>
    apiClient
      .post<{ redirect: string }>(apiPath`/admin/oauth/requests/${id}/approve`, {
        project_id: projectId,
        scopes,
        ...(users && users.length ? { users } : {}),
      })
      .then((r) => r.data),
  deny: (id: string) =>
    apiClient
      .post<{ redirect: string }>(apiPath`/admin/oauth/requests/${id}/deny`)
      .then((r) => r.data),
};
