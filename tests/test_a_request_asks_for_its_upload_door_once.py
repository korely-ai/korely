"""A request asks once whether its path is an upload door (the code review
of 5 Nov 2026: main._BodyCap asked four times, each a walk of the parts the
mode serves, services/limits.upload_door)."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def counted(monkeypatch):
    from korely_agent.services import limits

    calls = []
    real = limits.upload_door

    def door(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(limits, "upload_door", door)
    return calls


def test_a_body_to_a_route_asks_once(counted, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.main import create_app

    with TestClient(create_app()) as c:
        counted.clear()
        c.post("/v1/memories", json={"content": "x"})
    assert len(counted) == 1, counted
