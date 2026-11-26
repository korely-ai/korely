"""An erasure's cost follows what it erases, and it marks what is in flight (audit of 27 Oct 2026).

- a project's erasure took one advisory lock per id it renamed, to its commit,
  and some thousands of ids (which a read key adds by reading) exhausted the
  lock table: now two, the project's and the rows of no project's;
- the pseudonym pass grew faster than the ids: each place ran its own
  statements once per id; now one per place for every 500 ids;
- a delivery enqueued while the erasure was open was looked up at its
  finish by time, against a record dated at the erasure's start, and kept
  the id; now the erasure marks it;
- the keys confined to the project's end users are revoked by its
  erasure, and the answer says how many;
- a restore's replay of an erasure with no project reached namesakes: a
  record whose project is not recorded is repeated nowhere,
  and counted.
"""
import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import (AgentForgetAudit, AgentMemory, AgentWebhookDelivery,
                                    AgentWebhookEndpoint, AuditEvent)
from korely_agent.services import end_user_places as places
from korely_agent.services import projects as project_service
from korely_agent.services import pseudonyms


@pytest.fixture(autouse=True)
def keyed(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "q" * 48)
    from korely_agent import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _beta(db, tenant, *, read=0, held=1):
    """Project Beta, holding `held` end users and having read `read` ids it
    never held (each a row of its trail)."""
    project_service.get_or_create_default_project(db, tenant.id)
    beta = project_service.create_project(db, tenant_id=tenant.id, name="Beta")
    for i in range(held):
        db.add(AgentMemory(tenant_id=tenant.id, project_id=beta.id, end_user_id=f"held-{i}",
                           content="dummy", graph_sync_status="done"))
    db.add_all(AuditEvent(tenant_id=tenant.id, project_id=beta.id, actor="rest", action="read",
                          result="ok", end_user_id=f"read-{i}") for i in range(read))
    db.commit()
    return beta


def _advisory_locks(db) -> int:
    return db.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                           " AND pid = pg_backend_pid()")).scalar()


def test_a_projects_erasure_takes_two_locks_whatever_it_renames(db, tenant, monkeypatch):
    beta = _beta(db, tenant, read=300, held=20)
    seen = []
    real = places.erase_project

    def counting(db_, *a, **kw):
        seen.append(_advisory_locks(db_))
        return real(db_, *a, **kw)

    monkeypatch.setattr(places, "erase_project", counting)
    project_service.delete_project(db, tenant.id, beta, erase=True)
    assert seen == [2], f"{seen} advisory locks held for 320 ids"


def _statements(db, tenant, read):
    beta = _beta(db, tenant, read=read)
    n = [0]

    def one(*a, **kw):
        n[0] += 1

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", one)
    try:
        project_service.delete_project(db, tenant.id, beta, erase=True)
    finally:
        event.remove(engine, "before_cursor_execute", one)
    return n[0]


def test_a_projects_erasure_runs_as_many_statements_for_50_ids_as_for_400(db, tenant, engine):
    few = _statements(db, tenant, 50)
    with engine.begin() as c:
        c.execute(text("DELETE FROM agent_forget_audit; DELETE FROM audit_events;"
                       " DELETE FROM agent_memories; DELETE FROM agent_projects"))
    db.expire_all()
    many = _statements(db, tenant, 400)
    assert many == few, f"{few} statements for 50 ids, {many} for 400"


def _trail_list_reads(db, tenant, read):
    """A project that read `read` ids, with 30 trail rows that recorded a
    key's list (one names an id of the last block of 500): the values bound
    in the statements that look for the lists on the trail, when it is
    erased, and the rows those statements renamed."""
    beta = _beta(db, tenant, read=read)
    for i in range(30):
        db.add(AuditEvent(tenant_id=tenant.id, project_id=beta.id, actor="rest",
                          action="key_create", result="ok",
                          meta={"users": str([f"read-{read - 1 - i}", "someone-else"])}))
    db.commit()
    bound = [0]

    def one(conn, cursor, statement, parameters, context, executemany):
        if "audit_events.meta" in statement and statement.lstrip().startswith("SELECT"):
            bound[0] += len(parameters) if isinstance(parameters, (dict, list, tuple)) else 0

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", one)
    try:
        project_service.delete_project(db, tenant.id, beta, erase=True)
    finally:
        event.remove(engine, "before_cursor_execute", one)
    db.expire_all()
    lists = [r.meta["users"] for r in db.query(AuditEvent).filter(AuditEvent.action == "key_create")]
    return bound[0], lists


def test_the_trail_lists_pass_costs_the_rows_not_the_ids_times_the_rows(db, tenant, engine):
    """Audit of 27 Oct 2026, run 6: the trail rows were found by a pattern
    per id, 500 patterns on every trail row for every block of 500 ids, so
    the pass cost ids times rows. Now each row with a list is read once and
    its entries looked up: the statements bind as many values for 2,000 ids
    as for 400, and the rows are renamed all the same."""
    few, lists = _trail_list_reads(db, tenant, 400)
    assert few, "the case was not reached: no statement read the trail's lists"
    assert all(pseudonyms.pseudonym(tenant.id, f"read-{399 - i}") in "".join(lists) for i in range(30))
    with engine.begin() as c:
        c.execute(text("DELETE FROM agent_forget_audit; DELETE FROM audit_events;"
                       " DELETE FROM agent_memories; DELETE FROM agent_projects"))
    db.expire_all()
    many, lists = _trail_list_reads(db, tenant, 2000)
    assert many == few, f"{few} values bound for 400 ids, {many} for 2,000"
    assert all("read-" not in users and "someone-else" not in users for users in lists)
    assert all(pseudonyms.pseudonym(tenant.id, f"read-{1999 - i}") in "".join(lists) for i in range(30))


def test_a_delivery_enqueued_while_the_erasure_is_open_takes_the_pseudonym(db, tenant, engine):
    """As the sandbox found it: the erasure's transaction starts, a
    write of the same end user commits its delivery, the erasure deletes
    both memories and commits, and the worker then delivers it."""
    from korely_agent.services import webhooks
    from korely_agent.services.facts import service as facts

    beta = _beta(db, tenant, held=0)
    ep = AgentWebhookEndpoint(tenant_id=tenant.id, url="http://127.0.0.1:9/h", secret="whsec_x",
                              events=["*"])
    db.add(ep)
    db.add(AgentMemory(tenant_id=tenant.id, project_id=beta.id, end_user_id="maria",
                       content="dummy", graph_sync_status="done"))
    db.commit()
    eraser = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        eraser.execute(text("SELECT 1"))          # its transaction, and its now(), start
        writer = sessionmaker(bind=engine)()
        writer.add(AgentMemory(tenant_id=tenant.id, project_id=beta.id, end_user_id="maria",
                               content="dummy too", graph_sync_status="done"))
        d = AgentWebhookDelivery(tenant_id=tenant.id, endpoint_id=ep.id,
                                 event_type="memory.created", status="pending",
                                 payload={"project_id": str(beta.id),
                                          "data": {"user_id": "maria"}})
        writer.add(d)
        writer.commit()
        did, eid = d.id, ep.id
        writer.close()
        out = facts.forget_end_user(eraser, tenant.id, "maria", project_id=beta.id)
        assert out["memories_deleted"] == 2
    finally:
        eraser.close()
    webhooks._write_outcome(db, did, 0, eid, (True, 200, None))
    db.expire_all()
    row = db.get(AgentWebhookDelivery, did)
    assert row.status == "delivered"
    assert row.payload["data"]["user_id"] == pseudonyms.pseudonym(tenant.id, "maria")


def test_the_keys_confined_to_its_end_users_are_revoked_and_counted(db, tenant):
    """A key of Beta confined to Yan, who lives in the Default
    project; Beta's erasure names Yan (the key's list) and revokes the key,
    and the answer says so."""
    from korely_agent.services import keys as key_service

    default = project_service.get_or_create_default_project(db, tenant.id)
    beta = _beta(db, tenant, held=0)
    db.add(AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="yan",
                       content="dummy", graph_sync_status="done"))
    db.commit()
    row, _secret = key_service.create_api_key(db, tenant_id=tenant.id, name="yan's",
                                              scopes=["memories:read"], project_id=beta.id,
                                              end_users=["yan"])
    out = project_service.delete_project(db, tenant.id, beta, erase=True)
    assert out["keys_revoked"] == 1 and out["keys"] == 1
    db.expire_all()
    assert db.get(type(row), row.id).revoked_at is not None


def test_a_record_whose_project_is_not_recorded_is_repeated_nowhere(db, tenant):
    """A namesake in another project keeps their memory; the record goes
    back and is counted apart."""
    from korely_agent.services import erasure_replay

    beta = _beta(db, tenant, held=0)
    db.add(AgentMemory(tenant_id=tenant.id, project_id=beta.id, end_user_id="maria",
                       content="a namesake's", graph_sync_status="done"))
    db.commit()
    rid = uuid.uuid4()
    record = {"id": str(rid), "account": str(tenant.id), "project_id": None,
              "erased_in_project_id": str(erasure_replay.PROJECT_NOT_RECORDED),
              "scope": "user", "end_user_id": pseudonyms.pseudonym(tenant.id, "maria"),
              "target_id": None, "key_check": pseudonyms.key_check(),
              "memories_forgotten": 1, "facts_invalidated": 0,
              "created_at": "2026-09-20T10:00:00+00:00"}
    out = erasure_replay.replay(db, [record], pseudonyms.key_check())
    assert out["project_unknown"] == {"user": 1} and out["replayed"] == {}
    db.expire_all()
    assert db.query(AgentMemory).filter(AgentMemory.end_user_id == "maria").count() == 1
    assert db.get(AgentForgetAudit, rid).erased_in_project_id == erasure_replay.PROJECT_NOT_RECORDED


def test_an_erasure_decides_again_only_the_relations_it_can_reach(db, tenant, monkeypatch):
    """The erasure of one memory read every relation of the account; only
    those of its (project, end user) vocabulary can lose their example."""
    from datetime import datetime, timezone

    from korely_agent.db.models import AgentFact, PredicateCluster
    from korely_agent.services.facts import service as facts
    from korely_agent.services.memory_core import store

    beta = _beta(db, tenant, held=0)
    for i in range(30):
        db.add(PredicateCluster(tenant_id=tenant.id, project_id=beta.id, end_user_id=f"o-{i}",
                                canonical="likes", member_count=1, example_object="tea"))
    db.add(PredicateCluster(tenant_id=tenant.id, project_id=beta.id, end_user_id="maria",
                            canonical="likes", member_count=1, example_object="coffee"))
    m = AgentMemory(tenant_id=tenant.id, project_id=beta.id, end_user_id="maria",
                    content="dummy", graph_sync_status="done")
    db.add(m)
    db.commit()
    db.add(AgentFact(tenant_id=tenant.id, project_id=beta.id, end_user_id="maria",
                     subject_label="maria", predicate_raw="likes", predicate_canonical="likes",
                     object_label="coffee", source_memory_id=m.id, source_memory_ids=[str(m.id)],
                     valid_from=datetime.now(timezone.utc)))
    db.commit()
    reached = []
    real = store.examples_after_the_erasure

    def counting(db_, tables, relations, *a, **kw):
        reached.append(db_.query(PredicateCluster).filter(relations).count())
        return real(db_, tables, relations, *a, **kw)

    monkeypatch.setattr(store, "examples_after_the_erasure", counting)
    facts.erase_memory(db, tenant.id, m.id, project_id=beta.id)
    assert reached == [1], f"{reached} relations decided again for one memory"
    db.expire_all()
    mine = db.query(PredicateCluster).filter(PredicateCluster.end_user_id == "maria").one()
    assert mine.example_object is None, "the erased value left the example"


def test_an_agents_erasure_decides_every_vocabulary_it_reaches(db, tenant, monkeypatch):
    """Review of the audit of 27 Oct 2026: delete_agent read at most MAX_VOCABULARIES
    (project, end user) pairs, and past it a relation kept the erased fact's
    value as its example. Every pair now, in statements of `_PAIRS`."""
    from datetime import datetime, timezone

    from korely_agent.db.models import AgentFact, PredicateCluster
    from korely_agent.services.facts import erasure
    from korely_agent.services.facts import service as facts

    monkeypatch.setattr(erasure, "_PAIRS", 1)
    # The cap of before, at one pair (gone now: nothing to patch).
    monkeypatch.setattr(erasure, "MAX_VOCABULARIES", 1, raising=False)
    default = project_service.get_or_create_default_project(db, tenant.id)
    now = datetime.now(timezone.utc)
    for u in ("u1", "u2", "u3"):
        db.add(PredicateCluster(tenant_id=tenant.id, project_id=default.id, end_user_id=u,
                                canonical="lives_in", member_count=1,
                                example_object=f"SecretCity-{u}"))
        m = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id=u,
                        agent_id="bot", content="x", graph_sync_status="done")
        db.add(m)
        db.flush()
        db.add(AgentFact(tenant_id=tenant.id, project_id=default.id, end_user_id=u,
                         agent_id="bot", subject_label=u, predicate_raw="lives in",
                         predicate_canonical="lives_in", object_label=f"SecretCity-{u}",
                         source_memory_id=m.id, valid_from=now))
    db.commit()
    facts.delete_agent(db, tenant.id, "bot", project_id=default.id)
    db.expire_all()
    left = [e for (e,) in db.query(PredicateCluster.example_object) if e]
    assert left == [], left


def test_a_replay_run_again_says_again_what_to_erase_by_hand(db, tenant):
    """Review of the audit of 27 Oct 2026: the second run found the record back and
    counted it in the dump, exit 0, with no word of the person to erase."""
    from korely_agent.services import erasure_replay

    record = {"id": str(uuid.uuid4()), "account": str(tenant.id), "project_id": None,
              "erased_in_project_id": str(erasure_replay.PROJECT_NOT_RECORDED),
              "scope": "agent", "end_user_id": None, "target_id": "bot",
              "key_check": None, "memories_forgotten": 1, "facts_invalidated": 0,
              "created_at": "2026-09-20T10:00:00+00:00"}
    for _ in range(2):
        out = erasure_replay.replay(db, [record], pseudonyms.key_check())
        assert out["project_unknown"] == {"agent": 1} and out["in_the_dump"] == 0
