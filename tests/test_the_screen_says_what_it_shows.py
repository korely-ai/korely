"""What the dashboard says, against what it reads (audit of 17 Oct 2026).

The dashboard has no test runner of its own (package.json: tsc and vite), so
these read its source, as tests/test_no_hosted_leftovers.py does, for the
sentences that disagreed with the product:

- the Home page put the active project's name on numbers of the whole account;
- the Playground said "the exact memories and facts your agent would get back"
  while reading the whole account, and turned a failed search into "No
  memories matched";
- the Requests page told the Cloud's customers that nothing is metered, and
  classified rows by the endpoint's suffix instead of the server's `kind`;
- the Agents page showed plan caps and a 403 on an install with no plans, and
  on the Cloud a 403 where the service answers 429, without the Scale plan.
"""
import re
from pathlib import Path

from tests import cloud

ROOT = Path(__file__).resolve().parent.parent
PAGES = ROOT / "dashboard" / "src" / "pages"


def _code(name: str) -> str:
    """The page without its comments."""
    text = (PAGES / name).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def test_the_memories_page_says_which_tool_wrote_each_memory():
    """`client` (Claude Code, Cursor...) is stored on every memory since 0.1.36
    and /admin/memories answers it; the page showed it nowhere, in the list or
    in the drawer (audit of 17 Oct 2026)."""
    api = (ROOT / "dashboard" / "src" / "lib" / "agentsApi.ts").read_text(encoding="utf-8")
    memory = api[api.index("export interface Memory {"):]
    assert re.search(r"^\s*client: string \| null;", memory[:memory.index("}")], flags=re.M)
    page = _code("MemoriesPage.tsx")
    # The column names the agent and, under it, the tool (pre-tester check
    # of 15 Nov 2026: it showed the tool only); MemoriesPage.behaviour.tsx
    # draws it.
    assert "Written by" in page and "<WrittenBy memory={r} />" in page
    assert "const { agent_id: agent, client } = memory;" in page
    assert "detail.client" in page


def test_the_home_page_names_the_scope_of_its_numbers():
    """The Home page put the active project's name on the whole account's
    numbers (audit of 17 Oct 2026), then named the account on them whatever
    the switcher said. Since 6 Nov 2026 its memories and calls are read in
    the project chosen at the top, and it names that project; the model's
    calls and the plan's writes are the account's, tagged `account`."""
    page = _code("HomePage.tsx")
    assert "at a glance" in page and "useProjectScope()" in page
    assert "memoriesApi.count(scope)" in page and "requestsApi.summary(scope)" in page
    assert "memoriesApi.list(" not in page, "a count is not a read of a memory"
    assert "scope.name" in page
    assert page.count("            account\n") == 2, "both account-wide cards are tagged"
    assert "activeProject" not in page


def test_the_playground_reads_the_chosen_project_and_says_when_it_failed():
    page = _code("PlaygroundPage.tsx")
    assert "useProjectScope()" in page
    assert "memoriesApi.search(scope," in page and "factsApi.list(scope," in page
    assert 'status === "rejected"' in page and "hitsError" in page and "factsError" in page


def test_the_playground_asks_what_the_served_parts_add_to_the_search():
    """The Rilievo of 3 Nov 2026: the Playground searched the memories
    alone. It asks the one search for the kinds the parts the account is
    served add (lib/playground.ts, its behaviour in node:
    lib/playground.behaviour.ts), keeps what the agent would get, and has
    each part draw its own results; with none, the request of before."""
    page = _code("PlaygroundPage.tsx")
    assert "const searches = searchesOf(useScreenParts().parts);" in page
    assert "memoriesApi.search(scope, q, 10, eu || undefined, kindsToAsk(searches))" in page
    assert "whatTheAgentGets(h.value, eu, searches)" in page
    assert "const part = partOf(h, searches);" in page and "<part.Found " in page
    assert "user_id === eu" not in page, "the end user's narrowing is lib/playground.ts's"


def test_the_requests_page_says_what_is_metered_by_mode_and_reads_the_servers_kind():
    page = _code("RequestsPage.tsx")
    assert "useMode()" in page
    assert "Nothing here is metered" in page.split("hosted ?", 1)[1], \
        "the self-hosted sentence shows on the Cloud too"
    assert "WRITE_ACTIONS" not in page and "kindOf" not in page
    assert "r.kind" in page and "row.kind" in page


def test_the_agents_page_shows_plans_only_on_the_cloud_and_from_the_server():
    page = _code("AgentsPage.tsx")
    assert "Plan caps" not in page and "403" not in page
    assert "useMode()" in page and "<cloud.AgentCaps />" in page
    if cloud.HERE:
        caps = (ROOT / "dashboard" / "src" / "hosted" / "AgentCaps.tsx").read_text()
        assert "hostedApi.plans()" in caps and "429" in caps


def test_the_consent_page_names_the_erasure_the_write_scope_allows():
    """The consent page called memories:write 'Write memories'; the same scope
    erases an end user for good (audit of 17 Oct 2026). Since the second
    reading of that day the page shows the server's own words for each scope
    (GET /admin/scopes, the ones API Keys shows), not a copy of its own."""
    from korely_agent.api.admin import list_scopes

    page = _code("OAuthConsentPage.tsx")
    assert '"Write memories"' not in page
    assert "keysApi.scopes()" in page and "SCOPE_LABELS" not in page
    opens = {s["name"]: s["opens"] for s in list_scopes()["scopes"]}
    assert "erasing an end user for good" in opens["memories:write"]


def test_a_fact_shows_its_dates_and_never_a_missing_one():
    """The API answers every fact's `valid_from`, `invalid_at` and
    `closed_known_at`; the Facts page and the graph's side panel showed none
    of them, only "ended" or "superseded" (the demo of 29 Oct 2026). Both read
    them through lib/factPages.ts, which gives null for a missing or unreadable
    date, so a fact never reads "from null"; the browser check looks for that
    text on every page (scripts/check_dashboard_in_a_browser.mjs, through
    `visitPages` of scripts/browser_sign_in.mjs)."""
    lib = (ROOT / "dashboard" / "src" / "lib" / "factPages.ts").read_text(encoding="utf-8")
    assert "Number.isNaN(t) ? null : new Date(t).toISOString().slice(0, 10)" in lib
    assert "knownOn: ended && known && known > ended ? known : null" in lib
    assert '"Known then"' in lib, "known on is the graph's second clock, not a new idea"
    for page in ("FactsPage.tsx", "GraphPage.tsx"):
        code = _code(page)
        assert "factDates(f)" in code, page
        # Each date only where it exists, the end next to its name.
        assert re.search(r"dates\.from && .*?from \$?\{[\w.]*dates\.from\}", code, flags=re.S), page
        assert re.search(r"closedLabel\(f\)\}\{[\w.]*dates\.ended && ` \$\{[\w.]*dates\.ended\}`\}", code), page
        assert re.search(r"dates\.knownOn && .*?known on \{[\w.]*dates\.knownOn\}", code, flags=re.S), page
        assert "valid_from}" not in code and "invalid_at}" not in code, page
    browser = (ROOT / "scripts" / "browser_sign_in.mjs").read_text(encoding="utf-8")
    assert '"from null"' in browser and '"Invalid Date"' in browser


def test_the_graph_is_drawn_by_sigma_and_stays_readable_at_a_thousand_facts():
    """The hand-rolled SVG graph drew every label of 959 nodes as one grey ball
    (screenshot of 1 Nov 2026). The page draws with Sigma.js on WebGL, lays out
    with ForceAtlas2, draws a label only for nodes big enough to carry one, and a
    node under the pointer (or selected) is the focus: at most
    LABELLED_NEIGHBOURS of its neighbours are named, and the rest fades. The
    first version named every neighbour of a hub and drew a wall of labels
    (screenshot of 2 Nov 2026), so the bound lives in one place, graphFocus.ts,
    for the 2D and the 3D view; a closed fact is drawn fainter than a current
    one."""
    src = ROOT / "dashboard" / "src"
    graph = (src / "components" / "FactGraph.tsx").read_text(encoding="utf-8")
    focus = (src / "lib" / "graphFocus.ts").read_text(encoding="utf-8")
    assert 'import Sigma from "sigma"' in graph
    assert "forceAtlas2.assign(" in graph and "barnesHutOptimize: g.order > 400" in graph
    assert "labelRenderedSizeThreshold" in graph and "labelDensity" in graph
    assert "export const LABELLED_NEIGHBOURS = 12" in focus
    assert "labelled.size >= LABELLED_NEIGHBOURS" in graph, "the neighbours that are named are bounded"
    assert 'out.labelVisibility = "hidden"' in graph and 'out.visibility = "hidden"' in graph
    assert "labelVisibility = \"visible\"" in graph
    assert "sh.focus.ranked.slice" not in graph and "g.areNeighbors" not in graph, (
        "no label is forced on every neighbour of the focused node"
    )
    assert "a.invalidated ? 0.45 : 1" in graph, "a closed fact is fainter, not hidden"
    assert "s.kill()" in graph, "the WebGL context is freed when the graph changes or the page leaves"
    assert "layout?.kill()" in graph, "the layout worker is freed with it"
    page = (src / "pages" / "GraphPage.tsx").read_text(encoding="utf-8")
    assert "<FactGraph" in page and "ForceGraph" not in page


def test_the_graph_can_be_enlarged_zoomed_and_focused():
    """The graph has a full-screen button (the browser's fullscreen, and a
    full-window layer of the page where there is none, closed by Esc), zoom
    buttons, a pause for the layout, and a focus mode that draws only the
    selected node and its strongest neighbours, says how many it leaves out and
    offers the way back."""
    src = ROOT / "dashboard" / "src"
    page = (src / "pages" / "GraphPage.tsx").read_text(encoding="utf-8")
    assert "requestFullscreen" in page and "useDismiss(stage, enlarged" in page, "Esc goes through the shared hook"
    assert 'aria-label="Zoom in"' in page and 'aria-label="Zoom out"' in page
    assert "toggleLayout()" in page and "setFocusOnly" in page
    graph = (src / "components" / "FactGraph.tsx").read_text(encoding="utf-8")
    assert "Leave focus" in graph and "more`" in graph, "the focus mode says how much it leaves out"
    assert "prefers-reduced-motion" in graph, "the camera does not glide for a person who asked for calm"
    assert "__sigma" not in graph, "no debugging hook in the shipped component"


def test_the_graph_has_a_3d_view_that_is_fetched_only_when_asked_for():
    """The 3D view (3d-force-graph on three.js, MIT) is a second way to look at the
    same nodes and facts. three.js is most of the weight, so it is a lazy chunk the
    2D page never loads, and the WebGL context is freed when the view closes."""
    view = (ROOT / "dashboard" / "src" / "components" / "FactGraph3D.tsx").read_text(encoding="utf-8")
    assert 'from "3d-force-graph"' in view and "new ForceGraph3D(el)" in view
    assert "_destructor()" in view, "the WebGL context is freed when the view closes"
    assert "pick.current((n as GNode).id)" in view and "onBackgroundClick" in view
    assert "focusOn(adj, degree, s, false)" in view, "the 3D view lights the same bounded neighbourhood"
    page = (ROOT / "dashboard" / "src" / "pages" / "GraphPage.tsx").read_text(encoding="utf-8")
    assert 'lazy(() => import("@/components/FactGraph3D"))' in page
    assert 'import { FactGraph3D }' not in page, "a static import would put three.js in the 2D page"
