"""The judge's candidates of this engine: memory_core's (`candidates.for_the_judge`,
piece 2 of the common facts engine, 0.1.15), with the wall this engine passes.

Until 2026-10-06 this file froze `_stage1_candidates` as it was on
0.1.15-dev and proved, on a random store, that the shared function returned
the same facts in the same order with the old object filter switched off
(the one deliberate difference, removed from both engines in memory_core
2.0.0: "uses Jira" against "uses Linear" sat at 0.217 and it separated
nothing). The equivalence held in every release since; the frozen copy kept
internal names alive (the code audit of 6 October). What the adapter decides
is tested here as behaviour: the wall (account, project, end user), the
recall stage only inside one end user, meaning only with a vector, a past
statement weighing only the same two entities, and what is never a
candidate. The project wall has its own file (test_project_isolation.py), the
facts of the same memory theirs (test_bug123_same_source.py).
"""
import uuid
from datetime import datetime, timedelta, timezone


from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, Tenant
from korely_agent.services.facts import contradiction


NOW = datetime.now(timezone.utc)
NEAR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
FAR = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)


def fact(db, tenant, subject, pred, obj, *, user=None, minutes_ago=60, vector=None,
         invalid_at=None, tenant_id=None):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant_id or tenant.id, end_user_id=user,
                  subject_label=subject, subject_canonical=subject.lower(),
                  predicate_raw=pred, predicate_canonical=pred,
                  object_label=obj, object_canonical=obj.lower(), confidence=0.9,
                  valid_from=NOW - timedelta(days=30), invalid_at=invalid_at,
                  created_at=NOW - timedelta(minutes=minutes_ago), embedding=vector)
    db.add(f)
    db.commit()
    return f


def candidates(db, tenant, subject, pred, obj=None, *, user=None, vector=None, past=False):
    return [c.object_label for c in contradiction._stage1_candidates(
        db, tenant.id, user, subject, pred, exclude_id=uuid.uuid4(), new_embedding=vector,
        subject_canonical=subject.lower(), object_canonical=(obj or "").lower() or None,
        only_arguments=past)]


def test_the_same_subject_and_relation_newest_first(db, tenant):
    fact(db, tenant, "Acme", "on_plan", "Business plan", minutes_ago=120)
    fact(db, tenant, "Acme", "on_plan", "Pro plan", minutes_ago=60)
    fact(db, tenant, "Acme", "based_in", "Milan")
    assert candidates(db, tenant, "Acme", "on_plan", "Starter plan") == ["Pro plan", "Business plan"]


def test_the_wall_another_account_another_end_user(db, tenant):
    other = Tenant(id=uuid.uuid4(), name="Other")
    db.add(other)
    db.commit()
    fact(db, tenant, "Acme", "on_plan", "Business plan", tenant_id=other.id)
    fact(db, tenant, "Acme", "on_plan", "Pro plan", user="luca")
    mine = fact(db, tenant, "Acme", "on_plan", "Team plan", user="maria")
    assert candidates(db, tenant, "Acme", "on_plan", "Starter plan", user="maria") == [mine.object_label]
    assert candidates(db, tenant, "Acme", "on_plan", "Starter plan") == [], \
        "the shared facts (no end user) are another memory than maria's"


def test_the_recall_stage_only_inside_one_end_user(db, tenant):
    """Inside one end user the same relation is almost always about the same
    entity, under whatever name it was written ("her company on_plan" and
    "Acme on_plan"); across everybody's shared facts that guess is too broad,
    and another subject's fact is not weighed."""
    fact(db, tenant, "Her company", "on_plan", "Business plan", user="maria")
    fact(db, tenant, "Maria", "works_at", "Initech", user="maria")
    fact(db, tenant, "Globex", "on_plan", "Pro plan")
    assert candidates(db, tenant, "Acme", "on_plan", "Starter plan", user="maria") == ["Business plan"]
    assert candidates(db, tenant, "Acme", "on_plan", "Starter plan") == []


def test_meaning_brings_what_sql_does_not_only_with_a_vector(db, tenant):
    """Inside one end user's memory meaning crosses subjects ("Acme Corp",
    "Acme"); with no end user, since memory_core 6.9.0, only inside one
    subject (`owner.keeps_to_its_subject`)."""
    fact(db, tenant, "Acme Corp", "subscribes_to", "Business plan", user="u1", vector=NEAR)
    fact(db, tenant, "Initech", "sells", "Staplers", user="u1", vector=FAR)
    assert candidates(db, tenant, "Acme", "on_plan", "Starter plan", user="u1") == []
    assert candidates(db, tenant, "Acme", "on_plan", "Starter plan", user="u1",
                      vector=NEAR) == ["Business plan"]


def test_with_no_end_user_meaning_stays_inside_one_subject(db, tenant):
    fact(db, tenant, "Acme Corp", "subscribes_to", "Business plan", vector=NEAR)
    fact(db, tenant, "Acme", "subscribes_to_addon", "Priority support", vector=NEAR)
    assert candidates(db, tenant, "Acme", "on_plan", "Starter plan") == []
    assert candidates(db, tenant, "Acme", "on_plan", "Starter plan", vector=NEAR) == ["Priority support"]


def test_a_past_statement_weighs_only_the_same_two_entities(db, tenant):
    """"Acme was on the Business plan" already closed its own twin: only the
    facts about Acme and the Business plan are weighed, never Acme's other
    plans."""
    fact(db, tenant, "Acme", "on_plan", "Pro plan", minutes_ago=30)
    fact(db, tenant, "Acme", "uses", "Business plan", minutes_ago=90)
    assert candidates(db, tenant, "Acme", "on_plan", "Business plan", past=True) == ["Business plan"]
    assert set(candidates(db, tenant, "Acme", "on_plan", "Business plan")) == {"Pro plan", "Business plan"}


def test_what_is_never_a_candidate(db, tenant):
    """The new fact itself, and a fact no longer true; a fact whose end is in
    the future is still true, and is weighed."""
    new = fact(db, tenant, "Acme", "on_plan", "Starter plan", minutes_ago=1)
    fact(db, tenant, "Acme", "on_plan", "Business plan", invalid_at=NOW - timedelta(days=1))
    fact(db, tenant, "Acme", "on_plan", "Trial plan", invalid_at=NOW + timedelta(days=10))
    got = [c.object_label for c in contradiction._stage1_candidates(
        db, tenant.id, None, "Acme", "on_plan", exclude_id=new.id,
        subject_canonical="acme", object_canonical="starter plan")]
    assert got == ["Trial plan"]
