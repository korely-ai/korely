"""memory_core 3.3.0, from the audit of 6 Oct 2026: two real values the quality
gate dropped. A date written day first ("05/12/2024") read as a path (two
slashes, no space); a person called Nan read as the literal nan (the
comparison ignored case). Both are shapes, not words: the gate still refuses
paths, identifiers and the literals of code in the case the languages write
them."""
from __future__ import annotations

import pytest

from korely_agent.services.memory_core.quality import drop_reason


class TestADateIsNotAPath:
    @pytest.mark.parametrize("data", ["05/12/2024", "12.05.2024", "2024/12/05", "5-12-2024"])
    def test_a_date_written_in_the_text_passes(self, data):
        assert drop_reason("born_on", "Anna", data, f"Anna was born on {data}.") is None

    def test_an_invented_day_first_date_is_dropped(self):
        assert drop_reason("tested_on", "Acme", "05/12/2024", "We tested it in June.") == "object_is_an_invented_date"

    @pytest.mark.parametrize("path", ["src/app/main.py", "./notes/a.md", "a/b/c"])
    def test_paths_stay_paths(self, path):
        assert drop_reason("uses", "Acme", path) == "object_is_a_path"


class TestANameIsNotALiteral:
    def test_nan_is_a_name(self):
        assert drop_reason("visits", "u1", "Nan") is None

    @pytest.mark.parametrize("literal", ["true", "false", "null", "undefined", "NaN", "nan", "True", "False", "None",
                                         "TRUE", "FALSE", "NULL", "Null"])
    def test_the_literals_of_code(self, literal):
        assert drop_reason("knowledge_graph", "Acme", literal) == "object_is_a_bare_boolean"

    def test_lowercase_none_is_a_word(self):
        assert drop_reason("allergic_to", "u1", "none") is None


class TestAPredicateInAnyAlphabet:
    """canonicalize_predicate kept [a-z0-9] only: a predicate written in
    another alphabet lost its letters or became "unknown"."""

    @pytest.mark.parametrize("raw, shape_of", [
        ("lavora già presso", "lavora_già_presso"),
        ("喜欢", "喜欢"),
        ("работает в", "работает_в"),
        ("يعمل في", "يعمل_في"),
        ("Straße_wohnt", "strasse_wohnt"),
        ("काम करता है", "काम_करता_है"),
    ])
    def test_the_letters_stay(self, raw, shape_of):
        from korely_agent.services.memory_core.predicate_forms import canonicalize_predicate

        assert canonicalize_predicate(raw) == shape_of

    def test_a_two_character_chinese_predicate_stays(self):
        from korely_agent.services.memory_core.fact_extractor import read_facts

        [f] = read_facts([{"subject": "u1", "predicate": "喜欢", "object": "茶"}], "我喜欢茶")
        assert f.predicate_raw == "喜欢"
        assert read_facts([{"subject": "u1", "predicate": "is", "object": "tired"}], "I am tired") == []

    def test_a_one_letter_copula_is_unknown(self):
        from korely_agent.services.memory_core.predicate_forms import canonicalize_predicate

        assert canonicalize_predicate("è") == "unknown"

    # 3.3.0: no prefixes or synonyms (the rule of 8 Oct): the
    # English phrase keeps the form the model wrote, in snake case.
    @pytest.mark.parametrize("raw, shape_of", [
        ("is fond of", "is_fond_of"), ("works   for", "works_for"), ("has", "has"), ("lives in", "lives_in"),
        ("of", "unknown"), ("Likes", "likes"), ("co-worker", "co_worker"), ("isn’t fond of", "isn_t_fond_of"),
    ])
    def test_english_stays_as_it_was(self, raw, shape_of):
        from korely_agent.services.memory_core.predicate_forms import canonicalize_predicate

        assert canonicalize_predicate(raw) == shape_of
