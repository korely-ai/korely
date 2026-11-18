"""Which facts a named reader was handed as true, and when: the read marks
of the agent notices (1.0.15, their first part).

A read that names its reader (`reader` on GET /v1/context, GET /v1/facts,
korely_get_context and korely_get_facts) leaves one row per fact it handed
over in `agent_read_marks` (db/models.AgentReadMark); a second read of the
same fact by the same reader moves `last_read_at` and makes no second row.
Nothing else changes: `reader` filters nothing, and a read without it runs
none of this, so its answer and its rows are those of a release without
the marks.

**Only a read of what is true now marks.** A mark means "this reader was
handed this fact as true": a read of the past (`as_of`, `known_as_of`) or
of the history (`include_invalidated`) hands over facts that had ended,
and the reader saw their end. So the notices can tell a change from the
state of the fact alone (handed as true, ended since, not told yet),
without ordering two clocks: `closed_known_at` is the start of the closing
transaction, which a read that began after it can still see open until it
commits, and so no time of the read alone says whether it came before the
closure.

**A mark never fails the read**, as the audit row (services/audit.py): it
is written after the answer is built, in a session of its own, and a
failure is logged by its class (log_safe) and counted for /health
(`records.read_marks`, services/bookkeeping.py), never raised. **Nor holds
it long**: it waits a fifth of a second for a lock and a second for its
statements (core/threads.mark_deadlines), and then gives way, to an erasure
of the same rows, a deletion, a late judgement holding a fact, a migration
holding the table: counted apart (`gave_way`), and no failure of
the install (`in_own_session`). What it names is locked first, as its
foreign keys would lock it, so a fact, a project or an account deleted
meanwhile is skipped (`dropped`); a fact another transaction holds is
marked in a second pass, after the others, so it is never the reason the
read's other facts go unmarked.

**No text**: the fact's id, the reader's name, the fact's end user (copied
from the fact, never the request's) and three times. The rows of a fact
come from the fact itself, within the read's account and project, so a
mark can name only a fact the read could see.

The reader is the caller's word for itself, by the rule of an `agent_id`
(limits.id_problem), not authenticated: with one key for several agents,
one agent can name itself as another. It never sees more than its key.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, List, Optional
from uuid import UUID

from sqlalchemy import String, func, literal, or_, select
from sqlalchemy.orm import Session

from ..config import AgentSettings
from .memory_core import log_safe

logger = logging.getLogger(__name__)

#: How long a mark is kept by default, the setting's own default.
DEFAULT_RETENTION_DAYS = AgentSettings.model_fields["read_marks_retention_days"].default

#: What `reader` is, in the words of every door that takes it (REST's query
#: parameter, the MCP tools' description).
READER = ("Your agent's own name, by the rule of `agent_id`. With it, this read keeps the ids "
          "of the facts it hands over as true, with the time, for "
          f"{DEFAULT_RETENTION_DAYS} days after the last read unless the install sets "
          "READ_MARKS_RETENTION_DAYS, and the context block of this reader opens with what "
          "changed since in the facts it was handed (agent notices), each change told at most "
          "once. "
          "It filters nothing, and a read of the past (`as_of`, `known_as_of`) or of the "
          "history (`include_invalidated`) keeps nothing and tells nothing. Not "
          "authenticated: whoever holds the key can give any name.")


def reader_problem(reader: Optional[str]) -> Optional[str]:
    """Why `reader` cannot be a reader's name, or None: the rule of an
    `agent_id` a write gives (ids.namespace_id_problem), since a reader is an
    agent's name and its marks go with that agent namespace's deletion
    (DELETE /v1/agents/{agent_id}). A name that rule refuses (empty, '.',
    '..', a '/', a line break or any other control character) could never be
    addressed there, and a read took it by the looser rule of what a read
    narrows on (review of piece 3, 9 Nov 2026)."""
    from .ids import namespace_id_problem

    if reader is None:
        return None
    problem = namespace_id_problem(reader)
    return f"reader {problem}" if problem else None


def reads_now(*, as_of: Optional[datetime] = None, known_as_of: Optional[datetime] = None,
              include_invalidated: bool = False) -> bool:
    """Whether a read with these moments reads what is true now, the only
    read that marks."""
    return as_of is None and known_as_of is None and not include_invalidated


def _fact_ids(facts_read: Iterable) -> List[UUID]:
    """The facts' UUIDs, from UUIDs or `fct_` ids, once each."""
    from .ids import parse_fact_id

    out: List[UUID] = []
    for f in facts_read:
        u = f if isinstance(f, UUID) else parse_fact_id(f)
        if u is not None and u not in out:
            out.append(u)
    return out


def _one_per_reader_and_fact():
    """The unique index of `agent_read_marks`: one row per account, project,
    reader and fact."""
    from ..db import AgentReadMark

    (index,) = [i for i in AgentReadMark.__table__.indexes if i.unique]
    return index


def in_own_session(what: str, n: int, tenant_id: UUID, work: Callable[[Session], Any]) -> Any:
    """`work(own)` in a session of its own with a mark's deadlines
    (core/threads.mark_deadlines: a fifth of a second for a lock, a second
    for a statement), committed: its result, or None when it could not be
    done. Never raises: the bookkeeping of the notices never fails, nor holds,
    the read it rides on. A deadline passed is the bookkeeping giving way to
    another transaction (a deletion, an erasure, a late judgement, a
    migration holding the rows or the table), as it is meant to: counted
    apart (`gave_way`, `n` facts), logged as a warning, and /health stays ok
    (review of piece 1, 8 Nov 2026: it read failing). Anything else is
    logged by its class and codes and counted lost: the class and codes
    only, since the parameters are a reader's name and ids and a driver's
    message can quote them."""
    from ..core.threads import deadline_passed, mark_deadlines
    from ..db import get_own_sessionmaker
    from .bookkeeping import READ_MARKS

    own = get_own_sessionmaker()()
    try:
        mark_deadlines(own)
        out = work(own)
        own.commit()
        READ_MARKS.kept()
        return out
    except Exception as e:
        if deadline_passed(e):
            logger.warning("[read_marks] gave way, a deadline passed: %s %d fact(s) for tenant "
                           "%s: %s", what, n, tenant_id, log_safe.error(e))
            READ_MARKS.gave_way(n)
        else:
            logger.error("[read_marks] could not %s %d fact(s) for tenant %s: %s", what, n,
                         tenant_id, log_safe.stack(e))
            READ_MARKS.lost(n, e)
        try:
            own.rollback()
        except Exception:
            pass
        return None
    finally:
        own.close()


def _marking(tenant_id: UUID, project_id: Any, reader: str, ids: List[UUID], *,
             wait: bool):
    """The statement that marks `ids`, returning the facts it marked. From
    the facts, never from the request: their own end user and project, and
    only those of the read's account and project. Locked FOR KEY SHARE, the
    lock the foreign key takes on each of them, taken by the SELECT instead:
    a fact deleted by a transaction not yet committed is waited for and then
    skipped, where the foreign key's own check refused the whole statement
    and every other fact of the read went unmarked (review of piece 1, 8 Nov
    2026). Without `wait`, a fact locked so (FOR UPDATE: a deletion, a late
    vector's judgement) is skipped at once, SKIP LOCKED, so it never costs the
    read's other facts their marks. In the order of the unique index, so two
    reads of the same facts take their rows in one order."""
    from sqlalchemy.dialects.postgresql import insert

    from ..db import AgentFact, AgentReadMark

    handed = (select(func.gen_random_uuid(), AgentFact.tenant_id, AgentFact.project_id,
                     literal(reader, String), AgentFact.id, AgentFact.end_user_id)
              .where(AgentFact.tenant_id == tenant_id, AgentFact.id.in_(ids),
                     AgentFact.project_id.is_not_distinct_from(project_id))
              .order_by(AgentFact.id)
              .with_for_update(read=True, key_share=True, skip_locked=not wait))
    m = AgentReadMark
    stmt = insert(m).from_select(
        [m.id, m.tenant_id, m.project_id, m.reader, m.fact_id, m.end_user_id], handed,
        include_defaults=False)
    # The conflict is the table's own unique index, read from the model.
    return (stmt.on_conflict_do_update(index_elements=list(_one_per_reader_and_fact().columns),
                                       set_={m.last_read_at: func.now()})
            .returning(m.fact_id))


def mark(tenant_id: UUID, project_id: Any, reader: Optional[str], facts_read: Iterable, *,
         as_of: Optional[datetime] = None, known_as_of: Optional[datetime] = None,
         include_invalidated: bool = False) -> None:
    """`reader` was handed `facts_read` (UUIDs or `fct_` ids) by a read in
    `project_id` (the project the read was made in: `keys.key_project`, the
    MCP's resolved one). Nothing without a reader, or for a read that is not
    of now (`reads_now`). Never raises.

    Two passes, each in its own transaction (`in_own_session`): every fact
    not locked by another transaction at once, then, only when some were,
    those, waiting for their lock up to a fifth of a second. A fact whose
    deletion commits within it is skipped and counted dropped; one still
    locked then gives way (`gave_way`): unmarked, no failure, and the read's
    other facts keep their marks."""
    if reader is None or not reads_now(as_of=as_of, known_as_of=known_as_of,
                                       include_invalidated=include_invalidated):
        return
    ids = _fact_ids(facts_read)
    if not ids:
        return
    from .bookkeeping import READ_MARKS

    def marked(wanted: List[UUID], wait: bool):
        def work(own):
            # The account's and the project's rows first, as a deletion takes
            # them; False when either is gone: then nothing is marked.
            if not _what_it_names_is_there(own, tenant_id, project_id):
                return False
            return set(own.execute(_marking(tenant_id, project_id, reader, wanted,
                                            wait=wait)).scalars())
        return in_own_session("mark", len(wanted), tenant_id, work)

    first = marked(ids, wait=False)
    if first is None:
        return
    if first is False:
        READ_MARKS.dropped(len(ids))
        return
    left = [i for i in ids if i not in first]
    if left:
        again = marked(left, wait=True)
        if again is None:
            return
        first |= again or set()
    if len(first) < len(ids):
        # Gone with what they would name (a fact deleted meanwhile, its
        # project, its account): nothing to tell anybody about, so no
        # failure, but said.
        READ_MARKS.dropped(len(ids) - len(first))


def claim(tenant_id: UUID, project_id: Any, reader: str, facts_shown: Iterable) -> Optional[set]:
    """Reserve the changes of these facts (UUIDs or `fct_` ids) for the
    answer that is about to tell them: their marks' `notified_at` set where
    it was not, in one statement, and the `fct_` ids so reserved returned.
    Only what an answer reserved is told, so two answers at once never tell
    the same change (review of piece 2, 8 Nov 2026: both told the same 30):
    the second waits for the first's rows and finds them set. The purge
    takes the rows at its next pass. None when it could not be done (it gave
    way to a lock, or failed: `in_own_session`): nothing reserved, nothing
    told, and the changes wait for the next read."""
    from .ids import fact_public_id

    ids = _fact_ids(facts_shown)
    if not ids:
        return set()
    from sqlalchemy import update

    from ..db import AgentReadMark

    m = AgentReadMark

    def work(own):
        return {fact_public_id(i) for i in own.execute(
            update(m).where(m.tenant_id == tenant_id, m.project_id.is_not_distinct_from(project_id),
                            m.reader == reader, m.fact_id.in_(ids), m.notified_at.is_(None))
            .values(notified_at=func.now()).returning(m.fact_id)).scalars()}
    return in_own_session("reserve the changes of", len(ids), tenant_id, work)


def _what_it_names_is_there(own: Session, tenant_id: UUID, project_id: Any) -> bool:
    """The account's row, then the project's, locked FOR KEY SHARE before the
    facts: the locks the marks' foreign keys would take on them, taken first
    and in the order a deletion takes them (an account's closure locks its
    row, then its facts; a project's deletion its row, then its facts), so a
    mark never holds a fact while it waits for its project or its account.
    False when either is gone: then nothing is marked."""
    from ..db import AgentProject, Tenant

    if own.execute(select(Tenant.id).where(Tenant.id == tenant_id)
                   .with_for_update(read=True, key_share=True)).first() is None:
        return False
    return project_id is None or own.execute(
        select(AgentProject.id).where(AgentProject.id == project_id,
                                      AgentProject.tenant_id == tenant_id)
        .with_for_update(read=True, key_share=True)).first() is not None


def purge_expired(db: Session, keep_days: int) -> int:
    """The marks not read again for `keep_days` days (READ_MARKS_RETENTION_
    DAYS, 90; 0 keeps them), and every mark whose change was shown
    (`notified_at`), which has nothing left to tell. On the database's
    clock, the one the marks are written on."""
    from ..core.times import at_most_a_century
    from ..db import AgentReadMark

    gone = [AgentReadMark.notified_at.isnot(None)]
    if keep_days > 0:
        keep = at_most_a_century("read_marks_retention_days", keep_days, 1)
        gone.append(AgentReadMark.last_read_at < func.now() - timedelta(days=keep))
    n = int(db.query(AgentReadMark).filter(or_(*gone)).delete(synchronize_session=False) or 0)
    db.commit()
    if n:
        logger.info("[read_marks] purged %d marks (older than %d days, or told)", n, keep_days)
    return n
