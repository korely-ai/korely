"""The dashboard's behaviour, written once (audit of 17 Oct 2026, W19).

The dashboard has no test runner of its own (package.json: tsc and vite), so
these read its source, as tests/test_the_screen_keeps_one_truth.py does, for
what the audit found:

- the dialogs' behaviour (the `inert`, Escape, the focus) was two copies in
  components/Modal.tsx, and the popovers' "close on a press outside or on
  Escape" two more, in the project switcher and the requests' type filter;
- the dialogs' effect depended on an inline `onClose`, so every render of the
  page behind ran it again and threw the focus back on the panel, and the
  focus never went back where it was on close; the memories drawer added a
  second Escape listener of its own;
- the dialogs' foot, the field class (eighteen copies), the filter bar of
  the facts and of the graph, the SDK steps and the region names were each
  written out more than once;
- row actions showed on a mouse hover only, icon buttons had no name, and
  two menus of buttons called themselves a listbox.
"""
import re
from pathlib import Path

import pytest

from tests import cloud

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dashboard" / "src"


def _code(text: str) -> str:
    """Source without its comments, JS and JSX."""
    text = re.sub(r"\{/\*.*?\*/\}", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def _sources():
    for f in sorted(SRC.rglob("*.ts*")):
        yield f.relative_to(SRC).as_posix(), _code(f.read_text(encoding="utf-8"))


def _where(pattern: str):
    return [rel for rel, code in _sources() if re.search(pattern, code)]


# ── one behaviour per kind of overlay ───────────────────────────────────────

def test_the_page_is_frozen_in_one_place():
    hits = [(rel, code.count('setAttribute("inert"')) for rel, code in _sources()]
    assert [h for h in hits if h[1]] == [("components/Modal.tsx", 1)]


def test_escape_and_the_outside_press_are_listened_to_in_two_places():
    """The dialogs' hook and the popovers' hook, and nowhere else: a page that
    adds its own listener is a copy the next fix will miss."""
    assert _where(r'addEventListener\("keydown"') == ["components/Modal.tsx", "lib/useDismiss.ts"]
    assert _where(r'addEventListener\("mousedown"') == ["lib/useDismiss.ts"]
    for page in ("components/ProjectSwitcher.tsx", "pages/RequestsPage.tsx"):
        assert "useDismiss(" in dict(_sources())[page], page


# ── one copy of each piece ───────────────────────────────────────────────────

FIELD = "rounded-md border border-input bg-background px-3 py-2 text-sm outline-hidden focus:ring-2 focus:ring-ring"


def test_the_field_class_is_written_once():
    assert _where(re.escape(FIELD)) == ["components/PageShell.tsx"]


def test_the_dialogs_share_one_foot():
    cancel = "rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-secondary"
    assert _where(re.escape(cancel)) == ["components/Modal.tsx"]
    for rel, code in _sources():
        if "<Modal " in code and "ModalActions" not in code:
            # The one-time key dialog has a single "Done" and no way back.
            assert rel == "pages/ApiKeysPage.tsx", rel


def test_the_facts_and_the_graph_share_one_filter_bar():
    assert _where(r"Filter by end user…") == ["components/FactFilters.tsx"]
    for page in ("pages/FactsPage.tsx", "pages/GraphPage.tsx"):
        assert "<FactFilters" in dict(_sources())[page], page


def test_the_region_names_are_written_once():
    assert _where(r'"Europe"') == ["components/RegionSelect.tsx"]
    assert _where(r'type Region\s*=') == ["lib/agentsApi.ts"]


def test_the_two_sdks_share_their_steps():
    page = dict(_sources())["pages/InstallPage.tsx"]
    for sentence in ("One call stores the memory", "A thin HTTP client",
                     "Add a memory: the facts follow", "assembles the active facts"):
        assert page.count(sentence) == 1, sentence


# ── the focus ───────────────────────────────────────────────────────────────

def test_the_dialog_effect_runs_once_and_gives_the_focus_back():
    modal = dict(_sources())["components/Modal.tsx"]
    # The effect does not depend on the caller's inline onClose any more.
    assert not re.search(r"\},\s*\[\s*onClose\s*\]\s*\)", modal)
    assert "close.current = onClose" in modal
    # An autoFocus inside keeps the focus; on close it goes back.
    assert "contains(document.activeElement)" in modal
    assert re.search(r"back\.focus\(\)", modal)
    # Read at the first render: by the effect, an inner autoFocus already
    # holds the focus, and giving it back to that input went nowhere; a
    # dialog that replaces another gives it where the first one gave it.
    assert re.search(r"useState\(\(\) =>\s*document\.activeElement", modal)
    assert "lastGivenBack" in modal
    # Both overlays call the one hook; a modal whose request is out passes
    # it a close that does nothing (`busy`), so its answer reaches it.
    assert modal.count("useOverlay(panel, ") == 2
    assert "useOverlay(panel, onClose)" in modal and "const close = busy ? () => {} : onClose;" in modal


def test_only_the_newest_overlay_closes_on_escape():
    modal = dict(_sources())["components/Modal.tsx"]
    assert re.search(r"open\[open\.length - 1\] === close", modal)
    assert re.search(r"if \(open\.length === 0\) root\?\.removeAttribute\(\"inert\"\)", modal)


# ── names and roles ─────────────────────────────────────────────────────────

def _buttons(text: str):
    """(attributes, inside, line) of every <button>…</button>, braces-aware."""
    for m in re.finditer(r"<button\b", text):
        i, depth = m.end(), 0
        while i < len(text):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif c == ">" and depth == 0:
                break
            i += 1
        attrs = text[m.end():i]
        if attrs.endswith("/"):
            continue
        yield attrs, text[i + 1:text.find("</button>", i)], text.count("\n", 0, m.start()) + 1


# A button whose whole content is one icon, or a choice of two.
_ICON_ONLY = re.compile(
    r"(\{[^{}]*\?\s*)?<[A-Z]\w*[^<>]*/>(\s*:\s*<[A-Z]\w*[^<>]*/>\s*\})?", re.S)


def test_every_icon_button_has_a_name():
    nameless = [
        f"{rel}:{line}"
        for rel, code in _sources() if rel.endswith(".tsx")
        for attrs, inside, line in _buttons(code)
        if _ICON_ONLY.fullmatch(inside.strip())
        and not re.search(r"\baria-label\b|\btitle=", attrs)
    ]
    assert nameless == []


def test_no_listbox_around_buttons():
    """role="listbox" promises the arrow keys of a select; these menus are
    lists of buttons that Tab walks."""
    assert _where(r'role="(listbox|option)"') == []


def test_row_actions_show_on_focus_too():
    hidden = [
        rel for rel, code in _sources()
        for cls in re.findall(r'className="([^"]*)"', code)
        if "group-hover:opacity-100" in cls and "group-focus-within:opacity-100" not in cls
    ]
    assert hidden == []


def test_a_memory_row_opens_from_the_keyboard():
    page = dict(_sources())["pages/MemoriesPage.tsx"]
    table = page[page.index("function MemoryTable"):]
    assert re.search(r"<button[^>]*?\n?[^<]*?onOpen\(r\.id\)", table, re.S)


# ── what the build ships ────────────────────────────────────────────────────

def test_the_build_ships_no_source_maps():
    """They doubled what the image carried and spa.py served (a 1.8 MB map
    next to a 0.5 MB page); the dev server has its own."""
    config = _code((ROOT / "dashboard" / "vite.config.ts").read_text(encoding="utf-8"))
    assert re.search(r"build:\s*\{[^}]*sourcemap:\s*false", config, re.S)
    assert "sourcemap: true" not in config


def test_each_page_is_its_own_chunk():
    app = dict(_sources())["App.tsx"]
    eager = re.findall(r'^import \{[^}]*\} from "@/pages/(\w+)"', app, re.M)
    # The login is the first screen; every other page is fetched when opened.
    assert eager == ["LoginPage"]
    assert re.search(r'import\("@/pages/HomePage"\)', app)
    layout = dict(_sources())["components/DashboardLayout.tsx"]
    assert re.search(r"<Suspense[\s\S]*<Outlet />[\s\S]*</Suspense>", layout)


# ── the visitor's country ───────────────────────────────────────────────────

@pytest.mark.cloud
def test_the_plan_page_asks_the_country_only_without_a_subscription():
    """The public site sees the browser's address: it was asked on every
    opening of the Plan page, a paying account's included."""
    plan = dict(_sources())["hosted/PlanPage.tsx"]
    calls = [m.start() for m in re.finditer(r"visitorCountry\(\)", plan)]
    assert len(calls) == 1
    before = plan[:calls[0]].splitlines()[-1]
    assert re.search(r"if \(subRead && !billedIn\)", before)


# ── what an install of one's own downloads and calls ────────────────────────

_STATIC = re.compile(r'^import\s+(?!type\b)[^;]*?from\s+"([^"]+)"|^import\s+"([^"]+)"', re.M | re.S)
# `import.meta.glob("...", { eager: true })` (lib/cloud.ts): bundled as a
# static import of every file it matches.
_EAGER_GLOB = re.compile(r'import\.meta\.glob(?:<[^>]*>)?\(\s*"([^"*]+)"[^)]*\beager:\s*true')


def _resolve(spec: str, here: Path):
    if spec.startswith("@/"):
        base = SRC / spec[2:]
    elif spec.startswith("."):
        base = (here.parent / spec).resolve()
    else:
        return spec  # a package
    for cand in (base.with_suffix(".tsx"), base.with_suffix(".ts"), base / "index.ts"):
        if cand.is_file():
            return cand
    return None


def _static_closure(entry: Path):
    """Every module and package the entry pulls in without an import():
    what the main chunk carries."""
    seen, packages, todo = set(), set(), [entry]
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        code = _code(f.read_text(encoding="utf-8"))
        for m in _STATIC.finditer(code):
            target = _resolve(m.group(1) or m.group(2), f)
            if isinstance(target, str):
                packages.add(target.split("/")[0])
            elif target is not None:
                todo.append(target)
        for m in _EAGER_GLOB.finditer(code):
            found = (f.parent / m.group(1)).resolve()
            if found.is_file():
                todo.append(found)
    return {p.relative_to(SRC).as_posix() for p in seen}, packages


def test_the_main_chunk_carries_no_sign_in_and_no_firebase():
    modules, packages = _static_closure(SRC / "main.tsx")
    assert "firebase" not in packages
    hosted = sorted(m for m in modules if m.startswith("hosted/"))
    # The Cloud's small pieces are in it, where the tree has them: the walk
    # follows lib/cloud.ts's glob (else this test would pass on a walk that
    # stops there).
    assert "lib/cloud.ts" in modules
    assert ("hosted/usage.tsx" in hosted) == (SRC / "hosted").is_dir(), hosted
    for lazy in ("hosted/auth.tsx", "hosted/firebase.ts", "hosted/LoginPage.tsx",
                 "hosted/PlanPage.tsx", "hosted/AccountPage.tsx"):
        assert lazy not in hosted, lazy
    # Nor the parts of the product (hosted/parts.ts and each part's folder):
    # a chunk asked only for an account the server serves one (lib/cloud.ts;
    # what the build makes of it: dashboard/scripts/check_the_login_bundle.mjs).
    assert not [m for m in hosted if m == "hosted/parts.ts" or m.count("/") > 1], hosted
    # What App.tsx's comment says ships in every build is what does.
    app = (SRC / "App.tsx").read_text(encoding="utf-8")
    assert "ship in every build" in app and "usage line" in app


@pytest.mark.cloud
def test_the_usage_line_reads_the_cloud_only_on_the_cloud():
    """GET /hosted/usage exists on the Cloud only; the self-hosted home page
    asked it at every opening and every focus, for a 404."""
    usage = dict(_sources())["hosted/usage.tsx"]
    hook = usage[usage.index("function useUsage"):usage.index("return usage;")]
    assert "useMode()" in hook and re.search(r"if \(!hosted\) return;", hook)
    assert re.search(r"\},\s*\[hosted\]\)", hook)


# ── dead code, comments that mislead, words that say the wrong thing ─────────

def _raw(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_no_dead_api_functions():
    for gone in (r"projectsApi\.get\b", r"providerApi\.clear\b", r"storedOperator"):
        assert _where(gone) == [], gone
    api = _raw("lib/agentsApi.ts")
    projects = api[api.index("export const projectsApi"):]
    assert not re.search(r"^\s*get:", projects[:projects.index("};")], re.M)
    provider = api[api.index("export const providerApi"):]
    assert "clear:" not in provider[:provider.index("};")]
    # A second call is a 409 (the name "first key" is taken): not idempotent.
    assert not re.search(r"//\s*Idempotent:", api)


def test_comments_name_tools_and_files_that_exist():
    package = (ROOT / "dashboard" / "package.json").read_text(encoding="utf-8")
    assert "eslint" not in package
    for f in SRC.rglob("*"):
        if f.is_file() and f.suffix in (".ts", ".tsx", ".css"):
            text = f.read_text(encoding="utf-8")
            assert "eslint-disable" not in text, f
            assert "used by frontend/" not in text, f
            assert not re.search(r"tailwind\.config has", text), f


def test_the_signed_in_person_has_its_own_shape():
    auth = _code(_raw("contexts/AuthContext.tsx"))
    operator = auth[auth.index("export interface Operator"):]
    operator = operator[:operator.index("}")]
    assert "email" not in operator and "providerData" not in operator
    assert _where(r"providerData|user\?\.email") == []
    settings = _code(_raw("pages/SettingsPage.tsx"))
    assert re.search(r'hosted \? "Email" : "Username"', settings)
    # Nothing writes a copy of the person that nothing reads.
    assert _where(r"setItem\(WHO_KEY") == []


def test_one_name_for_the_session_token():
    client = _code(_raw("lib/apiClient.ts"))
    assert 'export const TOKEN_KEY = "token"' in client
    assert 'getItem("token")' not in client and "userEmail" not in client
    assert _where(r'TOKEN_KEY = "token"') == ["lib/apiClient.ts"]


def test_both_logins_land_on_the_same_page():
    """Where the person was going, by the one rule (lib/returnTo.ts). The
    Cloud's sign-in lands, when nothing sent the person there, on the plan
    the site's pricing page passed (hosted/entry.ts, 14 Nov 2026), through
    the same rule's fallback."""
    assert re.search(r"returnTarget\(location\.state\)", _code(_raw("pages/LoginPage.tsx")))
    for page in cloud.in_this_tree(("hosted/LoginPage.tsx",)):
        assert "landingOf(location.state, location.search)" in _code(_raw(page))
        entry = _code(_raw("hosted/entry.ts"))
        assert re.search(r"return returnTarget\(state, ", entry)


@pytest.mark.cloud
def test_the_plan_prices_are_formatted_by_the_currency():
    plan = _code(_raw("hosted/PlanPage.tsx"))
    assert "plans.symbol" not in plan
    assert "price(plans.topup.price, plans.currency)" in plan


def test_words_that_say_what_the_screen_shows():
    assert "three steps" not in _raw("pages/InstallPage.tsx")
    assert ">Entity<" not in _raw("pages/MemoriesPage.tsx")
    for page in ("pages/FactsPage.tsx", "pages/GraphPage.tsx", "pages/MemoriesPage.tsx"):
        code = _code(_raw(page))
        assert not re.search(r">\s*superseded\s*<", code), page
        assert "closedLabel(f)" in code, page
    lib = _code(_raw("lib/factPages.ts"))
    assert re.search(r'invalidated_by \? "superseded" : "ended"', lib)
