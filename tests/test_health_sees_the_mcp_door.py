"""/health sees the MCP door (audit of 17 Oct 2026).

A mount that failed, or a session manager that did not start, went to the
log alone: REST went on, /health said "ok" and the image's HEALTHCHECK
stayed green while every MCP client met a 404 or no session. Now `mcp`
says what the door did at start, and a door that did not open is
`degraded`.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def start(monkeypatch):
    from korely_agent.config import get_settings

    monkeypatch.setenv("RUN_WORKER", "false")
    get_settings.cache_clear()

    def go():
        from korely_agent.main import create_app

        return TestClient(create_app())

    yield go
    get_settings.cache_clear()


def _broken(*_a, **_k):
    raise RuntimeError("the transport could not be built")


def test_an_open_door_is_working(start):
    with start() as c:
        body = c.get("/health").json()
    assert body["mcp"] == {"state": "working"}


def test_a_door_that_did_not_mount_is_degraded(start, monkeypatch):
    from korely_agent.mcp import server

    monkeypatch.setattr(server, "mount_agent_mcp", _broken)
    with start() as c:
        body = c.get("/health").json()
    assert body["status"] == "degraded"
    assert body["mcp"]["state"] == "failing" and "did not mount" in body["mcp"]["detail"]
    # The exception's class, never its words (log_safe.error).
    assert "could not be built" not in body["mcp"]["detail"]


def test_a_session_manager_that_did_not_start_is_degraded(start, monkeypatch):
    from korely_agent.mcp import server

    monkeypatch.setattr(server, "agent_mcp_lifespan", _broken)
    with start() as c:
        body = c.get("/health").json()
    assert body["status"] == "degraded"
    assert body["mcp"]["state"] == "failing" and "did not start" in body["mcp"]["detail"]
