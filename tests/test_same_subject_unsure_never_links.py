"""The decision of the review of subjects (memory_core 6.20.0,
`objects.decide_by_choice`): a union only when the first reading chooses a
candidate and the second, the pair alone and framed the other way round,
says "same". "none", "unsure", a disagreement, an answer outside the
choices: no union, ever."""
import pytest

from korely_agent.services.memory_core import objects as O
from korely_agent.services.memory_core.vocabulary import UnreadableAnswer
from tests.subject_world import Port

NEW = O.Side(name="Korely Cloud", key="korely cloud", facts=("Korely Cloud | has version | 1.0.30",))
HELD = [O.Side(name="Cloud", key="cloud", facts=("Cloud | runs version | 1.0.27",)),
        O.Side(name="Caddy di Korely", key="caddy di korely", facts=("Caddy di Korely | serves | Cloud",))]


def decide(*answers, severe=False):
    port = Port(*answers)
    [d] = O.decide_by_choice(port.choose, [(NEW, HELD)], severe=severe)
    return d, port


def test_two_same_readings_unite_with_the_chosen_candidate():
    d, port = decide("1", "same")
    assert d.answer == 0
    assert [r["framing"] for r in d.readings] == ["select", "pair"]
    state, instructions, criteria = port.calls[1]
    assert state["A"]["name"] == "Cloud" and state["B"]["name"] == "Korely Cloud", \
        "the second reading puts the name the memory held first as A"
    assert set(criteria) == {"same", "different", "unsure"}
    assert instructions == O.SUBJECT_PAIR


@pytest.mark.parametrize("answers,expected", [
    (("none",), O.DIFFERENT),
    (("unsure",), O.UNSURE),
    (("1", "different"), O.UNSURE),    # a disagreement
    (("2", "unsure"), O.UNSURE),
    (("3",), O.UNSURE),                # no third candidate: not a choice
    ((None,), O.UNSURE),               # no choice in the answer
    (("SAME",), O.UNSURE),             # "same" is not a choice of the first reading
], ids=["none", "unsure", "disagreement", "second-unsure", "out-of-range", "no-answer", "wrong-token"])
def test_anything_else_unites_nothing(answers, expected):
    d, _port = decide(*answers)
    assert d.answer == expected


def test_the_first_reading_offers_the_candidates_and_two_ways_out():
    _d, port = decide("unsure")
    state, instructions, criteria = port.calls[0]
    assert set(criteria) == {"1", "2", "none", "unsure"}
    assert list(state["names_used_before"]) == ["1", "2"]
    assert state["B"]["facts"] == list(NEW.facts)
    assert instructions == O.SUBJECT_SELECT
    for rule in ("A shared name alone never decides", "When one name is a shorter form of the other",
                 "A thing is not the same as what it is part of"):
        assert rule in instructions


@pytest.mark.parametrize("answers", [("3",), (None,), ("1", "maybe")], ids=["out-of-range", "none", "pair"])
def test_an_unusable_answer_raises_when_severe(answers):
    with pytest.raises(UnreadableAnswer):
        decide(*answers, severe=True)
