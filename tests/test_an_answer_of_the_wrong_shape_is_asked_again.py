"""memory_core 6.23.0: a readable answer of the wrong shape is asked for once more.

Measured cause (25 Nov 2026, the public date test re-extracted with
gpt-oss-120b): 3 of the 5 memories still in 'error' had an extraction answer
that was a readable JSON object with no list of facts, at the first answer and
not cut; 1 a batch judge answer with no "results". Until 6.23.0 such an answer
ended the memory in 'error' at once. Now the same prompt is asked once more
with the reason after it (`model_json.asked_again`, after Graphiti's retry with
the error), and only a second answer of the wrong shape is an error. An answer
that is no JSON at all (a cut one) is not asked again here: it has its own
second call with more room in core/llm.

No model call: the model is a list of scripted answers.
"""
from __future__ import annotations

import pytest

from korely_agent.services.memory_core import fact_extractor, judge, model_json


class _Model:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt, max_tokens):
        self.prompts.append(prompt)
        return self.answers.pop(0)


FACT = ('{"facts": [{"subject": "Maria", "predicate": "lives_in", "object": "Turin", '
        '"confidence": 0.9, "tense": "current"}]}')
TEXT = "Maria lives in Turin."


def test_an_object_without_its_list_is_asked_again_with_the_reason():
    m = _Model('{"fact": "Maria lives in Turin"}', FACT)
    facts = fact_extractor.extract(m, TEXT, "u1")
    assert [f.object for f in facts] == ["Turin"]
    assert len(m.prompts) == 2
    assert m.prompts[1].startswith(m.prompts[0])
    assert m.prompts[1].endswith(model_json.ASK_AGAIN.format(what='a JSON object with no "facts" list'))


def test_twice_the_wrong_shape_is_still_an_error():
    m = _Model('{"fact": 1}', '{"items": []}')
    with pytest.raises(fact_extractor.ExtractionFailed):
        fact_extractor.extract(m, TEXT, "u1")
    assert len(m.prompts) == 2


def test_a_right_answer_is_one_call_and_the_same_prompt():
    m = _Model(FACT)
    assert [f.object for f in fact_extractor.extract(m, TEXT, "u1")] == ["Turin"]
    assert len(m.prompts) == 1 and "previous answer" not in m.prompts[0]


def test_no_facts_is_an_answer_and_is_not_asked_again():
    m = _Model('{"facts": []}')
    assert fact_extractor.extract(m, TEXT, "u1") == []
    assert len(m.prompts) == 1


def test_an_answer_that_is_no_json_is_not_asked_again_here():
    m = _Model('{"facts": [{"subject": "Maria", "predic')
    with pytest.raises(fact_extractor.ExtractionFailed):
        fact_extractor.extract(m, TEXT, "u1")
    assert len(m.prompts) == 1


def test_the_judge_is_asked_again_once():
    m = _Model('{"verdict": "none"}', '{"contradictions": []}')
    assert judge._answer(m, "PROMPT", "contradictions", None, "contradiction judge") == []
    assert m.prompts == ["PROMPT", model_json.asked_again("PROMPT", 'a JSON answer with no "contradictions" list')]
    m = _Model('{"verdict": "none"}', '{"verdict": "none"}')
    with pytest.raises(judge.JudgementUnreadable):
        judge._answer(m, "PROMPT", "contradictions", None, "contradiction judge")
    assert len(m.prompts) == 2
    m = _Model('not json at all')
    with pytest.raises(judge.JudgementUnreadable):
        judge._answer(m, "PROMPT", "contradictions", None, "contradiction judge")
    assert len(m.prompts) == 1
