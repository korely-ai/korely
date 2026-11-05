"""A centroid the vector drain fills while an erasure is open does not keep
the erased value (the code audit of 20 Oct 2026).

The audit of 19 Oct 2026 made the drain's write a compare-and-set on the
example it embedded (predicates._store_centroid), which closed a drain
writing after the erasure committed. The reverse window stayed open: the erasure read the relation
without a lock and cleared the centroid by assigning None over a None it had
loaded, which the ORM does not write. A drain that committed in between kept
the vector of "relation + erased value" on a relation with no example, and
nothing revisits it.

Since then the erasure calls no embedder (erasure._erase_examples,
embed=None): a relation whose example changes loses its centroid and the
drain computes it again in the project's region. The order the old code lost is
built here, deterministically, with real sessions on Postgres: the drain's
write arriving while the erasure is about to write the row waits for it, and
after the erasure commits the centroid is NULL, never the erased value's
vector.
Dummy tenant from the suite's fixture; dummy end user "collaudo-maria".
"""
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory, PredicateCluster
from korely_agent.services import predicates
from tests.conftest import seeded_project_id


ERASED = "erased-value-of-collaudo-maria"
END_USER = "collaudo-maria"
AGENT = "collaudo-agent"
OF_THE_ERASED = 0.9     # the stub vector of any text holding the erased value
OF_THE_NEW = 0.1        # the stub vector of the relation's new example


def _vec(first):
    return [first] + [0.0] * (EMBEDDING_DIM - 1)


def _seed(db, tenant):
    """One memory, the one fact that states the value, and the relation
    learned from it with no centroid yet (as migration e7d1b3f9a2c6 leaves
    every relation until the drain reaches it)."""
    pid = seeded_project_id(db, tenant.id)
    mid = uuid.uuid4()
    db.add(AgentMemory(id=mid, tenant_id=tenant.id, project_id=pid, end_user_id=END_USER,
                       agent_id=AGENT, content=f"{END_USER} has {ERASED}.",
                       embedding=[0.1] * EMBEDDING_DIM, graph_sync_status="done"))
    db.commit()
    db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=END_USER,
                     agent_id=AGENT, subject_label=END_USER, predicate_raw="has condition",
                     predicate_canonical="has_condition", object_label=ERASED,
                     source_memory_id=mid, source_memory_ids=[str(mid)],
                     valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc)))
    rel = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                           end_user_id=END_USER, canonical="has_condition",
                           example_object=ERASED, member_count=1)
    db.add(rel)
    db.commit()
    return pid, mid, rel.id


def _erase(session, door, tenant, pid, mid):
    from korely_agent.services.facts.erasure import delete_agent, erase_memory

    if door == "memory":
        out = erase_memory(session, tenant.id, mid, project_id=pid)
        assert out and out["status"] == "erased"
    else:
        out = delete_agent(session, tenant.id, AGENT, project_id=pid)
        assert out["memories_deleted"] == 1


def _after(engine, tenant, rel_id):
    with sessionmaker(bind=engine)() as check:
        row = check.get(PredicateCluster, rel_id)
        facts = check.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).count()
        return row.example_object, (None if row.centroid is None else float(row.centroid[0])), facts


def _assert_nothing_of_the_erased_value(engine, tenant, rel_id):
    example, c0, facts = _after(engine, tenant, rel_id)
    assert facts == 0 and example is None
    assert c0 != pytest.approx(OF_THE_ERASED), "the vector of the erased value outlived the erasure"
    assert c0 is None, "the erasure leaves no centroid; the drain computes the new one"


def _embed(text_, task=None):
    """The stub embedder. Only the drain calls it: the erasure computes no vector."""
    return _vec(OF_THE_ERASED if ERASED in text_ else OF_THE_NEW)


def _drain_waiting_on_a_lock(engine) -> bool:
    with engine.connect() as c:
        return bool(c.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND query ILIKE 'UPDATE predicate_clusters%'")).scalar())


@pytest.mark.parametrize("door", ["memory", "agent"])
def test_a_drain_that_writes_while_the_erasure_writes_the_row(db, tenant, engine, monkeypatch, door):
    """The other order: just before the erasure's UPDATE of the relation, the
    drain (another connection, in a thread) reads the committed relation,
    embeds the erased value and issues its compare-and-set. It must wait for
    the erasure and then match nothing: the erasure holds the row, and the
    example it commits is not the one the drain embedded."""
    pid, mid, rel_id = _seed(db, tenant)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    drain, fired, waited = [], [], []

    def run_the_drain():
        with Session() as other:
            drain.append(predicates.fill_missing_centroids(other))

    def before_the_erasures_update(conn, cursor, statement, parameters, context, executemany):
        if fired or not statement.lstrip().upper().startswith("UPDATE PREDICATE_CLUSTERS"):
            return
        fired.append(True)
        t = threading.Thread(target=run_the_drain, daemon=True)
        t.start()
        fired.append(t)
        deadline = time.monotonic() + 15
        while t.is_alive() and time.monotonic() < deadline:
            if _drain_waiting_on_a_lock(engine):
                waited.append(True)
                break
            time.sleep(0.02)

    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.core.llm.embed_or_raise", _embed)
    event.listen(engine, "before_cursor_execute", before_the_erasures_update)
    try:
        with Session() as er:
            _erase(er, door, tenant, pid, mid)
    finally:
        event.remove(engine, "before_cursor_execute", before_the_erasures_update)
    assert len(fired) == 2, "the erasure wrote the relation"
    fired[1].join(15)
    assert not fired[1].is_alive()

    assert waited, "the drain's write did not wait for the erasure"
    assert drain == [0], "the drain's compare-and-set matched the erased example"
    _assert_nothing_of_the_erased_value(engine, tenant, rel_id)
