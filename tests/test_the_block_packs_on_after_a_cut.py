"""After a memory is cut to fit, the block goes on packing (audit of
27 Oct 2026: a comment said the next iteration stops; the loop does not).

The behaviour, held: a long first memory is cut back to a sentence boundary,
the room that cut leaves is filled by the shorter memories after it, whole,
and the block never passes its budget. Since 1.0.30 the cut falls only after
a whole sentence (or, for a text with no sentence end in its first 1,200
characters, after a word, marked), and a memory whose first sentence does
not fit is left out and named (tests/test_a_memory_is_cut_after_a_whole_sentence.py).
"""
from korely_agent.services import context
from korely_agent.services.context import BYTES_PER_TOKEN


def _pack(monkeypatch, contents, budget):
    hits = [{"id": f"00000000-0000-0000-0000-{i:012d}", "content": c}
            for i, c in enumerate(contents)]
    monkeypatch.setattr(context, "_memory_rows", lambda db, h: {})
    monkeypatch.setattr(context.experiments, "DIALOGUE_ORDER", False)
    block = context._Block(budget)
    lines = context._pack_memory_lines(block, context._memory_lines(None, hits, hits))
    return block, lines


def test_the_loop_goes_on_after_a_truncated_fragment(monkeypatch):
    long_first = " ".join(f"Sentence number {i} is about the weekend away at the lake." for i in range(30))
    short = ["Anna likes tea.", "Luca owns a boat."]
    # A room the cut leaves a short memory's line in: the first memory is
    # cut after its twelfth sentence. (200 until 1.0.39, when the whole line
    # on the dates stood under the heading; with the line on the day alone,
    # 1.0.40, the cut at 200 fills the room to 4 bytes.)
    budget = 205
    block, lines = _pack(monkeypatch, [long_first] + short, budget)
    assert lines[0].startswith("- ") and len(lines[0]) < len(long_first)
    assert lines[0].rstrip().endswith("."), "cut at a sentence boundary"
    # The shorter memories after the cut one still found room, whole.
    assert any("Anna likes tea." in line for line in lines[1:]), lines
    assert block.pack.used <= budget * BYTES_PER_TOKEN
    assert len(block.sources) == len(lines)


def test_a_memory_whose_first_character_passes_the_room_is_left_out(monkeypatch):
    """A memory whose first character after its date is one character, as a
    reader sees it, longer than the room (a letter under 1,200 marks): the
    cut never splits it. Until 1.0.29 the line was the date and the
    ellipsis (the 1.0.15 CHANGELOG); since 1.0.30 every cut of a memory
    shows at least 40 characters, and the 12 of the date are fewer: it is
    left out and named."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    hits = [{"id": "00000000-0000-0000-0000-000000000001",
             "content": "Z" + "\u0301\u0300\u0302\u0303" * 300}]
    row = SimpleNamespace(observation=None, event_time=None, relative_times=None,
                          created_at=datetime(2026, 11, 9, tzinfo=timezone.utc))
    monkeypatch.setattr(context, "_memory_rows", lambda db, h: {hits[0]["id"]: row})
    monkeypatch.setattr(context.experiments, "DIALOGUE_ORDER", False)
    block = context._Block(200)
    left_out = []
    lines = context._pack_memory_lines(block, context._memory_lines(None, hits, hits), left_out)
    assert lines == [] and block.sources == []
    assert left_out == [context.mem_public_id(hits[0]["id"])]
    assert block.pack.used <= 200 * BYTES_PER_TOKEN
