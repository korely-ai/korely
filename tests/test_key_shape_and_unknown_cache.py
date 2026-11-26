"""A key that cannot exist, and a key already found missing, never reach the
database (services/keys.py, `_KEY_SHAPE` and `_unknown`).

The hosted product's logs of 2026-10-05: one address, 5,224 requests in nine
minutes, an analyzer calling every method of the SDK with made-up values
(`/v1/facts/[object Object]`). Every 401 was a lookup in Postgres and the pool
stayed saturated. The hosted product fixed it in its commit e21edc8; this is
the same rule here, so the two products refuse a wrong key the same way.

What must not change while doing it: an unknown and a revoked key still answer
alike, a revocation still takes effect on the next request, and every key this
product has ever minted (a prefix and secrets.token_urlsafe(32), 43 URL-safe
characters) still gets in.
"""
import secrets
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from korely_agent.db.models import Tenant
from korely_agent.services import keys as key_service



@pytest.fixture(autouse=True)
def _empty_cache():
    key_service._unknown.clear()
    yield
    key_service._unknown.clear()


@pytest.fixture
def lookups():
    """Every statement that reads the key table, from any engine (MCP opens its
    own session)."""
    seen = []

    def count(conn, cursor, statement, params, context, executemany):
        if "agent_api_keys" in statement and statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    event.listen(Engine, "before_cursor_execute", count)
    yield seen
    event.remove(Engine, "before_cursor_execute", count)


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


def new_tenant(db):
    t = Tenant(id=uuid.uuid4(), name="T")
    db.add(t)
    db.commit()
    return t


def made_up(prefix=key_service.PREFIX_SELF):
    return prefix + secrets.token_urlsafe(32)


class TestTheShape:
    def test_every_key_this_product_mints_has_it(self, db):
        t = new_tenant(db)
        for prefix in (key_service.PREFIX_SELF, key_service.PREFIX_LIVE):
            for _ in range(20):
                _, full = key_service.create_api_key(db, tenant_id=t.id, prefix=prefix,
                                                     name=f"{prefix}{uuid.uuid4()}")
                assert key_service._KEY_SHAPE.fullmatch(full), full
                # 0.2.0: a `kor_live_` key is the Cloud's alone, refused here.
                assert (key_service.verify_api_key(db, full) is not None) == (prefix == key_service.PREFIX_SELF)

    @pytest.mark.parametrize("presented", [
        "",
        "kor_live_x",
        "kor_self_" + "z" * 40,                       # short
        "kor_self_" + "z" * 44,                       # long
        "kor_self_" + "z" * 42 + "!",                 # not base64url
        "kor_self_" + "z" * 43 + "\n",                # a trailing newline
        " kor_self_" + "z" * 43,
        "Bearer kor_self_" + "z" * 43,
        "kor_prod_" + "z" * 43,                       # no such prefix
        "KOR_SELF_" + "z" * 43,
        "[object Object]",
        "undefined",
        "kor_self_" + "z" * 4_000,
    ])
    def test_a_string_without_it_is_refused_without_a_lookup(self, db, lookups, presented):
        assert key_service.verify_api_key(db, presented) is None
        assert lookups == []


class TestTheUnknownAreRemembered:
    def test_the_same_wrong_key_is_looked_up_once(self, db, lookups):
        wrong = made_up()
        for _ in range(50):
            assert key_service.verify_api_key(db, wrong) is None
        assert len(lookups) == 1

    def test_a_different_wrong_key_is_looked_up_again(self, db, lookups):
        key_service.verify_api_key(db, made_up())
        key_service.verify_api_key(db, made_up())
        assert len(lookups) == 2

    def test_a_live_key_is_looked_up_every_time(self, db, lookups):
        t = new_tenant(db)
        _, full = key_service.create_api_key(db, tenant_id=t.id)
        lookups.clear()
        for _ in range(3):
            assert key_service.verify_api_key(db, full) is not None
        assert len(lookups) == 3
        assert key_service._unknown == {}

    def test_a_revoked_key_is_not_remembered_and_its_revocation_is_at_once(self, db, lookups):
        t = new_tenant(db)
        row, full = key_service.create_api_key(db, tenant_id=t.id)
        assert key_service.verify_api_key(db, full) is not None
        key_service.revoke_api_key(db, key_id=row.id, tenant_id=t.id)
        lookups.clear()
        assert key_service.verify_api_key(db, full) is None
        assert key_service.verify_api_key(db, full) is None
        assert len(lookups) == 2, "a revoked key must always be read"
        assert key_service._unknown == {}

    def test_an_expired_key_is_not_remembered(self, db):
        t = new_tenant(db)
        _, full = key_service.create_api_key(db, tenant_id=t.id, expires_in_days=0)
        assert key_service.verify_api_key(db, full) is None
        assert key_service._unknown == {}

    def test_an_hour_later_it_is_looked_up_again(self, db, lookups, monkeypatch):
        now = [1_000.0]
        monkeypatch.setattr(key_service.time, "monotonic", lambda: now[0])
        wrong = made_up()
        key_service.verify_api_key(db, wrong)
        now[0] += key_service._UNKNOWN_TTL_S - 1
        key_service.verify_api_key(db, wrong)
        assert len(lookups) == 1
        now[0] += 2
        key_service.verify_api_key(db, wrong)
        assert len(lookups) == 2

    def test_the_oldest_go_first_when_full(self, db, lookups, monkeypatch):
        monkeypatch.setattr(key_service, "_UNKNOWN_MAX", 3)
        wrong = [made_up() for _ in range(4)]
        for k in wrong:
            key_service.verify_api_key(db, k)
        assert len(key_service._unknown) == 3
        lookups.clear()
        key_service.verify_api_key(db, wrong[-1])
        assert lookups == []
        key_service.verify_api_key(db, wrong[0])
        assert len(lookups) == 1, "the oldest should have been let go"

    def test_a_key_minted_after_it_was_tried_gets_in(self, db, monkeypatch):
        """A row exists before anybody holds its key, so this cannot happen
        by guessing. It can with a key restored from a backup into an install
        that had already refused it: minting takes the hash out of the cache."""
        secret = secrets.token_urlsafe(32)
        monkeypatch.setattr(key_service.secrets, "token_urlsafe", lambda n: secret)
        full = key_service.PREFIX_SELF + secret
        assert key_service.verify_api_key(db, full) is None
        assert key_service._unknown

        t = new_tenant(db)
        _, minted = key_service.create_api_key(db, tenant_id=t.id)
        assert minted == full
        assert key_service.verify_api_key(db, full) is not None


class TestAtTheDoors:
    def test_a_burst_of_one_wrong_key_reads_the_table_once(self, client, lookups):
        h = {"Authorization": "Bearer " + made_up()}
        answers = [client.get("/v1/memories", headers=h) for _ in range(30)]
        assert {r.status_code for r in answers} == {401}
        assert len(lookups) == 1

    def test_malformed_unknown_and_revoked_still_answer_alike(self, client, db):
        t = new_tenant(db)
        row, revoked = key_service.create_api_key(db, tenant_id=t.id, name="gone")
        key_service.revoke_api_key(db, key_id=row.id, tenant_id=t.id)
        unknown = made_up()
        answers = [client.get("/v1/memories", headers={"Authorization": "Bearer " + k})
                   for k in ("kor_self_short", unknown, unknown, revoked)]
        assert {r.status_code for r in answers} == {401}
        # The malformed one is told the shape of a key (2026-10-07), read off
        # the string; an unknown key and a revoked one stay one answer.
        assert "43 characters" in answers[0].json()["detail"]
        assert len({r.text for r in answers[1:]}) == 1, [r.text for r in answers[1:]]

    def test_mcp_refuses_a_malformed_key_without_a_lookup(self, lookups):
        import asyncio

        from korely_agent.mcp import server as mcp_server

        verifier = mcp_server.KorelyAgentKeyVerifier()
        assert asyncio.run(verifier.verify_token("[object Object]")) is None
        assert lookups == []
