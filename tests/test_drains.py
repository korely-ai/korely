"""The background drains, asked whether two of them are really safe.

worker.py says: "Running several is safe but wasteful: each drain claims rows
before working them, so replicas do not duplicate work, they contend for it."
That was documentation, not a property. All three drains selected rows and then
committed inside the loop, and a commit ends the transaction, which releases
every lock the select took.

For facts a duplicate is two model calls billed for one memory and two passes
of contradiction detection over the same content. For batch it is the same
payload imported twice. Neither announces itself.
"""
import threading
import time
import uuid

import pytest
from sqlalchemy import func, text
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import AgentMemory
from korely_agent.services.facts import drain as facts_drain
from tests.thread_helpers import wait_idle
from korely_agent.services.facts import service as facts_service



def make_memories(db, tenant, n):
    ids = []
    for i in range(n):
        m = AgentMemory(
            id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=f"u{i}",
            content=f"memory number {i}", graph_sync_status="pending",
        )
        db.add(m)
        ids.append(m.id)
    db.commit()
    return ids


class TestTwoDrainsDoNotDuplicateWork:
    def test_a_drain_skips_rows_another_transaction_is_claiming(
        self, db, engine, tenant, monkeypatch
    ):
        """Driven by hand, because the race will not reproduce on demand.

        The window is between the SELECT and the commit that marks the rows
        'processing': a hundred microseconds. Two threads started from a
        barrier almost never land inside it, and a test built that way passes
        against the broken code, which is worse than no test - it is a green
        assertion that the defect is gone. (Confirmed: the thread version
        passed three times in a row with the fix reverted.)

        So this holds the window open instead. Session A takes the same lock the
        drain takes and stops. While it is there, a drain on session B must find
        nothing to do. Without FOR UPDATE SKIP LOCKED it finds everything, marks
        it 'processing' and works it, and the same memory is extracted twice:
        two model calls billed for one write.
        """
        ids = make_memories(db, tenant, 5)

        Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        holder = Session()
        try:
            held = (
                holder.query(AgentMemory)
                .filter(AgentMemory.graph_sync_status == "pending")
                .order_by(AgentMemory.created_at.asc())
                .limit(5)
                .with_for_update(skip_locked=True)
                .all()
            )
            # The five made above, not five of anything: the drain below is
            # asked about exactly these rows.
            assert {m.id for m in held} == set(ids), "the holder did not take the rows"

            worked = []
            monkeypatch.setattr(
                facts_drain, "_run_group",
                lambda gdb, t, project, eu, rows, claims: (worked.extend(r.id for r in rows),
                                          (len(rows), 0))[1],
            )

            other = Session()
            try:
                # Fail fast if the lock is not respected. Without
                # skip_locked the drain reads the rows anyway (readers do not
                # block under MVCC) and then blocks forever on the UPDATE that
                # claims them, which is its own production failure - a worker
                # that stops draining and says nothing. Untimed, this test
                # would hang the suite instead of reporting it.
                other.execute(text("SET LOCAL lock_timeout = '3s'"))
                r = facts_service.process_pending_memories(other, limit=10)
            finally:
                other.rollback()
                other.close()

            assert r["claimed"] == 0, (
                f"claimed {r['claimed']} rows another transaction is holding"
            )
            assert worked == [], f"worked rows held by someone else: {worked}"
        finally:
            holder.rollback()
            holder.close()

        # And once the holder lets go, the work is still there to be done.
        monkeypatch.setattr(facts_drain, "_run_group",
                            lambda gdb, t, project, eu, rows, claims: (len(rows), 0))
        assert facts_service.process_pending_memories(db, limit=10)["claimed"] == 5

    def test_each_memory_is_worked_once_when_two_drains_race(
        self, db, engine, tenant, monkeypatch
    ):
        """The realistic version. It does not reliably reproduce the defect, and
        is kept for what it does cover: that two concurrent drains complete
        without deadlocking each other and between them work every row once.
        """
        ids = make_memories(db, tenant, 10)
        worked, lock = [], threading.Lock()

        def fake_group(gdb, tenant_id, project_id, end_user_id, rows, claims):
            with lock:
                worked.extend(r.id for r in rows)
            for r in rows:
                r.graph_sync_status = "done"
            gdb.commit()
            return len(rows), 0

        monkeypatch.setattr(facts_drain, "_run_group", fake_group)

        Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        start = threading.Barrier(2)
        errors = []

        def drain():
            s = Session()
            try:
                start.wait(timeout=10)
                facts_service.process_pending_memories(s, limit=10)
            except Exception as e:
                errors.append(e)
            finally:
                s.rollback()
                s.close()

        ts = [threading.Thread(target=drain) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)

        assert not errors, errors[:2]
        assert len(worked) == len(set(worked)), "worked twice"
        assert set(worked) == set(ids)

    def test_an_empty_queue_is_not_an_error(self, db, tenant):
        r = facts_service.process_pending_memories(db, limit=10)
        assert r == {"processed": 0, "failed": 0, "claimed": 0}

    def test_claimed_rows_are_not_reclaimed_on_the_next_tick(
        self, db, tenant, monkeypatch
    ):
        make_memories(db, tenant, 3)
        seen = []

        def fake_group(gdb, tenant_id, project_id, end_user_id, rows, claims):
            seen.extend(r.id for r in rows)
            return len(rows), 0        # leaves them 'processing', as a crash would

        monkeypatch.setattr(facts_drain, "_run_group", fake_group)
        facts_service.process_pending_memories(db, limit=10)
        first = list(seen)
        assert len(first) == 3

        # Still 'processing' and inside the stale window: nobody picks them up
        r = facts_service.process_pending_memories(db, limit=10)
        assert r["claimed"] == 0
        assert seen == first

    def test_rows_stranded_by_a_crash_are_reclaimed_eventually(
        self, db, tenant, monkeypatch
    ):
        """The reaper is the other half: without it a worker killed mid-drain
        leaves memories in 'processing' forever, and the API keeps answering
        'processing' to a caller who will never get facts."""
        from datetime import datetime, timedelta, timezone

        ids = make_memories(db, tenant, 2)
        # Claimed three hours ago by a worker that died. The claim's own
        # clock, not `updated_at`, which is the developer's since 2026-09-28
        # (tests/test_updated_at_moves_only_on_an_edit.py).
        db.query(AgentMemory).filter(AgentMemory.id.in_(ids)).update(
            {"graph_sync_status": "processing",
             "graph_sync_claimed_at": datetime.now(timezone.utc) - timedelta(hours=3)},
            synchronize_session=False,
        )
        db.commit()

        seen = []
        monkeypatch.setattr(
            facts_drain, "_run_group",
            lambda gdb, t, project, eu, rows, claims: (seen.extend(r.id for r in rows), (len(rows), 0))[1],
        )
        r = facts_service.process_pending_memories(db, limit=10)
        assert r["claimed"] == 2, "stranded rows were never reclaimed"
        assert set(seen) == set(ids)


class TestAProviderThatRefusesIsNotAnEmptyResult:
    """The failure found on 2026-09-12, with a real key on a real machine.

    A brand-new Google account could not use the configured model. Every
    extraction call came back 404, the extractor logged a warning and returned
    None, and the caller read None as "the model looked and found nothing".
    Both memories were filed as processed and would never have been tried
    again. `/health` said `failing` the whole time, which is the only reason
    anybody noticed.

    None means nothing was there. An exception means we could not look. The
    two must not share a return value.
    """

    def test_the_memory_ends_at_error_not_done(self, db, tenant, monkeypatch):
        import uuid

        from korely_agent.db.models import AgentMemory
        from korely_agent.services.facts import service as facts_service

        class Refusing:
            name = "gemini"

            def complete(self, *a, **k):
                raise RuntimeError(
                    "404 NOT_FOUND: this model is no longer available to new users")

        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Refusing())

        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="c9001",
                        content="Customer 9001 is on the Priority Support plan.",
                        graph_sync_status="pending")
        db.add(m)
        db.commit()

        facts_service.process_pending_memories(db, limit=5)
        db.expire_all()

        got = db.query(AgentMemory).filter(AgentMemory.id == m.id).one()
        assert got.graph_sync_status == "error", (
            "a memory the model was never able to read is filed as processed"
        )

    def test_and_reextract_puts_it_back_in_the_queue(self, db, tenant, monkeypatch):
        """Because "I have fixed the cause, now redo the work" had no way of
        being said."""
        import uuid

        from korely_agent import manage
        from korely_agent.db.models import AgentMemory

        monkeypatch.setattr(manage, "_session", lambda: db)
        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="c9001",
                        content="anything", graph_sync_status="error")
        db.add(m)
        db.commit()

        assert manage.main(["reextract"]) == 0
        db.expire_all()
        assert db.query(AgentMemory).filter(
            AgentMemory.id == m.id).one().graph_sync_status == "pending"

    def test_a_memory_with_genuinely_nothing_in_it_stays_done(self, db, tenant, monkeypatch):
        """The control. Without it the test above passes on an extractor that
        marks everything as an error."""
        import uuid

        from korely_agent.core.llm import LLMResult
        from korely_agent.db.models import AgentMemory
        from korely_agent.services.facts import service as facts_service

        class Empty:
            name = "gemini"

            def complete(self, *a, **k):
                return LLMResult(text="[]")

        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Empty())

        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="c9001",
                        content="hello", graph_sync_status="pending")
        db.add(m)
        db.commit()

        facts_service.process_pending_memories(db, limit=5)
        db.expire_all()
        assert db.query(AgentMemory).filter(
            AgentMemory.id == m.id).one().graph_sync_status == "done"


class TestEndUsersAreWorkedTogether:
    """Twenty people chatting together queued behind one another (seen on
    2026-10-02): one memory at a time, about seven a minute with llama on
    Scaleway and eighteen with gpt-oss :nitro, for the whole install. Groups
    of different end users now run at once, each in its own thread and
    session, and inside a group the event-time order is the same as before:
    it is what lets "lives in Turin" close "lives in Milan"."""

    def _pending(self, db, tenant, users, per_user):
        from datetime import datetime, timedelta, timezone

        t0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        for u in range(users):
            # Written newest first, so a drain that ignored event time would
            # visibly work them out of order.
            for k in reversed(range(per_user)):
                db.add(AgentMemory(
                    id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=f"user{u}",
                    content=f"user{u} memory {k}", graph_sync_status="pending",
                    event_time=t0 + timedelta(minutes=k)))
        db.commit()

    def _fake_drain(self, monkeypatch, seconds=0.2, block=None):
        import time

        seen, lock = [], threading.Lock()
        live = {"now": 0, "max": 0}

        def drain_one(gdb, m, region=None):
            with lock:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
                seen.append((m.end_user_id, m.content, id(gdb), threading.current_thread().name))
            if block is not None and m.end_user_id == "user0":
                block.wait(30)
            time.sleep(seconds)
            m.graph_sync_status = "done"
            gdb.commit()
            with lock:
                live["now"] -= 1
            return True

        monkeypatch.setattr(facts_drain, "_drain_one_memory", drain_one)
        return seen, live

    def test_four_users_at_once_each_in_event_order(self, db, tenant, monkeypatch):
        import time

        self._pending(db, tenant, users=4, per_user=3)
        seen, live = self._fake_drain(monkeypatch, seconds=0.2)
        started = time.monotonic()
        r = facts_service.process_pending_memories(db, limit=20)
        took = time.monotonic() - started

        assert r == {"processed": 12, "failed": 0, "claimed": 12}
        # One at a time is 12 x 0.2 = 2.4 s; four users at once is about 0.6 s.
        assert took < 1.6, f"took {took:.2f}s: the users were not worked together"
        assert live["max"] >= 2
        for u in range(4):
            mine = [c for eu, c, _s, _t in seen if eu == f"user{u}"]
            assert mine == [f"user{u} memory {k}" for k in range(3)], mine
        # Every group on a session of its own, none on the drain's, and in
        # the drain's threads.
        assert id(db) not in {s for _eu, _c, s, _t in seen}
        assert all(t.startswith("facts-drain") for *_x, t in seen)

    def test_one_user_is_worked_in_the_drains_threads_too(self, db, tenant, monkeypatch):
        """A lone group stayed on the drain's session, in the tick's thread,
        so the tick waited for it however long it ran (audit of 20 Oct 2026: a round
        never waits for its slowest account)."""
        self._pending(db, tenant, users=1, per_user=3)
        seen, _live = self._fake_drain(monkeypatch, seconds=0)
        assert facts_service.process_pending_memories(db, limit=20)["processed"] == 3
        assert id(db) not in {s for _eu, _c, s, _t in seen}
        assert all(t.startswith("facts-drain") for *_x, t in seen)

    def test_a_group_that_hangs_does_not_hold_the_drain(self, db, tenant, monkeypatch):
        """Upstream a parallel drain hung and its cause was never found. Here
        a hung group costs its own rows' delay (the reaper takes them back
        after drain_stale_minutes), never the tick or the other users."""
        import time

        self._pending(db, tenant, users=2, per_user=1)
        release = threading.Event()
        seen, _live = self._fake_drain(monkeypatch, seconds=0, block=release)
        monkeypatch.setattr(facts_drain, "_group_timeout", lambda: 1.0)
        started = time.monotonic()
        try:
            r = facts_service.process_pending_memories(db, limit=20)
            took = time.monotonic() - started
        finally:
            release.set()
        assert took < 5, f"the tick waited {took:.1f}s on a hung group"
        assert r["processed"] == 1 and r["claimed"] == 2

    def test_a_full_batch_asks_for_the_next_one_at_once(self, db, tenant, monkeypatch):
        self._pending(db, tenant, users=3, per_user=1)
        self._fake_drain(monkeypatch, seconds=0)
        assert facts_service.process_pending_memories(db, limit=2)["more"] is True
        assert "more" not in facts_service.process_pending_memories(db, limit=2)

    def _backlog(self, db, tenant, sizes):
        """`sizes` pending memories per end user, the first user's written
        first: an import, or a benchmark that writes a conversation at a time."""
        from datetime import datetime, timedelta, timezone

        t0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
        n = 0
        for u, size in enumerate(sizes):
            for k in range(size):
                db.add(AgentMemory(
                    id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=f"user{u}",
                    content=f"user{u} memory {k}", graph_sync_status="pending",
                    created_at=t0 + timedelta(seconds=n), event_time=t0 + timedelta(seconds=n)))
                n += 1
        db.commit()

    def _done_by_user(self, db):
        rows = db.query(AgentMemory.end_user_id, AgentMemory.content).filter(
            AgentMemory.graph_sync_status == "done").all()
        out = {}
        for eu, content in rows:
            out.setdefault(eu, []).append(int(content.rsplit(" ", 1)[1]))
        return {eu: sorted(v) for eu, v in out.items()}

    def test_one_backlog_does_not_hold_the_others(self, db, tenant, monkeypatch):
        """LoCoMo whole, 2026-10-06: the twenty oldest were all the first
        conversation's two end users, eighteen others waited with everything
        pending, and the groups ran two at a time. Now each end user gives
        at most `_PER_END_USER` to a claim."""
        self._backlog(db, tenant, [30, 3, 3, 3])
        self._fake_drain(monkeypatch, seconds=0)
        r = facts_service.process_pending_memories(db, limit=20)
        assert r["claimed"] == 14
        db.expire_all()
        assert self._done_by_user(db) == {"user0": [0, 1, 2, 3, 4], "user1": [0, 1, 2],
                                          "user2": [0, 1, 2], "user3": [0, 1, 2]}

    def test_many_end_users_fill_a_claim_with_four_groups(self, db, tenant, monkeypatch):
        self._backlog(db, tenant, [30, 30, 30, 30, 30])
        seen, live = self._fake_drain(monkeypatch, seconds=0.05)
        r = facts_service.process_pending_memories(db, limit=20)
        assert r["claimed"] == 20 and r["more"] is True
        assert {eu for eu, *_ in seen} == {"user0", "user1", "user2", "user3"}
        assert live["max"] == 4

    def test_a_lone_backlog_goes_on_without_resting(self, db, tenant, monkeypatch):
        """Claimed five at a time, it must not rest between claims: `more`
        whenever the queue is not empty and nothing failed."""
        self._backlog(db, tenant, [12])
        self._fake_drain(monkeypatch, seconds=0)
        firsts = [facts_service.process_pending_memories(db, limit=20) for _ in range(3)]
        assert [r["claimed"] for r in firsts] == [5, 5, 2]
        assert [r.get("more") for r in firsts] == [True, True, None]
        db.expire_all()
        assert self._done_by_user(db) == {"user0": list(range(12))}

    def test_a_person_with_a_group_running_is_not_claimed_again(self, db, tenant, monkeypatch):
        """Audit of 27 Oct 2026: a tick collects its groups for two seconds and a group still
        running goes on in its thread, so the next tick claimed the same
        person's next five and two groups of one person ran at once, out of
        event order. One person, one group at a time."""
        monkeypatch.setattr(facts_drain, "_DRAIN_PATIENCE", 0.2)
        monkeypatch.setattr(facts_drain, "_ROOM_SECONDS", 0.3)
        self._backlog(db, tenant, [8])
        release = threading.Event()
        seen, live = self._fake_drain(monkeypatch, seconds=0, block=release)
        try:
            first = facts_service.process_pending_memories(db, limit=20)
            assert first["claimed"] == 5
            second = facts_service.process_pending_memories(db, limit=20)
            assert second["claimed"] == 0, "the person's next memories were claimed under the running group"
            assert live["max"] == 1
        finally:
            release.set()
        assert wait_idle(facts_drain._draining, 10)
        db.expire_all()
        third = facts_service.process_pending_memories(db, limit=20)
        assert third["claimed"] == 3
        assert [c for _eu, c, _s, _t in seen] == [f"user0 memory {k}" for k in range(8)]

    def test_another_person_of_the_account_is_still_claimed(self, db, tenant, monkeypatch):
        self._backlog(db, tenant, [8, 2])
        db.query(AgentMemory).filter(AgentMemory.end_user_id == "user0",
                                     AgentMemory.content.in_(["user0 memory 0", "user0 memory 1"])
                                     ).update({"graph_sync_status": "processing",
                                               "graph_sync_claimed_at": func.now()},
                                              synchronize_session=False)
        db.commit()
        claimed = facts_drain._claim(db, 20)
        assert {m.end_user_id for m in claimed} == {"user1"}

    def test_the_tick_holds_no_transaction_when_a_group_starts(self, db, tenant, monkeypatch):
        """Group C1 allowlists the group's own session on the ground that the
        tick has committed its claim before the group starts, so the group's
        connection is never held next to an open transaction of the tick's.
        Tested here: the claim's rows are 'processing' and committed (the
        group's session loads them by id), and the tick's session is out of
        any transaction when the group runs."""
        self._pending(db, tenant, users=2, per_user=1)
        states = []

        def drain_one(gdb, m, region=None):
            states.append((db.in_transaction(), gdb.get(AgentMemory, m.id).graph_sync_status))
            m.graph_sync_status = "done"
            gdb.commit()
            return True

        monkeypatch.setattr(facts_drain, "_drain_one_memory", drain_one)
        assert facts_service.process_pending_memories(db, limit=20)["processed"] == 2
        assert states == [(False, "processing")] * 2

    def test_a_failing_batch_never_does(self, db, tenant, monkeypatch):
        """A provider that is down fails every memory fast: without this the
        worker would burn a backlog into 'error' rows at full speed."""
        self._pending(db, tenant, users=3, per_user=1)

        def fails(gdb, m, region=None):
            m.graph_sync_status = "error"
            gdb.commit()
            return False

        monkeypatch.setattr(facts_drain, "_drain_one_memory", fails)
        r = facts_service.process_pending_memories(db, limit=2)
        assert r["failed"] == 2 and "more" not in r


def test_a_memory_erased_during_its_extraction_does_not_hold_its_group(db, engine, tenant,
                                                                         monkeypatch):
    """DELETE /v1/agents/{id} (or an end user's erasure) while the drain
    extracts one memory of a group: memory_core's `drain_one` reads the
    row again and the ORM raises ObjectDeletedError, which left the drain,
    so the group's other memories stayed 'processing' until the reaper, 15
    minutes later (audit of 17 Oct 2026). The group goes on."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.orm import Session

    t0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    first, second = (AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u0",
                                 content=f"memory {k}", graph_sync_status="pending",
                                 created_at=t0 + timedelta(seconds=k),
                                 event_time=t0 + timedelta(seconds=k)) for k in range(2))
    db.add_all([first, second])
    db.commit()
    first_id, second_id = first.id, second.id

    def extract(gdb, tenant_id, *, memory, valid_from=None, model_name=None, claim=None):
        if memory.id == first_id:
            with Session(bind=engine) as other:
                other.query(AgentMemory).filter(AgentMemory.id == first_id).delete()
                other.commit()
            gdb.rollback()  # what a write that lost its row does
            return []
        memory.graph_sync_status = "done"
        gdb.commit()
        return []

    monkeypatch.setattr(facts_service, "extract_and_persist_facts", extract)
    r = facts_service.process_pending_memories(db, limit=10)

    db.expire_all()
    assert db.get(AgentMemory, first_id) is None
    assert db.get(AgentMemory, second_id).graph_sync_status == "done", (
        "the group stopped at the erased memory")
    # The erased one is no longer this drain's, as a lost claim is: no failure.
    assert (r["claimed"], r["processed"], r["failed"]) == (2, 2, 0)


def test_the_worker_does_not_rest_on_a_backlog(monkeypatch):
    """`more` from the facts drain: the next tick starts at once, not after
    FACTS_EVERY seconds, until the backlog is gone."""
    import asyncio
    import time

    from korely_agent import worker

    calls = []

    def job(db):
        calls.append(time.monotonic())
        return {"processed": 20, "claimed": 20, "more": True} if len(calls) < 3 else {}

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(worker._loop("facts", job, 30, stop))
        while len(calls) < 3:
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 35)

    started = time.monotonic()
    asyncio.run(go())
    assert len(calls) == 3
    assert calls[-1] - started < 2, "the worker rested between two full batches"


def _http(code, msg=""):
    """A provider's answer with a status, as urllib raises it."""
    import urllib.error

    return urllib.error.HTTPError("https://provider.example/v1", code, msg, None, None)


class _ApiError(Exception):
    """A google-genai APIError: the status is its `code` field."""

    def __init__(self, code, text):
        super().__init__(f"{code} {text}")
        self.code = code


def _timed_out():
    import socket
    import urllib.error

    return urllib.error.URLError(socket.timeout("timed out"))


class TestAProviderThatRefusesIsWaitedFor:
    """The provider refusing the install as a whole (a rejected key, credit or
    quota gone, too many requests, no answer) is no fault of the memory: it
    goes back to the queue and the drain rests, instead of filing every
    memory as 'error' until somebody runs `manage reextract` (the hosted
    product's BUG-351, 2026-10-05)."""

    @pytest.fixture(autouse=True)
    def rested(self, monkeypatch):
        facts_drain._pauses.clear()
        facts_drain._timeouts.clear()
        # These tests read what a round did once its groups have ended: the
        # round collects them for `_DRAIN_PATIENCE` (2 s) and lets one still
        # running write its outcome later, in its thread, so on a loaded
        # machine a group past 2 s left a memory `processing` when the test
        # read it, and the round counted none of what that group did (the
        # full suite of 6 Nov 2026: `test_one_accounts_fresh_refused_
        # memories_never_rest_the_region[failure4]`, the memory filed as an
        # error in the teardown, after the assertion). The patience of
        # tests/test_a_model_that_does_not_answer.py, for the same reason: a
        # round still ends as soon as its last group does.
        monkeypatch.setattr(facts_drain, "_DRAIN_PATIENCE", 60.0)
        yield
        facts_drain._pauses.clear()
        facts_drain._timeouts.clear()

    def _write(self, monkeypatch, failure=None):
        from korely_agent.core import llm

        def extract(db, tenant_id, *, memory, valid_from=None, model_name=None, claim=None):
            if failure is not None:
                llm._failed(failure)
                return []  # status left 'processing', as the real one does
            memory.graph_sync_status = "done"
            db.commit()
            return []

        monkeypatch.setattr(facts_service, "extract_and_persist_facts", extract)

    def _status(self, db, ids):
        db.expire_all()
        return sorted(db.get(AgentMemory, i).graph_sync_status for i in ids)

    def test_an_out_of_credit_key_leaves_memories_pending_and_rests(self, db, tenant, monkeypatch):
        ids = make_memories(db, tenant, 1)
        self._write(monkeypatch, failure=_http(402, "Payment Required"))
        r = facts_service.process_pending_memories(db, limit=10)
        assert r == {"processed": 0, "failed": 0, "claimed": 1, "deferred": 1}
        assert self._status(db, ids) == ["pending"]
        # Resting: nothing is claimed, so nothing can be filed as an error.
        again = facts_service.process_pending_memories(db, limit=10)
        assert again["claimed"] == 0 and again["paused"] > 0

    def test_it_takes_the_work_back_once_the_provider_answers(self, db, tenant, monkeypatch):
        ids = make_memories(db, tenant, 2)
        self._write(monkeypatch, failure=_ApiError(429, "RESOURCE_EXHAUSTED quota exceeded"))
        facts_service.process_pending_memories(db, limit=10)
        assert self._status(db, ids) == ["pending", "pending"]
        assert facts_drain._pause_of(None)["step"] == 1

        facts_drain._pause_of(None)["until"] = 0.0  # the minute has passed
        self._write(monkeypatch, failure=None)
        r = facts_service.process_pending_memories(db, limit=10)
        assert r["processed"] == 2 and self._status(db, ids) == ["done", "done"]
        assert facts_drain._pause_of(None)["step"] == 0, "a success did not reset the wait"

    def test_each_refusal_in_a_row_rests_longer(self, db, tenant, monkeypatch):
        make_memories(db, tenant, 1)
        self._write(monkeypatch, failure=_timed_out())
        waits = []
        for _ in range(3):
            facts_drain._pause_of(None)["until"] = 0.0
            facts_service.process_pending_memories(db, limit=10)
            waits.append(round(facts_drain._drain_paused()))
        assert waits[0] <= 60 < waits[1] <= 120 < waits[2] <= 300

    def test_a_memory_the_provider_cannot_read_is_still_an_error(self, db, tenant, monkeypatch):
        ids = make_memories(db, tenant, 1)
        self._write(monkeypatch, failure=_http(400, "Bad Request"))
        r = facts_service.process_pending_memories(db, limit=10)
        assert r == {"processed": 0, "failed": 1, "claimed": 1}
        assert self._status(db, ids) == ["error"]
        assert facts_drain._drain_paused() == 0

    @pytest.mark.parametrize("failure", [
        _http(503, "Service Unavailable"),
        _ApiError(503, "UNAVAILABLE"),
        _http(502, "Bad Gateway"),
        _http(504, "Gateway Timeout"),
        _ApiError(529, "overloaded_error"),
        # No answer at all, whatever its words (audit of 17 Oct 2026: these
        # were in no list, and the memory went to 'error').
        __import__("httpx").RemoteProtocolError("Server disconnected without sending a response."),
        __import__("httpx").ConnectError("[Errno -3] Temporärer Fehler bei der Namensauflösung"),
        ConnectionResetError(104, "Connection reset by peer"),
    ])
    def test_a_provider_down_or_overloaded_is_waited_for(self, db, tenant, monkeypatch, failure):
        """502, 503 and 504 speak about the server, never the request (RFC
        9110), 529 is an overloaded Anthropic API, and a call that got no
        answer is the network's: the memory waits."""
        ids = make_memories(db, tenant, 1)
        self._write(monkeypatch, failure=failure)
        facts_service.process_pending_memories(db, limit=10)
        assert self._status(db, ids) == ["pending"]
        assert facts_drain._drain_paused() > 0

    def test_a_refusal_a_retry_got_past_is_not_the_reason(self, db, tenant, monkeypatch):
        """A 429, then the call answered, then the engine failed for its own
        reason: the memory's error, and nobody rests. The thread used to keep
        the 429 and read it as the provider refusing."""
        from korely_agent.core import llm

        def extract(db, tenant_id, *, memory, valid_from=None, model_name=None, claim=None):
            llm._failed(_ApiError(429, "RESOURCE_EXHAUSTED quota exceeded"))
            llm._record(True)
            return []  # status left 'processing': the engine failed after the call

        ids = make_memories(db, tenant, 1)
        monkeypatch.setattr(facts_service, "extract_and_persist_facts", extract)
        facts_service.process_pending_memories(db, limit=10)
        assert self._status(db, ids) == ["error"]
        assert facts_drain._drain_paused() == 0

    def test_a_500_is_still_the_memorys(self, db, tenant, monkeypatch):
        """A server that crashes on one input crashes on it every time: back
        in the queue, claimed oldest first, it would hold every memory behind
        it."""
        ids = make_memories(db, tenant, 1)
        self._write(monkeypatch, failure=_http(500, "internal error"))
        facts_service.process_pending_memories(db, limit=10)
        assert self._status(db, ids) == ["error"]
        assert facts_drain._drain_paused() == 0

    # A memory the provider times out on every time (the Bussola's lane 3,
    # 2026-10-06): it went back to the queue for ever, the drain resting
    # each time.

    def _one_slow_memory(self, db, tenant, monkeypatch, failure):
        """'slow' always fails with `failure`; every other memory is written.
        'slow' is the newest event, so its group writes the others first."""
        from datetime import datetime, timezone

        from korely_agent.core import llm

        def extract(db_, tenant_id, *, memory, valid_from=None, model_name=None, claim=None):
            if memory.content == "slow":
                llm._failed(failure)
                return []  # status left 'processing', as the real one does
            # Written as the real one writes: 'done' on the claim.
            from sqlalchemy.orm.attributes import set_committed_value

            from korely_agent.services.memory_core import worker

            assert worker.close_the_claim(db_, claim, "done")
            db_.commit()
            set_committed_value(memory, "graph_sync_status", "done")
            return []

        monkeypatch.setattr(facts_service, "extract_and_persist_facts", extract)
        slow = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u0", content="slow",
                           graph_sync_status="pending",
                           event_time=datetime(2030, 1, 1, tzinfo=timezone.utc))
        db.add(slow)
        db.commit()
        return slow.id

    def _a_round(self, db, tenant, day):
        """The minute has passed, and another memory of the same end user waits."""
        from datetime import datetime, timezone

        facts_drain._pause_of(None)["until"] = 0.0
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u0",
                           content=f"memory of day {day}", graph_sync_status="pending",
                           event_time=datetime(2020, 1, day, tzinfo=timezone.utc)))
        db.commit()
        return facts_service.process_pending_memories(db, limit=10)

    @pytest.mark.parametrize("failure", [_timed_out(), _http(504, "Gateway Timeout")])
    def test_a_memory_that_times_out_every_time_is_filed_in_the_end(self, db, tenant, monkeypatch,
                                                                     failure):
        slow = self._one_slow_memory(db, tenant, monkeypatch, failure)
        for day in range(1, facts_drain._TIMEOUTS_PER_MEMORY):
            r = self._a_round(db, tenant, day)
            assert r["processed"] == 1 and self._status(db, [slow]) == ["pending"]
        r = self._a_round(db, tenant, facts_drain._TIMEOUTS_PER_MEMORY)
        assert r == {"processed": 1, "failed": 1, "claimed": 2}
        assert self._status(db, [slow]) == ["error"]
        assert facts_drain._drain_paused() == 0, "the drain rested for a memory that is the cause"
        assert not facts_drain._timeouts

    def test_a_provider_that_times_out_for_everybody_files_nothing(self, db, tenant, monkeypatch):
        """No memory written between the timeouts: an outage, not the memory."""
        ids = make_memories(db, tenant, 2)
        self._write(monkeypatch, failure=_timed_out())
        for _ in range(facts_drain._TIMEOUTS_PER_MEMORY + 2):
            facts_drain._pause_of(None)["until"] = 0.0
            facts_service.process_pending_memories(db, limit=10)
        assert self._status(db, ids) == ["pending", "pending"]

    @pytest.mark.parametrize("failure", [
        _http(403, "Forbidden"), _http(429, "Too Many Requests"), _http(503, "Service Unavailable"),
        ConnectionResetError(104, "Connection reset by peer"),
    ])
    def test_a_memory_refused_every_time_is_filed_in_the_end(self, db, tenant, monkeypatch,
                                                              failure):
        """Audit of 18 Oct 2026: a status says who refused, not why (a
        moderated model's 403 to one input, a 429 for one request above a
        low tier's tokens per minute, a 502 for an input the upstream crashes
        on). Refused while the region wrote the others, the memory came back
        on every pass and rested the region for every account, for ever. Now
        it counts as a timeout does: the region rests on its first refusal
        only, and the fifth files it."""
        slow = self._one_slow_memory(db, tenant, monkeypatch, failure)
        r = self._a_round(db, tenant, 1)
        assert r["processed"] == 1 and self._status(db, [slow]) == ["pending"]
        assert facts_drain._drain_paused() > 0, "alone in its region, the first refusal rests"
        for day in range(2, facts_drain._TIMEOUTS_PER_MEMORY):
            r = self._a_round(db, tenant, day)
            assert r["processed"] == 1 and self._status(db, [slow]) == ["pending"]
            assert facts_drain._drain_paused() == 0, "the region rested for the memory's refusal"
        r = self._a_round(db, tenant, facts_drain._TIMEOUTS_PER_MEMORY)
        assert r == {"processed": 1, "failed": 1, "claimed": 2}
        assert self._status(db, [slow]) == ["error"] and not facts_drain._timeouts

    @pytest.mark.parametrize("failure", [_http(401, "Unauthorized"), _http(402, "Payment Required")])
    def test_a_key_or_a_credit_refused_for_everybody_files_nothing(self, db, tenant, monkeypatch,
                                                                    failure):
        """Nobody written between the refusals: the install's, not the memory's
        (the hosted product's BUG-351)."""
        ids = make_memories(db, tenant, 2)
        self._write(monkeypatch, failure=failure)
        for _ in range(facts_drain._TIMEOUTS_PER_MEMORY + 2):
            facts_drain._pause_of(None)["until"] = 0.0
            facts_service.process_pending_memories(db, limit=10)
        assert self._status(db, ids) == ["pending", "pending"]

    # Audit of 19 Oct 2026 (the rule: a refused memory goes to 'error'
    # after five attempts, and its region's drain does not stop). The FIRST
    # refusal of any memory rested the region for every account: one account
    # writing one fresh memory per pass that the writer refuses, for its
    # content, stopped everybody else's, each memory counted once and never
    # filed.

    def _account(self, db, name):
        from korely_agent.db.models import Tenant

        t = Tenant(id=uuid.uuid4(), name=name)
        db.add(t)
        db.commit()
        return t

    def _refused_and_written(self, monkeypatch, failure):
        """A memory whose content starts "refused" fails with `failure` at
        once; any other is written a moment later, as the real one writes."""
        from sqlalchemy.orm.attributes import set_committed_value

        from korely_agent.core import llm
        from korely_agent.services.memory_core import worker

        def extract(db_, tenant_id, *, memory, valid_from=None, model_name=None, claim=None):
            if memory.content.startswith("refused"):
                llm._failed(failure)
                return []  # status left 'processing', as the real one does
            time.sleep(0.2)
            assert worker.close_the_claim(db_, claim, "done")
            db_.commit()
            set_committed_value(memory, "graph_sync_status", "done")
            return []

        monkeypatch.setattr(facts_service, "extract_and_persist_facts", extract)

    @pytest.mark.parametrize("failure", [
        _http(403, "Forbidden"), _http(429, "Too Many Requests"), _http(503, "Service Unavailable"),
        _http(504, "Gateway Timeout"), _timed_out(), ConnectionResetError(104, "Connection reset"),
    ])
    def test_one_accounts_fresh_refused_memories_never_rest_the_region(self, db, tenant,
                                                                         monkeypatch, failure):
        from datetime import datetime, timezone

        other = self._account(db, "Other")
        self._refused_and_written(monkeypatch, failure)
        first = None
        for k in range(facts_drain._TIMEOUTS_PER_MEMORY):
            # One fresh refused memory of `tenant` each pass, dated before
            # everything else so it is claimed at once, and one ordinary
            # memory of the other account.
            bad = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="a",
                              content=f"refused {k}", graph_sync_status="pending",
                              event_time=datetime(2000 - k, 1, 1, tzinfo=timezone.utc))
            good = AgentMemory(id=uuid.uuid4(), tenant_id=other.id, end_user_id="b",
                               content=f"memory {k}", graph_sync_status="pending",
                               event_time=datetime(2020, 1, 1 + k, tzinfo=timezone.utc))
            db.add_all([bad, good])
            db.commit()
            first = first or bad.id
            facts_service.process_pending_memories(db, limit=20)
            assert facts_drain._drain_paused() == 0, f"pass {k}: one account's memory rested the region"
            assert self._status(db, [good.id]) == ["done"], f"pass {k}: the other account waited"
            if k < facts_drain._TIMEOUTS_PER_MEMORY - 1:
                assert self._status(db, [first]) == ["pending"]
        assert self._status(db, [first]) == ["error"], "the refused memory was never filed"

    @pytest.mark.parametrize("failure", [
        _http(403, "Forbidden"), _http(429, "Too Many Requests"), _timed_out(),
    ])
    def test_two_accounts_refused_memories_do_not_keep_the_region_resting(
            self, db, tenant, monkeypatch, failure):
        """Audit of 20 Oct 2026 (the residual of e105c0c): two accounts' refused memories,
        dated early, were claimed first, filled every group and rested the
        region at every pass; nothing was written, so nothing ended the rest,
        no memory was counted twice and none was filed, and every other
        account's memories waited. The rule: a refused memory goes
        to 'error' after five attempts, and its region's drain does not stop."""
        from datetime import datetime, timezone

        self._refused_and_written(monkeypatch, failure)
        refused = []
        for name in ("First", "Second"):
            bad = self._account(db, name)
            for user in ("x", "y"):
                m = AgentMemory(id=uuid.uuid4(), tenant_id=bad.id, end_user_id=user,
                                content=f"refused {name} {user}", graph_sync_status="pending",
                                event_time=datetime(1990, 1, 1, tzinfo=timezone.utc))
                db.add(m)
                refused.append(m.id)
        db.commit()
        written_late = 0
        for k in range(12):
            # The rest, if any, has passed; one ordinary memory of the
            # victim account arrives each pass.
            facts_drain._pause_of(None)["until"] = 0.0
            good = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="v",
                               content=f"memory {k}", graph_sync_status="pending",
                               event_time=datetime(2020, 1, 1 + k, tzinfo=timezone.utc))
            db.add(good)
            db.commit()
            facts_service.process_pending_memories(db, limit=20)
            lanes = getattr(facts_drain, "_draining", None)   # absent before the audit of 20 Oct 2026
            if lanes is not None:
                wait_idle(lanes, 10)
            if self._status(db, [good.id]) != ["done"]:
                written_late += 1
            if self._status(db, refused) == ["error"] * 4:
                break
        assert written_late <= 2, f"the victim waited {written_late} passes"
        assert self._status(db, refused) == ["error"] * 4, "the refused memories were never filed"
        assert facts_drain._drain_paused() == 0

    @pytest.mark.parametrize("failure", [
        _http(403, "Forbidden"), _http(503, "Service Unavailable"), _timed_out(),
    ])
    def test_a_writer_down_for_every_account_still_rests(self, db, tenant, monkeypatch, failure):
        """Every call fails, for memories of two accounts: the writer is
        down, the region rests (after the second account's failure), longer
        each time, and nothing is filed."""
        other = self._account(db, "Other")
        ids = make_memories(db, tenant, 2) + make_memories(db, other, 2)
        self._write(monkeypatch, failure=failure)
        r = facts_service.process_pending_memories(db, limit=10)
        assert r["failed"] == 0 and self._status(db, ids) == ["pending"] * 4
        assert facts_drain._drain_paused() > 0
        assert facts_service.process_pending_memories(db, limit=10)["claimed"] == 0
        waits = []
        for _ in range(facts_drain._TIMEOUTS_PER_MEMORY + 2):
            facts_drain._pause_of(None)["until"] = 0.0
            facts_service.process_pending_memories(db, limit=10)
            waits.append(round(facts_drain._drain_paused()))
        assert waits[0] <= 120 < waits[1] <= 300 < waits[2], waits
        assert self._status(db, ids) == ["pending"] * 4

    def test_a_key_a_credit_or_a_brake_rests_at_once_whoever_waits(self, db, tenant):
        """401 (the key), 402 (the credit) and a call this machine did not
        make are the writer's whatever the memory says: the region rests at
        the first one, while another account waits. A 403 or a timeout
        alone, with another account waiting, does not."""
        from korely_agent.core import llm

        other = self._account(db, "Other")
        make_memories(db, other, 1)
        assert facts_drain._the_writers_failure(db, None, tenant.id, 401, llm.REFUSED)
        assert facts_drain._the_writers_failure(db, None, tenant.id, 402, llm.REFUSED)
        assert facts_drain._the_writers_failure(db, None, tenant.id, None, llm.NOT_CALLED)
        assert not facts_drain._the_writers_failure(db, None, tenant.id, 403, llm.REFUSED)
        assert not facts_drain._the_writers_failure(db, None, tenant.id, None, llm.TIMED_OUT)
        # Nobody else waits: the rest costs nobody, and it rests as before.
        alone = self._account(db, "Alone")
        facts_drain._pauses.clear()
        db.query(AgentMemory).filter(AgentMemory.tenant_id == other.id).update(
            {AgentMemory.graph_sync_status: "done"})
        db.commit()
        assert facts_drain._the_writers_failure(db, None, alone.id, 403, llm.REFUSED)

    def test_memories_written_in_another_region_do_not_count(self, db, tenant, monkeypatch):
        """Europe's writer refusing while Global writes: no European memory is
        counted against for what the Global projects did."""
        from korely_agent.core import llm

        mid = uuid.uuid4()
        failure = _http(429, "Too Many Requests")
        for _ in range(facts_drain._TIMEOUTS_PER_MEMORY + 2):
            facts_drain._a_memory_was_written("global")
            assert facts_drain._times_out_on_it(mid, llm.failure_kind(failure, 429), "eu") is False
        assert facts_drain._timeouts[mid][0] == 1

    def test_a_memory_written_in_the_end_starts_from_zero(self, db, tenant, monkeypatch):
        slow = self._one_slow_memory(db, tenant, monkeypatch, _timed_out())
        for day in range(1, facts_drain._TIMEOUTS_PER_MEMORY):
            self._a_round(db, tenant, day)
        assert facts_drain._timeouts[slow][0] == facts_drain._TIMEOUTS_PER_MEMORY - 1
        self._write(monkeypatch, failure=None)
        self._a_round(db, tenant, facts_drain._TIMEOUTS_PER_MEMORY)
        assert self._status(db, [slow]) == ["done"] and not facts_drain._timeouts

    def test_a_refusal_is_logged_by_its_kind_never_its_words(self, db, tenant, monkeypatch,
                                                             caplog):
        """The provider's own words came from `last_failure_here()` into the
        log line, and a provider's error can quote the request, the end
        user's memory (audit of 17 Oct 2026)."""
        import logging

        make_memories(db, tenant, 1)
        self._write(monkeypatch, failure=_ApiError(429, "quota of zz-marker-maria@example.test"))
        with caplog.at_level(logging.WARNING):
            facts_service.process_pending_memories(db, limit=10)
        assert "writer refused (refused, HTTP 429)" in caplog.text, "the case was not reached"
        assert "zz-marker" not in caplog.text

    def test_a_memory_filed_for_its_timeouts_is_logged_without_the_words(self, db, tenant,
                                                                         monkeypatch, caplog):
        import logging

        slow = self._one_slow_memory(db, tenant, monkeypatch,
                                     _http(504, "Gateway Timeout for zz-marker-maria@example.test"))
        with caplog.at_level(logging.WARNING):
            for day in range(1, facts_drain._TIMEOUTS_PER_MEMORY + 1):
                self._a_round(db, tenant, day)
        assert self._status(db, [slow]) == ["error"]
        assert "filed as an error" in caplog.text and "HTTP 504" in caplog.text
        assert "zz-marker" not in caplog.text

    def test_a_broken_local_embedder_is_waited_for(self, db, tenant, monkeypatch):
        """LoCoMo trial3, 2026-10-06: the model's folder emptied, every new
        relation without a vector, every such write rolled back, and 55
        memories filed 'error' for `manage reextract`. The real path: the
        write asks this machine's embedder, which cannot answer."""
        from korely_agent.core import llm, local_embedder

        def broken(text, prompt):
            raise local_embedder.Unavailable("the model is not in /opt/korely-models (missing onnx)")

        monkeypatch.setattr(local_embedder, "embed", broken)
        before = dict(llm._last_call)

        def extract(db_, tenant_id, *, memory, valid_from=None, model_name=None, claim=None):
            assert llm._embed_here("a new relation", llm.TASK_SIMILARITY) is None
            return []  # the write rolled back: status left 'processing'

        monkeypatch.setattr(facts_service, "extract_and_persist_facts", extract)
        ids = make_memories(db, tenant, 2)
        r = facts_service.process_pending_memories(db, limit=10)
        assert self._status(db, ids) == ["pending", "pending"]
        assert r["failed"] == 0 and facts_drain._drain_paused() > 0
        assert llm._last_call == before, "the extraction model did not fail: /health keeps it"


class TestEachAccountItsShare:
    """On the hosted service every customer is an account of one install
    (8 Oct 2026, the one codebase). The cap per end user works inside an
    account; across accounts an account with many end users, or with a
    backlog dated years back, took every claim. Now each account waiting
    gets an equal share of the claim, its end users capped as before."""

    def _account(self, db, name):
        from korely_agent.db.models import Tenant

        t = Tenant(id=uuid.uuid4(), name=name)
        db.add(t)
        db.commit()
        return t

    def _pending(self, db, tenant, users, per_user, year):
        from datetime import datetime, timedelta, timezone

        t0 = datetime(year, 1, 1, 9, 0, tzinfo=timezone.utc)
        for u in range(users):
            for k in range(per_user):
                db.add(AgentMemory(
                    id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=f"user{u}",
                    content=f"{tenant.name} user{u} memory {k}", graph_sync_status="pending",
                    event_time=t0 + timedelta(minutes=k)))
        db.commit()

    def _claimed(self, db):
        rows = db.query(AgentMemory).filter(AgentMemory.graph_sync_status == "processing").all()
        out = {}
        for m in rows:
            out[m.tenant_id] = out.get(m.tenant_id, 0) + 1
        return out, rows

    def test_an_old_import_does_not_hold_another_account(self, db):
        importer = self._account(db, "Importer")
        newcomer = self._account(db, "Newcomer")
        # Ten end users with ten memories each, dated 2019: event order alone
        # gives every claim to them.
        self._pending(db, importer, users=10, per_user=10, year=2019)
        self._pending(db, newcomer, users=1, per_user=3, year=2026)
        facts_drain._claim(db, 20)
        per_account, _ = self._claimed(db)
        # A share of ten each: the newcomer's three are all in, the importer
        # takes its ten, and the claim is short (the drain claims again).
        assert per_account == {importer.id: 10, newcomer.id: 3}

    def test_two_busy_accounts_split_the_claim(self, db):
        a = self._account(db, "A")
        b = self._account(db, "B")
        self._pending(db, a, users=8, per_user=10, year=2019)
        self._pending(db, b, users=8, per_user=10, year=2026)
        facts_drain._claim(db, 20)
        per_account, rows = self._claimed(db)
        assert per_account == {a.id: 10, b.id: 10}
        # Inside an account, still at most five of an end user, its oldest.
        per_person = {}
        for m in rows:
            per_person.setdefault((m.tenant_id, m.end_user_id), []).append(int(m.content.rsplit(" ", 1)[1]))
        assert all(sorted(v) == list(range(len(v))) and len(v) <= facts_drain._PER_END_USER
                   for v in per_person.values())

    def test_one_account_claims_as_before(self, db, tenant):
        """An install of one's own has one account: the claim of before,
        twenty out of five end users' queues."""
        self._pending(db, tenant, users=5, per_user=10, year=2026)
        assert facts_drain._each_account_its_share(db, 20) is None
        assert len(facts_drain._claim(db, 20)) == 20

    def test_a_forgotten_memory_is_not_an_account_waiting(self, db, tenant):
        from datetime import datetime, timezone

        gone = self._account(db, "Gone")
        self._pending(db, gone, users=1, per_user=1, year=2019)
        db.query(AgentMemory).filter(AgentMemory.tenant_id == gone.id).update(
            {AgentMemory.deleted_at: datetime.now(timezone.utc)})
        db.commit()
        self._pending(db, tenant, users=5, per_user=10, year=2026)
        assert facts_drain._each_account_its_share(db, 20) is None


def test_a_failure_is_classified_by_its_fields_never_its_words():
    """core/llm.failure_kind (audit of 17 Oct 2026): the same words with
    another status decide otherwise, and other words with the same class
    decide the same."""
    from korely_agent.core import llm

    assert llm.failure_kind(None, 429) == llm.REFUSED
    assert llm.failure_kind(None, 400) is None
    assert llm.failure_kind(None, 500) is None
    assert llm.failure_kind(None, 504) == llm.TIMED_OUT
    assert llm.failure_kind(ValueError("429 quota timed out Connection refused")) is None
    assert llm.failure_kind(_timed_out()) == llm.TIMED_OUT
    assert llm.failure_kind(TimeoutError()) == llm.TIMED_OUT
    import httpx

    assert llm.failure_kind(httpx.ReadTimeout("x")) == llm.TIMED_OUT
    assert llm.failure_kind(httpx.RemoteProtocolError("x")) == llm.UNREACHABLE
    assert llm.failure_kind(ConnectionRefusedError()) == llm.UNREACHABLE


def test_a_group_left_running_is_logged_by_ids_and_counts(db, tenant, monkeypatch, caplog):
    """The parallel drain named the groups still running by the end user's
    id, at ERROR, in the container's log (audit of 17 Oct 2026)."""
    import logging

    monkeypatch.setattr(facts_drain, "_group_timeout", lambda: 0.2)
    monkeypatch.setattr(facts_drain, "_run_group_in_own_session",
                        lambda *a, **k: time.sleep(1) or (0, 0))

    for who in ("zz-marker-maria@example.test", "zz-marker-anna@example.test"):
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=who,
                           content="x", graph_sync_status="pending"))
    db.commit()
    with caplog.at_level(logging.ERROR):
        facts_service.process_pending_memories(db, limit=10)
        time.sleep(0.4)
        facts_drain._draining.running()   # past their deadline: given up on, logged
    assert "still running" in caplog.text, "the case was not reached"
    assert "zz-marker" not in caplog.text


class TestATickDoesNotWaitForTheSlowestGroup:
    """Audit of 20 Oct 2026 (the class of the webhooks' delivery round that
    waited for the slowest account): a tick started its groups on a pool of its own and
    waited for the last, so one account's long group set the pace of every
    account's next claim while the other threads sat idle."""

    def test_another_accounts_memories_are_written_while_one_group_runs_long(
            self, db, tenant, monkeypatch):
        from korely_agent.db.models import Tenant

        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        release = threading.Event()
        written = []

        def drain_one(gdb, m, region=None):
            if m.tenant_id == tenant.id:
                release.wait(30)            # a group that runs long
            m.graph_sync_status = "done"
            gdb.commit()
            written.append(m.content)
            return True

        monkeypatch.setattr(facts_drain, "_drain_one_memory", drain_one)
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="slow",
                           content="long", graph_sync_status="pending"))
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=other.id, end_user_id="b",
                           content="b0", graph_sync_status="pending"))
        db.commit()
        try:
            started = time.monotonic()
            facts_service.process_pending_memories(db, limit=20)
            assert time.monotonic() - started < 5, "the tick waited for the long group"
            assert written == ["b0"]
            db.add(AgentMemory(id=uuid.uuid4(), tenant_id=other.id, end_user_id="b",
                               content="b1", graph_sync_status="pending"))
            db.commit()
            started = time.monotonic()
            facts_service.process_pending_memories(db, limit=20)
            assert time.monotonic() - started < 5
            assert written == ["b0", "b1"], "the next claim waited for the long group"
        finally:
            release.set()
        assert wait_idle(facts_drain._draining, 10)
        assert written[-1] == "long"


class TestAFailedMemoryDoesNotHoldItsPerson(TestEndUsersAreWorkedTogether):
    """Review of C2: a memory that fails outside the engine's handling stayed
    'processing', and the person exclusion held its person for
    DRAIN_STALE_MINUTES, again and again."""

    def test_a_memory_failing_outside_the_engine_is_filed_and_releases_its_person(
            self, db, tenant, monkeypatch):
        self._backlog(db, tenant, [8])

        def drain_one(gdb, m, region=None):
            if m.content == "user0 memory 0":
                raise RuntimeError("boom outside the engine")
            m.graph_sync_status = "done"
            gdb.commit()
            return True

        monkeypatch.setattr(facts_drain, "_drain_one_memory", drain_one)
        monkeypatch.setattr(facts_drain, "_ROOM_SECONDS", 0.3)
        facts_service.process_pending_memories(db, limit=20)
        assert wait_idle(facts_drain._draining, 10)
        db.expire_all()
        st = {m.content: m.graph_sync_status for m in db.query(AgentMemory).all()}
        assert st["user0 memory 0"] == "error"
        second = facts_service.process_pending_memories(db, limit=20)
        assert second["claimed"] == 3, f"the person is held by its failed memory: {second}, {st}"

    def test_a_group_raising_outside_its_memories_files_its_rows(self, db, tenant, monkeypatch):
        self._backlog(db, tenant, [3])

        def group_raises(*a, **k):
            raise RuntimeError("boom around the group")

        monkeypatch.setattr(facts_drain, "_run_group", group_raises)
        monkeypatch.setattr(facts_drain, "_ROOM_SECONDS", 0.3)
        facts_service.process_pending_memories(db, limit=20)
        assert wait_idle(facts_drain._draining, 10)
        db.expire_all()
        assert {m.graph_sync_status for m in db.query(AgentMemory).all()} == {"error"}


def test_a_batch_job_erased_after_another_failed_does_not_end_the_pass(db, engine, tenant,
                                                                        monkeypatch):
    """Two accounts, one claimed import each. The first one's slice raises,
    and its rollback expires every job in the session; the second one's
    project is erased meanwhile. The pass read the second job's `job.id`
    outside any try, the ORM raised ObjectDeletedError and the pass ended
    for every account, the first job left 'processing' until the reaper
    (30 Oct 2026). The pass goes on: the failed job waits for its retry, the
    erased one is no longer there to finish."""
    from sqlalchemy.orm import Session

    from korely_agent.db.models import AgentBatchJob, Tenant
    from korely_agent.services import batch

    other = Tenant(id=uuid.uuid4(), name="Other")
    db.add(other)
    db.commit()
    first = batch.enqueue_batch(db, tenant.id, [{"content": "mine", "user_id": "u1"}])
    second = batch.enqueue_batch(db, other.id, [{"content": "theirs", "user_id": "u1"}])
    first_id, second_id = first.id, second.id

    def import_one(db_, tenant_id, item, **k):
        # The second job's erasure, committed while the first one is worked.
        with Session(bind=engine) as erasure:
            erasure.query(AgentBatchJob).filter(AgentBatchJob.id == second_id).delete()
            erasure.commit()
        raise RuntimeError("the first job's item fails")

    monkeypatch.setattr(batch, "_import_one", import_one)
    batch.process_batch_jobs(db, limit=5)

    db.expire_all()
    assert db.get(AgentBatchJob, second_id) is None
    failed = db.get(AgentBatchJob, first_id)
    assert failed.status == batch.STATUS_FAILED and failed.next_retry_at is not None
    assert db.query(AgentBatchJob).filter(
        AgentBatchJob.status == batch.STATUS_PROCESSING).count() == 0
