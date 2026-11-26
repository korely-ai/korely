"""The snippets cut at 280 code points, not where a character ends (review of
9 Nov 2026): a memory with a three-code-point emoji at the 280th place gave
a snippet ending in a lone ZERO WIDTH JOINER, one with a combining accent
there a letter without it. The context block already cut between characters
as a reader sees them (services/graphemes.py); the search's snippet, the
MCP's korely_search, the audit screen and the previous memories the writer
reads now cut the same way, never past the 280 code points they promised."""
import pytest
from fastapi.testclient import TestClient

from korely_agent.services import graphemes, keys as key_service, limits

N = limits.SNIPPET_CHARS
EMOJI = "\U0001F469‍\U0001F4BB"  # woman technologist: three code points
ACCENTED = "é"                   # e and COMBINING ACUTE ACCENT


def _straddling(char: str) -> str:
    """A text whose character `char` begins one code point before the cut,
    so the cut at N code points falls inside it."""
    return "a" * (N - 1) + char + " and after"


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
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="admin",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}


class _Search:
    """The steps of a search these doors call (services/memory_search.py)."""

    def public_id(self, hit):
        return "mem_" + "1" * 32

    def result(self, hit):
        return {}

    def head(self, hit):
        return None

    def read_whole(self, hit):
        return None


def test_first_cuts_back_to_where_a_character_ends():
    for char, kept in ((EMOJI, N - 1), (ACCENTED, N - 1), ("bc", N)):
        text = _straddling(char)
        cut = graphemes.first(text, N)
        assert len(cut) == kept, (char, len(cut))
        assert len(cut) <= len(text[:N])
    assert graphemes.first("short", N) == "short"


def test_a_first_character_longer_than_the_cut_is_shown_cut_and_marked():
    """One character of more than N code points (a letter under 300
    combining accents) has no place to cut before N: the snippet was empty.
    It shows the character's first N - 1 code points and an ellipsis."""
    heavy = "a" + "\u0301" * 300 + " and after"
    cut = graphemes.first(heavy, N)
    assert cut == heavy[:N - 1] + "\u2026" and len(cut) == N
    assert graphemes.first("", N) == "" and graphemes.first("x", 0) == ""


def test_the_audit_screen_cuts_a_memory_between_characters(client, db, tenant, admin):
    """GET /admin/audit/read shows a memory's first 280 code points, cut back
    where a character ends: `(m.content or "")[:280]` ended on half of one."""
    import uuid

    from sqlalchemy import insert

    from korely_agent.db.models import AgentMemory
    from korely_agent.services.ids import mem_public_id
    from tests.conftest import seeded_project_id

    mid = uuid.uuid4()
    db.execute(insert(AgentMemory), [dict(id=mid, tenant_id=tenant.id, end_user_id="anna",
                                          project_id=seeded_project_id(db, tenant.id),
                                          content=_straddling(EMOJI))])
    db.commit()
    r = client.get("/admin/audit/read", headers=admin, params={"memories": mem_public_id(mid)})
    assert r.status_code == 200, r.text
    assert r.json()["memories"][mem_public_id(mid)]["text"] == "a" * (N - 1)


def test_the_search_snippet_never_ends_on_half_a_character():
    from korely_agent.api.memories import search_result

    for char in (EMOJI, ACCENTED):
        hit = {"content": _straddling(char), "score": 0.5, "type": "memory"}
        snippet = search_result(hit, search=_Search()).snippet
        assert snippet == "a" * (N - 1), (char, snippet[-3:])


def test_the_mcp_search_never_ends_on_half_a_character():
    from korely_agent.mcp.server import _search_answer

    for char in (EMOJI, ACCENTED):
        hit = {"content": _straddling(char), "score": 0.5, "type": "memory"}
        line = _search_answer([hit], False, _Search())
        assert line == f"[0.500] {'a' * (N - 1)}  (mem_{'1' * 32})", (char, line[-50:])


def test_a_previous_memory_the_writer_reads_is_cut_between_characters():
    from korely_agent.services.facts.earlier_memories import _cut

    cut = _cut("b" * 8 + EMOJI + "c" * 5, 10)
    assert cut == "b" * 8 + "…"
