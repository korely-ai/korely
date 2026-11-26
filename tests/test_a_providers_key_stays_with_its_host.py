"""A model provider is known by the host of its address, and its key goes
to that host alone.

The host (the review of 4 Nov 2026): the particulars of a provider were read
from substrings of the URL, with their letter case, so a MISTRAL_API_BASE
written https://OpenRouter.ai/... lost OpenRouter's reasoning field, and
any address that held "openrouter.ai" in another host's name or in its path
got it. A host name has no letter case (RFC 3986 section 3.2.2).

The key (the review of 16 Nov 2026): urllib follows a 301, 302 or 303,
a POST as a GET, and copied the Authorization header to whatever host the
redirect named. Tried here with two servers on the loopback: the provider's
answers 302 to the other, which writes down what it is sent."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from korely_agent.core import llm

KEY = "FAKE-provider-key-0123456789"


@pytest.mark.parametrize("base,openrouter,openai", [
    ("https://openrouter.ai/api/v1", True, False),
    ("https://OpenRouter.AI/api/v1", True, False),
    ("https://openrouter.ai.example.net/v1", False, False),
    ("https://proxy.example.net/openrouter.ai/v1", False, False),
    ("https://api.openai.com/v1", False, True),
    ("https://API.OpenAI.com/v1", False, True),
    ("https://api.openai.com.example.net/v1", False, False),
    ("https://example.net/v1?api.openai.com", False, False),
    ("not a url", False, False),
    ("https://[openrouter.ai/v1", False, False),
    # A final dot is the same host, fully qualified; a port and a user part
    # are not the host's name (the review of 26 Nov 2026, mutant M05).
    ("https://openrouter.ai./api/v1", True, False),
    ("https://openrouter.ai:443/api/v1", True, False),
    ("https://someone@openrouter.ai/api/v1", True, False),
    ("https://someone:pw@API.openai.com:8443/v1", False, True),
    ("https://openrouter.ai@proxy.example.net/v1", False, False),
])
def test_a_provider_is_its_host(base, openrouter, openai):
    sent = llm._least_reasoning(base, "gpt-oss-120b")
    assert ("reasoning" in sent) is openrouter, (base, sent)
    payload = {"model": "gpt-5-mini", "max_tokens": 10, "temperature": 0.1}
    llm._openai_reasoning_fields(base, payload)
    assert ("reasoning_effort" in payload) is openai, (base, payload)


class _Seen:
    def __init__(self):
        self.requests = []


def _server(handler_for):
    seen = _Seen()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self):
            seen.requests.append({"method": self.command, "path": self.path,
                                  "headers": {k.lower(): v for k, v in self.headers.items()}})
            handler_for(self)

        do_GET = do_POST = _answer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture
def elsewhere():
    """The host a redirect points at: it answers 200 with a completion, so a
    call that reaches it with the key would succeed."""
    def answer(h):
        body = json.dumps({"choices": [{"message": {"content": "from elsewhere"}}],
                           "usage": {}, "data": []}).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    server, seen = _server(answer)
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()


@pytest.fixture
def redirecting(elsewhere):
    target, _ = elsewhere

    def answer(h):
        h.send_response(302)
        h.send_header("Location", target + h.path)
        h.send_header("Content-Length", "0")
        h.end_headers()

    server, seen = _server(answer)
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", seen
    server.shutdown()


@pytest.fixture
def quiet(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(
        llm_timeout_seconds=5, mistral_extraction_model="m"))
    monkeypatch.setattr(llm, "_record", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_failed", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_bill", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_before_the_call", lambda *a, **k: None)
    monkeypatch.setattr("time.sleep", lambda s: None)


def _key_reached(seen) -> bool:
    return any(KEY in json.dumps(r["headers"]) for r in seen.requests)


def test_a_write_s_key_does_not_follow_a_redirect(quiet, redirecting, elsewhere):
    base, first = redirecting
    _target, second = elsewhere
    try:
        llm.MistralProvider(KEY, base).complete([{"role": "user", "content": "hi"}])
    except Exception:  # noqa: BLE001 - the call may fail; the key must not travel
        pass
    assert first.requests and first.requests[0]["headers"]["authorization"] == f"Bearer {KEY}"
    assert not _key_reached(second), second.requests


def test_the_decisions_model_s_key_does_not_follow_a_redirect(quiet, redirecting, elsewhere):
    base, first = redirecting
    _target, second = elsewhere
    try:
        llm.DecisionsProvider(KEY, model="m", api_base=base).choose({}, "?", {"a": "1"})
    except Exception:  # noqa: BLE001
        pass
    assert first.requests and _key_reached(first)
    assert not _key_reached(second), second.requests


def test_the_models_list_s_key_does_not_follow_a_redirect(redirecting, elsewhere):
    from korely_agent.services import model_choice

    base, first = redirecting
    _target, second = elsewhere
    try:
        model_choice._get(f"{base}/models", model_choice._bearer(KEY))
    except Exception:  # noqa: BLE001
        pass
    assert first.requests and _key_reached(first)
    assert not _key_reached(second), second.requests
