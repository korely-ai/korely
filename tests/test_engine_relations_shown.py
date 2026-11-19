"""memory_core 3.3.0: the log of the relations a judge call shows.

The vocabulary asks its single-value votes for every new relation, and only a
judge ever reads the answer (7 Oct: "the exact share is not
readable today because the judge does not log which relations it showed").
The line names the relations and stars the tagged ones; never an object or a
subject (3.0.0). The tag of each candidate is read once per call:
the prompt's rows and the log share it.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from korely_agent.services.memory_core import judge as judge_mod


def _fact(predicate, obj, subject="Acme"):
    return SimpleNamespace(
        id=uuid.uuid4(), subject_label=subject, subject_canonical=subject.lower(),
        predicate_raw=predicate, predicate_canonical=predicate, object_label=obj,
        valid_from=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )


def _counter(singles):
    read_ids = []

    def single_value(c):
        read_ids.append(c.id)
        return (c.predicate_canonical in singles, "Business plan" if c.predicate_canonical in singles else None)

    return single_value, read_ids


class TestTheJudgeOfAFact:
    def test_names_the_relations_and_never_the_values(self, caplog):
        plan, taste = _fact("on_plan", "Business plan"), _fact("likes", "Secret Sauce")
        single_value, read_ids = _counter({"on_plan"})
        with caplog.at_level(logging.INFO, logger=judge_mod.__name__):
            judge_mod.judge(lambda prompt, n: json.dumps({"contradictions": []}), [plan, taste],
                         "Acme", "subscribes_to", "Starter plan", single_value=single_value, functional=True)
        line = [r.getMessage() for r in caplog.records if "shows" in r.getMessage()]
        assert line == ["[memory/F3] the contradiction judge shows 3 relations: likes, on_plan*, subscribes_to*"]
        for word in ("Acme", "Secret Sauce", "Business plan", "Starter plan"):
            assert word not in line[0]
        assert sorted(read_ids) == sorted([plan.id, taste.id])  # once each

    def test_without_candidates_no_log(self, caplog):
        with caplog.at_level(logging.INFO, logger=judge_mod.__name__):
            assert judge_mod.judge(lambda p, n: "{}", [], "Acme", "on_plan", "x", single_value=lambda c: (False, None)) == ([], {})
        assert not [r for r in caplog.records if "shows" in r.getMessage()]


class TestTheBatchJudge:
    def test_a_candidate_in_two_groups_is_read_once(self, caplog):
        plan = _fact("on_plan", "Business plan")
        single_value, read_ids = _counter({"on_plan"})
        groups = [
            {"new_id": "n1", "new_text": "Acme on_plan Starter plan", "candidates": [plan],
             "new_relation": ("on_plan", True)},
            {"new_id": "n2", "new_text": "Acme likes jazz", "candidates": [plan], "new_relation": ("likes", False)},
        ]
        answer = json.dumps({"results": [{"new_id": "n1", "contradictions": []},
                                           {"new_id": "n2", "contradictions": []}]})
        with caplog.at_level(logging.INFO, logger=judge_mod.__name__):
            judge_mod.judge_batch(lambda p, n: answer, groups, single_value=single_value)
        assert read_ids == [plan.id]
        line = [r.getMessage() for r in caplog.records if "shows" in r.getMessage()]
        assert line == ["[memory/F3] the batch contradiction judge shows 2 relations: likes, on_plan*"]

    def test_a_group_without_the_relation_is_judged_all_the_same(self):
        plan = _fact("on_plan", "Business plan")
        answer = json.dumps({"results": [{"new_id": "n1", "contradictions": [{"existing_id": str(plan.id)}]}]})
        out = judge_mod.judge_batch(lambda p, n: answer,
                                 [{"new_id": "n1", "new_text": "Acme on_plan Starter plan", "candidates": [plan]}],
                                 single_value=lambda c: (True, "Business plan"))
        assert out == {"n1": [plan.id]}
