"""Writing one fact in an Agents memory (memory_core, the shared-code plan,
second half, 2026-09-28).

Until today `_persist_one` existed twice, in the Cloud's `agent_facts/service.py`
and in korely-agent `services/facts/service.py`: 235 and 226 lines with the
same steps and different comments. Every rule it applies was already here
(aliases, reconfirmation, tense, identity); the order in which they are
applied, which is itself a rule, was not.

The order:
  1. canonical names from the memory's aliases (`identity.canonicals_of_the_triple`);
  2. the same canonical triple already true in this memory is RECONFIRMED
     (Graphiti edge dedup), not written again; a plan said again as done
     ends there and the done fact is a new row, judged as any (3.0.0);
  2-bis. (6.14.0) a statement whose triple the caller's last word says is
     over, of a triple this memory stated, is not written again when the
     memory is written again (`held_by_hand`, given by the engine);
  3. a past statement closes its true twin, or reconfirms the closed history,
     or is stored born closed, ended when it was said (Graphiti §2.2.3: the
     tense is the model's; 2.12.0 for the end); with `past_event` (6.19.0,
     off unless the engine passes it) a past statement that is a row of its
     own may also end ANOTHER value of its subject, when two readings name
     that value (`_the_value_it_replaced`);
  4. the insert, in a savepoint: losing the race to the unique index means
     reconfirming the winner, not failing the caller's transaction;
  5. an explicit identity statement creates the alias by itself
     (`identity.alias_rule_from_identity`);
  6. the vector BEFORE the judge, so the semantic candidates can find it;
  6-bis. (2.8.0, off by default) a restatement in other words of a fact
     already true flows into it (`twins.py`, `store.merge_into`);
  7. the judge (or nothing, when the caller judges the whole write at once).

What the engine passes: the memory (`Memory`), the engine's columns for a
new row (owner, project, end user, agent, run), the relation already
resolved by its vocabulary, `is_open` (what "true now" means), `embed(f)`
(its embedding, with its own retry of a failed call) and `judge(g)` (its
contradiction check, which knows its scope). Nothing is committed here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from . import store as _store
from . import judge as _judge
from . import twins as _twins
from . import log_safe as _log
from . import thresholds as _thresholds
from .contradiction import arguments_swapped, compatible_objects
from .identity import SOURCE_IDENTITY_FACT, canonicals_of_the_triple, alias_rule_from_identity
from .fact_time import effective_tense

logger = logging.getLogger(__name__)


def _aware(v: Optional[datetime]) -> Optional[datetime]:
    if v is None:
        return None
    return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ToJudge:
    """What the contradiction check needs about a fact just written."""
    fact_id: Any
    subject: str
    subject_canonical: str
    predicate: str
    predicate_canonical: str
    obj: str
    object_canonical: str
    when: Optional[datetime]
    source: Any
    past: bool = False          # only the facts about the same two entities
    vector: Any = None
    functional: bool = False
    example: Optional[str] = None
    # The words the fact was read from (2.5.0), for the judge only.
    evidence: Optional[str] = None
    # 6.19.0 (`write_fact(past_event=)`). `among_its_subject`: the fact is
    # weighed against the facts of its own subject (the stages of a subject
    # its writer named, `candidates.for_the_judge(subject_named=)`).
    # `ask_only`: the judge answers the facts it names, and closes none.
    among_its_subject: bool = False
    ask_only: bool = False


def write_fact(
    db,
    tables: _store.Tables,
    mem: _store.Memory,
    *,
    columns: dict,
    relation: _store.Relation,
    is_open: Any,
    embed: Callable[[Any], None],
    judge: Callable[[ToJudge], List[Any]],
    subject: str,
    subject_type: str,
    predicate_raw: str,
    obj: str,
    object_is_literal: bool,
    confidence: float,
    source_memory_id: Any,
    model_used: Optional[str],
    valid_from: Optional[datetime] = None,
    text_valid_from: Optional[datetime] = None,
    tense: str = "current",
    defer: bool = False,
    arrival: Optional[datetime] = None,
    evidence: Optional[str] = None,
    twins: bool = False,
    day_of_the_words: Optional[datetime] = None,
    held_by_hand: Optional[Callable[[str, str, str], Optional[Any]]] = None,
    past_event: bool = False,
) -> Tuple[Any, List[Any]]:
    """Write one fact; returns (fact, the ids it closed). A fact closed as
    past comes back with its `invalid_at` set. `valid_from`: the caller's
    timestamp, else the date the text states (`text_valid_from`), else the
    moment the memory's text arrived (`arrival`, `worker.arrival`), else now.
    `defer`: write and embed, but leave the judge to the caller (the
    batched judgement of a whole write, `judge.batch`).

    `arrival` (2026-09-29, 2.3.0): with deferred extraction a fact was dated
    when the worker got to it, 1 to 22 seconds after the write (the public
    conformance test of 29 Sep), and an `as_of` in that gap returned the old
    value. The docs promise the moment the new information arrived.

    `evidence` (2.5.0): the words of the memory the fact was read from
    (`ExtractedFact.evidence`). They reach the judge in `ToJudge` and are
    not written on the row: a forgotten memory leaves no copy of its text.

    `twins` (2.8.0, off): a current or planned fact that restates in other
    words a fact already true in the memory flows into it once its vector is
    known (`twins.find_twin`, `store.merge_into`): no new row, no
    judge, the kept row returned as a reconfirmation is. It also needs the
    thresholds measured on the embedding model in use (`thresholds.Twin`);
    see `twins.py` for what must come before it is turned on.

    The time of a fact said in the past (2.12.0, code audit of 6 Oct).
    Zep/Graphiti (Rasmussen et al. 2025, arXiv:2501.13956, section
    2.2.3) read the time of a fact from its episode with the episode's
    reference time t_ref ("The system extracts temporal information about
    facts from the episode context using t_ref"), and "t_valid and t_invalid
    ∈ T track the temporal range during which facts held true". A statement
    in the past tense ("I used to work at Acme", said in 2023) says that the
    state had ended by the time it was said: t_invalid is at most the moment
    of the saying (`stated`: the caller's timestamp, else when the text
    arrived). Until 2.12.0 such a fact was stored true from that moment
    until the write ran, so `as_of=2024` returned it as the present. Now:
      - a new past fact closes when it was said (never before its own
        valid_from, which stays what the code makes of it: the caller's
        timestamp, else the date the text states, else the arrival; a
        statement with no start of its own is history from the first day,
        true on no date, as the personal-memory engine stores it);
      - its true twin closes when the statement was said, not at the date
        the text gives (that date is when the state BEGAN, per
        `extraction.RULES_DATE`), and not at all when the twin began
        after the statement was said: the twin is a later period, true now,
        and the statement becomes a row of history of its own (the rule of
        `judge.close`);
      - what the statement can still make false (`_judge_the_past`)
        stops being true when it was said.

    A plan said again as done (3.0.0, the decision of 6 Oct). The plan
    row is CLOSED when the plan came true, and the done fact is a NEW row,
    true from then, written and judged as any new current fact ("uses Jira"
    stayed true next to "uses Linear" until 2.12.0). Zep/Graphiti (Rasmussen
    et al. 2025, arXiv:2501.13956, section 2.2.3): "t_valid and t_invalid ∈
    T track the temporal range during which facts held true", and a newer
    edge "invalidates the affected edges by setting their t_invalid to the
    t_valid of the invalidating edge": only the end is written, the
    invalidated edge keeps its start, "while maintaining both current
    relationship states and historical records of relationship evolution
    over time". The plan held as an intention from its own date until the
    realization; `as_of` before the realization returns the plan, after it
    the done fact. Until 3.0.0 (2.11.0) one row held both: promoted to
    current in place, with the plan's date as the start of the state, so an
    `as_of` between plan and change returned both trackers; 2.12.0 moved
    that start to the realization and lost the plan instead. The plan ends
    where the done fact begins, never before its own start (the rule of
    `judge.close`); `invalidated_by_id` is the done fact, and the
    plan's id is among the ids this write closed. A plan that already ends
    before then (a known end date) is left as it is: the two do not overlap. In a batched write (`defer`) the done fact
    is one of the facts born from the memory, judged with them. A plan in
    other words that comes true (`twins`, off) ends the same way, closed
    and not merged.

    2.9.0 to 3.3.1 also had `dall_assistente`, a fact said only in the
    assistant's turns closed nothing (LongMemEval 38146c39). Measured with
    the turns rule of the extractor, LoCoMo conversation 26 with Gemini 3.1
    Flash-Lite as the writer: 127 against 129 of 152 (p 0.80), no gain; the
    adapter's switch left in korely-agent 0679017, the engine's code with
    4.0.0 (12 Oct 2026).

    `held_by_hand(subject, relation, object)` (6.14.0, the founder's
    decision of 15 Nov 2026: a forget is the person's will, and an edit of
    the text does not undo it): the engine's answer, for a canonical triple
    (`subject` and `object` as the alias map names them, `relation` the
    resolved one), to "is this triple one the memory being written stated,
    and whose last word, by hand, is that it is over?" (a forget, a
    correction, a past statement posted by hand, later than any row of it
    posted by hand), with the row of that word, or None. The engine reads
    it once a write, before the write, from
    `store.rows_of_the_triples_stated_by`. A current or planned statement of such a
    triple, when nothing of the triple is true now (no row to reconfirm and
    no plan come true: those are read first, as before), is not written
    again: the row comes back as it is, not reconfirmed (its count, sources
    and dates stay), no new row, no judge, so nothing it would have closed
    closes, and the write leaves it alone (`store.mark_held`). The fact
    posted again (POST /v1/facts) is the caller's later word, and the
    statement is written as any while it stands; posted and forgotten
    again, it is held again. A row a memory wrote is no word of the
    caller's, whatever became of it. A past statement is not held: it says
    the fact is over, as the caller did, and is written and judged as one.
    What the other products do with an edit: Mem0's `Memory.update`
    overwrites the text and its vector and extracts nothing from it
    (mem0ai/mem0 f1791da, mem0/memory/main.py `update` and
    `_update_memory`: the embedding of the new text, `vector_store.update`
    and an "UPDATE" history row, no model call), and its documentation says
    "Mem0 overwrites the stored value and adjusts indexes" (docs.mem0.ai,
    "Update Memory Operation"), so nothing deleted comes back through an
    edit. Zep lets an episode's metadata be changed, never its text
    (help.getzep.com/v4, "Update episode": the body is `metadata` alone),
    and "A deletion is permanent. Zep has no undelete operation. ... When
    you ingest the same content again, Zep creates new objects"
    (help.getzep.com/v4/context-retention, "Deletion behavior").
    Supermemory forgets by a soft delete, "Forgotten memories are excluded
    from search results but preserved" (supermemory.ai/docs, "Memory
    Operations", "Forget Memories"), and documents no call that brings one
    back. The same canonical triple is reconfirmation's (step 2, Graphiti
    edge dedup), so a restatement the vocabulary brings to another relation
    is another fact, as it is for a reconfirmation."""
    F = tables.Fact
    tense = effective_tense(tense)
    predicate_canonical = relation.canonical
    alias_map = _store.memory_alias_map(db, tables, mem)
    subject = subject[:200]
    obj = obj[:500]
    sc, oc = canonicals_of_the_triple(subject, predicate_canonical, obj, alias_map)
    t = _the_clocks(tense, relation, valid_from=valid_from, text_valid_from=text_valid_from,
                 arrival=arrival, day_of_the_words=day_of_the_words)

    # Step 2: the same triple already true is reconfirmed; a plan said again
    # as done is closed, and the done fact is written below.
    existing, came_true = _already_true(db, tables, mem, sc, predicate_canonical, oc, is_open=is_open,
                                   tense=tense, start=t.start)
    if existing is not None and tense != "past":
        _store.reconfirm(tables, existing, source_memory_id, when=t.stated)
        if existing.predicate_family is None:
            existing.predicate_family = relation.family
        db.flush()
        return existing, []

    # Step 2-bis (6.14.0): the caller's last word that a triple of this
    # memory is over stands against the memory's text written again
    # (`held_by_hand`). Only when nothing of the triple is true now: a row
    # to reconfirm, or a plan of it that comes true now (`came_true`), is
    # handled above as before.
    if existing is None and not came_true and tense != "past" and held_by_hand is not None:
        held = held_by_hand(sc, predicate_canonical, oc)
        if held is not None:
            _store.mark_held(db, held.id)
            return held, []

    def _judge_the_past(past_fact) -> List[Any]:
        """What a past statement can still make false: the facts about the
        same two entities (another relation, or the same one the other way
        round). Found by the MCP acceptance run: "Globex no longer uses Salesforce"
        closed its twin and left "Salesforce serves_as_crm_for Globex" true."""
        return judge(ToJudge(
            fact_id=past_fact.id, subject=subject, subject_canonical=sc,
            predicate=predicate_raw, predicate_canonical=predicate_canonical,
            # 2.12.0: they stop being true when the statement was said.
            obj=obj, object_canonical=oc, when=t.stated,
            source=source_memory_id, past=True, evidence=evidence,
        ))

    # Step 3: a past statement closes its true twin or reconfirms its history.
    if tense == "past":
        row = _the_past(db, tables, mem, existing, sc, predicate_canonical, oc,
                           stated=t.stated, source=source_memory_id)
        if row is not None:
            return row, _judge_the_past(row)

    fact = _new_row(F, columns, t, came_true, tense=tense, relation=relation,
                       subject=subject, sc=sc, subject_type=subject_type, predicate_raw=predicate_raw,
                       obj=obj, oc=oc, object_is_literal=object_is_literal, confidence=confidence,
                       source_memory_id=source_memory_id, model_used=model_used)

    # Step 4: the insert, in a savepoint.
    theirs = _insert(db, tables, mem, fact, sc, predicate_canonical, oc, t, came_true,
                        source=source_memory_id)
    if theirs is not None:
        return theirs, []

    # 3.0.0: the plan that came true is closed by the done fact.
    for plan in came_true:
        plan.invalidated_by_id = fact.id
    if came_true:
        db.flush()

    # Step 5: an explicit identity statement makes its alias.
    if tense == "current":
        _alias_from_identity(db, tables, mem, fact, predicate_canonical, subject, obj,
                           subject_type=subject_type, confidence=confidence)

    # Step 6: the vector BEFORE the judge, so the semantic candidates (Stage
    # 1b) can use it. Detection used to run first, leaving the vector path
    # blind.
    embed(fact)
    if tense == "past":
        # The facts about the same two entities, judged as before 6.19.0:
        # the same candidates, the same question, the same closings.
        ended = _judge_the_past(fact)
        if past_event:
            ended += _the_value_it_replaced(
                db, tables, mem, fact, judge, relation, t, is_open=is_open, sc=sc, oc=oc,
                subject=subject, predicate_raw=predicate_raw, obj=obj,
                source=source_memory_id, evidence=evidence, already=ended,
            )
        return fact, ended
    # Step 6-bis (off by default): a restatement in other words flows into
    # the fact already true.
    if twins:
        twin = _twin(db, tables, mem, fact, relation, came_true, is_open=is_open, tense=tense,
                           start=t.start, source=source_memory_id)
        if twin is not None:
            return twin, [p.id for p in came_true]
    own = [p.id for p in came_true]
    # Step 7: the judge, or nothing when the caller judges the whole write.
    if defer or tense != "current":
        return fact, own
    return fact, judge(ToJudge(
        fact_id=fact.id, subject=fact.subject_label, subject_canonical=fact.subject_canonical,
        predicate=fact.predicate_raw, predicate_canonical=predicate_canonical,
        obj=fact.object_label, object_canonical=fact.object_canonical,
        when=fact.valid_from, vector=fact.embedding,
        functional=bool(relation.is_functional), example=relation.example,
        # BUG-123: the facts this memory just stated do not close each other
        # (a list of alternatives is not a change).
        source=source_memory_id,
        evidence=evidence,
    )) + own


def _same_two_entities(row, sc: str, oc: str) -> bool:
    """A fact about the new fact's own two entities: its subject with a
    compatible object, or the two the other way round. The rule of stages 1c
    and 1d (`candidates._same_arguments`), the facts a past statement was
    judged against before 6.19.0."""
    rs = row.subject_canonical or row.subject_label
    ro = row.object_canonical or row.object_label
    return ((rs or "").strip().lower() == (sc or "").strip().lower() and compatible_objects(oc, ro)) \
        or arguments_swapped(sc, oc, rs, ro)


def _the_value_it_replaced(db, tables: _store.Tables, mem: _store.Memory, fact,
                           judge: Callable[[ToJudge], List[Any]],
                           relation: _store.Relation, t: "_Clocks", *, is_open: Any, sc: str, oc: str,
                           subject: str, predicate_raw: str, obj: str, source: Any,
                           evidence: Optional[str], already: List[Any]) -> List[Any]:
    """6.19.0 (`write_fact(past_event=)`): the other value of its subject a
    past statement replaced, closed only when two readings name it; the ids
    closed. Nothing else changes: what the statement ends about its own two
    entities was judged just before, as before 6.19.0 (`_judge_the_past`).

    The finding (Korely Cloud 1.0.33, 18 Nov 2026), told here with a made-up
    example of the same shape: "Tom is on the Gold plan", then "Tom
    downgraded from Gold to Basic", left Gold current. With the Cloud's
    European writer (gpt-oss-120b on Scaleway) the only fact was the event,
    in the past ("Tom downgrades_to Basic"): born closed and weighed only
    against the facts about Tom and Basic, so the judge was never asked
    about Gold. Recorded that day on 50 cases written
    before, two extractions: 14 and 18 of the 26 changes told as events ended
    wrong that way, with no call to the judge. Gemini 3.1 Flash-Lite (the
    default region) wrote the new state in the present, 26 of 26 right.

    Why the tense must not decide who the judge sees. TimeML (Pustejovsky
    et al., IWCS-5 2003, the EVENT classes) keeps an OCCURRENCE apart from the
    STATE it changes: "downgraded" is over as a happening, and the state it
    brought holds. Graphiti (Rasmussen et al. 2025, arXiv:2501.13956,
    section 2.2.3) compares a new edge "against semantically related
    existing edges" whatever the tense of its words, and the old edge ends at
    the new edge's start.

    So, for a past statement that is a row of its own:
      1. it is read once more, with its past mark, against the facts of its
         own subject (the stages of a subject its writer named, 6.5.0),
         asking only which it makes false; the facts about its own two
         entities are left out of the answer: they were judged before, on
         the candidates of 6.18.0 and earlier;
      2. if it names another value, it is read as a present fact, from
         when it holds, against the same facts, asking the same;
      3. only a value BOTH readings name is closed (`judge.close`, from
         when the statement holds), and the statement then holds as the
         new state, `current`, not judged again by the batch of the write
         (`store.mark_judged`); otherwise it is history, as before.
    The verdict is the model's, twice; the code decides who is shown and
    keeps only what the two answers share (the single value stays a tag
    for the judge, never a rule that closes on its own: Lin, Mausam and
    Etzioni, EMNLP 2010, section 3; Ritter et al., EMNLP 2008).

    What the earlier attempt taught (a branch of 18 Nov 2026, never
    merged). With the first reading alone, Gemini closed a billing cycle
    under "the plan was Pro" and an employer under "three years, before"
    (one judgement each in about fifty), gpt-oss-120b a plan under
    "considered the downgrade" once in two runs; with a second reading
    required for another value, no control of the 18 closed in six runs.
    But on LoCoMo conversation 26 (338 memories, Gemini writing) it closed
    two facts main does not, both wrong ("feels accepted" by "felt accepted
    and happy", a pride parade by "missed pride parade"), both about the
    statement's own two entities, after ONE reading among the wider
    candidates: ten lines in the question changed a verdict that main,
    asking the pair alone, did not give. Here such facts are never closed by
    the wider reading (step 1), and a value closes only where the two
    readings name the same fact, not when each names something.

    Why the switch must stay off (a review of 22 Nov 2026, before any
    measurement): once the statement holds as the new state, a later memory
    that tells the same event again in the past finds it as its true twin,
    and step 3 closes it where it was said, with no judge: the old value
    closed, the new one closed, nothing true. Telling a state that ended from
    an occurrence told again needs either a mark on the row this path opened
    (a column) or a judgement where step 3 now closes by itself; neither is
    here (tests/test_engine_past_event_two_readings.py, the expected failure)."""
    F = tables.Fact

    def _ask(past: bool) -> List[Any]:
        return judge(ToJudge(
            fact_id=fact.id, subject=subject, subject_canonical=sc,
            predicate=predicate_raw, predicate_canonical=relation.canonical,
            obj=obj, object_canonical=oc,
            when=t.stated if past else (_aware(fact.valid_from) or t.stated),
            source=source, past=past, vector=fact.embedding,
            functional=bool(relation.is_functional) and not past,
            example=None if past else relation.example,
            evidence=evidence, among_its_subject=True, ask_only=True,
        ))

    # A row of the same triple still holding the index's place (one dated
    # ahead, not true now, so step 3 did not close it as the twin): the
    # statement cannot become an open row beside it. It stays history.
    if _store.find_open(db, tables, mem, sc, relation.canonical, oc, is_open=F.invalid_at.is_(None)) is not None:
        return []
    named = [i for i in _ask(past=True) if i not in already]
    if not named:
        return []
    others = [r.id for r in db.query(F).filter(F.id.in_(named)).all() if not _same_two_entities(r, sc, oc)]
    if not others:
        return []
    before = (fact.tense, fact.invalid_at, fact.invalidated_by_id)
    try:
        # In a savepoint, as the insert (step 4): a row of the triple opened
        # meanwhile costs this reading, not the memory's whole write.
        with db.begin_nested():
            fact.tense, fact.invalid_at, fact.invalidated_by_id = "current", None, None
            db.flush()
    except IntegrityError:
        fact.tense, fact.invalid_at, fact.invalidated_by_id = before
        db.flush()
        return []
    again = set(_ask(past=False))
    both = [i for i in others if i in again]
    closed = _judge.close(db, F, both, fact.id, _aware(fact.valid_from) or t.stated,
                          is_open=is_open) if both else []
    if not closed:
        fact.tense, fact.invalid_at, fact.invalidated_by_id = before
        db.flush()
        return []
    _store.mark_judged(db, fact.id)
    return closed


# ---------------------------------------------------------------------------
# The steps of `write_fact`, one function each (5.6.1, 18 Oct 2026: the same
# rules in the same order, nothing changed in what a write does). Checked by
# running the function before the split and this one on the same 400 scripted
# writes, with a fake model: same rows, same returns, same judge calls.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Clocks:
    """The clocks of one write. `born`: when the fact began, as known (the
    caller's timestamp, the date of the text, the arrival; None when none is).
    `stated`: when it was said, never in the future. `start`: where a done
    fact begins and the plan it realizes ends."""
    born: Optional[datetime]
    stated: datetime
    start: datetime


def _the_clocks(tense: str, relation: _store.Relation, *, valid_from: Optional[datetime],
             text_valid_from: Optional[datetime], arrival: Optional[datetime],
             day_of_the_words: Optional[datetime]) -> _Clocks:
    now = datetime.now(timezone.utc)
    born = valid_from or text_valid_from or arrival
    # 2.8.0 (cantiere 12): the confirmation is dated when the fact was SAID
    # (the memory's event time, else when its text arrived), not when the
    # write ran. Zep/Graphiti (Rasmussen et al. 2025, section 2.2.3) keep two
    # timelines, T for when things happened and T' for when the system learned
    # them; the recency the readers weigh (`presentation.rank_with_recency`,
    # "the latest confirmation wins") is on T. Before, a history imported today
    # looked as fresh as this morning, and the bench blocks changed hour by
    # hour. Never in the future: a write dated ahead counts as now. Not the
    # date the text states (`text_valid_from`): that is when the fact became
    # true, not when it was restated.
    stated = valid_from or arrival or now
    if stated.tzinfo is None:
        stated = stated.replace(tzinfo=timezone.utc)
    stated = min(stated, now)
    # 3.3.0: a current fact of the events family (an occurrence, TimeML; the
    # family `twins` reads for the same purpose) begins the day its own
    # words date, when the code dated it (`fact_extractor.the_day_it_happened`, the
    # conditions there) and the engine passed it, never after it was said.
    # Not a past fact: "past" is a state that ended, and the time in its words
    # dates the end (the review of 7 Oct evening: "worked at Acme until I quit
    # yesterday" would have begun yesterday). Not a state that is still true
    # (7 Oct: the time in its sentence may date something else).
    # Ahead of the caller's timestamp, which is when it was SAID.
    happened = tense == "current" and relation.family == "events"
    if happened and day_of_the_words is not None:
        day = _aware(day_of_the_words)
        if day <= stated:
            born = day
    # 3.0.0: when the done fact begins (the docstring of `write_fact`).
    return _Clocks(born=born, stated=stated, start=_aware(born) or stated)


def _already_true(db, tables: _store.Tables, mem: _store.Memory, sc: str, predicate_canonical: str,
              oc: str, *, is_open: Any, tense: str, start: datetime) -> Tuple[Optional[Any], List[Any]]:
    """(the same triple true now in this memory, or None; the plans this write
    closed). Per-message ingestion re-creates the same triple on every
    mention: a reconfirmation, counted and dated. "True now" (`is_open`), not
    merely invalid_at NULL: a fact closed on a future date is still true
    today. A plan said again as done (3.0.0) is closed where the done fact
    begins and comes back as None: the done fact is a new row."""
    existing = _store.find_open(db, tables, mem, sc, predicate_canonical, oc, is_open=is_open)
    came_true: List[Any] = []
    if existing is not None and existing.tense == "planned" and tense == "current":
        if _store.close_the_plan(existing, start):
            came_true.append(existing)
            _store.mark_closed(db, existing.id)
        # Written now: the done fact takes the open-triple index's place.
        db.flush()
        existing = None
    return existing, came_true


def _the_past(db, tables: _store.Tables, mem: _store.Memory, existing: Optional[Any], sc: str,
                predicate_canonical: str, oc: str, *, stated: datetime, source: Any) -> Optional[Any]:
    """The row a past statement lands on, or None when it is a new row."""
    F = tables.Fact
    born = _aware(existing.valid_from) if existing is not None else None
    if existing is not None and (born is None or stated >= born):
        # The memory held it true; the text says it is over. Close THAT
        # row (its history is itself) instead of adding a closed twin,
        # when it was said (2.12.0: not at the date the text gives, which
        # is when the state began).
        existing.invalid_at = stated
        existing.tense = "past"
        _store.mark_closed(db, existing.id)
        db.flush()
        return existing
    # 2.12.0: a twin that began after the statement was said is a later
    # period, true now: it stays, and the statement is history of its
    # own (below), as `judge.close` keeps a fact that began after the
    # one that contradicts it.
    history = (
        db.query(F)
        .filter(
            mem.facts,
            # Any closed row with the same triple is the same history.
            F.invalid_at.isnot(None),
            # The database's lower() on both sides (2.11.0, as
            # `store.find_open`): Python's is another function.
            func.lower(F.subject_canonical) == func.lower(sc),
            F.predicate_canonical == predicate_canonical,
            func.lower(F.object_canonical) == func.lower(oc),
        )
        .first()
    )
    if history is not None:
        _store.reconfirm(tables, history, source, when=stated)
        db.flush()
        return history
    return None


def _new_row(F, columns: dict, t: _Clocks, came_true: List[Any], *, tense: str,
                relation: _store.Relation, subject: str, sc: str, subject_type: str,
                predicate_raw: str, obj: str, oc: str, object_is_literal: bool, confidence: float,
                source_memory_id: Any, model_used: Optional[str]):
    """The new row, not yet in the session."""
    fact = F(
        **columns,
        subject_label=subject,
        subject_canonical=sc,
        subject_type=(subject_type or "unknown")[:20],
        predicate_raw=predicate_raw[:200],
        predicate_canonical=relation.canonical[:200],
        predicate_family=relation.family,
        object_label=obj,
        object_canonical=oc,
        object_is_literal=bool(object_is_literal),
        confidence=max(0.0, min(1.0, float(confidence))),
        source_memory_id=source_memory_id,
        source_memory_ids=[str(source_memory_id)] if source_memory_id else None,
        observation_count=1,
        last_confirmed_at=t.stated,
        tense=tense,
        model_used=model_used,
    )
    if t.born is not None:
        fact.valid_from = t.born
    elif came_true:
        # The plan's end and the done fact's start are one instant.
        fact.valid_from = t.start
    if tense == "past":
        # History from birth, ended when it was said (2.12.0), never before
        # it began. Until 2.12.0: true until the write ran, so "I used to
        # work at Acme" said in 2023 came back with `as_of` in 2024.
        fact.invalid_at = max(t.stated, _aware(t.born)) if t.born is not None else t.stated
        if fact.valid_from is None:
            # No start is known (POST /v1/facts with no `valid_from`): it began
            # and ended when it was said, an empty interval by construction.
            # Left to the column's default it was read at the flush, after
            # `t.stated`, and the fact was born closed before it began (audit of
            # 27 Oct 2026; the engine's rule, and the interval of Graphiti,
            # Rasmussen et al., 2025, section 2.2.3: valid from t_valid, to
            # t_invalid, never the other way round).
            fact.valid_from = t.stated
    return fact


def _insert(db, tables: _store.Tables, mem: _store.Memory, fact, sc: str, predicate_canonical: str,
               oc: str, t: _Clocks, came_true: List[Any], *, source: Any) -> Optional[Any]:
    """The insert; None when it went in, else the row that won the race,
    reconfirmed. Raises the IntegrityError when there is no winner to find."""
    F = tables.Fact
    try:
        # A savepoint, so losing the race costs this insert and not the
        # caller's whole transaction. The flush assigns the id the judge and
        # `invalidated_by_id` need.
        #
        # The row is added INSIDE the savepoint (2.11.0). `begin_nested()`
        # flushes what is pending before it emits SAVEPOINT, so the row added
        # before it was inserted outside the savepoint: losing the race
        # rolled back the caller's whole transaction (SQLAlchemy expunges the
        # rows it was adding), the `expunge` below then failed, and the write
        # of the memory failed with it. Found by the test of the 6 Oct finding (a fact
        # open with a valid_from in the future, restated).
        with db.begin_nested():
            db.add(fact)
            db.flush()
        return None
    except IntegrityError:
        # Somebody else wrote this exact true fact while we were deciding it
        # did not exist: the unique index arbitrates, and the loser
        # reconfirms the winner. The savepoint's rollback takes the row we
        # built out of the session; if it is still there, out by hand, or the
        # next flush tries it again.
        if fact in db:
            db.expunge(fact)
        # The winner is the row that holds the index's place, so it is looked
        # for with the INDEX's predicate, "invalid_at IS NULL" (2.11.0), not
        # with `is_open`. The two differ on an open fact whose valid_from is
        # still in the future (a write dated ahead: a naive local timestamp
        # from a client east of UTC reads as UTC, hours ahead): not true now,
        # so the read before the insert did not find it, yet in the index.
        # Read again with `is_open` it was not found either, and the write
        # failed every time the fact was restated, until the clock reached
        # its valid_from (code audit of 6 Oct).
        theirs = _store.find_open(db, tables, mem, sc, predicate_canonical, oc, is_open=F.invalid_at.is_(None))
        if theirs is not None:
            _store.reconfirm(tables, theirs, source, when=t.stated)
            # 3.0.0: a plan this write closed came true in the winner's
            # write too: it ends where the winner's fact begins, closed by
            # it (the winner already said so; nothing to report here).
            for plan in came_true:
                born_on = _aware(plan.valid_from)
                its_own = _aware(theirs.valid_from) or t.start
                plan.invalid_at = max(its_own, born_on) if born_on is not None else its_own
                plan.invalidated_by_id = theirs.id
            db.flush()
            return theirs
        raise


def _alias_from_identity(db, tables: _store.Tables, mem: _store.Memory, fact, predicate_canonical: str,
                       subject: str, obj: str, *, subject_type: str, confidence: float) -> None:
    """An explicit identity statement creates the alias by itself
    (`identity.alias_rule_from_identity`); an alias never blocks the write."""
    rule = alias_rule_from_identity(
        predicate_canonical, subject, obj, subject_type=subject_type, confidence=confidence,
    )
    if rule is None:
        return
    old, new = rule
    try:
        with db.begin_nested():
            _store.add_alias(
                db, tables, mem, old, new,
                source=SOURCE_IDENTITY_FACT, confidence=confidence, fact_id=fact.id,
            )
        db.refresh(fact)
    except Exception as e:  # noqa: BLE001 - an alias never blocks the write
        # 3.0.0: the fact, never the two names, and the
        # error without its message (`log_safe`).
        logger.warning("[memory] identity alias of fact %s failed: %s", fact.id, _log.error(e))


def _twin(db, tables: _store.Tables, mem: _store.Memory, fact, relation: _store.Relation,
             came_true: List[Any], *, is_open: Any, tense: str, start: datetime,
             source: Any) -> Optional[Any]:
    """`twins` (2.8.0, off): the fact already true this one restates in
    other words, with the new row merged into it; None when there is none (a
    plan in other words that came true is closed, appended to `came_true`, and
    not merged)."""
    twin_threshold = _thresholds.current().twin
    if twin_threshold is None:
        return None
    twin = _twins.find_twin(
        db, tables, mem, fact, family=relation.family, is_open=is_open,
        thresholds=twin_threshold, source=source,
    )
    if twin is not None and twin.tense == "planned" and tense == "current":
        # 3.0.0: a plan in other words that came true ends as the plan of
        # the same words does (the docstring): closed where the done
        # fact begins, not merged into it.
        if _store.close_the_plan(twin, _aware(fact.valid_from) or start):
            twin.invalidated_by_id = fact.id
            came_true.append(twin)
            _store.mark_closed(db, twin.id)
        db.flush()
        return None
    if twin is None:
        return None
    # The new row was in the session for one flush and nobody saw it:
    # it flows into the kept one (sources, count, latest confirmation,
    # tense promoted, confidence) and leaves, as two triples made
    # equal by an alias do. The judge already weighed the kept row.
    # A plan the new row closed now points at the kept one
    # (`merge_into` moves `invalidated_by_id`).
    _store.merge_into(db, tables, twin, fact)
    db.flush()
    for plan in came_true:
        db.refresh(plan)
    logger.info("[memory] near-duplicate of fact %s merged into it", twin.id)
    return twin
