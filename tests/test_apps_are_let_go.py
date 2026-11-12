"""An app the tests are done with is let go (audit of 17 Oct 2026).

FastAPI keeps every endpoint function in a cache of the process
(fastapi.dependencies.models), so an endpoint that closes over its `app`
keeps that app alive for as long as the process runs. /docs did: each test
that built an app left about 3 MB behind, and a pytest process grew to
1.4-1.85 GB. No database: the app is built and asked for /docs without its
lifespan.
"""
import gc
import weakref

from fastapi.testclient import TestClient


def test_an_app_built_and_used_is_collected():
    from korely_agent.main import create_app

    built = []
    for _ in range(3):
        app = create_app()
        assert TestClient(app).get("/docs").status_code == 200
        built.append(weakref.ref(app))
        del app
    gc.collect()
    assert [r() is None for r in built] == [True, True, True], (
        "something of the process holds the apps: an endpoint closing over `app`?")
