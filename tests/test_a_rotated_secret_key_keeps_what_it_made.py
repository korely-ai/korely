"""A rotation of SECRET_KEY loses nothing while the old value is in
SECRET_KEY_FALLBACKS (review of 15 Nov 2026).

The pseudonym an erasure leaves in place of an end user's id is keyed under
SECRET_KEY (services/pseudonyms.py), and so is what the screen seals (the
model key, the model choice, an OAuth client's secret). A new SECRET_KEY,
after a leak say, made every earlier erasure unfindable by the person's id
(GET /v1/audit?user_id=) and every sealed value unreadable. Django's answer,
copied here: SECRET_KEY_FALLBACKS, the old values, read after SECRET_KEY by
every lookup and never used to make anything new
(docs.djangoproject.com/en/5.2/ref/settings/#secret-key-fallbacks).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services import pseudonyms
from tests.conftest import seeded_project_id

OLD, NEW = "the-secret-of-before-" + "x" * 24, "the-secret-of-now-" + "y" * 24


@pytest.fixture
def secrets(monkeypatch):
    from korely_agent.config import get_settings

    def set_(key, fallbacks=None):
        monkeypatch.setenv("SECRET_KEY", key or "")
        if fallbacks is None:
            monkeypatch.delenv("SECRET_KEY_FALLBACKS", raising=False)
        else:
            monkeypatch.setenv("SECRET_KEY_FALLBACKS", fallbacks)
        get_settings.cache_clear()
    yield set_
    get_settings.cache_clear()


@pytest.fixture
def client(db, monkeypatch):
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def maria_erased(db, tenant, client, secrets):
    """Maria's memory, erased under the old SECRET_KEY; the key that erased."""
    secrets(OLD)
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id,
                       project_id=seeded_project_id(db, tenant.id),
                       end_user_id="maria", content="user: I live in Milan."))
    db.commit()
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="eraser")
    head = {"Authorization": f"Bearer {full}"}
    assert client.delete("/v1/users/maria/memories", headers=head).status_code == 200
    return head


def _the_proof(client, head):
    r = client.get("/v1/audit", params={"user_id": "maria"}, headers=head)
    assert r.status_code == 200, r.text
    return [e for e in r.json()["events"] if e["action"] == "erase"]


class TestTheErasuresProof:
    def test_after_a_rotation_the_proof_is_found_by_the_id(self, client, secrets,
                                                           maria_erased):
        secrets(NEW, OLD)
        assert _the_proof(client, maria_erased)

    def test_without_the_fallback_it_is_not(self, client, secrets, maria_erased):
        """The limit the guide states: the old value dropped, the proof is
        found by its `audit_id` alone."""
        secrets(NEW)
        assert not _the_proof(client, maria_erased)

    def test_several_fallbacks_newest_first(self, client, secrets, maria_erased):
        secrets(NEW, f" another-old-one ,{OLD}, ")
        assert _the_proof(client, maria_erased)


def _maria_erasure(db, tenant):
    from korely_agent.db.models import AgentForgetAudit

    return (db.query(AgentForgetAudit)
            .filter(AgentForgetAudit.tenant_id == tenant.id, AgentForgetAudit.scope == "user")
            .one())


class TestTheTrailAndTheDeliveries:
    """What reads the erasure's pseudonym again after it is filed (the
    skeptic's review of the scopes piece, 15 Nov 2026): the trail's label of
    a later call that names the person (pseudonyms.as_logged), and a
    delivery that finishes after the erasure (pseudonyms.erased_since)."""

    def _trail(self, db, tenant):
        from korely_agent.db.models import AuditEvent

        db.expire_all()
        return dict(db.query(AuditEvent.id, AuditEvent.end_user_id)
                    .filter(AuditEvent.tenant_id == tenant.id).all())

    @pytest.mark.parametrize("fallback", [OLD, None], ids=["with-the-fallback", "dropped"])
    def test_a_later_call_naming_the_person_is_written_with_the_old_pseudonym(
            self, db, tenant, client, secrets, maria_erased, fallback):
        written = _maria_erasure(db, tenant).end_user_id
        assert written.startswith(pseudonyms.PREFIX)
        secrets(NEW, fallback)
        before = self._trail(db, tenant)
        r = client.get("/v1/facts", params={"user_id": "maria"}, headers=maria_erased)
        assert r.status_code == 200, r.text
        rows = {i: who for i, who in self._trail(db, tenant).items() if i not in before}
        # Dropped, the limit the guide states: the id is written again.
        assert list(rows.values()) == [written if fallback else "maria"]

    @pytest.mark.parametrize("fallback", [OLD, None], ids=["with-the-fallback", "dropped"])
    def test_a_delivery_that_finishes_after_it_keeps_the_old_pseudonym(
            self, db, tenant, secrets, maria_erased, fallback):
        from datetime import timedelta
        from types import SimpleNamespace

        from korely_agent.services import webhooks

        erasure = _maria_erasure(db, tenant)
        secrets(NEW, fallback)
        d = SimpleNamespace(tenant_id=tenant.id, erased_user=False,
                            created_at=erasure.created_at - timedelta(seconds=1),
                            payload={"event": "memory.added", "project_id": None,
                                     "data": {"user_id": "maria"}})
        webhooks._renamed_if_erased_meanwhile(db, d, finishing=True)
        assert d.payload["data"]["user_id"] == (erasure.end_user_id if fallback else "maria")


class TestWhatIsMadeNow:
    def test_a_new_pseudonym_is_made_under_secret_key_alone(self, secrets):
        t = uuid.uuid4()
        secrets(NEW)
        under_new = pseudonyms.pseudonym(t, "bob")
        secrets(OLD)
        under_old = pseudonyms.pseudonym(t, "bob")
        secrets(NEW, OLD)
        assert pseudonyms.pseudonym(t, "bob") == under_new
        assert pseudonyms.every_pseudonym(t, "bob") == [under_new, under_old]
        assert pseudonyms.key_check() == pseudonyms.key_checks()[0]
        assert len(set(pseudonyms.key_checks())) == 2

    def test_no_secret_key_makes_nothing_and_still_finds(self, secrets):
        t = uuid.uuid4()
        secrets(OLD)
        under_old = pseudonyms.pseudonym(t, "bob")
        secrets(None, OLD)
        assert pseudonyms.pseudonym(t, "bob") is None and pseudonyms.key_check() is None
        assert pseudonyms.every_pseudonym(t, "bob") == [under_old]


class TestWhatWasSealed:
    def test_a_value_sealed_under_the_old_key_opens_under_a_fallback(self, secrets):
        from cryptography.fernet import InvalidToken

        from korely_agent.services.provider_key import decrypt_secret, encrypt_secret

        secrets(OLD)
        sealed = encrypt_secret("the-model-key")
        secrets(NEW, OLD)
        assert decrypt_secret(sealed) == "the-model-key"
        again = encrypt_secret("the-model-key")
        secrets(NEW)
        assert decrypt_secret(again) == "the-model-key", "a new seal is under SECRET_KEY"
        with pytest.raises(InvalidToken):
            decrypt_secret(sealed)

    def test_no_secret_key_still_refuses_to_seal_or_open(self, secrets):
        from korely_agent.services.provider_key import (NoEncryptionKey, decrypt_secret,
                                                         encrypt_secret)

        secrets(OLD)
        sealed = encrypt_secret("the-model-key")
        secrets(None, OLD)
        with pytest.raises(NoEncryptionKey):
            encrypt_secret("x")
        with pytest.raises(NoEncryptionKey):
            decrypt_secret(sealed)


class TestSealingAgain:
    """`manage reseal-secrets` (provider_key.seal_again): an OAuth client's
    secret is sealed only at its registration, so without it a fallback
    could never go without losing it (the skeptic's review of the scopes
    piece, 15 Nov 2026)."""

    def _client_row(self, db, secret):
        from korely_agent.db.models import OAuthClient
        from korely_agent.services.provider_key import encrypt_secret

        row = OAuthClient(client_id=f"c-{uuid.uuid4().hex[:12]}", info={"client_name": "x"},
                          secret_encrypted=encrypt_secret(secret))
        db.add(row)
        db.commit()
        return row.client_id

    def test_after_it_the_fallback_can_go(self, db, secrets, monkeypatch, capsys):
        from korely_agent import manage
        from korely_agent.db.models import OAuthClient
        from korely_agent.services import provider_key

        secrets(OLD)
        provider_key.set_google_key(db, "the-model-key")
        db.commit()
        cid = self._client_row(db, "the-client-secret")
        secrets(NEW, OLD)
        monkeypatch.setattr(manage, "_session", lambda: db)
        assert manage.main(["reseal-secrets"]) == 0
        assert "2 sealed value(s) sealed again under SECRET_KEY, 0 under it already" in (
            capsys.readouterr().out)
        assert provider_key.seal_again(db) == {"sealed_again": 0, "current": 2, "unreadable": 0}
        secrets(NEW)
        db.expire_all()
        assert provider_key.get_google_key(db) == "the-model-key"
        assert provider_key.decrypt_secret(db.get(OAuthClient, cid).secret_encrypted) == (
            "the-client-secret")

    def test_the_clients_are_read_a_page_at_a_time(self, db, secrets, monkeypatch):
        """Any client may register, so the clients are read STREAM_ROWS ids
        at a time (db/bounded.py, the 1.0.27 gate): with a page of one,
        each of three is sealed again, once."""
        from korely_agent.db import bounded
        from korely_agent.db.models import OAuthClient
        from korely_agent.services import provider_key

        secrets(OLD)
        ids = [self._client_row(db, f"secret-{n}") for n in range(3)]
        secrets(NEW, OLD)
        monkeypatch.setattr(bounded, "STREAM_ROWS", 1)
        assert provider_key.seal_again(db) == {"sealed_again": 3, "current": 0, "unreadable": 0}
        secrets(NEW)
        db.expire_all()
        assert [provider_key.decrypt_secret(db.get(OAuthClient, cid).secret_encrypted)
                for cid in ids] == ["secret-0", "secret-1", "secret-2"]

    def test_what_no_secret_opens_is_counted_and_left(self, db, secrets, monkeypatch, capsys):
        from korely_agent import manage
        from korely_agent.db.models import OAuthClient

        secrets("a-secret-nobody-kept-" + "z" * 24)
        cid = self._client_row(db, "lost")
        sealed = db.get(OAuthClient, cid).secret_encrypted
        secrets(NEW, OLD)
        monkeypatch.setattr(manage, "_session", lambda: db)
        assert manage.main(["reseal-secrets"]) == 2
        assert "1 sealed value(s) open with neither" in capsys.readouterr().err
        db.expire_all()
        assert db.get(OAuthClient, cid).secret_encrypted == sealed

    def test_without_secret_key_it_refuses(self, db, secrets, monkeypatch, capsys):
        from korely_agent import manage

        secrets(None, OLD)
        monkeypatch.setattr(manage, "_session", lambda: db)
        assert manage.main(["reseal-secrets"]) == 1
        assert "SECRET_KEY is not set" in capsys.readouterr().err
