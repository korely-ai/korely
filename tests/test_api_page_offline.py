"""/docs works on a network with no way out (2026-10-06).

The page loaded Swagger UI's script and style from cdn.jsdelivr.net in the
browser: on a closed network, where a self-hosted install often runs, it
stayed blank. Swagger UI's own files are in the package now
(korely_agent/docs_ui, with their licence), and the page asks this machine
for them.
"""
import pytest
from fastapi.testclient import TestClient



@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c


def test_the_page_asks_this_machine_and_nobody_else(client):
    r = client.get("/docs")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    page = r.text
    assert "/docs-ui/swagger-ui-bundle.js" in page and "/docs-ui/swagger-ui.css" in page
    assert "/openapi.json" in page
    assert "cdn.jsdelivr.net" not in page and "fastapi.tiangolo.com" not in page


@pytest.mark.parametrize("name, kind", [
    ("swagger-ui-bundle.js", "javascript"),
    ("swagger-ui.css", "text/css"),
    ("favicon-32x32.png", "image/png"),
])
def test_its_files_are_served(client, name, kind):
    r = client.get(f"/docs-ui/{name}")
    assert r.status_code == 200, r.status_code
    assert kind in r.headers["content-type"]
    assert len(r.content) > 500


def test_the_licence_travels_with_them():
    from pathlib import Path

    import korely_agent

    here = Path(korely_agent.__file__).parent / "docs_ui"
    assert "Apache License" in (here / "LICENSE").read_text()
    assert "SmartBear" in (here / "NOTICE").read_text()


def test_the_contract_is_where_it_was(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200 and r.json()["info"]["title"] == "Korely Agent"
    assert "/docs" not in r.json()["paths"], "the page is not part of the API"


def test_there_is_no_second_page_that_asks_third_parties(client):
    """/redoc, FastAPI's default, loaded redoc@2 from jsDelivr, fonts from
    Google and an icon from fastapi.tiangolo.com (audit of 17 Oct 2026)."""
    r = client.get("/redoc")
    assert "cdn.jsdelivr.net" not in r.text and "fonts.googleapis.com" not in r.text
    assert "redoc" not in r.headers.get("content-type", "") and "<redoc" not in r.text.lower()
