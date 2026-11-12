"""The contradiction judge of the Agents engines (memory_core, piece 3 of
the shared-code plan, 2026-09-28).

Stage 2 of ConflictBank (Su et al. 2024): the candidates gathered with recall
(`candidates.for_the_judge`) go in front of a model that decides which earlier
facts the new one makes false. The question itself (`AGENTS_JUDGE`, the
[single-valued] tag, the lines) was already here; the call, the reading of
the verdict, the batched form and the bi-temporal closing were written twice,
in the Cloud's `agent_facts/contradiction.py` and in korely-agent
`services/facts/contradiction.py`, and had drifted: the cloud kept a batched
judge the self-hosted never had, with a token ceiling of 352 for one group,
the ceiling the single judge had already learned was too small.

The engine passes `call(prompt, max_tokens) -> text` (provider, model and
usage tracking stay in the engine, as for `fact_extractor`) and `single_value(c)
-> (holds one value, example)` for each candidate (the cloud reads it per
project, the self-hosted per tenant).

A judge that fails is not a verdict. A failed call raises `JudgementMissing`
(worth retrying), an answer that cannot be read raises `JudgementUnreadable`
(paid for; at temperature 0 the same prompt gives it again). Until 2026-09-28
both engines read either as "nothing contradicts": the new fact stayed open
next to the one it contradicts, the memory was marked done, and nobody asked
again. The engine maps the two onto its own retry classes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from .contradiction import (
    AGENTS_JUDGE,
    AGENTS_JUDGE_OTHER_SUBJECT,
    AGENTS_JUDGE_BATCH,
    AGENTS_JUDGE_BATCH_OTHER_SUBJECT,
    other_subject,
    new_fact_for_the_judge,
    line_for_the_judge,
)
from . import store as _store
from . import log_safe as _log
from .model_json import read_answer

logger = logging.getLogger(__name__)

# (prompt, max_tokens) -> the model's text. Raises when the provider fails.
Call = Callable[[str, int], str]
# candidate -> (the relation holds one value at a time, the object that was measured on)
SingleValue = Callable[[Any], Tuple[bool, Optional[str]]]

# Thinking tokens are spent from this budget before the answer starts. At 256
# the single judge returned "Here is the JSON requested:" and nothing else,
# for months, which read as "no contradiction" (korely-agent 1051c67, measured
# 2026-09-11: six pairs that must supersede went from 3/18 to 17/18, the seven
# that must not stayed 7/7). The batched judge had `min(2048, 256 + 96 * n)`,
# 352 for one group. The cost is the output emitted, not the output allowed,
# so one ceiling for both.
MAX_TOKENS = 8192


class JudgementMissing(RuntimeError):
    """The judge gave no answer (a failed or refused call). Not a verdict."""


class JudgementUnreadable(RuntimeError):
    """The judge answered something that cannot be read. Not a verdict."""


def _day(v) -> Optional[str]:
    return v.date().isoformat() if v else None


def _aware(v: Optional[datetime]) -> Optional[datetime]:
    """A timestamp as an aware UTC datetime (SQLite hands back naive values;
    the columns are UTC)."""
    if v is None:
        return None
    return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)


def row(c: Any, single_value: SingleValue, new: Optional[str] = None) -> str:
    """One earlier fact as the judge reads it: id, written and canonical
    subject, relation, object, since when, and the [single-valued] tag.
    `new` (5.8.0, off): the NEW fact's canonical subject, for the
    [another subject] tag (contradiction.RULE_OTHER_SUBJECT)."""
    return line_for_the_judge(
        c.id, c.subject_label, c.subject_canonical, c.predicate_raw, c.object_label,
        _day(c.valid_from), *single_value(c),
        other_subject=other_subject(c.subject_label, c.subject_canonical, new),
    )


def _tags(candidates: Sequence[Any], single_value: SingleValue) -> Dict[Any, Tuple[bool, Optional[str]]]:
    """The [single-valued] tag of each candidate, read once per call: the
    prompt's rows and the log of the relations shown share it."""
    return {c.id: single_value(c) for c in candidates}


def _relation(f: Any) -> str:
    return getattr(f, "predicate_canonical", None) or f.predicate_raw


def _shown(what: str, new_ones: Sequence[Tuple[str, bool]], candidates: Sequence[Any], tags: dict) -> None:
    """Which relations a judge call shows, a star on those carrying the
    [single-valued] tag (3.3.0, cantiere 8). The vocabulary asks its votes
    for every new relation, and only a judge ever reads the answer: this line
    says how many of the relations that were voted on reach one (7 Oct:
    "the exact share is not readable today"). Relation names only, the
    vocabulary's words, never an object or a subject (3.0.0)."""
    if not logger.isEnabledFor(logging.INFO):
        return
    seen: Dict[str, bool] = {}
    for name, single in new_ones:
        seen[name] = seen.get(name, False) or bool(single)
    for c in candidates:
        name = _relation(c)
        seen[name] = seen.get(name, False) or bool(tags[c.id][0])
    logger.info("[memory/F3] the %s shows %d relations: %s", what, len(seen),
                ", ".join(f"{n}*" if s else n for n, s in sorted(seen.items())))


def _answer(call: Call, prompt: str, key: str, extract_json, what: str) -> list:
    """The list under `key` in the judge's JSON answer, or raise."""
    try:
        raw = (call(prompt, MAX_TOKENS) or "").strip()
    except Exception as e:
        # 3.0.0: the error's class and codes, never its message
        # (`log_safe`), in the log and in the exception the engines log.
        logger.error("[memory/F3] the %s's call failed, no verdict: %s", what, _log.error(e))
        raise JudgementMissing(f"{what} call failed: {_log.error(e)}") from e
    if not raw:
        logger.error("[memory/F3] the %s returned nothing: no verdict", what)
        raise JudgementUnreadable(f"the {what} returned nothing")
    # 2.11.0: the rule of `model_json` (one object with stray text
    # around it is that object, two different objects are no answer), then
    # the engine's reader; until then a bare `json.loads`, and the engines'
    # fallback cut from the first brace to the end, so one brace too many
    # (`{...}\n}`) was no verdict and the write rolled back.
    data = read_answer(raw, extract_json)
    # 3.0.0: the shape of the answer, never its start. The judge's
    # answer names the facts and gives its reasons in the memory's words.
    if data is None:
        logger.error("[memory/F3] could not read the %s's answer, no verdict: %s", what, _log.answer(raw, data))
        raise JudgementUnreadable(f"the {what}'s answer is not JSON")
    # A JSON list is not the object the prompt asks for: `.get` on it raised
    # AttributeError on the self-hosted engine, a 500 on a direct write.
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        logger.error("[memory/F3] the %s's answer had no '%s' list, no verdict: %s", what, key,
                     _log.answer(raw, data, key))
        raise JudgementUnreadable(f"the {what}'s answer has no {key} list")
    return items


def _ids(entries: Sequence, allowed: set, reasons: Optional[dict] = None) -> List[UUID]:
    """The existing ids the judge named, only among the candidates it was
    shown, once each, in its order. `reasons` collects the reasons."""
    out: List[UUID] = []
    for v in entries:
        if not isinstance(v, dict):
            continue
        eid = str(v.get("existing_id") or "").strip()
        if not eid or eid not in allowed:
            continue
        try:
            uid = UUID(eid)
        except ValueError:
            continue
        if uid in out:
            continue
        out.append(uid)
        if reasons is not None:
            reasons[eid] = (v.get("reason") or "").strip()[:200]
    return out


def judge(
    call: Call,
    candidates: Sequence,
    subject: str,
    predicate: str,
    obj: str,
    *,
    single_value: SingleValue,
    functional: bool = False,
    past: bool = False,
    example: Optional[str] = None,
    extract_json=None,
    evidence: Optional[str] = None,
    since: Optional[datetime] = None,
    relation: Optional[str] = None,
    ensure: Optional[Callable[..., Tuple[bool, Optional[str]]]] = None,
    subject_canonical: Optional[str] = None,
    mark_other_subject: bool = False,
) -> Tuple[List[UUID], dict]:
    """(the candidates the new fact makes false, {id: reason}). No call when
    there are no candidates. `evidence`: the words the new fact was read from
    (2.5.0, contradiction rule 4). `since` (2.12.0): since when the new fact
    holds, shown as the earlier facts show theirs (contradiction rule 5);
    not for a past statement, whose date in the engines is when it was said,
    not when it began.

    `ensure(relation, examples)` (3.3.0, with the vocabulary's lazy votes): when an
    earlier fact has the new fact's relation (`relation`, canonical), the
    relation's tag is decided now if it was not (`store.decide_if_missing`)
    and read again for the new fact."""
    if not candidates:
        return [], {}
    # Not for a past statement: it is judged without the tag, as before 3.3.0
    # (the review of 7 Oct evening: "Maria was married to Luca" gained
    # [single-valued] against the reversed "Luca married_to Maria").
    tied = [c for c in candidates if _relation(c) == relation] if ensure is not None and relation else []
    if ensure is not None and relation and not past and tied:
        # 8 Oct: typed by the two values in conflict (`store.decide_if_missing`).
        functional, example = ensure(relation, [obj, tied[0].object_label])
    tags = _tags(candidates, single_value)
    _shown("contradiction judge", [(relation or predicate, functional)], candidates, tags)
    # 5.8.0, off: the [another subject] tag and its rule (contradiction).
    new = (subject_canonical or subject) if mark_other_subject else None
    prompt = (
        (AGENTS_JUDGE_OTHER_SUBJECT if mark_other_subject else AGENTS_JUDGE)
        .replace("{EXISTING}", "\n".join(row(c, lambda x: tags[x.id], new) for c in candidates))
        .replace("{NEW}", new_fact_for_the_judge(
            subject, predicate, obj, functional=functional, past=past, example=example,
            evidence=evidence, since=None if past else _day(since),
        ))
    )
    items = _answer(call, prompt, "contradictions", extract_json, "contradiction judge")
    reasons: dict = {}
    return _ids(items, {str(c.id) for c in candidates}, reasons), reasons


def judge_batch(
    call: Call,
    groups: Sequence[dict],
    *,
    single_value: SingleValue,
    extract_json=None,
    mark_other_subject: bool = False,
) -> Dict[str, List[UUID]]:
    """One call for several new facts. `groups`: [{"new_id": str, "new_text":
    str, "candidates": [...]}]. Returns {new_id: [existing ids]}, only ids of
    that group's candidates. Every new_id must come back, even with an empty
    list: one left out was not judged, and reading it as "compatible" is the
    unreadable answer on a smaller scale."""
    if not groups:
        return {}
    everything = list({c.id: c for g in groups for c in g["candidates"]}.values())
    tags = _tags(everything, single_value)
    # `new_relation` (3.3.0): the new fact's relation and its tag, for the log
    # only; a group without it is judged the same.
    _shown("batch contradiction judge", [g["new_relation"] for g in groups if g.get("new_relation")],
              everything, tags)
    blocks = []
    for g in groups:
        # 5.8.0, off: `new_subject`, the NEW fact's canonical subject (`batch`).
        new = g.get("new_subject") if mark_other_subject else None
        rows = "\n".join("  " + row(c, lambda x: tags[x.id], new) for c in g["candidates"])
        blocks.append(f"[new_id={g['new_id']}] NEW: {g['new_text']}\n  candidates:\n{rows}")
    question = AGENTS_JUDGE_BATCH_OTHER_SUBJECT if mark_other_subject else AGENTS_JUDGE_BATCH
    prompt = question.replace("{BLOCKS}", "\n\n".join(blocks))
    items = _answer(call, prompt, "results", extract_json, "batch contradiction judge")

    allowed = {g["new_id"]: {str(c.id) for c in g["candidates"]} for g in groups}
    out: Dict[str, List[UUID]] = {}
    answered: set = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("new_id") or "").strip()
        if nid not in allowed:
            continue
        contradictions = item.get("contradictions")
        if not isinstance(contradictions, list):
            continue
        answered.add(nid)
        ids = _ids(contradictions, allowed[nid])
        if ids:
            out[nid] = ids
    missing = [g["new_id"] for g in groups if g["new_id"] not in answered]
    if missing:
        logger.error("[memory/F3] the batch judge left out %d of %d new facts: no verdict",
                     len(missing), len(groups))
        raise JudgementUnreadable(f"the batch contradiction judge left out {len(missing)} new facts")
    return out


def close(db, F: Any, ids: Sequence, new_id: Any, when: Optional[datetime], *, is_open: Any = None) -> list:
    """Close the facts the judge says the new one contradicts, bi-temporally.
    Returns the ids of the earlier facts it closed. `when`: since when the
    new fact holds (its valid_from; now when no date is known). `is_open`:
    what "still true" means (Agents: `true_at()`).

    Zep/Graphiti (Rasmussen et al. 2025, arXiv:2501.13956, section 2.2.3):
    "t_valid and t_invalid ∈ T track the temporal range during which facts
    held true", and "When the system identifies temporally overlapping
    contradictions, it invalidates the affected edges by setting their
    t_invalid to the t_valid of the invalidating edge." So an old fact stops
    being true when the superseding fact began, not when the contradiction
    was ingested: that is what makes `as_of` right ("on 2025-09-01 Maria
    works at Globex", the change having happened on 2025-06-01).

    2.12.0 (code audit of 6 Oct): which of the two is the
    invalidating one. Until today it was always the fact written last. A
    /v1/batch exported newest first wrote "Maria works at Globex" (2025-06)
    before "Maria works at Acme" (2024-01), and the judge of Acme closed
    Globex in 2024, before it began: a row true on no date, the state that
    `ClosedBeforeItBegan` refuses on a manual closing, with Acme left open as
    the present. The paper does not say what happens when the new edge is
    the older one; its reference implementation does (getzep/graphiti,
    graphiti_core/utils/maintenance/edge_operations.py, read at 7514b44 of
    2026-05-20): `resolve_edge_contradictions` invalidates an existing edge
    only when "edge_valid_at_utc < resolved_edge_valid_at_utc", and
    `resolve_extracted_edge` closes the new edge at the start of a later one
    it contradicts, with the comment "Expire new edge since we have
    information about more recent events". The same here:
      - an earlier fact is never closed before its own valid_from: one that
        began AFTER the new fact stays true;
      - the new fact is then history, closed where the earliest of those
        began, `invalidated_by_id` that fact: the row it would have become
        had the two arrived in their order, so the store ends the same
        whatever the order of arrival;
      - the same valid time goes to the newer information, as the paper
        says ("Following the transactional timeline T′, Graphiti
        consistently prioritizes new information when determining edge
        invalidation"): the earlier fact closes at its own valid_from, as it
        did before 2.12.0. (The reference implementation leaves both open on
        a tie, the contradiction unresolved.)
    A fact already closed meanwhile is skipped; so is a new fact already
    closed by then (a past statement is history by its own tense).

    6.7.1: an end already given is never moved later. A fact true now may
    have an end ahead: a forget's `at`, an earlier closing by a value dated
    ahead, a past statement. When the new fact begins at or after that end,
    the two never held at the same time, so there is nothing to invalidate:
    the fact keeps its end and whatever named it (none for a forget). The
    paper invalidates "temporally overlapping contradictions" (§2.2.3,
    above); the reference implementation skips an edge whose end is at or
    before the new edge's start (getzep/graphiti, read at 7514b44,
    `resolve_edge_contradictions`: "(Edge invalid before new edge becomes
    valid) ... continue", `edge_invalid_at_utc <= resolved_edge_valid_at_utc`).
    Until 6.7.0 the end moved to the new fact's start: a fact forgotten from
    day 2, then a value from day 10, read true on day 5 (a review finding of
    9 Nov 2026). `_history` already kept the same rule for the new
    fact's own end. A new fact that begins BEFORE the end still closes the
    fact there, earlier, as a forget would (`forget_fact` never moves an end
    later either)."""
    if not ids:
        return []
    end = _aware(when) or datetime.now(timezone.utc)
    closed = []
    after: List[Tuple[datetime, Any]] = []
    for old_id in ids:
        f = (
            db.query(F)
            .filter(F.id == old_id, is_open if is_open is not None else F.invalid_at.is_(None))
            .first()
        )
        if f is None:
            continue
        born = _aware(f.valid_from)
        if born is not None and born > end:
            after.append((born, f.id))
            continue
        ended = _aware(f.invalid_at)
        if ended is not None and ended <= end:
            # 6.7.1: over by the time the new fact begins (docstring).
            continue
        f.invalid_at = end
        f.invalidated_by_id = new_id
        closed.append(old_id)
        # 3.1.3: still weighed by the later judgements of the same write
        # (`store.one_write`).
        _store.mark_closed(db, f.id)
    history = _history(db, F, new_id, after) if after else False
    # Written now, so a second closing of the same fact in this transaction
    # finds it closed: both engines run with autoflush off, and without this
    # two new facts of one batched write that contradict the same old fact
    # closed it twice, the last one in the model's answer order winning.
    if closed or history:
        db.flush()
    return closed


def _history(db, F: Any, new_id: Any, after: Sequence[Tuple[datetime, Any]]) -> bool:
    """The new fact closed where the earliest later fact it contradicts began
    (`close`, 2.12.0). False when there is no such row (a caller that judged
    an id not written) or the new fact had already ended by then."""
    new = db.get(F, new_id) if new_id is not None else None
    if new is None:
        return False
    start, by_whom = min(after, key=lambda d: (d[0], str(d[1])))
    ended = _aware(new.invalid_at)
    if ended is not None and ended <= start:
        return False
    new.invalid_at = start
    new.invalidated_by_id = by_whom
    return True


def batch(
    db,
    F: Any,
    new_facts: Sequence[dict],
    *,
    gather: Callable[[dict], list],
    single_value_of_new: Callable[[dict], Tuple[bool, Optional[str]]],
    classify: Callable[[List[dict]], Dict[str, List[UUID]]],
    is_open: Any = None,
    ensure: Optional[Callable[..., Tuple[bool, Optional[str]]]] = None,
) -> Dict[Any, list]:
    """The batched judgement of one write: every new fact against the facts
    that existed BEFORE the write (never its batch-mates: the verdict would
    depend on the order of the list), one call, then the closings. "Before"
    includes the facts an earlier statement of the same write already closed
    (3.1.3, `store.one_write`): they are candidates again, and naming
    them closes nothing.

    `new_facts`: dicts with at least id, subject, predicate_raw, predicate_canonical,
    object, valid_from, and "evidence" when the words are known (2.5.0). The
    judge reads each new fact with its date (2.12.0, contradiction rule 5),
    and `close` decides which of two contradicting facts ended.
    `gather(nf)`: its candidates (the engine applies its
    scope, its source and its similarity). `single_value_of_new(nf)`: the tag of
    the new fact's relation. `classify(groups)`: the call, normally
    `judge_batch` with the engine's `call`, a seam for the tests.
    Returns {new fact id: [closed ids]}; no call when nothing has candidates."""
    of_the_batch = {nf["id"] for nf in new_facts}
    groups: List[dict] = []
    by_id: Dict[str, dict] = {}
    for nf in new_facts:
        cands = [c for c in gather(nf) if c.id not in of_the_batch]
        if not cands:
            continue
        nid = str(nf["id"])
        # 3.3.0: the lazy votes, when an earlier fact has the same relation
        # (`judge`, `store.decide_if_missing`); read again just below.
        canonical_form = nf.get("predicate_canonical")
        tied = [c for c in cands if _relation(c) == canonical_form] if ensure is not None and canonical_form else []
        if tied:
            ensure(canonical_form, [nf["object"], tied[0].object_label])
        single, example = single_value_of_new(nf)
        groups.append({
            "new_id": nid,
            "new_text": new_fact_for_the_judge(
                nf["subject"], nf["predicate_raw"], nf["object"], functional=single, example=example,
                evidence=nf.get("evidence"), since=_day(nf.get("valid_from")),
            ),
            "candidates": cands,
            "new_relation": (nf.get("predicate_canonical") or nf["predicate_raw"], bool(single)),
            "new_subject": nf.get("subject_canonical") or nf["subject"],
        })
        by_id[nid] = nf
    verdicts = classify(groups) if groups else {}

    to_close: Dict[Any, list] = {}
    for nid, old in verdicts.items():
        nf = by_id.get(nid)
        if nf is None:
            continue
        listed = to_close.setdefault(nf["id"], [])
        listed.extend(i for i in old if i not in listed)
    per_new = {nf["id"]: nf for nf in new_facts}
    # The earliest valid time closes first: an old fact stopped being true at
    # the first change that superseded it (bi-temporal), whatever the order of
    # the model's answer. A new fact with no date of its own dates from now.
    def _when(new_id):
        v = per_new[new_id].get("valid_from")
        if v is None:
            return datetime.max.replace(tzinfo=timezone.utc)
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)

    closed: Dict[Any, list] = {}
    for new_id, old in sorted(to_close.items(), key=lambda kv: _when(kv[0])):
        facts = close(db, F, old, new_id, per_new[new_id].get("valid_from"), is_open=is_open)
        if facts:
            closed[new_id] = facts
    return closed


def after_the_vector(
    db,
    tables: Any,
    scope: Any,
    fact: Any,
    vector,
    *,
    is_open: Any,
    single_value: SingleValue,
    classify: Callable[[List[dict]], Dict[str, List[UUID]]],
    ensure: Optional[Callable[..., Tuple[bool, Optional[str]]]] = None,
    named: Any = None,
    agent_scoped_facts: Any = None,
) -> Dict[Any, list]:
    """A fact whose vector arrived late: the two judgements the semantic stage
    could not make (2026-09-28).

    Both engines keep a fact whose inline embedding failed and retry the
    vector later. The write judged it on the SQL candidates only (stages 1a,
    1c, 1d, 1a-bis): the semantic stage 1b needs its vector. And every fact
    written after it could not find it by meaning either, since it had none.
    When the vector arrives, before it is stored:

      1. the fact against the OLDER facts that only 1b brings (the candidates
         with the vector minus the candidates without it, both restricted to
         facts created before it): the judgement its write would have made;
      2. each LATER true fact near it by meaning (1b's own threshold and cap),
         whose own write did not already see it as a SQL candidate, against
         this one fact: the judgements those writes would have made.

    One batched call for all of it (`classify`, normally `judge_batch`).
    Closings are bi-temporal, by `close`: of two facts that contradict each
    other, the one that began first ends where the other began, whichever
    was written first (2.12.0: "created later" is not "began later", an
    import can bring an older fact after a newer one). The later facts are
    weighed in the order they began, so every older one is closed by the
    late fact before a newer one closes it. Nothing to do for a fact no
    longer true, or not current (history and plans close nothing), or on a
    database without pgvector (the semantic stage does not exist there).

    Limit, said plainly: a later fact that was itself closed since is not
    asked; the late fact may stay true where it would have been closed at
    that fact's date. And the words a fact was read from are not stored
    (2.5.0: a forgotten memory must leave no copy of its text in the facts),
    so these judgements read the triples alone. Returns {closing fact id:
    [closed ids]}: the late fact may be on either side.

    `named` (6.5.0): the condition on the fact
    table that is true for the facts whose subject their writer named (`candidates.for_the_judge(
    named=)`, where the reason is), read here by the database for the late
    fact and the later ones near it. Both judgements are the ones the writes
    would have made: the late fact's candidates follow the rule of its
    write, and a later fact near it is not judged against it when either of
    the two is named and the two are about different subjects, by the one
    rule of the write (6.6.0, `candidates.about_the_subject`, in SQL).

    `agent_scoped_facts` (6.7.0): the condition true for the facts of an
    agent's memory with no end user (`candidates.for_the_judge(
    agent_scoped_facts=)`, where the reason is), read by the database for the
    late fact, and applied by the same rule: such a late fact is weighed
    against older facts of its subject only, and a later fact near it of
    another subject is not weighed against it when either of the two is one
    of those facts."""
    from sqlalchemy import and_

    from .store import _pg, outside_the_source
    from . import thresholds
    from .candidates import SEMANTIC_LIMIT, about_the_subject, for_the_judge
    from .vector_search import nearest as _nearest

    F = tables.Fact
    if vector is None or not _pg(db) or (fact.tense or "current") != "current":
        return {}
    if db.query(F.id).filter(F.id == fact.id, is_open).first() is None:
        return {}

    def _ids_where(condition, ids) -> set:
        if condition is None or not ids:
            return set()
        return {i for (i,) in db.query(F.id).filter(F.id.in_(list(ids)), condition).all()}

    named_ids = _ids_where(named, [fact.id])
    # 6.7.0: whether the late fact belongs to an agent's memory with no end
    # user. A later fact's own write is replayed below without a vector
    # (`_stages(y, ...)`: the SQL stages only), where this changes nothing:
    # it narrows stage 1b, and 1a-bis does not run without an end user.
    scoped_ids = _ids_where(agent_scoped_facts, [fact.id])

    def _stages(f, before, v=None):
        return for_the_judge(
            db, tables, and_(scope, F.created_at < before), is_open=is_open, exclude_id=f.id,
            subject_label=f.subject_label, predicate_canonical=f.predicate_canonical,
            subject_canonical=f.subject_canonical, object_canonical=f.object_canonical,
            source=f.source_memory_id, with_recall=f.end_user_id is not None, vector=v,
            subject_named=f.id in named_ids, named=named,
            agent_scoped=f.id in scoped_ids, agent_scoped_facts=agent_scoped_facts,
        )

    already = {c.id for c in _stages(fact, fact.created_at)}
    only_semantic = [c for c in _stages(fact, fact.created_at, vector) if c.id not in already]

    # 2.12.0 (BUG-320): with the depth of the HNSW scan
    # (`vector_search.nearest`), as the semantic stage of the candidates.
    later = db.query(F).filter(
        scope, is_open, F.id != fact.id, F.created_at > fact.created_at,
        F.tense == "current", F.embedding.isnot(None),
        outside_the_source(tables, fact.source_memory_id),
    )
    # 6.6.0: the later facts that may meet the late one, by the rule their
    # writes apply (`candidates.about_the_subject`, in SQL: the same
    # comparison at the write and here). The rule holds the same from either
    # fact of a pair, so it is built on the late fact. Until 6.5.1 a Python
    # comparison (NFKC and case folding) decided here, the database's lower()
    # at the write: "Straße 5" and "STRASSE 5" met here and not there.
    # 6.7.0: and the facts of an agent's memory with no end user, by the
    # same rule.
    about = about_the_subject(F, fact.subject_label, fact.subject_canonical,
                              subject_named=fact.id in named_ids, named=named,
                              agent_scoped=fact.id in scoped_ids,
                              agent_scoped_facts=agent_scoped_facts)
    if about is not None:
        later = later.filter(about)
    nearest = _nearest(db, later, F.embedding, vector, SEMANTIC_LIMIT)
    named_ids |= _ids_where(named, [y.id for y, _d in nearest])
    after = [
        y for y, d in nearest
        if d is not None and float(d) <= thresholds.current().semantic
        and fact.id not in {c.id for c in _stages(y, y.created_at)}
    ]

    def _group(f, candidates):
        # 3.3.0: the lazy votes, as in `batch`.
        tied = [c for c in candidates if _relation(c) == _relation(f)] if ensure is not None else []
        if tied:
            ensure(_relation(f), [f.object_label, tied[0].object_label])
        single, example = single_value(f)
        return {
            "new_id": str(f.id),
            "new_text": new_fact_for_the_judge(
                f.subject_label, f.predicate_raw, f.object_label, functional=single, example=example,
                since=_day(f.valid_from),
            ),
            "candidates": candidates,
            "new_relation": (_relation(f), bool(single)),
            "new_subject": f.subject_canonical or f.subject_label,
        }

    groups: List[dict] = []
    if only_semantic:
        groups.append(_group(fact, only_semantic))
    for y in after:
        groups.append(_group(y, [fact]))
    if not groups:
        return {}
    verdicts = classify(groups)

    closed: Dict[Any, list] = {}

    def _mark(who, closed_id):
        listed = closed.setdefault(who, [])
        if closed_id not in listed:
            listed.append(closed_id)

    def _close(old, new):
        """`close`, and both directions in the result: the earlier facts the
        new one closed, and the new one when a later fact closed it."""
        for i in close(db, F, old, new.id, new.valid_from, is_open=is_open):
            _mark(new.id, i)
        if new.invalid_at is not None and new.invalidated_by_id in set(old):
            _mark(new.invalidated_by_id, new.id)

    old = verdicts.get(str(fact.id), [])
    if old:
        _close(old, fact)
    for y in sorted(after, key=lambda r: (_aware(r.valid_from) or _aware(r.created_at), str(r.id))):
        if fact.id in verdicts.get(str(y.id), []):
            _close([fact.id], y)
    return closed
