"""`manage recompute-vectors`: every stored vector again, with the embedding
model in force, for an install that changes EMBEDDING_PROVIDER.

No model is called. The embedder in force answers NEW for every text and
writes down what it was asked, so a row shows which model made its vector and
the log shows the order: the relations' centroids, then the facts, then the
memories (the order for both engines, 2026-09-29).
"""
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent import manage
from korely_agent.core import llm
from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory, PredicateCluster
from korely_agent.services import memory_engine, predicates, vectors


OLD = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
NEW = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)


@pytest.fixture
def new_model(monkeypatch):
    """The model in force: NEW for every text, and the (task, text) asked, in
    order. `fail` holds texts whose call fails."""
    asked, fail = [], set()

    def embed(text, *, task=llm.TASK_DOCUMENT):
        asked.append((task, text))
        return None if text in fail else NEW

    monkeypatch.setattr(llm, "embed", embed)
    monkeypatch.setattr(memory_engine, "embed", embed)
    monkeypatch.setattr(llm, "current_google_key", lambda: "k")
    return SimpleLog(asked, fail)


class SimpleLog:
    def __init__(self, asked, fail):
        self.asked, self.fail = asked, fail

    def texts(self):
        return [t for _task, t in self.asked]


def seed(db, tenant, *, memories=("Maria is on the Business plan.",)):
    c = PredicateCluster(tenant_id=tenant.id, canonical="subscribes_to",
                         example_object="Business plan", centroid=OLD, member_count=1)
    live = [AgentMemory(tenant_id=tenant.id, content=t, embedding=OLD, embedding_status="done")
            for t in memories]
    gone = AgentMemory(tenant_id=tenant.id, content="forgotten", embedding=OLD,
                       embedding_status="done", deleted_at=datetime.now(timezone.utc))
    db.add_all([c, *live, gone])
    db.commit()
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, subject_label="Maria",
                  subject_canonical="maria", predicate_raw="subscribes_to",
                  predicate_canonical="subscribes_to", object_label="Business plan",
                  object_canonical="business plan", source_memory_id=live[0].id,
                  embedding=OLD, embedding_status="done")
    db.add(f)
    db.commit()
    return c, live, gone, f


def vector(db, model, row_id, column="embedding"):
    db.expire_all()
    got = getattr(db.get(model, row_id), column)
    return [float(x) for x in got[:2]] if got is not None else None


class TestADryRun:
    def test_counts_and_writes_nothing(self, db, tenant, new_model, capsys):
        c, live, gone, f = seed(db, tenant)
        assert manage.main(["recompute-vectors"]) == 0
        out = capsys.readouterr().out
        assert "relations   1 centroids" in out and "facts       1" in out
        assert "memories    1" in out, "a forgotten memory is not recomputed"
        assert "dry run" in out
        assert new_model.asked == []
        assert vector(db, AgentMemory, live[0].id) == [1.0, 0.0]
        assert db.get(AgentMemory, live[0].id).embedding_status == "done"


class TestARun:
    def test_centroids_then_facts_then_memories_as_their_writers(self, db, tenant, new_model, capsys):
        c, live, gone, f = seed(db, tenant)
        assert manage.main(["recompute-vectors", "--apply"]) == 0

        assert new_model.asked == [
            (llm.TASK_SIMILARITY, "subscribes to Business plan"),
            (llm.TASK_DOCUMENT, "maria subscribes_to business plan"),
            (llm.TASK_DOCUMENT, "Maria is on the Business plan."),
        ]
        assert vector(db, PredicateCluster, c.id, "centroid") == [0.0, 1.0]
        assert vector(db, AgentFact, f.id) == [0.0, 1.0]
        assert vector(db, AgentMemory, live[0].id) == [0.0, 1.0]
        assert vector(db, AgentMemory, gone.id) == [1.0, 0.0]
        for model, row_id in ((AgentFact, f.id), (AgentMemory, live[0].id)):
            assert db.get(model, row_id).embedding_status == "done"

    def test_a_failed_row_keeps_its_old_vector_and_the_next_run_goes_on(
            self, db, tenant, new_model, capsys):
        c, live, gone, f = seed(db, tenant, memories=("first", "second", "third"))
        new_model.fail.add("second")
        assert manage.main(["recompute-vectors", "--apply"]) == 1
        assert "1 rows still have the old model's vector" in capsys.readouterr().out
        second = next(m for m in live if m.content == "second")
        assert vector(db, AgentMemory, second.id) == [1.0, 0.0]
        assert db.get(AgentMemory, second.id).embedding_status == vectors.STALE

        new_model.asked.clear()
        new_model.fail.clear()
        assert manage.main(["recompute-vectors"]) == 0
        assert "left by a run that stopped" in capsys.readouterr().out
        assert manage.main(["recompute-vectors", "--apply"]) == 0
        # The centroid again (they are few, and first), then only what was left.
        assert new_model.texts() == ["subscribes to Business plan", "second"]
        assert vector(db, AgentMemory, second.id) == [0.0, 1.0]

    def test_an_embedder_that_is_down_stops_the_run(self, db, tenant, new_model, capsys):
        c, live, gone, f = seed(db, tenant, memories=("a", "b", "c", "d", "e"))
        new_model.fail.update({"a", "b", "c", "d", "e"})
        assert manage.main(["recompute-vectors", "--apply"]) == 1
        assert "stopped" in capsys.readouterr().err
        tried = [t for t in new_model.texts() if t in {"a", "b", "c", "d", "e"}]
        assert len(tried) == vectors.FAILURES_BEFORE_STOPPING

    def test_a_memory_edited_meanwhile_keeps_its_own_new_vector(
            self, db, tenant, new_model, monkeypatch):
        c, live, gone, f = seed(db, tenant)
        vectors.mark_stale(db)
        m_id = live[0].id
        edited = [0.0, 0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 3)
        fake = memory_engine.embed

        def the_user_edits_while_the_run_computes(text, *, task=llm.TASK_DOCUMENT):
            if text == "Maria is on the Business plan.":
                db.query(AgentMemory).filter(AgentMemory.id == m_id).update(
                    {"content": "Maria moved to the Team plan.", "embedding": edited,
                     "embedding_status": "done"}, synchronize_session=False)
                db.commit()
            return fake(text, task=task)

        monkeypatch.setattr(memory_engine, "embed", the_user_edits_while_the_run_computes)
        vectors.recompute(db)
        db.expire_all()
        assert db.get(AgentMemory, m_id).content == "Maria moved to the Team plan."
        assert vector(db, AgentMemory, m_id) == [0.0, 0.0], (
            "the run wrote the old text's vector over the edit")

    def test_a_finished_run_leaves_nothing_and_status_says_nothing(self, db, tenant, new_model, capsys):
        seed(db, tenant)
        assert manage.main(["recompute-vectors", "--apply"]) == 0
        capsys.readouterr()
        assert vectors.to_recompute(db)["resuming"] is False
        assert manage.main(["status"]) == 0
        assert "embedding model before" not in capsys.readouterr().out

    def test_status_says_when_a_run_did_not_finish(self, db, tenant, new_model, capsys):
        seed(db, tenant)
        vectors.mark_stale(db)
        assert manage.main(["status"]) == 0
        assert "2 from the embedding model before" in capsys.readouterr().out

    def test_without_an_embedder_it_refuses(self, db, tenant, monkeypatch, capsys):
        c, live, gone, f = seed(db, tenant)
        monkeypatch.setattr(llm, "current_google_key", lambda: None)
        assert manage.main(["recompute-vectors", "--apply"]) == 1
        assert "no embedder" in capsys.readouterr().err
        db.expire_all()
        assert db.get(AgentMemory, live[0].id).embedding_status == "done", (
            "marked stale with nothing to recompute them")


class TestTheCentroidText:
    def test_the_first_phrase_with_its_object(self):
        c = PredicateCluster(canonical="subscribes_to", example_object="Business plan")
        assert predicates.centroid_text(c) == "subscribes to Business plan"

    def test_a_relation_from_before_the_example_has_its_name(self):
        assert predicates.centroid_text(PredicateCluster(canonical="works_at")) == "works at"
