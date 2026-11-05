"""A project's deletion reads what it moves or renames in blocks, and gets the
same answer (audit of 27 Oct 2026).

The whole-read guard (tests/test_no_read_loads_a_whole_table.py) holds the
reads to a LIMIT; these hold the answers to what they were, with blocks of
two rows, so every boundary between blocks is crossed:

- the aliases moved (`projects._move_aliases`, `_MOVE_CHUNK`);
- the twin facts folded (`projects._fold_duplicates`, `_FOLD_CHUNK`);
- the confined keys, codes and trail lists renamed by the erasure
  (`end_user_places.rename_in_lists`, `_BLOCK`), and the deliveries
  renamed in the database (`rename_in_deliveries`);
- the live keys an archive counts, and the apps a deletion disconnects, in
  SQL (`archive_project`, `_hand_over_keys`).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.db.models import (AgentApiKey, AgentEntityAlias, AgentFact, AgentWebhookDelivery,
                                    AgentWebhookEndpoint, AuditEvent)
from korely_agent.services import end_user_places as places
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services import pseudonyms

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def small_blocks(monkeypatch):
    monkeypatch.setattr(project_service, "_MOVE_CHUNK", 2)
    monkeypatch.setattr(project_service, "_FOLD_CHUNK", 2)
    monkeypatch.setattr(places, "_BLOCK", 2)
    monkeypatch.setenv("SECRET_KEY", "b" * 48)
    from korely_agent import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def default(db, tenant):
    return project_service.get_or_create_default_project(db, tenant.id)


@pytest.fixture
def beta(db, tenant, default):
    return project_service.create_project(db, tenant_id=tenant.id, name="Beta")


def test_five_aliases_move_two_at_a_time(db, tenant, default, beta):
    for i in range(5):
        db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=beta.id, end_user_id="u",
                                alias_lower=f"name {i}", alias_label=f"Name {i}",
                                canonical_label=f"Person {i}"))
    db.commit()
    out = project_service.delete_project(db, tenant.id, beta)
    assert (out["aliases_moved"], out["aliases_not_moved"]) == (5, 0)
    db.expire_all()
    moved = {a.alias_lower for a in db.query(AgentEntityAlias)
             .filter(AgentEntityAlias.project_id == default.id)}
    assert moved == {f"name {i}" for i in range(5)}


def _fact(db, tenant, project, obj, created):
    db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project.id, end_user_id="u",
                     subject_label="u", subject_canonical="u", predicate_raw="likes",
                     predicate_canonical="likes", object_label=obj, object_canonical=obj,
                     valid_from=NOW - timedelta(days=9), confidence=0.9, observation_count=1,
                     created_at=created))
    db.commit()


def test_five_twins_fold_two_at_a_time(db, tenant, default, beta):
    for i in range(5):
        _fact(db, tenant, default, f"thing {i}", NOW - timedelta(days=2))
        _fact(db, tenant, beta, f"thing {i}", NOW)
    _fact(db, tenant, beta, "only here", NOW)
    out = project_service.delete_project(db, tenant.id, beta)
    assert out["facts_merged"] == 5 and out["facts"] == 1
    db.expire_all()
    assert db.query(AgentFact).filter(AgentFact.project_id == default.id).count() == 6


def test_an_erasure_renames_five_keys_and_lists_two_at_a_time(db, tenant, default, beta):
    for i in range(5):
        key_service.create_api_key(db, tenant_id=tenant.id, name=f"k{i}", scopes=["memories:read"],
                                   project_id=beta.id, end_users=[f"p{i}"])
        db.add(AuditEvent(tenant_id=tenant.id, project_id=beta.id, actor="rest",
                          action="key_create", result="ok", meta={"users": str([f"p{i}"])}))
    ep = AgentWebhookEndpoint(tenant_id=tenant.id, url="http://127.0.0.1:9/h", secret="whsec_x",
                              events=["*"])
    db.add(ep)
    db.flush()
    db.add(AgentWebhookDelivery(tenant_id=tenant.id, endpoint_id=ep.id, event_type="memory.created",
                                status="delivered",
                                payload={"project_id": str(beta.id), "data": {"user_id": "p3"}}))
    db.commit()
    out = project_service.delete_project(db, tenant.id, beta, erase=True)
    assert out["keys_revoked"] == 5
    db.expire_all()
    lists = sorted(k.end_users[0] for k in db.query(AgentApiKey))
    assert lists == sorted(pseudonyms.pseudonym(tenant.id, f"p{i}") for i in range(5))
    recorded = {r.meta["users"] for r in db.query(AuditEvent).filter(AuditEvent.action == "key_create")}
    assert recorded == {str([pseudonyms.pseudonym(tenant.id, f"p{i}")]) for i in range(5)}
    d = db.query(AgentWebhookDelivery).one()
    assert d.payload["data"]["user_id"] == pseudonyms.pseudonym(tenant.id, "p3")


def test_an_archive_counts_the_live_keys_in_the_database(db, tenant, default, beta):
    key_service.create_api_key(db, tenant_id=tenant.id, name="k", scopes=["memories:read"],
                               project_id=beta.id)
    with pytest.raises(project_service.ProjectHasLiveKeys):
        project_service.archive_project(db, beta)
    db.rollback()
    db.query(AgentApiKey).update({AgentApiKey.revoked_at: datetime.now(timezone.utc)})
    db.commit()
    assert project_service.archive_project(db, beta).archived_at is not None
