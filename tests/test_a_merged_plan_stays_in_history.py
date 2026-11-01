"""A plan and the same thing done do not become one row (memory_core 5.4.0).

Code audit findings P2, G3D1-11: when an alias, a vocabulary revision or a
project merge made two open rows the same triple, `store.merge_into` kept the
older one and took the loser's tense: a plan became "current" in place and the
done fact's row was deleted, against the rule of 3.0.0 (a plan that comes true
stays in history: closed where the fact begins, the fact a row of its own).
"""
import uuid
from datetime import datetime, timedelta, timezone


from korely_agent.db.models import AgentFact
from korely_agent.services import projects as project_service
from korely_agent.services.facts import entities
from korely_agent.services.memory_core import store


NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
PLANNED_ON = NOW - timedelta(days=60)
DONE_ON = NOW - timedelta(days=10)


def row(db, tenant, project, *, subject, tense, valid_from, created):
    f = AgentFact(
        id=uuid.uuid4(), tenant_id=tenant.id, project_id=project, end_user_id="u1",
        subject_label=subject, subject_canonical=subject,
        predicate_raw="moves to", predicate_canonical="moves_to",
        object_label="Lisbon", object_canonical="Lisbon", tense=tense,
        valid_from=valid_from, confidence=0.9, created_at=created,
    )
    db.add(f)
    db.commit()
    return f


def test_the_plan_is_closed_where_the_fact_begins_and_both_rows_stay(db, tenant):
    plan = row(db, tenant, None, subject="Acme", tense="planned", valid_from=PLANNED_ON, created=PLANNED_ON)
    done = row(db, tenant, None, subject="Globex", tense="current", valid_from=DONE_ON, created=DONE_ON)

    assert store.merge_into(db, entities.TABLES, plan, done) is done
    db.commit()
    db.expire_all()

    plan, done = db.get(AgentFact, plan.id), db.get(AgentFact, done.id)
    assert plan.tense == "planned", "never promoted in place"
    assert plan.invalid_at == DONE_ON and plan.invalidated_by_id == done.id
    assert done.tense == "current" and done.invalid_at is None


def test_two_plans_or_two_facts_still_merge_into_the_older(db, tenant):
    a = row(db, tenant, None, subject="Acme", tense="current", valid_from=PLANNED_ON, created=PLANNED_ON)
    b = row(db, tenant, None, subject="Globex", tense="current", valid_from=DONE_ON, created=DONE_ON)

    assert store.merge_into(db, entities.TABLES, a, b) is a
    db.commit()
    db.expire_all()
    assert db.get(AgentFact, b.id) is None
    assert db.get(AgentFact, a.id).invalid_at is None


def test_a_project_merge_keeps_the_plan_in_history(db, tenant):
    default = project_service.get_or_create_default_project(db, tenant.id)
    gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
    plan = row(db, tenant, default.id, subject="u1", tense="planned", valid_from=PLANNED_ON, created=PLANNED_ON)
    done = row(db, tenant, gamma.id, subject="u1", tense="current", valid_from=DONE_ON, created=DONE_ON)

    project_service.delete_project(db, tenant.id, gamma)
    db.expire_all()

    plan, done = db.get(AgentFact, plan.id), db.get(AgentFact, done.id)
    assert done is not None and done.invalid_at is None and done.project_id == default.id
    assert plan.tense == "planned" and plan.invalid_at == DONE_ON and plan.invalidated_by_id == done.id
