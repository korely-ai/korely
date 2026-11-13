"""memory_core 2.4.0: the distance thresholds belong to the embedding model.

A cosine distance means something only in the space of the model that made the
vectors. Until 2.3.0 the two cutoffs were constants tuned on Gemini; from 2.4.0
each engine says which model makes its vectors (`thresholds.use_model`) and the
readers take `thresholds.current()` at every use, so the thresholds follow the
embedding model the install is given.

No model is called.
"""
# Notes: the binding of the model in use is this product's: korely-agent binds
# `core.llm.embedding_model_name` (EMBEDDING_PROVIDER / EMBEDDING_MODEL) through
# `core.llm.pick_distance_thresholds`, tested by
# tests/test_local_embedder.py::TestTheThresholdsFollowTheModel (the thresholds
# follow the provider, are read at every use, and a model nobody measured
# stops the start).
from __future__ import annotations

from types import SimpleNamespace

import pytest

from korely_agent.services.memory_core import store, thresholds


@pytest.fixture(autouse=True)
def _restore_the_model():
    before = thresholds._source
    yield
    thresholds._source = before


class TestFamilies:
    @pytest.mark.parametrize("name", [
        "gemini-embedding-2", "gemini-embedding-2-preview",
        "models/gemini-embedding-2-preview", " Gemini-Embedding-2 ",
    ])
    def test_gemini(self, name):
        assert thresholds.for_model(name) == thresholds.GEMINI
        assert thresholds.family(name) == "gemini-embedding-2"

    @pytest.mark.parametrize("name", ["embeddinggemma-300m", "EmbeddingGemma-300m"])
    def test_embeddinggemma(self, name):
        assert thresholds.for_model(name) == thresholds.EMBEDDINGGEMMA

    @pytest.mark.parametrize("name", [
        "gemini-embedding-001",       # another Gemini model: another space
        "embeddinggemma-1b",          # another size: another space
        "nomic-embed-text-v1.5", "text-embedding-3-small", "", None,
    ])
    def test_a_never_measured_model_does_not_take_the_thresholds_of_another(self, name):
        with pytest.raises(thresholds.ModelWithoutThresholds):
            thresholds.for_model(name)

    def test_the_measured_values(self):
        assert thresholds.GEMINI == thresholds.Thresholds(semantic=0.45, shortlist=0.65)
        assert thresholds.EMBEDDINGGEMMA == thresholds.Thresholds(
            semantic=0.67, shortlist=0.67,
            # 2.8.0: the merge of near-duplicates, read only on EmbeddingGemma.
            twin=thresholds.Twin(content=0.07, different=0.05),
        )
        assert thresholds.GEMINI.twin is None


class TestTheModelInUse:
    def test_a_name(self):
        assert thresholds.use_model("embeddinggemma-300m") == thresholds.EMBEDDINGGEMMA
        assert thresholds.model() == "embeddinggemma-300m"
        assert thresholds.current() == thresholds.EMBEDDINGGEMMA

    def test_a_function_read_at_every_use(self):
        state = {"name": "gemini-embedding-2"}
        thresholds.use_model(lambda: state["name"])
        assert thresholds.current() == thresholds.GEMINI
        state["name"] = "embeddinggemma-300m"
        assert thresholds.current() == thresholds.EMBEDDINGGEMMA

    def test_a_never_measured_model_stops_at_once(self):
        thresholds.use_model("gemini-embedding-2")
        with pytest.raises(thresholds.ModelWithoutThresholds):
            thresholds.use_model("nomic-embed-text-v1.5")
        assert thresholds.model() == "gemini-embedding-2"  # the previous one stays


class _Column:
    def cosine_distance(self, _v):
        return self

    def label(self, _n):
        return self

    def isnot(self, _x):
        return True


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_a):
        return self

    def order_by(self, *_a):
        return self

    def limit(self, *_a):
        return self

    def all(self):
        return self.rows


class TestTheVocabularyShortlist:
    """store._candidates: the known relations offered for a new phrase are
    those within the shortlist of the model in use."""

    ROWS = [("near", 0.60), ("middle", 0.66), ("far", 0.70)]

    def _candidates(self):
        db = SimpleNamespace(query=lambda *_a: _Query(self.ROWS))
        R = SimpleNamespace(centroid=_Column())
        return store._candidates(db, R, SimpleNamespace(relations=True), [0.1] * 768)

    def test_gemini(self):
        thresholds.use_model("gemini-embedding-2")
        assert self._candidates() == ["near"]

    def test_embeddinggemma(self):
        thresholds.use_model("embeddinggemma-300m")
        assert self._candidates() == ["near", "middle"]
