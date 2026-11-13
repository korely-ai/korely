"""The states of the context's memories in one query (2026-10-05, BUG-367).

`memories_for_the_context` (memory_core) drops a memory whose facts are all
superseded, and asks each memory's state through a callable. The callable was
`store.state_of_the_source`: two counts per memory over the whole facts
table, every tenant's, because its OR with a LIKE on the JSON sources keeps
Postgres off every index. On the bench copy (one end user with 3,571 memories
and 5,348 facts, a table of 20,784) that was 80 sequential scans per context,
75% of the call, median 0.30 s. memory_core 2.8.0 has
`store.states_of_the_sources`, one query bounded by a `scope`; /v1/context
passes `context._facts_of` (the tenant, and the end user). These tests hold it
equal to state_of_the_source case by case on this product's tables.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from korely_agent.db.models import AgentFact, AgentMemory, Tenant
from korely_agent.services import context
from korely_agent.services.facts.entities import TABLES
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core.store import states_of_the_sources, state_of_the_source
from tests.conftest import seeded_project_id


NOW = datetime.now(timezone.utc)


def memory(db, tenant, text, eu="u1"):
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                    end_user_id=eu, content=text, graph_sync_status="done")
    db.add(m)
    db.commit()
    return m


def fact(db, tenant, m, pred, obj, *, tense="current", closed=False, by=None,
         also=(), seen=1):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=m.project_id,
                  end_user_id=m.end_user_id, subject_label="u1", subject_canonical="u1",
                  predicate_raw=pred, predicate_canonical=pred,
                  object_label=obj, object_canonical=obj.lower(), confidence=0.9,
                  valid_from=NOW - timedelta(days=4),
                  invalid_at=NOW - timedelta(seconds=1) if closed else None,
                  invalidated_by_id=by, tense=tense, observation_count=seen,
                  source_memory_id=m.id,
                  source_memory_ids=[str(m.id)] + [str(x.id) for x in also])
    db.add(f)
    db.commit()
    return f


def one_by_one(db, ids):
    return {i: state_of_the_source(db, TABLES, uuid.UUID(i), is_open=true_at()) for i in ids}


def batched(db, tenant, ids, eu="u1"):
    got = states_of_the_sources(db, TABLES, [uuid.UUID(i) for i in ids], is_open=true_at(),
                            scope=context._facts_of(tenant.id, eu))
    return {i: tuple(got[i]) for i in ids}


class TestTheSameAnswerAsStateOfTheSource:
    def test_every_kind_of_memory(self, db, tenant):
        open_one = memory(db, tenant, "Maria works at Acme.")
        fact(db, tenant, open_one, "works_at", "Acme")

        history = memory(db, tenant, "I redeemed a $5 coupon last Sunday.")
        fact(db, tenant, history, "redeems", "5 USD coupon", tense="past", closed=True)

        new = memory(db, tenant, "Maria moved to Milan.")
        newer = fact(db, tenant, new, "lives_in", "Milan")
        old = memory(db, tenant, "Maria lives in Rome.")
        fact(db, tenant, old, "lives_in", "Rome", closed=True, by=newer.id)

        closed_present = memory(db, tenant, "Maria has a red car.")
        fact(db, tenant, closed_present, "owns", "red car", closed=True)

        nothing = memory(db, tenant, "Thanks, that was helpful!")

        mixed = memory(db, tenant, "I sold the bike and bought a kayak.")
        fact(db, tenant, mixed, "owns", "bike", closed=True, by=newer.id)
        fact(db, tenant, mixed, "owns", "kayak")

        said_again = memory(db, tenant, "As I said, I work at Acme.")
        first = memory(db, tenant, "I work at Acme.")
        fact(db, tenant, first, "works_at_self", "Acme", also=[said_again], seen=2)

        ids = [str(m.id) for m in (open_one, history, new, old, closed_present,
                                   nothing, mixed, said_again, first)]
        assert batched(db, tenant, ids) == one_by_one(db, ids)
        # The cases the rule turns on, spelled out.
        got = batched(db, tenant, ids)
        assert got[str(history.id)] == (1, 1)
        assert got[str(old.id)] == (1, 0)
        assert got[str(nothing.id)] == (0, 0)
        assert got[str(mixed.id)] == (2, 1)
        assert got[str(said_again.id)] == (1, 1)

    def test_a_reconfirmation_seen_once_does_not_count(self, db, tenant):
        # observation_count 1 with a second id listed: state_of_the_source only
        # reads the sources of a fact seen more than once.
        a = memory(db, tenant, "I live in Turin.")
        b = memory(db, tenant, "Turin, as I told you.")
        fact(db, tenant, a, "lives_in", "Turin", also=[b], seen=1)
        ids = [str(a.id), str(b.id)]
        assert batched(db, tenant, ids) == one_by_one(db, ids)
        assert batched(db, tenant, ids)[str(b.id)] == (0, 0)

    def test_other_tenants_and_users_do_not_leak_in(self, db, tenant):
        """Another end user's fact and another tenant's that both list this
        memory among their sources (seen twice, so the LIKE branch reads
        them): the whole table counts them, the scope of /v1/context does
        not. Until the audit of 17 Oct 2026 the outside facts named only
        their own memories, so a `_facts_of` that scoped nothing passed."""
        mine = memory(db, tenant, "I like tea.")
        fact(db, tenant, mine, "likes", "tea")
        theirs = memory(db, tenant, "I like coffee.", eu="u2")
        fact(db, tenant, theirs, "likes", "coffee", closed=True, also=[mine], seen=2)
        other = Tenant(name=f"other-{uuid.uuid4().hex[:6]}")
        db.add(other)
        db.commit()
        elsewhere = memory(db, other, "I like juice.")
        fact(db, other, elsewhere, "likes", "juice", closed=True, also=[mine], seen=2)
        ids = [str(mine.id)]
        # The facts are there to leak: the unscoped count reads all three.
        assert one_by_one(db, ids) == {str(mine.id): (3, 1)}
        assert batched(db, tenant, ids) == {str(mine.id): (1, 1)}

    def test_no_end_user_reads_the_whole_tenant(self, db, tenant):
        a = memory(db, tenant, "I like tea.")
        fact(db, tenant, a, "likes", "tea")
        b = memory(db, tenant, "I like coffee.", eu="u2")
        fact(db, tenant, b, "likes", "coffee", closed=True)
        ids = [str(a.id), str(b.id)]
        assert batched(db, tenant, ids, eu=None) == one_by_one(db, ids)

    def test_no_memories_no_query(self, db, tenant):
        assert batched(db, tenant, []) == {}


class TestOneQuery:
    def test_one_statement_for_any_number_of_memories(self, db, tenant):
        ms = [memory(db, tenant, f"Fact number {n}.") for n in range(12)]
        for m in ms:
            fact(db, tenant, m, "states", f"thing {m.id}")
        statements = []

        def count(conn, cursor, statement, *args):
            statements.append(statement)

        bind = db.get_bind()
        event.listen(bind, "before_cursor_execute", count)
        try:
            got = batched(db, tenant, [str(m.id) for m in ms])
        finally:
            event.remove(bind, "before_cursor_execute", count)
        assert len(statements) == 1
        assert all(v == (1, 1) for v in got.values())
