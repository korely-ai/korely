"""One key cannot fill the MCP door for everybody (audit of 17 Oct 2026).

The tenant's limiter counted tool calls only, not the requests that open a
session, and nothing bounded the sessions one key held: a read-only key
opened 2,020 in 3.7 s, and at the SDK's 10,000 for the process every other
tenant got 503 on /mcp. Now an opening request counts like a tool call, and
a key holds at most MAX_SESSIONS_PER_KEY open. Real HTTP through the mounted
transport, as tests/test_mcp_identity_per_request.py."""
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import Tenant
from korely_agent.services import keys as key_service



@pytest.fixture
def make_client(db, monkeypatch):
    from korely_agent.config import get_settings
    from korely_agent.mcp import server as mcp_server

    monkeypatch.setenv("RUN_WORKER", "false")
    made = []

    def make(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        from korely_agent.db import get_db
        from korely_agent.main import create_app

        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        c = TestClient(app)
        c.__enter__()
        made.append(c)
        return c

    yield make
    for c in made:
        c.__exit__(None, None, None)
    get_settings.cache_clear()
    mcp_server._agent_mcp_singleton = None
    mcp_server._agent_streamable_app_singleton = None


def _key(db, scopes=("memories:read",)):
    t = Tenant(id=uuid.uuid4(), name=f"t-{uuid.uuid4().hex[:6]}")
    db.add(t)
    db.commit()
    _r, full = key_service.create_api_key(db, tenant_id=t.id, name="k", scopes=list(scopes))
    return full


def _initialize(c, key):
    return c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                            "clientInfo": {"name": "t", "version": "1"}}},
                  headers={"Authorization": f"Bearer {key}",
                           "Accept": "application/json, text/event-stream"})


def test_one_key_holds_a_bounded_number_of_sessions(make_client, db, monkeypatch):
    from korely_agent.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "MAX_SESSIONS_PER_KEY", 3)
    c = make_client()
    a, b = _key(db), _key(db)
    assert [_initialize(c, a).status_code for _ in range(3)] == [200, 200, 200]
    r = _initialize(c, a)
    assert r.status_code == 429 and r.json()["code"] == "rate_limit_exceeded"
    assert int(r.headers["retry-after"]) > 0
    assert _initialize(c, b).status_code == 200, "another tenant was shut out"


def test_an_opening_request_counts_in_the_tenants_limiter(make_client, db):
    c = make_client(RATE_LIMIT_PER_MINUTE="2")
    a = _key(db)
    codes = [_initialize(c, a).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_a_request_without_a_key_still_meets_the_sdks_401(make_client):
    c = make_client()
    r = c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
               headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401


# ── the audit of 18 Oct 2026: the ceilings are decided where the SDK
# admits a session, on the identity it authenticated ─────────────────────────

def _owned(key=None):
    from korely_agent.mcp import server as mcp_server

    owners = mcp_server._get_agent_mcp().session_manager._session_owners.values()
    return len(list(owners))


def _two_keys_of_one_tenant(db):
    t = Tenant(id=uuid.uuid4(), name=f"t-{uuid.uuid4().hex[:6]}")
    db.add(t)
    db.commit()
    return [key_service.create_api_key(db, tenant_id=t.id, name=f"k{i}",
                                       scopes=["memories:read"])[1] for i in range(2)]


def test_two_authorization_headers_are_a_400(make_client, db, monkeypatch):
    """The ceiling read the last of two headers and the SDK authenticated the
    first: a valid key first and anything after it opened sessions uncounted
    and unbounded."""
    from korely_agent.services import rate_limit

    counted = []
    real = rate_limit.check_and_increment
    monkeypatch.setattr(rate_limit, "check_and_increment",
                        lambda *a, **k: counted.append(1) or real(*a, **k))
    c = make_client()
    a = _key(db)
    r = c.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
               headers=[("Authorization", f"Bearer {a}"), ("Authorization", "Bearer x"),
                        ("Accept", "application/json, text/event-stream")])
    assert r.status_code == 400 and r.json()["code"] == "bad_request"
    assert _owned() == 0 and counted == []


def test_a_burst_of_one_keys_openings_stops_at_its_ceiling(make_client, db, monkeypatch):
    """The count was read across awaits before the SDK registered anything,
    so concurrent openings of one key all passed it."""
    from concurrent.futures import ThreadPoolExecutor

    from korely_agent.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "MAX_SESSIONS_PER_KEY", 2)
    c = make_client()
    a = _key(db)
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = sorted(pool.map(lambda _: _initialize(c, a).status_code, range(8)))
    assert codes == [200, 200] + [429] * 6
    assert _owned() == 2


def test_the_keys_of_one_tenant_share_a_ceiling(make_client, db, monkeypatch):
    """An account mints keys without a count: the ceiling per key alone let
    one tenant fill the process with enough of them."""
    from korely_agent.mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "MAX_SESSIONS_PER_KEY", 2)
    monkeypatch.setattr(mcp_server, "MAX_SESSIONS_PER_TENANT", 3)
    c = make_client()
    first, second = _two_keys_of_one_tenant(db)
    assert [_initialize(c, first).status_code for _ in range(2)] == [200, 200]
    assert _initialize(c, second).status_code == 200
    r = _initialize(c, second)
    assert r.status_code == 429 and r.json()["code"] == "rate_limit_exceeded"
    assert "account" in r.json()["message"]
    assert _initialize(c, _key(db)).status_code == 200, "another tenant was shut out"


def test_a_session_is_still_bound_to_the_key_that_opened_it(make_client, db):
    """The tenant is the subject now; the key is still the client, so another
    key of the same account cannot use a session it did not open."""
    c = make_client()
    first, second = _two_keys_of_one_tenant(db)
    r = _initialize(c, first)
    sid = r.headers["mcp-session-id"]
    r = c.post("/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
               headers={"Authorization": f"Bearer {second}", "mcp-session-id": sid,
                        "Accept": "application/json, text/event-stream"})
    assert r.status_code == 404


# ── what a session keeps of its opening (audit before 1.0.0) ────────────────
# An MCP message was decoded
# unweighed (main._BodyCap left the door out of the count), and the SDK's
# session kept the opening's params whole, with every member nobody declared
# (its models allow any): an `initialize` of 1 MiB kept some 49 MiB for the
# session's 30 idle minutes, and a free key's handful of sessions held more
# than the API's memory has to spare.

def _opening(c, key, params, sid=None, kind="application/json"):
    head = {"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream",
            "content-type": kind}
    if sid:
        head["mcp-session-id"] = sid
    import json

    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
    return c.post("/mcp/", content=json.dumps(body).encode(), headers=head)


_LAWFUL = {"protocolVersion": "2025-06-18", "capabilities": {},
           "clientInfo": {"name": "t", "version": "1"}}


def _sessions_named(name):
    import gc

    from mcp.server.session import ServerSession

    return [o for o in gc.get_objects() if isinstance(o, ServerSession)
            and o._client_params is not None and o._client_params.clientInfo.name == name]


def test_a_dense_opening_is_refused_before_it_is_decoded(make_client, db):
    """The finding's own: an undeclared member of a million `{}`, refused
    413 by the count every other door's body meets."""
    c = make_client()
    r = _opening(c, _key(db), {**_LAWFUL, "zz": [{}] * 350_000})
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large", r.text[:300]
    assert _owned() == 0


@pytest.mark.parametrize("kind", ["application/json", "application/json , text/html"])
def test_a_dense_tool_call_is_refused_before_it_is_decoded(make_client, db, kind):
    """The same count for a message on an open session: a tools/call whose
    `_meta` is dense (the rule of the arguments walks only `arguments`).
    Whatever the spelling of its type: the SDK takes any comma-separated
    part that is JSON, and `application/json , text/html` passed unweighed,
    the tool ran (the review of this fix)."""
    import json

    c = make_client()
    key = _key(db)
    r = _opening(c, key, _LAWFUL)
    assert r.status_code == 200, r.text[:300]
    sid = r.headers["mcp-session-id"]
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "korely_search", "arguments": {"query": "x"},
                       "_meta": {"zz": [{}] * 350_000}}}
    r = c.post("/mcp/", content=json.dumps(body).encode(),
               headers={"Authorization": f"Bearer {key}", "mcp-session-id": sid,
                        "Accept": "application/json, text/event-stream",
                        "content-type": kind})
    assert r.status_code == 413, r.text[:300]


def test_an_undeclared_member_of_the_opening_is_dropped_not_refused(make_client, db):
    """A later revision of the protocol, or a client's own extension, adds
    members to `initialize`: the session opens and keeps none of them (a
    422 refused such a client, and bounded nothing the slimming did not;
    the review of this fix)."""
    c = make_client()
    name = "extended-" + uuid.uuid4().hex[:6]
    r = _opening(c, _key(db), {**_LAWFUL, "clientInfo": {"name": name, "version": "1"},
                               "extensions": {"io.example/x": {}}, "zz": [{}] * 10})
    assert r.status_code == 200, r.text[:300]
    (session,) = _sessions_named(name)
    assert not session._client_params.model_extra


def test_a_client_name_past_an_ids_length_is_refused(make_client, db):
    """The strings the session keeps whole are bounded as an id is."""
    c = make_client()
    r = _opening(c, _key(db), {**_LAWFUL, "clientInfo": {"name": "n" * 300, "version": "1"}})
    assert r.status_code == 422 and r.json()["code"] == "invalid_request", r.text[:300]
    assert "clientInfo.name" in r.json()["message"]
    assert _owned() == 0


def test_only_a_body_the_sdk_decodes_is_decoded_before_it():
    """mcp/server._messages_read decodes a POST only when the SDK would
    (any comma-separated part of its type `application/json`); any other
    reaches the SDK as sent, for its 415."""
    import asyncio
    import json

    from korely_agent.mcp.server import _messages_read

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {**_LAWFUL, "capabilities": {"experimental": {"x": {}}}}}).encode()

    def given(kind):
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        scope = {"type": "http", "method": "POST", "headers": [(b"content-type", kind)]}

        async def go():
            return (await (await _messages_read(scope, receive))())["body"]
        return asyncio.run(go())

    assert given(b"text/plain") == body
    assert given(b"application/json;charset=utf-8") != body
    assert given(b"text/html, application/json") != body


def test_a_session_keeps_only_what_is_read_of_its_opening(make_client, db):
    """What the SDK declares free (capabilities.experimental, clientInfo's
    icons, `_meta`) passes the rule, but the session keeps only what this
    server reads of it: the client's name and version, the protocol's, and
    which capabilities it named."""
    c = make_client()
    name = "dense-opening-" + uuid.uuid4().hex[:6]
    params = {"protocolVersion": "2025-06-18",
              "capabilities": {"roots": {"listChanged": True}, "sampling": {},
                               "experimental": {"x": {"zz": [{}] * 5000}}},
              "clientInfo": {"name": name, "version": "1",
                             "icons": [{"src": "https://example.com/i.png"}] * 500},
              "_meta": {"zz": [{}] * 2000}}
    r = _opening(c, _key(db), params)
    assert r.status_code == 200, r.text[:300]
    (session,) = _sessions_named(name)
    kept = session._client_params
    assert kept.capabilities.experimental is None and kept.clientInfo.icons is None
    assert kept.meta is None and not kept.model_extra
    assert kept.capabilities.roots is not None and kept.capabilities.sampling is not None
    assert len(kept.model_dump_json()) < 1024, kept.model_dump_json()[:300]


def test_a_second_initialize_on_a_session_is_read_the_same(make_client, db):
    """The SDK keeps the params of an `initialize` sent on an open session
    too, in place of the first's."""
    c = make_client()
    key = _key(db)
    name = "again-" + uuid.uuid4().hex[:6]
    r = _opening(c, key, {**_LAWFUL, "clientInfo": {"name": name, "version": "1"}})
    sid = r.headers["mcp-session-id"]
    r = _opening(c, key, {**_LAWFUL, "clientInfo": {"name": name, "version": "1"},
                          "capabilities": {"experimental": {"x": {"zz": [{}] * 5000}}}}, sid)
    assert r.status_code == 200, r.text[:300]
    (session,) = _sessions_named(name)
    assert session._client_params.capabilities.experimental is None


# ── the SDK's own reading of the type, and of the bytes (audit before 1.0.0) ─

def _kinds():
    """Spellings of a request's Content-Type: case, spaces, parameters,
    lists, +json, empty, missing, and two headers."""
    one = [b"application/json", b"Application/JSON", b"APPLICATION/JSON", b" application/json ",
           b"application/json; charset=utf-8", b"application/json;charset=UTF-8",
           b"application/json , text/html", b"text/html, application/json",
           b"text/html,application/json;q=1", b"text/plain; application/json",
           b"application/vnd.api+json", b"application/json-patch+json", b"application/jsonx",
           b"text/plain", b"", b"application/x-www-form-urlencoded"]
    yield from ([(b"content-type", k)] for k in one)
    yield []
    yield [(b"content-type", b"text/plain"), (b"content-type", b"application/json")]
    yield [(b"content-type", b"application/json"), (b"content-type", b"text/plain")]


def test_the_type_is_read_as_the_sdk_reads_it():
    """mcp/server._json_to_the_sdk copies the SDK's check: an upgrade that
    reads the type otherwise fails here, before an initialize it takes as
    JSON reaches it unread."""
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    from starlette.requests import Request

    from korely_agent.mcp.server import _json_to_the_sdk

    sdk = object.__new__(StreamableHTTPServerTransport)
    for headers in _kinds():
        scope = {"type": "http", "method": "POST", "path": "/mcp/", "headers": headers}
        assert _json_to_the_sdk(scope) == sdk._check_content_type(Request(scope)), headers


def test_a_body_not_in_utf8_is_a_400(make_client, db):
    """Typed JSON and not UTF-8 (a gzip body): the SDK answered 500 (-32603)."""
    import gzip
    import json

    c = make_client()
    body = gzip.compress(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": _LAWFUL}).encode())
    r = c.post("/mcp/", content=body,
               headers={"Authorization": f"Bearer {_key(db)}", "content-type": "application/json",
                        "Accept": "application/json, text/event-stream"})
    assert r.status_code == 400 and r.json()["code"] == "bad_request", r.text[:300]
    assert "UTF-8" in r.json()["message"]
    assert _owned() == 0
