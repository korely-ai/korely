"""The model layer.

Nothing here talks to a network. What is worth testing offline is the parser
that turns a model's answer into something we can load, and the rule that a
missing provider is visible rather than silent.
"""
import pytest

from korely_agent.core import llm


class TestExtractJson:
    """Models do not return what you asked for, they return something close."""

    def test_bare_object(self):
        assert llm.extract_json('{"a": 1}') == '{"a": 1}'

    def test_markdown_fence(self):
        assert llm.extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_fence_without_language(self):
        assert llm.extract_json('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_leading_commentary(self):
        """Asking for JSON only does not reliably get JSON only."""
        raw = 'Sure! Here is the JSON you asked for:\n{"a": 1}'
        assert llm.extract_json(raw) == '{"a": 1}'

    def test_trailing_commentary(self):
        raw = '{"a": 1}\n\nLet me know if you need anything else.'
        assert llm.extract_json(raw) == '{"a": 1}'

    def test_array_at_top_level(self):
        assert llm.extract_json('```json\n[{"a": 1}]\n```') == '[{"a": 1}]'

    def test_nested_braces_keep_the_last_close(self):
        """Stopping at the first '}' would truncate every nested object."""
        raw = '{"outer": {"inner": 1}}'
        assert llm.extract_json(raw) == raw

    def test_prose_with_a_brace_in_it(self):
        """A '{' inside commentary before the real payload is a false start.

        Not currently handled, and pinned so the behaviour is a decision rather
        than a surprise: the extractor starts at the first bracket it sees.
        """
        out = llm.extract_json('I will use {} notation:\n{"a": 1}')
        assert out.startswith("{")

    @pytest.mark.parametrize("raw", ["", "   ", "no json here at all", "```json\n```"])
    def test_nothing_usable_returns_empty(self, raw):
        """Empty string, never an exception: the caller decides if it is fatal."""
        assert llm.extract_json(raw) == ""

    def test_result_is_loadable(self):
        import json
        raw = 'Here you go:\n```json\n{"facts": [{"s": "Maria", "p": "likes"}]}\n```\nDone.'
        assert json.loads(llm.extract_json(raw))["facts"][0]["s"] == "Maria"


class TestProviderSelection:
    def setup_method(self):
        llm.reset_provider_cache()

    def teardown_method(self):
        llm.reset_provider_cache()

    def test_no_key_means_no_provider(self, monkeypatch):
        monkeypatch.setattr(llm, "get_settings", lambda: _settings(google_api_key=None))
        assert llm.get_provider() is None

    def test_mistral_provider_ignores_a_gemini_key(self, monkeypatch):
        """Configuring Mistral and setting only a Gemini key must not silently
        fall back to Gemini: the operator chose a provider for a reason, and in
        the EU-processor case that reason is contractual."""
        monkeypatch.setattr(llm, "get_settings", lambda: _settings(
            extraction_provider="mistral", google_api_key="g", mistral_api_key=None))
        assert llm.get_provider() is None

    def test_construction_failure_is_not_an_exception(self, monkeypatch):
        """A broken SDK must degrade to 'no provider', not crash a request."""
        monkeypatch.setattr(llm, "get_settings", lambda: _settings(google_api_key="k"))
        class Boom:
            def __init__(self, *a, **k): raise RuntimeError("sdk missing")
        monkeypatch.setattr(llm, "GeminiProvider", Boom)
        assert llm.get_provider() is None


def _settings(**over):
    from korely_agent.config import AgentSettings
    base = dict(database_url="postgresql://u:p@h/d", google_api_key="k")
    base.update(over)
    return AgentSettings(**base)


def test_embedding_width_matches_the_column():
    """The constant and the schema must agree, or every write fails at INSERT.

    Found by an end-to-end run with a real key: the model returns 3072
    dimensions by default, the column is Vector(768), and the mismatch surfaces
    only after the model call has been made and paid for. Nothing offline
    catches it, so pin the two together here.
    """
    from korely_agent.core.llm import EMBEDDING_DIM
    from korely_agent.db.base import Base

    for table in ("agent_facts", "agent_memories"):
        col = Base.metadata.tables[table].c.embedding
        assert col.type.dim == EMBEDDING_DIM, (
            f"{table}.embedding is Vector({col.type.dim}) but the embedder asks "
            f"for {EMBEDDING_DIM}"
        )


def test_the_width_and_the_cut_are_written_once():
    """Audit of 17 Oct 2026: 768 was a literal in five places of the product
    and a memory's text was cut at 8,000 characters in two, twice on the
    vector drain's path. One literal each now: core/embedding_model.DIMENSIONS
    and memory_engine.VECTOR_CHARS (alembic's history keeps its own)."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "korely_agent"
    widths, cuts = [], []
    for path in sorted(root.rglob("*.py")):
        # A part's chain of migrations keeps its own, as alembic's does.
        if "memory_core" in path.parts or "migrations" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, ast.Constant) and type(n.value) is int and n.value == 768:
                widths.append(rel)
            elif isinstance(n, ast.Slice) and n.upper is not None:
                cuts += [rel] if ast.unparse(n.upper) in ("8000", "VECTOR_CHARS") else []
    assert widths == ["core/embedding_model.py"]
    assert cuts == ["services/memory_engine.py"]


class TestTheGeminiClientHasADeadline:
    """The SDK defaults to no timeout, and this call is not on a background
    thread: extraction and canonicalization run inside the request that wrote
    the memory, holding an open transaction. Without a deadline a provider that
    accepts the connection and then stalls holds locks until something else
    gives up first. The Mistral path always passed timeout=60 to urlopen; the
    Gemini path passed nothing.

    These patch `llm.get_settings` rather than set an environment variable.
    `get_settings` is memoized, several tests re-import the config module, and a
    test that depends on which module object won that race is a test that fails
    for reasons having nothing to do with what it is checking. Here the claim is
    only that the configured number reaches the client, so that is all it needs.
    """

    def test_client_carries_the_configured_deadline(self, monkeypatch):
        from types import SimpleNamespace

        from korely_agent.core import llm

        monkeypatch.setattr(
            llm, "get_settings", lambda: SimpleNamespace(llm_timeout_seconds=17)
        )
        client = llm._gemini_client("not-a-real-key")
        # milliseconds, per HttpOptions
        assert client._api_client._http_options.timeout == 17_000

    def test_the_default_is_not_unbounded(self):
        """The point of the setting is that there is always a deadline."""
        from korely_agent.config import AgentSettings

        s = AgentSettings(database_url="postgresql://x:x@localhost/x")
        assert s.llm_timeout_seconds > 0

    def test_embedding_client_is_reused(self, monkeypatch):
        """Embedding runs on every write and every search. Building a client
        per call paid for a connection pool on the hottest path in the service.
        """
        from types import SimpleNamespace

        from korely_agent.core import llm

        monkeypatch.setattr(
            llm, "get_settings", lambda: SimpleNamespace(llm_timeout_seconds=60)
        )
        llm.reset_provider_cache()
        try:
            assert llm._embed_client("k1") is llm._embed_client("k1")
            assert llm._embed_client("k1") is not llm._embed_client("k2")
        finally:
            llm.reset_provider_cache()


class TestTheMistralCallHasTheSameDeadline:
    """LLM_TIMEOUT_SECONDS is documented in .env.example as how long "a model
    call" may take, and it reached only the Gemini client: the Mistral path
    kept a literal `timeout=60` in its `urlopen`. An operator who chose Mistral
    (the EU route in the README) and set a shorter deadline got sixty seconds
    anyway, with nothing saying the setting did not apply.
    """

    def test_the_mistral_call_carries_the_configured_deadline(self, monkeypatch):
        import json
        from types import SimpleNamespace

        monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(
            llm_timeout_seconds=17, mistral_extraction_model="mistral-small-latest"))
        monkeypatch.setattr(llm, "_record", lambda *a, **k: None)
        monkeypatch.setattr(llm, "_bill", lambda *a, **k: None)
        seen = {}

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "ok"}}],
                                   "usage": {}}).encode()

        def urlopen(request, timeout=None):
            seen["timeout"] = timeout
            return Reply()

        monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
        out = llm.MistralProvider("not-a-real-key", "https://mistral.invalid/v1").complete(
            [{"role": "user", "content": "hi"}])
        assert out.text == "ok"
        assert seen["timeout"] == 17


class TestGptOssWritesWithItsLeastReasoning:
    """The writer measured for the US column (2026-10-02) is gpt-oss-120b
    through OpenRouter at its least reasoning: 3.3 s per memory, $2.03 per
    thousand. That setting lived in a bench-only patch, and without it every
    short call on the write path paid a default chain of thought. Each host
    has its own field for it, and other models get no field at all."""

    def payload(self, monkeypatch, base, model):
        import json
        from types import SimpleNamespace

        monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(
            llm_timeout_seconds=17, mistral_extraction_model=model))
        monkeypatch.setattr(llm, "_record", lambda *a, **k: None)
        monkeypatch.setattr(llm, "_bill", lambda *a, **k: None)
        seen = {}

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}}],
                                   "usage": {}}).encode()

        def urlopen(request, timeout=None):
            seen.update(json.loads(request.data))
            return Reply()

        monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
        llm.MistralProvider("not-a-real-key", base).complete(
            [{"role": "user", "content": "hi"}], json_only=True)
        return seen

    def test_on_openrouter_the_unified_field(self, monkeypatch):
        p = self.payload(monkeypatch, "https://openrouter.ai/api/v1", "openai/gpt-oss-120b:nitro")
        assert p["reasoning"] == {"effort": "low"} and "reasoning_effort" not in p

    def test_on_scaleway_openais_field(self, monkeypatch):
        p = self.payload(monkeypatch, "https://api.scaleway.ai/v1", "gpt-oss-120b")
        assert p["reasoning_effort"] == "low" and "reasoning" not in p

    def test_openais_reasoning_models_get_their_own_fields(self, monkeypatch):
        """gpt-5/gpt-6 on OpenAI's own API refuse max_tokens and any
        temperature but theirs: the screen offers OpenAI, so the door adapts."""
        p = self.payload(monkeypatch, "https://api.openai.com/v1", "gpt-6-luna")
        assert "max_tokens" not in p and p["max_completion_tokens"] == 1024
        assert "temperature" not in p and p["reasoning_effort"] == "low"
        p = self.payload(monkeypatch, "https://api.openai.com/v1", "gpt-4.1-mini")
        assert p["max_tokens"] == 1024 and "reasoning_effort" not in p

    def test_other_models_get_nothing(self, monkeypatch):
        for base, model in [("https://api.scaleway.ai/v1", "llama-3.3-70b-instruct"),
                            ("https://openrouter.ai/api/v1", "mistralai/mistral-small-3.2-24b-instruct")]:
            p = self.payload(monkeypatch, base, model)
            assert "reasoning" not in p and "reasoning_effort" not in p
            assert p["response_format"] == {"type": "json_object"}


class TestHealthReportsWhatHappenedNotWhatIsConfigured:
    """`/health` said `extraction: available` for hours while the prepaid credit
    was gone and not one fact was being mined. The check read whether a key was
    SET.

    A store that cannot learn and reports that it is fine is the failure this
    codebase keeps finding in other people's code, and it was in its own health
    endpoint. So the state is now what the provider last DID.
    """

    def setup_method(self):
        from korely_agent.core import llm

        llm._last_call.update({"ok": None, "at": None, "error": None})

    def test_unknown_until_something_calls_it(self):
        from korely_agent.core.llm import provider_state

        assert provider_state()["state"] == "unknown"

    def test_a_success_is_remembered(self):
        from korely_agent.core import llm

        llm._record(True)
        st = llm.provider_state()
        assert st["state"] == "working"
        assert st["seconds_ago"] < 5

    def test_a_failure_is_remembered_with_its_reason(self):
        from korely_agent.core import llm

        llm._record(False, "ClientError: 429 RESOURCE_EXHAUSTED prepayment credits")
        st = llm.provider_state()
        assert st["state"] == "failing"
        assert "429" in st["detail"]

    def test_a_later_success_clears_a_failure(self):
        from korely_agent.core import llm

        llm._record(False, "429")
        llm._record(True)
        assert llm.provider_state()["state"] == "working"
        assert "detail" not in llm.provider_state()

    # With a key set, /health asks the store whether anything is mining
    # (main._extraction_now), so this needs the database. It set a placeholder
    # address and was green only when an earlier test had already built the
    # process's engine on the real one; in `make test` it was red (audit of
    # 17 Oct 2026).
    def test_the_endpoint_degrades_when_the_provider_fails(self, monkeypatch, engine):
        from fastapi.testclient import TestClient

        from korely_agent.core import llm

        monkeypatch.setenv("RUN_WORKER", "false")
        monkeypatch.setenv("GOOGLE_API_KEY", "set-but-broken")
        from korely_agent import config

        config.get_settings.cache_clear()
        from korely_agent.main import create_app

        app = create_app()
        with TestClient(app) as c:
            llm._record(False, "ClientError: 429 prepayment credits are depleted")
            body = c.get("/health").json()
            assert body["status"] == "degraded"
            assert body["extraction"]["state"] == "failing"
            assert "429" in body["extraction"]["detail"]

            llm._record(True)
            assert c.get("/health").json()["status"] == "ok"
        config.get_settings.cache_clear()


class TestTheThinkingSetting:
    """`reasoning=False` has to produce a request the model will accept.

    Found on a real install, by a tester who had never seen this code. Every
    predicate resolution call had been returning 400 INVALID_ARGUMENT since the
    default model moved from gemini-2.5-flash to gemini-3.6-flash: 2.5 accepts a
    thinking budget of zero, 3.x refuses it and wants a level. Verified against
    a real key on gemini-3.6-flash, thinking_budget=0 -> 400, and
    thinking_level="low" -> 200 with seven output tokens, so the reason the knob
    exists at all survives the fix.

    The consequence was not a slower answer. It was that the one call deciding
    whether two relation phrases name the same relation never ran, so a plan
    change never met the plan it replaced and the store held two contradictory
    facts, both marked currently true.
    """

    def test_gemini_3_is_asked_for_a_level(self):
        from korely_agent.core.llm import _thinking_knob
        assert _thinking_knob("gemini-3.6-flash") == ("thinking_level", "low")

    def test_the_model_prefix_is_tolerated(self):
        from korely_agent.core.llm import _thinking_knob
        assert _thinking_knob("models/gemini-3.6-flash") == ("thinking_level", "low")

    def test_gemini_2_5_keeps_the_budget_of_zero(self):
        from korely_agent.core.llm import _thinking_knob
        assert _thinking_knob("gemini-2.5-flash") == ("thinking_budget", 0)

    def test_a_later_gemini_is_assumed_to_behave_like_3(self):
        """Two digits sort before 3 as text, which is the bug this avoids."""
        from korely_agent.core.llm import _thinking_knob
        assert _thinking_knob("gemini-12.0-pro") == ("thinking_level", "low")

    def test_an_unknown_model_falls_back_to_the_older_shape(self):
        from korely_agent.core.llm import _thinking_knob
        assert _thinking_knob("") == ("thinking_budget", 0)

    @staticmethod
    def _refusal(code, status, message="Request contains an invalid argument."):
        from google.genai.errors import APIError
        return APIError(code, {"error": {"code": code, "status": status, "message": message}})

    def test_an_invalid_argument_is_recognised_as_the_knob_being_refused(self):
        from korely_agent.core.llm import _rejected_the_knob
        assert _rejected_the_knob(self._refusal(400, "INVALID_ARGUMENT"))

    def test_it_is_read_from_the_fields_not_the_words(self):
        """Audit of 17 Oct 2026: a refusal is a field. The status decides,
        whatever the message says or in whichever language it says it."""
        from korely_agent.core.llm import _rejected_the_knob
        assert _rejected_the_knob(self._refusal(400, "INVALID_ARGUMENT", "Argomento non valido."))
        assert not _rejected_the_knob(RuntimeError(
            "400 INVALID_ARGUMENT. Request contains an invalid argument."))
        assert not _rejected_the_knob(self._refusal(
            400, "FAILED_PRECONDITION", "Request contains an invalid argument."))

    def test_a_quota_error_is_not(self):
        """A retry without the knob would not help, and costs a second call."""
        from korely_agent.core.llm import _rejected_the_knob
        assert not _rejected_the_knob(self._refusal(429, "RESOURCE_EXHAUSTED", "quotaValue: 20"))


class TestAVectorOfTheWrongWidthIsAFailure:
    """`embed` refused a vector of the wrong width (the column would reject it
    at INSERT) and returned None without recording anything, so `/health`
    went on saying "working" from the last good call while every vector was
    being thrown away (2026-09-28). A misconfigured EMBEDDING_MODEL looked
    healthy for as long as anybody looked."""

    @pytest.fixture
    def wrong_width(self, monkeypatch):
        from types import SimpleNamespace

        reply = SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.1] * 3072)],
            usage_metadata=SimpleNamespace(prompt_token_count=4))
        client = SimpleNamespace(models=SimpleNamespace(embed_content=lambda **kw: reply))
        monkeypatch.setattr(llm, "get_settings",
                            lambda: SimpleNamespace(embedding_model="gemini-embedding-001",
                                                    embedding_provider="gemini"))
        monkeypatch.setattr(llm, "current_google_key", lambda: "k")
        monkeypatch.setattr(llm, "_embed_client", lambda key: client)
        for st in (llm._last_call, llm._last_embedding):
            st.update({"ok": None, "at": None, "error": None})
        yield
        for st in (llm._last_call, llm._last_embedding):
            st.update({"ok": None, "at": None, "error": None})

    def test_health_says_failing_and_why(self, wrong_width):
        llm._record(True, state=llm._last_embedding)   # the last good vector
        assert llm.embed("Maria lives in Rome.") is None
        st = llm.embedding_state()
        assert st["state"] == "failing", "a thrown-away vector left /health on 'working'"
        assert "3072" in st["detail"] and str(llm.EMBEDDING_DIM) in st["detail"]

    def test_a_vector_does_not_speak_for_the_extraction(self, wrong_width):
        """The audit of 2026-10-06, row 3: one state for both, and a vector
        that came back after a failed extraction said "working"."""
        llm._record(False, "ClientError: 404 model retired")
        llm._record(True, state=llm._last_embedding)
        assert llm.provider_state()["state"] == "failing"
        assert llm.embedding_state()["state"] == "working"

    def test_the_fact_path_reads_it_as_a_failed_call(self, wrong_width):
        """With a key set, no vector is a failure the caller can act on (the
        worker retries it), not "no embedder"."""
        with pytest.raises(llm.EmbeddingFailed):
            llm.embed_or_raise("Maria lives_in Rome")


class TestAHiccupOfTheDatabaseIsNotRemembered:
    """The first read of the screen's key or writer failing (Postgres
    restarting, a connection dropped) stood for "no key" / "nothing chosen"
    until the next save or restart (the audit of 2026-10-06, row 20)."""

    def test_the_key_is_read_again_once_the_database_answers(self, monkeypatch):
        from korely_agent import db as dbpkg
        from korely_agent.core import llm
        from korely_agent.services import provider_key

        monkeypatch.setattr(provider_key, "get_google_key", lambda s: "AIza" + "x" * 35)

        class Session:
            def close(self):
                pass

        def down():
            raise RuntimeError("could not connect to server")

        monkeypatch.setattr(dbpkg, "get_own_sessionmaker", down)
        monkeypatch.setattr(llm, "screen_writers", lambda: {})
        llm.reset_provider_cache()
        try:
            assert llm.current_google_key() is None, "the hiccup itself"
            monkeypatch.setattr(dbpkg, "get_own_sessionmaker", lambda: Session)
            assert llm.current_google_key() == "AIza" + "x" * 35
        finally:
            llm.reset_provider_cache()

    def test_the_writer_is_read_again_too(self, monkeypatch):
        from korely_agent.core import llm
        from korely_agent.services import model_choice

        calls = []

        def read():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("could not connect to server")
            return {"default": "eu", "global": None,
                    "eu": ("scaleway", "https://api.scaleway.ai/v1", "k", "gpt-oss-120b")}

        monkeypatch.setattr(model_choice, "read_writers", read)
        llm.reset_provider_cache()
        try:
            assert llm.screen_writers() == {}
            assert llm.screen_writers()["eu"] == ("scaleway", "https://api.scaleway.ai/v1", "k",
                                                  "gpt-oss-120b")
            assert llm.screen_writers()["eu"] is not None and len(calls) == 2, "then remembered"
        finally:
            llm.reset_provider_cache()

    def test_it_warns_once_per_streak(self, monkeypatch, caplog):
        from korely_agent.core import llm
        from korely_agent.services import model_choice

        def down():
            raise RuntimeError("could not connect to server")

        monkeypatch.setattr(model_choice, "read_writers", down)
        llm.reset_provider_cache()
        try:
            for _ in range(3):
                llm.screen_writers()
            warned = [r for r in caplog.records if r.levelname == "WARNING"
                      and "could not be read from the database" in r.getMessage()]
            assert len(warned) == 1
        finally:
            llm.reset_provider_cache()


class TestATransientRefusalIsAskedAgain:
    """A provider's 429 (or 408, 409, a 5xx) is asked again twice, after its
    Retry-After or 0.5 s doubling, the rule of openai-python and
    anthropic-sdk-python (16 Oct 2026). Before, one 429 sent the memory back
    to the queue and rested the region's drain for minutes: two writers on
    one Scaleway account wrote 7 memories a minute instead of 25."""

    def call(self, monkeypatch, answers):
        import io
        import json
        import urllib.error
        from types import SimpleNamespace

        monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(
            llm_timeout_seconds=17, mistral_extraction_model="m"))
        monkeypatch.setattr(llm, "_record", lambda *a, **k: None)
        monkeypatch.setattr(llm, "_bill", lambda *a, **k: None)
        slept = []
        monkeypatch.setattr("time.sleep", slept.append)
        calls = []

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "ok"}}], "usage": {}}).encode()

        def urlopen(request, timeout=None):
            calls.append(1)
            status, headers = answers.pop(0)
            if status == 200:
                return Reply()
            raise urllib.error.HTTPError(request.full_url, status, "refused", headers, io.BytesIO(b""))

        monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)

        def run():
            return llm.MistralProvider("not-a-real-key", "https://p.invalid/v1").complete(
                [{"role": "user", "content": "hi"}])
        return run, calls, slept

    def test_a_429_with_retry_after_waits_that_long_and_succeeds(self, monkeypatch):
        run, calls, slept = self.call(monkeypatch, [(429, {"Retry-After": "3"}), (200, {})])
        assert run().text == "ok"
        assert len(calls) == 2 and slept == [3.0]

    def test_without_retry_after_the_wait_doubles_and_stays_short(self, monkeypatch):
        run, calls, slept = self.call(monkeypatch, [(503, {}), (529, {}), (200, {})])
        assert run().text == "ok"
        assert len(calls) == 3
        assert 0.375 <= slept[0] <= 0.5 and 0.75 <= slept[1] <= 1.0

    def test_after_two_retries_the_refusal_reaches_the_drain(self, monkeypatch):
        import urllib.error

        run, calls, _ = self.call(monkeypatch, [(429, {})] * 3)
        with pytest.raises(urllib.error.HTTPError):
            run()
        assert len(calls) == 3

    def test_a_refusal_that_is_not_a_moment_is_not_asked_again(self, monkeypatch):
        import urllib.error

        for status in (400, 401, 402, 403, 404):
            run, calls, slept = self.call(monkeypatch, [(status, {})])
            with pytest.raises(urllib.error.HTTPError):
                run()
            assert len(calls) == 1 and slept == []

    def test_a_retry_after_too_long_or_a_date_falls_back_to_the_short_wait(self, monkeypatch):
        for named in ("120", "Wed, 21 Oct 2026 07:28:00 GMT"):
            run, calls, slept = self.call(monkeypatch, [(429, {"Retry-After": named}), (200, {})])
            assert run().text == "ok"
            assert slept[0] <= 0.5


class TestEveryGeminiCallPassesTheGate:
    """CONTRIBUTING.md: every model call goes through core/llm.py and its gate
    `_before_the_call(vendor, model, region)`, where the Cloud puts its price
    check, its brakes and its daily stop (hosted/spending.py). Gemini's
    vectors went out without it, and the key test of the provider page built
    a Gemini client of its own (audit of 17 Oct 2026)."""

    @pytest.fixture
    def gate(self, monkeypatch):
        from types import SimpleNamespace

        from korely_agent.core import extensions

        seen, sent = [], []

        def refuse(vendor, model, region):
            seen.append((vendor, model, region))
            raise llm.NotNow("the daily emergency stop is reached")

        reply = SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1] * llm.EMBEDDING_DIM)],
                                usage_metadata=SimpleNamespace(prompt_token_count=1))
        client = SimpleNamespace(models=SimpleNamespace(
            embed_content=lambda **kw: (sent.append(kw), reply)[1]))
        monkeypatch.setattr(llm, "get_settings",
                            lambda: SimpleNamespace(embedding_model="gemini-embedding-2-preview",
                                                    embedding_provider="gemini",
                                                    llm_timeout_seconds=20))
        monkeypatch.setattr(llm, "current_google_key", lambda: "AIza-key")
        monkeypatch.setattr(llm, "_embed_client", lambda key: client)
        monkeypatch.setattr(llm, "_gemini_client", lambda key: client)
        monkeypatch.setattr(llm, "_bill", lambda *a, **k: None)
        llm._last_embedding.update({"ok": None, "at": None, "error": None})
        yield SimpleNamespace(seen=seen, sent=sent, refuse=lambda: extensions.register(
            "before_model_call", refuse))
        extensions.reset()
        llm.forget_failure_here()
        llm._last_embedding.update({"ok": None, "at": None, "error": None})

    def test_a_vector_refused_at_the_gate_is_not_sent(self, gate):
        from korely_agent.services.facts import drain as facts_drain

        gate.refuse()
        llm.forget_failure_here()
        assert llm.embed("Maria lives in Rome.") is None
        assert gate.seen == [("google", "gemini-embedding-2-preview", "global")]
        assert gate.sent == [], "the call went out past the gate"
        assert llm.last_kind_here() in facts_drain._NOT_THE_MEMORYS, \
            "the drain reads it as a refusal: the memory waits, the region rests"
        assert llm.embedding_state()["state"] == "unknown", "nothing was asked of Google"
        with pytest.raises(llm.EmbeddingFailed):
            llm.embed_or_raise("Maria lives_in Rome")

    def test_with_no_hook_the_vector_goes_out(self, gate):
        assert llm.embed("Maria lives in Rome.") == [0.1] * llm.EMBEDDING_DIM
        assert len(gate.sent) == 1

    def test_the_vector_is_billed_to_the_vendor_its_gate_names(self, gate, monkeypatch):
        """Audit of 17 Oct 2026: the cost row of one of Gemini's vectors named
        no vendor, and its stage was a literal beside llm_usage.EMBEDDING."""
        from korely_agent.services import llm_usage

        billed = []
        monkeypatch.setattr(llm, "_bill", lambda *a, **k: billed.append((a, k)))
        llm.embed("Maria lives in Rome.")
        assert billed == [(("gemini", "gemini-embedding-2-preview", 1, 0),
                           {"stage": llm_usage.EMBEDDING, "vendor": "google", "region": "global"})]

    def test_the_key_test_asks_the_gate_first(self, gate):
        from korely_agent.services import provider_key

        gate.refuse()
        out = provider_key.check("AIza-pasted-key")
        assert out["ok"] is False and "emergency stop" in out["detail"]
        assert gate.seen == [("google", "gemini-embedding-2-preview", "global")]
        assert gate.sent == []

    def test_the_key_test_goes_through_the_model_layer(self, gate):
        from korely_agent.services import provider_key

        assert provider_key.check("AIza-pasted-key") == {"ok": True, "detail": "the provider answered"}
        assert len(gate.sent) == 1
        assert llm.embedding_state()["state"] == "unknown", \
            "a key being tried is not the key in force: /health is not about it"
