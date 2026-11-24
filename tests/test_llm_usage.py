"""What each model call cost, and who caused it.

The counts were always computed in `core/llm.py` and always thrown away, so on
a machine whose owner pays the model bill, "which of my agents is spending the
money" had no answer. Two things have to hold for the answer to be worth
anything: the number has to be the real one the provider reported, and no call
may happen outside a scope, because a call nobody is billed for is a bill that
quietly does not add up.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentFact, AgentLlmUsage, AgentMemory, Tenant
from korely_agent.services import llm_usage



@pytest.fixture(autouse=True)
def own_session(db, monkeypatch):
    """`flush` opens its own session, like the audit trail. Point it at the test one."""
    monkeypatch.setattr("korely_agent.db.get_own_sessionmaker", lambda: (lambda: db))
    llm_usage.reset_unattributed()
    yield


def rows(db, tenant_id=None):
    q = db.query(AgentLlmUsage)
    if tenant_id is not None:
        q = q.filter(AgentLlmUsage.tenant_id == tenant_id)
    return q.order_by(AgentLlmUsage.ts).all()


class TestRecording:
    def test_a_call_in_a_scope_becomes_a_row(self, db, tenant):
        with llm_usage.attributed(tenant.id, agent_id="support-bot"):
            with llm_usage.stage(llm_usage.EXTRACTION):
                llm_usage.record("gemini", "gemini-2.5-flash", 1200, 340)

        (r,) = rows(db, tenant.id)
        assert r.stage == "extraction"
        assert r.provider == "gemini"
        assert r.model == "gemini-2.5-flash"
        assert r.prompt_tokens == 1200
        assert r.completion_tokens == 340
        assert r.agent_id == "support-bot"

    def test_a_memory_gone_during_its_calls_keeps_the_cost(self, db, tenant):
        """15 Oct 2026: an account closed while the drain extracted
        its last memory, and the cost row failed its foreign key. The calls
        were paid: the row is kept for the account, without the memory."""
        tid = tenant.id
        with llm_usage.attributed(tid, memory_id=uuid.uuid4()):
            llm_usage.record("gemini", "gemini-3.1-flash-lite", 100, 20)
        (r,) = rows(db, tid)
        assert r.memory_id is None and r.prompt_tokens == 100

    def test_a_cost_row_never_names_the_end_user(self, db, tenant, monkeypatch):
        """A cost does not need whose (audit of 19 Oct 2026). The rows
        named the end user and an erasure took them off afterwards, so a row
        written in the moment between the two kept the erased id for 400
        days: since then the table has no column for an end user, and a write
        and a search naming one leave rows of their tokens and their agent."""
        from sqlalchemy import text

        from korely_agent.core.llm import EMBEDDING_DIM
        from korely_agent.services.memory_engine import AgentMemoryEngine
        from tests.conftest import seeded_project_id

        assert not [c.name for c in AgentLlmUsage.__table__.columns if "user" in c.name]

        def billed_embed(text_, task=None, **kw):
            llm_usage.record("gemini", "gemini-embedding-001", 7, 0)
            return [0.1] * EMBEDDING_DIM

        monkeypatch.setattr("korely_agent.services.memory_engine.embed", billed_embed)
        engine = AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id))
        engine.add("user: I live in Milan.", end_user_id="c4821", agent_id="support-bot")
        engine.search("where do I live", end_user_id="c4821", agent_id="support-bot")

        written = db.execute(text("SELECT to_jsonb(u)::text FROM agent_llm_usage u"
                                  " WHERE tenant_id = :t"), {"t": tenant.id}).scalars().all()
        assert len(written) == 2 and not [w for w in written if "c4821" in w], written
        assert {r.agent_id for r in rows(db, tenant.id)} == {"support-bot"}

    def test_an_account_gone_drops_its_rows_quietly(self, db, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="korely_agent.services.llm_usage")
        with llm_usage.attributed(uuid.uuid4(), memory_id=uuid.uuid4()):
            llm_usage.record("gemini", "gemini-3.1-flash-lite", 100, 20)
        assert "account closed" in caplog.text
        assert "could not record" not in caplog.text

    def test_nothing_is_written_until_the_scope_closes(self, db, tenant):
        with llm_usage.attributed(tenant.id):
            llm_usage.record("gemini", "m", 1, 1)
            assert rows(db, tenant.id) == [], "a row was written mid-scope"
        assert len(rows(db, tenant.id)) == 1

    def test_each_stage_is_its_own_row(self, db, tenant):
        with llm_usage.attributed(tenant.id):
            with llm_usage.stage(llm_usage.EXTRACTION):
                llm_usage.record("gemini", "m", 10, 1)
            with llm_usage.stage(llm_usage.CONTRADICTION):
                llm_usage.record("gemini", "m", 20, 2)
                llm_usage.record("gemini", "m", 30, 3)

        got = [(r.stage, r.prompt_tokens) for r in rows(db, tenant.id)]
        assert sorted(got) == [("contradiction", 20), ("contradiction", 30), ("extraction", 10)]

    def test_the_stage_goes_back_to_what_it_was(self, db, tenant):
        with llm_usage.attributed(tenant.id, stage=llm_usage.EXTRACTION):
            with llm_usage.stage(llm_usage.CONTRADICTION):
                pass
            llm_usage.record("gemini", "m", 1, 1)
        assert rows(db, tenant.id)[0].stage == "extraction"

    def test_an_explicit_stage_wins(self, db, tenant):
        """An embedding is an embedding whichever part of the pipeline asked
        for one. Filed under `extraction` it would put the calls that cost a
        thousandth of a cent in the same column as the ones that do not."""
        with llm_usage.attributed(tenant.id):
            with llm_usage.stage(llm_usage.EXTRACTION):
                llm_usage.record("gemini", "embed-model", 8, 0, stage=llm_usage.EMBEDDING)
        assert rows(db, tenant.id)[0].stage == "embedding"

    def test_no_calls_no_row(self, db, tenant):
        with llm_usage.attributed(tenant.id):
            pass
        assert rows(db, tenant.id) == []


class TestOutsideAScope:
    def test_recording_without_a_scope_does_not_crash(self, db):
        llm_usage.record("gemini", "m", 1, 1)
        assert rows(db) == []

    def test_but_it_is_counted(self, db):
        """The silence is the dangerous half. A call nobody is billed for is a
        month that does not add up, and nothing else would say so."""
        assert llm_usage.unattributed() == 0
        llm_usage.record("gemini", "m", 1, 1)
        llm_usage.record("gemini", "m", 1, 1)
        assert llm_usage.unattributed() == 2


class TestItNeverBreaksTheCaller:
    def test_a_failed_write_is_logged_and_swallowed(self, db, tenant, caplog):
        """The work that succeeded must not be reported as failed because the
        bookkeeping could not be stored. Same rule as the audit trail."""
        import logging

        class Broken:
            def add_all(self, *a, **k):
                raise RuntimeError("database is on fire")

            def rollback(self):
                pass

            def close(self):
                pass

        import korely_agent.db as dbmod

        original = dbmod.get_own_sessionmaker
        dbmod.get_own_sessionmaker = lambda: (lambda: Broken())
        try:
            with caplog.at_level(logging.ERROR):
                with llm_usage.attributed(tenant.id):
                    llm_usage.record("gemini", "m", 1, 1)
        finally:
            dbmod.get_own_sessionmaker = original

        assert "could not record" in caplog.text, "it failed silently"

    def test_a_failed_write_is_counted_for_health(self, db, tenant):
        """A log line was the whole trace, and the Cloud's daily stop sums
        these rows: a lost one is spending it does not see (audit of 17 Oct
        2026). /health reads `records.costs`."""
        from korely_agent.services import bookkeeping

        class Broken:
            def add_all(self, *a, **k):
                raise RuntimeError("database is on fire")

            def rollback(self):
                pass

            def close(self):
                pass

        import korely_agent.db as dbmod

        bookkeeping.COSTS.reset()
        original = dbmod.get_own_sessionmaker
        dbmod.get_own_sessionmaker = lambda: (lambda: Broken())
        try:
            with llm_usage.attributed(tenant.id):
                llm_usage.record("gemini", "m", 1, 1)
                llm_usage.record("gemini", "m", 1, 1)
        finally:
            dbmod.get_own_sessionmaker = original
        state = bookkeeping.state()["costs"]
        assert state["state"] == "failing" and state["lost"] == 2, state
        assert "RuntimeError" in state["detail"] and "fire" not in state["detail"]

        with llm_usage.attributed(tenant.id):
            llm_usage.record("gemini", "m", 1, 1)
        state = bookkeeping.state()["costs"]
        assert state["state"] == "working" and state["lost"] == 2, (
            "the losses stay counted after a write that went in")
        bookkeeping.COSTS.reset()


class TestIsolationAndRetention:
    def test_rows_belong_to_one_tenant(self, db, tenant):
        other = Tenant(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()

        with llm_usage.attributed(tenant.id):
            llm_usage.record("gemini", "m", 1, 1)
        with llm_usage.attributed(other.id):
            llm_usage.record("gemini", "m", 2, 2)

        assert [r.prompt_tokens for r in rows(db, tenant.id)] == [1]
        assert [r.prompt_tokens for r in rows(db, other.id)] == [2]

    def test_erasing_a_memory_keeps_the_cost(self, db, tenant):
        """SET NULL, not CASCADE. Deleting the memory must not delete the record
        that it cost money, or the month stops adding up the day somebody
        exercises an erasure request."""
        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                        content="Luca lives in Milan.")
        db.add(m)
        db.commit()

        with llm_usage.attributed(tenant.id, memory_id=m.id):
            llm_usage.record("gemini", "m", 100, 10)
        db.commit()

        db.delete(m)
        db.commit()

        (r,) = rows(db, tenant.id)
        assert r.prompt_tokens == 100
        assert r.memory_id is None

    def test_purge_drops_old_rows_only(self, db, tenant):
        with llm_usage.attributed(tenant.id):
            llm_usage.record("gemini", "m", 1, 1)
        db.commit()
        old = rows(db, tenant.id)[0]
        old.ts = datetime.now(timezone.utc) - timedelta(days=500)
        db.commit()

        with llm_usage.attributed(tenant.id):
            llm_usage.record("gemini", "m", 2, 2)
        db.commit()

        assert llm_usage.purge_expired(db, 400) == 1
        assert [r.prompt_tokens for r in rows(db, tenant.id)] == [2]

    def test_zero_retention_keeps_everything(self, db, tenant):
        with llm_usage.attributed(tenant.id):
            llm_usage.record("gemini", "m", 1, 1)
        db.commit()
        rows(db, tenant.id)[0].ts = datetime.now(timezone.utc) - timedelta(days=5000)
        db.commit()
        assert llm_usage.purge_expired(db, 0) == 0
        assert len(rows(db, tenant.id)) == 1


class TestTheProviderReportsTheRealNumber:
    """`_bill` is called from inside the provider with what the API returned,
    not with an estimate."""

    def test_gemini_counts_thinking_tokens_as_output(self, db, tenant, monkeypatch):
        """Thinking tokens are billed as output. Leaving them out understates
        exactly the models most likely to produce them, which was the whole
        reason the judge was silently truncated for months."""
        from korely_agent.core import llm

        class Usage:
            prompt_token_count = 900
            candidates_token_count = 120
            thoughts_token_count = 1608

        class Resp:
            text = '{"ok": true}'
            usage_metadata = Usage()

        class Models:
            def generate_content(self, **kw):
                return Resp()

        class Client:
            models = Models()

        monkeypatch.setattr(llm, "_gemini_client", lambda key: Client())
        p = llm.GeminiProvider("fake-key")

        with llm_usage.attributed(tenant.id, agent_id="a1"):
            with llm_usage.stage(llm_usage.CONTRADICTION):
                r = p.complete([{"role": "user", "content": "x"}], model="gemini-2.5-flash")

        assert r.completion_tokens == 120 + 1608
        (row,) = rows(db, tenant.id)
        assert row.prompt_tokens == 900
        assert row.completion_tokens == 1728, "thinking tokens were not billed"
        assert row.stage == "contradiction"
        assert row.agent_id == "a1"


class TestEveryCallSiteIsInsideAScope:
    """The test that stops the next model call from being added somewhere that
    never reaches the table.

    A fake provider cannot exercise `_bill`, because it never runs the real
    provider's code. What it can do is look at the seam that matters: whether
    a scope is open at the moment the call site asks for a completion, and what
    stage it is labelled with. That is the half that regresses when somebody
    adds a call in a new place.
    """

    def test_extraction_and_the_judge_run_attributed(self, db, tenant, monkeypatch):
        """The model extracts a plan that differs from the one on record, so
        the judge is asked too, and both calls are seen in their scope. Until
        the audit of 17 Oct 2026 the fake model extracted nothing, the judge
        was never reached, and the name promised a stage nobody watched."""
        seen = []

        class Watching:
            name = "fake"

            def complete(self, messages, **kw):
                scope = llm_usage._active.get()
                seen.append(None if scope is None else (scope.stage, str(scope.tenant_id),
                                                        scope.agent_id, str(scope.memory_id)))
                if scope is not None and scope.stage == llm_usage.EXTRACTION:
                    return LLMResult(text='{"facts": [{"subject": "Maria", "subject_type": "person", '
                                          '"predicate": "subscribes_to", "object": "Starter plan", '
                                          '"confidence": 0.95, "tense": "current"}]}')
                if scope is not None and scope.stage == llm_usage.CONTRADICTION:
                    return LLMResult(text='{"contradictions": []}')
                # The vocabulary's questions, answered as tests/test_common_engine_writing.py does.
                p = messages[0]["content"]
                if '"family"' in p:
                    return LLMResult(text='{"family": "other"}')
                if "many_values" in p:
                    return LLMResult(text='{"many_values": false}')
                if '{"past": false}' in p:
                    return LLMResult(text='{"past": false}')
                return LLMResult(text='{"same_as": null}')

        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Watching())

        from korely_agent.services.facts.service import extract_and_persist_facts

        db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="c4821",
                         subject_label="Maria", subject_canonical="maria",
                         predicate_raw="subscribes_to", predicate_canonical="subscribes_to",
                         object_label="Priority plan", object_canonical="priority plan",
                         confidence=0.9))
        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="c4821",
                        agent_id="support-bot", content="Maria is on the Starter plan.")
        db.add(m)
        db.commit()

        extract_and_persist_facts(db, tenant.id, memory=m)

        assert seen, "the write path made no model call at all"
        assert None not in seen, "a model call happened with nobody to bill"
        stages = {s[0] for s in seen}
        assert {"extraction", "contradiction"} <= stages, f"stages seen: {seen}"
        for stg, tid, agent, mem in seen:
            assert tid == str(tenant.id)
            assert agent == "support-bot"
            assert mem == str(m.id)
            assert stg in ("extraction", "contradiction", "predicates"), stg

    def test_a_fact_written_directly_runs_attributed(self, db, tenant, monkeypatch):
        """Audit of 27 Oct 2026: POST and
        PATCH /v1/facts asked the vocabulary, the judge and the embedder with
        no scope open, so no cost row was written, the Cloud's daily stop
        never saw the calls, and the embedder's per-account share did not
        count them."""
        from korely_agent.services.facts import service as facts_service

        seen = []

        class Watching:
            name = "fake"

            def complete(self, messages, **kw):
                scope = llm_usage._active.get()
                seen.append(None if scope is None else (str(scope.tenant_id), scope.agent_id))
                p = messages[0]["content"]
                if '"family"' in p:
                    return LLMResult(text='{"family": "other"}')
                if "many_values" in p:
                    return LLMResult(text='{"many_values": false}')
                if '{"past": false}' in p:
                    return LLMResult(text='{"past": false}')
                if "contradictions" in p:
                    return LLMResult(text='{"contradictions": []}')
                return LLMResult(text='{"same_as": null}')

        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Watching())
        db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="c4821",
                         subject_label="Maria", subject_canonical="maria",
                         predicate_raw="subscribes_to", predicate_canonical="subscribes_to",
                         object_label="Priority plan", object_canonical="priority plan",
                         confidence=0.9))
        db.commit()

        out = facts_service.write_fact_direct(
            db, tenant.id, subject="Maria", predicate="subscribes_to", obj="Starter plan",
            end_user_id="c4821", agent_id="support-bot")
        assert seen, "the direct write made no model call at all"
        assert None not in seen, "a model call of a direct fact write had nobody to bill"
        assert {s for s in seen} == {(str(tenant.id), "support-bot")}

        seen.clear()
        from korely_agent.services.ids import parse_fact_id

        facts_service.correct_fact(db, tenant.id, parse_fact_id(out["id"]), obj="Pro plan",
                                   project_id=None, write_shape=True)
        assert seen and None not in seen, "a correction made a model call with nobody to bill"

    def test_a_search_is_attributed_too(self, db, tenant, monkeypatch):
        """A read costs money as well, and on a read-heavy install it is the
        only line that says so."""
        seen = []

        def fake_embed(text, task=None, **kw):
            scope = llm_usage._active.get()
            seen.append(None if scope is None else str(scope.tenant_id))
            return None

        monkeypatch.setattr("korely_agent.services.memory_engine.embed", fake_embed)

        from korely_agent.services.memory_engine import AgentMemoryEngine, MemorySearchUnavailable

        engine = AgentMemoryEngine(db, tenant.id)
        with pytest.raises(MemorySearchUnavailable):
            engine.search("anything")

        assert seen == [str(tenant.id)], "the query embedding was billed to nobody"
