"""A database that does not answer while a key is checked is a 503, never the
401 of an unknown key (audit of 17 Oct 2026).

Over MCP every exception of the key lookup was None, and the SDK answered
401 `invalid_token`: during a Postgres blip ChatGPT and Claude threw their
OAuth token away and asked for a new authorization, and the holder of a
key read it as revoked. REST answered a bare 500. Both doors now answer the
same 503 `unavailable` with Retry-After, and the failure is not remembered
as an unknown key: the key works again the moment the store answers.

The outage is the query, never the lookup function: `store.down()` makes the
database refuse every statement that reads the key table, and the real
services/keys.verify_api_key runs into it. Until 18 Oct 2026 these tests
replaced verify_api_key itself with a function that raised, so the cache of
unknown hashes inside it was never reached: a verify_api_key that turned a
database error into "unknown" and remembered the hash for an hour passed all
four (audit of 18 Oct 2026).
"""
import asyncio
import secrets
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from korely_agent.db.models import AgentApiKey, Tenant
from korely_agent.services import keys as key_service



def _reads_the_keys(statement) -> bool:
    froms = getattr(statement, "get_final_froms", None)
    return froms is not None and AgentApiKey.__table__ in froms()


class _Store:
    """`down()`: the database stops answering every read of the key table, in
    every session (the request's and the one the MCP verifier opens);
    `back()`: it answers again."""

    def __init__(self, monkeypatch):
        self._mp = monkeypatch
        self._real = Session.execute

    def down(self):
        real = self._real

        def execute(session, statement, *a, **k):
            if _reads_the_keys(statement):
                raise OperationalError(str(statement), {},
                                       ConnectionRefusedError("server closed the connection"))
            return real(session, statement, *a, **k)

        self._mp.setattr(Session, "execute", execute)

    def back(self):
        self._mp.setattr(Session, "execute", self._real)


@pytest.fixture
def store(monkeypatch):
    return _Store(monkeypatch)


@pytest.fixture
def client(db, monkeypatch):
    from korely_agent.config import get_settings
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _key(db):
    t = Tenant(id=uuid.uuid4(), name=f"t-{uuid.uuid4().hex[:6]}")
    db.add(t)
    db.commit()
    _r, full = key_service.create_api_key(db, tenant_id=t.id, name="k",
                                          scopes=["memories:read"])
    return full


def _initialize(c, key, **extra):
    return c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "t", "version": "1"}}},
                  headers={"Authorization": f"Bearer {key}",
                           "Accept": "application/json, text/event-stream", **extra})


def _is_the_503(r):
    assert r.status_code == 503, (r.status_code, r.text[:200])
    assert r.json()["code"] == "unavailable", r.text[:200]
    assert int(r.headers["retry-after"]) > 0
    assert "www-authenticate" not in r.headers, "a 503 must not ask for another credential"


def test_the_verifier_raises_instead_of_answering_unknown(store):
    from korely_agent.mcp.server import KorelyAgentKeyVerifier

    # A key of the right shape never seen before: its hash is in no cache, so
    # the lookup reaches the query.
    token = "kor_self_" + secrets.token_urlsafe(32)
    store.down()
    with pytest.raises(HTTPException) as e:
        asyncio.run(KorelyAgentKeyVerifier().verify_token(token))
    assert e.value.status_code == 503


def test_rest_answers_503(client, db, store):
    key = _key(db)
    store.down()
    _is_the_503(client.get("/v1/ping", headers={"Authorization": f"Bearer {key}"}))


def test_mcp_answers_503_on_an_opening_and_inside_a_session(client, db, store):
    key = _key(db)
    store.down()
    # An opening request, read by the session limiter in front of the SDK.
    _is_the_503(_initialize(client, key))
    # A request of a session, read by the SDK's own verifier.
    _is_the_503(_initialize(client, key, **{"mcp-session-id": "0" * 32}))


def test_the_failure_is_not_remembered_as_an_unknown_key(client, db, store):
    key = _key(db)
    store.down()
    _is_the_503(_initialize(client, key))
    _is_the_503(client.get("/v1/ping", headers={"Authorization": f"Bearer {key}"}))
    store.back()
    assert _initialize(client, key).status_code == 200
    assert client.get("/v1/ping", headers={"Authorization": f"Bearer {key}"}).status_code == 200
