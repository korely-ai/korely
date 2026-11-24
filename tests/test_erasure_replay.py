"""The erasures made after a dump survive its restore (audit of 17 Oct 2026).

restore.sh replaced the database with the dump, bringing back the people
erased since, and the README's recipe to repeat them by hand could not be
followed: the erased end user is only a pseudonym in the records. Now the
live database's erasure records are kept before the restore and the ones the
dump lacks are made again after it (services/erasure_replay.py). A restore is
played here by putting the dump's rows back and dropping the record."""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

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


#: When the rows of a dump were written: before any erasure a test makes,
#: as a dump older than the erasures holds them (a replay tells someone who
#: came back after an erasure by what they wrote since, `_came_back`).
DUMPED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _memory(db, tenant, pid, user, agent=None, text="Maria's card ends in 4242."):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=user,
                    agent_id=agent, content=text, graph_sync_status="done", created_at=DUMPED)
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
                           agent_id=agent, content="restored", graph_sync_status="done",
                           created_at=DUMPED))
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
                      end_user_id="maria", ts=DUMPED))
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


def test_a_rotated_key_replays_while_the_old_one_is_a_fallback(db, tenant, monkeypatch, capsys):
    """SECRET_KEY rotated after the erasure, the old value in
    SECRET_KEY_FALLBACKS (Django's rule, services/pseudonyms.secrets): the
    restore finds the end user by the pseudonym made under the old key and
    erases them again (review of 15 Nov 2026: refused, as under any other
    key). The refusal without the fallback is the test above."""
    pid, maria = _maria_erased_then_restored(db, tenant)
    kept = _export_via_manage(capsys)
    _restore(db, tenant, pid, [("maria", None, maria.id)])

    _keyed_as(monkeypatch, "y" * 48)
    monkeypatch.setenv("SECRET_KEY_FALLBACKS", "x" * 48)
    from korely_agent import config

    config.get_settings.cache_clear()
    code, err = _replay_via_manage(monkeypatch, capsys, kept)
    assert code == 0, err
    assert _alive(db, tenant) == ["bob"]


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
    for pid, user, agent, words in rows:
        _memory(db, tenant, pid, user, agent=agent, text=words)


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
                   "not_made": {}, "came_back": {}, "failed": []}, "one erasure, counted once"
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
                       content="restored", graph_sync_status="done", created_at=DUMPED))
    db.add(AgentMemory(id=bot.id, tenant_id=other.id, project_id=oid, end_user_id="olga",
                       agent_id="helper", content="restored", graph_sync_status="done",
                       created_at=DUMPED))
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
        "INSERT INTO agent_batch_jobs (id, tenant_id, project_id, status, received, payload,"
        " created_at) VALUES (:id, :t, :p, 'pending', 1, CAST(:payload AS json), :dumped)"),
        {"id": uuid.uuid4(), "t": tenant.id, "p": seeded_project_id(db, tenant.id),
         "dumped": DUMPED,
         "payload": '[{"user_id": "maria", "content": "x\\ud800y"}]'})
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["failed"] == [] and out["replayed"] == {"user": 1, "agent": 1}, out
    assert _alive(db, tenant) == []


# The file of erasures is kept beside the dumps (deploy/backup.sh) and copied
# off the machine with them: it names no end user in clear. The erasure of one
# memory keeps, in the database, the id of the person it was of, who may still
# be there, and the file carried it in clear until 1.0.31.

def test_the_file_names_the_end_user_of_a_memorys_erasure_by_pseudonym(db, tenant, capsys):
    from korely_agent.services.pseudonyms import pseudonym

    pid = seeded_project_id(db, tenant.id)
    one = _memory(db, tenant, pid, "carla")
    _memory(db, tenant, pid, "carla", text="Carla lives in Turin.")
    agent_facts.erase_memory(db, tenant.id, one.id, project_id=EVERY_PROJECT)
    kept = _export_via_manage(capsys)
    assert "carla" not in str(kept), "the file names a living end user in clear"
    [rec] = kept["records"]
    assert rec["scope"] == "memory" and rec["end_user_id"] == pseudonym(tenant.id, "carla")


def test_a_restore_after_the_live_database_is_lost_keeps_the_erased_rows_gone(
        db, tenant, monkeypatch, capsys):
    """An older dump restored when the live database is gone: the erasures
    come back from the file kept beside the dumps, the people and memories
    erased after the dump stay gone, the others are back, and each record
    goes back as the live database had it."""
    pid = seeded_project_id(db, tenant.id)
    maria, bob = _memory(db, tenant, pid, "maria"), _memory(db, tenant, pid, "bob")
    card = _memory(db, tenant, pid, "carla")
    carla = _memory(db, tenant, pid, "carla", text="Carla lives in Turin.")
    dump = [("maria", None, maria.id), ("bob", None, bob.id),
            ("carla", None, card.id), ("carla", None, carla.id)]
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)
    agent_facts.erase_memory(db, tenant.id, card.id, project_id=EVERY_PROJECT)
    live = sorted((r.scope, r.end_user_id) for r in db.query(AgentForgetAudit))
    journal = _export_via_manage(capsys)        # the hourly file, off the machine

    # The live database is gone: the dump alone, rows and all.
    db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id).delete()
    _restore(db, tenant, pid, dump)
    assert _alive(db, tenant) == ["bob", "carla", "carla", "maria"]
    code, err = _replay_via_manage(monkeypatch, capsys, journal)

    assert code == 0, err
    assert _alive(db, tenant) == ["bob", "carla"]
    db.expire_all()
    assert db.get(AgentMemory, card.id) is None and db.get(AgentMemory, carla.id) is not None
    assert sorted((r.scope, r.end_user_id) for r in db.query(AgentForgetAudit)) == live


def test_someone_who_came_back_after_the_erasure_keeps_what_they_wrote_since(
        db, tenant, monkeypatch, capsys):
    """The review of 16 Nov 2026: a file of erasures is kept beside the dumps
    until the newest holds its records, and after a loss the newest never
    does. maria, erased, came back with a new memory, and a later restore
    made the file again and erased it, with exit 0, as every restore after
    would. An erasure takes what was there when it was made: what she held
    before it goes, what she wrote since stays; an agent namespace the same."""
    pid = seeded_project_id(db, tenant.id)
    before, bob = _memory(db, tenant, pid, "maria"), _memory(db, tenant, pid, "bob")
    bots = _memory(db, tenant, pid, "bob", agent="planner")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)
    agent_facts.delete_agent(db, tenant.id, "planner", project_id=pid)
    kept = _export_via_manage(capsys)
    erased_at = max(datetime.fromisoformat(r["created_at"]) for r in kept["records"])

    # The restored data holds her memory of before (a dump older than the
    # erasure, restored without its replay) and the one she wrote since.
    _restore(db, tenant, pid, [("maria", None, before.id), ("bob", "planner", bots.id)])
    since = []
    for who, agent in (("maria", None), ("bob", "planner")):
        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=who,
                        agent_id=agent, content="Moved to Rome in December.",
                        graph_sync_status="done", created_at=erased_at + timedelta(minutes=5))
        db.add(m)
        since.append(m.id)
    db.commit()

    out = erasure_replay.replay(db, *erasure_replay.read_file(kept))
    assert out["came_back"] == {"user": 1, "agent": 1} and out["failed"] == [], out
    db.expire_all()
    assert db.get(AgentMemory, before.id) is None and db.get(AgentMemory, bots.id) is None, \
        "what was there when the erasure was made goes"
    assert all(db.get(AgentMemory, m) is not None for m in since), "what they wrote since stays"
    assert db.get(AgentMemory, bob.id) is not None
    assert sorted(str(r.id) for r in db.query(AgentForgetAudit)) == \
        sorted(r["id"] for r in kept["records"]), "the original records, and no other"

    code, err = _replay_via_manage(monkeypatch, capsys, kept)
    assert code == 0, err
    assert all(db.get(AgentMemory, m) is not None for m in since), "and at every replay after"


def test_a_fact_extracted_late_from_a_memory_of_before_is_not_coming_back(db, tenant):
    """A dump older than the erasure, restored without its replay, whose
    queued memory had its fact extracted after the erasure: that fact is
    newer, and says nothing of the person writing again. The whole erasure
    is made again; a fact posted by itself since would have kept them."""
    from korely_agent.db.models import AgentFact

    pid = seeded_project_id(db, tenant.id)
    old = _memory(db, tenant, pid, "maria")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)
    kept = erasure_replay.export(db)
    erased_at = datetime.fromisoformat(kept[0]["created_at"])
    _restore(db, tenant, pid, [("maria", None, old.id)])
    db.add(AgentFact(tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                     subject_label="maria", predicate_raw="likes", predicate_canonical="likes",
                     object_label="coffee", source_memory_id=old.id,
                     source_memory_ids=[str(old.id)], valid_from=DUMPED,
                     created_at=erased_at + timedelta(minutes=5)))
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["replayed"] == {"user": 1} and out["came_back"] == {}, out
    assert _alive(db, tenant) == []


def test_back_with_one_fact_by_another_key_everything_of_before_goes(db, tenant):
    """The review of 16 Nov 2026, round 3: the replay kept, of someone who
    came back, every place but their memories from before the erasure (a
    fact they posted by hand, a name they declared). Now the erasure is
    made again by its own door up to its moment, in every place of the
    person; here they came back with one fact alone, posted by another key."""
    from korely_agent.db.models import AgentEntityAlias, AgentFact

    pid = seeded_project_id(db, tenant.id)
    old = _memory(db, tenant, pid, "maria")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)
    kept = erasure_replay.export(db)
    erased_at = datetime.fromisoformat(kept[0]["created_at"])

    def fact(created_at, obj):
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                      subject_label="maria", predicate_raw="lives in",
                      predicate_canonical="lives_in", object_label=obj, valid_from=DUMPED,
                      created_at=created_at)
        db.add(f)
        return f.id

    _restore(db, tenant, pid, [("maria", None, old.id)])
    by_hand = fact(DUMPED, "Turin")
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                            alias_lower="my wife", alias_label="My Wife",
                            canonical_label="Dummy Rossi", created_at=DUMPED, since=DUMPED))
    since = fact(erased_at + timedelta(minutes=5), "Rome")
    db.commit()

    out = erasure_replay.replay(db, kept)
    assert out["came_back"] == {"user": 1} and out["failed"] == [], out
    db.expire_all()
    assert db.get(AgentMemory, old.id) is None and db.get(AgentFact, by_hand) is None
    assert db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant.id).count() == 0
    assert db.get(AgentFact, since) is not None, "what was written after the erasure stays"


def test_a_row_written_while_the_erasure_was_open_is_older_than_its_record(
        db, tenant, engine, monkeypatch):
    """The review of 16 Nov 2026, round 3: the record said the start of the
    erasure's transaction (now()), so a memory written by another call 1.5 s
    into it was newer than the erasure, and a replay kept it as the person
    writing again. The record is written once the rows went, with the time
    of its own statement: such a memory is older than it, and the replay
    takes it."""
    from sqlalchemy.orm import sessionmaker

    from korely_agent.services import pseudonyms

    pid = seeded_project_id(db, tenant.id)
    old = _memory(db, tenant, pid, "maria").id
    window = uuid.uuid4()
    real = pseudonyms.after_an_erasure

    def another_call_meanwhile(*args, **kwargs):
        # After the rows went, before the record: another call writes for
        # her, in its own transaction, and commits.
        other = sessionmaker(bind=engine)()
        try:
            other.execute(text("SET LOCAL lock_timeout = '5s'"))
            other.add(AgentMemory(id=window, tenant_id=tenant.id, project_id=pid,
                                  end_user_id="maria", content="written meanwhile",
                                  graph_sync_status="done"))
            other.commit()
        finally:
            other.close()
        return real(*args, **kwargs)

    monkeypatch.setattr(pseudonyms, "after_an_erasure", another_call_meanwhile)
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)
    monkeypatch.setattr(pseudonyms, "after_an_erasure", real)
    written = db.query(AgentMemory.created_at).filter(AgentMemory.id == window).scalar()
    (recorded,) = db.query(AgentForgetAudit.created_at).filter(
        AgentForgetAudit.scope == "user").one()
    assert written < recorded, "the record is of when the erasure ended"

    # Written after the erasure's deletes ran, it outlived them; the
    # restored data holds it beside her memory of before.
    assert db.query(AgentMemory.id).filter(AgentMemory.id == window).scalar() == window
    kept = erasure_replay.export(db)
    _restore(db, tenant, pid, [("maria", None, old)])
    out = erasure_replay.replay(db, kept)
    assert out["replayed"] == {"user": 1} and out["came_back"] == {}, out
    assert _alive(db, tenant) == []


# ── round 4 of the review of 16 Nov 2026 ────────────────────────────────────

def _erased_then_back(db, tenant, user="maria"):
    """`user` erased in the seeded project, then a restore that brought back
    their memory of before: (project, the memory of before, the file, the
    erasure's moment)."""
    pid = seeded_project_id(db, tenant.id)
    old = _memory(db, tenant, pid, user)
    agent_facts.forget_end_user(db, tenant.id, user, project_id=pid)
    kept = erasure_replay.export(db)
    moment = datetime.fromisoformat(kept[0]["created_at"])
    _restore(db, tenant, pid, [(user, None, old.id)])
    return pid, old, kept, moment


def test_a_fact_restated_since_stays_without_the_memories_of_before(db, tenant):
    """Review, round 4: a memory restating a fact joins its sources on the
    row of before (`store.reconfirm`), and the replay erased the row by its
    date, with what the memory written since said. Now a fact is decided by
    the memories it comes from: one a memory written since states stays,
    without the memories of before among its sources; one only they state
    goes. The vocabulary follows the facts that stay (a phrase by its own
    date, a relation while a fact that stays is stored under it), a name by
    when it was last stated, a read mark by its last read."""
    from korely_agent.db.models import (AgentEntityAlias, AgentFact, AgentReadMark,
                                        PredicateAlias, PredicateCluster)

    pid, old, kept, moment = _erased_then_back(db, tenant)
    later = moment + timedelta(minutes=5)
    new = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                      content="Maria lives in Turin.", graph_sync_status="done",
                      created_at=later)
    db.add(new)
    db.flush()

    def fact(predicate, obj, sources):
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                      subject_label="maria", predicate_raw=predicate.replace("_", " "),
                      predicate_canonical=predicate, object_label=obj,
                      source_memory_id=sources[0], source_memory_ids=[str(s) for s in sources],
                      observation_count=len(sources), valid_from=DUMPED, created_at=DUMPED)
        db.add(f)
        return f.id

    restated = fact("lives_in", "Turin", [old.id, new.id])
    alone = fact("likes", "coffee", [old.id])
    # Rows that name no memory, written since: posted by themselves (a JSON
    # null for their sources, as a write with none stores it), and two
    # corrections, of words of the memory of before and of the one since.
    by_hand = {}
    for name, first in (("posted", None), ("of_before", old.id), ("of_since", new.id)):
        row = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                        end_user_id="maria", subject_label="maria", predicate_raw="works at",
                        predicate_canonical="works_at", object_label=name,
                        source_memory_ids=None, first_memory_id=first, model_used="correction",
                        valid_from=DUMPED, created_at=later)
        db.add(row)
        by_hand[name] = row.id
    rel = {}
    for canonical, members in (("lives_in", 2), ("likes", 1)):
        rel[canonical] = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                                          end_user_id="maria", canonical=canonical,
                                          member_count=members, created_at=DUMPED)
        db.add(rel[canonical])
    db.flush()
    for surface, canonical, when in (("lives in", "lives_in", DUMPED),
                                     ("resides in", "lives_in", later),
                                     ("likes", "likes", DUMPED)):
        db.add(PredicateAlias(tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                              surface=surface, cluster_id=rel[canonical].id, created_at=when))
    for name, stated in (("my wife", later), ("the boss", DUMPED)):
        db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                                alias_lower=name, alias_label=name, canonical_label="Dummy Rossi",
                                created_at=DUMPED, since=stated))
    for reader, read in (("read-since", later), ("read-before", DUMPED)):
        db.add(AgentReadMark(tenant_id=tenant.id, project_id=pid, reader=reader, fact_id=restated,
                             end_user_id="maria", first_read_at=DUMPED, last_read_at=read))
    db.commit()

    out = erasure_replay.replay(db, kept)
    assert out["came_back"] == {"user": 1} and out["failed"] == [], out
    db.expire_all()
    f = db.get(AgentFact, restated)
    assert f is not None, "what the memory written since said stays"
    assert (f.source_memory_id, f.source_memory_ids, f.observation_count) == \
        (new.id, [str(new.id)], 1), "without the memory of before"
    assert db.get(AgentFact, alone) is None and db.get(AgentMemory, old.id) is None
    assert db.get(AgentMemory, new.id) is not None
    assert {n for n, fid in by_hand.items() if db.get(AgentFact, fid) is not None} == \
        {"posted", "of_since"}, "a correction made of the words of before goes with them"
    mine = PredicateCluster.tenant_id == tenant.id
    assert {c.canonical: c.member_count for c in db.query(PredicateCluster).filter(mine)} == \
        {"lives_in": 1}, "a relation no fact that stays is stored under goes"
    assert {v.surface for v in db.query(PredicateAlias).filter(
        PredicateAlias.tenant_id == tenant.id)} == {"resides in"}
    assert {a.alias_lower for a in db.query(AgentEntityAlias).filter(
        AgentEntityAlias.tenant_id == tenant.id)} == {"my wife"}
    assert {r.reader for r in db.query(AgentReadMark).filter(
        AgentReadMark.tenant_id == tenant.id)} == {"read-since"}


@pytest.mark.parametrize("back_with", ["name", "queued item"])
def test_back_with_anything_else_counts_as_back(db, tenant, back_with):
    """Review, round 4: `came_back` asked only the memories and the facts, so
    a person back with only a name or a queued /v1/batch item of theirs
    counted as replayed. Now it asks every place that holds a person
    (end_user_places.held_in)."""
    from korely_agent.db.models import AgentBatchJob, AgentEntityAlias

    pid, old, kept, moment = _erased_then_back(db, tenant)
    later = moment + timedelta(minutes=5)
    if back_with == "name":
        db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                                alias_lower="my wife", alias_label="My Wife",
                                canonical_label="Dummy Rossi", created_at=later, since=later))
    else:
        db.add(AgentBatchJob(tenant_id=tenant.id, project_id=pid, created_at=later,
                             payload=[{"content": "queued since", "user_id": "maria"}]))
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["came_back"] == {"user": 1} and out["replayed"] == {}, out
    assert db.get(AgentMemory, old.id) is None


def test_an_agent_back_with_only_a_name_counts_as_back(db, tenant):
    from korely_agent.db.models import AgentEntityAlias

    pid = seeded_project_id(db, tenant.id)
    bots = _memory(db, tenant, pid, "bob", agent="planner")
    agent_facts.delete_agent(db, tenant.id, "planner", project_id=pid)
    kept = erasure_replay.export(db)
    later = datetime.fromisoformat(kept[0]["created_at"]) + timedelta(minutes=5)
    _restore(db, tenant, pid, [("bob", "planner", bots.id)])
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=pid, agent_id="planner",
                            alias_lower="the boss", alias_label="The Boss",
                            canonical_label="Dummy Bruni", created_at=later, since=later))
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["came_back"] == {"agent": 1} and out["replayed"] == {}, out
    assert db.get(AgentMemory, bots.id) is None


def test_the_record_put_back_keeps_the_moment_for_the_next_restore(db, tenant):
    """Review, round 4: nothing held the date of the record the replay puts
    back. Dated at the restore, the next restore (its file made from this
    database) would erase what the person wrote between the erasure and the
    first restore."""
    pid, old, kept, moment = _erased_then_back(db, tenant)
    since = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                        content="Back right after.", graph_sync_status="done",
                        created_at=moment + timedelta(microseconds=1))
    db.add(since)
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["came_back"] == {"user": 1}, out
    (put,) = db.query(AgentForgetAudit.created_at).filter(AgentForgetAudit.scope == "user").one()
    assert put == moment, "the original record's date"

    again = erasure_replay.export(db)
    _restore(db, tenant, pid, [("maria", None, old.id)])
    out = erasure_replay.replay(db, again)
    assert out["came_back"] == {"user": 1} and out["failed"] == [], out
    db.expire_all()
    assert db.get(AgentMemory, since.id) is not None and db.get(AgentMemory, old.id) is None


def test_what_was_written_at_the_very_moment_goes(db, tenant):
    """The moment is when the erasure ended (`facts.erasure._ENDED`): a row
    dated exactly then is older than the erasure's end, not newer, and goes,
    a memory, the fact it states and a fact posted by itself alike."""
    from korely_agent.db.models import AgentFact

    pid, old, kept, moment = _erased_then_back(db, tenant)
    at = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                     content="At the moment.", graph_sync_status="done", created_at=moment)
    db.add(at)
    db.flush()
    at = at.id
    facts = []
    for source, obj in ((at, "Turin"), (None, "Acme")):
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                      subject_label="maria", predicate_raw="is at", predicate_canonical="is_at",
                      object_label=obj, source_memory_id=source, valid_from=DUMPED,
                      created_at=moment)
        db.add(f)
        facts.append(f.id)
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["replayed"] == {"user": 1} and out["came_back"] == {}, out
    db.expire_all()
    assert db.get(AgentMemory, at) is None
    assert all(db.get(AgentFact, f) is None for f in facts)


def test_an_agents_row_written_while_its_erasure_was_open_is_older_than_its_record(
        db, tenant, engine, monkeypatch):
    """As for an end user (`test_a_row_written_while_the_erasure_was_open_is_
    older_than_its_record`), the agent namespace's door: its record is dated
    once its rows went (review, round 4: only the end user's door was held)."""
    from sqlalchemy.orm import sessionmaker

    from korely_agent.services.facts import erasure

    pid = seeded_project_id(db, tenant.id)
    old = _memory(db, tenant, pid, "bob", agent="planner").id
    window = uuid.uuid4()
    real = erasure._erase_examples

    def another_call_meanwhile(*args, **kwargs):
        other = sessionmaker(bind=engine)()
        try:
            other.execute(text("SET LOCAL lock_timeout = '5s'"))
            other.add(AgentMemory(id=window, tenant_id=tenant.id, project_id=pid,
                                  end_user_id="bob", agent_id="planner",
                                  content="written meanwhile", graph_sync_status="done"))
            other.commit()
        finally:
            other.close()
        return real(*args, **kwargs)

    monkeypatch.setattr(erasure, "_erase_examples", another_call_meanwhile)
    agent_facts.delete_agent(db, tenant.id, "planner", project_id=pid)
    monkeypatch.setattr(erasure, "_erase_examples", real)
    written = db.query(AgentMemory.created_at).filter(AgentMemory.id == window).scalar()
    (recorded,) = db.query(AgentForgetAudit.created_at).filter(
        AgentForgetAudit.scope == "agent").one()
    assert written < recorded, "the record is of when the erasure ended"
    kept = erasure_replay.export(db)
    _restore(db, tenant, pid, [("bob", "planner", old)])
    out = erasure_replay.replay(db, kept)
    assert out["replayed"] == {"agent": 1} and out["came_back"] == {}, out
    assert _alive(db, tenant) == []


def test_what_names_them_since_keeps_their_id(db, tenant):
    """The renamers of a record are held to the moment too: a record of
    before naming the person in clear (a forget of one of their memories)
    takes the pseudonym, one written since keeps the id; and of the names
    the upgrade could not give a project (`project_not_recorded`), one
    stated since stays (review, round 4: no test held either)."""
    from korely_agent.db.models import AgentEntityAlias
    from korely_agent.services.ids import mem_public_id
    from korely_agent.services.pseudonyms import pseudonym

    pid, old, kept, moment = _erased_then_back(db, tenant)
    later = moment + timedelta(minutes=5)
    for when in (DUMPED, later):
        db.add(AgentForgetAudit(tenant_id=tenant.id, project_id=pid, scope="memory",
                                end_user_id="maria", target_id=mem_public_id(uuid.uuid4()),
                                memories_forgotten=1, created_at=when))
    for name, when in (("my aunt", DUMPED), ("my uncle", later)):
        db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=None, end_user_id="maria",
                                alias_lower=name, alias_label=name, canonical_label="Dummy Verdi",
                                project_not_recorded=True, created_at=DUMPED, since=when))
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["came_back"] == {"user": 1} and out["failed"] == [], out
    db.expire_all()
    named = {r.created_at: r.end_user_id for r in db.query(AgentForgetAudit).filter(
        AgentForgetAudit.scope == "memory")}
    assert named == {DUMPED: pseudonym(tenant.id, "maria"), later: "maria"}, named
    assert {a.alias_lower for a in db.query(AgentEntityAlias).filter(
        AgentEntityAlias.tenant_id == tenant.id)} == {"my uncle"}


def test_what_a_fact_restated_since_had_closed_stays_closed(db, tenant):
    """The agent namespace's door gives back what its erased facts had
    closed (facts/given_back.py); a fact a memory of the namespace written
    since restated stays, and what it closed stays closed (review, round 4:
    the facts the door read were those of before by their date)."""
    from korely_agent.db.models import AgentFact

    pid = seeded_project_id(db, tenant.id)
    old = _memory(db, tenant, pid, "bob", agent="planner")
    helper = _memory(db, tenant, pid, "bob", agent="helper", text="Bob lives in Milan.")
    agent_facts.delete_agent(db, tenant.id, "planner", project_id=pid)
    kept = erasure_replay.export(db)
    later = datetime.fromisoformat(kept[0]["created_at"]) + timedelta(minutes=5)
    _restore(db, tenant, pid, [("bob", "planner", old.id)])
    new = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="bob",
                      agent_id="planner", content="Bob lives in Turin.",
                      graph_sync_status="done", created_at=later)
    db.add(new)
    db.flush()
    moved = DUMPED + timedelta(days=30)

    def lives_in(agent, obj, sources, **kw):
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="bob",
                      agent_id=agent, subject_label="bob", predicate_raw="lives in",
                      predicate_canonical="lives_in", object_label=obj,
                      source_memory_id=sources[0], source_memory_ids=[str(s) for s in sources],
                      observation_count=len(sources), created_at=DUMPED, **kw)
        db.add(f)
        db.flush()
        return f.id

    turin = lives_in("planner", "Turin", [old.id, new.id], valid_from=moved)
    lives_in("helper", "Milan", [helper.id], valid_from=DUMPED, invalid_at=moved,
             invalidated_by_id=turin)
    # A fact of the namespace another namespace's memory restated: the
    # erasure of the namespace takes its facts, as it took them when made;
    # only a memory of its own written since keeps one.
    also = lives_in("planner", "Rome", [old.id, helper.id], valid_from=DUMPED,
                    invalid_at=DUMPED + timedelta(days=1))
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["came_back"] == {"agent": 1} and out["failed"] == [], out
    db.expire_all()
    assert db.get(AgentFact, turin) is not None and db.get(AgentFact, also) is None
    assert db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id,
                                      AgentFact.object_label == "Milan",
                                      AgentFact.invalid_at.is_(None)).count() == 0, \
        "what a fact that stays closed was given back"


# ── round 5 of the review of 16 Nov 2026 ────────────────────────────────────

def _since_memory(db, tenant, pid, when, *, agent=None, kind=None, user="maria"):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=user,
                    agent_id=agent, kind=kind, content="Written since.",
                    graph_sync_status="done", created_at=when)
    db.add(m)
    db.flush()
    return m


def _a_fact(db, tenant, pid, predicate, obj, sources, created, *, user="maria", agent=None,
            single=False, **kw):
    """A fact of `user` stated by `sources`; `single`: in the single-source
    form (`source_memory_id` only, no list), as one memory's write leaves it."""
    from korely_agent.db.models import AgentFact

    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=user,
                  agent_id=agent, subject_label=user, predicate_raw=predicate.replace("_", " "),
                  predicate_canonical=predicate, object_label=obj, source_memory_id=sources[0],
                  source_memory_ids=None if single else [str(s) for s in sources],
                  observation_count=len(sources), created_at=created,
                  valid_from=kw.pop("valid_from", DUMPED), **kw)
    db.add(f)
    db.flush()
    return f.id


def _open_rows(db, tenant, obj):
    from korely_agent.db.models import AgentFact

    db.expire_all()
    return db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id,
                                      AgentFact.object_label == obj,
                                      AgentFact.invalid_at.is_(None)).all()


@pytest.mark.parametrize("beside", ["nothing", "a value said since"])
def test_a_kept_fact_closed_by_a_fact_that_goes_is_given_back(db, tenant, beside):
    """Review, round 5 (P1): a fact a memory written since restated was kept
    closed by a fact that goes, extracted late from the memory of before:
    the person's current statement read as over. The end user's door gives
    back what the facts that go had closed, as the agent's door does
    (facts/given_back.py): a new open row of the same triple, stated by the
    memory written since, beginning where the kept row ended. Not when a
    fact that stays says something else of the same relation (rule (c))."""
    from korely_agent.db.models import AgentFact

    pid, old, kept, moment = _erased_then_back(db, tenant)
    new = _since_memory(db, tenant, pid, moment + timedelta(minutes=1))
    ended = moment + timedelta(minutes=5)
    late = _a_fact(db, tenant, pid, "lives_in", "Rome", [old.id], ended, valid_from=ended)
    turin = _a_fact(db, tenant, pid, "lives_in", "Turin", [old.id, new.id], DUMPED,
                    invalid_at=ended, invalidated_by_id=late,
                    closed_known_at=moment + timedelta(minutes=2))
    if beside != "nothing":
        other = _since_memory(db, tenant, pid, moment + timedelta(minutes=3))
        _a_fact(db, tenant, pid, "lives_in", "Milan", [other.id], moment + timedelta(minutes=3),
                valid_from=ended)
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["came_back"] == {"user": 1} and out["failed"] == [], out
    row = db.get(AgentFact, turin)
    assert row is not None and row.invalid_at is not None, "the kept row is not rewritten"
    assert db.get(AgentFact, late) is None
    back = _open_rows(db, tenant, "Turin")
    if beside == "nothing":
        assert [(r.source_memory_ids, r.valid_from) for r in back] == [([str(new.id)], ended)]
    else:
        assert back == [], "a fact that stays says otherwise: it stays closed"


def test_a_correction_made_since_goes_with_the_words_of_before(db, tenant):
    """The correction case of the review's round 5: a correction made since of a
    fact the memory of before first stated is made of that memory's words,
    and goes with it, as when one memory is erased (`facts.erasure.
    _made_of_its_words`); the fact it corrected stays closed, since the
    correction's record says the caller ended it (given_back rule (b))."""
    from korely_agent.db.models import AgentFact

    pid, old, kept, moment = _erased_then_back(db, tenant)
    new = _since_memory(db, tenant, pid, moment + timedelta(microseconds=1))
    turin = _a_fact(db, tenant, pid, "lives_in", "Turin", [old.id, new.id], DUMPED)
    db.commit()
    agent_facts.correct_fact(db, tenant.id, turin, obj="Rome", project_id=pid)
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["came_back"] == {"user": 1} and out["failed"] == [], out
    assert _open_rows(db, tenant, "Rome") == [] and _open_rows(db, tenant, "Turin") == []
    assert db.get(AgentFact, turin) is not None


def test_what_a_fact_extracted_late_taught_goes_with_it(db, tenant):
    """Review, round 5 (P2, P3): a fact extracted late from a memory of
    before goes, and the relation, the phrase and the name it taught, all
    newer than the moment, stayed by their dates. A name now goes with the
    fact it was learned from (`facts.erasure.aliases_learned_from`, as the
    erasure of one memory), a relation when no fact of theirs that stays is
    stored under it, whenever learned and with every phrase it has (another
    end user's facts are another vocabulary)."""
    from korely_agent.db.models import AgentEntityAlias, PredicateAlias, PredicateCluster

    pid, old, kept, moment = _erased_then_back(db, tenant)
    later = moment + timedelta(minutes=5)
    new = _since_memory(db, tenant, pid, later)
    _a_fact(db, tenant, pid, "likes", "tea", [new.id], later)
    late = _a_fact(db, tenant, pid, "takes_insulin_for", "diabetes", [old.id], later)
    bob = _memory(db, tenant, pid, "bob", text="Bob takes insulin for diabetes.")
    _a_fact(db, tenant, pid, "takes_insulin_for", "diabetes", [bob.id], DUMPED, user="bob")
    for canonical, learned, phrase, phrase_learned in (
            ("takes_insulin_for", later, "takes insulin for", later),
            ("enjoys", DUMPED, "is fond of", later)):
        rel = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid,
                               end_user_id="maria", canonical=canonical, member_count=1,
                               created_at=learned)
        db.add(rel)
        db.flush()
        db.add(PredicateAlias(tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                              surface=phrase, cluster_id=rel.id, created_at=phrase_learned))
    # Two facts that go, each the source of a name: both names go.
    late_too = _a_fact(db, tenant, pid, "works_for", "the boss", [old.id], later)
    for name, label, fact in (("my wife anna", "Anna Rossi", late),
                              ("the boss", "Dummy Bruni", late_too)):
        db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=pid, end_user_id="maria",
                                alias_lower=name, alias_label=name, canonical_label=label,
                                created_at=later, since=later, source_fact_id=fact))
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["came_back"] == {"user": 1} and out["failed"] == [], out
    db.expire_all()
    assert db.query(PredicateCluster).filter(PredicateCluster.tenant_id == tenant.id,
                                             PredicateCluster.end_user_id == "maria").count() == 0
    assert db.query(PredicateAlias).filter(PredicateAlias.tenant_id == tenant.id).count() == 0
    assert db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant.id).count() == 0
    assert [r.end_user_id for r in _open_rows(db, tenant, "diabetes")] == ["bob"]


def test_an_agents_facts_go_whoever_restated_them(db, tenant):
    """Review, round 5 (P4, O5): the erasure of an agent namespace takes every
    fact of the namespace, as it did when it was made: a fact another
    namespace's memory written since restated goes, and that memory stays,
    without it. A fact that names a memory is never one posted by itself,
    in the single-source form too."""
    from korely_agent.db.models import AgentFact

    pid = seeded_project_id(db, tenant.id)
    old = _memory(db, tenant, pid, "maria", agent="planner")
    agent_facts.delete_agent(db, tenant.id, "planner", project_id=pid)
    kept = erasure_replay.export(db)
    later = datetime.fromisoformat(kept[0]["created_at"]) + timedelta(minutes=5)
    _restore(db, tenant, pid, [("maria", "planner", old.id)])
    helper = _since_memory(db, tenant, pid, later, agent="helper")
    facts = [_a_fact(db, tenant, pid, "lives_in", "Turin", [old.id, helper.id], DUMPED,
                     agent="planner"),
             _a_fact(db, tenant, pid, "works_at", "Acme", [helper.id], later, agent="planner",
                     single=True)]
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["replayed"] == {"agent": 1} and out["came_back"] == {}, out
    db.expire_all()
    assert all(db.get(AgentFact, f) is None for f in facts)
    assert db.get(AgentMemory, helper.id) is not None


def test_every_memory_written_since_is_asked_and_its_facts_recounted(db, tenant, monkeypatch):
    """Review, round 5 (O2, O3): the memories written since are asked
    `_SOURCES` at a time, and a fact that keeps only a work session's memory
    is `session_only` again, as after the erasure of one memory
    (`facts.erasure.recount_session_only`)."""
    from korely_agent.db.models import AgentFact
    from korely_agent.services import end_user_places

    monkeypatch.setattr(end_user_places, "_SOURCES", 2)
    pid, old, kept, moment = _erased_then_back(db, tenant)
    restated = []
    for i, kind in enumerate((None, "session", None)):
        new = _since_memory(db, tenant, pid, moment + timedelta(minutes=i + 1), kind=kind)
        restated.append((_a_fact(db, tenant, pid, "visited", f"city {i}", [old.id, new.id],
                                 DUMPED), new.id))
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["came_back"] == {"user": 1} and out["failed"] == [], out
    db.expire_all()
    rows = [db.get(AgentFact, f) for f, _ in restated]
    assert [(r.source_memory_ids if r else None) for r in rows] == \
        [[str(m)] for _, m in restated], "each memory written since keeps its fact"
    assert [r.session_only for r in rows] == [False, True, False]


def test_a_relation_used_in_another_project_still_goes_in_the_erased_one(db, tenant):
    """Review, round 6 (Y2): the vocabulary is one per end user of a project
    (18 Oct 2026). A relation of the project the erasure was made in goes
    when no fact of theirs that stays there is stored under it, even when
    their facts in another project are stored under the same name."""
    from korely_agent.db.models import PredicateAlias, PredicateCluster

    pid, old, kept, moment = _erased_then_back(db, tenant)
    other = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Other", slug="other",
                         is_default=False)
    db.add(other)
    db.flush()
    there = _memory(db, tenant, other.id, "maria", text="Maria takes insulin for diabetes.")
    _a_fact(db, tenant, other.id, "takes_insulin_for", "diabetes", [there.id], DUMPED)
    rels = {}
    for project in (pid, other.id):
        rels[project] = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant.id,
                                         project_id=project, end_user_id="maria",
                                         canonical="takes_insulin_for", member_count=1,
                                         created_at=DUMPED)
        db.add(rels[project])
        db.flush()
        db.add(PredicateAlias(tenant_id=tenant.id, project_id=project, end_user_id="maria",
                              surface=f"takes insulin for {project}",
                              cluster_id=rels[project].id, created_at=DUMPED))
    db.commit()
    out = erasure_replay.replay(db, kept)
    assert out["replayed"] == {"user": 1} and out["failed"] == [], out
    db.expire_all()
    assert {c.project_id for c in db.query(PredicateCluster).filter(
        PredicateCluster.tenant_id == tenant.id)} == {other.id}
    assert [v.project_id for v in db.query(PredicateAlias).filter(
        PredicateAlias.tenant_id == tenant.id)] == [other.id]
    assert [r.project_id for r in _open_rows(db, tenant, "diabetes")] == [other.id]
