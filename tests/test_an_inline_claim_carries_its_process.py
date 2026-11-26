"""An inline extraction's claim carries its process's key (1.0.40).

With WRITE_ASYNC=false a memory is written claimed ('processing') and
extracted before the write answers (memory_engine `_claimed_now`). Until
1.0.40 that claim had no stamp: a restart in the middle of the model call
left the memory 'processing' until the reaper's clock gave it back,
`drain_stale_minutes` (15) later, while the drain's own claims of a process
that ended go back at the next drain of any process (services/holder.py,
1.0.22). Now the inline claim is stamped with the key of the process that
writes it, so the next drain tells it ended and gives it back at once.

The writing process is a Holder of its own, whose lock session is closed
as a process's that ends; no model is called.
"""
import pytest

from korely_agent.db.models import AgentMemory
from korely_agent.services import holder as holders
from korely_agent.services.facts import drain as facts_drain
from korely_agent.services.memory_engine import AgentMemoryEngine


def _row(db, mid):
    db.expire_all()
    return db.get(AgentMemory, mid)


@pytest.fixture
def a_writer_that_ends(db, monkeypatch):
    """The process that writes, with a key of its own; `end()` ends it, as a
    restart ends a process's sessions."""
    writer = holders.Holder()
    with writer.lock:
        key = writer.key(db.get_bind())
    assert key is not None
    mine = facts_drain._holder
    monkeypatch.setattr(facts_drain, "_holder", writer)

    def end():
        with writer.lock:
            writer._drop()
        monkeypatch.setattr(facts_drain, "_holder", mine)

    yield key, end
    with writer.lock:
        writer._drop()


def test_an_inline_write_cut_by_a_restart_goes_back_at_the_next_drain(db, tenant,
                                                                         a_writer_that_ends):
    key, end = a_writer_that_ends
    m = AgentMemoryEngine(db, tenant.id).add("Maria moved to Milan.", end_user_id="maria",
                                            extracting_now=True)
    row = _row(db, m.id)
    assert (row.graph_sync_status, row.graph_sync_claimed_by) == ("processing", key)

    end()   # the restart, during the model call: the write never closes its claim
    assert facts_drain._the_ended_ones_back(db, 10) == 1
    row = _row(db, m.id)
    assert (row.graph_sync_status, row.graph_sync_claimed_at, row.graph_sync_claimed_by) == (
        "pending", None, None)


def test_an_inline_edit_cut_by_a_restart_goes_back_at_the_next_drain(db, tenant,
                                                                      a_writer_that_ends):
    key, end = a_writer_that_ends
    engine = AgentMemoryEngine(db, tenant.id)
    m = engine.add("Maria moved to Milan.", end_user_id="maria")
    db.query(AgentMemory).filter(AgentMemory.id == m.id).update(
        {AgentMemory.graph_sync_status: "completed"}, synchronize_session=False)
    db.commit()
    engine.update(str(m.id), "Maria moved to Turin.", extracting_now=True)
    row = _row(db, m.id)
    assert (row.graph_sync_status, row.graph_sync_claimed_by) == ("processing", key)

    end()
    assert facts_drain._the_ended_ones_back(db, 10) == 1
    assert _row(db, m.id).graph_sync_status == "pending"


def test_a_live_writer_keeps_its_inline_claim(db, tenant, a_writer_that_ends):
    key, _end = a_writer_that_ends
    m = AgentMemoryEngine(db, tenant.id).add("Maria moved to Milan.", end_user_id="maria",
                                            extracting_now=True)
    assert facts_drain._the_ended_ones_back(db, 10) == 0
    assert _row(db, m.id).graph_sync_claimed_by == key


def test_a_writer_giving_its_claims_back_leaves_the_claim_to_the_clock(db, tenant):
    """A process that has given its claims back (a clean stop under way)
    stamps nothing: its key is no live process's any more."""
    with facts_drain._holder.lock:
        facts_drain._holder._closed = True
    m = AgentMemoryEngine(db, tenant.id).add("Maria moved to Milan.", end_user_id="maria",
                                            extracting_now=True)
    row = _row(db, m.id)
    assert (row.graph_sync_status, row.graph_sync_claimed_by) == ("processing", None)
