"""The shared extractor (memory_core/fact_extractor.py), on the self-hosted product.

The hosted Agents product runs this same code (one codebase since 9 Oct 2026);
these were the pure tests of the engine's earlier home, which pinned the
rule in both suites until the engine moved here for good (11 Oct 2026): a
broken reply is not "no facts" (BUG-116), grounding works in any
script, a batched reply keeps each memory's facts, and the prompt carries no
developer commentary.
"""
import pytest


# ---------------------------------------------------------------------------
# fact_extractor: one extractor for the two Agents engines (strada 2, BUG-116)
# ---------------------------------------------------------------------------

def _answers(text):
    return lambda prompt, max_tokens: text


def test_extractor_a_broken_answer_is_not_no_fact():
    """BUG-116: a failed call, an empty reply and JSON cut off by the token
    ceiling are not "no facts"; only a readable empty list is."""
    from korely_agent.services.memory_core import fact_extractor

    def explodes(prompt, max_tokens):
        raise RuntimeError("quota exhausted")

    for call in (explodes, _answers(""), _answers('{"facts": [{"subject": "acme",')):
        with pytest.raises(fact_extractor.ExtractionFailed):
            fact_extractor.extract(call, "Acme uses Salesforce.", "k1")
    assert fact_extractor.extract(_answers('{"facts": []}'), "Nice weather today.", "k1") == []
    # A bare list is an answer too: the model dropped the wrapper.
    assert fact_extractor.extract(_answers("[]"), "Nice weather today.", "k1") == []
    bare = '[{"subject": "Acme", "subject_type": "organization", "predicate": "uses", "object": "Salesforce", "confidence": 0.9}]'
    assert [f.object for f in fact_extractor.extract(_answers(bare), "Acme uses Salesforce.", "k1")] == ["Salesforce"]
    with pytest.raises(fact_extractor.ExtractionFailed):
        fact_extractor.extract(_answers('"no facts here"'), "Nice weather today.", "k1")
    assert fact_extractor.MAX_TOKENS >= 4096


def test_extractor_grounds_facts_in_any_script():
    """An object not written verbatim is grounded through its words, in any
    script; a word the memory never says is still dropped."""
    import json
    from korely_agent.services.memory_core import fact_extractor

    facts = [
        {"subject": "Мария", "subject_type": "person", "predicate": "works_at",
         "object": "компания Ромашка", "object_is_literal": False, "confidence": 0.9},
        {"subject": "Мария", "subject_type": "person", "predicate": "lives_in",
         "object": "Москва", "object_is_literal": False, "confidence": 0.9},
    ]
    got = fact_extractor.extract(_answers(json.dumps({"facts": facts})),
                            "Ромашка наняла Мария в марте.", "k1")
    assert [f.object for f in got] == ["компания Ромашка"]


def test_extractor_a_batch_keeps_the_facts_of_its_memory():
    import json
    from korely_agent.services.memory_core import fact_extractor

    answer = {"memories": [
        {"mem_id": "a", "facts": [{"subject": "Acme", "subject_type": "organization", "predicate": "uses",
                                   "object": "Salesforce", "object_is_literal": False, "confidence": 0.9}]},
        # attributed to the wrong memory: "b" never names Salesforce
        {"mem_id": "b", "facts": [{"subject": "Acme", "subject_type": "organization", "predicate": "uses",
                                   "object": "Salesforce", "object_is_literal": False, "confidence": 0.9}]},
        {"mem_id": "invented", "facts": []},
    ]}
    # memory_core 4.0.0: the batched path is the one with the observation
    # (`estrai_lotto` is gone); a memory nobody answered for is absent.
    got = fact_extractor.extract_batch_with_observation(
        _answers(json.dumps(answer)),
        [("a", "Acme uses Salesforce."), ("b", "Acme hired two people."), ("c", "Acme is in Rome.")], "k1")
    assert set(got) == {"a", "b"}
    assert [f.object for f in got["a"].facts] == ["Salesforce"] and got["b"].facts == []
    with pytest.raises(fact_extractor.ExtractionFailed):
        fact_extractor.extract_batch_with_observation(_answers('{"facts": []}'), [("a", "x y"), ("b", "z w")], "k1")


def test_extractor_the_prompt_is_for_the_model_only():
    """The prompt carries the subject rule in both forms and no commentary
    for developers (the self-hosted single prompt used to send thirty lines of
    Italian notes to the model on every write)."""
    from korely_agent.services.memory_core import fact_extractor

    seen = []
    fact_extractor.extract(lambda p, m: seen.append(p) or '{"facts": []}', "I like tea.", "k1")
    fact_extractor.extract_batch_with_observation(lambda p, m: seen.append(p) or '{"memories": []}',
                                             [("a", "I like tea."), ("b", "I like coffee.")], "k1")
    for p in seen:
        assert "the holder of account 12" in " ".join(p.split()) and '"k1"' in p
        assert "Perche'" not in p and "\n# " not in p


def _fact(subject, obj, *, predicate="has_plan", tense="current"):
    return {"subject": subject, "subject_type": "person", "predicate": predicate, "object": obj,
            "object_is_literal": False, "tense": tense, "confidence": 0.9}


def test_a_borrowed_word_does_not_ground_a_fact():
    """5.2.0, the VS Code demo of 15 Oct 2026: reading "moved to the Basic
    plan" the writer also took the old plan from the earlier memories, and
    "plan", a word of "Basic plan", kept it. The old plan goes; the new stays."""
    import json
    from korely_agent.services.memory_core import fact_extractor

    facts = [_fact("Customer 4821", "Basic plan"),
             _fact("Customer 4821", "Priority Support plan", tense="past")]
    got = fact_extractor.extract(_answers(json.dumps({"facts": facts})),
                            "Customer 4821 moved to the Basic plan.", "c4821")
    assert [f.object for f in got] == ["Basic plan"]


def test_a_words_own_stays_beside_other_arguments():
    """"Globex Inc" grounded by "Globex", a word no other argument has; and
    "компания Ромашка" next to "Ромашка", which names nothing it does not."""
    import json
    from korely_agent.services.memory_core import fact_extractor

    facts = [_fact("Anna", "Globex Inc", predicate="works_at"), _fact("Anna", "Acme", predicate="left")]
    got = fact_extractor.extract(_answers(json.dumps({"facts": facts})), "Anna left Acme for Globex.", "k1")
    assert sorted(f.object for f in got) == ["Acme", "Globex Inc"]
    facts = [_fact("Мария", "компания Ромашка", predicate="works_at"),
             _fact("Ромашка", "Мария", predicate="hired")]
    got = fact_extractor.extract(_answers(json.dumps({"facts": facts})), "Ромашка наняла Мария в марте.", "k1")
    assert [f.object for f in got] == ["компания Ромашка", "Мария"]
