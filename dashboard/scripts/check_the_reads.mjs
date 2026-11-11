// What the dashboard's reads send, run with node: `npm test` (after `npm ci`).
//
// The dashboard has no test runner of its own, and the tests of the
// repository read its source as text (tests/test_the_screen_reads_the_project_it_names.py).
// A source that looks right can send the wrong request: a review mutated it
// on 6 Nov 2026 and 7 changes of 13 went through those tests, among them a scope
// helper that no longer sends `project_id`, and a choice resolved to "every
// project" while the list of projects is still being read. Both bring back
// the defect of 6 Nov 2026: a page that names one project and reads the
// whole account.
//
// So this loads the modules themselves (lib/agentsApi.ts, lib/factPages.ts,
// lib/projectScope.ts) through Vite, as the build compiles them, puts a
// transport in place of the network under the real axios client, its
// interceptors included, and checks the request each read actually sends: a
// project's `project_id`, and none at all for every project. Then it runs the
// switcher's resolver on the cases where a page must read nothing yet, asks
// the TypeScript compiler whether a scope written by hand in a page passes
// (it must not: only lib/projectScope.ts makes one), and renders the
// projects' provider for two accounts to see that each reads its own choice.
//
// Exits non-zero on the first difference.
import assert from "node:assert/strict";
import path from "node:path";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import ts from "typescript";
import { createServer } from "vite";

// What the browser has and node has not: the client reads the session token
// from localStorage on every request.
const stored = new Map();
const asked = [];
globalThis.localStorage = {
  getItem: (k) => (asked.push(k), stored.has(k) ? stored.get(k) : null),
  setItem: (k, v) => stored.set(k, String(v)),
  removeItem: (k) => stored.delete(k),
};

const vite = await createServer({
  appType: "custom",
  logLevel: "error",
  server: { middlewareMode: true, hmr: false, watch: null },
  // No scan of the dependencies for a browser: nothing here is served, and a
  // scan still running when the server closes ends esbuild with a panic.
  optimizeDeps: { noDiscovery: true, include: [] },
});
let failed = false;
try {
  const client = (await vite.ssrLoadModule("/src/lib/apiClient.ts")).default;
  const sent = [];
  // An answer of the shape each read takes apart, so its `.then` runs.
  const answers = {
    "/admin/memories": { memories: [], total: 0 },
    "/admin/memories/count": { total: 0 },
    "/admin/memories/search": [],
    "/admin/memories/categories": [],
    "/admin/facts": { facts: [], total: 0 },
    "/admin/namespaces": { agents: [], total: 0 },
    "/admin/requests": { requests: [], total: 0 },
    "/admin/requests/summary": { by_endpoint: [], writes: 0, reads: 0, total: 0 },
    "/admin/requests/daily": { days: 7, buckets: [], total_writes: 0, total_reads: 0 },
    "/admin/audit": { events: [], total: 0, actions: [] },
  };
  client.defaults.adapter = async (config) => {
    const url = new URL(client.getUri(config), "http://screen.test");
    sent.push({ path: url.pathname, query: url.searchParams });
    assert.ok(url.pathname in answers, `a read of ${url.pathname} that this check does not know`);
    return { data: answers[url.pathname], status: 200, statusText: "OK", headers: {}, config };
  };

  const api = await vite.ssrLoadModule("/src/lib/agentsApi.ts");
  const { factsPage } = await vite.ssrLoadModule("/src/lib/factPages.ts");
  const { resolveScope, choiceKey, scopeOf, EVERY, EVERY_PROJECT } =
    await vite.ssrLoadModule("/src/lib/projectScope.ts");

  // Every read of data that lives in a project, as a page calls it.
  const READS = {
    "/admin/memories": (s) => api.memoriesApi.list(s, 50, 0, "bot", "work", 7),
    "/admin/memories/count": (s) => api.memoriesApi.count(s),
    "/admin/memories/search": (s) => api.memoriesApi.search(s, "a question", 10, "maria"),
    "/admin/memories/categories": (s) => api.memoriesApi.categories(s),
    "/admin/facts": (s) => api.factsApi.list(s, { user_id: "maria", include_invalidated: true }),
    "/admin/facts (a page)": (s) => factsPage(s, { predicate_family: "work" }, 200, 0),
    "/admin/namespaces": (s) => api.namespacesApi.list(s),
    "/admin/requests": (s) => api.requestsApi.list(s, 200),
    "/admin/requests/summary": (s) => api.requestsApi.summary(s),
    "/admin/requests/daily": (s) => api.requestsApi.daily(s, 7),
    "/admin/audit": (s) => api.auditApi.list(s, { user_id: "maria", limit: 100, offset: 0 }),
  };
  const ONE = scopeOf({ id: "3207f66b-9a0c-4c83-b108-caf40913a3ea", name: "Shop" });

  for (const [name, read] of Object.entries(READS)) {
    const path = name.split(" ")[0];
    for (const [scope, expected] of [[ONE, [ONE.id]], [EVERY_PROJECT, []]]) {
      sent.length = 0;
      await read(scope);
      assert.equal(sent.length, 1, `${name}: one request`);
      assert.equal(sent[0].path, path, `${name}: the route`);
      assert.deepEqual(sent[0].query.getAll("project_id"), expected,
        `${name} in ${scope.kind === "project" ? "one project" : "every project"}: project_id sent`);
    }
    console.log(`ok   ${name}: project_id in one project, none in every project`);
  }

  // The switcher's choice, resolved: nothing to read before the list is read,
  // never every project unless chosen or there is no project at all.
  const DEFAULT = { id: "d-1", name: "Default", is_default: true, archived: false };
  const SHOP = { id: "s-1", name: "Shop", is_default: false, archived: false };
  const OLD = { id: "o-1", name: "Old", is_default: false, archived: true };
  const LIST = [DEFAULT, SHOP, OLD];
  const project = (p) => scopeOf(p);
  const CASES = [
    ["the list not read yet, nothing chosen", null, null, true, null],
    ["the list not read yet, a project stored", null, SHOP.id, true, null],
    ["the list not read yet, every project stored", null, EVERY, true, null],
    ["the list read, nothing chosen", LIST, null, false, project(DEFAULT)],
    ["the list read, a project chosen", LIST, SHOP.id, false, project(SHOP)],
    ["the list read, every project chosen", LIST, EVERY, false, EVERY_PROJECT],
    ["the list read again, a project not in it yet", LIST, "new-1", true, null],
    ["a project deleted since, or of another account", LIST, "gone-1", false, project(DEFAULT)],
    ["an archived project", LIST, OLD.id, false, project(DEFAULT)],
    ["no project at all", [], null, false, EVERY_PROJECT],
  ];
  for (const [what, list, chosen, rereading, expected] of CASES) {
    assert.deepEqual(resolveScope(list, chosen, rereading), expected, what);
    console.log(`ok   resolve: ${what}`);
  }
  assert.notEqual(choiceKey("account-a"), choiceKey("account-b"), "one choice per account");

  // The provider itself, rendered for two accounts: each reads the choice
  // kept under its own account (contexts/ProjectContext.tsx), so "All
  // projects" chosen by one is never the next one's.
  const { AuthContext } = await vite.ssrLoadModule("/src/contexts/AuthContext.tsx");
  const { ProjectProvider } = await vite.ssrLoadModule("/src/contexts/ProjectContext.tsx");
  const keyRead = (uid) => {
    asked.length = 0;
    renderToString(createElement(AuthContext.Provider,
      { value: { user: { uid, name: "op", signIn: "password" } } },
      createElement(ProjectProvider, null, null)));
    return asked.filter((k) => k.startsWith("korely_agents_active_project"));
  };
  assert.deepEqual(keyRead("tenant-a"), [choiceKey("tenant-a")], "the choice read for account A");
  assert.deepEqual(keyRead("tenant-b"), [choiceKey("tenant-b")], "the choice read for account B");
  console.log("ok   the choice is kept per account, and read for the account signed in");

  // A scope written by hand in a page does not type-check: only
  // lib/projectScope.ts can make one. The compiler, with the dashboard's
  // tsconfig, on a file of the source tree that is never written to disk.
  const root = process.cwd();
  const parsed = ts.getParsedCommandLineOfConfigFile(
    path.join(root, "tsconfig.json"), {}, { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} });
  const probe = path.join(root, "src", "pages", "ScopeProbe.ts");
  const lines = [
    'import { memoriesApi } from "@/lib/agentsApi";',
    'import { EVERY_PROJECT, scopeOf } from "@/lib/projectScope";',
    "void memoriesApi.count(EVERY_PROJECT);",
    'void memoriesApi.count(scopeOf({ id: "p-1", name: "Shop" }));',
    'void memoriesApi.count({ kind: "every" });',
    'void memoriesApi.count({ kind: "project", id: "p-1", name: "Shop" });',
  ];
  const host = ts.createCompilerHost(parsed.options);
  const readSource = host.getSourceFile.bind(host);
  const exists = host.fileExists.bind(host);
  host.getSourceFile = (f, ...rest) =>
    path.resolve(f) === probe
      ? ts.createSourceFile(f, lines.join("\n"), ts.ScriptTarget.ES2020, true)
      : readSource(f, ...rest);
  host.fileExists = (f) => path.resolve(f) === probe || exists(f);
  const program = ts.createProgram([probe], parsed.options, host);
  const refused = ts.getPreEmitDiagnostics(program, program.getSourceFile(probe))
    .map((d) => d.file.getLineAndCharacterOfPosition(d.start).line + 1);
  assert.deepEqual([...new Set(refused)].sort(), [5, 6],
    `tsc refuses a scope written by hand, and only that (lines refused: ${refused})`);
  console.log("ok   a scope written by hand in a page does not type-check");
} catch (e) {
  failed = true;
  console.error(`FAIL ${e.message}`);
} finally {
  await vite.close();
}
process.exit(failed ? 1 : 0);
