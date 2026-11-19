"""A memory of something that happened stays in the context (memory_core 2.5.1).

Found on LongMemEval run A (2026-10-01): a fact stated in the past tense ("I
redeemed a $5 coupon on coffee creamer last Sunday") is stored born closed, as
history, and `state_of_the_source` counted it as no longer true. The rule of
`memories_for_the_context` (a memory whose facts are all superseded is out) then
dropped the memory, even when the search put it first: the evidence of 15
questions' answers never reached the reader. Now a fact closed only by its own
tense stands; one closed by a newer fact is still superseded.
"""
import uuid
from datetime import datetime, timedelta, timezone


from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts.entities import TABLES
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core.store import state_of_the_source
from korely_agent.services.memory_core.presentation import memories_for_the_context
from tests.conftest import seeded_project_id


NOW = datetime.now(timezone.utc)
COUPON = "I actually redeemed a $5 coupon on coffee creamer last Sunday."


def memory(db, tenant, text, eu="u1"):
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                    end_user_id=eu, content=text, graph_sync_status="done")
    db.add(m)
    db.commit()
    return m


def fact(db, tenant, m, pred, obj, *, tense="current", closed=False, by=None):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=m.project_id,
                  end_user_id=m.end_user_id, subject_label="u1", subject_canonical="u1",
                  predicate_raw=pred, predicate_canonical=pred,
                  object_label=obj, object_canonical=obj.lower(), confidence=0.9,
                  valid_from=NOW - timedelta(days=4),
                  invalid_at=NOW - timedelta(seconds=1) if closed else None,
                  invalidated_by_id=by, tense=tense,
                  source_memory_id=m.id, source_memory_ids=[str(m.id)])
    db.add(f)
    db.commit()
    return f


def kept(db, *memories):
    rows = [{"id": str(m.id), "content": m.content} for m in memories]
    return [r["content"] for r in memories_for_the_context(
        rows, lambda mid: state_of_the_source(db, TABLES, uuid.UUID(mid), is_open=true_at()))]


class TestHistoryStands:
    def test_a_past_fact_closed_at_birth_is_standing(self, db, tenant):
        m = memory(db, tenant, COUPON)
        fact(db, tenant, m, "redeems", "5 USD coupon", tense="past", closed=True)
        assert state_of_the_source(db, TABLES, m.id, is_open=true_at()) == (1, 1)

    def test_the_coupon_memory_reaches_the_context(self, db, tenant):
        m = memory(db, tenant, COUPON)
        fact(db, tenant, m, "redeems", "5 USD coupon", tense="past", closed=True)
        fact(db, tenant, m, "purchases", "coffee creamer", tense="past", closed=True)
        assert kept(db, m) == [COUPON]


class TestWhatIsSupersededStaysOut:
    def test_replaced_by_a_newer_fact(self, db, tenant):
        old = memory(db, tenant, "Maria lives in Rome.")
        new = memory(db, tenant, "Maria moved to Milan.")
        newer = fact(db, tenant, new, "lives_in", "Milan")
        fact(db, tenant, old, "lives_in", "Rome", closed=True, by=newer.id)
        assert kept(db, old, new) == ["Maria moved to Milan."]

    def test_closed_without_the_past_tense(self, db, tenant):
        m = memory(db, tenant, "Maria works at Acme.")
        fact(db, tenant, m, "works_at", "Acme", tense="current", closed=True)
        assert kept(db, m) == []
