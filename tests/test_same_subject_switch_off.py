"""The switch off (services/facts/subjects.SAME_SUBJECT_REVIEW, the default):
with links of a wall in the store, the judge of a write runs exactly the
statements it runs without them, and memory_core's candidates without a
group, or with a group of one name, are the statements of 6.19.0."""
from sqlalchemy import event

from korely_agent.db.models import AgentFactClosureLink, AgentObjectLink
from korely_agent.services.facts import contradiction, subjects
from korely_agent.services.facts.entities import TABLES, memory_scope
from korely_agent.services.memory_core import candidates as C
from tests.subject_world import Judge, fact, memory, vec, wall_link


def statements(db, run):
    """The statements `run` sends, a savepoint's number left out (the
    session counts them)."""
    import re

    seen = []

    def keep(conn, cursor, statement, parameters, context, executemany):
        seen.append((re.sub(r"sa_savepoint_\d+", "sa_savepoint", statement),
                     repr(sorted(parameters.items())) if isinstance(parameters, dict) else repr(parameters)))
    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", keep)
    try:
        out = run()
    finally:
        event.remove(engine, "before_cursor_execute", keep)
    return out, seen


def world(db, tenant):
    a = memory(db, tenant, "Korely Cloud has version 1.0.30.", agent="ops-agent", days_ago=5)
    b = memory(db, tenant, "Cloud di Korely runs 1.0.35.", agent="ops-agent", days_ago=1)
    old = fact(db, tenant, "Korely Cloud", "has_version", "1.0.30", mem=a, agent="ops-agent", minutes_ago=3000,
               vector=vec(5))
    new = fact(db, tenant, "Cloud di Korely", "runs_version", "1.0.35", mem=b, agent="ops-agent",
               minutes_ago=1, vector=vec(5))
    return old, new, a


def judge_it(db, tenant_id, args):
    out = contradiction.detect_and_invalidate(db, tenant_id, end_user_id=None, **args)
    db.rollback()
    return out


def args_of(new) -> dict:
    """The write's arguments as plain values, read before anything is
    counted (a row expired by a rollback would be read again inside)."""
    return dict(new_fact_id=new.id, new_subject=new.subject_label, new_predicate_raw=new.predicate_raw,
                new_predicate_canonical=new.predicate_canonical, new_object=new.object_label,
                new_embedding=list(new.embedding), new_valid_from=new.valid_from,
                new_subject_canonical=new.subject_canonical, new_object_canonical=new.object_canonical,
                source=new.source_memory_id, agent_id=new.agent_id)


def test_off_the_wall_links_change_no_statement_of_the_judge(db, tenant, monkeypatch):
    j = Judge()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: j)
    assert subjects.SAME_SUBJECT_REVIEW is False
    old, new, a = world(db, tenant)
    tid, args = tenant.id, args_of(new)
    judge_it(db, tid, args)    # the first run warms what a connection keeps (the HNSW depth)
    without, before = statements(db, lambda: judge_it(db, tid, args))
    wall_link(db, tenant, "Korely Cloud", "Cloud di Korely", source=a)
    judge_it(db, tid, args)
    with_links, after = statements(db, lambda: judge_it(db, tid, args))
    assert without == with_links == []
    assert [s for s, _p in after] == [s for s, _p in before], "the same statements, links or not"
    assert db.query(AgentFactClosureLink).count() == 0
    assert db.query(AgentObjectLink).count() == 1


def test_memory_cores_candidates_without_a_group_are_the_old_statements(db, tenant):
    _old, new, _a = world(db, tenant)
    scope = memory_scope(tenant.id, None, None)

    def ask(**kw):
        return [c.id for c in C.for_the_judge(
            db, TABLES, scope, exclude_id=new.id, subject_label=new.subject_label,
            predicate_canonical=new.predicate_canonical, subject_canonical=new.subject_canonical,
            object_canonical=new.object_canonical, vector=new.embedding, agent_scoped=True, **kw)]

    ask()    # warm
    plain, s0 = statements(db, lambda: ask())
    none, s1 = statements(db, lambda: ask(subject_names=None))
    alone, s2 = statements(db, lambda: ask(subject_names=["cloud di korely", "Cloud di Korely "]))
    assert plain == none == alone
    assert s0 == s1 == s2, "no group, or a group of its own name: byte for byte"
