"""An erasure reaches every copy of the person, in every order (audit of 20 Oct 2026).

Each audit found one more column, table or order the erasure, the
pseudonym rewrite or the restore's replay missed. The places are one list
now (services/end_user_places.py, its guard in tests/test_end_user_places.py);
these are that audit's records, one test each, the way each was found.
"""
import threading
import time
import uuid

import pytest
from sqlalchemy import text

from korely_agent.db.models import (AgentEntityAlias, AgentForgetAudit, AgentMemory, AgentProject,
                                    AuditEvent, Tenant)
from korely_agent.services import audit as audit_service
from korely_agent.services import erasure_replay, pseudonyms
from korely_agent.services import facts as agent_facts
from korely_agent.services import projects as project_service
from korely_agent.services.sentinels import EVERY_PROJECT
from tests.test_end_user_places import the_upgrade_places_the_names
from tests.test_erasure_replay import DUMPED


def _keyed_as(monkeypatch, secret):
    from korely_agent import config

    monkeypatch.setenv("SECRET_KEY", secret or "")
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def keyed(monkeypatch):
    _keyed_as(monkeypatch, "a" * 48)
    yield
    from korely_agent import config

    config.get_settings.cache_clear()


@pytest.fixture
def default(db, tenant):
    return project_service.get_or_create_default_project(db, tenant.id)


@pytest.fixture
def beta(db, tenant, default):
    return project_service.create_project(db, tenant_id=tenant.id, name="Beta")


def _memory(db, tenant, pid, user=None, agent=None, mid=None):
    # As old as a dump's rows: older than the erasures of the test (DUMPED).
    m = AgentMemory(id=mid or uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=user,
                    agent_id=agent, content="dummy", graph_sync_status="done", created_at=DUMPED)
    db.add(m)
    db.commit()
    return m


def _alias(db, tenant, pid, *, user=None, agent=None, label="My Wife", created_at=None,
           since=None):
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=pid, end_user_id=user, agent_id=agent,
                            alias_lower=label.lower(), alias_label=label,
                            canonical_label="Dummy Rossi", created_at=created_at, since=since))
    db.commit()


def _aliases(db, tenant):
    db.expire_all()
    return sorted((a.end_user_id or "", a.agent_id or "")
                  for a in db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant.id))


def _trail_naming(db, who) -> int:
    return db.execute(text("SELECT count(*) FROM audit_events AS t WHERE t::text LIKE :m"),
                      {"m": f"%{who}%"}).scalar()


# ── an alias made with no project survives the end user's erasure ───────────
# 0.2.0 filed the aliases made with no project under the Default project; an
# erasure in the person's own project left them there, in clear. The upgrade
# gives them their person's project (migration e7c4b2d9a6f1), and the
# erasure there takes them.

def test_a_legacy_alias_in_default_goes_with_its_end_user_and_agent(db, tenant, default, beta):
    _memory(db, tenant, beta.id, user="u-beta")
    _memory(db, tenant, beta.id, agent="ag-beta")
    _memory(db, tenant, default.id, user="u-home")
    for who in ({"user": "u-beta"}, {"agent": "ag-beta"}, {"user": "u-home"}):
        _alias(db, tenant, default.id, **who)
    the_upgrade_places_the_names(db)
    agent_facts.forget_end_user(db, tenant.id, "u-beta", project_id=beta.id)
    agent_facts.delete_agent(db, tenant.id, "ag-beta", project_id=beta.id)
    assert _aliases(db, tenant) == [("u-home", "")], "an erased person's names stayed in Default"


def test_an_alias_of_a_namesake_who_lives_there_stays(db, tenant, default, beta):
    _memory(db, tenant, beta.id, user="maria")
    _memory(db, tenant, default.id, user="maria")
    _alias(db, tenant, default.id, user="maria")
    the_upgrade_places_the_names(db)
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=beta.id)
    assert _aliases(db, tenant) == [("maria", "")]


def test_a_projects_erasure_takes_its_peoples_legacy_aliases(db, tenant, default, beta):
    _memory(db, tenant, beta.id, user="u-gamma")
    _memory(db, tenant, beta.id, agent="ag-gamma")
    _alias(db, tenant, default.id, user="u-gamma")
    _alias(db, tenant, default.id, agent="ag-gamma", label="The Lab")
    the_upgrade_places_the_names(db)
    project_service.delete_project(db, tenant.id, beta, erase=True)
    assert _aliases(db, tenant) == []


def _unplaced(db, tenant, default, beta):
    """u-two lives in Beta and Gamma; 0.2.0 filed a name of theirs made with
    no project under Default: the upgrade cannot place it, and marks it."""
    gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
    mems = [_memory(db, tenant, pid, user="u-two").id for pid in (beta.id, gamma.id)]
    _alias(db, tenant, default.id, user="u-two")
    return gamma, mems


def test_a_name_the_upgrade_could_not_place_goes_with_the_last_erasure(db, tenant, default,
                                                                         beta):
    """Review of the audit of 27 Oct 2026: with the strict wall, such a name stayed in clear
    after the person's erasure in every project they lived in, where 0.2.1
    took it. The erasure that leaves the id in no other project takes it."""
    gamma, _ = _unplaced(db, tenant, default, beta)
    assert the_upgrade_places_the_names(db)["not_recorded"] == 1
    agent_facts.forget_end_user(db, tenant.id, "u-two", project_id=beta.id)
    assert _aliases(db, tenant) == [("u-two", "")], "Gamma's u-two may be its owner"
    agent_facts.forget_end_user(db, tenant.id, "u-two", project_id=gamma.id)
    assert _aliases(db, tenant) == [], "the erased person's name stayed in Default"


def test_a_replayed_erasure_takes_a_name_the_upgrade_could_not_place(db, tenant, default, beta):
    """A dump taken before the upgrade, restored after both erasures: the
    upgrade marks the name again, and the replay of the two erasures takes
    it (services/erasure_replay)."""
    gamma, mems = _unplaced(db, tenant, default, beta)
    # When the name was written and last stated: the dump holds it so, older
    # than the erasures.
    (named_at, stated_at) = db.query(AgentEntityAlias.created_at, AgentEntityAlias.since).filter(
        AgentEntityAlias.tenant_id == tenant.id, AgentEntityAlias.end_user_id == "u-two").one()
    projects = (beta.id, gamma.id)
    dflt = default.id
    the_upgrade_places_the_names(db)
    for pid in projects:
        agent_facts.forget_end_user(db, tenant.id, "u-two", project_id=pid)
    kept = erasure_replay.export(db)
    # The dump: the memories and the name as 0.2.1 held them, no record.
    db.query(AgentForgetAudit).delete()
    db.commit()
    db.expunge_all()
    for pid, mid in zip(projects, mems):
        _memory(db, tenant, pid, user="u-two", mid=mid)
    _alias(db, tenant, dflt, user="u-two", created_at=named_at, since=stated_at)
    assert the_upgrade_places_the_names(db)["not_recorded"] == 1
    out = erasure_replay.replay(db, kept)
    assert out["replayed"] == {"user": 2}
    assert _aliases(db, tenant) == [], "the replay left the erased person's name in Default"


def test_a_name_kept_by_the_time_rule_goes_with_its_person(db, tenant, default, beta):
    """A name the dashboard filed in Default after 0.1.41 was released, for
    a person who lives in Beta only: an install still on 0.1.40 made it with
    no project. It stays in Default, marked, and their erasure in Beta takes
    it (erasure first, review of 692e1b1)."""
    _memory(db, tenant, beta.id, user="u-beta")
    _alias(db, tenant, default.id, user="u-beta")
    row = db.query(AgentEntityAlias).filter(AgentEntityAlias.end_user_id == "u-beta").one()
    db.add(AuditEvent(tenant_id=tenant.id, project_id=None, actor="dashboard",
                      action="setting_change", result="ok", target_id=str(row.id),
                      meta={"setting": "alias", "op": "create", "operator": "x"}))
    db.commit()
    assert the_upgrade_places_the_names(db) == {"moved": 0, "kept": 0, "not_recorded": 1}
    assert _aliases(db, tenant) == [("u-beta", "")]
    agent_facts.forget_end_user(db, tenant.id, "u-beta", project_id=beta.id)
    assert _aliases(db, tenant) == [], "the erased person's name stayed in Default"


def test_a_name_declared_again_in_default_is_its_own(db, tenant, default, beta):
    """A name the upgrade could not place, declared again in Default, is the
    Default project's: an erasure elsewhere no longer takes it."""
    from korely_agent.services.facts import entities

    gamma, _ = _unplaced(db, tenant, default, beta)
    the_upgrade_places_the_names(db)
    entities.declare_alias(db, tenant.id, default.id, "u-two", None, "My Wife", "Dummy Rossi")
    db.commit()
    for pid in (beta.id, gamma.id):
        agent_facts.forget_end_user(db, tenant.id, "u-two", project_id=pid)
    assert _aliases(db, tenant) == [("u-two", "")]


# ── the users a key's creation recorded survive the erasure ─────────────────
# A list recorded as the caller spelled it kept the erased id in clear.

def test_a_list_the_trail_records_is_rewritten_by_the_erasure(db, tenant, default):
    from korely_agent.services.usage import log_admin_action

    _memory(db, tenant, default.id, user="maria")
    owner = type("K", (), {"tenant_id": tenant.id, "id": None})()
    log_admin_action(db, owner, audit_service.SETTING_CHANGE,
                     meta={"setting": "oauth_consent", "users": [" maria", "luca"]},
                     project_id=default.id)
    # A row written that way before the audit of 20 Oct 2026.
    db.add(AuditEvent(tenant_id=tenant.id, project_id=default.id, actor="dashboard",
                      action="setting_change", result="ok",
                      meta={"users": str(["maria\x85", "luca"])}))
    db.commit()
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=default.id)
    db.expire_all()
    assert _trail_naming(db, "maria") == 0, "a recorded list kept the erased id"
    assert _trail_naming(db, "luca") == 2


# ── a project's end users counted ids it had refused ────────────────────────

def test_an_id_a_project_only_named_gets_no_erasure_record(db, tenant, default, beta):
    _memory(db, tenant, beta.id, user="alice")
    _memory(db, tenant, default.id, user="bob")
    # A key of Beta asked about bob, refused or not: its trail names him.
    db.add(AuditEvent(tenant_id=tenant.id, project_id=beta.id, actor="rest", action="read",
                      result="error", end_user_id="bob"))
    db.commit()
    project_service.delete_project(db, tenant.id, beta, erase=True)
    db.expire_all()
    named = {r.end_user_id for r in db.query(AgentForgetAudit)
             .filter(AgentForgetAudit.scope == "user")}
    assert named == {pseudonyms.pseudonym(tenant.id, "alice")}, "bob was recorded as erased"
    assert pseudonyms.as_logged(db, tenant.id, "bob", default.id) == "bob"
    assert _trail_naming(db, "bob") == 0, "the project's own rows naming bob take the pseudonym"


# ── an erasure record relabelled a namesake in another project ─────────────

def test_an_erasure_in_one_project_leaves_a_namesake_elsewhere_in_clear(db, tenant, default, beta):
    from datetime import datetime, timedelta, timezone

    _memory(db, tenant, default.id, user="maria")
    _memory(db, tenant, beta.id, user="maria")
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=beta.id)
    p = pseudonyms.pseudonym(tenant.id, "maria")
    assert pseudonyms.as_logged(db, tenant.id, "maria", default.id) == "maria"
    assert pseudonyms.as_logged(db, tenant.id, "maria", beta.id) == p
    assert pseudonyms.erased_since(db, tenant.id, "maria", since, project=str(default.id)) is None
    assert pseudonyms.erased_since(db, tenant.id, "maria", since, project=str(beta.id)) == p
    # The trail's own writer, for a row of the Default project.
    audit_service.record(db, tenant.id, "read", end_user_id="maria", project_id=default.id)
    row = db.query(AuditEvent).order_by(AuditEvent.ts.desc()).first()
    assert row.end_user_id == "maria"


def test_a_read_through_a_key_the_erasure_moved_still_takes_the_pseudonym(db, tenant, default,
                                                                          beta):
    _memory(db, tenant, beta.id, user="anna")
    project_service.delete_project(db, tenant.id, beta, erase=True)
    p = pseudonyms.pseudonym(tenant.id, "anna")
    assert pseudonyms.as_logged(db, tenant.id, "anna", default.id) == p


# ── the dashboard's writer of the trail ────────────────────────────────────

def test_an_act_of_administration_names_an_erased_end_user_by_pseudonym(db, tenant, default):
    from korely_agent.services.usage import log_admin_action

    _memory(db, tenant, default.id, user="maria")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=EVERY_PROJECT)
    owner = type("K", (), {"tenant_id": tenant.id, "id": None})()
    log_admin_action(db, owner, audit_service.ERASE, end_user_id="maria", project_id=default.id)
    db.expire_all()
    assert _trail_naming(db, "maria") == 0, "written in clear by log_admin_action"


# ── a trail label read before the erasure commits ──────────────────────────

@pytest.mark.parametrize("filed", ["no project", "the default project"])
def test_a_row_written_while_an_erasure_is_open_takes_the_pseudonym(db, tenant, default, filed):
    """The erasure has renamed the trail and not committed; a row about the
    person is written meanwhile, its label read before the record exists."""
    from korely_agent.db import get_sessionmaker

    _memory(db, tenant, default.id, user="maria")
    eraser = get_sessionmaker()()
    pseudonyms.after_an_erasure(eraser, tenant.id, "maria", project_id=default.id)
    eraser.add(AgentForgetAudit(tenant_id=tenant.id, project_id=default.id, scope="user",
                                end_user_id=pseudonyms.pseudonym(tenant.id, "maria")))
    eraser.flush()
    project = None if filed == "no project" else default.id
    done = threading.Event()
    t = threading.Thread(target=lambda: (audit_service.record(
        db.__class__(bind=db.get_bind()), tenant.id, "read", end_user_id="maria",
        project_id=project), done.set()))
    t.start()
    time.sleep(0.5)
    eraser.commit()
    eraser.close()
    t.join(15)
    assert done.is_set()
    db.expire_all()
    assert _trail_naming(db, "maria") == 0, "the row kept the erased id in clear"


# ── a restore's replay and a project refiled by its deletion ───────────────
# A project deleted without erase, then a person of it erased through a key
# the deletion moved; a dump older than the deletion is restored.

def _restore_beta(db, tenant, beta_id, rows):
    """The dump: Beta as it was, its rows, no erasure record."""
    db.query(AgentForgetAudit).delete()
    db.commit()
    db.add(AgentProject(id=beta_id, tenant_id=tenant.id, name="Beta", slug="beta"))
    db.commit()
    for user, agent, mid in rows:
        _memory(db, tenant, beta_id, user=user, agent=agent, mid=mid)


def _alive(db, tenant):
    db.expire_all()
    return sorted((m.end_user_id or "", m.agent_id or "", str(m.project_id))
                  for m in db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id))


@pytest.mark.parametrize("erased", ["user", "agent"])
def test_an_erasure_after_a_projects_move_is_made_again_after_the_move(db, tenant, default, beta,
                                                                       erased):
    beta_id = beta.id
    maria = _memory(db, tenant, beta_id, user="maria", agent="support-bot")
    bob = _memory(db, tenant, beta_id, user="bob")
    project_service.delete_project(db, tenant.id, beta, erase=False)
    if erased == "user":
        agent_facts.forget_end_user(db, tenant.id, "maria", project_id=default.id)
    else:
        agent_facts.delete_agent(db, tenant.id, "support-bot", project_id=default.id)
    kept = erasure_replay.export_file(db)
    db.query(AgentMemory).delete()
    db.commit()
    _restore_beta(db, tenant, beta_id, [("maria", "support-bot", maria.id), ("bob", None, bob.id)])

    out = erasure_replay.replay(db, *erasure_replay.read_file(kept))
    assert out["changes"] == {"move": 1} and out["replayed"] == {erased: 1}
    assert _alive(db, tenant) == [("bob", "", str(default.id))], "the erased person came back"
    assert db.get(AgentProject, beta_id) is None


# ── a restore's replay with another SECRET_KEY failed open ─────────────────

def _erased_then_restored(db, tenant, default):
    maria = _memory(db, tenant, default.id, user="maria")
    _memory(db, tenant, default.id, user="bob")
    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=default.id)
    return maria


def _restore(db, tenant, pid, mid):
    db.query(AgentForgetAudit).delete()
    db.commit()
    db.expunge_all()
    _memory(db, tenant, pid, user="maria", mid=mid)


def test_a_key_changed_between_the_erasure_and_the_restore_refuses(db, tenant, default,
                                                                    monkeypatch):
    maria = _erased_then_restored(db, tenant, default)
    _keyed_as(monkeypatch, "b" * 48)
    kept = erasure_replay.export_file(db)
    _restore(db, tenant, default.id, maria.id)
    with pytest.raises(erasure_replay.KeyMismatch):
        erasure_replay.replay(db, *erasure_replay.read_file(kept))
    assert "maria" in {u for (u, _a, _p) in _alive(db, tenant)}, "refused before erasing"


def test_an_export_with_no_key_replays_under_the_key_that_made_the_pseudonyms(db, tenant, default,
                                                                               monkeypatch):
    maria = _erased_then_restored(db, tenant, default)
    _keyed_as(monkeypatch, None)
    kept = erasure_replay.export_file(db)
    assert kept["key_check"] is None
    _restore(db, tenant, default.id, maria.id)
    _keyed_as(monkeypatch, "a" * 48)
    out = erasure_replay.replay(db, *erasure_replay.read_file(kept))
    assert out["replayed"] == {"user": 1}
    assert [u for (u, _a, _p) in _alive(db, tenant)] == ["bob"]


def test_an_export_no_key_could_replay_is_refused(db, tenant, default, monkeypatch, capsys):
    from korely_agent import manage

    _erased_then_restored(db, tenant, default)
    db.query(AgentForgetAudit).update({AgentForgetAudit.key_check: None})
    db.commit()
    _keyed_as(monkeypatch, None)
    capsys.readouterr()
    assert manage.erasures_export() == 3
    out = capsys.readouterr()
    assert out.out == "" and "SECRET_KEY" in out.err


# ── a restore's replay ignored the Cloud's account closes and merges ───────

def test_without_the_cloud_its_closes_and_merges_are_said_not_made(db, monkeypatch):
    """A tree without the Cloud's package (the public repository) exports none
    and cannot make a Cloud dump's: each is counted `not_made`, never skipped
    in silence (core/extensions.cloud)."""
    import sys

    from korely_agent.core import extensions

    monkeypatch.setitem(sys.modules, extensions.CLOUD_PACKAGE, None)
    assert erasure_replay.export_file(db)["records"] == []
    records = [{"id": str(uuid.uuid4()), "account": str(uuid.uuid4()), "scope": scope,
                "target_id": str(uuid.uuid4()), "created_at": None}
               for scope in ("merge", "close")]
    out = erasure_replay.replay(db, records)
    assert out["not_made"] == {"merge": 1, "close": 1}, out
    assert out["changes"] == {} and out["failed"] == []


@pytest.mark.cloud
def test_a_closed_account_is_closed_again_after_a_restore(db):
    from korely_agent.hosted import closing
    from korely_agent.hosted.models import HostedErasure

    t = Tenant(id=uuid.uuid4(), name="init account")
    db.add(t)
    db.commit()
    tid = t.id
    pid = project_service.get_or_create_default_project(db, tid).id
    db.add(AgentMemory(tenant_id=tid, project_id=pid, content="dummy", end_user_id="x"))
    db.commit()
    closing.close(db, tid, via="api")
    kept = erasure_replay.export_file(db)
    proof = db.query(HostedErasure).one().id
    # The dump, older than the close.
    db.query(HostedErasure).delete()
    db.add(Tenant(id=tid, name="init account"))
    db.commit()
    db.add(AgentMemory(tenant_id=tid, content="dummy", end_user_id="x"))
    db.commit()

    out = erasure_replay.replay(db, *erasure_replay.read_file(kept))
    db.expire_all()
    assert out["changes"] == {"close": 1} and out["replayed"] == {}
    assert db.get(Tenant, tid) is None, "the closed account came back"
    assert db.query(HostedErasure).one().id == proof, "the proof of the close is back as it was"


@pytest.mark.cloud
def test_an_erasure_after_a_merge_is_made_again_after_the_merge(db):
    from korely_agent.hosted import claim

    s, u = Tenant(id=uuid.uuid4(), name="key account"), Tenant(id=uuid.uuid4(), name="mine")
    db.add_all([s, u])
    db.commit()
    s_id, u_id = s.id, u.id
    s_pid = project_service.get_or_create_default_project(db, s_id).id
    project_service.get_or_create_default_project(db, u_id)
    maria = AgentMemory(id=uuid.uuid4(), tenant_id=s_id, project_id=s_pid, content="dummy",
                        end_user_id="maria", created_at=DUMPED)
    db.add(maria)
    db.commit()
    maria_id = maria.id
    claim.move(db, s_id, u_id)
    db.commit()
    agent_facts.forget_end_user(db, u_id, "maria", project_id=s_pid)
    kept = erasure_replay.export_file(db)
    assert [r["scope"] for r in kept["records"]] == ["merge", "user"]
    # The dump, older than the merge: S apart, with maria.
    db.query(AgentForgetAudit).delete()
    db.query(Tenant).filter(Tenant.id == u_id).update({Tenant.merged_tenant_ids: None})
    db.add(Tenant(id=s_id, name="key account"))
    db.commit()
    db.query(AgentProject).filter(AgentProject.id == s_pid).update({AgentProject.tenant_id: s_id})
    db.commit()
    db.expunge_all()
    db.add(AgentMemory(id=maria_id, tenant_id=s_id, project_id=s_pid, content="dummy",
                       end_user_id="maria", created_at=DUMPED))
    db.commit()

    out = erasure_replay.replay(db, *erasure_replay.read_file(kept))
    db.expire_all()
    assert out["changes"] == {"merge": 1} and out["replayed"] == {"user": 1}
    assert db.get(Tenant, s_id) is None
    assert db.query(AgentMemory).filter(AgentMemory.end_user_id == "maria").count() == 0
