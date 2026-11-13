"""Piece 4 of the common facts engine: the worker's steps are memory_core's
(`worker`): the reaper and the claim with SKIP LOCKED, the event-time
order inside a group, the write of one memory in one transaction, 'done' or
'error' after one attempt.

Until 2026-10-06 this file also held the drain as it was on 0.1.15-dev,
copied verbatim, and drained the same memories once with it and once with the
code of today, comparing the two. The equivalence held in every release
since; the 300 copied lines kept internal names alive and proved nothing
about a deliberate change (the code audit of 6 October). The memories and
the model are the same, and what the drain must do with them is said here:
which memories end 'done', 'error' or untouched, the counts, the plan of
June closing the plan of March at June's date, nothing left by a failed
write.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from korely_agent.core.llm import EMBEDDING_DIM, LLMResult
from korely_agent.db.models import AgentFact, AgentMemory, Tenant
from korely_agent.services.facts import service as S
from tests.conftest import is_the_judge, judge_reply, stub_extraction


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Model:
    def complete(self, messages, **kw):
        p = messages[0]["content"]
        if is_the_judge(p):
            return LLMResult(text=judge_reply(p, lambda existing: [
                {"existing_id": UUID_RE.search(line).group(0), "reason": "stale"}
                for line in existing.splitlines() if UUID_RE.search(line) and "Priority" in line]))
        if "breaks" in p:
            raise RuntimeError("502 upstream")     # the vocabulary does not answer
        if '"family"' in p:
            return LLMResult(text='{"family": "other"}')
        if "many_values" in p:
            return LLMResult(text='{"many_values": false}')
        if '{"past": false}' in p:
            return LLMResult(text='{"past": false}')
        return LLMResult(text='{"same_as": null}')


def ef(s, p, o):
    return SimpleNamespace(subject=s, subject_type="unknown", predicate_raw=p, object=o,
                           object_is_literal=False, confidence=0.9, tense="current", valid_from=None)


SAID = {
    "June: moved to Basic": [ef("c4821", "subscribes_to", "Basic plan")],
    "March: on Priority": [ef("c4821", "subscribes_to", "Priority plan")],
    "boom": RuntimeError("the extraction call failed"),
    "a relation the vocabulary cannot place": [ef("c7", "breaks_things", "x")],
    "left behind by a crash": [ef("c8", "lives_in", "Milan")],
    "somebody is working on it": [ef("c9", "lives_in", "Rome")],
}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    m = Model()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: m)
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: [0.1] * EMBEDDING_DIM)

    def extract(content, **kw):
        out = SAID[content]
        if isinstance(out, Exception):
            raise out
        return list(out)

    stub_extraction(monkeypatch, S, extract)


def seed(db):
    t = Tenant(id=uuid.uuid4(), name=f"t-{uuid.uuid4().hex[:6]}")
    db.add(t)
    db.commit()
    now = datetime.now(timezone.utc)
    rows = [
        # Created in the wrong order on purpose: the drain must follow the events.
        ("June: moved to Basic", "u1", "pending", None, datetime(2026, 6, 20, tzinfo=timezone.utc)),
        ("March: on Priority", "u1", "pending", None, datetime(2026, 3, 15, tzinfo=timezone.utc)),
        ("boom", "u2", "pending", None, None),
        ("a relation the vocabulary cannot place", "u3", "pending", None, None),
        ("left behind by a crash", "u4", "processing", now - timedelta(minutes=60), None),
        ("somebody is working on it", "u5", "processing", now, None),
    ]
    for i, (content, user, status, claimed, ev) in enumerate(rows):
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=t.id, content=content, end_user_id=user,
                           graph_sync_status=status, graph_sync_claimed_at=claimed, event_time=ev,
                           created_at=now - timedelta(seconds=10 - i)))
    db.commit()
    return t


def snapshot(db, t):
    mems = sorted((m.content, m.graph_sync_status) for m in
                  db.query(AgentMemory).filter(AgentMemory.tenant_id == t.id).all())
    facts = db.query(AgentFact).filter(AgentFact.tenant_id == t.id).all()
    by_id = {f.id: f for f in facts}
    trip = lambda f: (f.end_user_id, f.subject_canonical, f.predicate_canonical, f.object_canonical)
    rows = sorted((trip(f), str(f.valid_from)[:10], str(f.invalid_at)[:10] if f.invalid_at else None,
                   trip(by_id[f.invalidated_by_id]) if f.invalidated_by_id in by_id else None)
                  for f in facts)
    return mems, rows


def test_each_memory_ends_where_it_should_with_its_facts(db):
    t = seed(db)
    counts = S.process_pending_memories(db, limit=20)
    mems, facts = snapshot(db, t)

    assert counts == {"claimed": 5, "processed": 3, "failed": 2}, \
        "the memory held by a live claim is not taken"
    assert dict(mems) == {
        "June: moved to Basic": "done", "March: on Priority": "done", "boom": "error",
        "a relation the vocabulary cannot place": "error", "left behind by a crash": "done",
        "somebody is working on it": "processing"}
    priority = [f for f in facts if f[0][3] == "Priority plan"][0]
    assert priority[2] == "2026-06-20" and priority[3][3] == "Basic plan", "June closes March, at June"
    assert not [f for f in facts if f[0][0] in ("u2", "u3")], "a failed write leaves nothing behind"
