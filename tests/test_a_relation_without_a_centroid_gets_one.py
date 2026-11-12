"""The vector drain gives a relation with no centroid one (18 Oct 2026).

A relation is born without a centroid when its first phrase could not be
embedded, and migration e7d1b3f9a2c6 (one vocabulary per end user of a
project) leaves every relation of every end user without one (a vector made
from another end user's value is not carried, the audit of 18 Oct). Without
it a relation is never offered by similarity to a new phrase.
"""
import uuid

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import PredicateCluster
from korely_agent.services import predicates
from tests.conftest import seeded_project_id



def test_the_drain_computes_the_missing_centroid_from_the_projects_example(db, tenant, monkeypatch):
    seen = []

    def embed(text, task=None):
        seen.append(text)
        return [0.2] * EMBEDDING_DIM

    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.core.llm.embed_or_raise", embed)
    rel = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                           canonical="covered_under", example_object="PolicyBeta")
    db.add(rel)
    db.commit()

    assert predicates.fill_missing_centroids(db) == 1
    db.expire_all()
    assert db.get(PredicateCluster, rel.id).centroid is not None
    assert "PolicyBeta" in seen[0], "its own project's example"
    assert predicates.fill_missing_centroids(db) == 0


def test_a_failed_call_leaves_it_for_the_next_pass(db, tenant, monkeypatch):
    def down(text, task=None):
        raise RuntimeError("embedder down")

    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.core.llm.embed_or_raise", down)
    rel = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                           canonical="mentors")
    db.add(rel)
    db.commit()
    assert predicates.fill_missing_centroids(db) == 0
    db.expire_all()
    assert db.get(PredicateCluster, rel.id).centroid is None


# ── an erasure during the call keeps its centroid (audit of 19 Oct 2026) ─────
# The vector was computed from the example read before the slow call and
# written by id: an erasure that committed meanwhile had replaced the example
# and its centroid, and the vector of the erased value went over them.

ERASED = "erased-value-of-maria"


def _vec(text, task=None):
    return [0.9 if ERASED in text else 0.1] + [0.0] * (EMBEDDING_DIM - 1)


def _a_relation_whose_example_one_memory_states(db, tenant):
    from datetime import datetime, timezone

    from korely_agent.db.models import AgentFact, AgentMemory

    pid = seeded_project_id(db, tenant.id)
    mid = uuid.uuid4()
    db.add(AgentMemory(id=mid, tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                       content=f"maria has {ERASED}.", embedding=[0.1] * EMBEDDING_DIM,
                       graph_sync_status="done"))
    db.commit()
    db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                     subject_label="maria", predicate_raw="has condition",
                     predicate_canonical="has_condition", object_label=ERASED,
                     source_memory_id=mid, source_memory_ids=[str(mid)],
                     valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc)))
    rel = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                           canonical="has_condition", example_object=ERASED, member_count=1)
    db.add(rel)
    db.commit()
    return pid, mid, rel.id


def _erasing_during_the_call(engine, tenant, pid, mid):
    from sqlalchemy.orm import sessionmaker

    from korely_agent.services.facts.erasure import erase_memory

    erased = []

    def embed(text, task=None):
        if not erased and ERASED in text:
            with sessionmaker(bind=engine, expire_on_commit=False)() as other:
                erased.append(erase_memory(other, tenant.id, mid, project_id=pid))
        return _vec(text)
    return embed, erased


@pytest.mark.parametrize("door", ["drain", "recompute"])
def test_an_erasure_during_the_call_keeps_its_centroid(db, tenant, engine, monkeypatch, door):
    pid, mid, rel_id = _a_relation_whose_example_one_memory_states(db, tenant)
    if door == "recompute":
        db.get(PredicateCluster, rel_id).centroid = _vec(ERASED)
        db.commit()
    embed, erased = _erasing_during_the_call(engine, tenant, pid, mid)
    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.core.llm.embed_or_raise", embed)

    if door == "drain":
        assert predicates.fill_missing_centroids(db) == 0
    else:
        assert predicates.recompute_centroids(db, apply=True)["recomputed"] == 0
    assert erased and erased[0]["status"] == "erased"
    db.expire_all()
    row = db.get(PredicateCluster, rel_id)
    assert row.example_object is None
    assert row.centroid is None or float(row.centroid[0]) != pytest.approx(0.9), \
        "the vector of the erased value went over the erasure's centroid"


def test_a_relation_erased_during_the_pass_is_skipped(db, tenant, engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    pid = seeded_project_id(db, tenant.id)
    first, second = (PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                                      canonical=name) for name in ("mentors", "works_at"))
    db.add(first)
    db.commit()
    db.add(second)
    db.commit()

    def embed(text, task=None):
        with sessionmaker(bind=engine)() as other:
            other.query(PredicateCluster).filter(PredicateCluster.id == second.id).delete()
            other.commit()
        return [0.2] * EMBEDDING_DIM

    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.core.llm.embed_or_raise", embed)
    assert predicates.fill_missing_centroids(db) == 1


# ── a backlog larger than one pass (audit of 19 Oct 2026) ────────────────────
# Migration e7d1b3f9a2c6 leaves every relation of every end user with no
# centroid, and a relation with none is not offered to a new phrase: until the
# drain reaches it, each new phrasing of it is coined as a relation of its own.
# At 20 a pass, a pass every VECTORS_EVERY seconds, 6,000 relations waited 2.5
# hours. A pass that gave some their centroid and left others says `more`, as
# the facts drain does, and the worker starts the next one at once.

def _relations_with_no_centroid(db, tenant, n):
    pid = seeded_project_id(db, tenant.id)
    for i in range(n):
        db.add(PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                                end_user_id=f"user-{i}", canonical="subscribes_to",
                                example_object=f"Plan{i}"))
    db.commit()


def _missing(db):
    db.expire_all()
    return db.query(PredicateCluster).filter(PredicateCluster.centroid.is_(None)).count()


def test_a_pass_that_leaves_centroids_missing_says_more(db, tenant, monkeypatch):
    from korely_agent.services import vectors

    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.core.llm.embed_or_raise", lambda t, task=None: [0.2] * EMBEDDING_DIM)
    _relations_with_no_centroid(db, tenant, 25)

    first = vectors.process_pending_vectors(db)
    assert first["centroids"] == 20 and first["more"] is True
    last = vectors.process_pending_vectors(db)
    assert last["centroids"] == 5 and "more" not in last
    assert _missing(db) == 0


def test_an_embedder_that_fails_never_earns_more(db, tenant, monkeypatch):
    from korely_agent.services import vectors

    def down(text, task=None):
        raise RuntimeError("embedder down")

    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.core.llm.embed_or_raise", down)
    _relations_with_no_centroid(db, tenant, 25)
    assert "more" not in vectors.process_pending_vectors(db)


def test_the_worker_clears_a_backlog_without_resting_between_passes(db, engine, tenant, monkeypatch):
    import asyncio
    import time

    from sqlalchemy.orm import sessionmaker

    from korely_agent import worker

    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.core.llm.embed_or_raise", lambda t, task=None: [0.2] * EMBEDDING_DIM)
    monkeypatch.setattr(worker, "get_sessionmaker",
                        lambda: sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    _relations_with_no_centroid(db, tenant, 65)  # four passes of 20

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(worker._loop("vectors", worker._vectors, worker.VECTORS_EVERY, stop))
        deadline = time.monotonic() + 10
        while _missing(db) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, worker.VECTORS_EVERY + 5)

    asyncio.run(go())
    assert _missing(db) == 0, "the worker rested VECTORS_EVERY seconds between two passes"
