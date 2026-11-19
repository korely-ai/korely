"""Forgetting and erasing: one memory, one end user, one agent namespace;
and forgetting one fact by hand (`forget_fact`, POST /v1/facts/{id}/forget,
out of service.py since the 1.0.27 gate: 1,011 lines).

Taken out of service.py (audit of 17 Oct 2026: 1,990 lines), whole and as
they were. Below the write: nothing here judges a fact (the one row it
writes, a fact given back, is facts/given_back.py's), so this imports
nothing of service.py, and service.py imports it and serves its names as
before (`facts.service.forget_memory`, `facts.forget_end_user`).

`forget_memory` is the soft forget of DELETE /v1/memories/:id, which keeps
the history; `erase_memory` (the same route with `erase=true`),
`forget_end_user` and `delete_agent` are erasures, rows physically deleted
(GDPR Art. 17), each with its audit row.

What a retracted fact had closed comes back, as a new row, when nothing
left closes it (facts/given_back.py, decided on 31 Oct 2026): on the
forget and the erasure of one memory, and on the deletion of an agent
namespace, whose facts may have closed another agent's facts of the same end
user (the judge's candidates are of any agent, `entities.memory_scope`).
The erasure of an end user or of a project gives nothing back: a fact only
closes facts of its own end user in its own project, and those all go.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from ...db.models import (AgentEntityAlias, AgentFact, AgentForgetAudit, AgentMemory, AgentObjectLink,
                          PredicateCluster)
from ..ids import audit_public_id, fact_public_id, mem_public_id
from ..limits import MAX_ID_CHARS
from ..memory_core import worker as _worker
from ..memory_write import SESSION
from ..sentinels import EVERY_PROJECT, within_project
from .entities import facts_in_project, memories_in_project
from .reads import load_fact
from .temporal import not_ended, true_now

# The moment an erasure of an end user or an agent namespace is on file:
# when its record is written, after every row of theirs went
# (clock_timestamp(), the statement's own time), never the start of its
# transaction (now(), the column's default). A row written while the
# erasure was open is older than its record, as the rows it erased are: a
# replay after a restore takes it as of before the erasure, never as the
# person writing again (services/erasure_replay._repeat_a_person; review of 16 Nov
# 2026: a memory committed 1.5 s into the erasure was newer than a record
# dated at its start, and a replay kept it).
_ENDED = func.clock_timestamp()


def _invalidate_facts(db: Session, facts: List[AgentFact]) -> int:
    """End the facts not ended yet (no end, or an end ahead): now, or at
    their start when that is later (memory_core 2.11.0), so a fact dated
    ahead ends where it would have begun and is true on no date."""
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)

    from ...core.times import aware

    from . import closures

    n = 0
    for f in facts:
        end = aware(f.invalid_at)
        if end is None or end > now:
            begin = aware(f.valid_from)
            f.invalid_at = max(now, begin) if begin is not None else now
            n += 1
    # A forget announces nothing: never the pass of a write after it either.
    closures.unsaid(db, facts)
    return n


def _audit_project(project_id: Any) -> Optional[UUID]:
    """The project an erasure audit row is filed under, for an erasure scoped
    by the caller rather than by one row: the key's project. An
    account-wide erasure (EVERY_PROJECT, never a key) belongs to none.

    Every row of `agent_forget_audit` carries a project since 2026-09-28: the
    column was there (c1a7f3e94d20) and no writer filled it, so deleting a
    project, which moves every row it holds to the default one, never moved
    these. A row for one memory or one fact takes that row's project."""
    return None if project_id is EVERY_PROJECT else project_id


def erase_memory(db: Session, tenant_id: UUID, memory_uuid: UUID,
                 *, project_id: Any) -> Optional[dict]:
    """DELETE /v1/memories/:id?erase=true: one memory erased for good, in one
    transaction. Forgotten first when it is not yet (the forget's audit row,
    the proof, as `forget_memory` writes it); a memory forgotten earlier is
    erased too, with an audit row of its own (audit of 18 Oct 2026: it
    answered 404, so every memory forgotten before, from the API or the
    dashboard, kept its text for ever). Then the row goes, text, observation
    and vector, and with it the facts it alone stated (a fact other memories
    still assert, ended or not, loses this source and stays) and the
    corrections made of their words (`_made_of_its_words`), and what they
    left elsewhere, as every other erasure takes it (the audit of 18 Oct 2026:
    this one left all three):

    - the item of the /v1/batch payload that imported it (`batch.
      erase_memory_item`), first and under the jobs' locks, as
      `forget_end_user` scrubs the payloads;
    - the entity aliases learned from its facts, before the facts go (their
      `source_fact_id` is ON DELETE SET NULL, so after it nothing names them);
    - the vocabulary's examples its facts alone stated
      (`store.examples_after_the_erasure`);
    - the object links its words took part in (memory_core 6.1.0,
      facts/links.py): one that named the object it linked, or the object it
      was linked to, or whose fact it alone stated. A link keeps the
      writer's descriptions of both names, words derived from both
      memories, so it goes whichever of the two is erased; no fact changes
      with it, since a link never changed one.

    The erasure record is the forget's (scope 'memory'), the one a forget
    writes too; a restore tells the two apart by the row, gone only after an
    erasure (services/erasure_replay.py). Returns None when the memory is
    not this account's, or not behind `project_id`'s wall."""
    from ..batch import erase_memory_item

    erase_memory_item(db, tenant_id, memory_uuid, project_id=project_id)
    mem = (memories_in_project(
               db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant_id,
                                            AgentMemory.id == memory_uuid),
               project_id)
           .with_for_update().first())
    if mem is None:
        db.rollback()
        return None
    if mem.deleted_at is None:
        # Nothing given back by the forget: the erasure does it below, once,
        # with the closers erased, which never were (facts/given_back.py).
        facts_invalidated, audit, _ = _forget(db, tenant_id, mem, project_id=project_id,
                                              give_back=False)
    else:
        facts_invalidated = 0
        audit = AgentForgetAudit(tenant_id=tenant_id, end_user_id=mem.end_user_id,
                                 project_id=mem.project_id, scope="memory",
                                 target_id=mem_public_id(mem.id), memories_forgotten=1,
                                 facts_invalidated=0)
        db.add(audit)
    db.flush()
    # Every fact that names the memory loses it, ended or not, by the
    # forget's rule (`_lose_the_source`), and only those it alone stated
    # go. The forget re-points the facts not ended only, and this deleted
    # by `source_memory_id`, so an ended fact another live memory states too
    # (superseded, forgotten, closed) was deleted with its history (audit
    # of 19 Oct 2026); one born elsewhere kept the erased id in its sources.
    from .entities import TABLES
    from ..memory_core.store import source_condition

    naming = (db.query(AgentFact)
                .filter(AgentFact.tenant_id == tenant_id, source_condition(TABLES, memory_uuid))
                .all())
    lost = _lose_the_source(naming, memory_uuid) + _made_of_its_words(db, tenant_id, memory_uuid)
    if mem.kind != SESSION:
        # What the facts kept still state, as the forget recounts it.
        recount_session_only(db, [f for f in naming if f not in lost])
    sole = [f.id for f in lost]
    # What the facts that go had closed, given back before they go (their
    # closings lose `invalidated_by_id` with them, by the foreign key), from
    # where each was closed: what closes it now never was. A memory forgotten
    # before (an install upgraded since) too.
    reopened = _give_back(db, tenant_id, mem, lost, erased=True)
    # Written before the memory's row goes: a fact still pointing at it
    # there would go with it, by the foreign key.
    db.flush()
    learned = aliases_learned_from(db, tenant_id, sole)
    _erase_links(db, tenant_id, memory_uuid, sole)
    n_facts = (db.query(AgentFact)
                 .filter(AgentFact.tenant_id == tenant_id, AgentFact.id.in_(sole))
                 .delete(synchronize_session=False)) if sole else 0
    taught = {n for a in learned for n in (a.alias_label, a.canonical_label) if n}
    undo_aliases(db, learned)
    _written_again(db, mem, taught)
    db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant_id,
                                 AgentMemory.id == memory_uuid).delete(synchronize_session=False)
    _erase_examples(db, tenant_id, [(mem.project_id, mem.end_user_id)])
    audit_id = audit_public_id(audit.id)
    db.commit()
    from ..end_user_places import after_the_commit

    after_the_commit(db, tenant_id)
    return {"id": mem_public_id(memory_uuid), "status": "erased", "erasure": "permanent",
            "facts_invalidated": facts_invalidated, "facts_deleted": int(n_facts or 0),
            "facts_reopened": reopened, "audit_id": audit_id}


def forget_memory(db: Session, tenant_id: UUID, memory_uuid: UUID,
                  *, project_id: Any) -> Optional[dict]:
    """Forget one memory + invalidate its facts + write an audit row. The
    docs' DELETE /v1/memories/:id contract: 'the memory and its facts drop out
    of every default read.' Returns None if the memory isn't found."""
    # Locked, and 'forgotten' (memory_core 2.11.0): a drain extracting it
    # meanwhile finds its claim gone and discards its write, and no drain
    # takes it again.
    mem = (
        memories_in_project(
            db.query(AgentMemory).filter(
                AgentMemory.tenant_id == tenant_id,
                AgentMemory.id == memory_uuid,
                AgentMemory.deleted_at.is_(None),
            ),
            project_id,
        )
        .with_for_update()
        .first()
    )
    if mem is None:
        return None
    facts_invalidated, audit, reopened = _forget(db, tenant_id, mem, project_id=project_id)
    db.commit()
    return {
        "id": mem_public_id(memory_uuid),
        "status": "forgotten",
        "facts_invalidated": facts_invalidated,
        # 31 Oct 2026: the facts this memory's facts had closed that are true
        # again, each as a new row (facts/given_back.py).
        "facts_reopened": reopened,
        "audit_id": audit_public_id(audit.id),
    }


class ClosedBeforeItBegan(ValueError):
    """`at` is earlier than the fact's own valid_from.

    The result would be a row with an end before its start: not history, but
    a fact that no point-in-time read can ever return. Carries both moments,
    because the fix is to pick one of them.

    The message prints the instants, not the days (audit of 17 Oct 2026): a
    fact that began at noon and was closed at eight the same morning was
    refused with "began on 2026-09-01 ... on 2026-09-01, which is earlier".
    """

    def __init__(self, fact_id: str, born, at):
        self.fact_id, self.born, self.at = fact_id, born, at
        super().__init__(
            f"{fact_id} began at {born.isoformat()} and cannot stop being true at "
            f"{at.isoformat()}, which is earlier. A fact closed before it started is "
            f"true on no date at all; closing it at the moment it began retracts it."
        )


def forget_fact(db: Session, tenant_id: UUID, fact_uuid: UUID,
                *, project_id: Any, at: Optional[datetime] = None
                ) -> Optional[dict]:
    """Manually forget a single fact (dashboard correction tool). Invalidates
    it (set invalid_at=now) so it drops out of the current state but stays in
    bi-temporal history — never a destructive row mutation. GDPR Art. 17. One
    audit row. Idempotent: forgetting an already-superseded fact is a no-op.
    Returns None if the fact isn't found under this account.

    A fact closed on a date still ahead is still true, and a forget brings
    that end earlier: to `at`, or to now. A later `at` leaves it where it is,
    since moving an end later revives the fact on days the store had it over.
    Until the audit of 17 Oct 2026 any end date at all read as "already
    forgotten", so a contract ended on 2030 and cancelled in June could never
    be closed in June: the answer said forgotten, and every read said true.

    `at` equal to the fact's valid_from retracts it: the row ends where it
    begins, [t, t), and is true at no moment, while it stays in the history
    (`include_invalidated`). That is how the store says "this was never
    true": a fact dated ahead that is forgotten ends at its start
    (`_invalidate_facts`), and so does one forgotten here with no `at`, since
    a fact never closes before it begins (CONTRIBUTING.md; audit of 17 Oct 2026: a
    bare forget of a fact dated ahead closed it now, before its start). Only
    an `at` earlier than the start is refused (`ClosedBeforeItBegan`)."""
    from datetime import datetime as _dt, timezone as _tz
    from . import closures
    from ...core.times import aware
    from .given_back import record_by_hand

    fact = load_fact(db, tenant_id, fact_uuid, project_id=project_id)
    if fact is None:
        return None
    now = _dt.now(_tz.utc)
    born = aware(fact.valid_from) if fact.valid_from is not None else None
    ends = aware(at) if at is not None else (max(now, born) if born is not None else now)
    # Already over (an end in the past), or ending by `at` anyway.
    already = fact.invalid_at is not None and (
        not true_now(fact, now) or aware(fact.invalid_at) <= ends)
    if not already:
        # `at` is the date it STOPPED being true, not the date you noticed. A
        # store that can only say "closed today" answers "what did we believe in
        # June" with June's mistake still in it, which is the question this
        # product exists to answer properly.
        #
        # But it cannot be before the fact began. A row with invalid_at earlier
        # than valid_from is true over an empty interval: no `as_of` query can
        # ever return it, at any date, so it is not history, it is a row that
        # quietly is not there. Found by driving the endpoint from an n8n node
        # and watching a fact close in June that had started in September.
        if born is not None and ends < born:
            raise ClosedBeforeItBegan(fact_public_id(fact.id), born, ends)
        fact.invalid_at = ends
    audit = record_by_hand(db, tenant_id, fact, invalidated=0 if already else 1)
    if not already:
        # In the forget's transaction, which commits both (audit of 17 Oct
        # 2026: it was enqueued after, and could be lost with the fact closed).
        closures.announce(db, tenant_id, fact_public_id(fact.id), None,
                          fact.invalid_at.isoformat())
    db.commit()
    return {
        "id": fact_public_id(fact.id),
        "status": "already_forgotten" if already else "forgotten",
        "invalid_at": fact.invalid_at.isoformat() if fact.invalid_at else None,
        "audit_id": audit_public_id(audit.id),
    }


def _lose_the_source(facts, memory_uuid: UUID) -> list:
    """2026-09-27 — a fact other memories still assert loses this source
    (its sources, its count, and `source_memory_id` re-pointed to one of
    the others) and stays; returns the facts this memory alone asserted.
    The rule of the forget and of the erasure of one memory alike, and of an
    edit's (memory_core `store.lose_the_source`, 6.2.0: one rule, not two
    copies of it)."""
    from .entities import TABLES
    from ..memory_core.store import lose_the_source

    return [f for f in facts if not lose_the_source(TABLES, f, memory_uuid)]


def _made_of_its_words(db: Session, tenant_id: UUID, memory_uuid: UUID) -> list:
    """The facts no memory states whose words came from this one: a
    correction of one of its facts (facts/service.correct_fact), which names
    no source, so that an edit or a forget of the memory leaves it, and keeps
    the memory as its first writer (`first_memory_id`). The erasure takes
    them with the facts the memory alone stated, as it took them up to 1.0.26,
    when a correction carried the memory as its source: what the memory said,
    in whatever row it was said again, goes (GDPR Art. 17). One another
    memory restates since (its `source_memory_ids`) is that memory's too, and
    stays, as every fact other memories still assert."""
    from .entities import TABLES
    from ..memory_core.store import sources_of

    rows = (db.query(AgentFact)
              .filter(AgentFact.tenant_id == tenant_id, AgentFact.first_memory_id == memory_uuid,
                      AgentFact.source_memory_id.is_(None))
              .all())
    return [f for f in rows if not sources_of(TABLES, f)]


def _give_back(db: Session, tenant_id: UUID, mem: AgentMemory, retracted: List[AgentFact],
               *, erased: bool = False) -> int:
    """What the facts `mem` alone stated, retracted now, had closed, given
    back where nothing else closes it (facts/given_back.py); `erased` when
    they are deleted after this. The closed facts are in the memory's scope,
    as the judge's candidates were. Returns how many."""
    from .entities import memory_scope
    from .given_back import after_retracting, ended_in_place_by

    scope = memory_scope(tenant_id, mem.project_id, mem.end_user_id)
    # And what the memory's own words ended in place (a past statement of it,
    # or an edit that took a fact away): they stood until its forget, and an
    # erased memory never said them.
    ended = ended_in_place_by(db, tenant_id, [mem.id], lambda q: q.filter(scope))
    return after_retracting(db, tenant_id, retracted, scope, erased=erased, ended=ended,
                            until=None if erased else mem.deleted_at, by=[mem.id])


def _forget(db: Session, tenant_id: UUID, mem: AgentMemory, *, project_id: Any,
            give_back: bool = True):
    """The forget of a live memory, locked by the caller, not committed:
    returns (the facts invalidated, the audit row added, the facts given
    back; none without `give_back`)."""
    from datetime import datetime as _dt, timezone as _tz

    memory_uuid = mem.id
    now = _dt.now(_tz.utc)
    mem.deleted_at = now
    mem.graph_sync_status = _worker.FORGOTTEN
    mem.graph_sync_claimed_at = None
    from .entities import TABLES
    from ..memory_core.store import source_condition

    facts = (
        facts_in_project(
            db.query(AgentFact).filter(
                AgentFact.tenant_id == tenant_id,
                source_condition(TABLES, memory_uuid),
                # Not ended, rather than true now (2026-10-06): a fact dated
                # ahead (a write's timestamp in the future, which is taken)
                # was not true yet, so it stayed open and came true days
                # after its memory was forgotten.
                not_ended(now),
            ),
            project_id,
        )
        .all()
    )
    soli = _lose_the_source(facts, memory_uuid)
    if mem.kind != SESSION:
        recount_session_only(db, [f for f in facts if f not in soli])
    facts_invalidated = _invalidate_facts(db, soli)
    # The facts these had closed, true again when nothing else closes them
    # (facts/given_back.py, decided on 31 Oct 2026).
    reopened = _give_back(db, tenant_id, mem, soli) if give_back else 0
    # memory_core 6.1.0: the object links it took part in go with it, as at
    # its erasure: a forgotten memory's words no longer decide how the
    # memory's names read, and the link's reason holds descriptions made
    # from them (the review of the rebuild, P2.2). Its facts are history,
    # read under their own names.
    _erase_links(db, tenant_id, memory_uuid, [])
    # 1.0.33: the vectors of its windows go too (services/memory_windows.py):
    # derived from its words, and no read of a forgotten memory uses them.
    # The row's own vector stays with its text, as before; an erasure
    # deletes the row, and the windows with it by their foreign key.
    from ..memory_windows import drop as _drop_windows

    _drop_windows(db, [memory_uuid])
    audit = AgentForgetAudit(
        tenant_id=tenant_id,
        end_user_id=mem.end_user_id,
        project_id=mem.project_id,
        scope="memory",
        target_id=mem_public_id(mem.id),
        memories_forgotten=1,
        facts_invalidated=facts_invalidated,
    )
    db.add(audit)
    return facts_invalidated, audit, reopened


def recount_session_only(db: Session, facts: List[AgentFact]) -> None:
    """`session_only` again for facts that just lost a source, from the
    sources they keep: true when every one is a work session
    (AgentMemory.kind), as the column says. A note's source detached (a
    forget, an erasure, an edit that no longer states the fact) can leave a
    fact only sessions state; it stayed false for good, and exclude_sessions
    kept showing it (audit of 19 Oct 2026). A session's source detached
    changes nothing, so the callers pass the facts a note left."""
    from ..memory_core.store import sources_of
    from .entities import TABLES

    facts = [f for f in facts if not f.session_only]
    sources = {f.id: sources_of(TABLES, f) for f in facts}
    ids = {UUID(s) for kept in sources.values() for s in kept}
    if not ids:
        return
    kinds = dict(db.query(AgentMemory.id, AgentMemory.kind)
                 .filter(AgentMemory.tenant_id.in_({f.tenant_id for f in facts}),
                         AgentMemory.id.in_(ids)).all())
    for f in facts:
        kept = sources[f.id]
        if kept and all(kinds.get(UUID(s)) == SESSION for s in kept):
            f.session_only = True


def after_the_retirement(db: Session, tenant_id: UUID, detached: List[AgentFact],
                         retired: List[AgentFact], scope, *, ended: List[AgentFact] = (),
                         until=None, by=()) -> int:
    """After an edit retired the facts its new text no longer states
    (memory_core `store.retire_the_previous_version`), in the write's
    transaction: the facts a note's previous version stated and that stay
    open are counted again (`recount_session_only`, audit of 19 Oct 2026),
    and what the retired facts had closed is given back where nothing, the
    new text's facts included (written and judged by now), closes it: an edit
    that takes a statement away retracts it as a forget does
    (facts/given_back.py, decided on 31 Oct 2026). So is what an earlier
    version ended in place and the new text no longer says is over (`ended`,
    its words, the memories `by`, standing until `until`, the new text's
    arrival). A new row
    announces nothing: no event says a fact is true. Returns how many came
    back."""
    from .given_back import after_retracting

    if detached:
        closed = {f.id for f in retired}
        recount_session_only(db, [f for f in detached if f.id not in closed])
    return after_retracting(db, tenant_id, retired, scope, ended=ended, until=until, by=by)


def stated_by_the_previous_version(db: Session, memory_id: UUID, kept: set,
                                   scope) -> List[AgentFact]:
    """The open facts the memory stated and this round did not write or
    restate: the rows `store.retire_the_previous_version` takes, by its
    own condition (`source_condition` within the memory's scope, not ended,
    not in `kept`), read before it detaches the memory from them."""
    from ..memory_core.store import source_condition
    from .entities import TABLES

    q = db.query(AgentFact).filter(source_condition(TABLES, memory_id, scope=scope),
                                   not_ended())
    if kept:
        q = q.filter(~AgentFact.id.in_(list(kept)))
    return q.all()


def erase_what_it_leaves(db: Session, tenant_id: UUID, *, project_id: Any, end_user_id=None,
                      agent_id=None) -> dict:
    """What an erasure leaves elsewhere, taken away in its transaction
    (memory_core 2.11.0, the code audit of 6 Oct 2026):

    - the entity aliases learned from the erased end user's or agent's facts
      ("my wife" for a name), which carry their words;
    - the examples of the vocabulary: a relation keeps the object of the fact
      it was learned from and a centroid made of that text, and both went on
      into the prompts written for everybody else
      (`store.examples_after_the_erasure`, which keeps an example still
      stated by another fact and recomputes the centroid, or clears it).

    The vocabulary is one per end user of a project (18 Oct 2026), learned
    from that end user's facts alone: the erasure of an end user takes their
    whole vocabulary (relations, phrases, votes, examples, centroids), and
    any other erasure leaves a relation's example only while a fact of its
    own end user still states it."""
    from ..end_user_places import aliases_behind, vocabulary_behind

    aliases = aliases_behind(db, tenant_id, project_id=project_id, end_user_id=end_user_id,
                             agent_id=agent_id)
    vocabulary = vocabulary_behind(db, tenant_id, project_id=project_id,
                                   end_user_id=end_user_id) if end_user_id is not None else 0
    # The relations an erasure here can reach: the project's, or one end
    # user's in it.
    within = within_project(db.query(PredicateCluster.id).filter(
        PredicateCluster.tenant_id == tenant_id), PredicateCluster.project_id, project_id)
    if end_user_id is not None:
        within = within.filter(PredicateCluster.end_user_id == end_user_id)
    return {"aliases": aliases, "relations": vocabulary,
            "examples": _erase_examples(db, tenant_id, relations=within)}


def _written_again(db: Session, mem: AgentMemory, names: set) -> int:
    """The kept facts the erased memory `mem` wrote first under a name of
    the rename it taught (`names`: both names of each alias it taught, undone
    by now), deleted; for those still open, the memories that still state
    them put back on the extraction queue, as an edit puts them
    (memory_engine.update): their own words write them again.

    A restatement by another memory only adds that memory to the fact's
    sources (memory_core `store.reconfirm`); the labels stay the first
    writer's. "Acme is now called Globex" and "Globex hired Bob" erased,
    "Acme hired Bob" kept: the fact stayed "Globex hired Bob", a name only
    the erased memory said, and the undo of the alias recomputed its
    canonicals from those labels, so the name stayed readable through GET
    /v1/facts (audit of 27 Oct 2026: a memory's erasure left derived copies).
    Only those facts: any other kept fact carries a name another
    memory or a declared alias still states. Returns how many.

    The first writer is `first_memory_id`, which the database sets at the
    insert and nothing moves (db/triggers.py). Until the audit before 1.0.0
    it was `source_memory_id` read before this erasure's forget, which a forget
    made earlier, or an edit of the memory (`store.retire_the_previous_
    version`), had already re-pointed to the kept memory: the erasure
    found none of them and the name stayed (the same finding, and its
    review: inferring the writer from the labels deleted facts it never
    wrote, a correction among them).

    A fact no longer open (closed by a newer one, its memories forgotten)
    is history the extraction does not write again: it goes, and nothing is
    queued for it. A correction made since (`PATCH /v1/facts/{id}`) is a
    statement of its own, with its own first writer (the memory that stated
    the corrected fact when it was corrected), and stays.

    Set based, nothing loaded: the facts locked, in one statement, then the
    memories that still state them, as the drain's write takes a fact before
    its memory's claim (memory_core `worker`); the memories queued, then
    the facts deleted, one statement each. Not a full order: the drain
    takes its facts in the order it writes them, not by id, and the facts
    the forget above re-pointed (`_lose_the_source`) are locked already. The names compared lowercased by
    the database on both sides, as `store.find_open` compares them
    (memory_core 2.11.0)."""
    from sqlalchemy import cast, func, literal, or_, select, union
    from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

    from ...db.bounded import lock_rows

    if not names:
        return 0
    F, M = AgentFact, AgentMemory
    keys = [func.lower(func.trim(literal(n))) for n in sorted(names)]
    theirs = and_(F.tenant_id == mem.tenant_id, F.first_memory_id == mem.id,
                  or_(func.lower(func.trim(F.subject_label)).in_(keys),
                      func.lower(func.trim(F.object_label)).in_(keys)))
    db.flush()
    if not lock_rows(db, db.query(F.id).filter(theirs).order_by(F.id)):
        return 0
    still = and_(theirs, not_ended())
    stating = union(
        select(F.source_memory_id).where(still, F.source_memory_id.isnot(None)),
        select(cast(func.jsonb_array_elements_text(cast(F.source_memory_ids, JSONB)),
                    PG_UUID(as_uuid=True))).where(still, F.source_memory_ids.isnot(None)),
    ).subquery()
    again = (M.tenant_id == mem.tenant_id, M.id != mem.id, M.deleted_at.is_(None),
             M.id.in_(select(stating.c[0])))
    lock_rows(db, db.query(M.id).filter(*again).order_by(M.id))
    (db.query(M).filter(*again)
       .update({M.graph_sync_status: "pending", M.graph_sync_claimed_at: None},
               synchronize_session=False))
    return int(db.query(F).filter(theirs).delete(synchronize_session=False) or 0)


def aliases_learned_from(db: Session, tenant_id: UUID, source_facts: List[UUID]) -> list:
    """The aliases these facts taught (an identity statement, "Acme is now
    Globex"), read before the facts go: their `source_fact_id` is ON DELETE
    SET NULL, so after it nothing names them. Also read by an erasure made
    again up to a moment (end_user_places._erase_facts)."""
    if not source_facts:
        return []
    return (db.query(AgentEntityAlias)
            .filter(AgentEntityAlias.tenant_id == tenant_id,
                    AgentEntityAlias.source_fact_id.in_(source_facts)).all())


def _erase_links(db: Session, tenant_id: UUID, memory_uuid: UUID, source_facts: List[UUID]) -> int:
    """The object links an erased memory took part in (`erase_memory`), read
    before its facts go (`source_fact_id` is ON DELETE SET NULL). The memory
    columns are ON DELETE CASCADE as well; deleted here, before the row,
    whatever the database's order. Returns how many."""
    from sqlalchemy import or_

    L = AgentObjectLink
    which = [L.source_memory_id == memory_uuid, L.canonical_memory_id == memory_uuid]
    if source_facts:
        which.append(L.source_fact_id.in_(source_facts))
    return int(db.query(L).filter(L.tenant_id == tenant_id, or_(*which))
               .delete(synchronize_session=False) or 0)


def undo_aliases(db: Session, rows: list) -> int:
    """Each alias an erased memory taught, undone as the engine undoes one
    (`store.remove_alias`: the row goes and the memory's facts take their
    names again), after its own facts are deleted, so no merge the
    recanonicalization makes can keep an erased row and drop a kept one.

    The chain it flattened is put back with it (the code audit of 18 Oct
    2026: the bulk delete left the new name in the kept facts and in
    the aliases that had pointed at the old one). When "acme -> Globex" was
    learned, every alias of the memory pointing at "acme" was rewritten to
    point at "Globex" (`store.add_alias`); each alias keeps what it
    was stated with (`stated_canonical`), and the undo points again exactly
    the rows whose stated chain went through the rule
    (`store.restore_the_chain`, inside `remove_alias`). Until the audit of
    20 Oct 2026 this was inferred here, by age and by the alias's own
    fact, and a "GX -> Globex" declared by hand before the rename, which the
    rule had never touched, became "GX -> Acme". A fact whose names change
    has its vector computed again (`entities.TABLES.text_changed`). Names of
    products and organizations only: the engine never makes an alias for a
    person by itself (`identity.TYPES_THAT_RENAME`). The same object in two
    languages (memory_core 6.1.0) is a link, not an alias, never asked in the
    people family, and taken by `_erase_links`."""
    from ..memory_core.store import remove_alias
    from .entities import TABLES, memory_of

    undone = 0
    for row in rows:
        mem = memory_of(row.tenant_id, row.project_id, row.end_user_id, row.agent_id)
        remove_alias(db, TABLES, mem, row)
        undone += 1
    return undone


# How many (project, end user) vocabularies one statement of `_erase_examples`
# asks about.
_PAIRS = 200


def _erase_examples(db: Session, tenant_id: UUID, pairs=None, *, relations=None) -> int:
    """The vocabulary's examples no fact of their end user states any more,
    after the facts are deleted (`store.examples_after_the_erasure`):
    only the relations the erased facts could be in, the (project, end user)
    `pairs` they belonged to, or the query of relation ids `relations`
    (audit of 27 Oct 2026: every relation of the account was read and decided again, one
    statement each, for an erasure of one memory). The vocabulary is one per
    end user of a project (18 Oct 2026), so no other relation holds a value
    of theirs.

    No embedder (audit of 20 Oct 2026): a relation whose example
    changed loses its centroid, the copy of the erased value, here, and the
    vector drain computes it again from the new example
    (`predicates.fill_missing_centroids`), in the project's region and
    counted to the account (`regions.writing_for`, `llm_usage.attributed`).
    Computed here it ran outside both: with EMBEDDING_PROVIDER=gemini the
    call was nobody's and in no project's region. Meanwhile the relation is
    only not offered to a new phrase by similarity."""
    from sqlalchemy import or_

    from ..memory_core.store import examples_after_the_erasure
    from .entities import TABLES

    def facts_of(rel):
        return and_(AgentFact.tenant_id == rel.tenant_id,
                    AgentFact.project_id.is_not_distinct_from(rel.project_id),
                    AgentFact.end_user_id.is_not_distinct_from(rel.end_user_id))

    def decided(condition) -> int:
        return examples_after_the_erasure(
            db, TABLES, and_(PredicateCluster.tenant_id == tenant_id, condition), facts_of,
            embed=None)

    if relations is not None:
        return decided(PredicateCluster.id.in_(relations.subquery().select()))
    pairs = sorted(set(pairs or ()), key=str)
    n = 0
    for i in range(0, len(pairs), _PAIRS):
        n += decided(or_(*[and_(PredicateCluster.project_id.is_not_distinct_from(p),
                                PredicateCluster.end_user_id.is_not_distinct_from(u))
                           for p, u in pairs[i:i + _PAIRS]]))
    return n


def forget_end_user(db: Session, tenant_id: UUID, end_user_id: str,
                    *, project_id: Any) -> dict:
    """ERASE one end user: every memory and fact carrying that end_user_id is
    physically deleted, not flagged. This is the GDPR Art. 17 endpoint, so it
    has to actually erase.

    Until 2026-09-08 this soft-deleted instead: memories got `deleted_at`, facts
    got `invalid_at`, and the content stayed readable to the same API key via
    `include_invalidated=true`. Default reads were clean, so it looked right, but
    "the data is still there and you can still read it" is not erasure. For a
    product that sells EU data handling, that is the detail a buyer checks first.

    The bi-temporal supersede model is untouched and still correct: it describes
    how knowledge evolves, where nothing should be lost. Erasure is a different
    operation answering a legal right, and the two must not be confused.

    Rows deleted here are unrecoverable. Soft forgetting still exists per memory
    (`forget_memory`), which is honestly documented as dropping out of default
    reads rather than as erasure.

    The end user's items in /v1/batch payloads go too (services/batch.
    erase_items), queued or finished: a payload is a copy of the memories, and
    until 2026-09-28 it kept their text after this said it was erased, and a
    queued job imported them again.

    The audit row survives on purpose: Art. 5(2) accountability means we must be
    able to show the erasure happened. It carries counts, never the erased
    content, and since 0.1.27 a keyed pseudonym of the end user instead of
    their id, as every row the erasure leaves does (services/pseudonyms.py:
    the access trail too; the cost rows never name an end user; a delivered
    webhook takes the pseudonym). GET /v1/audit?user_id=<id> still finds them."""
    from ...core.threads import longer_deadlines

    # An erasure's deadlines (core/threads.longer_deadlines): it waits for
    # the writes of this person under way; theirs stay a request's.
    longer_deadlines(db)
    # Every place that holds the person, in the order of the one list of
    # them (services/end_user_places.py, audit of 20 Oct 2026): the /v1/batch
    # payloads first and under the jobs' locks, the memories locked, the
    # facts, the memories, the aliases and the vocabulary learned from them.
    # Scoped, like everything a key can reach. An erasure is the worst place
    # to cross the wall: a key from one project asking to erase an end user it
    # shares a name with would physically delete another project's rows, and an
    # erasure leaves nothing behind to notice it by.
    from ..end_user_places import END_USER, after_the_commit, erase_person, facts_taken, the_moment
    from .given_back import Closed, give_back, what_they_closed

    # What the facts that go had closed, given back after they go, as the
    # agent namespace's door does (`delete_agent`; facts/given_back.py: an
    # erased closer never was, and the fact comes back as a new row its
    # live sources state, never reopened in place, Graphiti, Rasmussen et
    # al. 2025, section 2.2.3). Only for an erasure made again up to a
    # moment (end_user_places.up_to), the one that keeps facts of the
    # person: any other takes every fact of theirs, and the judge closes
    # only facts of the same end user (`entities.memory_scope`), so nothing
    # it closed stays (review of 16 Nov 2026, round 5: a fact a memory
    # written since restated stayed closed by one extracted late from a
    # memory of before). Read once the locks are held, before the rows go.
    closed, locked = Closed(), None
    if the_moment() is not None:
        theirs = facts_taken(db, tenant_id, (END_USER, end_user_id),
                             project_id).with_entities(AgentFact.id)

        def read_what_they_closed() -> None:
            nonlocal closed
            closed = what_they_closed(
                db, tenant_id, theirs.subquery().select(),
                within=lambda q: within_project(q, AgentFact.project_id, project_id))

        locked = read_what_they_closed

    gone = erase_person(db, tenant_id, (END_USER, end_user_id), project_id=project_id,
                        locked=locked)
    give_back(db, closed)
    n_facts, n_mems = gone.get("facts", 0), gone.get("memories", 0)
    # Only the end user's own relations held their facts' values, and the
    # erasure took those whole (the vocabulary place): nothing else to scan.
    _erase_examples(db, tenant_id, relations=within_project(
        db.query(PredicateCluster.id).filter(PredicateCluster.tenant_id == tenant_id,
                                             PredicateCluster.end_user_id == end_user_id),
        PredicateCluster.project_id, project_id))
    # What stays names the end user by a pseudonym (0.1.27), this row too.
    from ..pseudonyms import after_an_erasure, pseudonym

    after_an_erasure(db, tenant_id, end_user_id, project_id=project_id)
    audit = AgentForgetAudit(
        tenant_id=tenant_id,
        end_user_id=pseudonym(tenant_id, end_user_id),
        project_id=_audit_project(project_id),
        scope="user",
        target_id=None,
        memories_forgotten=n_mems,
        facts_invalidated=n_facts,
        created_at=_ENDED,
    )
    db.add(audit)
    db.commit()
    # The Markdown export's copy (FACTS_EXPORT_DIR), at once.
    after_the_commit(db, tenant_id)
    # Until 0.1.42 the answer also carried the names it was first published
    # with, from when erasure was a soft delete (`memories_forgotten`,
    # `facts_invalidated`, the same numbers); they went in 0.2.0, as the
    # CHANGELOG said from 0.1.41.
    return {
        "user_id": end_user_id,
        "memories_deleted": n_mems,
        "facts_deleted": n_facts,
        **parts_deleted(gone, tenant_id),
        "erasure": "permanent",
        "audit_id": audit_public_id(audit.id),
    }


def parts_deleted(gone: dict, tenant_id) -> dict:
    """`<place>_deleted` for each place of a part of the product served to
    the account `tenant_id` (core/extensions.served, Part.places), from what
    `erase_person` erased: what an answer names is what the account is
    served. Nothing on an install of one's own, nor of a part the account is
    not served, whose rows the erasure takes all the same."""
    from ...core import extensions

    return {f"{place.name}_deleted": gone.get(place.name, 0)
            for part in extensions.served(tenant_id) for place in part.places()}


def delete_agent(db: Session, tenant_id: UUID, agent_id: str,
                 *, project_id: Any) -> dict:
    """HARD-delete every memory + fact written under one agent_id namespace,
    like forget_end_user does for one end user (a hard delete since
    2026-09-08; this said "SOFT, keeps the rows" until 2026-09-28), because the namespace set (distinct_agent_ids, what GET /v1/agents lists)
    counts ANY row carrying the agent_id, including soft-deleted/invalidated
    ones. So a forgotten agent is still listed; only a hard delete removes it.
    This is the dev clearing their own throwaway/typo agent_id. (On the hosted
    service the same set is counted against a per-plan cap; there is none here.)

    AgentFact.source_memory_id is ON DELETE CASCADE, so deleting the memories would
    also drop their facts — but we delete facts FIRST (and by agent_id directly) so
    facts written straight to /v1/facts (no source memory) are purged too. One audit
    row records the purge (scope='agent')."""
    # As for an end user (`forget_end_user`), every place that holds the
    # namespace (services/end_user_places.py): a queued /v1/batch job would
    # write it back, a finished one still holds its text; locked first, so
    # the count is true.
    from ...core.threads import longer_deadlines
    from ..end_user_places import AGENT, after_the_commit, erase_person

    longer_deadlines(db)   # an erasure's deadlines, as for an end user
    # The (project, end user) vocabularies the namespace's facts are in, read
    # before they go: only those relations can lose their example. Streamed,
    # every one of them: a cap here left a relation past it with the erased
    # fact's value as its example (review of the audit of 27 Oct 2026). Of
    # every fact of the namespace, also when an erasure made again keeps
    # the newer ones (end_user_places.up_to): a relation keeps an example a
    # fact still states (`store.examples_after_the_erasure`), so a vocabulary
    # only the facts that stay are in costs one decision more, and the facts
    # that go are not told by a date (`end_user_places.facts_taken`: one
    # extracted late from a memory of before goes too).
    from ...db.bounded import STREAM_ROWS
    from ..end_user_places import facts_taken

    pairs = {tuple(p) for p in within_project(
        db.query(AgentFact.project_id, AgentFact.end_user_id)
          .filter(AgentFact.tenant_id == tenant_id, AgentFact.agent_id == agent_id),
        AgentFact.project_id, project_id).distinct().yield_per(STREAM_ROWS)}
    # The facts of other agents the namespace's facts closed (the judge's
    # candidates are the end user's, of any agent), read once the erasure
    # holds its locks, so a write of the namespace under way has committed
    # its closings, and given back after the rows go, from where each was
    # closed: an erased closer never was (facts/given_back.py, 31 Oct 2026).
    # The facts the erasure takes: up to its moment, a fact a memory of the
    # namespace written since states stays, and what it closed stays closed.
    from .given_back import Closed, give_back, what_they_closed

    theirs = facts_taken(db, tenant_id, (AGENT, agent_id), project_id).with_entities(AgentFact.id)
    closed = Closed()

    def read_what_they_closed() -> None:
        nonlocal closed
        closed = what_they_closed(
            db, tenant_id, theirs.subquery().select(),
            within=lambda q: within_project(q, AgentFact.project_id, project_id))

    gone = erase_person(db, tenant_id, (AGENT, agent_id), project_id=project_id,
                        locked=read_what_they_closed)
    n_facts, n_mems = gone.get("facts", 0), gone.get("memories", 0)
    reopened = len(give_back(db, closed))
    _erase_examples(db, tenant_id, pairs)
    audit = AgentForgetAudit(
        tenant_id=tenant_id,
        end_user_id=None,
        project_id=_audit_project(project_id),
        scope="agent",
        target_id=agent_id[:MAX_ID_CHARS],
        memories_forgotten=n_mems,
        facts_invalidated=n_facts,
        created_at=_ENDED,
    )
    db.add(audit)
    db.commit()
    after_the_commit(db, tenant_id)
    return {
        "agent_id": agent_id,
        "memories_deleted": n_mems,
        "facts_deleted": n_facts,
        **parts_deleted(gone, tenant_id),
        "facts_reopened": reopened,
        # The marks read under the agent's name (agent notices), deleted
        # with the namespace (services/end_user_places.py).
        "read_marks_deleted": gone.get("read_marks", 0),
        "audit_id": audit_public_id(audit.id),
    }
