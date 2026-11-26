"""A memory erased while its facts are extracted is not an error (1.0 fixes,
27 Oct 2026; Notion row on StaleDataError at the flush of `_the_shortlist`).

The observation written on the memory's row finds no row at the next flush
(`erase_memory`, from another session): the write is rolled back and nothing
is left, as ever, but the log carried an ERROR with a stack for an expected
race. Now the engine reads the database: if the memory's row is gone it is one
INFO line with ids only; any other StaleDataError stays an ERROR.
"""
import logging
import uuid

from sqlalchemy import delete
from sqlalchemy.orm.exc import StaleDataError

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.memory_core import worker as lav


def _mem(db, tenant):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, content="x", graph_sync_status="processing")
    db.add(m)
    db.commit()
    return m


def _write(db, m, erase):
    def write_one(ef, defer):
        m.content = "observation"          # dirty: the next flush UPDATEs the row
        if erase:
            with db.get_bind().begin() as other:                  # another session's erasure, committed
                other.execute(delete(AgentMemory.__table__).where(AgentMemory.__table__.c.id == m.id))
        db.flush()
        return None, []
    return lav.write_memory(db, m, ["ef"], write_one=write_one, shape_of=lambda f, c: {})


def test_an_erased_memory_is_one_info_line_with_ids_only(db, tenant, caplog):
    m = _mem(db, tenant)
    mid = str(m.id)
    with caplog.at_level(logging.DEBUG):
        assert _write(db, m, erase=True) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1 and mid in infos[0] and "Traceback" not in infos[0] and "stack" not in infos[0]
    assert db.query(AgentMemory).filter(AgentMemory.id == uuid.UUID(mid)).count() == 0


def test_a_stale_row_of_a_memory_that_exists_stays_an_error(db, tenant, caplog):
    m = _mem(db, tenant)

    def write_one(ef, defer):
        raise StaleDataError("UPDATE statement expected to update 1 row(s); 0 were matched")

    with caplog.at_level(logging.DEBUG):
        assert lav.write_memory(db, m, ["ef"], write_one=write_one, shape_of=lambda f, c: {}) == []
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_an_erased_memory_that_was_not_changed_fails_on_its_fact_and_is_info_too(db, tenant, caplog):
    """The row is not dirty: the first statement to fail is the INSERT of the
    fact, a foreign-key violation, not a stale UPDATE."""
    m = _mem(db, tenant)
    mid = m.id

    def write_one(ef, defer):
        with db.get_bind().begin() as other:
            other.execute(delete(AgentMemory.__table__).where(AgentMemory.__table__.c.id == mid))
        db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="maria",
                         subject_label="maria", subject_canonical="maria",
                         predicate_raw="lives_in", predicate_canonical="lives_in",
                         object_label="Rome", object_canonical="Rome", confidence=0.9,
                         source_memory_id=mid, source_memory_ids=[str(mid)]))
        db.flush()
        return None, []

    with caplog.at_level(logging.DEBUG):
        assert lav.write_memory(db, m, ["ef"], write_one=write_one, shape_of=lambda f, c: {}) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [str(mid) in r.getMessage() for r in caplog.records if r.levelno == logging.INFO] == [True]


def test_a_lost_claim_on_an_erased_memory_is_info_and_does_not_raise(db, tenant, caplog):
    m = _mem(db, tenant)
    m.graph_sync_status = "pending"
    db.commit()
    mid = m.id
    p = lav.claim_this_one(db, m)
    assert p is not None

    def write_one(ef, defer):
        with db.get_bind().begin() as other:
            other.execute(delete(AgentMemory.__table__).where(AgentMemory.__table__.c.id == mid))
        return None, []

    with caplog.at_level(logging.DEBUG):
        assert lav.write_memory(db, m, ["ef"], write_one=write_one, shape_of=lambda f, c: {}, claim=p) == []
    assert p.outcome == "lost"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_synchronous_write_of_an_erased_memory_does_not_answer_500(db, tenant, monkeypatch, caplog):
    from korely_agent.services import facts as agent_facts
    from korely_agent.services.facts import service

    m = _mem(db, tenant)
    m.graph_sync_status = "pending"
    db.commit()
    mid = m.id
    assert lav.claim_this_one(db, m) is not None

    def crash(*a, **k):
        with db.get_bind().begin() as other:
            other.execute(delete(AgentMemory.__table__).where(AgentMemory.__table__.c.id == mid))
        db.rollback()                                   # expires the object, as a failed flush does
        raise RuntimeError("extraction crashed")

    monkeypatch.setattr(agent_facts, "extract_and_persist_facts", crash)
    with caplog.at_level(logging.DEBUG):
        assert service.extract_inline(db, tenant.id, m) == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
