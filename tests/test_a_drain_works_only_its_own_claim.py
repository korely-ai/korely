"""A drain extracts a memory only under the claim it took (memory_core 6.27.0).

The facts drain claims a batch (memory_core `claim_pending`, one claim time
for the tick) and works it in groups. memory_core's `drain_one` and the
write it calls read the claim fresh from the row, a snapshot of now: a
memory given back meanwhile (the reaper after `drain_stale_minutes`, a
process read as ended while its lock session was cut) and claimed again by
another drain read as this drain's claim, and was extracted a second time
beside its new holder: one model call more, two passes of the judge, one
write kept by the compare-and-set (the review of 13 Nov 2026, two rows).
Now each read holds only the claim the tick took, by its claim time.
No model is called: the extraction is a stand-in that counts.
"""
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent.db.models import AgentMemory
from korely_agent.services.facts import drain as facts_drain
from korely_agent.services.facts import service as S
from korely_agent.services.memory_core import worker as mc_worker
from korely_agent.services.memory_core.fact_extractor import Extraction


def _memory(db, tenant, content="Maria moved to Milan."):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="maria", content=content,
                    created_at=datetime.now(timezone.utc))
    db.add(m)
    db.commit()
    return m


def _claimed_by_a_tick(db, m):
    """The tick's claim on `m`, and its claim time."""
    rows = mc_worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
    assert [r.id for r in rows] == [m.id]
    return rows[0], rows[0].graph_sync_claimed_at


def _claimed_again_by_another_drain(db, m, read=None):
    """Given back and taken again, as a reaper and a second drain do."""
    assert mc_worker.release(db, (read or mc_worker.claim)(db, m))
    p = mc_worker.claim_this_one(db, m)
    assert p is not None
    return p.when


@pytest.fixture
def extraction(monkeypatch):
    calls = []

    def extract(content, **kw):
        calls.append(content)
        return Extraction([], None)

    monkeypatch.setattr(S, "extract_facts_and_observation", extract)
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    return calls


def test_drain_one_leaves_a_claim_taken_since_to_its_holder(db, tenant):
    m = _memory(db, tenant)
    row, when = _claimed_by_a_tick(db, m)
    theirs = _claimed_again_by_another_drain(db, m)
    calls = []

    assert mc_worker.drain_one(db, row, lambda mem, ev: calls.append(mem.id), when=when) is True
    assert calls == []
    db.expire_all()
    fresh = db.get(AgentMemory, m.id)
    assert (fresh.graph_sync_status, fresh.graph_sync_claimed_at) == ("processing", theirs)


def test_drain_one_works_the_claim_it_took(db, tenant):
    m = _memory(db, tenant)
    row, when = _claimed_by_a_tick(db, m)
    calls = []
    mc_worker.drain_one(db, row, lambda mem, ev: calls.append(mem.id), when=when)
    assert calls == [m.id]


def test_the_drain_calls_no_model_for_a_memory_taken_since(db, tenant, extraction):
    m = _memory(db, tenant)
    row, when = _claimed_by_a_tick(db, m)
    theirs = _claimed_again_by_another_drain(db, m)

    assert facts_drain._drain_one_memory(db, row, None, when) is True
    assert extraction == []
    db.expire_all()
    fresh = db.get(AgentMemory, m.id)
    assert (fresh.graph_sync_status, fresh.graph_sync_claimed_at) == ("processing", theirs)


def test_a_claim_taken_between_the_two_reads_costs_no_call(db, tenant, extraction, monkeypatch):
    """The window of a few milliseconds the second row names: `drain_one`
    reads the tick's claim, another drain takes the memory, then the write
    reads the claim again."""
    m = _memory(db, tenant)
    row, when = _claimed_by_a_tick(db, m)
    real = mc_worker.claim
    reads = []

    def claim(db_, mem):
        p = real(db_, mem)
        reads.append(p.when if p else None)
        if len(reads) == 1:
            _claimed_again_by_another_drain(db_, mem, real)
        return p

    monkeypatch.setattr(mc_worker, "claim", claim)
    facts_drain._drain_one_memory(db, row, None, when)
    assert reads[0] == when and reads[1] != when
    assert extraction == []


def test_the_drain_extracts_its_own_claim(db, tenant, extraction):
    m = _memory(db, tenant)
    row, when = _claimed_by_a_tick(db, m)
    assert facts_drain._drain_one_memory(db, row, None, when) is True
    assert extraction == ["Maria moved to Milan."]


def test_a_group_works_each_memory_under_the_ticks_claim_time(db, tenant, extraction,
                                                              monkeypatch):
    """`_run_group` hands `drain_one` the claim time the tick took for each
    memory, never None (any claim), so the check above holds in the drain
    as it runs."""
    first = _memory(db, tenant, "Maria moved to Milan.")
    second = _memory(db, tenant, "Maria works at Acme.")
    rows = mc_worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)
    claims = {r.id: r.graph_sync_claimed_at for r in rows}
    assert set(claims) == {first.id, second.id}
    real, seen = mc_worker.drain_one, {}

    def drain_one(db_, m, do, **kw):
        seen[m.id] = kw.get("when")
        return real(db_, m, do, **kw)

    monkeypatch.setattr(mc_worker, "drain_one", drain_one)
    facts_drain._run_group(db, tenant.id, first.project_id, "maria", rows, claims)
    assert seen == claims
    assert None not in seen.values()


def test_a_memory_taken_since_ends_no_rest_of_its_region(db, tenant, extraction, monkeypatch):
    """No model was asked for it: it says nothing of the provider, and a
    region that rests keeps resting (`_provider_answered`). A memory this
    drain extracts ends the rest, as before."""
    monkeypatch.setitem(facts_drain._pauses, None, {"step": 2})
    m = _memory(db, tenant)
    row, when = _claimed_by_a_tick(db, m)
    _claimed_again_by_another_drain(db, m)
    assert facts_drain._drain_one_memory(db, row, None, when) is True
    assert extraction == [] and facts_drain._pauses[None]["step"] == 2

    other = _memory(db, tenant, "Maria works at Acme.")
    row, when = _claimed_by_a_tick(db, other)
    assert facts_drain._drain_one_memory(db, row, None, when) is True
    assert facts_drain._pauses[None]["step"] == 0
