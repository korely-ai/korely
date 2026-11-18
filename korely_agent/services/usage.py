"""Per-request usage metering for the Agents programmatic API.

One ApiUsageLog row per /v1/* call (request_count, zero direct cost — reads are
retrieval). Feeds the usage dashboard and the sizing of the rate limits.
Best-effort: telemetry must never break the request path.

The dashboard's reads of end users' memories and facts (/admin/memories,
/admin/facts, /admin/audit/read) are recorded here too, with actor
`dashboard` and the operator's name (`principal=`): the trail answers who
read a person's data, and a person at the screen is somebody (audit of 17
Oct 2026; until then only the screen's writes were there).

The one way into the trail from a request (audit of 17 Oct 2026): a call on
the memory, by its endpoint label, through `log_v1_request`; an act of
administration, by its verb, through `log_admin_action`. Both tell who
acted from the caller the same way (`_by`). Before, /admin had its own two
wrappers, and the OAuth consent and the Cloud's sign-in imported the
private one from api/admin. One act has one label on every door: the MCP
writes the label of its REST twin (tests/test_audit.py checks it).
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from .sentinels import EVERY_PROJECT, within_project

logger = logging.getLogger(__name__)


def _by(principal) -> dict:
    """Who acted, from the caller a route resolved: an operator signed in to
    the dashboard is the `dashboard`, with their name and no key; a key is
    `rest`, with its id."""
    from ..db import Operator
    from .audit import OPERATOR_FIELD

    if isinstance(principal, Operator):
        return {"actor": "dashboard", "api_key_id": None,
                "meta": {OPERATOR_FIELD: principal.username}}
    return {"actor": "rest", "api_key_id": principal.id, "meta": {}}


def log_admin_action(db: Session, principal, action: str, *, target_id=None,
                     end_user_id=None, meta=None, project_id=None,
                     result: str = "ok") -> None:
    """One act of administration in the trail (2026-10-06), by its verb
    (services/audit.py: KEY_CREATE, SETTING_CHANGE, LOGIN...), made by an
    operator or an admin key. Filed under the project the act touched, or
    with no project, which every key without one reads."""
    from . import audit

    who = _by(principal)
    audit.record(db, principal.tenant_id, action,
                 actor=who["actor"], api_key_id=who["api_key_id"],
                 end_user_id=end_user_id, target_id=target_id,
                 meta={**(meta or {}), **who["meta"]} or None,
                 project_id=project_id, result=result)


def log_v1_request(
    db: Session,
    tenant_id: UUID,
    endpoint: str,
    *,
    principal=None,
    actor: str = "rest",
    api_key_id=None,
    end_user_id=None,
    target_id=None,
    meta=None,
    result: str = "ok",
    ip=None,
    project_id=None,
    memories_read=None,
    facts_read=None,
    also_read=None,
    also_counted=None,
) -> None:
    """Record one API call in the audit trail.

    A read passes what it returned (`memories_read`, `facts_read`: ids;
    `also_read`: a part of the product's, services/audit.record), the
    end user it was about and the memory it touched, so that the trail can say
    what the agent knew when it decided (services/audit.py).

    The name is unchanged from the version this replaces, which is why there are
    twenty-two call sites: that version imported a table belonging to the other
    product, failed on every call, and swallowed the error as best-effort
    telemetry. Twenty-two places that looked like record-keeping and wrote
    nothing.

    `principal`: the operator or admin key of a read made from the screen
    (/admin), told as `log_admin_action` tells it; it sets the actor, the key
    and the operator's name.

    Endpoint labels map to audit verbs. Anything unmapped is recorded as a read,
    because the alternative is losing it: an over-broad `read` is a report that
    is noisy, a dropped event is a report that is wrong.
    """
    from . import audit

    audit.record(db, tenant_id, **_v1_event(
        endpoint, principal=principal, actor=actor, api_key_id=api_key_id,
        end_user_id=end_user_id, target_id=target_id, meta=meta, result=result, ip=ip,
        project_id=project_id, memories_read=memories_read, facts_read=facts_read,
        also_read=also_read, also_counted=also_counted))


def log_v1_call(db: Session, tenant_id: UUID, endpoint: str, rows: list, **common) -> None:
    """One call filed as several rows: `rows`, the arguments of
    `log_v1_request` that differ from row to row (the end user, the project,
    what was read of theirs), `common` the others. Written in one
    transaction (audit.record_all), and the rows of one call share
    `meta.call`, an id of the call, by which the Requests page counts it
    once (usage_report.is_a_request): an admin key's list of 50 end users
    counted 50 reads (review of 14 Nov 2026). One row is written as
    `log_v1_request` writes it."""
    from uuid import uuid4

    from . import audit

    if len(rows) <= 1:
        log_v1_request(db, tenant_id, endpoint, **common, **(rows[0] if rows else {}))
        return
    call = {"call": uuid4().hex[:16]}
    audit.record_all(db, tenant_id, [
        _v1_event(endpoint, **{**common, **row,
                               "meta": {**(common.get("meta") or {}), **(row.get("meta") or {}),
                                        **call}})
        for row in rows])


def _v1_event(endpoint: str, *, principal=None, actor: str = "rest", api_key_id=None,
              end_user_id=None, target_id=None, meta=None, result: str = "ok", ip=None,
              project_id=None, memories_read=None, facts_read=None, also_read=None,
              also_counted=None) -> dict:
    """`audit.record`'s arguments for one call of `endpoint`, as
    `log_v1_request` describes them."""
    from . import audit

    action = _action_of(endpoint, meta)
    if principal is not None:
        who = _by(principal)
        actor, api_key_id = who["actor"], who["api_key_id"]
        meta = {**(meta or {}), **who["meta"]}
        # A read of the whole account carries no project of its own.
        project_id = project_id if isinstance(project_id, UUID) else None
    # An end user already erased is written as their pseudonym: checking the
    # erasure as the README says (GET /v1/facts?user_id=<the id>) wrote the id
    # back in clear next to it (audit of 17 Oct 2026). audit.record decides
    # it, for the row's project, for every writer of the trail (audit of 20 Oct
    # 2026).
    return dict(
        action=action, actor=actor, result=result, api_key_id=api_key_id,
        end_user_id=end_user_id, target_id=target_id,
        meta={**(meta or {}), "endpoint": endpoint}, ip=ip,
        # The project the call was made in, which GET /v1/audit reads by. A
        # REST call names its key and the project is the key's; the MCP has
        # no key row at hand and passes the project it resolved. A call of
        # the dashboard's (`principal`, an operator or an admin key) is filed
        # where the data it read lives, or under no project for the whole
        # account, whoever made it: an admin key's read of another project
        # was filed under the key's own, where that project's read keys
        # listed its end users and an erasure there left them in clear
        # (audit of 20 Oct 2026).
        project_id=(project_id if principal is not None or api_key_id is None
                    else audit.FROM_THE_KEY),
        memories_read=memories_read, facts_read=facts_read, also_read=also_read,
        also_counted=also_counted,
    )


# Endpoint label -> audit verb. Keyed on the whole label, not the namespace:
# "memories:get" and "memories:add" are a read and a write, and collapsing them
# gives a report that says the account wrote a thousand times when it read a
# thousand times.
#
# Anything absent falls through to `read`, which is why only the non-reads are
# listed: a new read endpoint is correct by default, and a new write endpoint
# that someone forgets to add here shows up as suspiciously busy reading, which
# is a mistake you notice.
# The labels only the Cloud's package writes (korely_agent/hosted/), apart
# so a tree without it can tell them from a label nobody writes any more
# (tests/test_audit.py).
CLOUD_ACTIONS = {
    # The Cloud's sign-up without a login: an account made, as a tenant is.
    "agents:init": "tenant_create",
}

_ACTION_FOR = {
    **CLOUD_ACTIONS,
    "memories:add": "write",
    "memories:update": "write",
    "batch:create": "write",
    "facts:create": "fact_write",
    "users:delete": "erase",
    "agents:delete": "erase",
    # Closing a fact: it stops being part of what the system says about the
    # person, and its row stays in history, so it is neither a read (which
    # would answer an access request wrongly) nor an erasure (which would say
    # the data is gone). `fact_invalidate`, the verb the trail lists for it;
    # it was filed as `erase` until 1.0.23 (review of 1 Nov 2026).
    "facts:forget": "fact_invalidate",
    # A correction writes a new fact and closes the old one. Recorded as a
    # write because what it produces is a new assertion; the closure that comes
    # with it is visible in the fact's own history.
    "facts:correct": "fact_write",
    # A declared name makes the end user's facts about it read as theirs: the
    # facts already written are recanonicalized, as a correction changes them.
    "names:declare": "fact_write",
    # The dashboard's alias doors, as the names door: declaring or removing a
    # name recanonicalizes the facts that name it. Their trail row is a
    # refusal of the Cloud's quota only (memory_write.before_write); the
    # change itself is in the operator's actions (log_admin_action).
    "admin:aliases:create": "fact_write",
    "admin:aliases:delete": "fact_write",
}


#: The doors that forget by default and erase when asked (`erase=true`): one
#: label for both acts, and the act read from the record every caller of
#: the door writes for an erasure, `meta.erase` (api/memories.delete_memory,
#: the MCP's korely_forget_memory, a refusal through api/errors.refused).
#: A forget keeps the memory's text, so filing it as `erase` said the data
#: was gone when it was not (review of 1 Nov 2026).
FORGETS_OR_ERASES = {"memories:delete"}


def _action_of(endpoint: str, meta=None) -> str:
    """The trail's category of `endpoint`: `forget` or `erase` for a door
    that does either (`FORGETS_OR_ERASES`, by `meta.erase`), the core's
    (`_ACTION_FOR`), a part of the product's own
    (core/extensions.Part.actions), or a read."""
    from . import audit

    if endpoint in FORGETS_OR_ERASES:
        return audit.ERASE if (meta or {}).get("erase") is True else audit.FORGET
    if endpoint in _ACTION_FOR:
        return _ACTION_FOR[endpoint]
    from ..core import extensions

    for part in extensions.parts():
        if endpoint in part.actions():
            return part.actions()[endpoint]
    return audit.READ


def _part_stores(db, parts=None) -> tuple:
    """The models of a part of the product the Cloud's package carries that
    hold what an agent namespace wrote (core/extensions.Part.stores), but
    those whose table the database does not have (db/chains.absent_tables:
    no row there). `parts`: of those parts only; every one by default."""
    from ..core import extensions
    from ..db.chains import absent_tables

    gone = absent_tables(db)
    every = extensions.parts() if parts is None else parts
    return tuple(model for part in every for _name, model in part.stores()
                 if model.__tablename__ not in gone)


def _stores(db) -> tuple:
    """The models that hold what an agent namespace wrote: the memories,
    the facts, and a part of the product's (`_part_stores`)."""
    from ..db import AgentFact, AgentMemory

    return (AgentMemory, AgentFact, *_part_stores(db))


def has_agent_id(db, tenant_id, agent_id: str, project_id=None) -> bool:
    """Whether this tenant has written under `agent_id` behind the project
    wall, in any of its stores (`_stores`: the memories, the facts, a part of
    the product's), or read under it (the read marks of agent notices: a
    reader is an agent's name, and its marks are that namespace's data,
    services/end_user_places.py): one indexed probe each, where the check of
    DELETE /v1/agents/{agent_id} read every agent id of the project to find
    one (audit of 20 Oct 2026). An agent that only ever read under its name
    was a 404 there, and its marks could be reached by no deletion but the
    project's (review of piece 3 of agent notices, 9 Nov 2026)."""
    from ..db import AgentReadMark

    for model, column in (*((model, model.agent_id) for model in _stores(db)),
                          (AgentReadMark, AgentReadMark.reader)):
        q = within_project(db.query(model.id).filter(model.tenant_id == tenant_id,
                                                     column == agent_id),
                           model.project_id, project_id)
        if q.first() is not None:
            return True
    return False


def distinct_agent_ids(db, tenant_id, project_id=None, *, every_project=False,
                       by_project=False, parts=None) -> set:
    """Every agent_id namespace this tenant has written under; with
    `by_project`, each as (its project, the name), since an agent is a name
    in a project (the Cloud's cap counts them so, hosted/quotas.py). `parts`:
    the parts of the product whose stores are read beside the memories and
    the facts (core/extensions.Part.stores); every one by default, the
    parts that serve the account for a count against its limits.

    Every store is scanned: an agent that only ever wrote facts through
    /v1/facts would be invisible if we looked at memories alone, and
    `GET /v1/agents` would omit it. The models are named in the loop, where
    the guard of whole-table reads sees them
    (tests/test_no_read_loads_a_whole_table.py).

    In the hosted product this also fed a cap on how many agents an account
    could register. There is no cap here; the function survives because listing
    and deleting an agent namespace are real operations.
    """
    from ..db import AgentFact, AgentMemory

    out: set = set()
    for model in (AgentMemory, AgentFact, *_part_stores(db, parts)):
        columns = (model.project_id, model.agent_id) if by_project else (model.agent_id,)
        q = db.query(*columns).filter(
            model.tenant_id == tenant_id, model.agent_id.isnot(None))
        # `every_project=True` is the deliberate account-wide read, for a caller
        # that genuinely wants the whole account. Anything reached by an API key
        # passes a project, because a key is confined to one.
        q = within_project(q, model.project_id, EVERY_PROJECT if every_project else project_id)
        rows = q.distinct().all()
        out |= {tuple(r) if by_project else r[0] for r in rows if r[-1]}
    return out
