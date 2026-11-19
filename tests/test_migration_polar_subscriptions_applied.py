"""The migration that keeps a subscription put on record apart from one applied
(d8a4f2c6b1e9), on data.

Every row made before it was made by an applied version (`billing._in_order`),
so each one it finds is marked applied; a row made after it, with no value
given, is not. The downgrade drops the column and keeps the rows.

On a database of its own, made and dropped here, migrated with Alembic.
"""
import uuid

from sqlalchemy import create_engine, inspect, text

from tests import test_migration_vocabulary_per_project as per_project

scratch_db = per_project.scratch_db
_alembic = per_project._alembic


BEFORE, AFTER = "b7d2e9f4a1c6", "d8a4f2c6b1e9"


def test_every_row_made_before_is_applied_and_the_downgrade_keeps_the_rows(scratch_db):
    _alembic("upgrade", BEFORE)
    eng = create_engine(scratch_db)
    t = uuid.uuid4()
    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:t, 'Acme')"), {"t": t})
        c.execute(text(
            "INSERT INTO hosted_polar_subscriptions (subscription_id, tenant_id, modified_at, "
            "revoked_at, customer_id) VALUES "
            "('sub_live', :t, '2026-10-01T00:00:00Z', NULL, 'cus_A'), "
            "('sub_gone', :t, '2026-09-01T00:00:00Z', '2026-09-15T00:00:00Z', NULL)"), {"t": t})

    _alembic("upgrade", AFTER)
    with eng.begin() as c:
        rows = dict(c.execute(text(
            "SELECT subscription_id, applied FROM hosted_polar_subscriptions WHERE tenant_id = :t"),
            {"t": t}).all())
        assert rows == {"sub_live": True, "sub_gone": True}
        c.execute(text("INSERT INTO hosted_polar_subscriptions (subscription_id, tenant_id) "
                       "VALUES ('sub_new', :t)"), {"t": t})
        assert c.execute(text("SELECT applied FROM hosted_polar_subscriptions "
                              "WHERE subscription_id = 'sub_new'")).scalar() is False

    _alembic("downgrade", BEFORE)
    columns = {col["name"] for col in inspect(eng).get_columns("hosted_polar_subscriptions")}
    assert "applied" not in columns
    with eng.connect() as c:
        kept = c.execute(text("SELECT subscription_id, customer_id FROM hosted_polar_subscriptions "
                              "WHERE tenant_id = :t ORDER BY subscription_id"), {"t": t}).all()
        assert [tuple(r) for r in kept] == [("sub_gone", None), ("sub_live", "cus_A"),
                                            ("sub_new", None)]
    eng.dispose()
