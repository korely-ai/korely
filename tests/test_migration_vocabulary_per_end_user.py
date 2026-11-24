"""The migration that rebuilds the vocabulary per end user (e7d1b3f9a2c6), on data.

Each end user's relations are rebuilt from that end user's own facts: the
example is their oldest value, the family their facts' own, the vote
undecided (the old one was taken on somebody else's value), no centroid, the
phrases their facts were written with. The shared rows go, and a relation no
fact uses goes with them. The downgrade puts one vocabulary per project back.

On a database of its own, made and dropped here, migrated with Alembic.
"""
import uuid

from sqlalchemy import create_engine, text

from tests import test_migration_vocabulary_per_project as per_project

scratch_db = per_project.scratch_db
_alembic = per_project._alembic


BEFORE, AFTER = "c45a7e2b9d13", "e7d1b3f9a2c6"


def test_each_end_users_vocabulary_is_rebuilt_from_their_own_facts(scratch_db):
    _alembic("upgrade", BEFORE)
    eng = create_engine(scratch_db)
    t, p = uuid.uuid4(), uuid.uuid4()
    shared, unused = uuid.uuid4(), uuid.uuid4()
    with eng.begin() as c:
        c.execute(text("INSERT INTO tenants (id, name) VALUES (:t, 'Acme')"), {"t": t})
        c.execute(text("INSERT INTO agent_projects (id, tenant_id, name, slug, is_default) "
                       "VALUES (:p, :t, 'Default', 'default', true)"), {"p": p, "t": t})
        for user, raw, obj, when in (("anna", "is subscribed to", "Platinum-Anna", "2026-10-01"),
                                     ("anna", "subscribes to", "Gold", "2026-10-03"),
                                     ("bruno", "is on plan", "Basic plan", "2026-10-02")):
            c.execute(text(
                "INSERT INTO agent_facts (id, tenant_id, project_id, end_user_id, subject_label, "
                "subject_canonical, predicate_raw, predicate_canonical, predicate_family, object_label, "
                "object_canonical, created_at) VALUES (:id, :t, :p, :u, :u, :u, :raw, 'subscribes_to', "
                "'work', :o, :o, :w)"),
                {"id": uuid.uuid4(), "t": t, "p": p, "u": user, "raw": raw, "o": obj, "w": when})
        c.execute(text(
            "INSERT INTO predicate_clusters (id, tenant_id, project_id, canonical, is_functional, family, "
            "example_object, member_count) VALUES "
            "(:s, :t, :p, 'subscribes_to', true, 'work', 'Platinum-Anna', 3), "
            "(:u, :t, :p, 'mentors', NULL, NULL, 'Elena', 1)"), {"s": shared, "u": unused, "t": t, "p": p})
        c.execute(text(
            "INSERT INTO predicate_aliases (tenant_id, project_id, surface, cluster_id) VALUES "
            "(:t, :p, 'subscribes_to', :s), (:t, :p, 'is_on_plan', :s), (:t, :p, 'mentors', :u)"),
            {"t": t, "p": p, "s": shared, "u": unused})

    _alembic("upgrade", AFTER)
    with eng.connect() as c:
        rel = {(r.end_user_id, r.canonical): r for r in c.execute(text(
            "SELECT * FROM predicate_clusters WHERE tenant_id = :t"), {"t": t})}
        assert set(rel) == {("anna", "subscribes_to"), ("bruno", "subscribes_to")}, \
            "one per end user; the relation no fact uses is gone"
        assert rel[("anna", "subscribes_to")].example_object == "Platinum-Anna"
        assert rel[("bruno", "subscribes_to")].example_object == "Basic plan", "his own value"
        assert all(r.is_functional is None for r in rel.values()), "the shared vote is not copied"
        assert all(r.centroid is None for r in rel.values())
        assert rel[("anna", "subscribes_to")].member_count == 2
        assert rel[("bruno", "subscribes_to")].family == "work"
        phrases = {(r.end_user_id, r.surface) for r in c.execute(text(
            "SELECT end_user_id, surface FROM predicate_aliases WHERE tenant_id = :t"), {"t": t})}
        assert phrases == {("anna", "subscribes_to"), ("anna", "is_subscribed_to"),
                           ("bruno", "subscribes_to"), ("bruno", "is_on_plan")}

    _alembic("downgrade", BEFORE)
    with eng.connect() as c:
        rows = c.execute(text("SELECT canonical FROM predicate_clusters WHERE tenant_id = :t"),
                         {"t": t}).scalars().all()
        assert rows == ["subscribes_to"]
        surfaces = sorted(c.execute(text("SELECT surface FROM predicate_aliases WHERE tenant_id = :t"),
                                    {"t": t}).scalars().all())
        assert surfaces == ["is_on_plan", "is_subscribed_to", "subscribes_to"]
    eng.dispose()
