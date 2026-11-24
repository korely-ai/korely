"""`manage reextract --empty`: the memories marked done that produced no fact.

`id NOT IN (the source memory of every fact)` was never true once a single
fact had no source memory (POST /v1/facts writes such facts), so the command
found nothing and printed "nothing to redo" (the audit of 2026-10-06, row 9).
"""
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent import manage
from korely_agent.db.models import AgentFact, AgentMemory



def _memory(db, tenant, content):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                    content=content, graph_sync_status="done")
    db.add(m)
    db.commit()
    return m


def _fact(db, tenant, source=None, also=None):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                  subject_label="Maria", predicate_raw="lives in", predicate_canonical="lives_in",
                  object_label="Milan", source_memory_id=source,
                  source_memory_ids=[str(x) for x in (also or [])] or None,
                  observation_count=len(also) if also else 1,
                  valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    return f


def _status(db, m):
    db.expire_all()
    return db.get(AgentMemory, m.id).graph_sync_status


@pytest.fixture(autouse=True)
def _the_test_session(db, monkeypatch):
    monkeypatch.setattr(manage, "_session", lambda: db)


def test_a_fact_with_no_source_memory_does_not_hide_the_empty_ones(db, tenant, capsys):
    empty = _memory(db, tenant, "hello there")
    _fact(db, tenant, source=None)            # a POST /v1/facts row
    assert manage.reextract(errors=False, empty=True) == 0
    assert _status(db, empty) == "pending"
    assert "1 without any fact" in capsys.readouterr().out


def test_a_memory_that_produced_a_fact_stays_done(db, tenant):
    full = _memory(db, tenant, "Maria lives in Milan.")
    _fact(db, tenant, source=full.id)
    manage.reextract(errors=False, empty=True)
    assert _status(db, full) == "done"


def test_a_memory_that_only_reconfirmed_a_fact_produced_it(db, tenant):
    first = _memory(db, tenant, "Maria lives in Milan.")
    again = _memory(db, tenant, "Maria still lives in Milan.")
    _fact(db, tenant, source=first.id, also=[first.id, again.id])
    manage.reextract(errors=False, empty=True)
    assert (_status(db, first), _status(db, again)) == ("done", "done")


def test_another_tenants_fact_does_not_count(db, tenant):
    from korely_agent.db.models import Tenant

    other = Tenant(id=uuid.uuid4(), name="Somebody Else")
    db.add(other)
    db.commit()
    empty = _memory(db, tenant, "hello there")
    f = _fact(db, other, source=None)
    f.source_memory_id = empty.id             # never across tenants in practice
    db.commit()
    manage.reextract(errors=False, empty=True)
    assert _status(db, empty) == "pending"
