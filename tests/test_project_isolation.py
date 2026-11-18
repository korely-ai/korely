"""The project is a wall, not a label.

Before this module a project grouped keys and nothing else. Memories and facts
sat in one heap and any key on the install read all of them. The dashboard
said so in a warning, but "project" is a word that makes a promise on its own,
and a warning is not access control.

Shown on a real install on 12 September: two projects, one key each, and the
Beta key reads back the sentence written by Alfa.

    memories visible to Beta: 1
     -> Client Alfa's revenue is 2.4 million

These tests are that demonstration, put where it will be tripped over every
time. They cover the three directions in which the wall can be crossed, which
are not equally visible:

  reading     noticed, sooner or later: a row that is not yours shows up
  erasing     not noticed: rows of yours disappear, and nothing is left to see
  contradicting  not noticed: something of yours stops being true, and it
                 looks like the product working
"""
import uuid

import pytest

from korely_agent.core.auth import key_project
from korely_agent.db.models import AgentFact, AgentMemory, AgentProject
from korely_agent.services import facts as agent_facts
from korely_agent.services import keys as key_service
from korely_agent.services.ids import parse_fact_id
from korely_agent.services.memory_engine import AgentMemoryEngine
from korely_agent.services.sentinels import EVERY_PROJECT



def _project(db, tenant, name, default=False):
    p = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name=name,
                     slug=name.lower(), is_default=default)
    db.add(p)
    db.commit()
    return p


@pytest.fixture
def alfa(db, tenant):
    return _project(db, tenant, "Alfa")


@pytest.fixture
def beta(db, tenant):
    return _project(db, tenant, "Beta")


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    """No model: these tests are about who sees what, not about extraction.

    It also keeps the wall from depending on a network call: isolation that
    held only while the provider answered would not be isolation.
    """
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)


class TestReading:
    def test_a_key_does_not_see_another_projects_memories(
            self, db, tenant, alfa, beta):
        AgentMemoryEngine(db, tenant.id, alfa.id).add(
            "Client Alfa's revenue is 2.4 million.", end_user_id="internal")

        rows, total = AgentMemoryEngine(db, tenant.id, beta.id).get_all(
            end_user_id="internal")
        assert rows == [] and total == 0

    def test_and_whoever_wrote_them_does(self, db, tenant, alfa):
        AgentMemoryEngine(db, tenant.id, alfa.id).add("Something of Alfa's.",
                                                      end_user_id="internal")
        rows, total = AgentMemoryEngine(db, tenant.id, alfa.id).get_all(
            end_user_id="internal")
        assert len(rows) == 1 and total == 1

    def test_not_even_by_direct_id(self, db, tenant, alfa, beta):
        """The filter lives in `_base()`, so it holds when the id is known too.

        A memory id is not a secret: it shows up in responses, in the client's
        logs, in webhooks. If the wall held only on listings, anyone with an id
        would get through."""
        m = AgentMemoryEngine(db, tenant.id, alfa.id).add("Alfa's only.")

        # The bare uuid, not `mem_public_id`. The first draft passed that one,
        # and passed for the wrong reason: `_get_row` does not decode the `mem_`
        # prefix, so it returned None on a format error and the test would have
        # stayed green with the wall removed. Found by breaking it on purpose,
        # which is the only way to notice.
        assert AgentMemoryEngine(db, tenant.id, beta.id).get(str(m.id)) is None
        assert AgentMemoryEngine(db, tenant.id, alfa.id).get(str(m.id)) is not None

    def test_facts_follow_the_memorys_project(self, db, tenant, alfa, beta):
        m = AgentMemoryEngine(db, tenant.id, alfa.id).add("x", end_user_id="u1")
        agent_facts.write_fact_direct(
            db, tenant.id, subject="c1", predicate="on_plan", obj="Priority",
            end_user_id="u1", project_id=alfa.id,
        )
        assert m.project_id == alfa.id

        rows, _ = agent_facts.retrieve_agent_facts(
            db, tenant.id, end_user_id="u1", project_id=beta.id)
        assert rows == []
        rows, _ = agent_facts.retrieve_agent_facts(
            db, tenant.id, end_user_id="u1", project_id=alfa.id)
        assert len(rows) == 1


class TestErasing:
    """The direction that leaves nothing to look at afterwards."""

    def test_erasing_an_end_user_does_not_touch_the_other_project(
            self, db, tenant, alfa, beta):
        AgentMemoryEngine(db, tenant.id, alfa.id).add("Alfa's", end_user_id="mario")
        AgentMemoryEngine(db, tenant.id, beta.id).add("Beta's", end_user_id="mario")

        agent_facts.forget_end_user(db, tenant.id, "mario", project_id=beta.id)

        left = db.query(AgentMemory).filter(
            AgentMemory.tenant_id == tenant.id, AgentMemory.project_id == alfa.id).all()
        assert len(left) == 1
        assert left[0].content == "Alfa's"

    def test_deleting_an_agent_does_not_touch_the_other_project(
            self, db, tenant, alfa, beta):
        """The same agent name in two projects is two agents.

        `delete_agent` deletes physically to free a slot under the cap, and a
        name like `support-bot` is one anybody picks."""
        AgentMemoryEngine(db, tenant.id, alfa.id).add("Alfa's", agent_id="support-bot")
        AgentMemoryEngine(db, tenant.id, beta.id).add("Beta's", agent_id="support-bot")

        agent_facts.delete_agent(db, tenant.id, "support-bot", project_id=beta.id)

        left = db.query(AgentMemory).filter(
            AgentMemory.tenant_id == tenant.id, AgentMemory.project_id == alfa.id).all()
        assert len(left) == 1

    def test_forgetting_a_memory_of_another_project_finds_nothing(
            self, db, tenant, alfa, beta):
        m = AgentMemoryEngine(db, tenant.id, alfa.id).add("Alfa's")
        assert agent_facts.forget_memory(db, tenant.id, m.id, project_id=beta.id) is None
        assert agent_facts.forget_memory(db, tenant.id, m.id, project_id=alfa.id) is not None


class TestContradicting:
    """The direction that looks like the product working."""

    @pytest.fixture
    def single_valued_relation(self, db, tenant, monkeypatch):
        """A vocabulary in which `on_plan` holds one value at a time.

        Without this the test proves nothing, and the first draft indeed proved
        nothing: with `get_provider` returning None no relation is ever
        classified, `is_functional` stays NULL, NULL means "never supersede",
        and the two facts both stay standing for a reason that has nothing to
        do with projects. The test passed with the wall removed too.

        Found by breaking the wall on purpose and watching the test stay green.
        Building the cluster and its alias beforehand puts the vocabulary in
        the state in which the supersede would fire, and from there the only
        thing that can stop it is the wall.

        Since 2026-09-27 the one who closes is always the judge, never a rule
        (memory_core.contradiction: Lin 2010, Ritter 2008). Here the judge
        says yes to every candidate it sees, so the only thing that can prevent
        the closing is still the wall, which decides WHICH candidates it sees.
        """
        from korely_agent.db.models import PredicateAlias, PredicateCluster
        from korely_agent.services.facts import contradiction as contra

        monkeypatch.setattr(contra, "_classify", lambda candidates, *a, **k: ([c.id for c in candidates], {}))

        c = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id,
                             canonical="on_plan", is_functional=True,
                             member_count=1)
        db.add(c)
        db.flush()
        db.add(PredicateAlias(tenant_id=tenant.id, surface="on_plan", cluster_id=c.id))
        db.commit()
        return c

    def test_the_supersede_fires_within_the_same_project(
            self, db, tenant, alfa, single_valued_relation):
        """The check on the check: without it, the test below would also be
        satisfied by a product that never supersedes anything."""
        agent_facts.write_fact_direct(
            db, tenant.id, subject="c4821", predicate="on_plan",
            obj="Priority Support plan", end_user_id="c4821", project_id=alfa.id,
        )
        agent_facts.write_fact_direct(
            db, tenant.id, subject="c4821", predicate="on_plan",
            obj="Basic plan", end_user_id="c4821", project_id=alfa.id,
        )
        closed = db.query(AgentFact).filter(
            AgentFact.tenant_id == tenant.id,
            AgentFact.invalid_at.isnot(None)).all()
        assert len(closed) == 1
        assert closed[0].object_label == "Priority Support plan"

    def test_a_write_does_not_close_a_fact_of_another_project(
            self, db, tenant, alfa, beta, single_valued_relation):
        agent_facts.write_fact_direct(
            db, tenant.id, subject="c4821", predicate="on_plan",
            obj="Priority Support plan", end_user_id="c4821", project_id=alfa.id,
        )
        agent_facts.write_fact_direct(
            db, tenant.id, subject="c4821", predicate="on_plan",
            obj="Basic plan", end_user_id="c4821", project_id=beta.id,
        )

        alfas = db.query(AgentFact).filter(
            AgentFact.tenant_id == tenant.id, AgentFact.project_id == alfa.id).all()
        assert len(alfas) == 1
        assert alfas[0].invalid_at is None, (
            "a write in Beta closed a fact of Alfa's")


class TestWhoMustSeeEverything:
    def test_the_operator_sees_the_whole_account(self, db, tenant, alfa, beta):
        """The dashboard is the one thing that stands above the walls, and
        rightly: it is the screen of the install's owner."""
        AgentMemoryEngine(db, tenant.id, alfa.id).add("Alfa's", end_user_id="u")
        AgentMemoryEngine(db, tenant.id, beta.id).add("Beta's", end_user_id="u")

        rows, total = AgentMemoryEngine(db, tenant.id, EVERY_PROJECT).get_all(
            end_user_id="u")
        assert len(rows) == 2 and total == 2


class TestAKeyWithNoProject:
    def test_an_orphan_key_falls_back_to_the_default_not_to_everything(
            self, db, tenant, alfa):
        """Deleting a project leaves its keys with no project, because the
        constraint is SET NULL: a live key does not disappear from under a
        running agent. That key has to fall back to the default project.

        The direction of the mistake is everything. A key that sees too little
        makes an agent fail and somebody fixes it in a minute. A key that sees
        too much is a data leak nobody reports, because nothing looks broken."""
        default = _project(db, tenant, "Default", default=True)
        row, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="orphan")
        # Orphaned by hand. A key is no longer born like this: since 2026-09-13
        # the default project is set where keys are born, precisely because
        # NULL on a live key had become a wrong address rather than an absence.
        # The case remains possible only for keys created before, and through
        # the SET NULL constraint if somebody deletes a project outside the
        # service.
        row.project_id = None
        db.commit()

        assert key_project(db, row) == default.id
        assert key_project(db, row) is not EVERY_PROJECT

    def test_a_key_with_its_project_stays_there(self, db, tenant, alfa):
        _project(db, tenant, "Default", default=True)
        row, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                            project_id=alfa.id)
        assert key_project(db, row) == alfa.id


class TestTheSemanticPath:
    """The door in the wall I had left open.

    `_stage1_candidates` looks for candidates in three ways. Two go through
    `_base()`, and the wall was there. The third, the semantic Stage 1b, needs
    `db.query(AgentFact, dist)` to order by distance: a different shape, so it
    had built its own chain of filters, with everything in it but the project.

    Found by a tester who had never seen this code, by writing a contradicting
    fact from another project:

        revenue "2.4 million"  project=Alfa  closed
        revenue "5 million"    project=Beta  open

    The break-tests could not catch it, and that is the part worth
    remembering: I had broken the walls I knew about and watched the right
    tests turn red, and concluded there was one wall. Stage 1b never runs on
    SQLite, so in a suite running on SQLite that branch is invisible by
    construction. This suite runs on the Postgres the product actually runs
    on, and so it can see it.
    """

    @pytest.fixture
    def vector(self):
        from korely_agent.core.llm import EMBEDDING_DIM
        return [0.05] * EMBEDDING_DIM

    def _fact(self, db, tenant, project, subject, obj, vec):
        from korely_agent.db.models import AgentFact

        f = AgentFact(
            id=uuid.uuid4(), tenant_id=tenant.id, project_id=project.id,
            end_user_id="u1", subject_label=subject, subject_type="unknown",
            predicate_raw="revenue", predicate_canonical="revenue",
            object_label=obj, object_is_literal=True, confidence=0.9,
            embedding=vec,
        )
        db.add(f)
        db.commit()
        return f

    def test_a_fact_of_another_project_does_not_become_a_candidate(
            self, db, tenant, alfa, beta, vector):
        """The subject is different on purpose.

        With the same subject the structural path would find the candidate too,
        and the test would not say which of the two it came through. With
        different subjects the only path that can find it is the semantic one,
        and if it arrives, that is where it came in.
        """
        from korely_agent.services.facts.contradiction import _stage1_candidates

        self._fact(db, tenant, alfa, "client-alfa", "2.4 million", vector)

        candidates = _stage1_candidates(
            db, tenant.id, "u1", "Beta's client", "revenue",
            project_id=beta.id, exclude_id=uuid.uuid4(), new_embedding=vector,
        )
        assert candidates == [], (
            "a fact of Alfa's became a candidate for a write in Beta")

    def test_and_within_the_same_project_it_does(
            self, db, tenant, alfa, vector):
        """The check on the check.

        Without it, the test above would also be satisfied by a semantic path
        that is broken, switched off, or never finds anything: exactly the way
        two tests in this file had already passed for the wrong reason.
        """
        from korely_agent.services.facts.contradiction import _stage1_candidates

        f = self._fact(db, tenant, alfa, "client-alfa", "2.4 million", vector)

        candidates = _stage1_candidates(
            db, tenant.id, "u1", "Alfa's client", "revenue",
            project_id=alfa.id, exclude_id=uuid.uuid4(), new_embedding=vector,
        )
        assert [c.id for c in candidates] == [f.id]


class TestDeletingAProject:
    """What a project held has to stay reachable afterwards.

    The constraint is ON DELETE SET NULL, which was the right shape while a
    project was a label: nothing was lost, the rows just stopped being filed
    under anything. Since the project became a wall, NULL does not mean
    "unfiled", it is an address, and the keys were being sent to another one:
    `key_project` resolves a key with no project to the default project's
    concrete id, so after the delete the key looked for that id and the rows
    carried NULL.

    Found by a tester who deleted a project and then tried to erase the data
    that had been in it:

        delete_all(user_id="g-user")
        -> memories_forgotten=0, facts_invalidated=0, audit_id=...

    while in the database one memory and three facts were still alive,
    readable by nobody and erasable by nobody.

    Not a leak: nothing became visible to another project. Worse in a
    different direction, for a product that promises an erasure that really
    erases and offers the audit trail as proof.
    """

    def test_the_memories_follow_the_keys_into_the_default_project(
            self, db, tenant):
        from korely_agent.core.auth import key_project
        from korely_agent.services import keys as key_service
        from korely_agent.services import projects as project_service

        default = project_service.get_or_create_default_project(db, tenant.id)
        gamma = _project(db, tenant, "Gamma")
        key, _ = key_service.create_api_key(db, tenant_id=tenant.id,
                                            name="g", project_id=gamma.id)
        m = AgentMemoryEngine(db, tenant.id, gamma.id).add("something of Gamma's",
                                                           end_user_id="g-user")
        agent_facts.write_fact_direct(
            db, tenant.id, subject="Ada", predicate="leads", obj="Gamma",
            end_user_id="g-user", project_id=gamma.id)
        db.commit()

        project_service.delete_project(db, tenant.id, gamma)
        db.expire_all()

        # The key and the memory must have ended up in the same place.
        assert key_project(db, key) == default.id
        assert db.get(AgentMemory, m.id).project_id == default.id
        facts = db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).all()
        assert [f.project_id for f in facts] == [default.id]

    def test_so_the_key_reads_them_again_and_can_erase_them(
            self, db, tenant):
        """The proof that counts: the tester's whole round.

        The column being right is not enough. The key that came out of it has
        to see that data and manage to erase it, because the defect was exactly
        an erasure that answered well about data that stayed.
        """
        from korely_agent.core.auth import key_project
        from korely_agent.services import keys as key_service
        from korely_agent.services import projects as project_service

        project_service.get_or_create_default_project(db, tenant.id)
        gamma = _project(db, tenant, "Gamma")
        key, _ = key_service.create_api_key(db, tenant_id=tenant.id,
                                            name="g", project_id=gamma.id)
        AgentMemoryEngine(db, tenant.id, gamma.id).add("something of Gamma's",
                                                       end_user_id="g-user")
        agent_facts.write_fact_direct(
            db, tenant.id, subject="Ada", predicate="leads", obj="Gamma",
            end_user_id="g-user", project_id=gamma.id)
        db.commit()

        project_service.delete_project(db, tenant.id, gamma)
        db.expire_all()

        after = key_project(db, key)
        rows, total = AgentMemoryEngine(db, tenant.id, after).get_all(
            end_user_id="g-user")
        assert total == 1, "the memory became unreachable"

        outcome = agent_facts.forget_end_user(db, tenant.id, "g-user",
                                              project_id=after)
        assert outcome["memories_deleted"] == 1, (
            "the erasure answered well about data that stayed")
        assert db.query(AgentMemory).filter(
            AgentMemory.tenant_id == tenant.id).count() == 0
        assert db.query(AgentFact).filter(
            AgentFact.tenant_id == tenant.id).count() == 0


class TestReallyErasing:
    """The irreversible road is asked for, not taken by mistake.

    The two products did opposite things behind the same button: self-hosted
    moved, hosted erased. Now they do the same thing, and that thing is moving.

    What decided which to keep is which of the two mistakes can be undone. If
    I move and you wanted to erase, you can still erase. If I erase and you
    wanted to keep, it does not come back.
    """

    def test_without_asking_nothing_is_erased(self, db, tenant, alfa):
        from korely_agent.services import projects as project_service

        project_service.get_or_create_default_project(db, tenant.id)
        m = AgentMemoryEngine(db, tenant.id, alfa.id).add("something", end_user_id="u")
        agent_facts.write_fact_direct(
            db, tenant.id, subject="a", predicate="likes", obj="b",
            end_user_id="u", project_id=alfa.id)
        db.commit()

        outcome = project_service.delete_project(db, tenant.id, alfa)
        db.expire_all()

        assert outcome["memories_deleted"] == 0
        assert db.get(AgentMemory, m.id).deleted_at is None
        assert db.query(AgentFact).filter(
            AgentFact.invalid_at.isnot(None)).count() == 0

    def test_asking_for_it_erases(self, db, tenant, alfa):
        """Erased means gone from the table, as the end-user erasure has
        meant since 2026-09-08 (README: "erasure that erases").

        Until 2026-09-28 this asserted a soft forget: the memory marked
        deleted and still in the default project, the fact closed, both still
        there with their text, and the closed fact readable by any key of the
        default project with `include_invalidated=true`. The forgotten memory
        and the fact already closed before the erasure are erased too: they
        hold the same text."""
        from korely_agent.db.models import AgentBatchJob, AgentEntityAlias
        from korely_agent.services import batch as batch_service
        from korely_agent.services import projects as project_service

        default = project_service.get_or_create_default_project(db, tenant.id)
        eng = AgentMemoryEngine(db, tenant.id, alfa.id)
        m = eng.add("Maria likes tea", end_user_id="u")
        gone_before = eng.add("Maria liked coffee", end_user_id="u")
        eng.delete(str(gone_before.id))
        first = agent_facts.write_fact_direct(
            db, tenant.id, subject="Maria", predicate="likes", obj="coffee",
            end_user_id="u", project_id=alfa.id)
        agent_facts.forget_fact(db, tenant.id, parse_fact_id(first["id"]), project_id=EVERY_PROJECT)
        agent_facts.write_fact_direct(
            db, tenant.id, subject="Maria", predicate="likes", obj="tea",
            end_user_id="u", project_id=alfa.id)
        db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=alfa.id, end_user_id="u",
                                alias_lower="mary", alias_label="Mary",
                                canonical_label="Maria"))
        batch_service.enqueue_batch(db, tenant.id, [{"content": "Maria likes tea"}],
                                    project_id=alfa.id)
        db.commit()
        # Something in the default project, which the erasure must not touch.
        AgentMemoryEngine(db, tenant.id, default.id).add("kept", end_user_id="u")
        db.commit()

        m_id, gone_before_id = m.id, gone_before.id
        outcome = project_service.delete_project(db, tenant.id, alfa, erase=True)
        db.expire_all()

        assert db.get(AgentMemory, m_id) is None, "the memory is still in the table"
        assert db.get(AgentMemory, gone_before_id) is None
        assert db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).count() == 0, (
            "a fact is still in the table, readable with include_invalidated=true")
        assert db.query(AgentBatchJob).filter(AgentBatchJob.tenant_id == tenant.id).count() == 0, (
            "a batch payload is a copy of the memories, and a queued one would "
            "import them again into the default project")
        assert db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant.id).count() == 0
        assert (outcome["memories_deleted"], outcome["facts_deleted"]) == (2, 2)
        assert outcome["batches_deleted"] == 1
        # 0.2.0: the first names are gone.
        assert "memories_forgotten" not in outcome and "facts_invalidated" not in outcome
        # The default project's own data is untouched.
        (kept,) = db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id).all()
        assert (kept.content, kept.project_id) == ("kept", default.id)

    def test_nothing_erased_is_readable_through_the_api(self, db, tenant, alfa):
        """What a buyer checks: read back with include_invalidated=true, count."""
        from fastapi.testclient import TestClient

        from korely_agent.db import get_db
        from korely_agent.main import create_app
        from korely_agent.services import projects as project_service

        project_service.get_or_create_default_project(db, tenant.id)
        _key, full = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                               project_id=alfa.id)
        first = agent_facts.write_fact_direct(
            db, tenant.id, subject="Maria", predicate="likes", obj="coffee",
            end_user_id="u", project_id=alfa.id)
        agent_facts.forget_fact(db, tenant.id, parse_fact_id(first["id"]), project_id=EVERY_PROJECT)
        db.commit()

        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        _admin, admin_key = key_service.create_api_key(
            db, tenant_id=tenant.id, name="op",
            scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
        with TestClient(app) as c:
            # The control: before, the closed fact is there to read.
            before = c.get("/v1/facts", params={"user_id": "u", "include_invalidated": "true"},
                           headers={"Authorization": f"Bearer {full}"})
            assert before.json()["total"] == 1, before.text
            r = c.delete(f"/admin/projects/{alfa.id}", params={"erase": "true"},
                         headers={"Authorization": f"Bearer {admin_key}"})
            assert r.status_code == 200, r.text
            # A request is a session of its own in production; here one session
            # serves both, and without this the key would still carry the
            # project it had before the DELETE moved it, and read nothing for
            # the wrong reason.
            db.expire_all()
            # The key moved to the default project; it reads nothing back.
            got = c.get("/v1/facts", params={"user_id": "u", "include_invalidated": "true"},
                        headers={"Authorization": f"Bearer {full}"})
            assert got.status_code == 200, got.text
            assert got.json()["total"] == 0, "the erased fact is still readable"
            body = r.json()
            assert (body["facts_deleted"], body["erasure"]) == (1, "permanent")
        app.dependency_overrides.clear()

    def test_asking_for_it_leaves_a_row_in_the_erasure_audit(self, db, tenant, alfa):
        """The docstring promised "a row in the erasure audit" and none was
        written, so this was the one erasure nobody could show had happened.

        Filed the way the other erasures file theirs (forget_end_user,
        delete_agent): counts, never content. Its project is the one the keys
        read afterwards, the default one, since every audit row the deleted
        project held moves there too; the erased project is named in
        `target_id`, because its own row is gone.
        """
        from korely_agent.db.models import AgentForgetAudit
        from korely_agent.services import projects as project_service
        from korely_agent.services.ids import audit_public_id

        default = project_service.get_or_create_default_project(db, tenant.id)
        AgentMemoryEngine(db, tenant.id, alfa.id).add("something", end_user_id="u")
        agent_facts.write_fact_direct(
            db, tenant.id, subject="a", predicate="likes", obj="b",
            end_user_id="u", project_id=alfa.id)
        db.commit()
        alfa_id = alfa.id

        outcome = project_service.delete_project(db, tenant.id, alfa, erase=True)
        db.expire_all()

        (row,) = db.query(AgentForgetAudit).filter(
            AgentForgetAudit.tenant_id == tenant.id).all()
        assert row.scope == "project"
        assert row.target_id == str(alfa_id)
        assert row.project_id == default.id
        assert row.end_user_id is None
        assert (row.memories_forgotten, row.facts_invalidated) == (1, 1)
        assert outcome["audit_id"] == audit_public_id(row.id)

    def test_moving_leaves_no_erasure_row(self, db, tenant, alfa):
        from korely_agent.db.models import AgentForgetAudit
        from korely_agent.services import projects as project_service

        project_service.get_or_create_default_project(db, tenant.id)
        AgentMemoryEngine(db, tenant.id, alfa.id).add("something", end_user_id="u")
        db.commit()

        outcome = project_service.delete_project(db, tenant.id, alfa)
        db.expire_all()

        assert outcome["audit_id"] is None
        # Only the record of the move, which a restore makes again (audit
        # of 20 Oct 2026); no erasure.
        assert [a.scope for a in db.query(AgentForgetAudit).filter(
            AgentForgetAudit.tenant_id == tenant.id)] == ["move"]

    def test_the_keys_move_in_both_cases(self, db, tenant, alfa):
        """A live key does not disappear from under a running agent, whatever
        happens to the data."""
        from korely_agent.core.auth import key_project
        from korely_agent.services import keys as key_service
        from korely_agent.services import projects as project_service

        default = project_service.get_or_create_default_project(db, tenant.id)
        key, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="k",
                                            project_id=alfa.id)
        db.commit()
        project_service.delete_project(db, tenant.id, alfa, erase=True)
        db.expire_all()
        assert key_project(db, key) == default.id


def test_every_read_and_erasure_of_facts_names_its_project():
    """Audit of 17 Oct 2026: each defaulted to EVERY_PROJECT, "no
    wall", and /admin relied on it; a new door reached with a key that
    forgot the argument would have crossed the wall in silence. Required by
    name, forgetting it is a TypeError at the call."""
    import inspect

    from korely_agent.services.facts import service

    for name in ("facts_for_memory", "forget_memory", "forget_end_user", "delete_agent",
                 "load_fact", "forget_fact", "correct_fact", "retrieve_agent_facts",
                 "memory_timeline"):
        p = inspect.signature(getattr(service, name)).parameters["project_id"]
        assert p.kind is p.KEYWORD_ONLY and p.default is p.empty, name


def test_the_project_wall_is_written_once():
    """`within_project` is the one rule (services/sentinels.py); a copy of it
    written out by hand can drift from it (audit of 17 Oct 2026: the
    dialogue order's query had one)."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "korely_agent"
    copies = [p.relative_to(root).as_posix() for p in sorted(root.rglob("*.py"))
              if "memory_core" not in p.parts and p.name != "sentinels.py"
              and re.search(r"project_id\.is_\(None\) if \w+ is None", p.read_text())]
    assert copies == []


def test_the_project_of_a_key_is_the_keys_domain():
    """Audit of 17 Oct 2026: services/audit.py reached into core/auth, the
    FastAPI dependencies, for `key_project`. It lives in services/keys.py;
    core/auth keeps the name for the routes, and it is the same function."""
    from pathlib import Path

    from korely_agent.core import auth
    from korely_agent.services import keys

    assert auth.key_project is keys.key_project
    services = Path(__file__).resolve().parents[1] / "korely_agent" / "services"
    assert [p.name for p in sorted(services.rglob("*.py"))
            if "core.auth import key_project" in p.read_text()] == []
