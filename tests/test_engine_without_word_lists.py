"""memory_core 3.3.0: no English word lists in the code.

`predicate_forms.py` held 25 English prefixes and ~70 synonyms applied to every
predicate before the learned vocabulary, `families.PREDICATE_TO_FAMILY` gave a
relation its family from 49 English words, and five English pairs of opposing
predicates widened the judge's candidates. The rule of 8 Oct 2026 ("in the
code we must have no word list at all"): they left the code,
with no switch. Merging phrasings and choosing a family is the learned
vocabulary's job (CESI, Vashishth et al. 2018); only the predicates of our
own schema (identity, instructions) keep a family without asking.

No model call: the vocabulary is a function.
"""
# Port notes: on korely-agent's tables (tenant_id, `facts.entities`).
#   * TestTheCloud::test_no_switch checked that the hosted settings
#     have no `agents_no_word_lists`; the same on this product's switch
#     (`predicates._NO_WORD_LISTS` absent) is already here:
#     tests/test_memory_core_3_3_adapter.py::TestNoWordLists::test_there_is_no_switch.
#     Not duplicated.
#   * TestTheCloud::test_the_read_uses_the_form_of_the_writes runs on
#     korely-agent's `facts.entities.predicate_form`, and
#     ::test_the_cloud_extractor_passes_no_switches on its extractor
#     (`facts.extractor.extract_facts_and_observation`, the model call faked at `_call_or_raise`,
#     the hosted adapter's `_chiama`).
from __future__ import annotations

import ast
import inspect
import pathlib
import unicodedata

import pytest

from korely_agent.services.facts import entities
from korely_agent.services.facts import extractor as cloud_extractor
from korely_agent.services.memory_core import store, fact_extractor, families, predicate_forms
from korely_agent.services.memory_core.families import family_for
from korely_agent.services.memory_core.predicate_forms import canonicalize_predicate
from tests.test_engine_uncovered_branches import _Vocabulary


# raw -> the form kept: the model's own words, one surface form
_CASES = {
    "loves": "loves",
    "is fond of": "is_fond_of",
    "prefers": "prefers",
    "works_with": "works_with",
    "based in": "based_in",
    "has": "has",
    "is on": "is_on",
    "subscribes to": "subscribes_to",
    "Lives  In": "lives_in",
    "of": "unknown",
    "  ": "unknown",
    "lavora_già": "lavora_già",
    "喜欢": "喜欢",
}


class TestTheNormalizer:
    @pytest.mark.parametrize("raw", sorted(_CASES))
    def test_the_form_of_the_model(self, raw):
        assert canonicalize_predicate(raw) == _CASES[raw]

    def test_no_word_list_and_no_switch(self):
        assert not hasattr(predicate_forms, "_AUX_PREFIXES") and not hasattr(predicate_forms, "_SYNONYM_MAP")
        assert list(inspect.signature(canonicalize_predicate).parameters) == ["raw"]


def _words_of_the_engine():
    """Every string constant written in memory_core's code, each alone and
    followed by a word of no language. A map of synonyms or a list of
    prefixes put back under any name has its words among these constants,
    so feeding them to the normalizer exercises whatever map it is."""
    folder = pathlib.Path(predicate_forms.__file__).resolve().parent
    words = set(_CASES)
    for f in sorted(folder.glob("*.py")):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.strip():
                words.add(n.value)
    return sorted(words | {f"{p} qzx7" for p in words})


def _letters(s):
    """The letters, marks and digits of `s`, case folded: what a plain
    normalization keeps, whatever the script."""
    t = unicodedata.normalize("NFKC", s).casefold()
    return "".join(ch for ch in t if unicodedata.category(ch)[0] in "LMN")


class TestAProperty:
    """The rule as a property, not as the absence of old names (audit of 17 Oct
    2026): `hasattr` on `_SYNONYM_MAP` and `_AUX_PREFIXES` stops only their
    return under the same names, and a map `{"adores": "loves"}` applied in
    `canonicalize_predicate` under a new one passed every test of the engine."""

    def test_the_normalizer_rewrites_and_does_not_replace(self):
        """What comes out is the input's own letters, separated by
        underscores: no word replaced by another, none dropped from the front.
        Since memory_core 5.2.1 a form longer than its column is cut at the
        end, at a word boundary: then it is the start of the input's letters,
        within MAX_PREDICATE."""
        from korely_agent.services.memory_core.predicate_forms import MAX_PREDICATE

        changed = []
        for raw in _words_of_the_engine():
            shape_of = canonicalize_predicate(raw)
            letters, kept = _letters(raw), shape_of.replace("_", "")
            if shape_of == "unknown":
                if len(letters) > 2 and letters != "unknown":
                    changed.append((raw[:40], shape_of))
            elif kept != letters and not (letters.startswith(kept) and len(shape_of) <= MAX_PREDICATE
                                             and len(letters) > len(kept)):
                changed.append((raw[:40], shape_of))
        assert not changed, f"forms that are not the input's own words: {changed[:10]}"

    def test_the_family_is_only_the_one_of_the_schema(self):
        """Without the model a relation is `other`, except the few of our own
        schema (`PREDICATE_TO_FAMILY`, identity only): a family map put back
        under a new name gives some word another family."""
        different = []
        for raw in _words_of_the_engine():
            shape_of = canonicalize_predicate(raw)
            expected = families.PREDICATE_TO_FAMILY.get(shape_of, "other")
            for which, fam in (("family_for", family_for(shape_of)),
                              ("family_of_the_fact", families.family_of_the_fact(None, shape_of)),
                              ("schema_family", store.schema_family(shape_of))):
                if fam != expected:
                    different.append((which, shape_of[:40], fam))
        assert not different, f"a family decided by a word: {different[:10]}"


class TestTheFamilies:
    def test_the_schema_stays_the_rest_is_said_by_the_model(self):
        assert store.schema_family("renamed_to") == "identity"
        assert store.schema_family("instructs") == "other"
        assert store.schema_family("likes") == "other"
        assert store.schema_family("lives_in") == "other"

    def test_the_map_is_only_our_schema(self):
        assert set(families.PREDICATE_TO_FAMILY.values()) == {"identity"}
        assert family_for("likes") == "other" and family_for("renamed_to") == "identity"
        assert not hasattr(families, "OPPOSING_PREDICATE_FAMILIES")

    def test_the_stored_family_wins(self):
        assert families.family_of_the_fact("preferences", "likes") == "preferences"
        assert families.family_of_the_fact(None, "likes") == "other"


def _resolve(db, tenant, phrase, voc, **kw):
    return store.resolve_predicate(db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None), phrase, "tea",
                                      ask=voc, embed=None, strict=True, severe=True, **kw)


class TestTheVocabulary:
    def test_loves_stays_loves_and_the_family_is_said_by_the_model(self, db, tenant):
        voc = _Vocabulary(family="preferences")
        rel = _resolve(db, tenant, "loves", voc)
        assert rel.canonical == "loves" and rel.family == "preferences"
        assert "family" in voc.asked

    def test_likes_also_asks_the_family(self, db, tenant):
        voc = _Vocabulary(family="people")
        rel = _resolve(db, tenant, "likes", voc)
        assert rel.family == "people" and "family" in voc.asked

    def test_the_schema_does_not_go_through_the_vocabulary(self, db, tenant):
        voc = _Vocabulary()
        rel = _resolve(db, tenant, "renamed_to", voc)
        assert rel.canonical == "renamed_to" and rel.family == "identity" and voc.asked == []

    def test_the_past_goes_back_to_the_present_form_of_the_model(self, db, tenant):
        voc = _Vocabulary(present="loves")
        rel = _resolve(db, tenant, "loved", voc, tense="past")
        assert rel.canonical == "loves"


def _fact(predicate):
    return {"subject": "user", "subject_type": "person", "predicate": predicate, "object": "tea",
            "evidence": "I love tea", "tense": "current", "confidence": 0.9}


class TestTheExtractor:
    def test_two_different_phrases_stay_two_facts(self):
        facts = fact_extractor.read_facts([_fact("loves"), _fact("likes")], "I love tea and I like tea")
        assert len(fact_extractor._without_duplicates(facts)) == 2

    def test_the_same_phrase_twice_is_one_fact(self):
        facts = fact_extractor.read_facts([_fact("loves"), _fact("Loves")], "I love tea")
        assert len(fact_extractor._without_duplicates(facts)) == 1

    def test_no_function_has_the_switch(self):
        for name in ("extract", "extract_with_observation", "extract_batch_with_observation", "read_facts"):
            assert "without_word_lists" not in inspect.signature(getattr(fact_extractor, name)).parameters, name
        assert "without_word_lists" not in inspect.signature(store.resolve_predicate).parameters


class TestTheCloud:
    def test_the_read_uses_the_form_of_the_writes(self, db, tenant):
        assert entities.predicate_form("loves") == "loves"
        assert entities.predicate_form("is fond of") == "is_fond_of"

    def test_the_cloud_extractor_passes_no_switches(self, db, tenant, monkeypatch):
        seen = {}
        monkeypatch.setattr(cloud_extractor.fact_extractor, "extract_with_observation",
                            lambda *a, **k: seen.update(k) or cloud_extractor.fact_extractor.Extraction([], None))
        monkeypatch.setattr(cloud_extractor, "_call_or_raise", lambda *a, **k: None)
        cloud_extractor.extract_facts_and_observation("I love tea", end_user_id="u1", db=db,
                                                      tenant_id=tenant.id)
        assert "without_word_lists" not in seen


class TestObjectsInAnyAlphabet:
    """3.3.0 (an inventory of the word lists): the judge's candidate stages compare the
    words of two objects in any script, not [a-z0-9] only."""

    @pytest.mark.parametrize("a,b,expected", [
        ("SSO", "SSO support", True),
        ("компания Ромашка", "Ромашка", True),
        ("Straße", "STRASSE", True),
        ("数据库", "数据库", True),
        ("Acme", "Initech", False),
        ("a", "a b", False),
    ])
    def test_compatible(self, a, b, expected):
        from korely_agent.services.memory_core.contradiction import compatible_objects

        assert compatible_objects(a, b) is expected


class TestTheFamiliesInThePrompt:
    def test_defined_in_words_without_relation_names(self):
        from korely_agent.services.memory_core import vocabulary

        d = vocabulary._FAMILIES_DESCRIPTION
        assert "_" not in d  # no relation name like married_to
        for name in ("knows", "owns", "uses", "likes", "dislikes", "manages"):
            assert name not in d.split(), name
