"""The relation vocabulary is one per project (18 Oct 2026), and since that
evening one per end user of a project (tests/test_the_vocabulary_is_one_per_end_user.py).

The code audit of 17 Oct 2026 (P1): the vocabulary was the account's, and a
relation carries an end user's value as its example, shown to the model next
to the relation. A value written in project A (EU) reached the judge's prompt
of project B (Global), sent to B's provider. The reproduction of the audit:
"covered_under PolicyAlpha-verify-A" in A, then two values in B.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentFact, PredicateAlias, PredicateCluster
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from tests.conftest import in_the_judges_shape, is_the_judge


SECRET = "PolicyAlpha-verify-A"


class Model:
    def __init__(self):
        self.prompts = []

    def complete(self, messages, **kw):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        if is_the_judge(prompt):
            return LLMResult(text=in_the_judges_shape(prompt, '{"contradictions": []}'))
        if '"family"' in prompt:
            return LLMResult(text='{"family": "other"}')
        if "many_values" in prompt:
            return LLMResult(text='{"many_values": false}')
        return LLMResult(text='{"same_as": null}')


@pytest.fixture
def model(monkeypatch):
    m = Model()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: m)
    return m


@pytest.fixture
def client(db, monkeypatch, model):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def key_for(db, tenant, project):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k {project.name}",
                                            project_id=project.id)
    return {"Authorization": f"Bearer {full}"}


def post(client, auth, obj):
    r = client.post("/v1/facts", headers=auth, json={
        "user_id": "c1", "subject": "Customer 1", "predicate": "covered_under", "object": obj})
    assert r.status_code == 201, r.text


def test_a_value_of_one_project_never_reaches_another_projects_prompts(client, db, tenant, model):
    a = project_service.create_project(db, tenant_id=tenant.id, name="Alpha")
    b = project_service.create_project(db, tenant_id=tenant.id, name="Beta")
    post(client, key_for(db, tenant, a), SECRET)
    seen_in_a = len(model.prompts)

    auth_b = key_for(db, tenant, b)
    post(client, auth_b, "PolicyBeta")
    post(client, auth_b, "PolicyGamma")

    assert not any(SECRET in p for p in model.prompts[seen_in_a:]), "A's value in B's prompts"
    db.expire_all()
    rel = {r.project_id: r for r in db.query(PredicateCluster).filter(
        PredicateCluster.tenant_id == tenant.id, PredicateCluster.canonical == "covered_under")}
    assert set(rel) == {a.id, b.id}
    assert rel[a.id].example_object == SECRET
    assert rel[b.id].example_object == "PolicyBeta"
    phrases = {(p.surface, p.project_id) for p in db.query(PredicateAlias).filter(
        PredicateAlias.tenant_id == tenant.id)}
    assert ("covered_under", a.id) in phrases and ("covered_under", b.id) in phrases


def test_a_deleted_projects_vocabulary_follows_its_facts(client, db, tenant, model):
    default = project_service.get_or_create_default_project(db, tenant.id)
    gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
    post(client, key_for(db, tenant, default), "PolicyDefault")
    auth_g = key_for(db, tenant, gamma)
    post(client, auth_g, "PolicyGamma")
    r = client.post("/v1/facts", headers=auth_g, json={
        "user_id": "c1", "subject": "Customer 1", "predicate": "mentored_by", "object": "Elena"})
    assert r.status_code == 201, r.text

    out = project_service.delete_project(db, tenant.id, gamma)

    assert out["relations_moved"] == 1, "mentored_by moves whole; covered_under joins its twin"
    db.expire_all()
    rel = {r.canonical: r for r in db.query(PredicateCluster).filter(
        PredicateCluster.tenant_id == tenant.id)}
    assert {r.project_id for r in rel.values()} == {default.id}
    assert rel["covered_under"].example_object == "PolicyDefault"
    assert rel["mentored_by"].example_object == "Elena"
    assert db.query(AgentFact).filter(AgentFact.project_id == default.id).count() == 3


def test_a_read_resolves_the_phrase_in_its_own_projects_vocabulary(client, db, tenant, model):
    """One phrase can name another relation in another project: a read of
    project B by `insured_by` finds B's facts by B's relation, never A's."""
    import uuid as _uuid

    a = project_service.create_project(db, tenant_id=tenant.id, name="Alpha")
    b = project_service.create_project(db, tenant_id=tenant.id, name="Beta")
    for project, canonical, obj in ((a, "covered_under", "PolicyA"), (b, "insured_with", "PolicyB")):
        rel = PredicateCluster(id=_uuid.uuid4(), tenant_id=tenant.id, project_id=project.id,
                               end_user_id="c1", canonical=canonical)
        db.add(rel)
        db.flush()
        db.add(PredicateAlias(tenant_id=tenant.id, project_id=project.id, end_user_id="c1",
                              surface="insured_by", cluster_id=rel.id))
        db.add(AgentFact(tenant_id=tenant.id, project_id=project.id, end_user_id="c1",
                         subject_label="Customer 1", subject_canonical="Customer 1",
                         predicate_raw="insured by", predicate_canonical=canonical,
                         object_label=obj, object_canonical=obj))
    db.commit()
    r = client.get("/v1/facts", headers=key_for(db, tenant, b),
                   params={"user_id": "c1", "predicate": "insured by"})
    assert r.status_code == 200, r.text
    assert [f["object"] for f in r.json()["facts"]] == ["PolicyB"]
