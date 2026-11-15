"""A fact whose subject its writer named is judged against that subject only (memory_core 6.5.0, 6.6.0).

The finding of 6 Nov 2026 (on a store of an agent team's own facts): a fact
written directly (POST /v1/facts, `korely add-fact`) closed true facts of
OTHER subjects. 119 of the 418 direct facts were closed, and 108 of the 111
closures between two direct facts had two different subjects ("deploy Cloud
1.0.8 happened on 5 November" closed "deploy Cloud 1.0.7 happened on 2
November"; one agent's role was shown another agent's). The road:
stages 1a, 1c and 1d of `memory_core.candidates.for_the_judge` want the same
subject or the same two entities, 1a-bis runs only inside one end user, and
the semantic stage 1b brought up to ten facts within the cutoff, of ANY
subject; the judge's question says that a subject phrased differently is the
same entity, which is what the extractor needs and what a name chosen by the
writer is not.

Now a fact whose subject the caller chose (a direct write or a correction, the
boundary of BUG-202) is weighed only against the facts about the same
canonical subject, aliases applied, or about the same two entities; and
(6.5.0) it enters the judgement of a fact the extraction read only about
that fact's subject. Two extracted facts meet as before.

These tests began as a reproduction of 6 Nov (17 local tests that
photographed the behaviour); the ones the change turns are written the other
way round here, and fail on memory_core 6.3.0. The judge is a fake that
records what it is shown and says yes to the subjects it is told to: no
model, no network (tests/conftest.py).
"""
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import facts as agent_facts
from korely_agent.services import vectors
from korely_agent.services.facts import contradiction as contra
from korely_agent.services.facts import entities as ent
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import candidates
from korely_agent.services.memory_core import judge as _judge
from korely_agent.services.sentinels import EVERY_PROJECT

VEC = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
D2 = datetime(2026, 11, 2, tzinfo=timezone.utc)
D5 = datetime(2026, 11, 5, tzinfo=timezone.utc)
D6 = datetime(2026, 11, 6, tzinfo=timezone.utc)


@pytest.fixture
def seen(monkeypatch):
    """The fake judge of one fact: records the candidates of every call and
    names as contradicted the candidates whose subject is in `seen["close"]`."""
    box = {"calls": [], "close": set()}

    def classify(candidates_, new_subject, new_predicate_raw, new_object, *a, **k):
        box["calls"].append({
            "new": (new_subject, new_predicate_raw, new_object),
            "candidates": [(c.subject_label, c.predicate_raw, c.object_label, c.agent_id)
                           for c in candidates_],
            "rows": [_judge.row(c, lambda _c: (False, None)) for c in candidates_],
        })
        return [c.id for c in candidates_ if c.subject_label in box["close"]], {}

    monkeypatch.setattr(contra, "_classify", classify)
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
    return box


@pytest.fixture
def vectors_on(monkeypatch):
    """Every fact gets the same vector: what the semantic stage brings is
    decided by who may enter it, not by the distances (the real ones were
    measured apart: on that store 9.7 of the 10 semantic candidates of a
    direct fact had another subject, 385 of 418 writes had all ten)."""
    state = {"on": True}
    monkeypatch.setattr("korely_agent.services.facts.service.fact_vector",
                        lambda text: list(VEC) if state["on"] else None)
    return state


def direct(db, tenant, subject, predicate, obj, agent, when=None, user=None, kind="unknown"):
    return agent_facts.write_fact_direct(
        db, tenant.id, subject=subject, predicate=predicate, obj=obj, subject_type=kind,
        agent_id=agent, valid_from=when, project_id=None, end_user_id=user)


def rows_of(db, tenant, subject):
    return (db.query(AgentFact)
            .filter(AgentFact.tenant_id == tenant.id, AgentFact.subject_label == subject)
            .order_by(AgentFact.created_at).all())


def extracted(db, tenant, subject, predicate, obj, *, user=None, agent=None, when=None, vector=VEC):
    """A fact as the extraction leaves it: read from a memory, by a model."""
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=user, agent_id=agent,
                    content="a memory", graph_sync_status="done")
    db.add(m)
    db.commit()
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=user, agent_id=agent,
                  subject_label=subject, subject_canonical=subject, subject_type="unknown",
                  predicate_raw=predicate, predicate_canonical=predicate, object_label=obj,
                  object_canonical=obj, confidence=0.9, source_memory_id=m.id,
                  source_memory_ids=[str(m.id)], tense="current", model_used="a-writer",
                  valid_from=when or D2, embedding=list(vector) if vector else None,
                  embedding_status=vectors.DONE if vector else vectors.PENDING)
    db.add(f)
    db.commit()
    return f


# 1. Two subjects ------------------------------------------------------------
# Turned: on 6.3.0 the second deploy was shown the first (the semantic stage),
# and a judge that said yes closed 1.0.7 on 5 November.

def test_another_subject_never_reaches_the_judge_of_a_direct_fact(db, tenant, seen, vectors_on):
    direct(db, tenant, "deploy Cloud 1.0.7", "happened_on", "2 November 2026 at 11:38 UTC",
           "agent-a", D2)
    direct(db, tenant, "deploy Cloud 1.0.8", "happened_on", "5 November 2026 at 08:03 UTC",
           "agent-a", D5)
    assert seen["calls"] == [], "a deploy of another version is another subject"


def test_a_judge_that_would_say_yes_closes_nothing_of_another_subject(db, tenant, seen, vectors_on):
    seen["close"] = {"deploy Cloud 1.0.7"}
    direct(db, tenant, "deploy Cloud 1.0.7", "happened_on", "2 November 2026", "agent-a", D2)
    out = direct(db, tenant, "deploy Cloud 1.0.8", "happened_on", "5 November 2026", "agent-a", D5)
    assert out["invalidated"] == []
    assert rows_of(db, tenant, "deploy Cloud 1.0.7")[0].invalid_at is None


def test_without_a_vector_nothing_reached_the_judge_before_either(db, tenant, seen, vectors_on):
    """The guard of the cause: no SQL stage brings another subject, only the
    semantic one did (unchanged, true on 6.3.0 too)."""
    vectors_on["on"] = False
    direct(db, tenant, "deploy Cloud 1.0.7", "happened_on", "2 November 2026", "agent-a", D2)
    direct(db, tenant, "deploy Cloud 1.0.8", "happened_on", "5 November 2026", "agent-a", D5)
    assert seen["calls"] == []


# 2. Two agents --------------------------------------------------------------

def test_one_agents_role_does_not_see_anothers(db, tenant, seen, vectors_on):
    """Turned: on 6.3.0 the second agent's fact was shown the first's."""
    direct(db, tenant, "agent-c", "is", "the builder", "agent-c")
    direct(db, tenant, "agent-d", "is", "the docs writer", "agent-d")
    assert seen["calls"] == []


def test_the_same_subject_from_another_agent_still_reaches_the_judge(db, tenant, seen, vectors_on):
    """The wall of the candidates is still the end user's, of any agent: two
    agents of one end user speak of the same things (`entities.memory_scope`).
    A real change of the same subject, told by another agent, still closes."""
    seen["close"] = {"Cloud"}
    direct(db, tenant, "Cloud", "deployed_version", "1.0.7", "agent-a", D2)
    out = direct(db, tenant, "Cloud", "deployed_version", "1.0.8", "agent-b", D5)
    assert seen["calls"][-1]["candidates"] == [("Cloud", "deployed_version", "1.0.7", "agent-a")]
    old = rows_of(db, tenant, "Cloud")[0]
    assert out["invalidated"] and old.invalid_at == D5


def test_the_same_fact_of_two_agents_is_still_shown_to_the_judge(db, tenant, seen, vectors_on):
    """Unchanged: the reconfirmation looks in the agent's memory
    (`memory_of`), the judge in the end user's (`memory_scope`), so the same
    triple of two agents is two rows, and the second is judged against the
    first (the judge reads one fact twice, not a change)."""
    direct(db, tenant, "the charter", "applies_from", "5 November 2026", "agent-b")
    direct(db, tenant, "the charter", "applies_from", "5 November 2026", "agent-a")
    assert len(rows_of(db, tenant, "the charter")) == 2
    assert seen["calls"][-1]["candidates"] == [("the charter", "applies_from", "5 November 2026", "agent-b")]


# 3. The same subject, all its stages -----------------------------------------

def test_a_real_change_of_the_same_subject_still_closes(db, tenant, seen, vectors_on):
    seen["close"] = {"Cloud"}
    direct(db, tenant, "Cloud", "deployed_version", "1.0.10", "agent-a", D2)
    out = direct(db, tenant, "Cloud", "deployed_version", "1.0.11", "agent-a", D5)
    assert len(out["invalidated"]) == 1
    assert rows_of(db, tenant, "Cloud")[0].invalid_at == D5


def test_the_same_subject_with_another_relation_still_comes_by_meaning(db, tenant, seen, vectors_on):
    """The semantic stage still runs, among the facts of the same subject: a
    relation of another name, with an object no SQL stage relates (on the
    agent team's store 3 of the 111 closures between direct facts were these)."""
    direct(db, tenant, "deploy Cloud 1.0.8", "happened_on", "5 November 2026", "agent-a", D5)
    direct(db, tenant, "deploy Cloud 1.0.8", "has_date", "6 November", "agent-a", D6)
    assert seen["calls"][-1]["candidates"] == [
        ("deploy Cloud 1.0.8", "happened_on", "5 November 2026", "agent-a")]


def test_the_case_of_the_name_does_not_make_another_subject(db, tenant, seen, vectors_on):
    """The database's lower() on both sides, as stage 1a (2.11.0)."""
    direct(db, tenant, "Cloud", "deployed_version", "1.0.7", "agent-a", D2)
    direct(db, tenant, "cloud", "has_version", "1.0.8", "agent-a", D5)
    assert [c[0] for c in seen["calls"][-1]["candidates"]] == ["Cloud"]


def test_the_same_two_entities_the_other_way_round_still_reach_the_judge(db, tenant, seen, vectors_on):
    """Stage 1d, the same two entities: "Salesforce serves_as_crm_for Globex"
    against "Globex no_longer_uses Salesforce"."""
    direct(db, tenant, "Salesforce", "serves_as_crm_for", "Globex", "agent-c")
    direct(db, tenant, "Globex", "dropped", "Salesforce", "agent-c")
    assert ("Salesforce", "serves_as_crm_for", "Globex", "agent-c") in seen["calls"][-1]["candidates"]


def test_a_direct_fact_still_sees_every_fact_of_its_subject(db, tenant, seen, vectors_on):
    """From case 5 of the reproduction: an extracted fact and a direct one of the
    same subject; the judge reads the relation as written, not its merge."""
    extracted(db, tenant, "LoCoMo", "has_dataset_ceiling", "93.6%", agent="agent-f")
    direct(db, tenant, "LoCoMo", "has_score", "86.9% with the Mem0 protocol", "agent-f")
    rows = "\n".join(seen["calls"][-1]["rows"])
    assert "LoCoMo has_dataset_ceiling 93.6%" in rows


# 4. Aliases -----------------------------------------------------------------

def test_a_declared_name_is_the_same_subject(db, tenant, seen, vectors_on):
    """The risk the change takes, covered by the aliases the store has: the
    extraction calls the end user by their id, a developer by their name.
    With the name declared (`POST /v1/users/{id}/names`, `declare_alias`), the
    direct fact on "Maria" is a fact on u-42, and closes the extracted one."""
    seen["close"] = {"u-42"}
    old = extracted(db, tenant, "u-42", "works_at", "Acme", user="u-42")
    ent.declare_alias(db, tenant.id, None, "u-42", None, "Maria", "u-42")
    db.commit()
    out = direct(db, tenant, "Maria", "works_at", "Globex", None, D5, user="u-42")
    assert [c[0] for c in seen["calls"][-1]["candidates"]] == ["u-42"]
    db.refresh(old)
    assert out["invalidated"] and old.invalid_at == D5


def test_without_the_declared_name_maria_is_another_subject(db, tenant, seen, vectors_on):
    """The price, said by a test (docs/guide/limits.md): with no name
    declared, "Maria" and "u-42" are two subjects for a direct fact, and the
    extracted fact stays true. On 6.3.0 the end user's recall stage (1a-bis)
    and the semantic stage both showed it."""
    seen["close"] = {"u-42"}
    old = extracted(db, tenant, "u-42", "works_at", "Acme", user="u-42")
    direct(db, tenant, "Maria", "works_at", "Globex", None, D5, user="u-42")
    assert seen["calls"] == []
    db.refresh(old)
    assert old.invalid_at is None


def test_an_alias_made_by_a_rename_is_the_same_subject(db, tenant, seen, vectors_on):
    """An alias made by itself from an identity statement (`writing.
    _alias_from_identity`): "Gordon renamed_to Korely", then a direct fact on
    Korely sees what was said of Gordon."""
    direct(db, tenant, "Gordon", "uses", "Groq", "agent-c", kind="product")
    direct(db, tenant, "Gordon", "renamed_to", "Korely", "agent-c", kind="product")
    seen["calls"].clear()
    direct(db, tenant, "Korely", "uses", "Deepgram", "agent-c", kind="product")
    assert ("Gordon", "uses", "Groq", "agent-c") in seen["calls"][-1]["candidates"]


# 5. The chain ---------------------------------------------------------------

def test_rewriting_a_fact_reconfirms_it_instead_of_starting_a_chain(db, tenant, seen, vectors_on):
    """Turned: on 6.3.0 "a public release" closed "silence"
    (another subject), and the fact written again was a new row that closed,
    in turn, the one that had closed it."""
    seen["close"] = {"silence", "a public release"}
    direct(db, tenant, "silence", "does_not_count_as", "a yes", "agent-b", D2)
    direct(db, tenant, "a public release", "waits_for", "an explicit answer", "agent-b", D5)
    direct(db, tenant, "silence", "does_not_count_as", "a yes", "agent-b")
    assert [r.invalid_at for r in rows_of(db, tenant, "silence")] == [None]
    assert rows_of(db, tenant, "a public release")[0].invalid_at is None
    assert rows_of(db, tenant, "silence")[0].observation_count == 2


# 6. A correction ------------------------------------------------------------

def test_a_correction_is_judged_against_its_own_subject(db, tenant, seen, vectors_on):
    """The other door whose words the caller chose (PATCH /v1/facts/{id}).
    Turned: on 6.3.0 the corrected deploy date was shown the other deploy."""
    direct(db, tenant, "deploy Cloud 1.0.7", "happened_on", "2 November 2026", "agent-a", D2)
    wrong = direct(db, tenant, "deploy Cloud 1.0.8", "happened_on", "4 November 2026", "agent-a", D5)
    seen["calls"].clear()
    seen["close"] = {"deploy Cloud 1.0.7"}
    from korely_agent.services.ids import parse_fact_id
    out = agent_facts.correct_fact(db, tenant.id, parse_fact_id(wrong["id"]),
                                   obj="5 November 2026", project_id=EVERY_PROJECT,
                                   write_shape=True)
    assert out["invalidated"] == [wrong["id"]], "only the corrected fact"
    assert all(c[0] == "deploy Cloud 1.0.8" for call in seen["calls"] for c in call["candidates"])
    assert rows_of(db, tenant, "deploy Cloud 1.0.7")[0].invalid_at is None


# 7. A vector that comes late ------------------------------------------------

def _late(db, tenant, subject, predicate, obj, *, when, vector, model_used, user=None):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=user,
                  subject_label=subject, subject_canonical=subject, subject_type="unknown",
                  predicate_raw=predicate, predicate_canonical=predicate,
                  object_label=obj, object_canonical=obj, confidence=0.9,
                  valid_from=when, created_at=when, tense="current", model_used=model_used,
                  embedding=vector, embedding_status=vectors.DONE if vector else vectors.PENDING)
    db.add(f)
    db.commit()
    return f


@pytest.fixture
def late_judge(monkeypatch):
    seen_groups = []

    def judge(groups, db, tenant_id, model_name=None):
        seen_groups.extend((g["new_id"], sorted(c.subject_label for c in g["candidates"]))
                           for g in groups)
        return {g["new_id"]: [c.id for c in g["candidates"]] for g in groups}

    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.services.facts.service.fact_vector", lambda text: list(VEC))
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
    monkeypatch.setattr(contra, "_classify_batch", judge)
    return seen_groups


def test_a_direct_fact_whose_vector_comes_late_is_not_judged_against_another_subject(db, tenant,
                                                                                     late_judge):
    """`judge.after_the_vector`, the judgement the vector drain makes when a
    write's own vector failed. Turned: on 6.3.0 the late direct fact was
    judged against the facts only meaning finds, of any subject."""
    old = _late(db, tenant, "deploy Cloud 1.0.7", "happened_on", "2 November", when=D2,
                vector=list(VEC), model_used="direct")
    late = _late(db, tenant, "deploy Cloud 1.0.8", "happened_on", "5 November", when=D5,
                 vector=None, model_used="direct")
    vectors.process_pending_vectors(db)
    db.expire_all()
    assert late_judge == []
    assert db.get(AgentFact, old.id).invalid_at is None
    assert db.get(AgentFact, late.id).embedding is not None


def test_a_later_direct_fact_is_not_judged_against_a_late_one_of_another_subject(db, tenant,
                                                                                late_judge):
    """The other half of the late judgement: the LATER facts near the late
    one are judged against it, as their writes would have been. A later
    direct fact of another subject would not have seen it."""
    late = _late(db, tenant, "deploy Cloud 1.0.7", "happened_on", "2 November", when=D2,
                 vector=None, model_used="a-writer")
    _late(db, tenant, "deploy Cloud 1.0.8", "happened_on", "5 November", when=D5,
          vector=list(VEC), model_used="direct")
    vectors.process_pending_vectors(db)
    db.expire_all()
    assert late_judge == []
    assert db.get(AgentFact, late.id).invalid_at is None


def test_a_late_direct_fact_still_meets_its_own_subject(db, tenant, late_judge):
    old = _late(db, tenant, "Cloud", "deployed_version", "1.0.7", when=D2, vector=list(VEC),
                model_used="direct")
    late = _late(db, tenant, "Cloud", "has_version", "1.0.8", when=D5, vector=None,
                 model_used="direct")
    vectors.process_pending_vectors(db)
    db.expire_all()
    assert late_judge == [(str(late.id), ["Cloud"])]
    assert db.get(AgentFact, old.id).invalidated_by_id == late.id


def test_the_late_judgement_of_extracted_facts_is_unchanged(db, tenant, late_judge):
    """Not touched by 6.5.0 nor 6.6.0: two extracted facts of
    two subjects, near by meaning, are still weighed together (the case
    `tests/test_late_vector_judgement.py` was built on: "Acme Corp" and "Acme").
    Inside one end user's memory: with no end user, since 6.9.0, two
    subjects meet by meaning no more (`owner.keeps_to_its_subject`)."""
    old = _late(db, tenant, "Acme Corp", "on_plan", "Business plan", when=D2, vector=list(VEC),
                model_used="a-writer", user="u1")
    late = _late(db, tenant, "Acme", "subscribes_to", "Starter plan", when=D5, vector=None,
                 model_used="a-writer", user="u1")
    vectors.process_pending_vectors(db)
    db.expire_all()
    assert late_judge == [(str(late.id), ["Acme Corp"])]
    assert db.get(AgentFact, old.id).invalidated_by_id == late.id


# 8. The engine's function ---------------------------------------------------

def _search(db, tenant, s, p, o, *, named, user=None, vector=VEC):
    return candidates.for_the_judge(
        db, ent.TABLES, ent.memory_scope(tenant.id, None, user), is_open=true_at(),
        exclude_id=uuid.uuid4(), subject_label=s, predicate_canonical=p, subject_canonical=s,
        object_canonical=o, with_recall=user is not None, vector=list(vector),
        subject_named=named)


def test_the_extraction_still_reads_every_subject_by_meaning(db, tenant):
    """Without `subject_named` (and `named`) the stages are those of 6.3.0: the extractor
    names one entity several ways, and stages 1a-bis and 1b are how its
    facts meet ("the runner" and "<user>")."""
    extracted(db, tenant, "the runner", "personal_best", "21 minutes", user="u1")
    extracted(db, tenant, "charity 5K run", "finish_time", "22 minutes", user="u1")
    got = _search(db, tenant, "<user>", "personal_best", "19 minutes", named=False, user="u1")
    assert sorted(c.subject_label for c in got) == ["charity 5K run", "the runner"]
    assert _search(db, tenant, "<user>", "personal_best", "19 minutes", named=True, user="u1") == []


def test_a_named_subject_keeps_every_stage_of_its_own_subject(db, tenant):
    a = extracted(db, tenant, "Maria", "works_at", "Apple", user="u1")           # 1a
    c = extracted(db, tenant, "Maria", "visited", "Google", user="u1")           # 1c
    d = extracted(db, tenant, "Google", "employs", "Maria", user="u1")           # 1d
    b = extracted(db, tenant, "Maria", "is_employed_by", "Initech", user="u1")   # 1b
    extracted(db, tenant, "Marta", "works_at", "Umbrella", user="u1")            # 1a-bis, 1b
    got = _search(db, tenant, "Maria", "works_at", "Google", named=True, user="u1")
    assert {x.id for x in got} == {a.id, c.id, d.id, b.id}


# 9. The other direction: a fact read from a memory against a named one ------
# The decision of 7 Nov 2026: a fact written directly enters the judgement of
# an EXTRACTED fact only when it is about the same canonical subject or the
# same two entities. On the agent team's store,
# 3 of the 8 closures of a direct fact by an extracted one were judged on the
# extracted fact's write, between two subjects, and survived the first half
# of 6.5.0. Turned: each test below fails on that first half (d0e01b77). Two
# extracted facts are judged as before.

def by_extraction(db, tenant, subject, predicate, obj, *, user=None, when=None):
    """A fact written as the extraction writes one: from a memory, by a model,
    through the same door (`service._persist_one`) and its per-fact judge."""
    from korely_agent.services.facts.service import _persist_one

    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=user, content="a memory",
                    graph_sync_status="done")
    db.add(m)
    db.commit()
    fact, closed = _persist_one(
        db, tenant.id, end_user_id=user, agent_id=None, run_id=None, subject=subject,
        subject_type="unknown", predicate_raw=predicate, obj=obj, object_is_literal=False,
        confidence=0.9, source_memory_id=m.id, model_used="a-writer", valid_from=when,
        project_id=None)
    db.commit()
    return fact, closed


def test_an_extracted_fact_does_not_close_a_direct_fact_of_another_subject(db, tenant, seen,
                                                                            vectors_on):
    seen["close"] = {"the reviewer"}
    reviewer = direct(db, tenant, "the reviewer", "works",
                      "read-only: no Docker, no changes to the code", "agent-e", D2)
    _, closed = by_extraction(db, tenant, "user", "uses_tool", "Docker", when=D5)
    assert closed == [] and seen["calls"] == []
    assert rows_of(db, tenant, "the reviewer")[0].invalid_at is None, reviewer["id"]


def test_an_extracted_cost_does_not_close_a_measured_direct_cost(db, tenant, seen, vectors_on):
    seen["close"] = {"the measured cost of the last full run"}
    direct(db, tenant, "the measured cost of the last full run", "was", "8 euro in all",
           "agent-f", D2)
    _, closed = by_extraction(db, tenant, "the benchmark", "costs", "13 euro", when=D5)
    assert closed == [] and seen["calls"] == []


def test_the_batched_judge_of_a_memory_follows_the_same_rule(db, tenant, monkeypatch, vectors_on):
    """The extraction's write judges a memory's facts in one call
    (`detect_and_invalidate_batch`): the same candidates. Inside one end
    user's memory, where two extracted facts of two subjects still meet by
    meaning (with no end user, since 6.9.0, they do not)."""
    groups_seen = []
    monkeypatch.setattr(contra, "_classify_batch",
                        lambda groups, *a, **k: groups_seen.extend(groups) or {})
    direct(db, tenant, "the reviewer", "works", "read-only: no Docker", "agent-e", D2, user="u1")
    other = extracted(db, tenant, "the team", "runs_in", "Docker containers", user="u1")
    new = extracted(db, tenant, "user", "uses_tool", "Docker", when=D5, user="u1")
    contra.detect_and_invalidate_batch(db, tenant.id, project_id=None, new_facts=[{
        "id": new.id, "subject": new.subject_label, "subject_canonical": new.subject_canonical,
        "predicate_raw": new.predicate_raw, "predicate_canonical": new.predicate_canonical,
        "object": new.object_label, "object_canonical": new.object_canonical,
        "valid_from": new.valid_from, "embedding": list(VEC),
        "source_memory_id": new.source_memory_id, "end_user_id": "u1"}])
    assert [[c.subject_label for c in g["candidates"]] for g in groups_seen] == [[other.subject_label]]


def test_a_direct_fact_of_the_same_subject_is_still_closed_by_an_extracted_one(db, tenant, seen,
                                                                               vectors_on):
    seen["close"] = {"Cloud"}
    direct(db, tenant, "Cloud", "deployed_version", "1.0.7", "agent-a", D2)
    _, closed = by_extraction(db, tenant, "Cloud", "has_version", "1.0.8", when=D5)
    assert [c[0] for c in seen["calls"][-1]["candidates"]] == ["Cloud"]
    assert len(closed) == 1 and rows_of(db, tenant, "Cloud")[0].invalid_at == D5


def test_an_extracted_fact_meets_a_direct_one_on_a_declared_name(db, tenant, seen, vectors_on):
    """The end user's recall stage (1a-bis) and the semantic stage no longer
    bring "Maria" to a fact on u-42; the declared name does, through 1a."""
    seen["close"] = {"Maria"}
    ent.declare_alias(db, tenant.id, None, "u-42", None, "Maria", "u-42")
    db.commit()
    direct(db, tenant, "Maria", "works_at", "Acme", None, D2, user="u-42")
    _, closed = by_extraction(db, tenant, "u-42", "works_at", "Globex", user="u-42", when=D5)
    assert [c[0] for c in seen["calls"][-1]["candidates"]] == ["Maria"]
    assert len(closed) == 1


def test_without_the_declared_name_an_extracted_fact_does_not_meet_maria(db, tenant, seen,
                                                                         vectors_on):
    """The price in this direction too (docs/guide/limits.md)."""
    seen["close"] = {"Maria"}
    direct(db, tenant, "Maria", "works_at", "Acme", None, D2, user="u-42")
    _, closed = by_extraction(db, tenant, "u-42", "works_at", "Globex", user="u-42", when=D5)
    assert closed == [] and seen["calls"] == []


def test_two_extracted_facts_of_two_subjects_still_meet(db, tenant, seen, vectors_on):
    """Not touched inside one end user's memory: the extractor names one
    thing several ways, and stages 1a-bis and 1b are how its facts meet (here
    "Acme Corp" and "Acme"). With no end user, since 6.9.0, they do not
    (tests/test_an_agents_memory_is_judged_subject_by_subject.py)."""
    seen["close"] = {"Acme Corp"}
    by_extraction(db, tenant, "Acme Corp", "on_plan", "Business plan", user="u1", when=D2)
    _, closed = by_extraction(db, tenant, "Acme", "subscribes_to", "Starter plan", user="u1", when=D5)
    assert [c[0] for c in seen["calls"][-1]["candidates"]] == ["Acme Corp"]
    assert len(closed) == 1


def test_two_extracted_facts_of_one_end_user_still_meet_by_relation(db, tenant, seen, vectors_on):
    """Stage 1a-bis between two extracted facts, without vectors."""
    vectors_on["on"] = False
    by_extraction(db, tenant, "the runner", "personal_best", "21 minutes", user="u1", when=D2)
    by_extraction(db, tenant, "u1", "personal_best", "19 minutes", user="u1", when=D5)
    assert [c[0] for c in seen["calls"][-1]["candidates"]] == ["the runner"]


def test_a_late_extracted_fact_is_not_judged_against_a_direct_one_of_another_subject(db, tenant,
                                                                                    late_judge):
    old = _late(db, tenant, "the reviewer", "works", "read-only", when=D2, vector=list(VEC),
                model_used="direct")
    _late(db, tenant, "user", "uses_tool", "Docker", when=D5, vector=None, model_used="a-writer")
    vectors.process_pending_vectors(db)
    db.expire_all()
    assert late_judge == []
    assert db.get(AgentFact, old.id).invalid_at is None


def test_a_later_extracted_fact_is_not_judged_against_a_late_direct_one_of_another_subject(
        db, tenant, late_judge):
    late = _late(db, tenant, "the reviewer", "works", "read-only", when=D2, vector=None,
                 model_used="direct")
    _late(db, tenant, "user", "uses_tool", "Docker", when=D5, vector=list(VEC), model_used="a-writer")
    vectors.process_pending_vectors(db)
    db.expire_all()
    assert late_judge == []
    assert db.get(AgentFact, late.id).invalid_at is None


def test_the_engine_lets_every_extracted_fact_in_and_a_named_one_by_its_subject(db, tenant):
    """`candidates.for_the_judge(named=)` for an extracted new fact: the
    extracted candidates as before, the named ones only about its subject
    (a fact with no `model_used` is not named: NULL is no)."""
    a = extracted(db, tenant, "the runner", "personal_best", "21 minutes", user="u1")
    b = extracted(db, tenant, "charity 5K run", "finish_time", "22 minutes", user="u1")
    unknown = extracted(db, tenant, "Marta", "personal_best", "20 minutes", user="u1")
    unknown.model_used = None
    named_other = extracted(db, tenant, "coach", "personal_best", "18 minutes", user="u1")
    named_other.model_used = "direct"
    named_same = extracted(db, tenant, "<user>", "trains_for", "a marathon", user="u1")
    named_same.model_used = "direct"
    db.commit()
    got = candidates.for_the_judge(
        db, ent.TABLES, ent.memory_scope(tenant.id, None, "u1"), is_open=true_at(),
        exclude_id=uuid.uuid4(), subject_label="<user>", predicate_canonical="personal_best",
        subject_canonical="<user>", object_canonical="19 minutes", with_recall=True,
        vector=list(VEC), named=ent.NAMED_FACTS)
    assert {c.id for c in got} == {a.id, b.id, unknown.id, named_same.id}


# 10. The design of 7 Nov 2026 ------------------------------------------------
# A filter on the candidates, not the [another subject] tag; one rule of "the
# same subject" (the database's lower() on the canonical name), at the write
# and in the late judgement; a correction is the caller's only when its
# subject is.

def test_a_direct_fact_with_no_fact_of_its_subject_asks_no_judge(db, tenant, seen, vectors_on):
    """The cost of the filter: no call at all when no open fact has the
    subject (nor names it as its subject with the two entities the other way
    round); one call when one does."""
    for i in range(5):
        direct(db, tenant, f"service {i}", "runs_on", "the cluster", "agent-a", D2)
    seen["calls"].clear()
    direct(db, tenant, "the gateway", "runs_on", "the cluster", "agent-a", D5)
    assert seen["calls"] == []
    direct(db, tenant, "the gateway", "runs_on", "the new cluster", "agent-a", D6)
    assert len(seen["calls"]) == 1
    assert [c[0] for c in seen["calls"][0]["candidates"]] == ["the gateway"]


@pytest.mark.parametrize("first,second", [
    ("Anna Rossi", "Anna"),
    ("the user", "user"),
    ("Anna  Rossi", "Anna Rossi"),
    ("Straße 5", "STRASSE 5"),
    ("Ｃｌｏｕｄ", "Cloud"),
    ("u-42", "user"),
])
def test_two_spellings_of_a_name_are_two_subjects_until_declared(db, tenant, seen, vectors_on,
                                                                 first, second):
    """The limit, fixed by a test (docs/guide/limits.md): the store compares
    two subjects with the database's lower() on the canonical name, nothing
    else. A shorter name, an article, a second space, another Unicode form
    are another subject until a name is declared."""
    direct(db, tenant, first, "lives_in", "Rome", "agent-a", D2, user="u-42")
    direct(db, tenant, second, "lives_in", "Milan", "agent-a", D5, user="u-42")
    assert seen["calls"] == []


def test_two_spellings_that_differ_by_case_are_one_subject(db, tenant, seen, vectors_on):
    direct(db, tenant, "Anna", "lives_in", "Rome", "agent-a", D2, user="u-42")
    direct(db, tenant, "anna", "lives_in", "Milan", "agent-a", D5, user="u-42")
    assert [c[0] for c in seen["calls"][-1]["candidates"]] == ["Anna"]


def test_a_declared_name_joins_two_spellings(db, tenant, seen, vectors_on):
    ent.declare_alias(db, tenant.id, None, "u-42", None, "Anna", "Anna Rossi")
    db.commit()
    direct(db, tenant, "Anna Rossi", "lives_in", "Rome", None, D2, user="u-42")
    direct(db, tenant, "Anna", "lives_in", "Milan", None, D5, user="u-42")
    assert [c[0] for c in seen["calls"][-1]["candidates"]] == ["Anna Rossi"]


def test_a_user_id_that_is_not_a_name_cannot_be_declared_and_the_limits_say_so(db, tenant):
    """The remedy has a limit of its own: a name is declared only for a
    `user_id` that is itself a name (`identity.valid_name`): one that starts
    with a digit is refused with a 422, and the limits say so."""
    from pathlib import Path

    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app
    from korely_agent.services import keys as key_service

    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="names")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        r = client.post("/v1/users/42-anna/names", headers={"Authorization": f"Bearer {full}"},
                        json={"name": "Anna Rossi"})
    app.dependency_overrides.clear()
    assert r.status_code == 422, r.text
    limits = " ".join(Path("docs/guide/limits.md").read_text(encoding="utf-8").split())
    assert "only a `user_id` that is itself a name can have one (422 otherwise)" in limits


def test_the_late_judgement_and_the_write_agree_on_who_meets_whom(db, tenant, seen, vectors_on,
                                                                    monkeypatch):
    """One rule in both roads. At the write, a direct "Straße 5" after an
    extracted "STRASSE 5" is shown nothing (the database's lower() keeps the
    two apart); when the extracted fact's vector comes late, the later direct
    fact is not judged against it either. Turned: on 6.5.0 the late road
    compared the two with NFKC and case folding, met them and closed one."""
    extracted(db, tenant, "STRASSE 5", "lives_in", "Rome", vector=VEC)
    direct(db, tenant, "Straße 5", "lives_in", "Milan", "agent-a", D5)
    assert seen["calls"] == []
    late_judge = []
    monkeypatch.setattr("korely_agent.core.llm.embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.services.webhooks.emit_event", lambda *a, **k: None)
    monkeypatch.setattr(contra, "_classify_batch", lambda groups, *a, **k: late_judge.extend(
        (g["new_id"], sorted(c.subject_label for c in g["candidates"])) for g in groups) or {
        g["new_id"]: [c.id for c in g["candidates"]] for g in groups})
    late = _late(db, tenant, "STRASSE 5", "lives_in", "Turin", when=D2, vector=None,
                 model_used="a-writer")
    _late(db, tenant, "Straße 5", "lives_in", "Naples", when=D6, vector=list(VEC),
          model_used="direct")
    vectors.process_pending_vectors(db)
    db.expire_all()
    assert late_judge == []
    assert db.get(AgentFact, late.id).invalid_at is None


def _probe_extracted(db, tenant, subject, predicate, obj, *, user, when=D2):
    return extracted(db, tenant, subject, predicate, obj, user=user, when=when)


def test_a_correction_of_an_extracted_object_keeps_the_extractions_subject(db, tenant, seen,
                                                                            vectors_on):
    """The extraction wrote one wrong value under two names of the end user;
    the caller corrects one of them giving only the object. The correction
    keeps the extraction's subject and provenance, so the other copy is shown
    and closed. Turned: on 6.5.0 the correction was 'named' and saw only its
    own subject."""
    seen["close"] = {"u1"}
    other = _probe_extracted(db, tenant, "u1", "personal_best", "21 minutes", user="u1")
    wrong = _probe_extracted(db, tenant, "the runner", "personal_best", "21 minutes", user="u1")
    seen["calls"].clear()
    out = agent_facts.correct_fact(db, tenant.id, wrong.id, obj="19 minutes",
                                   project_id=EVERY_PROJECT, write_shape=True)
    db.refresh(other)
    assert other.invalid_at is not None
    from korely_agent.services.ids import parse_fact_id
    assert db.get(AgentFact, parse_fact_id(out["id"])).model_used == "a-writer"


def test_a_later_extraction_meets_a_correction_of_an_extracted_fact(db, tenant, seen, vectors_on):
    """After that correction, a value the extraction reads later under the
    end user's id meets it (the end user's recall stage and the semantic
    stage) and closes it. Turned: on 6.5.0 the correction was a named fact of
    another subject, and stayed true beside the later value."""
    wrong = _probe_extracted(db, tenant, "the runner", "personal_best", "21 minutes", user="u1")
    out = agent_facts.correct_fact(db, tenant.id, wrong.id, obj="19 minutes",
                                   project_id=EVERY_PROJECT, write_shape=True)
    seen["calls"].clear()
    seen["close"] = {"the runner"}
    _, closed = by_extraction(db, tenant, "u1", "personal_best", "18 minutes", user="u1",
                              when=datetime(2027, 1, 1, tzinfo=timezone.utc))
    from korely_agent.services.ids import parse_fact_id
    assert parse_fact_id(out["id"]) in closed


def test_a_correction_that_names_the_subject_is_judged_against_it_only(db, tenant, seen,
                                                                       vectors_on):
    """With `subject` given, the correction is the caller's: candidates of
    that subject only, and its row says so."""
    _probe_extracted(db, tenant, "u1", "personal_best", "21 minutes", user="u1")
    wrong = _probe_extracted(db, tenant, "the runner", "personal_best", "21 minutes", user="u1")
    seen["calls"].clear()
    out = agent_facts.correct_fact(db, tenant.id, wrong.id, subject="the runner",
                                   obj="19 minutes", project_id=EVERY_PROJECT, write_shape=True)
    assert all(c[0] == "the runner" for call in seen["calls"] for c in call["candidates"])
    from korely_agent.services.ids import parse_fact_id
    assert db.get(AgentFact, parse_fact_id(out["id"])).model_used == "correction"


def test_a_correction_of_a_correction_stays_the_callers(db, tenant, seen, vectors_on):
    wrong = direct(db, tenant, "the gateway", "runs_on", "cluster A", "agent-a", D2)
    from korely_agent.services.ids import parse_fact_id
    first = agent_facts.correct_fact(db, tenant.id, parse_fact_id(wrong["id"]), obj="cluster B",
                                     project_id=EVERY_PROJECT, write_shape=True)
    second = agent_facts.correct_fact(db, tenant.id, parse_fact_id(first["id"]), obj="cluster C",
                                      project_id=EVERY_PROJECT, write_shape=True)
    assert db.get(AgentFact, parse_fact_id(second["id"])).model_used == "correction"


def test_a_correction_without_a_subject_merges_the_inherited_relation(db, tenant, seen, vectors_on,
                                                                      monkeypatch):
    """The relation a correction inherits from an extracted fact is the
    extraction's word, resolved as the extraction's are (`merge=True`); one
    the caller names is resolved as the caller's (`merge=False`). The
    relation stays the extracted fact's."""
    from korely_agent.services import predicates

    asked = []
    real = predicates.resolve_relation

    def spy(*a, **k):
        asked.append(k.get("merge"))
        return real(*a, **k)

    monkeypatch.setattr(predicates, "resolve_relation", spy)
    from korely_agent.services.ids import parse_fact_id

    wrong = _probe_extracted(db, tenant, "the runner", "personal_best", "21 minutes", user="u1")
    out = agent_facts.correct_fact(db, tenant.id, wrong.id, obj="19 minutes",
                                   project_id=EVERY_PROJECT, write_shape=True)
    assert asked == [True]
    assert out["predicate"] == "personal_best"
    asked.clear()
    agent_facts.correct_fact(db, tenant.id, parse_fact_id(out["id"]), subject="the runner",
                             obj="18 minutes", project_id=EVERY_PROJECT, write_shape=True)
    assert asked == [False]


# 11. The mutants left alive by the first round of review -------------------

def test_a_late_direct_fact_is_not_judged_against_an_older_extracted_one_of_another_subject(
        db, tenant, late_judge):
    _late(db, tenant, "the team", "deploy_happened", "on 2 November", when=D2,
          vector=list(VEC), model_used="a-writer")
    _late(db, tenant, "deploy Cloud 1.0.8", "happened_on", "5 November", when=D5,
          vector=None, model_used="direct")
    vectors.process_pending_vectors(db)
    assert late_judge == [], "a late direct fact is judged against its own subject only"


def test_a_declared_name_is_met_by_meaning_too(db, tenant, seen, vectors_on):
    """An aliased subject reached only by the semantic stage (another
    relation's name): "Maria" is u-42 once declared."""
    ent.declare_alias(db, tenant.id, None, "u-42", None, "Maria", "u-42")
    db.commit()
    direct(db, tenant, "Maria", "works_at", "Acme", None, D2, user="u-42")
    seen["calls"].clear()
    direct(db, tenant, "u-42", "is_employed_by", "Globex", None, D5, user="u-42")
    assert [c[0] for c in seen["calls"][-1]["candidates"]] == ["Maria"]


def test_a_correction_is_not_shown_extracted_facts_of_another_subject(db, tenant, seen, vectors_on):
    extracted(db, tenant, "the team", "deploy_happened", "on 2 November")
    wrong = direct(db, tenant, "deploy Cloud 1.0.8", "happened_on", "4 November", "agent-a", D5)
    seen["calls"].clear()
    from korely_agent.services.ids import parse_fact_id
    agent_facts.correct_fact(db, tenant.id, parse_fact_id(wrong["id"]), obj="5 November",
                             project_id=EVERY_PROJECT, write_shape=True)
    assert all(c[0] == "deploy Cloud 1.0.8" for call in seen["calls"] for c in call["candidates"])


def test_a_late_aliased_fact_meets_a_later_direct_one(db, tenant, late_judge):
    late = _late(db, tenant, "Maria", "works_at", "Acme", when=D2, vector=None, model_used="a-writer")
    late.subject_canonical = "u-42"
    db.commit()
    _late(db, tenant, "u-42", "is_employed_by", "Globex", when=D5, vector=list(VEC),
          model_used="direct")
    vectors.process_pending_vectors(db)
    assert late_judge, "Maria is u-42 for the store: the later direct fact meets it"


def test_a_new_fact_on_a_declared_name_meets_its_end_user_by_meaning(db, tenant, seen, vectors_on):
    """The other side of the alias: the NEW fact is written under the declared
    name ("Maria" -> u-42), the old fact is about u-42 under another
    relation, reachable by the semantic stage only. The rule compares the new
    fact's canonical name, not the name as written."""
    seen["close"] = {"u-42"}
    old, _ = by_extraction(db, tenant, "u-42", "works_at", "Acme", user="u-42", when=D2)
    ent.declare_alias(db, tenant.id, None, "u-42", None, "Maria", "u-42")
    db.commit()
    seen["calls"].clear()
    direct(db, tenant, "Maria", "is_employed_by", "Globex", None, D5, user="u-42")
    assert [c[0] for c in seen["calls"][-1]["candidates"]] == ["u-42"]
    db.refresh(old)
    assert old.invalid_at is not None
