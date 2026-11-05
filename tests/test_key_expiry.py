"""Keys that stop working, and say so first.

A credential with no end date is one nobody ever gets round to rotating, so
these expire. But an expiry on its own is an outage with a timer on it: the
agent stops at three in the morning and nothing anywhere says why. That makes
the notice the load-bearing half of this feature, and most of this file is
about the notice rather than about the expiry.
"""
import uuid
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from korely_agent.core.times import aware
from korely_agent.db.models import AgentApiKey, AgentWebhookDelivery, Tenant
from korely_agent.services import keys as key_service
from korely_agent.services import webhooks as webhook_service


NOW = lambda: datetime.now(timezone.utc)


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr(webhook_service, "check_url", lambda u: u.strip())
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin(db, tenant):
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE],
        expires_in_days=None)
    return {"Authorization": f"Bearer {full}"}


class TestItActuallyStops:
    def test_a_key_works_until_its_date(self, db, tenant):
        row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                              expires_in_days=30)
        assert key_service.verify_api_key(db, raw) is not None

        row.expires_at = NOW() - timedelta(seconds=1)
        db.commit()
        assert key_service.verify_api_key(db, raw) is None, "it still worked after expiring"

    def test_never_means_never(self, db, tenant):
        _row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                               expires_in_days=None)
        assert db.query(AgentApiKey).filter(AgentApiKey.name == "k").one().expires_at is None
        assert key_service.verify_api_key(db, raw) is not None

    def test_zero_days_means_expired_now(self, db, tenant):
        """This test used to assert the opposite, and the opposite was wrong.

        It read: "somebody passing 0 means no expiry, not expire immediately",
        which is a guess about intent asserted without evidence. A tester
        produced the counter-case: a script computing
        `days = max(0, deadline - today)` on a date already past mints an
        immortal credential instead of being refused, and nothing anywhere says
        so. Two spellings of "never" and the one that reads like "now" being one
        of them is a trap, and the direction of the mistake is the wrong one for
        a credential.

        So: `None` is the only way to say never, and `0` means what it looks
        like. It also gives an operator the thing there was no way to do, which
        is to expire a key on command without revoking it.
        """
        _row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                               expires_in_days=0)
        assert key_service.verify_api_key(db, raw) is None, (
            "0 still mints a working key; a script doing max(0, ...) gets an "
            "immortal one")

    def test_the_default_is_finite(self, db, tenant):
        """The value somebody inherits without reading anything."""
        row, _raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k")
        assert row.expires_at is not None
        days = (aware(row.expires_at) - NOW()).days
        assert 88 <= days <= 90, days

    def test_expired_looks_like_revoked_from_outside(self, db, tenant, client):
        """Telling an unauthenticated caller which it was says which guesses
        were close. The owner finds out from the listing."""
        row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                              expires_in_days=1)
        row.expires_at = NOW() - timedelta(days=1)
        db.commit()
        expired = client.get("/v1/ping", headers={"Authorization": f"Bearer {raw}"})

        _r2, raw2 = key_service.create_api_key(db, tenant_id=tenant.id, name="k2")
        key_service.revoke_api_key(db, key_id=_r2.id, tenant_id=tenant.id)
        revoked = client.get("/v1/ping", headers={"Authorization": f"Bearer {raw2}"})

        assert expired.status_code == revoked.status_code == 401
        assert expired.json() == revoked.json()


class TestSayingWhichItIs:
    def test_state_of(self, db, tenant):
        live, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="a", expires_in_days=30)
        gone, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="b", expires_in_days=1)
        dead, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="c")

        gone.expires_at = NOW() - timedelta(days=1)
        key_service.revoke_api_key(db, key_id=dead.id, tenant_id=tenant.id)
        db.commit()

        assert key_service.state_of(live) == "live"
        assert key_service.state_of(gone) == "expired"
        assert key_service.state_of(dead) == "revoked"

    def test_a_kor_live_key_here_is_refused_in_every_listing(self, db, tenant, client, admin,
                                                            capsys, monkeypatch):
        """Audit of 19 Oct 2026: since 0.2.0 a `kor_live_` key on an install of
        one's own is refused at every door (keys.verify_api_key), yet
        state_of called it live, so the keys page, `manage list-keys`,
        `manage status` and the restore's list of the keys in force showed
        it working while the agent got a 401. One rule now
        (keys.refused_here), read by the door and by state_of."""
        from korely_agent import manage
        from korely_agent.services import credential_replay

        row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name="before 0.1.3",
                                              prefix=key_service.PREFIX_LIVE,
                                              expires_in_days=None)
        db.commit()
        assert key_service.verify_api_key(db, raw) is None
        assert key_service.state_of(row) == "refused"

        listed = {k["id"]: k for k in client.get("/admin/keys", headers=admin).json()}
        assert listed[str(row.id)]["state"] == "refused"

        monkeypatch.setattr(manage, "_session", lambda: db)
        manage.main(["list-keys", "--tenant", str(tenant.id)])
        line = next(ln for ln in capsys.readouterr().out.splitlines() if "before 0.1.3" in ln)
        assert "refused" in line and "never expires" not in line
        manage.main(["status"])
        keys_line = next(ln for ln in capsys.readouterr().out.splitlines()
                         if ln.startswith("keys"))
        assert "1 refused" in keys_line, keys_line

        in_force = next(a for a in credential_replay.in_force(db) if a["account"].id == tenant.id)
        assert row.id not in {k.id for k in in_force["keys"]}

        # On the Cloud `kor_live_` is the prefix it mints: live there.
        monkeypatch.setattr("korely_agent.config.get_settings",
                            lambda: SimpleNamespace(hosted=True))
        assert key_service.state_of(row) == "live"

    def test_the_listing_carries_the_date_and_the_state(self, db, tenant, client, admin):
        made = client.post("/admin/keys", headers=admin,
                           json={"name": "ninety", "expires_in_days": 90}).json()
        assert made["expires_at"] is not None
        assert made["state"] == "live"

        # `null` is the one way to say never. `0` used to be a second way, and
        # that was the trap: it is also what a script produces for a date that
        # has already passed.
        forever = client.post("/admin/keys", headers=admin,
                              json={"name": "forever", "expires_in_days": None}).json()
        assert forever["expires_at"] is None, (
            "explicit null is the screen's own spelling of Never expires, and "
            "it used to quietly mint a ninety-day key")
        assert forever["state"] == "live"

        omitted = client.post("/admin/keys", headers=admin,
                              json={"name": "omitted"}).json()
        assert omitted["expires_at"] is not None, (
            "omitting the field has to keep meaning the sensible default, not "
            "never")

        already = client.post("/admin/keys", headers=admin,
                              json={"name": "already", "expires_in_days": 0}).json()
        assert already["expires_at"] is not None
        assert already["state"] == "expired", (
            "0 minted a key that still works; that is how max(0, ...) makes an "
            "immortal credential")

    def test_the_screen_is_told_what_to_offer(self, client, admin):
        out = client.get("/admin/keys/lifetimes", headers=admin).json()
        assert out["default_days"] == 90
        assert [c["days"] for c in out["choices"]] == [30, 60, 90, 180, None]
        assert out["choices"][-1]["label"] == "Never expires"
        assert out["warning_days"] == 14
        assert out["max_days"] == key_service.MAX_LIFETIME_DAYS

    def test_the_longest_lifetime_is_one_number(self, client, admin):
        """The API and the CLI each wrote 3650 (audit of 17 Oct 2026):
        both read services/keys.MAX_LIFETIME_DAYS, the number the route serves."""
        import re
        from pathlib import Path

        most = key_service.MAX_LIFETIME_DAYS
        root = Path(__file__).resolve().parent.parent / "korely_agent"
        for rel in ("api/admin.py", "manage.py"):
            text = (root / rel).read_text(encoding="utf-8")
            assert not re.search(rf"\b{most}\b", text), rel
        assert client.post("/admin/keys", headers=admin,
                           json={"name": "longest", "expires_in_days": most}).status_code == 201
        assert client.post("/admin/keys", headers=admin,
                           json={"name": "longer", "expires_in_days": most + 1}).status_code == 422

    def test_the_dialog_keeps_no_copy_of_the_lifetimes(self):
        """The dialog draws what the route serves: no list, default or notice
        written into the page as a fallback."""
        import re
        from pathlib import Path

        page = (Path(__file__).resolve().parent.parent
                / "dashboard" / "src" / "pages" / "ApiKeysPage.tsx").read_text(encoding="utf-8")
        assert "keysApi.lifetimes" in page
        assert not re.search(r"days: (30|60|90|180)\b|warning_days \?\? \d|useState<number \| null>\(\d", page)


@pytest.mark.usefixtures("public_dns")   # the hook is on example.com (conftest)
class TestTheNoticeThatMakesItSafe:
    def _hook(self, db, tenant):
        return webhook_service.create_endpoint(
            db, tenant.id, url="https://example.com/hook", events=["*"])

    def _events(self, db, tenant):
        rows = db.query(AgentWebhookDelivery).all()
        return [r.event_type for r in rows]

    def test_two_weeks_of_notice(self, db, tenant):
        self._hook(db, tenant)
        row, _raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                               expires_in_days=30)
        # Nothing yet: thirty days is outside the window.
        assert key_service.warn_about_expiring(db) == {}

        row.expires_at = NOW() + timedelta(days=10)
        db.commit()
        assert key_service.warn_about_expiring(db) == {"warned": 1, "expired": 0}
        assert "key.expiring" in self._events(db, tenant)

    def test_it_says_how_long_is_left(self, db, tenant):
        self._hook(db, tenant)
        row, _raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                               expires_in_days=30)
        row.expires_at = NOW() + timedelta(days=5, hours=1)
        db.commit()
        key_service.warn_about_expiring(db)

        payload = db.query(AgentWebhookDelivery).one().payload
        data = payload.get("data", payload)
        assert data["days_left"] == 5
        assert data["name"] == "k"
        assert data["last_four"]

    def test_it_goes_out_once(self, db, tenant):
        """A notification that repeats every tick is one everybody filters."""
        self._hook(db, tenant)
        row, _raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                               expires_in_days=30)
        row.expires_at = NOW() + timedelta(days=3)
        db.commit()

        assert key_service.warn_about_expiring(db) == {"warned": 1, "expired": 0}
        assert key_service.warn_about_expiring(db) == {}
        assert key_service.warn_about_expiring(db) == {}
        assert self._events(db, tenant).count("key.expiring") == 1

    def test_the_day_it_happens_is_its_own_message(self, db, tenant):
        """"It stopped an hour ago" is what somebody needs at that moment."""
        self._hook(db, tenant)
        row, _raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                               expires_in_days=1)
        row.expires_at = NOW() - timedelta(hours=1)
        db.commit()

        assert key_service.warn_about_expiring(db) == {"warned": 0, "expired": 1}
        assert "key.expired" in self._events(db, tenant)

    def test_a_key_warned_before_hears_when_it_expires(self, db, tenant):
        """Every key that lives past the warning window was warned and then
        never told it stopped (audit of 17 Oct 2026)."""
        self._hook(db, tenant)
        row, _raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                               expires_in_days=30)
        row.expires_at = NOW() + timedelta(days=3)
        db.commit()
        assert key_service.warn_about_expiring(db) == {"warned": 1, "expired": 0}
        # Three days on: warned then, expired an hour ago.
        db.refresh(row)
        row.expiry_warned_at = NOW() - timedelta(days=3)
        row.expires_at = NOW() - timedelta(hours=1)
        db.commit()
        assert key_service.warn_about_expiring(db) == {"warned": 0, "expired": 1}
        assert key_service.warn_about_expiring(db) == {}
        assert self._events(db, tenant) == ["key.expiring", "key.expired"]

    def test_a_revoked_key_is_not_worth_a_warning(self, db, tenant):
        self._hook(db, tenant)
        row, _raw = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                               expires_in_days=30)
        row.expires_at = NOW() + timedelta(days=3)
        key_service.revoke_api_key(db, key_id=row.id, tenant_id=tenant.id)
        db.commit()
        assert key_service.warn_about_expiring(db) == {}

    def test_a_key_that_never_expires_is_never_warned_about(self, db, tenant):
        self._hook(db, tenant)
        key_service.create_api_key(db, tenant_id=tenant.id, name="k", expires_in_days=None)
        assert key_service.warn_about_expiring(db) == {}

    def test_one_account_is_not_told_about_another_s_keys(self, db, tenant):
        other = Tenant(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()
        self._hook(db, tenant)

        row, _raw = key_service.create_api_key(db, tenant_id=other.id, name="theirs",
                                               expires_in_days=30)
        row.expires_at = NOW() + timedelta(days=3)
        db.commit()
        key_service.warn_about_expiring(db)

        # The delivery exists, but for the other account, which has no endpoint.
        for d in db.query(AgentWebhookDelivery).all():
            assert d.tenant_id == other.id
