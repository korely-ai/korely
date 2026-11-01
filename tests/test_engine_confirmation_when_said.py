"""memory_core 2.8.0 (work item 12): a fact's last confirmation is when the
thing was said, not when the write ran.

Found on 5 Oct: a history imported today looked as fresh as the morning, and
the bench's blocks changed hour by hour. Now the confirmation takes the
memory's event time (the client's `timestamp`), and an old memory imported
after a new one reconfirms the fact without making it look older. No model:
fake extraction, judge off.
"""
# Port notes: through korely-agent's write (`facts.service.extract_and_persist_facts`)
# on its tables. The extraction is faked with tests.conftest.stub_extraction (this
# product asks for the facts and the observation in one call); no vectors (the
# suite has no embedder); the judge is switched off on both of this product's
# paths, one fact at a time and the batched one (`_BATCHED_JUDGE`).
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import contradiction
from korely_agent.services.facts import service as fsvc
from korely_agent.services.memory_core.fact_extractor import ExtractedFact
from tests.conftest import stub_extraction



@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(fsvc, "_embed_fact", lambda *a, **k: None)
    monkeypatch.setattr(fsvc, "detect_and_invalidate", lambda *a, **k: [])
    monkeypatch.setattr(contradiction, "detect_and_invalidate_batch", lambda *a, **k: {})
    stub_extraction(monkeypatch, fsvc,
                    lambda *a, **k: [ExtractedFact("Ana", "lives_in", "Rome", 0.9, subject_type="person")])


def _said_on(db, tenant, day: datetime) -> AgentFact:
    m = AgentMemory(tenant_id=tenant.id, end_user_id="ana", content="Ana lives in Rome.",
                    graph_sync_status="pending", event_time=day)
    db.add(m); db.commit(); db.refresh(m)
    fsvc.extract_and_persist_facts(db, tenant.id, memory=m, valid_from=day)
    db.expire_all()
    return db.query(AgentFact).filter(AgentFact.predicate_canonical == "lives_in").one()


def _day(v) -> str:
    return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).date().isoformat()


def test_the_confirmation_is_the_day_it_was_said(db, tenant):
    f = _said_on(db, tenant, datetime(2023, 5, 1, 9, tzinfo=timezone.utc))
    assert _day(f.last_confirmed_at) == "2023-05-01"


def test_a_newer_one_moves_it_forward_an_older_one_does_not_bring_it_back(db, tenant):
    _said_on(db, tenant, datetime(2023, 5, 1, 9, tzinfo=timezone.utc))
    f = _said_on(db, tenant, datetime(2023, 6, 1, 9, tzinfo=timezone.utc))
    assert _day(f.last_confirmed_at) == "2023-06-01"
    f = _said_on(db, tenant, datetime(2023, 4, 1, 9, tzinfo=timezone.utc))
    assert _day(f.last_confirmed_at) == "2023-06-01"
    assert f.observation_count == 3


def test_a_date_in_the_future_counts_as_now(db, tenant):
    # Between the two readings of the clock around the write, not the day of
    # one of them: at UTC midnight the write and the check fell on two days
    # (audit of 17 Oct 2026).
    before = datetime.now(timezone.utc)
    f = _said_on(db, tenant, datetime(2999, 1, 1, tzinfo=timezone.utc))
    after = datetime.now(timezone.utc)
    said = f.last_confirmed_at if f.last_confirmed_at.tzinfo else (
        f.last_confirmed_at.replace(tzinfo=timezone.utc))
    assert before <= said <= after, (before, said, after)
