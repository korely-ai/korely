"""The judge's candidates are the same every time (memory_core 5.2.7).

A code review found that the candidate stages cut with LIMIT ordering
on created_at alone, and the facts of one write share their created_at (the
transaction's now()). Past the limit, which tied facts the judge saw was the
order Postgres happened to return: on the kept LoCoMo database the cut at 10
fell inside a tie for 13 of the 40 subjects with more than 10 open facts.
"""
import uuid


from korely_agent.db.models import AgentFact
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import candidates


def test_a_cut_inside_a_tie_keeps_the_highest_ids(db, tenant):
    rows = [AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1", subject_label="Maria",
                       subject_canonical="Maria", predicate_raw="likes", predicate_canonical="likes",
                       object_label=f"thing {i}", object_canonical=f"thing {i}")
             for i in range(14)]
    db.add_all(rows)
    db.commit()   # one transaction: one created_at for all fourteen
    assert len({r.created_at for r in db.query(AgentFact).all()}) == 1

    def read():
        return [c.id for c in candidates.for_the_judge(
            db, entities.TABLES, entities.memory_scope(tenant.id, None, "u1"),
            exclude_id=uuid.uuid4(), subject_label="Maria", predicate_canonical="likes",
            is_open=true_at(), limit=10)]

    expected = sorted((r.id for r in rows), reverse=True)[:10]
    assert sorted(read(), reverse=True) == expected
    # Rows rewritten move in the heap; the cut does not.
    for r in rows[:7]:
        r.object_label = r.object_label + " "
    db.commit()
    assert sorted(read(), reverse=True) == expected
