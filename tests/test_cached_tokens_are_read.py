"""The prompt tokens a provider served from its cache are recorded (2026-10-07).

The hosted product reads them since its r65; Scaleway caches prefixes by itself
and bills them less, Gemini too. Both clients of core/llm.py read the
provider's field and pass it to the bill, within `prompt_tokens`; a provider
that says nothing is 0, nothing known to be cached.
"""
import json
from types import SimpleNamespace

import pytest

from korely_agent.core import llm


class _Answer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def billed(monkeypatch):
    out = []
    monkeypatch.setattr(llm, "_bill", lambda *a, **k: out.append((a, k)))
    return out


def openai_compatible(monkeypatch, usage):
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout=None: _Answer({
        "choices": [{"message": {"content": "ok"}}], "usage": usage}))
    llm.MistralProvider("k", "https://api.scaleway.ai/v1").complete(
        messages=[{"role": "user", "content": "x"}], model="gpt-oss-120b")


def test_the_openai_compatible_client_reads_cached_tokens(monkeypatch, billed):
    openai_compatible(monkeypatch, {"prompt_tokens": 3753, "completion_tokens": 367,
                                    "prompt_tokens_details": {"cached_tokens": 3072}})
    (_provider, _model, prompt, completion), kw = billed[-1]
    assert (prompt, completion, kw["cached"]) == (3753, 367, 3072)


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 10, "completion_tokens": 2},
    {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": None},
])
def test_no_field_is_zero(monkeypatch, billed, usage):
    openai_compatible(monkeypatch, usage)
    assert billed[-1][1]["cached"] == 0


def test_gemini_reads_its_cached_content(monkeypatch, billed):
    meta = SimpleNamespace(prompt_token_count=3800, candidates_token_count=300,
                           thoughts_token_count=0, cached_content_token_count=2900)
    answer = SimpleNamespace(text="ok", usage_metadata=meta)
    models = SimpleNamespace(generate_content=lambda **k: answer)
    monkeypatch.setattr(llm, "_gemini_client", lambda key: SimpleNamespace(models=models))
    llm.GeminiProvider("k").complete([{"role": "user", "content": "x"}], model="gemini-3.1-flash-lite")
    (_provider, _model, prompt, completion), kw = billed[-1]
    assert (prompt, completion, kw["cached"]) == (3800, 300, 2900)
