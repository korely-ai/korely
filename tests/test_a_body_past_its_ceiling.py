"""No request body past its route's ceiling is read, and none before the key
(audit of 17 Oct 2026): every JSON body was read and decoded whole before the
key was checked, with no ceiling in the app, uvicorn or Caddy, so a request
without a key could fill the memory of the one process serving every tenant.
main._BodyCap answers 413 `payload_too_large` first."""
import pytest
from fastapi.testclient import TestClient

from korely_agent.services import limits
from tests import cloud


@pytest.fixture
def client(monkeypatch):
    from korely_agent import config
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    config.get_settings.cache_clear()
    asked = []
    from korely_agent.services import keys

    real = keys.verify_api_key
    monkeypatch.setattr(keys, "verify_api_key", lambda *a, **k: asked.append(1) or real(*a, **k))
    with TestClient(create_app()) as c:
        c.asked = asked
        yield c
    config.get_settings.cache_clear()


def _big(n):
    return b'{"content": "' + b"a" * n + b'", "user_id": "maria"}'


def test_a_declared_length_past_the_ceiling_is_refused_unread(client):
    r = client.post("/v1/memories", content=_big(limits.MAX_BODY_BYTES + 1),
                    headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"
    assert client.asked == [], "the key was looked up before the body was refused"


def test_a_chunked_body_is_cut_at_the_first_piece_past_it(client):
    piece = b"a" * 65536

    def chunks():
        yield b'{"content": "'
        for _ in range(limits.MAX_BODY_BYTES // len(piece) + 2):
            yield piece
        yield b'"}'

    r = client.post("/v1/memories", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


def test_a_body_under_it_reaches_the_route(client):
    r = client.post("/v1/memories", content=_big(1000), headers={"content-type": "application/json"})
    assert r.status_code == 401  # the key's answer: the body went through


def test_the_batch_takes_its_most_items_at_their_largest():
    assert limits.body_cap("/v1/batch", writer=True) >= 500 * limits.MAX_CONTENT_CHARS * 6
    assert limits.body_cap("/v1/memories") == limits.MAX_BODY_BYTES
    assert limits.body_cap("/v1/memories", writer=True) == limits.MAX_BODY_BYTES
    assert limits.body_cap("/mcp") == limits.MAX_MCP_BODY_BYTES


# ── the batch's ceiling is a write key's, never anybody's (audit of 18 Oct 2026) ─
# FastAPI reads and decodes a body before the route's key is checked: with the
# batch's ceiling given by the path alone, a request with no key had 78 MB
# decoded, at some 26 times its bytes, before its 401.

_PAST_THE_COMMON = limits.MAX_BODY_BYTES + 1024


def _batch_body(n=_PAST_THE_COMMON):
    # Past the content's limit, so a body that reaches the route is a 422.
    return b'{"memories": [{"content": "' + b"a" * n + b'"}]}'


def _batch(client, key=None, content=None):
    headers = {"content-type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return client.post("/v1/batch", content=content if content is not None else _batch_body(),
                       headers=headers)


def _a_key(db, scopes):
    import uuid

    from korely_agent.db.models import Tenant
    from korely_agent.services import keys

    t = Tenant(id=uuid.uuid4(), name="t")
    db.add(t)
    db.commit()
    return keys.create_api_key(db, tenant_id=t.id, name="k", scopes=list(scopes))[1]


def test_a_large_batch_without_a_key_is_refused_unread(client):
    r = _batch(client)
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"
    assert f"{limits.MAX_BODY_BYTES:,}" in r.json()["message"]
    assert client.asked == []


def test_a_large_batch_with_a_key_that_cannot_write_is_refused_unread(client, db):
    assert _batch(client, "kor_self_" + "0" * 43).status_code == 413
    assert _batch(client, _a_key(db, ["memories:read"])).status_code == 413


def test_a_large_batch_of_a_write_key_reaches_the_route(client, db):
    r = _batch(client, _a_key(db, ["memories:read", "memories:write"]))
    assert r.status_code == 422, r.text  # the route's answer: the body went through


def test_a_chunked_large_batch_without_a_key_is_cut_at_the_common_ceiling(client):
    piece = b"a" * 65536

    def chunks():
        yield b'{"memories": [{"content": "'
        for _ in range(limits.MAX_BODY_BYTES // len(piece) + 2):
            yield piece
        yield b'"}]}'

    r = _batch(client, content=chunks())
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


def test_the_large_bodies_are_decoded_a_few_at_a_time(monkeypatch):
    """A write key costs nothing on the Cloud: its large bodies wait for one
    of LARGE_BODIES_AT_ONCE places instead of piling up in the one process."""
    import anyio

    from korely_agent import main

    accounts = iter(range(1000))

    async def may_write(scope):
        return f"account-{next(accounts)}"  # one account each: the process's places

    monkeypatch.setattr(main, "_writer_account", may_write)
    inside, most = 0, 0

    async def app(scope, receive, send):
        nonlocal inside, most
        inside += 1
        most = max(most, inside)
        await anyio.sleep(0.05)
        inside -= 1

    cap = main._BodyCap(app)
    body = _batch_body()
    scope = {"type": "http", "path": "/v1/batch", "method": "POST",
             "headers": [(b"content-length", str(len(body)).encode())]}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def run():
        async with anyio.create_task_group() as tg:
            for _ in range(limits.LARGE_BODIES_AT_ONCE + 3):
                tg.start_soon(cap, scope, receive, None)

    anyio.run(run)
    assert most == limits.LARGE_BODIES_AT_ONCE


# ── a slow sender gives its place back (audit of 19 Oct 2026) ───────────────
# A place was taken before
# the client-paced read and held with no deadline, so one write key's two
# stalled uploads held both, and every other account's large batch waited
# with no answer until that sender stopped.

def _stalling(body):
    """A client that sends the first piece of a large body, then nothing."""
    import anyio

    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body[: limits.MAX_BODY_BYTES + 1],
                    "more_body": True}
        await anyio.sleep_forever()
    return receive


def _large_scope(account, body, chunked=False):
    headers = [(b"authorization", f"Bearer {account}".encode())]
    if not chunked:
        headers.append((b"content-length", str(len(body)).encode()))
    return {"type": "http", "path": "/v1/batch", "method": "POST", "headers": headers}


def _two_stalled_and_one_whole(monkeypatch, *, chunked, deadline):
    """Account A stalls two large uploads; then account B sends one whole.
    The seconds B waited to reach the route."""
    import anyio

    from korely_agent import main

    async def writer(scope):
        return dict(scope["headers"])[b"authorization"].decode().split()[1]

    monkeypatch.setattr(main, "_writer_account", writer, raising=False)
    monkeypatch.setattr(limits, "LARGE_BODY_READ_SECONDS", deadline)
    body = _batch_body()
    reached = {}

    async def app(scope, receive, send):
        reached["at"] = anyio.current_time()

    async def collect(message):
        pass

    cap = main._BodyCap(app)

    async def whole():
        return {"type": "http.request", "body": body, "more_body": False}

    async def run():
        async with anyio.create_task_group() as tg:
            for _ in range(limits.LARGE_BODIES_AT_ONCE):
                tg.start_soon(cap, _large_scope("A", body, chunked), _stalling(body), collect)
            await anyio.sleep(0.1)
            started = anyio.current_time()
            with anyio.move_on_after(5):
                await cap(_large_scope("B", body, chunked), whole, collect)
            tg.cancel_scope.cancel()
        return reached.get("at", float("inf")) - started

    return anyio.run(run)


@pytest.mark.parametrize("chunked", [False, True])
def test_one_account_never_holds_every_large_place(monkeypatch, chunked):
    waited = _two_stalled_and_one_whole(monkeypatch, chunked=chunked, deadline=60.0)
    assert waited < 1.0, waited


@pytest.mark.parametrize("chunked", [False, True])
def test_a_stalled_large_body_gives_its_place_back_with_408(monkeypatch, chunked):
    import anyio

    from korely_agent import main

    async def writer(scope):
        return "A"

    monkeypatch.setattr(main, "_writer_account", writer, raising=False)
    monkeypatch.setattr(limits, "LARGE_BODY_READ_SECONDS", 0.2)
    body = _batch_body()
    statuses, bodies = [], []

    async def app(scope, receive, send):
        raise AssertionError("a body that never arrived reached the route")

    async def collect(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])
        else:
            bodies.append(message["body"])

    async def run():
        with anyio.fail_after(3):
            await main._BodyCap(app)(_large_scope("A", body, chunked), _stalling(body), collect)

    anyio.run(run)
    import json

    assert statuses == [408]
    assert json.loads(bodies[0])["code"] == "request_timeout"


def test_the_408_is_published_where_a_body_is_taken():
    """Every body has a deadline since the audit of 27 Oct 2026; the large one its own."""
    from korely_agent.main import create_app

    doc = create_app().openapi()
    batch = doc["paths"]["/v1/batch"]["post"]["responses"]["408"]["description"]
    write = doc["paths"]["/v1/memories"]["post"]["responses"]["408"]["description"]
    assert f"{limits.LARGE_BODY_READ_SECONDS:.0f} s" in batch
    assert f"{limits.BODY_READ_SECONDS:.0f} s" in write and "from being let in" not in write
    assert "as many request bodies" in doc["paths"]["/v1/memories"]["post"]["responses"]["503"]["description"]


# ── every spelling of the MCP's address has the MCP's ceiling (audit of 18 Oct 2026) ─

@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/agent/mcp", "/agent/mcp/"])
def test_every_spelling_of_the_mcp_has_its_ceiling(client, path):
    # Past REST's ceiling and within the MCP's: it reaches the SDK, whose
    # answer to a request without a key is its 401.
    r = client.post(path, content=b"{" + b" " * (limits.MAX_BODY_BYTES + 1024) + b"}",
                    headers={"content-type": "application/json",
                             "accept": "application/json, text/event-stream"})
    assert r.status_code == 401, (path, r.status_code, r.text[:200])


# ── a batch's values are counted before it is decoded (audit of 19 Oct 2026) ─
# Bytes do not bound what decoding costs: `{"memories":[{},{},...]}` at the
# batch's ceiling decodes to some 1.7 GB, and two at once restarted an API held
# to 1.5 GB (measured 18 Oct 2026).

def test_the_count_sets_the_strings_aside():
    # Seven values; every one but the outermost follows one mark, counted on
    # the bytes body_weighed counts.
    def marks(body):
        plain = limits._without_escaped_quotes(body)[0]
        return sum(limits._marks_by_kind(plain.translate(None, limits._NOT_STRUCTURE)))
    assert marks(b'{"a": "{[,:]}", "b": [1, 2]}') == 6
    assert marks(b'{"a": "say \\"hi\\", {x}"}') == 2
    assert marks(b"[" + b"{}," * 1000 + b"{}]") == 2002


@pytest.mark.parametrize("dense", ["values", "strings", "chars", "raw"])
def test_the_largest_lawful_batch_is_not_too_dense(dense):
    """MAX_BATCH_ITEMS items, each with every field and metadata as dense as
    JSON can spell it within MAX_METADATA_BYTES: in values (`[0,0,...]`) or
    in strings (`{"":"",...}` nested keys and values)."""
    import json

    from korely_agent.services.batch import MAX_BATCH_ITEMS

    content, ident = "x", "u"
    if dense == "values":
        meta = "[" + ",".join(["0"] * (limits.MAX_METADATA_BYTES // 2 - 1)) + "]"
    elif dense == "chars":
        # Every value at its longest, spelled at its widest: escaped, and
        # past U+FFFF.
        content = "\\ud83d\\ude00" * limits.MAX_CONTENT_CHARS
        ident = "\\u00e9" * limits.MAX_ID_CHARS
        meta = '"' + "y" * (limits.MAX_METADATA_BYTES - 10) + '"'
    elif dense == "raw":
        # The same, written in UTF-8: the whole text decodes at four bytes
        # a character (`body_text_bytes`).
        content = "\U0001F600" * limits.MAX_CONTENT_CHARS
        ident = "\u00e9" * limits.MAX_ID_CHARS
        meta = '"' + "y" * (limits.MAX_METADATA_BYTES - 10) + '"'
    else:
        meta = "[" + ",".join(['""'] * (limits.MAX_METADATA_BYTES // 3 - 1)) + "]"
    assert len(meta) <= limits.MAX_METADATA_BYTES
    item = ('{"content": "' + content + '", "user_id": "' + ident + '", "agent_id": "'
            + ident + '", "run_id": "' + ident + '", '
            '"timestamp": "2026-10-18T00:00:00Z", "client": "c", "kind": "note", '
            '"metadata": {"m": ' + meta + "}}")
    json.loads(item)
    body = ('{"memories": [' + ",".join([item] * MAX_BATCH_ITEMS) + "]}").encode()
    assert not limits.body_weighed(body, limits._batch_items())[0]


def test_a_batch_of_empty_objects_is_refused_before_it_is_decoded(client, db):
    """The route never sees it: the 413 is the shape's, not the route's."""
    n = limits.batch_json_values_cap() // 2 + 10
    body = b'{"memories": [' + b",".join([b"{}"] * n) + b"]}"
    assert len(body) > limits.MAX_BODY_BYTES
    r = _batch(client, _a_key(db, ["memories:read", "memories:write"]), content=body)
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large", r.text
    assert "JSON values" in r.json()["message"]


def test_a_chunked_batch_of_empty_objects_is_refused_too(client, db):
    n = limits.batch_json_values_cap() // 2 + 10
    piece = b",".join([b"{}"] * 10000)

    def chunks():
        yield b'{"memories": [{}'
        sent = 1
        while sent < n:
            yield b"," + piece
            sent += 10000
        yield b"]}"

    r = _batch(client, _a_key(db, ["memories:read", "memories:write"]), content=chunks())
    assert r.status_code == 413 and "JSON values" in r.json()["message"], r.text


def test_a_batch_of_empty_strings_is_refused_on_its_strings():
    n = limits.batch_json_strings_cap() + 10
    body = b'{"memories": [' + b'"",' * n + b'""]}'
    assert limits.body_weighed(body, limits._batch_items())[0]


# ── and its characters (audit of 20 Oct 2026) ───────────────────────────────
# One long string of
# ASCII with one character past U+FFFF decodes at 9 times its bytes (the
# whole text, then the value, both four bytes a character), and the quotes
# and marks did not see it. A batch's strings decode to at most what its most
# items' values hold. Measured at a batch of two items, so the body is small.

@pytest.fixture
def two_items(monkeypatch):
    from korely_agent.services import batch

    monkeypatch.setattr(batch, "MAX_BATCH_ITEMS", 2)
    return limits.max_batch_body_bytes()


def _wide(n, tail="\U0001F600"):
    return ('{"memories": [{"content": "' + "a" * n + tail + '"}]}').encode()


def test_the_characters_are_counted_without_decoding():
    import json

    for text in ["", "abc", "é", "\U0001F600", 'say "hi"', "a\\b", "\n\t", "日本"]:
        body = json.dumps({"k": text, "kk": [text, {"x": text}]}, ensure_ascii=False).encode()
        escaped = json.dumps({"k": text, "kk": [text, {"x": text}]}).encode()
        want = sum(len(s) for s in ("k", text, "kk", text, "x", text))
        for spelled in (body, escaped):
            plain, pairs, quotes = limits._without_escaped_quotes(spelled)
            assert limits._string_chars(spelled, plain, pairs, quotes) >= want, (text, spelled)
            assert limits._string_chars(spelled, plain, pairs, quotes) <= want + 6, (text, spelled)


def test_one_wide_string_is_refused_before_it_is_decoded(client, db, two_items):
    body = _wide(limits.MAX_BODY_BYTES + 10)
    assert limits.MAX_BODY_BYTES + 10 > limits.batch_json_chars_cap()
    assert limits.MAX_BODY_BYTES < len(body) <= two_items
    assert not limits.body_weighed(_wide(10), limits._batch_items())[0]
    r = _batch(client, _a_key(db, ["memories:read", "memories:write"]), content=body)
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large", r.text
    # The ceiling it crossed, named: the characters'.
    assert "characters" in r.json()["message"], r.text


def test_a_lawful_batch_of_two_still_reaches_the_route(client, db, two_items):
    import json

    item = {"content": "\U0001F600" * limits.MAX_CONTENT_CHARS, "user_id": "u",
            "metadata": {"m": "y" * (limits.MAX_METADATA_BYTES - 20)}}
    body = json.dumps({"memories": [item, item]}).encode()  # escaped: twelve bytes a character
    padded = body[:-1] + b" " * (limits.MAX_BODY_BYTES - len(body) + 10) + b"}"
    assert limits.MAX_BODY_BYTES < len(padded) <= two_items
    r = _batch(client, _a_key(db, ["memories:read", "memories:write"]), content=padded)
    assert r.status_code in (202, 429), r.text  # the route's answer: the body went through


# ── a body under the common ceiling is counted too (audit of 20 Oct 2026) ───
# Such a
# body went to the app unread and uncounted, and FastAPI decoded it before the
# key was checked, at up to some 26 times its bytes for empty containers.
# Counted as what items at their largest fill of the common ceiling.

def test_a_dense_body_without_a_key_is_refused_before_it_is_decoded(client):
    n = limits.batch_json_values_cap(limits.COMMON_ITEMS) // 2 + 10
    body = b'{"content": "x", "metadata": {"m": [' + b",".join([b"{}"] * n) + b"]}}"
    assert len(body) <= limits.MAX_BODY_BYTES
    r = client.post("/v1/memories", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large", r.text
    assert client.asked == [], "the key was looked up before the body was refused"


def test_a_wide_body_without_a_key_is_refused_before_it_is_decoded(client):
    body = ('{"content": "' + "a" * (limits.batch_json_chars_cap(limits.COMMON_ITEMS) + 10)
            + "\U0001F600" + '"}').encode()
    assert len(body) <= limits.MAX_BODY_BYTES
    r = client.post("/v1/memories", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 413, r.text


def test_a_dense_body_with_no_content_type_is_counted_too(client):
    n = limits.batch_json_values_cap(limits.COMMON_ITEMS) // 2 + 10
    body = b"[" + b",".join([b"[]"] * n) + b"]"
    r = client.post("/admin/session", content=body, headers={"content-type": ""})
    assert r.status_code == 413, r.text


def test_an_ordinary_write_still_reaches_the_route(client):
    import json

    body = json.dumps({"content": "é" * limits.MAX_CONTENT_CHARS, "user_id": "maria",
                       "metadata": {"k": ["v"] * 1000}}).encode()
    r = client.post("/v1/memories", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 401  # the key's answer: the body went through


def test_a_dense_small_batch_of_a_write_key_is_counted_as_a_batch(client, db):
    """Under the common ceiling a lawful batch can be denser than items at
    their largest: once its bearer is a write key it is counted as the
    batch it is."""
    import json

    meta = {"m": [0] * 2000}
    body = json.dumps({"memories": [{"content": "x", "metadata": meta}] * 60}).encode()
    assert len(body) <= limits.MAX_BODY_BYTES
    assert limits.body_weighed(body, limits.COMMON_ITEMS)[0]
    r = _batch(client, _a_key(db, ["memories:read", "memories:write"]), content=body)
    assert r.status_code in (202, 429), r.text


# ── every body has a deadline, and together a most (audit of 27 Oct 2026) ──
# A body under the common ceiling was read with no deadline, and nothing
# counted what such bodies held together, so keyless stalled uploads held
# their bytes in the one process for as long as their connections stayed.

def _small_stalling(first: bytes):
    import anyio

    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": first, "more_body": True}
        await anyio.sleep_forever()
    return receive


def _answers():
    statuses, bodies, headers = [], [], []

    async def collect(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])
            headers.append(dict(message["headers"]))
        else:
            bodies.append(message["body"])
    return statuses, bodies, headers, collect


@pytest.mark.parametrize("kind", [b"application/json", b"application/x-www-form-urlencoded"])
@pytest.mark.parametrize("chunked", [False, True])
def test_a_stalled_small_body_is_answered_408(monkeypatch, kind, chunked):
    import json

    import anyio

    from korely_agent import main

    monkeypatch.setattr(limits, "BODY_READ_SECONDS", 0.2)

    async def app(scope, receive, send):
        raise AssertionError("a body that never arrived reached the route")

    headers = [(b"content-type", kind)]
    if not chunked:
        headers.append((b"content-length", b"1000"))
    scope = {"type": "http", "path": "/token" if b"form" in kind else "/v1/memories",
             "method": "POST", "headers": headers}
    statuses, bodies, _h, collect = _answers()

    async def run():
        with anyio.fail_after(3):
            await main._BodyCap(app)(scope, _small_stalling(b"x" * 100), collect)

    anyio.run(run)
    assert statuses == [408]
    assert json.loads(bodies[0])["code"] == "request_timeout"


def _from(network: str, path="/v1/memories", chunked=True, account=None):
    headers = [(b"content-type", b"application/json")]
    if account:
        headers.append((b"authorization", f"Bearer {account}".encode()))
    return {"type": "http", "path": path, "method": "POST", "headers": headers,
            "client": (network, 50000)}


def _whole(body):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return receive


def test_the_bodies_under_the_ceiling_have_a_most_together(monkeypatch):
    import json

    import anyio

    from korely_agent import main

    monkeypatch.setattr(limits, "BODIES_IN_FLIGHT_BYTES", 1000)
    monkeypatch.setattr(limits, "BODIES_PER_NETWORK", 1000, raising=False)
    monkeypatch.setattr(limits, "BODY_READ_SECONDS", 60.0)
    reached = []

    async def app(scope, receive, send):
        reached.append((await receive())["body"])

    cap = main._BodyCap(app)
    statuses, bodies, headers, collect = _answers()

    async def run():
        async with anyio.create_task_group() as tg:
            tg.start_soon(cap, _from("10.0.0.1"), _small_stalling(b" " * 800), collect)
            await anyio.sleep(0.05)
            assert cap._budget.held() == 800
            await cap(_from("10.0.0.2"), _whole(b"{}" + b" " * 498), collect)
            assert statuses == [503]
            await cap(_from("10.0.0.2"), _whole(b"{}"), collect)
            tg.cancel_scope.cancel()

    anyio.run(run)
    assert json.loads(bodies[0])["code"] == "unavailable"
    assert headers[0][b"retry-after"]
    assert reached == [b"{}"]
    assert cap._budget.held() == 0, "a body's bytes are given back when its request ends"


def test_one_network_holds_its_share_only(monkeypatch):
    """The review of the audit of 27 Oct 2026: one keyless host's stalled bodies held the
    whole of the process's count, and every tenant's write answered 503."""
    import anyio

    from korely_agent import main

    monkeypatch.setattr(limits, "BODIES_IN_FLIGHT_BYTES", 8 * limits.MAX_BODY_BYTES)
    monkeypatch.setattr(limits, "BODIES_PER_NETWORK", 2 * limits.MAX_BODY_BYTES, raising=False)
    monkeypatch.setattr(limits, "BODY_READ_SECONDS", 60.0)
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["client"][0])
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    cap = main._BodyCap(app)
    statuses, _b, _h, collect = _answers()

    async def run():
        async with anyio.create_task_group() as tg:
            for _ in range(2):
                tg.start_soon(cap, _from("203.0.113.9"),
                              _small_stalling(b" " * (limits.MAX_BODY_BYTES - 10)), collect)
            await anyio.sleep(0.05)
            await cap(_from("203.0.113.9"), _whole(b'{"content": "x"}'), collect)
            assert statuses == [503], statuses
            await cap(_from("198.51.100.7"), _whole(b'{"content": "x"}'), collect)
            assert statuses == [503, 200], statuses
            tg.cancel_scope.cancel()

    anyio.run(run)
    assert reached == ["198.51.100.7"]
    assert cap._budget.held() == 0


def test_a_chunked_batch_waits_for_its_place_no_longer_than_its_deadline(monkeypatch):
    """The review of the audit of 27 Oct 2026: a chunked batch crossing the common
    ceiling while its account's large place was taken waited with no end,
    holding what it had read of the count, and one write key's 32 of them
    held all of it. It waits until its deadline, then 503 with Retry-After,
    and gives its share back."""
    import json

    import anyio

    from korely_agent import main

    monkeypatch.setattr(limits, "BODY_READ_SECONDS", 0.3)

    async def writer(scope):
        return "acct-1"

    monkeypatch.setattr(main, "_writer_account", writer)

    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    cap = main._BodyCap(app)
    statuses, bodies, headers, collect = _answers()
    crossing = [b" " * limits.MAX_BODY_BYTES, b" "]

    def chunks():
        async def receive():
            if crossing:
                piece = crossing.pop(0)
                return {"type": "http.request", "body": piece, "more_body": True}
            await anyio.sleep_forever()
        return receive

    async def run():
        async with cap._places.place("acct-1"):   # its own large upload, under way
            with anyio.fail_after(3):
                await cap(_from("10.0.0.1", "/v1/batch", account="kor_x"), chunks(), collect)
            assert cap._budget.held() == 0
            await cap(_from("10.0.0.2", account="kor_y"), _whole(b"{" + b" " * 998 + b"}"), collect)

    anyio.run(run)
    assert statuses == [503, 200], statuses
    assert json.loads(bodies[0])["code"] == "unavailable" and headers[0][b"retry-after"]


# ── the whole text of a body is counted (audit of 27 Oct 2026) ─────────────
# The counts saw the
# strings and the marks, not the spaces between them; a body of spaces with
# one character past U+FFFF decodes to a str at four bytes a character, five
# times its bytes at the peak (78 MB to some 390 MB), and two at the batch's
# places were past the API's mem_limit.

def test_the_whole_text_is_counted_without_decoding():
    for text in ('{"a": 1}', '{"a": "é"}', '{"a": "日本"}', '{"a": "\U0001F600"}',
                 " " * 100 + '["\U0001F600"]'):
        widest = max(map(ord, text))
        width = 4 if widest > 0xFFFF else 2 if widest > 0xFF else 1
        assert limits.body_text_bytes(text.encode()) == len(text) * width, text


def test_a_padded_wide_body_without_a_key_is_refused_before_it_is_decoded(client):
    body = ('{"content": "x\U0001F600"' + " " * (limits.MAX_BODY_BYTES - 100) + "}").encode()
    assert len(body) <= limits.MAX_BODY_BYTES
    assert limits.body_text_bytes(body) > limits.batch_json_text_cap(limits.COMMON_ITEMS)
    r = client.post("/v1/memories", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 413 and "bytes of text" in r.json()["message"], r.text
    assert client.asked == []


def test_a_padded_wide_batch_is_refused_before_it_is_decoded(client, db, two_items):
    body = ('{"memories": [{"content": "x\U0001F600"}]' + " " * (two_items - 100) + "}").encode()
    assert limits.MAX_BODY_BYTES < len(body) <= two_items
    assert not limits.body_weighed(_wide(10), limits._batch_items())[0]
    r = _batch(client, _a_key(db, ["memories:read", "memories:write"]), content=body)
    assert r.status_code == 413 and "bytes of text" in r.json()["message"], r.text


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-32"])
def test_a_body_not_in_utf8_is_refused_before_it_is_decoded(client, encoding):
    body = '{"content": "x", "user_id": "maria"}'.encode(encoding)
    r = client.post("/v1/memories", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400 and "UTF-8" in r.json()["message"], r.text
    assert client.asked == []


# ── what the places and the count cost, against the API's memory ───────────
# The audit of 20 Oct 2026 asked for it: the two
# large places were sized on a decode factor measured on empty objects
# only, never against mem_limit. Measured here at a batch of 20 items (a
# few MB, one process, no load) and scaled to the batch's most, on the
# shapes that cost the most a mark, a string and a character.

_SHAPES = {
    "one-key objects": (b'{"a":0}', 3, 1), "empty objects": (b"{}", 2, 0),
    "empty arrays": (b"[]", 2, 0), "object in object": (b'{"a":{}}', 4, 1),
    "array in object": (b'{"a":[]}', 4, 1), "numbers": (b"0.5", 1, 0),
    "big numbers": (b"123456789012345678901234567890", 1, 0),
    "strings": (b'"ab"', 1, 1),
}


def _filled(items: int, unit: bytes, marks: int, strings: int, tail: bytes = b"") -> bytes:
    """A body of `unit`s filling the values' or the strings' count of
    `items` items, and `tail` in it."""
    n = min(limits.batch_json_values_cap(items) // marks,
            limits.batch_json_strings_cap(items) // strings if strings else 10**9) - 20
    return b"[" + b",".join([unit] * n) + tail + b"]"


def _decoded(body: bytes) -> int:
    """The body's bytes and the peak of decoding it, measured."""
    import json
    import tracemalloc

    tracemalloc.start()
    value = json.loads(body)
    _now, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del value
    return len(body) + peak


def _costliest(items: int):
    """Each shape filling the counts of `items` items; and one-key objects
    with the characters' count filled by one string past U+FFFF."""
    for name, (unit, marks, strings) in _SHAPES.items():
        yield name, _filled(items, unit, marks, strings)
    objects = limits.batch_json_values_cap(items) // 3 - 100
    wide = ("\U0001F600" * (limits.batch_json_chars_cap(items) - objects - 20)).encode()
    yield "objects and wide characters", (b"[" + b",".join([b'{"a":0}'] * objects)
                                          + b',"' + wide + b'"]')


def _api_memory():
    """(mem_limit, the memory at rest) of the API, MiB, as docker-compose.yml
    sets and measured them."""
    import re
    from pathlib import Path

    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
    limit = int(re.search(r"mem_limit: \$\{API_MEMORY_LIMIT:-(\d+)m\}", compose).group(1))
    rest = int(re.search(r"(\d+) MiB at rest", compose).group(1))
    return limit, rest


def test_the_cost_of_a_body_bounds_what_decoding_it_takes():
    """The count of the bodies under the common ceiling charges each its cost
    (limits.body_weighed): never less than what it takes, measured."""
    for name, body in _costliest(limits.COMMON_ITEMS):
        dense, cost = limits.body_weighed(body, limits.COMMON_ITEMS)
        assert dense is None, (name, dense)
        assert cost >= _decoded(body), (name, cost, _decoded(body))


def test_the_large_places_and_the_count_fit_in_the_api_memory():
    from korely_agent.services.batch import MAX_BATCH_ITEMS

    sample = 20
    worst = 0
    for name, body in _costliest(sample):
        assert limits.body_weighed(body, sample)[0] is None, name
        worst = max(worst, _decoded(body) * MAX_BATCH_ITEMS // sample)
    limit, rest = _api_memory()
    held = limits.LARGE_BODIES_AT_ONCE * worst + limits.BODIES_IN_FLIGHT_BYTES
    assert held <= (limit - rest) * 2**20, (
        f"{limits.LARGE_BODIES_AT_ONCE} large bodies at {worst / 2**20:.0f} MiB and "
        f"{limits.BODIES_IN_FLIGHT_BYTES / 2**20:.0f} MiB of small ones: "
        f"{held / 2**20:.0f} MiB, past the {limit - rest} MiB above the API's rest")


# ── every door weighs its body (the audit before 1.0.0) ─────────────────────
# The MCP's door was left out of the
# count (limits.body_counted, "its SDK checks the key first"), but a key is
# free on the Cloud and the count bounds the heap, not anonymity: an MCP
# message was decoded unweighed, and only its bytes were charged. Every door
# the app mounts, in either mode and at every spelling of its address, is
# sent a body denser than lawful items spell: each is refused 413 before
# anything reads it. Whatever its Content-Type: each decoder reads the type
# its own way (FastAPI by its media type, the MCP's SDK by any of its
# comma-separated parts, its /register whatever the type), and a tools/call
# typed `application/json , text/html` was decoded unweighed (the review of
# this fix), as a keyless /register typed text/plain was.

_EVERY_DOOR = r"""
import asyncio, json, logging, os, re, sys
os.environ["KORELY_MODE"] = sys.argv[1]
os.environ["RUN_WORKER"] = "false"
os.environ.setdefault("HOSTED_FIREBASE_PROJECT_ID", "walk")
os.environ.setdefault("DATABASE_URL", "postgresql://walk:walk@localhost:1/none")
logging.disable(logging.CRITICAL)
from korely_agent import main
from korely_agent.api.errors import every_route
from korely_agent.mcp.server import HOSTED_PATH
from korely_agent.services import limits

app = main.create_app()
doors = {HOSTED_PATH, HOSTED_PATH + "/"}
for r in every_route(app.router.routes):
    # A Mount (the MCP's, the static pages) answers every method.
    if getattr(r, "methods", None) is None or r.methods - {"GET", "HEAD", "OPTIONS"}:
        doors.add(re.sub(r"\{[^}]*\}", "x", r.path))
n = limits.batch_json_values_cap(limits.COMMON_ITEMS) // 2 + 10
body = b'{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"zz": [' \
    + b",".join([b"{}"] * n) + b"]}}"
assert len(body) <= limits.MAX_BODY_BYTES
reached, answered, now = [], {}, [None]

async def door(scope, receive, send):
    reached.append(now[0])

KINDS = [b"application/json", b"application/json , text/html", b"text/plain",
         b"application/x-www-form-urlencoded", None]

async def go():
    cap = main._BodyCap(door)
    for path, kind in ((p, k) for p in sorted(doors) for k in KINDS):
        now[0] = f"{path} {(kind or b'(none)').decode()}"
        def collect(message, at=now[0]):
            if message["type"] == "http.response.start":
                answered[at] = message["status"]
        async def done(message, collect=collect):
            collect(message)
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        headers = [(b"content-length", str(len(body)).encode())]
        if kind is not None:
            headers.append((b"content-type", kind))
        scope = {"type": "http", "method": "POST", "path": path, "client": ("192.0.2.1", 1),
                 "headers": headers}
        await cap(scope, receive, done)

asyncio.run(go())
print(json.dumps({"doors": sorted(doors), "reached": reached,
                  "not_413": sorted(p for p, s in answered.items() if s != 413)}))
"""


@pytest.mark.parametrize("mode", cloud.MODES)
def test_every_door_weighs_its_body(mode):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {k: v for k, v in os.environ.items() if not k.startswith(("KORELY_", "HOSTED_"))}
    out = subprocess.run([sys.executable, "-c", _EVERY_DOOR, mode],
                         cwd=Path(__file__).resolve().parent.parent, env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert "/mcp" in got["doors"] and len(got["doors"]) > 20, got["doors"]
    assert got["reached"] == [] and got["not_413"] == [], got


@pytest.mark.parametrize("kind", ["text/plain", "application/json , text/html", None])
def test_a_dense_registration_is_refused_whatever_its_type(client, kind):
    """The SDK's /register decodes its body as JSON whatever its type
    (`request.json()`), with no key: typed text/plain it was decoded
    unweighed (the review of the fix of the audit before 1.0.0)."""
    n = limits.batch_json_values_cap(limits.COMMON_ITEMS) // 2 + 10
    body = b'{"redirect_uris": [' + b",".join([b"{}"] * n) + b"]}"
    r = client.post("/register", content=body,
                    headers={"content-type": kind} if kind else {})
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large", r.text[:300]


# ── a lawful batch under the common ceiling is let in (the audit before 1.0.0) ─
# The count
# charged every character of a body's strings at four bytes whatever its
# width, so a lawful batch of 1.4 to 2 MiB cost more than a network's share
# and was answered 503 with Retry-After on an idle server, every retry the
# same (a regression of fe0e947).

@pytest.mark.parametrize("spelled", ["ascii", "escaped", "raw"])
def test_a_lawful_batch_at_the_common_ceiling_reaches_the_route(client, db, spelled):
    import json

    from korely_agent.services.batch import MAX_BATCH_ITEMS

    text = "a" * 4000
    if spelled != "ascii":
        text = text[:-2] + "\U0001F600"  # one character past U+FFFF in each item
    body = json.dumps({"memories": [{"content": text, "user_id": "maria"}] * MAX_BATCH_ITEMS},
                      ensure_ascii=spelled == "escaped").encode()
    assert 1.9 * 2**20 < len(body) <= limits.MAX_BODY_BYTES, len(body)
    r = _batch(client, _a_key(db, ["memories:read", "memories:write"]), content=body)
    assert r.status_code == 202, r.text  # the route's answer: the body went through


@pytest.mark.parametrize("unit", ['"a"', '"é"', '"\\u00e9"', '"日"', '"\\u65e5"', '"\U0001F600"',
                                  '"\\ud83d\\ude00"', '"' + "a" * 4000 + '"',
                                  '"' + "a" * 3998 + '\U0001F600"', '"' + "é" * 2000 + '"',
                                  '"' + "\U0001F600" * 1000 + '"'])
def test_the_characters_are_charged_at_their_width(unit):
    """Never less than decoding takes, measured, for strings short and long
    at each width, written in UTF-8 or escaped; and plain text at one byte
    a character, where it was four."""
    import json

    n = min(limits.batch_json_chars_cap(limits.COMMON_ITEMS) // len(json.loads(unit)),
            limits.batch_json_strings_cap(limits.COMMON_ITEMS)) - 20
    body = ("[" + ",".join([unit] * n) + "]").encode()
    dense, cost = limits.body_weighed(body, limits.COMMON_ITEMS)
    assert dense is None, dense
    assert cost >= _decoded(body), (cost, _decoded(body))
    if len(unit) > 1000 and unit.isascii():
        assert cost < 4 * len(body), cost  # some 3: its bytes, its text, its strings


def test_a_body_that_never_fits_a_networks_share_is_a_413_not_a_503(client):
    """Without a key that may write, a body under every count that would
    cost more than a network's share once decoded: a 503 said to retry, and
    no retry could let it in."""
    n = limits.batch_json_values_cap(limits.COMMON_ITEMS) // 2 - 100
    body = b"[" + b",".join([b"{}"] * n) + b"]"
    dense, cost = limits.body_weighed(body, limits.COMMON_ITEMS)
    assert dense is None and cost > limits.BODIES_PER_NETWORK, cost
    r = client.post("/v1/memories", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 413 and "once decoded" in r.json()["message"], r.text
    assert client.asked == []


def test_a_dense_batch_under_the_ceiling_still_costs_what_it_decodes_to():
    """fe0e947's bodies: a write key's batch of empty objects under the
    common ceiling is let past the counts of COMMON_ITEMS as the batch it is,
    and its cost is still more than any network's share."""
    n = (limits.MAX_BODY_BYTES - 100) // 3
    body = b'{"memories": [' + b",".join([b"{}"] * n) + b"]}"
    from korely_agent.services.batch import MAX_BATCH_ITEMS

    dense, cost = limits.body_weighed(body, MAX_BATCH_ITEMS)
    assert dense is None
    assert cost >= _decoded(body) and cost > limits.BODIES_PER_NETWORK, cost


# ── a declared large body waits for its place no longer than its deadline ───
# The audit before 1.0.0: the chunked twin had its deadline since the review
# of the audit of 27 Oct 2026.

def test_a_declared_batch_waits_for_its_place_no_longer_than_its_deadline(monkeypatch):
    import json

    import anyio

    from korely_agent import main

    monkeypatch.setattr(limits, "BODY_READ_SECONDS", 0.3)

    async def writer(scope):
        return "acct-1"

    monkeypatch.setattr(main, "_writer_account", writer)

    async def app(scope, receive, send):
        raise AssertionError("a body that never had its place reached the route")

    cap = main._BodyCap(app)
    statuses, bodies, headers, collect = _answers()
    body = _batch_body()

    async def run():
        async with cap._places.place("acct-1"):   # its own large upload, under way
            with anyio.fail_after(3):
                await cap(_large_scope("kor_x", body), _whole(body), collect)

    anyio.run(run)
    assert statuses == [503], statuses
    assert json.loads(bodies[0])["code"] == "unavailable" and headers[0][b"retry-after"]
