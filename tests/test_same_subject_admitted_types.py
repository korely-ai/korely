"""Who enters the review of subjects (memory_core 6.20.0, step 1): admitted
types, not forbidden ones. A subject some open fact types as a product or an
organization (`identity.TYPES_THAT_RENAME`), on both sides of every pair; a
name that even one open fact types as a person never enters; a memory with
an end user is not a wall and is never read."""
from korely_agent.services.facts import subjects
from korely_agent.services.facts.entities import TABLES, wall_of
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import objects as O
from tests.subject_world import fact, memory, vec


def admitted(db, tenant):
    return [n for _k, n, _t in O.admitted_subjects(db, TABLES, wall_of(tenant.id, None), is_open=true_at())]


def test_only_products_and_organizations_enter(db, tenant):
    m = memory(db, tenant, "notes")
    fact(db, tenant, "Korely", "builds", "memory", mem=m, typ="organization", vector=vec(1))
    fact(db, tenant, "Korely Cloud", "runs_version", "1.0.35", mem=m, typ="product", vector=vec(1))
    fact(db, tenant, "Roadmap", "lists", "SDK", mem=m, typ="concept", vector=vec(1))
    fact(db, tenant, "Deploy", "happened_on", "5 Nov", mem=m, typ="event", vector=vec(1))
    assert sorted(admitted(db, tenant)) == ["Korely", "Korely Cloud"]


def test_one_fact_typed_person_keeps_a_name_out(db, tenant):
    m = memory(db, tenant, "notes")
    fact(db, tenant, "Orion", "coordinates", "squad", mem=m, typ="product", vector=vec(1))
    fact(db, tenant, "Orion", "speaks", "Italian", mem=m, typ="person", vector=vec(1))
    fact(db, tenant, "Cloud", "runs_version", "1.0.35", mem=m, typ="product", vector=vec(1))
    fact(db, tenant, "Cloud", "is", "a hosted service", mem=m, typ="concept", vector=vec(1))
    assert admitted(db, tenant) == ["Cloud"], "a product typed once as a concept stays; a person never"


def test_people_are_never_paired(db, tenant):
    m = memory(db, tenant, "Anna moved. My sister Anna lives in Rome.")
    fact(db, tenant, "Anna", "moved_to", "Milan", mem=m, typ="person", vector=vec(2), minutes_ago=300)
    fact(db, tenant, "mia sorella Anna", "lives_in", "Rome", mem=m, typ="person", vector=vec(2), minutes_ago=200)
    r = subjects.review(db, tenant.id, None, dry_run=True)
    assert r["subjects"] == 0 and r["pairs"] == []


def test_a_memory_with_an_end_user_is_not_a_wall(db, tenant):
    fact(db, tenant, "Acme", "on_plan", "Pro", user="maria", vector=vec(3), minutes_ago=300)
    fact(db, tenant, "Acme Inc", "on_plan", "Free", user="maria", vector=vec(3), minutes_ago=200)
    assert admitted(db, tenant) == []
    assert subjects.walls_with_subjects(db, tenant.id, project_id=None) == []
