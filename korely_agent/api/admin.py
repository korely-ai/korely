"""The administrative surface: keys, webhooks, projects.

These have existed as services since the beginning and were reachable only from
the command line, because the product had no screen. A screen is being built,
so they need routes.

**Not under `/v1`.** `/v1` is the memory API, held by whatever an agent runs
with. If a key could mint keys, one leaked key would be permanent access: the
first thing it does is issue itself a second one, and revoking the first
changes nothing. So these routes want a credential ordinary keys are not,
and the API key issued by `create-tenant` cannot reach any of them.

Two credentials do reach them: an operator who logged in, and a key issued with
the `admin` scope for a script. Both carry a tenant, which is the only thing
these routes read, so the login arrived without any of them changing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from ._bodies import Body
from .errors import (ADMIN_MODEL_503, ADMIN_ROUTE_ERRORS, CodedHTTPException, admin_errors,
                     outbound_errors, service_paused)
from .audit import MOMENT
from .memories import UserErasureResponse, validate_filters, public_memory

from sqlalchemy.orm import Session

from ..config import get_settings
from ..core import extensions
from ..core import llm as _llm
from ..core.auth import (a_turn_for_its_question, admin_principal, count_the_read,
                         enforce_admin_rate_limit, give_back_the_read, is_signed_in_person,
                         ready_to_wait, still_admin)
from ..core.threads import on_a_request_thread
from ..db import AgentApiKey, Operator, get_db
from ..services import audit as audit_service
from ..services import limits, user_scope
from ..services import keys as key_service
from ..services import projects as project_service
from ..services.facts import service as facts_service
from ..services.facts.reads import FAMILY_FILTER
from ..services import operators as operator_service
from ..services import model_choice
from ..services.regions import LABELS
from ..services import provider_key
from ..services import usage_report
from ..services import webhooks as webhook_service
from ..services.sentinels import EVERY_PROJECT
from ..services.usage import log_admin_action, log_v1_request
from ..services.facts.temporal import true_at

# Every error here is the envelope of api/errors.py, declared as such: /admin
# described its 422s as FastAPI's validation list while half of them answer a
# sentence (2026-09-28). Routes add the statuses they raise themselves.
router = APIRouter(prefix="/admin", tags=["admin"],
                   dependencies=[Depends(enforce_admin_rate_limit)],
                   responses=ADMIN_ROUTE_ERRORS)

# Logging in and out cannot sit behind a dependency that requires being logged
# in. The first version of this had them on the router above, which meant a
# session was refused before any route saw it and the login route wanted a key
# in order to hand out a session.
#
# Nor behind the admin rate limit, which counts per tenant: these routes have
# no tenant to count against until somebody is signed in (audit of 17 Oct
# 2026, said here because the router's name does not). The login has its own
# ceilings instead (services/operators.py: per username and address, per
# address, and the hashes computed at once), logging out ends a session and
# opens nothing, and /admin/scopes is a list written in the code.
public = APIRouter(prefix="/admin", tags=["admin"], responses=admin_errors(500))

# Either an operator who logged in, or a key issued with the admin scope.
# Both carry a tenant_id, which is the only thing the routes below read.
_admin = admin_principal


def _count_a_keys_read(db: Session, key) -> None:
    """A read of memories or facts made with an API key holding the admin
    scope counts against the month as a /v1 read does (`count_the_read`, on
    the Cloud 429 `quota_exceeded` past it): the same data through another
    door, which counted nothing (audit of 18 Oct 2026). The
    person signed in to the dashboard, looking at their own account, does
    not count: those screens are not an agent's reads."""
    if not is_signed_in_person(key):
        count_the_read(db, key)


def _not_on_the_service():
    """A route of an install of one's own that the hosted service does not
    have: there it answers 404, as a route that is not there. The password
    login (the service signs people in with their provider, and an
    account's operator has no password) and the model spend (on the service
    that number is the vendor's cost, not the customer's bill)."""
    if get_settings().hosted:
        raise CodedHTTPException(status.HTTP_404_NOT_FOUND, "Not Found", code="not_found")


# ── shapes ──────────────────────────────────────────────────────────────────

class KeyOut(BaseModel):
    id: str
    name: Optional[str] = None
    prefix: str
    last_four: Optional[str] = None
    # `response_model` filters the reply to what is declared here. Adding a
    # field to `_key_out` and not to this class changes nothing, silently,
    # which is how the key column stayed blank after it was "fixed" once.
    masked: str = ""
    # The hosted plan name, a label here: no limit reads it (models.AgentApiKey).
    tier: str = "hobby"
    revoked: bool = False
    project_name: Optional[str] = None
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    # live | revoked | refused | expired (keys.state_of). Computed in one
    # place so a listing and a refusal never disagree about why a key stopped
    # working; `refused` is a `kor_live_` key on an install of one's own.
    state: str = "live"
    scopes: List[str] = []
    project_id: Optional[str] = None
    # The end users it is confined to inside its project, or null for the
    # whole project (services/user_scope.py).
    users: Optional[List[str]] = None


class KeyCreated(KeyOut):
    # The only time the raw key exists outside the caller's hands. It is stored
    # as a hash and cannot be shown again, which is the sentence the UI has to
    # put next to it.
    key: str


class KeyNew(Body):
    name: str = Field(min_length=1, max_length=128)
    project_id: Optional[str] = None
    # Left out (or null) is the default set; an empty list is a key that can
    # do nothing, which the screen let Enter mint past its "Tick at least
    # one" (audit of 17 Oct 2026).
    scopes: Optional[List[str]] = Field(
        default=None, min_length=1, max_length=limits.MAX_LISTED_NAMES,
        description="What the key may do. Omitted or null: the default scopes.")
    # Days. 0 or null means it never expires, which is a choice somebody makes
    # rather than the absence of one. Omitted entirely means the default.
    # null = never expires (what the screen sends for that choice).
    # 0 = expired already, which is how you retire a key without revoking it.
    # Two spellings of "never" was the bug: a script computing
    # `max(0, deadline - today)` on a past date used to mint an immortal key.
    expires_in_days: Optional[int] = Field(
        default=None, ge=0, le=key_service.MAX_LIFETIME_DAYS,
        description="Days until it expires. null never expires; 0 expires it now.")
    users: Optional[List[str]] = Field(
        default=None, max_length=user_scope.MAX_ENTRIES,
        description="Confine the key to these end users of its project: exact user_ids and "
                    "prefixes ending in '*' (e.g. 'repo:*'). Omitted or null: the whole "
                    "project. A confined key cannot hold 'admin'.")


class WebhookOut(BaseModel):
    id: str
    url: str
    events: List[str] = []
    active: bool = True
    created_at: Optional[datetime] = None
    # How it has been behaving. An endpoint list that shows only the URL
    # cannot answer the question somebody opens that page with, which is
    # "which of these has stopped working". The failed attempts since the
    # last accepted delivery: the endpoint's own count, which a success and
    # Enable set back to 0.
    failure_count: int = 0
    last_delivery_at: Optional[datetime] = None
    # The project whose events it receives; None: every project.
    project_id: Optional[str] = None
    # What went wrong the last time, verbatim. `active: false` says an endpoint
    # was switched off after failing over and over; without this line the page
    # cannot say whether that was a 500, a timeout or a name that stopped
    # resolving, and the owner has to guess at their own server.
    last_error: Optional[str] = None
    # True for an endpoint made before 0.1.11, whose secret is the whole
    # string as the HMAC key, which the official Standard Webhooks libraries
    # cannot verify. Since 0.2.0 it is not sent: each delivery is refused at
    # every attempt, counted in `failure_count` with the reason in
    # `last_error`, and switched off after the schedule like any endpoint
    # that accepts nothing. `manage rotate-webhook-secret` gives it a spec
    # secret and sends what is still queued; recreating it also works.
    legacy_signature: bool = False


class WebhookCreated(WebhookOut):
    # Same rule as a key: shown once, unrecoverable, and without it nobody can
    # verify a delivery signature.
    secret: str


class WebhookNew(Body):
    url: str = Field(min_length=1, max_length=2000)
    events: Optional[List[str]] = Field(
        default=None, max_length=limits.MAX_LISTED_NAMES,
        description="memory.created, fact.invalidated, key.expiring, key.expired, "
                    "or [\"*\"] for all. Left out: all. An empty list or an "
                    "unknown name is a 422 that names it.")
    project_id: Optional[str] = Field(
        default=None,
        description="The project whose events it receives, and the account's. "
                    "Left out: every project.")


Region = Literal["eu", "global"]


class ProjectOut(BaseModel):
    id: str
    name: str
    slug: str
    description: Optional[str] = None
    is_default: bool = False
    # The project's own region, None when it follows the install's default
    # writer (on an install of one's own; on the Cloud a project is stored
    # with its region, Global when none was chosen: projects._region_of);
    # `processing_region` is where its memories are read now, None when
    # that region has no writer (they wait). services/regions.py.
    region: Optional[Region] = None
    processing_region: Optional[Region] = None
    archived_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class ProjectNew(Body):
    name: str = Field(min_length=1, max_length=128)
    description: Optional[str] = None
    region: Optional[Region] = None


class ProjectPatch(Body):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = None
    # Sent as null: the install's default writer again on an install of
    # one's own, Global on the Cloud (projects._region_of). Left out:
    # unchanged.
    region: Optional[Region] = None


class ProjectDeleted(BaseModel):
    deleted: bool
    keys_moved: int
    # The keys of apps connected over OAuth to this project: revoked, never
    # moved, since the person consented to this project only (17 Oct 2026).
    apps_disconnected: int = 0
    # Erased: the keys confined to end users, whose every end user the
    # erasure erased or only named there, revoked rather than moved working
    # (audit of 27 Oct 2026). A prefix rule ("acme:*") is not a person, and a
    # key with one stays.
    keys_revoked: int = 0
    # What else moved with them. It used to be keys only, and the rest of a
    # deleted project's rows were left with no project at all, which after the
    # isolation wall meant no key could reach them and no key could erase them.
    # Saying the numbers out loud is also the cheapest way for an operator to
    # notice that a project they thought was empty was not.
    memories_moved: int = 0
    facts_moved: int = 0
    # Open facts the default project already held, the same triple from the
    # same end user and agent: merged into that one (sources united) instead
    # of moved, which is what used to break the unique index and answer 500.
    facts_merged: int = 0
    # The entity aliases ("Mary" is "Maria"), which the project row's cascade
    # used to take with it while the facts they name moved (2026-09-28). Not
    # moved: the default project had already decided that name differently,
    # or taking it there would close a cycle; the default project's stands.
    aliases_moved: int = 0
    aliases_not_moved: int = 0
    # Filled in only when `?erase=true` was asked for. The counters of the
    # outcome that did not happen stay at zero, so the reply says which of the
    # two things happened without anyone having to infer it.
    #
    # What the erasure physically deleted (2026-09-28: it used to flag the rows
    # and leave them in the table): every memory and every fact of the
    # project, and its /v1/batch jobs, whose payloads are copies of memories.
    memories_deleted: int = 0
    facts_deleted: int = 0
    batches_deleted: int = 0
    # "permanent" when the data was erased, as the end-user erasure says it;
    # None when it was moved.
    erasure: Optional[str] = None
    # The erasure audit row `?erase=true` files (scope 'project'), as every
    # other erasure returns its own; None when the data was moved.
    audit_id: Optional[str] = None


class Deleted(BaseModel):
    deleted: bool
    id: str


# ── helpers ─────────────────────────────────────────────────────────────────

# The account's wall, in one place (audit of 17 Oct 2026). Every route here
# reads rows by an id somebody typed, and every one of those reads has to be
# filtered by the caller's tenant: the lookup and its 404 were written by hand
# in each route, so a new route that forgot `key.tenant_id` would have read
# another account's row and nothing would have said so. A row is found through
# `_owned` (or `account_project`, which is the projects service's own lookup), and
# "not found", "not yours" and "not an id" are one answer, since telling them
# apart tells whoever is probing which ids exist.

def _missing(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"no {what} with that id")


def _uuid(value: str, what: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise _missing(what)


def _owned(db: Session, key, model, raw_id: str, what: str, parse=None, *also):
    """The row of `model` with this id on the caller's account, or 404.
    `parse` reads a published id (`mem_...`, `fct_...`) into its UUID, None
    for anything that is not one; `also` narrows further (a memory not yet
    forgotten)."""
    ident = parse(raw_id) if parse is not None else _uuid(raw_id, what)
    row = (db.query(model)
             .filter(model.id == ident, model.tenant_id == key.tenant_id, *also)
             .first()) if ident is not None else None
    if row is None:
        raise _missing(what)
    return row


def account_project(db: Session, key, project_id: str, *, to_hold_keys: bool = False):
    """The account's project with this id, or 404. Public: the OAuth consent
    (api/oauth.py) puts a key in a project too, and reads it the same way.

    `to_hold_keys`: a project a key is being put in, which an archived one is
    not (409 `project_archived`). Archiving refuses a project with live keys
    because an agent would go on filling a project nobody sees on the screen,
    and until the audit of 17 Oct 2026 POST /admin/keys and its move made
    exactly that key the moment after. Read under the project's lock, held
    until the caller commits the key (`projects.get_project(lock=True)`),
    the one archiving takes: the two never both go through."""
    row = project_service.get_project(db, key.tenant_id, _uuid(project_id, "project"),
                                      lock=to_hold_keys)
    if row is None:
        raise _missing("project")
    if to_hold_keys and row.archived_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            {"code": project_service.ProjectArchived.code,
                             "message": "this project is archived: unarchive it first, "
                                        "or choose another"})
    return row


def _within_project(db: Session, key, project_id: Optional[str]):
    """A read of the screen narrowed to one project of the account, as an
    agent's key reads it, or the whole account (EVERY_PROJECT) without one;
    404 for a project that is not the account's."""
    from ..services.sentinels import EVERY_PROJECT

    if not project_id:
        return EVERY_PROJECT
    return account_project(db, key, project_id).id


def _key_out(row, project_names: Optional[dict] = None) -> dict:
    return {
        "id": str(row.id), "name": row.name, "prefix": row.prefix,
        "last_four": row.last_four,
        # What the screen prints in the key column. It read `masked` and got
        # nothing, so the column was blank while every test was green: the path
        # existed, the status was 200, the field was not there.
        "masked": f"{row.prefix}\u2022\u2022\u2022\u2022{row.last_four or '????'}",
        "tier": row.tier,
        "revoked": row.revoked_at is not None,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at, "revoked_at": row.revoked_at,
        "expires_at": row.expires_at,
        "state": key_service.state_of(row),
        "scopes": list(row.scopes or []),
        "project_id": str(row.project_id) if row.project_id else None,
        "project_name": (project_names or {}).get(row.project_id),
        "users": list(row.end_users) if row.end_users else None,
    }


def _hook_out(row, stats: Optional[dict] = None) -> dict:
    got = (stats or {}).get(row.id, {})
    return {"id": str(row.id), "url": row.url, "events": list(row.events or []),
            "active": bool(row.active), "created_at": row.created_at,
            "project_id": str(row.project_id) if row.project_id else None,
            # The endpoint's column, which the delivery round counts
            # (services/webhooks.py). Until the audit of 17 Oct 2026 it was
            # the number of its deliveries not yet delivered, the ones still
            # waiting their first attempt included: a new, healthy endpoint
            # read "1 recent failures" until the worker's next round, and
            # Enable, which forgets the failures, left the number as it was.
            "failure_count": int(row.failure_count or 0),
            "last_delivery_at": got.get("last_at"),
            "last_error": got.get("last_error"),
            "legacy_signature": webhook_service.is_legacy_secret(row.secret)}


def _project_out(row, key_counts: Optional[dict] = None, db: Optional[Session] = None) -> dict:
    from ..services import regions

    return {"id": str(row.id), "name": row.name, "slug": row.slug,
            "description": row.description, "is_default": bool(row.is_default),
            "region": row.region,
            "processing_region": (regions.processing(db, row.tenant_id, row.id)
                                  if db is not None else None),
            "archived_at": row.archived_at,
            # `archived` as a boolean, next to the date. The screen filters on
            # it, and reading `undefined` there meant an archived project still
            # counted as a live target.
            "archived": row.archived_at is not None,
            "key_count": (key_counts or {}).get(row.id, 0),
            "created_at": row.created_at}


# ── keys ────────────────────────────────────────────────────────────────────

@router.get("/keys", response_model=List[KeyOut])
def list_keys(limit: int = Query(default=limits.MAX_PAGE, ge=1, le=limits.MAX_PAGE),
              offset: int = Query(default=0, ge=0, le=limits.MAX_OFFSET),
              key=Depends(_admin), db: Session = Depends(get_db)):
    """The keys on this account, revoked ones included, a page at a time.

    Revoked keys stay in the list on purpose: "when did we turn that one off"
    is a question somebody asks during an incident, and a list that hides them
    answers it with silence. The keys not revoked come first (an account holds
    `keys.MAX_API_KEYS` of them at most, a page holds them all), then the
    revoked ones, each in the order they were made. Cut by the database: an
    account's every key came back in one answer."""
    rows = (db.query(AgentApiKey)
              .filter(AgentApiKey.tenant_id == key.tenant_id)
              .order_by(AgentApiKey.revoked_at.is_not(None), AgentApiKey.created_at,
                        AgentApiKey.id)
              .limit(limit).offset(offset)
              .all())
    names = {p.id: p.name for p in project_service.list_projects(
        db, key.tenant_id, include_archived=True)}
    return [_key_out(r, names) for r in rows]


@router.post("/keys", response_model=KeyCreated, status_code=status.HTTP_201_CREATED,
             responses=admin_errors(404, 409))
def create_key(body: KeyNew, key=Depends(_admin),
               db: Session = Depends(get_db)):
    """Issue a key. Its raw value is in this answer and never again: it is
    stored as a hash. `project_id` binds it to a project of the account (left
    out: the default one); an archived project is a 409 `project_archived`.
    `scopes` left out are the default ones, without `admin`; `users` confines
    it to some end users of its project. `expires_in_days` left out is the
    default lifetime, null never expires, 0 expires it now. 409 on a name
    already taken, 409 `too_many_keys` past the keys an account may hold
    that are not revoked."""
    extensions.hooks.before_new_key(db, key.tenant_id)
    project_id = (account_project(db, key, body.project_id, to_hold_keys=True).id
                  if body.project_id else None)
    try:
        row, raw = key_service.create_api_key(
            db, tenant_id=key.tenant_id, name=body.name,
            project_id=project_id, scopes=body.scopes, end_users=body.users,
            # Three things to say, three spellings, and they have to be told
            # apart. Omitting the field means "whatever you think is sensible"
            # and gets the default; sending null means "never", which is what
            # the screen's own list offers as `days: null`; sending 0 means
            # "expired now", which is how a key is retired without revoking it.
            #
            # Omitted and explicit-null both arrive as None, so the only way to
            # tell them apart is to ask which fields the caller actually sent.
            # Before this, null meant the default, so the screen's
            # "Never expires" quietly minted a ninety-day key — the option had
            # never once done what it said.
            expires_in_days=(body.expires_in_days
                             if "expires_in_days" in body.model_fields_set
                             else key_service.DEFAULT_LIFETIME_DAYS),
            # The maker's admin, read again where the key is made: an
            # adoption may have taken it since the request came in.
            recheck=lambda s: still_admin(s, key),
        )
    except key_service.DuplicateKeyNameError:
        raise HTTPException(status.HTTP_409_CONFLICT, "a key with that name already exists")
    except key_service.TooManyKeys as e:
        raise CodedHTTPException(status.HTTP_409_CONFLICT, str(e), code="too_many_keys")
    except (key_service.UnknownScope, key_service.BadEndUsers) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))
    log_admin_action(db, key, audit_service.KEY_CREATE, target_id=str(row.id),
                     meta={"name": row.name,
                           **({"users": row.end_users} if row.end_users else {})},
                     project_id=row.project_id)
    return {**_key_out(row), "key": raw}


@public.get("/scopes")
def list_scopes():
    """The scopes a key can hold, and what each one opens.

    Open on purpose: knowing the names opens nothing, and not knowing them is
    how somebody mints a key with a scope nobody serves.
    \f
    It used to be written down in no reachable place, so the only way to
    learn the names was to create a key WITHOUT asking for any and read what
    came back. (`\\f` ends what /openapi.json shows: what follows is for
    whoever reads the code, and the published text says what the route does
    now, never its history; audit of 17 Oct 2026.)
    """
    return {"scopes": [
        {"name": "memories:read",
         "opens": "reading memories, facts, context, users, agents, audit"},
        # All of it: the same scope erases an end user for good and deletes
        # agent namespaces. The consent page an app's OAuth request opens
        # reads these words too (audit of 17 Oct 2026: it kept its own).
        {"name": "memories:write",
         "opens": "writing, changing and deleting memories and facts, erasing an end "
                  "user for good, deleting agent namespaces, and batch loads"},
        {"name": "admin",
         "opens": "keys, webhooks, projects, provider settings, and the memories "
                  "and facts of every project of the account, whatever the key's own "
                  "project. Kept out of the default set: a leaked key that can mint "
                  "keys makes revoking it pointless."},
    ], "default": key_service.DEFAULT_SCOPES}


@router.post("/keys/{key_id}/revoke", response_model=Deleted,
             responses=admin_errors(404))
def revoke_key(key_id: str, key=Depends(_admin),
               db: Session = Depends(get_db)):
    """Revoke a key: from this moment every call made with it is a 401. It
    stays in GET /admin/keys, with `revoked_at`. 404 for a key that is not
    this account's, or is already revoked."""
    kid = _uuid(key_id, "key")
    extensions.hooks.before_key_revoked(db, key.tenant_id, kid)
    if not key_service.revoke_api_key(db, key_id=kid, tenant_id=key.tenant_id):
        # Not found, not owned and already revoked share one answer: telling
        # them apart tells whoever is probing which ids are real.
        raise _missing("live key")
    log_admin_action(db, key, audit_service.KEY_REVOKE, target_id=key_id)
    return {"deleted": True, "id": key_id}


# ── webhooks ────────────────────────────────────────────────────────────────

@router.get("/webhooks", response_model=List[WebhookOut])
def list_webhooks(key=Depends(_admin), db: Session = Depends(get_db)):
    """The account's webhook endpoints, with how each has been behaving: the
    failed attempts since its last accepted delivery, the last delivery and
    the last error, and whether it has a secret of before 0.1.11
    (`legacy_signature`), which since 0.2.0 is not sent. Never the secret."""
    from sqlalchemy import func as sqlfunc

    from ..db import AgentWebhookDelivery

    rows = webhook_service.list_endpoints(db, key.tenant_id)
    stats = {}
    for eid, last_at in (
        db.query(AgentWebhookDelivery.endpoint_id,
                 # created_at, not delivered_at: the second is null for an endpoint
                 # that has never succeeded, which is exactly the one somebody
                 # opens this page to find.
                 sqlfunc.max(AgentWebhookDelivery.created_at))
          .filter(AgentWebhookDelivery.tenant_id == key.tenant_id)
          .group_by(AgentWebhookDelivery.endpoint_id).all()
    ):
        stats[eid] = {"last_at": last_at}
    # The newest failure per endpoint, so a disabled webhook can say what broke.
    # Ordered descending and kept on first sight: one pass, no window function,
    # and the number of endpoints an account has is small.
    for eid, err in (
        db.query(AgentWebhookDelivery.endpoint_id, AgentWebhookDelivery.last_error)
          .filter(AgentWebhookDelivery.tenant_id == key.tenant_id,
                  AgentWebhookDelivery.last_error.isnot(None))
          .order_by(AgentWebhookDelivery.created_at.desc(), AgentWebhookDelivery.id.desc())
          .limit(500).all()
    ):
        stats.setdefault(eid, {"last_at": None})
        stats[eid].setdefault("last_error", err)
    return [_hook_out(r, stats) for r in rows]


@router.get("/webhooks/events")
def webhook_events(key=Depends(_admin)):
    """The events an endpoint can subscribe to here: the product's, and those
    the hosted service adds (`webhooks.add_event`). The screen offers these
    and nothing else, so it cannot offer one that never fires or miss one
    that does."""
    return {"events": sorted(webhook_service.VALID_EVENTS)}


@router.post("/webhooks", response_model=WebhookCreated, status_code=status.HTTP_201_CREATED,
             responses={**admin_errors(404, 409), **outbound_errors(400, 429)})
def create_webhook(body: WebhookNew, key=Depends(_admin),
                   db: Session = Depends(get_db)):
    """A webhook URL is an address this server will connect to, chosen by
    whoever holds the credential, so the service refuses anything that resolves
    somewhere it should not reach. 409 `too_many_endpoints` past the
    account's number of endpoints. 429 `rate_limit_exceeded` while the
    account has two tests or address checks under way already."""
    project_id = account_project(db, key, body.project_id).id if body.project_id is not None else None
    try:
        ep = webhook_service.create_endpoint(
            db, key.tenant_id, url=body.url, events=body.events, project_id=project_id,
            recheck=lambda s: still_admin(s, key))
    except webhook_service.UnsafeWebhookUrl as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except webhook_service.TooManyTests as e:
        raise _outbound_refused(e)
    except webhook_service.InvalidWebhookEvents as e:
        # A value the route does not take: 422, as an unknown scope is on
        # POST /admin/keys and a refused value on /v1. It was a 400, the
        # status of an address the check refuses (audit of 17 Oct 2026).
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))
    except webhook_service.TooManyEndpoints as e:
        raise CodedHTTPException(status.HTTP_409_CONFLICT, str(e), code="too_many_endpoints")
    # The address is not recorded: a webhook URL can carry a token.
    log_admin_action(db, key, audit_service.SETTING_CHANGE, target_id=str(ep.id),
                     meta={"setting": "webhook", "op": "create"})
    return {**_hook_out(ep), "secret": ep.secret}


@router.delete("/webhooks/{endpoint_id}", response_model=Deleted,
               responses=admin_errors(404))
def delete_webhook(endpoint_id: str, key=Depends(_admin),
                   db: Session = Depends(get_db)):
    """Delete an endpoint: nothing more is delivered to it, and its secret is
    gone. 404 for an endpoint that is not this account's."""
    eid = _uuid(endpoint_id, "endpoint")
    if not webhook_service.delete_endpoint(db, key.tenant_id, eid):
        raise _missing("endpoint")
    log_admin_action(db, key, audit_service.SETTING_CHANGE, target_id=endpoint_id,
                     meta={"setting": "webhook", "op": "delete"})
    return {"deleted": True, "id": endpoint_id}


# ── projects ────────────────────────────────────────────────────────────────

@router.get("/projects")
def list_projects(include_archived: bool = False,
                  key=Depends(_admin), db: Session = Depends(get_db)):
    """The account's projects, the default one first, each with its region and
    how many live keys it holds. Archived ones only with
    `include_archived=true`. An account with no project gets its default one
    here."""
    from sqlalchemy import func as sqlfunc

    rows = project_service.list_projects(db, key.tenant_id,
                                         include_archived=include_archived)
    if not rows:
        # An account with no project at all cannot be shown: the switcher has
        # nothing to select and every page that scopes by project is empty.
        # Creating it here rather than only at `create-tenant` means an install
        # made before that fix mends itself the first time somebody looks,
        # instead of needing a command nobody knows to run.
        project_service.get_or_create_default_project(db, key.tenant_id)
        rows = project_service.list_projects(db, key.tenant_id,
                                             include_archived=include_archived)
    counts = dict(db.query(AgentApiKey.project_id, sqlfunc.count(AgentApiKey.id))
                    .filter(AgentApiKey.tenant_id == key.tenant_id,
                            AgentApiKey.revoked_at.is_(None))
                    .group_by(AgentApiKey.project_id).all())
    # An envelope, not a bare list. The screen reads `.projects`, and returning
    # the list gave it `undefined.filter` and a blank page: the endpoint
    # existed, answered 200, and was still wrong. Shapes are a contract too.
    return {"projects": [_project_out(r, counts, db) for r in rows], "total": len(rows)}


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED,
             responses=admin_errors(409))
def create_project(body: ProjectNew, key=Depends(_admin),
                   db: Session = Depends(get_db)):
    """Create a project: a wall inside the account, whose keys read and write
    its memories only. `region` (`eu` or `global`) decides where a model reads
    them; left out, on the Cloud the project is Global, and on an install of
    one's own it follows the install's default writer. 409 on a name already
    taken, and 409 `too_many_projects` past the 100 projects an account may
    hold, archived ones included."""
    try:
        row = project_service.create_project(
            db, tenant_id=key.tenant_id, name=body.name, description=body.description,
            region=body.region)
    except project_service.DuplicateProjectError:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            {"code": project_service.DuplicateProjectError.code,
                             "message": "a project with that name already exists"})
    except project_service.TooManyProjects as e:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": e.code, "message": str(e)})
    log_admin_action(db, key, audit_service.SETTING_CHANGE, target_id=str(row.id),
                     meta={"setting": "project", "op": "create"}, project_id=row.id)
    return _project_out(row, db=db)


@router.get("/projects/{project_id}", response_model=ProjectOut,
            responses=admin_errors(404))
def get_project(project_id: str, key=Depends(_admin),
                db: Session = Depends(get_db)):
    """One project of the account, with its region and where its memories are
    read now (`processing_region`). 404 for a project that is not this
    account's."""
    row = account_project(db, key, project_id)
    return _project_out(row, db=db)


@router.patch("/projects/{project_id}", response_model=ProjectOut,
              responses=admin_errors(404, 409))
def update_project(project_id: str, body: ProjectPatch,
                   key=Depends(_admin), db: Session = Depends(get_db)):
    """Rename a project, change its description or its region. A region sent
    as null makes the project Global on the Cloud, and on an install of one's
    own makes it follow the install's default writer again; left out, it is
    unchanged. A new region is used from the next write: no fact is extracted
    again. 409 on a name already taken."""
    row = account_project(db, key, project_id)
    try:
        changes = {"region": body.region} if "region" in body.model_fields_set else {}
        row = project_service.rename_project(
            db, row, name=body.name, description=body.description, **changes)
    except project_service.DuplicateProjectError:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            {"code": project_service.DuplicateProjectError.code,
                             "message": "a project with that name already exists"})
    log_admin_action(db, key, audit_service.SETTING_CHANGE, target_id=str(row.id),
                     meta={"setting": "project", "op": "rename", **changes}, project_id=row.id)
    return _project_out(row, db=db)


@router.post("/projects/{project_id}/archive", response_model=ProjectOut,
             responses=admin_errors(404, 409))
def archive_project(project_id: str, key=Depends(_admin),
                    db: Session = Depends(get_db)):
    """Hide a project from the lists without losing anything it holds;
    `/unarchive` brings it back. 409 for the default project, and for a
    project with live keys: move or revoke them first. An archived project
    takes no new key."""
    row = account_project(db, key, project_id)
    try:
        row = project_service.archive_project(db, row)
    except project_service.ProjectRefused as e:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": e.code, "message": str(e)})
    log_admin_action(db, key, audit_service.SETTING_CHANGE, target_id=str(row.id),
                     meta={"setting": "project", "op": "archive"}, project_id=row.id)
    return _project_out(row, db=db)


@router.post("/projects/{project_id}/unarchive", response_model=ProjectOut,
             responses=admin_errors(404))
def unarchive_project(project_id: str, key=Depends(_admin),
                      db: Session = Depends(get_db)):
    """Back from the archive: the project is in the lists again and takes
    keys. 404 for a project that is not this account's.
    \f
    The service could do it and no route or command reached it, so archiving
    a project could not be undone (the audit of 6 October, row 14)."""
    row = account_project(db, key, project_id)
    row = project_service.unarchive_project(db, row)
    log_admin_action(db, key, audit_service.SETTING_CHANGE, target_id=str(row.id),
                     meta={"setting": "project", "op": "unarchive"}, project_id=row.id)
    return _project_out(row, db=db)


@router.delete("/projects/{project_id}", response_model=ProjectDeleted,
               responses=admin_errors(404, 409))
def delete_project(project_id: str, erase: bool = False, key=Depends(_admin),
                   db: Session = Depends(get_db)):
    """Delete a project. What it remembered moves to the default one.

    Keys, memories, facts, entity aliases, audit rows and queued batches
    (an alias whose name the default project decided differently stays out,
    and `aliases_not_moved` counts it), and the relations it learned.

    Its webhook endpoints do not move, erased or not: they are deleted with
    the project, their signing secrets and every delivery still waiting
    included. An endpoint of no project, which receives every project's
    events, stays.

    `?erase=true` erases instead of moving: every memory and fact the project
    held is physically deleted, history included, with its batch jobs, and an
    erasure audit row (counts, never content) is filed and returned as
    `audit_id`. It counts as erasing each of the project's end users: each
    also gets an erasure row of their own, under their pseudonym and with
    no counts, and what the trail writes about them afterwards names them by
    that pseudonym. The keys still move. What is erased does not come back, and
    no read, `include_invalidated=true` included, returns it: the same
    erasure as DELETE /v1/users/{id}/memories, for a whole project.
    \f
    The earlier version moved only the keys and said "nothing anybody
    remembered is touched", which was true while a project was a label and
    became false the day it became a wall: untouched rows kept a project id
    that no longer existed, and nothing could read them or erase them again."""
    row = account_project(db, key, project_id)
    try:
        moved = project_service.delete_project(db, key.tenant_id, row, erase=erase)
    except project_service.ProjectRefused as e:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": e.code, "message": str(e)})
    # Erased, its memories and facts are gone: an erase. Otherwise they
    # moved to the default project and the project is a setting gone.
    log_admin_action(db, key, audit_service.ERASE if erase else audit_service.SETTING_CHANGE,
                     target_id=project_id,
                     meta={"setting": "project", "op": "erase" if erase else "delete",
                           "keys_moved": moved["keys"],
                           "apps_disconnected": moved["apps_disconnected"]})
    return {"deleted": True, "keys_moved": moved["keys"],
            "apps_disconnected": moved["apps_disconnected"],
            "keys_revoked": moved["keys_revoked"],
            "memories_moved": moved["memories"], "facts_moved": moved["facts"],
            "facts_merged": moved["facts_merged"],
            "aliases_moved": moved["aliases_moved"],
            "aliases_not_moved": moved["aliases_not_moved"],
            "memories_deleted": moved["memories_deleted"],
            "facts_deleted": moved["facts_deleted"],
            "batches_deleted": moved["batches_deleted"],
            "erasure": "permanent" if erase else None,
            "audit_id": moved["audit_id"]}


# ── consumption ─────────────────────────────────────────────────────────────

@router.get("/usage", dependencies=[Depends(_not_on_the_service)])
def usage(days: int = 30, key=Depends(_admin),
          db: Session = Depends(get_db)):
    """What the model provider has been asked to do, and by whom, over the
    last `days` days: the bill of this install's own provider account, by
    agent and by model. `by_agent` names the 50 agents with the most output
    tokens; the rest are one `other_agents` entry (null when there are
    none), and the calls without an agent id are counted in
    `calls_without_an_agent_id`. An install of one's own only: the hosted
    service answers 404.
    \f
    The hosted service cannot have this page: there that number is the
    vendor's cost, not the customer's bill. Here the bill arrives at the
    operator's own account, so "which of my agents burned four hundred euros
    last month" is their question and nobody else can answer it.

    Not a response_model: the shape is three groupings whose keys differ, and
    pinning it to a Pydantic class would mean three classes and a union for no
    gain. It is read by one screen.
    """
    return usage_report.report(db, key.tenant_id, days=days,
                               rates_raw=get_settings().llm_rates)


# ── facts: forget and correct ───────────────────────────────────────────────
#
# Neither of these deletes anything. Forgetting closes a fact, correcting
# writes a new one and closes the old against it. If either mutated a row, the
# product would stop being able to answer what it believed in March, which is
# the only question it exists to answer.

class FactCorrection(Body):
    # The limits of /v1's correction (api/facts.CorrectBody): without them a
    # 201-character predicate answered 500 and a 600-character object was cut
    # to 500 in silence, where /v1 answers 422 naming the field.
    subject: Optional[str] = Field(default=None, min_length=1, max_length=limits.MAX_FACT_NAME_CHARS)
    predicate: Optional[str] = Field(default=None, min_length=1, max_length=limits.MAX_FACT_NAME_CHARS)
    object: Optional[str] = Field(default=None, min_length=1, max_length=limits.MAX_FACT_OBJECT_CHARS)


@router.post("/facts/{fact_id}/forget",
             responses=admin_errors(404))
def forget_fact(fact_id: str, key=Depends(_admin),
                db: Session = Depends(get_db)):
    """Take a fact out of the current state. It stays in history with the date
    it stopped being true, and `as_of` before that date still returns it."""
    from ..services.ids import parse_fact_id

    fid = parse_fact_id(fact_id)
    out = (facts_service.forget_fact(db, key.tenant_id, fid, project_id=EVERY_PROJECT)
           if fid is not None else None)
    if out is None:
        raise _missing("fact")
    log_admin_action(db, key, audit_service.FACT_INVALIDATE, target_id=fact_id)
    return out


@router.patch("/facts/{fact_id}",
              responses=admin_errors(404, 409, 503, descriptions={503: ADMIN_MODEL_503}))
def correct_fact(fact_id: str, body: FactCorrection,
                 key=Depends(_admin), db: Session = Depends(get_db)):
    """Supersede a fact with a corrected one. Both stay readable.

    409 `fact_not_current` for a fact that is history, with
    `current_fact_id`, as on PATCH /v1/facts/{fact_id}. 503 when a model the
    correction needs (the vocabulary, the contradiction
    check) gave no usable answer: nothing is written. The new fact's own
    vector is not one: it is retried by the worker.
    \f
    It used to be written anyway when the contradiction check failed, and a
    500 when the vocabulary did (2026-09-28)."""
    from ..services.ids import parse_fact_id

    fields = facts_service.correction_fields(body.subject, body.predicate, body.object)
    if fields is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "give at least one of subject, predicate or object")
    fid = parse_fact_id(fact_id)
    if fid is None:
        raise _missing("fact")
    # A correction is a write for the hosted quota, as on PATCH /v1/facts: it
    # calls the models the plan pays for (audit of 17 Oct 2026). Counted when
    # it commits; a 404, 409 or 503 rolls back and costs nothing.
    # A refusal of the quota is in the trail as every other door's
    # (memory_write._kept_in_the_trail; review of the audit of 27 Oct 2026).
    from ..services import memory_write

    memory_write.before_write(db, key.tenant_id, [""], endpoint="facts:correct",
                              principal=key, target_id=fact_id)
    try:
        out = facts_service.correct_fact(db, key.tenant_id, fid, project_id=EVERY_PROJECT, **fields)
    except facts_service.FactNotCurrent as e:
        from .facts import not_current_refusal

        raise not_current_refusal(e)
    except facts_service.MODEL_FAILURES:
        db.rollback()
        if _llm.not_now_here():
            raise service_paused()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "a model the correction needs gave no usable answer; "
                            "nothing was written, try again")
    if out is None:
        raise _missing("fact")
    log_admin_action(db, key, audit_service.FACT_WRITE, target_id=fact_id, meta={"op": "correct"})
    return out


# ── entity aliases (2026-09-27) ─────────────────────────────────────────────
# The rule of memory_core (`identity`): a
# former name points to the current one ("Acme -> Globex"), dated, with who
# decided it. The store adds one by itself when a memory says something was
# renamed; the operator merges or undoes here. An alias lives in ONE memory:
# project + end user + agent namespace, the key facts are deduplicated on.

class AliasBody(Body):
    alias: str = Field(min_length=1, max_length=limits.MAX_FACT_NAME_CHARS)
    canonical: str = Field(min_length=1, max_length=limits.MAX_FACT_NAME_CHARS)
    user_id: Optional[str] = Field(default=None, max_length=limits.MAX_ID_CHARS, description="End user the alias applies to.")
    agent_id: Optional[str] = Field(default=None, max_length=limits.MAX_ID_CHARS)
    project_id: Optional[str] = None


def _alias_out(a) -> dict:
    return {
        "id": str(a.id), "alias": a.alias_label, "canonical": a.canonical_label,
        "user_id": a.end_user_id, "agent_id": a.agent_id,
        "project_id": str(a.project_id) if a.project_id else None,
        "since": a.since.isoformat() if a.since else None,
        "source": a.source,
        "confidence": float(a.confidence) if a.confidence is not None else None,
    }


@router.get("/aliases")
def list_aliases(user_id: Optional[str] = None, key=Depends(_admin),
                 db: Session = Depends(get_db)):
    """The entity aliases of the account ("Acme" is now "Globex"), newest first,
    at most 500; `user_id` narrows to one end user's."""
    from ..db.models import AgentEntityAlias

    q = db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == key.tenant_id)
    if user_id is not None:
        q = q.filter(AgentEntityAlias.end_user_id == user_id)
    rows = q.order_by(AgentEntityAlias.since.desc(), AgentEntityAlias.id).limit(500).all()
    return {"aliases": [_alias_out(a) for a in rows]}


@router.post("/aliases", status_code=status.HTTP_201_CREATED,
             responses=admin_errors(404, 409))
def create_alias(body: AliasBody, key=Depends(_admin), db: Session = Depends(get_db)):
    """Merge `alias` into `canonical` in one memory. Every fact of that memory
    is re-canonicalized at once; open duplicates born from the merge collapse
    into one row with all their sources. 404 for a project that is not this
    account's; 409 on a cycle or on an alias already decided differently by
    hand, or 409 `too_many_names` past the names an account may have
    declared; 422 for names that are not entities, or the same name twice."""
    from ..services.facts import entities as _ent

    if body.project_id:
        pid = account_project(db, key, body.project_id).id
    else:
        # The memory the facts are written in: the key's project (its
        # Default as the fallback, `key_project`). With no project the alias
        # sat in a memory of no project, which no fact of an install with
        # projects is in: 201, and it never applied (the identity bench,
        # 17 Oct 2026: 25 aliases, no effect).
        pid = key_service.key_project(db, key)
    # A write for the hosted quota, as POST /v1/users/{id}/names: it re-embeds
    # every fact that names the alias. A refusal of the quota is in the trail
    # as every other door's (memory_write.before_write).
    from ..services import memory_write

    memory_write.before_write(db, key.tenant_id, [""], endpoint="admin:aliases:create",
                              principal=key)
    try:
        row, outcome = _ent.declare_alias(db, key.tenant_id, pid, body.user_id, body.agent_id,
                                         body.alias, body.canonical)
    except _ent.TooManyNames as e:
        raise CodedHTTPException(status.HTTP_409_CONFLICT, str(e), code="too_many_names")
    # 422 for names the route does not take, as on every other route that
    # refuses a value (audit of 17 Oct 2026: they were 400s).
    if outcome == "invalid":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "alias and canonical must be entity names")
    if outcome == "conflict":
        raise HTTPException(status.HTTP_409_CONFLICT, "this alias would create a cycle or overrides a manual alias")
    if row is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "alias and canonical are the same name")
    db.commit()
    db.refresh(row)
    log_admin_action(db, key, audit_service.SETTING_CHANGE, target_id=str(row.id),
                     meta={"setting": "alias", "op": "create"})
    return _alias_out(row)


@router.delete("/aliases/{alias_id}", response_model=Deleted,
               responses=admin_errors(404))
def delete_alias(alias_id: str, key=Depends(_admin), db: Session = Depends(get_db)):
    """Remove an alias: it goes, and every fact takes back the canonical
    names its written words give without it. Not an undo: two open facts the
    alias had merged stay one row (the newer was deleted), and a plan it had
    closed stays closed (memory_core.store.remove_alias). The way back
    from a name declared with `POST /v1/users/{id}/names` too."""
    # Audit of 19 Oct 2026: published as "Undo: ... the facts go back to their
    # written names", which the merge it cannot split made untrue.
    from ..db.models import AgentEntityAlias
    from ..services.facts import entities as _ent
    from ..services.memory_core import store as _store

    row = _owned(db, key, AgentEntityAlias, alias_id, "alias")
    mem = _ent.memory_of(row.tenant_id, row.project_id, row.end_user_id, row.agent_id)
    # It re-embeds the facts that name it: a write for the hosted quota, a
    # refusal in the trail (memory_write.before_write).
    from ..services import memory_write

    memory_write.before_write(db, key.tenant_id, [""], endpoint="admin:aliases:delete",
                              principal=key, target_id=alias_id)
    _store.remove_alias(db, _ent.TABLES, mem, row)
    db.commit()
    log_admin_action(db, key, audit_service.SETTING_CHANGE, target_id=alias_id,
                     meta={"setting": "alias", "op": "delete"})
    return Deleted(id=alias_id, deleted=True)


def _link_out(r) -> dict:
    """An object link (memory_core 6.1.0, "the same as", off by default):
    the two names, the memory, who decided, the memory and fact each name
    came from, and the writer's descriptions and verdicts (`reason`)."""
    from ..services.ids import fact_public_id, mem_public_id

    return {
        "id": str(r.id), "name": r.name_label, "same_as": r.canonical_label,
        "user_id": r.end_user_id, "agent_id": r.agent_id,
        "project_id": str(r.project_id) if r.project_id else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "decided_by": r.decided_by,
        "source_memory_id": mem_public_id(r.source_memory_id) if r.source_memory_id else None,
        "source_fact_id": fact_public_id(r.source_fact_id) if r.source_fact_id else None,
        "canonical_memory_id": mem_public_id(r.canonical_memory_id) if r.canonical_memory_id else None,
        "reason": r.reason,
    }


@router.get("/object-links")
def list_object_links(user_id: Optional[str] = None, key=Depends(_admin),
                      db: Session = Depends(get_db)):
    """The object links of the account ("Italy" read as "Italia": one thing
    in two languages, judged by the writer, memory_core 6.1.0), newest
    first, at most 500; `user_id` narrows to one end user's. Not aliases:
    no fact was rewritten for them (services/facts/links.py)."""
    from ..db.models import AgentObjectLink as L

    q = db.query(L).filter(L.tenant_id == key.tenant_id)
    if user_id is not None:
        q = q.filter(L.end_user_id == user_id)
    rows = q.order_by(L.created_at.desc(), L.id).limit(500).all()
    return {"object_links": [_link_out(r) for r in rows]}


@router.delete("/object-links/{link_id}", response_model=Deleted,
               responses=admin_errors(404))
def delete_object_link(link_id: str, key=Depends(_admin), db: Session = Depends(get_db)):
    """Undo an object link: the row goes and nothing else changes. A link
    never rewrote, merged or closed a fact, so the facts read as two objects
    again, exactly as before it."""
    from ..db.models import AgentObjectLink
    from ..services.facts import entities as _ent
    from ..services.facts.links import forget_prefetched
    from ..services.memory_core import objects as _objects

    row = _owned(db, key, AgentObjectLink, link_id, "object link")
    _objects.remove_link(db, _ent.TABLES, row)
    forget_prefetched(db)
    db.commit()
    log_admin_action(db, key, audit_service.SETTING_CHANGE, target_id=link_id,
                     end_user_id=row.end_user_id, project_id=row.project_id,
                     meta={"setting": "object_link", "op": "delete"})
    return Deleted(id=link_id, deleted=True)


# ── the rest of the screen ──────────────────────────────────────────────────

class KeyMove(Body):
    project_id: Optional[str] = None


@router.post("/keys/{key_id}/move", response_model=KeyOut,
             responses=admin_errors(404, 409))
def move_key(key_id: str, body: KeyMove, key=Depends(_admin),
             db: Session = Depends(get_db)):
    """Put a key in another project of the account: from its next call it
    reads and writes there. 404 for a key or a project that is not this
    account's; 409 `project_archived` for an archived project, and
    `connected_app_key` for the key of an app connected over OAuth, which
    the person allowed into its own project only."""
    kid = _uuid(key_id, "key")
    pid = account_project(db, key, body.project_id, to_hold_keys=True).id if body.project_id else None
    try:
        moved = key_service.move_key(db, key_id=kid, tenant_id=key.tenant_id, project_id=pid)
    except key_service.ConnectedAppKey:
        raise CodedHTTPException(
            status.HTTP_409_CONFLICT,
            "This key belongs to an app connected over OAuth, which the person allowed into its "
            "project only. Connect the app again and choose the other project.",
            code="connected_app_key")
    if not moved:
        raise _missing("key")
    row = db.query(AgentApiKey).filter(AgentApiKey.id == kid).one()
    log_admin_action(db, key, audit_service.KEY_CHANGE, target_id=key_id, meta={"op": "move"},
                     project_id=row.project_id)
    return _key_out(row)


def _outbound_refused(e) -> CodedHTTPException:
    """The 429 of an account with its outbound waits under way already
    (core/threads.OUTBOUND_PER_ACCOUNT: tests and address checks of webhooks)."""
    return CodedHTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(e),
                              code="rate_limit_exceeded",
                              headers={"Retry-After": str(e.retry_after)})


@router.post("/webhooks/{endpoint_id}/test",
             responses={**admin_errors(404, 409), **outbound_errors(429)})
def test_webhook(endpoint_id: str, key=Depends(_admin),
                 db: Session = Depends(get_db)):
    """Send one delivery now and say what came back.

    Synchronous, because the question is "is my endpoint reachable and is my
    signature check right", and an answer that turns up in a log fifteen
    seconds later does not answer it. 409 `endpoint_disabled` for an
    endpoint switched off: enable it first. 429 `rate_limit_exceeded` while
    the account has two tests running already (`webhooks.send_test`).
    """
    eid = _uuid(endpoint_id, "endpoint")
    try:
        out = webhook_service.send_test(db, key.tenant_id, eid)
    except webhook_service.EndpointDisabled as e:
        raise CodedHTTPException(status.HTTP_409_CONFLICT, str(e), code="endpoint_disabled")
    except webhook_service.TooManyTests as e:
        raise _outbound_refused(e)
    if out is None:
        raise _missing("endpoint")
    return out


@router.post("/webhooks/{endpoint_id}/enable", response_model=WebhookOut,
             responses={**admin_errors(404), **outbound_errors(400, 429)})
def enable_webhook(endpoint_id: str, key=Depends(_admin), db: Session = Depends(get_db)):
    """Switch a disabled endpoint back on: its URL checked again, its
    failures forgotten, URL and secret kept. An endpoint is switched off
    after a delivery spent its ten attempts, about three days, with nothing
    accepted since.
    \f
    Until 2026-10-06 there was no way back but delete and create, which
    changed the secret every receiver verifies with."""
    try:
        ep = webhook_service.enable_endpoint(db, key.tenant_id, _uuid(endpoint_id, "endpoint"),
                                             recheck=lambda s: still_admin(s, key))
    except webhook_service.UnsafeWebhookUrl as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except webhook_service.TooManyTests as e:
        raise _outbound_refused(e)
    if ep is None:
        raise _missing("endpoint")
    log_admin_action(db, key, audit_service.SETTING_CHANGE, target_id=str(ep.id),
                     meta={"setting": "webhook", "op": "enable"})
    return _hook_out(ep)


@router.get("/requests/summary")
def requests_summary(days: int = 30, key=Depends(_admin),
                     db: Session = Depends(get_db)):
    """The calls on the memory over the last `days` days (1 to 400, default
    30), off the audit trail: reads, writes, and how many went to each
    endpoint. The administration is the audit log's, not here."""
    return usage_report.requests_summary(db, key.tenant_id, days=days)


@router.get("/requests/daily")
def requests_daily(days: int = 30, key=Depends(_admin),
                   db: Session = Depends(get_db)):
    """The same calls one row per day over the last `days` days (1 to 400,
    default 30), writes and reads apart, the quiet days as zero."""
    return usage_report.requests_daily(db, key.tenant_id, days=days)


# ── logging in ──────────────────────────────────────────────────────────────

class Credentials(Body):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class Session_(BaseModel):
    token: str
    expires_at: datetime
    username: str
    tenant_id: str


@public.post("/session", response_model=Session_,
             responses=admin_errors(401, 422, 429),
             dependencies=[Depends(_not_on_the_service)])
def log_in(body: Credentials, request: Request, db: Session = Depends(get_db)):
    """Open a session. The one route here that needs no credential.

    An unknown username, a wrong password and a disabled account get the same
    answer, because telling them apart turns this into a way of finding out
    which usernames exist. Ten wrong answers lock that username from that
    address for fifteen minutes, thirty from one address lock the address, and
    the locks are rows so a restart does not clear them (services/operators.py
    has the three ceilings).
    """
    s = get_settings()
    try:
        out = operator_service.login(
            db, username=body.username, password=body.password,
            hours=s.session_hours,
            user_agent=request.headers.get("user-agent"),
            ip=(request.client.host if request.client else None),
        )
    except operator_service.LockedOut as e:
        wait = max(1, int((e.until - datetime.now(timezone.utc)).total_seconds()))
        raise CodedHTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many attempts. Try again after {e.until.isoformat()}.",
            code="too_many_attempts", headers={"Retry-After": str(wait)},
        )
    except operator_service.TooBusy:
        raise CodedHTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many logins at once. Try again in a few seconds.",
            code="too_many_attempts", headers={"Retry-After": "5"},
        )
    if out is None:
        # Not `invalid_key`, the code a 401 carries elsewhere: nobody sent a key.
        raise CodedHTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password",
                                 code="invalid_credentials")
    row, raw = out
    op = db.get(Operator, row.operator_id)
    # Who came in and from where; the failures are login_attempts' rows.
    log_admin_action(db, op, audit_service.LOGIN, target_id=op.username,
                     meta={"ip": (request.client.host if request.client else None)})
    return {"token": raw, "expires_at": row.expires_at,
            "username": op.username, "tenant_id": str(op.tenant_id)}


@public.delete("/session", response_model=Deleted)
def log_out(request: Request, db: Session = Depends(get_db)):
    """End this session. Deliberately deletes the row rather than marking it:
    a token that still exists somewhere is a token somebody can still use.

    Filed as a `logout`, as signing out everywhere is: the trail had every
    sign-in and none of the sign-outs (functional test of 28 Oct 2026)."""
    auth = request.headers.get("authorization") or ""
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    op = operator_service.logout(db, token)
    if op is not None:
        log_admin_action(db, op, audit_service.LOGOUT, target_id=op.username,
                         meta={"what": "operator_session", "ended": 1})
    return {"deleted": op is not None, "id": "session"}


@router.delete("/sessions", response_model=Deleted,
               responses=admin_errors(409))
def log_out_everywhere(key=Depends(_admin), db: Session = Depends(get_db)):
    """End every session of the operator asking, this one included: a laptop
    left signed in somewhere stops working at once: "Sign out of all
    devices". A key has no sessions: it is revoked instead (409)."""
    if not hasattr(key, "username"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Only a dashboard sign-in has sessions; revoke the key instead.")
    ended = operator_service.logout_everywhere(db, key.id)
    log_admin_action(db, key, audit_service.LOGOUT, target_id=key.username,
                     meta={"what": "operator_sessions", "ended": ended})
    return {"deleted": ended > 0, "id": "sessions"}


@router.get("/whoami")
def whoami(key=Depends(_admin)):
    """What the screen asks on load: who am I and which account am I in."""
    is_operator = hasattr(key, "username")
    return {
        "kind": "operator" if is_operator else "api_key",
        "name": key.username if is_operator else (key.name or "api key"),
        "tenant_id": str(key.tenant_id),
    }


# The widest `days` window the Memories page reads: a hundred years.
MAX_WINDOW_DAYS = 36500


# ── what the operator looks at ──────────────────────────────────────────────
#
# Memories, facts and agents, read by a person rather than by a program. These
# are not `/v1` routes and deliberately so: `/v1` is a published contract with
# a version in its name, held by whatever an agent runs with, and a screen's
# needs should not be able to move it. They delegate to the same engine, so
# there is one implementation and two doors, not two implementations.

def _on_the_screen(engine, rows: list[dict]) -> list[dict]:
    """The engine's memories in the shape the dashboard reads: /v1's
    (`user_id`, `mem_` ids, `client`), with each memory's categories. These
    routes answered the engine's dict, so the Memories page's End user
    column, the Playground's end-user filter and the drawer's facts read
    keys that never came (audit of 17 Oct 2026)."""
    families = engine.families_of([r["id"] for r in rows])
    return [{**public_memory(r), "categories": families.get(str(r["id"]), [])} for r in rows]


@router.get("/memories")
def list_memories(user_id: Optional[str] = None, agent_id: Optional[str] = None,
                  family: Optional[str] = None,
                  # Bounded: a day count is subtracted from now, and past
                  # what a date can hold that subtraction raised and
                  # answered 500 (audit of 17 Oct 2026). A hundred years is
                  # every memory there is, so the bound says no more than
                  # 0 does; the trail's reports clamp to its 400 days of
                  # retention instead, which here would hide older memories.
                  days: Optional[int] = Query(
                      default=None, ge=0, le=MAX_WINDOW_DAYS,
                      description="Only memories of the last `days` days. 0 or left out: "
                                  "all of them."),
                  limit: int = 20,
                  offset: int = Query(default=0, ge=0, le=limits.MAX_OFFSET),
                  key=Depends(_admin), db: Session = Depends(get_db)):
    """The Memories page: the account's memories, newest first. `days` is its
    1d / 7d / 30d window and `agent_id=__none__` its "no agent" choice.
    \f
    The screen sent both and this read neither, so a memory of two months ago
    sat in "1d" (the audit of 2026-10-06, row 10)."""
    from datetime import timedelta

    from ..services.ids import mem_public_id
    from ..services.memory_engine import NO_AGENT, AgentMemoryEngine
    from ..services.sentinels import EVERY_END_USER as _ALL

    # By the rule of every door, as GET /v1/memories: a NUL byte reached the
    # driver and answered 500 (audit of 19 Oct 2026).
    validate_filters(user_id=user_id, agent_id=agent_id)
    after = (datetime.now(timezone.utc) - timedelta(days=days)) if days else None
    _count_a_keys_read(db, key)
    engine = AgentMemoryEngine(db, key.tenant_id)
    rows, total = engine.get_all(
        end_user_id=user_id if user_id is not None else _ALL,
        agent_id=NO_AGENT if agent_id == "__none__" else agent_id,
        family=family, created_after=after, limit=limit, offset=offset)
    log_v1_request(db, key.tenant_id, "admin:memories:list", principal=key, end_user_id=user_id,
                   memories_read=[mem_public_id(r["id"]) for r in rows])
    return {"memories": _on_the_screen(engine, rows), "total": total}


@router.get("/memories/search",
            responses=admin_errors(404, 503, descriptions={503: ADMIN_MODEL_503}))
async def search_memories(query: str, limit: int = 20, user_id: Optional[str] = None,
                          project_id: Optional[str] = None,
                          key=Depends(_admin), db: Session = Depends(get_db)):
    """`project_id`: what a key of that project would find (the
    Playground); without it, the whole account. 404 for a project that is
    not this account's.
    \f
    Async since 31 Oct 2026: it waits on the loop for its account's turn at
    the embedder once its checks have passed, as POST /v1/memories/search
    (core/auth.a_turn_for_its_question): the Playground searched by a person
    while the account's agents send a burst waits its turn."""
    from ..services.ids import mem_public_id
    from ..services.memory_engine import AgentMemoryEngine, MemorySearchUnavailable
    from ..services.sentinels import EVERY_END_USER as _ALL

    def checked():
        # The question and the end user by the rule of POST /v1/memories/search
        # (audit of 19 Oct 2026).
        problem = limits.query_problem(query)
        if problem:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)
        validate_filters(user_id=user_id)
        within = _within_project(db, key, project_id)
        _count_a_keys_read(db, key)
        return within, ready_to_wait(db, key)

    def searched(within):
        engine = AgentMemoryEngine(db, key.tenant_id, within)
        try:
            hits = engine.search(query, end_user_id=user_id if user_id is not None else _ALL,
                                 limit=limit)
        except MemorySearchUnavailable as e:
            if not is_signed_in_person(key):
                give_back_the_read(key.tenant_id)   # not answered: not a read
            # 503, not an empty list. "You have no memories about this" is a
            # statement about their data, and making it out of our failure, with
            # a 200 on it, is the kind of lie this codebase keeps finding.
            if e.paused:
                raise service_paused()
            from ..core.threads import BUSY_RETRY_SECONDS

            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e),
                                headers={"Retry-After": str(BUSY_RETRY_SECONDS)})
        log_v1_request(db, key.tenant_id, "admin:memories:search", principal=key,
                       end_user_id=user_id, project_id=within,
                       memories_read=[mem_public_id(h["id"]) for h in hits])
        return _on_the_screen(engine, hits)

    within, account = await on_a_request_thread(checked)
    async with a_turn_for_its_question(account):
        return await on_a_request_thread(searched, within)


# Above `/memories/{memory_id}`, and it has to stay there: routes match in the
# order they are registered, and below it `categories` would be read as a
# memory id and answer 404. It was registered twice, here and next to the
# request reports, which put two operations with one id in /openapi.json.
@router.get("/memories/categories")
def memory_categories(key=Depends(_admin), db: Session = Depends(get_db)):
    """A category is a predicate family: how many memories produced a live fact
    about where somebody lives, what they are subscribed to, and so on."""
    return usage_report.memory_categories(db, key.tenant_id)


@router.get("/memories/{memory_id}",
            responses=admin_errors(404))
def get_memory(memory_id: str, key=Depends(_admin), db: Session = Depends(get_db)):
    """One memory of the account, any project: its text, end user, agent,
    metadata and status. The read is in the audit trail, filed under the
    memory's project. 404 for a memory that is not this account's."""
    from ..db import AgentMemory
    from ..services.ids import parse_mem_id
    from ..services.memory_engine import AgentMemoryEngine

    # `mem_...` is the published form and is decoded at the edge, which is
    # what services/ids.py exists for.
    _count_a_keys_read(db, key)
    mem = _owned(db, key, AgentMemory, memory_id, "memory", parse_mem_id)
    row = AgentMemoryEngine(db, key.tenant_id).get(str(mem.id))
    if row is None:
        raise _missing("memory")
    # Every fact the memory asserted, the closed ones too, each with
    # `closed`: the drawer's "Facts extracted" reads `facts` and draws a
    # closed one struck through, and with only the facts true now (as GET
    # /v1/memories/{id} answers a program) it said "Facts extracted (0)" for
    # a memory whose fact a later one superseded, while its Timeline showed
    # the fact (functional test of 28 Oct 2026).
    shown = facts_service.facts_for_memory(db, key.tenant_id, mem.id, project_id=EVERY_PROJECT)
    now = datetime.now(timezone.utc)
    # Filed under the memory's project, where a key of it reads the trail.
    log_v1_request(db, key.tenant_id, "admin:memories:get", principal=key,
                   end_user_id=row.get("end_user_id"), target_id=memory_id,
                   project_id=mem.project_id, memories_read=[mem.id],
                   facts_read=[f.id for f in shown])
    return {**public_memory(row),
            "facts": [{**facts_service.serialize_fact(f), "closed": not facts_service.true_now(f, now)}
                      for f in shown]}


@router.get("/memories/{memory_id}/history",
            responses=admin_errors(404))
def memory_history(memory_id: str, key=Depends(_admin), db: Session = Depends(get_db)):
    """The life of one memory, in order: written, edited, each fact it
    produced and when that fact stopped being true, forgotten. The read is
    in the audit trail. 404 for a memory that is not this account's."""
    from ..services.facts import service as facts_service
    from ..services.ids import parse_mem_id

    from ..db import AgentMemory
    from ..services.ids import mem_public_id

    _count_a_keys_read(db, key)
    mem = _owned(db, key, AgentMemory, memory_id, "memory", parse_mem_id)
    events, shown = facts_service.memory_timeline(db, key.tenant_id, mem, project_id=EVERY_PROJECT)
    log_v1_request(db, key.tenant_id, "admin:memories:history", principal=key,
                   end_user_id=mem.end_user_id, target_id=mem_public_id(mem.id),
                   project_id=mem.project_id, memories_read=[mem.id],
                   facts_read=[f.id for f in shown])
    return {"id": mem_public_id(mem.id), "events": events}


@router.delete("/users/{end_user:path}/memories", response_model=UserErasureResponse,
               responses=admin_errors())
def erase_end_user_everywhere(end_user: str, key=Depends(_admin), db: Session = Depends(get_db)):
    """ERASE everything about one end user in EVERY project of the account: the
    same erasure as DELETE /v1/users/{id}/memories, which a key makes in its
    own project only, since the same id in another project may be another
    customer's person. For the account's administrator, never a project key.
    One erasure record for the account, with the end user's pseudonym.
    \f
    Until the audit of 17 Oct 2026 nothing erased across the account."""
    from ..services.ids import ERASED_PREFIX, is_pseudonym
    from ..services.pseudonyms import pseudonym
    from ..services.sentinels import EVERY_PROJECT

    # As DELETE /v1/users/{id}/memories: a NUL byte reached the driver and
    # answered 500 (audit of 19 Oct 2026).
    validate_filters(end_user=end_user)
    if is_pseudonym(end_user):
        # As DELETE /v1/users/{id}/memories: a pseudonym is what an erasure
        # left, and erasing it relabelled the records proving that erasure,
        # which GET /v1/audit and the replay after a restore then lost
        # (audit of 18 Oct 2026).
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"{ERASED_PREFIX!r} names an end user already erased; nothing to erase.")
    result = facts_service.forget_end_user(db, key.tenant_id, end_user, project_id=EVERY_PROJECT)
    log_admin_action(db, key, audit_service.ERASE, end_user_id=pseudonym(key.tenant_id, end_user),
                     meta={"memories": result.get("memories_deleted"),
                           "facts": result.get("facts_deleted"), "scope": "account"})
    return result


class MemoryDeleted(Deleted):
    #: "permanent" when `erase=true` erased it; absent for a forget.
    erasure: Optional[str] = None


@router.delete("/memories/{memory_id}", response_model=MemoryDeleted,
               response_model_exclude_none=True, responses=admin_errors(404))
def delete_memory(memory_id: str, erase: bool = Query(default=False, description=(
                      "Erase it for good, as DELETE /v1/memories/{id}?erase=true does; a "
                      "memory forgotten earlier too.")),
                  key=Depends(_admin), db: Session = Depends(get_db)):
    """The same forget as DELETE /v1/memories/:id, because it is the same
    act: the memory goes, the facts only it stated stop being true, and the
    forget is filed. 404 for a memory that is not this account's, or is
    forgotten already. `erase=true` is the same erasure as the API's, for
    the dashboard's Erase, and takes a memory forgotten earlier too.
    \f
    `erase` arrived on the audit of 18 Oct 2026: the screen had only the
    forget.

    Until 2026-10-06 this route only set `deleted_at`: the operator saw the
    sentence disappear while the agent kept receiving its facts in
    /v1/context (the audit of 6 October, row 2)."""
    from ..db import AgentMemory
    from ..services.ids import parse_mem_id

    live = () if erase else (AgentMemory.deleted_at.is_(None),)
    mem = _owned(db, key, AgentMemory, memory_id, "memory", parse_mem_id, *live)
    end_user, project = mem.end_user_id, mem.project_id
    act = facts_service.erase_memory if erase else facts_service.forget_memory
    if act(db, key.tenant_id, mem.id, project_id=EVERY_PROJECT) is None:
        raise _missing("memory")
    log_admin_action(db, key, audit_service.ERASE, target_id=memory_id, end_user_id=end_user,
                     project_id=project, meta={"erase": True} if erase else None)
    return {"deleted": True, "id": memory_id, "erasure": "permanent" if erase else None}


# Above `/facts` and anything under it with an id, for the same reason as
# `/memories/categories`.
@router.get("/facts/families")
def fact_families(key=Depends(_admin)):
    """The predicate families the engine sorts a fact into, in its display
    order, for the screen's filters and legend. Read from the engine, never
    changed here.
    \f
    Served rather than copied in the dashboard: its copy had nine of the
    engine's ten and no `identity`, so renamed_to, formerly and
    also_known_as could not be filtered (audit of 17 Oct 2026)."""
    from ..services.memory_core.families import FAMILY_LABELS_EN, FAMILY_ORDER

    return {"families": [{"name": f, "label": FAMILY_LABELS_EN.get(f, f)} for f in FAMILY_ORDER]}


@router.get("/facts", responses=admin_errors(404))
def list_facts(user_id: Optional[str] = None, entity: Optional[str] = None,
               predicate: Optional[str] = None,
               predicate_family: Optional[str] = Query(default=None, description=FAMILY_FILTER),
               include_invalidated: bool = False, as_of: Optional[str] = None,
               limit: int = 100,
               offset: int = Query(default=0, ge=0, le=limits.MAX_OFFSET),
               known_as_of: Optional[str] = None,
               project_id: Optional[str] = None,
               key=Depends(_admin), db: Session = Depends(get_db)):
    """The facts, as GET /v1/facts reads them; `known_as_of` as the store
    knew them then (the graph's "what the agent knew" view). `project_id`:
    one project's, as its keys read them; without it, the whole account.
    404 for a project that is not this account's."""
    from ..api.facts import names_problem, parse_as_of
    from ..services.facts import service as facts_service
    from ..services.sentinels import EVERY_END_USER as _ALL

    # The names by GET /v1/facts' own check: a NUL byte reached the driver
    # and answered 500 (audit of 19 Oct 2026).
    problem = names_problem(user_id=user_id, entity=entity, predicate=predicate,
                            predicate_family=predicate_family)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)
    known = parse_as_of(known_as_of, "known_as_of")
    as_of_at = parse_as_of(as_of)
    within = _within_project(db, key, project_id)
    _count_a_keys_read(db, key)
    rows, total = facts_service.retrieve_agent_facts(
        db, key.tenant_id,
        end_user_id=user_id if user_id is not None else _ALL,
        entity=entity, predicate=predicate, predicate_family=predicate_family,
        include_invalidated=include_invalidated, as_of=as_of_at,
        known_as_of=known, limit=limit, offset=offset, project_id=within)
    log_v1_request(db, key.tenant_id, "admin:facts:list", principal=key, end_user_id=user_id,
                   project_id=within, facts_read=[f.id for f in rows])
    return {"facts": [facts_service.serialize_fact(f, known) for f in rows], "total": total}


@router.get("/namespaces")
def list_namespaces(limit: int = Query(default=limits.MAX_PAGE, ge=1, le=limits.MAX_PAGE),
                    offset: int = Query(default=0, ge=0, le=limits.MAX_OFFSET),
                    key=Depends(_admin), db: Session = Depends(get_db)):
    """The agent ids this account has written under, the most memories
    first, a page at a time (`total` is how many). Not a table: a namespace
    is a string the caller chose, so this is what the store has seen.

    Paged by the database: every agent of the account came back in one
    answer, grouped, merged and sorted here."""
    from sqlalchemy import false, or_, select
    from sqlalchemy import func as sqlfunc

    from ..db import AgentFact, AgentMemory

    # Counted as its /v1 twin, GET /v1/agents, is: the same numbers read by
    # an admin-scope key through another door (audit of 19 Oct 2026).
    _count_a_keys_read(db, key)
    # Four numbers per agent, because that is what the screen shows. Returning
    # two of them gave it `undefined.toLocaleString()` and a blank page.
    n = sqlfunc.count(AgentMemory.id)
    groups = (db.query(AgentMemory.agent_id, n,
                       sqlfunc.count(sqlfunc.distinct(AgentMemory.end_user_id)))
                .filter(AgentMemory.tenant_id == key.tenant_id,
                        AgentMemory.deleted_at.is_(None))
                .group_by(AgentMemory.agent_id))
    mem = (groups.order_by(n.desc(), AgentMemory.agent_id.collate("C").asc().nulls_first())
                 .limit(limit).offset(offset).all())
    total = db.execute(select(sqlfunc.count()).select_from(groups.subquery())).scalar() or 0
    # The facts of the agents on the page only.
    named = [a for a, _n, _u in mem if a is not None]
    facts = dict(db.query(AgentFact.agent_id, sqlfunc.count(AgentFact.id))
                   .filter(AgentFact.tenant_id == key.tenant_id, true_at(),
                           or_(AgentFact.agent_id.in_(named) if named else false(),
                               AgentFact.agent_id.is_(None) if len(named) < len(mem) else false()))
                   .group_by(AgentFact.agent_id)
                   .all()) if mem else {}
    # The memories written without an agent_id are a row too, `agent_id:
    # null`, the screen's "Default namespace". They were dropped here, so the
    # page that shows who wrote what never showed them (audit of 17 Oct
    # 2026). An agent_id is never "" (memory_write.checked_ids).
    agents = [{"agent_id": a, "memories": int(n), "end_users": int(u),
               "facts": int(facts.get(a, 0))}
              for a, n, u in mem]
    return {"agents": agents, "total": int(total)}


@router.get("/requests")
def list_requests(limit: int = 100, key=Depends(_admin), db: Session = Depends(get_db)):
    """The recent calls, off the audit trail. One source, so this cannot
    disagree with the summary next to it: the calls on the memory, without
    the administration, which is the Audit log's (usage_report)."""
    from ..db import AuditEvent

    rows = (db.query(AuditEvent)
              .filter(AuditEvent.tenant_id == key.tenant_id,
                      AuditEvent.action.in_(usage_report.REQUEST_ACTIONS))
              .order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
              .limit(max(1, min(int(limit or 100), limits.MAX_AUDIT_PAGE)))
              .all())
    return {
        "requests": [
            # `at`, not `ts`: the screen reads `at`. The rest is extra and
            # harmless, but that one field being called something else is a
            # blank page.
            {"at": r.ts, "endpoint": (r.meta or {}).get("endpoint") or r.action,
             "kind": usage_report.kind_of(r.action),
             "actor": r.actor, "action": r.action, "result": r.result,
             "user_id": r.end_user_id, "target_id": r.target_id}
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/audit")
def list_audit(user_id: Optional[str] = None, action: Optional[str] = None,
               since: Optional[str] = Query(default=None, max_length=64, description=MOMENT),
               until: Optional[str] = Query(default=None, max_length=64, description=MOMENT),
               limit: int = 100,
               offset: int = Query(default=0, ge=0, le=limits.MAX_OFFSET),
               key=Depends(_admin), db: Session = Depends(get_db)):
    """The trail for the screen's Audit log: the events of the whole
    account, as the operator reads everything here, with the filters of
    GET /v1/audit and what each read returned, by id. Newest
    first. The ids are turned into text only on demand (`/audit/read`), so
    this answer, like the table, holds no content. `since` and `until` are
    read as GET /v1/audit reads them."""
    from ..api.facts import parse_as_of
    from ..db import AuditEvent

    since_at, until_at = parse_as_of(since, "since"), parse_as_of(until, "until")
    q = db.query(AuditEvent).filter(AuditEvent.tenant_id == key.tenant_id)
    if user_id:
        # With the pseudonym an erasure left (services/pseudonyms.py).
        from ..services.pseudonyms import as_asked

        q = q.filter(AuditEvent.end_user_id.in_(as_asked(db, key.tenant_id, user_id)))
    if action:
        q = q.filter(AuditEvent.action == action)
    if since_at:
        q = q.filter(AuditEvent.ts >= since_at)
    if until_at:
        q = q.filter(AuditEvent.ts <= until_at)
    total = q.count()
    # Ties broken by id, as /v1/audit does: one transaction's events share
    # their timestamp, and an offset over ties repeats and skips.
    rows = (q.order_by(AuditEvent.ts.desc(), AuditEvent.id.desc())
             .offset(max(0, int(offset or 0)))
             .limit(max(1, min(int(limit or 100), limits.MAX_AUDIT_PAGE)))
             .all())
    return {
        "events": [
            {"ts": r.ts, "actor": r.actor, "action": r.action, "result": r.result,
             "endpoint": (r.meta or {}).get("endpoint") or r.action,
             "user_id": r.end_user_id, "target_id": r.target_id, "meta": r.meta,
             "ip": r.ip, "project_id": str(r.project_id) if r.project_id else None,
             "read": r.read_ids or None}
            for r in rows
        ],
        "total": total,
        # The verbs the filter offers, the trail's own (services/audit.py).
        "actions": sorted(audit_service.ACTIONS),
    }


@router.get("/audit/read")
def audit_read(memories: str = "", facts: str = "",
               key=Depends(_admin), db: Session = Depends(get_db)):
    """What the ids of one audit event say NOW, for the screen to show next to
    the event: a memory's text, a fact's line and its validity. Read from the
    live tables, never from the trail, which holds no content: a memory that
    was erased since comes back as null, which is the erasure working. At most
    100 of each, the account's own only."""
    from ..db import AgentFact, AgentMemory
    from ..services.ids import fact_public_id, mem_public_id, parse_fact_id, parse_mem_id

    def ids(raw: str, parse) -> list:
        out = []
        for s in (raw or "").split(","):
            u = parse(s.strip())
            if u is not None and u not in out:
                out.append(u)
        return out[:100]

    mem_ids, fact_ids = ids(memories, parse_mem_id), ids(facts, parse_fact_id)
    _count_a_keys_read(db, key)
    found_m = {m.id: m for m in db.query(AgentMemory).filter(
        AgentMemory.tenant_id == key.tenant_id, AgentMemory.id.in_(mem_ids)).all()} if mem_ids else {}
    found_f = {f.id: f for f in db.query(AgentFact).filter(
        AgentFact.tenant_id == key.tenant_id, AgentFact.id.in_(fact_ids)).all()} if fact_ids else {}
    # This read shows the text the trail itself never holds: it is a read
    # like any other, of what it found.
    log_v1_request(db, key.tenant_id, "admin:audit:read", principal=key,
                   memories_read=[u for u in mem_ids if u in found_m] if mem_ids else None,
                   facts_read=[u for u in fact_ids if u in found_f] if fact_ids else None)
    return {
        "memories": {
            # A forgotten memory's text is not shown again: forgetting drops it
            # out of every read, this one included (audit of 17 Oct 2026).
            mem_public_id(u): ({"text": None if m.deleted_at is not None else (m.content or "")[:280],
                                "user_id": m.end_user_id,
                                "deleted": m.deleted_at is not None} if (m := found_m.get(u)) else None)
            for u in mem_ids
        },
        "facts": {
            fact_public_id(u): ({"line": f"{f.subject_label} {f.predicate_canonical} {f.object_label}",
                                 "user_id": f.end_user_id, "valid_from": f.valid_from,
                                 "invalid_at": f.invalid_at} if (f := found_f.get(u)) else None)
            for u in fact_ids
        },
    }


@router.get("/keys/lifetimes")
def key_lifetimes(key=Depends(_admin)):
    """What the screen offers when somebody creates a key.

    Served rather than hardcoded in the page so the list and the default can
    move without a rebuild of the dashboard, and so a script can read the same
    thing the screen shows.
    """
    return {
        "default_days": key_service.DEFAULT_LIFETIME_DAYS,
        "choices": [
            {"days": d, "label": f"{d} days" if d else "Never expires"}
            for d in key_service.LIFETIME_CHOICES
        ],
        "warning_days": key_service.WARN_DAYS,
        "max_days": key_service.MAX_LIFETIME_DAYS,
    }


# ── the model provider key, from the screen ─────────────────────────────────

class ProviderKey(Body):
    key: str = Field(min_length=8, max_length=512)


class ProviderKeyCheck(Body):
    """A key to try, or none for the one already saved or in force."""
    key: Optional[str] = Field(default=None, max_length=512)


def _install_wide(key=Depends(_admin), db: Session = Depends(get_db)):
    """The provider and the models are the install's, one for every account
    in it. On an install of one's own its operator sets them. On the hosted
    service (KORELY_MODE=hosted) they are the service's, set in its
    configuration, and an account neither reads nor changes them: one
    account's admin key would otherwise switch the model, and the key, of
    every other (8 Oct 2026, the first safety of the one codebase). Closed by
    the mode, so it holds with no hosted hook registered; the hook may only
    let in a principal the service trusts."""
    if get_settings().hosted and extensions.hooks.can_change_install_settings(db, key) is not True:
        raise CodedHTTPException(
            status.HTTP_403_FORBIDDEN,
            "The model and its key are set by the service, for every account.",
            code="set_by_the_service")
    # An install of one's own with more than one account: only the person the
    # command line named (`Operator.install_admin`), never an admin key, never
    # another account's administrator (audit of 17 Oct 2026: any of them could
    # read, remove or replace the provider and its key for every account).
    if not get_settings().hosted and _accounts_on_the_install(db) > 1 and not (
            isinstance(key, Operator) and key.install_admin):
        raise CodedHTTPException(
            status.HTTP_403_FORBIDDEN,
            "This install holds more than one account, and the model and its key are every "
            "account's: only the person named with `manage grant-install-admin` sets them, "
            "signed in to the dashboard.",
            code="install_admin_only")
    return key


def _accounts_on_the_install(db: Session) -> int:
    from ..db import Tenant

    return db.query(Tenant.id).limit(2).count()


def _audit_install(db: Session, key, action: str, **kw) -> None:
    """A change to what is the whole install's, in the trail of every account
    on it: each is affected, and each one's trail is the place it looks."""
    from ..db import Tenant

    log_admin_action(db, key, action, **kw)
    meta = {**(kw.get("meta") or {}), "by_account": str(key.tenant_id),
            **({"operator": key.username} if isinstance(key, Operator) else {})}
    for (other,) in db.query(Tenant.id).filter(Tenant.id != key.tenant_id).all():
        audit_service.record(db, other, action, actor="install", target_id=kw.get("target_id"),
                             meta=meta)


@router.get("/provider", summary="Provider State")
def provider_state_(key=Depends(_install_wide), db: Session = Depends(get_db)):
    """Whether a key is set, where it came from, and what it last did.

    Never the key itself. The screen gets the last four characters, the same
    convention as the API keys, for the same reason: enough to recognise,
    useless to anybody else.
    """
    from ..core.llm import embedding_state, provider_state

    s = get_settings()
    out = provider_key.describe(db)
    out["provider"] = s.extraction_provider
    # What the provider last actually did, rather than whether a key is set.
    # The two are different questions and the second one is the one that told
    # us everything was fine for hours while the credit was gone. The vectors
    # apart, since 2026-10-06: they answered for the extraction.
    out["last_call"] = provider_state()
    out["last_vector"] = embedding_state()
    return out


@router.put("/provider",
            responses=admin_errors(400, 503))
def set_provider_key(body: ProviderKey, key=Depends(_install_wide),
                     db: Session = Depends(get_db)):
    """Save it, after checking that it works.

    Checked first, deliberately. A key you paste and cannot test is the same
    silent failure as everything else here: the page says saved, nothing
    extracts, and the first write fails hours later with nobody watching.
    """
    checked = provider_key.check(body.key)
    if not checked["ok"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"The provider refused this key, so it was not saved. {checked['detail']}",
        )
    try:
        provider_key.set_google_key(
            db, body.key, by=getattr(key, "username", None) or "api key")
    except provider_key.NoEncryptionKey as e:
        # 503 rather than 400: nothing is wrong with what they typed.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    _audit_install(db, key, audit_service.SETTING_CHANGE, target_id="provider",
           meta={"setting": "provider_key", "op": "set"})
    return provider_key.describe(db)


@router.post("/provider/test",
             responses=admin_errors(404))
def test_provider_key(body: Optional[ProviderKeyCheck] = None, key=Depends(_install_wide),
                      db: Session = Depends(get_db)):
    """Try a key without saving it, or try the one already in force.

    No body, `{}`, a null or an empty `key` all mean the key in force, as
    POST /admin/models/{provider}/check reads them: the screen's "Test only"
    with an empty box sent `{}`, and the saving body's required `key`
    answered it 422 "key: Field required"."""
    from ..core.llm import current_google_key

    candidate = ((body.key or "").strip() if body else "") or current_google_key()
    if not candidate:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "there is no key to test")
    return provider_key.check(candidate)


# Forgetting what writes, on the Cloud (5 Nov 2026): "Use what .env says
# instead" forgot the writer of every region, the Cloud's `.env` sets none, and
# the projects in Global extracted nothing for six minutes with nothing said.
# Both clears that can take a region's writer away ask first, as DELETE
# /v1/account does: 400 `confirmation_required` naming the regions, as a
# sentence and as `regions`, until `?confirm=true`.
_LEAVES_A_REGION = admin_errors(400, descriptions={
    400: "`confirmation_required` (the Cloud only): the clear would leave a region with no "
         "writer; `regions` names each, and `?confirm=true` clears anyway."})


def _confirm():
    return Query(False, description="The Cloud only: true clears even when a region is left "
                                    "with no writer.")


def _ask_before_a_region_loses_its_writer(db: Session, forgetting: str, confirm: bool,
                                          what: str) -> List[str]:
    """On the Cloud: the regions the clear of `forgetting` leaves with no
    writer (model_choice.left_without_writer), read and cleared under one
    hold of the install's settings, so what was named is what goes; 400
    `confirmation_required` unless `confirm`. [] on an install of one's
    own, which its operator configures in `.env`."""
    if not get_settings().hosted:
        return []
    provider_key.hold_install_settings(db)
    lost = model_choice.left_without_writer(db, forgetting)
    if lost and not confirm:
        db.rollback()
        whose = " and ".join(f"Projects in {LABELS[r]}" for r in lost)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail={
            "code": "confirmation_required",
            "message": f"{whose} would be left with no provider: their memories would wait, with "
                       "no facts extracted, until a provider is saved again. On the Cloud .env "
                       f"sets no writer. Nothing changed: add ?confirm=true to {what} anyway.",
            "regions": lost})
    return lost


@router.delete("/provider", response_model=Deleted, responses=_LEAVES_A_REGION)
def clear_provider_key(confirm: bool = _confirm(), key=Depends(_install_wide),
                       db: Session = Depends(get_db)):
    """Forget the key set from the screen. Whatever is in `.env` takes over
    again, which is the state the install shipped in. On the Cloud, when
    that key writes a region (Gemini with no writer chosen for Global),
    400 `confirmation_required` until `?confirm=true`."""
    lost = _ask_before_a_region_loses_its_writer(db, model_choice.GOOGLE_KEY_SAVED, confirm,
                                                 "forget the key")
    gone = provider_key.clear_google_key(db)
    _audit_install(db, key, audit_service.SETTING_CHANGE, target_id="provider",
           meta={"setting": "provider_key", "op": "clear",
                 **({"left_without_writer": ",".join(lost)} if lost else {})})
    return {"deleted": gone, "id": "provider"}


# ── where the memories are written, chosen at first login ──────────────────

class ModelChoiceBody(Body):
    # Empty keeps the key already saved for this provider (checked again).
    key: Optional[str] = Field(default=None, max_length=512)
    model: str = Field(min_length=1, max_length=200)


def _known_provider(provider: str) -> str:
    if provider not in model_choice.PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no provider {provider!r}: " + ", ".join(model_choice.PROVIDERS))
    return provider


@router.get("/models")
def models_state(key=Depends(_install_wide), db: Session = Depends(get_db)):
    """Whether a writer is configured (here or in `.env`), which one, and the
    providers the first-login screen offers. Never a key: its last four."""
    return model_choice.describe(db)


@router.post("/models/{provider}/check",
             responses=admin_errors(404))
def check_models_key(provider: str, body: Optional[ProviderKeyCheck] = None,
                     key=Depends(_install_wide), db: Session = Depends(get_db)):
    """Try a key without saving it, or the one saved for this provider. The
    answer carries the provider's model list, OpenRouter's most used this
    week first, and the writer we suggest from it."""
    _known_provider(provider)
    candidate = (body.key if body else None) or model_choice.saved_key(db, provider)
    if not candidate:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "there is no key to check")
    return model_choice.check(provider, candidate)


@router.put("/models/{provider}",
            responses=admin_errors(400, 404, 503))
def save_models(provider: str, body: ModelChoiceBody, key=Depends(_install_wide),
                db: Session = Depends(get_db)):
    """Check the key with the provider, store the choice, put it in force.

    Checked first, as the provider key is: a choice that looks saved and
    cannot write is found by the next write, hours later, with nobody
    watching."""
    _known_provider(provider)
    extensions.hooks.before_writer_saved(db, provider)
    try:
        out = model_choice.save(db, provider, key=body.key, model=body.model,
                                by=getattr(key, "username", None) or "api key")
    except model_choice.Refused as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except provider_key.NoEncryptionKey as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
    _audit_install(db, key, audit_service.SETTING_CHANGE, target_id=provider,
           meta={"setting": "writer", "op": "set", "model": body.model})
    return out


@router.delete("/models", response_model=Deleted, responses=_LEAVES_A_REGION)
def clear_models(confirm: bool = _confirm(), key=Depends(_install_wide),
                 db: Session = Depends(get_db)):
    """Forget the screen's choice: whatever `.env` says writes again. On
    the Cloud, whose `.env` sets no writer, a clear that would leave a
    region with no writer answers 400 `confirmation_required` until
    `?confirm=true` (5 Nov 2026). An install of one's own is unchanged."""
    lost = _ask_before_a_region_loses_its_writer(db, model_choice.WRITERS, confirm,
                                                 "forget the writers")
    gone = model_choice.clear(db)
    _audit_install(db, key, audit_service.SETTING_CHANGE, target_id="models",
           meta={"setting": "writer", "op": "clear",
                 **({"left_without_writer": ",".join(lost)} if lost else {})})
    return {"deleted": gone, "id": "models"}
