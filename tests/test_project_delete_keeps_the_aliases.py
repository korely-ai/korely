"""Deleting a project without `erase` moves its entity aliases with the rest.

Deleting a project moves what it held to the default project: keys, memories,
facts, audit rows, batch jobs. Its aliases ("Mary" is "Maria", "Acme" is now
"Globex") went by the foreign key (ON DELETE CASCADE) with the project row.
The facts arrived in the default project carrying the canonical names the
aliases had given them, and the aliases themselves were gone: a read by the
old name found nothing, and the next write by the old name started a second
entity next to the first.

An alias joins its new memory by the rule every alias obeys there
(memory_core `add_alias`: no cycles, the facts of that memory
recanonicalized). A name the default project has already decided stays
decided: a move brings no newer information than what is there, and the
reply counts what could not come.
"""
import uuid

import pytest

from korely_agent.db.models import AgentEntityAlias, AgentFact
from korely_agent.services import facts as agent_facts
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services.facts import entities as _ent
from korely_agent.services.memory_core import store as _store



@pytest.fixture
def default(db, tenant):
    return project_service.get_or_create_default_project(db, tenant.id)


@pytest.fixture
def gamma(db, tenant, default):
    return project_service.create_project(db, tenant_id=tenant.id, name="Gamma")


def _alias(db, tenant, project, alias, canonical, *, user="u", agent=None):
    mem = _ent.memory_of(tenant.id, project.id, user, agent)
    row, outcome = _store.add_alias(db, _ent.TABLES, mem, alias, canonical, source="user")
    assert outcome == "created", outcome
    db.commit()
    return row


def _aliases(db, tenant):
    db.expire_all()
    return [(str(a.project_id), a.end_user_id, a.alias_label, a.canonical_label)
            for a in db.query(AgentEntityAlias)
                       .filter(AgentEntityAlias.tenant_id == tenant.id)
                       .order_by(AgentEntityAlias.alias_lower).all()]


class TestTheAliasesMove:
    def test_they_land_in_the_default_project_and_still_resolve(
            self, db, tenant, default, gamma):
        row = _alias(db, tenant, gamma, "Mary", "Maria")
        since = row.since
        agent_facts.write_fact_direct(
            db, tenant.id, subject="Mary", predicate="likes", obj="tea",
            end_user_id="u", project_id=gamma.id)

        out = project_service.delete_project(db, tenant.id, gamma)

        assert _aliases(db, tenant) == [(str(default.id), "u", "Mary", "Maria")], (
            "the project's aliases were lost with the project row")
        moved = db.query(AgentEntityAlias).one()
        assert (moved.source, moved.since) == ("user", since), (
            "the alias arrived as a new one: who decided it and since when are lost")
        assert (out["aliases_moved"], out["aliases_not_moved"]) == (1, 0)

        # A read by the old name, in the project the fact now lives in.
        rows, _ = agent_facts.retrieve_agent_facts(
            db, tenant.id, end_user_id="u", subject="Mary", project_id=default.id)
        assert [(r.subject_canonical, r.object_label) for r in rows] == [("Maria", "tea")]
        # And a write by the old name is the same entity, not a second one.
        again = agent_facts.write_fact_direct(
            db, tenant.id, subject="Mary", predicate="likes", obj="tea",
            end_user_id="u", project_id=default.id)
        assert again["observation_count"] == 2
        assert db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).count() == 1

    def test_the_default_projects_facts_follow_the_alias_that_arrived(
            self, db, tenant, default, gamma):
        """The alias now holds in the default project, so that memory's own
        facts under the old name read under the current one, as they would if
        the alias had been made there."""
        _alias(db, tenant, gamma, "Acme", "Globex")
        agent_facts.write_fact_direct(
            db, tenant.id, subject="Acme", predicate="uses", obj="Salesforce",
            end_user_id="u", project_id=default.id)

        project_service.delete_project(db, tenant.id, gamma)
        db.expire_all()

        (fact,) = db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).all()
        assert (fact.subject_label, fact.subject_canonical) == ("Acme", "Globex")

    def test_every_namespace_keeps_its_own(self, db, tenant, default, gamma):
        _alias(db, tenant, gamma, "Mary", "Maria", user="u")
        _alias(db, tenant, gamma, "Mary", "Mariam", user="v", agent="bot")

        project_service.delete_project(db, tenant.id, gamma)

        db.expire_all()
        got = {(a.end_user_id, a.agent_id, a.project_id, a.canonical_label)
               for a in db.query(AgentEntityAlias).all()}
        assert got == {("u", None, default.id, "Maria"), ("v", "bot", default.id, "Mariam")}


class TestANameTheDefaultProjectDecidedStaysDecided:
    def test_a_different_decision_is_not_overwritten(self, db, tenant, default, gamma):
        _alias(db, tenant, default, "Mary", "Marie")
        _alias(db, tenant, gamma, "Mary", "Maria")

        out = project_service.delete_project(db, tenant.id, gamma)

        assert _aliases(db, tenant) == [(str(default.id), "u", "Mary", "Marie")]
        assert (out["aliases_moved"], out["aliases_not_moved"]) == (0, 1)

    def test_the_same_decision_is_one_alias(self, db, tenant, default, gamma):
        _alias(db, tenant, default, "Mary", "Maria")
        _alias(db, tenant, gamma, "Mary", "Maria")

        out = project_service.delete_project(db, tenant.id, gamma)

        assert _aliases(db, tenant) == [(str(default.id), "u", "Mary", "Maria")]
        assert (out["aliases_moved"], out["aliases_not_moved"]) == (1, 0)

    def test_a_cycle_is_refused_and_the_delete_goes_through(self, db, tenant, default, gamma):
        _alias(db, tenant, default, "Maria", "Mary")
        _alias(db, tenant, gamma, "Mary", "Maria")

        out = project_service.delete_project(db, tenant.id, gamma)

        assert _aliases(db, tenant) == [(str(default.id), "u", "Maria", "Mary")]
        assert out["aliases_not_moved"] == 1


class TestTheReplySaysIt:
    def test_the_admin_route_counts_the_aliases(self, db, tenant, default, gamma, monkeypatch):
        from fastapi.testclient import TestClient
        from korely_agent import config
        from korely_agent.db import get_db
        from korely_agent.main import create_app

        _alias(db, tenant, gamma, "Mary", "Maria")
        _row, full = key_service.create_api_key(
            db, tenant_id=tenant.id, name=f"admin-{uuid.uuid4().hex[:6]}",
            scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
        monkeypatch.setenv("RUN_WORKER", "false")
        config.get_settings.cache_clear()
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        try:
            with TestClient(app) as c:
                r = c.delete(f"/admin/projects/{gamma.id}",
                             headers={"Authorization": f"Bearer {full}"})
        finally:
            app.dependency_overrides.clear()
            config.get_settings.cache_clear()
        assert r.status_code == 200, r.text
        assert (r.json()["aliases_moved"], r.json()["aliases_not_moved"]) == (1, 0)

    def test_erasing_still_erases_them(self, db, tenant, default, gamma):
        _alias(db, tenant, gamma, "Mary", "Maria")
        out = project_service.delete_project(db, tenant.id, gamma, erase=True)
        assert _aliases(db, tenant) == []
        assert (out["aliases_moved"], out["aliases_not_moved"]) == (0, 0)
