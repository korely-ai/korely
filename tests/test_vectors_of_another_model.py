"""The start's check: stored vectors of another model stop the API and the
worker before they write or answer (services/vectors.py, 2026-10-06).

The default embedder changed in 0.1.19, from Gemini to EmbeddingGemma on the
machine, and both give 768 numbers: an install upgraded without
EMBEDDING_PROVIDER in .env would have searched one space with the other's
questions, and nothing would have failed. The install records the model of
its vectors and the start compares; an install from before the record is
checked once by computing its latest vectors again. No model is called here:
the embedder in force is a function, and the stored vectors are written by
hand.
"""
import uuid

import pytest

from korely_agent import manage
from korely_agent.core import llm
from korely_agent.core.embedding_model import NAME as LOCAL
from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import memory_engine, vectors


GEMINI = "gemini-embedding-2-preview"   # the suite's model in force (tests/conftest.py)


def unit(*head):
    v = list(head) + [0.0] * (EMBEDDING_DIM - len(head))
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


THEIRS = unit(1.0)              # what the stored rows were computed with
OURS = unit(0.0, 1.0)           # another model's answer for the same text: cosine 0
NEAR = unit(1.0, 0.3)           # cosine 0.96: the same model, the text a little changed
CLOSE = unit(1.0, 0.5)          # cosine 0.89: close, and still another model


@pytest.fixture
def model_in_force(monkeypatch):
    """The embedder in force answers `answer["vector"]` for every text (None:
    the call failed) and counts how many times it was asked."""
    answer = {"vector": OURS, "asked": 0}

    def embed(text, *, task=llm.TASK_DOCUMENT):
        answer["asked"] += 1
        return answer["vector"]

    monkeypatch.setattr(llm, "embed", embed)
    monkeypatch.setattr(memory_engine, "embed", embed)
    monkeypatch.setattr(llm, "current_google_key", lambda: "k")
    return answer


@pytest.fixture
def local(monkeypatch):
    from korely_agent.config import get_settings

    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def seed(db, tenant, *, vector=THEIRS, status="done"):
    m = AgentMemory(tenant_id=tenant.id, content="Maria is on the Business plan.",
                    embedding=vector, embedding_status=status)
    db.add(m)
    db.commit()
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, subject_label="Maria",
                  subject_canonical="maria", predicate_raw="subscribes_to",
                  predicate_canonical="subscribes_to", object_label="Business plan",
                  object_canonical="business plan", source_memory_id=m.id,
                  embedding=vector, embedding_status=status)
    db.add(f)
    db.commit()
    return m, f


class TestTheComparison:
    def test_another_models_vectors(self, db, tenant, model_in_force):
        seed(db, tenant)
        out = vectors.compare_with_stored(db)
        assert out["verdict"] == "other"
        assert out["cosines"] == [0.0, 0.0], "the fact, then the memory"

    def test_the_same_models_vectors(self, db, tenant, model_in_force):
        seed(db, tenant)
        model_in_force["vector"] = THEIRS
        assert vectors.compare_with_stored(db)["verdict"] == "same"

    def test_a_little_changed_is_still_the_same(self, db, tenant, model_in_force):
        seed(db, tenant)
        model_in_force["vector"] = NEAR
        assert vectors.compare_with_stored(db)["verdict"] == "same"

    def test_close_is_not_enough(self, db, tenant, model_in_force):
        """A model distilled from another can come close to it; one model gives
        its own vector back, so the line is near 1."""
        seed(db, tenant)
        model_in_force["vector"] = CLOSE
        assert vectors.compare_with_stored(db)["verdict"] == "other"

    def test_no_embedder(self, db, tenant):
        seed(db, tenant)
        out = vectors.compare_with_stored(db)
        assert out == {"verdict": "nothing", "cosines": [], "why": "no embedder is configured"}

    def test_a_failed_call(self, db, tenant, model_in_force):
        seed(db, tenant)
        model_in_force["vector"] = None
        out = vectors.compare_with_stored(db)
        assert out["verdict"] == "nothing" and "no vector" in out["why"]

    def test_vectors_a_recomputation_left_stale_are_not_read(self, db, tenant, model_in_force):
        seed(db, tenant, status="stale")
        assert vectors.compare_with_stored(db)["verdict"] == "nothing"


class TestTheRecord:
    def test_a_new_install_records_its_model(self, db, tenant, model_in_force):
        out = vectors.check_the_vectors(db)
        assert out["start"] and vectors.recorded_model(db) == GEMINI
        assert model_in_force["asked"] == 0

    def test_no_vector_yet_follows_a_change_of_embedder(self, db, tenant, model_in_force):
        """Started once on one embedder and changed before the first write:
        nothing to protect, the new one is recorded."""
        vectors.record_model(db, LOCAL)
        assert vectors.check_the_vectors(db)["start"]
        assert vectors.recorded_model(db) == GEMINI

    def test_the_recorded_model_starts_without_a_call(self, db, tenant, model_in_force):
        seed(db, tenant)
        vectors.record_model(db, GEMINI)
        assert vectors.check_the_vectors(db)["start"]
        assert model_in_force["asked"] == 0

    def test_another_recorded_model_stops_it(self, db, tenant, model_in_force, local):
        seed(db, tenant)
        vectors.record_model(db, GEMINI)
        out = vectors.check_the_vectors(db)
        assert not out["start"]
        assert f"computed by {GEMINI}" in out["say"] and f"with {LOCAL} now" in out["say"]
        assert f"EMBEDDING_PROVIDER=gemini (EMBEDDING_MODEL={GEMINI})" in out["say"]
        assert "recompute-vectors --apply" in out["say"]

    def test_a_recomputation_under_way_lets_it_start(self, db, tenant, model_in_force, local):
        seed(db, tenant, status="stale")
        vectors.record_model(db, GEMINI)
        out = vectors.check_the_vectors(db)
        assert out["start"] and out["level"] == "warning"
        assert "1 facts and 1 memories left" in out["say"]

    def test_an_install_from_before_the_record_is_checked_once(self, db, tenant, model_in_force):
        seed(db, tenant)
        model_in_force["vector"] = THEIRS
        assert vectors.check_the_vectors(db)["start"]
        assert vectors.recorded_model(db) == GEMINI
        asked = model_in_force["asked"]
        vectors.check_the_vectors(db)
        assert model_in_force["asked"] == asked, "the second start reads the record"

    def test_an_upgrade_onto_gemini_vectors_stops_with_the_way_back(self, db, tenant,
                                                                    model_in_force, local):
        seed(db, tenant)
        out = vectors.check_the_vectors(db)
        assert not out["start"] and vectors.recorded_model(db) is None
        assert "EMBEDDING_PROVIDER=gemini" in out["say"] and "0.1.18" in out["say"]
        assert "recompute-vectors --apply" in out["say"]

    def test_a_check_that_could_not_compare_records_nothing(self, db, tenant, model_in_force):
        seed(db, tenant)
        model_in_force["vector"] = None
        out = vectors.check_the_vectors(db)
        assert out["start"] and out["level"] == "warning"
        assert vectors.recorded_model(db) is None


class TestTheStart:
    def test_another_models_vectors_raise(self, db, tenant, model_in_force):
        seed(db, tenant)
        with pytest.raises(vectors.VectorsOfAnotherModel):
            vectors.refuse_vectors_of_another_model()

    def test_the_api_does_not_start(self, db, tenant, model_in_force):
        from fastapi.testclient import TestClient

        from korely_agent.main import create_app

        seed(db, tenant)
        with pytest.raises(vectors.VectorsOfAnotherModel):
            with TestClient(create_app()):
                pass

    def test_a_check_that_cannot_run_does_not_keep_it_down(self, db, tenant, model_in_force,
                                                           monkeypatch, caplog):
        def broken():
            raise RuntimeError("the database is not there yet")

        monkeypatch.setattr("korely_agent.db.get_sessionmaker", broken)
        vectors.refuse_vectors_of_another_model()
        assert "starting anyway" in caplog.text


class TestTheRecomputationRecords:
    def test_a_whole_run_records_the_new_model(self, db, tenant, model_in_force, capsys):
        seed(db, tenant)
        vectors.record_model(db, "the model before")
        assert manage.main(["recompute-vectors", "--apply"]) == 0
        db.expire_all()
        assert vectors.recorded_model(db) == GEMINI
        assert f"{GEMINI} recorded" in capsys.readouterr().out

    def test_a_run_cut_short_records_nothing(self, db, tenant, model_in_force):
        seed(db, tenant)
        db.add(AgentMemory(tenant_id=tenant.id, content="Maria moved to Lyon.",
                           embedding=THEIRS, embedding_status="done"))
        db.commit()
        vectors.record_model(db, "the model before")
        manage.main(["recompute-vectors", "--apply", "--limit", "1"])
        db.expire_all()
        assert vectors.recorded_model(db) == "the model before"
