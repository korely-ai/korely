"""memory_core 2.12.0: a long memory is read in windows, never cut.

The decision of 6 October (code audit of 6 Oct 2026): the API accepts
16,000 characters, the extractor read the first 6,000 and the memory ended up
'ready' without the facts of the rest. Now `fact_extractor.windows` splits the text at
its boundaries (paragraph, line, sentence, space), with 500 characters repeated; one
call per window, the facts merged, and a window that fails makes
the whole memory fail.

No model is called: `call` is fake.
"""
# Notes: the last test runs through korely-agent's write
# (`facts.service.extract_and_persist_facts`) with the fake model behind this
# product's extractor (`facts.extractor._call_or_raise`), on its tables. This
# product always asks for the facts and the observation in one call; the
# hosted test switched the observation off (`agents_observation`).
from __future__ import annotations

import json
import re

import pytest

from korely_agent.services.memory_core import fact_extractor
from korely_agent.services.memory_core.fact_extractor import ExtractionFailed, windows

FILLER = "The weather in the harbour was grey and the boats stayed in port that week. "


def _text(n_chars: int, last: str) -> str:
    body = FILLER * (n_chars // len(FILLER))
    return body + last


class _Model:
    """Answers with one fact for each sentence "Maria works at X." of the window
    it receives; `fail_at` = the number of the call that fails."""

    def __init__(self, fail_at=None, observation=False, times=False):
        self.prompt = []
        self.fail_at, self.observation, self.times = fail_at, observation, times

    def __call__(self, prompt, max_tokens):
        self.prompt.append(prompt)
        if self.fail_at is not None and len(self.prompt) == self.fail_at:
            raise RuntimeError("the window could not be read")
        memory = prompt.rsplit(":\n", 1)[1]
        facts = [
            {"subject": "Maria", "subject_type": "person", "predicate": "works_at", "object": o,
             "object_is_literal": False, "confidence": 0.9, "tense": "current", "valid_from": "",
             "evidence": f"Maria works at {o}."}
            for o in re.findall(r"Maria works at (\w+)\.", memory)
        ]
        answer = {"facts": facts}
        if self.observation:
            answer["observation"] = f"The user talked about {len(facts)} employers." if facts else ""
        if self.times:
            answer["relative_times"] = []
        return json.dumps(answer)


class TestWindows:
    def test_a_short_text_is_one_identical_window(self):
        t = "  Maria works at Acme.  "
        assert windows(t) == ["Maria works at Acme."]
        assert windows("   ") == [] and windows(None) == []

    def test_twelve_thousand_characters_three_overlapping_windows(self):
        t = _text(12000, "Maria works at Globex.")
        pieces = windows(t)
        assert len(pieces) == 3
        assert all(len(p) <= fact_extractor.MAX_CONTENT_CHARS for p in pieces)
        for a, b in zip(pieces, pieces[1:]):
            assert a[-200:] in b, "the next window repeats the end of the previous one"
        # Every sentence of the text sits whole in at least one window.
        for sentence in re.findall(r"[^.]+\.", t):
            assert any(sentence.strip() in p for p in pieces)

    def test_it_cuts_at_the_paragraph_when_there_is_one(self):
        first = "a " * 2400 + "end of the first part."
        t = first + "\n\n" + "b " * 2000 + "\n\n" + "Maria works at Globex."
        pieces = windows(t, width=6000)
        assert pieces[0] == first.strip()
        assert pieces[-1].endswith("Maria works at Globex.")

    def test_a_script_without_spaces_is_cut_after_a_mark(self):
        sentence = "玛丽在环球公司工作。"
        t = sentence * 1500
        pieces = windows(t, width=6000)
        assert len(pieces) >= 3
        assert all(p.endswith("。") for p in pieces[:-1])

    def test_no_word_cut_in_half_at_the_start_of_a_window(self):
        t = _text(12000, "Maria works at Globex.")
        for p in windows(t)[1:]:
            assert t.find(p) == 0 or t[t.find(p) - 1] in " ."


class TestExtract:
    def test_a_single_fact_at_the_end_of_twelve_thousand_characters(self):
        """Before: the text stopped at 6,000 characters and Globex was not read."""
        t = _text(12000, "Maria works at Globex.")
        m = _Model()
        facts = fact_extractor.extract(m, t, "maria")
        assert [f.object for f in facts] == ["Globex"]
        assert len(m.prompt) == len(windows(t)) == 3

    def test_a_short_memory_one_call_and_the_same_text(self):
        m = _Model()
        fact_extractor.extract(m, "Maria works at Acme.", "maria")
        assert len(m.prompt) == 1 and m.prompt[0].endswith("Memory:\nMaria works at Acme.\n")

    def test_a_window_that_fails_makes_the_memory_fail(self):
        t = _text(12000, "Maria works at Globex.")
        with pytest.raises(ExtractionFailed):
            fact_extractor.extract(_Model(fail_at=2), t, "maria")

    def test_the_same_fact_in_two_windows_is_written_once(self):
        half = FILLER * (5900 // len(FILLER))
        t = half + "Maria works at Acme. " + half + "Maria works at Acme."
        m = _Model()
        facts = fact_extractor.extract(m, t, "maria")
        assert len(m.prompt) >= 2 and [f.object for f in facts] == ["Acme"]

    def test_the_facts_per_window(self):
        """MAX_FACTS holds per window: a long memory does not lose the facts
        of the windows after the first."""
        names = ["Alfa", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel"]
        first = " ".join(f"Maria works at {n}." for n in names)
        t = first + " " + FILLER * 160 + " Maria works at Zeta."
        facts = fact_extractor.extract(_Model(), t, "maria")
        objects = [f.object for f in facts]
        assert "Zeta" in objects and len(objects) == 9


class TestWithObservation:
    def test_the_windows_and_the_sentences_merged(self):
        t = _text(12000, "Maria works at Globex.")
        m = _Model(observation=True)
        e = fact_extractor.extract_with_observation(m, t, "maria")
        assert [f.object for f in e.facts] == ["Globex"]
        assert len(m.prompt) == 3
        assert e.observation == "The user talked about 1 employers."

    def test_the_relative_times_only_if_every_window_answered(self):
        t = _text(12000, "Maria works at Globex.")
        e = fact_extractor.extract_with_observation(_Model(observation=True, times=True), t, "maria",
                                               memory_date="2026-10-06", times=True)
        assert e.times is not None and e.times["times"] == []
        e = fact_extractor.extract_with_observation(_Model(observation=True), t, "maria",
                                               memory_date="2026-10-06", times=True)
        assert e.times is None

    def test_a_window_that_fails(self):
        t = _text(12000, "Maria works at Globex.")
        with pytest.raises(ExtractionFailed):
            fact_extractor.extract_with_observation(_Model(fail_at=3, observation=True), t, "maria")


class TestInTheBatch:
    # memory_core 4.0.0: `extract_batch_answers` and `extract_batch` are gone
    # (only tests called them); the batched path that stays is the one with
    # the observation, and a long memory stays out of it in the same way.
    def test_with_observation_the_long_one_stays_out_and_has_no_answer(self):
        long = _text(12000, "Maria works at Globex.")
        seen = []

        def call(prompt, n):
            seen.append(prompt)
            return json.dumps({"memories": [{"mem_id": "a", "facts": [], "observation": ""},
                                            {"mem_id": "b", "facts": [], "observation": ""}]})

        out = fact_extractor.extract_batch_with_observation(call, [("a", "x y"), ("b", "z w"), ("c", long)], "maria")
        assert set(out) == {"a", "b"}
        assert len(seen) == 1 and "[mem_id=c]" not in seen[0]


def test_from_the_cloud_the_long_memory_does_not_end_ready_halfway(db, tenant, monkeypatch):
    """A window that fails: the memory is not 'done' and no fact is
    written (all or nothing, like a short memory)."""
    from korely_agent.db.models import AgentFact, AgentMemory
    from korely_agent.services.facts import extractor as extr
    from korely_agent.services.facts import service as svc

    monkeypatch.setattr(svc, "_embed_fact", lambda *a, **k: None)
    monkeypatch.setattr(extr, "_call_or_raise", lambda *a, **k: _Model(fail_at=3))
    t = "Maria works at Acme. " + _text(12000, "Maria works at Globex.")
    m = AgentMemory(tenant_id=tenant.id, end_user_id="maria", content=t, graph_sync_status="pending")
    db.add(m)
    db.commit()
    svc.extract_and_persist_facts(db, tenant.id, memory=m)
    db.refresh(m)
    assert m.graph_sync_status != "done"
    assert db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).count() == 0

    monkeypatch.setattr(extr, "_call_or_raise", lambda *a, **k: _Model())
    m.graph_sync_status = "pending"
    db.commit()
    svc.extract_and_persist_facts(db, tenant.id, memory=m)
    db.refresh(m)
    assert m.graph_sync_status == "done"
    assert {f.object_label for f in db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id)} == {
        "Acme", "Globex"}
