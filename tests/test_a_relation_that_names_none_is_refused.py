"""A fact posted with a relation of one or two letters is refused (a finding
of 6 Nov 2026, a known issue until 1.0.40).

"è", "ha", "fa", "is": the store's reading of the relation
(memory_core.predicate_forms.canonicalize_predicate) makes such a relation
`unknown`, and every such fact of a subject then read as one relation, so the
judge closed one with another. The door now answers 422
`relation_says_nothing` and writes nothing. The rule is the store's, for every
script: a word of one or two letters of a script with case names no relation;
one of one or two characters of a script without case (Chinese, Japanese)
carries meaning and is written. No list of words decides.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact
from korely_agent.services import facts as agent_facts
from korely_agent.services import keys as key_service


@pytest.fixture
def client(db, monkeypatch):
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
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="t")
    return {"Authorization": f"Bearer {full}"}


def _post(client, auth, predicate):
    return client.post("/v1/facts", headers=auth, json={
        "subject": "Marco", "predicate": predicate, "object": "a driver", "user_id": "u1"})


@pytest.mark.parametrize("predicate", ["è", "ha", "fa", "is", "IS", " of ", "e'"],
                         ids=["e-grave", "ha", "fa", "is", "IS", "of", "e-apostrophe"])
def test_a_relation_of_one_or_two_letters_is_refused_and_nothing_is_written(
        db, client, auth, predicate):
    r = _post(client, auth, predicate)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "relation_says_nothing"
    assert "has_license" in r.json()["message"]
    assert db.query(AgentFact).count() == 0


@pytest.mark.parametrize("predicate", ["has_license", "喜欢", "住む", "is_on"],
                         ids=["has_license", "zh-likes", "ja-lives", "is_on"])
def test_a_relation_that_says_what_it_is_is_written(db, client, auth, predicate):
    r = _post(client, auth, predicate)
    assert r.status_code == 201, r.text
    assert db.query(AgentFact).one().predicate_canonical != "unknown"


def test_a_correction_to_a_relation_of_two_letters_is_refused(db, client, auth):
    fid = _post(client, auth, "has_license").json()["id"]
    r = client.patch(f"/v1/facts/{fid}", headers=auth, json={"predicate": "ha"})
    assert r.status_code == 422 and r.json()["code"] == "relation_says_nothing"
    assert db.query(AgentFact).count() == 1
    # A correction that keeps the relation is not asked about it.
    assert client.patch(f"/v1/facts/{fid}", headers=auth,
                        json={"object": "a pilot"}).status_code == 200


def test_the_service_refuses_too_whatever_door_calls_it(db, tenant):
    with pytest.raises(agent_facts.RelationSaysNothing):
        agent_facts.write_fact_direct(db, tenant.id, subject="Marco", predicate="è",
                                      obj="a driver", end_user_id="u1")
    with pytest.raises(agent_facts.RelationSaysNothing):
        agent_facts.correct_fact(db, tenant.id, uuid.uuid4(), predicate="is", project_id=None)
