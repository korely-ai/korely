"""Schema invariants.

These run against the metadata rather than a live database, so they are fast and
have no fixtures. Creating the DDL for real belongs in CI against Postgres: the
codebase this came from tested on SQLite, which does not apply migrations, and
shipped a migration referencing a column that did not exist. It crashed
production in a boot loop. Metadata tests catch a different class of mistake and
do not replace that one.
"""
import pytest

# From the package: it imports every model, which is what puts the tables on
# this metadata.
from korely_agent.db import Base
from tests import cloud

TENANT_SCOPED = {
    "agent_api_keys", "agent_batch_jobs", "agent_facts",
    "agent_forget_audit", "agent_llm_usage", "agent_memories", "agent_projects", "agent_rate_limits",
    "agent_webhook_deliveries", "agent_webhook_endpoints",
    # Not agent_*-named, but owned by a tenant all the same.
    "operators",
    # 2026-09-27: former names -> current names, per memory (memory_core).
    "agent_entity_aliases",
    # 30 Oct 2026: two names of one memory judged one thing, a link the reads
    # resolve (memory_core 6.1.0 `objects`, off by default).
    "agent_object_links",
    # The learned vocabulary, which holds customers' values; the trail; an
    # OAuth grant once consented (audit of 17 Oct 2026: these four were left
    # out of this set, so the isolation and cascade tests never read them).
    "predicate_clusters", "predicate_aliases", "audit_events", "oauth_grants",
}
# The hosted service's (korely_agent/hosted/models.py, 8 Oct 2026): created on
# every install, one chain of migrations; empty unless KORELY_MODE=hosted. On
# the metadata where the tree carries the Cloud's package (tests/cloud.py).
CLOUD_TENANT_SCOPED = {
    "hosted_accounts", "hosted_usage_months", "hosted_topups",
    # 17 Oct 2026: what the webhooks last applied of each Polar subscription.
    "hosted_polar_subscriptions",
    # 28 Oct 2026: the Polar events kept unfiled until `cli refile`.
    "hosted_polar_unfiled",
}
# The hosted service's, service-wide: Polar events handled, providers braked,
# the sign-ups the caps count, outliving their accounts (17 Oct 2026), and the
# proof of an erasure, which exists to outlive its tenant.
CLOUD_SERVICE_WIDE = {"hosted_polar_events", "hosted_brakes", "hosted_signups", "hosted_erasures"}
if cloud.HERE:
    TENANT_SCOPED |= CLOUD_TENANT_SCOPED

# A tenant column that may be empty for a while, and why. An OAuth grant is
# requested before anybody has consented, so before it has an account; it
# cascades with the account all the same once it has one.
_TENANT_LATER = {"oauth_grants"}


def _tenant_owned():
    """The tables the schema itself says a tenant owns: a column with a
    foreign key to tenants.id. Read from the metadata, so a new table cannot
    stay out of the tests below by being left out of a list."""
    return {name for name, t in Base.metadata.tables.items()
            if any(fk.target_fullname == "tenants.id" for c in t.c for fk in c.foreign_keys)}


_SCOPED = sorted(TENANT_SCOPED | _tenant_owned())


class TestSurface:
    def test_exactly_the_expected_tables(self):
        """20 tables, not 62. `install_state` (2026-10-06) holds the model
        that computed the stored vectors, for the start's check;
        `login_attempts` (2026-10-06) the login's locks, per address; and
        `agents` and `agent_daily_usage`, which nothing read or wrote, went
        the same day.

        A client installing this gets a database containing only what runs. The
        monolith created 62, including `telegram_files` and
        `calendar_connections`, which is a conversation nobody wants to have
        during a security review.
        """
        got = set(Base.metadata.tables)
        expected = TENANT_SCOPED | {"tenants",
                                    # sessions hang off an operator, not a tenant
                                    "operator_sessions",
                                    # install-wide, so no tenant either
                                    "install_settings", "install_state",
                                    # per address, before anybody is known
                                    "login_attempts",
                                    # OAuth for the MCP (16 Oct 2026): the clients
                                    # that register are service-wide (a grant
                                    # belongs to a tenant once consented)
                                    "oauth_clients"}
        if cloud.HERE:
            expected |= CLOUD_SERVICE_WIDE
        assert got == expected, (
            f"unexpected: {got - expected}, missing: {expected - got}"
        )

    def test_nothing_references_a_users_table(self):
        """The `users` table belongs to the product this was extracted from."""
        for t in Base.metadata.tables.values():
            for fk in t.foreign_keys:
                assert not fk.target_fullname.startswith("users."), (
                    f"{t.name} still points at {fk.target_fullname}"
                )


class TestTenancy:
    def test_the_list_is_the_schemas(self):
        """TENANT_SCOPED, written by hand for the surface above, is exactly the
        set of tables with a foreign key to tenants.id."""
        derived = _tenant_owned()
        assert TENANT_SCOPED == derived, (
            f"owned by a tenant but not listed: {sorted(derived - TENANT_SCOPED)}, "
            f"listed but with no key to tenants: {sorted(TENANT_SCOPED - derived)}")

    @pytest.mark.parametrize("table", _SCOPED)
    def test_every_agent_table_is_scoped_to_a_tenant(self, table):
        """Isolation is a foreign key, not a convention.

        An agency runs one install for several of its own clients. If a table
        can hold a row with no owner, a query that forgets the filter returns
        another client's data, and that is not a bug you find in testing.
        """
        t = Base.metadata.tables[table]
        assert "tenant_id" in t.c, f"{table} has no tenant_id"
        targets = {fk.target_fullname for fk in t.c.tenant_id.foreign_keys}
        assert targets == {"tenants.id"}, f"{table}.tenant_id points at {targets}"
        assert table in _TENANT_LATER or not t.c.tenant_id.nullable, f"{table}.tenant_id is nullable"

    @pytest.mark.parametrize("table", _SCOPED)
    def test_tenant_delete_cascades(self, table):
        """Erasure has to be one statement.

        GDPR Article 17 is answered by deleting the tenant row. If any table is
        RESTRICT or SET NULL, the delete fails or leaves orphans, and 'we
        deleted your data' becomes a claim rather than a fact. The published
        example verifies erasure by reading back and counting; that only returns
        zero if this holds.
        """
        t = Base.metadata.tables[table]
        for fk in t.c.tenant_id.foreign_keys:
            assert fk.ondelete == "CASCADE", (
                f"{table}.tenant_id is ON DELETE {fk.ondelete}, so deleting a "
                f"tenant leaves data behind"
            )


    def test_no_foreign_key_stops_the_cascade(self):
        """Deleting the tenant row cascades through tables that also point at
        each other (aliases at their clusters, deliveries at their endpoints,
        grants at their keys). A key between two of them that is RESTRICT, or
        SET NULL on a column that cannot be empty, makes the one statement of
        the erasure fail (hosted/closing.py deletes the tenant row and counts
        on these)."""
        stops = []
        for name, t in sorted(Base.metadata.tables.items()):
            for c in t.c:
                for fk in c.foreign_keys:
                    rule = (fk.ondelete or "NO ACTION").upper()
                    if rule not in ("CASCADE", "SET NULL") or (rule == "SET NULL" and not c.nullable):
                        stops.append(f"{name}.{c.name} -> {fk.target_fullname} ON DELETE {rule}")
        assert not stops, stops


class TestFactsModel:
    """The bi-temporal columns are the product; guard their shape."""

    def test_validity_window_and_supersede_pointer_exist(self):
        c = Base.metadata.tables["agent_facts"].c
        assert not c.valid_from.nullable, "a fact with no start has no window"
        assert c.invalid_at.nullable, "NULL invalid_at is what 'still true' means"
        assert "agent_facts.id" in {
            fk.target_fullname for fk in c.invalidated_by_id.foreign_keys
        }, "a superseded fact must point at what replaced it"

    def test_superseding_fact_deletion_does_not_erase_history(self):
        """invalidated_by_id is SET NULL, never CASCADE.

        With CASCADE, deleting a newer fact would delete the older one it
        replaced, and the audit trail would quietly lose the row that explains
        why the agent answered what it did in March.
        """
        c = Base.metadata.tables["agent_facts"].c
        for fk in c.invalidated_by_id.foreign_keys:
            assert fk.ondelete == "SET NULL"

    def test_end_user_id_is_a_plain_string(self):
        """Namespaces are caller-chosen opaque strings, not foreign keys.

        Making this a relation would force callers to register an entity before
        storing a memory, which is exactly the friction the published contract
        avoids.
        """
        c = Base.metadata.tables["agent_facts"].c
        assert not c.end_user_id.foreign_keys


class TestPublicContract:
    """The wire format says `user_id`; the column is `end_user_id`.

    These are two names for the same thing at different layers, and the rename
    from the monolith's `user_id` (which meant the owning account) to
    `tenant_id` nearly broke it: a mechanical find-and-replace hit dictionary
    keys inside response builders as well as Python identifiers, which would
    have renamed the field in every API response and broken every SDK that
    reads it. Caught by reading the diff, not by a test, so here is the test.
    """

    def test_response_builders_emit_user_id_not_tenant_id(self):
        import pathlib

        root = pathlib.Path(__file__).parent.parent / "korely_agent" / "services"
        offenders = []
        for f in root.rglob("*.py"):
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if '"tenant_id"' in line and "Column" not in line and "Index" not in line:
                    offenders.append(f"{f.name}:{i}: {line.strip()}")
        assert not offenders, (
            "a quoted \"tenant_id\" in the service layer is almost certainly a "
            "wire-format key that should be \"user_id\":\n  " + "\n  ".join(offenders)
        )
