"""Single-valued relations: what the judge is told, and the line it must not cross.

Until 2026-09-27 a relation that holds one value at a time closed the old value
by rule, without the judge. Removed, with the papers that say why:
  * Lin, Mausam, Etzioni (EMNLP 2010, §2): functionality is TYPED by the value.
    "published in" holds one value when it is a year and many when it is a
    language; the vocabulary learns the cardinality once, from the first value.
  * Ritter, Downey, Soderland, Etzioni (EMNLP 2008): on functional relations
    with different values only 1.2% of the pairs were genuine contradictions.
The hosted MCP acceptance run showed it on this engine's code: "subscribes_to Slack"
closed by "subscribes_to Starter plan". Now the judge is told the relation is
single-valued and on which value that was measured, and it decides.

The line is still which candidates may be closed at all. Stage 1 gathers them
with deliberately loose recall, and before these tests a rule read "both
predicates hold one value at a time and the values differ", which is true of
every pair of single-valued facts in the store: on a real corpus `Luca lives_in
Bologna` was closed by `u-ferrari signed_up_for Business plan`. Those tests stay:
with no judge nothing may close, whatever the vocabulary says.
"""
import uuid

import pytest

from korely_agent.db.models import AgentFact, PredicateCluster
from korely_agent.services.facts import contradiction



def _everything(db_, tenant_id, end_user_id, subject, predicate, *,
                exclude_id=None, new_embedding=None, **kw):
    """Stage 1, at its widest.

    Three of these tests passed against the broken code for the worst possible
    reason: `_stage1_candidates` filters by predicate, so the candidate never
    reached the branch under test and the assertion held vacuously. On the real
    corpus it reached it, through the family and semantic paths, which is why
    the defect was in production and green in the suite.
    """
    return (db_.query(AgentFact)
            .filter(AgentFact.tenant_id == tenant_id,
                    AgentFact.end_user_id == end_user_id,
                    AgentFact.invalid_at.is_(None),
                    AgentFact.id != exclude_id)
            .all())


@pytest.fixture(autouse=True)
def wide_candidates(monkeypatch):
    monkeypatch.setattr(contradiction, "_stage1_candidates", _everything)


@pytest.fixture(autouse=True)
def no_judge(monkeypatch, request):
    """Stage 2 off, except where what stage 2 does IS the point.

    Without it a passing test could be the judge declining rather than the rule.
    """
    if "TheJudgeGetsRoom" in request.node.nodeid:
        return
    monkeypatch.setattr(contradiction, "_classify", lambda *a, **k: ([], {}))


@pytest.fixture
def functional(db, tenant):
    """Mark predicates single-valued, the way the classifier would."""
    def _mark(*names, value=True):
        for n in names:
            db.add(PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                                    canonical=n, is_functional=value,
                                    member_count=1))
        db.commit()
    return _mark


def put(db, tenant_id, subject, predicate, obj, eu="u1"):
    f = AgentFact(
        id=uuid.uuid4(), tenant_id=tenant_id, end_user_id=eu,
        subject_label=subject, predicate_raw=predicate,
        predicate_canonical=predicate, object_label=obj, confidence=0.9,
    )
    db.add(f)
    db.commit()
    return f


def arrive(db, tenant_id, subject, predicate, obj, eu="u1"):
    """Write a new fact and run detection for it. Returns the ids it closed."""
    f = put(db, tenant_id, subject, predicate, obj, eu)
    return contradiction.detect_and_invalidate(
        db, tenant_id, end_user_id=eu, new_fact_id=f.id, new_subject=subject,
        new_predicate_raw=predicate, new_predicate_canonical=predicate,
        new_object=obj,
    )


class TestWhereThereIsNothingToWeigh:
    def test_same_subject_same_relation_new_value_goes_to_the_judge(self, db, tenant,
                                                                    functional, monkeypatch):
        """The plan change reaches the judge with the single-value tag, and it
        is the judge that closes. With no judge (autouse `no_judge` elsewhere)
        nothing would."""
        functional("is_on")
        old = put(db, tenant.id, "Luca", "is_on", "Business plan")
        seen = {}

        def judge(candidates, *a, **k):
            seen["tags"] = [contradiction._single_value(db, tenant.id, c.predicate_canonical,
                                                           c.project_id, c.end_user_id) for c in candidates]
            return [c.id for c in candidates], {}

        monkeypatch.setattr(contradiction, "_classify", judge)
        closed = arrive(db, tenant.id, "Luca", "is_on", "Starter plan")
        assert closed == [old.id]
        assert seen["tags"] == [(True, None)]

    def test_without_a_judge_a_single_valued_change_keeps_both(self, db, tenant, functional):
        functional("is_on")
        put(db, tenant.id, "Luca", "is_on", "Business plan")
        assert arrive(db, tenant.id, "Luca", "is_on", "Starter plan") == []

    def test_restating_the_same_value_changes_nothing(self, db, tenant, functional):
        """2026-09-27: the same value in another case is the SAME true fact.

        The unique index is on the canonical names, case-insensitive, so a
        second row cannot even be inserted; through the write path the restated
        value is a reconfirmation (counted and dated), never a change."""
        from sqlalchemy.exc import IntegrityError

        from korely_agent.services.facts import service

        functional("is_on")
        put(db, tenant.id, "Luca", "is_on", "Business plan")
        with pytest.raises(IntegrityError):
            put(db, tenant.id, "Luca", "is_on", "business PLAN")
        db.rollback()
        out = service.write_fact_direct(
            db, tenant.id, subject="Luca", predicate="is_on", obj="business PLAN",
            end_user_id="u1",
        )
        assert out["invalidated"] == []
        assert out["observation_count"] == 2

    def test_a_multi_valued_relation_is_left_to_the_judge(self, db, tenant,
                                                          functional):
        functional("likes", value=False)
        put(db, tenant.id, "Luca", "likes", "fish")
        assert arrive(db, tenant.id, "Luca", "likes", "meat") == []


class TestTheLineItMustNotCross:
    def test_a_different_relation_is_not_superseded(self, db, tenant, functional):
        """Both single-valued and the values differ, which is true of every pair
        of single-valued facts in the store. This is the bug."""
        functional("lives_in", "signed_up_for")
        bologna = put(db, tenant.id, "Luca", "lives_in", "Bologna")
        closed = arrive(db, tenant.id, "Luca", "signed_up_for", "Business plan")
        assert bologna.id not in closed, (
            "a plan change closed where the customer lives"
        )

    def test_a_different_subject_is_not_superseded_by_rule(self, db, tenant,
                                                           functional):
        """It may well be the same person under another name, and deciding that
        is exactly the judge's job. By rule it is a guess that deletes."""
        functional("lives_in")
        luca = put(db, tenant.id, "Luca", "lives_in", "Bologna")
        closed = arrive(db, tenant.id, "u-ferrari", "lives_in", "Milan")
        assert luca.id not in closed

    def test_a_chain_of_unrelated_facts_leaves_everything_standing(self, db,
                                                                   tenant,
                                                                   functional):
        """The end-to-end shape of the defect: each fact closed by the next.

        Six unrelated single-valued facts arriving in order. Every one of them
        must still be open, because not one of them contradicts another.
        """
        functional("lives_in", "signed_up_for", "works_in", "born_in")
        arrive(db, tenant.id, "Luca", "signed_up_for", "Starter plan")
        arrive(db, tenant.id, "Luca", "lives_in", "Bologna")
        arrive(db, tenant.id, "Luca", "works_in", "Italian")
        arrive(db, tenant.id, "Luca", "born_in", "1986")

        rows = db.query(AgentFact).filter(
            AgentFact.tenant_id == tenant.id).all()
        closed = [(f.subject_label, f.predicate_canonical, f.object_label)
                  for f in rows if f.invalid_at is not None]
        assert closed == [], f"closed facts that contradict nothing: {closed}"

    def test_and_a_real_change_inside_that_chain_reaches_the_judge(self, db, tenant,
                                                                  functional, monkeypatch):
        """The control: the genuine move is put in front of the judge. The
        stand-in judge closes only a candidate with the same relation, which is
        what a correct judge does for a move; everything else stands."""
        functional("lives_in", "signed_up_for")
        arrive(db, tenant.id, "Luca", "signed_up_for", "Starter plan")
        bologna = put(db, tenant.id, "Luca", "lives_in", "Bologna")
        arrive(db, tenant.id, "Luca", "works_in", "Italian")

        def judge(candidates, new_subject, new_predicate_raw, new_object, *a, **k):
            return [c.id for c in candidates if c.predicate_canonical == new_predicate_raw], {}

        monkeypatch.setattr(contradiction, "_classify", judge)
        closed = arrive(db, tenant.id, "Luca", "lives_in", "Milan")
        assert closed == [bologna.id], "the genuine move was not put in front of the judge"

        still_open = {f.object_label for f in db.query(AgentFact).filter(
            AgentFact.tenant_id == tenant.id, AgentFact.invalid_at.is_(None)).all()}
        assert "Starter plan" in still_open and "Italian" in still_open


class TestTheJudgeGetsRoomToAnswer:
    """The judge was never refusing. It was being cut off.

    The judge's token ceiling (now memory_core `judge.MAX_TOKENS`) was 256,
    and thinking tokens are spent from that budget before
    a single character of the answer is emitted. Every call came back as the
    string "Here is the JSON requested:" with nothing after it, and every parse
    failure returned the same value as "nothing contradicts". So the judge
    answered "no contradiction" to everything, for as long as the file existed,
    while looking like a cautious model.

    Measured on six pairs that must supersede, three runs, with a real provider:

        before   3 / 18      (only the pairs the deterministic shortcut caught)
        after   17 / 18

    and on seven pairs that must NOT supersede, 7 / 7 both before and after, so
    the budget did not buy recall by giving up precision.

    The comment in this module that says "asking anyway does not work" was a
    misdiagnosis of this. It stays, because the deterministic shortcut is still
    right for the case it covers and cheaper than asking.
    """

    def test_the_budget_leaves_room_for_an_answer(self):
        from korely_agent.services.memory_core import judge

        assert judge.MAX_TOKENS >= 4096, (
            f"MAX_TOKENS is {judge.MAX_TOKENS}: a reasoning model spends this "
            f"budget thinking before it emits anything, and the answer never "
            f"arrives. Measured: 2048 gives the answer, 256 gives an empty "
            f"string, 64 gives 'Here is'."
        )

    def test_an_unreadable_answer_is_not_a_verdict(self, db, tenant,
                                                   monkeypatch, caplog):
        """An unreadable answer is not a verdict, and is no longer read as one.

        Until 2026-09-28 it returned what "nothing contradicts" returns and
        kept both facts, which looked like the recoverable direction and was
        not: the write it belonged to was filed as done, and the next write of
        the same fact only reconfirmed it, so the judge was never asked again.
        Now it raises: the write rolls back and is retried,
        and the log says why.
        """
        import logging

        from korely_agent.core.llm import LLMResult

        class Truncated:
            def complete(self, messages, **kw):
                return LLMResult(text="Here is the JSON requested:",
                                 completion_tokens=256)

        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Truncated())
        put(db, tenant.id, "Maria", "lives_in", "Rome")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(contradiction.JudgeUnreadable):
                arrive(db, tenant.id, "Maria", "is_based_in", "Milan")

        assert any("judge" in r.message.lower() for r in caplog.records), (
            "the judge's answer could not be read and nothing said so"
        )

    def test_a_call_that_fails_is_not_a_verdict(self, db, tenant, monkeypatch):
        class Down:
            def complete(self, messages, **kw):
                raise RuntimeError("429 RESOURCE_EXHAUSTED")

        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Down())
        put(db, tenant.id, "Maria", "lives_in", "Rome")
        with pytest.raises(contradiction.JudgeUnavailable):
            arrive(db, tenant.id, "Maria", "is_based_in", "Milan")

    def test_nothing_contradicts_is_still_an_answer(self, db, tenant, monkeypatch):
        from korely_agent.core.llm import LLMResult

        class Calm:
            def complete(self, messages, **kw):
                return LLMResult(text='{"contradictions": []}')

        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: Calm())
        put(db, tenant.id, "Maria", "lives_in", "Rome")
        assert arrive(db, tenant.id, "Maria", "is_based_in", "Milan") == []
