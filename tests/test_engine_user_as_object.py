"""The end user as the OBJECT of a fact reads "the user" in the context block (memory_core 3.1.4, 2026-10-06).

On the 3.1 bench (LoCoMo conv-26): "Melanie supports gb-0-caroline",
"kids give_strength_to gn-0-melanie" showed the end user's opaque id in 142
blocks of 152 with the 3.1 rules off and 97 with them on, every time the
block's own end user. The subject was already replaced (measured on
LongMemEval: the reader does not bind a handle to the question's "I"); the
object was not. Red on the code before.
"""
from korely_agent.services.memory_core.presentation import context_line


def _fact(subject, predicate, obj):
    return {"subject_label": subject, "subject_canonical": subject, "predicate_raw": predicate,
            "object_label": obj, "object_canonical": obj, "valid_from": None, "tense": "current"}


def test_the_user_as_object_is_the_user():
    assert context_line(_fact("Her manager", "mentors", "c4821"), "c4821") == "- Her manager mentors the user"


def test_the_user_as_subject_stays_the_user():
    assert context_line(_fact("c4821", "lives_in", "Rome"), "c4821") == "- The user lives_in Rome"


def test_another_person_keeps_their_name():
    assert context_line(_fact("Melanie", "supports", "Caroline"), "c4821") == "- Melanie supports Caroline"


def test_without_an_end_user_nothing_changes():
    assert context_line(_fact("Melanie", "supports", "c4821"), None) == "- Melanie supports c4821"
