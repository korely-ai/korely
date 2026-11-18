"""What the operator looks at: memories, facts, agents, requests.

These delegate to the same engine `/v1` uses, so the behaviour is tested there.
What is not tested there is the thing that makes them a separate door: they
authenticate a person rather than a program, and a person on one account must
not be able to read another's. One install holds several of an agency's
clients, and a route that forgets the filter hands one of them the others.
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from tests.conftest import seeded_project_id
from korely_agent.db.models import AgentFact, AgentMemory, Tenant
from korely_agent.services import keys as key_service



@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def operator_key(db, tenant):
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}


@pytest.fixture
def mine(db, tenant):
    return operator_key(db, tenant)


@pytest.fixture
def theirs(db):
    other = Tenant(id=uuid.uuid4(), name="Somebody Else")
    db.add(other)
    db.commit()
    return other, operator_key(db, other)


def memory(db, tenant_id, content="Maria is on the Priority plan.", agent="support-bot"):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant_id, end_user_id="c4821",
                    project_id=seeded_project_id(db, tenant_id),
                    agent_id=agent, content=content)
    db.add(m)
    db.commit()
    return m


def fact(db, tenant_id, obj="Priority plan", memory_id=None):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant_id, end_user_id="c4821",
                  project_id=seeded_project_id(db, tenant_id),
                  subject_label="Maria", predicate_raw="is on",
                  predicate_canonical="is_on", object_label=obj,
                  source_memory_id=memory_id,
                  valid_from=datetime(2026, 3, 15, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    return f


class TestIsolation:
    """One assertion per route, and it is the same assertion."""

    def test_memories(self, db, tenant, client, mine, theirs):
        other, their_key = theirs
        memory(db, tenant.id, "MINEMARKER")
        memory(db, other.id, "THEIRMARKER")

        ours = client.get("/admin/memories", headers=mine).json()
        assert [m["content"] for m in ours["memories"]] == ["MINEMARKER"]
        assert ours["total"] == 1

        listing = client.get("/admin/memories", headers=their_key).json()
        assert [m["content"] for m in listing["memories"]] == ["THEIRMARKER"]

    def test_one_memory_by_id(self, db, tenant, client, mine, theirs):
        other, their_key = theirs
        m = memory(db, tenant.id)
        from korely_agent.services.ids import mem_public_id

        assert client.get(f"/admin/memories/{mem_public_id(m.id)}", headers=mine).status_code == 200
        assert client.get(f"/admin/memories/{mem_public_id(m.id)}",
                          headers=their_key).status_code == 404

    def test_deleting_somebody_elses_memory(self, db, tenant, client, theirs):
        other, their_key = theirs
        m = memory(db, tenant.id)
        from korely_agent.services.ids import mem_public_id

        assert client.delete(f"/admin/memories/{mem_public_id(m.id)}",
                             headers=their_key).status_code == 404
        db.expire_all()
        assert db.query(AgentMemory).filter(AgentMemory.id == m.id).one().deleted_at is None

    def test_facts(self, db, tenant, client, mine, theirs):
        other, their_key = theirs
        fact(db, tenant.id, obj="MINEPLAN")
        fact(db, other.id, obj="THEIRPLAN")

        ours = client.get("/admin/facts", headers=mine).json()
        assert [f["object"] for f in ours["facts"]] == ["MINEPLAN"]
        assert client.get("/admin/facts", headers=their_key).json()["facts"][0]["object"] == "THEIRPLAN"

    def test_namespaces(self, db, tenant, client, mine, theirs):
        other, their_key = theirs
        memory(db, tenant.id, agent="ZZMINE")
        memory(db, other.id, agent="ZZTHEIRS")

        assert [a["agent_id"] for a in client.get("/admin/namespaces", headers=mine).json()["agents"]] == ["ZZMINE"]
        assert [a["agent_id"] for a in client.get("/admin/namespaces", headers=their_key).json()["agents"]] == ["ZZTHEIRS"]

    def test_the_memories_without_an_agent_are_a_namespace(self, db, tenant, client, mine):
        """The page's "Default namespace" row: /admin/namespaces dropped the
        null agent_id, so it never appeared (audit of 17 Oct 2026)."""
        memory(db, tenant.id, agent=None)
        memory(db, tenant.id, agent=None, content="Maria moved to Lyon.")
        memory(db, tenant.id, agent="ZZMINE")
        rows = client.get("/admin/namespaces", headers=mine).json()["agents"]
        assert [(a["agent_id"], a["memories"]) for a in rows] == [(None, 2), ("ZZMINE", 1)]

    def test_requests(self, db, tenant, client, mine, theirs):
        other, their_key = theirs
        from korely_agent.services import audit

        audit.record(db, tenant.id, audit.READ, target_id="ZZMINE")
        audit.record(db, other.id, audit.READ, target_id="ZZTHEIRS")

        ours = client.get("/admin/requests", headers=mine).json()
        assert all(r["target_id"] != "ZZTHEIRS" for r in ours["requests"])

    def test_categories(self, db, tenant, client, mine, theirs):
        other, their_key = theirs
        m = memory(db, tenant.id)
        fact(db, tenant.id, memory_id=m.id)

        assert client.get("/admin/memories/categories", headers=mine).json() != []
        assert client.get("/admin/memories/categories", headers=their_key).json() == []

    def test_an_agent_key_reaches_none_of_them(self, db, tenant, client):
        _r, plain = key_service.create_api_key(db, tenant_id=tenant.id, name="agent")
        h = {"Authorization": f"Bearer {plain}"}
        for path in ("/admin/memories", "/admin/facts", "/admin/namespaces",
                     "/admin/requests", "/admin/memories/categories"):
            assert client.get(path, headers=h).status_code == 403, path


class TestSearchDoesNotLieWhenItCannotRun:
    def test_a_broken_retriever_is_a_503_not_an_empty_page(self, db, tenant, client, mine, monkeypatch):
        """"You have no memories about this" is a statement about their data.
        Making it out of our failure, with a 200 on it, is the shape of every
        defect this codebase keeps finding."""
        from korely_agent.services import memory_engine

        monkeypatch.setattr(memory_engine, "embed", lambda *a, **k: None)
        memory(db, tenant.id)

        r = client.get("/admin/memories/search?query=anything", headers=mine)
        assert r.status_code == 503
        assert "[]" not in r.text

    def test_a_working_one_answers(self, db, tenant, client, mine, monkeypatch):
        """The control. Without it the test above passes on a search that is
        broken for some entirely different reason."""
        from korely_agent.services import memory_engine

        monkeypatch.setattr(memory_engine, "embed", lambda *a, **k: [0.0] * 768)
        memory(db, tenant.id)
        assert client.get("/admin/memories/search?query=plan", headers=mine).status_code == 200


class TestShapes:
    def test_history_is_a_timeline_in_the_shape_the_screen_reads(self, db, tenant, client, mine):
        """`{id, events}`, the same shape `/v1` returns, from the same code.

        It used to be `{facts: [...]}` here and `{id, events}` there: two
        implementations of one thing, and the page that read the second
        crashed on the first with `undefined.filter`. An endpoint that exists
        and answers 200 in the wrong shape is a blank screen with a green test
        behind it.
        """
        from korely_agent.services.ids import mem_public_id

        m = memory(db, tenant.id)
        fact(db, tenant.id, memory_id=m.id)
        out = client.get(f"/admin/memories/{mem_public_id(m.id)}/history",
                         headers=mine).json()

        assert out["id"] == mem_public_id(m.id)
        kinds = [e["event"] for e in out["events"]]
        assert "created" in kinds
        assert "fact_extracted" in kinds
        extracted = [e for e in out["events"] if e["event"] == "fact_extracted"][0]
        assert "Priority plan" in extracted["fact"]

    def test_the_two_doors_return_the_same_timeline(self, db, tenant, client, mine):
        """One implementation, so they cannot drift apart again."""
        from korely_agent.services import keys as ks
        from korely_agent.services.ids import mem_public_id

        m = memory(db, tenant.id)
        fact(db, tenant.id, memory_id=m.id)
        _r, agent = ks.create_api_key(db, tenant_id=tenant.id, name="agent")

        a = client.get(f"/admin/memories/{mem_public_id(m.id)}/history", headers=mine).json()
        b = client.get(f"/v1/memories/{mem_public_id(m.id)}/history",
                       headers={"Authorization": f"Bearer {agent}"}).json()
        assert [e["event"] for e in a["events"]] == [e["event"] for e in b["events"]]

    def test_as_of_works_on_the_operator_door_too(self, db, tenant, client, mine):
        """The moat is not a `/v1` feature. A person looking at the screen has
        to be able to ask the same question."""
        f = fact(db, tenant.id)
        f.invalid_at = datetime(2026, 6, 20, tzinfo=timezone.utc)
        db.commit()

        now = client.get("/admin/facts", headers=mine).json()
        assert now["facts"] == []
        then = client.get("/admin/facts?as_of=2026-04-01", headers=mine).json()
        assert len(then["facts"]) == 1

    def test_memories_come_in_the_shape_the_screen_reads(self, db, tenant, client, mine,
                                                         monkeypatch):
        """The Memories page and the Playground read /v1's shape (`user_id`,
        `client`, `categories`, and `facts` in the drawer); these routes
        answered the engine's dict, `end_user_id` and no facts, so the End
        user column was always empty, the Playground's end-user filter
        dropped every hit and the drawer read `undefined.length` (audit of
        17 Oct 2026)."""
        from korely_agent.services import memory_engine
        from korely_agent.services.ids import mem_public_id

        from korely_agent.core.embedding_model import DIMENSIONS
        from korely_agent.services import vector_state

        m = memory(db, tenant.id)
        m.client = "Claude Code"
        m.embedding, m.embedding_status = [0.1] * DIMENSIONS, vector_state.DONE
        db.commit()
        f = fact(db, tenant.id, memory_id=m.id)
        memory(db, tenant.id, content="Nothing came of this one.")

        listed = client.get("/admin/memories", headers=mine).json()["memories"]
        row = next(r for r in listed if r["id"] == mem_public_id(m.id))
        assert row["user_id"] == "c4821" and "end_user_id" not in row
        assert row["client"] == "Claude Code"
        one = client.get(f"/admin/memories/{mem_public_id(m.id)}", headers=mine).json()
        assert [x["object"] for x in one["facts"]] == [f.object_label]
        assert row["categories"] == [one["facts"][0]["predicate_family"]]
        assert next(r for r in listed if r["id"] != row["id"])["categories"] == []
        assert one["user_id"] == "c4821" and one["client"] == "Claude Code"

        monkeypatch.setattr(memory_engine, "embed", lambda *a, **k: [0.1] * DIMENSIONS)
        hits = client.get("/admin/memories/search?query=plan", headers=mine).json()
        assert [h["id"] for h in hits] == [row["id"]]
        assert hits[0]["user_id"] == "c4821" and hits[0]["client"] == "Claude Code"
        assert hits[0]["categories"] == row["categories"]

    def test_a_mistyped_memory_id_is_a_404(self, client, mine):
        assert client.get("/admin/memories/not-an-id", headers=mine).status_code == 404
        assert client.get("/admin/memories/not-an-id/history", headers=mine).status_code == 404


class TestTheScreensDeleteIsTheForget:
    """DELETE /admin/memories/:id is the same act as DELETE /v1/memories/:id
    (the audit of 2026-10-06, row 2): it set `deleted_at` only, and the agent
    kept receiving the facts of a sentence the operator had deleted."""

    def test_the_facts_only_it_stated_stop_being_true(self, db, tenant, client, mine):
        from korely_agent.db.models import AgentForgetAudit, AuditEvent
        from korely_agent.services.ids import mem_public_id

        m = memory(db, tenant.id)
        f = fact(db, tenant.id, memory_id=m.id)
        r = client.delete(f"/admin/memories/{mem_public_id(m.id)}", headers=mine)
        assert r.status_code == 200 and r.json() == {"deleted": True, "id": mem_public_id(m.id)}
        db.expire_all()
        assert db.get(AgentMemory, m.id).deleted_at is not None
        assert db.get(AgentFact, f.id).invalid_at is not None
        forgot = db.query(AgentForgetAudit).one()
        assert (forgot.scope, forgot.facts_invalidated) == ("memory", 1)
        # A forget, filed as one: its text stays (`erase` until 1.0.23).
        ev = db.query(AuditEvent).filter(AuditEvent.action == "forget").one()
        assert (ev.actor, ev.end_user_id, ev.target_id) == ("rest", "c4821", mem_public_id(m.id))
        assert ev.project_id == m.project_id

    def test_a_fact_another_memory_states_stays_true(self, db, tenant, client, mine):
        from korely_agent.services.ids import mem_public_id

        m, other = memory(db, tenant.id), memory(db, tenant.id, content="Maria said it again.")
        f = fact(db, tenant.id, memory_id=m.id)
        f.source_memory_ids = [str(m.id), str(other.id)]
        db.commit()
        client.delete(f"/admin/memories/{mem_public_id(m.id)}", headers=mine)
        db.expire_all()
        assert db.get(AgentFact, f.id).invalid_at is None

    def test_deleted_twice_is_a_404(self, db, tenant, client, mine):
        from korely_agent.services.ids import mem_public_id

        m = memory(db, tenant.id)
        assert client.delete(f"/admin/memories/{mem_public_id(m.id)}", headers=mine).status_code == 200
        assert client.delete(f"/admin/memories/{mem_public_id(m.id)}", headers=mine).status_code == 404


class TestThePagesFilters:
    """The Memories page's 1d / 7d / 30d window and its "no agent" choice
    reached a route that read neither (the audit of 2026-10-06, row 10), and
    its categories used another family rule than the Facts page (row 11)."""

    def test_days_is_a_window(self, db, tenant, client, mine):
        from datetime import timedelta

        old = memory(db, tenant.id, content="from two months ago")
        old.created_at = datetime.now(timezone.utc) - timedelta(days=60)
        db.commit()
        memory(db, tenant.id, content="from today")
        got = client.get("/admin/memories", params={"days": 1}, headers=mine).json()
        assert [m["content"] for m in got["memories"]] == ["from today"]
        assert got["total"] == 1

    def test_a_window_past_any_date_is_refused_not_a_500(self, db, tenant, client, mine):
        """`days` became a timedelta taken from now with no bound, and a large
        one raised OverflowError: a 500 for a valid integer (audit of 17 Oct
        2026). The widest window is every memory, as 0 is."""
        memory(db, tenant.id, content="kept")
        for days in (1000000000, 999999999, -1):
            r = client.get("/admin/memories", params={"days": days}, headers=mine)
            assert r.status_code == 422, (days, r.status_code)
        widest = client.get("/admin/memories", params={"days": 36500}, headers=mine).json()
        everything = client.get("/admin/memories", params={"days": 0}, headers=mine).json()
        assert widest["total"] == everything["total"] == 1

    def test_none_is_the_memories_with_no_agent(self, db, tenant, client, mine):
        memory(db, tenant.id, content="no agent", agent=None)
        memory(db, tenant.id, content="the bot's", agent="bot-1")
        got = client.get("/admin/memories", params={"agent_id": "__none__"}, headers=mine).json()
        assert [m["content"] for m in got["memories"]] == ["no agent"]

    def test_a_learned_relation_is_under_its_stored_family_everywhere(self, db, tenant, client,
                                                                     mine):
        m = memory(db, tenant.id, content="Maria adores jazz.")
        f = fact(db, tenant.id, obj="jazz", memory_id=m.id)
        f.predicate_canonical, f.predicate_family = "adores", "preferences"
        db.commit()
        cats = client.get("/admin/memories/categories", headers=mine).json()
        assert {c["family"]: c["count"] for c in cats} == {"preferences": 1}
        got = client.get("/admin/memories", params={"family": "preferences"}, headers=mine).json()
        assert [x["content"] for x in got["memories"]] == ["Maria adores jazz."]


# ── the screen reads what it says it shows (audit of 17 Oct 2026) ───────────

def test_the_playgrounds_facts_are_the_active_projects(client, db, tenant, mine):
    from korely_agent.services import projects

    staging = projects.create_project(db, tenant_id=tenant.id, name="staging")
    for pid, obj in ((seeded_project_id(db, tenant.id), "Priority plan"), (staging.id, "Basic plan")):
        db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                         subject_label="maria", predicate_raw="on plan",
                         predicate_canonical="on_plan", object_label=obj,
                         valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    db.commit()
    every = client.get("/admin/facts", params={"user_id": "maria"}, headers=mine).json()
    assert every["total"] == 2
    one = client.get("/admin/facts", params={"user_id": "maria", "project_id": str(staging.id)},
                     headers=mine).json()
    assert [f["object"] for f in one["facts"]] == ["Basic plan"]
    assert client.get("/admin/facts", params={"project_id": str(uuid.uuid4())},
                      headers=mine).status_code == 404


def test_a_project_of_another_account_is_not_read(client, db, tenant, theirs):
    from korely_agent.services import projects

    other, their_key = theirs
    mine_project = projects.create_project(db, tenant_id=tenant.id, name="mine")
    r = client.get("/admin/facts", params={"project_id": str(mine_project.id)}, headers=their_key)
    assert r.status_code == 404


def test_every_request_row_says_write_or_read_as_the_summary_counts(client, db, tenant, mine):
    from korely_agent.services import audit

    for action, endpoint in ((audit.ERASE, "facts:forget"), (audit.FACT_WRITE, "facts:correct"),
                             (audit.READ, "memories:search")):
        audit.record(db, tenant.id, action, actor="rest", meta={"endpoint": endpoint})
    rows = {r["endpoint"]: r["kind"] for r in
            client.get("/admin/requests", headers=mine).json()["requests"]}
    assert rows == {"facts:forget": "write", "facts:correct": "write", "memories:search": "read"}
    summary = client.get("/admin/requests/summary", headers=mine).json()
    assert (summary["writes"], summary["reads"]) == (2, 1)


class TestAMemorysClosedFacts:
    """A memory whose fact a later one superseded: the drawer's Details said
    "Facts extracted (0)" (GET /admin/memories/{id} answered the facts true
    now only) while its Timeline showed the fact, and the Timeline's
    "Superseded a fact" had no fact under it (functional test of 28 Oct
    2026)."""

    def test_the_memory_answers_its_closed_facts_marked_closed(self, db, tenant, client, mine):
        from korely_agent.services.ids import fact_public_id, mem_public_id

        m = memory(db, tenant.id, content="Luca works at Acme.")
        old = fact(db, tenant.id, obj="Acme", memory_id=m.id)
        later = fact(db, tenant.id, obj="Globex", memory_id=memory(db, tenant.id).id)
        old.invalid_at, old.invalidated_by_id = datetime(2026, 6, 1, tzinfo=timezone.utc), later.id
        mine_too = fact(db, tenant.id, obj="Priority plan", memory_id=m.id)
        db.commit()

        one = client.get(f"/admin/memories/{mem_public_id(m.id)}", headers=mine).json()
        by_id = {f["id"]: f for f in one["facts"]}
        assert set(by_id) == {fact_public_id(old.id), fact_public_id(mine_too.id)}
        assert by_id[fact_public_id(old.id)]["closed"] is True
        assert by_id[fact_public_id(old.id)]["invalidated_by"] == fact_public_id(later.id)
        assert by_id[fact_public_id(mine_too.id)]["closed"] is False

    def test_an_end_still_to_come_is_not_closed(self, db, tenant, client, mine):
        from korely_agent.services.ids import mem_public_id

        m = memory(db, tenant.id)
        f = fact(db, tenant.id, memory_id=m.id)
        f.invalid_at = datetime(3026, 1, 1, tzinfo=timezone.utc)
        db.commit()
        one = client.get(f"/admin/memories/{mem_public_id(m.id)}", headers=mine).json()
        assert [x["closed"] for x in one["facts"]] == [False]

    def test_the_timeline_names_the_fact_that_was_closed(self, db, tenant, client, mine):
        from korely_agent.services.ids import mem_public_id

        m = memory(db, tenant.id)
        f = fact(db, tenant.id, obj="Acme", memory_id=m.id)
        f.invalid_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
        db.commit()
        out = client.get(f"/admin/memories/{mem_public_id(m.id)}/history", headers=mine).json()
        closed = [e for e in out["events"] if e["event"] == "fact_invalidated"]
        assert [e["fact"] for e in closed] == ["Maria is_on Acme"]
