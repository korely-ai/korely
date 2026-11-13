"""memory_core.judge: the contradiction judge of the two Agents engines.

Piece 3 of the plan for one shared engine (2026-09-28). A judge
that does not answer is not a verdict; the batch uses the same token cap as the
single call (before: 256 + 96 per group, that is 352 for one group only).

No model call: `call` is fake.
"""
# Port notes:
#   * TestClose::test_closes_at_the_date_of_the_new_fact_and_skips_the_already_closed is
#     already here as tests/test_common_engine_judge.py::TestTheClosing::
#     test_the_open_facts_close_at_the_date_under_the_fact (open facts closed
#     at the new fact's date and by it, a closed one skipped): not duplicated.
#   * test_the_cloud_adapter_translates_failures_into_its_classes checked
#     another adapter's classes; korely-agent's adapter translates the same two
#     memory_core exceptions into JudgeUnavailable / JudgeUnreadable, already
#     tested by tests/test_common_engine_judge.py::test_every_answer_gives_its_verdict
#     (a failed call, prose): not duplicated.
#   * The DB tests run on korely-agent's tables (tenant_id). `invalidated_by_id`
#     is a foreign key here, so the new facts a closing names are real rows.
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from korely_agent.db.models import AgentFact
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import judge

T = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _c(s="Maria", p="works_at", o="Acme"):
    return NS(id=uuid.uuid4(), subject_label=s, subject_canonical=s, predicate_raw=p,
              object_label=o, valid_from=T, predicate_canonical=p, project_id=None)


def _vs(c):
    return (c.predicate_raw == "works_at", "Acme")


class Model:
    def __init__(self, answer):
        self.answer, self.prompt, self.max_tokens = answer, [], []

    def __call__(self, prompt, max_tokens):
        self.prompt.append(prompt)
        self.max_tokens.append(max_tokens)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class TestJudge:
    def test_without_candidates_it_does_not_call(self):
        m = Model("{}")
        assert judge.judge(m, [], "Maria", "works at", "Globex", single_value=_vs) == ([], {})
        assert m.prompt == []

    def test_reads_only_the_ids_shown_once_with_the_reason(self):
        a, b = _c(), _c(p="likes", o="fish")
        m = Model(json.dumps({"contradictions": [
            {"existing_id": str(a.id), "reason": "job"}, {"existing_id": str(a.id)},
            {"existing_id": str(uuid.uuid4())}, {"existing_id": "x"}, "y"]}))
        ids, reasons = judge.judge(m, [a, b], "Maria", "works at", "Globex", single_value=_vs,
                                      functional=True, example="Acme")
        assert ids == [a.id] and reasons == {str(a.id): "job"}
        assert m.max_tokens == [judge.MAX_TOKENS]
        assert "[single-valued" in m.prompt[0] and "2025-06-01" in m.prompt[0]

    @pytest.mark.parametrize("answer, error", [
        (RuntimeError("quota"), judge.JudgementMissing),
        ("", judge.JudgementUnreadable),
        ("Here is the JSON requested:", judge.JudgementUnreadable),
        (json.dumps([{"existing_id": "x"}]), judge.JudgementUnreadable),
        (json.dumps({"results": []}), judge.JudgementUnreadable),
    ])
    def test_an_unreadable_answer_is_not_no_contradiction(self, answer, error):
        with pytest.raises(error):
            judge.judge(Model(answer), [_c()], "Maria", "works at", "Globex", single_value=_vs)

    def test_json_inside_prose_with_the_engines_extractor(self):
        a = _c()
        text = "```json\n" + json.dumps({"contradictions": [{"existing_id": str(a.id)}]}) + "\n```"
        extract = lambda raw: raw.split("```json\n")[1].split("\n```")[0]
        ids, _ = judge.judge(Model(text), [a], "M", "w", "G", single_value=_vs, extract_json=extract)
        assert ids == [a.id]


class TestJudgeBatch:
    def _groups(self):
        a, b = _c(), _c(s="Globex", p="uses", o="Salesforce")
        return a, b, [{"new_id": "n1", "new_text": "Maria works at Globex", "candidates": [a]},
                      {"new_id": "n2", "new_text": "Globex uses HubSpot", "candidates": [b]}]

    def test_the_cap_is_the_single_ones(self):
        a, b, groups = self._groups()
        m = Model(json.dumps({"results": [{"new_id": "n1", "contradictions": [{"existing_id": str(a.id)}]},
                                            {"new_id": "n2", "contradictions": []}]}))
        assert judge.judge_batch(m, groups, single_value=_vs) == {"n1": [a.id]}
        assert m.max_tokens == [judge.MAX_TOKENS]

    def test_the_ids_stay_in_their_group(self):
        a, b, groups = self._groups()
        m = Model(json.dumps({"results": [{"new_id": "n1", "contradictions": [{"existing_id": str(b.id)}]},
                                            {"new_id": "n2", "contradictions": [{"existing_id": str(b.id)}]}]}))
        assert judge.judge_batch(m, groups, single_value=_vs) == {"n2": [b.id]}

    def test_a_fact_left_out_is_not_compatible(self):
        _, _, groups = self._groups()
        m = Model(json.dumps({"results": [{"new_id": "n1", "contradictions": []}]}))
        with pytest.raises(judge.JudgementUnreadable):
            judge.judge_batch(m, groups, single_value=_vs)


# Since when the earlier facts hold: before every date these tests close them
# at. Until 2.12.0 they began "now" (the column's default) and were closed in
# 2025, the state `judge.close` no longer writes (code audit of 6 October).
BEFORE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _fact_row(db, tenant, o, *, closed=False, since=BEFORE):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1", subject_label="Maria",
                  subject_canonical="Maria", predicate_raw="works_at", predicate_canonical="works_at",
                  object_label=o, object_canonical=o, valid_from=since, invalid_at=T if closed else None)
    db.add(f)
    db.commit()
    return f


class TestBatch:
    def test_never_against_the_companions_of_the_write_and_no_call_without_candidates(self, db, tenant):
        old = _fact_row(db, tenant, "Acme")
        # The two new facts of the write, as rows (a closing names its fact).
        n1, n2 = _fact_row(db, tenant, "Globex", since=T).id, _fact_row(db, tenant, "Initech", since=T).id
        new_facts = [dict(id=n1, subject="Maria", predicate_raw="works at", predicate_canonical="works_at",
                      object="Globex", valid_from=None),
                 dict(id=n2, subject="Maria", predicate_raw="works at", predicate_canonical="works_at",
                      object="Initech", valid_from=None)]
        companion = NS(id=n2)
        seen = []

        def classify(groups):
            seen.append(groups)
            return {str(n1): [old.id]}

        closed_map = judge.batch(
            db, AgentFact, new_facts,
            gather=lambda nf: [old, companion] if nf["id"] == n1 else [NS(id=n1)],
            single_value_of_new=lambda nf: (True, "Acme"),
            classify=classify, is_open=true_at(),
        )
        assert closed_map == {n1: [old.id]}
        assert [g["new_id"] for g in seen[0]] == [str(n1)]
        assert [c.id for c in seen[0][0]["candidates"]] == [old.id]

        assert judge.batch(db, AgentFact, new_facts, gather=lambda nf: [],
                             single_value_of_new=lambda nf: (False, None),
                             classify=lambda g: pytest.fail("called without candidates")) == {}


def test_two_new_facts_closing_the_same_old_one_the_first_change_closes_it(db, tenant):
    """Before: with autoflush off the second `close` found the fact still open
    in the database and closed it again, and the last in the order of the
    model's answer won."""
    old = _fact_row(db, tenant, "Acme")
    when_early = datetime(2025, 3, 1, tzinfo=timezone.utc)
    when_late = datetime(2025, 9, 1, tzinfo=timezone.utc)
    # The two new facts, as rows (a closing names its fact).
    early = _fact_row(db, tenant, "Globex", since=when_early).id
    late = _fact_row(db, tenant, "Initech", since=when_late).id
    new_facts = [dict(id=late, subject="Maria", predicate_raw="works at", predicate_canonical="works_at",
                  object="Initech", valid_from=when_late),
             dict(id=early, subject="Maria", predicate_raw="works at", predicate_canonical="works_at",
                  object="Globex", valid_from=when_early)]
    closed_map = judge.batch(
        db, AgentFact, new_facts,
        gather=lambda nf: [old], single_value_of_new=lambda nf: (True, None),
        # the model names the later change first
        classify=lambda groups: {str(late): [old.id], str(early): [old.id]},
        is_open=true_at(),
    )
    assert closed_map == {early: [old.id]}
    assert old.invalidated_by_id == early
    assert old.invalid_at.replace(tzinfo=timezone.utc) == when_early
