"""The migration that marks the accounts made without a login before the
trial existed (b6e2d9f4a8c1), on data.

From 1.0.10 the key `POST /v1/agents/init` makes (`agent-cli`) has an end
date; before, it had none. An account born without a login, with no login
yet and no `agent-cli` key with an end date, is marked; an account made
from 1.0.10 on (its key live, expired or revoked), an adopted one and one
born from a sign-in are not. No key is touched. A row made after the
migration, with no value given, is not marked. The downgrade drops the
column and keeps the rows.

On a database of its own, made and dropped here, migrated with Alembic.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, inspect, text

from tests import test_migration_vocabulary_per_project as per_project

scratch_db = per_project.scratch_db
_alembic = per_project._alembic


BEFORE, AFTER = "e4b7c1d9a2f6", "b6e2d9f4a8c1"
NOW = datetime.now(timezone.utc)


def _account(c, *, without_login=True, uid=None):
    t = uuid.uuid4()
    c.execute(text("INSERT INTO tenants (id, name) VALUES (:t, 'agent')"), {"t": t})
    c.execute(text("INSERT INTO hosted_accounts (tenant_id, born_without_login, firebase_uid) "
                   "VALUES (:t, :b, :u)"), {"t": t, "b": without_login, "u": uid})
    return t


def _key(c, tenant, name, expires_at=None, revoked_at=None):
    c.execute(text(
        "INSERT INTO agent_api_keys (id, tenant_id, key_hash, prefix, name, expires_at, revoked_at) "
        "VALUES (:i, :t, :h, 'kor_live_', :n, :e, :r)"),
        {"i": uuid.uuid4(), "t": tenant, "h": uuid.uuid4().hex, "n": name, "e": expires_at,
         "r": revoked_at})


def test_the_accounts_made_before_the_trial_are_marked_and_no_key_changes(scratch_db):
    _alembic("upgrade", BEFORE)
    eng = create_engine(scratch_db)
    with eng.begin() as c:
        before = _account(c)                      # init's key of before 1.0.10: no end
        _key(c, before, "agent-cli")
        no_uid = _account(c, uid="")              # no login, as an empty one reads
        _key(c, no_uid, "agent-cli")
        revoked = _account(c)                     # before 1.0.10, its init key revoked and
        _key(c, revoked, "agent-cli", revoked_at=NOW)  # a key of its own, with an end
        _key(c, revoked, "ci", expires_at=NOW + timedelta(days=90))
        trial = _account(c)                       # from 1.0.10: the key has its 7 days
        _key(c, trial, "agent-cli", expires_at=NOW + timedelta(days=7))
        expired = _account(c)
        _key(c, expired, "agent-cli", expires_at=NOW - timedelta(days=1))
        gone = _account(c)
        _key(c, gone, "agent-cli", expires_at=NOW + timedelta(days=3), revoked_at=NOW)
        adopted = _account(c, uid="uid-1")        # adoption lifts the end date and logs in
        _key(c, adopted, "agent-cli")
        signed_in = _account(c, without_login=False, uid="uid-2")
        _key(c, signed_in, "agent-cli")
        # Not born without a login, and with none now (its login's row gone):
        # only `born_without_login` keeps it out (a mutant that dropped it
        # from the rule lived, the sceptic's review of 10-11 Nov 2026).
        not_inits = _account(c, without_login=False, uid=None)
        _key(c, not_inits, "agent-cli")

    _alembic("upgrade", AFTER)
    with eng.begin() as c:
        marked = dict(c.execute(text(
            "SELECT tenant_id, born_before_the_trial FROM hosted_accounts")).all())
        assert marked == {before: True, no_uid: True, revoked: True, trial: False,
                          expired: False, gone: False, adopted: False, signed_in: False,
                          not_inits: False}
        ends = {(t, n): e for t, n, e in c.execute(text(
            "SELECT tenant_id, name, expires_at FROM agent_api_keys")).all()}
        assert ends[(before, "agent-cli")] is None and ends[(no_uid, "agent-cli")] is None
        assert ends[(trial, "agent-cli")] is not None and ends[(revoked, "ci")] is not None
        later = _account(c)
        assert c.execute(text("SELECT born_before_the_trial FROM hosted_accounts "
                              "WHERE tenant_id = :t"), {"t": later}).scalar() is False

    _alembic("downgrade", BEFORE)
    columns = {col["name"] for col in inspect(eng).get_columns("hosted_accounts")}
    assert "born_before_the_trial" not in columns
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM hosted_accounts")).scalar() == 10
    eng.dispose()
