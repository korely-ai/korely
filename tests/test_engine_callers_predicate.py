"""memory_core 3.3.0, BUG-202: a predicate the caller named keeps its name.

A fact written with `predicate: "plan"` came back as `role`: the vocabulary's
"same relation?" question merged `plan developer` into `role`, and the merge
stayed for every later write. A relation the developer named (POST /v1/facts,
a correction) is never merged by the model; the extractor's still can.

No model call: the vocabulary is a function.
"""
# Port notes: on korely-agent's tables (the helpers of
# tests/test_engine_uncovered_branches.py, tenant_id).
#   * TestInTheCloud::test_direct_write_does_not_merge is already here on
#     this product's adapter: tests/test_memory_core_3_3_adapter.py::
#     TestACallersPredicateKeepsItsName::test_direct_and_correction_do_not_merge
#     (`write_fact_direct` asks with merge=False). Not duplicated.
#   * TestInTheCloud::test_extraction_merges_as_before is ported on
#     korely-agent's adapter: `facts.service._persist_one` resolves the
#     relation through `predicates.resolve_relation`, which calls the engine's
#     `store.resolve_predicate` (the hosted test spied on its own
#     `entities.resolve_predicate`).
from __future__ import annotations


from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.services import predicates
from korely_agent.services.facts import entities
from korely_agent.services.facts import service as fsvc
from korely_agent.services.memory_core import store
from tests.test_engine_uncovered_branches import _relation, _resolve, _Vocabulary


class TestInTheEngine:
    def _shortlist(self, db, tenant, monkeypatch):
        role_relation = _relation(db, tenant, "role", centroid=[0.1] * EMBEDDING_DIM)
        monkeypatch.setattr(store, "_pg", lambda db: True)
        monkeypatch.setattr(store, "_candidates", lambda db, R, mem, vec: [role_relation])

    def test_by_default_the_model_merges(self, db, tenant, monkeypatch):
        self._shortlist(db, tenant, monkeypatch)
        voc = _Vocabulary(same=1)
        rel = _resolve(db, tenant, "plan", voc, embed=lambda t: [0.1] * EMBEDDING_DIM)
        assert rel.canonical == "role" and voc.asked == ["same"]

    def test_the_callers_predicate_stays_its_own(self, db, tenant, monkeypatch):
        self._shortlist(db, tenant, monkeypatch)
        voc = _Vocabulary(same=1)
        rel = store.resolve_predicate(db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None),
                                         "plan", "developer", ask=voc, embed=lambda t: [0.1] * EMBEDDING_DIM,
                                         strict=True, severe=True, merge=False)
        assert rel.canonical == "plan" and rel.new is True
        assert "same" not in voc.asked and "family" in voc.asked  # the rest is asked as before
        # The next write of the same name finds it, the model is not asked again.
        voc2 = _Vocabulary(same=1)
        again = store.resolve_predicate(db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None),
                                           "plan", "team", ask=voc2, embed=lambda t: [0.1] * EMBEDDING_DIM,
                                           strict=True, severe=True, merge=False)
        assert again.canonical == "plan" and voc2.asked == []


class TestInTheCloud:
    def _capture(self, monkeypatch):
        seen = []
        monkeypatch.setattr(predicates._store, "resolve_predicate",
                            lambda *a, **k: seen.append(k.get("merge", True)) or store.Relation("plan", None, "other"))
        return seen

    def test_extraction_merges_as_before(self, db, tenant, monkeypatch):
        seen = self._capture(monkeypatch)
        fsvc._persist_one(db, tenant.id, end_user_id="u1", agent_id=None, run_id=None,
                          subject="Acme", subject_type="organization", predicate_raw="plan", obj="developer",
                          object_is_literal=False, confidence=0.9, source_memory_id=None,
                          model_used="gemini-3.1-flash-lite", project_id=None)
        assert seen == [True]
