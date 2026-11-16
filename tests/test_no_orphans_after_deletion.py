"""After a forget or an erasure, and after the same memories are written
again, no row points at a row that is gone.

Backs the claim against Cognee issue #5473 (topoteretes/cognee): deleting
data left orphans in the stores around it (graph nodes and edges, vectors,
relational rows with no parent).

The places Korely keeps a person's data are listed once, in
services/end_user_places.py: the /v1/batch payloads, the memories (with their
vectors), the facts (with theirs), the entity aliases, the object links and
the vocabulary (relations, their phrases, their example and centroid). The
scenario fills every one of them from one memory that is then deleted, keeps
a second fact stated by two memories (it must lose one source and stay), and
the check (`orphans`, tests/deletion_world.py) looks for:

- a fact whose source memory is gone, by its column and by every id of its
  JSON list of sources; an open fact whose every source is forgotten;
- a fact superseded by a fact that is gone;
- an alias or an object link whose fact (or memory) is gone;
- a vector without its row: a centroid with no example, an example no fact
  states, a relation of an end user who has nothing left;
- a predicate phrase of a relation that is gone;
- a /v1/batch payload item of the erased memory, user or agent (a physical
  erasure leaves the words in no table at all).

The same check runs again after the same memories are written once more.
"""
from tests import cloud
import pytest

from korely_agent.db.models import (AgentBatchJob, AgentEntityAlias, AgentFact, AgentMemory,
                                    AgentObjectLink)
from korely_agent.services import batch
from tests.conftest import seeded_project_id
from tests.deletion_world import Fact, dump_rows_with, orphans

# the `world` fixture
pytest_plugins = ["tests.deletion_world"]

MARKER = "jackfruit"
TARGET = "I am allergic to jackfruit."
HOME_A, HOME_B = "I live in Lisbon.", "Lisbon is my home town."
AGENT = "bot-7"
DELETIONS = ["forget_memory", "erase_memory", "erase_user", "delete_agent"]
PHYSICAL = {"erase_memory", "erase_user", "delete_agent"}


def memory_id(db, content):
    db.expire_all()
    return db.query(AgentMemory).filter(AgentMemory.content == content).one()


def scenario(w, db, tenant, agent):
    """One memory imported by a batch (its fact, an alias and an object link
    only it taught), and a fact two other memories state."""
    pid = seeded_project_id(db, tenant.id)
    w.reads[TARGET] = [Fact("u1", "allergic_to", MARKER)]
    w.reads[HOME_A] = [Fact("u1", "lives_in", "Lisbon")]
    w.reads[HOME_B] = [Fact("u1", "lives_in", "Lisbon")]
    job = batch.enqueue_batch(db, tenant.id, [{"content": TARGET, "user_id": "u1",
                                               **({"agent_id": agent} if agent else {})}],
                              project_id=pid)
    batch.process_batch_jobs(db, limit=5)
    w.drain()
    w.write(HOME_A, agent=agent)
    w.write(HOME_B, agent=agent)
    target, home = memory_id(db, TARGET), memory_id(db, HOME_A)
    fact = db.query(AgentFact).filter(AgentFact.object_label == MARKER).one()
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=pid, end_user_id="u1", agent_id=agent,
                            alias_lower="jackfruit tree", alias_label="Jackfruit Tree",
                            canonical_label="Jackfruit", source="writer", source_fact_id=fact.id))
    db.add(AgentObjectLink(tenant_id=tenant.id, project_id=pid, end_user_id="u1", agent_id=agent,
                           name_label="jackfruit", canonical_label="Artocarpus", family="other",
                           decided_by="writer", source_memory_id=target.id, source_fact_id=fact.id,
                           canonical_memory_id=home.id,
                           reason={"descriptions": {"name": "the fruit jackfruit"}}))
    db.commit()
    # A part of the product's rows of the same end user and agent
    # (core/extensions.Part.places).
    for rows_of_a_part in cloud.of_the_parts("ORPHANS_SCENARIO", tuple):
        rows_of_a_part(w, db, agent)
    assert dump_rows_with(db, MARKER), "the scenario holds nothing to delete"
    shared = db.query(AgentFact).filter(AgentFact.object_label == "Lisbon").one()
    assert len(shared.source_memory_ids) == 2
    assert orphans(db) == [], "the scenario starts with an orphan"
    return pid, target, job, agent


def delete(w, deletion, target_id, agent):
    c = w.client
    if deletion == "forget_memory":
        r = c.delete(f"/v1/memories/mem_{target_id.hex}")
    elif deletion == "erase_memory":
        r = c.delete(f"/v1/memories/mem_{target_id.hex}", params={"erase": "true"})
    elif deletion == "erase_user":
        r = c.delete("/v1/users/u1/memories")
    else:
        r = c.delete(f"/v1/agents/{agent}")
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("deletion", DELETIONS)
def test_no_row_points_at_a_deleted_row_after_a_deletion_and_a_rewrite(world, db, tenant, deletion):
    """Cognee #5473: after forget/erase, and after similar memories are added
    again, nothing points at a deleted row: no fact without its source, no
    vector (centroid, example) without its row, no object link or alias on a
    deleted fact, no batch item left."""
    w = world
    agent = AGENT if deletion == "delete_agent" else None
    pid, target, job, agent = scenario(w, db, tenant, agent)

    delete(w, deletion, target.id, agent)
    assert orphans(db) == []
    for after_a_part in cloud.of_the_parts("ORPHANS_AFTER", tuple):
        after_a_part(db, deletion)
    if deletion in PHYSICAL:
        assert dump_rows_with(db, MARKER) == [], "the erased words are in a table"
        db.expire_all()
        assert MARKER not in str(db.get(AgentBatchJob, job.id).payload), "a batch item is left"
        assert db.query(AgentEntityAlias).filter(AgentEntityAlias.alias_lower == "jackfruit tree").count() == 0
        assert db.query(AgentObjectLink).count() == 0
        if deletion == "erase_user":
            from korely_agent.db.models import PredicateAlias, PredicateCluster

            assert db.query(PredicateCluster).filter(PredicateCluster.end_user_id == "u1").count() == 0
            assert db.query(PredicateAlias).filter(PredicateAlias.end_user_id == "u1").count() == 0
    else:
        # A forget keeps the memory's row, its closed fact and the batch copy;
        # it takes the object links and leaves no open fact.
        assert db.query(AgentObjectLink).count() == 0
        assert [f for f in db.query(AgentFact) if f.object_label == MARKER and f.invalid_at is None] == []

    # The same memories again, in the same words: new rows, none orphaned.
    w.write(TARGET, agent=agent)
    w.write(HOME_A, agent=agent)
    w.write(HOME_B, agent=agent)
    w.drain()
    assert orphans(db) == []
    db.expire_all()
    assert [f.object_label for f in db.query(AgentFact)
            if f.invalid_at is None and f.object_label == MARKER] == [MARKER]

    # And the memory that shares its fact: erased, the fact loses that one
    # source and stays, with no dangling id in its list.
    delete_home = (db.query(AgentMemory).filter(AgentMemory.content == HOME_B)
                   .order_by(AgentMemory.created_at.desc()).first())
    gone_id = str(delete_home.id)
    r = w.client.delete(f"/v1/memories/mem_{delete_home.id.hex}", params={"erase": "true"})
    assert r.status_code == 200, r.text
    assert orphans(db) == []
    db.expire_all()
    [lisbon] = [f for f in db.query(AgentFact) if f.object_label == "Lisbon" and f.invalid_at is None]
    assert gone_id not in lisbon.source_memory_ids
    assert lisbon.observation_count == len(lisbon.source_memory_ids)
