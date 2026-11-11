"""The drain's claim holds (memory_core 2.11.0, `worker.Claim`): a memory
forgotten or edited while its facts are being extracted is not filed 'done'
by the write that was working on the old state. The other session acts
inside the model call, as a person would while the drain waits on the
provider. No model is called.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import service as S
from korely_agent.services.memory_core.fact_extractor import Extraction
from korely_agent.services.sentinels import EVERY_PROJECT



@pytest.fixture
def during_the_call(monkeypatch, engine):
    """`act[0](session)` runs inside the extraction's model call, in a session
    of its own; the call then answers with no fact."""
    act = [None]
    other = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

    def extraction(content, **kw):
        if act[0] is not None:
            act[0](other)
            act[0] = None
        return Extraction([], None)

    monkeypatch.setattr(S, "extract_facts_and_observation", extraction)
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    yield act
    other.close()


def _memory(db, tenant, content="Maria moved to Milan."):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1", content=content)
    db.add(m)
    db.commit()
    return m


def _row(db, mid):
    db.expire_all()
    return db.get(AgentMemory, mid)


def test_forgotten_during_its_extraction_stays_forgotten(db, tenant, during_the_call):
    m = _memory(db, tenant)
    during_the_call[0] = lambda other: S.forget_memory(other, tenant.id, m.id, project_id=EVERY_PROJECT)
    S.process_pending_memories(db, limit=10)
    row = _row(db, m.id)
    assert row.deleted_at is not None
    assert row.graph_sync_status == "forgotten", "the write filed a forgotten memory as done"


def test_edited_during_its_extraction_waits_with_its_new_text(db, tenant, during_the_call):
    from korely_agent.services.memory_engine import AgentMemoryEngine

    m = _memory(db, tenant)

    def edit(other):
        AgentMemoryEngine(other, tenant.id).update(str(m.id), "Maria moved to Rome.")
        other.commit()

    during_the_call[0] = edit
    S.process_pending_memories(db, limit=10)
    row = _row(db, m.id)
    assert (row.content, row.graph_sync_status, row.graph_sync_claimed_at) == (
        "Maria moved to Rome.", "pending", None)
    S.process_pending_memories(db, limit=10)
    assert _row(db, m.id).graph_sync_status == "done", "the new text is extracted next"


def test_a_forgotten_memory_is_never_claimed(db, tenant):
    m = _memory(db, tenant)
    S.forget_memory(db, tenant.id, m.id, project_id=EVERY_PROJECT)
    r = S.process_pending_memories(db, limit=10)
    assert not r.get("claimed")


def test_a_fact_dated_ahead_closes_when_it_begins(db, tenant):
    m = _memory(db, tenant)
    m.graph_sync_status = "done"
    ahead = datetime.now(timezone.utc) + timedelta(days=3)
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1", subject_label="Maria",
                  predicate_raw="lives in", predicate_canonical="lives_in", object_label="Milan",
                  source_memory_id=m.id, valid_from=ahead)
    db.add(f)
    db.commit()
    S.forget_memory(db, tenant.id, m.id, project_id=EVERY_PROJECT)
    db.expire_all()
    assert db.get(AgentFact, f.id).invalid_at >= ahead, "closed before it began"
