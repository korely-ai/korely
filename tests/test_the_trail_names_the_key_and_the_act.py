"""The trail says which key made a call and what the call did (review of
1 Nov 2026).

GET /v1/audit kept the key of every call in its table and answered without
it, while the dashboard and the documentation said the key was in the log;
and a memory forgotten (its text kept, out of every read) was filed as
`erase`, so a report of `action=erase` listed memories whose text the store
still held. Now each event carries `api_key_id`, a forget is `forget`, an
erasure `erase`, and a fact closed by /forget `fact_invalidate`, the verb
the trail already listed for it.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact, AgentMemory, AuditEvent
from korely_agent.services import keys as key_service
from korely_agent.services.ids import fact_public_id, mem_public_id
from tests.conftest import seeded_project_id


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
def keyed(db, tenant):
    """A key of the default project: its row and its header."""
    row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="t",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return row, {"Authorization": f"Bearer {full}"}


def memory(db, tenant):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="anna",
                    project_id=seeded_project_id(db, tenant.id), content="Anna is on Basic.")
    db.add(m)
    db.commit()
    return m


def fact(db, tenant):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="anna",
                  project_id=seeded_project_id(db, tenant.id), subject_label="Anna",
                  predicate_raw="is on", predicate_canonical="is_on", object_label="Basic",
                  valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    return f


def trail(client, head, **params):
    r = client.get("/v1/audit", headers=head, params=params)
    assert r.status_code == 200, r.text
    return r.json()["events"]


def one(events, target):
    (e,) = [e for e in events if e["target_id"] == target]
    return e


@pytest.mark.parametrize("erase,verb", [(False, "forget"), (True, "erase")],
                         ids=["forget", "erase"])
def test_deleting_a_memory_is_filed_as_what_it_did(db, tenant, client, keyed, erase, verb):
    row, head = keyed
    mid = mem_public_id(memory(db, tenant).id)
    r = client.delete(f"/v1/memories/{mid}", headers=head,
                      params={"erase": "true"} if erase else None)
    assert r.status_code == 200, r.text
    e = one(trail(client, head), mid)
    assert (e["action"], e["api_key_id"]) == (verb, str(row.id))
    # A report of erasures lists only the erasures.
    assert [x["target_id"] for x in trail(client, head, action="erase")] == ([mid] if erase else [])


@pytest.mark.parametrize("erase,verb", [(False, "forget"), (True, "erase")],
                         ids=["forget", "erase"])
@pytest.mark.parametrize("how", ["unknown", "malformed", "confined"])
def test_a_refused_deletion_says_which_act_was_refused(db, tenant, client, keyed, erase, verb,
                                                       how):
    """Every refusal of the door, the ones before the act included: an id
    that does not parse, and another end user's memory for a confined key."""
    _row, head = keyed
    mid = {"unknown": lambda: mem_public_id(uuid.uuid4()), "malformed": lambda: "mem_nope",
           "confined": lambda: mem_public_id(memory(db, tenant).id)}[how]()
    if how == "confined":
        _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="bruno's",
                                              end_users=["bruno"])
        asking = {"Authorization": f"Bearer {full}"}
    else:
        asking = head
    r = client.delete(f"/v1/memories/{mid}", headers=asking,
                      params={"erase": "true"} if erase else None)
    assert r.status_code == 404
    db.expire_all()
    (ev,) = [e for e in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id)
             if (e.meta or {}).get("endpoint") == "memories:delete"]
    assert (ev.action, ev.result) == (verb, "error")


def test_closing_a_fact_is_an_invalidation_not_an_erasure(db, tenant, client, keyed):
    row, head = keyed
    fid = fact_public_id(fact(db, tenant).id)
    assert client.post(f"/v1/facts/{fid}/forget", headers=head).status_code == 200
    e = one(trail(client, head), fid)
    assert (e["action"], e["api_key_id"]) == ("fact_invalidate", str(row.id))


@pytest.mark.parametrize("erase,verb", [(False, "forget"), (True, "erase")],
                         ids=["forget", "erase"])
def test_the_dashboards_delete_is_filed_as_what_it_did(db, tenant, client, keyed, erase, verb):
    row, head = keyed
    mid = mem_public_id(memory(db, tenant).id)
    r = client.delete(f"/admin/memories/{mid}", headers=head,
                      params={"erase": "true"} if erase else None)
    assert r.status_code == 200, r.text
    db.expire_all()
    (ev,) = [e for e in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id)
             if e.target_id == mid]
    assert ev.action == verb
    # The screen's own trail says the key too.
    screen = client.get("/admin/audit", headers=head).json()["events"]
    assert one(screen, mid)["api_key_id"] == str(row.id)


def test_an_act_without_a_key_says_none(db, tenant, client, keyed):
    from korely_agent.services import audit

    _row, head = keyed
    audit.record(db, tenant.id, audit.READ, actor="manage", target_id="mem_none",
                 project_id=seeded_project_id(db, tenant.id))
    db.commit()
    assert one(trail(client, head), "mem_none")["api_key_id"] is None


def test_every_call_of_a_door_that_forgets_or_erases_says_which():
    """The verb of such a door is read from `meta.erase` (services/usage.
    FORGETS_OR_ERASES), so every call that files one of its labels passes
    `meta`: a call written later that erased without it would be filed as a
    forget (review of 14 Nov 2026). Read from the source with the parser."""
    import ast
    import pathlib

    import korely_agent
    from korely_agent.services.usage import FORGETS_OR_ERASES

    calls = []
    for path in sorted(pathlib.Path(korely_agent.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and any(
                    isinstance(a, ast.Constant) and a.value in FORGETS_OR_ERASES
                    for a in [*node.args, *(k.value for k in node.keywords)]):
                calls.append((f"{path.name}:{node.lineno}",
                              any(k.arg == "meta" for k in node.keywords)))
    assert len(calls) >= 8, f"found {len(calls)} calls of the doors; the scan is broken"
    assert [where for where, says in calls if not says] == []
