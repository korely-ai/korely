"""memory_core 6.4.0: an agent's memory with no end user is told so.

Until 6.3.0 a memory an agent wrote with no `user_id` was asked the prompts
of an end user's memory with the handle "user": "a short memory stored by a
software product about one of its end users", "subject = END_USER" for the
first person, the observation "The user ...". On the 58 memories of a team of
agents (7 Nov 2026) 48 of 168 facts came back with "user" as their subject
or object, none of them right.

The new frame is for a memory with an `agent_id` and no `user_id`, as Mem0
reads it (`is_agent_scoped = agent_id and not user_id`). A memory of an end
user, and one that names neither, are asked the prompts of 6.3.0 byte for
byte (tests/test_prompts_with_an_end_user_unchanged.py); here, what the new
frame asks and what the net keeps, and that a memory of no ids keeps the
first person it had.

No model is called: the model is a function that records the prompts and
answers what it is given.
"""
from __future__ import annotations

import itertools
import json
import re

import pytest

from korely_agent.services.memory_core import fact_extractor as est


class _Model:
    def __init__(self, facts=None):
        self.prompts = []
        self.facts = facts or []

    def __call__(self, prompt, max_tokens):
        self.prompts.append(prompt)
        ids = [r.split("]")[0].split()[0] for r in prompt.split("[mem_id=")[1:]]
        if ids:
            return json.dumps({"memories": [{"mem_id": i, "observation": "", "facts": self.facts} for i in ids]})
        return json.dumps({"observation": "", "facts": self.facts})


PROCEDURE = ("How a Cloud rollback is done: the same deploy command is run again with the previous "
             "version and digest, saved at the first step of the deploy.")
AGENT = "build-agent"
END_USER_BINDING = "END_USER in the rules above is the end user"
WRITER_BINDING = 'WRITER in the rules above is the agent "'

VARIANTS = {
    "single": lambda m, t, user, **kw: est.extract(m, t, user, **kw),
    "observation": lambda m, t, user, **kw: est.extract_with_observation(
        m, t, user, previous=["An earlier memory."], memory_date="2026-11-06", **kw),
    "observation_times": lambda m, t, user, **kw: est.extract_with_observation(
        m, t, user, previous=["An earlier memory."], memory_date="2026-11-06", times=True, **kw),
    "observation_batch": lambda m, t, user, **kw: est.extract_batch_with_observation(
        m, [("m1", t), ("m2", t + " Again.")], user, previous=["Before."], date={"m1": "2026-11-06"}, **kw),
}


def _prompt(name, text=PROCEDURE, *, user=None, agent_id=AGENT, **kw):
    # Without an agent the keyword is not passed: the call every caller made
    # before 6.4.0.
    if agent_id is not None:
        kw["agent_id"] = agent_id
    m = _Model()
    VARIANTS[name](m, text, user, **kw)
    [p] = m.prompts
    return p


def _switches(name):
    names = ["states", "attributes", "fixed_prefix", "without_threshold"]
    if name != "single":
        names.append("faithful")
    for values in itertools.product((False, True), repeat=len(names)):
        yield dict(zip(names, values))


@pytest.mark.parametrize("name", sorted(VARIANTS))
class TestTheFrameOfAnAgent:
    def test_no_end_user_is_named_or_bound(self, name):
        for kw in _switches(name):
            p = _prompt(name, **kw)
            assert "END_USER" not in p, kw
            assert '"user"' not in p, kw
            assert "about one of its end users" not in p and "about ONE of its end users" not in p
            assert "NO end user" in p
            assert 'The user ...' not in p, "the observation is told around nobody"
            assert not re.search(r"\{[A-Z_]+\}", p), f"a placeholder left unfilled: {kw}"

    def test_an_impersonal_statement_is_about_what_it_describes(self, name):
        p = _prompt(name)
        assert "A statement made impersonally" in p
        assert "never the writer and never someone who would follow it" in p

    def test_the_tense_of_a_rule_in_force_and_of_a_lasting_result(self, name):
        """A procedure, a rule or a decision in force is current (Friedrich,
        Palmer, Pinkal, ACL 2016, Figure 1); the result of something that
        happened and still stands is current from its day; past only when the
        text says the state ended (Graphiti extract_edges)."""
        p = _prompt(name)
        assert '"current" also for what holds as a standing rule: a procedure' in p
        assert "an approval given" in p and 'the result is "current", with valid_from' in p
        assert '"past": ONLY when the text says that the state itself ended' in p
        assert ('"past": the text presents the fact as having been true and no longer true'
                not in p), "the tense rule of an end user's memory"

    def test_the_first_person_is_the_agent_bound_once_after_the_rules(self, name):
        a = _prompt(name, fixed_prefix=True)
        b = _prompt(name, text="We closed the deal with Initech.", agent_id="review-agent-7", fixed_prefix=True)
        assert a.count(WRITER_BINDING) == 1 and b.count(WRITER_BINDING) == 1
        pa, pb = a.split(WRITER_BINDING)[0], b.split(WRITER_BINDING)[0]
        assert pa == pb, "two agents share every rule"
        assert AGENT not in pa and a.count(f'"{AGENT}"') == 1
        assert "subject = WRITER" in pa

    def test_without_the_fixed_prefix_the_agent_is_in_place(self, name):
        p = _prompt(name)
        assert f'subject = "{AGENT}"' in p and "WRITER" not in p


@pytest.mark.parametrize("name", sorted(VARIANTS))
class TestTheOtherTwoCases:
    def test_with_no_ids_it_is_the_frame_of_before(self, name):
        """No end user and no agent: an end user named "user", as on 6.3.0, so
        "I prefer the dark theme" stays a preference of "user"."""
        for kw in _switches(name):
            p = _prompt(name, agent_id=None, **kw)
            assert p == _prompt(name, user="user", agent_id=None, **kw), kw
            assert "NO end user" not in p and "WRITER" not in p
            if kw["fixed_prefix"]:
                assert f'{END_USER_BINDING} "user"' in p

    def test_with_an_end_user_the_agent_is_not_read(self, name):
        for kw in _switches(name):
            assert _prompt(name, user="ana-77", **kw) == _prompt(name, user="ana-77", agent_id=None, **kw)


def test_the_writer_name_comes_back_as_the_handle():
    m = _Model([{"subject": "WRITER", "subject_type": "product", "predicate": "runs_tests_in",
                 "object": "Docker", "tense": "current"}])
    [f] = est.extract(m, "I always run the tests in Docker.", None, agent_id=AGENT, fixed_prefix=True)
    assert f.subject == AGENT


@pytest.mark.parametrize("function", ["extract", "extract_with_observation"])
def test_the_writer_name_as_object_comes_back_as_the_handle(function):
    """The product writes with `fixed_prefix` on: "Lucia gave me the piece"
    comes back with WRITER as the object."""
    m = _Model([{"subject": "Lucia", "subject_type": "person", "predicate": "assigns_piece_to",
                 "object": "WRITER", "tense": "current"}])
    out = getattr(est, function)(m, "Lucia gave me the piece.", None, agent_id=AGENT, fixed_prefix=True)
    [f] = out if function == "extract" else out.facts
    assert (f.subject, f.object) == ("Lucia", AGENT)


def test_with_no_ids_the_first_person_is_still_user():
    m = _Model([{"subject": "END_USER", "subject_type": "person", "predicate": "prefers",
                 "object": "dark theme", "tense": "current"}])
    [f] = est.extract(m, "I prefer the dark theme.", None, fixed_prefix=True)
    assert f.subject == "user"


class TestTheObservation:
    @pytest.mark.parametrize("faithful", [False, True])
    def test_it_is_the_faithful_one_whatever_the_switch(self, faithful):
        """3.1.0's faithful rule (Maynez et al., ACL 2020) with nobody to tell
        it around: the second sentence, the caption and the time, verbatim."""
        p = _prompt("observation", faithful=faithful)
        assert "that says what this memory states,\n  with its own subject" in p
        assert est._FAITHFUL_OBSERVATION[1][1] in p
        assert "Do NOT extract general knowledge" not in p
        assert "how something is done or works is a fact\n  about the thing it describes" in p


class TestTheNet:
    """keep_grounded: "user" passes only when the memory is an end user's
    (a memory of no ids is read as the end user "user"'s, as before)."""

    def _user_fact(self, subject="user", obj="deploy procedure"):
        return [{"subject": subject, "subject_type": "person", "predicate": "follows_procedure",
                 "object": obj, "tense": "current"}]

    def test_in_an_agents_memory_a_user_the_text_does_not_name_is_dropped(self):
        m = _Model(self._user_fact())
        assert est.extract(m, "The deploy procedure is run from the server.", None, agent_id="ops-agent") == []

    def test_as_an_object_too(self):
        m = _Model([{"subject": "deploy procedure", "predicate": "is_followed_by", "object": "user",
                     "tense": "current"}])
        assert est.extract(m, "The deploy procedure is run from the server.", None, agent_id="ops-agent") == []

    def test_a_user_the_text_names_is_kept_like_any_name(self):
        m = _Model(self._user_fact(obj="deploy procedure"))
        [f] = est.extract(m, "The user follows the deploy procedure.", None, agent_id="ops-agent")
        assert f.subject == "user"

    @pytest.mark.parametrize("text", [
        "The user_id of the deploy procedure is kept in the vault.",
        "The users of the deploy procedure are the operators.",
        "The username of the deploy procedure is ops.",
    ])
    def test_user_inside_another_word_is_not_user(self, text):
        """`argument_grounded` matches a substring; in an agent's memory the
        net wants "user" as a whole word (review of 7 Nov 2026)."""
        m = _Model(self._user_fact())
        assert est.extract(m, text, None, agent_id="ops-agent") == []
        m = _Model([{"subject": "deploy procedure", "predicate": "is_followed_by", "object": "user",
                     "object_is_literal": True, "tense": "current"}])
        assert est.extract(m, text, None, agent_id="ops-agent") == [], "a literal user is the same stand-in"

    @pytest.mark.parametrize("text", ["The User follows the deploy procedure.",
                                      "Every user follows the deploy procedure."])
    def test_user_as_a_whole_word_is_kept(self, text):
        m = _Model(self._user_fact())
        [f] = est.extract(m, text, None, agent_id="ops-agent")
        assert f.subject == "user"

    def test_the_whole_word_gate_is_only_the_agents(self):
        """An end user's memory, and one of no ids, ground as before: there
        "user" passes anyway, and every other name keeps its substring check."""
        m = _Model(self._user_fact())
        text = "The user_id of the deploy procedure is kept in the vault."
        assert [f.subject for f in est.extract(m, text, None)] == ["user"]
        assert [f.subject for f in est.extract(m, text, "ana-77")] == ["user"]

    def test_the_agent_passes_as_subject_and_object(self):
        m = _Model([{"subject": AGENT, "predicate": "runs_tests_in", "object": "Docker", "tense": "current"},
                    {"subject": "Lucia", "predicate": "assigns_piece_to", "object": AGENT, "tense": "current"}])
        facts = est.extract(m, "I run the tests in Docker. Lucia gave me the piece.", None, agent_id=AGENT)
        assert {(f.subject, f.object) for f in facts} == {(AGENT, "Docker"), ("Lucia", AGENT)}

    def test_another_agent_does_not_pass(self):
        m = _Model([{"subject": "review-agent", "predicate": "runs_tests_in", "object": "Docker",
                     "tense": "current"}])
        assert est.extract(m, "I run the tests in Docker.", None, agent_id=AGENT) == []

    def test_with_no_ids_user_still_passes(self):
        """6.3.0's reading for a memory that names nobody: "user" is its end user."""
        m = _Model(self._user_fact())
        [f] = est.extract(m, "The deploy procedure is run from the server.", None)
        assert f.subject == "user"

    @pytest.mark.parametrize("blank", ["", " ", "  "])
    def test_an_id_of_spaces_is_an_end_user_even_with_an_agent(self, blank):
        """A probe of the review of 7 Nov 2026: the product accepts and stores such an
        id, and 6.3.0 read it as the end user "user". Read as no end user,
        the agent's frame would drop the end user's fact."""
        m = _Model(self._user_fact())
        [f] = est.extract(m, "The deploy procedure is run from the server.", blank, agent_id="ops-agent")
        assert f.subject == "user"
        p = _prompt("observation", user=blank, fixed_prefix=True)
        assert f'{END_USER_BINDING} "user"' in p and "NO end user" not in p

    def test_with_an_end_user_user_still_passes(self):
        m = _Model(self._user_fact())
        [f] = est.extract(m, "I follow the deploy procedure.", "ana-77")
        assert f.subject == "user"

    def test_the_net_reads_its_owner(self):
        facts = est.read_facts(self._user_fact(), "x deploy procedure")
        assert est.keep_grounded(facts, "x deploy procedure", AGENT, end_user=False) == []
        assert est.keep_grounded(facts, "x deploy procedure", "user", end_user=True) != []
