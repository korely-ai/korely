"""memory_core 3.2.0: the attributes rule, off by default (7 Oct 2026).

"I am vegetarian." never became a fact: the model answered (user, is,
vegetarian) and the guard on predicates under three letters dropped it. The
rule (extraction.RULES_ATTRIBUTES) asks for the attribute named in the
relation (has_diet vegetarian). Off, the prompts are the 3.1 ones byte for
byte (the prompt pins of the other memory_core tests stay green); on, the
rule is in every extraction prompt, single, batch and with the observation.
"""
# Port notes: the test that the rule is on in the cloud only where Gemini
# writes is not ported. It checked another hosted adapter, which korely-agent
# does not have; the same
# rule on korely-agent's adapter (on only where the writer in force is Gemini)
# is tests/test_observation_rules.py::test_the_3_2_0_attributes_rule_follows_the_writer.
from korely_agent.services.memory_core import fact_extractor
from korely_agent.services.memory_core.extraction import RULES_ATTRIBUTES


def _try_it(f, *a, **k):
    """The prompt is what is checked: an answer the fake cannot shape (a
    batch wants its `memories`) is beside the point."""
    try:
        f(*a, **k)
    except fact_extractor.ExtractionFailed:
        pass


def _prompt(call_result, **kw):
    seen = []

    def call(prompt, *a, **k):
        seen.append(prompt)
        return call_result

    return seen, call


def test_off_it_is_not_there():
    assert "ATTRIBUTES:" not in fact_extractor._core_rules(False)
    assert fact_extractor._core_rules(False) == fact_extractor._core_rules(False, False)


def test_on_it_is_in_every_prompt():
    assert RULES_ATTRIBUTES in fact_extractor._core_rules(False, True)
    seen, call = _prompt('{"facts": []}')
    _try_it(fact_extractor.extract, call, "I am vegetarian.", "anna", attributes=True)
    _try_it(fact_extractor.extract_with_observation, call, "I am vegetarian.", "anna", attributes=True)
    _try_it(fact_extractor.extract_batch_with_observation, call,
           [("m1", "I am vegetarian."), ("m2", "I live in Turin.")], "anna", attributes=True)
    assert len(seen) == 3 and all("ATTRIBUTES:" in p for p in seen)


def test_off_no_prompt_carries_it():
    seen, call = _prompt('{"facts": []}')
    _try_it(fact_extractor.extract, call, "I am vegetarian.", "anna")
    _try_it(fact_extractor.extract_batch_with_observation, call,
           [("m1", "I am vegetarian."), ("m2", "I live in Turin.")], "anna")
    assert len(seen) == 2 and not any("ATTRIBUTES:" in p for p in seen)
