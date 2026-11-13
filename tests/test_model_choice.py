"""Where the memories are written, chosen at first login (2026-10-02).

The flow: Europe or the United States, a provider (EU: Mistral,
Scaleway; US: OpenAI, Claude, Gemini, Grok, OpenRouter), the key, and the
writing model preselected from our measures. No reading model: the
customer's own model reads, and we never pay for it. The same three rules
as the provider key: not stored in clear text, never handed back, in force
without a restart. Plus the ones this screen adds: the choice wins over any
model name left in `.env`, Gemini runs on its own client, and the screen
knows whether anything writes at all, so the first login can ask.
"""
import io
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from korely_agent.core import llm
from korely_agent.db.models import InstallSetting
from korely_agent.services import keys as key_service
from korely_agent.services import model_choice


SCW = "scw-secret-0000-1111-2222-3333-abcd"
LISTS = {
    "scaleway": {"data": [{"id": "gpt-oss-120b"}, {"id": "llama-3.3-70b-instruct"},
                          {"id": "bge-multilingual-gemma2"}, {"id": "whisper-large-v3"}]},
    "openrouter": {"data": [
        {"id": "stealth/space-bunny-alpha", "pricing": {"prompt": "0"}},
        {"id": "deepseek/deepseek-v4.1-flash", "pricing": {"prompt": "0.0000003"}},
        {"id": "openai/gpt-oss-120b", "pricing": {"prompt": "0.00000005"}},
        {"id": "anthropic/claude-sonnet-5.5", "pricing": {"prompt": "0.000002"}}]},
    "anthropic": {"data": [{"id": "claude-sonnet-5-5"}, {"id": "claude-haiku-4-5-20251001"}]},
    "google": {"models": [
        {"name": "models/gemini-3.1-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-embedding-2-preview", "supportedGenerationMethods": ["embedContent"]}]},
    "openai": {"data": [{"id": "gpt-4.1-mini"}, {"id": "gpt-6-luna"}, {"id": "text-embedding-3-large"},
                        {"id": "dall-e-3"}, {"id": "whisper-1"}, {"id": "o9-nova"}]},
}


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
def providers(monkeypatch):
    """Every provider, faked at the HTTP call. `calls` records what was asked."""
    calls = []
    state = {"refuse": False}

    def fake(url, headers):
        calls.append({"url": url, "headers": headers})
        if state["refuse"]:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b""))
        if url.endswith("/key"):
            return {"data": {"usage": 0}}
        for pid, body in LISTS.items():
            if model_choice.PROVIDERS[pid].base in url:
                return body
        raise AssertionError(url)

    monkeypatch.setattr(model_choice, "_get", fake)
    return {"calls": calls, "state": state}


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


def save(client, admin, provider="scaleway", key=SCW, model="llama-3.3-70b-instruct"):
    return client.put(f"/admin/models/{provider}", headers=admin, json={"key": key, "model": model})


class TestTheFirstLoginKnowsWhetherAnythingWrites:
    def test_a_fresh_install_is_not_configured(self, client, admin, monkeypatch):
        from korely_agent import config

        monkeypatch.setenv("GOOGLE_API_KEY", "")
        config.get_settings.cache_clear()
        out = client.get("/admin/models", headers=admin).json()
        assert out["configured"] is False and out["source"] is None and out["active"] is None
        assert [p["id"] for p in out["providers"]] == [
            "mistral", "scaleway", "openai", "anthropic", "google", "xai", "openrouter"]
        assert {p["region"] for p in out["providers"] if p["id"] in ("mistral", "scaleway")} == {"eu"}

    def test_an_install_configured_in_env_is_left_alone(self, client, admin, monkeypatch):
        from korely_agent import config

        monkeypatch.setenv("EXTRACTION_PROVIDER", "mistral")
        monkeypatch.setenv("MISTRAL_API_KEY", "from-the-file")
        config.get_settings.cache_clear()
        out = client.get("/admin/models", headers=admin).json()
        assert out["configured"] is True and out["source"] == "env"

    @pytest.mark.parametrize("env, active", [
        ({"EXTRACTION_PROVIDER": "gemini", "GOOGLE_API_KEY": "AIza-from-the-file-g00g",
          "EXTRACTION_MODEL": "gemini-9-flash"},
         {"provider": "google", "label": "Gemini", "region": "global",
          "model": "gemini-9-flash", "last_four": "g00g"}),
        ({"EXTRACTION_PROVIDER": "mistral", "MISTRAL_API_KEY": "sk-or-from-the-file-0r0r",
          "MISTRAL_API_BASE": "https://openrouter.ai/api/v1",
          "MISTRAL_EXTRACTION_MODEL": "openai/gpt-oss-120b:nitro"},
         {"provider": "openrouter", "label": "OpenRouter", "region": "global",
          "model": "openai/gpt-oss-120b:nitro", "last_four": "0r0r"}),
        ({"EXTRACTION_PROVIDER": "mistral", "MISTRAL_API_KEY": "from-the-file-m1n3",
          "MISTRAL_API_BASE": "http://localhost:9/v1", "MISTRAL_EXTRACTION_MODEL": "mine"},
         {"provider": "mistral", "label": "OpenAI-compatible address in .env", "region": None,
          "model": "mine", "last_four": "m1n3"}),
    ])
    def test_the_writer_in_env_is_described(self, client, admin, monkeypatch, env, active):
        """Settings' Writing model showed dashes for Region, Model and Key
        when `.env` set the writer (`active` was the screen's choice only),
        while Usage named the model writing (functional test of 28 Oct
        2026). The provider and region are the door's address's, as
        core/llm.get_provider builds the writer."""
        from korely_agent import config

        for k, v in env.items():
            monkeypatch.setenv(k, v)
        config.get_settings.cache_clear()
        llm.reset_provider_cache()
        out = client.get("/admin/models", headers=admin).json()
        assert out["source"] == "env" and out["active"] == active

    def test_a_saved_choice_is_the_source(self, client, admin, providers):
        save(client, admin)
        out = client.get("/admin/models", headers=admin).json()
        assert out["source"] == "screen"
        assert out["active"] == {"provider": "scaleway", "label": "Scaleway", "region": "eu",
                                 "model": "llama-3.3-70b-instruct", "last_four": "abcd"}


class TestEachRegionHasOurWriter:
    """Decided on 2026-10-06: in either region the operator chooses nothing.
    Each region has one provider we chose and measured, and the first login
    goes straight to its key; the others are "another provider"."""

    def test_one_recommended_provider_per_region(self, client, admin):
        out = client.get("/admin/models", headers=admin).json()
        ours = {p["region"]: p["id"] for p in out["providers"] if p["recommended"]}
        assert ours == {"eu": "scaleway", "global": "google"}

    def test_their_writer_is_the_measured_one(self):
        for pid, model in (("scaleway", "gpt-oss-120b"), ("google", "gemini-3.1-flash-lite")):
            p = model_choice.PROVIDERS[pid]
            assert p.prefer[0] == model
            assert "129 of 152" in p.measured[model]

    def test_the_default_gemini_writer_is_the_measured_one(self):
        from korely_agent.config import AgentSettings

        assert AgentSettings.model_fields["extraction_model"].default == "gemini-3.1-flash-lite"


class TestNotStoredInTheClearNeverHandedBack:
    def test_the_row_holds_no_key(self, db, client, admin, providers):
        assert save(client, admin).status_code == 200
        row = db.get(InstallSetting, model_choice.ROW)
        assert SCW not in row.value_encrypted and row.value_encrypted.startswith("gAAAAA")

    def test_the_screen_gets_the_last_four(self, client, admin, providers):
        save(client, admin)
        r = client.get("/admin/models", headers=admin)
        assert SCW not in r.text
        scw = next(p for p in r.json()["providers"] if p["id"] == "scaleway")
        assert scw["key_set"] and scw["last_four"] == "abcd"


class TestCheckedBeforeItIsStored:
    def test_a_refused_key_stores_nothing(self, db, client, admin, providers):
        providers["state"]["refuse"] = True
        r = save(client, admin)
        assert r.status_code == 400 and "refused" in r.text
        assert db.get(InstallSetting, model_choice.ROW) is None

    def test_a_model_the_provider_does_not_list_is_refused(self, db, client, admin, providers):
        r = save(client, admin, model="claude-9")
        assert r.status_code == 400 and "claude-9" in r.text
        assert db.get(InstallSetting, model_choice.ROW) is None

    def test_an_empty_key_keeps_the_saved_one_and_checks_it_again(self, client, admin, providers):
        save(client, admin)
        providers["calls"].clear()
        assert save(client, admin, key="", model="gpt-oss-120b").status_code == 200
        assert providers["calls"] and all(
            c["headers"]["Authorization"] == f"Bearer {SCW}" for c in providers["calls"])


class TestTheModelList:
    def check(self, client, admin, provider, key="k-0000"):
        r = client.post(f"/admin/models/{provider}/check", headers=admin, json={"key": key})
        assert r.status_code == 200, r.text
        return r.json()

    def test_only_text_models_and_our_writer_preselected(self, client, admin, providers):
        out = self.check(client, admin, "scaleway")
        assert out["models"] == ["gpt-oss-120b", "llama-3.3-70b-instruct"]
        # gpt-oss-120b since 2026-10-06: the choice for Europe,
        # measured 129 of 152 on LoCoMo; llama is second.
        assert out["suggested"] == "gpt-oss-120b"
        out = self.check(client, admin, "openai")
        # o9-nova: a family no list here knows is shown (audit of 17 Oct
        # 2026: the ids not starting gpt-, o1, o3, o4 or chatgpt were
        # hidden, and refused by save).
        assert out["models"] == ["gpt-4.1-mini", "gpt-6-luna", "o9-nova"]
        assert out["suggested"] == "gpt-4.1-mini"

    def test_a_new_openai_family_can_be_saved(self, client, admin, providers):
        r = save(client, admin, provider="openai", key="sk-0000", model="o9-nova")
        assert r.status_code == 200, r.text
        assert r.json()["active"]["model"] == "o9-nova"

    def test_openrouter_most_used_first_without_free_or_stealth(self, client, admin, providers):
        out = self.check(client, admin, "openrouter")
        assert out["top"] == ["deepseek/deepseek-v4.1-flash", "openai/gpt-oss-120b",
                              "anthropic/claude-sonnet-5.5"]
        assert out["suggested"] == "openai/gpt-oss-120b:nitro"
        assert any(c["url"].endswith("/key") for c in providers["calls"])
        listed = next(c["url"] for c in providers["calls"] if "/models?" in c["url"])
        assert "sort=top-weekly" in listed and "output_modalities=text" in listed

    def test_claude_answers_with_its_own_headers_and_dated_ids(self, client, admin, providers):
        out = self.check(client, admin, "anthropic", key="sk-ant-1")
        assert out["suggested"] == "claude-haiku-4-5-20251001"
        h = providers["calls"][-1]["headers"]
        assert h["x-api-key"] == "sk-ant-1" and h["anthropic-version"] == "2023-06-01"

    def test_gemini_lists_generation_models_by_short_name(self, client, admin, providers):
        out = self.check(client, admin, "google", key="AIza-1")
        assert out["models"] == ["gemini-3.1-flash-lite"]
        assert providers["calls"][-1]["headers"] == {"x-goog-api-key": "AIza-1"}

    def test_check_uses_the_saved_key(self, client, admin, providers):
        assert client.post("/admin/models/scaleway/check", headers=admin).status_code == 404
        save(client, admin)
        r = client.post("/admin/models/scaleway/check", headers=admin)
        assert r.json()["ok"]

    def test_an_unknown_provider_is_a_404(self, client, admin):
        assert client.post("/admin/models/ovh/check", headers=admin, json={"key": "x"}).status_code == 404


class TestInForceWithoutARestart:
    def test_the_writer_chosen_here_writes(self, client, admin, providers):
        save(client, admin)
        p = llm.get_provider()
        assert p.name == "mistral" and p._base == "https://api.scaleway.ai/v1"
        assert p._force_model == "llama-3.3-70b-instruct"
        assert llm.provider_configured()

    def test_gemini_runs_on_its_own_client(self, client, admin, providers, monkeypatch):
        monkeypatch.setattr(llm, "_gemini_client", lambda key: ("client for", key))
        save(client, admin, provider="google", key="AIza-1", model="gemini-3.1-flash-lite")
        p = llm.get_provider()
        assert p.name == "gemini" and p._client == ("client for", "AIza-1")
        assert p._force_model == "gemini-3.1-flash-lite"

    def test_it_wins_over_a_model_name_left_in_env(self, client, admin, providers, monkeypatch):
        """The 422 of the EU bench: a Gemini VOCABULARY_MODEL sent by name to
        Scaleway. Chosen here, every write call goes to the chosen writer."""
        from korely_agent import config

        monkeypatch.setenv("VOCABULARY_MODEL", "gemini-3.6-flash")
        config.get_settings.cache_clear()
        save(client, admin)
        sent = {}

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}],
                                   "usage": {}}).encode()

        def urlopen(request, timeout=None):
            sent.update(json.loads(request.data))
            return Reply()

        monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
        llm.get_provider().complete([{"role": "user", "content": "hi"}],
                                    model=config.get_settings().vocabulary_model)
        assert sent["model"] == "llama-3.3-70b-instruct"

    def test_on_an_install_of_ones_own_the_provider_saved_last_is_the_default(
            self, client, admin, providers):
        """The rule of an install of one's own, unchanged on 5 Nov 2026 (the
        Cloud's default is Global, tests/test_hosted_writers_are_safe.py),
        and the page told it: `default_region`. Clearing it asks nothing."""
        out = save(client, admin, provider="google", key="AIza-1",
                   model="gemini-3.1-flash-lite").json()
        assert out["default_region"] == "global"
        out = save(client, admin).json()
        assert out["default_region"] == "eu" and llm.default_region() == "eu"
        r = client.delete("/admin/models", headers=admin)
        assert r.status_code == 200 and r.json()["deleted"] is True

    def test_clearing_gives_the_file_back(self, client, admin, providers):
        save(client, admin)
        assert llm.get_provider().name == "mistral"
        assert client.delete("/admin/models", headers=admin).json()["deleted"] is True
        p = llm.get_provider()
        assert p is None or p.name == "gemini"


class TestTheVectorsFollow:
    def test_gemini_chosen_at_first_login_makes_the_vectors_too(self, client, admin, providers, monkeypatch):
        from korely_agent import config

        monkeypatch.setenv("GOOGLE_API_KEY", "")
        monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
        config.get_settings.cache_clear()
        assert llm.current_google_key() is None
        assert client.get("/admin/models", headers=admin).json()["embeddings"]["ready"] is False
        save(client, admin, provider="google", key="AIza-1", model="gemini-3.1-flash-lite")
        assert llm.current_google_key() == "AIza-1"
        assert client.get("/admin/models", headers=admin).json()["embeddings"]["ready"] is True

    def test_another_provider_leaves_gemini_vectors_off_and_says_so(self, client, admin, providers, monkeypatch):
        from korely_agent import config

        monkeypatch.setenv("GOOGLE_API_KEY", "")
        monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
        config.get_settings.cache_clear()
        save(client, admin)
        assert llm.current_google_key() is None
        assert client.get("/admin/models", headers=admin).json()["embeddings"]["ready"] is False


class TestAnotherProcessSeesTheSave:
    """A save dropped the memo of core/llm.py only in the process that served
    it: a worker run apart (RUN_WORKER=false) or another replica kept the
    writer, the key and the default region it had read first until it
    restarted. Moving the install to Europe left that worker sending the
    memories of the projects that follow the install to the Global writer,
    and a removed writer went on writing (audit of 18 Oct 2026). Here
    the save comes from "another process": stored, with this process's memo
    left as it was, as the other process's save leaves it."""

    def _elsewhere(self, monkeypatch, do):
        real = llm.reset_provider_cache
        llm.reset_provider_cache = lambda: None
        try:
            do()
        finally:
            llm.reset_provider_cache = real

    def test_a_move_to_europe_and_a_removal_reach_the_worker(self, db, providers, monkeypatch):
        model_choice.save(db, "google", key="AIza-1", model="gemini-3.1-flash-lite")
        assert llm.default_region() == "global" and llm.get_provider().vendor == "google"

        self._elsewhere(monkeypatch, lambda: model_choice.save(
            db, "scaleway", key=SCW, model="llama-3.3-70b-instruct"))
        assert llm.default_region() == "global", "within RECHECK_SECONDS the memo serves"
        monkeypatch.setattr(llm, "_next_check", 0.0)
        assert llm.default_region() == "eu"
        assert llm.get_provider().vendor == "scaleway"
        assert llm.get_provider("global").vendor == "google", "each region keeps its own"

        self._elsewhere(monkeypatch, lambda: model_choice.clear(db))
        monkeypatch.setattr(llm, "_next_check", 0.0)
        assert llm.get_provider() is None and llm.get_provider("global") is None

    def test_a_key_pasted_elsewhere_reaches_a_worker_started_without_one(self, db, monkeypatch):
        """A worker started with no key memoized 'no key' and never mined a
        memory after the key was pasted on the dashboard."""
        from korely_agent.services import provider_key

        assert llm.current_google_key() is None
        self._elsewhere(monkeypatch, lambda: provider_key.set_google_key(db, "AIza-pasted"))
        monkeypatch.setattr(llm, "_next_check", 0.0)
        assert llm.current_google_key() == "AIza-pasted"
