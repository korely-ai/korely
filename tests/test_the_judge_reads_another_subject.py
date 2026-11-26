"""memory_core 5.8.0, off: the judge reads [another subject] (18 Oct 2026).

On the identity bench of 17 Oct the judge closed 2-3 of the user's facts per
run with a namesake colleague's ("Marco Bianchi works the day shift" closed
"marco-n does night shifts"). Switched on, an earlier fact whose subject is
another name than the new fact's, aliases applied, carries a tag, and the
judge reads one rule more (Ritter et al. EMNLP 2008 §4, argument ambiguity).
Off, every prompt is 5.4.0's byte for byte.
"""
import uuid
from types import SimpleNamespace

from korely_agent.services.memory_core import contradiction, judge


def fact(subject, canonical=None, obj="night shifts"):
    return SimpleNamespace(id=uuid.uuid4(), subject_label=subject, subject_canonical=canonical,
                           predicate_raw="works", predicate_canonical="works", object_label=obj,
                           valid_from=None)


def asked(**kw):
    seen = {}

    def call(prompt, max_tokens):
        seen["prompt"] = prompt
        return '{"contradictions": []}'

    judge.judge(call, kw.pop("candidates"), "Marco Bianchi", "works", "the day shift",
                    single_value=lambda c: (False, None), **kw)
    return seen["prompt"]


def test_off_the_question_is_the_one_of_before():
    prompt = asked(candidates=[fact("marco-n")])
    assert "[another subject]" not in prompt
    assert contradiction.RULE_OTHER_SUBJECT not in prompt
    assert prompt.startswith(contradiction.AGENTS_JUDGE.split("{EXISTING}")[0])


def test_on_another_name_is_tagged_and_the_rule_is_read():
    prompt = asked(candidates=[fact("marco-n")], subject_canonical="Marco Bianchi",
                   mark_other_subject=True)
    assert "marco-n works night shifts [another subject]" in prompt
    assert contradiction.RULE_OTHER_SUBJECT in prompt


def test_on_the_same_entity_by_an_alias_is_not_tagged():
    prompt = asked(candidates=[fact("Marco", canonical="Marco Bianchi")],
                   subject_canonical="Marco Bianchi", mark_other_subject=True)
    assert "Marco Bianchi (then: Marco) works night shifts\n" in prompt, "the row, untagged"


def test_on_the_batch_judge_tags_by_each_new_facts_subject():
    seen = {}

    def call(prompt, max_tokens):
        seen["prompt"] = prompt
        return '{"results": [{"new_id": "n1", "contradictions": []}]}'

    groups = [{"new_id": "n1", "new_text": "Marco Bianchi works the day shift",
               "candidates": [fact("marco-n"), fact("Marco Bianchi", obj="nights")],
               "new_subject": "Marco Bianchi"}]
    judge.judge_batch(call, groups, single_value=lambda c: (False, None),
                          mark_other_subject=True)
    assert "marco-n works night shifts [another subject]" in seen["prompt"]
    assert "Marco Bianchi works nights [another subject]" not in seen["prompt"]
    off = {}
    judge.judge_batch(lambda p, m: off.setdefault("p", p) and '{"results": [{"new_id": "n1", "contradictions": []}]}',
                          groups, single_value=lambda c: (False, None))
    assert "[another subject]" not in off["p"]
