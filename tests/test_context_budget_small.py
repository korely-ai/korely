"""A small budget still shows the facts and the memories (the documentation
review of 1 Nov 2026).

The facts take up to half of the block (`_FACTS_BUDGET_FRACTION`), and the
reader note (`_READER_GUIDANCE`, 725 bytes with its separator) is packed
before them whenever the budget reaches `_GUIDANCE_MIN_BUDGET`. The facts'
share was half of the whole budget, counted from the start of the block: at
400 tokens it is 800 bytes and the note had already used 725 of them, so a
current fact did not fit and the block came back without "## Known facts",
with no error. The same thing had been found for the NOTE about a degraded
part, and corrected for that note alone. The share is now half of the room
the notes leave, so the note is paid by the facts and the memories alike.
Counted from where the facts begin, as this correction first did, the share
left the memories nothing at 400 tokens when ten facts were there.
"""
import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services.context import KNOWN_FACTS_HEADING, assemble_context
from korely_agent.services.facts.service import write_fact_direct
from korely_agent.services.memory_engine import AgentMemoryEngine, MemorySearchUnavailable
from tests.conftest import seeded_project_id


VEC = [0.1] * EMBEDDING_DIM


@pytest.fixture(autouse=True)
def query_vector(monkeypatch):
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: VEC)


# A fact of an ordinary length: its line, "- Maria works_as senior product
# manager at Globex (since ...)", is about 75 bytes with its newline, the room
# the reader note left at 400 tokens.
JOB = "senior product manager at Globex"


def facts(db, tenant, *objects):
    for obj in objects:
        write_fact_direct(db, tenant.id, subject="Maria", predicate="works_as",
                          obj=obj, end_user_id="u1",
                          project_id=seeded_project_id(db, tenant.id))
    db.commit()
    db.query(AgentFact).update({AgentFact.embedding: VEC, AgentFact.embedding_status: "done"})
    db.commit()


def memories(db, tenant, monkeypatch, count):
    rows = []
    for i in range(count):
        m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", graph_sync_status="pending",
                        # Sentences begun with a capital: since 1.0.30 a memory is
                        # cut only after a whole sentence, and "a long story. a long
                        # story." is one (Unicode's rule SB8).
                        content=f"Maria told us about her week, day {i}: " + "A long story. " * 20)
        db.add(m)
        rows.append(m)
    db.commit()
    found = [{"id": str(m.id), "content": m.content} for m in rows]
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, *a, **k: found)


def block(db, tenant, budget):
    return assemble_context(db, tenant.id, query="what does Maria do",
                            end_user_id="u1", token_budget=budget,
                            project_id=seeded_project_id(db, tenant.id))


def facts_section(context: str) -> str:
    assert KNOWN_FACTS_HEADING in context, "the block came back without its facts"
    start = context.index(KNOWN_FACTS_HEADING)
    end = context.find("\n\n", start)
    return context[start:] if end == -1 else context[start:end]


def ten_facts(db, tenant):
    """Ten facts of the same length: about 770 bytes, more than the facts'
    share at 400 tokens (437 bytes) and less than the room the note leaves."""
    facts(db, tenant, *[f"{JOB}, team {i}" for i in range(10)])


def room_after_the_note(budget: int) -> int:
    return (budget * ctx.BYTES_PER_TOKEN
            - ctx._cost(ctx._READER_GUIDANCE) - ctx._cost(ctx._SEPARATOR))


def room_the_old_share_left(budget: int) -> int:
    """What the facts had until 3 Nov 2026: half of the whole budget, less the
    reader note packed before them (75 bytes at 400 tokens, 375 at 550)."""
    return (int(budget * ctx.BYTES_PER_TOKEN * ctx._FACTS_BUDGET_FRACTION)
            - ctx._cost(ctx._READER_GUIDANCE) - ctx._cost(ctx._SEPARATOR))


def a_job_longer_than(size: int) -> str:
    job = JOB
    while ctx._cost(job) <= size:
        job += ", Milan office"
    return job


@pytest.mark.parametrize("budget", [400, 450, 500, 550])
def test_the_current_fact_is_in_the_block(db, tenant, monkeypatch, budget):
    """One fact whose line is longer than the room the old share left at this
    budget: with that share, it did not fit and the block had no facts."""
    job = a_job_longer_than(room_the_old_share_left(budget))
    facts(db, tenant, job)
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, *a, **k: [])
    out = block(db, tenant, budget)
    assert ctx._READER_GUIDANCE in out["stable"], "the reader note is part of this budget"
    assert KNOWN_FACTS_HEADING in out["context"], "the reader note took the facts' share"
    assert job in out["context"]
    assert out["tokens"] <= budget


@pytest.mark.parametrize("budget", [400, 800])
def test_with_memories_too_the_block_stays_under_its_budget(db, tenant, monkeypatch, budget):
    facts(db, tenant, JOB)
    memories(db, tenant, monkeypatch, 12)
    out = block(db, tenant, budget)
    assert KNOWN_FACTS_HEADING in out["context"]
    assert JOB in out["context"]
    assert out["tokens"] <= budget


@pytest.mark.parametrize("budget", [400, 450, 500, 550])
def test_the_note_is_paid_by_both_shares(db, tenant, monkeypatch, budget):
    """With more facts than their share holds, the memories keep theirs: the
    note's bytes come out of both, not out of the memories alone."""
    ten_facts(db, tenant)
    memories(db, tenant, monkeypatch, 12)
    out = block(db, tenant, budget)
    taken = ctx._cost(facts_section(out["context"])) + ctx._cost(ctx._SEPARATOR)
    assert taken > room_the_old_share_left(budget), "the facts paid the whole note"
    assert ctx.MEMORIES_HEADING in out["context"], "the facts took the memories' share"
    assert out["tokens"] <= budget


def test_the_facts_keep_to_their_share(db, tenant, monkeypatch):
    """What the facts take is counted on the facts: never more than half of
    the room the note leaves, and a fact that would fit in the block is left
    out when it would not fit in the share."""
    budget = 400
    ten_facts(db, tenant)
    memories(db, tenant, monkeypatch, 12)
    out = block(db, tenant, budget)
    section = facts_section(out["context"])
    lines = section.split("\n")[1:]
    taken = ctx._cost(section) + ctx._cost(ctx._SEPARATOR)
    share = int(room_after_the_note(budget) * ctx._FACTS_BUDGET_FRACTION)
    assert 0 < len(lines) < 10, "every fact fit: the share was never what limited them"
    assert taken <= share
    next_fact = 1 + max(ctx._cost(line) for line in lines)
    assert taken + next_fact > share, "a fact was left out with room in its share"
    assert taken + next_fact <= room_after_the_note(budget), "the block was full, not the share"
    assert out["tokens"] <= budget


@pytest.mark.parametrize("budget", [400, 450])
@pytest.mark.parametrize("search", ["finds nothing", "is down"])
def test_with_no_memory_the_facts_take_the_room(db, tenant, monkeypatch, budget, search):
    """The share keeps room for the memories: when the search brings none,
    the facts are not held to it (with the search down, the NOTE's room had
    been given back to the facts for this reason since 1 Oct 2026)."""
    ten_facts(db, tenant)

    def down(self, *a, **k):
        raise MemorySearchUnavailable("down")
    monkeypatch.setattr(AgentMemoryEngine, "search",
                        down if search == "is down" else lambda self, *a, **k: [])
    out = block(db, tenant, budget)
    taken = ctx._cost(facts_section(out["context"])) + ctx._cost(ctx._SEPARATOR)
    assert taken > int(room_after_the_note(budget) * ctx._FACTS_BUDGET_FRACTION), \
        "the facts kept to a share with no memory to keep it for"
    assert out["tokens"] <= budget


@pytest.mark.parametrize("budget", [400, 450])
def test_a_memory_the_block_leaves_out_keeps_no_room(db, tenant, monkeypatch, budget):
    """The share is kept for the memories the block will show, not for every
    hit of the search: a hit with no text is not a line, so with only that
    one no memory follows and the facts take the room."""
    ten_facts(db, tenant)
    m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", graph_sync_status="pending",
                    content="x")
    db.add(m)
    db.commit()
    monkeypatch.setattr(AgentMemoryEngine, "search",
                        lambda self, *a, **k: [{"id": str(m.id), "content": "   "}])
    out = block(db, tenant, budget)
    assert ctx.MEMORIES_HEADING not in out["context"]
    taken = ctx._cost(facts_section(out["context"])) + ctx._cost(ctx._SEPARATOR)
    assert taken > int(room_after_the_note(budget) * ctx._FACTS_BUDGET_FRACTION), \
        "the facts kept to a share for a memory the block does not show"
    assert out["tokens"] <= budget
