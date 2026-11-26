"""A name in a facts read is resolved in each fact's own memory (2026-10-06).

An alias is a statement of one memory (account, project, end user, agent
namespace: `entities.memory_of`): Maria's "Acme is now Globex" does not rename
Luca's Acme. But a read across memories (GET /v1/facts and korely_get_facts
without user_id, and the agent namespaces of one end user even with it)
resolved `subject=` / `entity=` once, with ONE map built from the aliases of
every memory, a dict keyed by the alias: with two end users holding the same
alias for two entities the row read last won, so the answer depended on the
order of the writes and held one end user's facts; and a memory without the
alias lost its facts about the name, read under another memory's canonical.
The hosted product had the same code (code audit of 6 Oct 2026).
"""
import pytest

from korely_agent.db.models import AgentEntityAlias
from korely_agent.services import facts as agent_facts
from korely_agent.services.sentinels import EVERY_END_USER as _ALL
from korely_agent.services.sentinels import EVERY_PROJECT



def alias(db, tenant, eu, name, canonical, agent=None):
    db.add(AgentEntityAlias(tenant_id=tenant.id, end_user_id=eu, agent_id=agent,
                            alias_lower=name.lower(), alias_label=name,
                            canonical_label=canonical))
    db.commit()


def fact(db, tenant, eu, subject, predicate, obj, agent=None):
    agent_facts.write_fact_direct(db, tenant.id, subject=subject, predicate=predicate,
                                  obj=obj, end_user_id=eu, agent_id=agent)


def read(db, tenant, **filters):
    filters.setdefault("project_id", EVERY_PROJECT)
    rows, _total = agent_facts.retrieve_agent_facts(db, tenant.id, **filters)
    return sorted((r.end_user_id, r.agent_id, r.object_label) for r in rows)


def _two_end_users(db, tenant, maria_first):
    writes = [lambda: alias(db, tenant, "maria", "Acme", "Globex"),
              lambda: alias(db, tenant, "luca", "Acme", "Initech")]
    for w in (writes if maria_first else writes[::-1]):
        w()
    fact(db, tenant, "maria", "Globex", "uses_crm", "Salesforce")
    fact(db, tenant, "luca", "Initech", "uses_crm", "HubSpot")
    fact(db, tenant, "anna", "Acme", "uses_crm", "Pipedrive")


@pytest.mark.parametrize("maria_first", [True, False])
def test_across_end_users_each_memory_reads_the_name_its_own_way(db, tenant, maria_first):
    _two_end_users(db, tenant, maria_first)
    assert read(db, tenant, end_user_id=_ALL, subject="Acme") == [
        ("anna", None, "Pipedrive"), ("luca", None, "HubSpot"), ("maria", None, "Salesforce")]


@pytest.mark.parametrize("maria_first", [True, False])
def test_the_entity_filter_too(db, tenant, maria_first):
    _two_end_users(db, tenant, maria_first)
    assert read(db, tenant, end_user_id=_ALL, entity="Acme") == [
        ("anna", None, "Pipedrive"), ("luca", None, "HubSpot"), ("maria", None, "Salesforce")]


def test_across_the_agents_of_one_end_user(db, tenant):
    """With user_id: the support agent learned Acme is Globex, the sales
    agent did not, and each one's facts come back under its own reading."""
    alias(db, tenant, "maria", "Acme", "Globex", agent="support")
    fact(db, tenant, "maria", "Globex", "uses_crm", "Salesforce", agent="support")
    fact(db, tenant, "maria", "Acme", "on_plan", "Business", agent="sales")
    assert read(db, tenant, end_user_id="maria", subject="Acme") == [
        ("maria", "sales", "Business"), ("maria", "support", "Salesforce")]
    assert read(db, tenant, end_user_id="maria", agent_id="support", subject="Acme") == [
        ("maria", "support", "Salesforce")]


def test_one_memory_reads_as_before(db, tenant):
    alias(db, tenant, "maria", "Acme", "Globex")
    fact(db, tenant, "maria", "Globex", "uses_crm", "Salesforce")
    assert read(db, tenant, end_user_id="maria", subject="Acme") == [("maria", None, "Salesforce")]
    assert read(db, tenant, end_user_id="maria", subject="Globex") == [("maria", None, "Salesforce")]
    assert read(db, tenant, end_user_id="maria", subject="Initech") == []


def test_a_name_read_costs_the_same_statements_however_many_memories_hold_the_name(db, tenant):
    """Audit of 27 Oct 2026: the name was resolved memory by memory, a query and two SQL
    branches each. Now one condition: the count of statements does not move
    with the number of memories that hold the alias."""
    from sqlalchemy import event

    def statements(n):
        for i in range(n):
            db.add(AgentEntityAlias(tenant_id=tenant.id, end_user_id=f"u{n}-{i}", agent_id=None,
                                    alias_lower="acme", alias_label="Acme",
                                    canonical_label=f"Globex{i % 3}"))
        db.commit()
        seen = []

        def count(conn, cursor, statement, *a):
            seen.append(statement)
        event.listen(db.get_bind(), "before_cursor_execute", count)
        try:
            read(db, tenant, end_user_id=_ALL, entity="Acme")
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", count)
        return len(seen), max(len(s) for s in seen)

    few = statements(3)
    many = statements(60)
    assert many[0] == few[0], (few, many)
    assert many[1] <= few[1] + 200, "the statement grew with the memories"


def test_the_condition_reads_the_alias_of_the_facts_own_memory_in_sql(db, tenant):
    alias(db, tenant, "maria", "Acme", "Globex")
    alias(db, tenant, "luca", "Acme", "Acme")   # a name that means itself
    fact(db, tenant, "maria", "Globex", "uses_crm", "Salesforce")
    fact(db, tenant, "luca", "Acme", "uses_crm", "HubSpot")
    assert read(db, tenant, end_user_id=_ALL, subject="acme") == [
        ("luca", None, "HubSpot"), ("maria", None, "Salesforce")]


def test_the_subqueries_do_not_reference_the_fact_so_they_are_hashed():
    """Review of the audit of 27 Oct 2026: a subquery correlated with each fact scanned the
    alias rows once per fact (14.7 s at 4,000 memories and 20,000 facts). The
    subqueries of the name condition stand alone: two, and since memory_core
    6.25.0 two more for a name of the person read in its agents' memories."""
    import uuid as _u

    from sqlalchemy.dialects import postgresql

    from korely_agent.db.models import AgentFact
    from korely_agent.services.facts import entities

    cond = entities.name_condition(None, _u.uuid4(), "Acme", AgentFact.subject_canonical,
                                      all_end_users=True)
    sql = str(cond.compile(dialect=postgresql.dialect()))
    subqueries = sql.split("(SELECT")[1:]
    assert len(subqueries) == 4
    for sub in subqueries:
        # The subquery alone: up to the parenthesis that closes "(SELECT".
        depth, end = 1, 0
        for end, ch in enumerate(sub):
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if depth == 0:
                break
        assert "agent_facts" not in sub[:end], sub[:end]
