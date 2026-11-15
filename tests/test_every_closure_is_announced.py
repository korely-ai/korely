"""Every fact a write closes gets one `fact.invalidated` (functional test of
1.0.0, 28 Oct 2026).

"Luca works at Acme", then "Luca left Acme and joined Globex": the past
statement closes "works at Acme" in place (memory_core
`writing._the_past`, the memory held it true and the text says it is
over) and the write's fact IS that closed row, so it was in no write's
`invalidated` and no event went out, while the store had closed it. What a
transaction closed is now read from the rows it closed
(services/facts/closures.py), the same way for every closure, and each gets
exactly one event, whichever door wrote it.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact
from korely_agent.services import keys as key_service
from korely_agent.services.ids import fact_public_id


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)


@pytest.fixture
def client(db):
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def head(db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="agent",
                                            scopes=["memories:read", "memories:write"])
    return {"Authorization": f"Bearer {full}"}


@pytest.fixture
def webhooks(monkeypatch):
    seen = []
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event",
                        lambda db, tenant_id, event, payload: seen.append((event, payload)))
    return seen


def _post(client, head, obj, tense="current", subject="Luca", predicate="works at"):
    r = client.post("/v1/facts", headers=head, json={
        "subject": subject, "predicate": predicate, "object": obj, "user_id": "luca",
        "tense": tense})
    assert r.status_code == 201, r.text
    return r.json()


def _invalidations(seen):
    return [(p["fact_id"], p["invalidated_by"]) for e, p in seen if e == "fact.invalidated"]


def test_a_fact_closed_in_place_by_a_past_statement_is_announced(client, head, db, webhooks):
    acme = _post(client, head, "Acme")
    assert _invalidations(webhooks) == []
    left = _post(client, head, "Acme", tense="past")
    # The engine closed the open row itself: the write answers that row.
    assert left["id"] == acme["id"] and left["invalid_at"] is not None
    db.expire_all()
    assert db.get(AgentFact, next(f.id for f in db.query(AgentFact))).invalid_at is not None
    assert _invalidations(webhooks) == [(acme["id"], None)], "one event, no replacing fact"


def test_a_past_statement_of_history_already_closed_announces_nothing(client, head, webhooks):
    """The same past statement again reconfirms the closed row: nothing
    closes, nothing is announced a second time."""
    _post(client, head, "Acme")
    _post(client, head, "Acme", tense="past")
    webhooks.clear()
    _post(client, head, "Acme", tense="past")
    assert _invalidations(webhooks) == []


def test_a_fact_born_as_history_is_no_closure(client, head, webhooks):
    """A past statement with nothing open to close is a new row, closed from
    its birth: no fact the store held true stopped being true."""
    _post(client, head, "Initech", tense="past")
    assert _invalidations(webhooks) == []


def test_a_closure_the_judge_makes_is_announced_once_with_its_closer(
        client, head, db, webhooks, monkeypatch):
    """The control: what a write's `invalidated` names keeps its closer,
    and the reading from the rows adds no second event for it."""
    from korely_agent.db.models import AgentFact as F
    from korely_agent.services.facts import contradiction

    monkeypatch.setattr(contradiction, "_stage1_candidates",
                        lambda db_, tenant_id, end_user_id, *a, exclude_id=None, **k:
                        db_.query(F).filter(F.tenant_id == tenant_id, F.invalid_at.is_(None),
                                            F.id != exclude_id).all())
    monkeypatch.setattr(contradiction, "_classify",
                        lambda candidates, *a, **k: ([c.id for c in candidates], {}))
    acme = _post(client, head, "Acme")
    globex = _post(client, head, "Globex")
    assert globex["invalidated"] == [acme["id"]]
    assert _invalidations(webhooks) == [(acme["id"], globex["id"])]


def test_forgetting_a_fact_is_announced_once(client, head, db, webhooks):
    acme = _post(client, head, "Acme")
    r = client.post(f"/v1/facts/{acme['id']}/forget", headers=head)
    assert r.status_code == 200, r.text
    assert _invalidations(webhooks) == [(acme["id"], None)]


def test_the_record_of_a_transaction_ends_with_it(db, tenant):
    """What one transaction closed is not announced by the next one of the
    same session."""
    from datetime import datetime, timezone
    import uuid

    from korely_agent.services.facts import closures
    from tests.conftest import seeded_project_id

    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="luca",
                  project_id=seeded_project_id(db, tenant.id), subject_label="Luca",
                  predicate_raw="works at", predicate_canonical="works_at", object_label="Acme",
                  valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    f.invalid_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert [x.id for x in closures.closed_unannounced(db, tenant.id)] == [f.id]
    db.commit()
    assert closures.closed_unannounced(db, tenant.id) == []
    assert fact_public_id(f.id)


def test_the_memory_write_announces_the_fact_its_past_statement_closed(db, tenant, monkeypatch,
                                                                       webhooks):
    """The functional test's two memories, through the extraction's write
    (scripted extraction, the judge off): Acme closed in place, one event,
    and Globex closes nothing."""
    from korely_agent.db.models import AgentMemory
    from korely_agent.services.facts import contradiction, service as fsvc
    from korely_agent.services.memory_core.fact_extractor import ExtractedFact
    from tests.conftest import stub_extraction

    monkeypatch.setattr(fsvc, "_embed_fact", lambda *a, **k: None)
    monkeypatch.setattr(fsvc, "detect_and_invalidate", lambda *a, **k: [])
    monkeypatch.setattr(contradiction, "detect_and_invalidate_batch", lambda *a, **k: {})
    said = {
        "Luca works at Acme.": [("Luca", "works_at", "Acme", "current")],
        "Luca left Acme and joined Globex.": [("Luca", "works_at", "Acme", "past"),
                                              ("Luca", "works_at", "Globex", "current")],
    }
    stub_extraction(monkeypatch, fsvc, lambda content, **k: [
        ExtractedFact(s, p, o, 0.9, subject_type="person", tense=t) for s, p, o, t in said[content]])

    def write(text):
        m = AgentMemory(tenant_id=tenant.id, end_user_id="luca", content=text,
                        graph_sync_status="pending")
        db.add(m)
        db.commit()
        db.refresh(m)
        return fsvc.extract_and_persist_facts(db, tenant.id, memory=m)

    write("Luca works at Acme.")
    acme = db.query(AgentFact).filter(AgentFact.object_label == "Acme").one()
    shapes = write("Luca left Acme and joined Globex.")
    assert all(s["invalidated"] == [] for s in shapes), "no write shape names it"
    db.expire_all()
    assert db.get(AgentFact, acme.id).invalid_at is not None
    # E3 (memory_core 6.3.0): the event names the fact that replaced it.
    globex = db.query(AgentFact).filter(AgentFact.object_label == "Globex").one()
    assert _invalidations(webhooks) == [(fact_public_id(acme.id), fact_public_id(globex.id))]


# ── review of fix/1.0-screen (28 Oct 2026) ──────────────────────────────────

def _open_fact(db, tenant, obj="Acme"):
    from datetime import datetime, timezone
    import uuid

    from tests.conftest import seeded_project_id

    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="luca",
                  project_id=seeded_project_id(db, tenant.id), subject_label="Luca",
                  predicate_raw="works at", predicate_canonical="works_at", object_label=obj,
                  valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    return f


def test_a_closed_row_deleted_in_the_same_transaction_is_skipped(db, tenant, webhooks):
    """Its tenant was read before the guard, and the deleted row raised
    ObjectDeletedError out of the announcing pass, rolling the write back."""
    from datetime import datetime, timezone

    from korely_agent.services.facts import closures

    f = _open_fact(db, tenant)
    f.invalid_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db.flush()
    db.query(AgentFact).filter(AgentFact.id == f.id).delete(synchronize_session=False)
    db.expire_all()
    closures.announce_the_rest(db, tenant.id)
    assert _invalidations(webhooks) == []


def test_a_forgets_closures_are_never_announced_by_a_later_write(db, tenant, webhooks):
    """A forget's closures, not committed, then a write that announces in the
    same transaction: the forgotten facts went out as fact.invalidated. The
    privacy rule holds whatever commits when: what a forget or an erasure
    closes is left out of the announcing pass."""
    from korely_agent.db.models import AgentMemory
    from korely_agent.services.facts import erasure, service as fsvc
    from korely_agent.services.sentinels import EVERY_PROJECT

    m = AgentMemory(tenant_id=tenant.id, end_user_id="luca", content="Luca works at Acme.",
                    graph_sync_status="done")
    db.add(m)
    db.commit()
    f = _open_fact(db, tenant)
    f.source_memory_id, f.source_memory_ids = m.id, [str(m.id)]
    db.commit()
    erasure._forget(db, tenant.id, m, project_id=EVERY_PROJECT)
    assert f.invalid_at is not None, "the forget closed it"
    fsvc.write_fact_direct(db, tenant.id, subject="Bob", predicate="likes", obj="tea",
                           end_user_id="bob")
    assert _invalidations(webhooks) == []


def test_a_closure_another_transaction_committed_first_is_not_announced_again(
        db, tenant, webhooks):
    """Two writes complete the same plan at once (memory_core
    `writing._insert`, the loser's branch): the winner closes the plan,
    names it and commits; the loser, which read the plan open, closes it again
    after the winner's commit and points it at the winner's fact. Its own pass
    announced the plan a second time. A closure is announced by the pass only
    when the committed row was still open at its end."""
    from datetime import datetime, timezone

    from korely_agent.db import get_own_sessionmaker
    from korely_agent.services.facts import closures

    plan = _open_fact(db, tenant, obj="Globex")
    done = _open_fact(db, tenant, obj="Globex, done")
    assert plan.invalid_at is None  # read open by this (the loser's) session
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with get_own_sessionmaker()() as winner:
        row = winner.get(AgentFact, plan.id)
        row.invalid_at, row.invalidated_by_id = end, done.id
        winner.commit()
    plan.invalid_at, plan.invalidated_by_id = end, done.id
    db.flush()
    closures.announce_the_rest(db, tenant.id)
    assert _invalidations(webhooks) == []
    db.rollback()


def test_a_closure_this_transaction_makes_first_is_announced(db, tenant, webhooks):
    """The control: the committed row is open, so the closure is this
    transaction's."""
    from datetime import datetime, timezone

    from korely_agent.services.facts import closures

    f = _open_fact(db, tenant)
    f.invalid_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db.flush()
    closures.announce_the_rest(db, tenant.id)
    assert _invalidations(webhooks) == [(fact_public_id(f.id), None)]
