"""The relation vocabulary is one per end user of a project (18 Oct 2026).

The rule: every end user is a thing of its own, every project is a thing
of its own. A relation keeps a value of the facts it was learned from (its
example, and the centroid made of it) and decisions taken on them (one value
or many, its family, its name). One per project, Anna's value went into the
prompts written for Bruno's facts, and her first conflict decided how his
facts were judged (the audit of 18 Oct 2026). Here, through the API, with a
model that records every prompt.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentFact, PredicateAlias, PredicateCluster
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services.facts import service as facts_service
from korely_agent.services.sentinels import EVERY_PROJECT
from tests.conftest import in_the_judges_shape, is_the_judge


ANNAS = "Platinum-Anna-7731"


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


@pytest.fixture
def auth(db, tenant):
    project = project_service.get_or_create_default_project(db, tenant.id)
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="k", project_id=project.id)
    return {"Authorization": f"Bearer {full}"}


def post(client, auth, user, obj, predicate="subscribes_to"):
    r = client.post("/v1/facts", headers=auth, json={
        "user_id": user, "subject": user, "predicate": predicate, "object": obj})
    assert r.status_code == 201, r.text


def relations(db, tenant):
    return {(r.end_user_id, r.canonical): r for r in
            db.query(PredicateCluster).filter(PredicateCluster.tenant_id == tenant.id)}


def test_one_end_users_value_never_reaches_anothers_prompts(client, db, tenant, model, auth):
    post(client, auth, "anna", ANNAS)
    seen = len(model.prompts)
    post(client, auth, "bruno", "Basic plan")
    post(client, auth, "bruno", "Pro plan")
    assert not any(ANNAS in p for p in model.prompts[seen:]), "Anna's value in Bruno's prompts"
    db.expire_all()
    rel = relations(db, tenant)
    assert rel[("anna", "subscribes_to")].example_object == ANNAS
    assert rel[("bruno", "subscribes_to")].example_object == "Basic plan", "his own value"
    phrases = {(p.end_user_id, p.surface) for p in db.query(PredicateAlias).filter(
        PredicateAlias.tenant_id == tenant.id)}
    assert {("anna", "subscribes_to"), ("bruno", "subscribes_to")} <= phrases


def test_one_end_users_vote_never_decides_anothers(client, db, tenant, model, auth):
    """Anna's two plans vote her relation one value at a time; Bruno's stays
    undecided until two of his own facts meet."""
    post(client, auth, "anna", "Basic plan")
    post(client, auth, "anna", "Pro plan")
    post(client, auth, "bruno", "Basic plan")
    db.expire_all()
    rel = relations(db, tenant)
    assert rel[("anna", "subscribes_to")].is_functional is True
    assert rel[("bruno", "subscribes_to")].is_functional is None


def test_a_read_of_several_end_users_resolves_each_in_their_own(client, db, tenant, auth):
    """A phrase Anna's vocabulary learned as another relation does not move
    Bruno's facts in or out of a read."""
    pid = project_service.get_or_create_default_project(db, tenant.id).id
    for user, canonical, obj in (("anna", "covered_under", "PolicyA"), ("bruno", "insured_with", "PolicyB")):
        rel = PredicateCluster(tenant_id=tenant.id, project_id=pid, end_user_id=user, canonical=canonical)
        db.add(rel)
        db.flush()
        if user == "anna":
            db.add(PredicateAlias(tenant_id=tenant.id, project_id=pid, end_user_id=user,
                                  surface="insured_by", cluster_id=rel.id))
        db.add(AgentFact(tenant_id=tenant.id, project_id=pid, end_user_id=user,
                         subject_label=user, subject_canonical=user, predicate_raw="insured by",
                         predicate_canonical=canonical, object_label=obj, object_canonical=obj))
    db.commit()
    r = client.get("/v1/facts", headers=auth, params={"predicate": "insured by"})
    assert r.status_code == 200, r.text
    assert [f["object"] for f in r.json()["facts"]] == ["PolicyA"]


def test_an_erasure_takes_the_end_users_vocabulary_and_only_theirs(client, db, tenant, model, auth):
    post(client, auth, "anna", ANNAS)
    post(client, auth, "bruno", "Basic plan")
    facts_service.forget_end_user(db, tenant.id, "anna", project_id=EVERY_PROJECT)
    db.expire_all()
    rel = relations(db, tenant)
    assert ("anna", "subscribes_to") not in rel
    assert rel[("bruno", "subscribes_to")].example_object == "Basic plan"
    assert not db.query(PredicateAlias).filter(PredicateAlias.end_user_id == "anna").count()
