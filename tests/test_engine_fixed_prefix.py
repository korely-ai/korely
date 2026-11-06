"""memory_core 3.3.0, work item 8: the fixed prefix of the extraction prompt.

A provider's prompt cache reuses only an identical prefix. With
`fixed_prefix` the rules name the end user END_USER and the handle is bound
once, at the end, before the parts that change with every call: two end
users, two memories, two days share everything before that line. Off (the
default), the prompts are those of before, byte for byte.

No model call: the model is a function that records the prompts.
"""
from __future__ import annotations

import json

import pytest

from korely_agent.services.memory_core import fact_extractor as est


class _Model:
    def __init__(self, answer=None):
        self.prompt = []
        self.answer = answer

    def __call__(self, prompt, max_tokens):
        self.prompt.append(prompt)
        if self.answer is not None:
            return json.dumps(self.answer)
        ids = [r.split("]")[0].split()[0] for r in prompt.split("[mem_id=")[1:]]
        if ids:
            return json.dumps({"memories": [{"mem_id": i, "observation": "", "facts": []} for i in ids]})
        return json.dumps({"observation": "", "facts": []})


BINDING = 'END_USER in the rules above is the end user "'


def _call(function, *, user, text, **kw):
    m = _Model()
    function(m, user, text, **kw)
    [p] = m.prompt
    return p


VARIANTS = {
    "single": lambda m, u, t, **kw: est.extract(m, t, u, **kw),
    "observation": lambda m, u, t, **kw: est.extract_with_observation(
        m, t, u, previous=["An earlier memory."], memory_date="2023-05-08", **kw),
    "observation_times": lambda m, u, t, **kw: est.extract_with_observation(
        m, t, u, previous=["An earlier memory."], memory_date="2023-05-08", times=True, **kw),
    "observation_batch": lambda m, u, t, **kw: est.extract_batch_with_observation(
        m, [("m1", t), ("m2", t + " Again.")], u, previous=["Before."], date={"m1": "2023-05-08"}, **kw),
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
class TestThePrefix:
    def test_off_the_prompt_is_the_one_before(self, name):
        f = VARIANTS[name]
        assert _call(f, user="ana-77", text="I moved to Lyon.") == \
            _call(f, user="ana-77", text="I moved to Lyon.", fixed_prefix=False)
        p = _call(f, user="ana-77", text="I moved to Lyon.")
        assert "END_USER" not in p and '"ana-77"' in p

    def test_on_two_users_share_everything_up_to_the_binding(self, name):
        f = VARIANTS[name]
        a = _call(f, user="ana-77", text="I moved to Lyon.", fixed_prefix=True)
        b = _call(f, user="customer-9031-zq", text="We closed the deal with Initech.", fixed_prefix=True)
        assert a.count(BINDING) == 1 and b.count(BINDING) == 1
        pa, pb = a.split(BINDING)[0], b.split(BINDING)[0]
        assert pa == pb
        assert len(pa) > 0.6 * len(a)  # the shared part is most of the prompt
        assert "ana-77" not in pa and "{END_USER}" not in a
        assert a.count("ana-77") == 1  # bound once
        assert 'subject = END_USER' in pa


class TestTheNameComesBackAsTheName:
    def test_a_subject_written_end_user_is_the_user(self):
        m = _Model({"facts": [{"subject": "END_USER", "subject_type": "person", "predicate": "lives_in",
                                 "object": "Lyon", "tense": "current", "confidence": 0.9}]})
        [f] = est.extract(m, "I moved to Lyon.", "ana-77", fixed_prefix=True)
        assert f.subject == "ana-77" and f.object == "Lyon"

    def test_an_object_written_end_user_is_the_user_and_is_not_dropped(self):
        """The review's case: the quality gate read "END_USER" as a code
        identifier and dropped "Maria mentors END_USER"."""
        m = _Model({"facts": [{"subject": "Maria", "subject_type": "person", "predicate": "mentors",
                                 "object": "END_USER", "tense": "current", "confidence": 0.9}]})
        [f] = est.extract(m, "Maria mentors me on Fridays.", "ana-77", fixed_prefix=True)
        assert f.subject == "Maria" and f.object == "ana-77"

    def test_off_end_user_stays_written(self):
        [f] = est.read_facts([{"subject": "END_USER", "predicate": "likes", "object": "jazz"}], "I like jazz.")
        assert f.subject == "END_USER"


class TestTheGuard:
    def test_a_placeholder_without_quotes_does_not_pass(self):
        with pytest.raises(ValueError):
            est._with_the_user("rules {END_USER} here\nMemory:\n{CONTENT}", "u1", True)

    def test_without_a_variable_part_does_not_pass(self):
        with pytest.raises(ValueError):
            est._with_the_user('rules "{END_USER}" here', "u1", True)


class TestTheUserAsObject:
    def test_the_users_name_as_object_stays(self):
        """3.3.0: `keep_grounded` keeps the end user as object as it keeps them as
        subject; before, "Maria mentors me" was dropped with the switch off
        too (the handle is not in the text)."""
        m = _Model({"facts": [{"subject": "Maria", "subject_type": "person", "predicate": "mentors",
                                 "object": "ana-77", "tense": "current", "confidence": 0.9}]})
        [f] = est.extract(m, "Maria mentors me on Fridays.", "ana-77")
        assert f.object == "ana-77"

    def test_an_invented_object_is_still_dropped(self):
        m = _Model({"facts": [{"subject": "Maria", "subject_type": "person", "predicate": "mentors",
                                 "object": "Tampa", "tense": "current", "confidence": 0.9}]})
        assert est.extract(m, "Maria mentors me on Fridays.", "ana-77") == []
