"""The predicate vocabulary (memory_core `predicate_forms` and `families`) is the
bottom of the dependency graph.

These tests exist for two reasons. The obvious one is that canonicalization and
family assignment drive contradiction detection: if `works_at` and `Works At`
stop collapsing to the same key, two facts that contradict each other are stored
as unrelated and the store starts lying.

The less obvious one is the import at the top. These modules must stay free of
database sessions, settings objects and framework imports. A test that fails the
moment someone adds `from ..db import ...` here is cheaper than rediscovering the
coupling six months later, which is exactly how the agent surface ended up
transitively importing calendar sync.
"""
import pytest

from korely_agent.services.memory_core.families import family_for
from korely_agent.services.memory_core.predicate_forms import canonicalize_predicate


class TestCanonicalization:
    @pytest.mark.parametrize("raw,expected", [
        ("works_at", "works_at"),
        ("Works At", "works_at"),
        ("  WORKS  AT  ", "works_at"),
        ("works-at", "works_at"),
    ])
    def test_spelling_variants_collapse(self, raw, expected):
        assert canonicalize_predicate(raw) == expected

    def test_unknown_predicate_survives(self):
        """An unmapped predicate must still come back usable, not empty.

        Extraction invents predicates the vocabulary has never seen; dropping
        them would silently discard facts.
        """
        out = canonicalize_predicate("enjoys_sailing_with")
        assert out and out.strip() == out


class TestFamilies:
    """No hand-written vocabulary (memory_core 3.3.0, the rule of 8 Oct
    2026: no word lists in the code). The static map holds only the
    predicates our own schema asks the model for; every other relation gets
    its family from the learned vocabulary, stored on the relation and on the
    fact."""

    def test_our_schema_has_its_family(self):
        assert family_for("renamed_to") == "identity"
        assert family_for("instructs") == "other"  # memory_core 5.0.0

    def test_an_english_word_is_not_looked_up(self):
        assert family_for("likes") == "other"

    def test_unknown_predicate_falls_back(self):
        assert family_for("enjoys_sailing_with") == "other"


def test_package_has_no_infrastructure_imports():
    """Guard: the vocabulary must not reach for a db, settings or a framework,
    directly or through a sibling module: importing it in a fresh interpreter
    loads none of them."""
    import subprocess
    import sys
    forbidden = ("sqlalchemy", "fastapi", "pydantic", "korely_agent.db", "korely_agent.config")
    probe = (
        "import sys\n"
        "import korely_agent.services.memory_core.families\n"
        "import korely_agent.services.memory_core.predicate_forms\n"
        f"print(sorted(m for m in sys.modules"
        f" if any(m == f or m.startswith(f + '.') for f in {forbidden!r})))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", f"the leaf is no longer a leaf: {out.stdout.strip()}"


class TestNothingIsStrippedOrMerged:
    """Canonicalization changes the spelling, never the words (memory_core
    3.3.0). Until then 25 English prefixes were stripped and 59 synonyms merged
    by hand: "is_on" became "on" and then "unknown", "works_with" became
    "works_at" (a colleague read as the employer), "based_in" became
    "lives_in", "has" became "owns". Merging phrasings of one relation is the
    learned vocabulary's question to the model, in every language."""

    @pytest.mark.parametrize("raw", [
        "is_on", "is_at", "is_in", "is_a", "is_by", "has_a",
        "was_at", "was_in", "been_in", "are_in", "were_at",
    ])
    def test_short_relations_survive(self, raw):
        assert canonicalize_predicate(raw) == raw

    @pytest.mark.parametrize("raw", ["loves", "is_fond_of", "works_with", "based_in", "has", "prefers"])
    def test_the_words_the_model_wrote_are_kept(self, raw):
        assert canonicalize_predicate(raw) == raw

    @pytest.mark.parametrize("raw", ["a", "of", "in", "to", "", "   "])
    def test_genuinely_degenerate_input_is_still_unknown(self, raw):
        """The fallback must not resurrect noise: 'unknown' still means
        the model gave us nothing to work with."""
        assert canonicalize_predicate(raw) == "unknown"
