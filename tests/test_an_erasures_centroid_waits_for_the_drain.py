"""An erasure computes no vector: the drain does, counted and in the region (audit of 20 Oct 2026).

`facts.erasure._erase_examples` gave the vocabulary's examples a new value
after an erasure and computed the relation's centroid again there, with the
embedder, outside `regions.writing_for` and `llm_usage.attributed`: with
EMBEDDING_PROVIDER=gemini the call was nobody's and in no project's region.
Now the centroid goes with the erased value, and the
vector drain (`predicates.fill_missing_centroids`) computes it from the new
example, inside both. No erased value is in a centroid meanwhile: it is NULL.
"""
import uuid
from datetime import datetime, timezone

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory, PredicateCluster
from korely_agent.services import predicates
from korely_agent.services.facts import service as S
from korely_agent.services.sentinels import EVERY_PROJECT
from tests.conftest import seeded_project_id


def _fact(db, tenant, pid, obj, *, agent=None, at):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="anna",
                    agent_id=agent, content=f"anna has {obj}.", graph_sync_status="done")
    db.add(m)
    db.commit()
    db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="anna",
                     agent_id=agent, subject_label="anna", predicate_raw="has condition",
                     predicate_canonical="has_condition", object_label=obj,
                     source_memory_id=m.id, valid_from=at))
    db.commit()


def test_the_erasure_clears_the_centroid_and_the_drain_computes_it_attributed(db, tenant,
                                                                              monkeypatch):
    from korely_agent.services import llm_usage, regions

    calls = []
    inside = {"region": 0, "attributed": 0}
    real_region, real_attributed = regions.writing_for, llm_usage.attributed

    def region(*a, **k):
        inside["region"] += 1
        return real_region(*a, **k)

    def attributed(*a, **k):
        inside["attributed"] += 1
        return real_attributed(*a, **k)

    def embed(text, task=None):
        calls.append((text, inside["region"], inside["attributed"]))
        return [0.3] * EMBEDDING_DIM

    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.core.llm.embed_or_raise", embed)
    monkeypatch.setattr(regions, "writing_for", region)
    monkeypatch.setattr(llm_usage, "attributed", attributed)

    pid = seeded_project_id(db, tenant.id)
    _fact(db, tenant, pid, "type 2 diabetes", agent="bot-7",
          at=datetime(2026, 3, 1, tzinfo=timezone.utc))
    _fact(db, tenant, pid, "asthma", at=datetime(2026, 4, 1, tzinfo=timezone.utc))
    rel = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                           end_user_id="anna", canonical="has_condition",
                           example_object="type 2 diabetes", centroid=[0.1] * EMBEDDING_DIM,
                           member_count=1)
    db.add(rel)
    db.commit()

    S.delete_agent(db, tenant.id, "bot-7", project_id=EVERY_PROJECT)
    db.expire_all()
    after = db.get(PredicateCluster, rel.id)
    assert after.example_object == "asthma"
    assert calls == [], "the erasure called the embedder, outside the account and the region"
    assert after.centroid is None, "the centroid made of the erased value stayed"

    assert predicates.fill_missing_centroids(db) == 1
    db.expire_all()
    assert db.get(PredicateCluster, rel.id).centroid is not None
    (text, in_region, in_account), = calls
    assert "asthma" in text and "diabetes" not in text
    assert in_region and in_account, "the drain's call is counted and in the project's region"
