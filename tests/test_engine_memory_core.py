"""memory_core: the pure rules of the facts engine.

Two things in here:
  1. the manifest: no file of the folder was modified without its manifest
     (the check runs in tests/test_memory_core_shared_rules.py);
  2. the pure rules, one per bug: BUG-110 (quality), BUG-112 (date from the
     text), BUG-109 (reads of the vocabulary's answers), BUG-111 (replacement
     by rule and same arguments), identity and time.
"""
# Notes (12 Oct 2026):
#   * test_the_folder_matches_the_manifest is already here as
#     tests/test_memory_core_shared_rules.py::test_the_folder_matches_the_manifest
#     (the same call, `differences_from_the_manifest() == []`): not duplicated.
#   * the four `extractor` tests the engine's first test file ended with
#     (test_extractor_a_broken_answer_is_not_no_fact,
#     test_extractor_grounds_facts_in_any_script,
#     test_extractor_a_batch_keeps_the_facts_of_its_memory,
#     test_extractor_the_prompt_is_for_the_model_only) are already here, byte
#     for byte but the imports, in tests/test_shared_extractor.py: not
#     duplicated.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.services.memory_core import contradiction, extraction, identity, quality, fact_time, vocabulary


def test_the_folder_imports_nothing_of_the_app():
    """It must run identically inside korely_agent: no `app.`."""
    import pathlib
    import re

    folder = pathlib.Path(identity.__file__).parent
    for f in folder.glob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+app[\s.]", text, re.M), f.name
        assert not re.search(r"^\s*(from|import)\s+korely_agent[\s.]", text, re.M), f.name


# ---------------------------------------------------------------------------
# BUG-110 — quality
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pred,s,o", [
    ("owns", "Gordon", "22"),
    ("owns", "Gordon", "9"),
    ("uses", "Gordon", "images/Gordon Robot.png"),
    ("uses", "Gordon", "Gordon_chat_session_id"),
    ("owns", "Gordon", "FREE_TIER_ID"),
    ("knowledge_graph", "Gordon", "true"),
    ("stores_in", "Korely", "src/app/services/x.py"),
    ("has", "Korely", "<value>"),
    ("uses", "Korely", "x" * 130),
    ("costs", "Korely Pro", "20"),     # without a unit: BONIE, the unit carries the meaning
])
def test_drops_the_technical_rows(pred, s, o):
    assert quality.drop_reason(pred, s, o) is not None


@pytest.mark.parametrize("pred,s,o", [
    ("costs", "Korely Pro", "20 EUR"),
    ("limit", "Gordon", "16000 characters"),
    ("born_in", "user", "1986"),
    ("uses", "Korely", "Node.js"),
    ("supports", "Korely", "TCP/IP"),
    ("available", "Support", "24/7"),
    ("hosted_at", "Korely", "https://app.korely.ai"),
    ("works_at", "Marco", "Iconsulting"),
    ("has", "Acme", "12 employees"),
])
def test_keeps_the_real_values(pred, s, o):
    assert quality.drop_reason(pred, s, o) is None


# ---------------------------------------------------------------------------
# BUG-112 and time
# ---------------------------------------------------------------------------

def test_date_from_text_only_if_the_year_is_written():
    text = "Gordon was renamed Korely in March 2026."
    assert fact_time.date_from_text("2026-03", text) == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert fact_time.date_from_text("2026-03-15", text) == datetime(2026, 3, 15, tzinfo=timezone.utc)
    assert fact_time.date_from_text("2025-03", text) is None      # invented year
    assert fact_time.date_from_text("", text) is None
    assert fact_time.date_from_text("March", text) is None
    assert fact_time.date_from_text("2026-13", text) is None      # impossible month
    assert fact_time.date_from_text("20260", "20260") is None


def test_a_future_date_is_a_plan_not_a_validity():
    today = datetime(2026, 9, 27, tzinfo=timezone.utc)
    assert fact_time.date_from_text("2027-01", "since 2027", now=today) is None
    assert fact_time.date_from_text("2026", "in 2026", now=today) == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_the_model_says_the_tense():
    """Graphiti §2.2.3: the model reads the tense from the context. The code
    does not correct it with a dictionary of verbs (the rule of 2026-09-27)."""
    assert fact_time.effective_tense("past") == "past"
    assert fact_time.effective_tense("planned") == "planned"
    assert fact_time.effective_tense(None) == "current"
    assert fact_time.effective_tense("unknown") == "current"


def test_time_weight_reinforcement_and_floor():
    now = datetime(2026, 9, 27, tzinfo=timezone.utc)
    fresh = fact_time.recency_weight(now - timedelta(days=1), 1, 0.02, now)
    old = fact_time.recency_weight(now - timedelta(days=120), 1, 0.02, now)
    seen = fact_time.recency_weight(now - timedelta(days=120), 10, 0.02, now)
    assert fresh > seen > old
    assert fact_time.recency_weight(now - timedelta(days=5000), 1, 0.02, now) == fact_time.FLOOR
    assert fact_time.recency_weight(now - timedelta(days=500), 1, 0.0, now) == 1.0


def test_read_tense_of_an_extracted_item():
    tense, since = extraction.read_tense(
        {"tense": "past", "valid_from": "2024"}, "Marco worked at Iconsulting since 2024",
    )
    assert tense == "past" and since == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_the_extractors_rules_list_no_trigger_words():
    """The rules describe what to recognise, never which words signal it: no
    quoted example sentences in the text of tense and identity."""
    assert '"' not in extraction.RULES_TENSE.split("\n", 1)[1].replace('"current"', "").replace('"past"', "").replace('"planned"', "").replace('"tense"', "")
    assert "PRESENT tense" in extraction.RULES_TENSE
    assert "renamed_to" in extraction.RULES_IDENTITY and "also_known_as" in extraction.RULES_IDENTITY


# ---------------------------------------------------------------------------
# BUG-109 — reads of the vocabulary's answers
# ---------------------------------------------------------------------------

def test_same_relation_rejects_the_boolean():
    assert vocabulary.read_same_relation('{"same_as": 2}', 3) == 1
    assert vocabulary.read_same_relation('{"same_as": true}', 3) is None
    assert vocabulary.read_same_relation('{"same_as": null}', 3) is None
    assert vocabulary.read_same_relation('{"same_as": 4}', 3) is None
    assert vocabulary.read_same_relation("```json\n{\"same_as\": 1}\n```", 3) == 0
    assert vocabulary.read_same_relation("I do not know", 3) is None


def test_single_value_and_family():
    assert vocabulary.read_single_value('{"many_values": false}') is True
    assert vocabulary.read_single_value('{"many_values": true}') is False
    assert vocabulary.read_single_value('{"many_values": "no"}') is None
    assert vocabulary.read_family('{"family": "Financial"}') == "financial"
    assert vocabulary.read_family('{"family": "identity"}') is None
    assert vocabulary.read_family('{"family": "misc"}') is None


@pytest.mark.parametrize("answer", [
    "", "   ", "I do not know", "{}", '{"same_as": 0}', '{"same_as": 4}', '{"same_as": true}',
    '{"same_as": "2"}', '["same_as", 1]', '{"same_as": 1',
])
def test_severe_same_relation_does_not_read_an_unusable_answer(answer):
    """(2026-09-28) Only null is "a new relation": everything else that names
    no option is unusable. The lenient reader keeps reading it as new."""
    with pytest.raises(vocabulary.UnreadableAnswer):
        vocabulary.read_same_relation(answer, 3, severe=True)
    assert vocabulary.read_same_relation(answer, 3) is None


def test_severe_same_relation_reads_the_real_answers():
    assert vocabulary.read_same_relation('{"same_as": null}', 3, severe=True) is None
    assert vocabulary.read_same_relation('{"same_as": 3}', 3, severe=True) == 2
    assert vocabulary.read_same_relation("```json\n{\"same_as\": null}\n```", 3, severe=True) is None


@pytest.mark.parametrize("answer", ["", "garbage", "{}", '{"many_values": "no"}', '{"many_values": null}'])
def test_severe_single_value(answer):
    with pytest.raises(vocabulary.UnreadableAnswer):
        vocabulary.read_single_value(answer, severe=True)
    assert vocabulary.read_single_value(answer) is None
    assert vocabulary.read_single_value('{"many_values": false}', severe=True) is True
    assert vocabulary.read_single_value('{"many_values": true}', severe=True) is False


@pytest.mark.parametrize("answer", ["", "{}", '{"family": "misc"}', '{"family": "identity"}', '{"family": 3}'])
def test_severe_family(answer):
    with pytest.raises(vocabulary.UnreadableAnswer):
        vocabulary.read_family(answer, severe=True)
    assert vocabulary.read_family(answer) is None
    assert vocabulary.read_family('{"family": "Other"}', severe=True) == "other"
    assert vocabulary.read_family('{"family": "financial"}', severe=True) == "financial"


@pytest.mark.parametrize("answer", [
    "", "{}", '{"past": true}', '{"past": true, "present": "  "}', '{"past": "yes", "present": "uses"}',
])
def test_severe_phrase_tense(answer):
    with pytest.raises(vocabulary.UnreadableAnswer):
        vocabulary.read_phrase_tense(answer, severe=True)
    assert vocabulary.read_phrase_tense(answer) is None
    assert vocabulary.read_phrase_tense('{"past": false}', severe=True) is None
    assert vocabulary.read_phrase_tense('{"past": true, "present": "uses"}', severe=True) == "uses"


def test_the_measured_questions_have_not_changed():
    """The text of the questions was chosen with measurements in the
    self-hosted: a word changed here has to be measured again there, not just
    changed."""
    d = vocabulary.same_relation_question("on_plan", "Business plan", ["subscribes_to"])
    assert "Prefer null when unsure" in d and "1. subscribes_to" in d
    v = vocabulary.single_value_question("on_plan", "Business plan")
    assert "many_values FALSE: subscription plan" in v


# ---------------------------------------------------------------------------
# BUG-111 — contradiction
# ---------------------------------------------------------------------------

def test_the_single_value_is_a_hint_for_the_judge_not_a_rule():
    """Lin 2010 (typed functionality) and Ritter 2008: no rule closes by itself.
    The judge reads the tag and the precision on the value's type."""
    assert not hasattr(contradiction, "replaced_by_rule")
    assert "OF THAT KIND" in contradiction.RULE_SINGLE_VALUE
    for prompt in (contradiction.AGENTS_JUDGE, contradiction.AGENTS_JUDGE_BATCH):
        assert contradiction.RULE_SINGLE_VALUE in prompt
    # 6.22.0: the tag is on the EXISTING facts only; on the NEW one it would
    # say that the new value is of the kind the vote was taken on, which is
    # what the judge decides (measured on 25 Nov 2026).
    assert contradiction.new_fact_for_the_judge("Acme", "has", "Business plan", functional=True) == "Acme has Business plan"
    assert contradiction.new_fact_for_the_judge(
        "Acme", "subscribes to", "Slack", functional=True, example="Business plan",
    ) == "Acme subscribes to Slack"
    # 6.22.0: with an example, the tag goes on the fact of the measured value only.
    row = contradiction.line_for_the_judge("f1", "Acme", None, "is on", "Business Plan", None, True, "Business plan")
    assert row == "- id=f1 | Acme is on Business Plan [single-valued for values like: Business plan]"
    row = contradiction.line_for_the_judge("f1", "Acme", None, "is on", "Pro plan", None, True, "Business plan")
    assert row == "- id=f1 | Acme is on Pro plan"
    row = contradiction.line_for_the_judge("f1", "Acme", None, "is on", "Pro plan", None, True, None)
    assert row == "- id=f1 | Acme is on Pro plan [single-valued]"
    assert contradiction.tag(False, "Business plan") == ""


def test_same_arguments():
    assert contradiction.same_arguments("Gordon", "adopt", "Temporal Decay", "gordon", "lacks", "temporal decay")
    assert not contradiction.same_arguments("Gordon", "lacks", "X", "Gordon", "lacks", "X")
    assert not contradiction.same_arguments("Gordon", "adopt", "X", "Korely", "lacks", "X")


def test_same_source_bug_123():
    """Graphiti §2.2.3: the facts of an episode have the same time, none closes
    another. The source counts as first source or as reconfirmation, whatever
    form the id comes in (UUID or string, upper case)."""
    import uuid

    note = uuid.uuid4()
    assert contradiction.same_source(note, [str(note)])
    assert contradiction.same_source(str(note).upper(), [str(uuid.uuid4()), str(note)])
    assert contradiction.same_source(note, (note,))
    assert not contradiction.same_source(note, [str(uuid.uuid4())])
    assert not contradiction.same_source(note, None)
    assert not contradiction.same_source(note, [None])
    # a write without a source (a triple sent by hand) excludes nothing
    assert not contradiction.same_source(None, [str(note)])
    assert not contradiction.same_source(None, [None])


def test_row_for_the_judge_shows_todays_name():
    r = contradiction.line_for_the_judge("id1", "Gordon", "Korely", "uses", "Groq", "2026-06-03")
    assert r == "- id=id1 | Korely (then: Gordon) uses Groq (since 2026-06-03)"


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

def test_new_alias_outcome():
    m = {"gordon": "Korely"}
    assert identity.new_alias_outcome(m, "Korely", "Gordon", source="user", existing_source=None, existing_canonical=None)[0] == "conflict"
    assert identity.new_alias_outcome(m, "Gordon Pro", "Gordon", source="user", existing_source=None, existing_canonical=None) == ("created", "Korely")
    assert identity.new_alias_outcome(m, "Gordon", "Other", source="rename_fact", existing_source="user", existing_canonical="Korely")[0] == "conflict"
    assert identity.new_alias_outcome(m, "2026-06-03", "Korely", source="user", existing_source=None, existing_canonical=None)[0] == "invalid"
    assert identity.new_alias_outcome(m, "x", "y", source="unknown", existing_source=None, existing_canonical=None)[0] == "invalid"


# ---------------------------------------------------------------------------
# Found by the MCP acceptance run (2026-09-27)
# ---------------------------------------------------------------------------

def test_the_predicate_form_does_not_decide_the_tense():
    """No dictionary of the past: the form stays as written. The tense comes
    from the `tense` field (extractor) or, for old facts, from the question by
    distinct form."""
    from korely_agent.services.memory_core.predicate_forms import canonicalize_predicate

    assert canonicalize_predicate("used") == "used"
    assert canonicalize_predicate("renamed to") == "renamed_to"
    assert vocabulary.read_phrase_tense('{"past": true, "present": "uses"}') == "uses"
    assert vocabulary.read_phrase_tense('{"past": false}') is None
    assert vocabulary.read_phrase_tense('{"past": true}') is None
    assert vocabulary.read_phrase_tense('{"past": "yes", "present": "uses"}') is None
    d = vocabulary.phrase_tense_question("used", "Groq")
    assert '"used"' in d and "Groq" in d


def test_identity_facts_keep_the_names():
    m = {"orion analytics": "Vega Labs"}
    assert identity.canonicals_of_the_triple("Vega Labs", "formerly", "Orion Analytics", m) == ("Vega Labs", "Orion Analytics")
    assert identity.canonicals_of_the_triple("Orion Analytics", "renamed_to", "Vega Labs", m) == ("Orion Analytics", "Vega Labs")
    assert identity.canonicals_of_the_triple("Orion Analytics", "uses", "Groq", m) == ("Vega Labs", "Groq")


def test_an_invented_date_does_not_become_a_fact():
    text = "In June Orion Analytics used Groq."
    assert quality.drop_reason("tested_on", "Orion Analytics", "2024-06", text) == "object_is_an_invented_date"
    assert quality.drop_reason("founded_in", "Orion Analytics", "2019", "Founded in 2019.") is None
    assert quality.drop_reason("tested_on", "Orion Analytics", "2024-06") is None   # without a text it is not judged


def test_compatible_objects():
    assert contradiction.compatible_objects("SSO", "SSO support")
    assert contradiction.compatible_objects("Acme Corp", "acme")
    assert not contradiction.compatible_objects("SSO", "SAML")
    assert contradiction.same_arguments("Vega", "uses", "SSO", "vega", "lacks", "SSO support")


def test_the_standing_instructions_are_gone():
    """5.0.0: the `instructions` family and the `instructs` predicate (2.8.0)
    left with the standing instructions, measured with no gain: a family the
    model cannot choose and the map no longer carries."""
    from korely_agent.services.memory_core import store, families
    from korely_agent.services.memory_core import fact_extractor as e

    assert "instructions" not in families.FAMILY_ORDER
    assert "instructions" not in families.FAMILY_LABELS_EN
    assert families.PREDICATE_TO_FAMILY.get("instructs") is None
    assert store.schema_family("instructs") == "other"
    assert vocabulary.read_family('{"family": "instructions"}') is None
    assert vocabulary._CHOOSABLE_FAMILIES == [f for f in families.FAMILY_ORDER if f != "identity"]
    assert not hasattr(extraction, "RULES_INSTRUCTIONS")
    with pytest.raises(TypeError):
        e.extract_with_observation(lambda p, m: "{}", "Always reply in Italian.", "u1", instructions=True)


def test_bug365_a_plan_and_the_thing_done_are_different_relations():
    """memory_core 2.8.0: "visited" ended up in "plans_to_visit" (TimeML: an
    I_STATE like "intend" does not assert the event, an OCCURRENCE does). The
    question says so; the text must be measured again in the self-hosted when
    the spend is approved."""
    d = vocabulary.same_relation_question("visited", "Yellowstone", ["plans_to_visit"])
    assert "A plan, a wish or a need to do something and doing it are different relations" in d
    assert "a lasting relation and a single event" in d
    assert "Prefer null when unsure" in d


def test_the_question_shows_the_relations_example():
    d = vocabulary.same_relation_question(
        "uses", "Salesforce", [("subscribes_to", "Business plan"), ("works_at", None), "lives_in"],
    )
    assert "1. subscribes_to (as in: ... Business plan)" in d
    assert "2. works_at\n" in d and "3. lives_in" in d
