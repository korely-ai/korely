"""The decisions port (core/llm.DecisionsProvider, memory_core 6.20.0's review
of subjects): every call passes the service's gate first, is billed with its
tokens to the account it runs for, sends the decisions API's shape, and an
answer with no choice is no answer. Faked at urlopen: no network."""
import io
import json

import pytest

from korely_agent.core import llm


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Wire:
    """What was sent, and what the API answers."""

    def __init__(self):
        self.calls = []
        self.reply = {"answers": {"verdict": {"choice": "same", "probabilities": {"same": 0.97}}},
                      "usage": {"prompt_tokens": 1200, "completion_tokens": 0, "cost": 0.00005}}

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url, json.loads(req.data.decode()), dict(req.header_items())))
        return Answer(json.dumps(self.reply).encode())


@pytest.fixture
def wire(monkeypatch):
    w = Wire()
    monkeypatch.setattr(llm.urllib.request, "urlopen", w.urlopen)
    return w


def port():
    return llm.DecisionsProvider("sk-or-test", model="typesafe/jev-1.13", region="global")


def test_the_call_has_the_decisions_shape_and_reads_the_choice(wire):
    assert port().choose({"A": {"name": "Cloud"}}, "Are A and B two things?",
                         {"same": "one", "different": "two"}) == "same"
    [(url, body, headers)] = wire.calls
    assert url == "https://openrouter.ai/api/alpha/decisions"
    assert body == {"model": "typesafe/jev-1.13", "state": {"A": {"name": "Cloud"}},
                    "questions": {"verdict": {"type": "choice", "instructions": "Are A and B two things?",
                                              "criteria": {"same": "one", "different": "two"}}}}
    assert headers["Authorization"] == "Bearer sk-or-test"


def test_an_answer_with_no_choice_is_none(wire):
    wire.reply = {"answers": {"verdict": {"probabilities": {"same": 1.0}}}}
    assert port().choose({}, "q", {"same": "one"}) is None


def test_the_gate_refuses_before_anything_is_sent(wire, monkeypatch):
    from korely_agent.core import extensions

    seen = []

    def refuse(vendor, model, region):
        seen.append((vendor, model, region))
        raise llm.NotNow("the daily stop")
    monkeypatch.setattr(extensions.hooks, "before_model_call", refuse)
    with pytest.raises(llm.NotNow):
        port().choose({}, "q", {"same": "one"})
    assert seen == [("openrouter", "typesafe/jev-1.13", "global")]
    assert wire.calls == [], "nothing was sent"


def test_the_call_is_billed_to_the_account(db, tenant, wire):
    from korely_agent.services import llm_usage

    with llm_usage.attributed(tenant.id, stage=llm_usage.OPERATOR) as scope:
        port().choose({}, "q", {"same": "one"})
        [call] = scope.calls
    assert (call.model, call.prompt_tokens, call.completion_tokens) == ("typesafe/jev-1.13", 1200, 0)


@pytest.mark.parametrize("usage,asked,unpriced", [
    (None, 1, True),                                          # no measure: stops, never free
    ({"input_tokens": 1200, "output_tokens": 3}, 1, False),   # the other names of the tokens
    ({"cost": 0.01}, 1, False),                               # only the provider's cost
    ({"prompt_tokens": 1200, "completion_tokens": 3}, 1, False),
    ({"prompt_tokens": 10, "completion_tokens": 0}, 50, False),   # cheap and measured: runs on
], ids=["no-usage", "other-names", "cost-only", "openai-names", "cheap"])
def test_the_cap_counts_every_measure_and_stops_on_none(db, tenant, wire, usage, asked, unpriced):
    """The review of 23 Nov 2026, P1: an answer with no `usage`, or with its
    tokens under other names, or with only a cost, counted nothing, and the
    cap never stopped the run."""
    from korely_agent.manage import _Spending
    from korely_agent.services import llm_usage

    wire.reply = {"answers": {"verdict": {"choice": "none"}}}
    if usage is not None:
        wire.reply["usage"] = usage
    card = {"currency": "USD", "models": {"typesafe/jev-1.13": {"input_per_million": 1.0,
                                                                "output_per_million": 1.0}}}
    spent = _Spending(card, 0.001)
    n = 0
    with llm_usage.attributed(tenant.id, stage=llm_usage.OPERATOR) as scope:
        go = spent.before_each_of(scope)
        for _ in range(50):
            if not go():
                break
            port().choose({}, "q", {"none": "x"})
            n += 1
    assert (n, spent.unpriced) == (asked, unpriced)
