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
    assert "Written by" in page and "r.client" in page
    assert "detail.client" in page


def test_the_home_page_names_no_project_on_the_accounts_numbers():
    assert "at a glance" in _code("HomePage.tsx")
    assert "activeProject" not in _code("HomePage.tsx")


def test_the_playground_reads_the_active_project_and_says_when_it_failed():
    page = _code("PlaygroundPage.tsx")
    assert "activeProject" in page and "project_id" in page
    assert 'status === "rejected"' in page and "hitsError" in page and "factsError" in page


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
    text on every page (scripts/check_dashboard_in_a_browser.mjs)."""
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
    browser = (ROOT / "scripts" / "check_dashboard_in_a_browser.mjs").read_text(encoding="utf-8")
    assert '"from null"' in browser and '"Invalid Date"' in browser


def test_the_graph_fits_its_nodes_unless_one_was_moved_by_hand():
    """Three nodes in the 920-unit frame drew labels about 5 px tall at the
    README's width (29 Oct 2026): the view closes in on the nodes once the
    layout settles, and a node dragged by hand keeps the view where it is."""
    graph = (ROOT / "dashboard" / "src" / "components" / "ForceGraph.tsx").read_text(encoding="utf-8")
    assert "viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}" in graph
    settled = graph.split("raf.current = null;", 1)[1][:200]
    assert "if (!movedByHand.current)" in settled and "fitView(nodes" in settled
    assert "movedByHand.current = true;" in graph.split("function onPointerMove", 1)[1].split("function", 1)[0]
    # The drag maps the pointer through the view on screen, not the full frame.
    assert "v.x + ((e.clientX - r.left) / r.width) * v.w" in graph
