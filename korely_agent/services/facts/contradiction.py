"""Agent fact contradiction — two-stage, scoped per end user.

On `agent_facts`, scoped by (owner tenant_id, end_user_id): a new fact about
end user A can only invalidate prior facts about end user A. The
opposing-family table and the classifier prompt are memory_core's (until 11
Oct 2026 an earlier product used the same ones on its own tables).

Paper: ConflictBank (Su 2024) — a similarity/structure pre-filter (Stage 1)
before the LLM judge (Stage 2) keeps the false-positive rate down.

Sync (the /v1 endpoint is sync). The old row is invalidated in-session
(invalid_at + invalidated_by_id); the caller commits.

A judge that fails is not a verdict (2026-09-28): a failed call raises
`JudgeUnavailable`, an answer that cannot be read raises `JudgeUnreadable`,
instead of returning what "nothing contradicts" returns. The write of one
memory is one transaction (`service._persist_extracted`), so the raise rolls
its facts back too: nothing is kept, the memory stays `pending` (synchronous
write) or goes to `error` after the worker's attempt, and POST / PATCH
/v1/facts answer 503. Until today the new fact was kept open next to the one
it may contradict, the memory was marked done, and a later write of the same
fact only reconfirmed it, so the judge was never asked again. No provider
configured stays "no contradiction detection": a configuration, not a failure.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from ...core.llm import ModelDidNotAnswer, UnusableAnswer
from .temporal import true_at
from ..memory_core import judge as _judge
from ..memory_core.store import ResolutionUnusable
from ..memory_core import log_safe
from ...db.models import AgentFact

logger = logging.getLogger(__name__)

# There was an NLI pre-filter here, between the candidates and the judge: a
# cross-encoder that ran in a separate ML service of the hosted product and
# dropped the candidates it did not think contradicted, so the judge was asked
# less often. That service is not part of this product and its client was
# always None, so the branch passed every candidate through unchanged, and the
# two settings that selected it (`contradiction_provider="nli"`,
# `contradiction_nli_threshold`) changed nothing. Removed with them. The judge
# sees every candidate, which is what it did anyway.



class JudgeUnavailable(ModelDidNotAnswer):
    """The judge gave no answer (a failed or refused call). Not a verdict."""


class JudgeUnreadable(UnusableAnswer):
    """The judge answered something that cannot be read. Not a verdict."""


# memory_core 5.8.0 (5.5.0 on the branch of 18 Oct): the judge reads
# [another subject] on an earlier fact whose subject is another name than the
# new fact's, and one rule more (contradiction.RULE_OTHER_SUBJECT; Ritter
# et al. EMNLP 2008 §4, argument ambiguity). Found on the identity bench of
# 17 Oct: the user's facts closed by a namesake colleague's. Off: measured on
# 19 Oct, it missed its pre-written criterion (3af3fa5; README, Honest
# limits), and it stays off until a measure says otherwise. Since memory_core
# 6.5.0 it is a question about the facts the extraction read only: for a
# subject the caller named, who may be shown is decided by the candidates
# (`candidates.for_the_judge(subject_named=, named=)`, where the reason is).
_ANOTHER_SUBJECT_TAG = False

# memory_core 6.25.0 (25 Nov 2026; a namesake's fact closed the person's on
# the identity bench of 17 Oct 2026): once a name is
# declared for the end user, the judge's candidates that look past the
# subject keep the person and every other subject apart
# (`candidates.about_the_subject(the_person=)`, Graphiti's invalidation among
# the edges of the resolved entity). A module constant the bench flips, as
# the switch above. OFF: measured on 25 Nov 2026 on the identity bench (22
# scenarios, names declared, gpt-oss-120b through OpenRouter, the extraction
# replayed in both arms): no wrong closing by a namesake on either arm in
# that run, and one right closing lost (i02: "Tomas", the user's first name
# alone, undeclared, no longer met the facts of "Tomas Radek"), must_close passed 10 of 20
# against 11. A name of the person that was not declared is another
# subject to this rule.
_DECLARED_PERSON = False


def _single_value(db, tenant_id, canonical: str, project_id: Optional[UUID],
                    end_user_id: Optional[str]):
    """(holds one value, the object that was measured on) for this tenant's
    learned vocabulary: the tag the judge reads, typed by its example (Lin,
    Mausam, Etzioni, EMNLP 2010 §2). Never a rule that closes on its own
    (memory_core.contradiction: Ritter et al. EMNLP 2008).

    Read from the cluster the store built from its own traffic, classified once
    by the model and cached. There is no list of relations in this codebase: a
    hand-written one cannot contain an insurer's `covered_under`, and the
    relations that matter to a client are exactly the ones nobody anticipated.
    Unknown resolves to (False, None): an unclassified relation is not tagged.
    """
    from ..predicates import single_value

    # The end user's vocabulary (18 Oct 2026): its tag and its example, a
    # value of that end user's own facts, never another end user's or
    # another project's.
    return single_value(db, tenant_id, canonical, project_id=project_id, end_user_id=end_user_id)


def _stage1_candidates(
    db: Session,
    tenant_id: UUID,
    end_user_id: Optional[str],
    subject_label: str,
    predicate_canonical: str,
    *,
    project_id: Optional[UUID] = None,
    exclude_id: UUID,
    new_embedding=None,
    limit: int = 10,
    subject_canonical: Optional[str] = None,
    object_canonical: Optional[str] = None,
    only_arguments: bool = False,
    source: Optional[UUID] = None,
    subject_named: bool = False,
    subject_names: Optional[List[str]] = None,
    before: Optional[datetime] = None,
    agent_id: Optional[str],
) -> List[AgentFact]:
    """The earlier facts the judge weighs against a new one: memory_core
    `candidates.for_the_judge` (piece 2 of the common engine, 0.1.15), the same
    function the hosted product calls. This engine passes who may be a
    candidate, as one condition: the account, the end user and the project.
    The project was the wall this file's own copy once left open on its
    semantic stage, so it is here, once, for every stage.

    `source` is the memory being read (BUG-123): the facts it stated, born from
    it or reconfirmed by it, never enter the list. A memory that lists
    "hobby, developer, team, scale" does not close its own plans
    (memory_core.contradiction rule 3, Graphiti §2.2.3).

    `subject_named` (memory_core 6.5.0): the new fact's subject was named by
    its caller (`entities.named_by_the_caller`: a fact written directly, a
    correction); only the facts about the same canonical subject, or about
    the same two entities, are its candidates (`candidates.for_the_judge`,
    where the reason and its papers are). And for every new fact, a fact a
    caller named enters only about the same subject (`entities.NAMED_FACTS`).

    A memory with no end user (memory_core 6.9.0,
    `memory_core.owner.keeps_to_its_subject`; 6.7.0 for an agent's memory
    alone): the new fact's candidates by meaning are the facts of its own
    subject, and a fact of such a memory is a candidate by meaning of
    another fact only about that fact's subject (`entities.kept_to_their_
    subject`, passed only where there is no end user: a memory with one
    holds no such fact, and its statements stay those of 6.6.0). Until
    6.8.1 this took the write's `agent_id`, which only an agent's memory
    narrowed by.

    One deliberate change on adoption: the structural candidates are no longer
    dropped when their object is farther than 0.20 from the new fact's. That
    filter lived only here; measured on 2026-09-28 it separated nothing ("uses
    Jira" -> "uses Linear" sits at 0.217) and memory_core 2.0.0 removed it.
    tests/test_common_engine_candidates.py proves that is the only difference.

    `subject_names` (memory_core 6.20.0, facts/subjects.py): the names of the
    new fact's subject's group in a memory with no end user, the links of
    `manage review-subjects` followed (`candidates.for_the_judge(
    subject_names=)`). `before`: only facts written before that instant (the
    judgement a write made then, `subjects.rejudge`). Neither given: the
    statements as before.

    `agent_id` (memory_core 6.28.0, a product decision of 26 Nov 2026):
    the agent of the write, always passed (None: the write names none).
    With no end user the candidates are the facts of that agent only, the
    facts with no agent for a write with none (`memory_core.owner.judged_
    within_its_agent`, where the reason and Mem0's documentation are): an
    agent no longer closes another agent's facts. With an end user it
    changes nothing: the end user's facts are weighed together, whichever
    agent wrote them.
    """
    from sqlalchemy import and_

    from ..memory_core import candidates
    from ..memory_core.owner import keeps_to_its_subject
    from .entities import NAMED_FACTS, TABLES as _TABLES, declared_person, kept_to_their_subject, memory_scope

    scope = the_judges_wall(memory_scope(tenant_id, project_id, end_user_id), end_user_id, agent_id)
    if before is not None:
        scope = and_(scope, AgentFact.created_at < before)
    return candidates.for_the_judge(
        db, _TABLES, scope,
        exclude_id=exclude_id,
        subject_label=subject_label,
        predicate_canonical=predicate_canonical,
        subject_canonical=subject_canonical,
        object_canonical=object_canonical,
        is_open=true_at(),
        source=source,
        # Stage 1a-bis only inside one end user: the global scope is too broad
        # to assume one relation family is one entity.
        with_recall=end_user_id is not None,
        vector=new_embedding,
        limit=limit,
        only_arguments=only_arguments,
        subject_named=subject_named,
        # 6.5.0: and a fact the caller named is a candidate of another
        # write, the extraction's too, only about its own subject.
        named=NAMED_FACTS,
        # 6.9.0 (6.7.0 for an agent's memory alone): in a memory with no end
        # user every fact keeps to its subject, both ways. The parameters
        # keep their 6.7.0 names.
        agent_scoped=keeps_to_its_subject(end_user_id),
        agent_scoped_facts=kept_to_their_subject() if end_user_id is None else None,
        subject_names=subject_names,
        # 6.25.0: once a name is declared for the end
        # user, a fact about them and a fact about another subject meet only
        # by subject (`candidates.about_the_subject(the_person=)`).
        the_person=declared_person(db, tenant_id, project_id, end_user_id) if _DECLARED_PERSON else None,
    )


def the_judges_wall(scope: Any, end_user_id: Optional[str], agent_id: Optional[str]) -> Any:
    """The memory's wall (`entities.memory_scope`) and, with no end user,
    the write's agent (memory_core 6.28.0, `owner.judged_within_its_agent`,
    `owner.same_agent_rows`): one condition for every judgement, the
    write's, the batch's, the late vector's and the group's, and for what
    keeps a fact closed after a forget (`given_back._open_beside`)."""
    from sqlalchemy import and_

    from ..memory_core.owner import judged_within_its_agent, same_agent_rows

    if not judged_within_its_agent(end_user_id):
        return scope
    return and_(scope, same_agent_rows(AgentFact.agent_id, agent_id))


def _with_the_group(db: Session, tenant_id: UUID, end_user_id: Optional[str], subject: str,
                    predicate_canonical: str, candidates: List[AgentFact], *, on: Optional[bool] = None,
                    **kw):
    """`candidates` and those that enter only through the group of the new
    fact's subject (memory_core 6.20.0, facts/subjects.extra_candidates),
    with the links each of those came through. With the switch off, an end
    user or no group: `candidates` as they are, and no statement more than
    the read of the wall's links (none with the switch off). `on`: the
    switch, None reads it (`subjects.extra_candidates`)."""
    from . import subjects

    extra, through = subjects.extra_candidates(
        db, tenant_id, kw.get("project_id"), end_user_id, subject, kw.get("subject_canonical"), candidates,
        lambda names: _stage1_candidates(db, tenant_id, end_user_id, subject, predicate_canonical,
                                         subject_names=names, **kw), on=on)
    return candidates + extra, through


def judge_through_the_group(db: Session, tenant_id: UUID, project_id: Optional[UUID], fact: AgentFact, *,
                            on: Optional[bool] = None) -> Optional[int]:
    """One open fact of a memory with no end user against the earlier facts
    that enter only through its subject's group (memory_core 6.20.0,
    facts/subjects.rejudge): the judgement its write would have made had the
    links existed, those of its write left out, as its write's judge reads
    them (`_stage1_candidates`, the facts written before it). One call of the
    project's judge when there is any; the closings in-session, recorded on
    the links (`subjects.record`). Returns how many were recorded, or None
    when there was nothing to ask. `on`: the switch, None reads it. A judge
    that gives no answer raises JudgeUnavailable, an answer that cannot be
    read JudgeUnreadable."""
    from . import subjects

    y = fact
    kw = dict(project_id=project_id, exclude_id=y.id, new_embedding=y.embedding,
              subject_canonical=y.subject_canonical, object_canonical=y.object_canonical,
              source=y.source_memory_id, before=y.created_at,
              # memory_core 6.28.0: the facts of its own agent.
              agent_id=y.agent_id)
    base = _stage1_candidates(db, tenant_id, None, y.subject_label, y.predicate_canonical, **kw)
    cands, through = _with_the_group(db, tenant_id, None, y.subject_label, y.predicate_canonical, base,
                                     on=on, **kw)
    extra = [c for c in cands if c.id in through]
    if not extra:
        return None
    nf = {"id": y.id, "end_user_id": None, "subject": y.subject_label,
          "subject_canonical": y.subject_canonical, "predicate_raw": y.predicate_raw,
          "predicate_canonical": y.predicate_canonical, "object": y.object_label,
          "object_canonical": y.object_canonical, "valid_from": y.valid_from,
          "source_memory_id": y.source_memory_id}
    closed = _judge.batch(
        db, AgentFact, [nf], gather=lambda _nf: extra,
        single_value_of_new=lambda _nf: _single_value(db, tenant_id, y.predicate_canonical, project_id, None),
        classify=lambda groups: _classify_batch(groups, db, tenant_id),
        is_open=true_at(), ensure=_ensure(db, tenant_id, project_id, None))
    return subjects.record(db, y.id, closed.get(y.id, []), through)


def _ensure(db: Session, tenant_id: UUID, project_id: Optional[UUID], end_user_id: Optional[str]):
    """The judges' `ensure` (memory_core 3.3.0): with the vocabulary's lazy
    votes on (`predicates.LAZY_SINGLE_VALUE`), the relation a new fact shares
    with a candidate is voted now if it is still undecided; None otherwise,
    and the judges read the tags as before. `examples` (3.3.0, 8 Oct): the values
    actually in conflict, the new fact's object and the candidate's, by which
    the vote is typed instead of the relation's birth example."""
    from .. import predicates

    if not predicates.LAZY_SINGLE_VALUE:
        return None
    return lambda canonical, examples=(): predicates.decide_if_undecided(
        db, tenant_id, canonical, examples=examples, project_id=project_id, end_user_id=end_user_id)


def _classify(
    candidates: List[AgentFact],
    new_subject: str,
    new_predicate_raw: str,
    new_object: str,
    db: Session,
    tenant_id: UUID,
    model_name: str,
    new_predicate_canonical: str = "",
    new_functional: bool = False,
    new_past: bool = False,
    new_example: Optional[str] = None,
    new_evidence: Optional[str] = None,
    new_since: Optional[datetime] = None,
    project_id: Optional[UUID] = None,
    end_user_id: Optional[str] = None,
    new_subject_canonical: Optional[str] = None,
) -> tuple[List[UUID], dict]:
    """The judge (memory_core `judge.judge`, piece 3 of the common engine,
    0.1.15): the prompt, the reading of the verdict and what counts as no
    verdict are memory_core's, shared with the hosted product. This engine
    keeps the call (provider, model, usage stage) and the tag each candidate
    carries, read from the tenant's vocabulary (typed functionality, Lin 2010
    §2): without it the judge read "customer on_plan Business" against
    "customer on_plan Standard" the way it reads "likes fish" against "likes
    meat". A failed call raises JudgeUnavailable, an answer that cannot be
    read JudgeUnreadable: neither is "nothing contradicts" (module docstring).
    """
    from ...core.llm import complete_whole, extract_json, get_provider as get_client
    from ...services import llm_usage

    client = get_client()
    if client is None or not candidates:
        return [], {}

    def call(prompt: str, max_tokens: int) -> str:
        with llm_usage.stage(llm_usage.CONTRADICTION):
            # Cut at its ceiling, asked again with more room (1.0.38).
            return complete_whole(
                client, [{"role": "user", "content": prompt}],
                model=model_name, max_tokens=max_tokens, temperature=0.0, json_only=True,
            ).text

    try:
        return _judge.judge(
            call, candidates, new_subject, new_predicate_raw, new_object,
            single_value=lambda c: _single_value(db, tenant_id, c.predicate_canonical, c.project_id,
                                                    c.end_user_id),
            functional=new_functional, past=new_past, example=new_example,
            # memory_core 2.12.0: since when the new fact holds, shown as
            # the earlier facts show theirs (never for a past statement).
            extract_json=extract_json, evidence=new_evidence, since=new_since,
            relation=new_predicate_canonical or None,
            ensure=_ensure(db, tenant_id, project_id, end_user_id),
            subject_canonical=new_subject_canonical, mark_other_subject=_ANOTHER_SUBJECT_TAG,
        )
    except _judge.JudgementMissing as e:
        raise JudgeUnavailable(str(e)) from e
    except _judge.JudgementUnreadable as e:
        raise JudgeUnreadable(str(e)) from e


def detect_and_invalidate(
    db: Session,
    tenant_id: UUID,
    *,
    project_id: Optional[UUID] = None,
    end_user_id: Optional[str],
    new_fact_id: UUID,
    new_subject: str,
    new_predicate_raw: str,
    new_predicate_canonical: str,
    new_object: str,
    new_embedding=None,
    new_valid_from: Optional[datetime] = None,
    model_name: Optional[str] = None,
    new_subject_canonical: Optional[str] = None,
    agent_id: Optional[str],
    new_valid_from_is_a_day: bool = False,
    new_object_canonical: Optional[str] = None,
    functional: bool = False,
    example: Optional[str] = None,
    new_past: bool = False,
    source: Optional[UUID] = None,
    evidence: Optional[str] = None,
    subject_named: bool = False,
    among_its_subject: bool = False,
    ask_only: bool = False,
) -> List[UUID]:
    """Detect + invalidate prior facts the new fact supersedes. Returns the
    invalidated ids. Mutates rows in-session (caller commits). No LLM call when
    Stage 1 finds no candidates (the common case). `new_embedding` enables the
    semantic candidate path (Stage 1b) — without it, structural-only.

    `functional` (the learned vocabulary says the relation holds one value at a
    time) becomes the [single-valued] tag on the new fact, typed by `example`:
    the judge reads it next to both objects. It is never a rule that closes on
    its own (typed functionality, Lin 2010; Ritter 2008: see
    memory_core.contradiction). `new_past`: a past statement, which
    already closed its own twin; only the facts about the same two entities
    are gathered. `source`: the memory the new fact was read from; its own
    facts are never candidates (BUG-123, memory_core.contradiction rule 3).
    `evidence`: the words of that memory the fact was read from, shown to the
    judge under the new fact (memory_core 2.5.0, contradiction rule 4): "also
    works part-time at Google" next to "works at Apple" is not a job change
    (BUG-200, Ritter et al. EMNLP 2008). Never stored on the fact.
    `subject_named` (memory_core 6.5.0): the caller named the subject (a
    direct write, a correction), so the facts of other subjects are not
    candidates (`_stage1_candidates`). With no end user (memory_core 6.9.0)
    its facts meet by meaning only about one subject (`_stage1_candidates`).
    `among_its_subject`, `ask_only` (memory_core 6.19.0,
    `writing._the_value_it_replaced`): the facts of the new fact's own
    subject are its candidates, with the stages of a subject its writer
    named, even for a past statement; and the judge's answer comes back
    with nothing closed.
    `agent_id` (memory_core 6.28.0): the write's agent, always passed (None:
    none); with no end user only its facts are candidates
    (`_stage1_candidates`).

    Raises JudgeUnavailable / JudgeUnreadable when there were candidates and
    the judge gave no verdict (module docstring)."""
    gathered = dict(
        project_id=project_id,
        exclude_id=new_fact_id, new_embedding=new_embedding,
        subject_canonical=new_subject_canonical,
        object_canonical=new_object_canonical,
        only_arguments=new_past and not among_its_subject,
        source=source,
        subject_named=subject_named or among_its_subject,
        agent_id=agent_id,
    )
    candidates = _stage1_candidates(db, tenant_id, end_user_id, new_subject, new_predicate_canonical,
                                    **gathered)
    through: dict = {}
    if not subject_named:
        # A subject its caller named meets its own facts only (6.5.0): no group.
        candidates, through = _with_the_group(db, tenant_id, end_user_id, new_subject,
                                              new_predicate_canonical, candidates, **gathered)
    if not candidates:
        return []

    # The judge follows the PROVIDER when no model was asked for: None lets
    # each provider pick its own default (Gemini: EXTRACTION_MODEL, the same
    # as before; Mistral-compatible: MISTRAL_EXTRACTION_MODEL). Resolving to
    # settings.extraction_model here sent a Gemini model name to Scaleway,
    # and every write with a candidate died on 422 (EU bench, 2026-10-02).
    invalidated, _reasons = _classify(
        candidates, new_subject, new_predicate_raw, new_object,
        db, tenant_id, model_name,
        new_predicate_canonical=new_predicate_canonical,
        new_functional=functional, new_past=new_past, new_example=example,
        new_evidence=evidence, new_since=new_valid_from, project_id=project_id,
        end_user_id=end_user_id,
        new_subject_canonical=new_subject_canonical,
    )
    if not invalidated or ask_only:
        # memory_core 6.19.0: an answer asked for, nothing closed.
        return list(invalidated or [])

    # Bi-temporal (Graphiti valid_time): the old fact stops being true when the
    # superseding fact's valid time starts, its event date, not when the
    # contradiction was ingested; that is what makes `as_of` right. memory_core
    # `judge.close`, which also writes the closings at once, so a second
    # closing of the same fact in this transaction finds it closed (BUG-189).
    if new_valid_from_is_a_day and new_valid_from is not None:
        closed = _close_from_a_day(db, invalidated, candidates, new_fact_id, new_valid_from)
    else:
        closed = _judge.close(db, AgentFact, invalidated, new_fact_id, new_valid_from, is_open=true_at())
    if through:
        from . import subjects

        subjects.record(db, new_fact_id, closed, through)
    return closed


def _close_from_a_day(db: Session, invalidated: List[UUID], candidates: List[AgentFact],
                      new_fact_id: UUID, day: datetime) -> List[UUID]:
    """The closings of a fact whose `valid_from` was given as a day and no
    time (`korely add-fact --valid-from 2026-11-06`,
    written at 21:08 to correct a fact read at 20:56 the same day). Such a
    start is midnight, and a fact that began later THAT day looked later than
    the correction: `judge.close` kept it true and filed the correction as
    history. Against a fact that began on the same day, a start known only
    to the day is the same valid time, so the newer information wins: the
    fact closes at its own start, as `judge.close` closes on a tie, the rule
    of Graphiti ("Graphiti consistently prioritizes new information when
    determining edge invalidation", Rasmussen et al. 2025, arXiv:2501.13956,
    section 2.2.3). Every other fact is closed as before, from the day.

    The starts are read off `candidates`, the rows the judge was shown and
    whose ids it returned (memory_core `judge.judge` answers with candidates
    only), so no table is read again here; an id not among them is closed
    from the day, as before."""
    from datetime import timezone

    from ...core.times import aware

    def _utc(v):
        # The day is a UTC day (temporal.parse_moment): a valid_from read back
        # from Postgres comes in the session's time zone, so both are brought
        # to UTC before their dates are compared (the review of 25 Nov 2026:
        # with the session in Asia/Tokyo the correction still lost, in America/New_York
        # it closed a fact of the next UTC day).
        a = aware(v)
        return None if a is None else a.astimezone(timezone.utc)

    day0 = _utc(day)
    same_day, other = {}, []
    started = {str(c.id): c.valid_from for c in candidates}
    for fid in invalidated:
        born = _utc(started.get(str(fid)))
        if born is not None and born > day0 and born.date() == day0.date():
            same_day.setdefault(born, []).append(fid)
        else:
            other.append(fid)
    closed = _judge.close(db, AgentFact, other, new_fact_id, day0, is_open=true_at()) if other else []
    for born, ids in sorted(same_day.items()):
        closed += _judge.close(db, AgentFact, ids, new_fact_id, born, is_open=true_at())
    return closed


def detect_and_invalidate_batch(
    db: Session,
    tenant_id: UUID,
    *,
    new_facts: List[dict],
    project_id: Optional[UUID] = None,
    model_name: Optional[str] = None,
) -> dict:
    """The judgement of one memory's write in one call (memory_core
    `judge.batch`), as the hosted product judges in production (its
    `detect_and_invalidate_batch`): every new current fact against the facts
    that existed before the write, never its batch-mates, closings
    bi-temporal by `judge.close`. A past statement is still judged as it
    is written (`writing.write_fact`). `new_facts`:
    `worker._new_for_the_batch`. Returns {new fact id: [closed ids]}.

    The candidates are the per-fact path's (`_stage1_candidates`, scope and
    BUG-123 source included): only the call changes. A judge that gives no
    answer raises JudgeUnavailable, an answer that cannot be read
    JudgeUnreadable; either rolls the memory's write back, as on the per-fact
    path. With no end user (memory_core 6.9.0) the facts meet by meaning one
    subject at a time (`_stage1_candidates`), and since 6.28.0 only with the
    facts of its own agent."""
    through: dict = {}

    def gather(nf):
        kw = dict(project_id=project_id, exclude_id=nf["id"], new_embedding=nf.get("embedding"),
                  subject_canonical=nf.get("subject_canonical"),
                  object_canonical=nf.get("object_canonical"), source=nf.get("source_memory_id"),
                  # memory_core 6.28.0 (`worker._new_for_the_batch`): the
                  # fact's agent, never guessed when it is missing.
                  agent_id=nf["agent_id"])
        cands = _stage1_candidates(db, tenant_id, nf.get("end_user_id"), nf["subject"],
                                   nf["predicate_canonical"], **kw)
        # memory_core 6.20.0: and through the group of its subject.
        cands, through[nf["id"]] = _with_the_group(db, tenant_id, nf.get("end_user_id"), nf["subject"],
                                                   nf["predicate_canonical"], cands, **kw)
        return cands

    closed = _judge.batch(
        db, AgentFact, new_facts,
        gather=gather,
        single_value_of_new=lambda nf: _single_value(db, tenant_id, nf["predicate_canonical"],
                                                     project_id, nf.get("end_user_id")),
        classify=lambda groups: _classify_batch(groups, db, tenant_id, model_name),
        is_open=true_at(),
        # One memory's facts: one end user, whose vocabulary votes.
        ensure=_ensure(db, tenant_id, project_id,
                           new_facts[0].get("end_user_id") if new_facts else None),
    )
    if any(through.values()):
        from . import subjects

        for nf in new_facts:
            if through.get(nf["id"]):
                subjects.record(db, nf["id"], closed.get(nf["id"], []), through[nf["id"]])
    return closed


def _classify_batch(groups: list, db: Session, tenant_id: UUID,
                    model_name: Optional[str] = None) -> dict:
    """One call for several new facts (memory_core `judge.judge_batch`),
    on this install's provider, with the tag each candidate carries.
    `groups`: [{"new_id": str, "new_text": str, "candidates": [AgentFact]}];
    returns {new_id: [existing id]}. No provider configured is no judgement,
    as for `_classify`; a failed call raises JudgeUnavailable, an answer that
    cannot be read JudgeUnreadable."""
    from ...core.llm import complete_whole, extract_json, get_provider as get_client
    from ...services import llm_usage

    if not groups:
        return {}
    client = get_client()
    if client is None:
        return {}

    def call(prompt: str, max_tokens: int) -> str:
        with llm_usage.stage(llm_usage.CONTRADICTION):
            # Cut at its ceiling, asked again with more room (1.0.38).
            return complete_whole(
                client, [{"role": "user", "content": prompt}],
                model=model_name, max_tokens=max_tokens, temperature=0.0, json_only=True,
            ).text

    try:
        return _judge.judge_batch(
            call, groups,
            single_value=lambda c: _single_value(db, tenant_id, c.predicate_canonical, c.project_id,
                                                    c.end_user_id),
            extract_json=extract_json, mark_other_subject=_ANOTHER_SUBJECT_TAG,
        )
    except _judge.JudgementMissing as e:
        raise JudgeUnavailable(str(e)) from e
    except _judge.JudgementUnreadable as e:
        raise JudgeUnreadable(str(e)) from e


def judge_after_the_vector(db: Session, tenant_id: UUID, fact: AgentFact, vector) -> dict:
    """The judgements a fact's late vector makes possible (memory_core
    `judge.after_the_vector`, 2026-10-06; the hosted product's since
    2026-09-28).

    A fact whose vector could not be computed on its write was judged on the
    SQL candidates only: the semantic stage needs the vector, and every fact
    written after it could not find it by meaning either. When the vector
    drain gets it (services/vectors.py), before it is stored: the fact against
    the older facts only the semantic stage finds, and the later facts near it
    that could not find it, in one batched call. Closings in-session, the
    caller commits them with the vector. Returns {closing fact id: [closed
    ids]}.

    A judge that gives no answer raises JudgeUnavailable: the vector is not
    stored and the drain retries both. An answer that cannot be read is logged
    and nothing is closed: at temperature 0 a retry would pay for the same
    answer, and the fact keeps what its write already judged."""
    from .entities import NAMED_FACTS, TABLES, declared_person, kept_to_their_subject, memory_scope

    try:
        return _judge.after_the_vector(
            # memory_core 6.28.0: with no end user, the facts of the late
            # fact's agent only, both ways (the older ones it is weighed
            # against and the later ones weighed against it).
            db, TABLES, the_judges_wall(memory_scope(tenant_id, fact.project_id, fact.end_user_id),
                                         fact.end_user_id, fact.agent_id),
            fact, vector, is_open=true_at(),
            single_value=lambda c: _single_value(db, tenant_id, c.predicate_canonical, c.project_id,
                                                    c.end_user_id),
            classify=lambda groups: _classify_batch(groups, db, tenant_id),
            ensure=_ensure(db, tenant_id, fact.project_id, fact.end_user_id),
            # memory_core 6.5.0: the late judgements the writes would have
            # made, a fact a caller named meeting only its own subject.
            named=NAMED_FACTS,
            # 6.9.0 (6.7.0 for an agent's memory alone): and every fact of a
            # memory with no end user, as `_stage1_candidates` passes it.
            agent_scoped_facts=kept_to_their_subject() if fact.end_user_id is None else None,
            # 6.25.0: the person, as `_stage1_candidates` passes it.
            the_person=(declared_person(db, tenant_id, fact.project_id, fact.end_user_id)
                        if _DECLARED_PERSON else None),
        )
    except (JudgeUnreadable, ResolutionUnusable) as e:
        # memory_core 3.3.0: the lazy votes can ask the vocabulary here, and an
        # answer it cannot read is no verdict, as the judge's own (the hosted
        # product catches the same pair).
        logger.error("[agent_facts] the late judgement of fact %s could not be read, the vector "
                     "is stored without it: %s", fact.id, log_safe.error(e))
        return {}
