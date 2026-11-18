"""A fact's late vector brings the judgements its write could not make (2026-10-06).

A fact whose vector could not be computed on its write was judged on the SQL
candidates only: the semantic stage needs the vector. The vector drain
(services/vectors.py) retries the vector; until today it stored it and
nothing else, so a contradiction only meaning could find was never judged.
memory_core `judge.after_the_vector` (the hosted product's since
2026-09-28, bussola 2d n. 3) makes them when the vector arrives, before it
is stored, in the same transaction.

The facts here are shared (no end user): the end user's recall stage would
bring an older fact of the same person anyway, and the point is a fact only
meaning finds. Until memory_core 6.8.1 the two facts were "Acme Corp" and
"Acme", two subjects to the SQL stages; since 6.9.0 a fact of a memory with
no end user meets another by meaning only about its own subject
(`owner.keeps_to_its_subject`), so here they are one subject, "Acme", with
two relations and two objects no SQL stage pairs: still a fact only meaning
finds.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact
from korely_agent.services import vectors
from korely_agent.services.facts import contradiction
from korely_agent.services.sentinels import EVERY_PROJECT


VEC = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _fact(db, tenant, subject, predicate, obj, *, when, vector):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=None,
                  subject_label=subject, subject_canonical=subject.lower(), subject_type="organization",
                  predicate_raw=predicate, predicate_canonical=predicate,
                  object_label=obj, object_canonical=obj.lower(), confidence=0.9,
                  valid_from=when, created_at=when, tense="current",
                  embedding=vector, embedding_status=vectors.DONE if vector else vectors.PENDING)
    db.add(f)
    db.commit()
    return f


@pytest.fixture
def setting(db, tenant, monkeypatch):
    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.services.facts.service.fact_vector", lambda text: list(VEC))
    old = _fact(db, tenant, "Acme", "on_plan", "Business plan", when=T0, vector=list(VEC))
    late = _fact(db, tenant, "Acme", "subscribes_to", "Starter plan", when=T0 + timedelta(days=30),
                 vector=None)
    events = []
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event",
                        lambda db_, tenant_id, kind, payload: events.append((kind, payload)))
    return old, late, events


def _judge_closing_everything(seen):
    def judge(groups, db, tenant_id, model_name=None):
        seen.extend((g["new_id"], sorted(c.object_label for c in g["candidates"])) for g in groups)
        return {g["new_id"]: [c.id for c in g["candidates"]] for g in groups}
    return judge


def test_the_late_vector_judges_what_only_meaning_finds(db, setting, monkeypatch):
    old, late, events = setting
    seen = []
    monkeypatch.setattr(contradiction, "_classify_batch", _judge_closing_everything(seen))

    out = vectors.process_pending_vectors(db)

    assert out["processed"] == 1
    db.expire_all()
    late_now, old_now = db.get(AgentFact, late.id), db.get(AgentFact, old.id)
    assert late_now.embedding_status == vectors.DONE and late_now.embedding is not None
    assert seen == [(str(late.id), ["Business plan"])], "the judge was not asked about the semantic candidate"
    assert old_now.invalid_at is not None and old_now.invalidated_by_id == late.id
    assert old_now.invalid_at == late.valid_from, "bi-temporal: the old plan ends where the new began"
    assert [k for k, _ in events] == ["fact.invalidated"]


def test_a_judge_that_gives_no_answer_keeps_the_vector_waiting(db, setting, monkeypatch):
    old, late, events = setting

    def down(*a, **k):
        raise contradiction.JudgeUnavailable("no answer")

    monkeypatch.setattr(contradiction, "_classify_batch", down)
    out = vectors.process_pending_vectors(db)

    assert out["processed"] == 0 and out["failed"] == 1
    db.expire_all()
    late_now = db.get(AgentFact, late.id)
    assert late_now.embedding is None and late_now.embedding_status == vectors.PENDING
    assert late_now.embedding_attempts == 1 and late_now.embedding_next_at is not None
    assert db.get(AgentFact, old.id).invalid_at is None
    assert events == []


def test_an_answer_that_cannot_be_read_stores_the_vector_without_closing(db, setting, monkeypatch):
    old, late, events = setting

    def unreadable(*a, **k):
        raise contradiction.JudgeUnreadable("not json")

    monkeypatch.setattr(contradiction, "_classify_batch", unreadable)
    out = vectors.process_pending_vectors(db)

    assert out["processed"] == 1
    db.expire_all()
    assert db.get(AgentFact, late.id).embedding_status == vectors.DONE
    assert db.get(AgentFact, old.id).invalid_at is None
    assert events == []


def test_no_model_configured_is_no_judgement(db, setting, monkeypatch):
    old, late, events = setting
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: None)
    out = vectors.process_pending_vectors(db)

    assert out["processed"] == 1
    db.expire_all()
    assert db.get(AgentFact, late.id).embedding_status == vectors.DONE
    assert db.get(AgentFact, old.id).invalid_at is None


def test_a_fact_forgotten_while_the_judge_waits_closes_nothing(db, setting, monkeypatch, engine):
    """The operator forgets a planted fact while its late judgement waits on
    the model: the forget answers "forgotten", and the judgement must not
    then close the true fact with it (audit of 17 Oct 2026)."""
    from sqlalchemy.orm import Session

    from korely_agent.services.facts import service as facts_service

    old, late, events = setting
    seen = []
    judge = _judge_closing_everything(seen)

    def forgotten_meanwhile(groups, db_, tenant_id, model_name=None):
        with Session(bind=engine, expire_on_commit=False) as other:
            out = facts_service.forget_fact(other, late.tenant_id, late.id, project_id=EVERY_PROJECT)
        assert out["status"] == "forgotten"
        return judge(groups, db_, tenant_id, model_name)

    monkeypatch.setattr(contradiction, "_classify_batch", forgotten_meanwhile)
    out = vectors.process_pending_vectors(db)

    assert seen, "the judge was not asked"
    assert out["processed"] == 1
    db.expire_all()
    late_now, old_now = db.get(AgentFact, late.id), db.get(AgentFact, old.id)
    assert late_now.embedding_status == vectors.DONE, "the vector is still the fact's own"
    assert late_now.invalid_at is not None
    assert old_now.invalid_at is None and old_now.invalidated_by_id is None, (
        "the forgotten fact closed the true one")
    from korely_agent.services.ids import fact_public_id

    assert not [p for _k, p in events if fact_public_id(old.id) in str(p)], (
        "a webhook said the true fact was closed")


def test_a_fact_closed_by_its_own_judgement_still_closes(db, setting, monkeypatch):
    """The check reads what other transactions committed, not the judgement's
    own change: the ordinary late judgement is unchanged."""
    old, late, events = setting
    monkeypatch.setattr(contradiction, "_classify_batch", _judge_closing_everything([]))
    vectors.process_pending_vectors(db)
    db.expire_all()
    assert db.get(AgentFact, old.id).invalidated_by_id == late.id


def test_a_fact_erased_while_the_judge_waits_does_not_end_the_pass(db, tenant, setting, monkeypatch,
                                                                    engine):
    """The late fact is deleted for good (DELETE /v1/agents/{id}, an end
    user's erasure) while its judgement waits on the model: closing the old
    fact then points at a row that is gone, and the flush raised
    IntegrityError out of the drain, so the other vectors of that pass
    waited for the next one (audit of 17 Oct 2026). The old fact
    stays open, rightly, and the pass goes on."""
    from sqlalchemy.orm import Session

    old, late, events = setting
    other_late = _fact(db, tenant, "Initech", "on_plan", "Gold plan", when=T0 + timedelta(days=40),
                       vector=None)
    judge = _judge_closing_everything([])
    late_id, old_id, other_id = late.id, old.id, other_late.id

    def erased_meanwhile(groups, db_, tenant_id, model_name=None):
        if any(g["new_id"] == str(late_id) for g in groups):
            with Session(bind=engine) as gone:
                gone.query(AgentFact).filter(AgentFact.id == late_id).delete()
                gone.commit()
            return judge(groups, db_, tenant_id, model_name)
        return {g["new_id"]: [] for g in groups}  # the other fact closes nothing

    monkeypatch.setattr(contradiction, "_classify_batch", erased_meanwhile)
    out = vectors.process_pending_vectors(db)

    db.expire_all()
    assert db.get(AgentFact, late_id) is None
    assert db.get(AgentFact, old_id).invalid_at is None, "closed by a fact that is gone"
    assert db.get(AgentFact, other_id).embedding_status == vectors.DONE, (
        "the rest of the pass was abandoned")
    assert out["claimed"] == 2


def test_a_later_fact_forgotten_while_the_judge_waits_closes_nothing(db, tenant, monkeypatch,
                                                                     engine):
    """The other direction (audit of 18 Oct 2026): the late fact is the
    true one, and the LATER fact that contradicts it is forgotten while the
    judge is asked. The forget answered "forgotten", and the forgotten fact
    still closed the true one, with a fact.invalidated naming it."""
    from sqlalchemy.orm import Session

    from korely_agent.services.facts import service as facts_service

    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.services.facts.service.fact_vector", lambda text: list(VEC))
    late = _fact(db, tenant, "Acme", "on_plan", "Business plan", when=T0, vector=None)
    later = _fact(db, tenant, "Acme", "subscribes_to", "Starter plan", when=T0 + timedelta(days=30),
                  vector=list(VEC))
    events = []
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event",
                        lambda db_, tenant_id, kind, payload: events.append((kind, payload)))
    seen = []
    judge = _judge_closing_everything(seen)
    forget = {"on": True}

    def forgotten_meanwhile(groups, db_, tenant_id, model_name=None):
        if forget["on"]:
            with Session(bind=engine, expire_on_commit=False) as other:
                out = facts_service.forget_fact(other, later.tenant_id, later.id,
                                                project_id=EVERY_PROJECT)
            assert out["status"] == "forgotten"
        return judge(groups, db_, tenant_id, model_name)

    monkeypatch.setattr(contradiction, "_classify_batch", forgotten_meanwhile)
    out = vectors.process_pending_vectors(db)

    assert seen == [(str(later.id), ["Business plan"])], "the later fact was not asked about"
    assert out["processed"] == 1
    db.expire_all()
    late_now = db.get(AgentFact, late.id)
    assert late_now.embedding_status == vectors.DONE, "the vector is still the fact's own"
    assert late_now.invalid_at is None and late_now.invalidated_by_id is None, (
        "the forgotten fact closed the true one")
    from korely_agent.services.ids import fact_public_id

    # The forget's own event says the later fact ended; none may say the
    # late fact was closed by it.
    assert not [p for _k, p in events if p.get("invalidated_by") == fact_public_id(later.id)
                or p.get("fact_id") == fact_public_id(late.id)], events


def test_a_later_fact_that_still_holds_closes_the_late_one(db, tenant, monkeypatch):
    """The control: nobody forgets anything, and the later fact closes the
    late one where it began."""
    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.services.facts.service.fact_vector", lambda text: list(VEC))
    late = _fact(db, tenant, "Acme", "on_plan", "Business plan", when=T0, vector=None)
    later = _fact(db, tenant, "Acme", "subscribes_to", "Starter plan", when=T0 + timedelta(days=30),
                  vector=list(VEC))
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
    monkeypatch.setattr(contradiction, "_classify_batch", _judge_closing_everything([]))
    vectors.process_pending_vectors(db)
    db.expire_all()
    late_now = db.get(AgentFact, late.id)
    assert late_now.invalidated_by_id == later.id and late_now.invalid_at == later.valid_from


class TestAVoteWithNoAnswerIsOneRowsAttempt:
    """Audit of 18 Oct 2026: the late judgement's vocabulary vote
    (memory_core's lazy single-value vote) failing raised
    ResolutionUnavailable out of the drain. The pass ended for every account
    behind the row, and the row skipped the ATTEMPTS ceiling, back every six
    hours for as long as its region's writer failed."""

    @pytest.fixture
    def two_accounts(self, db, tenant, monkeypatch):
        from korely_agent.db.models import Tenant

        monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
        monkeypatch.setattr("korely_agent.services.facts.service.fact_vector",
                            lambda text: list(VEC))
        monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
        other = Tenant(id=uuid.uuid4(), name="Globex Logistics")
        db.add(other)
        db.commit()
        _fact(db, tenant, "Acme", "on_plan", "Business plan", when=T0, vector=list(VEC))
        poisons = [_fact(db, tenant, "Acme", "subscribes_to", f"Plan {i}",
                         when=T0 + timedelta(days=30 + i), vector=None) for i in range(4)]
        healthy = _fact(db, other, "Globex", "uses", "Linear", when=T0 + timedelta(days=60),
                        vector=None)
        return [p.id for p in poisons], healthy.id

    @pytest.mark.parametrize("failure", ["vote", "anything"])
    def test_the_pass_goes_on_and_the_row_counts_an_attempt(self, db, two_accounts, monkeypatch,
                                                           failure):
        from korely_agent.services.memory_core.store import ResolutionUnavailable

        poisons, healthy = two_accounts
        poison = {str(p) for p in poisons}

        def judge(groups, db_, tenant_id, model_name=None):
            if any(g["new_id"] in poison for g in groups):
                raise (ResolutionUnavailable("the vocabulary gave no answer") if failure == "vote"
                       else RuntimeError("something nobody handles"))
            return {g["new_id"]: [] for g in groups}

        monkeypatch.setattr(contradiction, "_classify_batch", judge)
        out = vectors.process_pending_vectors(db)

        db.expire_all()
        assert db.get(AgentFact, healthy).embedding_status == vectors.DONE, (
            "the other account's vector waited behind the failed judgements")
        for p in poisons:
            row = db.get(AgentFact, p)
            assert row.embedding is None and row.embedding_status == vectors.PENDING
            assert row.embedding_attempts == 1 and row.embedding_next_at is not None
        assert out["failed"] == 4 and out["processed"] == 1

    def test_the_last_attempt_files_it(self, db, two_accounts, monkeypatch):
        from korely_agent.services.memory_core.store import ResolutionUnavailable

        poisons, _healthy = two_accounts

        def judge(groups, db_, tenant_id, model_name=None):
            raise ResolutionUnavailable("the vocabulary gave no answer")

        monkeypatch.setattr(contradiction, "_classify_batch", judge)
        (db.query(AgentFact).filter(AgentFact.id.in_(poisons))
         .update({"embedding_attempts": vectors.ATTEMPTS - 1}, synchronize_session=False))
        db.commit()
        out = vectors.process_pending_vectors(db)
        db.expire_all()
        assert {db.get(AgentFact, p).embedding_status for p in poisons} == {vectors.ERROR}
        assert out["gave_up"] == 4


def test_a_fact_whose_vector_waits_only_for_a_rename_is_not_judged_again(db, setting, monkeypatch):
    """Audit of 27 Oct 2026: declaring a name cleared the vector of every fact
    naming it, and the drain judged each again, a repeatable model spend. A
    fact judged when it was written takes its new vector and no judge."""
    old, late, events = setting
    db.query(AgentFact).filter(AgentFact.id == late.id).update({"judged_before_vector": True})
    db.commit()
    seen = []
    monkeypatch.setattr(contradiction, "_classify_batch", _judge_closing_everything(seen))

    out = vectors.process_pending_vectors(db)

    assert out["processed"] == 1 and seen == []
    db.expire_all()
    late_now = db.get(AgentFact, late.id)
    assert late_now.embedding_status == vectors.DONE and late_now.embedding is not None
    assert late_now.judged_before_vector is False
    assert db.get(AgentFact, old.id).invalid_at is None and events == []


def test_the_rename_clears_a_vector_with_the_judged_marker_and_a_failed_write_does_not(db, setting):
    from korely_agent.services import vector_state
    from korely_agent.services.facts import entities

    old, late, _events = setting
    assert entities.TABLES.text_changed is vector_state.renamed
    row = db.get(AgentFact, old.id)
    vector_state.renamed(row)
    assert row.judged_before_vector is True and row.embedding is None
    assert row.embedding_status == vectors.PENDING and row.embedding_attempts == 0
    vector_state.missed(row)
    assert row.judged_before_vector is False and row.embedding_attempts == 1


def test_a_fact_whose_write_vector_failed_then_renamed_is_still_judged(db, setting, monkeypatch):
    """Review of C2: `renamed` marked every fact judged, so a fact whose
    vector failed on its write (never judged) and was then renamed lost its
    only judgement."""
    from korely_agent.services import vector_state

    old, late, events = setting
    row = db.get(AgentFact, late.id)
    vector_state.missed(row)
    db.commit()
    row = db.get(AgentFact, late.id)
    vector_state.renamed(row)
    db.commit()
    assert row.judged_before_vector is False
    seen = []
    monkeypatch.setattr(contradiction, "_classify_batch", _judge_closing_everything(seen))
    vectors.process_pending_vectors(db)
    assert seen, "a fact never judged skipped its late judgement because a rename happened first"
