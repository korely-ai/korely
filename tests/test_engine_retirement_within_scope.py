"""memory_core 2.12.0: retiring the previous version of a memory reads only
the memory (audit of 6 Oct 2026).

`store.retire_the_previous_version` runs on every write of a memory; the
condition "said by this source" is an OR with a LIKE on the JSON text of the
sources, and without a limit PostgreSQL read all of `agent_facts`, of all the
customers (Parallel Seq Scan, 41 ms on 121,379 facts; with the scope 1.0
ms). The scope limits only the branch of the reconfirmations: the branch "born
from this source" stays on its index.
"""
# Notes: on korely-agent's tables (tenant_id), with this product's memory
# scope (`facts.entities.memory_scope`, the hosted `judge_scope`);
# the last test spies on korely-agent's write (`facts.service._persist_extracted`),
# whose scope names `agent_facts.tenant_id` where the hosted one named user_id.
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import or_

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import entities
from korely_agent.services.memory_core import store


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def _memory(db, tenant, eu="u1"):
    m = AgentMemory(tenant_id=tenant.id, end_user_id=eu, content="x", graph_sync_status="done")
    db.add(m)
    db.commit()
    return m


def _fact(db, tenant, o, *, eu="u1", born_from=None, sources=None):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=eu, subject_label="Maria",
                  predicate_raw="likes", predicate_canonical="likes", object_label=o,
                  source_memory_id=born_from, source_memory_ids=sources,
                  observation_count=max(1, len(sources or [])))
    db.add(f)
    db.commit()
    return f


def _retire(db, tenant, memory_of, *, scope):
    return store.retire_the_previous_version(
        db, entities.TABLES, memory_of.id, set(), when=NOW,
        is_open=or_(AgentFact.invalid_at.is_(None), AgentFact.invalid_at > NOW), scope=scope,
    )


def test_with_the_scope_reconfirmations_outside_the_memory_are_not_read(db, tenant):
    x, y = _memory(db, tenant), _memory(db, tenant)
    only_x = _fact(db, tenant, "tea", born_from=x.id, sources=[str(x.id)])
    also_y = _fact(db, tenant, "coffee", born_from=y.id, sources=[str(y.id), str(x.id)])
    # A row of another end user with x's id in the text of the sources:
    # the engine cannot produce it (reconfirmations stay inside the memory), and
    # with the scope the retirement does not read it.
    elsewhere = _fact(db, tenant, "water", eu="u2", born_from=y.id, sources=[str(y.id), str(x.id)])

    closed = _retire(db, tenant, x, scope=entities.memory_scope(tenant.id, None, "u1"))

    assert [f.id for f in closed] == [only_x.id]
    assert also_y.invalid_at is None and also_y.source_memory_ids == [str(y.id)]
    assert elsewhere.source_memory_ids == [str(y.id), str(x.id)] and elsewhere.invalid_at is None


def test_the_branch_born_from_this_source_is_not_limited(db, tenant):
    x = _memory(db, tenant)
    born = _fact(db, tenant, "tea", eu="u2", born_from=x.id, sources=[str(x.id)])
    closed = _retire(db, tenant, x, scope=entities.memory_scope(tenant.id, None, "u1"))
    assert [f.id for f in closed] == [born.id]


def test_without_a_scope_as_before(db, tenant):
    x, y = _memory(db, tenant), _memory(db, tenant)
    elsewhere = _fact(db, tenant, "water", eu="u2", born_from=y.id, sources=[str(y.id), str(x.id)])
    _retire(db, tenant, x, scope=None)
    assert elsewhere.source_memory_ids == [str(y.id)]


def test_the_cloud_passes_the_scope_of_the_memory(db, tenant, monkeypatch):
    """`_persist_extracted` retires within the memory: account, project, end
    user."""
    from korely_agent.services.facts import service as svc

    seen = []
    real = store.retire_the_previous_version

    def spy(*a, **k):
        seen.append(k.get("scope"))
        return real(*a, **k)

    monkeypatch.setattr(store, "retire_the_previous_version", spy)
    m = _memory(db, tenant)
    m.graph_sync_status = "processing"
    db.commit()
    svc._persist_extracted(db, tenant.id, memory=m, extracted=[])
    [scope] = seen
    assert scope is not None
    compiled = scope.compile()
    sql, values = str(compiled), set(map(str, compiled.params.values()))
    assert "agent_facts.tenant_id =" in sql and "agent_facts.end_user_id =" in sql
    assert "agent_facts.project_id IS NULL" in sql
    assert {"u1", str(tenant.id)} <= values or {"u1", tenant.id.hex} <= values


def test_an_edited_memory_also_retires_the_fact_dated_ahead(db, tenant):
    """Audit of 17 Oct 2026: re-extracted after an edit with nothing
    left of its old text, the memory closed its current fact and left open
    the one dated twelve hours ahead, which became true at its date. Now the
    write asks "not ended yet", as a forget does: that fact ends at its own
    start, true at no moment."""
    from datetime import timedelta

    from korely_agent.services.facts import service as svc
    from korely_agent.services.facts.temporal import true_at

    now = datetime.now(timezone.utc)
    m = _memory(db, tenant)
    m.graph_sync_status = "processing"
    current = _fact(db, tenant, "tea", born_from=m.id, sources=[str(m.id)])
    ahead = _fact(db, tenant, "the meeting", born_from=m.id, sources=[str(m.id)])
    current.valid_from = now - timedelta(days=1)
    ahead.valid_from = now + timedelta(hours=12)
    db.commit()

    svc._persist_extracted(db, tenant.id, memory=m, extracted=[])
    db.expire_all()

    for f in (current, ahead):
        assert db.get(AgentFact, f.id).invalid_at is not None, f.object_label
    assert db.get(AgentFact, ahead.id).invalid_at == db.get(AgentFact, ahead.id).valid_from
    later = now + timedelta(hours=13)
    assert db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id, true_at(later)).count() == 0
