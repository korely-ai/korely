"""A memory's write answers with what it wrote, read in its own transaction
before the commit, never read again after it (the review of 14 Nov 2026).

An end user's erasure that commits the moment a write has committed left the
row gone before the write read it back (`refresh` after the commit): the
write had happened, the request answered 500, and a client that retries a
500 wrote the erased person's text again. Each test forces that interleaving
with no sleep: the erasure runs in the write's own `after_commit`, in another
session, before anything else of the write."""
from __future__ import annotations

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import AgentMemory
from korely_agent.services.facts import erasure
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id

WHO = "alice-erased"


@pytest.fixture
def erased_right_after(db, tenant):
    """Arm with a test of the flush that is the write's (`armed.append`):
    once a commit carries such a flush, the end user is erased at once in a
    session of its own. What the erasure said is in `done`."""
    other = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    project = seeded_project_id(db, tenant.id)
    armed, done = [], []

    def the_write_flushed(session, _context):
        if armed and armed[0](session):
            session.info["the_write"] = True

    def erase_now(session):
        if not session.info.pop("the_write", False):
            return
        s = other()
        try:
            done.append(erasure.forget_end_user(s, tenant.id, WHO, project_id=project))
        finally:
            s.close()

    event.listen(db, "after_flush", the_write_flushed)
    event.listen(db, "after_commit", erase_now)
    try:
        yield project, armed, done
    finally:
        event.remove(db, "after_flush", the_write_flushed)
        event.remove(db, "after_commit", erase_now)


def test_an_add_met_by_an_erasure_right_after_its_commit_answers_what_it_wrote(
        db, tenant, erased_right_after):
    project, armed, done = erased_right_after
    text = "Alice moved to Lisbon in March."
    armed.append(lambda s: any(isinstance(o, AgentMemory) and o.content == text for o in s.new))
    m = AgentMemoryEngine(db, tenant.id, project).add(text, end_user_id=WHO)
    assert len(done) == 1, "the erasure did not run right after the write's commit"
    assert (m.content, m.end_user_id) == (text, WHO)
    assert m.created_at is not None and m.id is not None
    mid = m.id
    db.expire_all()
    assert db.get(AgentMemory, mid) is None, "the erasure came after the write and took it"


def test_an_edit_met_by_an_erasure_right_after_its_commit_answers_what_it_wrote(
        db, tenant, erased_right_after):
    project, armed, done = erased_right_after
    engine = AgentMemoryEngine(db, tenant.id, project)
    m = engine.add("Alice lives in Porto.", end_user_id=WHO)
    assert done == []
    armed.append(lambda s: any(isinstance(o, AgentMemory) and o.id == m.id
                               and inspect(o).attrs.content.history.has_changes()
                               for o in s.dirty))
    out = engine.update(str(m.id), "Alice lives in Lisbon.")
    assert len(done) == 1, "the erasure did not run right after the edit's commit"
    assert out is not None
    assert (out["id"], out["content"]) == (str(m.id), "Alice lives in Lisbon.")
    assert out["updated_at"] is not None
    mid = m.id
    db.expire_all()
    assert db.get(AgentMemory, mid) is None
