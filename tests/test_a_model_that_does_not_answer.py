"""A model call that fails is not an answer (2026-09-28).

The fact pipeline asks a model at four
points: the extraction, the relation vocabulary (and its embedding), the
embedding of the new fact, and the contradiction judge. Three of them turned a
failure into a result:

  * the vocabulary adapter handed memory_core an embedder that returned None
    when the call failed, and memory_core 1.2.0 reads None as "no embedder":
    a new relation was cached with no centroid, which no later phrase could
    ever join;
  * the judge's failed call, or an answer it could not read, was "nothing
    contradicts": the new fact stayed open next to the one it may close, the
    memory was marked done, and a later write of the same fact reconfirmed it
    without asking again;
  * a failed embedding of the new fact was "no vector": the judge was not
    offered the semantic candidates, and the fact was never found by
    similarity again (nothing re-embeds a fact).

What a failure does now: nothing of the write is kept, the facts already in
the store stay as they were, and the memory is retried the way this engine
already retried a failed extraction (a synchronous write leaves it `pending`,
the worker tries once more, a second failure files it as `error` for `manage
reextract`). A provider that refuses the install rather than the memory (a
429 of credit or quota, a 401, a 503: core/llm.failure_kind) is not a failure
of the memory: the drain puts it back in the queue and rests (BUG-351,
`TestTheJudge.test_a_refused_judge_puts_the_memory_back`). A direct write
answers 503 `model_unavailable`. A genuine answer
still behaves as before: "nothing contradicts" writes the fact and closes
nothing, "no facts" marks the memory done.

The new fact's own embedding is the exception since the evening of the same
day: the fact is written,
`pending`, and the worker retries its vector (tests/test_vectors.py). The
morning's rule refused the write instead.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from korely_agent.core import llm
from korely_agent.core.llm import EMBEDDING_DIM, LLMResult
from korely_agent.db.models import AgentFact, AgentMemory, PredicateAlias, PredicateCluster
from korely_agent.services import facts as agent_facts
from korely_agent.services import keys as key_service
from korely_agent.services.facts import drain as facts_drain
from korely_agent.services.facts import service as facts_service
from korely_agent.services.memory_core.fact_extractor import ExtractedFact, ExtractionFailed
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id
from tests.conftest import in_the_judges_shape, is_the_judge, stub_extraction


VEC = [0.1] * EMBEDDING_DIM

ROME = "Maria lives in Rome."
MILAN = "Maria moved to Milan."


@pytest.fixture(autouse=True)
def _a_tick_waits_for_its_groups(monkeypatch):
    """These tests read what a tick says it did. A tick collects its groups for
    `_DRAIN_PATIENCE` (2 s) and counts one still running as deferred, so on a
    loaded machine a group past 2 s turned a 'failed' into a 'deferred' (the
    release run of 28 Oct). What a group that outlives the tick does is
    tests/test_drains.py's subject, not this file's."""
    monkeypatch.setattr(facts_drain, "_DRAIN_PATIENCE", 60.0)


class Answered(Exception):
    """A provider's error answer, its HTTP status as the field `code`, the way
    google-genai's APIError and urllib's HTTPError carry it (core/llm._status_of)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"{code} {message}")
        self.code = code


class Model:
    """The provider, scripted per question.

    `judge` is what the contradiction judge answers: a string, or an exception
    to raise. The vocabulary is answered the way a model would, and fails only
    when `vocabulary` holds an exception.

    A failure is recorded before it is raised, as the real providers do
    (core/llm._failed): the drain decides on that record, its status and its
    category, whether the memory or the provider is at fault. A stand-in that
    raised without it made every failure look like the memory's own (audit
    of 18 Oct 2026).
    """

    def __init__(self):
        self.judge = '{"contradictions": []}'
        self.vocabulary = None
        self.judged = 0

    def complete(self, messages, **kw):
        prompt = messages[0]["content"]
        if is_the_judge(prompt):
            self.judged += 1
            answer = in_the_judges_shape(prompt, self.judge)
        elif self.vocabulary is not None:
            answer = self.vocabulary
        elif '"family"' in prompt:
            answer = '{"family": "other"}'
        elif "many_values" in prompt:
            answer = '{"many_values": false}'
        else:
            answer = '{"same_as": null}'
        if isinstance(answer, llm.NotNow):
            # The service's own gate, as every real provider passes it before
            # the call: the test registers the hook that refuses.
            llm._before_the_call("google", "gemini-3.1-flash-lite", "global")
        if isinstance(answer, Exception):
            llm._failed(answer)
            raise answer
        return LLMResult(text=answer)


@pytest.fixture
def model(monkeypatch):
    # No rest of the drain carried in from another test, and none left behind;
    # the failures recorded here stay out of /health's last call.
    monkeypatch.setattr(facts_drain, "_pauses", {})
    monkeypatch.setattr(facts_drain, "_timeouts", type(facts_drain._timeouts)())
    monkeypatch.setattr(llm, "_last_call", dict(llm._last_call))
    m = Model()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: m)
    yield m
    llm.forget_failure_here()


@pytest.fixture
def said(monkeypatch):
    """What the extraction reads in each memory, by its text. An exception
    stands for an extraction that failed."""
    by_text: dict = {}

    def extract(content, **kw):
        out = by_text.get(content, [])
        if isinstance(out, Exception):
            raise out
        return list(out)

    stub_extraction(monkeypatch, facts_service, extract)
    return by_text


class Embedder:
    def __init__(self):
        self.fail = False
        self.calls = 0

    def __call__(self, text, **kw):
        self.calls += 1
        return None if self.fail else VEC


@pytest.fixture
def embedder(monkeypatch):
    """A configured embedder (a Google key is set) whose calls can fail.

    `core.llm.embed` returns None when its call fails, which is how a failure
    reaches every caller. Every name it is or was bound under is replaced
    (the facts service imported it by name until 2026-09-28), so nothing here
    can reach Google: the development image has google-genai installed, and a
    test that set a key and left one binding real would make a network call
    with it.
    """
    e = Embedder()
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    monkeypatch.setattr("korely_agent.core.llm.embed", e)
    monkeypatch.setattr("korely_agent.services.memory_engine.embed", lambda *a, **k: None)
    monkeypatch.setattr("korely_agent.services.facts.service.embed", e, raising=False)
    return e


def fact(obj: str) -> ExtractedFact:
    return ExtractedFact(subject="Maria", predicate_raw="lives_in", object=obj, confidence=0.9)


def remember(db, tenant, text: str) -> AgentMemory:
    """A memory as a write stores it, before its facts are mined."""
    return AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id)).add(
        text, end_user_id="maria")


def status(db, memory) -> str:
    db.expire_all()
    return db.get(AgentMemory, memory.id).graph_sync_status


def open_objects(db, tenant) -> set:
    db.expire_all()
    return {f.object_label for f in db.query(AgentFact).filter(
        AgentFact.tenant_id == tenant.id, AgentFact.invalid_at.is_(None)).all()}


def all_objects(db, tenant) -> list:
    db.expire_all()
    return sorted(f.object_label for f in db.query(AgentFact).filter(
        AgentFact.tenant_id == tenant.id).all())


def rome_is_known(db, tenant, said) -> AgentFact:
    said[ROME] = [fact("Rome")]
    a = remember(db, tenant, ROME)
    assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=a)
    assert status(db, a) == "done"
    return db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).one()


# ── the judge ──────────────────────────────────────────────────────────────

NO_VERDICT = [
    # A failure of the memory's own (core/llm.failure_kind: None): a 500 a
    # server crashes on every time. A 429 of credit is the provider's, and
    # the drain treats it otherwise (test_a_refused_judge_..., below).
    Answered(500, "INTERNAL: the server crashed on this request"),
    "",                                   # an empty answer
    "Here is the JSON requested:",        # cut off by the token ceiling
    '{"verdict": "none"}',                # without the list the prompt asks for
    "[]",                                 # JSON, not the object asked for
]


class TestTheJudge:
    @pytest.mark.parametrize("failure", NO_VERDICT, ids=[
        "call-failed-500", "empty", "truncated", "no-list", "not-an-object"])
    def test_no_verdict_writes_nothing_and_the_memory_is_retried(
            self, db, tenant, model, said, failure):
        rome = rome_is_known(db, tenant, said)
        model.judge = failure
        said[MILAN] = [fact("Milan")]
        b = remember(db, tenant, MILAN)

        assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=b) == []
        # memory_core 6.23.0: a readable answer of the wrong shape is asked for
        # once more (judge._answer, model_json.asked_again); the others once.
        assert model.judged == (2 if failure in ('{"verdict": "none"}', "[]") else 1)
        assert status(db, b) == "pending", "a write the judge never ruled on was marked done"
        assert all_objects(db, tenant) == ["Rome"], "the new fact was kept with no verdict"
        assert open_objects(db, tenant) == {"Rome"}

        # The worker tries once more; a second failure is filed, not looped.
        assert facts_service.process_pending_memories(db, limit=10) == {
            "processed": 0, "failed": 1, "claimed": 1}
        assert status(db, b) == "error"
        assert all_objects(db, tenant) == ["Rome"]

        # The judge answers again and the operator re-drives it (manage reextract).
        model.judge = json.dumps({"contradictions": [
            {"existing_id": str(rome.id), "reason": "moved"}]})
        db.get(AgentMemory, b.id).graph_sync_status = "pending"
        db.commit()
        facts_service.process_pending_memories(db, limit=10)
        assert status(db, b) == "done"
        assert open_objects(db, tenant) == {"Milan"}
        assert db.get(AgentFact, rome.id).invalid_at is not None

    def test_a_refused_judge_puts_the_memory_back_and_rests_the_drain(
            self, db, tenant, model, said):
        """Credit run out (BUG-351, 0.1.30): no fault of the memory. The
        synchronous write leaves it `pending` as for any failure, and the
        drain puts it back in the queue instead of filing it `error`, and
        rests the region; once the provider answers, it is written."""
        rome = rome_is_known(db, tenant, said)
        model.judge = Answered(429, "RESOURCE_EXHAUSTED: prepayment credits are depleted")
        said[MILAN] = [fact("Milan")]
        b = remember(db, tenant, MILAN)

        assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=b) == []
        assert status(db, b) == "pending"
        assert all_objects(db, tenant) == ["Rome"]

        assert facts_service.process_pending_memories(db, limit=10) == {
            "processed": 0, "failed": 0, "claimed": 1, "deferred": 1}
        assert status(db, b) == "pending", "a refusal of the provider filed the memory as an error"
        assert all_objects(db, tenant) == ["Rome"]
        assert any(p["until"] > time.monotonic() for p in facts_drain._pauses.values()), (
            "the drain did not rest after the provider refused")

        # The credit is topped up and the rest is over: nobody re-drives it.
        model.judge = json.dumps({"contradictions": [
            {"existing_id": str(rome.id), "reason": "moved"}]})
        facts_drain._pauses.clear()
        facts_service.process_pending_memories(db, limit=10)
        assert status(db, b) == "done"
        assert open_objects(db, tenant) == {"Milan"}

    def test_a_genuine_no_still_writes_the_fact(self, db, tenant, model, said):
        rome_is_known(db, tenant, said)
        model.judge = '{"contradictions": []}'
        said[MILAN] = [fact("Milan")]
        b = remember(db, tenant, MILAN)

        out = agent_facts.extract_and_persist_facts(db, tenant.id, memory=b)
        assert [s["object"] for s in out] == ["Milan"]
        assert model.judged == 1
        assert status(db, b) == "done"
        assert open_objects(db, tenant) == {"Rome", "Milan"}


class TestTheExtraction:
    """Unchanged, and pinned: this is the retry the other three now share."""

    def test_a_failed_extraction_keeps_the_facts_and_is_retried(self, db, tenant, model, said):
        rome_is_known(db, tenant, said)
        said[MILAN] = ExtractionFailed("the model call failed: 503")
        b = remember(db, tenant, MILAN)

        assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=b) == []
        assert status(db, b) == "pending"
        assert open_objects(db, tenant) == {"Rome"}

    def test_no_facts_is_an_answer(self, db, tenant, model, said):
        said[MILAN] = []
        b = remember(db, tenant, MILAN)
        assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=b) == []
        assert status(db, b) == "done"


# ── the embeddings ─────────────────────────────────────────────────────────

class TestTheVocabularyEmbedding:
    """memory_core 1.2.0: on Postgres an embedder that RAISES is a failed
    resolution; one that is None is "no embedder". The adapter has to say
    which, because only it knows whether a key is set."""

    def test_a_failed_call_leaves_the_memory_pending_and_caches_nothing(
            self, db, tenant, model, said, embedder):
        embedder.fail = True
        said[ROME] = [fact("Rome")]
        a = remember(db, tenant, ROME)

        assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=a) == []
        assert status(db, a) == "pending"
        assert db.query(PredicateCluster).filter(PredicateCluster.tenant_id == tenant.id).count() == 0, (
            "a relation was cached from a failed embedding: no later phrase can join it")
        assert db.query(PredicateAlias).filter(PredicateAlias.tenant_id == tenant.id).count() == 0
        assert all_objects(db, tenant) == []

        facts_service.process_pending_memories(db, limit=10)
        assert status(db, a) == "error"
        assert db.query(PredicateCluster).filter(PredicateCluster.tenant_id == tenant.id).count() == 0

        embedder.fail = False
        db.get(AgentMemory, a.id).graph_sync_status = "pending"
        db.commit()
        facts_service.process_pending_memories(db, limit=10)
        assert status(db, a) == "done"
        (cluster,) = db.query(PredicateCluster).filter(PredicateCluster.tenant_id == tenant.id).all()
        assert cluster.centroid is not None
        assert open_objects(db, tenant) == {"Rome"}

    def test_with_no_embedder_the_relation_is_still_learned(self, db, tenant, model, said):
        """No key: the store works and does not merge (a configuration)."""
        said[ROME] = [fact("Rome")]
        a = remember(db, tenant, ROME)
        assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=a)
        assert status(db, a) == "done"
        (cluster,) = db.query(PredicateCluster).filter(PredicateCluster.tenant_id == tenant.id).all()
        assert cluster.centroid is None


# ── the doors that write a fact directly ──────────────────────────────────

@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth(db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="t")
    return {"Authorization": f"Bearer {full}"}


@pytest.fixture
def admin(db, tenant):
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}


def post(client, auth, obj):
    return client.post("/v1/facts", headers=auth, json={
        "user_id": "maria", "subject": "Maria", "predicate": "lives_in", "object": obj})


class TestDirectWrites:
    def test_post_with_no_verdict_is_503_and_writes_nothing(self, client, auth, db, tenant, model):
        assert post(client, auth, "Rome").status_code == 201
        model.judge = Answered(429, "RESOURCE_EXHAUSTED")

        r = post(client, auth, "Milan")
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "model_unavailable"
        assert all_objects(db, tenant) == ["Rome"]

    def test_post_with_a_genuine_no_still_writes(self, client, auth, db, tenant, model):
        assert post(client, auth, "Rome").status_code == 201
        r = post(client, auth, "Milan")
        assert r.status_code == 201, r.text
        assert r.json()["invalidated"] == []
        assert open_objects(db, tenant) == {"Rome", "Milan"}

    def test_patch_with_no_verdict_is_503_and_writes_nothing(self, client, auth, db, tenant, model):
        fid = post(client, auth, "Rome").json()["id"]
        model.judge = "not json at all"

        r = client.patch(f"/v1/facts/{fid}", headers=auth, json={"object": "Milan"})
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "model_unavailable"
        assert all_objects(db, tenant) == ["Rome"]
        assert open_objects(db, tenant) == {"Rome"}

    def test_patch_when_the_vocabulary_does_not_answer_is_503(self, client, auth, db, tenant, model):
        """It was a 500: PATCH caught nothing, POST caught ResolutionUnavailable.
        `summers_in` is a phrase the vocabulary has not seen (`resides_in`
        would be folded into the cached `lives_in` and ask nothing)."""
        fid = post(client, auth, "Rome").json()["id"]
        model.vocabulary = RuntimeError("502 upstream")

        r = client.patch(f"/v1/facts/{fid}", headers=auth, json={"predicate": "summers_in"})
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "model_unavailable"
        assert all_objects(db, tenant) == ["Rome"]

    def test_a_vocabulary_that_never_answers_readably_is_503_not_500(
            self, client, auth, db, tenant, model):
        """With strict reads (0.1.15) an answer none of the question's allows
        is asked again and then raises ResolutionUnusable. It was not among the
        failures the doors catch, so it would have been a 500."""
        fid = post(client, auth, "Rome").json()["id"]
        model.vocabulary = "I would say it depends on the context."

        r = client.patch(f"/v1/facts/{fid}", headers=auth, json={"predicate": "summers_in"})
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "model_unavailable"
        assert all_objects(db, tenant) == ["Rome"]

    def test_the_dashboard_correction_with_no_verdict_is_503(
            self, client, admin, db, tenant, model):
        from korely_agent.services.ids import fact_public_id

        rome = agent_facts.write_fact_direct(
            db, tenant.id, subject="Maria", predicate="lives_in", obj="Rome",
            end_user_id="maria", project_id=seeded_project_id(db, tenant.id))
        model.judge = RuntimeError("timeout")

        r = client.patch(f"/admin/facts/{rome['id']}", headers=admin, json={"object": "Milan"})
        assert r.status_code == 503, r.text
        assert all_objects(db, tenant) == ["Rome"]
        assert open_objects(db, tenant) == {"Rome"}
        assert rome["id"] == fact_public_id(db.query(AgentFact).one().id)

    @pytest.fixture
    def stopped(self):
        """The Cloud's daily emergency stop: the hook of the model gate refuses."""
        from korely_agent.core import extensions

        def refuse(provider, model, region):
            raise llm.NotNow("the daily emergency stop is reached")

        extensions.register("before_model_call", refuse)
        yield
        extensions.reset()

    def test_a_service_stop_is_its_own_503_with_a_retry_after(
            self, client, auth, db, tenant, model, stopped):
        """Audit of 27 Oct 2026: NotNow was swallowed by the engine and
        reached the door as `model_unavailable`, 'retry the request', though a
        retry cannot succeed until the stop lifts. One code, `service_paused`,
        with a Retry-After, on POST and PATCH /v1/facts and the dashboard's."""
        assert post(client, auth, "Rome").status_code == 201
        model.judge = llm.NotNow("the daily emergency stop is reached")

        r = post(client, auth, "Milan")
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "service_paused"
        assert int(r.headers["Retry-After"]) >= 600
        assert all_objects(db, tenant) == ["Rome"]

        fid = db.query(AgentFact).one().id
        from korely_agent.services.ids import fact_public_id

        r = client.patch(f"/v1/facts/{fact_public_id(fid)}", headers=auth, json={"object": "Milan"})
        assert r.status_code == 503 and r.json()["code"] == "service_paused", r.text
        assert "Retry-After" in r.headers

    def test_the_dashboards_correction_in_a_service_stop_is_service_paused(
            self, client, admin, db, tenant, model, stopped):
        rome = agent_facts.write_fact_direct(
            db, tenant.id, subject="Maria", predicate="lives_in", obj="Rome",
            end_user_id="maria", project_id=seeded_project_id(db, tenant.id))
        model.judge = llm.NotNow("the daily emergency stop is reached")
        r = client.patch(f"/admin/facts/{rome['id']}", headers=admin, json={"object": "Milan"})
        assert r.status_code == 503 and r.json()["code"] == "service_paused", r.text
        assert "Retry-After" in r.headers

    def test_a_search_in_a_service_stop_is_service_paused_not_search_unavailable(
            self, client, auth, admin, db, tenant, monkeypatch, stopped):
        """The question's vector is refused by the same gate; the 503 says so."""
        def embed(text, **kw):
            try:
                llm._before_the_call("google", "gemini-embedding", "global")
            except llm.NotNow:
                return None
            return [0.1] * EMBEDDING_DIM

        monkeypatch.setattr("korely_agent.services.memory_engine.embed", embed)
        r = client.post("/v1/memories/search", headers=auth, json={"query": "where does Maria live"})
        assert r.status_code == 503 and r.json()["code"] == "service_paused", r.text
        assert int(r.headers["Retry-After"]) >= 600
        r = client.get("/admin/memories/search", headers=admin, params={"query": "Maria"})
        assert r.status_code == 503 and r.json()["code"] == "service_paused", r.text

    def test_a_model_that_failed_is_still_model_unavailable_after_a_stop(
            self, client, auth, db, tenant, model, stopped):
        """The flag is the last call's: a stop an earlier call met does not
        make a later provider failure a pause."""
        assert post(client, auth, "Rome").status_code == 201
        model.judge = Answered(429, "RESOURCE_EXHAUSTED")
        r = post(client, auth, "Milan")
        assert r.status_code == 503 and r.json()["code"] == "model_unavailable", r.text

    def test_a_failed_embedding_of_the_new_fact_keeps_the_fact_and_retries_it(
            self, client, auth, db, tenant, model, embedder):
        """The vector is what Stage 1b searches with, and what every later
        fact finds this one by, so a fact must not stay without one. The
        morning of 2026-09-28 made the failure refuse the write (503); the
        hosted engine keeps the fact and retries its vector, and so does this
        one now: a provider failing embeddings for an hour no longer turns
        every fact write into a 503."""
        from korely_agent.services import vectors

        assert post(client, auth, "Rome").status_code == 201
        assert db.query(AgentFact).one().embedding is not None
        embedder.fail = True       # "lives_in" is known now: only the fact is embedded

        r = post(client, auth, "Milan")
        assert r.status_code == 201, r.text
        assert open_objects(db, tenant) == {"Rome", "Milan"}
        milan = db.query(AgentFact).filter(AgentFact.object_label == "Milan").one()
        assert milan.embedding is None
        assert (milan.embedding_status, milan.embedding_attempts) == ("pending", 1)
        assert milan.embedding_next_at is not None, "nothing scheduled the retry"

        # The worker's pass once the wait is over, with the provider back.
        embedder.fail = False
        milan.embedding_next_at = None
        db.commit()
        assert vectors.process_pending_vectors(db, limit=10)["processed"] == 1
        db.expire_all()
        milan = db.get(AgentFact, milan.id)
        assert milan.embedding is not None and milan.embedding_status == "done"

    def test_with_no_embedder_the_fact_is_written_without_a_vector(
            self, client, auth, db, tenant, model):
        """A writer but no embedder: a configuration, not a failure."""
        assert post(client, auth, "Rome").status_code == 201
        assert db.query(AgentFact).one().embedding is None

    def test_with_no_model_at_all_facts_are_written_and_read(self, client, auth, db, tenant):
        """The README's promise: with no key, `POST /v1/facts` keeps working
        because it never needed a model. No writer and no embedder here (the
        test above had a writer installed, and promised this; audit of 17 Oct
        2026): a relation the account has never seen, then a second value of
        it, are both written, unembedded, and read back."""
        from korely_agent.core import llm

        assert llm.get_provider() is None and llm.embed("Rome") is None, (
            "this test needs an install with no model at all")
        for obj in ("Rome", "Milan"):
            r = post(client, auth, obj)
            assert r.status_code == 201, r.text
        assert open_objects(db, tenant) == {"Rome", "Milan"}
        assert all(f.embedding is None for f in db.query(AgentFact).all())
        got = client.get("/v1/facts", headers=auth, params={"user_id": "maria"})
        assert got.status_code == 200, got.text
        assert {f["object"] for f in got.json()["facts"]} == {"Rome", "Milan"}


class TestAStaleStopOnAReusedThread:
    def test_an_earlier_requests_refusal_is_not_this_requests(self, db, tenant, monkeypatch):
        """Review of C2: the flag lives on the thread, and a request thread is
        reused. A search and a direct write start with the flag down."""
        from korely_agent.services.memory_engine import MemorySearchUnavailable

        llm._note_here("the service does not call the model now", kind=llm.NOT_CALLED, not_now=True)
        monkeypatch.setattr("korely_agent.services.memory_engine.embed", lambda *a, **k: None)
        with pytest.raises(MemorySearchUnavailable) as e:
            AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id)).search("where")
        assert e.value.paused is False

        llm._note_here("the service does not call the model now", kind=llm.NOT_CALLED, not_now=True)
        seen = []
        orig = facts_service._persist_one
        monkeypatch.setattr(facts_service, "_persist_one",
                            lambda *a, **k: (seen.append(llm.not_now_here()), orig(*a, **k))[1])
        agent_facts.write_fact_direct(db, tenant.id, subject="Maria", predicate="lives_in",
                                      obj="Rome", end_user_id="maria")
        assert seen == [False]
