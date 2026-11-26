"""Every administrative write lands in the audit trail (the audit of
2026-10-06, row 8): keys created, moved and revoked, the model key and the
writer changed, webhooks, projects and aliases, facts corrected from the
screen, an operator's login, and the commands of `manage`. Not one of them
reached GET /v1/audit or the screen's log before.

An operator is the `dashboard`, as in the hosted product; an admin key is
`rest`, with its id; a command is `manage`. No model is called.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent import manage
from korely_agent.db.models import AuditEvent
from korely_agent.services import keys as key_service
from korely_agent.services import operators as ops



@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr("korely_agent.services.webhooks.check_url", lambda u: u.strip())
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin(db, tenant):
    row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="admin",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}, row


@pytest.fixture
def a_secret(monkeypatch):
    """SECRET_KEY, which a key saved from the screen is encrypted with."""
    from korely_agent import config
    from korely_agent.core import llm

    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-test")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()
    llm.reset_provider_cache()


def events(db, action=None):
    db.expire_all()
    q = db.query(AuditEvent)
    if action:
        q = q.filter(AuditEvent.action == action)
    return q.order_by(AuditEvent.ts).all()


class TestTheScreensWrites:
    def test_a_key_created_moved_and_revoked(self, db, tenant, client, admin):
        head, admin_row = admin
        made = client.post("/admin/keys", headers=head, json={"name": "bot"}).json()
        proj = client.post("/admin/projects", headers=head, json={"name": "Support"}).json()
        client.post(f"/admin/keys/{made['id']}/move", headers=head,
                    json={"project_id": proj["id"]})
        client.post(f"/admin/keys/{made['id']}/revoke", headers=head)
        got = [(e.action, e.target_id) for e in events(db)
               if e.action in ("key_create", "key_change", "key_revoke")]
        assert got == [("key_create", made["id"]), ("key_change", made["id"]),
                       ("key_revoke", made["id"])]
        assert all(e.actor == "rest" and e.api_key_id == admin_row.id
                   for e in events(db, "key_create"))

    def test_the_writer_and_the_provider_key(self, db, tenant, client, admin, monkeypatch,
                                             a_secret):
        """The writer of the Models page and the model key of the provider
        page (PUT and DELETE /admin/provider), each set and cleared; the key
        itself never in the trail. Until the audit of 17 Oct 2026 this test
        covered only the writer, under the name of both."""
        from korely_agent.services import model_choice, provider_key

        head, _ = admin
        monkeypatch.setattr(model_choice, "save", lambda *a, **k: {"configured": True})
        assert client.put("/admin/models/scaleway", headers=head,
                          json={"key": "k" * 20, "model": "gpt-oss-120b"}).status_code == 200
        client.delete("/admin/models", headers=head)
        monkeypatch.setattr(provider_key, "check", lambda raw: {"ok": True, "detail": "ok"})
        google = "AIzaSy" + "g" * 30
        assert client.put("/admin/provider", headers=head,
                          json={"key": google}).status_code == 200
        assert client.delete("/admin/provider", headers=head).json()["deleted"] is True
        rows = events(db, "setting_change")
        assert [(e.meta.get("setting"), e.meta.get("op")) for e in rows] == [
            ("writer", "set"), ("writer", "clear"),
            ("provider_key", "set"), ("provider_key", "clear")]
        assert [e.target_id for e in rows[2:]] == ["provider", "provider"]
        trail = str([(e.meta, e.target_id) for e in events(db)])
        assert "k" * 20 not in trail and google not in trail, "a key in the trail"

    def test_webhooks_without_their_address(self, db, tenant, client, admin):
        head, _ = admin
        made = client.post("/admin/webhooks", headers=head,
                           json={"url": "https://example.com/hook?token=secret"}).json()
        client.delete(f"/admin/webhooks/{made['id']}", headers=head)
        got = [(e.meta["setting"], e.meta["op"]) for e in events(db, "setting_change")]
        assert got == [("webhook", "create"), ("webhook", "delete")]
        assert "secret" not in str([e.meta for e in events(db)])

    def test_a_project_renamed_and_deleted(self, db, tenant, client, admin):
        head, _ = admin
        proj = client.post("/admin/projects", headers=head, json={"name": "Support"}).json()
        client.patch(f"/admin/projects/{proj['id']}", headers=head, json={"name": "Help"})
        client.delete(f"/admin/projects/{proj['id']}", headers=head)
        ops_ = [e.meta["op"] for e in events(db, "setting_change") if e.meta.get("setting") == "project"]
        assert ops_ == ["create", "rename", "delete"]


class TestWhoDidIt:
    def test_an_operator_is_the_dashboard(self, db, tenant, client):
        row, password = ops.create(db, tenant_id=tenant.id, username="anna")
        token = client.post("/admin/session",
                            json={"username": "anna", "password": password}).json()["token"]
        client.post("/admin/keys", headers={"Authorization": f"Bearer {token}"},
                    json={"name": "bot"})
        login, created = events(db, "login")[0], events(db, "key_create")[0]
        assert (login.actor, login.target_id) == ("dashboard", "anna")
        assert (created.actor, created.api_key_id, created.meta["operator"]) == (
            "dashboard", None, "anna")


class TestTheScreensReads:
    """What a person reads of end users' memories and facts from the screen
    is in the trail as a /v1 read is: the ids returned, the end user, the
    operator's name, never the text nor the query (audit of 17 Oct 2026:
    only the writes were there, while the privacy policy says the reads are)."""

    @pytest.fixture
    def anna(self, db, tenant, client):
        _row, password = ops.create(db, tenant_id=tenant.id, username="anna")
        token = client.post("/admin/session",
                            json={"username": "anna", "password": password}).json()["token"]
        return {"Authorization": f"Bearer {token}"}

    def test_memories_facts_and_their_text_are_reads_of_the_dashboard(self, db, tenant, client,
                                                                       anna):
        import uuid
        from datetime import datetime, timezone

        from korely_agent.db.models import AgentFact, AgentMemory
        from korely_agent.services.ids import fact_public_id, mem_public_id
        from tests.conftest import seeded_project_id

        project = seeded_project_id(db, tenant.id)
        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="c4821",
                        project_id=project, agent_id="bot", content="Maria is on the Priority plan.")
        db.add(m)
        db.commit()
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="c4821", project_id=project,
                      subject_label="Maria", predicate_raw="is on", predicate_canonical="is_on",
                      object_label="Priority plan", source_memory_id=m.id,
                      valid_from=datetime(2026, 3, 15, tzinfo=timezone.utc))
        db.add(f)
        db.commit()
        mid, fid = mem_public_id(m.id), fact_public_id(f.id)
        for path in ("/admin/memories?user_id=c4821", f"/admin/memories/{mid}",
                     f"/admin/memories/{mid}/history", "/admin/facts?user_id=c4821",
                     f"/admin/audit/read?memories={mid}&facts={fid}"):
            assert client.get(path, headers=anna).status_code == 200, path
        reads = {e.meta["endpoint"]: e for e in events(db, "read")}
        assert set(reads) == {"admin:memories:list", "admin:memories:get",
                              "admin:memories:history", "admin:facts:list", "admin:audit:read"}
        for e in reads.values():
            assert (e.actor, e.api_key_id, e.meta["operator"]) == ("dashboard", None, "anna")
        assert reads["admin:memories:list"].read_ids == {"memories": [mid]}
        assert reads["admin:memories:list"].end_user_id == "c4821"
        one = reads["admin:memories:get"]
        assert (one.target_id, one.end_user_id, one.project_id) == (mid, "c4821", project)
        assert reads["admin:memories:history"].read_ids == {"memories": [mid], "facts": [fid]}
        assert reads["admin:facts:list"].read_ids == {"facts": [fid]}
        assert reads["admin:audit:read"].read_ids == {"memories": [mid], "facts": [fid]}
        assert "Priority" not in str([(e.meta, e.read_ids) for e in events(db)]), "text in the trail"

    def test_a_search_names_what_it_found_never_the_query(self, db, tenant, client, anna,
                                                          monkeypatch):
        import uuid

        from korely_agent.services import memory_engine

        hit = str(uuid.uuid4())
        monkeypatch.setattr(memory_engine.AgentMemoryEngine, "search",
                            lambda self, q, **kw: [{"id": hit, "content": "x", "score": 0.9}])
        r = client.get("/admin/memories/search", params={"query": "where does Maria live"},
                       headers=anna)
        assert r.status_code == 200, r.text
        (e,) = events(db, "read")
        from korely_agent.services.ids import mem_public_id

        assert e.meta["endpoint"] == "admin:memories:search"
        assert e.read_ids == {"memories": [mem_public_id(uuid.UUID(hit))]}
        assert "Maria" not in str(e.meta)


class TestManage:
    @pytest.fixture(autouse=True)
    def _the_test_session(self, db, monkeypatch):
        monkeypatch.setattr(manage, "_session", lambda: db)

    def test_a_tenant_its_key_and_an_operator(self, db, capsys):
        assert manage.create_tenant("Acme") == 0
        tid = events(db, "tenant_create")[0].tenant_id
        assert manage.create_operator(str(tid), "bruno") == 0
        assert manage.reset_password("bruno") == 0
        got = [(e.action, e.actor) for e in events(db)]
        assert got == [("tenant_create", "manage"), ("key_create", "manage"),
                       ("setting_change", "manage"), ("setting_change", "manage")]

    def test_a_key_issued_and_revoked(self, db, tenant, capsys):
        assert manage.issue_key(str(tenant.id), "ops", admin=True) == 0
        kid = events(db, "key_create")[0].target_id
        assert manage.revoke_key(str(tenant.id), kid) == 0
        assert [e.action for e in events(db)] == ["key_create", "key_revoke"]
        assert events(db, "key_create")[0].meta["admin"] is True


class TestManageFilesAKeysActsInItsProject:
    """A key made or revoked by `manage` is filed in the key's project, as
    the dashboard's and an admin key's acts are (since 1.0.12): up to 1.0.39
    `manage` filed it in none, and a key of the project did not see it in
    GET /v1/audit (the review of 7 Nov 2026)."""

    @pytest.fixture(autouse=True)
    def _the_test_session(self, db, monkeypatch):
        monkeypatch.setattr(manage, "_session", lambda: db)

    def test_issued_and_revoked_in_the_key_s_project(self, db, tenant, capsys):
        from korely_agent.db.models import AgentApiKey

        assert manage.issue_key(str(tenant.id), "ops") == 0
        kid = events(db, "key_create")[0].target_id
        project = key_service.key_project(db, db.get(AgentApiKey, uuid.UUID(kid)))
        assert project is not None
        assert manage.revoke_key(str(tenant.id), kid) == 0
        assert [(e.action, e.project_id) for e in events(db)] == [
            ("key_create", project), ("key_revoke", project)]

    def test_the_first_key_of_a_tenant_too(self, db, capsys):
        assert manage.create_tenant("Acme") == 0
        (made,) = events(db, "key_create")
        assert made.project_id is not None
        (tenant_made,) = events(db, "tenant_create")
        assert tenant_made.project_id is None, "an act on the account is the account's"
