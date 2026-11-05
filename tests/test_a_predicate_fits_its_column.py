"""A predicate's canonical form fits its column (memory_core 5.2.1).

Code audit findings, P2 (17 Oct 2026): `canonicalize_predicate` had no cap, and
NFKC plus casefold can make a form longer than what was written ("ß" casefolds
to "ss"). A 150-character predicate inside /v1's limit of 200 became 300
characters, the vocabulary's String(200) insert raised DataError, only
IntegrityError was caught, and the write answered 500. The dashboard's
correction had no limit at all.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentFact
from korely_agent.services import facts as agent_facts
from korely_agent.services import keys as key_service
from korely_agent.services.memory_core.predicate_forms import MAX_PREDICATE, canonicalize_predicate
from tests.conftest import in_the_judges_shape, is_the_judge, seeded_project_id


def test_a_form_that_grows_under_casefold_stays_within_the_column():
    assert len("ß" * 150) == 150
    form = canonicalize_predicate("ß" * 150)
    assert 0 < len(form) <= MAX_PREDICATE


def test_a_long_form_is_cut_between_words_never_inside_one():
    raw = "_".join(["remembers"] * 30)
    form = canonicalize_predicate(raw)
    assert len(form) <= MAX_PREDICATE
    assert set(form.split("_")) == {"remembers"}


def test_one_word_longer_than_the_column_is_cut_at_the_column():
    assert canonicalize_predicate("a" * 300) == "a" * MAX_PREDICATE


def test_a_form_within_the_column_is_unchanged():
    assert canonicalize_predicate("lives_in") == "lives_in"
    assert canonicalize_predicate("x" * MAX_PREDICATE) == "x" * MAX_PREDICATE


class Model:
    def complete(self, messages, **kw):
        prompt = messages[0]["content"]
        if is_the_judge(prompt):
            return LLMResult(text=in_the_judges_shape(prompt, '{"contradictions": []}'))
        if '"family"' in prompt:
            return LLMResult(text='{"family": "other"}')
        if "many_values" in prompt:
            return LLMResult(text='{"many_values": false}')
        return LLMResult(text='{"same_as": null}')


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Model())
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_a_predicate_that_grows_under_casefold_is_written_not_a_500(client, db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="t")
    r = client.post("/v1/facts", headers={"Authorization": f"Bearer {full}"}, json={
        "user_id": "maria", "subject": "Maria", "predicate": "ß" * 150, "object": "Rome"})
    assert r.status_code == 201, r.text
    db.expire_all()
    row = db.query(AgentFact).one()
    assert len(row.predicate_canonical) <= MAX_PREDICATE


@pytest.mark.parametrize("field,length", [("subject", 201), ("predicate", 201), ("object", 501)])
def test_the_dashboard_correction_has_the_limits_of_v1(client, db, tenant, field, length):
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    rome = agent_facts.write_fact_direct(
        db, tenant.id, subject="Maria", predicate="lives_in", obj="Rome",
        end_user_id="maria", project_id=seeded_project_id(db, tenant.id))

    r = client.patch(f"/admin/facts/{rome['id']}", headers={"Authorization": f"Bearer {full}"},
                     json={field: "x" * length})
    assert r.status_code == 422, r.text
    assert field in r.text
    db.expire_all()
    assert db.query(AgentFact).one().object_label == "Rome"
