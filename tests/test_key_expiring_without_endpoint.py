"""`key.expiring` is not lost when nobody listens yet (code review finding, 1
Nov 2026).

The round stamped `expiry_warned_at` whenever the enqueue did not fail, and
`emit_event` answers 0, not None, when no endpoint takes the event: a key that
entered its last two weeks before the account had a webhook was marked as
warned with nothing sent, and an endpoint created later heard only
`key.expired`. A notice is spent only when a delivery was queued; until then the
key waits for a listener, and waiting does not hold the round's block. The
keys the old round stamped that way are given back their notice by the upgrade
(migration c5e9a3f7b1d4).
"""
import importlib.util
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.db.models import (AgentApiKey, AgentWebhookDelivery, AgentWebhookEndpoint,
                                    OAuthClient, Tenant)
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services import webhooks as webhook_service


NOW = lambda: datetime.now(timezone.utc)


def _hook(db, tenant_id, **kw):
    kw.setdefault("events", ["*"])
    return webhook_service.create_endpoint(db, tenant_id, url="https://example.com/hook", **kw)


def _queued(db, event):
    return [d for d in db.query(AgentWebhookDelivery).all() if d.event_type == event]


def _soon(db, tenant_id, name="k", days=10, **kw):
    row, _ = key_service.create_api_key(db, tenant_id=tenant_id, name=name,
                                        expires_at=NOW() + timedelta(days=days), **kw)
    return row


def _the_upgrade(db):
    """What the upgrade does to the stamps of the round up to 1.0.7
    (migration c5e9a3f7b1d4, `repair`), on the rows the test wrote."""
    path = next(pathlib.Path(__file__).resolve().parent.parent.glob(
        "alembic/versions/c5e9a3f7b1d4_*.py"))
    spec = importlib.util.spec_from_file_location("mig_key_expiring_nobody_heard", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    out = mig.repair(db.connection())
    db.commit()
    db.expire_all()
    return out


@pytest.mark.usefixtures("public_dns")   # the hook is on example.com (conftest)
class TestTheNoticeWaitsForAListener:
    def test_an_endpoint_created_in_the_last_two_weeks_hears_key_expiring(self, db, tenant):
        """The row's reproduction: the key enters its window with no endpoint,
        the endpoint comes after, and the notice still goes out once."""
        row = _soon(db, tenant.id)
        assert key_service.warn_about_expiring(db) == {}
        assert _queued(db, "key.expiring") == []
        assert db.get(AgentApiKey, row.id).expiry_warned_at is None

        _hook(db, tenant.id)
        assert key_service.warn_about_expiring(db) == {"warned": 1, "expired": 0}
        sent = _queued(db, "key.expiring")
        assert len(sent) == 1 and sent[0].payload["data"]["key_id"] == str(row.id)
        assert db.get(AgentApiKey, row.id).expiry_warned_at is not None
        assert key_service.warn_about_expiring(db) == {}

    def test_an_endpoint_that_does_not_take_the_event_is_nobody(self, db, tenant):
        """Subscribed to something else, or bound to another project: no
        delivery, so nothing is spent."""
        other = project_service.create_project(db, tenant_id=tenant.id, name="Staging")
        _hook(db, tenant.id, events=["memory.created"])
        _hook(db, tenant.id, project_id=other.id)
        row = _soon(db, tenant.id)
        assert key_service.warn_about_expiring(db) == {}
        assert db.get(AgentApiKey, row.id).expiry_warned_at is None

        _hook(db, tenant.id, events=["key.expiring"], project_id=row.project_id)
        assert key_service.warn_about_expiring(db) == {"warned": 1, "expired": 0}
        assert len(_queued(db, "key.expiring")) == 1

    def test_a_switched_off_endpoint_is_nobody(self, db, tenant):
        """An endpoint that is not active takes no delivery: the key waits."""
        ep = _hook(db, tenant.id)
        ep.active = False
        db.commit()
        row = _soon(db, tenant.id)
        assert key_service.warn_about_expiring(db) == {}
        assert db.get(AgentApiKey, row.id).expiry_warned_at is None

    def test_an_endpoint_gone_before_the_notice_leaves_the_key_waiting(self, db, tenant,
                                                                       monkeypatch):
        """The round found a listener, and it was deleted before the enqueue:
        0 deliveries is not a notice sent, so the key is not stamped."""
        ep = _hook(db, tenant.id)
        row = _soon(db, tenant.id)
        emit = webhook_service.emit_event

        def deleted_first(db_, *args, **kwargs):
            db_.query(AgentWebhookEndpoint).filter(AgentWebhookEndpoint.id == ep.id).delete()
            return emit(db_, *args, **kwargs)

        monkeypatch.setattr(webhook_service, "emit_event", deleted_first)
        assert key_service.warn_about_expiring(db) == {}
        assert db.get(AgentApiKey, row.id).expiry_warned_at is None
        assert _queued(db, "key.expiring") == []

    def test_a_key_that_expired_unheard_is_done(self, db, tenant):
        """`key.expired` is the notice of one day: with nobody listening it is
        spent anyway, or the key would come back every round for ever, and an
        endpoint created a month later is not told about it."""
        row = _soon(db, tenant.id, days=-1)
        assert key_service.warn_about_expiring(db) == {"warned": 0, "expired": 1}
        assert db.get(AgentApiKey, row.id).expiry_warned_at is not None

        _hook(db, tenant.id)
        assert key_service.warn_about_expiring(db) == {}
        assert _queued(db, "key.expired") == []

    def test_keys_waiting_for_a_listener_do_not_hold_the_round(self, db, tenant, monkeypatch):
        """Unstamped, they would be read again every round, first in the block
        as they expire first, and the keys of an account that listens would
        never be reached (the starvation of the audit of 27 Oct 2026)."""
        monkeypatch.setattr(key_service, "STREAM_ROWS", 2)
        quiet = Tenant(id=uuid.uuid4(), name="No Webhooks")
        db.add(quiet)
        db.commit()
        for i in range(3):
            _soon(db, quiet.id, name=f"quiet-{i}", days=1 + i)
        _hook(db, tenant.id)
        mine = _soon(db, tenant.id, name="mine", days=9)

        assert key_service.warn_about_expiring(db) == {"warned": 1, "expired": 0}
        assert [d.payload["data"]["key_id"] for d in _queued(db, "key.expiring")] == [str(mine.id)]


@pytest.mark.usefixtures("public_dns")
class TestTheUpgradeGivesBackANoticeNobodyHeard:
    def test_a_stamp_with_nothing_queued_is_due_again(self, db, tenant):
        """A key the round up to 1.0.7 stamped with no endpoint to take the
        notice: after the upgrade an endpoint added in its two weeks hears
        `key.expiring`. A notice that was queued, a key that expired and a
        revoked one keep their stamp."""
        _hook(db, tenant.id)
        heard = _soon(db, tenant.id, name="heard")
        assert key_service.warn_about_expiring(db) == {"warned": 1, "expired": 0}
        day_before = NOW() - timedelta(days=1)
        unheard = _soon(db, tenant.id, name="unheard", days=9)
        expired = _soon(db, tenant.id, name="expired", days=-1)
        revoked = _soon(db, tenant.id, name="revoked", days=8)
        for row in (unheard, expired, revoked):
            row.expiry_warned_at = day_before - timedelta(days=1)
        revoked.revoked_at = day_before
        db.commit()

        assert _the_upgrade(db) == 1
        assert db.get(AgentApiKey, unheard.id).expiry_warned_at is None
        for row in (heard, expired, revoked):
            assert db.get(AgentApiKey, row.id).expiry_warned_at is not None
        assert _the_upgrade(db) == 0

        assert key_service.warn_about_expiring(db) == {"warned": 1, "expired": 1}
        assert sorted(d.payload["data"]["key_id"] for d in _queued(db, "key.expiring")) == \
            sorted([str(heard.id), str(unheard.id)])

    def test_only_a_key_expiring_of_its_own_account_is_a_notice_heard(self, db, tenant):
        """The record of a notice is a `key.expiring` delivery of the key's
        account about it: a `key.expired` naming the key, or another account's
        `key.expiring` carrying the same id, is not one, and the stamp goes."""
        other = Tenant(id=uuid.uuid4(), name="Other Account")
        db.add(other)
        db.commit()
        _hook(db, tenant.id)
        _hook(db, other.id)
        wrong_event = _soon(db, tenant.id, name="wrong-event", days=9)
        wrong_account = _soon(db, tenant.id, name="wrong-account", days=8)
        assert webhook_service.emit_event(db, tenant.id, "key.expired",
                                          {"key_id": str(wrong_event.id)},
                                          project_id=wrong_event.project_id, commit=True) == 1
        assert webhook_service.emit_event(db, other.id, "key.expiring",
                                          {"key_id": str(wrong_account.id)},
                                          project_id=None, commit=True) == 1
        for row in (wrong_event, wrong_account):
            row.expiry_warned_at = NOW() - timedelta(days=2)
        db.commit()

        assert _the_upgrade(db) == 2
        for row in (wrong_event, wrong_account):
            assert db.get(AgentApiKey, row.id).expiry_warned_at is None

    def test_a_connected_apps_key_keeps_its_stamp(self, db, tenant):
        """An OAuth client's key expires every hour and renews itself, and the
        round leaves it out: so does the upgrade."""
        db.add(OAuthClient(client_id="app-1", info={}))
        db.commit()
        app = _soon(db, tenant.id, name="app", days=9, oauth_client_id="app-1")
        app.expiry_warned_at = NOW() - timedelta(days=1)
        db.commit()

        assert _the_upgrade(db) == 0
        assert db.get(AgentApiKey, app.id).expiry_warned_at is not None
