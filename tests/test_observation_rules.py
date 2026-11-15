"""The observation rules of memory_core 2.8.0, switched from korely-agent.

memory_core 2.8.0 added three rules to the prompt that writes the facts and
the observation: events mentioned in passing, relative times written as dates
counted from the memory's day, standing instructions. They are off by default
(the prompt of 2.7 byte for byte) and korely-agent turns them on with its own
flags, once measured. The first two were measured worse or useless and left
the engine (memory_core 3.3.0 and 4.0.0): the standing instructions remain.
"""
from korely_agent.services.facts import extractor
from korely_agent.services.memory_core import fact_extractor


def _captured(monkeypatch):
    seen = {}

    def fake(call, content, end_user_id, parse, **kw):
        seen.update(kw)
        return fact_extractor.Extraction([], None)

    monkeypatch.setattr(fact_extractor, "extract_with_observation", fake)
    monkeypatch.setattr(extractor, "_call_or_raise", lambda model_name: None)
    return seen


def test_by_default_the_relative_times_and_nothing_else(monkeypatch):
    """The relative times dated by code are on since 0.1.18, measured twice on
    LoCoMo (extractor.py says how); every other rule is off, the prompt of
    before."""
    seen = _captured(monkeypatch)
    extractor.extract_facts_and_observation("user: we went camping last weekend.",
                                            end_user_id="melanie", db=None, tenant_id=None,
                                            date="2023-07-17")
    assert "istruzioni" not in seen and "eventi" not in seen
    assert "date_assolute" not in seen and "turni" not in seen, "the engine's own defaults: off"
    assert seen["times"] is True
    assert seen["memory_date"] == "2023-07-17"


def test_off_the_prompt_of_before(monkeypatch):
    seen = _captured(monkeypatch)
    monkeypatch.setattr(extractor, "_OBSERVATION_RELATIVE_TIMES", False)
    extractor.extract_facts_and_observation("user: we went camping last weekend.",
                                            end_user_id="melanie", db=None, tenant_id=None)
    assert seen["times"] is False and "turni" not in seen


def test_the_standing_instructions_are_gone():
    """22 of 28 probes against 21 on the procedural bench (12 Oct 2026), a
    tie: removed, here and in memory_core 5.0.0."""
    assert not hasattr(extractor, "_OBSERVATION_INSTRUCTIONS")


def test_the_events_rule_is_gone():
    """Measured worse with both writers in the 3.3.0 pack (7-8 Oct 2026):
    removed from memory_core, no switch left."""
    assert not hasattr(extractor, "_OBSERVATION_EVENTS")
    assert not hasattr(fact_extractor, "REGOLE_EVENTI")


def test_the_dates_written_by_the_model_are_gone(monkeypatch):
    """Measured at 1 converted of 40, replaced by the dates computed by
    code, and a write-breaking pair with them (the audit of 2026-10-06, row
    13): never asked for any more."""
    seen = _captured(monkeypatch)
    extractor.extract_facts_and_observation("user: we went camping last weekend.",
                                            end_user_id="melanie", db=None, tenant_id=None)
    assert not seen.get("date_assolute")
    assert not hasattr(extractor, "_OBSERVATION_ABSOLUTE_DATES")
    # memory_core 4.0.0: gone from the engine too.
    assert not hasattr(fact_extractor, "FRASE_DATE_ASSOLUTE")


def test_off_the_rules_are_the_ones_of_before():
    """memory_core's promise, checked on the copy this product ships: all
    off, the rules of 2.6/2.7 and nothing else; each on, its own text."""
    assert fact_extractor._observation_rules(False, False) == fact_extractor.RULES_OBSERVATION
    assert fact_extractor._observation_rules(True, False).startswith(
        fact_extractor.RULES_OBSERVATION_TIMES)


def test_the_relative_times_reach_the_engine(monkeypatch):
    """memory_core 2.9.0: the relative times dated by code. The turns rule
    of the same version was measured without gain and is not passed (gone
    from the engine with memory_core 4.0.0)."""
    seen = _captured(monkeypatch)
    monkeypatch.setattr(extractor, "_OBSERVATION_RELATIVE_TIMES", True)
    extractor.extract_facts_and_observation("user: we went camping last weekend.",
                                            end_user_id="melanie", db=None, tenant_id=None)
    assert seen["times"] is True and "turni" not in seen


def test_the_3_1_rules_are_on_by_default_for_gpt_oss_and_reach_the_engine(monkeypatch):
    """memory_core 3.1: the states and the faithful observation. On by default
    from 0.1.20 (after the whole LoCoMo run of 7 Oct 2026) with the
    writer they were measured on, gpt-oss; off with no writer and with any
    other; each switch, off, turns its rule off on both calls."""
    seen = _captured(monkeypatch)
    monkeypatch.setattr(extractor, "_select_provider", lambda m: (_Writer("mistral"), "gpt-oss-120b"))
    extractor.extract_facts_and_observation("user: Globex moved to the Starter plan.",
                                            end_user_id="u1", db=None, tenant_id=None)
    assert (seen["states"], seen["faithful"]) == (True, True)

    monkeypatch.setattr(extractor, "_STATES_RULE", False)
    monkeypatch.setattr(extractor, "_FAITHFUL_OBSERVATION", False)
    extractor.extract_facts_and_observation("user: Globex moved to the Starter plan.",
                                            end_user_id="u1", db=None, tenant_id=None)
    assert (seen["states"], seen["faithful"]) == (False, False)
    monkeypatch.setattr(extractor, "_STATES_RULE", True)
    monkeypatch.setattr(extractor, "_FAITHFUL_OBSERVATION", True)
    monkeypatch.setattr(extractor, "_select_provider", lambda m: (None, None))
    extractor.extract_facts_and_observation("user: Globex moved to the Starter plan.",
                                            end_user_id="u1", db=None, tenant_id=None)
    assert (seen["states"], seen["faithful"]) == (False, False), "no writer: the prompts of before"


def test_off_the_3_1_0_prompts_are_the_ones_of_3_0_0():
    """memory_core's promise, on the copy this product ships."""
    assert fact_extractor._core_rules(states=False) == fact_extractor._core_rules()


class _Writer:
    def __init__(self, name):
        self.name = name


def test_the_3_2_0_attributes_rule_follows_the_writer(monkeypatch):
    """memory_core 3.2.0: an attribute said with the copula named in the
    predicate. On since 0.1.28 (12 Oct 2026), and it
    reaches the engine only when the writer in force is Gemini, measured
    better with it (13 to 33 of 37; LoCoMo conversation 26 level, 134 against
    135), never with gpt-oss, measured worse (25 to 22), nor any other writer,
    unmeasured. The switch off turns it off for Gemini too."""
    seen = _captured(monkeypatch)
    monkeypatch.setattr(extractor, "_select_provider", lambda model_name: (_Writer("gemini"), "m"))
    extractor.extract_facts_and_observation("user: I am vegetarian.", end_user_id="u1",
                                            db=None, tenant_id=None)
    assert seen["attributes"] is True, "on by default with Gemini"
    monkeypatch.setattr(extractor, "_ATTRIBUTES_RULE", False)
    extractor.extract_facts_and_observation("user: I am vegetarian.", end_user_id="u1",
                                            db=None, tenant_id=None)
    assert seen["attributes"] is False
    monkeypatch.setattr(extractor, "_ATTRIBUTES_RULE", True)
    monkeypatch.setattr(extractor, "_select_provider", lambda model_name: (_Writer("mistral"), "m"))
    extractor.extract_facts_and_observation("user: I am vegetarian.", end_user_id="u1",
                                            db=None, tenant_id=None)
    assert seen["attributes"] is False, "gpt-oss and every OpenAI-compatible writer: off"


def test_off_the_3_2_0_prompts_are_the_ones_of_3_1():
    """memory_core's promise, on the copy this product ships."""
    assert fact_extractor._core_rules(states=False, attributes=False) == fact_extractor._core_rules()
    assert fact_extractor._core_rules(attributes=True) != fact_extractor._core_rules()


class _Forced:
    name = "mistral"

    def __init__(self, forced):
        self.forced_model = forced


def test_the_3_1_rules_apply_to_the_writer_they_were_measured_on(monkeypatch):
    """states and faithful, measured with gpt-oss-120b (LoCoMo whole +15, 2026-10-07)
    and never with another writer: switched on, they reach the engine only when
    the writer in force is gpt-oss, chosen in the file or on the Models page."""
    seen = _captured(monkeypatch)
    monkeypatch.setattr(extractor, "_STATES_RULE", True)
    monkeypatch.setattr(extractor, "_FAITHFUL_OBSERVATION", True)
    for writer, model, expected in [
        (_Forced(None), "gpt-oss-120b", True),           # the file: MISTRAL_EXTRACTION_MODEL
        (_Forced("gpt-oss-120b"), "mistral-small", True),  # the Models page wins
        (_Forced("llama-3.3-70b"), "gpt-oss-120b", False),
        (_Writer("gemini"), "gemini-3.1-flash-lite", False),
    ]:
        monkeypatch.setattr(extractor, "_select_provider", lambda m, w=writer, mm=model: (w, mm))
        extractor.extract_facts_and_observation("user: Globex moved to the Starter plan.",
                                                end_user_id="u1", db=None, tenant_id=None)
        assert (seen["states"], seen["faithful"]) == (expected, expected), (writer, model)


def test_one_rule_names_the_measured_writer(monkeypatch):
    """A code review found that the two writers the prompt rules were
    measured on are named by one function, with the answers each rule gave
    before when it decided alone."""
    for writer, model, expected in [
        (_Writer("gemini"), "gemini-3.1-flash-lite", "gemini"),
        (_Forced(None), "gpt-oss-120b", "gpt-oss"),               # Scaleway, OVH
        (_Forced(None), "openai/gpt-oss-120b:nitro", "gpt-oss"),  # OpenRouter
        (_Forced("gpt-oss-120b"), "mistral-small", "gpt-oss"),    # the Models page wins
        (_Forced("llama-3.3-70b"), "gpt-oss-120b", None),
        (_Forced(None), "google/gemini-2.5-flash", None),         # Gemini behind OpenRouter
        (None, None, None),                                        # no writer
    ]:
        monkeypatch.setattr(extractor, "_select_provider", lambda m, w=writer, mm=model: (w, mm))
        assert extractor._measured_writer(None) == expected, (writer, model)
        assert extractor._rules_3_1(None) is (expected == "gpt-oss")
        assert extractor._attributes(None) is (expected == "gemini")
