"""The dashboard against the server it shows (audit of 17 Oct 2026, G6).

The dashboard has no test runner of its own (package.json: tsc and vite), so
these read its source, as tests/test_the_screen_says_what_it_shows.py does,
for what the audit found copied, stale or said for the wrong product:

- the Playground still described a keyword search removed on 12 Oct 2026;
- the predicate families were a copy of nine of the engine's ten, without
  `identity`, so a rename or an alias could not be filtered;
- the Projects page kept its own copy of the projects, so the switcher
  showed a renamed or deleted project until a reload, and so did the
  Webhooks page's project picker;
- the Cloud's screens spoke of "this install", of a provider "saved in
  Settings" that the service refuses to an account, and of installation help;
- Enter in the "Create key" dialog went past the rule of its disabled button.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests import cloud

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dashboard" / "src"


def _code(rel: str) -> str:
    """A dashboard file without its comments."""
    text = (SRC / rel).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin(db, tenant):
    from korely_agent.services import keys as key_service

    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE],
        expires_in_days=None)
    return {"Authorization": f"Bearer {full}"}


def test_the_playground_says_the_search_is_by_meaning():
    """HYBRID_SEARCH left on 12 Oct 2026 (CONTRIBUTING.md); the help still said
    'semantic + keyword'."""
    page = _code("pages/PlaygroundPage.tsx")
    assert "keyword" not in page.lower()
    assert re.search(r"by\s+meaning", page)


# ── the predicate families ──────────────────────────────────────────────────

def test_the_server_serves_the_engines_families_in_its_order(client, admin):
    from korely_agent.services.memory_core.families import FAMILY_ORDER

    body = client.get("/admin/facts/families", headers=admin).json()
    assert [f["name"] for f in body["families"]] == list(FAMILY_ORDER)
    assert {"name": "identity", "label": "Identity"} in body["families"]


def test_the_filters_and_the_legend_read_the_served_families():
    lib = _code("lib/families.ts")
    assert "FAMILIES =" not in lib and "/admin/facts/families" in lib
    for page in ("pages/FactsPage.tsx", "pages/GraphPage.tsx"):
        code = _code(page)
        assert "useFamilies()" in code and "FAMILIES" not in code, page


def test_every_family_of_the_engine_has_a_color_on_the_screen():
    """The colors stay the screen's; a family the engine adds without one
    would be drawn as 'other', which is how `identity` was drawn."""
    from korely_agent.services.memory_core.families import FAMILY_ORDER

    lib = _code("lib/families.ts")
    colors = lib.split("FAMILY_COLORS", 1)[1].split("};", 1)[0]
    named = set(re.findall(r"^\s*(\w+):\s*\"#", colors, flags=re.M))
    assert set(FAMILY_ORDER) <= named, set(FAMILY_ORDER) - named


# ── one list of projects ────────────────────────────────────────────────────

def test_the_projects_page_changes_the_switchers_list_not_a_copy():
    page = _code("pages/ProjectsPage.tsx")
    assert "useProjects()" in page
    assert "projectsApi.list" not in page and "setProjects" not in page
    context = _code("contexts/ProjectContext.tsx")
    assert "projectsLoad" in context and "projectsApi.list()" in context


def test_no_screen_inside_the_shell_reads_a_list_of_projects_of_its_own():
    """The Webhooks page still read its own copy after the Projects page
    stopped (audit of 17 Oct 2026): a project renamed there kept its
    old name in the endpoint picker until a reload. The one list is the
    switcher's; the OAuth consent page sits outside the shell (App.tsx, no
    ProjectProvider around it) and reads once for its own answer."""
    outside = {"contexts/ProjectContext.tsx", "pages/OAuthConsentPage.tsx"}
    readers = sorted(
        str(f.relative_to(SRC)) for f in SRC.rglob("*.ts*")
        if "projectsApi.list" in _code(str(f.relative_to(SRC)))
    )
    assert set(readers) <= outside, set(readers) - outside
    assert "useProjects()" in _code("pages/WebhooksPage.tsx")


# ── Enter in "Create key" stops where the button does ───────────────────────

def test_enter_in_the_create_dialog_is_held_by_the_buttons_rule():
    """Enter in the name field called submit past the disabled button: two
    POSTs on a double Enter, and a key with no scopes with nothing ticked."""
    page = _code("pages/ApiKeysPage.tsx")
    dialog = page.split("function CreateKeyDialog", 1)[1].split("\nfunction ", 1)[0]
    assert 'e.key === "Enter" && submit()' in dialog
    body = dialog.split("const submit = async () => {", 1)[1]
    assert body.lstrip().startswith("if (blocked) return;")
    # The button is the dialogs' shared foot (ModalActions).
    assert "confirmDisabled={blocked}" in dialog
    assert "const blocked = busy || (scopes !== null && scopes.length === 0);" in dialog


def test_the_server_refuses_a_key_with_no_scopes(client, admin):
    r = client.post("/admin/keys", headers=admin, json={"name": "nothing", "scopes": []})
    assert r.status_code == 422, r.text
    left_out = client.post("/admin/keys", headers=admin, json={"name": "default"})
    assert left_out.status_code == 201 and left_out.json()["scopes"]


# ── the Cloud is not an install ─────────────────────────────────────────────

def _cloud_branch(code: str, sentence: str) -> None:
    """`sentence`, a self-hosted one, is said only where `hosted` is false:
    it sits after a `hosted ?` whose Cloud half comes first."""
    assert sentence in code, sentence
    before = code.split(sentence, 1)[0]
    assert "hosted" in before.rsplit("\n\n", 1)[-1] or "hosted ?" in before[-600:], sentence


def test_the_cloud_is_told_about_its_account_not_an_install():
    shell = _code("components/PageShell.tsx")
    assert "useMode()" in shell and "in your account" in shell
    _cloud_branch(shell, '"on this install"')

    region = _code("components/RegionSelect.tsx")
    assert "service's provider for Europe" in region
    _cloud_branch(region, "provider you saved for Europe in Settings")
    _cloud_branch(region, "Follow this install")
    _cloud_branch(region, "saved for this region in Settings")
    assert "REGION_HELP" not in region

    projects = _code("pages/ProjectsPage.tsx")
    assert "useMode()" in projects and "followsLabel(hosted)" in projects
    assert "waitingReason(hosted)" in projects
    _cloud_branch(projects, "you own the install")
    assert "Follows this install" not in projects and "in Settings" not in projects

    settings = _code("pages/SettingsPage.tsx")
    support = settings.split("Help &amp; support", 1)[1]
    # The address its own refusals name, read from lib/docsLinks.ts since
    # the site's addresses live there (audit of 17 Oct 2026).
    assert "SUPPORT_EMAIL" in support
    if cloud.HERE:
        from korely_agent.hosted.settings import SUPPORT_EMAIL

        assert f'SUPPORT_EMAIL = "{SUPPORT_EMAIL}"' in (SRC / "lib" / "docsLinks.ts").read_text()
    _cloud_branch(support, "Installation help")


# ── a failed sign-in is not always a wrong password ─────────────────────────

def test_only_the_servers_refusal_is_called_a_wrong_password():
    auth = _code("contexts/AuthContext.tsx")
    failure = auth.split("export function loginFailure", 1)[1].split("\n}\n", 1)[0]
    wrong = failure.index('"Wrong username or password."')
    # Read through the one reader of server errors (lib/apiError.ts).
    assert failure.rfind('refusal?.code === "invalid_credentials"', 0, wrong) != -1
    assert "Could not reach this install" in failure and "status === null" in failure
    assert "HTTP ${status}" in failure
    assert "setAuthError(loginFailure(e))" in auth


def test_the_server_names_the_wrong_password_with_its_code(client, db, tenant):
    """The code the screen reads, on the body's top level (api/errors.py)."""
    r = client.post("/admin/session", json={"username": "nobody", "password": "not-this-one"})
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"


# ── only the newest read fills the screen ───────────────────────────────────

@pytest.mark.parametrize("page,reads", [
    ("pages/FactsPage.tsx", 1),
    ("pages/GraphPage.tsx", 2),      # the reload and the refetch after an edit
    # The list and a search are one lib/useLoad.ts read, which drops a late
    # answer itself; the ticket stays on "Show more".
    ("pages/MemoriesPage.tsx", 1),
    ("pages/AuditPage.tsx", 1),      # a first page and "Load older"
    # The range buttons of the Requests page read through lib/useLoad.ts,
    # which drops a late answer the same way
    # (tests/test_every_screen_says_when_it_failed.py).
])
def test_a_read_driven_by_the_person_lands_only_if_it_is_the_newest(page, reads):
    code = _code(page)
    assert "useLatest()" in code, page
    assert code.count("const current = ticket();") >= reads, page
    assert "current()" in code.split("const current = ticket();", 1)[1], page


def test_the_ticket_drops_every_answer_but_the_newest():
    lib = _code("lib/latest.ts")
    assert "++seq.current" in lib and "mine === seq.current" in lib


# ── a list under a total says what it shows, and shows the rest ─────────────

@pytest.mark.parametrize("page,shown", [
    ("pages/FactsPage.tsx", "facts.length"),
    ("pages/MemoriesPage.tsx", "rows.length"),
])
def test_a_list_cut_below_its_total_says_so_and_reads_the_next_page(page, shown):
    code = _code(page)
    assert "Showing ${" + shown + ".toLocaleString()} of ${total.toLocaleString()}" in code
    assert f"{shown} < total" in code and "loadMore" in code
    assert f"PAGE, {shown}" in code, "the next page starts where the list ends"


def test_the_graph_reads_past_one_page_and_says_when_it_stops():
    graph = _code("pages/GraphPage.tsx")
    assert "factsApi.list" not in graph and "readGraph(" in graph
    assert "GRAPH_MAX" in graph and "facts.length < total" in graph
    assert "time travel replays these only" in graph
    pages = _code("lib/factPages.ts")
    assert "offset" in pages and "FACTS_PAGE_MAX = 200" in pages


def test_the_facts_route_pages_in_a_stable_order(client, admin, db, tenant):
    """What the screen's pages rely on: `offset`, in an order that neither
    repeats nor skips a fact, and a total that counts every match."""
    import uuid
    from datetime import datetime, timedelta, timezone

    from korely_agent.db.models import AgentFact
    from tests.conftest import seeded_project_id

    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    for i in range(5):
        db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="c4821",
                         project_id=seeded_project_id(db, tenant.id),
                         subject_label="Maria", predicate_raw="likes",
                         predicate_canonical="likes", object_label=f"thing {i}",
                         valid_from=start + timedelta(days=i)))
    db.commit()
    first = client.get("/admin/facts", params={"limit": 2, "offset": 0}, headers=admin).json()
    rest = client.get("/admin/facts", params={"limit": 2, "offset": 2}, headers=admin).json()
    last = client.get("/admin/facts", params={"limit": 2, "offset": 4}, headers=admin).json()
    assert first["total"] == rest["total"] == last["total"] == 5
    ids = [f["id"] for page in (first, rest, last) for f in page["facts"]]
    assert len(ids) == len(set(ids)) == 5


# ── the plan page reads the server's plans, not a copy ──────────────────────

@pytest.mark.cloud
def test_the_plan_page_keeps_no_copy_of_the_servers_plans():
    hosted = "\n".join(_code(f"hosted/{f}") for f in ("PlanPage.tsx", "usage.tsx", "periodEnd.ts"))
    for copy in ("RANK", "LIVE =", "SYMBOL", '"hobby"', "Hobby", "0.8", "6,000", '"€"'):
        assert copy not in hosted, copy
    plan = _code("hosted/PlanPage.tsx")
    assert "sub?.live" in plan and "plans.plans.findIndex" in plan
    assert "preview.upgrade" in plan, "today's charge follows the server's preview"
    assert "usage.warn_percent" in plan and "usage.chars_per_write" in plan
    assert "sub.next_plan" in _code("hosted/periodEnd.ts")
    assert "usage.paid" in _code("hosted/usage.tsx")
