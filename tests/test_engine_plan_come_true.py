"""memory_core 3.0.0: a plan that comes true stays in the history.

The decision of 6 October (16:45). 2.11.0 promoted the plan in place with
its date (an `as_of` between plan and change returned two trackers), 2.12.0
moved the date to the moment it came true and the plan vanished from the
history. Now, like Graphiti (Rasmussen et al. 2025, arXiv:2501.13956, 2.2.3:
only t_invalid is written, "historical records of relationship evolution over
time"):
  * the plan's row closes when the fact begins, and keeps its date;
  * the fact that came true is a new row, with the judge like every new fact;
  * `as_of` before it came true returns the plan, after it the fact;
  * the plan's memory stays in the context: it is not refuted, it is history.

No model call.
"""
# Port notes: on korely-agent's tables (`facts.entities.TABLES`, tenant_id); the
# batched sources' states are read within this tenant (`AgentFact.tenant_id`).
from __future__ import annotations

from datetime import datetime, timedelta, timezone


from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core.store import Relation, states_of_the_sources, state_of_the_source
from korely_agent.services.memory_core.presentation import mcp_line
from korely_agent.services.memory_core.writing import write_fact


def _d(*a):
    return datetime(*a, tzinfo=timezone.utc)


def _aware(v):
    return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc))


USES = Relation(canonical="uses", is_functional=True, family="tools", example="Jira")


class _Writer:
    def __init__(self, db, tenant):
        self.db, self.tenant = db, tenant
        self.judged = []
        self.close = []

    def memory_of(self, text="x"):
        m = AgentMemory(tenant_id=self.tenant.id, end_user_id="u1", content=text, graph_sync_status="done")
        self.db.add(m)
        self.db.commit()
        return m

    def write(self, obj, *, tense="current", valid_from=None, source=None, defer=False):
        source = source or self.memory_of().id

        def judge(g):
            self.judged.append(g)
            return list(self.close)

        return write_fact(
            self.db, entities.TABLES, entities.memory_of(self.tenant.id, None, "u1", None),
            columns=dict(tenant_id=self.tenant.id, project_id=None, end_user_id="u1", agent_id=None, run_id=None),
            relation=USES, is_open=true_at(), embed=lambda f: None, judge=judge,
            subject="Globex", subject_type="organization", predicate_raw="uses", obj=obj,
            object_is_literal=False, confidence=0.9, source_memory_id=source, model_used=None,
            valid_from=valid_from, tense=tense, defer=defer,
        )

    def as_it_was(self, when):
        rows = self.db.query(AgentFact).filter(AgentFact.tenant_id == self.tenant.id, true_at(when)).all()
        return {(r.object_label, r.tense) for r in rows}


class TestThePlansHistory:
    def test_as_of_before_the_plan_after_the_fact(self, db, tenant):
        w = _Writer(db, tenant)
        plan, _ = w.write("Linear", tense="planned", valid_from=_d(2025, 1, 1))
        fact, closed = w.write("Linear", valid_from=_d(2025, 6, 1))
        assert closed == [plan.id]
        assert w.as_it_was(_d(2024, 12, 1)) == set()
        assert w.as_it_was(_d(2025, 3, 1)) == {("Linear", "planned")}
        assert w.as_it_was(_d(2025, 5, 31, 23, 59)) == {("Linear", "planned")}
        assert w.as_it_was(_d(2025, 6, 1)) == {("Linear", "current")}
        assert w.as_it_was(datetime.now(timezone.utc)) == {("Linear", "current")}
        # The plan keeps its date: only the end is written.
        assert _aware(plan.valid_from) == _d(2025, 1, 1) and _aware(plan.invalid_at) == _d(2025, 6, 1)
        assert plan.invalidated_by_id == fact.id and plan.tense == "planned"

    def test_the_fact_that_came_true_goes_to_the_judge_like_every_new_fact(self, db, tenant):
        w = _Writer(db, tenant)
        jira, _ = w.write("Jira", valid_from=_d(2024, 6, 1))
        plan, _ = w.write("Linear", tense="planned", valid_from=_d(2025, 1, 1))
        w.judged.clear()
        w.close = [jira.id]
        fact, closed = w.write("Linear", valid_from=_d(2025, 6, 1))
        [g] = w.judged
        assert g.fact_id == fact.id and _aware(g.when) == _d(2025, 6, 1)
        assert closed == [jira.id, plan.id]

    def test_without_a_date_the_plan_ends_where_the_fact_begins(self, db, tenant):
        w = _Writer(db, tenant)
        plan, _ = w.write("Linear", tense="planned")
        fact, _ = w.write("Linear")
        assert plan.invalid_at is not None and _aware(plan.invalid_at) == _aware(fact.valid_from)
        assert fact.invalid_at is None

    def test_a_fact_that_began_before_the_plan_leaves_it_without_days(self, db, tenant):
        """Said as a plan in June, written later as a fact from March: the plan
        does not close before it begins (the rule of judge.close), it stays a
        row true on no day."""
        w = _Writer(db, tenant)
        plan, _ = w.write("Linear", tense="planned", valid_from=_d(2025, 6, 1))
        fact, closed = w.write("Linear", valid_from=_d(2025, 3, 1))
        assert _aware(plan.invalid_at) == _aware(plan.valid_from) == _d(2025, 6, 1)
        assert closed == [plan.id] and _aware(fact.valid_from) == _d(2025, 3, 1)
        assert w.as_it_was(_d(2025, 4, 1)) == {("Linear", "current")}
        assert w.as_it_was(_d(2025, 7, 1)) == {("Linear", "current")}

    def test_a_plan_with_a_future_end_closes_earlier(self, db, tenant):
        w = _Writer(db, tenant)
        plan, _ = w.write("Linear", tense="planned", valid_from=_d(2025, 1, 1))
        plan.invalid_at = datetime.now(timezone.utc) + timedelta(days=90)
        db.commit()
        fact, closed = w.write("Linear", valid_from=_d(2025, 6, 1))
        assert _aware(plan.invalid_at) == _d(2025, 6, 1) and closed == [plan.id]

    def test_in_a_batched_write_the_batch_judges_it(self, db, tenant):
        w = _Writer(db, tenant)
        plan, _ = w.write("Linear", tense="planned", valid_from=_d(2025, 1, 1))
        w.judged.clear()
        fact, closed = w.write("Linear", valid_from=_d(2025, 6, 1), defer=True)
        assert w.judged == [] and closed == [plan.id] and fact.id != plan.id

    def test_the_plan_said_again_after_the_fact_reconfirms_the_fact(self, db, tenant):
        w = _Writer(db, tenant)
        w.write("Linear", tense="planned", valid_from=_d(2025, 1, 1))
        fact, _ = w.write("Linear", valid_from=_d(2025, 6, 1))
        again, closed = w.write("Linear", tense="planned", valid_from=_d(2025, 7, 1))
        assert again.id == fact.id and again.tense == "current" and closed == []
        assert db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).count() == 2


class TestInTheContext:
    def test_the_memory_of_the_plan_that_came_true_stays_standing(self, db, tenant):
        w = _Writer(db, tenant)
        m_plan, m_fact = w.memory_of("We will move to Linear."), w.memory_of("We moved to Linear.")
        w.write("Linear", tense="planned", valid_from=_d(2025, 1, 1), source=m_plan.id)
        w.write("Linear", valid_from=_d(2025, 6, 1), source=m_fact.id)
        for m in (m_plan, m_fact):
            assert state_of_the_source(db, entities.TABLES, m.id, is_open=true_at()) == (1, 1)
        in_block = states_of_the_sources(db, entities.TABLES, [m_plan.id, m_fact.id], is_open=true_at(),
                                      scope=AgentFact.tenant_id == tenant.id)
        assert in_block == {str(m_plan.id): (1, 1), str(m_fact.id): (1, 1)}

    def test_a_refuted_plan_stays_superseded(self, db, tenant):
        """Closed by another fact (the judge: "uses Jira" after Linear's plan),
        the plan did not come true."""
        w = _Writer(db, tenant)
        m_plan = w.memory_of("We will move to Linear.")
        plan, _ = w.write("Linear", tense="planned", valid_from=_d(2025, 1, 1), source=m_plan.id)
        w.close = [plan.id]
        jira, _ = w.write("Jira", valid_from=_d(2025, 6, 1))
        plan.invalid_at, plan.invalidated_by_id = _d(2025, 6, 1), jira.id
        db.commit()
        assert state_of_the_source(db, entities.TABLES, m_plan.id, is_open=true_at()) == (1, 0)
        assert states_of_the_sources(db, entities.TABLES, [m_plan.id], is_open=true_at()) == {
            str(m_plan.id): (1, 0)}


class TestTheLine:
    def test_the_closed_plan_reads_as_a_plan(self, db, tenant):
        w = _Writer(db, tenant)
        plan, _ = w.write("Linear", tense="planned", valid_from=_d(2025, 1, 1))
        w.write("Linear", valid_from=_d(2025, 6, 1))
        line = mcp_line(plan, now=_d(2026, 1, 1))
        assert "[planned, superseded 2025-06-01]" in line and "(since 2025-01-01" in line
