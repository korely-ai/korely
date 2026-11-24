"""The dashboard's findings fixed in 1.0.29, read in
its source (the behaviour that runs in node is in
dashboard/src/lib/useFactEditing.behaviour.tsx,
dashboard/src/pages/FactsPage.behaviour.tsx, which draws Facts and the
graph with a read that failed, and
dashboard/src/pages/MemoriesPage.behaviour.tsx; the colours in
tests/test_the_screen_reads_in_both_themes.py):

- Facts and the Memory Graph drew "No facts yet" / "No graph yet" under a
  failed read, as if the account were empty, with no Try again; Agents,
  Requests and the Audit log said a failure with no way to try again, and
  the Audit log sent a Cloud customer to an "API log" they do not have;
- on a phone the buttons a row shows on hover were invisible and still took
  a tap: one tap forgot a fact with no question, and Projects' Edit, Archive
  and Delete never showed;
- on a phone the keys' expiry, project and Revoke were off the screen, and a
  long end user id pushed the Facts page sideways.
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
    text = re.sub(r"\{/\*.*?\*/\}", "", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


# ── a failed read is said, with the server's sentence and Try again ──────────

# The page, its read through lib/useLoad.ts, and the empty state that must be
# drawn only inside <Loaded>, i.e. only with data the server sent. Facts and
# the graph draw theirs in a piece of their own (FactsShown in the page,
# components/GraphShown.tsx), which FactsPage.behaviour.tsx draws with a read
# that failed; the whole tag is read here, so a `quiet` added to it, which
# says no failure, fails too.
PAGES = [
    ("pages/FactsPage.tsx", "pages/FactsPage.tsx", '<Loaded load={load} className="mb-4">', 'title="No facts yet"'),
    ("pages/GraphPage.tsx", "components/GraphShown.tsx", '<Loaded load={load} className="mb-4">', 'title="No graph yet"'),
    ("pages/AgentsPage.tsx", "pages/AgentsPage.tsx", "<Loaded load={list}>", 'title="No agents yet"'),
    ("pages/RequestsPage.tsx", "pages/RequestsPage.tsx", "<Loaded load={calls}>", 'title="No requests yet"'),
    ("pages/AuditPage.tsx", "pages/AuditPage.tsx", "<Loaded load={view}>", 'title="Nothing recorded yet"'),
]


@pytest.mark.parametrize("page,piece,loaded,empty", PAGES)
def test_a_failed_read_is_never_drawn_as_an_empty_account(page, piece, loaded, empty):
    assert "useLoad" in _code(page), page
    code = _code(page) + "\n" + (_code(piece) if piece != page else "")
    # A read caught by hand into a flag or an empty list: the finding.
    assert not re.search(r"\.catch\(\(\) =>\s*(current\(\) && )?set(Error|Loading|Facts|Agents|Rows|Events)",
                         code), page
    assert "setError(" not in code and "setLoading(" not in code, page
    assert loaded in code and empty in code, page
    # The empty state is drawn by the children of <Loaded>, after it opens.
    assert code.index(loaded) < code.index(empty), page


@pytest.mark.parametrize("page,piece,loads", [
    ("pages/FactsPage.tsx", "<FactsShown load={view} shown={facts.length} total={total}>", 1),
    ("pages/GraphPage.tsx", "<GraphShown load={graph} shown={facts.length}>", 0),
])
def test_facts_and_the_graph_draw_their_read_with_the_piece_that_is_checked(page, piece, loads):
    code = _code(page)
    assert code.count(piece) == 1, page
    assert code.count("<Loaded ") == loads, "the piece's, and no other"


@pytest.mark.parametrize("page", [p for p, _, _, _ in PAGES])
def test_no_failure_says_try_again_without_the_button(page):
    """Loaded says a failure with LoadNote (components/LoadState.tsx): the
    server's sentence, and Try again where trying again can help."""
    code = _code(page)
    for said in ("Agents unavailable", "Requests unavailable", "Try again in a moment", "API log", "Could not refresh the graph"):
        assert said not in code, (page, said)


def test_the_agents_drawer_says_its_failure_the_same_way():
    code = _code("pages/AgentsPage.tsx")
    assert "<Loaded load={recent}>" in code and "Couldn't load memories" not in code
    assert "setFailed" not in code


def test_the_graph_reads_again_after_an_edit_keeping_what_is_selected():
    code = _code("pages/GraphPage.tsx")
    assert "useFactEditing(graph.reload)" in code
    # An older read stops paging once a newer one started.
    assert "if (!current()) return null;" in code
    # A link's date is where the slider opens (`?as_of=`, `?known_as_of=`).
    assert "const [asOf, setAsOf] = useState<number | null>(linkedAsOf.current);" in code
    assert "onApply={graph.reload}" in code
    # Other filters start over, where they change; an edit does not.
    assert re.search(r"const refilter = .*\(v: T\) => \{\s*change\(v\);\s*setSelected\(null\);\s*setAsOf\(null\);",
                     code)
    for setter in ("setEndUser", "setFamily", "setShowHistory"):
        assert f"refilter({setter})" in code, setter


def test_show_reads_the_audit_log_again_when_the_filter_is_the_same():
    """Show only changed the applied filter: unchanged, it sent nothing."""
    code = _code("pages/AuditPage.tsx")
    assert re.search(r"const show = \(u: string\) => \{\s*if \(u === applied\) view\.reload\(\);\s*"
                     r"else setApplied\(u\);", code)
    assert "show(userId.trim());" in code
    # "Load older", its failure and the event opened belong to the first page
    # they were on: a new read of it (Show with the same filter too) drops them.
    assert "older?.of === base" in code
    assert "olderError?.of === base ? olderError.text : null" in code and "{olderFailed && <ErrorNote" in code
    assert "setOlderError({ of, text: errorMessage(e) ??" in code
    assert "open?.of === base ? open.at : null" in code and "setOpen(null)" not in code
    # The event opened is its place in the list as read, not as drawn.
    assert "const at = oldestFirst ? events.length - 1 - i : i;" in code


def test_the_facts_next_page_says_its_failure_without_hiding_the_list():
    code = _code("pages/FactsPage.tsx")
    assert "more?.of === base" in code
    # Its failure too: a filter changed or a fact forgotten since drops it.
    assert "moreError?.of === base ? moreError.text : null" in code
    assert "setMoreError({ of, text: errorMessage(e) ??" in code
    assert "{(moreFailed || err) && <ErrorNote" in code


# ── on a touch screen, every action is visible, and forgetting asks ─────────

def test_a_row_hides_its_actions_only_where_something_can_hover():
    """The hover-only buttons: `opacity-0 group-hover:opacity-100` hid them
    everywhere and showed them only on hover, which a touch screen has not."""
    css = (SRC / "index.css").read_text(encoding="utf-8")
    rule = css.split("@utility hover-reveal", 1)[1].split("\n}\n", 1)[0]
    # The whole rule: a condition added beside `(hover: hover)`, such as
    # `, (pointer: coarse)`, hides them on a phone again.
    assert " ".join(rule.split()) == (
        "{ @media (hover: hover) { .group:not(:hover):not(:focus-within) & { opacity: 0; } }"
    ), rule
    users = []
    for p in sorted(SRC.rglob("*.tsx")):
        rel = p.relative_to(SRC).as_posix()
        code = _code(rel)
        assert not re.search(r"(?<![\w:-])opacity-0\b", code), f"{rel}: hidden for every device"
        if "hover-reveal" in code:
            users.append(rel)
    assert users == ["pages/FactsPage.tsx", "pages/GraphPage.tsx", "pages/ProjectsPage.tsx"], users


def test_forgetting_a_fact_asks_first_in_the_one_place_both_views_share():
    lib = _code("lib/useFactEditing.ts")
    # The order (ask, then busy, then send) is checked in node, with stand-ins.
    steps = lib.split("export async function forgetAsking(", 1)[1]
    assert steps.index("if (!s.ask(forgetQuestion(f))) return;") < steps.index("s.busy(f.id);")
    assert "ask: (q) => confirm(q)," in lib and "send: factsApi.forget," in lib
    for page in ("pages/FactsPage.tsx", "pages/GraphPage.tsx"):
        code = _code(page)
        assert "useFactEditing(" in code and "factsApi.forget" not in code, page


# ── on a phone, nothing runs off the side ───────────────────────────────────

def test_the_keys_are_cards_on_a_phone_with_every_cell_named():
    page = _code("pages/ApiKeysPage.tsx")
    table = page.split("function KeyTable(", 1)[1].split("function MoveKey(", 1)[0]
    assert '<table className="block w-full text-sm md:table">' in table
    assert "hidden bg-secondary/50" in table and "md:table-header-group" in table
    assert table.count("md:table-cell") == 5, "each cell a line of its card below md"
    for name in ("Key", "Last used", "Expires"):
        assert f"<span className={{label}}>{name}</span>" in table or \
            f'<span className={{cn(label, "font-sans")}}>{name}</span>' in table, name
    # The actions wrap in their cell: at 1280 px a long project name pushed
    # Revoke past the table's edge too.
    assert '<div className="flex flex-wrap items-center gap-1 md:justify-end">' in table
    # The project's select takes no more than its share of a phone.
    move = page.split("function MoveKey(", 1)[1]
    assert "max-w-48" in move


def test_a_long_id_breaks_inside_its_fact():
    code = _code("pages/FactsPage.tsx")
    assert '<span className="min-w-0 break-all">{f.user_id}</span>' in code
    assert "break-words text-sm leading-relaxed" in code
