"""Vectors computed inside the installation (EMBEDDING_PROVIDER=local).

No model is loaded here: the network is closed (conftest.py) and the weights
are 1.2 GB. The session and the tokenizer are fakes with the real ones'
shapes; what the real model computes was checked once against the model
card's own numbers, in the built image (2026-09-29).
"""
import asyncio
import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from korely_agent.core import embedding_model as M
from korely_agent.core import llm, local_embedder

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _settings(**over):
    from korely_agent.config import AgentSettings

    base = dict(database_url="postgresql://u:p@h/d", google_api_key=None)
    base.update(over)
    return AgentSettings(**base)


def _a_database_url(monkeypatch):
    """Settings need one to be read. A placeholder only when there is no real
    one: the engine (db/base.py) is built once per process from the first
    address it is given, and a placeholder cached there by a test that starts
    the app would be the address every later test's `manage` command uses."""
    import os

    if not os.environ.get("DATABASE_URL"):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@localhost/x")


class FakeTokenizer:
    """One id per character, as SentencePiece gives one list of ints."""

    def encode(self, text):
        return [100 + (ord(c) % 1000) for c in text]


class FakeSession:
    def __init__(self, width=M.DIMENSIONS, delay=0.0):
        self.width, self.delay = width, delay
        self.feeds, self.running, self.most = [], 0, 0
        self._count = threading.Lock()

    def run(self, names, feed):
        import numpy as np

        with self._count:
            self.running += 1
            self.most = max(self.most, self.running)
        time.sleep(self.delay)
        self.feeds.append((names, feed))
        with self._count:
            self.running -= 1
        return [np.full((1, self.width), 3.0, dtype=np.float32)]


@pytest.fixture
def fake_model(monkeypatch):
    """The embedder with a fake model in place, and its state reset."""
    session = FakeSession()
    monkeypatch.setattr(local_embedder, "_model",
                        (session, FakeTokenizer(), ["input_ids", "attention_mask"]))
    local_embedder._last.update({"ok": None, "at": None, "error": None})
    yield session
    local_embedder._last.update({"ok": None, "at": None, "error": None})


@pytest.fixture
def local(monkeypatch):
    """EMBEDDING_PROVIDER=local."""
    s = _settings(embedding_provider="local")
    monkeypatch.setattr(llm, "get_settings", lambda: s)
    return s


class TestThePinnedModel:
    def test_one_revision_and_every_file_hashed(self):
        assert len(M.REVISION) == 40 and int(M.REVISION, 16) >= 0
        for path, (size, sha256) in M.FILES.items():
            assert size > 0 and len(sha256) == 64 and int(sha256, 16) >= 0, path

    def test_the_weights_sit_next_to_the_graph(self):
        """ONNX Runtime reads the weights from the file named in the graph, in
        the graph's own folder, and refuses the graph alone."""
        assert {"onnx/model.onnx", "onnx/model.onnx_data", "tokenizer.model"} == set(M.FILES)

    def test_full_precision_not_the_desktops_8_bit(self):
        assert M.FILES["onnx/model.onnx_data"][0] > 1_000_000_000

    def test_the_folder_is_where_the_image_puts_it(self):
        d = (ROOT / "Dockerfile").read_text()
        assert f"/models/{M.NAME}" in d and "COPY --from=model /models /opt/korely-models" in d
        assert local_embedder.MODEL_DIR == f"/opt/korely-models/{M.NAME}"

    def test_the_names_are_the_families_memory_core_measured(self):
        from korely_agent.services.memory_core import thresholds

        assert thresholds.family(M.NAME) == "embeddinggemma-300m"
        assert thresholds.family(_settings().embedding_model) == "gemini-embedding-2"

    def test_the_width_is_the_columns(self):
        assert M.DIMENSIONS == llm.EMBEDDING_DIM


class TestTheFetch:
    """The build's download, with the files and the network replaced."""

    @pytest.fixture
    def served(self, monkeypatch):
        import hashlib
        import io

        blobs = {"onnx/model.onnx": b"graph", "onnx/model.onnx_data": b"weights" * 10,
                 "tokenizer.model": b"pieces"}
        monkeypatch.setattr(M, "FILES", {p: (len(b), hashlib.sha256(b).hexdigest())
                                         for p, b in blobs.items()})
        asked = []

        def urlopen(req, timeout=None):
            asked.append(req.full_url)
            path = req.full_url.split(f"/resolve/{M.REVISION}/", 1)[1]
            return io.BytesIO(blobs[path])

        monkeypatch.setattr(M.urllib.request, "urlopen", urlopen)
        return blobs, asked

    def test_every_file_checked_then_the_manifest(self, served, tmp_path):
        blobs, asked = served
        M.fetch(str(tmp_path))
        for path, data in blobs.items():
            assert (tmp_path / path).read_bytes() == data
        manifest = json.loads((tmp_path / M.MANIFEST).read_text())
        assert manifest["revision"] == M.REVISION and set(manifest["files"]) == set(blobs)
        assert all(f"/{M.REPO}/resolve/{M.REVISION}/" in u for u in asked)

    def test_a_file_that_does_not_match_stops_the_build(self, served, tmp_path, monkeypatch):
        monkeypatch.setitem(M.FILES, "tokenizer.model", (6, "0" * 64))
        with pytest.raises(SystemExit, match="tokenizer.model"):
            M.fetch(str(tmp_path))
        assert not (tmp_path / "tokenizer.model").exists()
        assert not (tmp_path / "tokenizer.model.part").exists()
        assert not (tmp_path / M.MANIFEST).exists()

    def test_the_weights_come_with_the_gemma_notice(self, served, tmp_path):
        """What the Gemma Terms of Use ask a distribution of the model to
        carry, next to the weights of an image built with them."""
        M.fetch(str(tmp_path))
        notice = (tmp_path / "NOTICE").read_text()
        assert notice.startswith("Gemma is provided under and subject to the Gemma Terms "
                                 "of Use found at ai.google.dev/gemma/terms")
        assert M.REVISION in notice and "prohibited_use_policy" in notice

    def test_a_mirror_serves_the_same_revision(self):
        assert M.url("tokenizer.model", "https://mirror.example/") == (
            f"https://mirror.example/{M.REPO}/resolve/{M.REVISION}/tokenizer.model")

    def test_a_dropped_connection_is_tried_again(self, served, tmp_path, monkeypatch):
        real, calls = M.urllib.request.urlopen, []

        def flaky(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("connection reset")
            return real(req, timeout)

        monkeypatch.setattr(M.urllib.request, "urlopen", flaky)
        monkeypatch.setattr(M.time, "sleep", lambda s: None)
        M.fetch(str(tmp_path))
        assert (tmp_path / "onnx/model.onnx").exists()


class TestTheImage:
    def _dockerfile(self):
        return (ROOT / "Dockerfile").read_text()

    def _model_stage(self):
        """The model stage, from its FROM line to the next one, whatever
        options the FROM carries (`--platform=$BUILDPLATFORM` since 7 Oct)."""
        after = self._dockerfile().split(" AS model\n", 1)[1]
        return after.split("\nFROM ", 1)[0]

    def test_the_weights_come_only_with_the_local_provider(self):
        stage = self._model_stage()
        assert "ARG EMBEDDING_PROVIDER=local" in stage, "the default since 0.1.19"
        assert "tr -d ' ' | tr 'A-Z' 'a-z')\" = \"local\" ]" in stage
        # One file into the stage before the download: a change anywhere else
        # in the code, or in the terms copied after it, must not download the
        # weights again.
        before = stage.split("RUN mkdir -p /models", 1)[0]
        copies = [ln for ln in before.splitlines() if ln.startswith("COPY")]
        assert copies == ["COPY korely_agent/core/embedding_model.py /fetch/embedding_model.py"]

    def test_the_image_starts_with_the_embedder_it_was_built_for(self):
        """The image built without the model must not start as local, the
        default, with nothing to compute with: the build's choice is the
        environment's default, and .env still wins over it."""
        final = self._dockerfile().split("AS base", 1)[1]
        assert "ARG EMBEDDING_PROVIDER=local\nENV EMBEDDING_PROVIDER=${EMBEDDING_PROVIDER}" in final

    def test_the_terms_go_next_to_the_weights(self):
        """The Gemma Terms of Use ask every copy of the model to come with the
        notice and a copy of the terms; the additional terms were approved
        on 2026-10-01 (licenses/GEMMA-TERMS.md)."""
        stage = self._model_stage()
        after = stage.split("RUN mkdir -p /models", 1)[1]
        assert "COPY licenses/GEMMA-TERMS.md licenses/gemma-terms-of-use.txt /fetch/terms/" in after
        assert "/models/embeddinggemma-300m/" in after.split("COPY licenses", 1)[1]
        terms = (ROOT / "licenses" / "gemma-terms-of-use.txt").read_text()
        assert "Gemma Terms of Use" in terms and "EmbeddingGemma" in terms
        assert "3.2 Use Restrictions" in terms and "Last modified: April 1, 2026" in terms
        notice = (ROOT / "NOTICE").read_text()
        assert ("Gemma is provided under and subject to the Gemma Terms of Use found at "
                "ai.google.dev/gemma/terms") in notice

    def test_the_approved_clause_word_for_word(self):
        """The clause as it was approved (the clause for customers, self-hosted
        variant, 2026-10-01): changing it is a decision, not an edit."""
        clause = " ".join((ROOT / "licenses" / "GEMMA-TERMS.md").read_text().split())
        for words in (
            "When local embeddings are enabled, the Software downloads and runs EmbeddingGemma, "
            "a model made by Google LLC.",
            "Gemma is provided under and subject to the Gemma Terms of Use found at "
            "ai.google.dev/gemma/terms (the \"Gemma Terms\"), a copy of which is provided with "
            "the Software.",
            "Customer shall not use the model, or the Software to the extent it runs the model, "
            "(a) for any use restricted by the Gemma Prohibited Use Policy at "
            "ai.google.dev/gemma/prohibited_use_policy, which is incorporated into this "
            "Agreement by reference, or (b) in violation of applicable laws and regulations.",
            "Korely grants no rights in the model beyond those Customer receives under the "
            "Gemma Terms.",
            "If Customer makes the model or its functionality available to anyone else, "
            "Customer shall impose the same restrictions on them.",
        ):
            assert words in clause, words

    def test_the_weights_land_outside_app_and_before_the_code(self):
        """Before `COPY korely_agent`, so a code change does not rewrite the
        layer; and never chowned (a chown over the weights would copy 1.2 GB
        into another layer; since 3 Nov 2026 nothing in the image is
        chowned, the code included: tests/test_build_context.py)."""
        d = self._dockerfile()
        final = d.split("AS base", 1)[1]
        weights = final.index("COPY --from=model /models /opt/korely-models")
        assert weights < final.index("COPY korely_agent ./korely_agent")
        assert "chown" not in final.replace("`chown -R korely:korely /app`", "")

    def test_compose_passes_the_choice_to_the_build(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        assert "EMBEDDING_PROVIDER: ${EMBEDDING_PROVIDER:-local}" in compose


class TestTheTokens:
    def test_bos_prompt_text_eos(self):
        ids = local_embedder.token_ids(FakeTokenizer(), "p: ", "ab")
        assert ids[0] == local_embedder.BOS and ids[-1] == local_embedder.EOS
        assert ids[1:-1] == FakeTokenizer().encode("p: ab")

    def test_a_long_text_is_cut_at_the_end_and_keeps_its_eos(self):
        ids = local_embedder.token_ids(FakeTokenizer(), local_embedder.DOCUMENT, "x" * 5000)
        assert len(ids) == M.MAX_TOKENS
        assert ids[-1] == local_embedder.EOS
        assert ids[1:1 + len(local_embedder.DOCUMENT)] == FakeTokenizer().encode(local_embedder.DOCUMENT)


class TestOneVector:
    def test_unit_length_and_the_tokens_it_read(self, fake_model):
        vec, tokens = local_embedder.embed("Maria lives in Rome.", local_embedder.DOCUMENT)
        assert len(vec) == M.DIMENSIONS
        assert abs(sum(v * v for v in vec) - 1.0) < 1e-9
        assert tokens == len(local_embedder.DOCUMENT + "Maria lives in Rome.") + 2
        names, feed = fake_model.feeds[-1]
        assert names == ["sentence_embedding"]
        assert feed["input_ids"].dtype.name == "int64" and feed["input_ids"].shape == (1, tokens)
        assert feed["attention_mask"].tolist() == [[1] * tokens]
        assert local_embedder.state()["state"] == "working"

    def test_a_vector_of_the_wrong_width_is_a_failure(self, fake_model):
        fake_model.width = 3072
        with pytest.raises(local_embedder.Unavailable, match="3072"):
            local_embedder.embed("x", local_embedder.QUERY)
        assert local_embedder.state()["state"] == "failing"

    def test_one_inference_at_a_time(self, fake_model):
        """Requests and the worker call from several threads; the model sees
        them one by one."""
        fake_model.delay = 0.05
        threads = [threading.Thread(target=local_embedder.embed, args=(f"t{i}", local_embedder.QUERY))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(fake_model.feeds) == 4 and fake_model.most == 1

    def test_a_waiting_question_goes_before_waiting_documents(self, fake_model):
        """A long memory is being embedded, two more wait, then a search
        arrives: the search is next, not third in line."""
        fake_model.delay = 0.25
        sizes = {}

        def run(name, text, prompt):
            sizes[len(local_embedder.token_ids(FakeTokenizer(), prompt, text))] = name
            local_embedder.embed(text, prompt)

        long_doc = threading.Thread(target=run, args=("long", "L" * 50, local_embedder.DOCUMENT))
        long_doc.start()
        time.sleep(0.05)
        queued = [threading.Thread(target=run, args=(f"doc{i}", "D" * (10 + i), local_embedder.DOCUMENT))
                  for i in range(2)]
        for t in queued:
            t.start()
        time.sleep(0.05)
        question = threading.Thread(target=run, args=("question", "where?", local_embedder.QUERY))
        question.start()
        for t in (long_doc, *queued, question):
            t.join()
        order = [sizes[feed["input_ids"].shape[1]] for _names, feed in fake_model.feeds]
        assert order[:2] == ["long", "question"], order
        assert sorted(order[2:]) == ["doc0", "doc1"]


class TestTheTurnsAreShared:
    """Audit of 18 Oct 2026: questions went first by their kind alone. A
    2,000-character question of digits, CJK or emoji reaches the model's
    2,048 tokens (5-6 s on two cores in the release image), and one
    account's searches kept every other account's writes waiting, with no
    bound, and its other searches an arbitrary number of turns."""

    @staticmethod
    def _busy_loop(turns, stop, account, served, cost):
        def run():
            while not stop.is_set():
                with turns.turn(question=True, account=account):
                    served.append(("q", account))
                    time.sleep(cost)
        return threading.Thread(target=run, daemon=True)

    def test_a_document_waits_behind_questions_at_most_its_lead(self):
        turns = local_embedder._Turns(lead=0.3)
        stop, served = threading.Event(), []
        loops = [self._busy_loop(turns, stop, "a", served, 0.05) for _ in range(3)]
        for t in loops:
            t.start()
        time.sleep(0.1)
        started = time.monotonic()
        with turns.turn(question=False, account="b"):
            waited = time.monotonic() - started
        stop.set()
        for t in loops:
            t.join()
        # The lead, plus the question running when it ran out.
        assert waited < 0.3 + 0.05 + 0.15, waited

    def test_after_a_late_document_a_question_goes_next(self):
        turns = local_embedder._Turns(lead=0.0)
        order = []
        gate = threading.Event()

        def hold():
            with turns.turn(question=False, account="w"):
                gate.wait(2)

        def one(kind, name):
            with turns.turn(question=kind == "q", account=name):
                order.append(name)

        holder = threading.Thread(target=hold)
        holder.start()
        time.sleep(0.05)
        waiting = [threading.Thread(target=one, args=k) for k in
                   (("d", "d1"), ("d", "d2"), ("q", "q1"))]
        for t in waiting:
            t.start()
            time.sleep(0.05)
        gate.set()
        for t in (holder, *waiting):
            t.join()
        assert order == ["d1", "q1", "d2"], order

    def test_an_account_with_one_question_is_not_overtaken_by_another_with_many(self):
        turns = local_embedder._Turns()
        stop, served = threading.Event(), []
        loops = [self._busy_loop(turns, stop, "a", served, 0.02) for _ in range(3)]
        for t in loops:
            t.start()
        time.sleep(0.1)
        before = len(served)
        with turns.turn(question=True, account="b"):
            ahead = len(served) - before
        stop.set()
        for t in loops:
            t.join()
        # At most the one running and the one already chosen.
        assert ahead <= 2, ahead

    def test_one_inference_at_a_time_still(self):
        turns = local_embedder._Turns(lead=0.0)
        running, most = [0], [0]
        lock = threading.Lock()

        def one(i):
            with turns.turn(question=i % 2 == 0, account=i % 3):
                with lock:
                    running[0] += 1
                    most[0] = max(most[0], running[0])
                time.sleep(0.01)
                with lock:
                    running[0] -= 1

        threads = [threading.Thread(target=one, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert most[0] == 1

    def test_the_account_is_the_one_the_call_is_billed_to(self, fake_model, monkeypatch):
        import uuid

        from korely_agent.services import llm_usage

        seen = []
        real = local_embedder._turns.turn

        def turn(question, account=None, request=False):
            seen.append(account)
            return real(question, account, request)

        monkeypatch.setattr(local_embedder._turns, "turn", turn)
        monkeypatch.setattr(llm_usage, "flush", lambda scope: None)
        tenant = uuid.uuid4()
        with llm_usage.attributed(tenant):
            local_embedder.embed("where?", local_embedder.QUERY)
        local_embedder.embed("where?", local_embedder.QUERY)
        assert seen == [tenant, None]


class _ServerLoop:
    """An event loop on a thread of its own, as uvicorn's: the account's
    places at the embedder are kept on it (core/threads._embedder), and a
    request's thread takes one there."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro, timeout=10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def a_requests_context(self):
        """The context of a request that came in on this loop, as the
        outermost middleware leaves it (main._RememberTheRequest)."""
        from korely_agent.core import threads

        async def capture():
            threads.serving_a_request()
            return contextvars.copy_context()
        return self.run(capture())

    def hold(self, account, n=1):
        """`n` reads of `account` that wait for their place and hold it
        until `let_go`; each future answers (got a place, its context)."""
        from korely_agent.core import threads

        free = self.run(self._event())
        entered = []

        async def one():
            threads.serving_a_request()
            async with threads.waits_for_the_embedder(account) as got:
                entered.append(got)
                ctx = contextvars.copy_context()
                await free.wait()
                return got, ctx
        futures = [self.start(one()) for _ in range(n)]
        return futures, entered, lambda: self.loop.call_soon_threadsafe(free.set)

    @staticmethod
    async def _event():
        return asyncio.Event()

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        self.loop.close()


@pytest.fixture(autouse=True)
def _fresh_places(monkeypatch):
    """Every test with the process's places at the embedder of its own: a
    place given back from a thread after a test's loop has stopped is never
    given back, and would be the next test's."""
    from korely_agent.core import threads

    monkeypatch.setattr(threads, "_embedder", threads.Gates(threads.CALLS_PER_ACCOUNT))


@pytest.fixture
def server_loop():
    loop = _ServerLoop()
    yield loop
    loop.close()


def _until(condition, seconds=5.0):
    until = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < until, "never happened"
        time.sleep(0.01)


def _may_go_on(ctx, account):
    """Whether a vector asked on a request's thread (in `ctx`) for
    `account` may go on to the turns (core/threads.at_the_embedder)."""
    from korely_agent.core import threads

    def ask():
        with threads.at_the_embedder(account) as ok:
            return ok
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(ctx.run, ask).result(10)


class TestAnAccountHoldsAFewQuestions:
    """Audit of 19 Oct 2026: a REST search waits for its turn on a request
    thread, and nothing bounded how many one account had waiting. One read
    key's searches took every request thread, so another account's unrelated
    request waited for (backlog - threads) inferences before the share among
    accounts could apply, and every search whose client had gone still ran. An
    account holds at most core/threads.CALLS_PER_ACCOUNT places at the
    embedder, running or waiting, the MCP's own number.

    31 Oct 2026 (scripts/concurrent_agents.sh at 400294b3): those places
    were counted here, at the embedder, so a REST search, already on its
    thread, past them was refused at once, and REST's four and the MCP's
    four added up here. The count moved to core/threads (`_embedder`, one
    count for both doors): a read waits for its place on the loop before it
    takes a thread, and any other request's vector takes one at once or is
    refused, as before."""

    PLACES = local_embedder.CALLS_PER_ACCOUNT

    @staticmethod
    def _holding(turns, gate, account, question=True, request=False):
        """A caller that takes its turn and holds it until `gate` is set."""
        entered = threading.Event()

        def run():
            try:
                with turns.turn(question=question, account=account, request=request):
                    entered.set()
                    gate.wait(5)
            except local_embedder.Busy:
                entered.set()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, entered

    def test_the_mcp_and_the_embedder_keep_one_number(self):
        from korely_agent.core import threads
        from korely_agent.mcp import server

        assert server.MAX_RUNNING_CALLS_PER_TENANT == threads.CALLS_PER_ACCOUNT == self.PLACES
        assert threads._embedder.per_key == self.PLACES and threads._embedder.total is None

    def test_past_its_places_a_request_holding_none_is_refused_at_once(self, server_loop):
        """What test_past_its_places_a_question_is_refused_at_once held,
        where the count now is: a request's vector that holds no place (a
        write's, already on its thread) is refused at once past its
        account's places, never left to wait on its thread."""
        futures, entered, let_go = server_loop.hold("a", self.PLACES)
        _until(lambda: len(entered) == self.PLACES)
        ctx = server_loop.a_requests_context()
        started = time.monotonic()
        assert _may_go_on(ctx, "a") is False
        assert time.monotonic() - started < 0.5
        assert _may_go_on(ctx, "b") is True, "another account's places are its own"
        let_go()
        for f in futures:
            assert f.result(5)[0] is True
        # Its places back, the account asks again.
        assert _may_go_on(ctx, "a") is True
        # A place given back from a thread lands on the loop a moment later.
        _until(lambda: len(threads_of()._embedder) == 0, 2)

    def test_a_read_past_its_places_waits_for_one(self, server_loop):
        futures, entered, let_go = server_loop.hold("a", self.PLACES)
        _until(lambda: len(entered) == self.PLACES)
        fifth, fifth_in, let_fifth_go = server_loop.hold("a")
        time.sleep(0.2)
        assert not fifth_in, "a read past its places took one"
        let_go()
        _until(lambda: fifth_in == [True])
        for f in futures:
            assert f.result(5)[0] is True
        let_fifth_go()
        assert fifth[0].result(5)[0] is True
        _until(lambda: len(threads_of()._embedder) == 0, 2)

    def test_the_holders_vector_is_not_refused_and_its_place_is_back_after_it(self,
                                                                               server_loop):
        """A read that waited for its place is not counted again at the
        embedder, and gives the place back as soon as its vector has been
        computed, not when its route ends (the review of c203d330: the place
        held through the database search bounded an account's reads in
        flight, not its vectors)."""
        from korely_agent.core import threads

        seen, inside, done_vectors = [], [], threading.Event()

        def a_vector():
            with threads.at_the_embedder("a") as ok:
                inside.append(1)
                done_vectors.wait(5)
                return ok

        async def a_read(free):
            threads.serving_a_request()
            async with threads.waits_for_the_embedder("a") as got:
                ctx = contextvars.copy_context()
                seen.append(await asyncio.get_running_loop().run_in_executor(
                    None, ctx.run, a_vector))
                await free.wait()      # the rest of the read, its place given back
                return got

        outside = server_loop.a_requests_context()
        free = server_loop.run(_ServerLoop._event())
        reads = [server_loop.start(a_read(free)) for _ in range(self.PLACES)]
        _until(lambda: len(inside) == self.PLACES)
        # While the readers compute their vectors the account's places are
        # theirs: a request holding none is refused.
        assert _may_go_on(outside, "a") is False
        done_vectors.set()
        _until(lambda: len(seen) == self.PLACES)
        assert seen == [True] * self.PLACES, "a read that held its place was refused"
        # Every reader's vector is done; their reads go on, holding no place.
        _until(lambda: _may_go_on(outside, "a") is True, 2)
        server_loop.loop.call_soon_threadsafe(free.set)
        assert [r.result(5) for r in reads] == [True] * self.PLACES
        _until(lambda: len(threads._embedder) == 0, 2)

    def test_a_freed_place_goes_to_the_waiting_read_not_to_a_taker(self):
        """A request already on its thread takes a place at once or not at
        all, and never ahead of a read waiting for one: the reads wait their
        turn and the writes go without their vector."""
        from korely_agent.core.threads import Gates

        gates = Gates(per_key=2)

        async def go():
            free = asyncio.Event()
            order = []

            async def hold(name):
                async with gates.place("a"):
                    order.append(name)
                    await free.wait()

            held = [asyncio.create_task(hold(f"h{i}")) for i in range(2)]
            await asyncio.sleep(0.01)
            waiting = asyncio.create_task(hold("read"))
            await asyncio.sleep(0.01)
            assert gates.take_now("a") is False, "full"
            assert gates.take_now("b") is True
            gates.give_back("b")
            free.set()
            await asyncio.sleep(0.01)
            await asyncio.gather(*held, waiting)
            assert order == ["h0", "h1", "read"]
            assert gates.take_now("a") is True
            assert gates.take_now("a") is True
            assert gates.take_now("a") is False
            gates.give_back("a")
            gates.give_back("a")

        asyncio.run(go())
        assert len(gates) == 0

    def test_a_taker_waits_behind_nobody_and_goes_before_nobody(self):
        from korely_agent.core.threads import Gates

        gates = Gates(per_key=1)

        async def go():
            free = asyncio.Event()

            async def hold():
                async with gates.place("a"):
                    await free.wait()

            first = asyncio.create_task(hold())
            await asyncio.sleep(0.01)
            second = asyncio.create_task(hold())
            await asyncio.sleep(0.01)
            free.set()
            # The first gave its place to the second, waiting: none is free.
            await asyncio.sleep(0)
            assert gates.take_now("a") is False
            await asyncio.gather(first, second)
            assert gates.take_now("a") is True
            gates.give_back("a")

        asyncio.run(go())
        assert len(gates) == 0

    def test_past_its_wait_a_read_goes_on_without_a_place(self, server_loop, monkeypatch):
        from korely_agent.core import threads

        monkeypatch.setattr(threads, "WAIT_SECONDS", 0.3)
        futures, entered, let_go = server_loop.hold("a", self.PLACES)
        _until(lambda: len(entered) == self.PLACES)
        started = time.monotonic()
        late, late_in, let_late_go = server_loop.hold("a")
        _until(lambda: late_in)
        waited = time.monotonic() - started
        assert late_in == [False] and 0.25 <= waited < 2.0, (late_in, waited)
        let_late_go()
        got, ctx = late[0].result(5)
        # Without a place its question is refused at the embedder, as before.
        assert _may_go_on(ctx, "a") is False
        let_go()
        for f in futures:
            f.result(5)
        _until(lambda: len(threads._embedder) == 0, 2)

    def test_others_and_the_workers_and_unbilled_calls_are_not_counted(self, server_loop):
        futures, entered, let_go = server_loop.hold("a", self.PLACES)
        _until(lambda: len(entered) == self.PLACES)
        ctx = server_loop.a_requests_context()
        assert _may_go_on(ctx, "b") is True
        assert _may_go_on(ctx, None) is True, "an unbilled call is nobody's"
        assert _may_go_on(contextvars.Context(), "a") is True, "the worker's vector was refused"
        let_go()
        for f in futures:
            f.result(5)

    def test_the_turns_count_nobody_now(self):
        """The turns only order and time the callers: whether a caller may
        come is core/threads' (`at_the_embedder`)."""
        turns = local_embedder._Turns()
        gate = threading.Event()
        held = [self._holding(turns, gate, "a", request=True) for _ in range(self.PLACES + 2)]
        time.sleep(0.2)
        assert turns._here("a") == self.PLACES + 2
        gate.set()
        for t, _ in held:
            t.join(5)
        assert not turns._waiting and not turns._busy

    def test_a_requests_wait_has_a_deadline(self):
        """A request's caller waited with no end (Condition.wait()): past
        its deadline it is refused and the queue goes on without it. The
        worker's callers keep waiting: they hold no request's thread."""
        turns = local_embedder._Turns(wait=0.3)
        gate = threading.Event()
        holder, entered = self._holding(turns, gate, "w", question=False)
        entered.wait(2)
        started = time.monotonic()
        with pytest.raises(local_embedder.Busy):
            with turns.turn(question=False, account="a", request=True):
                pass
        waited = time.monotonic() - started
        assert 0.25 <= waited < 2.0, waited
        assert not turns._waiting
        worker, _ = self._holding(turns, gate, "w2", question=False)
        time.sleep(0.5)
        assert len(turns._waiting) == 1, "the worker's caller gave up"
        gate.set()
        holder.join(5)
        worker.join(5)
        assert not turns._waiting and not turns._busy

    def test_embed_on_a_request_counts_the_document(self, fake_model, local, monkeypatch,
                                                    server_loop):
        """Audit of 20 Oct 2026: a REST write's document vector, on its
        request's thread and holding its pooled connection, waited here
        uncounted. Past the account's places a request's document is refused
        at once; the write keeps the memory and leaves its vector to the
        worker (memory_engine._vector_of). The worker's is never refused."""
        import uuid

        from korely_agent.services import llm_usage

        monkeypatch.setattr(llm_usage, "flush", lambda scope: None)
        tenant = uuid.uuid4()
        futures, entered, let_go = server_loop.hold(tenant, self.PLACES)
        _until(lambda: len(entered) == self.PLACES)

        def asked():
            with llm_usage.attributed(tenant):
                return llm.embed("I live in Turin.", task=llm.TASK_DOCUMENT)

        ctx = server_loop.a_requests_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(ctx.run, asked).result(5) is None
            assert pool.submit(contextvars.Context().run, asked).result(5) is not None, \
                "the worker's document was refused"
        let_go()
        for f in futures:
            f.result(5)

    def test_one_accounts_burst_leaves_another_account_a_thread(self, server_loop):
        """The finding's own shape: a pool of request threads in front of the
        turns. Account A sends three times as many searches as there are
        threads; account B's request, which needs no vector, must get a
        thread meanwhile, and B's search must be served ahead of A's backlog.
        Before 19 Oct A's searches all waited at the turns, every thread was
        theirs, and B's request could not start until A's ended.

        Its premise changed on 31 Oct 2026: A's searches past its places were
        refused at once at the turns (this test counted 3 x threads - places
        refusals), and now they wait for their place on the loop before they
        take a thread, as the routes do (core/auth.a_turn_for_its_question). So
        the gate stands in front of the pool here, and none of A's searches
        is refused."""
        from korely_agent.core import threads

        threads_ = 2 * self.PLACES
        burst = 3 * threads_
        turns = local_embedder._Turns()
        pool = ThreadPoolExecutor(max_workers=threads_)
        refused, done, at_once = [], [], [0]
        lock = threading.Lock()

        def a_body():
            try:
                with turns.turn(question=True, account="a", request=True):
                    with lock:
                        at_once[0] = max(at_once[0], turns._here("a"))
                    time.sleep(0.05)
                done.append("a")
            except local_embedder.Busy:
                refused.append(1)

        def b_body():
            with turns.turn(question=True, account="b", request=True):
                time.sleep(0.05)
            done.append("b")

        async def search(account, body):
            threads.serving_a_request()
            async with threads.waits_for_the_embedder(account):
                await asyncio.get_running_loop().run_in_executor(pool, body)

        try:
            a_searches = [server_loop.start(search("a", a_body)) for _ in range(burst)]
            time.sleep(0.15)
            started = time.monotonic()
            waited = pool.submit(time.monotonic).result(2) - started   # needs no vector
            b_search = server_loop.start(search("b", b_body))
            b_search.result(5)
            a_done_before_b = done.index("b")
            for f in a_searches:
                f.result(30)
        finally:
            pool.shutdown(wait=True)
        assert waited < 1.0, waited
        assert not refused, f"{len(refused)} of A's searches were refused"
        assert done.count("a") == burst
        assert at_once[0] <= self.PLACES, at_once[0]
        # B's question took one of the next turns, not the last of A's backlog.
        assert a_done_before_b < burst // 2, a_done_before_b

    def test_a_refused_question_has_no_vector_and_is_not_a_failing_embedder(
            self, fake_model, local, monkeypatch, server_loop):
        import uuid

        from korely_agent.core import threads
        from korely_agent.services import llm_usage

        monkeypatch.setattr(threads, "_embedder", threads.Gates(0))
        monkeypatch.setattr(llm_usage, "flush", lambda scope: None)

        def asked():
            with llm_usage.attributed(uuid.uuid4()):
                return llm.embed("where?", task=llm.TASK_QUERY)

        assert server_loop.a_requests_context().run(asked) is None
        # /health does not hear about it: the embedder did nothing wrong.
        assert local_embedder.state()["state"] == "unknown"


def _ask_at_the_embedder(account):
    from korely_agent.core import threads

    with threads.at_the_embedder(account) as ok:
        return ok


def threads_of():
    from korely_agent.core import threads

    return threads


class TestARefusedQuestionThroughTheRoutes:
    """What a client reads when its account's question gets no place at the
    embedder, driven through the routes (audit of 20 Oct 2026: the test
    above named a 503 and sent no request, and the notes said 503 for
    /v1/context too). Since 31 Oct 2026 a read waits for its place first
    (core/auth.a_turn_for_its_question), so this is past that wait: no place
    to be had (`Gates(0)`) and a wait of 0.3 s."""

    def test_search_answers_503_and_the_context_answers_degraded(self, db, tenant, fake_model,
                                                                monkeypatch, caplog):
        import logging
        import uuid

        from fastapi.testclient import TestClient

        from korely_agent.core import threads
        from korely_agent.db import get_db
        from korely_agent.main import create_app
        from korely_agent.services import keys as key_service

        monkeypatch.setenv("RUN_WORKER", "false")
        monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
        from korely_agent.config import get_settings

        get_settings.cache_clear()
        monkeypatch.setattr(threads, "_embedder", threads.Gates(0))
        monkeypatch.setattr(threads, "WAIT_SECONDS", 0.3)
        _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
                                              scopes=["memories:read", "memories:write"])
        head = {"Authorization": f"Bearer {full}"}
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c, caplog.at_level(logging.WARNING):
            started = time.monotonic()
            r = c.post("/v1/memories/search", headers=head, json={"query": "where?"})
            assert time.monotonic() - started >= 0.3, "it did not wait for a place"
            assert (r.status_code, r.json()["code"]) == (503, "search_unavailable"), r.text
            assert r.headers["Retry-After"] == str(threads.BUSY_RETRY_SECONDS)
            r = c.get("/v1/context", headers=head, params={"query": "where?"})
            assert r.status_code == 200, r.text
            assert r.json()["degraded"] is True and r.json()["degraded_parts"] == ["memories"]
        assert not [x for x in caplog.records if x.levelno >= logging.ERROR], \
            [x.getMessage() for x in caplog.records if x.levelno >= logging.ERROR]
        # Not a failure of the embedder, for /health.
        assert local_embedder.state()["state"] != "failing"
        get_settings.cache_clear()


class TestAMissingModel:
    def test_it_says_where_and_how_to_fix_it(self, monkeypatch, tmp_path):
        monkeypatch.setattr(local_embedder, "MODEL_DIR", str(tmp_path))
        monkeypatch.setattr(local_embedder, "_model", None)
        local_embedder._last.update({"ok": None, "at": None, "error": None})
        with pytest.raises(local_embedder.Unavailable) as e:
            local_embedder.embed("x", local_embedder.QUERY)
        assert str(tmp_path) in str(e.value) and "docker compose up -d --build" in str(e.value)
        st = local_embedder.state()
        assert st["state"] == "failing" and "EMBEDDING_PROVIDER=local" in st["detail"]
        assert local_embedder.missing_files() == list(M.FILES)
        local_embedder._last.update({"ok": None, "at": None, "error": None})

    def test_a_model_of_another_revision_is_refused(self, monkeypatch, tmp_path):
        for p in M.FILES:
            (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / p).write_bytes(b"x")
        (tmp_path / M.MANIFEST).write_text(json.dumps({"revision": "0" * 40}))
        monkeypatch.setattr(local_embedder, "MODEL_DIR", str(tmp_path))
        monkeypatch.setattr(local_embedder, "_model", None)
        with pytest.raises(local_embedder.Unavailable, match="revision"):
            local_embedder.embed("x", local_embedder.QUERY)
        local_embedder._last.update({"ok": None, "at": None, "error": None})


class TestThreads:
    def test_the_cpus_the_container_is_given_at_most_four(self, monkeypatch):
        monkeypatch.setattr(local_embedder.os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
        assert local_embedder.threads() == 2
        monkeypatch.setattr(local_embedder.os, "sched_getaffinity", lambda pid: set(range(16)),
                            raising=False)
        assert local_embedder.threads() == 4


class TestTheSwitch:
    def test_local_is_the_default(self):
        """Since 0.1.19: no text leaves the machine for a vector unless the
        operator says so. The field's own default, because the suite's
        environment says gemini (tests/conftest.py)."""
        from korely_agent.config import AgentSettings

        assert AgentSettings.model_fields["embedding_provider"].default == "local"

    def test_a_typo_does_not_quietly_mean_gemini(self):
        with pytest.raises(ValueError, match="gemini or local"):
            _settings(embedding_provider="locale")
        assert _settings(embedding_provider=" Local ").embedding_provider == "local"

    def test_the_local_embedder_needs_no_key(self, local):
        assert llm.embedding_configured() is True

    def test_gemini_without_a_key_is_no_embedder(self, monkeypatch):
        monkeypatch.setattr(llm, "get_settings", lambda: _settings())
        monkeypatch.setattr(llm, "current_google_key", lambda: None)
        assert llm.embedding_configured() is False
        assert llm.embed("x") is None

    def test_the_model_name_says_which_distances(self, local, monkeypatch):
        assert llm.embedding_model_name() == M.NAME
        monkeypatch.setattr(llm, "get_settings", lambda: _settings())
        assert llm.embedding_model_name() == "gemini-embedding-2-preview"


class TestTheThresholdsFollowTheModel:
    """memory_core 2.4.0: the judge's candidates and the vocabulary's
    shortlist are cut at the distances of the model that made the vectors."""

    def test_local_gets_embeddinggemmas(self, local):
        from korely_agent.services.memory_core import thresholds

        assert llm.pick_distance_thresholds() == thresholds.EMBEDDINGGEMMA
        assert (thresholds.current().semantic, thresholds.current().shortlist) == (0.67, 0.67)

    def test_gemini_keeps_its_own(self, monkeypatch):
        from korely_agent.services.memory_core import thresholds

        monkeypatch.setattr(llm, "get_settings", lambda: _settings())
        assert llm.pick_distance_thresholds() == thresholds.GEMINI == thresholds.Thresholds(0.45, 0.65)

    def test_read_at_every_use_not_copied_at_startup(self, monkeypatch):
        from korely_agent.services.memory_core import thresholds

        now = {"s": _settings()}
        monkeypatch.setattr(llm, "get_settings", lambda: now["s"])
        llm.pick_distance_thresholds()
        now["s"] = _settings(embedding_provider="local")
        assert thresholds.current() == thresholds.EMBEDDINGGEMMA

    def test_a_model_nobody_measured_stops_the_start(self, monkeypatch):
        from korely_agent.services.memory_core import thresholds

        monkeypatch.setattr(llm, "get_settings",
                            lambda: _settings(embedding_model="gemini-embedding-001"))
        with pytest.raises(thresholds.ModelWithoutThresholds) as e:
            llm.pick_distance_thresholds()
        assert "EMBEDDING_MODEL='gemini-embedding-001'" in str(e.value)
        assert "EMBEDDING_PROVIDER=local" in str(e.value)

    def test_the_app_chooses_them_when_it_starts(self, monkeypatch):
        from korely_agent import config
        from korely_agent.services.memory_core import thresholds

        monkeypatch.setenv("RUN_WORKER", "false")
        _a_database_url(monkeypatch)
        monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
        config.get_settings.cache_clear()
        from korely_agent.main import create_app

        create_app()
        assert thresholds.current() == thresholds.EMBEDDINGGEMMA

        monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
        monkeypatch.setenv("EMBEDDING_MODEL", "gemini-embedding-001")
        config.get_settings.cache_clear()
        with pytest.raises(thresholds.ModelWithoutThresholds):
            create_app()
        config.get_settings.cache_clear()


class TestWhichPromptForWhichText:
    """The rule for both engines (2026-09-29): what questions look for
    is a document, a question is a query, and only what is compared with its
    own kind alone asks for sentence similarity."""

    @pytest.fixture
    def asked(self, local, monkeypatch):
        seen = []
        monkeypatch.setattr(local_embedder, "embed",
                            lambda text, prompt: (seen.append(prompt) or [0.0] * 768, 7))
        return seen

    @pytest.mark.parametrize("task, prompt", [
        (llm.TASK_QUERY, "task: search result | query: "),
        (llm.TASK_DOCUMENT, "title: none | text: "),
        (llm.TASK_SIMILARITY, "task: sentence similarity | query: "),
    ])
    def test_the_model_cards_prompts(self, asked, task, prompt):
        llm.embed("x", task=task)
        assert asked == [prompt]

    def test_an_unknown_task_is_a_bug_not_a_default(self, asked):
        with pytest.raises(ValueError):
            llm.embed("x", task="CLUSTERING")

    def test_a_call_is_billed_as_tokens_that_cost_nothing(self, asked, monkeypatch):
        billed = []
        monkeypatch.setattr(llm, "_bill", lambda *a, **k: billed.append((a, k)))
        llm.embed("x", task=llm.TASK_QUERY)
        assert billed == [(("local", M.NAME, 7, 0), {"stage": "embedding"})]

    def test_a_failure_is_none_as_on_gemini(self, local, monkeypatch):
        def fail(text, prompt):
            raise local_embedder.Unavailable("model missing")
        monkeypatch.setattr(local_embedder, "embed", fail)
        assert llm.embed("x") is None
        with pytest.raises(llm.EmbeddingFailed):
            llm.embed_or_raise("x")

    def test_on_gemini_a_relation_phrase_stays_a_document(self, monkeypatch):
        """Asking Gemini for its SEMANTIC_SIMILARITY would move every distance
        the vocabulary's 0.65 was measured on."""
        tasks = []
        reply = SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1] * 768)],
                                usage_metadata=None)

        def embed_content(**kw):
            tasks.append(kw["config"].task_type)
            return reply

        monkeypatch.setattr(llm, "get_settings", lambda: _settings())
        monkeypatch.setattr(llm, "current_google_key", lambda: "k")
        monkeypatch.setattr(llm, "_embed_client",
                            lambda key: SimpleNamespace(models=SimpleNamespace(embed_content=embed_content)))
        monkeypatch.setattr(llm, "_bill", lambda *a, **k: None)
        for task in (llm.TASK_SIMILARITY, llm.TASK_DOCUMENT, llm.TASK_QUERY):
            llm.embed("x", task=task)
        assert tasks == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]

    def test_the_relation_phrases_ask_for_similarity(self, monkeypatch):
        from korely_agent.services import predicates

        seen = []
        monkeypatch.setattr(llm, "embedding_configured", lambda: True)
        monkeypatch.setattr(llm, "embed_or_raise",
                            lambda text, task: seen.append(task) or [0.0] * 768)
        predicates.vocabulary_embedder()("works at")
        assert seen == [llm.TASK_SIMILARITY]

    def test_facts_and_memories_are_documents(self, monkeypatch):
        """A fact is also compared with other facts (the judge's candidates),
        and still a document: the one vector it has must answer questions."""
        from korely_agent.services import memory_engine
        from korely_agent.services.facts import service

        seen = []
        monkeypatch.setattr(memory_engine, "embed", lambda text, task: seen.append(task) or [0.0])
        monkeypatch.setattr(llm, "embed_or_raise", lambda text, task: seen.append(task) or [0.0])
        memory_engine.memory_vector("Maria lives in Rome.")
        service.fact_vector("Maria lives_in Rome")
        assert seen == [llm.TASK_DOCUMENT, llm.TASK_DOCUMENT]

    def test_questions_are_queries(self, monkeypatch, db, tenant):
        from korely_agent.services import context, memory_engine

        seen = []
        monkeypatch.setattr(memory_engine, "embed", lambda text, task: seen.append(task) or [0.0] * 768)
        monkeypatch.setattr(context, "embed", lambda text, task: seen.append(task) or [0.0] * 768)
        memory_engine.AgentMemoryEngine(db, tenant.id, None).search("where does Maria live")
        # /v1/context's one vector for the facts and the memories (BUG-299).
        context._query_vector(tenant.id, None, "where does Maria live")
        assert seen == [llm.TASK_QUERY, llm.TASK_QUERY]


class TestTheCost:
    def test_the_local_model_costs_nothing_and_is_not_unpriced(self):
        from korely_agent.services.usage_report import cost_of

        card = {"currency": "EUR", "models": {"gemini-3.6-flash": {"input_per_million": 0.26}}}
        assert cost_of(card, M.NAME, 1_000_000, 0) == 0.0
        assert cost_of(card, "gemini-embedding-2-preview", 1_000_000, 0) is None

    def test_an_operator_may_still_price_it(self):
        from korely_agent.services.usage_report import cost_of

        card = {"currency": "EUR", "models": {M.NAME: {"input_per_million": 0.01}}}
        assert cost_of(card, M.NAME, 1_000_000, 0) == 0.01


class TestHealth:
    def test_the_vectors_have_their_own_line_and_can_degrade(self, monkeypatch):
        from fastapi.testclient import TestClient

        from korely_agent import config

        monkeypatch.setenv("RUN_WORKER", "false")
        monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
        _a_database_url(monkeypatch)
        config.get_settings.cache_clear()
        monkeypatch.setattr(local_embedder, "warm_up", lambda: None)
        from korely_agent.main import create_app

        app = create_app()
        with TestClient(app) as c:
            local_embedder._record(False, "EMBEDDING_PROVIDER=local and the model is not in /x")
            body = c.get("/health").json()
            assert body["embeddings"]["state"] == "failing"
            assert body["embeddings"]["model"] == M.NAME
            assert body["status"] == "degraded"
            local_embedder._record(True)
            assert c.get("/health").json()["embeddings"]["state"] == "working"
        local_embedder._last.update({"ok": None, "at": None, "error": None})
        config.get_settings.cache_clear()

    def test_on_gemini_the_vectors_have_their_own_state(self, monkeypatch):
        """Not the local model's, and not the extraction's: Gemini's vectors
        answered for the extraction until 2026-10-06 (the audit of 6 October,
        row 3)."""
        from fastapi.testclient import TestClient

        from korely_agent import config
        from korely_agent.core import llm

        monkeypatch.setenv("RUN_WORKER", "false")
        _a_database_url(monkeypatch)
        monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
        config.get_settings.cache_clear()
        llm._last_embedding.update({"ok": None, "at": None, "error": None})
        from korely_agent.main import create_app

        with TestClient(create_app()) as c:
            vectors = c.get("/health").json()["embeddings"]
        assert vectors == {"state": "unknown", "detail": "nothing has asked for a vector yet"}
        config.get_settings.cache_clear()
