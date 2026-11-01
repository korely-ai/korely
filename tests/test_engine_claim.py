"""memory_core 2.11.0, the claim token (code audit of 6 October; a review
of 2.10.0).

Every exit from 'processing' is a compare-and-set on what the worker read when
it took the memory (`worker.Claim`): the write of a memory forgotten or edited
in the meantime goes back whole, and a forgotten memory is never taken.
Optimistic control (Kung and Robinson, ACM TODS 1981). The real race with
SKIP LOCKED and the row lock is tested on Postgres; here the sequence of
events, in the same session.

No model call.
"""
# Port notes: on korely-agent's tables (`AgentMemory`, `AgentFact`, tenant_id)
# and on PostgreSQL; the fair claim's person is (tenant_id, end_user_id). The
# engine's loggers are named after this package.
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.memory_core import worker


T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _mem(db, tenant, status="pending", text="Maria works at Acme.", **k):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="maria", content=text,
                    graph_sync_status=status, **k)
    db.add(m)
    db.commit()
    return m


def _status(db, m):
    db.expire_all()
    return db.get(AgentMemory, m.id).graph_sync_status


def _real_fact(db, tenant, m):
    """A real fact row written by the write, so a rollback can be seen."""
    f = AgentFact(tenant_id=tenant.id, end_user_id="maria", subject_label="Maria", predicate_raw="works at",
                  predicate_canonical="works_at", object_label="Acme", source_memory_id=m.id,
                  source_memory_ids=[str(m.id)])
    db.add(f)
    db.flush()
    return f, []


def _shape(fact, closed):
    return {"id": fact.id}


class TestTheClaimDoesNotTakeTheForgotten:
    def test_a_forgotten_queued_memory_is_not_taken_and_goes_to_rest(self, db, tenant):
        alive = _mem(db, tenant)
        forgotten = _mem(db, tenant, deleted_at=datetime.now(timezone.utc))
        taken = worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
        assert [m.id for m in taken] == [alive.id]
        assert _status(db, forgotten) == worker.FORGOTTEN

    def test_the_reaper_does_not_requeue_a_forgotten_one(self, db, tenant):
        stuck = _mem(db, tenant, "processing", graph_sync_claimed_at=T0,
                        deleted_at=datetime.now(timezone.utc))
        assert worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15) == []
        assert _status(db, stuck) == worker.FORGOTTEN

    def test_the_fair_claim_leaves_out_the_forgotten(self, db, tenant):
        _mem(db, tenant, deleted_at=datetime.now(timezone.utc))
        alive = _mem(db, tenant)
        taken = worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15,
                           person=(AgentMemory.tenant_id, AgentMemory.end_user_id), per_person=1)
        assert [m.id for m in taken] == [alive.id]


class TestTheToken:
    def test_claim_reads_the_row_and_says_none_if_it_is_no_longer_claimed(self, db, tenant):
        m = _mem(db, tenant)
        assert worker.claim(db, m) is None                     # pending: not a claim
        worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
        p = worker.claim(db, m)
        assert p is not None and p.text == "Maria works at Acme." and p.when is not None
        db.get(AgentMemory, m.id).deleted_at = datetime.now(timezone.utc)
        db.commit()
        assert worker.claim(db, m) is None                     # forgotten

    def test_release_only_its_own_claim(self, db, tenant):
        m = _mem(db, tenant)
        worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
        p = worker.claim(db, m)
        # Another drain took it again after the reaper: another claim time.
        row = db.get(AgentMemory, m.id)
        row.graph_sync_claimed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        db.commit()
        assert worker.release(db, p) is False
        assert _status(db, m) == "processing"

    def test_requeue_does_not_touch_the_forgotten(self, db, tenant):
        a = _mem(db, tenant, "processing", graph_sync_claimed_at=datetime.now(timezone.utc))
        b = _mem(db, tenant, worker.FORGOTTEN, deleted_at=datetime.now(timezone.utc))
        assert worker.requeue(db, [a, b]) == 1
        assert _status(db, a) == "pending" and _status(db, b) == worker.FORGOTTEN


class TestTheWriteChecksTheClaim:
    def test_with_the_claim_still_its_own_it_writes_done(self, db, tenant):
        m = _mem(db, tenant)
        worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
        p = worker.claim(db, m)
        out = worker.write_memory(db, m, ["ef"], write_one=lambda ef, d: _real_fact(db, tenant, m),
                                 shape_of=_shape, claim=p)
        assert len(out) == 1 and p.outcome == "done" and _status(db, m) == "done"
        assert db.query(AgentFact).filter(AgentFact.source_memory_id == m.id).count() == 1

    def test_an_edit_during_extraction_cancels_the_write(self, db, tenant):
        m = _mem(db, tenant)
        worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
        p = worker.claim(db, m)
        # The PATCH lands while the model reads the old text.
        row = db.get(AgentMemory, m.id)
        row.content, row.graph_sync_status, row.graph_sync_claimed_at = "Maria works at Globex.", "pending", None
        db.commit()
        out = worker.write_memory(db, m, ["ef"], write_one=lambda ef, d: _real_fact(db, tenant, m),
                                 shape_of=_shape, claim=p)
        assert out == [] and p.outcome == "lost"
        db.expire_all()
        row = db.get(AgentMemory, m.id)
        assert row.graph_sync_status == "pending" and row.content == "Maria works at Globex."
        assert db.query(AgentFact).count() == 0, "no fact of the old text"

    def test_a_forgetting_during_extraction_cancels_the_write(self, db, tenant):
        m = _mem(db, tenant)
        worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
        p = worker.claim(db, m)
        db.get(AgentMemory, m.id).deleted_at = datetime.now(timezone.utc)
        db.commit()
        out = worker.write_memory(db, m, ["ef"], write_one=lambda ef, d: _real_fact(db, tenant, m),
                                 shape_of=_shape, claim=p)
        assert out == [] and p.outcome == "lost" and db.query(AgentFact).count() == 0

    def test_the_retirement_of_the_old_version_also_goes_back(self, db, tenant):
        """An edited memory re-extracted: the facts of its previous version
        are retired in the write's transaction (2.3.0). If the memory changed
        again under that write, the retirement goes back with it: the facts
        stay as they were until the newest text is extracted."""
        from korely_agent.services.facts.entities import TABLES
        from korely_agent.services.facts.temporal import true_at
        from korely_agent.services.memory_core.store import retire_the_previous_version

        m = _mem(db, tenant)
        old, _ = _real_fact(db, tenant, m)
        db.commit()
        worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
        p = worker.claim(db, m)
        db.get(AgentMemory, m.id).graph_sync_status = "pending"   # edited again
        db.commit()
        worker.write_memory(
            db, m, [], write_one=lambda ef, d: None, shape_of=_shape, claim=p,
            retire=lambda kept: retire_the_previous_version(
                db, TABLES, m.id, kept, when=datetime.now(timezone.utc), is_open=true_at()),
        )
        assert p.outcome == "lost" and _status(db, m) == "pending"
        assert db.get(AgentFact, old.id).invalid_at is None

    def test_without_a_claim_as_before(self, db, tenant):
        m = _mem(db, tenant, "processing")
        worker.write_memory(db, m, [], write_one=lambda ef, d: None, shape_of=_shape)
        assert _status(db, m) == "done"


class TestDrainOne:
    def test_skips_a_memory_that_is_no_longer_claimed(self, db, tenant):
        m = _mem(db, tenant)
        worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
        db.get(AgentMemory, m.id).graph_sync_status = "pending"   # edited meanwhile
        db.commit()
        calls = []
        assert worker.drain_one(db, m, lambda mm, ev: calls.append(mm)) is True
        assert calls == [] and _status(db, m) == "pending"

    def test_does_not_file_an_error_on_someone_elses_claim(self, db, tenant):
        m = _mem(db, tenant)
        worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)

        def do_it(mm, ev):
            # Meanwhile the reaper gave it back and a second drain took it.
            row = db.get(AgentMemory, m.id)
            row.graph_sync_claimed_at = datetime.now(timezone.utc) + timedelta(seconds=5)
            db.commit()
            raise RuntimeError("the judge does not answer")

        assert worker.drain_one(db, m, do_it) is True
        assert _status(db, m) == "processing", "the second drain's claim is untouched"

    def test_the_error_goes_to_the_log_with_the_stack(self, db, tenant, caplog):
        """With the stack (file, line and function of every frame). 3.0.0
        (code audit of 6 October): without the message, which for a database
        error carries the row's values, and without `exc_info`, which the
        formatter prints."""
        m = _mem(db, tenant)
        worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
        message = "Maria " + "has type 2 diabetes"

        def do_it(mm, ev):
            raise RuntimeError(message)

        with caplog.at_level(logging.WARNING, logger="korely_agent.services.memory_core.worker"):
            assert worker.drain_one(db, m, do_it) is False
        assert _status(db, m) == "error"
        [row] = [r for r in caplog.records if "async pending process failed" in r.getMessage()]
        text = row.getMessage()
        assert "RuntimeError" in text and "in do_it" in text and "worker.py" in text, "M5: the stack"
        assert "diabetes" not in caplog.text and row.exc_info is None


class TestInline:
    def test_takes_the_memory_and_gives_it_back_to_the_queue_if_it_does_not_close_it(self, db, tenant):
        m = _mem(db, tenant)
        seen = []

        def do_it(p):
            seen.append((_status(db, m), p.text))
            return ["failed"]

        assert worker.inline(db, m, do_it) == ["failed"]
        assert seen == [("processing", "Maria works at Acme.")]
        assert _status(db, m) == "pending"

    def test_a_memory_claimed_by_the_drain_is_not_extracted_inline(self, db, tenant):
        """The drain holds it, the inline write leaves it to the drain."""
        m = _mem(db, tenant)
        worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
        calls = []
        assert worker.inline(db, m, lambda p: calls.append(p)) is None
        assert calls == [] and _status(db, m) == "processing"

    def test_an_exception_gives_the_memory_back_to_the_queue(self, db, tenant):
        m = _mem(db, tenant)

        def do_it(p):
            raise RuntimeError("down")

        with pytest.raises(RuntimeError):
            worker.inline(db, m, do_it)
        assert _status(db, m) == "pending"

    def test_written_with_the_claim_stays_done(self, db, tenant):
        m = _mem(db, tenant)

        def do_it(p):
            return worker.write_memory(db, m, ["ef"],
                                      write_one=lambda ef, d: _real_fact(db, tenant, m),
                                      shape_of=_shape, claim=p)

        assert len(worker.inline(db, m, do_it)) == 1
        assert _status(db, m) == "done"
