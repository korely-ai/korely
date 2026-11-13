"""The suite reaches no provider and no network (2026-09-28).

The development image installs `google-genai` with the rest of the product's
requirements, and the comments that said it did not were stale: a test that
set a key and left one model call real would have reached Google with it,
paid on every run and passed or failed on somebody else's uptime. Two walls: the
provider keys are emptied before the settings are read, and the network is
closed to anything but this machine. These tests are the walls' own.

No database: they run in `make test` too.
"""
import os
import socket

import pytest


class TestTheKeysAreEmpty:
    def test_in_the_environment(self):
        for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY"):
            assert os.environ.get(name) == "", f"{name} reaches the tests"

    def test_and_so_in_the_settings(self, monkeypatch):
        """An environment variable wins over a `.env` next to the code, which
        is where the settings look (the repository is the working directory
        in the test image)."""
        from korely_agent.config import AgentSettings

        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/k")
        s = AgentSettings()
        assert not (s.google_api_key or "").strip()
        assert not (s.mistral_api_key or "").strip()


class TestTheNetworkIsClosed:
    def test_a_lookup_is_refused(self):
        with pytest.raises(socket.gaierror, match="hermetic tests"):
            socket.getaddrinfo("generativelanguage.googleapis.com", 443)

    def test_a_connection_is_refused(self):
        with pytest.raises(OSError, match="hermetic tests"):
            socket.create_connection(("8.8.8.8", 53), timeout=1)

    def test_this_machine_is_open(self):
        """Postgres, the local webhook receivers and the TestClient live here."""
        assert socket.getaddrinfo("localhost", 5432)

    def test_a_real_embedding_call_with_a_key_set_fails_without_leaving(self, monkeypatch):
        """The case the stale comments made look impossible: a key set and
        `embed` left real, with the real Gemini client. It must not reach
        Google, and it must say it failed rather than hang or pay."""
        from types import SimpleNamespace

        from korely_agent.core import llm

        monkeypatch.setattr(llm, "current_google_key", lambda: "not-a-real-key")
        monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(
            embedding_model="gemini-embedding-001", embedding_provider="gemini",
            llm_timeout_seconds=5))
        llm.reset_provider_cache()
        saved = dict(llm._last_embedding)
        try:
            assert llm.embed("Maria lives in Rome.") is None
            # The vectors' own state since 2026-10-06 (core/llm._last_embedding).
            st = llm.embedding_state()
            assert st["state"] == "failing"
            # The record keeps the classes, never the message (audit of 18 Oct
            # 2026: a provider's error can quote the request): the network
            # the suite closed is the connection that failed.
            assert "ConnectError" in st["detail"] and "withheld" in st["detail"], st["detail"]
        finally:
            llm._last_embedding.update(saved)
            llm.reset_provider_cache()


@pytest.mark.real_network
def test_a_test_marked_real_network_is_skipped_unless_asked():
    """Runs only with RUN_REAL_NETWORK=1, and then the network is open."""
    assert os.environ.get("RUN_REAL_NETWORK") == "1"
