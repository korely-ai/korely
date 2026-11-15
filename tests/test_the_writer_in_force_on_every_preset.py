"""/health and the drain tell the writer in force the same way on every
preset of an install of one's own (7 Nov 2026).

A failure belongs to the writer that made the call (`Provider.writer`), and
it is forgotten once that writer is seen replaced (`core/llm.replaced`).
Whether it was replaced is told from what the writer is built from
(`_what_writes`), and it cannot be told only when a setting it is built
from could not be had: the writers of the Models page, or the Google key of
the Gemini writer of `.env`. The sceptic found that /health asked for the
Google key on every preset: with the European preset in `.env`
(EXTRACTION_PROVIDER=mistral, Scaleway, vectors computed locally) nothing
ever reads that key, and /health kept `degraded, failing 402` after a writer
was saved in place of the one that failed, while the drain, which asked
`writer_identity`, had already forgotten its rest.

No model is called and no key is real: the provider's check is faked and
every call is answered here.
"""
import io
import time
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from korely_agent.config import get_settings
from korely_agent.core import llm
from korely_agent.main import create_app
from korely_agent.services import model_choice, provider_key
from korely_agent.services.facts import drain

EU_PRESET = {"EXTRACTION_PROVIDER": "mistral", "MISTRAL_API_KEY": "scw-file-0123456789",
             "MISTRAL_API_BASE": "https://api.scaleway.ai/v1", "GOOGLE_API_KEY": ""}
US_PRESET = {"EXTRACTION_PROVIDER": "gemini", "GOOGLE_API_KEY": "AIza-file-0123456789",
             "MISTRAL_API_KEY": ""}
PAGE_KEY = "scw-page-0123456789"
OTHER_MODEL = "mistral-small-3.2-24b-instruct-2506"


@pytest.fixture
def install(db, monkeypatch):
    """`install(preset)`: a client of an install of one's own whose `.env`
    says `preset`, the vectors computed locally; the provider's check faked,
    the gate and the bill out of the way, the last call and the rests empty."""
    monkeypatch.setenv("KORELY_MODE", "self_hosted")
    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-tests")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    monkeypatch.setattr(model_choice, "check", lambda provider, key: {
        "ok": True, "detail": "", "models": ["gpt-oss-120b", OTHER_MODEL, "gemini-3.1-flash-lite"],
        "top": [], "suggested": None})
    monkeypatch.setattr(llm, "_before_the_call", lambda *a: None)
    monkeypatch.setattr(llm, "_bill", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_last_call", {"ok": None, "at": None, "error": None})
    monkeypatch.setattr(drain, "_pauses", {})
    # The vectors are not what is told here, and the test image has no local
    # model: its warm-up at startup would turn `status` degraded whenever it
    # ends before a /health.
    from korely_agent.core import local_embedder

    monkeypatch.setattr(local_embedder, "warm_up", lambda: None)
    monkeypatch.setattr(local_embedder, "_last", {"ok": None, "at": None, "error": None})
    clients = []

    def start(preset):
        for name, value in preset.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        llm.reset_provider_cache()
        c = TestClient(create_app())
        c.__enter__()
        clients.append(c)
        return c

    yield start
    for c in clients:
        c.__exit__(None, None, None)
    llm.reset_provider_cache()
    get_settings.cache_clear()


def refused_at_the_door(monkeypatch, code=402):
    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "refused", {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


class _Refused402(Exception):
    code = 402


def refused_by_gemini(provider):
    from types import SimpleNamespace

    def refuse(**kw):
        raise _Refused402("credit")
    provider._client = SimpleNamespace(models=SimpleNamespace(generate_content=refuse))


def call(provider):
    with pytest.raises(Exception):
        provider.complete([{"role": "user", "content": "x"}])


def health(c):
    s = c.get("/health").json()
    return s["status"], s["extraction"]["state"]


# ── the European preset: EXTRACTION_PROVIDER=mistral, nothing reads a Google key

def test_eu_preset_a_writer_saved_on_the_page_in_place_of_the_env_one(db, install, monkeypatch):
    c = install(EU_PRESET)
    old = llm.get_provider()
    assert type(old).__name__ == "MistralProvider" and old.region == "eu"
    refused_at_the_door(monkeypatch)
    call(old)
    assert health(c) == ("degraded", "failing")
    model_choice.save(db, "scaleway", key=PAGE_KEY, model="gpt-oss-120b")
    assert health(c) == ("ok", "unknown")
    # the drain says the same of the writer that refused: one rule
    assert drain._no_longer_in_force("eu", old.writer)
    # the writer saved now is the one in force: its failure is degraded
    with llm.writing_in("eu"):
        page = llm.get_provider()
    assert page.writer != old.writer
    call(page)
    assert health(c) == ("degraded", "failing")
    assert not drain._no_longer_in_force("eu", page.writer)
    # another key for the page's writer replaces it in turn
    model_choice.save(db, "scaleway", key=PAGE_KEY + "-new", model="gpt-oss-120b")
    assert health(c) == ("ok", "unknown")
    # and so does another model with the same key
    with llm.writing_in("eu"):
        call(llm.get_provider())
    assert health(c) == ("degraded", "failing")
    model_choice.save(db, "scaleway", key=PAGE_KEY + "-new", model=OTHER_MODEL)
    assert health(c) == ("ok", "unknown")


def test_eu_preset_the_env_writer_still_in_force_keeps_its_failure(db, install, monkeypatch):
    c = install(EU_PRESET)
    old = llm.get_provider()
    refused_at_the_door(monkeypatch)
    call(old)
    # a writer for Global is no writer of Europe
    model_choice.save(db, "google", key="AIza-page-0123456789", model="gemini-3.1-flash-lite")
    assert health(c) == ("degraded", "failing")
    assert not drain._no_longer_in_force("eu", old.writer)


def test_eu_preset_a_rest_of_the_env_writer_ends_with_a_writer_saved_in_its_place(
        db, install, monkeypatch):
    c = install(EU_PRESET)
    old = llm.get_provider()
    drain._provider_refused("eu", 402)
    assert c.get("/health").json()["writers"]["eu"]["state"] == "refused"
    model_choice.save(db, "scaleway", key=PAGE_KEY, model="gpt-oss-120b")
    assert "writers" not in c.get("/health").json()
    assert drain._drain_paused("eu") == 0 and old.writer is not None


# ── the US preset: the writer of `.env` is Gemini, its Google key counts ─────

def test_us_preset_a_new_google_key_on_the_key_screen(db, install, monkeypatch):
    c = install(US_PRESET)
    old = llm.get_provider()
    assert type(old).__name__ == "GeminiProvider" and old.region == "global"
    refused_by_gemini(old)
    call(old)
    assert health(c) == ("degraded", "failing")
    provider_key.set_google_key(db, "AIza-saved-0123456789")
    assert health(c) == ("ok", "unknown")
    assert drain._no_longer_in_force("global", old.writer)
    # the writer of the new key fails, then one is saved on the Models page
    keyed = llm.get_provider()
    assert keyed.writer != old.writer
    refused_by_gemini(keyed)
    call(keyed)
    assert health(c) == ("degraded", "failing")
    model_choice.save(db, "google", key="AIza-page-0123456789", model="gemini-3.1-flash-lite")
    assert health(c) == ("ok", "unknown")


def test_us_preset_a_google_key_that_cannot_be_read_replaces_nothing(db, install, monkeypatch):
    """The key saved on the key screen writes; then the database will not
    give it. The writer in force cannot be told, so neither /health's last
    call nor the drain's rest is forgotten: before, the drain told the
    writer from the key of `.env` (what the key's read falls back to) and
    dropped the rest of the writer still in force."""
    c = install(US_PRESET)
    provider_key.set_google_key(db, "AIza-saved-0123456789")
    old = llm.get_provider()
    refused_by_gemini(old)
    call(old)
    drain._provider_refused("global", 402)
    assert health(c) == ("degraded", "failing")

    def down(db):
        raise RuntimeError("database down")
    monkeypatch.setattr(provider_key, "get_google_key", down)
    llm.reset_provider_cache()
    s = c.get("/health").json()
    assert s["extraction"]["state"] == "failing" and s["status"] == "degraded"
    assert s["writers"]["global"]["state"] == "refused"
    assert drain._drain_paused("global") > 0
    assert not drain._no_longer_in_force("global", old.writer)
    with pytest.raises(llm.WritersUnread):
        llm.writer_identity("global")


def ten_seconds_pass(region):
    """What RECHECK_SECONDS later looks like to the writer kept for `region`
    (the tests of `_saved_elsewhere` move `_next_check` the same way)."""
    llm._kept_until[region] = 0.0


def test_a_writer_built_while_the_writers_cannot_be_read_is_kept_ten_seconds(
        db, install, monkeypatch):
    """The writers of the Models page cannot be read at the first call after
    a save: the writer of `.env` serves, and it is kept RECHECK_SECONDS, so
    the calls of a fault pay its failed reads once (one build) and not once
    each; then the writers are read again, and the page's writer writes once
    they can be (up to 6f212429 the writer of `.env` was kept until the next
    save or restart; at 0d2e8552 it was built again at every call)."""
    install(EU_PRESET)
    model_choice.save(db, "scaleway", key=PAGE_KEY, model="gpt-oss-120b")
    real = model_choice.read_writers
    reads = {"n": 0}

    def down():
        reads["n"] += 1
        raise RuntimeError("database down")
    monkeypatch.setattr(model_choice, "read_writers", down)
    llm.reset_provider_cache()
    meanwhile = llm.get_provider("eu")
    assert meanwhile is not None and meanwhile._key == EU_PRESET["MISTRAL_API_KEY"]
    one_build = reads["n"]
    assert one_build >= 1
    assert all(llm.get_provider("eu") is meanwhile for _ in range(4))
    assert reads["n"] == one_build, "five calls within ten seconds read once"
    assert 0 < llm._kept_until["eu"] - time.monotonic() <= llm.RECHECK_SECONDS
    # the database answers again: still the writer of `.env` until the ten seconds pass
    monkeypatch.setattr(model_choice, "read_writers", real)
    assert llm.get_provider("eu") is meanwhile
    ten_seconds_pass("eu")
    again = llm.get_provider("eu")
    assert again._key == PAGE_KEY and again.writer == llm.writer_identity("eu")
    assert "eu" not in llm._kept_until and llm.get_provider("eu") is again   # kept for good


def test_after_ten_seconds_of_a_fault_the_writers_are_read_again(db, install, monkeypatch):
    install(EU_PRESET)
    model_choice.save(db, "scaleway", key=PAGE_KEY, model="gpt-oss-120b")
    reads = {"n": 0}

    def down():
        reads["n"] += 1
        raise RuntimeError("database down")
    monkeypatch.setattr(model_choice, "read_writers", down)
    llm.reset_provider_cache()
    first = llm.get_provider("eu")
    one_build = reads["n"]
    ten_seconds_pass("eu")
    second = llm.get_provider("eu")   # still down: read again, the writer of `.env` again
    assert reads["n"] == 2 * one_build and second is not first
    assert second._key == EU_PRESET["MISTRAL_API_KEY"]
    assert llm.get_provider("eu") is second and reads["n"] == 2 * one_build


def test_a_writer_built_while_the_saved_google_key_cannot_be_read_is_kept_ten_seconds(
        db, install, monkeypatch):
    """The same with the key of the key screen, for the Gemini writer of
    `.env`: the key of `.env` serves the calls of the fault, its failed
    reads paid once in ten seconds, and the saved key writes once it can be
    read after them."""
    install(US_PRESET)
    provider_key.set_google_key(db, "AIza-saved-0123456789")
    real = provider_key.get_google_key
    reads = {"n": 0}

    def down(db):
        reads["n"] += 1
        raise RuntimeError("database down")
    monkeypatch.setattr(provider_key, "get_google_key", down)
    llm.reset_provider_cache()
    meanwhile = llm.get_provider("global")
    assert meanwhile is not None
    one_build = reads["n"]
    assert all(llm.get_provider("global") is meanwhile for _ in range(4))
    assert reads["n"] == one_build
    monkeypatch.setattr(provider_key, "get_google_key", real)
    ten_seconds_pass("global")
    again = llm.get_provider("global")
    assert again is not meanwhile and again.writer == llm.writer_identity("global")
    assert again.writer != meanwhile.writer


def test_us_preset_the_last_call_is_told_without_reading_the_key(db, install, monkeypatch):
    """Told from memory: with the writers in memory and the key not, the
    key is not read to judge the last call, which stands; /health reads
    the key first (`provider_configured`) and then tells."""
    c = install(US_PRESET)
    old = llm.get_provider()
    refused_by_gemini(old)
    call(old)
    provider_key.set_google_key(db, "AIza-saved-0123456789")   # the memo is dropped
    reads = {"n": 0}
    real = provider_key.get_google_key

    def counted(db):
        reads["n"] += 1
        return real(db)
    monkeypatch.setattr(provider_key, "get_google_key", counted)
    llm.screen_writers()   # the writers in memory, the key not
    assert llm.provider_state()["state"] == "failing"
    assert reads["n"] == 0
    assert health(c) == ("ok", "unknown")
    assert reads["n"] >= 1
