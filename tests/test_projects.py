"""Projects, and which constraint failed.

Small surface, but it carries the same handler shape as the key service, and
that shape was wrong in both: `except IntegrityError -> DuplicateNameError`
answers a foreign-key violation with a statement about a name nobody used.
Found once in keys.py this morning and again here, so the check is now shared
and both are tested against it.
"""
import uuid

import pytest

from korely_agent.db.models import AgentProject, Tenant
from korely_agent.services import projects



class TestSlugs:
    @pytest.mark.parametrize("name,expected", [
        ("Production", "production"),
        ("My Cool App", "my-cool-app"),
        ("  spaces  ", "spaces"),
        ("Ünïcødé", "n-c-d"),
        ("!!!", "project"),
        ("", "project"),
        (None, "project"),
        ("a--b", "a-b"),
    ])
    def test_slugify(self, name, expected):
        assert projects.slugify(name) == expected

    def test_a_clashing_slug_gets_a_suffix(self, db, tenant):
        a = projects.create_project(db, tenant_id=tenant.id, name="My App")
        b = projects.create_project(db, tenant_id=tenant.id, name="my app!")
        assert a.slug == "my-app"
        assert b.slug == "my-app-2"

    def test_the_suffix_keeps_counting(self, db, tenant):
        for _ in range(3):
            projects.create_project(db, tenant_id=tenant.id,
                                    name=f"App {uuid.uuid4().hex[:6]}")
        slugs = {p.slug for p in db.query(AgentProject)
                 .filter(AgentProject.tenant_id == tenant.id).all()}
        assert len(slugs) == 3, "two projects ended up with the same slug"


class TestTheDefault:
    def test_it_is_created_once_and_reused(self, db, tenant):
        a = projects.get_or_create_default_project(db, tenant.id)
        b = projects.get_or_create_default_project(db, tenant.id)
        assert a.id == b.id
        assert a.is_default is True
        assert db.query(AgentProject).filter(
            AgentProject.tenant_id == tenant.id).count() == 1

    def test_each_tenant_gets_its_own(self, db):
        a = Tenant(id=uuid.uuid4(), name="A")
        b = Tenant(id=uuid.uuid4(), name="B")
        db.add_all([a, b])
        db.commit()
        pa = projects.get_or_create_default_project(db, a.id)
        pb = projects.get_or_create_default_project(db, b.id)
        assert pa.id != pb.id
        assert pa.slug == pb.slug == "default", "the slug is per tenant, not global"

    def test_looking_it_up_on_an_empty_account_is_not_an_error(self, db, tenant):
        assert projects.get_default_project(db, tenant.id) is None


class TestWhichRuleWasBroken:
    def test_a_duplicate_name_is_a_duplicate_name(self, db, tenant):
        projects.create_project(db, tenant_id=tenant.id, name="Production")
        with pytest.raises(projects.DuplicateProjectError):
            projects.create_project(db, tenant_id=tenant.id, name="Production")

    def test_the_same_name_under_two_tenants_is_fine(self, db):
        a = Tenant(id=uuid.uuid4(), name="A")
        b = Tenant(id=uuid.uuid4(), name="B")
        db.add_all([a, b])
        db.commit()
        projects.create_project(db, tenant_id=a.id, name="Production")
        projects.create_project(db, tenant_id=b.id, name="Production")

    def test_a_tenant_that_does_not_exist_is_not_a_duplicate_name(self, db):
        """The bug this file exists for, in its second home.

        tenant_id is a foreign key, so a missing tenant raises the same
        IntegrityError a name clash does. Reported as a duplicate, whoever
        mistyped the id goes looking for a collision that is not there.
        """
        with pytest.raises(Exception) as e:
            projects.create_project(db, tenant_id=uuid.uuid4(), name="Production")
        assert not isinstance(e.value, projects.DuplicateProjectError), (
            "a missing tenant was reported as a duplicate project name"
        )

    def test_renaming_onto_a_taken_name_is_a_duplicate(self, db, tenant):
        projects.create_project(db, tenant_id=tenant.id, name="Production")
        staging = projects.create_project(db, tenant_id=tenant.id, name="Staging")
        with pytest.raises(projects.DuplicateProjectError):
            projects.rename_project(db, staging, name="Production")

    def test_a_rename_that_changes_nothing_is_allowed(self, db, tenant):
        p = projects.create_project(db, tenant_id=tenant.id, name="Production")
        projects.rename_project(db, p, description="the live one")
        assert p.name == "Production"
        assert p.slug == "production"

    def test_the_slug_survives_a_rename(self, db, tenant):
        """It is the stable handle: something out there is using it."""
        p = projects.create_project(db, tenant_id=tenant.id, name="Production")
        projects.rename_project(db, p, name="Live")
        assert p.name == "Live"
        assert p.slug == "production"


class TestTheSharedCheck:
    def test_it_says_no_when_it_cannot_tell(self):
        """Defaulting to True would recreate the bug everywhere at once."""
        from sqlalchemy.exc import IntegrityError

        from korely_agent.services.integrity import violated

        err = IntegrityError("stmt", {}, Exception("something else entirely"))
        assert violated(err, "uq_something") is False

    def test_it_reads_the_constraint_name_from_the_driver(self, db, tenant):
        from sqlalchemy.exc import IntegrityError

        from korely_agent.services.integrity import violated

        projects.create_project(db, tenant_id=tenant.id, name="Production")
        try:
            projects.create_project(db, tenant_id=tenant.id, name="Production")
        except projects.DuplicateProjectError as e:
            cause = e.__cause__
            assert isinstance(cause, IntegrityError)
            assert violated(cause, "uq_agent_projects_tenant_name",
                            "uq_agent_projects_tenant_slug")
        else:
            pytest.fail("no duplicate was raised")
