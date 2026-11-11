"""The model provider key, set from the screen.

It used to live only in `.env`, read once at boot, so the moment a customer
bought their own Google key somebody had to open an SSH session and restart
the service. That is the point where an install stops feeling like a product.

Three things have to hold, and they are the three this file is about: it is
not stored in clear text, it is never handed back, and saving it takes effect
without a restart.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.core import llm
from korely_agent.db.models import InstallSetting
from korely_agent.services import keys as key_service
from korely_agent.services import provider_key


REAL = "AIzaSyFAKEfakefakefakefakefakefake1234"


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-test")
    from korely_agent import config

    config.get_settings.cache_clear()
    llm.reset_provider_cache()
    yield
    config.get_settings.cache_clear()
    llm.reset_provider_cache()


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
def admin(db, tenant):
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE],
        expires_in_days=None)
    return {"Authorization": f"Bearer {full}"}


class TestItIsNotStoredInTheClear:
    def test_the_row_does_not_contain_the_key(self, db):
        provider_key.set_google_key(db, REAL)
        row = db.query(InstallSetting).one()
        assert REAL not in row.value_encrypted
        assert row.value_encrypted.startswith("gAAAAA"), "that is not a Fernet token"

    def test_it_comes_back_out_intact(self, db):
        provider_key.set_google_key(db, REAL)
        assert provider_key.get_google_key(db) == REAL

    def test_without_a_secret_it_refuses_rather_than_storing_plaintext(self, db, monkeypatch):
        from korely_agent import config

        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setattr(config.AgentSettings, "secret_key", None, raising=False)
        config.get_settings.cache_clear()
        with pytest.raises(provider_key.NoEncryptionKey):
            provider_key.set_google_key(db, REAL)
        assert db.query(InstallSetting).count() == 0

    def test_a_changed_secret_is_loud_and_treated_as_absent(self, db, monkeypatch, caplog):
        """Answering with a corrupt string would turn a recoverable
        configuration mistake into a provider error nobody can read."""
        import logging

        provider_key.set_google_key(db, REAL)
        monkeypatch.setenv("SECRET_KEY", "somebody-changed-it")
        from korely_agent import config

        config.get_settings.cache_clear()
        with caplog.at_level(logging.ERROR):
            assert provider_key.get_google_key(db) is None
        assert "will not decrypt" in caplog.text


class TestItIsNeverHandedBack:
    def test_the_endpoint_shows_four_characters_and_no_more(self, db, client, admin):
        provider_key.set_google_key(db, REAL)
        body = client.get("/admin/provider", headers=admin).json()
        assert body["set"] is True
        assert body["last_four"] == REAL[-4:]
        assert REAL not in client.get("/admin/provider", headers=admin).text

    def test_an_agent_key_cannot_read_or_write_it(self, db, tenant, client):
        _r, plain = key_service.create_api_key(db, tenant_id=tenant.id, name="agent")
        h = {"Authorization": f"Bearer {plain}"}
        assert client.get("/admin/provider", headers=h).status_code == 403
        assert client.put("/admin/provider", headers=h, json={"key": REAL}).status_code == 403


class TestItTakesEffectWithoutARestart:
    def test_the_resolver_prefers_the_screen_over_the_file(self, db, monkeypatch):
        from korely_agent import config

        monkeypatch.setenv("GOOGLE_API_KEY", "from-the-file")
        config.get_settings.cache_clear()
        llm.reset_provider_cache()
        assert llm.current_google_key() == "from-the-file"

        provider_key.set_google_key(db, REAL)
        assert llm.current_google_key() == REAL, \
            "the key was saved and the process kept using the old one"

    def test_clearing_it_falls_back_to_the_file(self, db, monkeypatch):
        from korely_agent import config

        monkeypatch.setenv("GOOGLE_API_KEY", "from-the-file")
        config.get_settings.cache_clear()
        provider_key.set_google_key(db, REAL)
        assert llm.current_google_key() == REAL

        provider_key.clear_google_key(db)
        assert llm.current_google_key() == "from-the-file"

    def test_with_neither_there_is_no_provider(self, db, monkeypatch):
        from korely_agent import config

        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setattr(config.AgentSettings, "google_api_key", None, raising=False)
        config.get_settings.cache_clear()
        llm.reset_provider_cache()
        assert llm.current_google_key() is None
        assert llm.provider_configured() is False

    def test_the_install_reports_itself_configured_once_a_key_is_pasted(self, db, monkeypatch):
        """`has_llm_provider` only ever saw the file, so an install whose key
        was pasted into the dashboard called itself unconfigured while working
        perfectly, and /health said so."""
        from korely_agent import config

        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setattr(config.AgentSettings, "google_api_key", None, raising=False)
        config.get_settings.cache_clear()
        llm.reset_provider_cache()
        assert llm.provider_configured() is False

        provider_key.set_google_key(db, REAL)
        assert llm.provider_configured() is True


class TestSavingChecksFirst:
    def test_a_key_the_provider_refuses_is_not_saved(self, db, client, admin, monkeypatch):
        """A key you paste and cannot test is the same silent failure as
        everything else here: the page says saved, nothing extracts, and the
        first write fails hours later with nobody watching."""
        monkeypatch.setattr(provider_key, "check",
                            lambda raw: {"ok": False, "detail": "API key not valid"})
        r = client.put("/admin/provider", headers=admin, json={"key": REAL})
        assert r.status_code == 400
        assert "API key not valid" in r.json()["detail"]
        assert db.query(InstallSetting).count() == 0, "it was saved anyway"

    def test_a_key_the_provider_accepts_is_saved(self, db, client, admin, monkeypatch):
        monkeypatch.setattr(provider_key, "check",
                            lambda raw: {"ok": True, "detail": "the provider answered"})
        r = client.put("/admin/provider", headers=admin, json={"key": REAL})
        assert r.status_code == 200
        assert r.json()["last_four"] == REAL[-4:]
        assert provider_key.get_google_key(db) == REAL

    def test_it_records_who_pasted_it(self, db, client, admin, monkeypatch):
        monkeypatch.setattr(provider_key, "check", lambda raw: {"ok": True, "detail": "ok"})
        client.put("/admin/provider", headers=admin, json={"key": REAL})
        assert db.query(InstallSetting).one().updated_by is not None

    def test_without_a_secret_the_answer_is_503_not_400(self, db, client, admin, monkeypatch):
        """Nothing is wrong with what they typed."""
        from korely_agent import config

        monkeypatch.setattr(provider_key, "check", lambda raw: {"ok": True, "detail": "ok"})
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setattr(config.AgentSettings, "secret_key", None, raising=False)
        config.get_settings.cache_clear()
        r = client.put("/admin/provider", headers=admin, json={"key": REAL})
        assert r.status_code == 503
        assert "SECRET_KEY" in r.json()["detail"]


class TestTheStateItReports:
    def test_it_says_where_the_key_came_from(self, db, client, admin, monkeypatch):
        from korely_agent import config

        monkeypatch.setenv("GOOGLE_API_KEY", "from-the-file-1234")
        config.get_settings.cache_clear()
        assert client.get("/admin/provider", headers=admin).json()["source"] == "env"

        provider_key.set_google_key(db, REAL)
        assert client.get("/admin/provider", headers=admin).json()["source"] == "screen"

    def test_it_carries_what_the_provider_last_did(self, db, client, admin):
        """Not whether a key is set. The two are different questions, and the
        second one said everything was fine for hours while the credit was
        gone."""
        body = client.get("/admin/provider", headers=admin).json()
        assert "last_call" in body
        assert "state" in body["last_call"]

    def test_it_says_when_it_cannot_store_anything(self, db, client, admin, monkeypatch):
        from korely_agent import config

        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setattr(config.AgentSettings, "secret_key", None, raising=False)
        config.get_settings.cache_clear()
        assert client.get("/admin/provider", headers=admin).json()["can_store"] is False


class TestTryingAndForgettingIt:
    """POST /admin/provider/test and DELETE /admin/provider, which only the
    Cloud's 403 reached before (audit of 17 Oct 2026)."""

    @pytest.fixture
    def asked(self, monkeypatch):
        tried = []
        monkeypatch.setattr(provider_key, "check", lambda raw: (
            tried.append(raw), {"ok": raw == REAL, "detail": "the provider answered"})[1])
        return tried

    def test_a_key_is_tried_without_being_saved(self, db, client, admin, asked):
        r = client.post("/admin/provider/test", headers=admin, json={"key": REAL})
        assert r.status_code == 200 and r.json() == {"ok": True, "detail": "the provider answered"}
        assert asked == [REAL]
        assert db.query(InstallSetting).count() == 0, "a test saved the key"

    def test_with_no_body_the_key_in_force_is_tried(self, db, client, admin, asked):
        provider_key.set_google_key(db, REAL)
        r = client.post("/admin/provider/test", headers=admin)
        assert r.status_code == 200 and r.json()["ok"] is True
        assert asked == [REAL]

    @pytest.mark.parametrize("body", [{}, {"key": ""}, {"key": None}, {"key": "   "}])
    def test_an_empty_box_tries_the_key_in_force(self, db, client, admin, asked, body):
        """Settings' "Test only" with nothing typed sends `{}` (lib/agentsApi.ts,
        providerApi.test): it was answered 422 "key: Field required", so the
        button could never test the key in use (functional test of 28 Oct
        2026)."""
        provider_key.set_google_key(db, REAL)
        r = client.post("/admin/provider/test", headers=admin, json=body)
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True and asked == [REAL]

    def test_with_no_key_at_all_there_is_nothing_to_test(self, db, client, admin, asked):
        r = client.post("/admin/provider/test", headers=admin)
        assert r.status_code == 404 and "no key to test" in r.json()["message"]
        assert asked == []

    def test_forgetting_it_hands_back_to_the_file(self, db, client, admin, monkeypatch):
        from korely_agent import config

        monkeypatch.setenv("GOOGLE_API_KEY", "from-the-file-1234")
        config.get_settings.cache_clear()
        provider_key.set_google_key(db, REAL)
        assert llm.current_google_key() == REAL

        r = client.delete("/admin/provider", headers=admin)
        assert r.status_code == 200 and r.json() == {"deleted": True, "id": "provider"}
        assert db.query(InstallSetting).count() == 0
        assert client.get("/admin/provider", headers=admin).json()["source"] == "env"
        assert llm.current_google_key() == "from-the-file-1234", "the old key still in force"
        # Twice is not an error, and says nothing was there.
        assert client.delete("/admin/provider", headers=admin).json()["deleted"] is False
