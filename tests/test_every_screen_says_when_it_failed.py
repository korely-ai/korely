"""The rest of the dashboard, read with the helpers of the first fixes (audit
of 17 Oct 2026).

tests/test_the_screen_says_when_it_failed.py and
tests/test_the_screen_keeps_one_truth.py guard the pages fixed first, with
lib/apiError.ts, lib/useLoad.ts, components/LoadState.tsx,
components/CopyButton.tsx, lib/latest.ts and lib/factPages.ts. The same
findings stood on other pages; these read their source, the dashboard having
no test runner of its own (package.json: tsc and vite).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dashboard" / "src"


def _code(rel: str) -> str:
    """A dashboard file without its comments."""
    text = (SRC / rel).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


# ── a list under a total reads the next page ────────────────────────────────

def test_the_facts_query_names_its_offset_and_the_page_is_a_plain_call():
    api = _code("lib/agentsApi.ts")
    query = api.split("export interface FactsQuery", 1)[1].split("}", 1)[0]
    assert "offset?: number;" in query
    pages = _code("lib/factPages.ts")
    assert "factsApi.list({ ...q, limit, offset })" in pages
    assert "& { offset: number }" not in pages


# ── an action that did not happen says so, a read that failed says so ─────

def _awaited_in_a_try(code: str, call: str) -> bool:
    """`await <call>` is the first statement of a try block."""
    return re.search(r"try \{\s*await " + re.escape(call), code) is not None


def test_use_what_env_says_says_when_it_did_not_happen():
    """The button awaited the clear with nothing around it (the finding
    "revoke, move and delete failed with nothing on screen")."""
    settings = _code("pages/SettingsPage.tsx")
    assert _awaited_in_a_try(settings, "modelsApi.clear()")
    assert re.search(r"setEnvError\(\s*errorMessage\(e\) \?\?", settings)
    assert "{envError && <ErrorNote" in settings


def test_the_settings_reads_hide_only_on_the_services_refusal():
    """Both reads ended in `.catch(() => setState(null))`: a failed read hid
    the writing model and said the key was "not set" (the finding "a failed
    read became an empty list, a zero or an endless Loading…")."""
    settings = _code("pages/SettingsPage.tsx")
    assert ".catch(() =>" not in settings
    assert "unlessTheServices(modelsApi.get())" in settings
    assert "unlessTheServices(providerApi.get())" in settings
    assert 'errorCode(e) === SERVICES' in settings and '"set_by_the_service"' in settings
    assert "<Loaded load={models}>" in settings and "<Loaded load={provider}" in settings
    # The code the screen hides on is the one the server sends.
    admin = (ROOT / "korely_agent" / "api" / "admin.py").read_text(encoding="utf-8")
    assert 'code="set_by_the_service"' in admin


def test_the_requests_series_says_when_it_could_not_be_read():
    """It ended in `.catch(() => setDaily(null))` and the chart stayed on
    "Loading…" for good, the Dashboard's finding on the same series."""
    page = _code("pages/RequestsPage.tsx")
    assert "setDaily" not in page
    assert "useLoad(() => requestsApi.daily(days), [days]," in page
    assert "<Loaded load={daily}" in page
    # The late answer of an earlier range is dropped by the hook itself.
    assert _code("lib/useLoad.ts").count("mine !== latest.current") == 2


# ── a copy button shows its tick only when the text was copied ──────────────

@pytest.mark.parametrize("page", ["pages/InstallPage.tsx", "pages/MemoriesPage.tsx"])
def test_the_remaining_copy_buttons_are_the_honest_one(page):
    """`navigator.clipboard?.writeText` does nothing off a secure context, and
    the tick showed anyway (the finding "the copy buttons showed their tick
    when nothing was copied")."""
    code = _code(page)
    assert "navigator.clipboard" not in code, page
    assert "<CopyButton" in code and "setCopied" not in code, page


def test_no_page_writes_to_the_clipboard_but_the_copy_button():
    writers = sorted(p.relative_to(SRC).as_posix() for p in SRC.rglob("*.ts*")
                     if "navigator.clipboard" in _code(p.relative_to(SRC).as_posix()))
    assert writers == ["components/CopyButton.tsx"], writers


# ── a failure said as a failure, never an empty list ────────────────────────

def test_the_memories_page_never_draws_no_memories_for_a_failed_read():
    """The list, the search, the categories and the drawer each caught their
    failure by hand: the list left the rows before it (or none) under the
    error, so the page said "No memories"; a failed categories read left only
    "All" (the finding "a failed read became an empty list, a zero or an
    endless Loading…")."""
    page = _code("pages/MemoriesPage.tsx")
    assert ".catch(() =>" not in page
    assert "setRows" not in page and "setCats" not in page
    assert "setDetailErr" not in page and "setHistoryErr" not in page
    assert "<Loaded load={view}>" in page
    assert "<Loaded load={detail}>" in page and "<Loaded load={history}>" in page
    assert "cats.error && <ErrorNote" in page
    # A delete that did not happen says the server's sentence.
    assert re.search(r"try \{\s*await memoriesApi\.remove\(id, erase\);\s*\} catch \(e\) \{\s*"
                     r"setActionError\(errorMessage\(e\) \?\?", page)
    # The drawer is keyed by the memory it shows.
    assert "key={openId}" in page


def test_the_projects_list_is_one_read_that_says_when_it_failed():
    """The app's list started empty and a failed read set a flag only the
    Projects page looked at: the switcher said "No projects found."."""
    context = _code("contexts/ProjectContext.tsx")
    assert "useLoad(" in context and "projectsApi.list()" in context
    assert "setProjects" not in context and "loadError" not in context
    switcher = _code("components/ProjectSwitcher.tsx")
    assert "projectsLoad.error && (" in switcher and "projectsLoad.data === null" in switcher
    projects = _code("pages/ProjectsPage.tsx")
    assert "<Loaded load={projectsLoad}>" in projects and "loadError" not in projects


def test_the_keys_page_reads_the_shared_list_of_projects():
    """It read projectsApi.list() for itself, a copy a rename or a delete on
    the Projects page left behind (the finding "the Projects page kept its
    own copy of the projects")."""
    keys = _code("pages/ApiKeysPage.tsx")
    assert "projectsApi" not in keys
    assert "projects, projectsLoad, refresh: refreshProjects } = useProjects();" in keys
    assert "projectsLoad.error && (" in keys


def test_the_sidebars_model_usage_says_when_it_could_not_be_read():
    """`.catch(() => setReport(null))` hid the card, as for an install whose
    model was never called."""
    sidebar = _code("components/Sidebar.tsx")
    assert ".catch(() =>" not in sidebar and "setReport" not in sidebar
    assert "useLoad(() => usageApi.report(30)" in sidebar
    assert "<ErrorNote onRetry={usage.reload}>" in sidebar


# ── only the newest read fills the screen ───────────────────────────────────

def test_the_home_pages_range_buttons_take_only_the_newest_series():
    """Already fixed by the Dashboard's move to lib/useLoad.ts: the series is
    read with the range as its dependency, and the hook drops the answer of a
    range picked before. Guarded here so it is not hand-written again."""
    home = _code("pages/HomePage.tsx")
    assert "useLoad(() => requestsApi.daily(days), [days]," in home
    assert "requestsApi.daily(days).then" not in home and "setDaily" not in home
