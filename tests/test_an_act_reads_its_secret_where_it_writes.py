"""An act decided by the secret that let it in reads that secret again where
it writes, under the key's row (services/keys.held, core/auth.still_admin).

Up to 1.0.39 the secret was read at the door alone: an admin act let in with
a key's current secret, and still running when its owner rotated the key,
finished with `admin` (the review of 15 Nov 2026), and an erasure let in the
same way revoked the key confined to its end user, the owner's new secret
with it. The tests of the adoption's race were in sequence, one connection
(the review of 6 Nov 2026): the lock order is shown here with two real
connections and a lock_timeout."""
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from korely_agent.core import auth, extensions
from korely_agent.db.models import AgentApiKey, Tenant
from korely_agent.services import keys as key_service

ADMIN = list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE]


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
def other(db):
    """A second connection to the same database, as another request has."""
    made = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    opened = []

    def open_one():
        s = made()
        opened.append(s)
        return s

    yield open_one
    for s in opened:
        s.rollback()
        s.close()


def _rotate_in(session, row_id) -> str:
    """The owner's rotation, in its own transaction, committed."""
    row = session.query(AgentApiKey).filter(AgentApiKey.id == row_id).with_for_update().one()
    new = key_service.reissue_secret(session, row, expires_at=None, old_lasts=timedelta(hours=24))
    return new


def _waits(session, statement, params) -> bool:
    """Whether `statement` waits on a lock another connection holds: it
    times out after 300 ms instead of running."""
    session.execute(text("SET LOCAL lock_timeout = '300ms'"))
    try:
        session.execute(text(statement), params)
        return False
    except OperationalError:
        return True
    finally:
        session.rollback()


class TestTheLockOrder:
    def test_a_rotation_and_an_adoption_wait_for_an_act_that_holds_the_key(self, db, tenant,
                                                                           other):
        row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k", scopes=ADMIN,
                                              expires_in_days=None)
        auth.still_admin(db, row, raw)  # held to this transaction's end
        y = other()
        assert _waits(y, "SELECT id FROM agent_api_keys WHERE id = :k FOR UPDATE", {"k": row.id}), \
            "a rotation reads the key FOR UPDATE: it must wait for the act"
        assert _waits(y, "SELECT id FROM tenants WHERE id = :t FOR UPDATE", {"t": tenant.id}), \
            "an adoption holds the account FOR UPDATE: it must wait for the act"
        # A revocation, an UPDATE of no key column, does not wait.
        assert not _waits(y, "UPDATE agent_api_keys SET revoked_at = now() WHERE id = :k",
                          {"k": row.id})
        db.commit()
        assert not _waits(y, "SELECT id FROM agent_api_keys WHERE id = :k FOR UPDATE",
                          {"k": row.id}), "once the act commits, the rotation goes on"

    def test_a_rotation_that_committed_first_is_seen(self, db, tenant, other):
        row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k", scopes=ADMIN,
                                              expires_in_days=None)
        y = other()
        new = _rotate_in(y, row.id)
        with pytest.raises(HTTPException) as refused:
            auth.still_admin(db, row, raw)
        assert refused.value.status_code == 403
        assert refused.value.detail["code"] == key_service.PREVIOUS_SECRET_NO_ADMIN
        auth.still_admin(db, row, new)  # the owner's secret goes on
        db.rollback()


class TestAnAdminActLetInBeforeItsOwnersRotation:
    def test_it_makes_no_key(self, db, tenant, client, other, monkeypatch):
        row, first = key_service.create_api_key(db, tenant_id=tenant.id, name="leaked",
                                                scopes=ADMIN, expires_in_days=None)
        real = extensions.hooks.before_new_key
        rotated = {}

        def the_owner_rotates_meanwhile(s, tenant_id):
            if not rotated:
                rotated["key"] = _rotate_in(other(), row.id)
            return real(s, tenant_id)

        monkeypatch.setattr(extensions.hooks, "before_new_key", the_owner_rotates_meanwhile)
        r = client.post("/admin/keys", json={"name": "made by the thief"},
                        headers={"Authorization": f"Bearer {first}"})
        assert r.status_code == 403, r.text
        assert r.json()["code"] == key_service.PREVIOUS_SECRET_NO_ADMIN, r.text
        db.expire_all()
        names = {k.name for k in db.query(AgentApiKey).filter(AgentApiKey.tenant_id == tenant.id)}
        assert "made by the thief" not in names


class TestARotationLetInBeforeItsOwnersRotation:
    def test_key_a_rotating_key_b_gets_no_secret_once_a_is_rotated(self, db, tenant, client,
                                                                   other, monkeypatch):
        """The rotation's answer carries the new secret of the key it
        rotates: a request let in with A's secret just before A's owner
        rotated A received B's new secret (the review of 26 Nov 2026)."""
        a, first = key_service.create_api_key(db, tenant_id=tenant.id, name="leaked",
                                              scopes=ADMIN, expires_in_days=None)
        b, b_secret = key_service.create_api_key(db, tenant_id=tenant.id, name="b",
                                                 expires_in_days=None)
        real = extensions.hooks.before_new_key
        rotated = {}

        def the_owner_rotates_a_meanwhile(s, tenant_id):
            if not rotated:
                rotated["key"] = _rotate_in(other(), a.id)
            return real(s, tenant_id)

        monkeypatch.setattr(extensions.hooks, "before_new_key", the_owner_rotates_a_meanwhile)
        r = client.post(f"/admin/keys/{b.id}/rotate", headers={"Authorization": f"Bearer {first}"})
        assert r.status_code == 403, r.text
        assert r.json()["code"] == key_service.PREVIOUS_SECRET_NO_ADMIN and "key" not in r.json()
        db.expire_all()
        assert key_service.verify_api_key(db, b_secret) is not None, "B was not rotated"
        assert db.get(AgentApiKey, b.id).previous_key_hash is None

    def test_a_key_rotating_another_with_its_current_secret_still_does(self, db, tenant,
                                                                         client):
        a, first = key_service.create_api_key(db, tenant_id=tenant.id, name="admin",
                                              scopes=ADMIN, expires_in_days=None)
        b, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="b",
                                          expires_in_days=None)
        r = client.post(f"/admin/keys/{b.id}/rotate", headers={"Authorization": f"Bearer {first}"})
        assert r.status_code == 200 and r.json()["key"], r.text


class TestAWebhookLetInBeforeItsOwnersRotation:
    def test_it_makes_no_webhook(self, db, tenant, client, other, monkeypatch):
        from korely_agent.db.models import AgentWebhookEndpoint
        from korely_agent.services import webhooks

        row, first = key_service.create_api_key(db, tenant_id=tenant.id, name="leaked",
                                                scopes=ADMIN, expires_in_days=None)
        rotated = {}

        def looked_up_while_the_owner_rotates(url):
            # The address taken as it is (no resolver here, as
            # tests/test_admin_actions_are_audited.py does), and the owner's
            # rotation committed while it was looked up.
            if not rotated:
                rotated["key"] = _rotate_in(other(), row.id)
            return url.strip()

        monkeypatch.setattr(webhooks, "check_url", looked_up_while_the_owner_rotates)
        r = client.post("/admin/webhooks", json={"url": "https://example.com/hook"},
                        headers={"Authorization": f"Bearer {first}"})
        assert rotated, "the address was looked up"
        assert r.status_code == 403, r.text
        assert r.json()["code"] == key_service.PREVIOUS_SECRET_NO_ADMIN, r.text
        db.expire_all()
        assert db.query(AgentWebhookEndpoint).filter(
            AgentWebhookEndpoint.tenant_id == tenant.id).count() == 0


class TestAnErasureLetInBeforeItsOwnersRotation:
    def test_it_does_not_revoke_the_key_the_owner_just_rotated(self, db, tenant, client, other,
                                                               monkeypatch):
        from korely_agent.services import end_user_places

        row, first = key_service.create_api_key(db, tenant_id=tenant.id, name="alices",
                                                end_users=["alice"], expires_in_days=None)
        real = end_user_places.keys_an_erasure_ends
        rotated = {}

        def the_owner_rotates_meanwhile(*a, **k):
            found = real(*a, **k)
            if not rotated:
                rotated["key"] = _rotate_in(other(), row.id)
            return found

        monkeypatch.setattr(end_user_places, "keys_an_erasure_ends", the_owner_rotates_meanwhile)
        r = client.delete("/v1/users/alice/memories", headers={"Authorization": f"Bearer {first}"})
        assert r.status_code == 403, r.text
        assert r.json()["code"] == key_service.PREVIOUS_SECRET_CODE, r.text
        db.expire_all()
        assert db.get(AgentApiKey, row.id).revoked_at is None
        assert key_service.verify_api_key(db, rotated["key"]) is not None

    def test_nor_after_two_rotations_in_a_row(self, db, tenant, client, other, monkeypatch):
        """The secret that let the erasure in is neither the one in force nor
        the one the last rotation replaced: refused all the same, on "not the
        current one" (the review of 26 Nov 2026)."""
        from korely_agent.services import end_user_places

        row, first = key_service.create_api_key(db, tenant_id=tenant.id, name="alices",
                                                end_users=["alice"], expires_in_days=None)
        real = end_user_places.keys_an_erasure_ends
        rotated = []

        def the_owner_rotates_twice_meanwhile(*a, **k):
            found = real(*a, **k)
            if not rotated:
                rotated.append(_rotate_in(other(), row.id))
                rotated.append(_rotate_in(other(), row.id))
            return found

        monkeypatch.setattr(end_user_places, "keys_an_erasure_ends",
                            the_owner_rotates_twice_meanwhile)
        r = client.delete("/v1/users/alice/memories", headers={"Authorization": f"Bearer {first}"})
        assert r.status_code == 403, r.text
        db.expire_all()
        assert db.get(AgentApiKey, row.id).revoked_at is None
        assert key_service.verify_api_key(db, rotated[-1]) is not None

    def test_an_erasure_with_no_rotation_still_revokes_the_key(self, db, tenant, client):
        row, first = key_service.create_api_key(db, tenant_id=tenant.id, name="alices",
                                                end_users=["alice"], expires_in_days=None)
        r = client.delete("/v1/users/alice/memories", headers={"Authorization": f"Bearer {first}"})
        assert r.status_code == 200, r.text
        db.expire_all()
        assert db.get(AgentApiKey, row.id).revoked_at is not None


def test_the_old_secret_s_refusal_says_what_it_refuses():
    """Its words name the two acts it is answered to (an erasure that would
    revoke a key, an adoption): up to 1.0.39 they said the old secret "does
    nothing that would end or change a key", while an erasure with it of one
    of the end users of a key confined to several rewrites that key's list
    (the review of 15 Nov 2026)."""
    said = key_service.PREVIOUS_SECRET_REFUSED
    assert "revoke a key" in said and "account" in said, said
    assert "change a key" not in said, said


def test_tenants_and_keys_tables_are_the_ones_locked():
    """The statements above name the tables the models use."""
    assert Tenant.__tablename__ == "tenants" and AgentApiKey.__tablename__ == "agent_api_keys"
