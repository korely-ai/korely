"""what an erasure leaves is marked, and the September erasures find their project

Revision ID: f6a3d9c1b8e2
Revises: d8b2f4a6c1e7
Create Date: 2026-10-27 12:00:00

The audit of 27 Oct 2026, three steps.

1. `agent_webhook_deliveries.erased_user`: the erasure marks a delivery
   still to be sent that names the end user it erased, and the delivery
   takes the pseudonym (or loses the id without SECRET_KEY) once it is
   finished (services/end_user_places.rename_in_deliveries: a lookup by
   time at the finish missed one enqueued while the erasure was open, and
   every one without a key). Existing rows: false.

2. The deliveries about a project deleted without erasing it say the
   Default project, as the rows they are about do since the deletion moved
   them (services/projects._move_deliveries: an erasure in the
   Default project afterwards matched them by the deleted project and left
   the id in clear). Only a project gone with no erasure record of it (scope
   'project'): an erased project's deliveries stay about it.

3. The end-user and agent erasures of 0.1.1-0.1.11, which wrote their
   record with no project (ef44feb filled it from 0.1.12), take the project
   they were made in from the erasure request's own trail row (endpoint
   users:delete or agents:delete, with a project, in the minute after the
   record), as d8b4e1a7c3f5 did for the keyed end-user records it could
   match:
   - an end user's record that a5d81f3c7e64 left with no id (no SECRET_KEY
     then): one id and one project in that minute is the person, and their
     trail rows in that project up to the erasure, their earlier records
     there and the deliveries delivered about it lose the id, as an erasure
     without a key does, or take the pseudonym when a key is set now;
   - an end user's record with a pseudonym that d8b4e1a7c3f5 did not
     match, through the trail rows it already renamed, or under a key set
     since;
   - an agent namespace's record, through the row naming its project.
   A record whose minute holds requests of the person in more than one
   project (or, without a key, of more than one person), and an agent's
   record not found (no door erases an agent namespace in the whole
   account), is marked "project not recorded" (the nil UUID in
   `erased_in_project_id`): a restore counts it apart instead of repeating
   it in every project of the account, where it would erase a namesake
   (services/erasure_replay.PROJECT_NOT_RECORDED). An end user's
   record whose minute holds no request of theirs is marked so too, unless
   it is the account door's: DELETE /admin/users/{id}/memories, the only
   door that erases an end user in every project, exists since 0.1.40 (17
   Oct 2026) and files its own trail row with no project (`scope`
   account). A record before it was made by the /v1 door, in its key's
   project, which nothing on file says: left with no project it was
   repeated in every project and erased a namesake (the audit before
   1.0.0). A record from that day on stays an erasure of the whole
   account (`_the_account_doors`) when
   - the door's row naming its pseudonym (any, without a key) is on file
     within the longest an erasure takes after its record (`_longest`,
     from the deadlines of core/threads), not a minute: the record's time
     is the start of the erasure's transaction, the row is written after
     its commit, and an erasure may wait 60 s for a lock and run 900 s;
     the minute marked a slow erasure's record "not recorded", and a
     restore erased that person nowhere (the review of the audit before
     1.0.0); or
   - its row is not on file (the trail never raises, and a row can be
     lost) but the install was past 0.1.11 by then: a record filed with
     its project, or a row of the account door, earlier than it. From
     0.1.12 the /v1 door files its project, so a record with none is the
     account door's.
   An error is let fall this way: an account-wide erasure lost is a person
   a restore brings back, and widening a project's one needs one of those
   two marks, which an install on 0.1.11 or earlier cannot have; a record
   with neither is marked not recorded, and a restore counts it.

   Each record is placed from the data as the product left it: a request's
   own record is one the product filed with its project (0.1.12 on), never
   one of these, whatever project this pass or an earlier run of it has
   given it (`_its_own_record`). Read live, a keyless record just given
   Beta looked like the next request's own one, which was dropped, and that
   record stayed account-wide with its person's id in clear in the trail
   (the audit before 1.0.0).

The downgrade drops the column and puts the marked records back to NULL;
the projects found and the ids taken off stay, as every erasure's do.
"""
import hashlib
import hmac
import os

import sqlalchemy as sa
from alembic import op

revision = "f6a3d9c1b8e2"
down_revision = "d8b2f4a6c1e7"
branch_labels = None
depends_on = None

# korely_agent/services/pseudonyms.py: _INFO, PREFIX and _CHECKED;
# services/erasure_replay.PROJECT_NOT_RECORDED.
_INFO = b"korely-erasure-pseudonym-v1"
_PREFIX = "erased:"
_CHECKED = b"korely-pseudonym-key-check"
_NOT_RECORDED = "00000000-0000-0000-0000-000000000000"
# The erasure request's own row in the trail follows the erasure's commit:
# the /v1 door's of 0.1.1-0.1.11 is looked for in the minute after the
# record, as d8b4e1a7c3f5 did; the account door's over the longest an
# erasure takes (`_longest`).
_AFTER = "60 seconds"
# The day of 0.1.40, which added DELETE /admin/users/{id}/memories, the only
# door that erases an end user in every project of an account.
_ACCOUNT_DOOR_SINCE = "2026-10-17 00:00:00+00"


def _key():
    # As d8b4e1a7c3f5: the environment, then ./.env through the settings.
    secret = (os.environ.get("SECRET_KEY") or "").strip()
    if not secret:
        try:
            from korely_agent.config import get_settings

            secret = (get_settings().secret_key or "").strip()
        except Exception:  # noqa: BLE001 - settings that do not load: no key
            secret = ""
    if not secret:
        return None
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=_INFO).derive(secret.encode("utf-8"))


def pseudonym(key, tenant_id, end_user_id):
    if key is None or not end_user_id:
        return None
    mac = hmac.new(key, f"{tenant_id}:{end_user_id}".encode("utf-8"), hashlib.sha256)
    return _PREFIX + mac.hexdigest()[:32]


def key_check(key):
    return None if key is None else hmac.new(key, _CHECKED, hashlib.sha256).hexdigest()[:32]


def _requests(bind, tenant_id, at, endpoint):
    """(end user, project) of the erasure requests with a project in the
    minute after `at`: the request's own row (endpoint `endpoint`, or none
    recorded)."""
    return bind.execute(sa.text(
        "SELECT DISTINCT end_user_id, project_id FROM audit_events"
        " WHERE tenant_id = :t AND action = 'erase' AND project_id IS NOT NULL"
        " AND COALESCE(meta->>'endpoint', :ep) = :ep"
        f" AND ts >= :at AND ts <= CAST(:at AS timestamptz) + interval '{_AFTER}'"),
        {"t": tenant_id, "at": at, "ep": endpoint}).fetchall()


def _its_own_record(bind, rid, tenant_id, scope, at, project_id, named, agent, p, u) -> bool:
    """Whether another record of the same kind, filed in `project_id` by the
    product (0.1.12 on), is within a minute of `at`: the request it found
    has its record, and the one being placed is not it. The product filed
    its project (`project_id`, which a project's deletion moves to the
    Default one and never empties); a record of 0.1.1-0.1.11 has none,
    whatever project this pass, d8b4e1a7c3f5 or an earlier run of this
    migration gave it (`erased_in_project_id`). The ids of the records this
    pass placed were sent with every query (a list as long as the pass) and
    missed the ones a downgrade left placed (the review of the audit before
    1.0.0)."""
    who = ("target_id = :agent" if scope == "agent"
           else "(end_user_id IS NULL OR end_user_id IN (:named, :p, :u))")
    return bind.execute(sa.text(
        "SELECT 1 FROM agent_forget_audit WHERE tenant_id = :t AND id <> :id AND scope = :s"
        " AND project_id IS NOT NULL"
        " AND (project_id = :proj OR erased_in_project_id = :proj) AND " + who +
        f" AND created_at BETWEEN CAST(:at AS timestamptz) - interval '{_AFTER}'"
        f" AND CAST(:at AS timestamptz) + interval '{_AFTER}' LIMIT 1"),
        {"t": tenant_id, "id": rid, "s": scope, "proj": project_id, "agent": agent,
         "named": named or "", "p": p or "", "u": u or "", "at": at}).first() is not None


# The account door's trail row: no project, `scope` account.
_DOORS_ROW = "action = 'erase' AND project_id IS NULL AND meta->>'scope' = 'account'"


def _longest() -> str:
    """How long after its record an erasure's own trail row may be written,
    as an interval: the record's time is the start of the erasure's
    transaction, which waits at most LONG_LOCK_WAIT_SECONDS for a lock and
    runs statements of at most LONG_STATEMENT_SECONDS (core/threads.
    longer_deadlines); the row is written after the commit, from a pool
    that makes it wait at most POOL_CHECKOUT_SECONDS for a connection."""
    from korely_agent.core import threads

    seconds = (threads.LONG_LOCK_WAIT_SECONDS + threads.LONG_STATEMENT_SECONDS
               + threads.POOL_CHECKOUT_SECONDS)
    return f"{int(seconds)} seconds"


def _past_0_1_11(bind):
    """The earliest moment the install is known to run 0.1.12 or later, or
    None: the first erasure or forget record filed with its project (ef44feb,
    0.1.12, filed one for every end user's, agent's, memory's and fact's),
    or the first trail row of the account door (0.1.40). Any account's: a
    version is the install's."""
    return bind.execute(sa.text(
        "SELECT LEAST((SELECT min(created_at) FROM agent_forget_audit"
        "              WHERE project_id IS NOT NULL"
        "              AND scope IN ('user', 'agent', 'memory', 'fact')),"
        f"            (SELECT min(ts) FROM audit_events WHERE {_DOORS_ROW}))")).scalar()


def _the_account_doors(bind, tenant_id, at, named, past_0_1_11) -> bool:
    """Whether an end user's record with no project is the account door's
    (DELETE /admin/users/{id}/memories): made on or after the day that door
    came, and either with the door's own trail row on file after it, within
    the longest an erasure takes (`_longest`), naming the same pseudonym (or
    any, without a key), or made once the install was past 0.1.11
    (`past_0_1_11`), when the /v1 door filed its project and a record with
    none could be no other door's."""
    return bool(bind.execute(sa.text(
        "SELECT CAST(:at AS timestamptz) >= CAST(:since AS timestamptz) AND ("
        " COALESCE(CAST(:at AS timestamptz) >= CAST(:past AS timestamptz), false)"
        f" OR EXISTS (SELECT 1 FROM audit_events WHERE tenant_id = :t AND {_DOORS_ROW}"
        " AND (CAST(:named AS text) IS NULL OR end_user_id = :named)"
        f" AND ts >= :at AND ts <= CAST(:at AS timestamptz) + interval '{_longest()}'))"),
        {"t": tenant_id, "at": at, "since": _ACCOUNT_DOOR_SINCE, "named": named,
         "past": past_0_1_11}).scalar())


def _found(bind, rid, project_id) -> None:
    bind.execute(sa.text("UPDATE agent_forget_audit SET erased_in_project_id = :p WHERE id = :id"),
                 {"p": project_id, "id": rid})


def _not_recorded(bind, rid) -> None:
    _found(bind, rid, _NOT_RECORDED)


def _take_the_id_off(bind, key, tenant_id, end_user, project_id, at, rid) -> None:
    """What an erasure without a key left in clear in the project it was
    made in, up to it: the id goes, or takes the pseudonym under the key
    set now, the record too."""
    p = pseudonym(key, tenant_id, end_user)
    args = {"t": tenant_id, "u": end_user, "p": p, "proj": project_id, "at": at,
            "proj_s": str(project_id), "id": rid, "c": key_check(key)}
    bind.execute(sa.text(
        "UPDATE audit_events SET end_user_id = :p WHERE tenant_id = :t AND end_user_id = :u"
        f" AND project_id = :proj AND ts <= CAST(:at AS timestamptz) + interval '{_AFTER}'"),
        args)
    bind.execute(sa.text(
        "UPDATE agent_forget_audit SET end_user_id = :p, key_check = :c WHERE tenant_id = :t"
        " AND end_user_id = :u AND project_id = :proj AND created_at <= :at"), args)
    bind.execute(sa.text(
        "UPDATE agent_webhook_deliveries"
        # A JSON null without a key: to_jsonb(NULL) is SQL NULL, and jsonb_set
        # with it is NULL, which the NOT NULL payload refuses.
        " SET payload = jsonb_set(payload::jsonb, '{data,user_id}',"
        " COALESCE(to_jsonb(CAST(:p AS text)), 'null'::jsonb))::json"
        " WHERE tenant_id = :t AND status = 'delivered' AND created_at <= :at"
        " AND payload->'data'->>'user_id' = :u AND payload->>'project_id' = :proj_s"), args)
    if p is not None:
        bind.execute(sa.text(
            "UPDATE agent_forget_audit SET end_user_id = :p, key_check = :c WHERE id = :id"), args)


def _september(bind, key) -> dict:
    out = {"found": 0, "not_recorded": 0}
    records = bind.execute(sa.text(
        "SELECT id, tenant_id, scope, end_user_id, target_id, created_at FROM agent_forget_audit"
        " WHERE scope IN ('user', 'agent') AND project_id IS NULL"
        " AND erased_in_project_id IS NULL"
        " AND (scope = 'agent' OR target_id IS NULL) ORDER BY created_at")).fetchall()
    past_0_1_11 = _past_0_1_11(bind)
    for rid, tenant_id, scope, named, agent, at in records:
        if scope == "agent":
            rows = _requests(bind, tenant_id, at, "agents:delete")
            projects = {proj for u, proj in rows if u is None}
            matched = [(None, proj) for proj in projects]
        else:
            rows = [(u, proj) for u, proj in _requests(bind, tenant_id, at, "users:delete") if u]
            if named is None:
                # Without a key then: every person erased with a project in
                # that minute could be this one.
                matched = sorted({(u, proj) for u, proj in rows if not u.startswith(_PREFIX)},
                                 key=str)
            else:
                matched = sorted({(u, proj) for u, proj in rows
                                  if u == named or pseudonym(key, tenant_id, u) == named},
                                 key=str)
        # A request that filed its own record in its project within the
        # minute either side is that record's (0.1.12 on), never this one's:
        # an account-wide erasure next to it stays account-wide. Never a
        # record of 0.1.1-0.1.11, whatever project it has been given.
        matched = [(u, proj) for u, proj in matched
                   if not _its_own_record(bind, rid, tenant_id, scope, at, proj, named, agent,
                                          pseudonym(key, tenant_id, u) if u else None, u)]
        if len(matched) != 1:
            # No agent door erases the whole account, so an agent's record
            # with no project is always one of those. An end user's with no
            # request of theirs in that minute stays the whole account's
            # only when it is the account door's; any other was made in one
            # project, which nothing on file says.
            if matched or scope == "agent" or not _the_account_doors(bind, tenant_id, at, named,
                                                                     past_0_1_11):
                _not_recorded(bind, rid)
                out["not_recorded"] += 1
            continue
        end_user, project_id = matched[0]
        if scope == "user" and end_user != named and not end_user.startswith(_PREFIX):
            _take_the_id_off(bind, key, tenant_id, end_user, project_id, at, rid)
        _found(bind, rid, project_id)
        out["found"] += 1
    return out


def upgrade() -> None:
    op.add_column("agent_webhook_deliveries",
                  sa.Column("erased_user", sa.Boolean(), nullable=False,
                            server_default=sa.text("false")))
    bind = op.get_bind()
    bind.execute(sa.text("""
        UPDATE agent_webhook_deliveries d
        SET payload = jsonb_set(d.payload::jsonb, '{project_id}', to_jsonb(p.id::text))::json
        FROM agent_projects p
        WHERE p.tenant_id = d.tenant_id AND p.is_default
          AND d.payload->>'project_id' IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM agent_projects x
                          WHERE x.id::text = d.payload->>'project_id')
          AND NOT EXISTS (SELECT 1 FROM agent_forget_audit a
                          WHERE a.tenant_id = d.tenant_id AND a.scope = 'project'
                            AND a.target_id = d.payload->>'project_id')
    """))
    _september(bind, _key())


def downgrade() -> None:
    op.execute(f"UPDATE agent_forget_audit SET erased_in_project_id = NULL"
               f" WHERE erased_in_project_id = '{_NOT_RECORDED}'")
    op.drop_column("agent_webhook_deliveries", "erased_user")
