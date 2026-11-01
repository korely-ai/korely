"""After a memory is cut to fit, the block goes on packing (audit of
27 Oct 2026: a comment said the next iteration stops; the loop does not).

The behaviour, held: a long first memory is cut back to a sentence boundary,
the room that cut leaves is filled by the shorter memories after it, whole,
and the block never passes its budget.
"""
from korely_agent.services import context
from korely_agent.services.context import BYTES_PER_TOKEN


def _pack(monkeypatch, contents, budget):
    hits = [{"id": f"00000000-0000-0000-0000-{i:012d}", "content": c}
            for i, c in enumerate(contents)]
    monkeypatch.setattr(context, "_memory_rows", lambda db, h: {})
    monkeypatch.setattr(context, "_memories_for_the_block", lambda *a, **k: hits)
    monkeypatch.setattr(context.experiments, "DIALOGUE_ORDER", False)
    block = context._Block(budget)
    lines = context._pack_memories(None, block, hits, None, None)
    return block, lines


def test_the_loop_goes_on_after_a_truncated_fragment(monkeypatch):
    long_first = " ".join(f"Sentence number {i} is about the weekend away at the lake." for i in range(30))
    short = ["Anna likes tea.", "Luca owns a boat."]
    budget = 200
    block, lines = _pack(monkeypatch, [long_first] + short, budget)
    assert lines[0].startswith("- ") and len(lines[0]) < len(long_first)
    assert lines[0].rstrip().endswith("."), "cut at a sentence boundary"
    # The shorter memories after the cut one still found room, whole.
    assert any("Anna likes tea." in line for line in lines[1:]), lines
    assert block.pack.used <= budget * BYTES_PER_TOKEN
    assert len(block.sources) == len(lines)
