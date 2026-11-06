"""The provider page's key test keeps its Gemini client alive for the call
(the upgrade check of 0.1.41, 18 Oct 2026). `_gemini_client(raw).models
.embed_content(...)` dropped the client after reading `.models`; the SDK's
client closes its connection when collected, and every key pasted on the
screen came back "Cannot send a request, as the client has been closed"."""
from korely_agent.core import llm


class _Api:
    closed = False


class _Models:
    def __init__(self, api):
        self._api = api

    def embed_content(self, **kw):
        if self._api.closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        return object()


class _Client:
    """Like google.genai.Client: `models` does not hold the client, and the
    client closes the connection they share when it is collected."""

    def __init__(self, api_key):
        self._api = _Api()
        self.models = _Models(self._api)

    def __del__(self):
        self._api.closed = True


def test_the_key_is_tried_with_a_live_client(monkeypatch):
    monkeypatch.setattr(llm, "_gemini_client", _Client)
    monkeypatch.setattr(llm, "_before_the_call", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_bill_gemini_vector", lambda *a, **k: None)
    llm.try_google_key("AIza-a-key-being-tried")
