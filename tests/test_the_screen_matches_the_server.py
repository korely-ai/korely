"""The dashboard against what the server sends (functional test of 1.0.0,
28 Oct 2026: a real install driven with Playwright).

The dashboard has no test runner of its own (package.json: tsc and vite),
so these read its source, as tests/test_the_screen_says_what_it_shows.py
does, for the places where the screen and the server disagreed. The
server's half of each is tested where its route is.
"""
import re
from pathlib import Path

from tests import cloud

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dashboard" / "src"


def _code(relative: str) -> str:
    """A source file without its comments."""
    text = (SRC / relative).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"\{/\*.*?\*/\}", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def test_the_memory_drawer_strikes_the_facts_the_server_marks_closed():
    """GET /admin/memories/{id} answers every fact of the memory with
    `closed` (tests/test_admin_reads.py, TestAMemorysClosedFacts): the
    drawer draws those struck through, and the Timeline says the closed
    fact was the memory's own."""
    page = _code("pages/MemoriesPage.tsx")
    assert "const stale = f.closed;" in page
    assert '"Superseded a fact"' not in page
    assert '"Its fact stopped being true"' in page
    api = _code("lib/agentsApi.ts")
    assert "facts: (Fact & { closed: boolean })[];" in api


def test_the_writing_model_reads_the_writer_whatever_sets_it():
    """`active` names the `.env` writer too (tests/test_model_choice.py):
    "Use what .env says instead" is offered for the screen's choice only,
    and a region of no known provider is said, not left blank."""
    page = _code("pages/SettingsPage.tsx")
    assert 'state.source === "screen" && (' in page
    assert "a.region ? REGION_LABEL[a.region]" in page
    assert "Set in .env (${state.env_provider})" not in page
    # An address of no known provider wears no provider's mark.
    assert re.search(r"a\.region \? \(\s*<ProviderLogo id=\{a\.provider\}[^)]*\) : \(\s*<Server ",
                     page), "the unknown address shows a neutral icon"


def test_the_key_list_shows_when_each_key_expires():
    """The key list had no expiry: `expires_at` came with every key and the
    page said only "Expired" once it was too late."""
    page = _code("pages/ApiKeysPage.tsx")
    heads = re.findall(r"<th[^>]*>([^<]*)</th>", page)
    assert "Expires" in heads
    assert 'k.expires_at ? new Date(k.expires_at).toLocaleDateString() : "Never"' in page


def test_a_session_the_server_ended_is_said_on_the_sign_in_page():
    """When the server refused the session in use (it expired, or was
    signed out from another device), the next click landed on the sign-in
    page with nothing said. The 401 says whether a session was held, both
    sign-ins turn it into one sentence, and both sign-in pages show it; a
    wrong password, with no session, stays a wrong password."""
    client = _code("lib/apiClient.ts")
    assert "const hadSession = localStorage.getItem(TOKEN_KEY) !== null;" in client
    assert client.index("const hadSession") < client.index("localStorage.removeItem(TOKEN_KEY)")
    assert "detail: { hadSession }" in client
    auth = _code("contexts/AuthContext.tsx")
    assert 'export const SESSION_ENDED = "Your session ended, sign in again.";' in auth
    for provider in cloud.in_this_tree(("contexts/AuthContext.tsx", "hosted/auth.tsx")):
        assert "if (endedASession(e)) setAuthError(SESSION_ENDED);" in _code(provider), provider
    assert "{authError}" in _code("pages/LoginPage.tsx")
    # The Cloud's page shows a refusal on the step its attempt was made from,
    # and one no attempt asked for (a session that ended) wherever the
    # person is (entry.refusalOn, run in node by entry.behaviour.tsx).
    for page in cloud.in_this_tree(("hosted/LoginPage.tsx",)):
        code = _code(page)
        assert "const refusal = refusalOn(authError, triedOn, step);" in code, page
        assert "{refusal}" in code, page


def test_below_the_md_breakpoint_the_sidebar_is_a_drawer_behind_a_menu_button():
    """At 390 px the 240 px sidebar stayed on screen, the content had about
    150 px and the page scrolled sideways to 560 px, with no menu button.
    Driven at 320, 390, 768, 1024 and 1280 px with Playwright on the built
    screen (28 Oct 2026): no page wider than the window, the menu opens and
    closes on a choice, on Escape and on a press outside."""
    layout = _code("components/DashboardLayout.tsx")
    assert 'aria-label="Open the menu"' in layout
    assert re.search(r'className="[^"]*\bmd:hidden"\s*>\s*<Menu ', layout), "the button is a phone's only"
    assert "aria-expanded={menu}" in layout and "aria-controls={drawerId}" in layout
    assert "useEffect(() => setMenu(false), [pathname]);" in layout
    assert 'px-4 py-6 md:px-8 md:py-8' in layout
    # The desktop column is hidden below md, the drawer shown only there.
    assert re.search(r'<div className="hidden md:flex">\s*<Sidebar />', layout)
    sidebar = _code("components/Sidebar.tsx")
    assert "onClick={onNavigate}" in sidebar


def test_the_menus_drawer_is_a_dialog_that_holds_the_focus():
    """Review of 28 Oct 2026: the drawer was a popover. It is the overlays'
    one mechanism now (components/Modal.tsx, ModalShell): role="dialog" and
    aria-modal, `#root` inert while it is open so the focus moves in and
    cannot leave, Escape closes it, and the focus goes back to the button
    that opened it."""
    layout = _code("components/DashboardLayout.tsx")
    drawer = re.search(r"\{menu && \(\s*<ModalShell\b(.*?)</ModalShell>", layout, flags=re.S)
    assert drawer, "the drawer is a ModalShell"
    assert "id={drawerId}" in drawer.group(1) and 'onClose={() => setMenu(false)}' in drawer.group(1)
    assert "<Sidebar onNavigate={() => setMenu(false)} />" in drawer.group(1)
    assert "useDismiss" not in layout
    modal = _code("components/Modal.tsx")
    shell = modal[modal.index("export function ModalShell"):]
    assert 'role="dialog"' in shell and 'aria-modal="true"' in shell and "id={id}" in shell
    assert "useOverlay(panel, onClose)" in shell


def test_the_tables_scroll_inside_their_own_box():
    for page in ("pages/ApiKeysPage.tsx", "pages/MemoriesPage.tsx", "pages/RequestsPage.tsx",
                 "pages/UsagePage.tsx"):
        code = _code(page)
        for m in re.finditer(r"<table\b", code):
            box = code[:m.start()].rsplit("<div", 1)[1]
            assert "overflow-x-auto" in box and "overflow-hidden" not in box, page
