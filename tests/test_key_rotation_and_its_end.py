"""A key rotates in one call, says when it ends on every answer, and an
install may cap how long a key made now lasts.

The founder's decision of 1-2 Nov 2026 on the keys' end and rotation, in
three parts: (a) rotation in one call, the old secret
working a while longer (as Stripe's "Rotate key", https://docs.stripe.com/keys,
read 14 Nov 2026), the new one shown once; (b) the key's end said to its
holder without a webhook; (c) a ceiling on a new key's lifetime where the
install has one (a hook of core/extensions).
Up to 1.0.22 a key was rotated by minting another and revoking the first,
which left the agents that held it nothing to switch over with, and its end
reached only a webhook.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.core import extensions
from korely_agent.core.times import aware
from korely_agent.db.models import AgentApiKey
from korely_agent.services import keys as key_service

NOW = lambda: datetime.now(timezone.utc)
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
def other_tenant(db):
    import uuid

    from korely_agent.db.models import Tenant

    t = Tenant(id=uuid.uuid4(), name="Someone else")
    db.add(t)
    db.commit()
    return t


@pytest.fixture
def admin(db, tenant):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="operator",
                                          scopes=ADMIN, expires_in_days=None)
    return {"Authorization": f"Bearer {full}"}


def _key(db, tenant, days=30, name="agent"):
    return key_service.create_api_key(db, tenant_id=tenant.id, name=name, expires_in_days=days)


def _works(db, raw) -> bool:
    db.expire_all()
    return key_service.verify_api_key(db, raw) is not None


def _rotate(client, admin, row, **body):
    return client.post(f"/admin/keys/{row.id}/rotate", json=body or None, headers=admin)


class TestOneCallRotates:
    def test_the_same_key_a_new_secret_and_the_old_one_still_works(self, db, tenant, client, admin):
        row, old = _key(db, tenant)
        r = _rotate(client, admin, row)
        assert r.status_code == 200, r.text
        out = r.json()
        new = out["key"]
        assert new != old and out["id"] == str(row.id) and out["name"] == "agent"
        assert _works(db, new) and _works(db, old), "the overlap is the point of a rotation"
        ends = datetime.fromisoformat(out["old_secret_expires_at"])
        assert timedelta(hours=23) < ends - NOW() <= timedelta(hours=24)
        listed = {k["id"]: k for k in client.get("/admin/keys", headers=admin).json()}
        assert listed[str(row.id)]["old_secret_expires_at"] == out["old_secret_expires_at"]

    def test_the_old_secret_stops_at_its_moment(self, db, tenant, client, admin):
        row, old = _key(db, tenant)
        new = _rotate(client, admin, row).json()["key"]
        db.expire_all()
        stored = db.get(AgentApiKey, row.id)
        stored.previous_expires_at = NOW() - timedelta(seconds=1)
        db.commit()
        assert not _works(db, old) and _works(db, new)

    def test_no_overlap_when_asked(self, db, tenant, client, admin):
        row, old = _key(db, tenant)
        r = _rotate(client, admin, row, old_key_expires_in_hours=0)
        assert r.json()["old_secret_expires_at"] is None
        assert not _works(db, old) and _works(db, r.json()["key"])

    def test_revoking_ends_both(self, db, tenant, client, admin):
        row, old = _key(db, tenant)
        new = _rotate(client, admin, row).json()["key"]
        assert client.post(f"/admin/keys/{row.id}/revoke", headers=admin).status_code == 200
        assert not _works(db, old) and not _works(db, new)

    def test_one_old_secret_at_a_time(self, db, tenant, client, admin):
        row, first = _key(db, tenant)
        second = _rotate(client, admin, row).json()["key"]
        third = _rotate(client, admin, row).json()["key"]
        assert not _works(db, first) and _works(db, second) and _works(db, third)

    def test_the_old_secret_never_outlives_the_key(self, db, tenant, client, admin):
        row, old = _key(db, tenant)
        row.expires_at = NOW() + timedelta(hours=2)
        db.commit()
        out = _rotate(client, admin, row, old_key_expires_in_hours=48).json()
        assert datetime.fromisoformat(out["old_secret_expires_at"]) <= aware(row.expires_at)

    def test_the_lifetime_is_kept_unless_told(self, db, tenant, client, admin):
        row, _ = _key(db, tenant, days=30)
        kept = datetime.fromisoformat(_rotate(client, admin, row).json()["expires_at"])
        assert abs(kept - (NOW() + timedelta(days=30))) < timedelta(minutes=5)
        told = _rotate(client, admin, row, expires_in_days=60).json()["expires_at"]
        assert abs(datetime.fromisoformat(told) - (NOW() + timedelta(days=60))) < timedelta(minutes=5)
        never, _ = _key(db, tenant, days=None, name="forever")
        assert _rotate(client, admin, never).json()["expires_at"] is None

    def test_what_does_not_rotate(self, db, tenant, client, admin, other_tenant):
        gone, _ = _key(db, tenant, name="gone")
        key_service.revoke_api_key(db, key_id=gone.id, tenant_id=tenant.id)
        r = _rotate(client, admin, gone)
        assert r.status_code == 409 and r.json()["code"] == "key_not_rotatable"
        theirs, _ = _key(db, other_tenant, name="theirs")
        assert _rotate(client, admin, theirs).status_code == 404
        mine, _ = _key(db, tenant, name="mine")
        assert _rotate(client, admin, mine,
                       old_key_expires_in_hours=key_service.MAX_OLD_SECRET_HOURS + 1).status_code == 422


class TestTheAnswerSaysWhenTheKeyEnds:
    HEADER = key_service.KEY_EXPIRES_HEADER

    def test_a_key_that_ends_says_so_on_every_answer(self, db, tenant, client):
        row, raw = _key(db, tenant)
        r = client.get("/v1/ping", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200
        assert datetime.fromisoformat(r.headers[self.HEADER]) == \
            aware(row.expires_at).replace(microsecond=0)

    def test_a_key_that_never_ends_says_nothing(self, db, tenant, client):
        _row, raw = _key(db, tenant, days=None)
        assert self.HEADER not in client.get(
            "/v1/ping", headers={"Authorization": f"Bearer {raw}"}).headers

    def test_the_old_secret_says_its_own_end(self, db, tenant, client, admin):
        row, old = _key(db, tenant)
        out = _rotate(client, admin, row, old_key_expires_in_hours=5).json()
        said = client.get("/v1/ping", headers={"Authorization": f"Bearer {old}"}).headers
        assert datetime.fromisoformat(said[self.HEADER]) == \
            datetime.fromisoformat(out["old_secret_expires_at"]).replace(microsecond=0)

    def test_the_mcp_door_says_it_too(self, db, tenant, client):
        _row, raw = _key(db, tenant)
        r = client.post("/mcp", headers={"Authorization": f"Bearer {raw}",
                                         "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 200, r.text
        assert self.HEADER in r.headers


class TestAnInstallMayCapALifetime:
    """The hook an install may fill (core/extensions `longest_key_lifetime`);
    an install of one's own has none and keeps `never`."""

    @pytest.fixture
    def cap(self, client, admin, monkeypatch):
        """Puts the ceiling in place when called: the keys a test makes
        before are keys made before the ceiling (the admin key is one)."""
        def put():
            monkeypatch.setattr(extensions.hooks, "longest_key_lifetime", lambda: 365,
                                raising=False)
            return client
        return put

    def test_longer_or_never_is_refused_and_nothing_is_made(self, db, cap, admin):
        capped = cap()
        before = db.query(AgentApiKey).count()
        for days in (None, 366, key_service.MAX_LIFETIME_DAYS):
            r = capped.post("/admin/keys", json={"name": f"k{days}", "expires_in_days": days},
                            headers=admin)
            assert r.status_code == 422 and "365 days" in r.json()["message"], (days, r.text)
        assert db.query(AgentApiKey).count() == before
        assert capped.post("/admin/keys", json={"name": "year", "expires_in_days": 365},
                           headers=admin).status_code == 201

    def test_the_screen_is_offered_what_may_be_made(self, cap, admin):
        out = cap().get("/admin/keys/lifetimes", headers=admin).json()
        assert out["max_days"] == 365
        assert all(c["days"] is not None and c["days"] <= 365 for c in out["choices"])

    def test_a_rotation_of_a_key_that_never_ends_gets_the_longest(self, db, tenant, cap, admin):
        row, _ = _key(db, tenant, days=None, name="old-forever")
        capped = cap()
        out = _rotate(capped, admin, row).json()
        assert abs(datetime.fromisoformat(out["expires_at"]) - (NOW() + timedelta(days=365))) \
            < timedelta(minutes=5)
        assert _rotate(capped, admin, row, expires_in_days=None).status_code == 422

    def test_a_key_made_before_keeps_working(self, db, tenant, cap, admin):
        _row, raw = _key(db, tenant, days=None, name="from-before")
        assert cap().get("/v1/ping", headers={"Authorization": f"Bearer {raw}"}).status_code == 200

    def test_without_the_hook_never_is_still_a_choice(self, client, admin):
        assert client.post("/admin/keys", json={"name": "n", "expires_in_days": None},
                           headers=admin).status_code == 201


class TestTheSecondReviewOfRotation:
    """The review of 14 Nov 2026 on the first version of the rotation."""

    def test_the_lifetime_does_not_grow_at_each_rotation(self, db, tenant, client, admin):
        """It was counted from the key's creation: a 30-day key rotated
        after 10 days got 40, then more at every rotation."""
        row, _ = _key(db, tenant, days=30)
        _rotate(client, admin, row)
        db.expire_all()
        stored = db.get(AgentApiKey, row.id)
        stored.created_at = NOW() - timedelta(days=40)
        stored.rotated_at = NOW() - timedelta(days=10)
        stored.expires_at = NOW() + timedelta(days=20)
        db.commit()
        ends = datetime.fromisoformat(_rotate(client, admin, row).json()["expires_at"])
        assert abs(ends - (NOW() + timedelta(days=30))) < timedelta(minutes=5)

    def test_a_rotation_never_passes_the_installs_most(self, db, tenant, client, admin):
        row, _ = _key(db, tenant, days=30)
        row.created_at = NOW() - timedelta(days=4000)
        row.expires_at = NOW() + timedelta(days=10)
        db.commit()
        ends = datetime.fromisoformat(_rotate(client, admin, row).json()["expires_at"])
        assert ends - NOW() <= timedelta(days=key_service.MAX_LIFETIME_DAYS)

    def _second_session(self, db):
        from sqlalchemy.orm import sessionmaker

        return sessionmaker(bind=db.get_bind(), expire_on_commit=False)()

    def test_a_stale_row_does_not_bring_the_first_secret_back(self, db, tenant):
        """A held the row, B rotated it, then A rotated it: the first secret
        worked again and B's died."""
        row, first = _key(db, tenant)
        held = db.get(AgentApiKey, row.id)                 # A holds it
        other = self._second_session(db)
        try:
            _r, by_b = key_service.rotate_api_key(other, key_id=row.id, tenant_id=tenant.id)
        finally:
            other.close()
        assert held.key_hash == key_service.hash_key(first)    # A's copy is stale
        _r, by_a = key_service.rotate_api_key(db, key_id=row.id, tenant_id=tenant.id)
        assert not _works(db, first)
        assert _works(db, by_b) and _works(db, by_a)

    def test_a_key_revoked_meanwhile_does_not_rotate(self, db, tenant):
        row, _ = _key(db, tenant)
        db.get(AgentApiKey, row.id)
        other = self._second_session(db)
        try:
            assert key_service.revoke_api_key(other, key_id=row.id, tenant_id=tenant.id)
        finally:
            other.close()
        with pytest.raises(key_service.NotRotatable):
            key_service.rotate_api_key(db, key_id=row.id, tenant_id=tenant.id)

    def test_two_rotations_at_once_take_turns(self, db, tenant, monkeypatch):
        """Each reads the row under its lock: without it both took the first
        secret as the old one, which then outlived them both."""
        import threading
        import time as _time

        row, first = _key(db, tenant)
        made = key_service.generate_api_key

        def slow(*a, **k):
            _time.sleep(0.3)
            return made(*a, **k)

        monkeypatch.setattr(key_service, "generate_api_key", slow)
        out = []

        def rotate():
            s = self._second_session(db)
            try:
                out.append(key_service.rotate_api_key(s, key_id=row.id, tenant_id=tenant.id)[1])
            finally:
                s.close()

        both = [threading.Thread(target=rotate) for _ in range(2)]
        for t in both:
            t.start()
        for t in both:
            t.join(10)
        assert len(out) == 2
        assert not _works(db, first)
        assert all(_works(db, secret) for secret in out)

    def test_the_old_secret_says_the_earlier_of_the_two_ends(self, db, tenant, client, admin):
        row, old = _key(db, tenant)
        _rotate(client, admin, row, old_key_expires_in_hours=24)
        db.expire_all()
        stored = db.get(AgentApiKey, row.id)
        stored.expires_at = NOW() + timedelta(hours=1)
        db.commit()
        listed = {k["id"]: k for k in client.get("/admin/keys", headers=admin).json()}
        said = datetime.fromisoformat(listed[str(row.id)]["old_secret_expires_at"])
        assert said <= aware(stored.expires_at)
        header = client.get("/v1/ping", headers={"Authorization": f"Bearer {old}"}).headers
        assert datetime.fromisoformat(header[key_service.KEY_EXPIRES_HEADER]) <= aware(stored.expires_at)

    def test_an_ended_overlap_and_a_revoked_key_report_none(self, db, tenant, client, admin):
        row, _ = _key(db, tenant)
        _rotate(client, admin, row)
        db.expire_all()
        stored = db.get(AgentApiKey, row.id)
        stored.previous_expires_at = NOW() - timedelta(seconds=1)
        db.commit()
        listed = {k["id"]: k for k in client.get("/admin/keys", headers=admin).json()}
        assert listed[str(row.id)]["old_secret_expires_at"] is None
        _rotate(client, admin, row)
        client.post(f"/admin/keys/{row.id}/revoke", headers=admin)
        db.expire_all()
        stored = db.get(AgentApiKey, row.id)
        assert stored.previous_key_hash is None and stored.previous_expires_at is None

    def test_the_header_is_in_utc_whatever_the_zone(self):
        from zoneinfo import ZoneInfo

        ends = datetime(2027, 1, 10, 12, 0, tzinfo=ZoneInfo("Europe/Rome"))
        name, value = key_service.expires_header({"ends": ends})
        assert value == b"2027-01-10T11:00:00+00:00"

    def test_a_connected_apps_key_sends_no_header(self, db, tenant, client):
        from korely_agent.db.models import OAuthClient

        db.add(OAuthClient(client_id="app-1", info={"client_name": "an app"}))
        db.commit()
        row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name=None,
                                              oauth_client_id="app-1",
                                              expires_at=NOW() + timedelta(hours=1))
        r = client.get("/v1/ping", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200 and key_service.KEY_EXPIRES_HEADER not in r.headers

    def test_the_rotate_route_declares_its_refusals(self, client):
        responses = client.app.openapi()["paths"][
            "/admin/keys/{key_id}/rotate"]["post"]["responses"]
        assert {"403", "404", "409", "422"} <= set(responses)
        assert "old_key_expires_in_hours" in responses["422"]["description"]


class TestTheOldSecretHoldsNoAdmin:
    """During the overlap the old secret of an admin key was the key in full,
    `admin` included. Up to 1.0.25 whoever held it (a leaked key's thief)
    could rotate the key again with no overlap, end the secret its owner had
    just taken and receive the newest (the security review of 14 Nov 2026),
    delete a project, which moves its keys, or set the model provider's key,
    which outlives the overlap (the skeptic's review of 15 Nov 2026). Now the
    old secret holds the key's scopes but `admin`, as Stripe's rolled key
    keeps the API's calls (services/keys.scopes_of)."""

    CODE = "previous_secret_has_no_admin"

    def _rotated(self, db, tenant, client, scopes=ADMIN, users=None, by=None):
        row, first = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k{users or ''}",
                                                scopes=scopes, end_users=users,
                                                expires_in_days=None)
        old = {"Authorization": f"Bearer {first}"}
        r = _rotate(client, by or old, row)
        assert r.status_code == 200, r.text
        return row, old, {"Authorization": f"Bearer {r.json()['key']}"}

    def test_the_old_secret_cannot_win_the_rotation(self, db, tenant, client):
        row, thief, owner = self._rotated(db, tenant, client)
        r = _rotate(client, thief, row, old_key_expires_in_hours=0)
        assert r.status_code == 403 and r.json()["code"] == self.CODE, r.text
        assert "key" not in r.json()
        # The owner's secret administers; the old one's other calls go on.
        assert client.get("/admin/keys", headers=owner).status_code == 200
        assert client.get("/v1/ping", headers=thief).status_code == 200
        # The owner ends the overlap with the current secret.
        assert _rotate(client, owner, row, old_key_expires_in_hours=0).status_code == 200
        assert client.get("/v1/ping", headers=thief).status_code == 401

    def test_nor_make_revoke_rotate_or_move_any_key(self, db, tenant, client):
        row, thief, owner = self._rotated(db, tenant, client)
        other, _ = _key(db, tenant, name="other")
        before = {(k.id, k.key_hash, k.revoked_at, k.project_id)
                  for k in db.query(AgentApiKey).all()}
        for path, body in (("/admin/keys", {"name": "theirs", "scopes": ADMIN}),
                           (f"/admin/keys/{other.id}/revoke", None),
                           (f"/admin/keys/{row.id}/revoke", None),
                           (f"/admin/keys/{other.id}/rotate", None),
                           (f"/admin/keys/{other.id}/move", {"project_id": None})):
            r = client.post(path, json=body, headers=thief)
            assert r.status_code == 403 and r.json()["code"] == self.CODE, (path, r.text)
        db.expire_all()
        assert {(k.id, k.key_hash, k.revoked_at, k.project_id)
                for k in db.query(AgentApiKey).all()} == before
        # The current secret does each of them.
        assert client.post("/admin/keys", json={"name": "owners"}, headers=owner).status_code == 201
        assert _rotate(client, owner, other).status_code == 200
        assert client.post(f"/admin/keys/{other.id}/revoke", headers=owner).status_code == 200

    def test_nor_any_other_door_of_admin(self, db, tenant, client, monkeypatch):
        """A project deleted moved its keys; the provider's key and a model
        set outlive the overlap and carry every memory after it; a webhook
        made then would receive the account's events. Each wants `admin`,
        and a webhook made before the rotation stays."""
        import uuid

        from korely_agent.services import webhooks

        row, thief, owner = self._rotated(db, tenant, client)
        r = client.post("/admin/projects", json={"name": "theirs"}, headers=owner)
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        held, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="held",
                                             project_id=uuid.UUID(pid), expires_in_days=None)
        # Made before, as data (the tests reach no DNS: tests/conftest.py).
        monkeypatch.setattr(webhooks, "check_url", lambda u: u.strip())
        hook = webhooks.create_endpoint(db, tenant.id, url="https://hooks.example/korely",
                                        events=None)
        for method, path, body in (("delete", f"/admin/projects/{pid}", None),
                                   ("post", "/admin/projects", {"name": "mine"}),
                                   ("put", "/admin/provider", {}),
                                   ("put", "/admin/models/gemini", {}),
                                   ("post", "/admin/webhooks", {"url": "https://x.example/h"}),
                                   ("get", "/admin/webhooks", None)):
            r = client.request(method, path, json=body, headers=thief)
            assert r.status_code == 403 and r.json()["code"] == self.CODE, (path, r.text)
        db.expire_all()
        assert str(db.get(AgentApiKey, held.id).project_id) == pid
        # The webhook made before stays, and the owner reads it.
        listed = client.get("/admin/webhooks", headers=owner).json()
        assert [w["url"] for w in listed] == [hook.url]

    def test_its_scopes_say_so_on_ping_and_at_the_mcp(self, db, tenant, client):
        from korely_agent.mcp import server

        row, thief, owner = self._rotated(db, tenant, client)
        assert "admin" in client.get("/v1/ping", headers=owner).json()["scopes"]
        assert client.get("/v1/ping", headers=thief).json()["scopes"] == list(key_service.DEFAULT_SCOPES)
        old = thief["Authorization"].split(" ", 1)[1]
        new = owner["Authorization"].split(" ", 1)[1]
        assert "admin" not in server._the_key(db, old)[2]
        assert "admin" in server._the_key(db, new)[2]

    def test_an_erasure_that_would_revoke_a_key_wants_the_current_secret(self, db, tenant, client,
                                                                         admin):
        """Erasing the one end user a key is confined to revokes the key
        (end_user_places.rename_in_lists): with the old secret of a key
        confined to alice, the erasure of alice revoked it, and the owner's
        new secret was a 401 (the skeptic's review of 15 Nov 2026)."""
        code = "previous_secret_cannot_manage_keys"
        confined, thief, owner = self._rotated(db, tenant, client, scopes=None,
                                               users=["alice"], by=admin)
        r = client.delete("/v1/users/alice/memories", headers=thief)
        assert r.status_code == 403 and r.json()["code"] == code, r.text
        assert client.get("/v1/ping", headers=owner).status_code == 200
        # A key of the whole project, rotated: its old secret erases whom no
        # key is confined to alone, and not carol, the one end user of a key.
        _whole, old, new = self._rotated(db, tenant, client, scopes=None, by=admin)
        carols, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="carols",
                                               end_users=["carol"], expires_in_days=None)
        assert client.delete("/v1/users/dave/memories", headers=old).status_code == 200
        r = client.delete("/v1/users/carol/memories", headers=old)
        assert r.status_code == 403 and r.json()["code"] == code, r.text
        db.expire_all()
        assert db.get(AgentApiKey, carols.id).revoked_at is None
        # The current secret erases carol, and her key goes, as before.
        assert client.delete("/v1/users/carol/memories", headers=new).status_code == 200
        db.expire_all()
        assert db.get(AgentApiKey, carols.id).revoked_at is not None

    def test_a_rotation_let_in_just_before_its_owners_does_not_win(self, db, tenant, client,
                                                                  monkeypatch):
        """The rotation reads the key under its row's lock, after the request
        was let in: if the owner's rotation committed in between, the secret
        that let the request in is the old one, and holds no `admin` (the
        skeptic's race of 15 Nov 2026)."""
        from sqlalchemy.orm import sessionmaker

        row, first = key_service.create_api_key(db, tenant_id=tenant.id, name="leaked",
                                                scopes=ADMIN, expires_in_days=None)
        other = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
        owners = {}
        real = extensions.hooks.before_new_key

        def the_owner_rotates_first(s, tenant_id):
            if not owners:
                s2 = other()
                try:
                    r2 = s2.query(AgentApiKey).filter(AgentApiKey.id == row.id).with_for_update().one()
                    owners["key"] = key_service.reissue_secret(s2, r2, expires_at=None,
                                                               old_lasts=timedelta(hours=24))
                finally:
                    s2.close()
            return real(s, tenant_id)

        monkeypatch.setattr(extensions.hooks, "before_new_key", the_owner_rotates_first)
        r = _rotate(client, {"Authorization": f"Bearer {first}"}, row, old_key_expires_in_hours=0)
        assert r.status_code == 403 and r.json()["code"] == self.CODE, r.text
        assert _works(db, owners["key"]) and _works(db, first)

    def test_every_admin_route_says_it(self, client):
        paths = client.app.openapi()["paths"]
        for path in ("/admin/keys", "/admin/keys/{key_id}/revoke",
                     "/admin/keys/{key_id}/rotate", "/admin/projects/{project_id}"):
            for op in paths[path].values():
                if "403" in op.get("responses", {}):
                    assert self.CODE in op["responses"]["403"]["description"], path


class TestTheDashboardRotates:
    """The dashboard's half of the same decision (1.0.27): a Rotate on each
    key that rotates, with the old secret's end chosen from what the server
    offers ("Now" for a key that leaked), and a mark on a key that ends
    within the webhooks' notice. Up to 1.0.26 the page had the date and no
    Rotate. What the page does with these is run in node
    (dashboard/src/lib/keyLife.behaviour.tsx); here, what it is handed and
    that it is wired to it."""

    def test_the_listing_says_which_keys_rotate_as_the_route_decides(self, db, tenant, client, admin):
        from korely_agent.db.models import OAuthClient

        live, _ = _key(db, tenant, name="live")
        gone, _ = _key(db, tenant, name="gone")
        key_service.revoke_api_key(db, key_id=gone.id, tenant_id=tenant.id)
        ended, _ = _key(db, tenant, days=0, name="ended")
        db.add(OAuthClient(client_id="app-rotates", info={}))
        db.commit()
        app, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="app",
                                            oauth_client_id="app-rotates",
                                            expires_at=NOW() + timedelta(hours=1))
        listed = {k["name"]: k["rotates"] for k in client.get("/admin/keys", headers=admin).json()}
        assert listed == {"operator": True, "live": True, "gone": False, "ended": False,
                          "app": False}, listed
        # One rule: the route takes exactly the keys the listing says rotate.
        for row in (live, gone, ended, app):
            r = _rotate(client, admin, row, old_key_expires_in_hours=0)
            assert (r.status_code == 200) is listed[row.name], (row.name, r.text)
            assert r.status_code in (200, 409), r.text

    def test_the_screen_is_told_what_rotate_offers_and_the_route_takes_each(self, db, tenant,
                                                                            client, admin):
        from korely_agent.api.admin import KeyRotate

        out = client.get("/admin/keys/lifetimes", headers=admin).json()
        offered = out["old_secret_choices"]
        assert [c["hours"] for c in offered] == [0, 1, 24, 72, key_service.MAX_OLD_SECRET_HOURS]
        assert [c["label"] for c in offered] == ["Now", "1 hour", "24 hours", "3 days", "7 days"]
        # What the rotation takes when it is told nothing is what is served.
        assert out["old_secret_default_hours"] == KeyRotate().old_key_expires_in_hours == 24
        row, _ = _key(db, tenant, name="each")
        for c in offered:
            r = _rotate(client, admin, row, old_key_expires_in_hours=c["hours"])
            assert r.status_code == 200, (c, r.text)
            assert (r.json()["old_secret_expires_at"] is None) is (c["hours"] == 0), c
        ends = datetime.fromisoformat(_rotate(client, admin, row).json()["old_secret_expires_at"])
        assert timedelta(hours=23) < ends - NOW() <= timedelta(hours=out["old_secret_default_hours"])

    def test_the_keys_page_and_home_are_wired_to_it(self):
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "dashboard" / "src"
        page = (src / "pages" / "ApiKeysPage.tsx").read_text(encoding="utf-8")
        assert "{k.rotates && (" in page, "Rotate only where the route takes it"
        assert "rotateFromTheScreen(keysApi, target, hours, held, {" in page
        # What the menu shows (OldSecretChoice, rendered by the node checks)
        # and what the dialog sends are the one choice in force.
        assert "overlapChoices(lifetimes)" in page and "const hours = overlapInForce(offer, picked);" in page
        assert "<OldSecretChoice offer={offer} picked={picked} onPick={setPicked} />" in page
        assert "<EndBadge near={endNear(k, warningDays, now)} />" in page
        assert "<OldSecretLine until={oldSecretUntil(k, now)} />" in page
        # The rows get the served notice, and the dialog after a rotation
        # says until when the old secret works (the skeptic's mutants of 15
        # Nov 2026 that lived: `warningDays={null}`, `rotated={false}`).
        assert page.count("warningDays={lifetimes.data?.warning_days ?? null}") == 1
        assert "setRevealed({ key: k, rotated: true })" in page and "rotated={revealed.rotated}" in page
        # Once: a second rotation would end the secret the first showed (the
        # guard itself is run by keyLife.behaviour.tsx), and none after one
        # that may have gone through.
        dialog = page.split("function RotateKeyDialog", 1)[1].split("\nfunction ", 1)[0]
        assert "const held = useRef(false);" in dialog and "confirmDisabled={busy || stopped}" in dialog
        assert "setStopped(!again);" in dialog and "onFailed={refresh}" in page
        # One read of the lifetimes for the page, its rows and both dialogs.
        assert page.count("useLoad(keysApi.lifetimes") == 1
        api = (src / "lib" / "agentsApi.ts").read_text(encoding="utf-8")
        assert "apiPath`/admin/keys/${id}/rotate`" in api
        home = (src / "pages" / "HomePage.tsx").read_text(encoding="utf-8")
        assert "endingSoon(keys.data ?? [], lifetimes.data?.warning_days ?? null, Date.now())" in home
        assert "<KeysEndingNote" in home and "<LoadNote load={lifetimes}" in home

    def test_the_guide_asks_for_one_key_per_agent_or_machine(self):
        from pathlib import Path

        guide = (Path(__file__).resolve().parent.parent / "docs" / "guide"
                 / "keys-and-limits.md").read_text(encoding="utf-8")
        part = guide.split("### One key per agent or machine", 1)[1].split("\n## ", 1)[0]
        assert "make one key per agent or machine" in part
        assert "not where from" in part and "rotate it with Now or revoke it" in part

    def test_the_rotate_dialog_stays_open_while_its_call_is_out(self):
        """Escape, the backdrop and Cancel closed the Rotate dialog while its
        call was out: the rotation went on, its secret appeared after the
        Cancel, a refusal was never seen, and a dialog opened again had a
        new guard, so a second rotation could go out beside the first (the
        skeptic's finding of 15 Nov 2026). Every dialog that sends a request
        passes its `busy` to the Modal and to its Cancel (Modal.tsx, the
        defect of 13 Nov 2026)."""
        from pathlib import Path

        page = (Path(__file__).resolve().parent.parent / "dashboard" / "src" / "pages"
                / "ApiKeysPage.tsx").read_text(encoding="utf-8")
        dialog = page.split("function RotateKeyDialog", 1)[1].split("\nfunction ", 1)[0]
        assert '<Modal title="Rotate API key" onClose={onClose} busy={busy}>' in dialog
        assert "cancelDisabled={busy}" in dialog
        assert "setBusy(true);" in dialog and "setBusy(false);" in dialog

    def test_a_rotation_whose_answer_was_lost_shows_in_the_listing(self, db, tenant, client, admin):
        """What the screen reads when a rotation got no answer
        (dashboard/src/lib/keyLife.ts, afterNoAnswer): the listing's `masked`
        ends with the current secret's last four, so it changes at every
        rotation, Now included, and an overlap gives `old_secret_expires_at`.
        A refusal of the route's own changes neither, so the screen may say
        "not rotated" for it and let the next try go."""
        row, _ = _key(db, tenant, name="lost")

        def listed():
            return next(k for k in client.get("/admin/keys", headers=admin).json() if k["id"] == str(row.id))

        before = listed()
        assert before["old_secret_expires_at"] is None
        # Refused: an overlap past the ceiling, a key not this account's.
        assert _rotate(client, admin, row, old_key_expires_in_hours=10_000).status_code == 422
        assert client.post(f"/admin/keys/{uuid.uuid4()}/rotate", json={}, headers=admin).status_code == 404
        assert listed() == before
        # The answers dropped, as a lost connection drops them.
        assert _rotate(client, admin, row, old_key_expires_in_hours=0).status_code == 200
        now_rotated = listed()
        assert now_rotated["masked"] != before["masked"] and now_rotated["old_secret_expires_at"] is None
        assert _rotate(client, admin, row, old_key_expires_in_hours=24).status_code == 200
        overlapped = listed()
        assert overlapped["masked"] != now_rotated["masked"]
        assert overlapped["old_secret_expires_at"] is not None
        # Refused while the overlap runs: the listing is as it was.
        key_service.revoke_api_key(db, key_id=row.id, tenant_id=tenant.id)
        gone = listed()
        r = _rotate(client, admin, row, old_key_expires_in_hours=24)
        assert r.status_code == 409 and r.json()["code"] == "key_not_rotatable", r.text
        assert listed() == gone
