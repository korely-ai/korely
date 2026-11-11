"""The hosted product's conformance test against its public docs (29 Sep),
the same promises here.

The hosted service was checked, with a fresh key, against every promise in its
documentation, and four failed. They were engine failures, so this engine had
them too; memory_core 2.3.0 fixes them once for both, and these tests mirror
the hosted product's (test_agenti_conformita_29_set.py):

1. An edited memory: "changed values supersede prior facts". The previous
   version's facts stayed open next to the new ones, because a memory's facts
   never close each other (BUG-123), and that held across two versions of one
   text.
2. GET /v1/memories/{id} shows the facts true now
   (tests/test_a_memory_shows_what_is_true_now.py).
3. The context does not repeat a memory, and leaves out one whose facts are
   all superseded.
4. A fact with no timestamp and no date in its text is born when its text
   arrived, not when the worker got round to it.

No model is called: extraction is scripted, the judge is off.
"""
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import service as fsvc
from korely_agent.services.memory_core import presentation
from korely_agent.services.memory_core.fact_extractor import ExtractedFact
from tests.conftest import stub_extraction



@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    monkeypatch.setattr(fsvc, "_embed_fact", lambda *a, **k: None)
    monkeypatch.setattr(fsvc, "detect_and_invalidate", lambda *a, **k: [])


def says(monkeypatch, *triples):
    stub_extraction(monkeypatch, fsvc, lambda *a, **k: [
        ExtractedFact(s, p, o, 0.9, subject_type="person") for s, p, o in triples])


def memory(db, tenant, text, *, at=None):
    m = AgentMemory(tenant_id=tenant.id, end_user_id="maria", content=text, graph_sync_status="pending")
    if at is not None:
        m.created_at = at
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def edit(db, m, text):
    """What engine.update does, with the edit a second later: the test runs in
    one transaction, where the database's now() would not move."""
    m.content = text
    m.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    m.graph_sync_status = "pending"
    db.commit()


def facts_of(db, m):
    db.expire_all()
    return {f.object_label: f for f in db.query(AgentFact).filter(AgentFact.source_memory_id == m.id)}


def aware(v):
    return v if v is None or v.tzinfo else v.replace(tzinfo=timezone.utc)


class TestAnEditedMemory:
    def test_the_previous_versions_facts_close_when_the_new_text_arrives(self, db, tenant, monkeypatch):
        m = memory(db, tenant, "Maria prefers email follow-ups.")
        says(monkeypatch, ("Maria", "likes", "email follow-ups"))
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)

        edit(db, m, "Maria now prefers phone calls.")
        says(monkeypatch, ("Maria", "likes", "phone calls"))
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)

        facts = facts_of(db, m)
        assert facts["phone calls"].invalid_at is None
        old = facts["email follow-ups"]
        assert old.invalid_at is not None, "the previous version's fact is still true"
        assert abs((aware(old.invalid_at) - aware(m.updated_at)).total_seconds()) < 1

    def test_a_fact_the_new_version_says_again_stays_one_and_open(self, db, tenant, monkeypatch):
        m = memory(db, tenant, "Maria lives in Berlin and works at Acme.")
        says(monkeypatch, ("Maria", "lives_in", "Berlin"), ("Maria", "works_at", "Acme"))
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)
        edit(db, m, "Maria lives in Munich and works at Acme.")
        says(monkeypatch, ("Maria", "lives_in", "Munich"), ("Maria", "works_at", "Acme"))
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)

        facts = facts_of(db, m)
        assert facts["Acme"].invalid_at is None
        assert facts["Munich"].invalid_at is None and facts["Berlin"].invalid_at is not None
        assert db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id,
                                          AgentFact.object_label == "Acme").count() == 1

    def test_a_fact_another_memory_also_states_only_loses_this_one(self, db, tenant, monkeypatch):
        a = memory(db, tenant, "Maria works at Acme.")
        says(monkeypatch, ("Maria", "works_at", "Acme"))
        fsvc.extract_and_persist_facts(db, tenant.id, memory=a)
        b = memory(db, tenant, "Maria is at Acme, in sales.")
        fsvc.extract_and_persist_facts(db, tenant.id, memory=b)
        acme = db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id,
                                          AgentFact.object_label == "Acme").one()
        assert acme.observation_count == 2

        edit(db, a, "Maria likes jazz.")
        says(monkeypatch, ("Maria", "likes", "jazz"))
        fsvc.extract_and_persist_facts(db, tenant.id, memory=a)

        db.expire_all()
        acme = db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id,
                                          AgentFact.object_label == "Acme").one()
        assert acme.invalid_at is None
        assert acme.observation_count == 1 and str(a.id) not in (acme.source_memory_ids or [])

    def test_the_first_extraction_closes_nothing(self, db, tenant, monkeypatch):
        m = memory(db, tenant, "Maria likes fish and pasta.")
        says(monkeypatch, ("Maria", "likes", "fish"), ("Maria", "likes", "pasta"))
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)
        assert all(f.invalid_at is None for f in facts_of(db, m).values())

    def test_the_retired_facts_are_announced(self, db, tenant, monkeypatch):
        from korely_agent.services import webhooks
        from korely_agent.services.ids import mem_public_id

        sent = []
        monkeypatch.setattr(webhooks, "emit_event", lambda db_, t, ev, payload: sent.append((ev, payload)))
        m = memory(db, tenant, "Maria prefers email.")
        says(monkeypatch, ("Maria", "likes", "email"))
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)
        edit(db, m, "Maria prefers phone.")
        says(monkeypatch, ("Maria", "likes", "phone"))
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)

        retired = [p for ev, p in sent if ev == "fact.invalidated"]
        assert len(retired) == 1
        assert retired[0]["invalidated_by"] is None and retired[0]["reason"] == "memory_updated"
        assert retired[0]["memory_id"] == mem_public_id(m.id)

    def test_through_the_real_edit_and_the_drain(self, db, tenant, monkeypatch):
        """"prefers email" edited to "now prefers phone" left four facts true."""
        from korely_agent.services.memory_engine import AgentMemoryEngine

        monkeypatch.setattr("korely_agent.services.memory_engine.embed", lambda *a, **k: None)
        eng = AgentMemoryEngine(db, tenant.id, None)
        said = {"Maria prefers email over phone.": [("maria", "likes", "email"), ("maria", "dislikes", "phone")],
                "Maria now prefers phone over email.": [("maria", "likes", "phone"), ("maria", "dislikes", "email")]}
        stub_extraction(monkeypatch, fsvc, lambda content, **k: [
            ExtractedFact(s, p, o, 0.9, subject_type="person") for s, p, o in said[content]])
        m = eng.add("Maria prefers email over phone.", end_user_id="maria")
        fsvc.process_pending_memories(db)
        eng.update(str(m.id), "Maria now prefers phone over email.")
        fsvc.process_pending_memories(db)

        db.expire_all()
        open_now = sorted((f.predicate_canonical, f.object_canonical) for f in db.query(AgentFact)
                          .filter(AgentFact.tenant_id == tenant.id, AgentFact.invalid_at.is_(None)))
        assert open_now == [("dislikes", "email"), ("likes", "phone")]


class TestWhenAFactIsBorn:
    def test_with_no_timestamp_and_no_date_in_the_text_it_is_born_when_the_text_arrived(
            self, db, tenant, monkeypatch):
        arrived = datetime.now(timezone.utc) - timedelta(seconds=20)
        m = memory(db, tenant, "Maria lives in Milan.", at=arrived)
        says(monkeypatch, ("Maria", "lives_in", "Milan"))
        fsvc.process_pending_memories(db, limit=10)
        fact = facts_of(db, m)["Milan"]
        assert abs((aware(fact.valid_from) - arrived).total_seconds()) < 1

    def test_a_date_read_in_the_text_wins_over_the_arrival(self, db, tenant, monkeypatch):
        m = memory(db, tenant, "Maria moved to Milan in May 2025.",
                   at=datetime.now(timezone.utc) - timedelta(seconds=20))
        may = datetime(2025, 5, 1, tzinfo=timezone.utc)
        stub_extraction(monkeypatch, fsvc, lambda *a, **k: [
            ExtractedFact("Maria", "lives_in", "Milan", 0.9, subject_type="person", valid_from=may)])
        fsvc.process_pending_memories(db, limit=10)
        assert aware(facts_of(db, m)["Milan"].valid_from) == may

    def test_the_writes_timestamp_wins_over_both(self, db, tenant, monkeypatch):
        m = memory(db, tenant, "Maria lives in Milan.", at=datetime.now(timezone.utc) - timedelta(seconds=20))
        m.event_time = datetime(2026, 3, 15, tzinfo=timezone.utc)
        db.commit()
        says(monkeypatch, ("Maria", "lives_in", "Milan"))
        fsvc.process_pending_memories(db, limit=10)
        assert aware(facts_of(db, m)["Milan"].valid_from) == datetime(2026, 3, 15, tzinfo=timezone.utc)


class TestTheMemoriesOfTheContext:
    def test_the_shared_rule(self):
        rows = [
            {"id": "a", "content": "Maria prefers email over Slack."},
            {"id": "b", "content": "maria  prefers email over slack."},   # the same text
            {"id": "c", "content": "Maria lives in Rome."},                # every fact superseded
            {"id": "d", "content": "Maria lives in Milan."},               # a fact still true
            {"id": "e", "content": "Call went well, nothing to note."},   # no fact at all
        ]
        state = {"a": (1, 1), "b": (1, 1), "c": (1, 0), "d": (1, 1), "e": (0, 0)}
        kept = presentation.memories_for_the_context(rows, lambda i: state[i])
        assert [r["id"] for r in kept] == ["a", "d", "e"]

    def test_the_context_neither_repeats_nor_brings_back_the_superseded(self, db, tenant, monkeypatch):
        from korely_agent.services import context
        from korely_agent.services.memory_engine import AgentMemoryEngine

        rome = memory(db, tenant, "Maria lives in Rome.")
        says(monkeypatch, ("Maria", "lives_in", "Rome"))
        fsvc.extract_and_persist_facts(db, tenant.id, memory=rome)
        facts_of(db, rome)["Rome"].invalid_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        twice = [memory(db, tenant, "Maria prefers email over Slack.") for _ in range(2)]

        found = [{"id": str(x.id), "content": x.content} for x in [rome, *twice]]
        monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, *a, **k: found)
        # A vector, and no model: since BUG-299 a missing one leaves the
        # memories unsearched, as a failed search would.
        from korely_agent.core.llm import EMBEDDING_DIM

        monkeypatch.setattr(context, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)
        ctx = context.assemble_context(db, tenant.id, query="how to reach Maria",
                                       end_user_id="maria", token_budget=800)["context"]
        assert ctx.count("Maria prefers email over Slack.") == 1
        # Since 0.1.23 (SUPERSEDED_AS_HISTORY, the decision of 8 Oct
        # 2026) the superseded memory is shown as history, never as current
        # truth: its line is marked after its day. Off, it is left out.
        from korely_agent.services.memory_core.presentation import SUPERSEDED_MEMORY_MARK

        rome_line = next(l for l in ctx.splitlines() if "Maria lives in Rome." in l)
        assert SUPERSEDED_MEMORY_MARK.strip() in rome_line
        monkeypatch.setattr("korely_agent.services.context_experiments.SUPERSEDED_AS_HISTORY", False)
        off = context.assemble_context(db, tenant.id, query="how to reach Maria",
                                       end_user_id="maria", token_budget=800)["context"]
        assert "Maria lives in Rome." not in off
