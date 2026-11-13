"""/v1/batch API (Sprint 18 D) — bulk import for migrations.

POST /v1/batch    — up to 500 memory objects, processed asynchronously, returns
                    a job id to poll. On an install of one's own nothing is
                    metered. On the Cloud (KORELY_MODE=hosted, the same code)
                    the batch is counted whole against the month's writes, or
                    refused 429 `quota_exceeded`, when its job is queued
                    (services/memory_write.admit); its agent names meet the
                    plan's cap there too.
GET /v1/batch/:id — poll job status (imported / failed / errors).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from ._bodies import Body, NamespaceId, Part
from .errors import V1_KEY_ROUTE_ERRORS, refused, v1_errors
from sqlalchemy.orm import Session

from ..core.auth import (OutOfScopeRefusal, enforce_rate_limit, key_end_users, key_project,
                         require_scopes)
from ..db import get_db, AgentApiKey
from ..services import batch as agent_batch
from ..services import limits, memory_write
from ..services.clients import HEADER as CLIENT_HEADER, client_name
from ..services.ids import NAMESPACE_ID_RULE, job_public_id, parse_job_id
from ..services.usage import log_v1_request

router = APIRouter(prefix="/v1", tags=["v1"], dependencies=[Depends(enforce_rate_limit)],
                   responses=V1_KEY_ROUTE_ERRORS)

_read_key = require_scopes("memories:read")
_write_key = require_scopes("memories:write")


class BatchMemory(Part):
    content: str = Field(min_length=1, max_length=limits.MAX_CONTENT_CHARS)
    user_id: NamespaceId | None = Field(
        default=None, max_length=limits.MAX_ID_CHARS,
        description="The end user this memory belongs to. " + memory_write.IDENTITY_RULE
                    + NAMESPACE_ID_RULE)
    agent_id: NamespaceId | None = Field(
        default=None, max_length=limits.MAX_ID_CHARS,
        description="Agent namespace: the agent writing. " + memory_write.IDENTITY_RULE
                    + NAMESPACE_ID_RULE)
    run_id: str | None = Field(default=None, max_length=limits.MAX_ID_CHARS)
    metadata: dict | None = None
    kind: Literal["note", "session"] = Field(
        default="note",
        description="What the text is: `note` (the default), what somebody said or decided; "
                    "`session`, the log of a work session (what a coding assistant's hooks "
                    "save). A read may leave the sessions out, their facts with them "
                    "(`exclude_sessions`).")
    # 2026-09-28. POST /v1/memories took it and this did not, and the body
    # refuses unknown fields: one item carrying it failed the whole batch with
    # a 422, so a history could only be imported one write at a time, or in
    # bulk with every fact dated the day of the import.
    timestamp: str | None = Field(
        default=None,
        description="ISO date/datetime the memory's events occurred (defaults to the time "
                    "the item is imported). Facts extracted inherit it as valid_from, exactly "
                    "as on POST /v1/memories, and an unreadable one refuses the batch (422) "
                    "rather than being replaced by the import time, as does one outside the "
                    f"moments that can be stored ({limits.MOMENTS_STORED}).",
    )



class BatchBody(Body):
    memories: list[BatchMemory] = Field(min_length=1, max_length=agent_batch.MAX_BATCH_ITEMS)


_STATUS = Field(description=(
    "pending (queued, no worker on it yet), processing (being imported now), completed "
    "(every item tried: `imported` and `failed` are final) or failed (stopped on an error "
    "of the worker, tried again from where it stopped until its attempts are used, so it "
    "may still become completed). The POST answers the stored value, as the GET does."))
JobStatus = Literal[agent_batch.STATUSES]


class BatchAccepted(BaseModel):
    id: str
    status: JobStatus = _STATUS
    received: int


class BatchStatus(BaseModel):
    id: str
    status: JobStatus = _STATUS
    received: int
    imported: int
    failed: int
    errors: list[dict]


@router.post("/batch", response_model=BatchAccepted, status_code=status.HTTP_202_ACCEPTED)
def create_batch(
    body: BatchBody,
    key: AgentApiKey = Depends(_write_key),
    db: Session = Depends(get_db),
    x_korely_client: Optional[str] = Header(
        default=None, alias=CLIENT_HEADER,
        description="The tool importing: kept on every imported memory as `client`."),
) -> BatchAccepted:
    """Import up to 500 memories in one call.

    Every row is checked as a single write is (its content, metadata,
    `timestamp`, `user_id`, `agent_id` and `run_id`) before anything is queued: a bad row is refused
    with its index in the message, and nothing is imported. Every row needs a `user_id` or an
    `agent_id` (422 `identity_required`, since 1.0.19). Then the job is queued and the
    call answers 202 with its `job_` id. The worker writes the rows, which get
    their facts as any write does; `GET /v1/batch/{job_id}` says how far it is.
    If the key is revoked, expires or is moved to another project before the
    worker is done, the rows not yet imported are not: each is a failed row
    with its reason in `errors`, and the job completes.
    """
    # Every row is checked as a single write is checked, by the same function
    # (services/memory_write.py), before the job exists, with the row's index
    # in the sentence. Here it matters more: a value that gets past this point
    # fails inside the worker, minutes later, on one row of a five-hundred-row
    # load. This door had its own list of the rules, and it had already lost
    # one: a NUL byte in run_id was queued, and the job failed on that item
    # for every attempt, importing nothing after it (audit of 17 Oct 2026).
    # Each row's end user is placed inside a confined key's list (its single
    # id when the row names none): the worker writes them later, with no key
    # to ask (services/user_scope.py). The event time is queued normalized,
    # so the worker is never the place a bad one is discovered; the client is
    # queued with the rows, since there is no request to read the header from
    # when they are written.
    client = client_name(x_korely_client)
    end_users = key_end_users(key)
    try:
        rows = [memory_write.checked(m.content, user_id=m.user_id, agent_id=m.agent_id,
                                     run_id=m.run_id, metadata=m.metadata, timestamp=m.timestamp,
                                     client=client, end_users=end_users,
                                     where=f"memories[{i}].", kind=m.kind, identity=True)
                for i, m in enumerate(body.memories)]
    except OutOfScopeRefusal as e:
        # A row for an end user outside the key's list: in the trail.
        raise refused(db, key, "batch:create", e, end_user_id=e.user_id) from None
    # The hosted service's ceilings, the batch counted whole or refused, and
    # its new agent names counted with it, before its job is queued (given
    # back if the job is not); the worker does not count it again
    # (hosted/quotas.py, audit of 18 Oct 2026).
    project = key_project(db, key)
    memory_write.admit(db, key.tenant_id, [w.content for w in rows], [w.agent_id for w in rows],
                       endpoint="batch:create", project_id=project, api_key_id=key.id)
    items = [w.as_item() for w in rows]
    # The key goes on the job with its project: the worker imports each row
    # only while this key could still write there (services/batch.py).
    job = agent_batch.enqueue_batch(db, key.tenant_id, items,
                                    project_id=project, api_key_id=key.id)
    # The job queued, by the id GET /v1/batch/{job_id} names it with (audit
    # of 19 Oct 2026: the row named no target).
    log_v1_request(db, key.tenant_id, "batch:create", api_key_id=key.id,
                   target_id=job_public_id(job.id), meta={"client": client} if client else None)
    # The status the job is stored with, the one GET answers next.
    return BatchAccepted(id=job_public_id(job.id), status=job.status, received=job.received)


@router.get("/batch/{job_id}", response_model=BatchStatus, responses=v1_errors(404))
def get_batch(
    job_id: str,
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
) -> BatchStatus:
    """Where an import stands: its `status`, how many rows were `received`,
    `imported` and `failed`, and the `errors` of the rows that failed. A job
    of another project is a 404, and so, for a key confined to some end
    users, is a job another key queued."""
    u = parse_job_id(job_id)
    # The job asked for, in the trail, and a refusal as `error` (api/
    # errors.refused): the row named neither (audit of 18 Oct 2026).
    if u is None:
        raise refused(db, key, "batch:get", HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Batch job not found"))
    # Behind the project wall: a job another project queued is a 404 here;
    # for a key confined to some end users, a job another key queued too.
    out = agent_batch.job_status(db, key.tenant_id, u, project_id=key_project(db, key),
                                 queued_by=key.id if key_end_users(key) is not None else None)
    if out is None:
        raise refused(db, key, "batch:get", HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Batch job not found"),
            target_id=job_public_id(u))
    log_v1_request(db, key.tenant_id, "batch:get", api_key_id=key.id, target_id=job_public_id(u))
    return BatchStatus(**out)
