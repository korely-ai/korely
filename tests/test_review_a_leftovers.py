"""Review of the audit of 27 Oct 2026: the cap on keys at the command line, and
the expiry round that must not starve."""
from datetime import datetime, timedelta, timezone

from korely_agent import manage
from korely_agent.db.models import AgentWebhookEndpoint
from korely_agent.services import keys as key_service


def test_issue_key_past_the_cap_is_a_sentence_not_a_traceback(db, tenant, monkeypatch, capsys):
    monkeypatch.setattr(manage, "_session", lambda: db)
    monkeypatch.setattr(key_service, "MAX_API_KEYS", 1)
    key_service.create_api_key(db, tenant_id=tenant.id, name="first")
    assert manage.main(["issue-key", "--tenant", str(tenant.id), "--name", "second"]) == 1
    err = capsys.readouterr().err
    assert "revoke one first" in err and "Traceback" not in err


def test_keys_refused_here_do_not_hold_the_expiry_round(db, tenant, monkeypatch):
    """Refused keys were skipped in Python and never stamped: a block of them at
    the front of the round left the others unwarned for good."""
    monkeypatch.setattr(key_service, "STREAM_ROWS", 2)
    db.add(AgentWebhookEndpoint(tenant_id=tenant.id, url="https://example.com/h",
                                secret="whsec_test", events=["key.expiring", "key.expired"]))
    db.commit()
    for i in range(3):   # they expire first, so they would fill the block
        key_service.create_api_key(db, tenant_id=tenant.id, name=f"old-{i}", prefix="kor_live_",
                                   expires_at=datetime.now(timezone.utc) + timedelta(days=1 + i))
    key_service.create_api_key(db, tenant_id=tenant.id, name="mine",
                               expires_at=datetime.now(timezone.utc) + timedelta(days=9))
    assert key_service.warn_about_expiring(db) == {"warned": 1, "expired": 0}
