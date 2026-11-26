"""memory_core.worker: the worker that drains the memories waiting for the
two Agents engines (2026-09-28). The claim with SKIP LOCKED is tried on Postgres (here SQLite has no
row lock): tried on korely-agent's test Postgres on 28 Sep, three
memories held by one session and three claimed by the other, none in common.

No model is called.
"""
# Notes: on korely-agent's memories (`AgentMemory`, tenant_id) and on
# PostgreSQL, so the claim runs with its real SKIP LOCKED; the fair claim's
# person is (tenant_id, end_user_id), as this product's drain passes it.
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS


from korely_agent.db.models import AgentMemory
from korely_agent.services.memory_core import worker

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _mem(db, tenant, status="pending", **k):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, content="x", graph_sync_status=status, **k)
    db.add(m)
    db.commit()
    return m


class TestTheTime:
    def test_the_order_is_the_order_of_the_events(self):
        a = NS(event_time=None, created_at=datetime(2026, 9, 2))            # naive: SQLite
        b = NS(event_time=datetime(2025, 1, 1, tzinfo=timezone.utc), created_at=None)
        c = NS(event_time=None, created_at=None)
        assert sorted([a, b, c], key=worker.event_key) == [c, b, a]

    def test_read_event_time(self):
        assert worker.read_event_time("2025-06-01") == datetime(2025, 6, 1, tzinfo=timezone.utc)
        assert worker.read_event_time("2025-06-01T09:00:00Z").hour == 9
        assert worker.read_event_time("ieri") is None and worker.read_event_time(None) is None

    def test_a_time_in_the_future_counts_as_now(self):
        # 3.1.5 (7 Oct 2026): a client clock ahead, or a far date, never hides
        # the memory from the reads of the present; never a 422 either.
        now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        assert worker.read_event_time("2026-10-07T12:03:00Z", now) == now
        assert worker.read_event_time("2030-01-01", now) == now
        assert worker.read_event_time("2026-10-07T11:00:00Z", now).hour == 11  # the past stays
        assert worker.read_event_time("2999-01-01") <= datetime.now(timezone.utc)


class TestTheClaim:
    def test_it_takes_back_the_stuck_ones_and_marks_the_claim(self, db, tenant):
        old = _mem(db, tenant, "processing", graph_sync_claimed_at=T0)
        never = _mem(db, tenant, "processing")                   # no claim time
        alive = _mem(db, tenant, "processing", graph_sync_claimed_at=datetime.now(timezone.utc))
        new = _mem(db, tenant)
        claimed = worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
        assert {m.id for m in claimed} == {old.id, never.id, new.id}
        assert all(m.graph_sync_status == "processing" and m.graph_sync_claimed_at for m in claimed)
        db.refresh(alive)
        assert alive.graph_sync_status == "processing"


class Fact(NS):
    pass


def _f(memory_id, **k):
    base = dict(id=uuid.uuid4(), source_memory_id=memory_id, tense="current", invalid_at=None,
                observation_count=1, end_user_id="u1", subject_label="Maria", subject_canonical="Maria",
                predicate_raw="works at", predicate_canonical="works_at", object_label="Acme",
                object_canonical="Acme", embedding=None, valid_from=None, invalidated_by_id=None)
    base.update(k)
    return Fact(**base)


class TestTheWriteOfAMemory:
    def test_per_fact(self, db, tenant):
        m = _mem(db, tenant, "processing")
        f = _f(m.id)
        out = worker.write_memory(db, m, ["ef"], write_one=lambda ef, d: (f, ["x"]),
                                 shape_of=lambda fact, closed: {"id": fact.id, "closed": closed})
        assert out == [{"id": f.id, "closed": ["x"]}] and m.graph_sync_status == "done"

    def test_the_batch_judges_only_the_new_facts_of_this_memory(self, db, tenant):
        m = _mem(db, tenant, "processing")
        new = _f(m.id)
        written = [new, new,                                         # the same fact twice
                   _f(uuid.uuid4()),                                  # reconfirmation of someone else's fact
                   _f(m.id, observation_count=2),                     # reconfirmation
                   _f(m.id, tense="past", invalid_at=T0),             # history
                   _f(m.id, tense="planned")]                          # plan
        deferred, seen = [], []
        it = iter(written)

        def write_one(ef, defer):
            deferred.append(defer)
            return next(it), []

        def batch(new_ones):
            seen.extend(n["id"] for n in new_ones)
            return {new.id: ["old"]}

        out = worker.write_memory(db, m, range(len(written)), write_one=write_one,
                                 shape_of=lambda f, c: c, batch=batch)
        assert seen == [new.id] and all(deferred)
        assert out[0] == ["old"] and out[2] == []

    def test_a_failure_writes_nothing_and_leaves_the_state(self, db, tenant):
        m = _mem(db, tenant, "processing")

        def write_one(ef, d):
            raise RuntimeError("the judge does not answer")

        assert worker.write_memory(db, m, ["ef"], write_one=write_one, shape_of=lambda f, c: {}) == []
        db.refresh(m)
        assert m.graph_sync_status == "processing"


class TestDrainOne:
    def test_done_left_in_progress_or_broken(self, db, tenant):
        ok = _mem(db, tenant, "processing")
        assert worker.drain_one(db, ok, lambda m, ev: setattr(m, "graph_sync_status", "done")) is True
        mute = _mem(db, tenant, "processing")
        assert worker.drain_one(db, mute, lambda m, ev: []) is False
        # 'error' is a compare-and-set in the database (`close_the_claim`):
        # a session that expires its objects on commit would see it; this suite's
        # does not (expire_on_commit=False), so the row is read again.
        db.refresh(mute)
        assert mute.graph_sync_status == "error"
        broken = _mem(db, tenant, "processing", event_time=T0)
        seen = []

        def do(m, ev):
            seen.append(ev)
            raise RuntimeError("boom")

        assert worker.drain_one(db, broken, do) is False
        db.refresh(broken)
        assert broken.graph_sync_status == "error" and seen[0].replace(tzinfo=timezone.utc) == T0


class TestTheFairClaim:
    """2.10.0 (korely-agent 6e795ce): at most N memories per person per
    claim, each in its own order (Demers, Keshav and Shenker, SIGCOMM 1989)."""

    def _queues(self, db, tenant, people=("a", "b", "c"), how_many=10):
        rows = {}
        for i, p in enumerate(people):
            rows[p] = []
            for j in range(how_many):
                m = _mem(db, tenant, end_user_id=p, created_at=T0 + timedelta(hours=i, minutes=j))
                rows[p].append(m.id)
        return rows

    def test_at_most_n_per_person_the_oldest(self, db, tenant):
        rows = self._queues(db, tenant)
        claimed = worker.claim_pending(db, AgentMemory, limit=20, expiry_minutes=15,
                           person=(AgentMemory.tenant_id, AgentMemory.end_user_id), per_person=5)
        assert [m.id for m in claimed] == rows["a"][:5] + rows["b"][:5] + rows["c"][:5]

    def test_without_the_parameters_the_claim_of_before(self, db, tenant):
        rows = self._queues(db, tenant)
        claimed = worker.claim_pending(db, AgentMemory, limit=20, expiry_minutes=15)
        assert [m.id for m in claimed] == rows["a"] + rows["b"]

    def test_the_filter_holds_before_the_count(self, db, tenant):
        rows = self._queues(db, tenant)
        claimed = worker.claim_pending(db, AgentMemory, limit=20, expiry_minutes=15,
                           filter_condition=AgentMemory.end_user_id != "a",
                           person=(AgentMemory.tenant_id, AgentMemory.end_user_id), per_person=5)
        assert [m.id for m in claimed] == rows["b"][:5] + rows["c"][:5]

    def test_the_limit_stays(self, db, tenant):
        rows = self._queues(db, tenant)
        claimed = worker.claim_pending(db, AgentMemory, limit=7, expiry_minutes=15,
                           person=(AgentMemory.tenant_id, AgentMemory.end_user_id), per_person=5)
        assert [m.id for m in claimed] == rows["a"][:5] + rows["b"][:2]
