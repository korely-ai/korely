"""A vector that did not come back is retried, a bounded number of times,
and then said (services/vectors.py, 2026-09-28).

The same fix the facts had, for the memories. Before, with an embedder
configured:

  * a memory whose call failed was stored without a vector, "a future worker
    can backfill" said the code, and there was no worker: it stayed out of
    similarity search for good, and `manage reextract` re-queued facts only;
  * an update whose call failed kept the OLD vector, marked done, on the new
    text: found by what it no longer said;
  * a fact whose call failed refused the whole write (503), where the hosted
    engine keeps the fact and retries.

Now the write keeps the row, `pending`, with the call counted and the next one
scheduled; the drain retries it on a doubling wait; after the last attempt the
row is `error` and the drain leaves it alone until `manage reextract`.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import facts as agent_facts
from korely_agent.services import vector_state, vectors
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id
from tests.conftest import in_the_judges_shape, is_the_judge, stub_extraction


VEC = [0.1] * EMBEDDING_DIM


class Embedder:
    """One embedder behind every name the code reaches it by: the memory
    engine binds `embed` by name, the fact path goes through `core.llm`. A
    Google key is "set" so the code tells a failed call from no embedder."""

    def __init__(self):
        self.fail = False
        self.calls = []
        self.during = None   # a callable run inside the call, once

    def __call__(self, text, **kw):
        self.calls.append(text)
        if self.during is not None:
            during, self.during = self.during, None
            during()
        return None if self.fail else list(VEC)


@pytest.fixture
def embedder(monkeypatch):
    e = Embedder()
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    monkeypatch.setattr("korely_agent.core.llm.embed", e)
    monkeypatch.setattr("korely_agent.services.memory_engine.embed", e)
    return e


@pytest.fixture
def memories(db, tenant):
    return AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id))


def fresh(db, row):
    db.expire_all()
    return db.get(type(row), row.id)


def due_now(db, row):
    """The wait is over: what the clock would do, without waiting for it.
    `updated_at` left as it is, as the drain leaves it."""
    model = type(row)
    db.query(model).filter(model.id == row.id).update(
        {"embedding_next_at": datetime.now(timezone.utc) - timedelta(seconds=1),
         "updated_at": model.updated_at},
        synchronize_session=False)
    db.commit()


# ── the memories ────────────────────────────────────────────────────────────


class TestAMemoryWhoseVectorFailed:
    def test_the_write_succeeds_and_the_memory_waits_with_the_call_counted(
            self, db, memories, embedder):
        embedder.fail = True
        m = memories.add("Maria lives in Rome.", end_user_id="maria")

        m = fresh(db, m)
        assert m.embedding is None
        assert (m.embedding_status, m.embedding_attempts) == ("pending", 1)
        # The first retry a minute after the call, not at once and not never.
        wait = m.embedding_next_at - datetime.now(timezone.utc)
        assert timedelta(seconds=30) < wait <= vector_state.FIRST_PAUSE

    def test_the_worker_gives_it_its_vector_and_search_finds_it(self, db, memories, embedder):
        """The backfill the code promised: there was no worker, and the
        memory stayed out of similarity search for good."""
        embedder.fail = True
        m = memories.add("Maria lives in Rome.", end_user_id="maria")

        embedder.fail = False
        # Search works again, and cannot find a memory with no vector.
        assert memories.search("where does Maria live", end_user_id="maria") == []
        # Not due yet: the drain leaves it alone.
        assert vectors.process_pending_vectors(db, limit=10) == {}
        assert fresh(db, m).embedding is None

        due_now(db, m)
        out = vectors.process_pending_vectors(db, limit=10)
        assert (out["claimed"], out["processed"]) == (1, 1)
        m = fresh(db, m)
        assert m.embedding is not None and m.embedding_status == "done"
        assert m.embedding_next_at is None
        assert [h["content"] for h in memories.search("where does Maria live",
                                                    end_user_id="maria")] == ["Maria lives in Rome."]

    def test_a_vector_computed_later_is_not_an_edit(self, db, memories, embedder):
        """`updated_at` is the developer's: GET returns it and PATCH checks it
        for a stale write. The worker writing a vector must not move it."""
        m = memories.add("Maria lives in Rome.", end_user_id="maria")
        embedder.fail = True
        memories.update(str(m.id), "Maria lives in Milan.")
        before = fresh(db, m).updated_at
        assert before is not None
        embedder.fail = False
        due_now(db, m)
        vectors.process_pending_vectors(db, limit=10)
        assert fresh(db, m).embedding_status == "done"
        assert fresh(db, m).updated_at == before

    def test_a_forgotten_memory_is_left_alone(self, db, tenant, memories, embedder):
        embedder.fail = True
        m = memories.add("Maria lives in Rome.", end_user_id="maria")
        embedder.fail = False
        memories.delete(str(m.id))
        due_now(db, m)
        calls = len(embedder.calls)
        assert vectors.process_pending_vectors(db, limit=10) == {}
        assert len(embedder.calls) == calls


class TestAnUpdate:
    def test_a_failed_re_embed_drops_the_old_vector(self, db, memories, embedder):
        """It kept the vector of the old text, marked done: the memory said
        Milan and was found by Rome, and nothing would ever fix it."""
        m = memories.add("Maria lives in Rome.", end_user_id="maria")
        assert fresh(db, m).embedding_status == "done"

        embedder.fail = True
        memories.update(str(m.id), "Maria lives in Milan.")
        m = fresh(db, m)
        assert m.content == "Maria lives in Milan."
        assert m.embedding is None, "the old text's vector stayed on the new text"
        assert (m.embedding_status, m.embedding_attempts) == ("pending", 1)

        embedder.fail = False
        due_now(db, m)
        vectors.process_pending_vectors(db, limit=10)
        assert embedder.calls[-1] == "Maria lives in Milan."
        assert fresh(db, m).embedding_status == "done"

    def test_the_same_text_keeps_its_vector(self, db, memories, embedder):
        m = memories.add("Maria lives in Rome.", end_user_id="maria")
        embedder.fail = True
        memories.update(str(m.id), "Maria lives in Rome.")
        m = fresh(db, m)
        assert m.embedding is not None and m.embedding_status == "done"

    def test_an_update_during_the_call_wins_over_the_late_vector(
            self, db, memories, embedder):
        """The drain holds no transaction while the model runs, so the text
        can change under it. The vector it gets back is of the OLD text, and
        is dropped rather than written on the new one."""
        embedder.fail = True
        m = memories.add("Maria lives in Rome.", end_user_id="maria")
        embedder.fail = False
        due_now(db, m)

        other = sessionmaker(bind=db.get_bind())()

        def an_update_meanwhile():
            # What `update` leaves when its own inline call fails too: the
            # new text, waiting, one attempt spent, the retry a minute away.
            other.query(AgentMemory).filter(AgentMemory.id == m.id).update(
                {"content": "Maria lives in Milan.", "embedding_status": "pending",
                 "embedding_attempts": 1,
                 "embedding_next_at": datetime.now(timezone.utc) + vector_state.FIRST_PAUSE},
                synchronize_session=False)
            other.commit()

        embedder.during = an_update_meanwhile
        try:
            out = vectors.process_pending_vectors(db, limit=10)
        finally:
            other.close()
        assert (out["claimed"], out["processed"]) == (1, 0)
        m = fresh(db, m)
        assert m.embedding is None, "the vector of 'Rome' was written on 'Milan'"
        assert (m.embedding_status, m.embedding_attempts) == ("pending", 1)


# ── the facts ──────────────────────────────────────────────────────────────


@pytest.fixture
def no_judge(monkeypatch):
    """A provider that answers the vocabulary and the judge like a model with
    nothing to object: the tests here are about the vector only."""
    from korely_agent.core.llm import LLMResult

    class Model:
        def complete(self, messages, **kw):
            prompt = messages[0]["content"]
            if is_the_judge(prompt):
                return LLMResult(text=in_the_judges_shape(prompt, '{"contradictions": []}'))
            if '"family"' in prompt:
                return LLMResult(text='{"family": "other"}')
            if "many_values" in prompt:
                return LLMResult(text='{"many_values": false}')
            return LLMResult(text='{"same_as": null}')

    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Model())


def write_fact(db, tenant, obj):
    return agent_facts.write_fact_direct(
        db, tenant.id, subject="Maria", predicate="lives_in", obj=obj,
        end_user_id="maria", project_id=seeded_project_id(db, tenant.id))


class TestAFactWhoseVectorFailed:
    def test_the_fact_is_written_and_retried_not_refused(
            self, db, tenant, embedder, no_judge):
        write_fact(db, tenant, "Rome")            # the relation is learned
        embedder.fail = True
        shape = write_fact(db, tenant, "Milan")   # only the fact is embedded

        assert shape["object"] == "Milan"
        milan = db.query(AgentFact).filter(AgentFact.object_label == "Milan").one()
        assert milan.embedding is None
        assert (milan.embedding_status, milan.embedding_attempts) == ("pending", 1)

        embedder.fail = False
        due_now(db, milan)
        assert vectors.process_pending_vectors(db, limit=10)["processed"] == 1
        milan = fresh(db, milan)
        assert milan.embedding is not None and milan.embedding_status == "done"
        assert embedder.calls[-1] == "Maria lives_in Milan"

    def test_a_memory_whose_fact_vector_failed_is_done_with_its_facts(
            self, db, tenant, memories, embedder, no_judge, monkeypatch):
        """The extraction path: the memory's facts are kept (no rollback), the
        memory is done, and the one fact without a vector waits for it."""
        from korely_agent.services.facts import service as facts_service
        from korely_agent.services.memory_core.fact_extractor import ExtractedFact

        write_fact(db, tenant, "Rome")
        stub_extraction(monkeypatch, facts_service, lambda content, **kw: [
            ExtractedFact(subject="Maria", predicate_raw="lives_in", object="Milan",
                          confidence=0.9)])
        m = memories.add("Maria moved to Milan.", end_user_id="maria")
        embedder.fail = True

        out = agent_facts.extract_and_persist_facts(db, tenant.id, memory=m)
        assert [s["object"] for s in out] == ["Milan"]
        assert fresh(db, m).graph_sync_status == "done"
        milan = db.query(AgentFact).filter(AgentFact.object_label == "Milan").one()
        assert (milan.embedding_status, milan.embedding_attempts) == ("pending", 1)


class TestBounded:
    def test_it_waits_longer_each_time_and_gives_up_with_error(
            self, db, tenant, memories, embedder):
        embedder.fail = True
        m = memories.add("Maria lives in Rome.", end_user_id="maria")
        waits = []
        for attempt in range(2, vectors.ATTEMPTS + 1):
            due_now(db, m)
            before = datetime.now(timezone.utc)
            vectors.process_pending_vectors(db, limit=10)
            row = fresh(db, m)
            assert row.embedding_attempts == attempt
            if attempt < vectors.ATTEMPTS:
                assert row.embedding_status == "pending"
                waits.append(round((row.embedding_next_at - before).total_seconds()))
        row = fresh(db, m)
        assert row.embedding_status == "error"
        assert row.embedding_next_at is None
        assert waits == sorted(waits), "the wait did not grow"
        assert max(waits) <= vector_state.LONGEST_PAUSE.total_seconds() + 5
        assert len(embedder.calls) == vectors.ATTEMPTS, "more calls than the ladder allows"

        # Given up: the drain no longer calls for it, whatever the clock says.
        embedder.fail = False
        db.query(AgentMemory).filter(AgentMemory.id == m.id).update(
            {"embedding_next_at": None}, synchronize_session=False)
        db.commit()
        assert vectors.process_pending_vectors(db, limit=10) == {}
        assert len(embedder.calls) == vectors.ATTEMPTS

    def test_a_provider_that_is_down_costs_a_few_calls_per_pass_not_one_per_row(
            self, db, tenant, memories, embedder):
        """A pass stops after FAILURES_BEFORE_STOPPING failures in a row. On a
        provider that hangs each call is a whole timeout, and every row would
        spend an attempt learning that the provider is down; the rows not
        reached keep theirs."""
        embedder.fail = True
        rows = [memories.add(f"memory {i}", end_user_id="maria") for i in range(6)]
        for r in rows:
            due_now(db, r)
        calls = len(embedder.calls)

        out = vectors.process_pending_vectors(db, limit=50)
        assert len(embedder.calls) - calls == vectors.FAILURES_BEFORE_STOPPING
        assert out["failed"] == vectors.FAILURES_BEFORE_STOPPING
        attempts = sorted(fresh(db, r).embedding_attempts for r in rows)
        assert attempts == [1, 1, 1, 2, 2, 2], "rows not reached lost an attempt"

    def test_the_ladder(self):
        assert vectors.pause_after(1) == vector_state.FIRST_PAUSE
        assert vectors.pause_after(2) == 2 * vector_state.FIRST_PAUSE
        assert vectors.pause_after(50) == vector_state.LONGEST_PAUSE
        total = sum((vectors.pause_after(n) for n in range(1, vectors.ATTEMPTS)), timedelta())
        # About a day of an unreachable provider, as on the hosted memories.
        assert timedelta(hours=12) < total < timedelta(hours=36)

    def test_reextract_queues_what_gave_up(self, db, tenant, memories, embedder, capsys):
        """`reextract` re-queued facts only; a memory whose vector gave up had
        no way back into search."""
        from korely_agent import manage

        m = memories.add("Maria lives in Rome.", end_user_id="maria")
        db.query(AgentMemory).filter(AgentMemory.id == m.id).update(
            {"embedding": None, "embedding_status": "error",
             "embedding_attempts": vectors.ATTEMPTS}, synchronize_session=False)
        db.commit()

        assert manage.reextract() == 0
        assert "1 memories and 0 facts without a vector -> queued" in capsys.readouterr().out
        row = fresh(db, m)
        assert (row.embedding_status, row.embedding_attempts, row.embedding_next_at) == (
            "pending", 0, None)
        vectors.process_pending_vectors(db, limit=10)
        assert fresh(db, m).embedding_status == "done"

    def test_status_counts_them(self, db, tenant, memories, embedder, capsys):
        from korely_agent import manage

        embedder.fail = True
        memories.add("Maria lives in Rome.", end_user_id="maria")
        manage.status()
        assert "vectors     1 waiting for a retry" in capsys.readouterr().out


class TestTheSafetyNet:
    def test_a_row_left_pending_before_the_retry_existed_is_taken(
            self, db, tenant, memories, embedder):
        """The drain reads the rows, not a queue: a memory stored without a
        vector before this change (no attempt, no schedule) is found too."""
        m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                        end_user_id="maria", content="Maria lives in Rome.")
        db.add(m)
        db.commit()
        assert (fresh(db, m).embedding_status, fresh(db, m).embedding_next_at) == ("pending", None)

        assert vectors.process_pending_vectors(db, limit=10)["processed"] == 1
        assert fresh(db, m).embedding_status == "done"

    def test_with_no_embedder_there_is_nothing_to_do(self, db, tenant, memories, monkeypatch):
        """No Google key: the rows wait, which is true, and nothing is called."""
        called = []
        monkeypatch.setattr("korely_agent.services.memory_engine.embed",
                            lambda *a, **k: called.append(a))
        memories.add("Maria lives in Rome.", end_user_id="maria")
        called.clear()
        assert vectors.process_pending_vectors(db, limit=10) == {}
        assert called == []

    def test_a_claim_survives_a_process_that_dies(self, db, tenant, memories, embedder):
        """The attempt is counted and the next one scheduled when the row is
        claimed, before the call: a worker killed during the call leaves a
        row that comes back when its wait is over, not one stuck forever."""
        embedder.fail = True
        m = memories.add("Maria lives in Rome.", end_user_id="maria")
        due_now(db, m)

        def the_process_dies():
            raise SystemExit("killed")

        embedder.during = the_process_dies
        with pytest.raises(SystemExit):
            vectors.process_pending_vectors(db, limit=10)
        db.rollback()
        row = fresh(db, m)
        assert (row.embedding_status, row.embedding_attempts) == ("pending", 2)
        assert row.embedding_next_at > datetime.now(timezone.utc)


@pytest.mark.parametrize("first", [
    "korely_agent.services.vectors", "korely_agent.services.facts",
    "korely_agent.services.facts.service", "korely_agent.services.memory_engine",
    "korely_agent.services.batch", "korely_agent.services.predicates",
    "korely_agent.services.facts.contradiction", "korely_agent.services.vector_state",
    "korely_agent.services.facts.erasure",
])
def test_each_module_of_the_old_circle_imports_first(first):
    """Audit of 17 Oct 2026: the facts' writer and the memories'
    imported the vector drain, which imported them back inside functions;
    `from .facts.service import fact_vector` moved to the top of vectors.py
    stopped `import korely_agent.services.facts`, and the service with it.
    Each one, imported first in an interpreter of its own, imports."""
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-c", f"import {first}"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]


def _imported(tree) -> set:
    """The modules named by the `from ... import` statements under `tree`,
    as written (`..vectors`, `.facts.service`)."""
    import ast

    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            base = "." * n.level + (n.module or "")
            out.add(base)
            out.update((f"{base}.{a.name}" if n.module else base + a.name) for a in n.names)
    return out


def test_the_writers_import_the_rows_job_not_the_drain():
    """The direction that keeps the circle open: the writers know the row's
    job (vector_state.py), the drain knows the writers, at the top of its
    file and never inside a function."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "korely_agent" / "services"
    for writer in ("facts/service.py", "memory_engine.py", "predicates.py"):
        named = _imported(ast.parse((root / writer).read_text()))
        assert not [m for m in named if m.split(".")[-1] == "vectors"], writer
    drain = ast.parse((root / "vectors.py").read_text())
    lazy = set().union(*(_imported(f) for f in ast.walk(drain) if isinstance(f, ast.FunctionDef)))
    assert not [m for m in lazy if "facts" in m or "memory_engine" in m], lazy
