"""/health tells an address on the internet the states, never why (audit of
17 Oct 2026).

The shipped Caddyfile serves /health to anybody, with no key, and its
`detail`s are a provider's words ("429 ... prepayment credits are
depleted"), a database driver's (its host and port) and the paths of the
embedder's model. This machine, its containers and its own network (the
image's HEALTHCHECK, `curl 127.0.0.1:8000/health`) still get them.
"""
import json
from types import SimpleNamespace

import pytest

from korely_agent.core import llm
from korely_agent.main import from_the_internet


PROVIDER_WORDS = "ClientError: 429 RESOURCE_EXHAUSTED prepayment credits are depleted"


@pytest.fixture
def app(db, monkeypatch):
    from korely_agent.config import get_settings
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr(llm, "provider_configured", lambda: True)
    get_settings.cache_clear()
    llm._last_call.update({"ok": None, "at": None, "error": None})
    yield create_app()
    llm._last_call.update({"ok": None, "at": None, "error": None})
    get_settings.cache_clear()


def _health(app, address):
    from fastapi.testclient import TestClient

    with TestClient(app, client=(address, 40000)) as c:
        llm._record(False, PROVIDER_WORDS)
        r = c.get("/health")
    assert r.status_code == 200
    return r.json()


def test_the_internet_gets_the_states_and_not_the_words(app):
    body = _health(app, "8.8.8.8")
    assert body["status"] == "degraded"
    assert body["extraction"]["state"] == "failing"
    assert "detail" not in json.dumps(body["extraction"])
    assert "429" not in json.dumps(body) and "prepayment" not in json.dumps(body)
    assert body["jobs"] and all(set(j) <= {"state", "seconds_ago"} for j in body["jobs"].values())
    assert "withheld" in body["details"]
    assert {"version", "mode"} <= set(body)


def test_the_machine_and_its_network_get_why(app):
    for address in ("127.0.0.1", "172.18.0.1", "10.0.0.5", "::1"):
        body = _health(app, address)
        assert "429" in body["extraction"]["detail"], address
        assert "details" not in body


def test_a_store_that_cannot_be_read_says_so_without_its_host(app, monkeypatch):
    def down():
        raise RuntimeError("could not connect to server at db.internal:5432")

    monkeypatch.setattr("korely_agent.db.get_sessionmaker", down)
    body = _health(app, "8.8.8.8")
    assert body["database"] == {"state": "failing"}
    assert "db.internal" not in json.dumps(body)


@pytest.mark.parametrize("host,public", [
    ("8.8.8.8", True), ("2001:4860:4860::8888", True), ("::ffff:8.8.8.8", True),
    ("127.0.0.1", False), ("192.168.65.1", False), ("fd00::1", False),
    ("::ffff:10.0.0.1", False), (None, False), ("testclient", False),
])
def test_who_is_on_the_internet(host, public):
    request = SimpleNamespace(client=SimpleNamespace(host=host) if host else None)
    assert from_the_internet(request) is public


def test_the_dashboard_still_reads_what_it_starts_with(app):
    """The dashboard reads the mode, the key prefix and the MCP door from
    /health at start (ModeContext.loadMode), and its browser is an address on
    the internet: the two branches of the audit of 17 Oct 2026 met here, one
    withholding all but the states, one adding the two fields."""
    from korely_agent.mcp import server as mcp_server
    from korely_agent.services import keys as key_service

    body = _health(app, "8.8.8.8")
    assert body["key_prefix"] == key_service.minted_prefix()
    assert body["mcp_path"] == mcp_server.published_path()
