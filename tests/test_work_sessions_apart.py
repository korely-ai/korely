"""Work sessions apart from notes (18 Oct 2026).

The hooks of a coding assistant save each work session as a memory; on the
session bench of 17 Oct 71% of a session's facts were working artefacts, and
nothing could leave them out of a read. A memory now says what kind of text it
is (`kind`: note or session), a fact whether only sessions stated it
(`session_only`), and the reads take `exclude_sessions`.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.core.llm import EMBEDDING_DIM, LLMResult
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import facts as agent_facts
from korely_agent.services import keys as key_service
from korely_agent.services.facts import service as facts_service
from korely_agent.services.memory_core.fact_extractor import ExtractedFact
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import in_the_judges_shape, is_the_judge, seeded_project_id, stub_extraction


NOTE = "Maria said she moved to Turin."
SESSION = "Ran pytest; Maria uses vim; edited api/facts.py."
VEC = [0.1] * EMBEDDING_DIM


class Model:
    def complete(self, messages, **kw):
        prompt = messages[0]["content"]
        if is_the_judge(prompt):
            return LLMResult(text=in_the_judges_shape(prompt, '{"contradictions": []}'))
        if '"family"' in prompt:
            return LLMResult(text='{"family": "other"}')
        if "many_values" in prompt:
            return LLMResult(text='{"many_values": true}')
        return LLMResult(text='{"same_as": null}')


@pytest.fixture
def written(db, tenant, monkeypatch):
    """A note and a session of one end user, with their facts extracted."""
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Model())
    said = {
        NOTE: [ExtractedFact(subject="maria", predicate_raw="lives_in", object="Turin", confidence=0.9)],
        SESSION: [ExtractedFact(subject="maria", predicate_raw="uses", object="vim", confidence=0.9)],
    }
    stub_extraction(monkeypatch, facts_service, lambda content, **kw: said.get(content, []))
    eng = AgentMemoryEngine(db, tenant.id, seeded_project_id(db, tenant.id))
    note = eng.add(NOTE, end_user_id="maria")
    session = eng.add(SESSION, end_user_id="maria", kind="session")
    db.commit()
    for m in (note, session):
        assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=m)
    return eng, note, session, said


def fact(db, obj):
    db.expire_all()
    return db.query(AgentFact).filter(AgentFact.object_label == obj).one()


def test_a_sessions_fact_is_the_sessions_until_a_note_states_it(db, tenant, written):
    eng, note, session, said = written
    assert fact(db, "vim").session_only is True
    assert fact(db, "Turin").session_only is False
    again = "Maria told me she uses vim."
    said[again] = [ExtractedFact(subject="maria", predicate_raw="uses", object="vim", confidence=0.9)]
    m = eng.add(again, end_user_id="maria")
    db.commit()
    assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=m)
    assert fact(db, "vim").session_only is False, "a note stated it: no longer only a session's"


def _restated_by_a_note(db, tenant, eng, said):
    """`vim`, a session's fact, stated again by a note: no longer the
    session's alone. Returns the note."""
    again = "Maria told me she uses vim."
    said[again] = [ExtractedFact(subject="maria", predicate_raw="uses", object="vim", confidence=0.9)]
    m = eng.add(again, end_user_id="maria")
    db.commit()
    assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=m)
    assert fact(db, "vim").session_only is False
    return m


def _left_out_by_exclude_sessions(db, tenant):
    rows, _ = agent_facts.retrieve_agent_facts(db, tenant.id, end_user_id="maria",
                                               project_id=seeded_project_id(db, tenant.id),
                                               exclude_sessions=True)
    return "vim" not in [f.object_label for f in rows]


# When the note goes, the fact is the session's alone again: `session_only`
# was set false for good by the first note and never recounted when that
# note's source was detached (audit of 19 Oct 2026), so exclude_sessions kept
# showing a fact only a work session states.
@pytest.mark.parametrize("how", ["forget", "erase"])
def test_a_fact_is_the_sessions_again_when_the_note_goes(db, tenant, written, how):
    eng, note, session, said = written
    m = _restated_by_a_note(db, tenant, eng, said)
    act = agent_facts.erase_memory if how == "erase" else agent_facts.forget_memory
    assert act(db, tenant.id, m.id, project_id=seeded_project_id(db, tenant.id)) is not None
    assert fact(db, "vim").session_only is True
    assert _left_out_by_exclude_sessions(db, tenant)
    assert fact(db, "Turin").session_only is False


def test_a_fact_is_the_sessions_again_when_the_note_no_longer_states_it(db, tenant, written):
    eng, note, session, said = written
    m = _restated_by_a_note(db, tenant, eng, said)
    edited = "Maria told me she switched editors."
    said[edited] = []
    assert eng.update(str(m.id), edited) is not None
    db.commit()
    agent_facts.extract_and_persist_facts(db, tenant.id, memory=db.get(AgentMemory, m.id))
    vim = fact(db, "vim")
    assert vim.invalid_at is None and vim.source_memory_ids == [str(session.id)]
    assert vim.session_only is True
    assert _left_out_by_exclude_sessions(db, tenant)


def test_a_fact_another_note_states_stays_a_notes(db, tenant, written):
    eng, note, session, said = written
    m = _restated_by_a_note(db, tenant, eng, said)
    other = "Maria's editor is vim."
    said[other] = [ExtractedFact(subject="maria", predicate_raw="uses", object="vim", confidence=0.9)]
    m2 = eng.add(other, end_user_id="maria")
    db.commit()
    assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=m2)
    assert agent_facts.forget_memory(db, tenant.id, m.id,
                                     project_id=seeded_project_id(db, tenant.id)) is not None
    assert fact(db, "vim").session_only is False


def test_a_session_restating_a_notes_fact_leaves_it_a_notes(db, tenant, written):
    eng, note, session, said = written
    restated = "Session log: Maria still lives in Turin."
    said[restated] = [ExtractedFact(subject="maria", predicate_raw="lives_in", object="Turin",
                                    confidence=0.9)]
    m = eng.add(restated, end_user_id="maria", kind="session")
    db.commit()
    assert agent_facts.extract_and_persist_facts(db, tenant.id, memory=m)
    assert fact(db, "Turin").session_only is False


def test_the_memory_reads_leave_the_sessions_out(db, tenant, written, monkeypatch):
    eng, note, session, said = written
    everything = sorted(m["content"] for m in eng.get_all(end_user_id="maria")[0])
    assert everything == sorted([NOTE, SESSION])
    assert [m["content"] for m in eng.get_all(end_user_id="maria", exclude_sessions=True)[0]] == [NOTE]
    for m in (note, session):
        db.get(AgentMemory, m.id).embedding = VEC
    db.commit()
    hits = eng.search("vim", end_user_id="maria", query_vector=VEC, exclude_sessions=True)
    assert [h["content"] for h in hits] == [NOTE]


def test_the_fact_reads_leave_the_sessions_facts_out(db, tenant, written):
    rows, _ = agent_facts.retrieve_agent_facts(db, tenant.id, end_user_id="maria",
                                               project_id=seeded_project_id(db, tenant.id),
                                               exclude_sessions=True)
    assert [f.object_label for f in rows] == ["Turin"]


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
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="claude-code")
    return {"Authorization": f"Bearer {full}"}


def test_the_rest_reads_take_exclude_sessions(client, auth, written):
    facts = client.get("/v1/facts", headers=auth, params={"user_id": "maria", "exclude_sessions": True})
    assert facts.status_code == 200, facts.text
    assert [f["object"] for f in facts.json()["facts"]] == ["Turin"]
    assert "vim" in client.get("/v1/profile", headers=auth, params={"user_id": "maria"}).text
    profile = client.get("/v1/profile", headers=auth, params={"user_id": "maria", "exclude_sessions": True})
    assert profile.status_code == 200 and "vim" not in profile.text and "Turin" in profile.text
    listed = client.get("/v1/memories", headers=auth, params={"user_id": "maria", "exclude_sessions": True})
    assert [m["content"] for m in listed.json()["memories"]] == [NOTE]
    assert listed.json()["memories"][0]["kind"] == "note"
    whole = client.get("/v1/context", headers=auth, params={"user_id": "maria", "query": "editor"})
    assert whole.status_code == 200 and "vim" in whole.json()["context"], whole.text
    context = client.get("/v1/context", headers=auth,
                         params={"user_id": "maria", "query": "editor", "exclude_sessions": True})
    assert context.status_code == 200, context.text
    assert "vim" not in context.json()["context"] and "Turin" in context.json()["context"]


def test_a_write_says_its_kind_and_an_unknown_one_is_refused(client, auth):
    r = client.post("/v1/memories", headers=auth,
                    json={"content": "Session: refactored the drain.", "user_id": "maria", "kind": "session"})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "session"
    r = client.post("/v1/memories", headers=auth, json={"content": "A note.", "user_id": "maria"})
    assert r.json()["kind"] == "note"
    r = client.post("/v1/memories", headers=auth,
                    json={"content": "x", "user_id": "maria", "kind": "transcript"})
    assert r.status_code == 422, r.text


def test_a_batch_item_carries_its_kind_to_the_worker():
    from korely_agent.services import memory_write

    w = memory_write.checked("Session log.", user_id="maria", kind="session")
    assert w.as_item()["kind"] == "session"
    assert memory_write.checked("A note.", user_id="maria", kind="note").kind is None
