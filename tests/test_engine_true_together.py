"""memory_core 3.2.0 (6 Oct evening): close a fact only when the new one makes it false.

A reading of all the closures of LoCoMo conv-26 found 0 true changes in 20 with
gpt-oss-120b (writer and judge), 1 in 27 with Gemini 3.1 Flash-Lite. With
gpt-oss half came from single-valued `feels` (every feeling closed the
previous one); with Gemini the judge closed compatible facts (a loved place
with a visit, one preference with another, one painting with another, an
identity with one of its faces), and 15 closures out of 27 came from a new
fact in the past, shown to the judge as "(no longer true)".

Four pieces, measured on the judge's words (modes 3.1.3 and 3.2.0) and on the
single-valued relations (replacements and occurrences):
  * the vocabulary: a feeling or a reaction of one moment is many-valued
    (one example among the many-valued ones, TimeML);
  * the vocabulary: two relations are the same only if their subjects and
    their values are of the same kind (Lin, Mausam, Etzioni, EMNLP 2010 §3);
  * the judge (the two of the Agents): RULE_TRUE_TOGETHER, a contradiction is
    two facts that cannot be true together (de Marneffe et al., ACL
    2008 §3.1);
  * the judge of one fact: RULE_NO_LONGER_TRUE, "(no longer true)" says that
    the new fact is over, and a finished event closes nothing.

The fingerprints that move: none in the tests. `TestThe2_8_0Prompts`
fixes the extraction prompts, which 3.2.0 does not touch (they stay green); the
sha256 fingerprints of the judges and of the two questions of 3.1.3 are in the
measurement (`FINGERPRINTS_3_1_3`), and here we check that 3.2.0 is 3.1.3 PLUS the new
rules, nothing else: take the rules away, and the texts of before are the same byte for byte.

No model is called.
"""
# Notes: the pure tests of the engine, as they were first written (imports
# only). The 11 tests of the measurement scripts stay with those scripts,
# which this repository does not have.
#   * test_the_same_relation_wants_subjects_and_values_of_the_same_kind needs
#     no measurement fixture.
#   * test_the_version pins VERSION >= 3.3.0 and equal to MANIFEST.json's.
from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest

from korely_agent.services import memory_core
from korely_agent.services.memory_core import contradiction as cc
from korely_agent.services.memory_core import judge
from korely_agent.services.memory_core import vocabulary as voc


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


AGENTS = {"agents": cc.AGENTS_JUDGE, "batch": cc.AGENTS_JUDGE_BATCH}


def test_the_version():
    # Written as 3.2.0 on its branch (6 Oct night), merged on 7 Oct above the
    # 3.2.0 of the attributes: the judge of piece 5 is 3.3.0. In korely-agent
    # the version moves with every change of the engine (3.3.1 on 11 Oct), so
    # what is pinned is that it is at least 3.3.0 and that MANIFEST.json says
    # the same one (scripts/memory_core_manifest.py writes both together).
    import json
    from pathlib import Path

    assert tuple(int(x) for x in memory_core.VERSION.split(".")) >= (3, 3, 0)
    manifest = Path(memory_core.__file__).with_name("MANIFEST.json")
    assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == memory_core.VERSION


class TestTheVocabulary:
    def test_a_feeling_of_one_moment_is_many_valued(self):
        """The example is among the many-valued ones, in the two forms of the
        question (with and without a value), and the question stays the one that
        asks for the usual case for the kind of value."""
        for d in (voc.single_value_question("feels", "happy"), voc.single_value_question("feels")):
            many = d[d.index("many_values TRUE:"):d.index("many_values FALSE:")]
            assert "feels (a feeling or" in many
            assert "happened then, it is not a" in many and "lasting state: a later one does not end it" in many
        d = voc.single_value_question("feels", "happy")
        assert "Judge the kind of value" in d and "USUALLY REPLACE" in d

    def test_the_same_relation_wants_subjects_and_values_of_the_same_kind(self):
        d = voc.same_relation_question("subscribes_to", "Slack", [("on_plan", "Business plan")])
        assert voc.ARGUMENT_TYPES in d
        assert "their subjects are of the same kind and so are their values" in d
        # Two examples, one per argument, never a list.
        assert "where a company is based and where a person" in voc.ARGUMENT_TYPES
        assert "a subscription to a service" in voc.ARGUMENT_TYPES
        # The rules of before stay, in their place.
        assert d.index("a lasting relation and a single event") < d.index(voc.ARGUMENT_TYPES)
        assert d.index(voc.ARGUMENT_TYPES) < d.index("New phrase:")
        assert "Prefer null when unsure" in d


class TestTheJudge:
    @pytest.mark.parametrize("name", ["agents", "batch"])
    def test_the_rule_in_the_two_judges_of_the_agents(self, name):
        prompt = AGENTS[name]
        assert cc.RULE_TRUE_TOGETHER in prompt
        assert "{RULE_TRUE_TOGETHER}" not in prompt and "{RULE_NO_LONGER_TRUE}" not in prompt
        # Before the tag's rule, which it does not change.
        assert prompt.index(cc.RULE_TRUE_TOGETHER) < prompt.index(cc.RULE_SINGLE_VALUE)
        assert prompt.index(cc.RULE_SINGLE_VALUE) < prompt.index(cc.RULE_WORDS) < prompt.index(cc.RULE_DATES)

    def test_what_it_says(self):
        r = cc.RULE_TRUE_TOGETHER
        # The definition is carried by the examples, not written as a first
        # sentence: written, it made gpt-oss-120b close a first job with a
        # part-time second one (BUG-200), measured on 6 Oct.
        assert "cannot both be true at once" not in r
        assert r.startswith("- What happened (an event, an action, a feeling or a reaction of one moment)")
        # A later event or action does not end an earlier state, preference,
        # feeling or identity, unless it says so; examples on both sides.
        assert "unless it says that the earlier one ended or changed" in r
        assert "-> NOT a contradiction (a visit" in r and "-> contradiction (selling it ends owning it)" in r
        assert "(two moments)" in r
        # A switch that names only the new value still ends the earlier one
        # (without it the batched judge stopped closing "prefers TypeScript"
        # with "Actually I switched to Rust", measured on 6 Oct).
        assert "prefers coffee\" vs \"Maria switched to herbal tea\" -> contradiction\n" \
               "  (switching to another one ends the earlier preference)" in r
        # A second preference, a second work, another side of an identity.
        assert "A second preference, a second work or another side of the same identity" in r
        # A restatement is not a change.
        assert "The same fact said in other words is NOT a contradiction" in r

    def test_no_longer_true_only_in_the_judge_of_one_fact(self):
        """The batch never shows "(no longer true)": a statement in the past
        is judged on its own even in a batched write
        (`worker.write_memory`), with `judge`."""
        assert cc.RULE_NO_LONGER_TRUE in cc.AGENTS_JUDGE
        assert cc.RULE_NO_LONGER_TRUE not in cc.AGENTS_JUDGE_BATCH
        assert cc.AGENTS_JUDGE.index(cc.RULE_TRUE_TOGETHER) < cc.AGENTS_JUDGE.index(cc.RULE_NO_LONGER_TRUE)
        assert cc.AGENTS_JUDGE.index(cc.RULE_NO_LONGER_TRUE) < cc.AGENTS_JUDGE.index(cc.RULE_SINGLE_VALUE)
        assert "says that the new fact itself happened or held" in cc.RULE_NO_LONGER_TRUE
        assert "never" in cc.RULE_NO_LONGER_TRUE and "did not happen" in cc.RULE_NO_LONGER_TRUE
        assert cc.PAST_MARK.strip() in cc.RULE_NO_LONGER_TRUE
        assert "being over ends nothing" in cc.RULE_NO_LONGER_TRUE

    def test_the_past_reaches_the_judge_of_one_fact_with_the_rule(self):
        seen = []

        def call(prompt, n):
            seen.append(prompt)
            return '{"contradictions": []}'

        c = SimpleNamespace(id=uuid.uuid4(), subject_label="Bruno", subject_canonical="Bruno",
                            predicate_raw="loves", object_label="hiking in the mountains", valid_from=None)
        judge.judge(call, [c], "Bruno", "visited", "the mountains", single_value=lambda _c: (False, None),
                        past=True)
        new = seen[0].split("NEW fact:")[1]
        # 3.3.0: the marker the rule explains; "(no longer true)" read as a
        # negation with gpt-oss (the bench next to PAST_MARK).
        assert cc.PAST_MARK in new and "(no longer true)" not in new
        assert cc.RULE_NO_LONGER_TRUE in seen[0]

    def test_a_single_marker(self):
        """memory_core 4.0.0: the other product's judge's " (no longer true)" left the
        code with the `mark` parameter that kept it; a past statement always
        carries PAST_MARK."""
        assert cc.new_fact_for_the_judge("Bruno", "visited", "the mountains", past=True) == \
            "Bruno visited the mountains" + cc.PAST_MARK

    def test_the_batch_does_not_show_the_past(self):
        seen = []
        new = uuid.uuid4()
        c = SimpleNamespace(id=uuid.uuid4(), subject_label="Bruno", subject_canonical="Bruno",
                            predicate_raw="loves", object_label="hiking", valid_from=None)
        judge.batch(
            None, None,
            [dict(id=new, subject="Bruno", predicate_raw="visited", predicate_canonical="visits",
                  object="the mountains", valid_from=None)],
            gather=lambda nf: [c], single_value_of_new=lambda nf: (False, None),
            classify=lambda groups: seen.extend(groups) or {},
        )
        assert "(no longer true)" not in seen[0]["new_text"]

    def test_the_rules_of_before_stay_byte_for_byte(self):
        """RULE_WORDS and RULE_DATES as in 3.1.3 (the 3.1.3 text of
        RULE_SINGLE_VALUE, which stayed for the other product's judge, left
        with it in 4.0.0: the fingerprint of the test below fixes it)."""
        assert _fingerprint(cc.RULE_WORDS) == "2c35fffa41202c0ef5a40b285d75bbef25e8b3b38e5eb54caa1f2162cfa5c430"
        assert _fingerprint(cc.RULE_DATES) == "dfa332c7ffb6db762e98f498078c6daac95cb3f4f715414a1c0753c7644803ee"

    def test_single_value_3_3_0_only_the_new_sentence(self):
        """3.3.0 (Ritter et al. 2008 §4): the Agents' rule is the one of
        3.1.3 plus ONE sentence: the same value said more or less precisely, or
        the part and the whole, are not a new value. The 3.1.3 text
        (`_RULE_SINGLE_VALUE_3_1`, the other product's judge, gone with
        4.0.0) is fixed by the fingerprint of the text with the spaces normalised."""
        sentence = (
            'A value that names the SAME thing, more or less precisely, or a part of it, '
            'or a whole that contains it, is not a new value: "Maria lives_in Turin '
            '[single-valued for values like: Turin]" vs "Maria lives_in Piedmont" -> NOT '
            'a contradiction (Turin is in Piedmont), and vs "Maria lives_in the old centre '
            'of Turin" -> NOT a contradiction (the same city, said more precisely). '
        )
        new = " ".join(cc.RULE_SINGLE_VALUE.split())
        assert new.count(sentence) == 1
        assert _fingerprint(new.replace(sentence, "")) == "5a19239b9715bf9f02e36b1c6aa54600e431beb9d70b9e69cd3b6da70b032a5f"
        assert cc.RULE_SINGLE_VALUE in cc.AGENTS_JUDGE and cc.RULE_SINGLE_VALUE in cc.AGENTS_JUDGE_BATCH


class TestTheExamples:
    """The examples are invented: none comes from LoCoMo (CC BY-NC licence,
    its texts stay out of the repository) nor from the measured cases (an
    example equal to the measured case would inflate the measurement)."""

    TEXTS = {
        "true_together": cc.RULE_TRUE_TOGETHER,
        "no_longer_true": cc.RULE_NO_LONGER_TRUE,
        "types": voc.ARGUMENT_TYPES,
        "single_value": voc.single_value_question("feels", "happy"),
        # Only the 3.3.0 sentence (and the 3.1.3 tail after it): the rest of the
        # rule is 3.1.3's, and its "Business" holds the case user "ines" as a
        # substring. Cut at the new sentence since 4.0.0, when the 3.1.3 text
        # (`_RULE_SINGLE_VALUE_3_1`) left with the other product's judge: the same text.
        "single_value_3_3_0": (lambda n: n[n.index(" A value that names the SAME thing"):])(
            " ".join(cc.RULE_SINGLE_VALUE.split())),
    }

    # Names and distinctive words of LoCoMo conversation 26 (the one read for
    # the measurement): if one appears in a prompt, an example was copied from there.
    LOCOMO = ("Caroline", "Melanie", "transgender", "womanhood", "LGBTQ", "camping", "beach", "purple",
              "Oliver", "violin", "clarinet", "pottery", "adoption", "sunrise", "Perseid", "pride parade")

    @pytest.mark.parametrize("name", sorted(TEXTS))
    def test_nothing_from_locomo(self, name):
        text = self.TEXTS[name].lower()
        for word in self.LOCOMO:
            assert word.lower() not in text, (name, word)
