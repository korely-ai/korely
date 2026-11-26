"""While the local embedder loads its model at the start, the process says
so and answers at once (code review finding, P2, 15 Nov 2026).

The API starts answering before the model is loaded (main.lifespan starts
`local_embedder.warm_up` off the startup path): 7.5 s with the files' cache
cold, 1.3 s warm (measured in Docker with two CPUs, 7 Nov 2026). Until 1.0.26
/health said `unknown`, "no vector has been asked for yet", and `ok`, the
image's HEALTHCHECK passed on any answer, and a request that needed a vector
waited for its turn behind the load: the Claude Code hook's GET /v1/context
at the start of a session gave up after its 8 s, eleven times since 18 Oct.

Now /health says `embeddings.state` `warming` and `ready: false` until the
first vector (Kubernetes' readiness probe: "waiting for an application to
perform time-consuming initial tasks, such as ... loading files, and warming
caches", told apart from an app that failed), the HEALTHCHECK passes only on
`ready`, and a request's vector is refused at once, answered as one past its
account's share is: a search 503 `search_unavailable` with Retry-After, a
context its facts with `degraded`, a write stored with its vector left to the
worker, a fact written or corrected with a relation new to the vocabulary 503
`unavailable` with Retry-After. The worker's own vectors wait for the model,
as before.

The load is a fake held by an event, so nothing here depends on the speed of
the machine; a request's wait for its turn is cut to 2 s (20 s in the
product), so the code of 1.0.26 fails here in seconds instead of passing late.
"""
import json
import re
import threading
import time
import uuid
from pathlib import Path

import pytest

from korely_agent.core import local_embedder
from tests.test_local_embedder import FakeSession, FakeTokenizer

ROOT = Path(__file__).resolve().parents[1]
#: Well under the turn's 2 s below: an answer this fast did not wait for it.
AT_ONCE = 1.0


@pytest.fixture
def held_load(monkeypatch):
    """The model's load, held until the test sets the event."""
    go = threading.Event()

    def load():
        assert go.wait(30), "the test never let the load end"
        local_embedder._model = (FakeSession(), FakeTokenizer(), ["input_ids", "attention_mask"])
        return local_embedder._model

    monkeypatch.setattr(local_embedder, "_model", None)
    monkeypatch.setattr(local_embedder, "_load", load)
    monkeypatch.setattr(local_embedder, "_turns", local_embedder._Turns(wait=2.0))
    local_embedder._last.update({"ok": None, "at": None, "error": None})
    yield go
    go.set()
    for t in threading.enumerate():
        if t.name == "local-embedder-warm-up":
            t.join(10)
    local_embedder._last.update({"ok": None, "at": None, "error": None})


@pytest.fixture
def starting(db, tenant, monkeypatch, held_load):
    """The API just started with EMBEDDING_PROVIDER=local, its model still
    loading: a client and a key's headers."""
    from fastapi.testclient import TestClient

    from korely_agent.config import get_settings
    from korely_agent.db import get_db
    from korely_agent.main import create_app
    from korely_agent.services import keys as key_service

    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    get_settings.cache_clear()
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
                                          scopes=["memories:read", "memories:write"])
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c, {"Authorization": f"Bearer {full}"}, held_load
    get_settings.cache_clear()


def _health(c):
    r = c.get("/health")
    assert r.status_code == 200, r.text
    return r.json()


def _until(condition, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_until_the_first_vector_the_process_says_it_is_warming(starting):
    c, _head, go = starting
    body = _health(c)
    assert body["embeddings"]["state"] == "warming", body["embeddings"]
    assert body["ready"] is False
    assert body["status"] == "ok", "a process that is starting has not failed"
    assert "since" in body["embeddings"]
    # From an address on the internet: the states, `ready` among them.
    from korely_agent.main import states_only

    assert states_only(body)["ready"] is False
    assert states_only(body)["embeddings"]["state"] == "warming"
    go.set()
    assert _until(lambda: _health(c)["embeddings"]["state"] == "working"), _health(c)
    assert _health(c)["ready"] is True


@pytest.mark.serial  # three answers timed under 1 s each: one went past it beside the 4-process suite (gate of 1.0.31, 16 Nov 2026), alone 7/7 three times; no parallel neighbours
def test_a_request_does_not_wait_for_the_load(starting, db):
    """A search, a context and a write while the model loads: each answered
    at once, and each as the docs say for a vector this process cannot give
    now (docs/guide/install.md)."""
    from korely_agent.core import threads
    from korely_agent.db.models import AgentMemory

    c, head, go = starting
    started = time.monotonic()
    r = c.get("/v1/context", headers=head, params={"query": "where does she work?",
                                                   "user_id": "u1"})
    assert time.monotonic() - started < AT_ONCE, "the context waited for the load"
    assert r.status_code == 200, r.text
    assert r.json()["degraded"] is True and r.json()["degraded_parts"] == ["memories"]

    started = time.monotonic()
    r = c.post("/v1/memories/search", headers=head, json={"query": "where?", "user_id": "u1"})
    assert time.monotonic() - started < AT_ONCE, "the search waited for the load"
    assert (r.status_code, r.json()["code"]) == (503, "search_unavailable"), r.text
    assert r.headers["Retry-After"] == str(threads.BUSY_RETRY_SECONDS)

    started = time.monotonic()
    r = c.post("/v1/memories", headers=head, json={"content": "Sofia works at Beacon Labs.",
                                                    "user_id": "u1"})
    assert time.monotonic() - started < AT_ONCE, "the write waited for the load"
    assert r.status_code == 201, r.text
    db.expire_all()
    [m] = db.query(AgentMemory).all()
    assert m.embedding is None and m.embedding_status == "pending", "not left to the worker"
    # Nothing failed: /health has heard of no failure of the embedder.
    assert _health(c)["embeddings"]["state"] == "warming"
    go.set()


@pytest.mark.serial  # three answers timed under 1 s each, as the test above: one took 2.31 s beside the 4-process suite (the 1.0.36 candidate, 23 Nov 2026), alone 7/7 three times; no parallel neighbours
def test_a_fact_write_is_told_to_retry_not_that_a_model_failed(starting, db, tenant, monkeypatch):
    """A fact written or corrected with a relation new to the vocabulary needs
    its vector (memory_core `store._the_vector`, strict): while the model
    loads it is refused, and the door says so, `unavailable` with the
    Retry-After of a busy resource, at once and with nothing written, on
    POST and PATCH /v1/facts (the MCP's correction answers PATCH's words) and
    on the dashboard's correction. It answered `model_unavailable`, "a model
    the write needs did not give a usable answer" (skeptic of 15 Nov 2026).
    Once the model is loaded the same write goes in.

    "At once" is asked of the code first: no door's vector asks for a turn
    at the embedder while the model loads (`local_embedder.Warming`, refused
    before the turn). Up to 1.0.26 it queued behind the load and, after the
    turn's wait, got the same 503 `unavailable`, so the answer alone cannot
    tell the two apart; the clock still bounds each answer, as in the test
    above. On 23 Nov 2026 the third door took 2.31 s beside the 4-process
    suite with every door refused by `Warming` (its log): the machine's
    pause, not a wait for the load."""
    from datetime import datetime, timezone

    from korely_agent.core import threads
    from korely_agent.db.models import AgentFact
    from korely_agent.services import keys as key_service
    from korely_agent.services.ids import fact_public_id
    from tests.conftest import seeded_project_id
    from tests.deletion_world import Writer

    c, head, go = starting
    turns_asked = []
    turn = local_embedder._turns.turn

    def counted(question, account=None, request=False):
        if request:
            turns_asked.append(account)
        return turn(question, account=account, request=request)

    monkeypatch.setattr(local_embedder._turns, "turn", counted)
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: Writer())
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    _r, operator = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    acme = AgentFact(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                     end_user_id="u1", subject_label="Sofia", subject_canonical="Sofia",
                     predicate_raw="works_at", predicate_canonical="works_at",
                     object_label="Acme Corp", object_canonical="Acme Corp",
                     valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.add(acme)
    db.commit()
    fid = fact_public_id(acme.id)
    asked = {
        "POST /v1/facts": lambda: c.post("/v1/facts", headers=head, json={
            "subject": "Sofia", "predicate": "mentors", "object": "Luca", "user_id": "u1"}),
        "PATCH /v1/facts": lambda: c.patch(f"/v1/facts/{fid}", headers=head,
                                           json={"predicate": "is employed by"}),
        "PATCH /admin/facts": lambda: c.patch(f"/admin/facts/{fid}",
                                              headers={"Authorization": f"Bearer {operator}"},
                                              json={"predicate": "is employed by"}),
    }
    for door, ask in asked.items():
        started = time.monotonic()
        r = ask()
        took = time.monotonic() - started
        assert turns_asked == [], f"{door} queued for a turn behind the load"
        assert took < AT_ONCE, f"{door} took {took:.2f} s"
        assert r.status_code == 503, (door, r.text)
        assert r.json()["code"] == "unavailable", (door, r.text)
        assert r.headers["Retry-After"] == str(threads.BUSY_RETRY_SECONDS), door
    db.expire_all()
    assert [f.object_label for f in db.query(AgentFact)] == ["Acme Corp"], "something was written"
    assert db.get(AgentFact, acme.id).invalid_at is None
    assert _health(c)["embeddings"]["state"] == "warming", "a refusal is no failure of the embedder"
    go.set()
    assert _until(lambda: _health(c)["ready"] is True), _health(c)
    assert asked["POST /v1/facts"]().status_code == 201


def test_the_workers_vector_waits_for_the_model(starting):
    """Off a request (the worker's drain of vectors), a vector waits for the
    load and gets the model's answer, as before."""
    _c, _head, go = starting
    out = {}
    t = threading.Thread(target=lambda: out.update(v=local_embedder.embed("x", local_embedder.DOCUMENT)))
    t.start()
    t.join(0.3)
    assert t.is_alive(), "it did not wait for the model"
    go.set()
    t.join(10)
    vec, tokens = out["v"]
    assert len(vec) == local_embedder.M.DIMENSIONS and tokens > 0


def test_a_load_that_fails_ends_the_warming(monkeypatch):
    """A missing model ends the start too: `failing` with the reason, as
    before, and no longer warming, since a probe that waited for it would
    wait for ever (the HEALTHCHECK then passes, `status` says degraded)."""
    monkeypatch.setattr(local_embedder, "MODEL_DIR", "/nowhere")
    monkeypatch.setattr(local_embedder, "_model", None)
    local_embedder._last.update({"ok": None, "at": None, "error": None})
    local_embedder.warm_up().join(10)
    assert local_embedder.state()["state"] == "failing"
    local_embedder._last.update({"ok": None, "at": None, "error": None})


def _healthcheck_code():
    """The Python the image's HEALTHCHECK runs (`python -c "..."`)."""
    text = (ROOT / "Dockerfile").read_text()
    [check] = re.findall(r"^HEALTHCHECK .*?CMD python -c \"(.+?)\"", text, re.S | re.M)
    return check


@pytest.mark.parametrize("ready, code", [(False, 1), (True, 0)], ids=["warming", "ready"])
def test_the_images_healthcheck_passes_only_when_ready(monkeypatch, ready, code):
    """The check run against a /health body: healthy only on `ready`."""
    import io
    import urllib.request

    body = {"version": "x", "status": "ok", "ready": ready,
            "embeddings": {"state": "warming" if not ready else "working"}}
    asked = []

    def answer(url, *a, **kw):
        asked.append(url)
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", answer)
    try:
        exec(compile(_healthcheck_code(), "<HEALTHCHECK>", "exec"), {})
        got = 0
    except SystemExit as e:
        got = e.code
    assert asked == ["http://127.0.0.1:8000/health"]
    assert got == code
