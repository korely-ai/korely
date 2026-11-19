"""A writer that drops one connection costs a moment, not a rest of its region.

On 15 Nov 2026 a measure on the European path, run on a computer that went
to sleep, met connections closed before the answer
(http.client.RemoteDisconnected, no HTTP status). Each one sent its memory
back to the queue and rested the whole region for a minute
(services/facts/drain.py, `_provider_refused`): core/llm.py asked a call
again only when the provider had answered with a status (`_transient`), so
the one failure that is a moment by definition, an answer that never came,
went straight to the drain. The rule core/llm.py says it takes from the
providers' own SDKs asks that one again too: openai-python and
anthropic-sdk-python retry a connection error (APIConnectionError) as they
retry a 429 or a 5xx, twice, 0.5 s doubling.

The provider here is a real OpenAI-compatible server on this machine that
closes the connection without a word when told to, so the call goes through
urllib and http.client exactly as it does to a real writer: no model is
called.
What must not change is held beside it: a refused key still rests the
region at once, a service that stops the region still sends nothing, a call
that times out is still not asked again here (the drain counts those per
memory), and a region that cannot write never writes in the other one.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from korely_agent.core import extensions, llm
from korely_agent.db.models import AgentMemory, AgentProject
from korely_agent.services.facts import drain as facts_drain
from korely_agent.services.facts import service as facts_service
from tests.thread_helpers import wait_idle

# What the stand-in answers every call that it does answer: an extraction
# with nothing in it, which the engine files 'done' with no fact.
NOTHING = json.dumps({"facts": [], "observation": None})


class Provider:
    """An OpenAI-compatible /chat/completions on 127.0.0.1. `script` says
    what each call gets, in order, and the last entry is repeated: "answer",
    "drop" (the connection closed with no status line),
    an int (that HTTP status), or ("slow", seconds) (an answer that comes
    after that long)."""

    def __init__(self, *script):
        self.script = list(script) or ["answer"]
        self.calls = 0
        self._lock = threading.Lock()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # the test's log stays the product's
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with provider._lock:
                    step = provider.script[min(provider.calls, len(provider.script) - 1)]
                    provider.calls += 1
                if step == "drop":
                    self.close_connection = True
                    return  # nothing written: the client reads no status line
                if isinstance(step, tuple):
                    time.sleep(step[1])
                    step = "answer"
                if isinstance(step, int):
                    body = json.dumps({"error": {"message": "refused"}}).encode()
                    self.send_response(step)
                else:
                    body = json.dumps({"choices": [{"message": {"content": NOTHING},
                                                    "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 10,
                                                 "completion_tokens": 5}}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def providers(monkeypatch):
    """Europe's writer and Global's, each its own stand-in: the Models page
    as core/llm reads it, the install's default region Global."""
    made = {}

    def writers(eu: Provider, glob: Provider):
        made.update(eu=eu, glob=glob)
        monkeypatch.setattr(llm, "screen_writers", lambda: {
            "default": "global",
            "eu": ("scaleway", eu.base, "not-a-real-key", "gpt-oss-120b"),
            "global": ("openrouter", glob.base, "not-a-real-key", "some-model"),
        })
        monkeypatch.setattr(llm, "current_google_key", lambda: None)
        llm.reset_provider_cache()

    monkeypatch.setattr(facts_drain, "_pauses", {})
    monkeypatch.setattr(facts_drain, "_timeouts", type(facts_drain._timeouts)())
    # A round reads its group's outcome once the group has ended, on a
    # loaded machine too (tests/test_drains.py, the same patience).
    monkeypatch.setattr(facts_drain, "_DRAIN_PATIENCE", 60.0)
    yield writers
    for p in made.values():
        p.close()
    llm.reset_provider_cache()
    extensions.reset()


def a_memory_in_europe(db, tenant) -> uuid.UUID:
    project = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="eu", slug="eu-project",
                           region="eu")
    db.add(project)
    db.commit()
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project.id,
                    end_user_id="anna", content="Anna lives in Lisbon.",
                    graph_sync_status="pending")
    db.add(m)
    db.commit()
    return m.id


def drained_once(db, memory_id):
    out = facts_service.process_pending_memories(db, limit=10)
    assert wait_idle(facts_drain._draining, 60), "the drain's group did not end"
    db.expire_all()
    return out, db.get(AgentMemory, memory_id).graph_sync_status


def test_a_connection_dropped_once_is_asked_again_and_the_memory_is_written(db, tenant,
                                                                             providers):
    eu, glob = Provider("drop", "answer"), Provider("answer")
    providers(eu, glob)
    mid = a_memory_in_europe(db, tenant)

    started = time.monotonic()
    out, status = drained_once(db, mid)

    assert status == "done", (out, status)
    assert facts_drain._drain_paused("eu") == 0, "one dropped connection rested Europe"
    assert eu.calls == 2, "the dropped call was asked again once, and answered"
    assert glob.calls == 0, "never written in the other region"
    # Asked again after the SDKs' first wait (0.5 s, up to a quarter less),
    # not after the drain's minute.
    assert time.monotonic() - started < 30


def test_a_writer_that_never_answers_is_asked_twice_more_and_then_rests(db, tenant, providers):
    eu, glob = Provider("drop"), Provider("answer")
    providers(eu, glob)
    mid = a_memory_in_europe(db, tenant)

    out, status = drained_once(db, mid)

    assert status == "pending", "a writer down is no fault of the memory"
    assert eu.calls == 3, "bounded: the call and two more"
    assert facts_drain._drain_paused("eu") > 0, "an outage the retries did not get past rests"
    assert glob.calls == 0, "no fallback from eu to global, ever"
    # Global still writes, so the pass is not `paused` as a whole: Europe's
    # memory is simply not claimed while its region rests.
    again = facts_service.process_pending_memories(db, limit=10)
    assert again["claimed"] == 0
    assert eu.calls == 3, "nothing sent while the region rests"


@pytest.mark.parametrize("refusal", [401, 402, 403])
def test_a_refused_key_or_credit_is_not_asked_again_and_rests(db, tenant, providers, refusal):
    eu, glob = Provider(refusal), Provider("answer")
    providers(eu, glob)
    mid = a_memory_in_europe(db, tenant)

    out, status = drained_once(db, mid)

    assert status == "pending"
    assert eu.calls == 1, "a refusal that waits for a person is never asked again"
    assert facts_drain._drain_paused("eu") > 0
    assert facts_drain.writers_refused()["eu"]["status"] == refusal
    assert glob.calls == 0


def test_a_region_the_service_stops_sends_nothing_and_rests(db, tenant, providers):
    eu, glob = Provider("answer"), Provider("answer")
    providers(eu, glob)

    def stop(vendor, model, region):
        if region == "eu":
            raise llm.NotNow("the European month is spent")

    extensions.register("before_model_call", stop)
    mid = a_memory_in_europe(db, tenant)

    out, status = drained_once(db, mid)

    assert status == "pending"
    assert eu.calls == 0 and glob.calls == 0
    assert facts_drain._drain_paused("eu") > 0


def test_a_call_that_times_out_is_still_not_asked_again_here(db, tenant, providers,
                                                              monkeypatch):
    """A timeout costs LLM_TIMEOUT_SECONDS a try: the drain counts it against
    the memory (`_times_out_on_it`) instead of the call trying three times."""
    eu, glob = Provider(("slow", 2.0)), Provider("answer")
    providers(eu, glob)
    monkeypatch.setattr(llm, "_model_call_seconds", lambda: 0.5)
    mid = a_memory_in_europe(db, tenant)

    out, status = drained_once(db, mid)

    assert status == "pending"
    assert eu.calls == 1
    assert glob.calls == 0
