"""Four defects from the parts of the product nobody had ever used.

Six rounds of blind testing covered memories, facts, projects, keys and `as_of`.
They never once touched webhooks, batch, the MCP surface, the provider screen,
key expiry or the profile: about two thirds of the endpoints. Going through that
part the same way, from outside, produced these in an hour.

Each test here was watched failing against the code as it was before the fix.
A test for a defect that has never been seen to go red is a test for nothing.
"""
import uuid

import pytest

from korely_agent.services import keys as key_service


@pytest.fixture
def client(db, monkeypatch):
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture
def agent_key(db, tenant):
    _row, plain = key_service.create_api_key(db, tenant_id=tenant.id, name="sweep")
    return {"Authorization": f"Bearer {plain}"}




@pytest.fixture
def client_with_the_screen(db, monkeypatch, tmp_path):
    """The app as it actually ships: with the dashboard built and mounted.

    This fixture is the test. Without the screen, Starlette has no route for
    `/mcp` at all and its own slash-redirect answers 307, so the defect does
    not reproduce and a test would pass while the product was broken. The screen
    is a catch-all for every path, it answers GET and only GET, and it is what
    turned a bare `/mcp` into 405. Every real install has it.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>screen</title>")
    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setenv("DASHBOARD_DIR", str(tmp_path))
    from korely_agent.config import get_settings
    get_settings.cache_clear()
    # The MCP session manager refuses to be started twice, and this is the
    # second app in the process. Dropping the module's singletons gives this
    # app its own manager; the next caller builds another one the same way.
    from korely_agent.mcp import server as mcp_server
    mcp_server._agent_mcp_singleton = None
    mcp_server._agent_streamable_app_singleton = None
    from korely_agent.db import get_db
    from korely_agent.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()
    mcp_server._agent_mcp_singleton = None
    mcp_server._agent_streamable_app_singleton = None


class TestTheAddressWeDocument:
    """`/mcp` is what our documentation says, so `/mcp` has to answer."""

    def test_the_mcp_surface_answers_without_the_trailing_slash(
            self, client_with_the_screen, agent_key):
        # 405 Method Not Allowed, with nothing in the body. Starlette's Mount
        # only matches a prefix followed by a slash, so the request never
        # reached the MCP app: it fell through to the screen's catch-all, which
        # answers GET and nothing else.
        c = client_with_the_screen
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}}
        head = {**agent_key, "Accept": "application/json, text/event-stream"}
        bare = c.post("/mcp", json=body, headers=head, follow_redirects=False)
        assert bare.status_code != 405, "the address in our own documentation answers 405"
        assert bare.status_code == 200, f"got {bare.status_code}: {bare.text[:200]}"
        assert "mcp-session-id" in {k.lower() for k in bare.headers}, \
            "answered, but not with a session: this is not the MCP surface"

    @pytest.mark.parametrize("address", ["/agent/mcp", "/agent/mcp/"])
    def test_the_hosted_products_address_is_the_same_server(
            self, client_with_the_screen, agent_key, address):
        """/agent/mcp is where the hosted product's clients and SDKs point (8
        Oct 2026, the one codebase): the same MCP surface, not the screen."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}}
        head = {**agent_key, "Accept": "application/json, text/event-stream"}
        r = client_with_the_screen.post(address, json=body, headers=head, follow_redirects=False)
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
        assert "mcp-session-id" in {k.lower() for k in r.headers}

    def test_the_metadata_the_401_points_at_is_there(self, client_with_the_screen):
        """A 401 names the resource's metadata at the root of the host (RFC
        9728); the document lived only inside the /mcp mount, so the pointer
        answered 404."""
        r = client_with_the_screen.post("/mcp", json={})
        assert r.status_code == 401
        pointer = r.headers["www-authenticate"].split('resource_metadata="', 1)[1].split('"', 1)[0]
        from urllib.parse import urlparse

        doc = client_with_the_screen.get(urlparse(pointer).path)
        assert doc.status_code == 200, doc.text[:200]
        assert doc.json()["resource"].endswith("/mcp")

    @pytest.mark.cloud
    def test_the_published_address_follows_the_mode(self, monkeypatch):
        from korely_agent import config
        from korely_agent.mcp import server as mcp_server

        monkeypatch.delenv("KORELY_MODE", raising=False)
        config.get_settings.cache_clear()
        assert mcp_server.published_path() == "/mcp"
        monkeypatch.setenv("KORELY_MODE", "hosted")
        config.get_settings.cache_clear()
        assert mcp_server.published_path() == "/agent/mcp", "the hosted domain keeps /mcp for another server"
        config.get_settings.cache_clear()

    def test_the_screen_is_really_there(self, client_with_the_screen):
        """The mirror: proves the fixture reproduces the condition that broke it."""
        assert "screen" in client_with_the_screen.get("/anything").text


class TestAWriteNeverSwallowsAFieldInSilence:
    """A field we do not know is an error. Dropping it is data loss with a 201."""

    def test_the_javascript_spelling_is_refused_and_named(self, client, agent_key):
        r = client.post("/v1/memories", headers=agent_key,
                        json={"content": "Anna lives in Turin.", "userId": "anna"})
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert any("userId" in str(e.get("msg", "")) for e in detail), detail
        assert any("user_id" in str(e.get("msg", "")) for e in detail), \
            "the error has to say which field they meant"

    def test_a_field_nobody_ever_defined_is_refused_too(self, client, agent_key):
        r = client.post("/v1/memories", headers=agent_key,
                        json={"content": "Bruno lives in Lecce.", "pippo": "x"})
        assert r.status_code == 422

    def test_the_error_still_does_not_read_the_body_back(self, client, agent_key):
        r = client.post("/v1/memories", headers=agent_key,
                        json={"content": "x", "api_key": "AQ.secret-value-here"})
        assert r.status_code == 422
        assert "secret-value-here" not in r.text

    def test_the_right_spelling_still_works(self, client, agent_key):
        r = client.post("/v1/memories", headers=agent_key,
                        json={"content": "Anna lives in Turin.", "user_id": "anna"})
        assert r.status_code == 201
        assert r.json()["user_id"] == "anna"


class TestAWebhookThatIsDeadSaysSo:
    """An endpoint that never works must not look like one that does."""

    def test_it_is_switched_off_after_failing_over_and_over(self, db, tenant, monkeypatch):
        """Switched off when a delivery has spent all its attempts (about three
        days on the schedule) and the endpoint accepted nothing since it was
        born (2026-10-06; it was 50 failures in a row)."""
        from korely_agent.db.models import AgentWebhookDelivery, AgentWebhookEndpoint
        from korely_agent.services import webhooks

        monkeypatch.setattr(webhooks, "_deliver", lambda *a, **k: (False, 503, "HTTP 503"))
        ep = AgentWebhookEndpoint(
            id=uuid.uuid4(), tenant_id=tenant.id, url="https://nowhere.example/x",
            secret="whsec_AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",  # bytes 0..31, gitleaks:allow
            events=["memory.created"], active=True)
        db.add(ep); db.commit()

        webhooks.emit_event(db, tenant_id=tenant.id, event_type="memory.created",
                            data={"id": "mem_1"})
        d = db.query(AgentWebhookDelivery).one()
        d.attempts = webhooks.ATTEMPTS - 1      # its last attempt is due
        db.commit()
        webhooks.process_deliveries(db, limit=5)
        db.refresh(ep)
        assert ep.active is False, "a webhook that only ever fails stays on forever"
