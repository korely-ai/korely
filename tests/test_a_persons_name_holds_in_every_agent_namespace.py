"""A name declared for an end user holds in every agent namespace of theirs (memory_core 6.25.0).

Found on the Cloud's 1.0.0 on 31 Oct 2026: two agents of one end user wrote
the same fact ("Marta ... penicillin") and the memory held two open facts,
one under the user_id and one under "Marta"; "Tom" and "test-tom" were two people. A name declared with
`POST /v1/users/{id}/names` and no agent is a row of the end user's own memory
(no agent namespace), and a write canonicalizes with the aliases of its own
memory only (`store.memory_alias_map`): an agent's memory of that end user never
read it, so "Marta" in the doctor agent's notes stayed "Marta" and never met
the user's facts.

Zep's user is one record whose names hold in every thread of that user
(help.getzep.com/users, read on 25 Nov 2026): here the names declared for the
person in their own memory are read by a write of each of their agent
namespaces (`store.Memory.person_names`), the agent's own aliases winning, and
the facts already written there follow a name when it is declared or removed.
A rename stated in the person's memory ("Acme is now Globex") is not a name of
the person and stays that memory's. The fakes: a fixed vector, a judge that
closes nothing; no model, no network (tests/conftest.py).
"""
import uuid

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentEntityAlias, AgentFact, AgentMemory
from korely_agent.services import facts as agent_facts
from korely_agent.services.facts import contradiction as contra
from korely_agent.services.facts import entities as ent
from korely_agent.services.facts import service
from korely_agent.services.memory_core import store as _store
from korely_agent.services.memory_core.fact_extractor import ExtractedFact
from korely_agent.services.sentinels import EVERY_PROJECT

VEC = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
USER = "u-4812"


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(service, "fact_vector", lambda text: list(VEC))
    monkeypatch.setattr(contra, "_classify", lambda c, *a, **k: ([], {}))
    monkeypatch.setattr(contra, "_classify_batch", lambda groups, *a, **k: {g["new_id"]: [] for g in groups})
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)


def remember(db, tenant, subject, predicate, obj, *, agent=None, user=USER, project=None):
    """One memory with one fact, as the extraction writes it."""
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=user, agent_id=agent,
                    project_id=project, content=f"{subject} {predicate} {obj}")
    db.add(m)
    db.commit()
    service._persist_extracted(db, tenant.id, memory=m, valid_from=None, extracted=[
        ExtractedFact(subject=subject, predicate_raw=predicate, object=obj, confidence=0.9)])


def declare(db, tenant, name, *, agent=None, canonical=USER, person=True):
    row, outcome = ent.declare_alias(db, tenant.id, None, USER, agent, name, canonical,
                                     canonical_is_an_end_user=person)
    db.commit()
    return row, outcome


def subjects(db, tenant, agent):
    db.expire_all()
    return sorted((f.subject_label, f.subject_canonical) for f in db.query(AgentFact).filter(
        AgentFact.tenant_id == tenant.id, AgentFact.agent_id == agent,
        AgentFact.invalid_at.is_(None)))


def test_a_name_declared_for_the_person_is_read_by_an_agents_write(db, tenant, quiet):
    """Fails on memory_core 6.24.0: the doctor's "Marta" stayed "Marta"."""
    declare(db, tenant, "Marta")
    remember(db, tenant, "Marta", "is_allergic_to", "penicillin", agent="doctor")
    remember(db, tenant, USER, "is_allergic_to", "penicillin", agent="nurse")
    assert subjects(db, tenant, "doctor") == [("Marta", USER)]
    assert subjects(db, tenant, "nurse") == [(USER, USER)]


def test_the_facts_an_agent_wrote_before_follow_the_name(db, tenant, quiet):
    remember(db, tenant, "Tom", "lives_in", "Munich", agent="assistant")
    assert subjects(db, tenant, "assistant") == [("Tom", "Tom")]
    declare(db, tenant, "Tom")
    assert subjects(db, tenant, "assistant") == [("Tom", USER)]


def test_removing_the_name_gives_the_agents_facts_their_written_names(db, tenant, quiet):
    remember(db, tenant, "Tom", "lives_in", "Munich", agent="assistant")
    row, _ = declare(db, tenant, "Tom")
    assert subjects(db, tenant, "assistant") == [("Tom", USER)]
    mem = ent.memory_of(tenant.id, None, USER, None)
    _store.remove_alias(db, ent.TABLES, mem, row)
    ent.after_a_persons_name(db, tenant.id, None, USER)
    db.commit()
    assert subjects(db, tenant, "assistant") == [("Tom", "Tom")]


def test_the_agents_own_alias_of_the_name_wins(db, tenant, quiet):
    """The sales agent knows another Marta: its own alias says so, and its
    facts stay hers."""
    declare(db, tenant, "Marta")
    declare(db, tenant, "Marta", agent="sales", canonical="Marta Bianchi", person=False)
    remember(db, tenant, "Marta", "works_at", "Acme", agent="sales")
    assert subjects(db, tenant, "sales") == [("Marta", "Marta Bianchi")]


def test_a_rename_in_the_persons_memory_is_not_a_name_of_the_person(db, tenant, quiet):
    declare(db, tenant, "Acme", canonical="Globex", person=False)
    remember(db, tenant, "Acme", "uses_crm", "HubSpot", agent="sales")
    assert subjects(db, tenant, "sales") == [("Acme", "Acme")]


def test_a_read_by_the_name_finds_the_agents_facts(db, tenant, quiet):
    """GET /v1/facts?user_id=...&subject=Marta (`entities.name_condition`)."""
    declare(db, tenant, "Marta")
    remember(db, tenant, "Marta", "is_allergic_to", "penicillin", agent="doctor")
    remember(db, tenant, USER, "lives_in", "Turin", agent="nurse")
    remember(db, tenant, "Marta", "works_at", "Acme", agent="sales")
    remember(db, tenant, USER, "lives_in", "Rome", agent="sales")
    declare(db, tenant, "Marta", agent="sales", canonical="Marta Bianchi", person=False)
    rows, _total = agent_facts.retrieve_agent_facts(db, tenant.id, project_id=EVERY_PROJECT, end_user_id=USER,
                                                    subject="Marta")
    # In the sales agent's memory "Marta" is Marta Bianchi: her fact is
    # read, the user's own fact there is not.
    assert sorted((r.agent_id, r.object_label) for r in rows) == [
        ("doctor", "penicillin"), ("nurse", "Turin"), ("sales", "Acme")]


def test_another_end_users_name_is_not_read(db, tenant, quiet):
    ent.declare_alias(db, tenant.id, None, "u-other", None, "Marta", "u-other",
                      canonical_is_an_end_user=True)
    db.commit()
    remember(db, tenant, "Marta", "is_allergic_to", "penicillin", agent="doctor")
    assert subjects(db, tenant, "doctor") == [("Marta", "Marta")]
    assert db.query(AgentEntityAlias).filter(AgentEntityAlias.agent_id == "doctor").count() == 0


# The review of 25 Nov 2026 (motore/limiti-nomi-correzioni 7465a1a3) --


def test_a_name_pointed_by_hand_at_somebody_else_leaves_the_agents_facts(db, tenant, quiet):
    """A name of the person pointed by the dashboard at somebody else is no
    longer theirs: the agents' facts leave it too (it was read after the row
    changed, and the doctor's "Marta" stayed the user)."""
    remember(db, tenant, "Marta", "lives_in", "Turin", agent="doctor")
    declare(db, tenant, "Marta")
    assert subjects(db, tenant, "doctor") == [("Marta", USER)]
    declare(db, tenant, "Marta", canonical="Marta Bianchi", person=False)
    assert subjects(db, tenant, "doctor") == [("Marta", "Marta")]


def test_a_chain_the_declaration_flattens_is_followed(db, tenant, quiet):
    """"M. Rossi" pointed at "Marta" by hand, then "Marta" declared the user:
    the flattening makes "M. Rossi" a name of the person, in the agents too."""
    declare(db, tenant, "M. Rossi", canonical="Marta", person=False)
    remember(db, tenant, "M. Rossi", "lives_in", "Turin", agent="doctor")
    declare(db, tenant, "Marta")
    assert subjects(db, tenant, "doctor") == [("M. Rossi", USER)]


def test_removing_a_name_keeps_one_the_facts_it_merged(db, tenant, quiet):
    """What the guide says (limits): removing a name gives each fact the
    subject its words give, and two open facts the name had merged into one
    stay one, as in the person's own memory (`store.remove_alias`)."""
    remember(db, tenant, "Marta", "is_allergic_to", "penicillin", agent="doctor")
    remember(db, tenant, USER, "is_allergic_to", "penicillin", agent="doctor")
    remember(db, tenant, "Marta", "lives_in", "Turin", agent="nurse")
    row, _ = declare(db, tenant, "Marta")
    assert len(subjects(db, tenant, "doctor")) == 1
    _store.remove_alias(db, ent.TABLES, ent.memory_of(tenant.id, None, USER, None), row)
    ent.after_a_persons_name(db, tenant.id, None, USER)
    db.commit()
    assert len(subjects(db, tenant, "doctor")) == 1, "the merge stays"
    assert subjects(db, tenant, "nurse") == [("Marta", "Marta")]


def test_declaring_and_removing_leaves_an_agent_with_its_own_alias_as_it_was(db, tenant, quiet):
    remember(db, tenant, "Marta", "lives_in", "Turin", agent="nurse")
    remember(db, tenant, "Marta", "works_at", "Acme", agent="sales")
    declare(db, tenant, "Marta", agent="sales", canonical="Marta Bianchi", person=False)
    before = subjects(db, tenant, "nurse") + subjects(db, tenant, "sales")
    row, _ = declare(db, tenant, "Marta")
    _store.remove_alias(db, ent.TABLES, ent.memory_of(tenant.id, None, USER, None), row)
    ent.after_a_persons_name(db, tenant.id, None, USER)
    db.commit()
    assert subjects(db, tenant, "nurse") + subjects(db, tenant, "sales") == before


def test_a_row_of_another_end_user_pointing_at_the_id_is_not_a_name_of_the_person(db, tenant, quiet):
    ent.declare_alias(db, tenant.id, None, "u-other", None, "Marta", USER, canonical_is_an_end_user=True)
    db.commit()
    remember(db, tenant, "Marta", "lives_in", "Turin", agent="doctor")
    assert subjects(db, tenant, "doctor") == [("Marta", "Marta")]


def test_a_name_declared_in_another_project_is_not_read(db, tenant, quiet):
    from korely_agent.services import projects as projects_service

    p1 = projects_service.create_project(db, tenant_id=tenant.id, name="P one")
    p2 = projects_service.create_project(db, tenant_id=tenant.id, name="P two")
    db.commit()
    ent.declare_alias(db, tenant.id, p1.id, USER, None, "Marta", USER, canonical_is_an_end_user=True)
    db.commit()
    remember(db, tenant, "Marta", "lives_in", "Turin", agent="doctor", project=p2.id)
    remember(db, tenant, "Marta", "lives_in", "Rome", agent="nurse", project=p1.id)
    assert subjects(db, tenant, "doctor") == [("Marta", "Marta")]
    assert subjects(db, tenant, "nurse") == [("Marta", USER)]


def test_a_name_declared_in_an_agent_namespace_holds_there_only(db, tenant, quiet):
    declare(db, tenant, "Marta", agent="sales")
    remember(db, tenant, "Marta", "lives_in", "Turin", agent="doctor")
    remember(db, tenant, "Marta", "works_at", "Acme", agent="sales")
    assert subjects(db, tenant, "doctor") == [("Marta", "Marta")]
    assert subjects(db, tenant, "sales") == [("Marta", USER)]
    row = db.query(AgentEntityAlias).filter(AgentEntityAlias.agent_id == "sales").one()
    assert not ent.is_a_persons_name(row)


def test_a_rename_learned_in_the_persons_memory_is_not_followed_by_the_agents(db, tenant, quiet):
    """Only declared rows are names of the person (`entities.persons_names`):
    a rename a memory taught, even one pointing at the id, stays that memory's."""
    mem = ent.memory_of(tenant.id, None, USER, None)
    row, _ = _store.add_alias(db, ent.TABLES, mem, "Marty", USER, source="rename_fact")
    db.commit()
    remember(db, tenant, "Marty", "lives_in", "Turin", agent="doctor")
    assert subjects(db, tenant, "doctor") == [("Marty", "Marty")]
    assert not ent.is_a_persons_name(row)


def test_every_agent_follows_and_past_the_cap_a_warning_with_counts(db, tenant, quiet, monkeypatch, caplog):
    for agent in ("a1", "a2", "a3"):
        remember(db, tenant, "Tom", "lives_in", "Munich", agent=agent)
    declare(db, tenant, "Tom")
    assert [subjects(db, tenant, a) for a in ("a1", "a2", "a3")] == [[("Tom", USER)]] * 3
    monkeypatch.setattr(ent, "_AGENTS_MAX", 2)
    remember(db, tenant, "Tommy", "works_at", "Acme", agent="a1")
    remember(db, tenant, "Tommy", "works_at", "Acme", agent="a3")
    with caplog.at_level("WARNING"):
        declare(db, tenant, "Tommy")
    assert subjects(db, tenant, "a1") == [("Tom", USER), ("Tommy", USER)]
    assert subjects(db, tenant, "a3") == [("Tom", USER), ("Tommy", "Tommy")], "past the cap: at its next write"
    said = [r.getMessage() for r in caplog.records if "agent namespaces" in r.getMessage()]
    assert said and "Tommy" not in said[0] and USER not in said[0]


def test_the_dashboard_removing_a_name_gives_the_agents_facts_their_names(db, tenant, quiet, monkeypatch):
    """DELETE /admin/aliases/{id} (api/admin.py)."""
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app
    from korely_agent.services import keys as key_service

    monkeypatch.setenv("RUN_WORKER", "false")
    remember(db, tenant, "Tom", "lives_in", "Munich", agent="assistant")
    row, _ = declare(db, tenant, "Tom")
    assert subjects(db, tenant, "assistant") == [("Tom", USER)]
    _r, admin = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as c:
            r = c.delete(f"/admin/aliases/{row.id}", headers={"Authorization": f"Bearer {admin}"})
            assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.clear()
    assert subjects(db, tenant, "assistant") == [("Tom", "Tom")]


def test_erasing_the_memory_of_a_rename_follows_a_name_it_had_made_the_persons(db, tenant, quiet):
    """facts/erasure.undo_aliases: "Marty" declared as "Marta" by hand, a
    rename the memory taught points "Marta" at the id, and the flattening
    makes "Marty" a declared name of the person; undoing the rename gives it
    back to "Marta", no longer a name of the person, and the agents' facts
    take back their written name ("Marta" is the person's memory's own)."""
    from korely_agent.services.facts.erasure import undo_aliases

    mem = ent.memory_of(tenant.id, None, USER, None)
    declare(db, tenant, "Marty", canonical="Marta", person=False)
    learned, _ = _store.add_alias(db, ent.TABLES, mem, "Marta", USER, source="rename_fact")
    db.commit()
    remember(db, tenant, "Marty", "lives_in", "Turin", agent="doctor")
    assert subjects(db, tenant, "doctor") == [("Marty", USER)]
    undo_aliases(db, [learned])
    db.commit()
    assert subjects(db, tenant, "doctor") == [("Marty", "Marty")]


def test_a_project_move_brings_the_name_to_the_agents_there(db, tenant, quiet):
    """services/projects._move_aliases: a name of the person moved into the
    default project reaches the agents' facts there."""
    from korely_agent.services import projects as projects_service

    gone = projects_service.create_project(db, tenant_id=tenant.id, name="Going")
    stays = projects_service.create_project(db, tenant_id=tenant.id, name="Staying")
    db.commit()
    ent.declare_alias(db, tenant.id, gone.id, USER, None, "Marta", USER, canonical_is_an_end_user=True)
    db.commit()
    remember(db, tenant, "Marta", "lives_in", "Turin", agent="doctor", project=stays.id)
    assert subjects(db, tenant, "doctor") == [("Marta", "Marta")]
    projects_service._move_aliases(db, tenant.id, gone.id, stays.id)
    db.commit()
    assert subjects(db, tenant, "doctor") == [("Marta", USER)]


def test_the_names_read_for_a_change_are_the_persons_declared_ones_within_the_cap(db, tenant, quiet, monkeypatch):
    # `names_of_the_person` reads the alias table without a LIMIT
    # (tests/test_no_read_loads_a_whole_table.py, BOUNDED): its filter is one
    # end user's declared rows in their own memory, and every declared row of
    # the account is counted against MAX_DECLARED_NAMES before it is written.
    monkeypatch.setattr(ent, "MAX_DECLARED_NAMES", 3)
    declare(db, tenant, "Marta")
    declare(db, tenant, "Marty")
    ent.declare_alias(db, tenant.id, None, "u-other", None, "Bea", "u-other", canonical_is_an_end_user=True)
    db.commit()
    with pytest.raises(ent.TooManyNames):
        declare(db, tenant, "Martina")
    mem = ent.memory_of(tenant.id, None, USER, None)
    _store.add_alias(db, ent.TABLES, mem, "Acme", "Globex", source="rename_fact")
    db.commit()
    names = ent.names_of_the_person(db, tenant.id, None, USER, None)
    assert names == frozenset(_store.alias_keys(n)[0] for n in ("marta", "marty"))
    assert ent.names_of_the_person(db, tenant.id, None, USER, "sales") is None
