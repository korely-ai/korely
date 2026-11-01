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

    if isinstance(principal, Operator):
        return {"actor": "dashboard", "api_key_id": None,
                "meta": {"operator": principal.username}}
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
) -> None:
    """Record one API call in the audit trail.

    A read passes what it returned (`memories_read`, `facts_read`: ids), the
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

    action = _ACTION_FOR.get(endpoint, audit.READ)
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
    audit.record(
        db, tenant_id, action,
        actor=actor, result=result, api_key_id=api_key_id,
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
        memories_read=memories_read, facts_read=facts_read,
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
    "memories:delete": "erase",
    "users:delete": "erase",
    "agents:delete": "erase",
    # Closing a fact is an erasure in the sense that matters to a person asking
    # what was done with their data: it stops being part of what the system
    # says about them. The row survives in history, which is why it is not a
    # delete, but answering an access request with "read" here would be
    # answering it wrongly.
    "facts:forget": "erase",
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


def has_agent_id(db, tenant_id, agent_id: str, project_id=None) -> bool:
    """Whether this tenant has written under `agent_id` behind the project
    wall, in either store: one indexed probe each, where the check of
    DELETE /v1/agents/{agent_id} read every agent id of the project to find
    one (audit of 20 Oct 2026)."""
    from ..db import AgentFact, AgentMemory

    for model in (AgentMemory, AgentFact):
        q = within_project(db.query(model.id).filter(model.tenant_id == tenant_id,
                                                     model.agent_id == agent_id),
                           model.project_id, project_id)
        if q.first() is not None:
            return True
    return False


def distinct_agent_ids(db, tenant_id, project_id=None, *, every_project=False) -> set:
    """Every agent_id namespace this tenant has written under.

    Both stores are scanned: an agent that only ever wrote facts through
    /v1/facts would be invisible if we looked at memories alone, and
    `GET /v1/agents` would omit it.

    In the hosted product this also fed a cap on how many agents an account
    could register. There is no cap here; the function survives because listing
    and deleting an agent namespace are real operations.
    """
    from ..db import AgentFact, AgentMemory

    out: set = set()
    for model in (AgentMemory, AgentFact):
        q = db.query(model.agent_id).filter(
            model.tenant_id == tenant_id, model.agent_id.isnot(None))
        # `every_project=True` is the deliberate account-wide read, for a caller
        # that genuinely wants the whole account. Anything reached by an API key
        # passes a project, because a key is confined to one.
        q = within_project(q, model.project_id, EVERY_PROJECT if every_project else project_id)
        rows = q.distinct().all()
        out |= {r[0] for r in rows if r[0]}
    return out
