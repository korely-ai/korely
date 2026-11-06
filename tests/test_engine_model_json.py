"""memory_core 2.11.0: the model's JSON read with the rule, and an answer
without the list of facts that is not "no fact" (code audit of 6 Oct 2026).

`memory_core/model_json.read_object` is the engine's own reader (the folder
imports nothing from the app), tested on the cases its earlier copy answered
and bound to answer the same. Before, the extractor and the judge read with a
bare `json.loads` and then with `extract_json_from_llm_response`, which cuts
from the first brace to the end:
the documented case of gemini-3.1-flash-lite, `{"facts": []}\\n}`, ended in
'error'. And an answer without `facts` meant "no fact": on an edited memory it
closed all the facts of the previous version.

No model call.
"""
# Notes:
#   * the engine's reader was compared with an earlier copy of the same rule,
#     gone since 11 Oct 2026 (one copy left). The cases stay, each with what
#     that copy answered (computed with it on 12 Oct 2026, the two copies
#     agreed on all of them): the engine must still answer the same.
#   * test_the_same_with_the_clouds_old_reader passes the reader
#     korely-agent's adapter gives the engine, `core.llm.extract_json`,
#     which also cuts from the first brace to the last: the rule still comes first.
from __future__ import annotations

import pytest

from korely_agent.services.memory_core import fact_extractor, judge
from korely_agent.services.memory_core import model_json as mc
from korely_agent.services.memory_core.fact_extractor import ExtractionFailed

# (text, what the earlier copy of the reader answered, and the list of JSON
# objects it found).
CASES = [
    ('{"facts": []}', {"facts": []}, [{"facts": []}]),
    ('{"facts": []}\n}', {"facts": []}, [{"facts": []}]),                 # gemini-3.1-flash-lite, 1 Oct
    ('```json\n{"a": 1}\n```', {"a": 1}, [{"a": 1}]),
    ('```\n{"a": 1}\n```', {"a": 1}, [{"a": 1}]),
    ('Here is the JSON: {"a": 1} and nothing else', {"a": 1}, [{"a": 1}]),
    ('{"a": 1}\n{"a": 1}', {"a": 1}, [{"a": 1}, {"a": 1}]),               # the same object twice
    ('{"a": 1} {"a": 2}', None, [{"a": 1}, {"a": 2}]),                     # two answers: none
    ('{"x": "a } inside"} trailing', {"x": "a } inside"}, [{"x": "a } inside"}]),
    ('[1, 2]', [1, 2], []),
    ('  {"a": {"b": [1, 2]}}  ', {"a": {"b": [1, 2]}}, [{"a": {"b": [1, 2]}}]),
    ('not json at all', None, []),
    ('', None, []),
    (None, None, []),
    ('{"facts": [{"subject": "Maria"', None, []),                          # cut by the token ceiling
]


@pytest.mark.parametrize("text, obj, objects", CASES, ids=range(len(CASES)))
def test_the_two_copies_answer_the_same(text, obj, objects):
    assert mc.read_object(text) == obj
    assert mc.json_objects(text or "") == objects


def test_the_extra_brace_is_read():
    assert mc.read_object('{"facts": []}\n}') == {"facts": []}


class TestTheEnginesReader:
    def test_a_reader_that_returns_the_text_as_before(self):
        assert mc.read_answer("garbage", lambda raw: '{"facts": []}') == {"facts": []}

    def test_a_reader_that_returns_the_value(self):
        assert mc.read_answer("garbage", lambda raw: {"facts": []}) == {"facts": []}

    def test_a_reader_that_fails_reads_nothing(self):
        def broken(raw):
            raise ValueError("no")
        assert mc.read_answer("garbage", broken) is None

    def test_the_rule_comes_before_the_reader(self):
        seen = []
        assert mc.read_answer('{"a": 1}\n}', lambda raw: seen.append(raw)) == {"a": 1}
        assert seen == []


def _call(answer):
    return lambda prompt, max_tokens: answer


class TestTheExtractor:
    def test_the_extra_brace_is_no_fact_not_an_error(self):
        assert fact_extractor.extract(_call('{"facts": []}\n}'), "Maria works at Acme.", "u1") == []

    def test_the_same_with_the_clouds_old_reader(self):
        """`extract_json_from_llm_response` (the cloud's reader until 2.11.0)
        does not read it either: the rule comes first."""
        from korely_agent.core.llm import extract_json

        assert fact_extractor.extract(_call('{"facts": []}\n}'), "Maria works at Acme.", "u1",
                                 extract_json) == []

    @pytest.mark.parametrize("answer", ['{}', '{"fact": []}', '{"facts": null}', '{"facts": "none"}'])
    def test_an_answer_without_the_list_is_not_no_fact(self, answer):
        with pytest.raises(ExtractionFailed):
            fact_extractor.extract(_call(answer), "Maria works at Acme.", "u1")

    def test_not_even_with_the_observation(self):
        with pytest.raises(ExtractionFailed):
            fact_extractor.extract_with_observation(_call('{"observation": "The user works at Acme."}'),
                                               "Maria works at Acme.", "u1")

    def test_an_empty_list_is_no_fact(self):
        assert fact_extractor.extract(_call('{"facts": []}'), "Maria works at Acme.", "u1") == []
        assert fact_extractor.extract(_call('[]'), "Maria works at Acme.", "u1") == []


class TestConfidenceIsNotAThreshold:
    """Xiong et al., ICLR 2024: the declared confidence does not separate right
    from wrong (CONTRIBUTING.md: the confidence a model states is not a
    threshold)."""

    def _fact(self, **fields):
        base = {"subject": "Maria", "subject_type": "person", "predicate": "works_at", "object": "Acme"}
        base.update(fields)
        return base

    def test_a_fact_without_confidence_stays(self):
        facts = fact_extractor.read_facts([self._fact()], "Maria works at Acme.")
        assert len(facts) == 1 and facts[0].confidence == 0.0 and facts[0].is_valid()

    def test_a_fact_with_low_confidence_stays(self):
        facts = fact_extractor.keep_grounded(fact_extractor.read_facts([self._fact(confidence=0.2)], "Maria works at Acme."),
                                  "Maria works at Acme.", "u1")
        assert [f.object for f in facts] == ["Acme"] and facts[0].confidence == 0.2

    def test_from_the_whole_extraction(self):
        answer = '{"facts": [{"subject": "Maria", "predicate": "works_at", "object": "Acme"}]}'
        assert [f.object for f in fact_extractor.extract(_call(answer), "Maria works at Acme.", "u1")] == ["Acme"]


class TestTheJudge:
    def test_the_extra_brace_is_a_verdict(self):
        candidate = type("C", (), {"id": "0b0b0b0b-0000-0000-0000-000000000001", "subject_label": "Maria",
                                   "subject_canonical": "Maria", "predicate_raw": "works at",
                                   "object_label": "Acme", "valid_from": None})()
        closed, reasons = judge.judge(_call('{"contradictions": []}\n}'), [candidate], "Maria",
                                         "works at", "Globex", single_value=lambda c: (False, None))
        assert closed == [] and reasons == {}

    def test_two_different_answers_are_not_a_verdict(self):
        candidate = type("C", (), {"id": "0b0b0b0b-0000-0000-0000-000000000001", "subject_label": "Maria",
                                   "subject_canonical": "Maria", "predicate_raw": "works at",
                                   "object_label": "Acme", "valid_from": None})()
        with pytest.raises(judge.JudgementUnreadable):
            judge.judge(_call('{"contradictions": []} {"contradictions": [{"existing_id": "x"}]}'),
                            [candidate], "Maria", "works at", "Globex", single_value=lambda c: (False, None))
