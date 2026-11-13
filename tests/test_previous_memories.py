"""The memories the extraction reads before a new one (memory_core 2.6).

The end user's own recent words, so "she" and "that plan" can be resolved:
the same tenant, project, end user and agent, before this memory in event
time and then in the order of writing, oldest first, each cut short. Another
person's memories never reach the prompt.
"""
from datetime import datetime, timedelta, timezone


from korely_agent.db.models import AgentMemory
from korely_agent.services.facts.service import previous_memories
from korely_agent.services.memory_core.fact_extractor import MAX_PREVIOUS_CHARS, MAX_PREVIOUS
from tests.conftest import seeded_project_id


T0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


def memory(db, tenant, text, *, minutes=0, eu="maria", agent=None, written=None, deleted=False):
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id), end_user_id=eu,
                    agent_id=agent, content=text, event_time=T0 + timedelta(minutes=minutes),
                    created_at=written or datetime.now(timezone.utc),
                    deleted_at=datetime.now(timezone.utc) if deleted else None)
    db.add(m)
    db.commit()
    return m


def test_the_last_ones_oldest_first(db, tenant):
    """As many as memory_core's MAX_PREVIOUS (ten since 2.6.1, as Mem0)."""
    n = MAX_PREVIOUS + 2
    for i in range(n):
        memory(db, tenant, f"memory {i}", minutes=i)
    new = memory(db, tenant, "the new one", minutes=n + 10)
    assert previous_memories(db, new) == [f"memory {i}" for i in range(2, n)]


def test_only_before_it_in_event_time(db, tenant):
    memory(db, tenant, "earlier", minutes=1)
    new = memory(db, tenant, "the new one", minutes=5)
    memory(db, tenant, "later, written first", minutes=9)
    assert previous_memories(db, new) == ["earlier"]


def test_one_conversation_keeps_its_order(db, tenant):
    """Turns of one session share its time: the order of writing decides."""
    w = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)
    memory(db, tenant, "first turn", written=w)
    memory(db, tenant, "second turn", written=w + timedelta(seconds=1))
    new = memory(db, tenant, "third turn", written=w + timedelta(seconds=2))
    assert previous_memories(db, new) == ["first turn", "second turn"]


def test_never_another_persons_or_a_deleted_one(db, tenant):
    memory(db, tenant, "Luca's memory", eu="luca")
    memory(db, tenant, "another agent's", agent="support-bot")
    memory(db, tenant, "deleted", deleted=True)
    memory(db, tenant, "mine")
    new = memory(db, tenant, "the new one", minutes=5)
    assert previous_memories(db, new) == ["mine"]


def test_cut_short(db, tenant):
    """At memory_core's MAX_PREVIOUS_CHARS (300 since 2.6.1, as Mem0)."""
    memory(db, tenant, "x" * (MAX_PREVIOUS_CHARS + 200))
    new = memory(db, tenant, "the new one", minutes=5)
    assert previous_memories(db, new) == ["x" * MAX_PREVIOUS_CHARS + "…"]


def test_no_end_user_no_context(db, tenant):
    memory(db, tenant, "shared", eu=None)
    new = memory(db, tenant, "the new one", minutes=5, eu=None)
    assert previous_memories(db, new) == []


def _maria_elsewhere(db, tenant_id, project_id, text):
    db.add(AgentMemory(tenant_id=tenant_id, project_id=project_id, end_user_id="maria",
                       content=text, event_time=T0, created_at=datetime.now(timezone.utc)))
    db.commit()


# end_user_id is a free string each customer picks, so 'maria' is in many
# tenants: without the tenant's filter or the project's, another customer's or
# another project's words reached this tenant's prompt, and nothing failed
# (audit of 17 Oct 2026). The same end user, the same agent namespace and an
# earlier time each time.

def test_never_another_projects(db, tenant):
    from korely_agent.services import projects

    elsewhere = projects.create_project(db, tenant_id=tenant.id, name="Pilot")
    _maria_elsewhere(db, tenant.id, elsewhere.id, "another project's")
    memory(db, tenant, "mine")
    new = memory(db, tenant, "the new one", minutes=5)
    assert previous_memories(db, new) == ["mine"]


def test_never_another_tenants(db, tenant):
    """Rows with no project (theirs was deleted: SET NULL) are the case only
    the tenant's filter tells apart, the project's being NULL for both."""
    import uuid

    from korely_agent.db.models import Tenant

    other = Tenant(id=uuid.uuid4(), name="Globex")
    db.add(other)
    db.commit()
    _maria_elsewhere(db, other.id, None, "another tenant's")
    _maria_elsewhere(db, other.id, seeded_project_id(db, other.id), "another tenant's project's")
    _maria_elsewhere(db, tenant.id, None, "mine")
    new = AgentMemory(tenant_id=tenant.id, project_id=None, end_user_id="maria",
                      content="the new one", event_time=T0 + timedelta(minutes=5),
                      created_at=datetime.now(timezone.utc))
    db.add(new)
    db.commit()
    assert previous_memories(db, new) == ["mine"]
