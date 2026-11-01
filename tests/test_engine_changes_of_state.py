"""A change of state is the new state, behind its switch (memory_core 3.1.0, 2026-10-06).

An acceptance run of the Self-hosted with gpt-oss-120b, twice 12/13:
"Globex moved to the Starter plan." became an event in the past
(`changes_plan_to_starter_plan`, then `moves_to`, tense past) instead of the
new state, so the old plan stayed open. The rule (extraction.RULES_STATES)
asks for the new value as the state, current, and keeps the value out of the
predicate. It is off by default: the measured prompts stay byte for byte
(TestThe2_8_0Prompts), and it is measured on a bench before
it is switched on.
"""
from __future__ import annotations

import json

import pytest

from korely_agent.services.memory_core import fact_extractor as e
from korely_agent.services.memory_core import extraction


class _Fake:
    def __init__(self, answer):
        self.answer = answer
        self.prompt = None

    def __call__(self, prompt, max_tokens):
        self.prompt = prompt
        return json.dumps(self.answer)


T1 = "Globex moved to the Starter plan."


def test_the_rule_does_not_come_from_the_acceptance_run():
    """What to recognise, with an invented shape of the output: never the
    sentence of the acceptance run it fixes (a rule tuned on its own test proves
    nothing), and no sentence on relation names, which pushed the unit out of
    a count with gpt-oss (3.1.1)."""
    rule = extraction.RULES_STATES
    assert "CHANGE OF STATE" in rule and "Shape of the output" in rule
    for word in ("Globex", "Starter", "moved", "moves_to"):
        assert word not in rule, word
    assert "RELATION NAMES" not in rule and "predicate never" not in rule


@pytest.mark.parametrize("call,answer,args", [
    (e.extract, {"facts": []}, (T1, "u1")),
    (e.extract_with_observation, {"observation": "", "facts": []}, (T1, "u1")),
    (e.extract_batch_with_observation, {"memories": []}, ([("m1", T1), ("m2", "Anna lives in Rome.")], "u1")),
], ids=["extract", "observation", "batch_observation"])
def test_off_it_is_not_there_on_it_is(call, answer, args):
    off, on = _Fake(answer), _Fake(answer)
    call(off, *args)
    call(on, *args, states=True)
    assert extraction.RULES_STATES not in off.prompt
    assert extraction.RULES_STATES in on.prompt
    # The rest of the prompt does not move: the rule is one paragraph more.
    assert on.prompt.replace("\n\n" + extraction.RULES_STATES, "", 1) == off.prompt


def test_on_it_comes_after_the_common_rules():
    """The rules come in a fixed order: the core rules, then the change of
    state (the turns of a conversation, which came after it, left with
    memory_core 4.0.0)."""
    rules = e._core_rules(states=True)
    assert rules == extraction.RULES_ALL + "\n\n" + extraction.RULES_STATES
    assert e._core_rules(states=False) == extraction.RULES_ALL


def test_the_fact_read_is_the_models():
    """The switch changes the question, never how the answer is read."""
    answer = {"facts": [{"subject": "Globex", "subject_type": "organization", "predicate": "plan",
                           "object": "Starter plan", "object_is_literal": False, "confidence": 0.9,
                           "tense": "current", "valid_from": "", "evidence": T1}]}
    [fact] = e.extract(_Fake(answer), T1, "u1", states=True)
    assert (fact.subject, fact.predicate_raw, fact.object, fact.tense) == ("Globex", "plan", "Starter plan",
                                                                          "current")


# ---------------------------------------------------------------------------
# The faithful observation (3.1.0, pieces 1 and 3), behind `faithful`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variants", [{}, {"times": True}], ids=["base", "times"])
def test_faithful_observation_on_every_variant(variants):
    """The three substitutions find their sentence in every variant of the
    rules (a marker that is gone raises, never silently)."""
    off_rules = e._observation_rules(variants.get("times", False))
    on_rules = e._observation_rules(variants.get("times", False), faithful=True)
    assert 'in the third person ("The user ...")' in off_rules
    assert 'in the third person ("The user ...")' not in on_rules
    assert "never gains a link to\n  the end user that it does not state" in on_rules
    assert "caption\n  or description shows" in on_rules
    assert "never a vaguer time in its place" in on_rules
    # 3.1.2: the referent sentence is gone (no gain on the bench).
    assert "that referent in the sentence" not in on_rules
    assert "understand who or what a pronoun means" in on_rules


def test_faithful_observation_in_the_prompts():
    for call, answer, args in (
        (e.extract_with_observation, {"observation": "", "facts": []}, (T1, "u1")),
        (e.extract_batch_with_observation, {"memories": []}, ([("m1", T1), ("m2", "Anna lives in Rome.")], "u1")),
    ):
        off, on = _Fake(answer), _Fake(answer)
        call(off, *args)
        call(on, *args, faithful=True)
        assert "never gains a link" not in off.prompt
        assert "never gains a link" in on.prompt
