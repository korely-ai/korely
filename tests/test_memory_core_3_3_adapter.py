"""This engine's lines for memory_core 3.3.0 (2026-10-07, branch, unmeasured).

Three things the common engine now takes, each passed here as the hosted
product passes it:
  * the day the code dated in a fact's words (`day_of_the_words`): a fact
    that happened starts on it, a state keeps the memory's time;
  * the fixed prefix of the extraction prompts (`_FIXED_PREFIX`, off);
  * the vocabulary's lazy single-value votes (`LAZY_SINGLE_VALUE`, off),
    asked by the judges through `ensure`.
Off, the prompts and the judgements are the ones of before.
"""
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import predicates
from korely_agent.services.facts import contradiction, extractor, service
from korely_agent.services.memory_core import fact_extractor
from korely_agent.services.memory_core.fact_extractor import ExtractedFact


SAID = datetime(2023, 5, 8, 13, 56, tzinfo=timezone.utc)
YESTERDAY = datetime(2023, 5, 7, tzinfo=timezone.utc)


@pytest.fixture
def no_judge(monkeypatch):
    monkeypatch.setattr(contradiction, "_classify", lambda c, *a, **k: ([], {}))
    monkeypatch.setattr(contradiction, "_classify_batch",
                        lambda groups, *a, **k: {g["new_id"]: [] for g in groups})


def write(db, tenant, facts):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="caroline",
                    content="Caroline: I went to a LGBTQ support group yesterday.", event_time=SAID)
    db.add(m)
    db.commit()
    service._persist_extracted(db, tenant.id, memory=m, extracted=facts, valid_from=SAID)
    db.commit()
    return {f.predicate_raw: f for f in db.query(AgentFact).filter(AgentFact.source_memory_id == m.id)}


class TestTheDayInTheWords:
    """`_EVENT_DAY` (on since 0.1.23): a current fact of the events family starts on
    the day dated in its words; a state keeps the memory's time."""

    @pytest.fixture
    def events(self, monkeypatch):
        """The vocabulary as a model would answer it: "attends" is an event."""
        from dataclasses import replace

        real = predicates.resolve_relation

        def resolve(db, tenant_id, raw, **k):
            r = real(db, tenant_id, raw, **k)
            return replace(r, family="events") if raw == "attends" else r

        monkeypatch.setattr(predicates, "resolve_relation", resolve)

    def facts(self):
        words = "I went to a LGBTQ support group yesterday"
        return [ExtractedFact("Caroline", "attends", "LGBTQ support group", 0.9, subject_type="person",
                              evidence=words, day_of_the_words=YESTERDAY),
                ExtractedFact("Caroline", "supports", "LGBTQ community", 0.9, subject_type="person",
                              evidence=words, day_of_the_words=YESTERDAY)]

    def test_off_every_fact_starts_as_before(self, db, tenant, no_judge, events, monkeypatch):
        assert service._EVENT_DAY is True, "on since 0.1.23"
        monkeypatch.setattr(service, "_EVENT_DAY", False)
        rows = write(db, tenant, self.facts())
        assert rows["attends"].valid_from == SAID and rows["supports"].valid_from == SAID

    def test_on_an_event_starts_on_its_day_a_state_does_not(self, db, tenant, no_judge, events, monkeypatch):
        monkeypatch.setattr(service, "_EVENT_DAY", True)
        rows = write(db, tenant, self.facts())
        assert rows["attends"].valid_from == YESTERDAY
        assert rows["supports"].valid_from == SAID


class TestACallersPredicateKeepsItsName:
    """BUG-202 (memory_core 3.3.0): a fact written directly or corrected keeps
    the predicate its caller chose; only the extraction asks "same relation?"."""

    def test_direct_and_correction_do_not_merge(self, db, tenant, monkeypatch):
        seen = []
        real = predicates._store.resolve_predicate
        monkeypatch.setattr(predicates._store, "resolve_predicate",
                            lambda *a, **k: seen.append(k.get("merge")) or real(*a, **k))
        from korely_agent.services.facts.service import write_fact_direct
        from tests.conftest import seeded_project_id

        write_fact_direct(db, tenant.id, subject="Acme", predicate="subscribes_to", obj="Slack",
                          end_user_id="acme", project_id=seeded_project_id(db, tenant.id))
        assert seen and seen[-1] is False


class TestTheFixedPrefix:
    def test_off_and_passed_to_the_extraction(self, db, tenant, monkeypatch):
        assert extractor._FIXED_PREFIX is True, "on since 0.1.23"
        monkeypatch.setattr(extractor, "_FIXED_PREFIX", False)
        seen = []
        monkeypatch.setattr(extractor, "_call_or_raise", lambda m: (lambda *a, **k: ""))
        for rule in ("_states", "_faithful", "_attributes"):
            monkeypatch.setattr(extractor, rule, lambda m: False)
        monkeypatch.setattr(fact_extractor, "extract_with_observation",
                            lambda *a, **k: seen.append(k["fixed_prefix"]) or fact_extractor.Extraction([], None))
        who = dict(end_user_id="maria", db=db, tenant_id=tenant.id)
        extractor.extract_facts_and_observation("Maria moved to Rome.", **who)
        monkeypatch.setattr(extractor, "_FIXED_PREFIX", True)
        extractor.extract_facts_and_observation("Maria moved to Rome.", **who)
        assert seen == [False, True]


class TestTheLazyVotes:
    def test_off_the_judges_get_nothing_to_ask(self, db, tenant, monkeypatch):
        assert predicates.LAZY_SINGLE_VALUE is True, "on since 0.1.23"
        monkeypatch.setattr(predicates, "LAZY_SINGLE_VALUE", False)
        assert contradiction._ensure(db, tenant.id, None, None) is None

    def test_on_the_judges_ask_the_vocabulary(self, db, tenant, monkeypatch):
        asked = []
        monkeypatch.setattr(predicates, "LAZY_SINGLE_VALUE", True)
        monkeypatch.setattr(predicates._store, "decide_if_missing",
                            lambda db_, tables, mem, canonical, ask, **k: asked.append((canonical, k)) or (True, "Pro"))
        ensure = contradiction._ensure(db, tenant.id, None, None)
        assert ensure("on_plan") == (True, "Pro")
        assert asked == [("on_plan", {"severe": True, "votes": predicates._vocabulary.SINGLE_VALUE_VOTES,
                                      "examples": ()})]

    def test_the_values_in_conflict_reach_the_vote(self, db, tenant, monkeypatch):
        """8 Oct: the judges pass the two objects that meet, and the vote is
        typed by them instead of the birth example ("reads", born with
        "reading", voted one value and closed every book with the next)."""
        asked = []
        monkeypatch.setattr(predicates, "LAZY_SINGLE_VALUE", True)
        monkeypatch.setattr(predicates._store, "decide_if_missing",
                            lambda db_, tables, mem, canonical, ask, **k: asked.append(k["examples"]) or (False, None))
        contradiction._ensure(db, tenant.id, None, None)("reads", ["Dune", "a series about friendship"])
        assert asked == [("Dune", "a series about friendship")]


class TestNoWordLists:
    """No word lists in the code (the rule of 8 Oct 2026): memory_core
    3.3.0 has one path, the learned vocabulary, and no switch to bring the
    hand-written English lists back."""

    def test_there_is_no_switch(self):
        assert not hasattr(predicates, "_NO_WORD_LISTS")

    def test_the_engine_has_no_list_left(self):
        from korely_agent.services.memory_core import families, predicate_forms

        assert not hasattr(predicate_forms, "_SYNONYM_MAP") and not hasattr(predicate_forms, "_AUX_PREFIXES")
        assert not hasattr(families, "OPPOSING_PREDICATE_FAMILIES")
        assert set(families.PREDICATE_TO_FAMILY.values()) <= {"identity"}

