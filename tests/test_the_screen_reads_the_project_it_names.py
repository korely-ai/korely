"""The dashboard reads the project its switcher names (6 Nov 2026).

With a project of 9 facts chosen at the top the Memory Graph drew the
2,808 facts of the whole account: the switcher was a label for the keys and
the Playground, and Memory Graph, Memories, Facts, Agents, Requests, the Audit
log and the Home page's numbers read every project whatever it said.

The dashboard has no test runner of its own (package.json: tsc and vite), so
these read its source, as tests/test_the_screen_says_what_it_shows.py does:

- every route of the server that takes `project_id` (api/admin.PROJECT_FILTER)
  is called by a read that takes a `ProjectScope`, required, and sends it, so
  a read cannot leave the project out by forgetting it (tsc holds the calls);
- every data page sits under InProjectScope, reads `useProjectScope()` and
  says what it shows (ProjectScopeBanner); no page outside it reads them;
- "All projects", the whole account, is a choice of its own in the switcher,
  and the counts that stay the account's say so.

Reading the source is not enough to hold the requests themselves: the
mutations of a review (6 Nov 2026) went through 7 times in 13. What each read sends, and
the switcher's resolver, are run with node by dashboard/scripts/check_the_reads.mjs
(`npm test`, in the CI's dashboard job); here, that it covers every route that
takes a project, and that every data page hands its reads the scope it was
given, never one of its own.
"""
import re
from pathlib import Path

import pytest

from tests import cloud

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dashboard" / "src"

# The pages of the data, under InProjectScope in App.tsx.
DATA_PAGES = {"HomePage", "PlaygroundPage", "AgentsPage", "MemoriesPage", "GraphPage",
              "FactsPage", "RequestsPage", "AuditPage"}

# The reads of data that lives in a project (lib/agentsApi.ts, lib/factPages.ts,
# and the graph's own reader of pages).
SCOPED_READS = ("memoriesApi.list(", "memoriesApi.search(", "memoriesApi.categories(",
                "memoriesApi.count(", "factsApi.list(", "factsPage(", "requestsApi.",
                "auditApi.list(", "namespacesApi.list(")
_A_READ = re.compile(r"\b(memoriesApi\s*\.\s*(?:list|search|categories|count)"
                     r"|factsApi\s*\.\s*list|factsPage"
                     r"|requestsApi\s*\.\s*(?:list|summary|daily)|auditApi\s*\.\s*list"
                     r"|namespacesApi\s*\.\s*list|readGraph)\b(\s*\()?")


def _code(rel: str) -> str:
    """The file without its comments."""
    text = (SRC / rel).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"\{/\*.*?\*/\}", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def _routes_taking_the_project() -> set:
    from korely_agent.api.admin import router

    return {r.path for r in router.routes
            if any(p.name == "project_id" for p in getattr(r, "dependant").query_params)}


def test_the_routes_that_take_a_project_are_the_reads_of_the_screens_data():
    """A new route that takes `project_id` lands here, and somebody decides
    which read of the screen sends it."""
    assert _routes_taking_the_project() == {
        "/admin/memories", "/admin/memories/count", "/admin/memories/search",
        "/admin/memories/categories",
        "/admin/facts", "/admin/namespaces", "/admin/requests", "/admin/requests/summary",
        "/admin/requests/daily", "/admin/audit"}


@pytest.mark.parametrize("path", sorted(_routes_taking_the_project()))
def test_every_read_of_such_a_route_takes_the_scope_and_sends_it(path):
    api = _code("lib/agentsApi.ts")
    at = api.index(f'"{path}"')
    # The method of the API object that names the path: from its key to the next.
    start = max(m.start() for m in re.finditer(r"\n  \w+: ", api[:at]))
    end = re.search(r"\n  \w+: |\n};", api[at:])
    method = api[start:at + (end.start() if end else len(api) - at)]
    assert "scope: ProjectScope" in method, (path, method)
    assert "inScope(scope)" in method, (path, method)


def test_a_scope_is_one_project_or_every_project_and_only_its_module_makes_one():
    """The type carries a mark no other module can name (a review's
    mutations, 6 Nov 2026: a page could write `{ kind: "every" }` and read the account);
    check_the_reads.mjs asks the compiler that such a page does not
    type-check. No module but lib/projectScope.ts asserts its way past it."""
    scope = _code("lib/projectScope.ts")
    kind = scope.split("export type ProjectScope =", 1)[1].split("export const EVERY_PROJECT", 1)[0]
    assert '{ readonly kind: "project"; readonly id: string; readonly name: string }' in kind
    assert '{ readonly kind: "every" }' in kind
    assert "& { readonly [madeHere]: true }" in kind
    assert "declare const madeHere: unique symbol;" in scope
    assert "export declare const madeHere" not in scope and "export { madeHere" not in scope
    api = _code("lib/agentsApi.ts")
    assert 'export type { ProjectScope } from "@/lib/projectScope";' in api
    assert "project_id?: string;" not in api.split("export interface FactsQuery", 1)[1] \
        .split("}", 1)[0], "the project is the read's scope, not an optional filter"
    for path in SRC.rglob("*.ts*"):
        if path.name != "projectScope.ts":
            code = _code(str(path.relative_to(SRC)))
            assert not re.search(r"\bas\s+(unknown\s+as\s+)?ProjectScope\b", code), path


def test_every_data_page_is_under_the_scope_and_only_those():
    app = _code("App.tsx")
    block = app.split("<Route element={<InProjectScope />}>", 1)[1].split("</Route>\n", 1)[0]
    assert set(re.findall(r"element=\{<(\w+) />\}", block)) == DATA_PAGES


def _the_parts_pages() -> list:
    """The page of each part of the screen the Cloud's tree carries, found
    as lib/cloud.ts finds the parts (hosted/*/part.ts): the module its lazy
    `Page` imports. None in the public repository."""
    pages = []
    for part in sorted((SRC / "hosted").glob("*/part.ts")):
        found = re.search(r'import\("\./(\w+)"\)', part.read_text(encoding="utf-8"))
        assert found, f"{part}: no page imported"
        pages.append(str((part.parent / f"{found.group(1)}.tsx").relative_to(SRC)))
    return pages


def test_the_parts_pages_are_pages_of_the_data():
    """A part of the product the Cloud carries (core/extensions.Part) keeps
    its data in a project as the core does, so its page is mounted under
    InProjectScope, reads the scope and says it. Found when the Cloud's line
    took main on 7 Nov 2026: a part's page, outside, took the project from
    the switcher's own state, which is no project for "All projects"."""
    app = _code("App.tsx")
    block = app.split("<Route element={<InProjectScope />}>", 1)[1].split("</Route>\n", 1)[0]
    assert "parts.map((p) => (" in block
    assert app.count("parts.map(") == 1 and "screenParts" not in app
    for page in _the_parts_pages():
        code = _code(page)
        assert "useProjectScope()" in code, page
        assert "<ProjectScopeBanner" in code, page
        assert "useProjects()" not in code, (page, "the scope, not the switcher's own state")


def _parts_reads_taking_the_project() -> set:
    """The reads (GET) the routes of the parts serve that take `project_id`
    (core/extensions.Part.routers); none in the public repository."""
    from korely_agent.core import extensions

    return {r.path for part in extensions.parts() for router in part.routers()
            for r in router.routes
            if "GET" in getattr(r, "methods", ()) and any(
                p.name == "project_id" for p in getattr(r, "dependant").query_params)}


def test_every_read_of_a_parts_route_takes_the_scope_and_is_run_with_node():
    """What the core's reads must do (above), a part's read of the screen
    does too: in its own module of calls (hosted/<part>/api.ts), the method
    that reads such a route takes a `ProjectScope` and sends it, and the
    part's reads.check.ts hands it to check_the_reads.mjs, which runs it."""
    routes = _parts_reads_taking_the_project()
    for api_file in sorted((SRC / "hosted").glob("*/api.ts")):
        api = _code(str(api_file.relative_to(SRC)))
        checked = api_file.with_name("reads.check.ts")
        for path in routes:
            # The route as a literal of any of JavaScript's three quotes
            # (a template, as apiPath`...` writes one, among them).
            literal = r"([\"'`])" + re.escape(path) + r"\1"
            for at in [m.start() for m in re.finditer(literal, api)]:
                start = max(m.start() for m in re.finditer(r"\n  \w+: ", api[:at]))
                end = re.search(r"\n  \w+: |\n};", api[at:])
                method = api[start:at + (end.start() if end else len(api) - at)]
                if ".get<" not in method and ".get(" not in method:
                    continue
                assert "scope: ProjectScope" in method, (api_file.name, path, method)
                assert "inScope(scope)" in method, (api_file.name, path, method)
                assert checked.exists(), f"{api_file.parent.name}: {path} is read and not run"
                assert re.search(r'"' + re.escape(path) + r'( \([^)]*\))?": \(s\) =>',
                                 checked.read_text(encoding="utf-8")), (checked, path)
    script = (ROOT / "dashboard" / "scripts" / "check_the_reads.mjs").read_text(encoding="utf-8")
    assert '"reads.check.ts"' in script and "ssrLoadModule(`/src/hosted/${part}/reads.check.ts`)" in script


@pytest.mark.parametrize("page", sorted(DATA_PAGES))
def test_each_data_page_reads_the_scope_and_says_it(page):
    code = _code(f"pages/{page}.tsx")
    assert "useProjectScope()" in code
    assert "<ProjectScopeBanner" in code
    assert "useProjects()" not in code, "the scope, not the switcher's own state"


def test_no_screen_outside_the_scope_reads_a_projects_data():
    outside = [p for p in (SRC / "pages").glob("*.tsx") if p.stem not in DATA_PAGES]
    outside += list((SRC / "components").glob("*.tsx")) + list((SRC / "hosted").glob("*.tsx"))
    for path in outside:
        code = _code(str(path.relative_to(SRC)))
        found = [r for r in SCOPED_READS if r in code]
        assert not found, (path.name, found)


@pytest.mark.parametrize("page", sorted(DATA_PAGES))
def test_every_read_of_a_data_page_is_handed_the_pages_scope(page):
    """The first argument of each read is the `scope` the page was given,
    never a scope of its own, and no read is passed around uncalled (a
    `useLoad(memoriesApi.categories, ...)` read the account)."""
    # The imports name a read without calling it.
    code = re.sub(r"^import\b[^;]*;", "", _code(f"pages/{page}.tsx"), flags=re.M)
    found = 0
    for m in _A_READ.finditer(code):
        if code[:m.start()].rstrip().endswith(("function", "async function")):
            continue  # the graph's reader itself
        assert m.group(2), f"{page}: {m.group(1)} passed around, not called with the scope"
        first = re.split(r"[,)]", code[m.end():], maxsplit=1)[0].strip()
        assert first == "scope", f"{page}: {m.group(1)}({first}, ...)"
        found += 1
    assert found, f"{page} reads nothing in a project"


def test_no_page_makes_a_scope_of_its_own():
    """Every project is the switcher's choice, never a page's: a page that
    wrote `{ kind: "every" }` or EVERY_PROJECT read the account under the
    name of a project."""
    for path in [*(SRC / "pages").glob("*.tsx"), *(SRC / "components").glob("*.tsx")]:
        code = _code(str(path.relative_to(SRC)))
        imports = " ".join(re.findall(r"^import\b[^;]*;", code, flags=re.M))
        assert not re.search(r"\bEVERY_PROJECT\b", imports), path.name
        assert not re.search(r"kind:\s*\"(?:every|project)\"", code), path.name


def test_what_the_reads_send_is_run_with_node_for_every_route_that_takes_a_project():
    script = (ROOT / "dashboard" / "scripts" / "check_the_reads.mjs").read_text(encoding="utf-8")
    for path in _routes_taking_the_project():
        assert f'"{path}": (s) =>' in script, f"check_the_reads.mjs does not read {path}"
    assert 'ssrLoadModule("/src/lib/agentsApi.ts")' in script
    assert 'ssrLoadModule("/src/lib/projectScope.ts")' in script
    # The compiler on a page that writes a scope by hand, and the provider
    # rendered for two accounts.
    assert "ts.createProgram([probe]" in script and "[5, 6]" in script
    assert 'ssrLoadModule("/src/contexts/ProjectContext.tsx")' in script
    assert "renderToString(" in script
    package = (ROOT / "dashboard" / "package.json").read_text(encoding="utf-8")
    assert ('"test": "node scripts/check_the_reads.mjs && node scripts/check_the_behaviour.mjs'
            ' && node scripts/check_the_login_bundle.mjs"') in package
    ci = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    dashboard_job = ci.split("\n  dashboard:\n", 1)[1]
    assert "- run: npm test" in dashboard_job
    # And at the gate and before a tag: `make test-all` (scripts/release.sh
    # runs it) runs `make dashboard-test`, which runs npm test (review of 6
    # Nov 2026: the CI runs on tags only, and nothing else ran it).
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^test-all: .*\bdashboard-test\b", make, flags=re.M)
    target = make.split("\ndashboard-test:\n", 1)[1].split("\n\n", 1)[0]
    assert "node:20-alpine" in target and "npm ci" in target and "npm test" in target
    assert "make test-all" in (ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")


def test_the_scope_is_resolved_in_the_render_and_a_change_mounts_the_page_again():
    """An effect resolving the stored choice left one render in which a page
    could read a project deleted since, or the whole account; the gate
    renders nothing before the list is read and says a failed read. What
    the resolver answers is run by check_the_reads.mjs."""
    ctx = _code("contexts/ProjectContext.tsx")
    assert "const resolved = resolveScope(list, chosen, projectsLoad.loading);" in ctx
    assert "useMemo(() => resolved, [scopeKey, scopeName])" in ctx
    assert "setActiveIdState" not in ctx and "setChosen((cur" not in ctx
    gate = ctx.split("export function InProjectScope()", 1)[1].split("\n}\n", 1)[0]
    assert "projectsLoad.error ? (" in gate and "<LoadNote load={projectsLoad} />" in gate
    assert "<Outlet key={scopeKeyOf(scope)} />" in gate


def test_all_projects_is_a_choice_of_its_own():
    switcher = _code("components/ProjectSwitcher.tsx")
    assert "chooseEveryProject();" in switcher
    assert switcher.count("All projects") == 2, "the entry and the button that names it"
    assert "The whole account, every project together" in switcher
    ctx = _code("contexts/ProjectContext.tsx")
    assert "const chooseEveryProject = useCallback(() => setChosen(EVERY), []);" in ctx
    scope = _code("lib/projectScope.ts")
    assert 'export const EVERY = "every";' in scope
    assert "if (chosen === EVERY) return EVERY_PROJECT;" in scope


def test_the_choice_is_kept_per_account_and_another_account_starts_afresh():
    """One key for the whole browser kept "All projects" from one account's
    sign-in to the next one's (review of 6 Nov 2026)."""
    ctx = _code("contexts/ProjectContext.tsx")
    provider = ctx.split("export function ProjectProvider", 1)[1].split("\n}\n", 1)[0]
    assert "const account = user?.uid" in provider
    assert "<AccountProjects key={account} account={account}>" in provider
    assert "localStorage.getItem(choiceKey(account))" in ctx
    assert "localStorage.setItem(choiceKey(account), scopeKey)" in ctx
    assert "localStorage.removeItem(OLD_STORAGE_KEY)" in ctx


def test_the_banner_names_the_project_or_every_project():
    shell = _code("components/PageShell.tsx")
    banner = shell.split("export function ProjectScopeBanner", 1)[1].split("\n}\n", 1)[0]
    assert "useProjectScope()" in banner
    assert 'scope.kind === "project"' in banner and "{scope.name}" in banner
    assert "every project" in banner


def test_the_counts_that_stay_the_accounts_say_so():
    """The model's calls are not filed by project (agent_llm_usage has no
    project column), and the plan counts every project together."""
    from korely_agent.db.models import AgentLlmUsage

    assert "project_id" not in AgentLlmUsage.__table__.columns, \
        "the model usage has a project now: read it in the scope"
    assert "account · model, 30 days" in _code("components/Sidebar.tsx")
    assert "in the whole account" in _code("pages/UsagePage.tsx")
    if cloud.HERE:
        assert "account · {label}" in _code("hosted/usage.tsx")
