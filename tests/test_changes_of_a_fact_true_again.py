"""A fact true again is no change for a reader (services/facts/changes.py,
`_true_again`).

The store never reopens a fact. Given back after the deletion of the memory
that closed it (services/facts/given_back.py) it is a new open row of the
same triple, F', and F stays closed as it was learned; the same triple told
again is a new row too. A reader handed F holds what is true now: no notice
while F' is true (review of piece 3, 9 Nov 2026: it was told as "no longer
true, now: nothing"). Through the doors a caller uses: the memories, a read
that names its reader, the deletion, the read after it. A plan that comes
true is a change still: the reader had it as not done.
"""
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.db.models import AgentFact, AgentReadMark
from korely_agent.services import read_marks
from korely_agent.services.facts import changes
from korely_agent.services.ids import fact_public_id
from korely_agent.services.sentinels import EVERY_END_USER
from tests.deletion_world import Fact
from tests.test_deleting_a_memory_gives_back_what_it_closed import (
    NEW, OLD, _delete, _open, _paris, _zanzibar)

# the `world` fixture
pytest_plugins = ["tests.deletion_world"]

READER = "planner"
MILAN = "Now I live in Milan."


def _told(w):
    """The changes a read that names the reader tells: (fact id, the fact that
    holds now or None), and how many more."""
    r = w.client.get("/v1/context", params={"query": "Where do I live?", "user_id": "u1",
                                            "reader": READER})
    assert r.status_code == 200, r.text
    out = r.json()
    return [(c["fact_id"], c["now"]["fact_id"] if c["now"] else None)
            for c in out["changed"]], out["changed_more"]


def _handed_paris(w, db):
    """Paris (memory P), read by the reader, then Zanzibar (memory Z) closing
    it; Z's memory id and Paris's row."""
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(OLD)
    r = w.client.get("/v1/facts", params={"user_id": "u1", "reader": READER})
    assert r.status_code == 200, r.text
    (paris,) = _paris(db)
    assert db.query(AgentReadMark).filter_by(fact_id=paris.id).count() == 1
    w.stale = ("Paris",)
    z = w.write(NEW)
    w.stale = ()
    db.expire_all()
    assert db.get(AgentFact, paris.id).invalid_at is not None
    return z, paris


@pytest.mark.parametrize("erase", [False, True], ids=["forget", "erase"])
def test_a_fact_given_back_is_no_change(world, db, erase):
    w = world
    z, paris = _handed_paris(w, db)
    assert _delete(w, z, erase)["facts_reopened"] == 1
    db.expire_all()
    assert "Paris" in _open(db)
    assert db.get(AgentFact, paris.id).invalid_at is not None, "F stays closed: F' is new"
    assert _told(w) == ([], 0), "Paris is true again: what the reader holds holds"


def test_given_back_and_ended_again_is_told(world, db):
    """F' ends later (Milan): the reader, handed Paris and nothing since, is
    told Paris no longer holds, without Milan: what holds now is read down
    F's own chain, and nothing links F' to F (the guide's limits)."""
    w = world
    z, paris = _handed_paris(w, db)
    assert _delete(w, z, False)["facts_reopened"] == 1
    w.reads[MILAN] = [Fact("u1", "lives_in", "Milan")]
    w.stale = ("Paris",)
    w.write(MILAN)
    w.stale = ()
    assert "Paris" not in _open(db)
    assert _told(w) == ([(fact_public_id(paris.id), None)], 0)


def test_before_the_deletion_it_is_a_change(world, db):
    """The same world without the deletion: Paris replaced by Zanzibar is
    told, with what holds now."""
    w = world
    _z, paris = _handed_paris(w, db)
    (g,) = _zanzibar(db)
    r = w.client.get("/v1/context", params={"query": "Where do I live?", "user_id": "u1",
                                            "reader": READER})
    assert r.status_code == 200, r.text
    (c,) = r.json()["changed"]
    assert c["fact_id"] == fact_public_id(paris.id)
    assert c["now"]["fact_id"] == fact_public_id(g.id) and g.invalid_at is None


def test_a_plan_come_true_is_a_change(db, tenant):
    """A plan (tense 'planned', "[planned, not done]" in a line) that comes
    true ends where the fact begins (memory_core 3.0.0): a row of the same
    triple, true now, but the reader had it as not done."""
    from korely_agent.services import projects as project_service

    project = project_service.get_or_create_default_project(db, tenant.id)
    start = datetime.now(timezone.utc) - timedelta(days=1)
    plan, done = (AgentFact(tenant_id=tenant.id, project_id=project.id, end_user_id="u1",
                            subject_label="Maria", predicate_raw="moves to",
                            predicate_canonical="moves_to", object_label="Milan",
                            valid_from=start, tense=tense, model_used="gemini-test")
                  for tense in ("planned", "current"))
    db.add(plan)
    db.commit()
    read_marks.mark(tenant.id, project.id, READER, [plan.id])
    plan.invalid_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    done.valid_from = plan.invalid_at
    db.add(done)
    db.flush()
    plan.invalidated_by_id = done.id
    db.commit()
    db.expire_all()
    (c,), more = changes.changed_since(db, tenant.id, project.id, READER, EVERY_END_USER)
    assert c["fact_id"] == fact_public_id(plan.id) and more == 0
    assert c["now"]["fact_id"] == fact_public_id(done.id)


@pytest.mark.parametrize("where", ["same_memory", "another_agent", "another_end_user",
                                   "another_project"])
def test_true_again_in_the_facts_own_memory_only(db, tenant, where):
    """The row that holds the triple again must be of the fact's own memory,
    as the one-open-row index reads one (account, project, end user, agent):
    the same triple open in another agent's, person's or project's memory
    leaves the reader's fact ended, and told. Its names are compared as the
    index compares them, a NULL canonical name read as its label (F' writes
    the label where F had NULL, given_back `_continuation`)."""
    from korely_agent.services import projects as project_service

    home = project_service.get_or_create_default_project(db, tenant.id)
    other = project_service.create_project(db, tenant_id=tenant.id, name="other")
    start = datetime.now(timezone.utc) - timedelta(days=1)

    def row(**kw):
        f = AgentFact(**{"tenant_id": tenant.id, "project_id": home.id, "end_user_id": "u1",
                         "agent_id": "a", "subject_label": "Maria", "predicate_raw": "lives in",
                         "predicate_canonical": "lives_in", "object_label": "Rome",
                         "valid_from": start, "model_used": "gemini-test", **kw})
        db.add(f)
        db.commit()
        return f

    f = row(subject_canonical=None, object_canonical=None)
    read_marks.mark(tenant.id, home.id, READER, [f.id])
    f.invalid_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    elsewhere = {"same_memory": {}, "another_agent": {"agent_id": "b"},
                 "another_end_user": {"end_user_id": "u2"},
                 "another_project": {"project_id": other.id}}[where]
    row(subject_canonical="maria", object_canonical="ROME", **elsewhere)
    db.expire_all()
    told, _more = changes.changed_since(db, tenant.id, home.id, READER, EVERY_END_USER,
                                        consume=False)
    assert [c["fact_id"] for c in told] == ([] if where == "same_memory"
                                            else [fact_public_id(f.id)])
