"""GET /v1/audit — read the trail.

A buyer does not want to be told that activity is recorded, they want to run a
query and export the answer. Two questions cover almost everything asked:
what happened on this account recently, and everything touching one end user,
which is the shape an access or erasure request takes.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import enforce_rate_limit, key_end_users, key_project, reading, require_scopes
from ..db import AgentApiKey, AuditEvent, get_db
from ..services import audit as audit_service
from ..services import limits, user_scope
from ..services.sentinels import EndUsers, within_project
from .errors import V1_KEY_ROUTE_ERRORS

router = APIRouter(prefix="/v1", tags=["v1"], dependencies=[Depends(enforce_rate_limit)],
                   responses=V1_KEY_ROUTE_ERRORS)

_read_key = require_scopes("memories:read")

# `since` and `until`, inclusive, read by the parser `as_of` is read with
# (temporal.parse_moment, through api.facts.parse_as_of): they were typed
# datetimes, and a value with no offset stayed naive, which Postgres compares
# with a timestamptz column in the session's time zone. With a database not
# in UTC an export for an access request moved by hours, while `as_of` read
# the same string as UTC (audit of 17 Oct 2026).
MOMENT = "ISO date (2026-06-01, midnight) or datetime; no offset is UTC. Inclusive."


class AuditRead(BaseModel):
    memories: List[str] = Field(default_factory=list,
                                description="Public ids of the memories the call returned, at most 100.")
    facts: List[str] = Field(default_factory=list,
                             description="Public ids of the facts the call returned, at most 100.")


class AuditRow(BaseModel):
    ts: datetime
    actor: str = Field(description="Who acted: " + "; ".join(
        f"`{name}`, {what}" for name, what in audit_service.ACTORS.items()) + ".")
    action: str = Field(description="read | write | fact_write | fact_invalidate | erase | "
                                    "key_create | key_revoke | key_change | tenant_create | "
                                    "setting_change | login | logout")
    result: str = Field(description="ok | error. `error`: the call was refused or could not "
                                    "be served, and `meta.code` says why with the token the "
                                    "call answered (`forbidden`, `not_found`, "
                                    "`quota_exceeded`, `search_unavailable`, ...), never a "
                                    "sentence.")
    user_id: Optional[str] = Field(
        default=None, description="The end user the event touched. After their erasure, a keyed "
                                  "pseudonym (`erased:...`): ask with their id and it is found.")
    target_id: Optional[str] = Field(default=None,
                                     description="The memory or fact acted on, by public id.")
    meta: Optional[dict] = Field(default=None, description="Counts and labels, never content. "
                                 "A read says how many memories and facts it returned "
                                 "(memories_read, facts_read).")
    ip: Optional[str] = None
    read: Optional[AuditRead] = Field(
        default=None, description="For reads: what the call returned, by id. With as_of on "
                                  "/v1/facts it rebuilds what the agent knew at that moment.")


class AuditPage(BaseModel):
    events: List[AuditRow]
    total: int


@router.get("/audit", response_model=AuditPage)
def list_audit(
    # min_length 1 (2026-10-06): an empty string read as "no filter" and
    # `user_id=` listed every end user's events (found by the SDK's check).
    user_id: Optional[str] = Query(default=None, min_length=1, max_length=limits.MAX_ID_CHARS,
                                   description="Only events touching this end user."),
    action: Optional[str] = Query(default=None, min_length=1, max_length=32),
    since: Optional[str] = Query(default=None, max_length=64, description=MOMENT),
    until: Optional[str] = Query(default=None, max_length=64, description=MOMENT),
    limit: int = Query(default=100, ge=1, le=limits.MAX_AUDIT_PAGE),
    offset: int = Query(default=0, ge=0, le=limits.MAX_OFFSET),
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
) -> AuditPage:
    """The events this key's project produced, newest first.

    Scoped to the caller's own account AND project by the key, like
    everything else here; a key confined to some end users reads their
    events only. The dashboard's acts and reads in the project are among
    them (`actor` `dashboard`), without the name the operator signs in
    with. The operator's screen (/admin/audit) reads one project or the
    whole account, and that name.
    \f
    An audit trail readable across tenants would be the most sensitive
    endpoint in the service, and across projects it is the same endpoint one
    wall further in. Until 2026-09-28 it read the whole account, so a key
    confined to one project listed the end user ids, memory ids and actions
    of every other one, and could ask for them by `user_id`. That history is
    after `\\f`, out of /openapi.json (audit of 17 Oct 2026): the published
    text is read by every customer and says what the route does now.
    """
    from .facts import parse_as_of

    # What it narrows on by every door's rule (limits.id_problem): a NUL byte
    # in `user_id` or `action` reached the driver and was a 500 (audit of 18
    # Oct 2026). An export for an access request that fails on a
    # client's stray byte looks like a trail that cannot be read.
    problem = limits.id_problem("user_id", user_id) or limits.id_problem("action", action)
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
    since_at, until_at = parse_as_of(since, "since"), parse_as_of(until, "until")
    proj = key_project(db, key)
    q = within_project(db.query(AuditEvent).filter(AuditEvent.tenant_id == key.tenant_id),
                       AuditEvent.project_id, proj)
    if user_id:
        reading(key, user_id, db=db, endpoint="audit:read")  # a confined key reads its own end users' events (403)
        # The end user's rows, and their pseudonym's once they were erased
        # (services/pseudonyms.py): the proof of an erasure is found by the id.
        from ..services.pseudonyms import as_asked

        q = q.filter(AuditEvent.end_user_id.in_(as_asked(db, key.tenant_id, user_id)))
    elif key_end_users(key) is not None:
        # A confined key's trail: its own end users' events, never the rows
        # with no end user (keys, settings) nor another end user's.
        q = user_scope.within_end_users(q, AuditEvent.end_user_id, EndUsers(key_end_users(key)))
    if action:
        q = q.filter(AuditEvent.action == action)
    if since_at:
        q = q.filter(AuditEvent.ts >= since_at)
    if until_at:
        q = q.filter(AuditEvent.ts <= until_at)

    total = q.count()
    # Then by id: the events of one transaction share its `now()` to the
    # microsecond, and with ties in the order an offset walks them in whatever
    # order each query happens to read, repeating some and skipping others
    # (2026-10-06). The hosted /v1/audit orders the same.
    rows = (q.order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
             .offset(offset).limit(limit).all())
    # Reading the trail writes no row of its own, on purpose (audit of 17
    # Oct 2026): every page asked would add a row at the top of the list it
    # pages through, so offsets walk over themselves (tests/test_audit_api.py,
    # TestPaging), and the reads it reports are ids already in the trail.
    return AuditPage(
        events=[
            AuditRow(
                ts=r.ts, actor=r.actor, action=r.action, result=r.result,
                user_id=r.end_user_id, target_id=r.target_id,
                meta=audit_service.meta_a_key_reads(r.meta), ip=r.ip,
                read=AuditRead(**r.read_ids) if r.read_ids else None,
            )
            for r in rows
        ],
        total=total,
    )
