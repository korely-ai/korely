"""A memory whose fact a caller changed is shown as history in the block
(services/context._changed_by_a_caller, 1.0.29).

The pre-tester check of 15 Nov 2026: after a correction (PATCH
/v1/facts/{id}, korely_correct_fact) the block showed the corrected fact,
"renewal in November", and, unmarked, the memory that said October, under a
note that calls the memories the verbatim source of truth. A closure a
later memory made has that memory in the block, dated after it; a caller's
word (a correction, a forget, a fact a caller wrote that ended it) has
none. Such a memory now wears the mark of a memory whose facts were all
superseded, "(true when said, changed later)", after its day, until its text
is edited: a closure counts against the text it was made on. What the judge
read from memories alone is the block of before.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import and_, select
from sqlalchemy.orm import aliased

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services import user_scope, vectors
from korely_agent.services.facts import changes
from korely_agent.services.ids import fact_public_id
from korely_agent.services.memory_core.presentation import SUPERSEDED_MEMORY_MARK
from korely_agent.services.memory_engine import AgentMemoryEngine

VECTOR = [0.1] * EMBEDDING_DIM
NOW = datetime.now(timezone.utc)
MARK = SUPERSEDED_MEMORY_MARK.strip()
RENEWAL = "Maria prefers email over Slack, and her renewal is in October."
WORK = "Maria works at Acme and lives in Rome."
MOVED = "Maria moved to Milan."


@pytest.fixture(autouse=True)
def _vector(monkeypatch):
    from korely_agent.services import memory_engine

    for module in (ctx, memory_engine):
        monkeypatch.setattr(module, "embed", lambda *a, **k: VECTOR)


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()

    def session():
        try:
            yield db
        finally:
            db.rollback()
    app.dependency_overrides[get_db] = session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def default(db, tenant):
    return project_service.get_or_create_default_project(db, tenant.id)


@pytest.fixture
def head(db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                            name=f"k-{uuid.uuid4().hex[:6]}")
    return {"Authorization": f"Bearer {full}"}


def memory(db, tenant, default, text, days_ago):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=default.id,
                    end_user_id="maria", content=text, embedding=VECTOR,
                    embedding_status=vectors.DONE, graph_sync_status="done",
                    event_time=NOW - timedelta(days=days_ago))
    db.add(m)
    db.commit()
    return m


def fact(db, tenant, default, m, predicate, obj, *, user="maria", model="gemini-test"):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=default.id,
                  end_user_id=user, subject_label="Maria", subject_type="person",
                  predicate_raw=predicate, predicate_canonical=predicate, object_label=obj,
                  object_is_literal=True, confidence=0.9,
                  valid_from=NOW - timedelta(days=40), model_used=model,
                  source_memory_id=getattr(m, "id", None),
                  source_memory_ids=[str(m.id)] if m is not None else None,
                  embedding=VECTOR, embedding_status=vectors.DONE)
    db.add(f)
    db.commit()
    return f


@pytest.fixture
def world(db, tenant, default):
    """Two memories of two facts each: the renewal, and the work whose
    "lives in Rome" a later memory, "moved to Milan", closed (the judge)."""
    renewal = memory(db, tenant, default, RENEWAL, 30)
    fact(db, tenant, default, renewal, "prefers", "email over Slack")
    month = fact(db, tenant, default, renewal, "has_renewal_month", "October")
    work = memory(db, tenant, default, WORK, 20)
    fact(db, tenant, default, work, "works_at", "Acme")
    rome = fact(db, tenant, default, work, "lives_in", "Rome")
    moved = memory(db, tenant, default, MOVED, 10)
    milan = fact(db, tenant, default, moved, "lives_in", "Milan")
    rome.invalid_at, rome.invalidated_by_id = milan.valid_from + timedelta(days=30), milan.id
    milan.valid_from = rome.invalid_at
    db.commit()
    return {"month": month, "rome": rome, "renewal": renewal}


def block(db, tenant, **kw):
    db.expire_all()
    return ctx.assemble_context(db, tenant.id, query="When is Maria's renewal?",
                                end_user_id="maria", token_budget=2000, **kw)["volatile"]


def line_of(out, text):
    (line,) = [line for line in out.splitlines() if text in line]
    return line


def test_a_memory_the_judge_partly_superseded_is_shown_as_before(db, tenant, world):
    out = block(db, tenant)
    for text in (RENEWAL, WORK, MOVED):
        assert MARK not in line_of(out, text), out


def test_a_correction_marks_the_memory_of_the_fact_it_corrected(client, db, tenant, world,
                                                               head):
    month = fact_public_id(world["month"].id)
    r = client.patch(f"/v1/facts/{month}", headers=head, json={"object": "November"})
    assert r.status_code == 200, r.text
    out = block(db, tenant)
    assert MARK in line_of(out, RENEWAL), out
    # The others as before: the judge's closure, and the later memory.
    assert MARK not in line_of(out, WORK) and MARK not in line_of(out, MOVED), out
    # Over the context route too, the door a tester asked through.
    over_rest = client.get("/v1/context", headers=head, params={
        "query": "When is Maria's renewal?", "user_id": "maria", "token_budget": 2000})
    assert over_rest.status_code == 200, over_rest.text
    assert MARK in line_of(over_rest.json()["context"], RENEWAL)


def test_the_block_of_before_the_correction_is_the_one_of_then(client, db, tenant, world,
                                                               head):
    then = datetime.now(timezone.utc)
    month = fact_public_id(world["month"].id)
    assert client.patch(f"/v1/facts/{month}", headers=head,
                        json={"object": "November"}).status_code == 200
    assert MARK not in line_of(block(db, tenant, known_as_of=then), RENEWAL)


def test_a_forget_by_hand_marks_its_memory(client, db, tenant, world, head):
    month = fact_public_id(world["month"].id)
    assert client.post(f"/v1/facts/{month}/forget", headers=head).status_code == 200
    out = block(db, tenant)
    assert MARK in line_of(out, RENEWAL), out
    assert MARK not in line_of(out, WORK), out


def _edit(db, tenant, m, text):
    """The memory's edit as PATCH /v1/memories/{id} makes it
    (`AgentMemoryEngine.update`, which moves `updated_at`), and what its
    extraction would do with a text that says November: the correction's
    open fact restated by this memory too (a reconfirmation)."""
    assert AgentMemoryEngine(db, tenant.id).update(str(m.id), text) is not None
    db.commit()
    db.expire_all()
    november = db.query(AgentFact).filter(AgentFact.object_label == "November",
                                          AgentFact.invalid_at.is_(None)).one()
    november.observation_count = 2
    november.source_memory_ids = list(november.source_memory_ids or []) + [str(m.id)]
    db.commit()


def test_a_memory_edited_after_the_correction_is_read_by_its_new_text(client, db, tenant,
                                                                      world, head):
    """Corrected October -> November, then edited to say November: the
    correction was word on the text before, and the retirement of the edit
    takes the open facts only, so the corrected one keeps the memory as its
    source (review of 16 Nov 2026: the memory, true again, stayed marked for
    good). Counted against the text it was made on, it marks nothing now."""
    month = fact_public_id(world["month"].id)
    assert client.patch(f"/v1/facts/{month}", headers=head,
                        json={"object": "November"}).status_code == 200
    assert MARK in line_of(block(db, tenant), RENEWAL)
    edited = RENEWAL.replace("October", "November")
    _edit(db, tenant, world["renewal"], edited)
    out = block(db, tenant)
    assert MARK not in line_of(out, edited), out


def test_a_correction_after_the_edit_marks_the_new_text(client, db, tenant, world, head):
    """The other way round: the edit first, then a correction of what the
    new text says. That closure is word on the text the block shows."""
    month = fact_public_id(world["month"].id)
    assert client.patch(f"/v1/facts/{month}", headers=head,
                        json={"object": "November"}).status_code == 200
    edited = RENEWAL.replace("October", "November")
    _edit(db, tenant, world["renewal"], edited)
    db.expire_all()
    november = db.query(AgentFact).filter(AgentFact.object_label == "November",
                                          AgentFact.invalid_at.is_(None)).one()
    assert client.patch(f"/v1/facts/{fact_public_id(november.id)}", headers=head,
                        json={"object": "December"}).status_code == 200
    assert MARK in line_of(block(db, tenant), edited)


def test_the_block_of_before_on_a_memory_the_judge_partly_superseded(client, db, tenant,
                                                                     world, head):
    """WORK is partly superseded by the judge (Rome, by Milan) before `then`,
    so the second query runs for it; its works_at is corrected after `then`.
    Read as the store knew it then, the facts open then stand (the review of
    16 Nov 2026 found `true_then(known_as_of)` of that query a live mutant)."""
    db.expire_all()
    then = datetime.now(timezone.utc)
    acme = db.query(AgentFact).filter(AgentFact.object_label == "Acme").one()
    assert client.patch(f"/v1/facts/{fact_public_id(acme.id)}", headers=head,
                        json={"object": "Globex"}).status_code == 200
    assert MARK in line_of(block(db, tenant), WORK)
    assert MARK not in line_of(block(db, tenant, known_as_of=then), WORK)


def test_a_fact_a_caller_wrote_that_ended_it_marks_its_memory(db, tenant, default, world):
    month = world["month"]
    stated = fact(db, tenant, default, None, "has_renewal_month", "November", model="direct")
    month.invalid_at, month.invalidated_by_id = stated.valid_from, stated.id
    db.commit()
    assert MARK in line_of(block(db, tenant), RENEWAL)


def test_a_successor_the_block_may_not_read_marks_nothing(db, tenant, default, world):
    """A caller's fact of another end user (or of another project) that the
    month's fact points at: for this block it is no successor, as for agent
    notices (changes.py, `_readable`)."""
    month = world["month"]
    other = fact(db, tenant, default, None, "has_renewal_month", "November", user="bob",
                 model="direct")
    month.invalid_at, month.invalidated_by_id = other.valid_from, other.id
    db.commit()
    assert MARK not in line_of(block(db, tenant), RENEWAL)
    elsewhere = project_service.create_project(db, tenant_id=tenant.id, name="elsewhere")
    other.end_user_id, other.project_id = "maria", elsewhere.id
    db.commit()
    assert MARK not in line_of(block(db, tenant), RENEWAL)
    other.project_id = default.id
    db.commit()
    assert MARK in line_of(block(db, tenant), RENEWAL)


def test_the_condition_is_the_origin_the_notices_call_explicit(client, db, tenant, default,
                                                               world, head):
    """`closed_by_a_caller` says, fact by fact, what `_origin` labels one of
    EXPLICIT with the successor joined as `_changes` joins it: one rule in
    two forms, on every kind of closure, among them a caller's successor that
    no longer ended the fact and a past statement posted by hand, an end in
    place (`_origin` reads it as one, and the block's states count such a
    fact as standing, memory_core `store.still_standing`)."""
    month = fact_public_id(world["month"].id)
    client.patch(f"/v1/facts/{month}", headers=head, json={"object": "November"})
    gone = fact(db, tenant, default, None, "likes", "jazz", model="direct")
    client.post(f"/v1/facts/{fact_public_id(gone.id)}/forget", headers=head)
    ended = fact(db, tenant, default, None, "likes", "tea")
    by_hand = fact(db, tenant, default, None, "likes", "coffee", model="direct")
    ended.invalid_at, ended.invalidated_by_id = by_hand.valid_from, by_hand.id
    # A caller's fact that no longer is what ended the row: its end was
    # brought earlier since, before the successor began (`_ends`).
    early = fact(db, tenant, default, None, "likes", "milk")
    later = fact(db, tenant, default, None, "likes", "cocoa", model="direct")
    early.invalid_at, early.invalidated_by_id = later.valid_from - timedelta(days=1), later.id
    db.commit()
    # The past said by hand, on the same triple as the work's "Acme".
    assert client.post("/v1/facts", headers=head, json={
        "subject": "Maria", "predicate": "works_at", "object": "Acme",
        "user_id": "maria", "tense": "past"}).status_code == 201

    def readable(f):
        return and_(f.tenant_id == tenant.id,
                    f.project_id.is_not_distinct_from(AgentFact.project_id),
                    user_scope.wall(f.end_user_id, "maria"))
    closer = aliased(AgentFact, name="closer")
    joined = (select(AgentFact.object_label,
                     changes._origin(tenant.id, closer).in_(changes.EXPLICIT))
              .outerjoin(closer, and_(closer.id == AgentFact.invalidated_by_id, readable(closer),
                                      changes._ends(AgentFact, closer)))
              .where(AgentFact.tenant_id == tenant.id, AgentFact.invalid_at.isnot(None)))
    alone = (select(AgentFact.object_label, changes.closed_by_a_caller(tenant.id, readable))
             .where(AgentFact.tenant_id == tenant.id, AgentFact.invalid_at.isnot(None)))
    db.expire_all()
    a, b = dict(db.execute(joined).all()), dict(db.execute(alone).all())
    assert a == b, (a, b)
    assert a == {"October": True, "jazz": True, "tea": True, "Acme": False,
                 "milk": False, "Rome": False}, a
