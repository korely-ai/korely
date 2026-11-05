"""What an erasure left behind or let through, found by the audit of 17 and
18 Oct 2026:

* a project deleted while a write into it was under way: the write was moved
  into the default project by the erasure (an OAuth app's too, consented for
  that project only), or left with no project by the foreign key's SET NULL;
* the keys confined to an erased project's end users kept their ids: the keys
  were moved to the default project before the end users were pseudonymised
  within the erased one;
* a pseudonym erased again through the account-wide route relabelled the
  records proving the first erasure, and an id written before the prefix was
  reserved could not be erased at all;
* a cost row written after an erasure, or while one was open, named the
  erased end user in clear (since the audit of 19 Oct 2026 a cost row names nobody);
* the export's error named an end user's file on /health;
* the trail cut a list of confined end users inside an id, which the erasure
  then could not find; and it spelled an id with escapes there, which the
  erasure did not look for (audit of 19 Oct 2026).
"""
import importlib.util
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import (AgentApiKey, AgentForgetAudit, AgentMemory,
                                    AgentProject, AuditEvent)
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services import pseudonyms
from tests.conftest import seeded_project_id



@pytest.fixture
def keyed(monkeypatch):
    from korely_agent.config import get_settings

    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-tests")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def sessions(engine):
    made = []

    def make():
        s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
        made.append(s)
        return s

    yield make
    for s in made:
        s.rollback()
        s.close()


@pytest.fixture
def pilot(db, tenant):
    project_service.get_or_create_default_project(db, tenant.id)
    return project_service.create_project(db, tenant_id=tenant.id, name="Pilot")


def _memory(tenant_id, project_id, user="anna"):
    return AgentMemory(id=uuid.uuid4(), tenant_id=tenant_id, project_id=project_id,
                       end_user_id=user, content="user: I live in Turin.",
                       graph_sync_status="done")


def _delete_in_thread(sessions, tenant_id, project_id, erase):
    out = {}

    def run():
        s = sessions()
        try:
            out["result"] = project_service.delete_project(
                s, tenant_id, project_service.get_project(s, tenant_id, project_id), erase=erase)
        except Exception as e:  # noqa: BLE001 - reported to the test
            out["error"] = e

    t = threading.Thread(target=run)
    t.start()
    return t, out


# ── a project deleted while a write into it is under way ────────────────────

@pytest.mark.parametrize("erase", [True, False])
def test_a_write_under_way_is_erased_or_moved_never_orphaned(db, tenant, pilot, sessions, erase):
    """The write has inserted its memory and not committed yet when the
    deletion starts. The deletion waits for it, then erases it (counted) or
    moves it; before, the project's DELETE waited for it and the foreign key
    set its project to NULL, where no key reads it."""
    writer = sessions()
    mem = _memory(tenant.id, pilot.id)
    writer.add(mem)
    writer.flush()

    runner, out = _delete_in_thread(sessions, tenant.id, pilot.id, erase)
    runner.join(timeout=1.0)
    assert runner.is_alive(), "the deletion did not wait for the write under way"
    writer.commit()
    runner.join(timeout=10)
    assert not runner.is_alive() and "error" not in out, out.get("error")

    db.expire_all()
    left = db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id).all()
    if erase:
        assert left == [] and out["result"]["memories_deleted"] == 1
    else:
        default = project_service.get_or_create_default_project(db, tenant.id)
        assert [m.project_id for m in left] == [default.id]


@pytest.mark.parametrize("erase", [True, False])
def test_a_write_that_starts_while_the_project_goes_is_refused(db, tenant, pilot, sessions,
                                                               monkeypatch, erase):
    """A write stamped with the project (its key read before the deletion,
    an OAuth app's included) that starts while the deletion runs waits, and
    fails on the deleted project: before, it committed at once and the
    erasure filed it under the default project."""
    reached, release = threading.Event(), threading.Event()
    if erase:
        from korely_agent.services.facts import service as facts_service

        real = facts_service.erase_what_it_leaves

        def paused(*a, **k):
            reached.set()
            release.wait(10)
            return real(*a, **k)

        monkeypatch.setattr(facts_service, "erase_what_it_leaves", paused)
    else:
        real = project_service._fold_duplicates

        def paused(*a, **k):
            reached.set()
            release.wait(10)
            return real(*a, **k)

        monkeypatch.setattr(project_service, "_fold_duplicates", paused)

    runner, out = _delete_in_thread(sessions, tenant.id, pilot.id, erase)
    assert reached.wait(10)
    wrote = {}

    def write():
        s = sessions()
        try:
            s.add(_memory(tenant.id, pilot.id))
            s.commit()
            wrote["ok"] = True
        except IntegrityError:
            s.rollback()
            wrote["refused"] = True

    w = threading.Thread(target=write)
    w.start()
    w.join(timeout=1.0)
    assert w.is_alive(), "the write did not wait for the deletion"
    release.set()
    runner.join(timeout=10)
    w.join(timeout=10)
    assert "error" not in out, out.get("error")
    assert wrote == {"refused": True}
    db.expire_all()
    assert db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id).count() == 0


def test_an_erased_projects_confined_keys_keep_no_id(keyed, db, tenant, pilot):
    """The keys were moved to the default project before the project's end
    users were pseudonymised within it, so their lists kept the ids."""
    db.add(_memory(tenant.id, pilot.id, user="anna"))
    db.commit()
    only, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="anna-only",
                                         scopes=["memories:read"], project_id=pilot.id,
                                         end_users=["anna"])
    project_service.delete_project(db, tenant.id, db.get(AgentProject, pilot.id), erase=True)
    db.expire_all()
    only = db.get(AgentApiKey, only.id)
    assert only.end_users == [pseudonyms.pseudonym(tenant.id, "anna")]
    assert only.revoked_at is not None, "a key that sees nobody"


def test_a_confined_key_with_no_project_is_the_default_projects(keyed, db, tenant, pilot,
                                                                 client):
    """Audit of 19 Oct 2026: a key with no project (moved to none, or older than projects) reads and
    writes the Default project (keys.key_project), but the erasure picked
    the confined keys by `project_id = <Default>`, so such a key kept the
    erased id in clear and stayed live. An erasure in another project still
    leaves it alone."""
    default = seeded_project_id(db, tenant.id)
    db.add(_memory(tenant.id, default, user="anna"))
    db.add(_memory(tenant.id, pilot.id, user="bruno"))
    db.commit()

    def confined(user):
        row, _ = key_service.create_api_key(db, tenant_id=tenant.id, name=f"{user}-only",
                                            scopes=["memories:read"], project_id=default,
                                            end_users=[user])
        row.project_id = None
        db.commit()
        assert key_service.key_project(db, row) == default
        return row.id

    anna_key, bruno_key = confined("anna"), confined("bruno")

    def writer(project_id):
        _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"w-{uuid.uuid4()}",
                                              scopes=["memories:read", "memories:write"],
                                              project_id=project_id)
        return {"Authorization": f"Bearer {full}"}

    assert client.delete("/v1/users/anna/memories", headers=writer(default)).status_code == 200
    assert client.delete("/v1/users/bruno/memories", headers=writer(pilot.id)).status_code == 200
    db.expire_all()
    anna = db.get(AgentApiKey, anna_key)
    assert anna.end_users == [pseudonyms.pseudonym(tenant.id, "anna")]
    assert anna.revoked_at is not None, "a key that sees nobody"
    bruno = db.get(AgentApiKey, bruno_key)
    assert bruno.end_users == ["bruno"] and bruno.revoked_at is None, \
        "another project's erasure reached a Default project's key"


# ── a pseudonym is nobody to erase; a legacy id is somebody ─────────────────

@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_the_account_wide_erasure_refuses_a_pseudonym(keyed, db, tenant, client):
    pid = seeded_project_id(db, tenant.id)
    db.add(_memory(tenant.id, pid, user="maria"))
    db.commit()
    _r, plain = key_service.create_api_key(
        db, tenant_id=tenant.id, name="adm", scopes=["memories:read", "memories:write", "admin"])
    head = {"Authorization": f"Bearer {plain}"}
    assert client.delete("/admin/users/maria/memories", headers=head).status_code == 200
    p = pseudonyms.pseudonym(tenant.id, "maria")
    r = client.delete(f"/admin/users/{p}/memories", headers=head)
    assert r.status_code == 422, r.text
    db.expire_all()
    records = db.query(AgentForgetAudit).filter(AgentForgetAudit.scope == "user").all()
    assert [x.end_user_id for x in records] == [p], "the proof keeps its pseudonym"
    # And nothing relabels one, whoever calls.
    assert pseudonyms.after_an_erasure(db, tenant.id, p) == {}


def test_an_id_written_before_the_prefix_was_reserved_is_erasable(keyed, db, tenant, client):
    from korely_agent.services.ids import is_pseudonym

    legacy = "erased:before-the-rule"
    assert not is_pseudonym(legacy)
    assert is_pseudonym(pseudonyms.pseudonym(tenant.id, "maria"))
    pid = seeded_project_id(db, tenant.id)
    db.add(_memory(tenant.id, pid, user=legacy))
    db.commit()
    _r, plain = key_service.create_api_key(db, tenant_id=tenant.id, name="w",
                                           scopes=["memories:read", "memories:write"],
                                           project_id=pid)
    r = client.delete(f"/v1/users/{legacy}/memories",
                      headers={"Authorization": f"Bearer {plain}"})
    assert r.status_code == 200 and r.json()["memories_deleted"] == 1, r.text


def test_an_erased_project_erases_a_legacy_id_too(keyed, db, tenant, pilot):
    legacy = "erased:before-the-rule"
    db.add(_memory(tenant.id, pilot.id, user=legacy))
    db.commit()
    from korely_agent.services import end_user_places

    assert end_user_places.held_in(db, tenant.id, pilot.id) == [legacy]


# ── the cost rows after an erasure ──────────────────────────────────────────

def _cost_rows_naming(db, tenant_id, who) -> tuple:
    """(the account's cost rows, how many carry `who` in any column): read as
    JSON, so the check holds whatever columns the table has."""
    from sqlalchemy import text

    db.expire_all()
    rows = db.execute(text("SELECT to_jsonb(u)::text FROM agent_llm_usage u WHERE tenant_id = :t"),
                      {"t": tenant_id}).scalars().all()
    return rows, sum(who in r for r in rows)


def _search_as(session, tenant_id, who) -> None:
    """A search naming `who`: its query vector is one model call, written as a
    cost row in its own session when the search's scope closes."""
    from korely_agent.services.memory_engine import AgentMemoryEngine, MemorySearchUnavailable

    try:
        AgentMemoryEngine(session, tenant_id).search("where does she live", end_user_id=who)
    except MemorySearchUnavailable:
        pass


def test_a_cost_row_written_inside_the_erasure_names_nobody(keyed, db, tenant, sessions,
                                                             monkeypatch):
    """Audit of 19 Oct 2026: a cost row flushed while an end user's
    erasure was open, after the erasure had taken them off the cost rows and
    before it committed, read no erasure on file and kept the id in clear for
    400 days. The search's cost row is written here at that moment, in its own
    session, and before and after the erasure too: none names the end user."""
    from korely_agent.services import facts as agent_facts
    from korely_agent.services import llm_usage

    def billed_embed(text, task=None, **kw):
        llm_usage.record("local", "embeddinggemma", 5, 0)
        return None

    monkeypatch.setattr("korely_agent.services.memory_engine.embed", billed_embed)
    pid = seeded_project_id(db, tenant.id)
    db.add(_memory(tenant.id, pid, user="maria"))
    db.commit()
    _search_as(sessions(), tenant.id, "maria")

    from korely_agent.services import end_user_places

    real, done = end_user_places.rename_in_lists, []

    def then_a_search(*a, **kw):
        out = real(*a, **kw)
        t = threading.Thread(target=lambda: (_search_as(sessions(), tenant.id, "maria"),
                                             done.append(True)))
        t.start()
        t.join(30)
        return out

    monkeypatch.setattr(end_user_places, "rename_in_lists", then_a_search)
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=pid)
    monkeypatch.setattr(end_user_places, "rename_in_lists", real)
    _search_as(sessions(), tenant.id, "maria")

    assert done == [True], "the search inside the erasure did not finish"
    rows, naming = _cost_rows_naming(db, tenant.id, "maria")
    assert len(rows) == 3 and naming == 0, rows


# ── the export's error ──────────────────────────────────────────────────────

def test_the_exports_error_names_no_end_users_file(db, tenant, tmp_path, monkeypatch, caplog):
    """A tenant folder that can be listed and not searched: is_file raises
    with the file's path, named after the end user, and it reached /health."""
    from korely_agent import worker
    from korely_agent.config import get_settings
    from korely_agent.services import export_md

    marker = "zzmarker@example.org"
    base = tmp_path / str(tenant.id)
    base.mkdir()
    (base / f"{export_md.safe_name(marker)}.md").write_text("old")
    real = Path.is_file

    def unsearchable(self):
        if self.parent == base:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "is_file", unsearchable)
    export_md._remove_all_but(base, set())
    assert "zzmarker" not in caplog.text and "errno 13" in caplog.text

    def failing(_db, _where):
        raise PermissionError(13, "Permission denied", str(base / f"{marker}.md"))

    monkeypatch.setenv("FACTS_EXPORT_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(export_md, "export_all", failing)
    try:
        with pytest.raises(OSError) as raised:
            worker._export(db)
    finally:
        get_settings.cache_clear()
    assert "zzmarker" not in str(raised.value)
    state = worker.export_state()
    assert state["state"] == "failing" and "zzmarker" not in state["detail"]
    assert "errno 13" in state["detail"]


# ── a list in the trail, whole entries only ─────────────────────────────────

def test_a_long_list_is_kept_by_whole_entries_and_the_erasure_finds_it(keyed, db, tenant):
    from korely_agent.services import audit

    users = [f"other-user-{i}@example.com" for i in range(7)] + ["vvmarker-erased@example.com"]
    meta = audit._safe_meta({"name": "cut", "users": users})
    assert len(meta["users"]) <= 200 and meta["users"].endswith("more)")
    assert "vvmarker" not in meta["users"], "never an entry cut in the middle"
    short = audit._safe_meta({"users": ["maria", "luca"]})["users"]
    assert short == "['maria', 'luca']"

    pid = seeded_project_id(db, tenant.id)
    audit.record(db, tenant.id, "key_create", actor="dashboard", project_id=pid,
                 meta={"name": "k", "users": ["maria"] + users[:4]})
    pseudonyms.after_an_erasure(db, tenant.id, "maria", project_id=pid)
    db.commit()
    db.expire_all()
    row = db.query(AuditEvent).filter(AuditEvent.action == "key_create").one()
    assert "maria" not in row.meta["users"]
    assert pseudonyms.pseudonym(tenant.id, "maria") in row.meta["users"]


@pytest.mark.parametrize("who", ["ACME\\maria", "maria'o\"x", "maria\u00a0nbsp", "maria\u200bzw",
                                 "maria"])
def test_an_id_the_list_spells_with_escapes_takes_the_pseudonym_too(keyed, db, tenant, who):
    """The trail keeps a list as its text, where an id holding a backslash,
    both quotes or a character Python escapes is spelled with escapes; the
    erasure looked for the id as written, never found those rows, and the
    id stayed in clear for 400 days (audit of 19 Oct 2026)."""
    from korely_agent.services import audit

    pid = seeded_project_id(db, tenant.id)
    audit.record(db, tenant.id, "key_create", actor="dashboard", project_id=pid,
                 meta={"name": "k", "users": [who, "luca"]})
    pseudonyms.after_an_erasure(db, tenant.id, who, project_id=pid)
    db.commit()
    db.expire_all()
    row = db.query(AuditEvent).filter(AuditEvent.action == "key_create").one()
    assert row.meta["users"] == str([pseudonyms.pseudonym(tenant.id, who), "luca"])


def test_the_migration_keeps_the_whole_entries_of_a_cut_list():
    path = next(Path(__file__).resolve().parent.parent.glob(
        "alembic/versions/c45a7e2b9d13_*.py"))
    spec = importlib.util.spec_from_file_location("mig_whole_entries", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    users = [f"other-user-{i}@example.com" for i in range(7)] + ["vvmarker-erased@example.com"]
    cut = str(users)[:200]
    assert cut.endswith("'vv"), "a prefix of the marker, cut"
    repaired = mig._whole(cut)
    assert repaired == str(users[:7]) + " (+more)"
    assert mig._whole("['vvmarker-erased@exa") == "[] (+more)"


# ── archiving a project an app is connected to (agent 48, 18 Oct 2026) ──────

def test_a_connected_apps_key_between_two_refreshes_keeps_a_project_from_the_archive(
        db, tenant, pilot):
    """An OAuth app's key expires with its access token, an hour, and the
    refresh makes it current again: live until revoked. Counted dead between
    two refreshes, it let the project be archived while the app wrote there."""
    from datetime import datetime, timedelta, timezone

    from korely_agent.db.models import OAuthClient

    db.add(OAuthClient(client_id="client-1", info={}))
    db.commit()
    app, _ = key_service.create_api_key(
        db, tenant_id=tenant.id, name="app", scopes=["memories:read", "memories:write"],
        project_id=pilot.id, oauth_client_id="client-1",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    with pytest.raises(project_service.ProjectHasLiveKeys):
        project_service.archive_project(db, db.get(AgentProject, pilot.id))
    # An ordinary key past its expiry is dead, as authentication says.
    app.revoked_at = datetime.now(timezone.utc)
    db.commit()
    key_service.create_api_key(db, tenant_id=tenant.id, name="old", project_id=pilot.id,
                               expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    archived = project_service.archive_project(db, db.get(AgentProject, pilot.id))
    assert archived.archived_at is not None


# ── archiving while a key is put in the project (audit of 19 Oct 2026) ──────
# The archive counted the live keys and
# the three doors that put a key in a project read `archived_at`, each in a
# plain SELECT, so a key committed into a project archived meanwhile and wrote
# there. Both sides now take the project's row lock (projects.get_project).

def _oauth_code(db, tenant_id, project_id):
    """An approved, unexchanged code for `project_id`: the grant row as the
    consent leaves it, and the provider that exchanges it."""
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from korely_agent.db.models import OAuthClient, OAuthGrant
    from korely_agent.services import oauth as oauth_service

    cid = f"client-{uuid.uuid4().hex[:8]}"
    db.add(OAuthClient(client_id=cid, info={}))
    db.flush()
    code = uuid.uuid4().hex
    db.add(OAuthGrant(kind="code", token_hash=key_service.hash_key(code), client_id=cid,
                      tenant_id=tenant_id, project_id=project_id,
                      scopes=["memories:read", "memories:write"],
                      expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))
    db.commit()
    provider = oauth_service.KorelyOAuthProvider(
        issuer="https://memory.example", resource="https://memory.example/mcp",
        consent_url="https://memory.example/oauth/consent")
    app = SimpleNamespace(client_id=cid, model_dump=lambda: {"client_name": "App"})
    return lambda: provider._exchange_code(app, SimpleNamespace(code=code))


def _door(kind, db, tenant, pilot, sessions):
    """The door that puts a key in `pilot`, as a function of no argument run
    in its own session: POST /admin/keys and its move as the routes do it
    (`account_project(to_hold_keys=True)`, then the key service), and the
    OAuth code exchange."""
    from types import SimpleNamespace

    from korely_agent.api.admin import account_project

    principal = SimpleNamespace(tenant_id=tenant.id)
    if kind == "create":
        def run():
            s = sessions()
            pid = account_project(s, principal, str(pilot.id), to_hold_keys=True).id
            key_service.create_api_key(s, tenant_id=tenant.id, name="raced", project_id=pid)
        return run
    if kind == "move":
        default = project_service.get_or_create_default_project(db, tenant.id)
        row, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="mover",
                                            project_id=default.id)

        def run():
            s = sessions()
            pid = account_project(s, principal, str(pilot.id), to_hold_keys=True).id
            key_service.move_key(s, key_id=row.id, tenant_id=tenant.id, project_id=pid)
        return run
    return _oauth_code(db, tenant.id, pilot.id)


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["result"] = fn()
        except Exception as e:  # noqa: BLE001 - reported to the test
            out["error"] = e

    t = threading.Thread(target=run)
    t.start()
    return t, out


def _live_keys_in(db, project_id):
    db.expire_all()
    return (db.query(AgentApiKey)
              .filter(AgentApiKey.project_id == project_id, AgentApiKey.revoked_at.is_(None))
              .count())


@pytest.mark.parametrize("kind", ["create", "move", "oauth"])
def test_an_archive_waits_for_a_key_being_put_in_and_refuses(db, tenant, pilot, sessions,
                                                            monkeypatch, kind):
    """The door has read the project and is writing its key when the archive
    starts. The archive waits for the key's commit, counts it, and refuses;
    before, it archived at once and the key committed into the archived
    project, where it wrote."""
    door = _door(kind, db, tenant, pilot, sessions)
    reached, release = threading.Event(), threading.Event()
    name = "move_key" if kind == "move" else "create_api_key"
    real = getattr(key_service, name)

    def paused(*a, **k):
        reached.set()
        release.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(key_service, name, paused)
    writer, wrote = _in_thread(door)
    assert reached.wait(10)

    def archive():
        s = sessions()
        return project_service.archive_project(s, project_service.get_project(s, tenant.id, pilot.id))

    archiver, archived = _in_thread(archive)
    archiver.join(timeout=1.0)
    alive = archiver.is_alive()
    release.set()
    writer.join(timeout=10)
    archiver.join(timeout=10)
    assert alive, "the archive did not wait for the key being put in"
    assert "error" not in wrote, wrote.get("error")
    assert isinstance(archived.get("error"), project_service.ProjectHasLiveKeys), archived
    db.expire_all()
    assert db.get(AgentProject, pilot.id).archived_at is None
    assert _live_keys_in(db, pilot.id) == 1


@pytest.mark.parametrize("kind", ["create", "move", "oauth"])
def test_a_key_put_in_while_the_archive_commits_is_refused(db, tenant, pilot, sessions, kind):
    """The archive has counted no live key and is committing when the door
    reads the project. The door waits for the archive's commit, finds the
    project archived, and refuses; before, it read the project as it was and
    committed its key into it."""
    from fastapi import HTTPException
    from mcp.server.auth.provider import TokenError

    door = _door(kind, db, tenant, pilot, sessions)
    s = sessions()
    reached, release = threading.Event(), threading.Event()
    real_commit = s.commit

    def paused_commit():
        reached.set()
        release.wait(10)
        return real_commit()

    s.commit = paused_commit
    archiver, archived = _in_thread(
        lambda: project_service.archive_project(s, project_service.get_project(s, tenant.id, pilot.id)))
    assert reached.wait(10)
    writer, wrote = _in_thread(door)
    writer.join(timeout=1.0)
    alive = writer.is_alive()
    release.set()
    archiver.join(timeout=10)
    writer.join(timeout=10)
    assert alive, "the door did not wait for the archive"
    assert "error" not in archived, archived.get("error")
    refused = wrote.get("error")
    if kind == "oauth":
        assert isinstance(refused, TokenError) and refused.error == "invalid_grant", wrote
    else:
        assert isinstance(refused, HTTPException) and refused.status_code == 409, wrote
        assert refused.detail["code"] == "project_archived"
    assert _live_keys_in(db, pilot.id) == 0
