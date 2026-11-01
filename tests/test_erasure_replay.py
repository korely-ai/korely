"""The erasures made after a dump survive its restore (audit of 17 Oct 2026).

restore.sh replaced the database with the dump, bringing back the people
erased since, and the README's recipe to repeat them by hand could not be
followed: the erased end user is only a pseudonym in the records. Now the
live database's erasure records are kept before the restore and the ones the
dump lacks are made again after it (services/erasure_replay.py). A restore is
played here by putting the dump's rows back and dropping the record."""
import uuid
from pathlib import Path

import pytest

from tests.conftest import seeded_project_id
from korely_agent.db.models import AgentForgetAudit, AgentMemory, AgentProject
from korely_agent.services import erasure_replay
from korely_agent.services import facts as agent_facts
from korely_agent.services.sentinels import EVERY_PROJECT



@pytest.fixture(autouse=True)
def _keyed(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 48)
    from korely_agent import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _memory(db, tenant, pid, user, agent=None, text="Maria's card ends in 4242."):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=user,
                    agent_id=agent, content=text, graph_sync_status="done")
    db.add(m)
    db.commit()
    return m


def _restore(db, tenant, pid, rows):
    """The dump: the rows as they were, and no record of what came after."""
    db.query(AgentForgetAudit).delete()
    db.commit()
    db.expunge_all()
    for user, agent, mid in rows:
        db.add(AgentMemory(id=mid, tenant_id=tenant.id, project_id=pid, end_user_id=user,
                           agent_id=agent, content="restored", graph_sync_status="done"))
    db.commit()


def _alive(db, tenant):
    db.expire_all()
    return sorted(m.end_user_id for m in db.query(AgentMemory)
                  .filter(AgentMemory.tenant_id == tenant.id, AgentMemory.deleted_at.is_(None)))


def test_an_end_user_erased_after_the_dump_is_erased_again(db, tenant):
    pid = seeded_project_id(db, tenant.id)
    maria, bob = _memory(db, tenant, pid, "maria"), _memory(db, tenant, pid, "bob")
    proof = agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)["audit_id"]
    kept = erasure_replay.export(db)
    assert [r["scope"] for r in kept] == ["user"]
    assert "maria" not in str(kept), "the file names the erased end user by pseudonym only"

    _restore(db, tenant, pid, [("maria", None, maria.id)])
    assert _alive(db, tenant) == ["bob", "maria"]
    out = erasure_replay.replay(db, kept)

    assert out["replayed"] == {"user": 1}
    assert _alive(db, tenant) == ["bob"]
    records = db.query(AgentForgetAudit).all()
    from korely_agent.services.ids import audit_public_id
    assert [audit_public_id(r.id) for r in records] == [proof], "the erasure's proof is not back"
    assert bob.id


def test_what_the_dump_already_holds_is_not_made_twice(db, tenant):
    pid = seeded_project_id(db, tenant.id)
    _memory(db, tenant, pid, "maria")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)
    kept = erasure_replay.export(db)
    out = erasure_replay.replay(db, kept)
    assert out["in_the_dump"] == 1 and out["replayed"] == {}
    assert db.query(AgentForgetAudit).count() == 1


def test_an_agent_and_a_memory_too(db, tenant):
    pid = seeded_project_id(db, tenant.id)
    bot = _memory(db, tenant, pid, "anna", agent="support")
    one = _memory(db, tenant, pid, "carla")
    agent_facts.delete_agent(db, tenant.id, "support", project_id=pid)
    agent_facts.forget_memory(db, tenant.id, one.id, project_id=EVERY_PROJECT)
    kept = erasure_replay.export(db)
    _restore(db, tenant, pid, [("anna", "support", bot.id)])
    db.get(AgentMemory, one.id).deleted_at = None
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["replayed"] == {"agent": 1, "memory": 1}
    assert _alive(db, tenant) == []


def test_restore_keeps_the_erasures_before_and_replays_them_after():
    script = (Path(__file__).resolve().parent.parent / "deploy" / "restore.sh").read_text()
    export, restore = script.index("erasures-export"), script.index("pg_restore -U")
    replay, start = script.index("erasures-replay < \"$ERASURES\""), script.index("docker compose start api >/dev/null")
    assert export < restore < replay < start


def test_a_failed_replay_keeps_the_erasures_and_the_api_stopped():
    """The acceptance run of 0.1.40: the replay refused its input and the restore
    stopped with the API down and the erasures file deleted, so the people
    erased since the dump were back and nobody could erase them again."""
    script = (Path(__file__).resolve().parent.parent / "deploy" / "restore.sh").read_text()
    replay = script.index("erasures-replay < \"$ERASURES\"")
    failed = script[replay:script.index("docker compose start api >/dev/null", replay)]
    assert 'cp "$ERASURES" "$KEEP"' in failed and "exit 1" in failed
    assert "erasures-replay < $KEEP" in failed, "it says how to make them again"


# A dump can hold an end user, or a memory, only as an item of a /v1/batch
# job still queued when it was taken (audit of 19 Oct 2026): the replay
# looked for the end user in the memories and the facts alone, and for an
# erased memory in its row alone, so it counted nothing to erase, and the
# worker imported the item again, the memory under the very id erased.
MARIA_ITEM = {"content": "Maria's card ends in 4242.", "user_id": "maria"}
BOB_ITEM = {"content": "Bob prefers email.", "user_id": "bob"}


@pytest.fixture
def no_model(monkeypatch):
    """The worker's import without a model: facts have their own tests."""
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])


def _restore_job(db, job_id, payload):
    """The dump, taken while the job was queued: the job as it was then, and
    no record of the erasure that came after."""
    import copy

    from korely_agent.db.models import AgentBatchJob
    from korely_agent.services import batch

    db.query(AgentForgetAudit).delete()
    db.commit()
    db.expire_all()
    job = db.get(AgentBatchJob, job_id)
    job.payload = copy.deepcopy(payload)
    job.status, job.imported, job.failed, job.errors = batch.STATUS_PENDING, 0, 0, []
    job.created_memory_ids, job.completed_at, job.attempts = [], None, 0
    db.commit()


def test_an_end_user_the_dump_holds_only_in_a_queued_batch_is_erased_again(db, tenant, no_model):
    import copy

    from korely_agent.db.models import AgentBatchJob
    from korely_agent.services import batch
    from korely_agent.services.ids import audit_public_id

    pid = seeded_project_id(db, tenant.id)
    job = batch.enqueue_batch(db, tenant.id, [dict(MARIA_ITEM), dict(BOB_ITEM)], project_id=pid)
    dumped = copy.deepcopy(job.payload)
    proof = agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)["audit_id"]
    kept = erasure_replay.export(db)

    _restore_job(db, job.id, dumped)
    out = erasure_replay.replay(db, kept)

    assert out["replayed"] == {"user": 1}, out
    db.expire_all()
    assert "4242" not in str(db.get(AgentBatchJob, job.id).payload), "the item is scrubbed"
    assert [audit_public_id(r.id) for r in db.query(AgentForgetAudit).all()] == [proof]
    batch.process_batch_jobs(db, limit=5)
    assert _alive(db, tenant) == ["bob"], "the worker imported the erased end user again"


def test_an_end_user_the_dump_holds_only_in_the_trail_takes_the_pseudonym(db, tenant):
    from korely_agent.db.models import AuditEvent
    from korely_agent.services.pseudonyms import pseudonym

    pid = seeded_project_id(db, tenant.id)
    _memory(db, tenant, pid, "maria")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)
    kept = erasure_replay.export(db)

    # The dump: Maria's memories already gone (one by one, before it), her
    # access trail still there under her id.
    db.query(AgentForgetAudit).delete()
    db.query(AuditEvent).delete()
    db.add(AuditEvent(tenant_id=tenant.id, project_id=pid, actor="rest", action="search",
                      end_user_id="maria"))
    db.commit()
    out = erasure_replay.replay(db, kept)

    assert out["replayed"] == {"user": 1}, out
    db.expire_all()
    assert [e.end_user_id for e in db.query(AuditEvent).all()] == [pseudonym(tenant.id, "maria")]


def test_a_memory_the_dump_holds_only_as_a_queued_item_is_not_imported_again(db, tenant, no_model):
    import copy

    from korely_agent.db.models import AgentBatchJob
    from korely_agent.services import batch
    from korely_agent.services.ids import audit_public_id

    pid = seeded_project_id(db, tenant.id)
    job = batch.enqueue_batch(db, tenant.id, [dict(MARIA_ITEM), dict(BOB_ITEM)], project_id=pid)
    dumped = copy.deepcopy(job.payload)
    # After the dump: the worker imports it, then DELETE /v1/memories/{id}?erase=true.
    batch.process_batch_jobs(db, limit=5)
    mid = batch.item_memory_id(job.id, 0)
    proof = agent_facts.erase_memory(db, tenant.id, mid, project_id=pid)["audit_id"]
    kept = erasure_replay.export(db)
    assert [(r["scope"], r["erased"]) for r in kept] == [("memory", True)]

    _restore_job(db, job.id, dumped)
    db.query(AgentMemory).delete()
    db.commit()
    out = erasure_replay.replay(db, kept)

    assert out["replayed"] == {"memory": 1}, out
    db.expire_all()
    assert "4242" not in str(db.get(AgentBatchJob, job.id).payload), "the item is scrubbed"
    assert [audit_public_id(r.id) for r in db.query(AgentForgetAudit).all()] == [proof]
    batch.process_batch_jobs(db, limit=5)
    db.expire_all()
    assert db.get(AgentMemory, mid) is None, "the erased memory came back under its id"
    assert _alive(db, tenant) == ["bob"]


# A replay under another SECRET_KEY, or none, found none of the erased end
# users by their pseudonym, counted them as nothing to erase and exited 0, so
# restore.sh started the API with them back (audit of 19 Oct 2026). The file
# carries the
# check of its key now, and the replay refuses before it erases anything.

def _keyed_as(monkeypatch, secret):
    from korely_agent import config

    if secret is None:
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setenv("SECRET_KEY", "")
    else:
        monkeypatch.setenv("SECRET_KEY", secret)
    config.get_settings.cache_clear()


def _export_via_manage(capsys):
    import json

    from korely_agent import manage

    capsys.readouterr()
    assert manage.erasures_export() == 0
    return json.loads(capsys.readouterr().out)


def _replay_via_manage(monkeypatch, capsys, data):
    import io
    import json

    from korely_agent import manage

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    code = manage.erasures_replay()
    return code, capsys.readouterr().err


def _maria_erased_then_restored(db, tenant):
    pid = seeded_project_id(db, tenant.id)
    maria = _memory(db, tenant, pid, "maria")
    _memory(db, tenant, pid, "bob")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)
    return pid, maria


@pytest.mark.parametrize("replayed_under", ["y" * 48, None])
def test_a_replay_under_another_key_or_none_refuses_and_erases_nothing(
        db, tenant, monkeypatch, capsys, replayed_under):
    pid, maria = _maria_erased_then_restored(db, tenant)
    kept = _export_via_manage(capsys)
    _restore(db, tenant, pid, [("maria", None, maria.id)])

    _keyed_as(monkeypatch, replayed_under)
    code, err = _replay_via_manage(monkeypatch, capsys, kept)

    assert code != 0, "restore.sh would start the API with the erased end user back"
    assert "SECRET_KEY" in err and "Nothing was replayed" in err
    assert "maria" not in err
    assert _alive(db, tenant) == ["bob", "maria"], "refused before anything was erased"
    assert db.query(AgentForgetAudit).count() == 0


def test_a_file_written_before_the_key_check_replays_as_before(db, tenant, monkeypatch, capsys):
    """The bare list of records an earlier export wrote: no key to compare,
    replayed under this one as it was."""
    pid, maria = _maria_erased_then_restored(db, tenant)
    old_file = erasure_replay.export(db)
    _restore(db, tenant, pid, [("maria", None, maria.id)])
    code, _err = _replay_via_manage(monkeypatch, capsys, old_file)
    assert code == 0
    assert _alive(db, tenant) == ["bob"]


def test_an_old_file_under_no_key_refuses_too(db, tenant, monkeypatch, capsys):
    pid, maria = _maria_erased_then_restored(db, tenant)
    old_file = erasure_replay.export(db)
    _restore(db, tenant, pid, [("maria", None, maria.id)])
    _keyed_as(monkeypatch, None)
    code, err = _replay_via_manage(monkeypatch, capsys, old_file)
    assert code != 0 and "no SECRET_KEY" in err
    assert _alive(db, tenant) == ["bob", "maria"]


def test_the_same_key_replays_and_other_erasures_need_none(db, tenant, monkeypatch, capsys):
    """Under the key of the file the end user is erased again; erasures of
    an agent need no key, and a file holding only those replays under any."""
    pid, maria = _maria_erased_then_restored(db, tenant)
    kept = _export_via_manage(capsys)
    assert kept["key_check"] and "x" * 48 not in str(kept)
    _restore(db, tenant, pid, [("maria", None, maria.id)])
    code, _err = _replay_via_manage(monkeypatch, capsys, kept)
    assert code == 0 and _alive(db, tenant) == ["bob"]

    bot = _memory(db, tenant, pid, "anna", agent="support")
    agent_facts.delete_agent(db, tenant.id, "support", project_id=pid)
    agents_only = _export_via_manage(capsys)
    agents_only["records"] = [r for r in agents_only["records"] if r["scope"] == "agent"]
    _restore(db, tenant, pid, [("anna", "support", bot.id)])
    _keyed_as(monkeypatch, "z" * 48)
    code, _err = _replay_via_manage(monkeypatch, capsys, agents_only)
    assert code == 0 and "anna" not in _alive(db, tenant)


# ── an erasure is replayed in the project it was made in ────────────────────
#
# Audit of 19 Oct 2026: deleting a project without `erase` files its
# erasure records under the default project, and the replay looked for the
# erased end user or agent there. The person the dump brought back in the
# deleted project stayed in clear, or a namesake in the default project was
# erased instead. The record keeps the project it was made in
# (`erased_in_project_id`), which no move touches.

def _project_x(db, tenant):
    from korely_agent.services import projects

    default = projects.get_or_create_default_project(db, tenant.id)
    x = projects.create_project(db, tenant_id=tenant.id, name="Project X")
    return default, x, {"id": x.id, "name": x.name, "slug": x.slug}


def _delete_without_erase(db, tenant, x):
    from korely_agent.db.models import AgentProject
    from korely_agent.services import projects

    projects.delete_project(db, tenant.id, db.get(AgentProject, x.id), erase=False)


def _restore_projects(db, tenant, projects_back, rows):
    """The dump, taken before the erasure: the projects it held, the rows as
    they were, no erasure record."""
    from korely_agent.db.models import AgentProject

    db.query(AgentForgetAudit).delete()
    db.query(AgentMemory).delete()
    db.commit()
    db.expunge_all()
    for p in projects_back:
        if db.get(AgentProject, p["id"]) is None:
            db.add(AgentProject(id=p["id"], tenant_id=tenant.id, name=p["name"],
                                slug=p["slug"], is_default=False))
    db.commit()
    for pid, user, agent, text in rows:
        _memory(db, tenant, pid, user, agent=agent, text=text)


def _alive_in(db, tenant):
    db.expire_all()
    return sorted((m.project_id, m.end_user_id, m.agent_id, m.content)
                  for m in db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id))


def test_an_erasure_in_a_project_deleted_since_reaches_that_project(db, tenant):
    """Nobody of that id in the default project: the replay counted nothing
    to erase, and the erased person was back in the restored project."""
    default, x, x_row = _project_x(db, tenant)
    _memory(db, tenant, x.id, "maria", text="X maria")
    _memory(db, tenant, x.id, "bob", text="X bob")
    proof = agent_facts.forget_end_user(db, tenant.id, "maria", project_id=x.id)["audit_id"]
    _delete_without_erase(db, tenant, x)
    kept = erasure_replay.export(db)
    assert kept[0]["project_id"] == str(default.id), "filed under the default project"

    _restore_projects(db, tenant, [x_row], [(x.id, "maria", None, "X maria"),
                                            (x.id, "bob", None, "X bob")])
    out = erasure_replay.replay(db, kept)

    assert out["replayed"] == {"user": 1} and out["nothing_to_erase"] == {}
    # The deletion that came after is made again too (audit of 20 Oct 2026): bob's
    # row is where the live database has it, in the default project.
    assert out["changes"] == {"move": 1}
    assert _alive_in(db, tenant) == [(default.id, "bob", None, "X bob")]
    from korely_agent.services.ids import audit_public_id
    record = db.query(AgentForgetAudit).filter(AgentForgetAudit.scope == "user").one()
    assert audit_public_id(record.id) == proof
    assert kept[0]["erased_in_project_id"] == str(x.id), "made in X, whatever its filing"
    assert (record.project_id, record.erased_in_project_id) == (default.id, x.id)


def test_a_namesake_in_the_default_project_is_not_erased_in_their_place(db, tenant):
    default, x, x_row = _project_x(db, tenant)
    _memory(db, tenant, x.id, "maria", text="X maria")
    _memory(db, tenant, default.id, "maria", text="another maria")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=x.id)
    _delete_without_erase(db, tenant, x)
    kept = erasure_replay.export(db)

    _restore_projects(db, tenant, [x_row], [(x.id, "maria", None, "X maria"),
                                            (default.id, "maria", None, "another maria")])
    out = erasure_replay.replay(db, kept)

    assert out["replayed"] == {"user": 1}
    assert _alive_in(db, tenant) == [(default.id, "maria", None, "another maria")]


def test_an_agent_erased_in_a_project_deleted_since_too(db, tenant):
    default, x, x_row = _project_x(db, tenant)
    _memory(db, tenant, x.id, "anna", agent="support", text="X support")
    _memory(db, tenant, default.id, "carla", agent="support", text="default support")
    agent_facts.delete_agent(db, tenant.id, "support", project_id=x.id)
    _delete_without_erase(db, tenant, x)
    kept = erasure_replay.export(db)

    _restore_projects(db, tenant, [x_row], [(x.id, "anna", "support", "X support"),
                                            (default.id, "carla", "support", "default support")])
    out = erasure_replay.replay(db, kept)

    assert out["replayed"] == {"agent": 1}
    assert _alive_in(db, tenant) == [(default.id, "carla", "support", "default support")]


def test_a_project_the_dump_does_not_hold_holds_nothing_of_its_erasure(db, tenant):
    """Project X made after the dump: the dump holds nothing of its agent,
    and the default project's namesake namespace is somebody else's."""
    default, x, _x_row = _project_x(db, tenant)
    _memory(db, tenant, x.id, "anna", agent="support", text="X support")
    agent_facts.delete_agent(db, tenant.id, "support", project_id=x.id)
    _delete_without_erase(db, tenant, x)
    kept = erasure_replay.export(db)

    _restore_projects(db, tenant, [], [(default.id, "carla", "support", "default support")])
    out = erasure_replay.replay(db, kept)

    assert out["nothing_to_erase"] == {"agent": 1} and out["replayed"] == {}
    assert out["changes_absent"] == {"move": 1}, "X is not in the dump: nothing to move"
    assert _alive_in(db, tenant) == [(default.id, "carla", "support", "default support")]
    assert db.query(AgentForgetAudit).filter(
        AgentForgetAudit.scope == "agent").one().erased_in_project_id == x.id


def test_the_project_left_alone_replays_as_before(db, tenant):
    """The control: no project deleted between the erasure and the restore."""
    default, x, x_row = _project_x(db, tenant)
    _memory(db, tenant, x.id, "maria", text="X maria")
    _memory(db, tenant, default.id, "maria", text="another maria")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=x.id)
    kept = erasure_replay.export(db)

    _restore_projects(db, tenant, [x_row], [(x.id, "maria", None, "X maria"),
                                            (default.id, "maria", None, "another maria")])
    out = erasure_replay.replay(db, kept)

    assert out["replayed"] == {"user": 1}
    assert _alive_in(db, tenant) == [(default.id, "maria", None, "another maria")]


def test_a_file_written_before_the_project_was_kept_replays_in_the_records_project(db, tenant):
    """An export without `erased_in_project_id`: the record's project, as
    before."""
    default, x, x_row = _project_x(db, tenant)
    _memory(db, tenant, x.id, "maria", text="X maria")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=x.id)
    old_file = [{k: v for k, v in r.items() if k != "erased_in_project_id"}
                for r in erasure_replay.export(db)]

    _restore_projects(db, tenant, [x_row], [(x.id, "maria", None, "X maria")])
    out = erasure_replay.replay(db, old_file)

    assert out["replayed"] == {"user": 1} and _alive_in(db, tenant) == []
    assert db.query(AgentForgetAudit).one().erased_in_project_id == x.id


# ── a project's erasure is the erasure of each of its end users ─────────────
#
# Audit of 19 Oct 2026, and the decision that followed: DELETE
# /admin/projects/{id}?erase=true files, beside the project's record, one
# scope 'user' record per end user it erased (services/projects.
# _file_each_end_user). The project's record replays the whole project; its
# end users' records are put back as they were, never replayed one by one
# (the project's replay erases them), never counted twice, and the records
# the replayed erasure files for the same people are not kept beside them.

def _project_erased(db, tenant):
    from korely_agent.services import projects

    default, x, x_row = _project_x(db, tenant)
    _memory(db, tenant, x.id, "maria", text="X maria")
    _memory(db, tenant, x.id, "bob", text="X bob")
    proof = projects.delete_project(db, tenant.id, db.get(AgentProject, x.id),
                                    erase=True)["audit_id"]
    return default, x, x_row, proof


def _records(db, tenant):
    db.expire_all()
    return sorted((str(r.id), r.scope, r.end_user_id, r.target_id)
                  for r in db.query(AgentForgetAudit).filter(AgentForgetAudit.tenant_id == tenant.id))


@pytest.mark.parametrize("order", ["as exported", "end users first", "project first"])
def test_a_project_erased_after_the_dump_is_erased_again_once(db, tenant, order):
    from korely_agent.services.ids import audit_public_id

    default, x, x_row, proof = _project_erased(db, tenant)
    before = _records(db, tenant)
    kept = erasure_replay.export(db)
    assert sorted(r["scope"] for r in kept) == ["project", "user", "user"]
    assert "maria" not in str(kept) and "bob" not in str(kept)
    if order != "as exported":
        kept.sort(key=lambda r: (r["scope"] == "project") == (order == "end users first"))

    _restore_projects(db, tenant, [x_row], [(x.id, "maria", None, "X maria"),
                                            (x.id, "bob", None, "X bob")])
    out = erasure_replay.replay(db, kept)

    assert out == {"in_the_dump": 0, "replayed": {"project": 1}, "nothing_to_erase": {},
                   "not_found": 0, "project_unknown": {}, "changes": {}, "changes_absent": {},
                   "not_made": {}, "failed": []}, "one erasure, counted once"
    assert _alive_in(db, tenant) == []
    assert _records(db, tenant) == before, "the records as they were, none twice"
    assert proof in {audit_public_id(uuid.UUID(r[0])) for r in before}


def test_a_project_erasure_the_dump_holds_counts_once(db, tenant):
    _project_erased(db, tenant)
    kept = erasure_replay.export(db)
    out = erasure_replay.replay(db, kept)
    assert out["in_the_dump"] == 1 and out["replayed"] == {}


def test_its_end_users_records_need_no_key_to_replay_the_project(db, tenant, monkeypatch,
                                                                  capsys):
    """The project's replay finds its end users by its rows, not by their
    pseudonyms: a file whose only end users are a project's replays under
    another key instead of refusing."""
    _default, x, x_row, _proof = _project_erased(db, tenant)
    kept = _export_via_manage(capsys)
    _restore_projects(db, tenant, [x_row], [(x.id, "maria", None, "X maria")])
    _keyed_as(monkeypatch, "y" * 48)
    code, _err = _replay_via_manage(monkeypatch, capsys, kept)
    assert code == 0
    assert _alive_in(db, tenant) == []


# ── one record that fails stops no other (the audit before 1.0.0) ──────────
# The replay walked the file with no record of its own, so the first record
# whose repeat raised ended the run, and every run again stopped at it: the
# erasures filed after it, other accounts' too, were never made again.

def _two_accounts_erased(db, tenant):
    """Maria erased in `tenant`, then an agent of another account: the
    records in that order, and the dump holding both again."""
    from korely_agent.db.models import Tenant

    other = Tenant(id=uuid.uuid4(), name="Other account")
    db.add(other)
    db.commit()
    pid, oid = seeded_project_id(db, tenant.id), seeded_project_id(db, other.id)
    maria = _memory(db, tenant, pid, "maria")
    bot = _memory(db, other, oid, "olga", agent="helper")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)
    agent_facts.delete_agent(db, other.id, "helper", project_id=oid)
    kept = erasure_replay.export(db)
    assert [r["scope"] for r in kept] == ["user", "agent"]
    db.query(AgentForgetAudit).delete()
    db.commit()
    db.expunge_all()
    db.add(AgentMemory(id=maria.id, tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                       content="restored", graph_sync_status="done"))
    db.add(AgentMemory(id=bot.id, tenant_id=other.id, project_id=oid, end_user_id="olga",
                       agent_id="helper", content="restored", graph_sync_status="done"))
    db.commit()
    return kept, other, bot


@pytest.fixture
def maria_cannot_be_erased(monkeypatch):
    """The repeat of Maria's erasure fails in the database, as one on a
    restored row it cannot read does: the session is left to roll back,
    and the error's message quotes a value."""
    from sqlalchemy import text

    def fails(db, tenant_id, end_user_id, **_kw):
        db.execute(text("SELECT CAST('maria-secret' AS integer)"))

    monkeypatch.setattr("korely_agent.services.facts.service.forget_end_user", fails)


def test_a_record_that_fails_stops_no_other(db, tenant, maria_cannot_be_erased, caplog):
    kept, other, bot = _two_accounts_erased(db, tenant)
    out = erasure_replay.replay(db, kept)

    assert [(f["id"], f["scope"]) for f in out["failed"]] == [(kept[0]["id"], "user")]
    assert out["replayed"] == {"agent": 1}, "the other account's erasure was not made again"
    db.expire_all()
    assert db.get(AgentMemory, bot.id) is None
    assert db.get(AgentForgetAudit, uuid.UUID(kept[1]["id"])) is not None
    assert db.get(AgentForgetAudit, uuid.UUID(kept[0]["id"])) is None, "a failed record is not back"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert kept[0]["id"] in logged and "maria" not in logged

    # A run again repeats the record that failed, and only it.
    again = erasure_replay.replay(db, kept)
    assert [f["id"] for f in again["failed"]] == [kept[0]["id"]]
    assert again["in_the_dump"] == 1 and again["replayed"] == {}


def test_the_command_names_the_records_that_failed_and_exits_non_zero(
        db, tenant, maria_cannot_be_erased, monkeypatch, capsys):
    kept, _other, _bot = _two_accounts_erased(db, tenant)
    from korely_agent.services.pseudonyms import key_check

    code, err = _replay_via_manage(monkeypatch, capsys, {"key_check": key_check(), "records": kept})
    assert code == 4, err
    assert kept[0]["id"] in err and "Run the replay again with the same file" in err
    assert "maria" not in err, "the person's id or a value of the error"


def test_restore_says_what_to_do_when_a_record_fails():
    script = (Path(__file__).resolve().parent.parent / "deploy" / "restore.sh").read_text()
    assert "repeats only what is not made yet" in script
    from tests import docs_text

    assert "exits\n4 with the API kept stopped" in docs_text.text() or "exits 4" in docs_text.flat()


def test_a_restored_job_stored_with_a_lone_surrogate_fails_no_record(db, tenant):
    """The finding's own case: the dump holds a job queued before the rule
    refused a lone surrogate (the audit before 1.0.0), in the account whose end user was erased after the dump.
    Reading the account's batch items raised on it, so this record failed;
    now nothing does, and both accounts' erasures are made again."""
    from sqlalchemy import text

    kept, _other, bot = _two_accounts_erased(db, tenant)
    db.execute(text(
        "INSERT INTO agent_batch_jobs (id, tenant_id, project_id, status, received, payload)"
        " VALUES (:id, :t, :p, 'pending', 1, CAST(:payload AS json))"),
        {"id": uuid.uuid4(), "t": tenant.id, "p": seeded_project_id(db, tenant.id),
         "payload": '[{"user_id": "maria", "content": "x\\ud800y"}]'})
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["failed"] == [] and out["replayed"] == {"user": 1, "agent": 1}, out
    assert _alive(db, tenant) == []
