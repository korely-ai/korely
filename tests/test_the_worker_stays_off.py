"""No test starts the background worker unless it asks for it.

The settings are read once per process and cached (config.get_settings), and
`create_app` reads them. Most fixtures set RUN_WORKER=false with monkeypatch
and build a TestClient, which is right only if nothing has cached the settings
yet: once any earlier test had, the cached `run_worker=True` won and the worker
started with the client. Its first drain then raced the rows the test had just
seeded (pending memories, queued batches, webhook deliveries), and 49 tests of
the suite ran with it on (counted 2026-09-28: test_memories_api 18,
test_mcp_scopes 17, test_operators 14).

conftest.py now gives every test fresh settings with RUN_WORKER=false. A test
that wants the worker sets RUN_WORKER=true and clears the cache, which is what
the second test here does.
"""


def _started(monkeypatch):
    """Build the app, open a client, and say whether the worker started."""
    from fastapi.testclient import TestClient
    from korely_agent import worker
    from korely_agent.main import create_app

    calls = []

    async def recorded(stop):
        calls.append(stop)

    monkeypatch.setattr(worker, "run_forever", recorded)
    app = create_app()
    with TestClient(app):
        pass
    return bool(calls)


def test_the_settings_a_test_starts_with_say_no_worker(db):
    from korely_agent.config import get_settings

    assert get_settings().run_worker is False


def test_a_client_built_after_the_settings_were_cached_starts_no_worker(db, monkeypatch):
    """The sequence that used to start it: something reads the settings
    first (any earlier fixture, any earlier test), then a fixture asks for no
    worker and builds a client."""
    from korely_agent.config import get_settings

    get_settings()
    monkeypatch.setenv("RUN_WORKER", "false")
    assert _started(monkeypatch) is False


def test_a_client_built_without_asking_starts_no_worker(db, monkeypatch):
    assert _started(monkeypatch) is False


def test_a_test_that_asks_for_it_gets_it(db, monkeypatch):
    from korely_agent.config import get_settings

    monkeypatch.setenv("RUN_WORKER", "true")
    get_settings.cache_clear()
    assert _started(monkeypatch) is True
