"""Deleting a project whose facts the default project already holds.

Deleting a project without `erase` moves its facts to the default project, and
an open fact whose triple was already open there broke the unique index on the
open triple: the delete answered 500 and nothing moved (round 13). The two are
one fact said twice, so they merge by memory_core's rule for every duplicate
(`merge_into`): the older row stays, the sources join, whoever pointed at the
loser points at the keeper, and nothing is invalidated, because nothing
stopped being true.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.db.models import AgentFact
from korely_agent.services import projects as project_service


NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def default(db, tenant):
    return project_service.get_or_create_default_project(db, tenant.id)


@pytest.fixture
def gamma(db, tenant, default):
    return project_service.create_project(db, tenant_id=tenant.id, name="Gamma")


def fact(db, tenant, project, *, obj="Priority Support plan", agent=None, sources=(),
         created=NOW, invalid_at=None, invalidated_by=None):
    f = AgentFact(
        id=uuid.uuid4(), tenant_id=tenant.id, project_id=project.id, end_user_id="c4821",
        agent_id=agent, subject_label="c4821", subject_canonical="c4821",
        predicate_raw="subscribes_to", predicate_canonical="subscribes_to",
        object_label=obj, object_canonical=obj,
        valid_from=NOW - timedelta(days=200), invalid_at=invalid_at,
        invalidated_by_id=invalidated_by, confidence=0.9,
        source_memory_ids=list(sources) or None, observation_count=max(1, len(sources)),
        created_at=created,
    )
    db.add(f)
    db.commit()
    return f


def open_in(db, tenant, project):
    db.expire_all()
    return (db.query(AgentFact)
              .filter(AgentFact.tenant_id == tenant.id, AgentFact.project_id == project.id,
                      AgentFact.invalid_at.is_(None))
              .all())


class TestTheSameFactInBothProjects:
    def test_the_delete_succeeds_and_one_fact_is_left(self, db, tenant, default, gamma):
        older = fact(db, tenant, default, sources=["m-default"], created=NOW - timedelta(days=3))
        fact(db, tenant, gamma, sources=["m-gamma"], created=NOW)

        out = project_service.delete_project(db, tenant.id, gamma)

        assert out["facts_merged"] == 1
        left = open_in(db, tenant, default)
        assert [f.id for f in left] == [older.id], "the older row is the one that stays"
        assert sorted(left[0].source_memory_ids) == ["m-default", "m-gamma"]
        assert left[0].observation_count == 2

    def test_the_older_one_stays_even_when_it_was_in_the_deleted_project(
            self, db, tenant, default, gamma):
        fact(db, tenant, default, sources=["m-default"], created=NOW)
        older = fact(db, tenant, gamma, sources=["m-gamma"], created=NOW - timedelta(days=3))

        project_service.delete_project(db, tenant.id, gamma)

        assert [f.id for f in open_in(db, tenant, default)] == [older.id]

    def test_history_that_pointed_at_the_loser_points_at_the_keeper(
            self, db, tenant, default, gamma):
        keeper = fact(db, tenant, default, created=NOW - timedelta(days=3))
        loser = fact(db, tenant, gamma, created=NOW)
        closed = fact(db, tenant, gamma, obj="Basic plan", created=NOW - timedelta(days=9),
                      invalid_at=NOW - timedelta(days=100), invalidated_by=loser.id)

        project_service.delete_project(db, tenant.id, gamma)

        db.expire_all()
        assert db.get(AgentFact, loser.id) is None
        assert db.get(AgentFact, closed.id).invalidated_by_id == keeper.id

    def test_another_agent_is_another_memory_and_nothing_merges(
            self, db, tenant, default, gamma):
        fact(db, tenant, default, agent="support-bot")
        fact(db, tenant, gamma, agent="billing-bot")

        out = project_service.delete_project(db, tenant.id, gamma)

        assert out["facts_merged"] == 0
        assert sorted(f.agent_id for f in open_in(db, tenant, default)) == ["billing-bot", "support-bot"]

    def test_an_erasure_has_nothing_to_merge(self, db, tenant, default, gamma):
        fact(db, tenant, default)
        fact(db, tenant, gamma)

        out = project_service.delete_project(db, tenant.id, gamma, erase=True)

        assert out["facts_merged"] == 0 and out["facts_deleted"] == 1
        assert len(open_in(db, tenant, default)) == 1
