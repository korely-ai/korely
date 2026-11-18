"""memory_core 2.5.0: the judge reads the words a fact was read from.

BUG-200 (acceptance run of 29 Sep): "Luca works at Apple." and then "Luca also works
part-time at Google as a consultant." closed `Luca works_at Apple`. The judge
read two triples, `works_at Apple` and `works_at Google`, with the
[single-valued] tag, and the words that said the two jobs hold together were
lost in the extraction. Ritter et al. (EMNLP 2008): seeming contradictions on
functional relations are told apart from genuine ones by the context.

So every extractor asks for `evidence`, the words of the text that state the
fact; they are kept only when they occur in the text (a paraphrase is the
model's reading), shown to the judge under the new fact ("said as"), and never
stored on the fact (a forgotten memory leaves no copy of its text).

These tests use no model: what the model then decides is measured by the
acceptance run of the memory (memory_core 2.5.0).
"""
# Notes:
#   * test_all_three_extractors_ask_for_it: the engine's two extractors are
#     checked; the third belonged to another product's memory.
#   * test_agents_the_words_reach_the_judge_and_are_not_saved runs through
#     korely-agent's write (`facts.service._persist_extracted`), with the judge's
#     model as this product's provider (`core.llm.get_provider`) and the batched
#     judge switched by `service._BATCHED_JUDGE`; the vocabulary asks no model
#     (`predicate_vocabulary_model=False`).
#   * The two tests of another product's memory (its notes and its pipeline)
#     are not here: korely-agent does not have that memory.
from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace

import pytest

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import predicates
from korely_agent.services.facts import service as agents_svc
from korely_agent.services.memory_core import contradiction, fact_extractor, extraction, judge, worker
from korely_agent.services.memory_core.fact_extractor import ExtractedFact

TEXT = "Luca works at Apple in Cupertino. Luca also works part-time at Google as a consultant."
WORDS = "Luca also works part-time at Google as a consultant."


# ---------------------------------------------------------------------------
# Reading the words
# ---------------------------------------------------------------------------

class TestReadEvidence:
    def test_copied_words_are_kept(self):
        assert extraction.read_evidence({"evidence": WORDS}, TEXT) == WORDS

    def test_case_punctuation_and_spaces_do_not_count(self):
        copy = "luca ALSO works   part-time at google, as a consultant"
        assert extraction.read_evidence({"evidence": copy}, TEXT) == " ".join(copy.split())

    def test_a_paraphrase_is_not_what_was_said(self):
        # The model's reading, not the text: the judge would decide on it.
        assert extraction.read_evidence({"evidence": "Luca works at Google."}, TEXT) is None
        assert extraction.read_evidence({"evidence": "Luca is a Google consultant"}, TEXT) is None

    @pytest.mark.parametrize("value", [None, "", "   ", "...", 42, ["Luca"], {"a": 1}])
    def test_nothing_usable_is_none(self, value):
        assert extraction.read_evidence({"evidence": value}, TEXT) is None

    def test_without_the_field_is_none(self):
        assert extraction.read_evidence({"subject": "Luca"}, TEXT) is None
        assert extraction.read_evidence("not a dict", TEXT) is None

    def test_every_alphabet(self):
        ru = "Мария работает в Ромашке. Мария также консультирует Газпром по выходным."
        assert extraction.read_evidence({"evidence": "Мария также консультирует Газпром"}, ru)
        # Text with no spaces between words: a span inside a sentence still counts.
        zh = "李华在苹果公司工作。他周末也在谷歌做顾问。"
        assert extraction.read_evidence({"evidence": "他周末也在谷歌做顾问"}, zh) == "他周末也在谷歌做顾问"
        assert extraction.read_evidence({"evidence": "他在微软做顾问"}, zh) is None

    def test_too_long_is_cut_at_a_word(self):
        text = " ".join(f"word{i}" for i in range(200))
        sentence = extraction.read_evidence({"evidence": text}, text)
        assert sentence.endswith(" ...")
        assert len(sentence) <= extraction.MAX_EVIDENCE + 4
        assert text.startswith(sentence[:-4])

    def test_all_three_extractors_ask_for_it(self):
        assert '"evidence"' in extraction.JSON_FIELDS
        assert "EVIDENCE (mandatory)" in extraction.RULES_ALL
        prompt = fact_extractor.PROMPT_SINGLE.replace("{TENSE_FIELDS}", fact_extractor._TENSE_FIELDS)
        assert '"evidence"' in prompt


class TestExtractor:
    def _answer(self, *facts):
        return json.dumps({"facts": list(facts)})

    def _fact(self, **k):
        base = {"subject": "Luca", "subject_type": "person", "predicate": "works_at",
                "object": "Google", "object_is_literal": False, "tense": "current",
                "valid_from": "", "confidence": 0.9}
        base.update(k)
        return base

    def test_the_fact_carries_its_words(self):
        call = lambda prompt, n: self._answer(self._fact(evidence=WORDS))
        [f] = fact_extractor.extract(call, TEXT, "luca")
        assert f.evidence == WORDS

    def test_invented_words_stay_out_but_the_fact_stays(self):
        call = lambda prompt, n: self._answer(self._fact(evidence="Luca switched jobs"))
        [f] = fact_extractor.extract(call, TEXT, "luca")
        assert f.evidence is None and f.object == "Google"

    def test_in_the_batch_the_words_are_those_of_its_memory(self):
        other = "Marta lives in Turin."

        def call(prompt, n):
            ids = re.findall(r"mem_id=([^\s\]>\"]+)", prompt)
            assert len(ids) >= 2, prompt
            return json.dumps({"memories": [
                # Words of the OTHER memory attached to this one: not its words.
                {"mem_id": "m1", "facts": [self._fact(evidence="Marta lives in Turin.")]},
                {"mem_id": "m2", "facts": [self._fact(subject="Marta", predicate="lives_in",
                                                       object="Turin", evidence="Marta lives in Turin.")]},
            ]})

        out = fact_extractor.extract_batch_with_observation(call, [("m1", TEXT), ("m2", other)], "u")
        assert out["m1"].facts[0].evidence is None
        assert out["m2"].facts[0].evidence == "Marta lives in Turin."


# ---------------------------------------------------------------------------
# What the judge reads
# ---------------------------------------------------------------------------

class TestWhatTheJudgeReads:
    def test_without_words_the_row_is_the_one_before(self):
        row = contradiction.new_fact_for_the_judge("Luca", "works_at", "Google", functional=True, example="Apple")
        assert row == "Luca works_at Google [single-valued for values like: Apple]"

    def test_with_the_words_one_more_row(self):
        row = contradiction.new_fact_for_the_judge(
            "Luca", "works_at", "Google", functional=True, example="Apple", evidence=WORDS,
        )
        assert row.splitlines() == [
            "Luca works_at Google [single-valued for values like: Apple]",
            f'  said as: "{WORDS}"',
        ]

    def test_quotes_do_not_close_the_quotation(self):
        row = contradiction.new_fact_for_the_judge("Luca", "says", "x", evidence='he said "hi"\n  twice')
        assert row.endswith('said as: "he said \'hi\' twice"')

    @pytest.mark.parametrize("prompt", [
        contradiction.AGENTS_JUDGE, contradiction.AGENTS_JUDGE_BATCH,
    ], ids=["agents", "batch"])
    def test_the_judges_have_the_words_rule(self, prompt):
        # memory_core 4.0.0: two judges, the other product's one left the code.
        assert contradiction.RULE_WORDS in prompt
        assert "{RULE_WORDS}" not in prompt and "{RULE_SINGLE_VALUE}" not in prompt
        # The rule comes after the tag's, which it overrides.
        rule = contradiction.RULE_SINGLE_VALUE
        assert prompt.index(rule) < prompt.index(contradiction.RULE_WORDS)

    def test_judge_puts_the_words_under_the_new_fact(self):
        seen = []

        def call(prompt, n):
            seen.append(prompt)
            return '{"contradictions": []}'

        c = SimpleNamespace(id=uuid.uuid4(), subject_label="Luca", subject_canonical="Luca",
                            predicate_raw="works_at", object_label="Apple", valid_from=None)
        judge.judge(call, [c], "Luca", "works_at", "Google",
                        single_value=lambda _c: (True, "Apple"), functional=True, example="Apple",
                        evidence=WORDS)
        new_part = seen[0].split("NEW fact:")[1]
        assert f'said as: "{WORDS}"' in new_part

    def test_batch_passes_the_words_of_every_fact(self):
        seen = []

        def classify(groups):
            seen.extend(groups)
            return {}

        old = SimpleNamespace(id=uuid.uuid4())
        new_facts = [
            {"id": uuid.uuid4(), "subject": "Luca", "predicate_raw": "works_at", "object": "Google",
             "predicate_canonical": "works_at", "valid_from": None, "evidence": WORDS},
            {"id": uuid.uuid4(), "subject": "Luca", "predicate_raw": "lives_in", "object": "Rome",
             "predicate_canonical": "lives_in", "valid_from": None},
        ]
        judge.batch(None, None, new_facts, gather=lambda nf: [old],
                      single_value_of_new=lambda nf: (False, None), classify=classify)
        assert f'said as: "{WORDS}"' in seen[0]["new_text"]
        assert "said as" not in seen[1]["new_text"]

    def test_the_worker_brings_the_words_to_the_batch(self):
        mid = uuid.uuid4()

        def row(obj):
            return SimpleNamespace(id=uuid.uuid4(), source_memory_id=mid, tense="current", invalid_at=None,
                                   observation_count=1, end_user_id="u", subject_label="Luca",
                                   subject_canonical="Luca", predicate_raw="works_at",
                                   predicate_canonical="works_at", object_label=obj,
                                   object_canonical=obj, embedding=None, valid_from=None)

        written = [(row("Google"), []), (row("IBM"), [])]
        extracted = [ExtractedFact("Luca", "works_at", "Google", 0.9, evidence=WORDS),
                    ExtractedFact("Luca", "works_at", "IBM", 0.9)]
        new_facts = worker._new_for_the_batch(written, mid, extracted)
        assert [n["evidence"] for n in new_facts] == [WORDS, None]
        # A caller written before 2.5.0 passes no extracted facts: no words.
        assert [n["evidence"] for n in worker._new_for_the_batch(written, mid)] == [None, None]


# ---------------------------------------------------------------------------
# The engine's two judges, end to end with a fake model
# ---------------------------------------------------------------------------

class _JudgeModel:
    """The judge's model: records every prompt, closes nothing."""

    def __init__(self):
        self.prompt: list[str] = []

    def __call__(self, prompt, max_tokens):
        self.prompt.append(prompt)
        if '"results"' in prompt:
            ids = re.findall(r"\[new_id=([^\]]+)\]", prompt)
            return json.dumps({"results": [{"new_id": i, "contradictions": []} for i in ids]})
        return '{"contradictions": []}'


class _Provider:
    """The judge's model behind this product's provider (`core.llm`)."""

    def __init__(self, model):
        self.model = model

    def complete(self, messages, **kw):
        return LLMResult(text=self.model(messages[0]["content"], kw.get("max_tokens")))


@pytest.mark.parametrize("batch", [False, True], ids=["per_fact", "batched"])
def test_agents_the_words_reach_the_judge_and_are_not_saved(db, tenant, monkeypatch, batch):
    model = _JudgeModel()
    provider = _Provider(model)
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: provider)
    monkeypatch.setattr(predicates, "_ask", lambda: None)
    monkeypatch.setattr(agents_svc, "_BATCHED_JUDGE", batch)

    def write(text, fact):
        m = AgentMemory(tenant_id=tenant.id, end_user_id="luca", content=text)
        db.add(m)
        db.commit()
        return agents_svc._persist_extracted(db, tenant.id, memory=m, extracted=[fact])

    write("Luca works at Apple.", ExtractedFact("Luca", "works_at", "Apple", 0.9, subject_type="person",
                                                  evidence="Luca works at Apple."))
    write(WORDS, ExtractedFact("Luca", "works_at", "Google", 0.9, subject_type="person", evidence=WORDS))

    assert len(model.prompt) == 1, "the second write must reach the judge"
    assert f'said as: "{WORDS}"' in model.prompt[0]
    assert {f.object_label for f in db.query(AgentFact).filter(AgentFact.invalid_at.is_(None))} == {"Apple", "Google"}
    # Not stored: no column of the fact holds the words of the memory.
    for f in db.query(AgentFact).all():
        for col in AgentFact.__table__.columns:
            v = getattr(f, col.key)
            assert not (isinstance(v, str) and "part-time" in v), col.key
