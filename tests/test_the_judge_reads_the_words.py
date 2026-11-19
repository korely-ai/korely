"""The judge reads the words a fact was read from (memory_core 2.5.0).

BUG-200 on the hosted product: "Luca also works part-time at Google" closed
"Luca works at Apple". The judge saw two triples and a single-valued tag, and
the words saying the two jobs go together were lost in the extraction. Since
2.5.0 the extractor copies those words (`ExtractedFact.evidence`) and the
judge reads them under the new fact ("said as"), the way the hosted adapter
passed them. They are never stored
on the fact: a memory that is forgotten leaves no copy of its words.

No model is called: the judge is a fake that writes down its prompt.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentFact, AgentMemory, PredicateCluster
from korely_agent.services.facts import service as fsvc
from korely_agent.services.facts.contradiction import detect_and_invalidate
from korely_agent.services.memory_core.fact_extractor import ExtractedFact
from tests.conftest import stub_extraction


NOW = datetime.now(timezone.utc)
WORDS = "Luca also works part-time at Google"


class Judge:
    def __init__(self):
        self.prompts = []

    def complete(self, messages, **kw):
        self.prompts.append(messages[0]["content"])
        return LLMResult(text='{"contradictions": []}')


def apple(db, tenant):
    db.add(PredicateCluster(tenant_id=tenant.id, canonical="works_at", is_functional=True,
                            family="work", member_count=1, example_object="Apple"))
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="luca",
                  subject_label="Luca", subject_canonical="luca", predicate_raw="works_at",
                  predicate_canonical="works_at", object_label="Apple", object_canonical="apple",
                  valid_from=NOW - timedelta(days=100), confidence=0.9)
    db.add(f)
    db.commit()
    return f


def judge_google(db, tenant, **kw):
    return detect_and_invalidate(
        db, tenant.id, end_user_id="luca", new_fact_id=uuid.uuid4(),
        new_subject="Luca", new_subject_canonical="luca",
        new_predicate_raw="works_at", new_predicate_canonical="works_at",
        new_object="Google", new_object_canonical="google", new_valid_from=NOW,
        functional=True, example="Apple", **kw)


class TestTheJudgeSeesTheWords:
    def test_under_the_new_fact(self, db, tenant, monkeypatch):
        apple(db, tenant)
        j = Judge()
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: j)
        assert judge_google(db, tenant, evidence=WORDS) == []
        assert len(j.prompts) == 1
        assert f'said as: "{WORDS}"' in j.prompts[0]

    def test_without_words_the_triple_alone_as_before(self, db, tenant, monkeypatch):
        """The rule that mentions the words is in every prompt; the line under
        the new fact (memory_core.contradiction, `\\n  said as:`) only when
        there are words."""
        apple(db, tenant)
        j = Judge()
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: j)
        judge_google(db, tenant)
        assert "\n  said as:" not in j.prompts[0]


class TestFromTheExtractionToTheJudge:
    @pytest.fixture
    def judged(self, monkeypatch):
        """The words each new fact reaches the judge with, whether it is judged
        on its own (`evidence`) or with its write in one call (`evidence` of
        each new fact, `_BATCHED_JUDGE`)."""
        from korely_agent.services.facts import contradiction

        seen = []
        monkeypatch.setattr(fsvc, "_embed_fact", lambda *a, **k: None)
        monkeypatch.setattr(fsvc, "detect_and_invalidate",
                            lambda *a, **k: seen.append(k.get("evidence")) or [])
        monkeypatch.setattr(contradiction, "detect_and_invalidate_batch",
                            lambda *a, **k: seen.extend(nf.get("evidence") for nf in k["new_facts"]) or {})
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
        return seen

    def test_the_extracted_words_reach_the_judge_and_are_not_kept(self, db, tenant, monkeypatch, judged):
        m = AgentMemory(tenant_id=tenant.id, end_user_id="luca",
                        content="Luca works at Apple, and also works part-time at Google.",
                        graph_sync_status="pending")
        db.add(m)
        db.commit()
        stub_extraction(monkeypatch, fsvc, lambda *a, **k: [
            ExtractedFact("Luca", "works_at", "Google", 0.9, subject_type="person",
                          evidence=WORDS)])
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)

        assert judged == [WORDS]
        db.expire_all()
        row = db.query(AgentFact).filter(AgentFact.object_label == "Google").one()
        assert not hasattr(AgentFact, "evidence") and not hasattr(row, "evidence")
        stored = " ".join(str(getattr(row, c.name)) for c in AgentFact.__table__.columns)
        assert "part-time" not in stored, "the memory's words were stored on the fact"

    def test_a_fact_with_no_words_passes_none(self, db, tenant, monkeypatch, judged):
        m = AgentMemory(tenant_id=tenant.id, end_user_id="luca", content="Luca works at Google.",
                        graph_sync_status="pending")
        db.add(m)
        db.commit()
        stub_extraction(monkeypatch, fsvc, lambda *a, **k: [
            ExtractedFact("Luca", "works_at", "Google", 0.9, subject_type="person")])
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)
        assert judged == [None]
