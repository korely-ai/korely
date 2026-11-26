"""memory_core 6.26.0: an answer whose every fact the filters refuse is asked for once more.

A case of the benchmarks: the writer declared a fact, the
engine's filters (the quality gate of `read_facts`, the grounding of
`keep_grounded`) dropped it, and the memory ended 'done' with no fact, no
error and no second try (the bench case fukuoka-ja: "left no fact: its answer
held 1"). Now the same prompt is asked once more with the engine's reason
(`fact_extractor.NONE_KEPT`, through `model_json.asked_again`), and the second
answer is taken only when it keeps a fact.

No model call: the model is a list of scripted answers.
"""
from __future__ import annotations

from korely_agent.services.memory_core import fact_extractor, model_json


class _Model:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt, max_tokens):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def _fact(subject, obj, predicate="lives_in"):
    return ('{"facts": [{"subject": "%s", "predicate": "%s", "object": "%s", '
            '"confidence": 0.9, "tense": "current"}], "observation": ""}' % (subject, predicate, obj))


TEXT = "私は大阪に住んでいます。"
# The object written in another script than the memory's: the grounding drops it.
TRANSLATED = _fact("u-lang-3", "Osaka", "住んでいる")
AS_WRITTEN = _fact("u-lang-3", "大阪", "住んでいる")


def test_a_fact_the_grounding_drops_is_asked_again_with_the_reason():
    m = _Model(TRANSLATED, AS_WRITTEN)
    facts = fact_extractor.extract(m, TEXT, "u-lang-3")
    assert [(f.subject, f.object) for f in facts] == [("u-lang-3", "大阪")]
    assert len(m.prompts) == 2
    assert m.prompts[1] == model_json.asked_again(m.prompts[0], fact_extractor.NONE_KEPT.format(n=1))


def test_the_path_with_the_observation_asks_again_too():
    m = _Model(TRANSLATED, AS_WRITTEN)
    out = fact_extractor.extract_with_observation(m, TEXT, "u-lang-3", memory_date="2026-06-01")
    assert [f.object for f in out.facts] == ["大阪"]
    assert len(m.prompts) == 2


def test_a_fact_the_quality_gate_drops_is_asked_again():
    # A bare number as the object is refused by the quality gate (quality.drop_reason).
    m = _Model(_fact("Northwind Hosting", "50", "costs"),
               '{"facts": [{"subject": "Northwind Hosting", "predicate": "costs", "object": "50 euro per month", '
               '"object_is_literal": true, "confidence": 0.9, "tense": "current"}]}')
    facts = fact_extractor.extract(m, "Northwind Hosting costs 50 euro per month.", "u-now-8")
    assert [f.object for f in facts] == ["50 euro per month"]
    assert len(m.prompts) == 2


def test_still_none_kept_leaves_the_first_answer_and_its_observation():
    m = _Model(TRANSLATED.replace('"observation": ""', '"observation": "The user lives in Osaka."'),
               _fact("u-lang-3", "Osaka", "住んでいる"))
    out = fact_extractor.extract_with_observation(m, TEXT, "u-lang-3")
    assert out.facts == [] and out.observation == "The user lives in Osaka."
    assert len(m.prompts) == 2


def test_a_second_answer_that_cannot_be_read_leaves_the_memory_as_before():
    m = _Model(TRANSLATED, "not json at all")
    assert fact_extractor.extract(m, TEXT, "u-lang-3") == []
    assert len(m.prompts) == 2


def test_one_fact_kept_is_one_call():
    two = ('{"facts": [{"subject": "u-lang-3", "predicate": "住んでいる", "object": "Osaka", "confidence": 0.9},'
           '{"subject": "u-lang-3", "predicate": "住んでいる", "object": "大阪", "confidence": 0.9}]}')
    m = _Model(two)
    assert [f.object for f in fact_extractor.extract(m, TEXT, "u-lang-3")] == ["大阪"]
    assert len(m.prompts) == 1


def test_no_facts_declared_is_one_call():
    m = _Model('{"facts": []}')
    assert fact_extractor.extract(m, TEXT, "u-lang-3") == []
    assert len(m.prompts) == 1


def test_the_reason_names_no_word_of_the_memory():
    assert "大阪" not in fact_extractor.NONE_KEPT and "{n}" in fact_extractor.NONE_KEPT


# The review of 26 Nov 2026 (mutants M05, M07, M08, M10, M11).

def _fixed(subject, obj):
    return _fact(subject, obj, "住んでいる")


def test_with_the_fixed_prefix_the_second_answer_keeps_the_end_users_name():
    # Production writes with the fixed prefix: the rules name the end user END_USER, and
    # read_facts maps the name back to the handle in the second answer as in the first (M10).
    for path in ("extract", "extract_with_observation"):
        m = _Model(_fixed("END_USER", "Osaka"), _fixed("END_USER", "大阪"))
        out = getattr(fact_extractor, path)(m, TEXT, "u-lang-3", fixed_prefix=True)
        facts = out if path == "extract" else out.facts
        assert [(f.subject, f.object) for f in facts] == [("u-lang-3", "大阪")], path
        assert len(m.prompts) == 2
        assert m.prompts[1] == model_json.asked_again(m.prompts[0], fact_extractor.NONE_KEPT.format(n=1))


def test_the_log_of_the_second_try_holds_no_word_of_the_memory_nor_of_the_facts(caplog):
    import logging
    m = _Model(_fact("u-lang-3", "Osaka", "住んでいる"), _fact("u-lang-3", "Osaka", "住んでいる"))
    with caplog.at_level(logging.DEBUG):
        fact_extractor.extract_with_observation(m, TEXT, "u-lang-3")
    lines = [r.getMessage() for r in caplog.records]
    assert any("asked once more" in line for line in lines)
    for line in lines:   # M11: the declared facts, i.e. the memory's words, never in a log line
        for word in ("Osaka", "大阪", "住んでいる", "u-lang-3", "lives_in"):
            assert word not in line, line


def test_a_second_answer_without_its_list_leaves_the_first_and_does_not_raise():
    m = _Model(TRANSLATED, '{"fact": "u-lang-3 lives in 大阪"}')   # M05
    assert fact_extractor.extract(m, TEXT, "u-lang-3") == []
    assert len(m.prompts) == 2


def test_entries_without_their_three_parts_are_not_declared_facts():
    m = _Model('{"facts": [{"subject": "u-lang-3", "predicate": "住んでいる"}, "a string"]}')   # M07
    assert fact_extractor.extract(m, TEXT, "u-lang-3") == []
    assert len(m.prompts) == 1


def test_the_observation_is_the_second_answers_when_it_is_taken():
    m = _Model(TRANSLATED.replace('"observation": ""', '"observation": "first"'),
               AS_WRITTEN.replace('"observation": ""', '"observation": "second"'))   # M08
    out = fact_extractor.extract_with_observation(m, TEXT, "u-lang-3")
    assert out.observation == "second" and [f.object for f in out.facts] == ["大阪"]


def test_with_the_fixed_prefix_an_agents_memory_keeps_the_writers_name():
    # The same for an agent's memory with no end user: WRITER mapped back to the agent (M10).
    text = "Ho spostato il rilascio a Torino."
    m = _Model(_fact("WRITER", "Turin", "moved_release_to"), _fact("WRITER", "Torino", "moved_release_to"))
    out = fact_extractor.extract_with_observation(m, text, None, agent_id="planner", fixed_prefix=True)
    assert [(f.subject, f.object) for f in out.facts] == [("planner", "Torino")]
    assert len(m.prompts) == 2
