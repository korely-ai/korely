"""Batch import for /v1/batch — bulk migration from other stores.

POST enqueues an AgentBatchJob (status pending); a scheduler-drained worker
imports each memory through the SAME pipeline as a single POST (engine.add +
fact extraction), so migrated memories get the full intelligence treatment.
On an install of one's own nothing is metered. On the Cloud (KORELY_MODE=
hosted, the same code) POST /v1/batch counts the batch whole against the
month's writes, or refuses it 429, when the job is queued
(memory_write.admit); the worker does not count it again. This said "nothing
is metered" for both until the audit of 17 Oct 2026.

Mirrors the BackgroundJobOutbox pattern: atomic claim via
with_for_update(skip_locked), bounded attempts, exponential backoff on crash.

A payload is a copy of the memories it imports, text and metadata, and is
treated as one (2026-09-28): erasing an end user, or deleting an agent
namespace, scrubs their items from every payload in reach (`erase_items`);
the worker reads each item as it is when it imports it, under the job's lock
until its memory commits, so an erasure that lands while a job runs holds for
the rest of the job and for the item being imported; and finished jobs go
after FINISHED_JOB_RETENTION (`purge_finished`). Erasing a project deletes
its jobs outright (services/projects.delete_project), and takes the same
locks first, so the item being imported is erased with the project.

A job carries the key that queued it, and the worker imports an item only
while that key could still write in the job's project (`_key_still_writes`,
audit of 18 Oct 2026): revoked, expired or moved, or the project archived,
the rest of the job is recorded as not imported and dropped from the payload.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..db.models import AgentBatchJob
from . import memory_write
from .ids import job_public_id, mem_public_id
from .sentinels import EVERY_PROJECT, within_project
from .memory_core import log_safe

logger = logging.getLogger(__name__)

MAX_BATCH_ITEMS = 500
# The words a job's `status` takes, stored and answered as they are: POST
# /v1/batch and GET /v1/batch/{id} both read the row. The POST answered
# 'processing' for a job stored 'pending' until the audit of 17 Oct 2026,
# so a client polling until the status was no longer 'processing'
# stopped at once, on 'pending', with nothing imported.
#: Queued: no worker has claimed it yet.
STATUS_PENDING = "pending"
#: A worker is importing it now; `imported` and `failed` grow as it goes.
STATUS_PROCESSING = "processing"
#: Every item was tried: `imported` and `failed` are final.
STATUS_COMPLETED = "completed"
#: The job stopped on an error of the worker (never on a bad item, which is
#: that item's error). It is tried again, from where it stopped, until its
#: attempts are used; a failed job may still become 'completed'.
STATUS_FAILED = "failed"
STATUSES = (STATUS_PENDING, STATUS_PROCESSING, STATUS_COMPLETED, STATUS_FAILED)

#: What an item of a payload becomes when its end user is erased or its agent
#: namespace deleted, imported or not. A marker in its place rather than a
#: shorter list: a job resumes from `imported + failed`, so every other item
#: has to stay where it was.
ERASED_ITEM = {"erased": True}
#: What the job's errors say about an erased item the worker had not reached.
ERASED_BEFORE_IMPORT = "erased before it was imported"

#: How long a finished job is kept: completed, or failed with its attempts
#: used up. Its payload is a copy of every memory it carried, text and
#: metadata, outside the table that forgetting and erasure act on, and once
#: the import is over nothing reads it: GET /v1/batch/{id} answers counts and
#: errors, which a caller polls within minutes of the job finishing. A week
#: covers polling after a weekend and anybody reading a failure's errors on
#: Monday. A constant and not a setting next to the other retention windows:
#: those keep evidence (audit, usage, deliveries), where `0` keeping it
#: forever is a decision an operator can make; this keeps nothing but a
#: second copy of people's data, and forever is not a choice to offer.
FINISHED_JOB_RETENTION = timedelta(days=7)

#: What the job's errors say about the items left when the key that queued
#: it can no longer write in its project (`_key_still_writes`).
KEY_ENDED = ("not imported: the key that queued it was revoked, expired or moved to another "
             "project, or the project was archived")
#: What such an item becomes in the payload: the job is over, and the text of
#: an item that will never be imported has no reason to be kept for a week.
DROPPED_ITEM = {"dropped": True}


def enqueue_batch(db: Session, tenant_id: UUID, memories: list[dict],
                  *, project_id=None, api_key_id=None) -> AgentBatchJob:
    """Persist a batch job (status pending). Caller validated the payload size.
    `api_key_id`: the key that queued it, which the worker asks before every
    item (`_key_still_writes`); None for a caller that is not a key."""
    job = AgentBatchJob(
        tenant_id=tenant_id,
        project_id=project_id,
        api_key_id=api_key_id,
        status=STATUS_PENDING,
        received=len(memories),
        payload=memories,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def job_status(db: Session, tenant_id: UUID, job_uuid: UUID,
               *, project_id=EVERY_PROJECT, queued_by: Optional[UUID] = None) -> Optional[dict]:
    """A batch job's progress, or None.

    `project_id` is the wall: GET /v1/batch/{job_id} passes the key's project,
    so a key reads only the jobs its own project queued (None is the unfiled
    project, as `key_project` returns it on an install with no default). The
    job carries the project that queued it (enqueue_batch). Until 2026-09-28
    the lookup was by account alone: a key read another project's counts and
    per-item errors, and a 200 against a 404 confirmed the id (found on the
    hosted product the same day). EVERY_PROJECT is for account-level callers,
    never for something reached with a key.

    `queued_by`: a key confined to some end users (services/user_scope.py)
    reads only the jobs it queued itself. A job holds the items of every end
    user it was sent with, and its counts and per-item errors ("erased
    before it was imported") were the one aggregate a confined key read
    whole, the job's id handed to it by its own trail (audit of 20 Oct
    2026)."""
    q = within_project(db.query(AgentBatchJob).filter(AgentBatchJob.tenant_id == tenant_id,
                                                      AgentBatchJob.id == job_uuid),
                       AgentBatchJob.project_id, project_id)
    if queued_by is not None:
        q = q.filter(AgentBatchJob.api_key_id == queued_by)
    job = q.first()
    if job is None:
        return None
    return {
        "id": job_public_id(job.id),
        "status": job.status,
        "received": job.received,
        "imported": job.imported,
        "failed": job.failed,
        "errors": job.errors or [],
    }


def erase_items(db: Session, tenant_id: UUID, field: str, value: str,
                *, project_id: Any = EVERY_PROJECT) -> int:
    """Scrub from every batch payload in reach the items whose `field`
    (`user_id` or `agent_id`) is `value`: each becomes ERASED_ITEM, in its
    place. Queued, running and finished jobs alike, since a finished one's
    payload still holds the text. Returns how many items. Does not commit: the
    erasure it belongs to commits it with its own rows.

    `project_id` is the erasure's wall, the same one its memories and facts
    are deleted behind: a key reaches its own project's jobs and nothing else;
    EVERY_PROJECT is the account, never a key. Rows locked while they are
    rewritten, so two erasures on one job cannot each write back a payload the
    other had not scrubbed; in the order of their ids, the order a project's
    deletion takes its own jobs in (services/projects._hold_the_jobs), so two
    erasures waiting on each other's jobs cannot deadlock.

    A job that lost an item also loses `last_error`, the text of an exception:
    a database error quotes the parameters of the statement that failed, and
    those can be the erased item's text."""
    return _scrub(db, tenant_id, project_id,
                  lambda job, i, item: isinstance(item, dict) and item.get(field) == value)


def erase_memory_item(db: Session, tenant_id: UUID, memory_id: UUID,
                      *, project_id: Any = EVERY_PROJECT) -> int:
    """Scrub the item a job imported as `memory_id` (`item_memory_id`: the
    memory's id is derived from the job and the item's place), as
    `erase_items` does, for the erasure of one memory (DELETE
    /v1/memories/{id}?erase=true, audit of 18 Oct 2026: its text stayed in
    the payload of the job that imported it). Does not commit."""
    return _scrub(db, tenant_id, project_id,
                  lambda job, i, item: item != ERASED_ITEM and item_memory_id(job.id, i) == memory_id)


def _scrub(db: Session, tenant_id: UUID, project_id: Any, hit_at) -> int:
    """The items `hit_at(job, index, item)` picks become ERASED_ITEM, in every
    job in reach, locked (`erase_items`). One job's payload in memory at a
    time: the ids first, then each job locked and read alone, in id order as
    before, and let go once looked at. Every job of the project, payload and
    all, came back in one list, some 24 MB a stored job at its largest, on
    every erasure, even of a memory id that answers 404 (audit of 19 Oct
    2026)."""
    ids = [jid for (jid,) in within_project(
               db.query(AgentBatchJob.id).filter(AgentBatchJob.tenant_id == tenant_id),
               AgentBatchJob.project_id, project_id).order_by(AgentBatchJob.id)]
    n = 0
    for jid in ids:
        job = (db.query(AgentBatchJob).filter(AgentBatchJob.id == jid)
                 .with_for_update().populate_existing().first())
        if job is None:  # finished and purged meanwhile
            continue
        items = list(job.payload or [])
        hit = [i for i, item in enumerate(items) if hit_at(job, i, item)]
        if hit:
            for i in hit:
                items[i] = dict(ERASED_ITEM)
            job.payload = items
            flag_modified(job, "payload")
            job.last_error = None
            n += len(hit)
            db.flush([job])
        db.expunge(job)
    return n


# A job not yet done: queued, running, or failed with an attempt still to
# come (the worker's own condition, `_ready`, without the retry time).
_UNFINISHED = ("(j.status IN ('pending', 'processing')"
               " OR (j.status = 'failed' AND j.attempts < j.max_attempts))")


def item_end_users(db: Session, tenant_id: UUID, project_id: Any = EVERY_PROJECT,
                   field: str = "user_id", *, unfinished: bool = False) -> set:
    """The end users the items of the jobs in reach name (`user_id`; the
    agent namespaces with `field="agent_id"`), read by the database from the
    stored payloads, never brought here: an erasure of a project, and the
    restore of an erasure, looked at each job's whole payload in turn (audit
    of 20 Oct 2026). services/end_user_places.py reads the batch items' place
    through it; the Cloud's agent cap the names of the jobs not yet done
    (`unfinished`, hosted/quotas._queued_agent_ids).

    A payload queued before the rule of every door refused a lone UTF-16
    surrogate (the audit before 1.0.0) can spell one (`"\\ud800"`): the json
    column keeps the escape, and Postgres refuses to decode it, in any field
    of the item, so this read raised and the project's erasure answered 500
    and the replay of the account's erasures stopped. Then the jobs in reach
    are read one at a time (`_item_end_users_one_by_one`), as an erasure
    scrubs them."""
    from sqlalchemy.exc import DataError

    wall = ("" if project_id is EVERY_PROJECT else
            " AND j.project_id IS NULL" if project_id is None else " AND j.project_id = :p")
    if unfinished:
        wall += " AND " + _UNFINISHED
    try:
        with db.begin_nested():
            rows = db.execute(text(
                "SELECT DISTINCT item->>CAST(:f AS text) FROM agent_batch_jobs j, "
                "json_array_elements(j.payload) item "
                "WHERE j.tenant_id = :t AND json_typeof(j.payload) = 'array'" + wall +
                " AND json_typeof(item) = 'object' AND item->>CAST(:f AS text) IS NOT NULL"),
                {"t": tenant_id, "f": field,
                 "p": project_id if project_id is not EVERY_PROJECT else None}).all()
    except DataError:
        return _item_end_users_one_by_one(db, tenant_id, project_id, field, wall)
    return {u for (u,) in rows}


def _item_end_users_one_by_one(db: Session, tenant_id: UUID, project_id: Any, field: str,
                               wall: str) -> set:
    """`item_end_users` over payloads Postgres cannot decode: each job's
    payload in turn, decoded here (Python's JSON reads a lone surrogate's
    escape), one in memory at a time as `_scrub` holds them. A value that is
    not text names nobody: no door ever took it as an id, and nothing else
    can hold it, so no erasure could ask for it; the job keeps failing on
    that item until it is purged or erased."""
    from .limits import not_storable

    ids = [jid for (jid,) in db.execute(text(
               "SELECT j.id FROM agent_batch_jobs j WHERE j.tenant_id = :t" + wall +
               " ORDER BY j.id"),
               {"t": tenant_id, "p": project_id if project_id is not EVERY_PROJECT else None})]
    out: set = set()
    unreadable = 0
    for jid in ids:
        raw = db.execute(text("SELECT payload::text FROM agent_batch_jobs WHERE id = :id"),
                         {"id": jid}).scalar()
        try:
            items = json.loads(raw) if raw is not None else []
        except ValueError:
            items = []
        for item in items if isinstance(items, list) else []:
            value = item.get(field) if isinstance(item, dict) else None
            if not isinstance(value, str):
                continue
            if not_storable(value):
                unreadable += 1
            else:
                out.add(value)
    if unreadable:
        logger.warning("[agent_batch] %d %s value(s) of stored items are not text; read as nobody",
                       unreadable, field)
    return out


def purge_finished(db: Session, keep: timedelta = FINISHED_JOB_RETENTION) -> int:
    """Delete the jobs that finished longer ago than `keep`, payload and all.

    Only finished ones, as the webhook outbox trims only finished deliveries
    (services/webhooks.purge_expired): completed, or failed with every attempt
    used. A job queued, running, or failed with a retry still to come is live
    state and is never removed on age. Its age is when it finished, or failing
    that when it was last written."""
    cutoff = datetime.now(timezone.utc) - keep
    finished = or_(
        AgentBatchJob.status == STATUS_COMPLETED,
        and_(AgentBatchJob.status == STATUS_FAILED,
             AgentBatchJob.attempts >= AgentBatchJob.max_attempts),
    )
    n = (
        db.query(AgentBatchJob)
        .filter(finished,
                func.coalesce(AgentBatchJob.completed_at, AgentBatchJob.updated_at) < cutoff)
        .delete(synchronize_session=False)
    )
    if n:
        db.commit()
        logger.info("[agent_batch] purged %d finished jobs older than %s", n, keep)
    return int(n or 0)


def _item_now(db: Session, job_id: UUID, idx: int) -> tuple[bool, Any]:
    """(whether the job is still there, its item `idx` as it is now).

    The item is read from the row when it is imported, not from the payload
    the worker loaded when it claimed the job: an erasure that lands while the
    job runs has scrubbed the row, and the rest of the job must see that. One
    small read per item, next to the model call each item makes.

    Read under the job's lock, held until the item's memory commits: an
    erasure scrubs the payloads first, under the same lock (`erase_items`), so
    it either comes before (the item reads erased) or waits for the memory and
    deletes it. Without the lock an erasure landing while the item's vector
    was computed found no memory yet, and the memory was written after it,
    under the real id (audit of 17 Oct 2026)."""
    row = (db.query(AgentBatchJob.payload[idx])
             .filter(AgentBatchJob.id == job_id)
             .with_for_update()
             .first())
    return (row is not None), (row[0] if row is not None else None)


def _key_still_writes(db: Session, job: AgentBatchJob) -> bool:
    """Whether the key that queued the job could still write its next item.

    The worker writes long after the request, with no key to ask, so it asks
    the row (audit of 18 Oct 2026): a key revoked by hand, or by an
    OAuth app's disconnection, theft detection, the abandoned-app purge or the
    deletion of its project (all of them set `revoked_at`), a key minted by
    hand past its `expires_at`, and a key moved out of the job's project all
    stop the job where it is. Read fresh, columns rather than the object: the
    worker's session keeps what it loaded (expire_on_commit=False), and a
    revocation committed during the job must be seen at the next item.

    An OAuth app's key is not asked its `expires_at`: that is the hour of its
    access token, renewed on the same row at each refresh (oauth.ACCESS_SECONDS,
    keys.reissue_secret), and a long import would stop while the grant is
    live. Its authority ends with `revoked_at`, which every end of it sets.

    An archived project takes no more of it either, whatever the key: the
    archive refuses a project with live keys, and an app's key between two
    refreshes does not count as live there (services/projects.archive_project).

    A job queued before the column existed (no key) is imported as before."""
    from ..core.times import aware
    from ..db.models import AgentApiKey, AgentProject
    from .keys import key_project

    now_in = db.query(AgentBatchJob.project_id).filter(AgentBatchJob.id == job.id).scalar()
    if now_in is not None and db.query(AgentProject.archived_at).filter(
            AgentProject.id == now_in).scalar() is not None:
        return False
    if job.api_key_id is None:
        return True

    row = (db.query(AgentApiKey.tenant_id, AgentApiKey.revoked_at, AgentApiKey.expires_at,
                    AgentApiKey.oauth_client_id, AgentApiKey.project_id)
             .filter(AgentApiKey.id == job.api_key_id, AgentApiKey.tenant_id == job.tenant_id)
             .first())
    if row is None or row.revoked_at is not None:
        return False
    if (row.oauth_client_id is None and row.expires_at is not None
            and aware(row.expires_at) <= datetime.now(timezone.utc)):
        return False
    # The project the key writes in now, as every route resolves it, against
    # the job's as the row has it now: deleting a project with its keys moved
    # moves its jobs too (services/projects.delete_project), and both then
    # write in the default.
    return key_project(db, row) == now_in


def _job_is_there(db: Session, job_id: UUID) -> bool:
    return db.query(AgentBatchJob.id).filter(AgentBatchJob.id == job_id).first() is not None


def item_memory_id(job_id: UUID, idx: int) -> UUID:
    """The id of the memory an item becomes, derived from the job and the
    item's place in it, so a second attempt can tell the write already
    happened. The memory is committed before the job's counter, and a process
    that died between the two left the item to be imported again when
    `reclaim_interrupted` put the job back: the customer's row, twice, and
    nothing saying so. The hosted product does the same (BUG-197)."""
    import uuid

    return uuid.uuid5(job_id, f"item-{idx}")


def _import_one(db: Session, tenant_id: UUID, item: dict, *,
                project_id=None, memory_id: Optional[UUID] = None,
                job_id: Optional[UUID] = None) -> tuple[Optional[str], Optional[str]]:
    """Import one memory. Returns (mem_public_id, None) on success or
    (None, error) on failure."""
    if item == ERASED_ITEM:
        return None, ERASED_BEFORE_IMPORT
    if memory_id is not None:
        from ..db.models import AgentMemory

        if db.get(AgentMemory, memory_id) is not None:
            # Written by an attempt that died before counting it. Its facts are
            # the memory drain's to finish, as for any memory left pending.
            return mem_public_id(memory_id), None
    # Checked again, by the rules of every write (services/memory_write.py):
    # POST /v1/batch refuses a bad row at the door, and this is for a job
    # queued before a rule existed, or by a caller that is not the API. A
    # refusal is this item's error, never the job's: a NUL byte in run_id
    # reached the driver here and failed the whole job, for every attempt,
    # with the rows after it never imported (audit of 17 Oct 2026). No key's
    # end users: the door placed each row. No quota and no namespace cap
    # either: the door counted the batch whole when it queued the job.
    from fastapi import HTTPException

    try:
        w = memory_write.checked(item.get("content"), user_id=item.get("user_id"),
                                 agent_id=item.get("agent_id"), run_id=item.get("run_id"),
                                 metadata=item.get("metadata"), timestamp=item.get("timestamp"),
                                 client=item.get("client"), kind=item.get("kind"))
    except HTTPException as e:
        return None, memory_write.reason(e)
    except RecursionError:
        # A row stored before a rule existed (metadata nested past what the
        # JSON reader takes) fails as that row, never as the whole job (audit
        # of 27 Oct 2026). Only the reader's own error: anything else is a
        # fault of ours and stays a retried job.
        return None, "the row is nested too deep to be read"
    # The write of every door, the event time on the row (the drain dates
    # the facts it finishes by it, never by the day of the backfill). The
    # facts, as on a single write: with WRITE_ASYNC, the default, the drain
    # extracts them, four end users at a time, five memories each, in each
    # one's order, while this loop only writes; it extracted every item
    # itself until 2026-10-06, one at a time, and the drain took the same
    # memories during the model call and extracted them again (the audit of
    # 6 October, row 1). Without it, inline and claimed, as POST
    # /v1/memories. One row in the trail per imported memory, as a single
    # write: the end user and the memory, so GET /v1/audit?user_id= finds
    # what was imported for them; the job's own `batch:create` row names
    # neither, and it was all there was (audit of 17 Oct 2026). Written by
    # the worker, in the project that queued the job; not again for an item
    # a dead attempt had already written (above), whose row that attempt
    # wrote. And one webhook per imported memory.
    m, _facts = memory_write.store(
        db, tenant_id, project_id, w, memory_id=memory_id, actor="worker",
        audit_meta={"batch": job_public_id(job_id)} if job_id is not None else None)
    return mem_public_id(m.id), None


INTERRUPTED = "interrupted: the worker stopped while this job ran; it resumes where it was"


def reclaim_interrupted(db: Session, now: Optional[datetime] = None) -> int:
    """Jobs left 'processing' by a worker that died (a restart, an OOM kill,
    an upgrade) go back to the queue, to resume where they stopped.

    They were never picked again: the drain reads pending and failed only, so
    such a job sat there with its counters frozen and the rest of its payload
    never imported (code review of 29 Sep 2026). The memories have the same reaper
    (`facts.drain.process_pending_memories`) and the same threshold. A job
    being worked commits after every item, and every commit moves
    `updated_at`, so one that has not moved for that long has nobody on it.
    It comes back as failed, the state of a job that is to be retried, with
    the attempt it used counted and the reason in `last_error`.
    """
    from ..config import get_settings

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max(1, int(get_settings().drain_stale_minutes)))
    n = int((db.query(AgentBatchJob)
               .filter(AgentBatchJob.status == STATUS_PROCESSING, AgentBatchJob.updated_at < cutoff)
               .update({AgentBatchJob.status: STATUS_FAILED,
                        AgentBatchJob.last_error: INTERRUPTED,
                        AgentBatchJob.next_retry_at: None},
                       synchronize_session=False)) or 0)
    if n:
        db.commit()
        logger.warning("[agent_batch] %d interrupted job(s) back in the queue", n)
    return n


def _ready(now):
    return (AgentBatchJob.status.in_([STATUS_PENDING, STATUS_FAILED]),
            AgentBatchJob.attempts < AgentBatchJob.max_attempts,
            or_(AgentBatchJob.next_retry_at.is_(None), AgentBatchJob.next_retry_at <= now))


# How many accounts' jobs one pass takes at most, however many are ready: the
# pass writes their items in turns (`process_batch_jobs`), so every account
# ready advances in it, and a claim holds a row lock per job (audit of 27 Oct
# 2026).
MOST_ACCOUNTS_PER_PASS = 50
# How many items of one job a turn writes before the next account's turn.
ITEMS_PER_TURN = 5


def _each_account_its_share(db: Session, limit: int, now):
    """The claim's size, each account's share of it, and the ready jobs
    ranked within their account (8 Oct 2026, the one codebase; by work, audit
    of 27 Oct 2026).

    Oldest first across the install, an account that queued a hundred imports
    held every other account's import behind all of them; on the hosted
    service each customer is an account. Each account ready gets `ceil(size /
    accounts)` of the claim, its own oldest first, and the claim is taken by
    rank (every account's oldest job, then every account's second) and not by
    age: with five accounts ready the share was one job each and `limit` by
    age left the newer accounts out of every pass. The claim is as large as
    the accounts ready (`MOST_ACCOUNTS_PER_PASS`), at least `limit`. What the
    claim takes is shared by work as well, in `process_batch_jobs`: fair
    queueing per account (Demers, Keshav and Shenker, SIGCOMM 1989), as the
    facts drain."""
    accounts = (db.query(func.count(func.distinct(AgentBatchJob.tenant_id)))
                .filter(*_ready(now)).scalar() or 0)
    size = max(1, int(limit), min(accounts, MOST_ACCOUNTS_PER_PASS))
    share = -(-size // max(1, accounts))
    rank = func.row_number().over(partition_by=AgentBatchJob.tenant_id,
                                  order_by=(AgentBatchJob.created_at.asc(), AgentBatchJob.id.asc()))
    ranked = (select(AgentBatchJob.id.label("id"), rank.label("rank"))
              .where(*_ready(now)).subquery())
    return size, share, ranked


def process_batch_jobs(db: Session, *, limit: int = 5) -> dict:
    """Drain pending/failed batch jobs. Idempotent + concurrency-safe. Each
    account ready gets its share of a claim (`_each_account_its_share`), and
    the claimed jobs are written in turns by account, not one after another."""
    now = datetime.now(timezone.utc)
    reclaim_interrupted(db, now)
    size, share, ranked = _each_account_its_share(db, limit, now)
    jobs = (
        db.query(AgentBatchJob)
        .join(ranked, ranked.c.id == AgentBatchJob.id)
        .filter(*_ready(now), ranked.c.rank <= share)
        .order_by(ranked.c.rank.asc(), AgentBatchJob.created_at.asc(), AgentBatchJob.id.asc())
        .limit(size)
        # The jobs only: the ranking is a subquery with a window, which
        # cannot be locked.
        .with_for_update(of=AgentBatchJob, skip_locked=True)
        .populate_existing()  # the counters the rows have now, not a session's older copy
        .all()
    )
    if not jobs:
        return {"processed": 0}

    # Claim every selected job in one transaction, before doing any work.
    #
    # The previous version claimed inside the loop, and the commit at the end of
    # the first iteration released every lock the SELECT had taken, so jobs 2..N
    # went back to being pending and unlocked while this worker was still
    # holding them in a list. A second drain would pick them up and import the
    # same payload twice. The docstring said "concurrency-safe".
    for job in jobs:
        job.status = STATUS_PROCESSING
        job.attempts = (job.attempts or 0) + 1
        db.add(job)
    db.commit()
    # Each job's ids as plain values, read while the claim has every row
    # fresh: a slice that fails rolls the session back, which expires every
    # job in it, and a job of the pass erased meanwhile (a project erasure)
    # then raised ObjectDeletedError at its `job.id`, outside any try, and
    # ended the pass for every account (30 Oct 2026). `_work_a_slice` finds
    # the erased row inside its try and ends that job alone.
    claimed = [(job, job.id, job.tenant_id) for job in jobs]

    # One round per pass: each account writes `ITEMS_PER_TURN` items, its
    # claimed jobs in the order they were queued (the first to its end, the
    # budget that is left to the next), then the jobs not finished go back to
    # the queue with the attempt given back and the pass ends, so an account
    # that became ready meanwhile is claimed by the next pass and waits one
    # round, not the sum of every claimed job (audit of 27 Oct 2026 and its
    # review: the share was by job count, not by work). A job being
    # worked commits after every item and the claim moves `updated_at`, so no
    # other replica takes it for dead (`reclaim_interrupted`).
    turns: dict = {}
    for job, job_id, tenant_id in claimed:
        turns.setdefault(tenant_id, []).append((job, job_id))
    processed = 0
    unfinished: list = []
    for queue in turns.values():
        budget = ITEMS_PER_TURN
        for job, job_id in queue:
            if budget <= 0:
                unfinished.append(job_id)
                continue
            outcome, handled = _work_a_slice(db, job, budget, job_id)
            budget -= max(1, handled)
            if outcome == "more":
                unfinished.append(job_id)
            processed += int(outcome == "completed")
    if unfinished:
        _give_back(db, unfinished)
    out = {"processed": processed}
    if unfinished or len(jobs) >= size:
        out["more"] = True  # the worker starts the next round at once
    return out


def _give_back(db: Session, job_ids: list) -> None:
    """Claimed jobs not finished in this round go back to the queue, the
    attempt they used given back: they resume where they are."""
    (db.query(AgentBatchJob)
     .filter(AgentBatchJob.id.in_(job_ids), AgentBatchJob.status == STATUS_PROCESSING)
     .update({AgentBatchJob.status: STATUS_PENDING,
              AgentBatchJob.attempts: func.greatest(AgentBatchJob.attempts - 1, 0),
              AgentBatchJob.next_retry_at: None,
              AgentBatchJob.updated_at: func.now()},
             synchronize_session=False))
    db.commit()


def _work_a_slice(db: Session, job: AgentBatchJob, items_max: int, job_id) -> tuple:
    """The next `items_max` items of a claimed job, from where it is:
    ("more" when items remain, "completed" when it ended, "ended" when it was
    erased or failed, to be tried again with a wait; and how many items it
    handled)."""
    # The id comes from the caller, kept apart since the claim: after an
    # earlier slice's rollback the row is read again on access, and a job
    # erased meanwhile has no row to read, so even `job.id` raised here,
    # before the try (30 Oct 2026).
    handled = 0
    try:
        imported_ids: list[str] = list(job.created_memory_ids or [])
        errors: list[dict] = list(job.errors or [])
        start = job.imported + job.failed  # where it resumes, here and after a retry
        gone = False
        stopped = False
        # How many items, counted by the database: the payload itself is
        # deferred (db/models.py), and every claim kept every claimed
        # job's whole payload for its whole drain to read this length
        # (audit of 20 Oct 2026: the scrub loaded every payload).
        items_in_job = db.query(func.json_array_length(AgentBatchJob.payload)) \
            .filter(AgentBatchJob.id == job_id).scalar() or 0
        for idx in range(start, min(items_in_job, start + items_max)):
            # Read now, not from the payload claimed above (`_item_now`).
            here, item = _item_now(db, job_id, idx)
            if not here:
                gone = True
                break
            # Under the job's lock, before the item is written: the key
            # that queued it can still write here, or the job ends now.
            if not _key_still_writes(db, job):
                # The payload as the row has it now, never the copy the
                # claim loaded: an erasure may have scrubbed items since.
                items = list(db.query(AgentBatchJob.payload)
                               .filter(AgentBatchJob.id == job_id).scalar() or [])
                for rest in range(idx, len(items)):
                    errors.append({"index": rest, "error": KEY_ENDED})
                    items[rest] = dict(DROPPED_ITEM)
                job.failed += len(items) - idx
                job.payload = items
                job.errors = errors
                flag_modified(job, "payload")
                flag_modified(job, "errors")
                logger.info("[agent_batch] job %s stopped: its key can no longer write there", job_id)
                stopped = True
                break
            handled += 1
            mem_id, err = _import_one(db, job.tenant_id, item or {},
                                      project_id=job.project_id,
                                      memory_id=item_memory_id(job_id, idx),
                                      job_id=job_id)
            if err:
                job.failed += 1
                errors.append({"index": idx, "error": err})
            else:
                job.imported += 1
                imported_ids.append(mem_id)
            # The same lists, grown in place, are what the session holds
            # as committed (expire_on_commit=False), and a plain JSON
            # column is compared by value: after the first item nothing
            # looked changed, so GET /v1/batch read a count of failures
            # and fewer errors, often none (audit of 17 Oct 2026).
            job.created_memory_ids = imported_ids
            job.errors = errors
            flag_modified(job, "created_memory_ids")
            flag_modified(job, "errors")
            db.add(job)
            db.commit()
        if gone:
            # Deleted while it ran: its project was erased with it
            # (services/projects.delete_project). Nothing to finish and
            # nothing to record a result on.
            logger.info("[agent_batch] job %s was erased while it ran; stopped", job_id)
            db.expunge(job)
            return "ended", handled
        if not stopped and job.imported + job.failed < items_in_job:
            return "more", handled
        job.status = STATUS_COMPLETED
        job.completed_at = datetime.now(timezone.utc)
        job.next_retry_at = None
        db.add(job)
        db.commit()
        return "completed", handled
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        if not _job_is_there(db, job_id):
            # The same, found by the write that failed on the missing row
            # rather than by the next read: there is no row to mark failed.
            logger.info("[agent_batch] job %s was erased while it ran; stopped", job_id)
            db.expunge(job)
            return "ended", handled
        backoff = min(300, 2 ** min(job.attempts, 8))
        job.status = STATUS_FAILED
        # The exception's classes and codes, never its message: a
        # database error quotes the statement's parameters, an item's
        # text among them (audit of 17 Oct 2026; log_safe.error).
        job.last_error = log_safe.error(exc)[:2000]
        job.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)
        db.add(job)
        db.commit()
        logger.warning("[agent_batch] job %s failed: %s", job_id, log_safe.error(exc))
        return "ended", handled
