"""With no writer configured, the memories wait for one (2026-10-07).

An install with no model key (none given at the first login, or removed)
stores and reads memories and mines no facts, as /health and `manage status`
say. The facts drain still claimed them every ten seconds, the extraction
found no provider, and the memory was filed 'error': two of the three
memories of the upgrade check of 7 October (0.1.19 to 0.1.20-dev) had "gave
up extracting" seconds after their write, and stayed so after a key was set,
until somebody ran `manage reextract`. The same kind of fault as a provider
that refuses (the hosted product's BUG-351), from the other side: nothing is
wrong with the memory. Now the worker claims nothing while no writer is
configured; the memories wait 'pending', and the first tick after a key is
set mines them.
"""
import uuid

import pytest

from korely_agent import worker
from korely_agent.db.models import AgentMemory
from korely_agent.services.facts import drain as facts_drain
from korely_agent.services.facts import service as facts_service
from tests.conftest import stub_extraction



@pytest.fixture(autouse=True)
def _the_drain_is_not_resting(monkeypatch):
    """A provider refusal in another test leaves the drain resting."""
    monkeypatch.setattr(facts_drain, "_pauses", {})


def no_writer(monkeypatch):
    monkeypatch.setattr("korely_agent.core.llm.provider_configured", lambda: False)
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)


def memories(db, tenant, n=3):
    ids = []
    for i in range(n):
        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="maria",
                        content=f"Maria said thing number {i}.", graph_sync_status="pending")
        db.add(m)
        ids.append(m.id)
    db.commit()
    return ids


def states(db, ids):
    db.expire_all()
    return [db.get(AgentMemory, i).graph_sync_status for i in ids]


def test_no_writer_claims_nothing_and_files_nothing(db, tenant, monkeypatch):
    no_writer(monkeypatch)
    ids = memories(db, tenant)
    for _ in range(3):
        out = worker._facts(db)
    assert out == {"processed": 0, "failed": 0, "claimed": 0, "no_writer": True}
    assert states(db, ids) == ["pending"] * 3


def test_the_first_tick_after_a_key_mines_them(db, tenant, monkeypatch):
    no_writer(monkeypatch)
    ids = memories(db, tenant)
    worker._facts(db)
    monkeypatch.setattr("korely_agent.core.llm.provider_configured", lambda: True)
    stub_extraction(monkeypatch, facts_service, lambda content, **kw: [])
    out = worker._facts(db)
    assert (out["claimed"], out["processed"], out["failed"]) == (3, 3, 0)
    assert states(db, ids) == ["done"] * 3
