"""memory_core 3.3.0: the present form of a past phrase without its object.

The judge cases of 8 Oct, on the 3.3.0 branch only: "Luca left Apple"
gave the relation `leaves_apple`, which then took other objects ("Luca
leaves_ferrero Apple"), and "Kenji moved to Kyoto in May" gave
`lives_in_kyoto`. The tense question shows the object as context, and
gpt-oss-120b wrote it into the present form. `store.without_the_object`
removes the words of the fact's own object from the end of the answer (the
relation phrase and its arguments are separate spans, ReVerb §3). No model
call: the vocabulary is a function.
"""
# Port notes: the vocabulary is korely-agent's (the helpers of
# tests/test_engine_uncovered_branches.py, tenant_id).
from __future__ import annotations

import pytest

from korely_agent.services.memory_core import store
from tests.test_engine_uncovered_branches import _Vocabulary, _resolve


@pytest.mark.parametrize("shape_of, obj, expected", [
    ("leaves_apple", "Apple", "leaves"),
    ("lives_in_kyoto", "Kyoto", "lives_in"),
    ("visits_the_dolomites", "the Dolomites", "visits"),
    ("works_at_acme_corp", "Acme Corp", "works_at"),
    ("leaves", "Apple", "leaves"),                       # nothing of the object: as it is
    ("apple", "Apple", "apple"),                          # nothing would be left: as it is
    ("is_apple", "Apple", "is_apple"),                    # what is left is too short: as it is
    ("leaves_apple_for_google", "Apple", "leaves_apple_for_google"),  # only at the end
    ("vive_a_kyōto", "Kyōto", "vive_a"),                  # any script (Italian on purpose)
    ("unknown", "Apple", "unknown"),
    ("leaves_apple", "", "leaves_apple"),
])
def test_without_the_object(shape_of, obj, expected):
    assert store.without_the_object(shape_of, obj) == expected


class TestInTheVocabulary:
    def test_the_past_with_its_object_becomes_the_relation_without_it(self, db, tenant):
        voc = _Vocabulary(present="leaves Apple")
        rel = _resolve(db, tenant, "left", voc, tense="past", obj="Apple")
        assert rel.canonical == "leaves"

    def test_another_object_is_not_touched(self, db, tenant):
        voc = _Vocabulary(present="leaves Apple")
        rel = _resolve(db, tenant, "left", voc, tense="past", obj="Ferrero")
        assert rel.canonical == "leaves_apple"

    def test_the_move(self, db, tenant):
        voc = _Vocabulary(present="lives in Kyoto")
        rel = _resolve(db, tenant, "moved to", voc, tense="past", obj="Kyoto")
        assert rel.canonical == "lives_in"
