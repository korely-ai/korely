"""A write sent again with its `Idempotency-Key` is made once (1.0.27).

Until 1.0.26 the header was accepted and ignored: a client that retried
POST /v1/memories after a timeout stored the memory twice and, on the Cloud,
paid two writes (a code review finding, 15 Nov 2026). Now POST /v1/memories and
POST /v1/batch honour it as the IETF draft
draft-ietf-httpapi-idempotency-key-header-08 says: the same key with the
same request answers what the first made and writes nothing again; the same
key with another request is a 422; a retry while the first is being made is
a 409; a key holds 24 hours; a key is the account's and the project's. With
no header nothing changes. No model is called.
"""
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import (AgentBatchJob, AgentIdempotencyKey, AgentMemory,
                                    AgentProject, AuditEvent, Tenant)
from korely_agent.services import idempotency
from korely_agent.services import keys as key_service
from korely_agent.services.ids import parse_mem_id


@pytest.fixture(autouse=True)
def no_extraction(monkeypatch):
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    def one_request():
        # The request's session ends as get_db's does (db/base.py: closed,
        # so what it did not commit is rolled back): a refused write's key
        # must not live on into the next request of the test.
        try:
            yield db
        finally:
            db.rollback()

    app = create_app()
    app.dependency_overrides[get_db] = one_request
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _key(db, tenant_id, **kw):
    return key_service.create_api_key(db, tenant_id=tenant_id,
                                      name=f"k-{uuid.uuid4().hex[:6]}", **kw)


@pytest.fixture
def key(db, tenant):
    return _key(db, tenant.id)


def _heads(key, idem=None):
    h = {"Authorization": f"Bearer {key[1]}"}
    if idem is not None:
        h[idempotency.HEADER] = idem
    return h


ADA = {"content": "Ada prefers tea.", "user_id": "ada"}


def _post(client, key, idem=None, body=None):
    return client.post("/v1/memories", json=body or ADA, headers=_heads(key, idem))


def _batch(client, key, idem=None, rows=None):
    rows = rows or [{"content": f"fact {i}", "user_id": "ada"} for i in range(3)]
    return client.post("/v1/batch", json={"memories": rows}, headers=_heads(key, idem))


def _memories(db, tenant_id):
    db.expire_all()
    return db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant_id).all()


def _key_rows(db):
    db.expire_all()
    return db.query(AgentIdempotencyKey).all()


# ── without the header: as before ──────────────────────────────────────────

def test_without_the_header_two_identical_writes_are_two_memories(client, db, tenant, key):
    a, b = _post(client, key), _post(client, key)
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] != b.json()["id"]
    assert idempotency.REPLAYED not in b.headers
    assert len(_memories(db, tenant.id)) == 2
    assert _key_rows(db) == []


# ── the same key, the same request: made once ──────────────────────────────

def test_the_same_key_and_request_answer_the_first_memory_and_write_once(client, db, tenant,
                                                                         key):
    a = _post(client, key, idem='"retry-1"')
    b = _post(client, key, idem='"retry-1"')
    assert a.status_code == b.status_code == 201, b.text
    assert b.json()["id"] == a.json()["id"]
    assert b.json()["content"] == "Ada prefers tea." and b.json()["facts"] == []
    assert b.headers[idempotency.REPLAYED] == "true"
    assert idempotency.REPLAYED not in a.headers
    assert [str(m.id) for m in _memories(db, tenant.id)] == [str(parse_mem_id(a.json()["id"]))]
    # One write in the trail; the retry is the read of what it made.
    db.expire_all()
    rows = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).all()
    assert [r.action for r in rows if r.action == "write"] == ["write"]
    replays = [r for r in rows if (r.meta or {}).get("idempotent_replay")]
    assert len(replays) == 1 and replays[0].target_id == a.json()["id"]


def test_a_retry_answers_what_the_request_sent_never_a_later_edit(client, db, tenant):
    """A key that may only write cannot read a memory (403 on GET): its
    retry answers the memory with the words it sent, never another key's
    edit made since (code review finding, 15 Nov 2026)."""
    writer = _key(db, tenant.id, scopes=["memories:write"])
    other = _key(db, tenant.id)
    a = _post(client, writer, idem="k", body={**ADA, "metadata": {"n": 1}})
    mid = a.json()["id"]
    assert client.get(f"/v1/memories/{mid}", headers=_heads(writer)).status_code == 403
    edit = client.patch(f"/v1/memories/{mid}", headers=_heads(other),
                        json={"content": "Ada's door code is 4321."})
    assert edit.status_code == 200, edit.text
    b = _post(client, writer, idem="k", body={**ADA, "metadata": {"n": 1}})
    assert b.status_code == 201 and b.headers[idempotency.REPLAYED] == "true"
    assert b.json()["id"] == mid and b.json()["user_id"] == "ada"
    assert b.json()["content"] == "Ada prefers tea." and b.json()["metadata"] == {"n": 1}
    assert b.json()["facts"] == [] and b.json()["created_at"] == a.json()["created_at"]
    assert "4321" not in b.text


def test_the_quoted_and_the_bare_form_are_the_same_key(client, db, tenant, key):
    a = _post(client, key, idem='"8e03978e-40d5-43e8-bc93-6894a57f9324"')
    b = _post(client, key, idem="8e03978e-40d5-43e8-bc93-6894a57f9324")
    assert b.json()["id"] == a.json()["id"] and len(_memories(db, tenant.id)) == 1


def test_the_same_write_with_other_whitespace_is_the_same_request(client, db, tenant, key):
    """The fingerprint is of the request as checked: the text stripped."""
    a = _post(client, key, idem="k")
    b = _post(client, key, idem="k", body={**ADA, "content": "  Ada prefers tea.  "})
    assert b.status_code == 201 and b.json()["id"] == a.json()["id"]


def test_a_batch_sent_again_answers_its_job_and_queues_once(client, db, tenant, key):
    a = _batch(client, key, idem="import-1")
    b = _batch(client, key, idem="import-1")
    assert a.status_code == b.status_code == 202, b.text
    assert b.json()["id"] == a.json()["id"] and b.json()["received"] == 3
    assert b.headers[idempotency.REPLAYED] == "true"
    db.expire_all()
    assert db.query(AgentBatchJob).filter(AgentBatchJob.tenant_id == tenant.id).count() == 1


# ── the same key, another request: 422 ─────────────────────────────────────

def test_the_same_key_with_another_request_is_a_422_and_writes_nothing(client, db, tenant, key):
    assert _post(client, key, idem="k").status_code == 201
    r = _post(client, key, idem="k", body={**ADA, "content": "Ada prefers coffee."})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "idempotency_key_reused"
    assert len(_memories(db, tenant.id)) == 1


def test_a_key_of_a_memory_sent_to_the_batch_is_another_request(client, db, tenant, key):
    assert _post(client, key, idem="k").status_code == 201
    r = _batch(client, key, idem="k", rows=[ADA])
    assert r.status_code == 422 and r.json()["code"] == "idempotency_key_reused"
    db.expire_all()
    assert db.query(AgentBatchJob).count() == 0


def test_a_refusal_of_the_key_is_in_the_trail_with_its_code(client, db, tenant, key):
    assert _post(client, key, idem="k").status_code == 201
    assert _post(client, key, idem="k", body={**ADA, "content": "other"}).status_code == 422
    assert _post(client, key, idem="two words").status_code == 400
    assert _batch(client, key, idem="k").status_code == 422
    db.expire_all()
    refusals = sorted(((r.action, r.result, (r.meta or {}).get("code"))
                       for r in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id)
                       if r.result == "error"), key=str)
    assert refusals == sorted([("write", "error", "idempotency_key_reused"),
                               ("write", "error", "idempotency_key_invalid"),
                               ("write", "error", "idempotency_key_reused")], key=str)


def test_a_confined_key_sending_another_keys_request_names_another_write(client, db, tenant,
                                                                        key):
    """The end user is placed for the key before the fingerprint: a key
    confined to Bea that sends the request of a key that wrote for nobody
    in particular names Bea's write, never a replay of the other's."""
    body = {"content": "The build is green.", "agent_id": "ci"}
    assert _post(client, key, idem="k", body=body).status_code == 201
    bea = _key(db, tenant.id, end_users=["bea"])
    r = _post(client, bea, idem="k", body=body)
    assert r.status_code == 422 and r.json()["code"] == "idempotency_key_reused"


# ── a retry while the first is being made: 409 ─────────────────────────────

def test_a_retry_while_the_first_is_being_made_is_a_409(client, db, engine, tenant, key):
    project = key_service.key_project(db, key[0])
    first = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        held = idempotency.hold(first, tenant.id, project, "k", "POST /v1/memories",
                                {"stands": "for the first request, still open"})
        assert held is not None and not held.again
        r = _post(client, key, idem="k")
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "idempotency_key_in_use"
        assert _memories(db, tenant.id) == []
        db.expire_all()
        assert [(e.result, (e.meta or {}).get("code")) for e in db.query(AuditEvent)
                .filter(AuditEvent.tenant_id == tenant.id)] == [("error", "idempotency_key_in_use")]
    finally:
        first.rollback()
        first.close()
    # The first ended without a commit: its key went with it, and the retry
    # is the write.
    assert _post(client, key, idem="k").status_code == 201
    assert len(_memories(db, tenant.id)) == 1


def test_a_key_being_made_holds_only_its_account_and_project(client, db, engine, tenant, key):
    """The lock is the account's and the project's key, as the row is: while
    a first request holds "k", the same key in another account, or in
    another project of the account, is written at once."""
    project = key_service.key_project(db, key[0])
    other = Tenant(id=uuid.uuid4(), name="Other")
    beta = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Beta",
                        slug=f"beta-{uuid.uuid4().hex[:6]}")
    db.add_all([other, beta])
    db.commit()
    first = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        idempotency.hold(first, tenant.id, project, "k", "POST /v1/memories", {"x": 1})
        assert _post(client, _key(db, other.id), idem="k").status_code == 201
        assert _post(client, _key(db, tenant.id, project_id=beta.id), idem="k").status_code == 201
        assert _post(client, key, idem="k").status_code == 409
    finally:
        first.rollback()
        first.close()


# ── a refused or failed write takes its key with it ─────────────────────────

def test_a_write_refused_after_the_key_is_held_keeps_no_key(client, db, tenant, key, monkeypatch):
    from korely_agent.core.extensions import hooks

    def full(*a, **k):
        raise HTTPException(status_code=429, detail={"code": "quota_exceeded",
                                                     "message": "spent"})

    monkeypatch.setattr(hooks, "before_write", full)
    assert _post(client, key, idem="k").status_code == 429
    assert _key_rows(db) == [] and _memories(db, tenant.id) == []
    monkeypatch.undo()
    assert _post(client, key, idem="k").status_code == 201
    assert len(_memories(db, tenant.id)) == 1 and len(_key_rows(db)) == 1


def test_a_key_cannot_be_committed_without_its_write(db, tenant):
    """The foreign key to what the key made is checked at the commit: a
    commit between the hold and the write fails, never a key of nothing."""
    from sqlalchemy.exc import IntegrityError

    idempotency.hold(db, tenant.id, None, "k", "POST /v1/memories", {"x": 1})
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert _key_rows(db) == []


# ── expiry, scope, deletion ────────────────────────────────────────────────

def test_a_key_past_its_24_hours_is_a_new_key(client, db, tenant, key):
    a = _post(client, key, idem="k")
    (row,) = _key_rows(db)
    # The 24 hours the docs publish (docs/guide/api.md), not the constant.
    assert row.expires_at - row.created_at == timedelta(hours=24)
    db.execute(text("UPDATE agent_idempotency_keys SET expires_at = now() - interval '1 second'"))
    db.commit()
    b = _post(client, key, idem="k", body={**ADA, "content": "Ada prefers coffee now."})
    assert b.status_code == 201 and b.json()["id"] != a.json()["id"]
    assert len(_memories(db, tenant.id)) == 2 and len(_key_rows(db)) == 1


def test_the_purge_takes_the_keys_past_their_24_hours(client, db, key):
    _post(client, key, idem="old")
    _post(client, key, idem="new")
    db.execute(text("UPDATE agent_idempotency_keys SET expires_at = now() - interval '1 second' "
                    "WHERE id = (SELECT id FROM agent_idempotency_keys ORDER BY created_at "
                    "LIMIT 1)"))
    db.commit()
    assert idempotency.purge_expired(db) == 1
    assert len(_key_rows(db)) == 1


def test_a_key_is_the_accounts_and_the_projects(client, db, tenant, key):
    a = _post(client, key, idem="k")
    other = Tenant(id=uuid.uuid4(), name="Other")
    db.add(other)
    db.commit()
    elsewhere = _post(client, _key(db, other.id), idem="k")
    beta = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Beta",
                        slug=f"beta-{uuid.uuid4().hex[:6]}")
    db.add(beta)
    db.commit()
    in_beta = _post(client, _key(db, tenant.id, project_id=beta.id), idem="k")
    same_project = _post(client, _key(db, tenant.id), idem="k")
    assert elsewhere.status_code == in_beta.status_code == 201
    assert len({a.json()["id"], elsewhere.json()["id"], in_beta.json()["id"]}) == 3
    # Another key of the same project: the same key, the same write.
    assert same_project.json()["id"] == a.json()["id"]


def test_a_memory_deleted_since_is_not_written_again(client, db, tenant, key):
    a = _post(client, key, idem="k")
    assert client.delete(f"/v1/memories/{a.json()['id']}", headers=_heads(key)).status_code == 200
    r = _post(client, key, idem="k")
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "idempotent_target_gone"
    db.expire_all()
    assert db.query(AgentMemory).filter(AgentMemory.deleted_at.is_(None)).count() == 0


def test_another_confined_keys_job_is_a_404_that_names_no_job(client, db, tenant):
    """Two keys confined to Ada, the same rows and key: the job is the
    first key's to read (GET /v1/batch/{id} is a 404 for the second), so
    the second's retry is a 404 that neither names it nor calls it deleted
    (code review finding, 15 Nov 2026), and nothing is queued again."""
    first, second = _key(db, tenant.id, end_users=["ada"]), _key(db, tenant.id, end_users=["ada"])
    rows = [{"content": "fact 1", "user_id": "ada"}]
    a = _batch(client, first, idem="imp", rows=rows)
    r = _batch(client, second, idem="imp", rows=rows)
    assert r.status_code == 404 and r.json()["code"] == "idempotent_target_gone", r.text
    job = a.json()["id"]
    assert job not in r.text
    assert client.get(f"/v1/batch/{job}", headers=_heads(first)).status_code == 200
    db.expire_all()
    assert db.query(AgentBatchJob).count() == 1


def test_a_memory_erased_takes_its_key_with_it(client, db, tenant, key):
    """An end user's erasure deletes the rows of their memories: the key
    goes with them (ON DELETE CASCADE), so nothing of the write is kept."""
    a = _post(client, key, idem="k")
    db.query(AgentMemory).filter(AgentMemory.id == parse_mem_id(a.json()["id"])).delete()
    db.commit()
    assert _key_rows(db) == []


def test_an_erasure_takes_the_key_of_a_batch_with_their_rows(client, db, tenant, key):
    """The batch's fingerprint is of the rows as sent, Ada's words among
    them: erasing Ada deletes the key with her rows (code review finding,
    15 Nov 2026: the key stayed and the retry answered the scrubbed job).
    Sent again, the batch is queued again, Bea's row with it."""
    rows = [{"content": "Ada's address is 1 Main St.", "user_id": "ada"},
            {"content": "Bea likes jazz.", "user_id": "bea"}]
    a = _batch(client, key, idem="imp", rows=rows)
    assert client.delete("/v1/users/ada/memories", headers=_heads(key)).status_code == 200
    assert _key_rows(db) == []
    b = _batch(client, key, idem="imp", rows=rows)
    assert b.status_code == 202 and idempotency.REPLAYED not in b.headers
    assert b.json()["id"] != a.json()["id"] and b.json()["received"] == 2
    db.expire_all()
    assert db.query(AgentBatchJob).filter(AgentBatchJob.tenant_id == tenant.id).count() == 2


# ── a key that is not one ──────────────────────────────────────────────────

@pytest.mark.parametrize("value", ['"unterminated', '"a"b', '""', "x" * 256, '"\\n"', "two words",
                                   "café"],
                         ids=["unterminated", "after-the-string", "empty", "too-long",
                              "bad-escape", "space", "not-ascii"])
def test_a_header_that_is_not_a_key_is_a_400_and_writes_nothing(client, db, tenant, key, value):
    r = client.post("/v1/memories", json=ADA,
                    headers={**_heads(key), idempotency.HEADER: value.encode("utf-8")})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "idempotency_key_invalid"
    assert _memories(db, tenant.id) == []


def test_the_longest_key_and_an_escaped_quote_are_keys(client, key):
    assert _post(client, key, idem="x" * 255).status_code == 201
    assert idempotency.key_of('"say \\"hi\\""') == 'say "hi"'


# ── the document ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,made", [("/v1/memories", "201"), ("/v1/batch", "202")],
                         ids=["memories", "batch"])
def test_the_document_says_the_replayed_header_and_the_reused_key(client, path, made):
    op = client.get("/openapi.json").json()["paths"][path]["post"]
    answer = op["responses"][made]
    assert idempotency.REPLAYED in answer["headers"] and "content" in answer
    said = op["responses"]["422"]["description"]
    assert "idempotency_key_reused" in said and "invalid_request" in said
    assert "idempotent_target_gone" in op["responses"]["404"]["description"]
