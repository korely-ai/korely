"""In an agent's memory with no end user, the judge's nearest facts are the facts of one subject (memory_core 6.7.0).

The finding of 7 Nov 2026, on the memory of a team of agents reloaded from
106 memories (422 facts, every one written with an `agent_id` and no
`user_id`), replayed for free with the store's embedding model and the real
judge's verdicts taken as they were: 32 facts were closed, 21 of them wrongly
("the SDK repository has_license MIT" closed by "the license is_currently
Apache 2.0", a rule on the push windows closed by an agent's way of pushing,
two disk thresholds that are both true). 29 of the 32 pairs reached the judge
only through stage 1b of `memory_core.candidates.for_the_judge`, the ten
facts nearest by meaning of ANY subject, and 19 of the 21 wrong ones; every
write filled the ten places under the cutoff. Stage 1b looks past the subject
because, in ONE person's memory, the extractor writes one entity several ways
("Maria", "the user", "the runner"). A memory with no end user is not one
person's story: the code already switched stage 1a-bis off there ("the global
scope is too broad to assume one relation family is one entity") and left 1b
on.

So, from 6.7.0, a fact of an agent's memory with no end user (written with an
`agent_id` and no `user_id`: the test of the extraction's frame of 6.4.0,
Mem0's `is_agent_scoped`) is weighed, in stage 1b, only against the facts of
its own canonical subject (`candidates.about_the_subject`, the one rule of
"the same subject"), and enters stage 1b of another fact only about that
fact's subject: the rule is the same seen from either fact of a pair, so the
store does not depend on which of two memories was written first. Stages 1a,
1c and 1d are as they were. A memory with a `user_id` is untouched, statement
for statement (LongMemEval and LoCoMo always write with one), and so is a
memory written with no ids at all, pair for pair, as long as no agent writes
without a `user_id` in the same project.

memory_core 6.9.0 (approved on 12 Nov 2026): the same rule for every memory
with no end user, an agent's or not (`owner.keeps_to_its_subject`), and the
entity in either role (`candidates.about_the_subject`). Measured on the team's memory
written with no ids (12 Nov 2026, three runs): 7 of its 19 wrong closures
joined two subjects met by meaning, none of them in the same memory written
with an `agent_id`. The tests that said a memory with no ids still meets
across subjects are turned below, and a memory with a `user_id` stays
statement for statement the one of 6.6.0.

The worlds below are small copies of the cases of that replay, told with
other names. The judge is a fake that says "contradiction" exactly where the
real judge said it; the vectors are fakes that put each case's facts at
distance 0 and the cases apart. No model, no network (tests/conftest.py).
The turned tests fail on memory_core 6.6.0 (origin/main 4e62afa9).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import event

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import vectors
from korely_agent.services.facts import contradiction as contra
from korely_agent.services.facts import entities as ent
from korely_agent.services.facts import service
from korely_agent.services.facts.service import fact_text
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import candidates
from korely_agent.services.memory_core.fact_extractor import ExtractedFact


def day(d: int) -> datetime:
    return datetime(2026, 11, d, tzinfo=timezone.utc)


def axis(k: int) -> list:
    """A unit vector of its own: two facts on one axis are at distance 0, on
    two axes at distance 1, past every cutoff of stage 1b."""
    v = [0.0] * EMBEDDING_DIM
    v[k] = 1.0
    return v


class World:
    """What the fakes need: the vector of each fact's text, and the pairs the
    real judge said contradict, as (new fact, candidate), each fact told by
    (subject, object)."""

    def __init__(self):
        self.vector_of = {}
        self.yes = set()
        self.shown = []  # (new fact, sorted candidates), one per judgement

    def place(self, subject, predicate, obj, k):
        self.vector_of[fact_text(subject, subject, predicate, obj, obj)] = axis(k)


@pytest.fixture
def world(monkeypatch):
    w = World()

    def vector(text):
        return list(w.vector_of[text])

    def key(f):
        return (f.subject_label, f.object_label)

    def one(cands, new_subject, new_predicate_raw, new_object, *a, **k):
        new = (new_subject, new_object)
        w.shown.append((new, sorted(key(c) for c in cands)))
        return [c.id for c in cands if (new, key(c)) in w.yes], {}

    def batch(groups, db, tenant_id, model_name=None):
        out = {}
        for g in groups:
            new = key(db.get(AgentFact, uuid.UUID(g["new_id"])))
            w.shown.append((new, sorted(key(c) for c in g["candidates"])))
            out[g["new_id"]] = [c.id for c in g["candidates"] if (new, key(c)) in w.yes]
        return out

    monkeypatch.setattr(service, "fact_vector", vector)
    monkeypatch.setattr(contra, "_classify", one)
    monkeypatch.setattr(contra, "_classify_batch", batch)
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
    return w


def remember(db, tenant, subject, predicate, obj, *, agent=None, user=None, when=None):
    """One memory with one fact, written as the extraction writes it (the
    extraction's provenance, the write's judgement: batched by default)."""
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=user, agent_id=agent,
                    content=f"{subject} {predicate} {obj}")
    db.add(m)
    db.commit()
    service._persist_extracted(db, tenant.id, memory=m, valid_from=when, extracted=[
        ExtractedFact(subject=subject, predicate_raw=predicate, object=obj, confidence=0.9)])
    return m


def closed(db, tenant) -> set:
    return {(f.subject_label, f.object_label) for f in db.query(AgentFact).filter(
        AgentFact.tenant_id == tenant.id, AgentFact.invalid_at.isnot(None))}


# 1. The team's memory ---------------------------------------------------------
# Each case: its facts in the order they were written (agent, subject,
# relation, object, day), and the pair the real judge said contradict. The
# numbers are the rows of the replay of 7 Nov 2026.

TEAM = [
    # Wrong, two subjects (row 24): two repositories.
    ("agent-b", "the SDK repository", "has_license", "MIT", 6),
    ("agent-a", "the license", "is_currently", "Apache 2.0", 7),
    # Wrong (row 26): two thresholds, both true.
    ("agent-a", "agent-a", "clears_disk_space_below", "8 GB", 6),
    ("agent-c", "an acceptance run", "is_invalid_with_disk_under", "4 GB", 7),
    # Wrong (row 25): the rule, and one agent's way of pushing.
    ("agent-a", "agent-a", "prohibits_push_during", "weekday office hours", 6),
    ("agent-d", "agent-d", "performs_push", "forced and outside the time slots", 7),
    # Wrong (row 16): the state of some articles, the rule on a new post's state.
    ("agent-e", "blog articles", "has_status", "proposed", 6),
    ("agent-a", "a post", "has_initial_status", "Proposed", 7),
    # Wrong (row 22): the scraper stays.
    ("agent-e", "Reddit", "uses_scraper", "a community scraper", 6),
    ("agent-e", "agent-e", "has_no_skill_for", "Reddit", 7),
    # Wrong (row 1): two keys.
    ("agent-a", "adoption", "revokes_permission", "admin", 6),
    ("agent-a", "the team profile key", "has_permission", "admin", 7),
    # Right, two spellings of one thing (rows 10-13): the price, kept open.
    ("agent-b", "Cloud", "has_version", "1.0.11", 5),
    ("agent-a", "Korely Cloud", "runs_version", "1.0.12", 7),
    # Right, one subject (row 19): kept.
    ("agent-f", "the engine", "has_version", "6.3.0", 5),
    ("agent-f", "the engine", "has_version", "6.4.0", 6),
    # Wrong, one subject (row 17): kept, the question of the judge (another piece).
    ("agent-b", "the product repository", "is_status", "private", 6),
    ("agent-a", "the product repository", "has_status", "suspended", 7),
]
# Row 7, the other road: the rule is written after a plan that begins later,
# and the rule's judge, told they contradict, made the NEW fact history
# (`judge.close`: the candidate began after it).
PLAN = ("agent-a", "the team memory", "rebuild_scheduled", "after the release", 7)
RULE = ("agent-a", "the main database server", "must_never_be", "resized, deleted or recreated", 5)


def write_the_team(db, tenant, w, user=None, agents=True):
    """`agents=False` (6.9.0): the same memories written with no ids."""
    if not agents:
        team = [(None, *t[1:]) for t in TEAM]
        plan, rule = (None, *PLAN[1:]), (None, *RULE[1:])
        return _write(db, tenant, w, user, team, plan, rule)
    return _write(db, tenant, w, user, TEAM, PLAN, RULE)


def _write(db, tenant, w, user, TEAM, PLAN, RULE):
    for i in range(0, len(TEAM), 2):
        older, newer = TEAM[i], TEAM[i + 1]
        for agent, s, p, o, _d in (older, newer):
            w.place(s, p, o, i // 2)
        w.yes.add(((newer[1], newer[3]), (older[1], older[3])))
    w.place(*PLAN[1:4], len(TEAM))
    w.place(*RULE[1:4], len(TEAM))
    w.yes.add(((RULE[1], RULE[3]), (PLAN[1], PLAN[3])))
    for agent, s, p, o, d in TEAM[0::2]:
        remember(db, tenant, s, p, o, agent=agent, user=user, when=day(d))
    remember(db, tenant, *PLAN[1:4], agent=PLAN[0], user=user, when=day(PLAN[4]))
    for agent, s, p, o, d in TEAM[1::2]:
        remember(db, tenant, s, p, o, agent=agent, user=user, when=day(d))
    remember(db, tenant, *RULE[1:4], agent=RULE[0], user=user, when=day(RULE[4]))


@pytest.fixture(params=[True, False], ids=["batched", "one-by-one"])
def judged(request, monkeypatch):
    """Both judgements of an extraction's write: the batched one (the
    default) and the one fact at a time (`_BATCHED_JUDGE` off)."""
    monkeypatch.setattr(service, "_BATCHED_JUDGE", request.param)
    return request.param


@pytest.mark.parametrize("agents", [True, False], ids=["with-agent-ids", "with-no-ids"])
def test_the_team_memory_closes_only_what_one_subject_says(db, tenant, world, judged, agents):
    """Turned: on 6.6.0 every pair below was shown to the judge and closed,
    the eight wrong ones included; written with no ids, until 6.8.1 too.
    6.9.0 lets a fact meet its entity in the other role
    (`candidates.about_the_subject`, Graphiti's two nodes): "Reddit uses a
    community scraper" and "agent-e has no skill for Reddit" share Reddit,
    and the judge's wrong verdict of row 22 closes again. That is the price
    of the other role, counted on the replay of 7 Nov 2026 (the real judge's
    verdicts as given, 32 closures): 1 right and 2 wrong closures come back.
    Turned again in 6.28.0 (a product decision of 26 Nov 2026,
    `owner.judged_within_its_agent`): with agent ids, "the product
    repository is private" (agent-b) is no longer closed by agent-a's
    "suspended"; an agent closes only its own facts. Written with no ids,
    every fact is of one namespace and nothing changes."""
    write_the_team(db, tenant, world, agents=agents)
    want = {("the engine", "6.3.0"), ("Reddit", "a community scraper")}
    if not agents:
        want.add(("the product repository", "private"))
    assert closed(db, tenant) == want


@pytest.mark.parametrize("agents", [True, False], ids=["with-agent-ids", "with-no-ids"])
def test_no_fact_of_another_subject_reaches_the_judge_of_an_agents_memory(db, tenant, world,
                                                                         judged, agents):
    """Turned: on 6.6.0 the judge read, at every write, the nearest facts of
    any subject (with no ids, until 6.8.1). Since 6.9.0 the facts of the same
    entity in either role, and nothing else."""
    write_the_team(db, tenant, world, agents=agents)
    assert world.shown, "the facts of one subject still reach the judge"
    for (new_subject, new_object), cands in world.shown:
        for s, o in cands:
            # 6.9.0: its subject, or its subject as the object, or its object
            # as the subject (the other role), never another entity.
            assert new_subject in (s, o) or s == new_object, (new_subject, new_object, cands)


def test_with_a_user_id_the_same_writes_close_as_before(db, tenant, world, judged):
    """The same memories with a `user_id`: every pair reaches the judge, as
    on 6.6.0, and every one the judge names is closed (the rule of the
    end user's memory: one person's story, written several ways)."""
    write_the_team(db, tenant, world, user="u-42")
    assert closed(db, tenant) == {older[1:4:2] for older in TEAM[0::2]} | {RULE[1:4:2]}


def test_two_spellings_are_two_subjects_until_an_alias_unites_them(db, tenant, world):
    """The price (docs/guide/limits.md): "Cloud" and "Korely Cloud" are two
    subjects, so the newer version no longer closes the older. An alias in
    the namespace of the agent that writes ("Korely Cloud" is "Cloud" for
    agent-a) makes them one subject, and the judge reads the pair again."""
    # The text of a vector is the canonical one (`fact_text`): with the alias,
    # agent-a's "Korely Cloud" is read as "Cloud".
    for s, p, o in (("Cloud", "has_version", "1.0.11"), ("Korely Cloud", "runs_version", "1.0.12"),
                    ("Cloud", "runs_version", "1.0.13")):
        world.place(s, p, o, 0)
    world.yes |= {(("Korely Cloud", "1.0.12"), ("Cloud", "1.0.11")),
                  (("Korely Cloud", "1.0.13"), ("Cloud", "1.0.11"))}
    # One agent (6.28.0: another agent's fact would never be its candidate).
    remember(db, tenant, "Cloud", "has_version", "1.0.11", agent="agent-a", when=day(5))
    remember(db, tenant, "Korely Cloud", "runs_version", "1.0.12", agent="agent-a", when=day(7))
    assert closed(db, tenant) == set()
    ent.declare_alias(db, tenant.id, None, None, "agent-a", "Korely Cloud", "Cloud")
    db.commit()
    remember(db, tenant, "Korely Cloud", "runs_version", "1.0.13", agent="agent-a", when=day(8))
    assert ("Cloud", "1.0.11") in closed(db, tenant)


# 2. Who meets whom: the rule seen from either fact ------------------------------

def _pair(db, tenant, world, first, second):
    """Two facts near by meaning (one axis), each written by its own (agent,
    user), the second told by the judge to close the first."""
    (a1, u1, s1, o1), (a2, u2, s2, o2) = first, second
    world.place(s1, "uses", o1, 0)
    world.place(s2, "uses", o2, 0)
    world.yes.add(((s2, o2), (s1, o1)))
    remember(db, tenant, s1, "uses", o1, agent=a1, user=u1, when=day(5))
    remember(db, tenant, s2, "uses", o2, agent=a2, user=u2, when=day(6))
    return closed(db, tenant)


@pytest.mark.parametrize("first,second", [
    (("agent-a", None, "the gateway", "the old cluster"), (None, None, "the user", "the new cluster")),
    ((None, None, "the user", "the old cluster"), ("agent-a", None, "the gateway", "the new cluster")),
    (("agent-b", None, "the gateway", "the old cluster"), ("agent-a", None, "the user", "the new cluster")),
], ids=["an-agent-then-no-ids", "no-ids-then-an-agent", "two-agents"])
def test_two_subjects_do_not_meet_when_either_fact_is_an_agents(db, tenant, world, judged, first,
                                                                 second):
    """Turned: on 6.6.0 the second write, near the first by meaning, closed
    it. The rule is the same in either order: a write with no ids, in a
    project where agents write without a `user_id`, does not close an
    agent's fact of another subject, nor an agent's write a fact with no
    ids (the first two cases are the ones the new fact's side and the
    candidate's side of the rule decide alone)."""
    got = _pair(db, tenant, world, first, second)
    assert got == set() and world.shown == []


def test_two_writes_with_no_ids_meet_only_about_one_subject(db, tenant, world, judged):
    """Turned in 6.9.0: until 6.8.1 a memory with no ids met facts of any
    subject by meaning (one person's, as the extraction frames it since
    6.4.0). With no end user the store cannot know it is one person's
    story, so two subjects no longer meet; the price is the one of 6.7.0,
    two names of one thing ("Acme Corp", "Acme") are two subjects until an
    alias unites them."""
    got = _pair(db, tenant, world, (None, None, "Acme Corp", "the Business plan"),
                (None, None, "Acme", "the Starter plan"))
    assert got == set() and world.shown == []


@pytest.mark.parametrize("who", [(None, None), ("agent-a", None), (None, "u-42")],
                         ids=["no-ids", "an-agent", "a-user"])
def test_one_entity_in_two_roles_still_meets(db, tenant, world, judged, who):
    """6.9.0, a review of 12 Nov 2026: "Acme employs user", then "user
    works_at Globex". One person, first as the object, then as the subject:
    with no ids the first 6.9.0 text no longer showed the pair (6.7.0 neither,
    for an agent's memory). Graphiti weighs the edges of both nodes of a new
    edge, in either position (`candidates.about_the_subject`)."""
    agent, user = who
    for s_, p_, o_ in (("Acme", "employs", "user"), ("user", "works_at", "Globex")):
        world.place(s_, p_, o_, 0)
    world.yes.add((("user", "Globex"), ("Acme", "user")))
    remember(db, tenant, "Acme", "employs", "user", agent=agent, user=user, when=day(2))
    remember(db, tenant, "user", "works_at", "Globex", agent=agent, user=user, when=day(5))
    assert closed(db, tenant) == {("Acme", "user")}


@pytest.mark.parametrize("agents", [(None, None), ("agent-a", "agent-b")], ids=["no-ids", "two-agents"])
def test_two_subjects_that_only_share_an_object_do_not_meet(db, tenant, world, judged, agents):
    """Left out on purpose: two facts that only share their object are two
    subjects, the shape of the closures between subjects this rule removed."""
    got = _pair(db, tenant, world, (agents[0], None, "the gateway", "Linear"),
                (agents[1], None, "the billing service", "Linear"))
    assert got == set() and world.shown == []


def test_two_writes_with_no_ids_still_meet_about_one_subject(db, tenant, world, judged):
    got = _pair(db, tenant, world, (None, None, "Acme", "the Business plan"),
                (None, None, "Acme", "the Starter plan"))
    assert got == {("Acme", "the Business plan")}


@pytest.mark.parametrize("agents", [(None, None), ("agent-a", "agent-b"), ("agent-a", None)],
                         ids=["no-agent", "two-agents", "one-agent"])
def test_with_a_user_id_two_subjects_still_meet(db, tenant, world, judged, agents):
    """Unchanged: inside one end user, of any agent."""
    got = _pair(db, tenant, world, (agents[0], "u-42", "Acme Corp", "the Business plan"),
                (agents[1], "u-42", "Acme", "the Starter plan"))
    assert got == {("Acme Corp", "the Business plan")}


@pytest.mark.parametrize("first,closes", [("agent-a", True), ("agent-b", False)],
                         ids=["one-agent", "two-agents"])
def test_two_agents_meet_about_one_subject_only_inside_one_agent(db, tenant, world, judged, first,
                                                                 closes):
    """Turned in 6.28.0. Until 6.27.0 the wall was the project's memory with
    no end user, of every agent ("another decision, not this one"): it was
    taken on 26 Nov 2026, so a change of one subject told by
    another agent no longer closes; told by the same agent it does."""
    got = _pair(db, tenant, world, (first, None, "the gateway", "the old cluster"),
                ("agent-a", None, "the gateway", "the new cluster"))
    assert got == ({("the gateway", "the old cluster")} if closes else set())


# 3. A vector that comes late ----------------------------------------------------

@pytest.fixture
def late(monkeypatch):
    seen = []

    def judge(groups, db, tenant_id, model_name=None):
        seen.extend((g["new_subject"], sorted(c.subject_label for c in g["candidates"]))
                    for g in groups)
        return {g["new_id"]: [c.id for c in g["candidates"]] for g in groups}

    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr(service, "fact_vector", lambda text: axis(0))
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
    monkeypatch.setattr(contra, "_classify_batch", judge)
    return seen


def _fact(db, tenant, subject, obj, *, when, agent, user=None, vector=True, predicate="uses"):
    """A fact as the extraction leaves it, with its vector or waiting for it.
    One relation unless told: two facts of one relation meet by stage 1a (and
    1a-bis, with an end user) at the write, so the late judgement is about
    the facts of another relation."""
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=user, agent_id=agent,
                  subject_label=subject, subject_canonical=subject, subject_type="unknown",
                  predicate_raw=predicate, predicate_canonical=predicate, object_label=obj,
                  object_canonical=obj, confidence=0.9, valid_from=when, created_at=when,
                  tense="current", model_used="a-writer",
                  embedding=axis(0) if vector else None,
                  embedding_status=vectors.DONE if vector else vectors.PENDING)
    db.add(f)
    db.commit()
    return f


@pytest.mark.parametrize("late_agent,other_agent", [
    ("agent-a", "agent-b"), ("agent-a", None), (None, "agent-a"),
], ids=["two-agents", "the-late-one-an-agents", "the-other-an-agents"])
@pytest.mark.parametrize("late_first", [True, False], ids=["late-older", "late-newer"])
def test_a_late_fact_does_not_meet_another_subject_when_either_is_an_agents(
        db, tenant, late, late_agent, other_agent, late_first):
    """`judge.after_the_vector`, the judgements the write of a fact without
    its vector could not make: the late fact against the older facts only
    meaning finds (`late-newer`), and the later facts near it against the
    late one (`late-older`), as their writes would have weighed it. Turned:
    on 6.6.0 every case met and closed one of the two."""
    when = (day(2), day(5)) if late_first else (day(5), day(2))
    gone = _fact(db, tenant, "the user", "the old cluster", when=when[0], agent=late_agent,
                 vector=False)
    other = _fact(db, tenant, "the gateway", "the new cluster", when=when[1], agent=other_agent)
    vectors.process_pending_vectors(db)
    db.expire_all()
    assert late == []
    assert db.get(AgentFact, gone.id).invalid_at is None
    assert db.get(AgentFact, other.id).invalid_at is None


@pytest.mark.parametrize("late_first", [True, False], ids=["late-older", "late-newer"])
def test_late_facts_with_no_ids_meet_only_about_one_subject(db, tenant, late, late_first):
    """Turned in 6.9.0: two facts with no ids, either of them late, of two
    subjects, are not weighed against each other (until 6.8.1 they were)."""
    when = (day(2), day(5)) if late_first else (day(5), day(2))
    _fact(db, tenant, "the user", "the old cluster", when=when[0], agent=None, vector=False)
    _fact(db, tenant, "the gateway", "the new cluster", when=when[1], agent=None)
    vectors.process_pending_vectors(db)
    assert late == []


@pytest.mark.parametrize("late_first", [True, False], ids=["late-older", "late-newer"])
def test_a_late_fact_meets_its_entity_in_the_other_role(db, tenant, late, late_first):
    """The late judgement applies the same rule: "Acme employs user" and
    "user works_at Globex", either of them late, with no ids."""
    when = (day(2), day(5)) if late_first else (day(5), day(2))
    _fact(db, tenant, "Acme", "user", when=when[0], agent=None, vector=False, predicate="employs")
    _fact(db, tenant, "user", "Globex", when=when[1], agent=None, predicate="works_at")
    vectors.process_pending_vectors(db)
    assert len(late) == 1


def test_a_late_fact_of_an_agents_memory_still_meets_its_subject(db, tenant, late):
    """Inside one agent (6.28.0: across two, tests/test_an_agent_closes_only_its_own_facts.py)."""
    old = _fact(db, tenant, "the gateway", "the old cluster", when=day(2), agent="agent-a")
    new = _fact(db, tenant, "the gateway", "the new cluster", when=day(5), agent="agent-a",
                vector=False, predicate="runs_on")
    vectors.process_pending_vectors(db)
    db.expire_all()
    assert late == [("the gateway", ["the gateway"])]
    assert db.get(AgentFact, old.id).invalidated_by_id == new.id


def test_late_facts_with_a_user_id_still_meet_across_subjects(db, tenant, late):
    """Unchanged, as the write: "Acme Corp" and "Acme" of one end user."""
    old = _fact(db, tenant, "Acme Corp", "the Business plan", when=day(2), agent="agent-a",
                user="u-42", predicate="on_plan")
    _fact(db, tenant, "Acme", "the Starter plan", when=day(5), agent="agent-b", user="u-42",
          vector=False, predicate="subscribes_to")
    vectors.process_pending_vectors(db)
    db.expire_all()
    assert late == [("Acme", ["Acme Corp"])]
    assert db.get(AgentFact, old.id).invalid_at is not None


# 4. The engine's function -------------------------------------------------------

def test_the_engine_keeps_every_stage_of_the_subject_and_drops_only_the_others(db, tenant):
    """`candidates.for_the_judge(agent_scoped=, agent_scoped_facts=)`: 1a, 1c
    and 1d as they were (1d brings another subject: the same two entities the
    other way round), 1b among the facts of the subject only."""
    def fact(s, p, o, agent="agent-a"):
        return _fact_row(db, tenant, s, p, o, agent)

    a = fact("Globex", "uses", "Jira")                       # 1a
    c = fact("Globex", "evaluated", "Linear")                # 1c
    d = fact("Linear", "serves_as_tracker_for", "Globex")    # 1d
    b = fact("Globex", "is_customer_of", "Initech")          # 1b, the subject
    fact("Umbrella", "uses", "Linear")                       # 1b, another subject
    fact("Hooli", "pays_for", "Linear", agent=None)          # 1b, another subject, no ids
    got = candidates.for_the_judge(
        db, ent.TABLES, ent.memory_scope(tenant.id, None, None), is_open=true_at(),
        exclude_id=uuid.uuid4(), subject_label="Globex", predicate_canonical="uses",
        subject_canonical="Globex", object_canonical="Linear", with_recall=False, vector=axis(0),
        named=ent.NAMED_FACTS, agent_scoped=True, agent_scoped_facts=ent.scoped_by_an_agent())
    assert {x.id for x in got} == {a.id, c.id, d.id, b.id}


def _fact_row(db, tenant, s, p, o, agent, user=None):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=user, agent_id=agent,
                  subject_label=s, subject_canonical=s, subject_type="unknown", predicate_raw=p,
                  predicate_canonical=p, object_label=o, object_canonical=o, confidence=0.9,
                  valid_from=day(2), tense="current", model_used="a-writer", embedding=axis(0),
                  embedding_status=vectors.DONE)
    db.add(f)
    db.commit()
    return f


# The agent ids of the rule: none, empty, ASCII spaces, an ideographic space
# (U+3000), a whitespace of each other kind Python strips, a name, a name with
# spaces around it.
IDS_OF_AGENTS = [None, "", "  ", "\u3000", "\u00a0\u2003\u202f", "agent-a", " agent-b "]


class _Prompts:
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt, max_tokens):
        self.prompts.append(prompt)
        return json.dumps({"facts": []})


@pytest.mark.parametrize("user", [None, "u-42", " "], ids=["no-user", "a-user", "a-user-of-spaces"])
def test_the_extraction_and_the_judge_read_whose_memory_it_is_by_one_rule(db, tenant, user):
    """The rules in one module (`memory_core.owner`). The extraction frames a
    memory as an agent's, with no end user, exactly when `owner.agent_scoped`
    and its stored form (`entities.scoped_by_an_agent`) say so: a blank
    `agent_id`, of any alphabet's spaces, is no agent (6.4.0, after a review
    of 7 Nov 2026); an end user, even one whose id is spaces, makes the
    memory theirs. Since 6.9.0 the judge keeps a fact to its subject for
    every memory with no end user, agent or not (`owner.keeps_to_its_subject`,
    stored `entities.kept_to_their_subject`); until 6.8.1 it read the
    extraction's rule."""
    from korely_agent.services.memory_core import fact_extractor, owner

    framed, scoped, kept, stored = set(), set(), set(), {}
    for i, agent in enumerate(IDS_OF_AGENTS):
        model = _Prompts()
        fact_extractor.extract(model, "The deploy runs on Fridays.", user, agent_id=agent)
        if "belongs to NO end user" in model.prompts[0]:
            framed.add(agent)
        if owner.agent_scoped(user, agent):
            scoped.add(agent)
        if owner.keeps_to_its_subject(user):
            kept.add(agent)
        stored[_fact_row(db, tenant, f"s{i}", "uses", "o", agent, user=user).id] = agent

    def rows(condition):
        return {stored[i] for (i,) in db.query(AgentFact.id).filter(
            AgentFact.tenant_id == tenant.id, condition)}

    expected = {"agent-a", " agent-b "} if user is None else set()
    assert framed == scoped == rows(ent.scoped_by_an_agent()) == expected
    assert kept == rows(ent.kept_to_their_subject()) == (set(IDS_OF_AGENTS) if user is None else set())


# 5. A memory with a user_id: statement for statement --------------------------
# LongMemEval and LoCoMo write every memory with a `user_id`
# (scripts/bench_locomo.py `Korely.add`, which bench_longmemeval.py imports):
# what their judge reads cannot move if every statement of its candidates is
# the 6.6.0 one. The fingerprints were taken by running this file on
# memory_core 6.6.0 (origin/main 4e62afa9, 7 Nov 2026). A memory with no
# ids at all keeps its pairs; its statements carry the new condition (a fact
# of an agent's memory enters only about its subject), true of no row there.

def _the_world(db, tenant, w, user, agents):
    k = 0
    for s, p, o, d in (("Acme Corp", "on_plan", "the Business plan", 2),
                       ("Acme", "subscribes_to", "the Starter plan", 3),
                       ("the runner", "personal_best", "21 minutes", 3),
                       ("Maria", "personal_best", "19 minutes", 4),
                       ("Maria", "works_at", "Globex", 4),
                       ("Globex", "employs", "Maria", 5),
                       ("Maria", "visited", "Globex HQ", 5),
                       ("the gym", "opens_at", "7 am", 6)):
        w.place(s, p, o, k % 3)
        k += 1
        remember(db, tenant, s, p, o, agent=agents[k % len(agents)], user=user, when=day(d))
    w.yes.add((("Acme", "the Starter plan"), ("Acme Corp", "the Business plan")))
    w.yes.add((("Maria", "19 minutes"), ("the runner", "21 minutes")))
    # And a fact whose vector comes late, judged when it arrives.
    late = _fact(db, tenant, "Acme Ltd", "the Pro plan", when=day(7), agent=agents[0], user=user,
                 vector=False)
    w.vector_of[fact_text("Acme Ltd", "Acme Ltd", "uses", "the Pro plan", "the Pro plan")] = axis(0)
    return late


@pytest.fixture
def statements(db, monkeypatch):
    """The statements on the facts table run while the judge's candidates
    are gathered, at the write and when a vector arrives, in order."""
    seen, inside = [], [0]
    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)

    def listen(conn, cursor, statement, parameters, context, executemany):
        text = " ".join(statement.split())
        if inside[0] and text.startswith("SELECT") and "FROM agent_facts" in text:
            seen.append(text)

    def around(fn):
        def wrapped(*a, **k):
            inside[0] += 1
            try:
                return fn(*a, **k)
            finally:
                inside[0] -= 1
        return wrapped

    monkeypatch.setattr(contra, "_stage1_candidates", around(contra._stage1_candidates))
    monkeypatch.setattr(contra, "judge_after_the_vector", around(contra.judge_after_the_vector))
    event.listen(engine, "before_cursor_execute", listen)
    yield seen
    event.remove(engine, "before_cursor_execute", listen)


def _fingerprint(items) -> str:
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode()).hexdigest()


def _run(db, tenant, world, user, agents, monkeypatch):
    _the_world(db, tenant, world, user, agents)
    # Only now: with an embedder configured the writes would ask the
    # vocabulary's embedder for the relations, and the suite has no key.
    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    vectors.process_pending_vectors(db)
    # In no order: the later facts of a late judgement at one distance come in
    # the order the database finds them.
    return sorted([list(new), [list(c) for c in cands]] for new, cands in world.shown)


# Taken on 6.6.0: the pairs the judge read, by world (the same batched and one
# by one), and the 65 statements of the candidates of the memory with a
# user_id (the same batched and one by one, with and without agent ids).
PAIRS_ON_6_6_0 = {
    "u-42": "33a60374114c081adf1ad65ee1d83e70bad77a50c02107428e083efe913672e1",
    None: "cab89e86dcabd31665d62f59b849f78382bea2b4ea99a04815e9b8678bbfa098",
}
STATEMENTS_WITH_A_USER_ID_ON_6_6_0 = "233b8fdde2d7c00f73c886bef21a7a2fc192fb4693425bdc19dc7598520512df"


@pytest.mark.parametrize("agents", [(None,), ("agent-a", "agent-b")], ids=["no-agent", "two-agents"])
def test_with_a_user_id_every_statement_and_pair_is_the_one_of_6_6_0(db, tenant, world, judged,
                                                                    statements, monkeypatch,
                                                                    agents):
    pairs = _run(db, tenant, world, "u-42", agents, monkeypatch)
    assert _fingerprint(pairs) == PAIRS_ON_6_6_0.get("u-42"), (judged, _fingerprint(pairs), pairs)
    assert _fingerprint(statements) == STATEMENTS_WITH_A_USER_ID_ON_6_6_0, (
        judged, _fingerprint(statements), len(statements))


# 6.9.0: with no end user every fact keeps to its subject, with an agent or
# without, so the world written with no ids reads the pairs the world written
# by two agents reads (the 6.7.0 rule, unchanged for agents). On 6.6.0-6.8.1
# the world with no ids read the pairs of PAIRS_ON_6_6_0[None].
PAIRS_WITH_NO_END_USER_ON_6_9_0 = "0ff7bae725052116fb6cad6b271ff25ea31c7a92424cce9f44612cfba594d4c1"
# 6.28.0: with no end user the facts of two agents no longer meet, so the
# world written by two agents reads one pair, of agent-b's two facts of Maria
# ("Maria visited Globex HQ" against "Maria works_at Globex").
PAIRS_OF_TWO_AGENTS_ON_6_28_0 = "e0117e509ed2016a8a4142443bf89d9ce134b80e55cd7731ce9f3bf9e41c4a85"


@pytest.mark.parametrize("agents", [(None,), ("agent-a", "agent-b")], ids=["no-ids", "two-agents"])
def test_with_no_end_user_ids_or_agents_read_the_same_pairs(db, tenant, world, judged, statements,
                                                            monkeypatch, agents):
    pairs = _run(db, tenant, world, None, agents, monkeypatch)
    assert _fingerprint(pairs) != PAIRS_ON_6_6_0.get(None)
    want = PAIRS_WITH_NO_END_USER_ON_6_9_0 if len(agents) == 1 else PAIRS_OF_TWO_AGENTS_ON_6_28_0
    assert _fingerprint(pairs) == want, (judged, agents, _fingerprint(pairs), pairs)
