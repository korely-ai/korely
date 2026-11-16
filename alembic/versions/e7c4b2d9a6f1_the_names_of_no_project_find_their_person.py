"""the names 0.2.0 filed under the Default project go to their person's project

Revision ID: e7c4b2d9a6f1
Revises: f6a3d9c1b8e2
Create Date: 2026-10-27 18:00:00

The audit of 27 Oct 2026. Up to 0.1.40 POST /admin/aliases without a
`project_id` wrote the name with no project (5860a8e put it in the key's
project from 0.1.41), and a9d3f6c2e8b4 moved those rows to the Default
project, since which project they were meant for was not recorded. An
erasure in the project the person lives in then had to reach into the
Default project for them (end_user_places.legacy_names, 0.2.1), and there it
could not tell them from the names a namesake declared in the Default
project: an erasure in Beta deleted them too.

Here each name in the Default project of an end user or an agent namespace
is placed from what the database already holds, so an erasure stays behind
its project's wall:

- the Default project's own, which stays: a name declared there through
  the names door (its trail row, endpoint names:declare, filed under the
  Default project with the name's id), and one the dashboard or an admin
  key of the Default project (or of no project, which is the Default's)
  made through POST /admin/aliases from 0.1.41 on, which put a name sent
  with no project in the key's project (its trail row, and the row made at
  or after 0.1.41's release, `_V0_1_41`: before it the door wrote no
  project at all). Such a name of an id with no memory or fact in the
  Default project but some elsewhere is also marked as below: an install
  that stayed on 0.1.40 after that day made it with no project, and the
  erasure of its person must still reach it (the trade-off: the last
  erasure of a namesake elsewhere takes a name the Default project meant);
- the others are of the person the end-user id (else the agent id) names,
  where that id has memories or facts. In the Default project the name
  stays; in exactly one other project, it goes there; in two or more, the
  key that declared it decides when its project is one of them (the trail
  row of POST /admin/aliases names the key);
- else nobody can say whose it is: it stays in the Default project, marked
  `project_not_recorded`, where an erasure of its id in the Default project
  takes it, and so does an erasure in any project that leaves the id in no
  other project (end_user_places.unplaced_names, 0.2.1's rule for these
  rows only). An id with no memory or fact anywhere is one of them.

Where the person's project already holds the same name of the same end user
and agent, the unique index admits one: the newer decision (`since`, then
`created_at`) stays and the other goes, as a9d3f6c2e8b4 did. A name moved
applies to the facts written there from now on, as a9d3f6c2e8b4's did in the
Default project; the facts already written keep their names.

Every statement reads the names of the Default projects once (a temporary
table), the trail by the index on (tenant, ts) in the day after each name
was made or last decided (the request's own row follows its commit, after
the facts it re-canonicalized; the name's id makes the match exact),
and the memories and facts of an end user by their (tenant, end user)
index, those of an agent namespace by the (tenant, project) index filtered
on the agent id (no index leads with it), one probe per id and project: no
scan of the trail or of its JSON.

The downgrade drops the column and leaves the names where this put them:
which rows moved is not recorded, and 0.2.1's erasures reach them there too.
"""
import sqlalchemy as sa
from alembic import op

revision = "e7c4b2d9a6f1"
down_revision = "f6a3d9c1b8e2"
branch_labels = None
depends_on = None

# v0.1.41 (5860a8e: a name sent with no project goes to the key's project).
_V0_1_41 = "2026-10-18T01:02:31+00:00"
# The request's own trail row follows the commit of the name it made, after
# the re-canonicalization of the memory's facts (`created_at` is the start
# of that transaction): a day, the name's id making the match exact.
_AFTER = "1 day"


def _names(bind) -> None:
    """The names of a person in the Default projects, once."""
    bind.execute(sa.text("DROP TABLE IF EXISTS _names_in_default"))
    bind.execute(sa.text("""
        CREATE TEMPORARY TABLE _names_in_default ON COMMIT DROP AS
        SELECT a.id, a.id::text AS tid, a.tenant_id, p.id AS dflt, a.end_user_id,
               a.agent_id, a.created_at, a.since
        FROM agent_entity_aliases a
        JOIN agent_projects p ON p.tenant_id = a.tenant_id AND p.is_default
                             AND p.id = a.project_id
        WHERE a.end_user_id IS NOT NULL OR a.agent_id IS NOT NULL
    """))
    bind.execute(sa.text("ANALYZE _names_in_default"))


def _trail(bind) -> list:
    """The trail rows naming each name, in the day after it was made or
    last decided: (name, actor, project, endpoint, setting, op, key known,
    key's project)."""
    rows = set()
    for at in ("created_at", "since"):
        rows |= set(bind.execute(sa.text(f"""
            SELECT n.id, e.actor, e.project_id, e.meta->>'endpoint', e.meta->>'setting',
                   e.meta->>'op', k.id IS NOT NULL, k.project_id
            FROM _names_in_default n
            JOIN audit_events e ON e.tenant_id = n.tenant_id
                 AND e.ts >= n.{at} AND e.ts <= n.{at} + interval '{_AFTER}'
                 AND e.action IN ('fact_write', 'setting_change') AND e.target_id = n.tid
            LEFT JOIN agent_api_keys k ON k.id = e.api_key_id
        """)))
    return list(rows)


def _homes(bind) -> dict:
    """{(kind, tenant, id): projects where the id has a memory or a fact}."""
    out: dict = {}
    for table in ("agent_memories", "agent_facts"):
        for kind, column in (("u", "end_user_id"), ("a", "agent_id")):
            other = "" if kind == "u" else " AND n.end_user_id IS NULL"
            for tenant, who, project in bind.execute(sa.text(f"""
                SELECT x.tenant_id, x.who, p.id
                FROM (SELECT DISTINCT tenant_id, {column} AS who FROM _names_in_default n
                      WHERE {column} IS NOT NULL{other}) x
                JOIN agent_projects p ON p.tenant_id = x.tenant_id
                WHERE EXISTS (SELECT 1 FROM {table} m WHERE m.tenant_id = x.tenant_id
                              AND m.{column} = x.who AND m.project_id = p.id)
            """)):
                out.setdefault((kind, tenant, who), set()).add(project)
    return out


def _place(bind, placed: list) -> None:
    """[(name, project or None)]: each name into its project, set based, the
    newer decision kept where that project holds the same name; None marks
    it `project_not_recorded` where it is."""
    bind.execute(sa.text("DROP TABLE IF EXISTS _names_placed"))
    bind.execute(sa.text("CREATE TEMPORARY TABLE _names_placed (id uuid PRIMARY KEY, dest uuid)"
                         " ON COMMIT DROP"))
    if placed:
        bind.execute(sa.text("INSERT INTO _names_placed (id, dest) VALUES (:id, :dest)"),
                     [{"id": i, "dest": d} for i, d in placed])
    bind.execute(sa.text("ANALYZE _names_placed"))
    bind.execute(sa.text("UPDATE agent_entity_aliases a SET project_not_recorded = true"
                         " FROM _names_placed d WHERE a.id = d.id AND d.dest IS NULL"))
    same = ("x.tenant_id = n.tenant_id AND x.project_id = d.dest"
            " AND x.end_user_id IS NOT DISTINCT FROM n.end_user_id"
            " AND x.agent_id IS NOT DISTINCT FROM n.agent_id AND x.alias_lower = n.alias_lower")
    bind.execute(sa.text(
        "DELETE FROM agent_entity_aliases x USING _names_placed d"
        " JOIN agent_entity_aliases n ON n.id = d.id WHERE " + same +
        " AND (x.since, x.created_at, x.id) < (n.since, n.created_at, n.id)"))
    bind.execute(sa.text(
        "DELETE FROM agent_entity_aliases n USING _names_placed d WHERE n.id = d.id AND"
        " EXISTS (SELECT 1 FROM agent_entity_aliases x WHERE " + same + ")"))
    bind.execute(sa.text("UPDATE agent_entity_aliases a SET project_id = d.dest"
                         " FROM _names_placed d WHERE a.id = d.id AND d.dest IS NOT NULL"))
    bind.execute(sa.text("DROP TABLE IF EXISTS _names_placed"))


def repair(bind) -> dict:
    """Each name of a person in the Default project placed: kept, moved to
    its person's project, or kept and marked `project_not_recorded`;
    counts. Run again, it moves nothing new."""
    from datetime import datetime

    v0_1_41 = datetime.fromisoformat(_V0_1_41)
    _names(bind)
    names = bind.execute(sa.text(
        "SELECT id, tenant_id, dflt, end_user_id, agent_id, created_at"
        " FROM _names_in_default")).fetchall()
    dflt_of = {r.id: r.dflt for r in names}
    declared_here: set = set()     # the Default project's own, by the trail
    keys: dict = {}                # the projects of the keys that declared it
    for alias_id, actor, project, endpoint, setting, op_, known, key_project in _trail(bind):
        if endpoint == "names:declare" and project == dflt_of[alias_id]:
            declared_here.add(alias_id)
        if setting == "alias" and op_ == "create":
            if actor == "dashboard" or (known and key_project in (None, dflt_of[alias_id])):
                keys.setdefault(alias_id, set()).add(None)
            elif known:
                keys.setdefault(alias_id, set()).add(key_project)
    homes = _homes(bind)
    out = {"moved": 0, "kept": 0, "not_recorded": 0}
    placed = []
    for alias_id, tenant, dflt, end_user, agent, created_at in names:
        by = keys.get(alias_id, set())
        there = homes.get(("u", tenant, end_user) if end_user is not None
                          else ("a", tenant, agent), set())
        if alias_id in declared_here:
            out["kept"] += 1
            continue
        if None in by and created_at >= v0_1_41:
            # Filed in Default on purpose, or by an install still on 0.1.40
            # with no project: kept, and marked when its person lives
            # elsewhere only, so their last erasure takes it.
            if there and dflt not in there:
                placed.append((alias_id, None))
                out["not_recorded"] += 1
            else:
                out["kept"] += 1
            continue
        if dflt in there:
            out["kept"] += 1
            continue
        if len(there) == 1:
            to = next(iter(there))
        else:
            by_key = by & there
            to = next(iter(by_key)) if len(by_key) == 1 else None
        placed.append((alias_id, to))
        out["moved" if to is not None else "not_recorded"] += 1
    _place(bind, placed)
    bind.execute(sa.text("DROP TABLE IF EXISTS _names_in_default"))
    return out


def upgrade() -> None:
    op.add_column("agent_entity_aliases",
                  sa.Column("project_not_recorded", sa.Boolean(), nullable=False,
                            server_default=sa.text("false")))
    repair(op.get_bind())


def downgrade() -> None:
    op.drop_column("agent_entity_aliases", "project_not_recorded")
