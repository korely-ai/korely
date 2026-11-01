"""memory_core 3.3.0: a current event begins the day it happened.

Proposal (c) of the 2026-10-06 quality reading, with the limits of 7 Oct
and the independent review of that evening: the extractor marks the
single day a CURRENT fact's own words date (one relative time the code dated,
not after the memory's day); the writer begins the fact that day only for a
current fact of the events family, and only when the engine passes the day
(Cloud AGENTS_EVENT_DAY, off). A past fact is a state that ended: the time in
its words dates the end ("I worked at Acme until I quit yesterday"), so it is
never moved. The day is computed by the code, never written by the model.

No model call: the relative times are dated by the real `annotate`.
"""
# Notes:
#   * TestTheWriting runs `write_fact` on korely-agent's tables
#     (`facts.entities.TABLES`, tenant_id).
#   * The two cases of the switch (off, no day; on, the event begins that
#     day) are not duplicated here: on korely-agent's switch
#     (`facts.service._EVENT_DAY`, on here since 0.1.23, which that test
#     asserts) they are tests/test_memory_core_3_3_adapter.py::TestTheDayInTheWords
#     (test_off_every_fact_starts_as_before,
#     test_on_an_event_starts_on_its_day_a_state_does_not).
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import relative_time as tr
from korely_agent.services.memory_core.store import Relation
from korely_agent.services.memory_core.fact_extractor import ExtractedFact, the_day_it_happened
from korely_agent.services.memory_core.presentation import context_line
from korely_agent.services.memory_core.writing import write_fact

DAY = "2023-05-08"  # the memory's day


def _d(*a):
    return datetime(*a, tzinfo=timezone.utc)


def _aware(v):
    return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc))


def _times(text, *objects):
    column, discarded = tr.annotate(list(objects), DAY, text=text)
    assert discarded == []
    return column


def _fact(predicate, obj, evidence, tense="current", subject="u1"):
    return ExtractedFact(subject=subject, predicate_raw=predicate, object=obj, confidence=0.9,
                         tense=tense, evidence=evidence)


YESTERDAY = {"text": "yesterday", "direction": "last", "unit": "day"}


class TestTheDayOfTheWords:
    def test_a_current_fact_takes_the_day(self):
        text = "Yesterday I attended Anna's graduation party."
        [f] = the_day_it_happened([_fact("attended", "Anna's graduation party", text)], _times(text, YESTERDAY))
        assert f.day_of_the_words == _d(2023, 5, 7)

    def test_a_past_fact_does_not(self):
        """The review's case: the time dates the END of a state that ended."""
        text = "I worked at Acme from 2019 until I quit yesterday."
        [f] = the_day_it_happened([_fact("works_at", "Acme", text, tense="past")], _times(text, YESTERDAY))
        assert f.day_of_the_words is None

    def test_a_plan_does_not(self):
        text = "Yesterday we agreed to move to Lisbon."
        [f] = the_day_it_happened([_fact("plans_to_move_to", "Lisbon", text, tense="planned")], _times(text, YESTERDAY))
        assert f.day_of_the_words is None

    def test_in_chinese_without_spaces(self):
        text = "我昨天去了聚会"
        column = _times(text, {"text": "昨天", "direction": "last", "unit": "day"})
        [f] = the_day_it_happened([_fact("去了", "聚会", text)], column)
        assert f.day_of_the_words == _d(2023, 5, 7)

    def test_never_in_the_future(self):
        text = "Tomorrow I attend the board meeting."
        tomorrow = {"text": "Tomorrow", "direction": "next", "unit": "day"}
        [f] = the_day_it_happened([_fact("attends", "board meeting", text)], _times(text, tomorrow))
        assert f.day_of_the_words is None

    def test_two_times_in_its_words_no_day(self):
        text = "Yesterday I returned the bike I bought last Saturday."
        saturday = {"text": "last Saturday", "direction": "last", "weekday": "saturday"}
        [f] = the_day_it_happened([_fact("returned", "bike", text)], _times(text, YESTERDAY, saturday))
        assert f.day_of_the_words is None

    def test_a_week_is_not_a_day(self):
        text = "Last week I finished the course."
        week = {"text": "Last week", "direction": "last", "unit": "week"}
        [f] = the_day_it_happened([_fact("finished", "course", text)], _times(text, week))
        assert f.day_of_the_words is None

    def test_a_vague_count_is_not_a_day(self):
        text = "A few days ago I repaired the fence."
        few = {"text": "A few days ago", "direction": "before", "quantity": "few", "unit": "day"}
        [f] = the_day_it_happened([_fact("repaired", "fence", text)], _times(text, few))
        assert f.day_of_the_words is None

    def test_whole_words(self):
        """"ieri" is not inside "pensieri"."""
        text = "Ieri ho scritto i miei pensieri in un diario."
        column = _times(text, {"text": "Ieri", "direction": "last", "unit": "day"})
        [f] = the_day_it_happened([_fact("wrote", "diary", "ho scritto i miei pensieri in un diario")], column)
        assert f.day_of_the_words is None
        [g] = the_day_it_happened([_fact("wrote", "diary", text)], column)
        assert g.day_of_the_words == _d(2023, 5, 7)

    def test_without_words_or_without_times_nothing(self):
        text = "Yesterday I attended the party."
        assert the_day_it_happened([_fact("attended", "party", None)], _times(text, YESTERDAY))[0].day_of_the_words is None
        assert the_day_it_happened([_fact("attended", "party", text)], None)[0].day_of_the_words is None


WENT = Relation(canonical="attended", is_functional=False, family="events", example="party")
KNOWS = Relation(canonical="knows", is_functional=False, family="people", example="Mel")
WORKS = Relation(canonical="works_at", is_functional=True, family="work", example="Acme")
SAID = _d(2023, 5, 8, 14, 0)


def _write(db, tenant, *, day, tense="current", relation=WENT):
    m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content="x", graph_sync_status="done")
    db.add(m)
    db.commit()
    fact, _ = write_fact(
        db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None),
        columns=dict(tenant_id=tenant.id, project_id=None, end_user_id="u1", agent_id=None, run_id=None),
        relation=relation, is_open=true_at(), embed=lambda f: None, judge=lambda g: [],
        subject="u1", subject_type="person", predicate_raw=relation.canonical, obj="something",
        object_is_literal=False, confidence=0.9, source_memory_id=m.id, model_used=None,
        valid_from=SAID, tense=tense, day_of_the_words=day,
    )
    return fact


class TestTheWriting:
    def test_a_current_event_starts_that_day_and_stays_true(self, db, tenant):
        f = _write(db, tenant, day=_d(2023, 5, 7))
        assert _aware(f.valid_from) == _d(2023, 5, 7) and f.invalid_at is None
        that_day = db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id,
                                                 true_at(_d(2023, 5, 7, 12))).count()
        assert that_day == 1

    def test_a_day_after_the_said_time_is_ignored(self, db, tenant):
        assert _aware(_write(db, tenant, day=_d(2023, 5, 9)).valid_from) == SAID

    def test_a_state_true_now_does_not_use_it(self, db, tenant):
        """B's guard: from "Yesterday Mel told me she quit her job", knows Mel
        (a state, current) keeps the memory's time."""
        f = _write(db, tenant, day=_d(2023, 5, 7), relation=KNOWS)
        assert _aware(f.valid_from) == SAID

    def test_a_past_fact_does_not_use_it(self, db, tenant):
        """The review's case, at the writer too: a state that ended keeps its
        start; its end is when it was said."""
        f = _write(db, tenant, day=_d(2023, 5, 7), tense="past", relation=WORKS)
        assert _aware(f.valid_from) != _d(2023, 5, 7)


class TestInTheBlock:
    def test_an_event_reads_since_as_before(self):
        f = SimpleNamespace(subject_label="u1", subject_canonical="u1", predicate_raw="attended",
                            object_label="Anna's graduation party", object_canonical="anna's graduation party",
                            valid_from=_d(2023, 5, 7), tense="current", predicate_family="events")
        assert context_line(f, "u1") == "- The user attended Anna's graduation party (since 2023-05-07)"
