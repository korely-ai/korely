"""POST /v1/users/{id}/names: an end user's name, declared (18 Oct 2026).

The store never merges two names of a person by itself (two people can share
one). On the identity bench of 17 Oct the declared name united the full name
and the id in 23 pairs of 28 with no wrong merge, but only the dashboard could
declare it (`POST /admin/aliases`, an admin key). A key that writes the end
user's memories now declares it too: `korely init --name` calls this.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AuditEvent
from korely_agent.services import keys as key_service
from tests.conftest import in_the_judges_shape, is_the_judge



class Model:
    def complete(self, messages, **kw):
        prompt = messages[0]["content"]
        if is_the_judge(prompt):
            return LLMResult(text=in_the_judges_shape(prompt, '{"contradictions": []}'))
        if '"family"' in prompt:
            return LLMResult(text='{"family": "places"}')
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


@pytest.fixture
def auth(db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="claude-code")
    return {"Authorization": f"Bearer {full}"}


def facts_about(client, auth, subject):
    r = client.get("/v1/facts", headers=auth, params={"user_id": "maria", "subject": subject})
    assert r.status_code == 200, r.text
    return sorted(f["object"] for f in r.json()["facts"])


def test_the_facts_about_the_name_become_the_end_users(client, auth):
    r = client.post("/v1/facts", headers=auth, json={
        "user_id": "maria", "subject": "Maria Rossi", "predicate": "lives_in", "object": "Rome"})
    assert r.status_code == 201, r.text
    assert facts_about(client, auth, "maria") == []

    r = client.post("/v1/users/maria/names", headers=auth, json={"name": "Maria Rossi"})
    assert r.status_code == 201, r.text
    assert r.json()["user_id"] == "maria" and r.json()["name"] == "Maria Rossi"
    assert facts_about(client, auth, "maria") == ["Rome"], "the fact already written reads as hers"

    again = client.post("/v1/users/maria/names", headers=auth, json={"name": "Maria Rossi"})
    assert again.status_code == 201 and again.json()["name"] == "Maria Rossi"


def test_a_name_decided_otherwise_by_hand_is_409(client, auth, db, tenant):
    _r, admin = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    r = client.post("/admin/aliases", headers={"Authorization": f"Bearer {admin}"},
                    json={"alias": "Maria Rossi", "canonical": "Mary Ross", "user_id": "maria"})
    assert r.status_code == 201, r.text
    r = client.post("/v1/users/maria/names", headers=auth, json={"name": "Maria Rossi"})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "conflict"
    # The spec says the code the route answers, not the generic `stale_write`
    # of the other /v1 409s (A, from the SDK docs of 18 Oct 2026).
    spec = client.get("/openapi.json").json()
    text = spec["paths"]["/v1/users/{end_user}/names"]["post"]["responses"]["409"]["description"]
    assert "`conflict`" in text and "stale_write" not in text


def test_the_id_itself_is_not_a_name(client, auth):
    r = client.post("/v1/users/maria/names", headers=auth, json={"name": "maria"})
    assert r.status_code == 422, r.text


def test_a_key_confined_to_other_end_users_is_refused(client, db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="confined",
                                            end_users=["luca"])
    r = client.post("/v1/users/maria/names", headers={"Authorization": f"Bearer {full}"},
                    json={"name": "Maria Rossi"})
    assert r.status_code == 403, r.text


def test_it_is_in_the_trail_without_the_name(client, auth, db, tenant):
    r = client.post("/v1/users/maria/names", headers=auth, json={"name": "Maria Rossi"})
    assert r.status_code == 201, r.text
    db.expire_all()
    rows = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id,
                                       AuditEvent.action == "fact_write").all()
    assert rows and rows[-1].end_user_id == "maria"
    assert "Maria Rossi" not in repr([r.meta for r in rows])


def test_declaring_and_removing_a_name_are_writes_for_the_plan(client, auth, db, tenant):
    """Audit of 27 Oct 2026, the rename's re-embedding was not metered: a
    name declared or removed re-embeds every fact that names it, and none of
    the three doors went through the plan. Each is one write for the hosted
    quota now (the hook does nothing on an install of one's own)."""
    from korely_agent.core import extensions

    counted = []
    extensions.register("before_write", lambda db_, tenant_id, contents: counted.append(
        (tenant_id, list(contents))))
    try:
        r = client.post("/v1/users/maria/names", headers=auth, json={"name": "Maria Rossi"})
        assert r.status_code == 201, r.text
        assert len(counted) == 1

        _row, full = key_service.create_api_key(
            db, tenant_id=tenant.id, name="operator",
            scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
        admin = {"Authorization": f"Bearer {full}"}
        r = client.post("/admin/aliases", headers=admin,
                        json={"alias": "M. Rossi", "canonical": "Maria Rossi", "user_id": "maria"})
        assert r.status_code == 201, r.text
        assert len(counted) == 2
        r = client.delete(f"/admin/aliases/{r.json()['id']}", headers=admin)
        assert r.status_code == 200, r.text
        assert len(counted) == 3
    finally:
        extensions.reset()


@pytest.mark.parametrize("user", ["mario@example.com", "4812", "auth0|abc", "7c9e6679-7425-40de-944b-e07fc1f90ae7"],
                         ids=["email", "number", "auth0", "uuid-with-a-digit"])
def test_any_user_id_can_be_given_a_name(client, auth, user):
    """memory_core 6.24.0: the user_id is the caller's
    key for a person, not a name (as Zep: a user_id of up to 500 characters,
    the names separate fields); until 1.0.39 these answered 422."""
    from urllib.parse import quote

    r = client.post("/v1/facts", headers=auth, json={
        "user_id": user, "subject": "Maria Rossi", "predicate": "lives_in", "object": "Rome"})
    assert r.status_code == 201, r.text
    r = client.post(f"/v1/users/{quote(user, safe='')}/names", headers=auth, json={"name": "Maria Rossi"})
    assert r.status_code == 201, r.text
    assert r.json()["user_id"] == user
    got = client.get("/v1/facts", headers=auth, params={"user_id": user, "subject": user})
    assert sorted(f["object"] for f in got.json()["facts"]) == ["Rome"], "the fact on the name reads as the end user's"


def test_the_name_still_has_to_be_a_name(client, auth):
    r = client.post("/v1/users/mario%40example.com/names", headers=auth, json={"name": "IBM, HP"})
    assert r.status_code == 422, r.text


def test_an_end_user_id_as_canonical_is_read_as_an_id():
    from korely_agent.services.memory_core import identity

    ok = identity.new_alias_outcome({}, "Maria", "auth0|abc", source=identity.SOURCE_USER, existing_source=None,
                                    existing_canonical=None, canonical_is_an_end_user=True)
    assert ok[0] == "created"
    # Not for a canonical that is not the end user's id, nor for an alias the engine makes.
    assert identity.new_alias_outcome({}, "Maria", "auth0|abc", source=identity.SOURCE_USER, existing_source=None,
                                      existing_canonical=None)[0] == "invalid"
    assert identity.valid_end_user_id("a\x00b") is False and identity.valid_end_user_id("x" * 256) is False


@pytest.mark.parametrize("n", [200, 201, 255], ids=["200", "201", "255"])
def test_a_user_id_wider_than_the_canonical_columns_is_a_clear_422(client, auth, n):
    """The review of 25 Nov 2026: the canonical columns hold 200 characters; a user_id
    of 201 to 255 answered 500. Now a 422 that says why."""
    user = "u" * (n - 12) + "@example.com"
    r = client.post(f"/v1/users/{user}/names", headers=auth, json={"name": "Maria Rossi"})
    if n <= 200:
        assert r.status_code == 201, r.text
    else:
        assert r.status_code == 422 and "at most 200 characters" in r.text, r.text


def test_the_id_rule_is_asked_inside_the_alias_rule():
    """The check of the id inside `new_alias_outcome` (a mutant of the review
    survived without it): an id that is not one is refused there too."""
    from korely_agent.services.memory_core import identity

    for bad in ("x" * 201, "a\x07b", "   "):
        assert identity.new_alias_outcome({}, "Maria", bad, source=identity.SOURCE_USER, existing_source=None,
                                          existing_canonical=None, canonical_is_an_end_user=True)[0] == "invalid", bad
