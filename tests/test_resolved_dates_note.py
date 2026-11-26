"""The line on the dates in brackets, under the memories' heading (2026-10-07).

On by default, the same line and place as the hosted product: a
relative time carries its computed date ("next month [June 2023]"), and the
reader is told not to recompute it. Under "## Relevant memories", only when the
block shows memories: the note (`stable`, the cached part) is the one of
before, and a block with no memory lines keeps its room for the facts.

1.0.40: each sentence of the line where what it reads is in the block. The
sentence on the times in brackets only when a memory shown carries resolved
times (its stored `relative_times`), paid by the first such memory at its
turn in the packing; the sentence on the day in front of each memory
whenever a memory is shown. Until 1.0.39 the whole line, 290 bytes, came
with every memory, and a 400-token block of a team's notes, which carry no
relative time, kept room for one memory (measured on 25 Nov 2026).
"""
from datetime import date, datetime, timezone
from types import SimpleNamespace
import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services import vectors
from korely_agent.services.memory_core.relative_time import annotate
from korely_agent.services.memory_engine import AgentMemoryEngine, MemorySearchUnavailable
from tests.conftest import seeded_project_id



NEXT_MONTH = {"text": "next month", "direction": "next", "unit": "month"}


def add(db, tenant, content, times=None, **kw):
    """A memory with a vector; `times` the model's relative times, dated
    from today as the write dates them (`relative_time.annotate`). `times=[]`
    is the column a write stores when the model found none: {"v", "anchor",
    "times": []}, a column that is there and holds no time."""
    column = annotate(times, date.today(), content)[0] if times is not None else None
    db.add(AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                       content=content, relative_times=column,
                       embedding=[0.1] * EMBEDDING_DIM, embedding_status=vectors.DONE, **kw))
    db.commit()


@pytest.fixture
def memory(db, tenant, monkeypatch):
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    add(db, tenant, "user: Maria moves to Rome next month.", [NEXT_MONTH], end_user_id="maria")


def block(db, tenant, budget=2000):
    return ctx.assemble_context(db, tenant.id, query="When does Maria move?",
                                end_user_id="maria", token_budget=budget)


def test_on_under_the_memories_heading(db, tenant, memory):
    assert ctx._RESOLVED_DATES_NOTE is True
    out = block(db, tenant)
    assert ctx.MEMORIES_HEADING + "\n" + ctx.RESOLVED_DATES_LINE + "\n- " in out["volatile"]
    assert out["stable"] == ctx._READER_GUIDANCE


def test_the_line_is_the_only_difference(db, tenant, memory, monkeypatch):
    on = block(db, tenant)
    monkeypatch.setattr(ctx, "_RESOLVED_DATES_NOTE", False)
    off = block(db, tenant)
    assert on["stable"] == off["stable"] and on["sources"] == off["sources"]
    assert on["volatile"].replace("\n" + ctx.RESOLVED_DATES_LINE, "", 1) == off["volatile"]


def test_no_memories_no_line_and_the_facts_keep_their_room(db, tenant, monkeypatch):
    from korely_agent.services.facts.service import write_fact_direct

    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    write_fact_direct(db, tenant.id, subject="maria", predicate="lives_in", obj="Rome",
                      end_user_id="maria", project_id=seeded_project_id(db, tenant.id))
    db.commit()

    def down(self, *a, **k):
        raise MemorySearchUnavailable("down")

    monkeypatch.setattr(AgentMemoryEngine, "search", down)
    out = block(db, tenant, budget=400)
    assert ctx.RESOLVED_DATES_LINE not in out["context"]
    assert "Rome" in out["context"]


def test_a_memory_with_no_relative_time_has_the_sentence_on_the_day_alone(db, tenant, monkeypatch):
    """What the line reads is in the block, and only that: the day in front
    of the memory, and no word about times in brackets it does not hold."""
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    add(db, tenant, "user: Maria lives in Rome.", end_user_id="maria")
    out = block(db, tenant)
    assert ctx.MEMORIES_HEADING + "\n" + ctx.MEMORY_DAY_LINE + "\n- " in out["volatile"]
    assert "square brackets" not in out["context"]


def test_the_first_memory_with_times_pays_for_their_sentence(db, tenant, monkeypatch):
    """A memory with no time first, one with a time after it: the block says
    both sentences, as it did with the whole line, and the same memories."""
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    add(db, tenant, "user: Maria lives in Rome.", end_user_id="maria")
    add(db, tenant, "user: Maria moves to Milan next month.", [NEXT_MONTH], end_user_id="maria")
    out = block(db, tenant)
    assert ctx.MEMORIES_HEADING + "\n" + ctx.RESOLVED_DATES_LINE + "\n- " in out["volatile"]
    assert len([s for s in out["sources"] if s.startswith("mem_")]) == 2


def test_a_400_token_block_of_notes_gives_their_room_to_the_notes(db, tenant, monkeypatch):
    """The case measured on 25 Nov 2026: an agent's notes, no relative
    time in any, read at 400 tokens. The 290 bytes of the whole line, spent
    on a sentence about nothing the block holds, were a second note's room."""
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    # One sentence of 312 bytes each, which no cut shortens: under the whole
    # line one fits in the 561 bytes the note and the heading leave, under
    # the sentence on the day alone two fit in 771.
    # The column as the write stores it for a note with no time (the model
    # answers "relative_times": []), not an absent one: the line reads the
    # times in it, never whether it is there (the review of 26 Nov 2026).
    for i in range(4):
        add(db, tenant, f"Lesson {i}: " + "the release waits for the site check and " * 7
            + "then it ships.", [], agent_id="reviewer")
    out = ctx.assemble_context(db, tenant.id, query="lesson on the release", agent_id="reviewer",
                               token_budget=400)
    assert "square brackets" not in out["context"]
    assert len([s for s in out["sources"] if s.startswith("mem_")]) >= 2
    assert out["tokens"] <= 400


def test_an_agents_note_with_a_time_in_second_place_has_both_sentences(db, tenant, monkeypatch):
    """A note of no end user after the first line is shown in its share
    (`_Note`, `_pack_memory_lines`): when it carries a time, the block says
    what the brackets mean, as for any other line (the review)."""
    from korely_agent.services import context_experiments as experiments
    from korely_agent.services.memory_engine import AgentMemoryEngine

    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
    monkeypatch.setattr(experiments, "DIALOGUE_ORDER", False)
    add(db, tenant, "Lesson 0: the release waits for the site check.", [], agent_id="reviewer")
    add(db, tenant, "Lesson 1: the deploy goes out next month, after the check.", [NEXT_MONTH],
        agent_id="reviewer")
    rows = db.query(AgentMemory).order_by(AgentMemory.content).all()
    found = [{"id": str(m.id), "content": m.content, "score": 0.9 - 0.1 * i}
             for i, m in enumerate(rows)]
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, *a, **k: list(found))
    out = ctx.assemble_context(db, tenant.id, query="the deploy", agent_id="reviewer",
                               token_budget=2000)
    assert "next month [" in out["context"]
    assert ctx.MEMORIES_HEADING + "\n" + ctx.RESOLVED_DATES_LINE + "\n- " in out["context"]


def _rows(monkeypatch, column_of):
    """`_memory_rows` for hits packed without a database: each hit's row
    with the times `column_of` gives its id."""
    def rows(db, hits):
        return {h["id"]: SimpleNamespace(
            observation=None, event_time=None, end_user_id="maria",
            created_at=datetime(2026, 11, 9, tzinfo=timezone.utc),
            relative_times=column_of.get(h["id"])) for h in hits}
    from korely_agent.services import context_experiments as experiments
    monkeypatch.setattr(ctx, "_memory_rows", rows)
    monkeypatch.setattr(experiments, "DIALOGUE_ORDER", False)


def test_a_dated_memory_left_out_for_its_room_leaves_the_short_line(monkeypatch):
    """A memory with a time that does not fit, left out and named, pays for
    nothing and turns nothing on: the block shows the sentence on the day
    alone, within its budget (the review)."""
    first = "Maria told us about the quiet streets of the old town, and the light. " * 8
    dated = "Maria moves to Milan next month " + "with the whole family and the dog " * 8 + "."
    hits = [{"id": "00000000-0000-0000-0000-000000000001", "content": first},
            {"id": "00000000-0000-0000-0000-000000000002", "content": dated}]
    _rows(monkeypatch, {hits[1]["id"]: annotate([NEXT_MONTH], date(2026, 11, 9), dated)[0]})
    block, left_out = ctx._Block(200), []
    lines = ctx._pack_memory_lines(block, ctx._memory_lines(None, hits, hits), left_out)
    assert len(lines) == 1 and left_out == [ctx.mem_public_id(hits[1]["id"])]
    assert block.dated is False
    assert block.pack.used <= 200 * ctx.BYTES_PER_TOKEN


def test_the_first_memorys_floor_counts_the_sentence_on_its_times(monkeypatch):
    """The fewest bytes the first memory needs (`_first_memory_floor`, what
    the agent notices leave it) include the sentence on the times when that
    memory carries one, and only then (the review)."""
    text = "Maria moves to Milan next month."
    hits = [{"id": "00000000-0000-0000-0000-000000000001", "content": text}]
    _rows(monkeypatch, {hits[0]["id"]: annotate([NEXT_MONTH], date(2026, 11, 9), text)[0]})
    dated = ctx._memory_lines(None, hits, hits)
    _rows(monkeypatch, {hits[0]["id"]: annotate([], date(2026, 11, 9), text)[0]})
    plain = ctx._memory_lines(None, hits, hits)
    block = ctx._Block(400)
    extra = ctx._cost(ctx._memories_heading(True)) - ctx._cost(ctx._memories_heading(False))
    assert (ctx._first_memory_floor(block, dated) - ctx._first_memory_floor(block, plain)
            == extra + ctx._cost(dated[0][0]) - ctx._cost(plain[0][0]))


def test_a_memory_shown_by_its_window_with_a_time_has_both_sentences(monkeypatch):
    """MEMORY_WINDOWS: a first memory too long for its room, shown by the
    sentences of its best window (`_in_the_window`), which hold its time:
    the block says what the brackets mean (the review)."""
    filler = "The old town is quiet in the evening and the streets are empty. " * 12
    text = filler + "Maria moves to Milan next month. " + filler
    start = text.index("Maria moves")
    hits = [{"id": "00000000-0000-0000-0000-000000000001", "content": text,
             "_window": (start, start + len("Maria moves to Milan next month."))}]
    _rows(monkeypatch, {hits[0]["id"]: annotate([NEXT_MONTH], date(2026, 11, 9), text)[0]})
    block = ctx._Block(150)
    lines = ctx._pack_memory_lines(block, ctx._memory_lines(None, hits, hits), [], "Milan")
    assert len(lines) == 1 and "Milan next month [" in lines[0]
    assert block.dated is True
    assert block.pack.used <= 150 * ctx.BYTES_PER_TOKEN
