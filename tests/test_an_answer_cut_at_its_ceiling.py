"""An answer the writer cut at its output ceiling is not "no facts" (1.0.38).

The date test of 24 Nov 2026 (gpt-oss-120b through OpenRouter as the
writer) left 15 of its 90 memories without a fact, filed 'error' at their
first try, until somebody ran `manage reextract`. In 10 of them a call of the
write had stopped at its ceiling of output tokens: the provider says so in
its own field (`finish_reason: "length"`, Gemini's `MAX_TOKENS`), and
core/llm dropped it, so a cut answer was "not usable JSON" like any other.

What happens now, read from the provider's field and never from the text:
the call is asked once more with twice the room (`core.llm.complete_whole`);
cut again, the memory goes back to the queue, counted against itself on
every try without resting its region (the writer answered), and the fifth
try files it 'error', where `status` counts it and `reextract` re-drives it.

Hermetic: the real providers' code, with the HTTP call (urllib) or the
Gemini client answered here; the network is closed (tests/conftest.py).
"""
import json
import uuid
from types import SimpleNamespace

import pytest

from korely_agent.core import llm
from korely_agent.db.models import AgentMemory
from korely_agent.services.facts import drain as facts_drain
from korely_agent.services.facts import service as facts_service


# JSON a writer stopped in the middle of: what a cut extraction looks like.
CUT_JSON = '{"facts": [{"subject": "Mina", "predicate": "has_trainer", "obj'
WHOLE_NO_FACTS = '{"facts": [], "observation": ""}'


class _Reply:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._body).encode()


class OpenAICompatibleHost:
    """The HTTP side of an OpenAI-compatible host (OpenRouter, Scaleway):
    each call answers the next of `script`, a (content, finish_reason)
    pair, the last one repeated; every payload sent is kept."""

    def __init__(self, monkeypatch, script):
        self.script = list(script)
        self.sent = []
        monkeypatch.setattr(llm.urllib.request, "urlopen", self.urlopen)

    def urlopen(self, request, timeout=None):
        self.sent.append(json.loads(request.data.decode()))
        content, finish = self.script[min(len(self.sent), len(self.script)) - 1]
        return _Reply({"choices": [{"message": {"content": content}, "finish_reason": finish}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 7}})

    def ceilings(self):
        return [p["max_tokens"] for p in self.sent]


def _provider():
    return llm.MistralProvider("not-a-real-key", "https://openrouter.invalid/api/v1",
                               force_model="openai/gpt-oss-120b", vendor="openrouter")


@pytest.fixture(autouse=True)
def _clean_drain(monkeypatch):
    facts_drain._pauses.clear()
    facts_drain._timeouts.clear()
    facts_drain._written.clear()
    llm.forget_failure_here()
    # These tests read what a pass did once its groups have ended
    # (tests/test_drains.py, the same patience for the same reason).
    monkeypatch.setattr(facts_drain, "_DRAIN_PATIENCE", 60.0)
    yield
    facts_drain._pauses.clear()
    facts_drain._timeouts.clear()
    facts_drain._written.clear()
    llm.forget_failure_here()


class TestTheProviderSaysTheAnswerWasCut:
    def test_an_openai_compatible_length_is_a_cut_answer(self, monkeypatch):
        OpenAICompatibleHost(monkeypatch, [(CUT_JSON, "length")])
        out = _provider().complete([{"role": "user", "content": "hi"}], max_tokens=64)
        assert out.cut is True and out.text == CUT_JSON
        assert llm.last_kind_here() == llm.CUT_OFF
        assert llm.last_status_here() is None
        # The provider answered: /health keeps saying so.
        assert llm._last_call["ok"] is True

    def test_a_stop_is_a_whole_answer_and_no_failure(self, monkeypatch):
        OpenAICompatibleHost(monkeypatch, [(WHOLE_NO_FACTS, "stop")])
        out = _provider().complete([{"role": "user", "content": "hi"}], max_tokens=64)
        assert out.cut is False
        assert llm.last_kind_here() is None and llm.last_failure_here() is None

    def test_the_text_never_decides(self, monkeypatch):
        """Broken JSON with `stop` is not a cut answer: only the field says so."""
        OpenAICompatibleHost(monkeypatch, [(CUT_JSON, "stop")])
        out = _provider().complete([{"role": "user", "content": "hi"}], max_tokens=64)
        assert out.cut is False and llm.last_kind_here() is None

    @pytest.mark.parametrize("finish, cut", [("MAX_TOKENS", True), ("STOP", False)])
    def test_gemini_max_tokens_is_a_cut_answer(self, monkeypatch, finish, cut):
        from google.genai import types

        class Models:
            def generate_content(self, **kw):
                self.config = kw["config"]
                return SimpleNamespace(
                    text=CUT_JSON,
                    candidates=[SimpleNamespace(finish_reason=getattr(types.FinishReason, finish))],
                    usage_metadata=SimpleNamespace(candidates_token_count=64, thoughts_token_count=0,
                                                   prompt_token_count=5,
                                                   cached_content_token_count=0))

        models = Models()
        monkeypatch.setattr(llm, "_gemini_client", lambda key: SimpleNamespace(models=models))
        out = llm.GeminiProvider("not-a-real-key").complete(
            [{"role": "user", "content": "hi"}], model="gemini-3.1-flash-lite", max_tokens=64)
        assert out.cut is cut
        assert llm.last_kind_here() == (llm.CUT_OFF if cut else None)


class TestMoreRoomOnce:
    def test_a_cut_answer_is_asked_again_with_twice_the_room(self, monkeypatch):
        host = OpenAICompatibleHost(monkeypatch, [(CUT_JSON, "length"), (WHOLE_NO_FACTS, "stop")])
        out = llm.complete_whole(_provider(), [{"role": "user", "content": "hi"}], max_tokens=100)
        assert host.ceilings() == [100, 200]
        assert out.text == WHOLE_NO_FACTS and out.cut is False
        # The second call answered whole: no failure is left for the drain.
        assert llm.last_kind_here() is None

    def test_only_once(self, monkeypatch):
        host = OpenAICompatibleHost(monkeypatch, [(CUT_JSON, "length")])
        out = llm.complete_whole(_provider(), [{"role": "user", "content": "hi"}], max_tokens=100)
        assert host.ceilings() == [100, 200]
        assert out.cut is True and llm.last_kind_here() == llm.CUT_OFF

    def test_a_whole_answer_is_asked_once(self, monkeypatch):
        host = OpenAICompatibleHost(monkeypatch, [(WHOLE_NO_FACTS, "stop")])
        llm.complete_whole(_provider(), [{"role": "user", "content": "hi"}], max_tokens=100)
        assert host.ceilings() == [100]


    def test_whole_json_with_blanks_up_to_the_ceiling_is_kept(self, monkeypatch):
        """A JSON-mode answer that closed its object and wrote blanks up to
        the ceiling comes with "length" too: it can be read, so it is kept."""
        host = OpenAICompatibleHost(monkeypatch, [(WHOLE_NO_FACTS + "\n" * 500, "length")])
        out = llm.complete_whole(_provider(), [{"role": "user", "content": "hi"}], max_tokens=100)
        assert host.ceilings() == [100]
        assert json.loads(out.text) == {"facts": [], "observation": ""}
        assert llm.last_kind_here() is None, "a kept answer left a failure for the drain"

    def test_one_whole_fact_inside_a_cut_list_is_not_whole(self, monkeypatch):
        """The reader's second step (the one object inside the text) is no
        proof: a list cut after its first fact holds that fact whole."""
        one_fact = '{"facts": [{"subject": "Mina", "predicate": "has_trainer", "object": "B"}, {"su'
        host = OpenAICompatibleHost(monkeypatch, [(one_fact, "length"), (WHOLE_NO_FACTS, "stop")])
        llm.complete_whole(_provider(), [{"role": "user", "content": "hi"}], max_tokens=100)
        assert host.ceilings() == [100, 200]


class TestEveryCallOfTheWriteGetsMoreRoom:
    """The vocabulary's and the judge's adapters, not memory_core's: each
    asks again with twice its own ceiling."""

    def _writer(self, monkeypatch):
        host = OpenAICompatibleHost(monkeypatch, [('{"same_as": nu', "length"),
                                                  ('{"same_as": null}', "stop")])
        provider = _provider()
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: provider)
        return host

    def test_the_vocabulary(self, monkeypatch):
        from korely_agent.services import predicates

        host = self._writer(monkeypatch)
        assert predicates._ask()("same relation?", False) == '{"same_as": null}'
        assert host.ceilings() == [2048, 4096]

    @pytest.mark.parametrize("batched", [False, True])
    def test_the_judge(self, monkeypatch, batched):
        from korely_agent.services.facts import contradiction
        from korely_agent.services.memory_core import judge as _judge

        host = self._writer(monkeypatch)
        heard = []

        def judge(call, *a, **k):
            heard.append(call("EXISTING facts: ...", 8192))
            return {} if batched else ([], {})

        monkeypatch.setattr(_judge, "judge_batch" if batched else "judge", judge)
        if batched:
            contradiction._classify_batch([{"new_id": "n", "new_text": "t", "candidates": [1]}],
                                          None, uuid.uuid4())
        else:
            contradiction._classify([object()], "Mina", "has_trainer", "B", None, uuid.uuid4(),
                                    "openai/gpt-oss-120b")
        assert heard == ['{"same_as": null}']
        assert host.ceilings() == [8192, 16384]


def _one_memory(db, tenant):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="datetest-b",
                    content="Mina books her sessions with Bertrand on Wednesdays.",
                    graph_sync_status="pending")
    db.add(m)
    db.commit()
    return m.id


def _status(db, mid):
    db.expire_all()
    return db.get(AgentMemory, mid).graph_sync_status


class TestTheDrainTriesACutMemoryAgain:
    """The whole write, as the drain runs it: the real extraction adapter,
    memory_core's reader and the real provider, on an answered HTTP call."""

    def _writer(self, monkeypatch, script):
        host = OpenAICompatibleHost(monkeypatch, script)
        provider = _provider()
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: provider)
        return host

    def test_more_room_that_helps_ends_done(self, db, tenant, monkeypatch):
        host = self._writer(monkeypatch, [(CUT_JSON, "length"), (WHOLE_NO_FACTS, "stop")])
        mid = _one_memory(db, tenant)
        r = facts_service.process_pending_memories(db, limit=10)
        assert r["processed"] == 1 and r["failed"] == 0
        assert _status(db, mid) == "done"
        assert host.ceilings()[:2] == [8192, 16384]

    def test_cut_on_every_try_waits_then_is_filed_on_the_fifth(self, db, tenant, monkeypatch):
        host = self._writer(monkeypatch, [(CUT_JSON, "length")])
        mid = _one_memory(db, tenant)
        for attempt in range(1, facts_drain._TIMEOUTS_PER_MEMORY):
            r = facts_service.process_pending_memories(db, limit=10)
            assert r["failed"] == 0, f"filed 'error' on try {attempt}"
            assert _status(db, mid) == "pending", f"not back in the queue after try {attempt}"
            assert facts_drain._drain_paused() == 0, "the region rested: the writer answered"
        r = facts_service.process_pending_memories(db, limit=10)
        assert r["failed"] == 1 and _status(db, mid) == "error"
        assert not facts_drain._timeouts
        # Each try asked twice, the second with twice the room.
        assert host.ceilings() == [8192, 16384] * facts_drain._TIMEOUTS_PER_MEMORY

    def test_an_unreadable_answer_that_was_not_cut_is_still_an_error_at_once(self, db, tenant,
                                                                             monkeypatch):
        """The control: without it the test above passes on a drain that puts
        every failed memory back in the queue."""
        host = self._writer(monkeypatch, [(CUT_JSON, "stop")])
        mid = _one_memory(db, tenant)
        r = facts_service.process_pending_memories(db, limit=10)
        assert r["failed"] == 1 and _status(db, mid) == "error"
        assert host.ceilings() == [8192]

    def test_the_log_says_the_numbers_never_the_words(self, db, tenant, monkeypatch, caplog):
        import logging

        self._writer(monkeypatch, [(CUT_JSON, "length")])
        mid = _one_memory(db, tenant)
        with caplog.at_level(logging.WARNING):
            for _ in range(facts_drain._TIMEOUTS_PER_MEMORY):
                facts_service.process_pending_memories(db, limit=10)
        assert _status(db, mid) == "error"
        assert "stopped its answer at the ceiling of 8192 output tokens" in caplog.text
        assert "it waits for the next pass, the region does not rest" in caplog.text
        assert "ceiling on 5 tries: filed as an error" in caplog.text
        assert "Mina" not in caplog.text and "Bertrand" not in caplog.text
