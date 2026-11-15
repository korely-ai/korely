"""The vocabulary reads a model's JSON as the extractor and the judge do
(memory_core 5.2.2).

A code review (17 Oct 2026): `vocabulary._json` had a chain of
its own `json.loads`, and on the same text could answer differently from
`model_json.read_object`: an object in a ``` block and a different one
outside it was the block's for the vocabulary, no answer for the rest of the
engine. Two different objects are no answer, for every reader.
"""
import pytest

from korely_agent.services.memory_core import vocabulary
from korely_agent.services.memory_core.model_json import read_object

TWO_ANSWERS = 'Here:\n```json\n{"many_values": true}\n```\nor rather {"many_values": false}'


def test_two_different_objects_are_no_answer_for_the_vocabulary_too():
    assert read_object(TWO_ANSWERS) is None
    assert vocabulary.read_single_value(TWO_ANSWERS) is None
    with pytest.raises(vocabulary.UnreadableAnswer):
        vocabulary.read_single_value(TWO_ANSWERS, severe=True)


@pytest.mark.parametrize("answer", [
    '{"many_values": true}',
    '```json\n{"many_values": true}\n```',
    'Sure. {"many_values": true} Hope it helps.',
    'Formatting:** * `{"many_values": true}` {"many_values": true}',
])
def test_one_answer_however_it_is_wrapped_is_read(answer):
    # many values at a time: not a single-value relation
    assert vocabulary.read_single_value(answer, severe=True) is False


def test_a_value_that_is_not_an_object_is_no_answer():
    assert vocabulary.read_family('["other"]') is None
