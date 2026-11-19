"""An erasure (an end user, an agent) takes with it what their facts left
elsewhere (memory_core 2.11.0, the audit of 2026-10-06, P0 n. 2): the
entity aliases learned from them, and the vocabulary's example, the object of
the fact a relation was learned from, which went on into the prompts written
for everybody else. No model is called; the centroid is recomputed by a fake
embedder.
"""
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent.db.models import AgentEntityAlias, AgentFact, AgentMemory, PredicateCluster
from korely_agent.services.facts import service as S
from korely_agent.services.sentinels import EVERY_PROJECT



@pytest.fixture
def vectors(monkeypatch):
    from korely_agent.core import llm
    from korely_agent.core.llm import EMBEDDING_DIM

    asked = []
    monkeypatch.setattr(llm, "embed", lambda text, **kw: asked.append(text) or [0.1] * EMBEDDING_DIM)
    monkeypatch.setattr(llm, "current_google_key", lambda: "k")
    return asked


def _fact(db, tenant, end_user, obj, *, agent=None, at=datetime(2026, 3, 1, tzinfo=timezone.utc)):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=end_user, agent_id=agent,
                    content=f"{end_user} has {obj}.", graph_sync_status="done")
    db.add(m)
    db.commit()
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=end_user, agent_id=agent,
                  subject_label=end_user, predicate_raw="has condition",
                  predicate_canonical="has_condition", object_label=obj,
                  source_memory_id=m.id, valid_from=at)
    db.add(f)
    db.commit()
    return f


def _relation(db, tenant, end_user, example, *, project=None):
    """`has_condition` in one end user's vocabulary (one per end user of a
    project since 18 Oct 2026)."""
    r = PredicateCluster(tenant_id=tenant.id, project_id=project, end_user_id=end_user,
                         canonical="has_condition", example_object=example, member_count=1)
    db.add(r)
    db.commit()
    return r


def test_the_erased_end_users_vocabulary_goes_whole(db, tenant, vectors):
    """Every part of an end user's vocabulary was learned from their facts:
    the erasure takes it all, and another end user's is untouched (it never
    held Anna's value)."""
    _fact(db, tenant, "anna", "type 2 diabetes")
    _fact(db, tenant, "bruno", "asthma")
    hers = _relation(db, tenant, "anna", "type 2 diabetes").id
    his = _relation(db, tenant, "bruno", "asthma").id
    S.forget_end_user(db, tenant.id, "anna", project_id=EVERY_PROJECT)
    db.expire_all()
    assert db.get(PredicateCluster, hers) is None, "Anna's vocabulary outlived her erasure"
    assert db.get(PredicateCluster, his).example_object == "asthma"
    assert "diabetes" not in " ".join(vectors), "a centroid was made of her words"


def test_an_agents_erasure_leaves_the_end_users_own_value(db, tenant, vectors):
    """The erased agent's fact gave the example: it becomes a value of the
    same end user's facts still there, never another end user's."""
    _fact(db, tenant, "anna", "type 2 diabetes", agent="bot-7")
    _fact(db, tenant, "anna", "asthma", at=datetime(2026, 4, 1, tzinfo=timezone.utc))
    _fact(db, tenant, "bruno", "eczema", at=datetime(2026, 2, 1, tzinfo=timezone.utc))
    r = _relation(db, tenant, "anna", "type 2 diabetes")
    S.delete_agent(db, tenant.id, "bot-7", project_id=EVERY_PROJECT)
    db.expire_all()
    assert db.get(PredicateCluster, r.id).example_object == "asthma"
    # The centroid goes with the erased value; the vector drain computes it
    # again, counted to the account (tests/test_an_erasures_centroid_waits_
    # for_the_drain.py). No vector here is made of another end user's words.
    assert db.get(PredicateCluster, r.id).centroid is None
    assert "diabetes" not in " ".join(vectors) and "eczema" not in " ".join(vectors)


def test_the_last_fact_gone_leaves_no_example(db, tenant, vectors):
    _fact(db, tenant, "anna", "type 2 diabetes", agent="bot-7")
    _fact(db, tenant, "bruno", "asthma")
    r = _relation(db, tenant, "anna", "type 2 diabetes")
    S.delete_agent(db, tenant.id, "bot-7", project_id=EVERY_PROJECT)
    db.expire_all()
    assert db.get(PredicateCluster, r.id).example_object is None


def test_the_aliases_of_the_erased_end_user_and_agent_go(db, tenant, vectors):
    for eu, agent in (("anna", None), ("bruno", None), (None, "bot-7")):
        db.add(AgentEntityAlias(tenant_id=tenant.id, end_user_id=eu, agent_id=agent,
                                alias_lower="my wife", alias_label="my wife",
                                canonical_label="Maria"))
    db.commit()
    S.forget_end_user(db, tenant.id, "anna", project_id=EVERY_PROJECT)
    S.delete_agent(db, tenant.id, "bot-7", project_id=EVERY_PROJECT)
    db.expire_all()
    left = [(a.end_user_id, a.agent_id) for a in db.query(AgentEntityAlias).all()]
    assert left == [("bruno", None)]


def test_erasing_a_project_takes_its_values_out_of_the_vocabulary(db, tenant, vectors):
    """DELETE /admin/projects/{id}?erase=true was the one erasure that left
    the vocabulary's example behind (audit of 17 Oct 2026). The project's
    vocabularies go with its row; another project's are untouched."""
    from korely_agent.services import projects

    client_a = projects.create_project(db, tenant_id=tenant.id, name="Client A")
    anna = _fact(db, tenant, "anna", "type 2 diabetes")
    db.query(AgentFact).filter(AgentFact.id == anna.id).update({AgentFact.project_id: client_a.id})
    db.query(AgentMemory).filter(AgentMemory.id == anna.source_memory_id).update(
        {AgentMemory.project_id: client_a.id})
    db.commit()
    _fact(db, tenant, "bruno", "asthma")
    hers = _relation(db, tenant, "anna", "type 2 diabetes", project=client_a.id).id
    his = _relation(db, tenant, "bruno", "asthma").id
    out = projects.delete_project(db, tenant.id, client_a, erase=True)
    assert out["facts_deleted"] == 1
    db.expire_all()
    assert db.get(PredicateCluster, hers) is None
    assert db.get(PredicateCluster, his).example_object == "asthma"
    assert "diabetes" not in " ".join(vectors)


def test_the_markdown_copy_goes_with_the_erasure(db, tenant, vectors, tmp_path, monkeypatch):
    """FACTS_EXPORT_DIR: the erased end user's file outlived the erasure that
    answered "permanent" (audit of 17 Oct 2026)."""
    from korely_agent import config
    from korely_agent.services import export_md

    monkeypatch.setenv("FACTS_EXPORT_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    try:
        _fact(db, tenant, "anna", "type 2 diabetes")
        _fact(db, tenant, "bruno", "asthma")
        export_md.export_all(db, str(tmp_path))
        folder = tmp_path / str(tenant.id)
        assert sorted(p.name for p in folder.iterdir()) == ["anna.md", "bruno.md"]
        (folder / "notes.txt").write_text("the operator's own file")
        S.forget_end_user(db, tenant.id, "anna", project_id=EVERY_PROJECT)
        assert sorted(p.name for p in folder.iterdir()) == ["bruno.md", "notes.txt"]
        S.forget_end_user(db, tenant.id, "bruno", project_id=EVERY_PROJECT)
        export_md.export_all(db, str(tmp_path))
        assert sorted(p.name for p in folder.iterdir()) == ["notes.txt"]
    finally:
        config.get_settings.cache_clear()
