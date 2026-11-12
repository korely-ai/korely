"""A memory forgotten or erased while its facts are being extracted leaves no
fact behind, and the write does not fail.

Backs the claim against Cognee issue #3766 (topoteretes/cognee): a delete
issued while the pipeline was still processing the document left the derived
graph behind (and failed in the pipeline).

Here the race is made deterministic: the scripted writer (tests/
deletion_world.py) runs a hook WHILE a model call is in flight, and the hook
deletes from another session, as another request would. The three moments a
write is waiting on a model are the three kinds of call: the extraction of the
facts, the classification of a new relation (vocabulary), and the judge of
the contradictions. The memory is in flight under its claim
(`worker.claim`, `graph_sync_status`), either an inline write (WRITE_ASYNC off,
the MCP door) or the drain's.

After the deletion has landed and the write has finished:
- no 500 (the inline write answers 201, the drain returns);
- no fact of the memory is open, and with an erasure no row of its words is
  in any table;
- the memory is not left 'processing' (a forgotten one is 'forgotten'), and a
  later drain does not extract it again;
- nothing points at a row that is gone.
"""
import threading
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import facts as agent_facts
from tests.conftest import seeded_project_id
from tests.deletion_world import Fact, dump_rows_with, orphans

# the `world` fixture
pytest_plugins = ["tests.deletion_world"]

MARKER = "jackfruit"
TEXT = "I am allergic to jackfruit."
EARLIER = "I am allergic to pollen."
AGENT = "bot-7"

DELETIONS = ["forget_memory", "erase_memory", "erase_user", "delete_agent"]
MOMENTS = ["extraction", "vocabulary", "judge"]
DOORS = ["inline write", "drain"]


@pytest.fixture
def other(engine):
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield s
    s.rollback()
    s.close()


def deletion_hook(w, other, tenant, pid, kind, fired):
    """What another request does the moment the model is asked."""
    def run():
        other.expire_all()
        mem = other.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id,
                                              AgentMemory.content == TEXT).one()
        if kind == "forget_memory":
            agent_facts.forget_memory(other, tenant.id, mem.id, project_id=pid)
        elif kind == "erase_memory":
            agent_facts.erase_memory(other, tenant.id, mem.id, project_id=pid)
        elif kind == "erase_user":
            agent_facts.forget_end_user(other, tenant.id, "u1", project_id=pid)
        elif kind == "delete_agent":
            agent_facts.delete_agent(other, tenant.id, AGENT, project_id=pid)

    def hook():
        fired.append(kind)
        t = threading.Thread(target=run)
        t.start()
        # An erasure that has to wait for the write in flight is allowed to:
        # the write goes on when the hook returns and the erasure lands after.
        t.join(timeout=5)
        fired.append("blocked" if t.is_alive() else "landed")
        fired.append(t)
    return hook


@pytest.mark.parametrize("door", DOORS)
@pytest.mark.parametrize("moment", MOMENTS)
@pytest.mark.parametrize("deletion", DELETIONS)
def test_a_deletion_during_the_extraction_leaves_no_fact_and_no_500(
        world, db, other, tenant, deletion, moment, door):
    """Cognee #3766: forgetting/erasing a memory while its facts are being
    extracted (or classified, or judged) leaves nothing behind and fails
    nowhere."""
    w = world
    pid = seeded_project_id(db, tenant.id)
    agent = AGENT if deletion == "delete_agent" else None
    w.reads[TEXT] = [Fact("u1", "allergic_to", MARKER)]
    w.reads[EARLIER] = [Fact("u1", "allergic_to", "pollen")]
    # An earlier fact of the same relation: the new one has a candidate, so the
    # judge is asked (and the relation is known, so the vocabulary is asked
    # only when there is no earlier fact).
    if moment == "judge":
        w.write(EARLIER, agent=agent)
        w.calls.clear()

    fired: list = []
    w.hooks[moment] = deletion_hook(w, other, tenant, pid, deletion, fired)

    if door == "inline write":
        r = w.client.post("/v1/memories", json={"content": TEXT, "user_id": "u1",
                                                **({"agent_id": agent} if agent else {})})
        assert r.status_code == 201, r.text
    else:
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="u1",
                           agent_id=agent, content=TEXT, graph_sync_status="pending"))
        db.commit()
        w.drain()
    assert fired and fired[0] == deletion, f"the model was never asked for the {moment!r}: nothing was tested"
    for t in [x for x in fired if isinstance(x, threading.Thread)]:
        t.join(timeout=60)
        assert not t.is_alive(), "the deletion never finished"
    # The drain may have given the memory back (an erasure that waited); a
    # later round must find nothing to do for it.
    w.drain()  # waits for the group still running, if any
    asked = len(w.calls)
    w.drain()

    db.expire_all()
    assert [f for f in db.query(AgentFact) if f.invalid_at is None and f.object_label == MARKER] == [], \
        "a fact of the deleted memory is open"
    mems = db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id,
                                        AgentMemory.content == TEXT).all()
    if deletion == "forget_memory":
        [m] = mems
        assert m.deleted_at is not None and m.graph_sync_status == "forgotten", \
            f"the forgotten memory is {m.graph_sync_status!r}"
    else:
        assert dump_rows_with(db, MARKER) == [], "the erased words are in a table"
    assert not [m for m in db.query(AgentMemory) if m.graph_sync_status == "processing"], \
        "a memory is left 'processing'"
    assert w.calls[asked:] == [], "a later drain asked the model again for a deleted memory"
    assert orphans(db) == []
