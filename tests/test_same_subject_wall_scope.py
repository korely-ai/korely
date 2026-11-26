"""A link of the wall (memory_core 6.20.0, `agent_object_links.scope` 'wall') is
not a link of the memory with no agent, never makes a name of a memory
"taken", and no read follows it: the reads of a memory resolve its own
links only (services/facts/links.py)."""
from korely_agent.db.models import AgentObjectLink
from korely_agent.services.facts import links as L
from korely_agent.services.facts.entities import TABLES, memory_of, wall_of
from korely_agent.services.memory_core import objects as O
from tests.subject_world import fact, memory, vec, wall_link


def test_a_wall_link_is_not_the_no_agent_memorys(db, tenant):
    m = memory(db, tenant, "notes", agent=None)
    fact(db, tenant, "Korely Cloud", "runs_version", "1.0.35", mem=m, agent=None, vector=vec(1))
    wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=m)
    no_agent = memory_of(tenant.id, None, None, None)
    assert O.link_rows(db, TABLES, no_agent) == []
    assert O.link_outcome(db, TABLES, no_agent, "Korely Cloud", "Cloud di Korely", family="other") == "created", \
        "the memory's own decision is still free"
    [row] = O.link_rows(db, TABLES, wall_of(tenant.id, None))
    assert row.scope == "wall"
    # The same name in both scopes: two rows, the unique index includes the scope.
    O.add_link(db, TABLES, no_agent, "Korely Cloud", "Cloud di Korely", decided_by="writer", family="other")
    db.commit()
    assert sorted(r.scope for r in db.query(AgentObjectLink)) == ["memory", "wall"]


def test_no_read_follows_a_wall_link(db, tenant):
    m = memory(db, tenant, "notes", agent=None)
    f = fact(db, tenant, "Orion", "deploys", "Korely Cloud", mem=m, agent=None, vector=vec(1))
    wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=m)
    db.expire_all()
    assert L._load(db, [L.memory_key(f)], None) == {L.memory_key(f): []}
