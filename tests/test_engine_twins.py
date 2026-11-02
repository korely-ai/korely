"""memory_core 2.8.0: the merge of near-duplicates at write time, off by
default.

The guards are those of the design proposal for duplicates (section 4),
tried on its ten examples with its distances; then the real write on the cloud's tables
(SQLite), with vectors built at the wanted distance. No model.
"""
# Port notes: the writes run on korely-agent's tables (`facts.entities.TABLES`,
# tenant_id) and on PostgreSQL, where the nearest rows come from pgvector
# (`vector_search.nearest`) instead of the Python distances SQLite used.
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import twins as g
from korely_agent.services.memory_core import thresholds
from korely_agent.services.memory_core.store import Relation
from korely_agent.services.memory_core.writing import write_fact

S = thresholds.EMBEDDINGGEMMA.twin


def _d(cosine: float) -> float:
    return 1.0 - cosine


class TestTheGuards:
    """The ten examples of section 1 of the proposal (cosine measured by B)."""

    def test_the_new_one_says_less_it_merges(self):
        assert g.same_claim("outdoor wedding", "outdoor wedding ceremony", _d(0.989), S)
        assert g.same_claim("1/24 scale '69 Camaro model", "1/24 scale '69 Camaro model kit",
                                     _d(0.978), S)

    def test_the_new_one_adds_words_it_is_a_refinement(self):
        assert not g.same_claim("outdoor wedding ceremony", "outdoor wedding", _d(0.989), S)
        assert not g.same_claim("coffee shops with free Wi-Fi and outdoor seating",
                                         "coffee shops with free Wi-Fi", _d(0.970), S)
        assert not g.same_claim("post-wedding celebration", "wedding", _d(0.941), S)

    def test_different_words_only_above_the_second_threshold(self):
        assert g.same_claim("exhaustion", "fatigue", _d(0.977), S)
        assert not g.same_claim("mustard velvet pillows", "teal velvet pillows", _d(0.942), S)

    def test_numbers_keep_them_apart(self):
        assert not g.same_claim("4 bikes", "3 bikes", _d(0.975), S)
        assert not g.same_claim("five tops from H&M", "5 H&M tops", _d(0.982), S)
        assert not g.same_claim("99.99 USD per month for 10 TB", "19.99 USD per month for 2 TB",
                                         _d(0.932), S)

    def test_proper_names_keep_them_apart(self):
        assert not g.same_claim("Oahu", "Hawaii", _d(0.948), S)
        assert not g.same_claim("Seoul", "South Korea", _d(0.96), S)

    def test_digits_of_every_alphabet_and_scripts_without_capitals(self):
        assert g.numbers("٣ دراجات") == g.numbers("3 bikes") == {"3"}
        assert g.capitalized("東京の旅行") == set()
        assert g.same_claim("東京 旅行", "東京 旅行 計画", _d(0.98), S)

    def test_without_a_distance_nothing(self):
        assert not g.same_claim("outdoor wedding", "outdoor wedding ceremony", None, S)


# ---------------------------------------------------------------------------
# The real write
# ---------------------------------------------------------------------------

def _vector(cosine: float) -> list:
    """A vector at that cosine from the base vector (axis 0)."""
    v = [0.0] * EMBEDDING_DIM
    v[0] = cosine
    v[1] = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    return v


BASE = _vector(1.0)


@pytest.fixture
def embeddinggemma():
    before = thresholds._source
    thresholds.use_model("embeddinggemma-300m")
    yield
    thresholds._source = before


def _memory(db, tenant) -> AgentMemory:
    m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content="x", graph_sync_status="done")
    db.add(m); db.commit(); db.refresh(m)
    return m


class _Writer:
    def __init__(self, db, tenant):
        self.db, self.tenant = db, tenant
        self.judged = []

    def write(self, obj, vector, *, tense="current", relation=None, source=None, when=None,
               twins=True, confidence=0.8):
        source = source or _memory(self.db, self.tenant).id

        def embed(f):
            f.embedding = vector

        return write_fact(
            self.db, entities.TABLES, entities.memory_of(self.tenant.id, None, "u1", None),
            columns=dict(tenant_id=self.tenant.id, project_id=None, end_user_id="u1", agent_id=None, run_id=None),
            relation=relation or Relation(canonical="plans", is_functional=False, family="events"),
            is_open=true_at(), embed=embed,
            judge=lambda d: self.judged.append(d) or [],
            subject="u1", subject_type="person", predicate_raw="plans", obj=obj,
            object_is_literal=True, confidence=confidence, source_memory_id=source, model_used=None,
            tense=tense, valid_from=when, twins=twins,
        )

    def open_rows(self):
        # The vector arrives after the insert (step 6) and the test session
        # does not autoflush: without this flush, expire_all would throw it away.
        self.db.flush()
        self.db.expire_all()
        return self.db.query(AgentFact).filter(AgentFact.invalid_at.is_(None)).all()


PLAN = Relation(canonical="plans", is_functional=False, family="events")
OWNS = Relation(canonical="owns", is_functional=False, family="ownership")
NAME = Relation(canonical="also_known_as", is_functional=False, family="identity")


class TestTheWrite:
    def test_off_by_default_two_rows(self, db, tenant, embeddinggemma):
        w = _Writer(db, tenant)
        w.write("outdoor wedding ceremony", BASE, tense="planned", twins=False)
        w.write("outdoor wedding", _vector(0.989), tense="planned", twins=False)
        assert len(w.open_rows()) == 2

    def test_on_an_unmeasured_model_it_stays_off(self, db, tenant):
        before = thresholds._source
        thresholds.use_model("gemini-embedding-2")
        try:
            w = _Writer(db, tenant)
            w.write("outdoor wedding ceremony", BASE, tense="planned")
            w.write("outdoor wedding", _vector(0.989), tense="planned")
            assert len(w.open_rows()) == 2
        finally:
            thresholds._source = before

    def test_the_duplicate_merges_into_the_existing_row(self, db, tenant, embeddinggemma):
        w = _Writer(db, tenant)
        m1, m2 = _memory(db, tenant).id, _memory(db, tenant).id
        first, _ = w.write("outdoor wedding ceremony", BASE, tense="planned", source=m1, confidence=0.7)
        second, closed = w.write("outdoor wedding", _vector(0.989), tense="planned", source=m2,
                                   confidence=0.9)
        assert second.id == first.id and closed == []
        [row] = w.open_rows()
        assert row.object_label == "outdoor wedding ceremony"
        assert row.observation_count == 2
        assert set(row.source_memory_ids) == {str(m1), str(m2)}
        assert row.confidence == pytest.approx(0.9)
        assert w.judged == []

    def test_an_update_stays_two_rows_and_goes_to_the_judge(self, db, tenant, embeddinggemma):
        w = _Writer(db, tenant)
        w.write("3 bikes", BASE, relation=OWNS)
        w.write("4 bikes", _vector(0.975), relation=OWNS)
        assert len(w.open_rows()) == 2
        assert len(w.judged) == 2

    def test_a_refinement_stays_two_rows(self, db, tenant, embeddinggemma):
        w = _Writer(db, tenant)
        w.write("outdoor wedding", BASE, tense="planned")
        w.write("outdoor wedding ceremony", _vector(0.989), tense="planned")
        assert len(w.open_rows()) == 2

    def test_the_same_memory_does_not_merge_with_itself(self, db, tenant, embeddinggemma):
        w = _Writer(db, tenant)
        m = _memory(db, tenant).id
        w.write("outdoor wedding ceremony", BASE, tense="planned", source=m)
        w.write("outdoor wedding", _vector(0.989), tense="planned", source=m)
        assert len(w.open_rows()) == 2

    def test_two_events_on_different_days_stay_two(self, db, tenant, embeddinggemma):
        w = _Writer(db, tenant)
        attended = Relation(canonical="attended", is_functional=False, family="events")
        w.write("the pottery workshop", BASE, relation=attended, when=datetime(2023, 5, 1, tzinfo=timezone.utc))
        w.write("pottery workshop", _vector(0.99), relation=attended,
                 when=datetime(2023, 6, 1, tzinfo=timezone.utc))
        assert len(w.open_rows()) == 2
        w.write("workshop", _vector(0.99), relation=attended, when=datetime(2023, 6, 1, 18, tzinfo=timezone.utc))
        assert len(w.open_rows()) == 2  # the same day as the second: merged into it

    def test_the_fulfilled_project_stays_in_the_history_and_a_true_fact_does_not_go_back(
        self, db, tenant, embeddinggemma,
    ):
        """3.0.0: a plan said in other words that comes true is closed like
        the one with the same words (writing.write_fact), without merging:
        before, the current fact entered the plan and promoted it in place."""
        w = _Writer(db, tenant)
        plan, _ = w.write("a new road bike", BASE, tense="planned", relation=OWNS)
        fact, closed = w.write("new road bike", _vector(0.99), tense="current", relation=OWNS)
        [row] = w.open_rows()
        assert row.id == fact.id != plan.id and row.tense == "current"
        plan = db.get(AgentFact, plan.id)
        assert plan.tense == "planned" and plan.invalid_at is not None and plan.invalidated_by_id == fact.id
        assert closed == [plan.id]
        # The fulfilled fact is a new fact: it went to the judge.
        assert [d.fact_id for d in w.judged] == [fact.id]
        w.write("road bike", _vector(0.99), tense="planned", relation=OWNS)
        [row] = w.open_rows()
        assert row.id == fact.id and row.tense == "current" and row.observation_count == 2

    def test_identity_does_not_merge(self, db, tenant, embeddinggemma):
        w = _Writer(db, tenant)
        w.write("coffee press", BASE, relation=NAME)
        w.write("cafetiere", _vector(0.966), relation=NAME)
        assert len(w.open_rows()) == 2

    def test_the_nearest_wins(self, db, tenant, embeddinggemma):
        w = _Writer(db, tenant)
        # At 0.92 from the base: the second does not merge into the first (0.93 for whoever says less).
        far, _ = w.write("small outdoor wedding ceremony by the lake", _vector(0.92), tense="planned")
        near, _ = w.write("outdoor wedding ceremony by the lake", BASE, tense="planned")
        merged, _ = w.write("outdoor wedding by the lake", _vector(0.99), tense="planned")
        assert merged.id == near.id != far.id

    def test_without_a_vector_no_merge(self, db, tenant, embeddinggemma):
        w = _Writer(db, tenant)
        w.write("outdoor wedding ceremony", BASE, tense="planned")
        w.write("outdoor wedding", None, tense="planned")
        assert len(w.open_rows()) == 2


# The merge is off by default, and nothing held it there: the test above
# passes twins=False by hand, _Writer defaults to True, and conftest's
# Gemini thresholds have no `twin`, so flipping write_fact's default or
# passing twins=True from the adapter (without touching memory_core)
# changed no test, while on EmbeddingGemma, production's model, the merge
# would have started in silence. CONTRIBUTING.md: a switch of the engine off
# by default goes on only after a measurement (audit of 17 Oct 2026). Here the write korely-agent makes, on EmbeddingGemma's thresholds,
# with the pair the guards would fuse.

def test_the_product_write_does_not_merge(db, tenant, embeddinggemma, monkeypatch):
    from korely_agent.services.facts import service

    assert g.same_claim("outdoor wedding", "outdoor wedding ceremony", _d(0.989), S), \
        "the control: on with these thresholds, the pair would fuse"
    vector_search = {"outdoor wedding ceremony": BASE, "outdoor wedding": _vector(0.989)}
    monkeypatch.setattr(service, "fact_vector",
                        lambda text: next(v for o, v in sorted(vector_search.items(), key=lambda kv: -len(kv[0]))
                                          if o in text))
    for obj in ("outdoor wedding ceremony", "outdoor wedding"):
        service.write_fact_direct(db, tenant.id, subject="u1", predicate="plans", obj=obj,
                                  end_user_id="u1", object_is_literal=True, tense="planned")
    db.expire_all()
    open_rows = db.query(AgentFact).filter(AgentFact.invalid_at.is_(None)).all()
    assert sorted(f.object_label for f in open_rows) == ["outdoor wedding", "outdoor wedding ceremony"]
