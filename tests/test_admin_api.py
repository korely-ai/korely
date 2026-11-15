"""`/admin`: keys, webhooks, projects.

These were services with no route for as long as the product had no screen.
The screen is being built, so they have routes now, and the first thing to
establish is the one that is not about features: a key an agent runs with must
not be able to reach any of this. If it could, revoking a leaked key would mean
nothing, because the first thing it would do is issue a second one.
"""
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import (AgentApiKey, AgentEntityAlias, AgentFact, AgentMemory,
                                    AgentWebhookEndpoint, Tenant)
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service



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
    _row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="admin",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}


@pytest.fixture
def agent_key(db, tenant):
    """What an agent actually runs with: the default scopes, nothing more."""
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="agent")
    return {"Authorization": f"Bearer {full}"}


def _admin_routes(app):
    """Every /admin route of the app as it is built, whichever file declared
    it and wherever it was included (FastAPI keeps an included router as one
    node, as tests/test_every_route_is_rate_limited.py walks it)."""
    from fastapi.routing import APIRoute

    def walk(routes):
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                yield from walk(inner.routes)
            elif isinstance(r, APIRoute) and r.path.startswith("/admin"):
                yield r

    return list(walk(app.router.routes))


# The /admin routes that answer without an administrator, each with why:
# logging in and out (no session yet), and the scope names (knowing them
# opens nothing). The same three as tests/test_every_route_is_rate_limited.py.
PUBLIC = {"POST /admin/session", "DELETE /admin/session", "GET /admin/scopes"}


def _admin_calls(app):
    """Every /admin route of the app but the public ones, as (method, path to
    call, name), an id in each path parameter. Read from the app, not
    written here: the list written by hand had 15 of the 47 routes of
    api/admin.py and none of /admin/oauth, so a new route without its
    credential failed nothing (audit of 17 Oct 2026)."""
    out = []
    for route in _admin_routes(app):
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            name = f"{method} {route.path}"
            if name not in PUBLIC:
                out.append((method.lower(), re.sub(r"\{[^}]+\}", str(uuid.uuid4()), route.path), name))
    return out


class TestAnAgentKeyCannotAdminister:
    """The whole reason these are not under /v1."""

    @staticmethod
    def _call(client, method, path, headers=None):
        # TestClient.get/delete take no `json`; the write verbs need one.
        kw = {"headers": headers} if headers else {}
        if method in ("post", "patch", "put"):
            kw["json"] = {}
        return getattr(client, method)(path, **kw)

    def test_every_route_refuses_a_plain_key(self, client, agent_key):
        calls = _admin_calls(client.app)
        assert len(calls) >= 50, f"the walk found {len(calls)} routes: it is broken, not the app"
        let_in = [f"{name}: {r.status_code}" for method, path, name in calls
                  if (r := self._call(client, method, path, agent_key)).status_code != 403]
        assert let_in == []

    def test_every_route_refuses_no_key_at_all(self, client):
        let_in = [f"{name}: {r.status_code}" for method, path, name in _admin_calls(client.app)
                  if (r := self._call(client, method, path)).status_code not in (401, 403)]
        assert let_in == []

    def test_the_public_ones_are_still_routes(self, client):
        """A name left here after its route went would let a new route of
        that name in without its credential."""
        found = {f"{m} {r.path}" for r in _admin_routes(client.app) for m in r.methods}
        assert PUBLIC <= found, sorted(PUBLIC - found)

    def test_a_leaked_agent_key_cannot_issue_itself_another(self, client, agent_key):
        r = client.post("/admin/keys", headers=agent_key, json={"name": "second"})
        assert r.status_code == 403
        assert "scope" in r.json()["detail"].lower()


class TestEveryStatusIsDeclared:
    """A client generated from /openapi.json has a branch only for the
    statuses a route declares. Three routes answered a 409 they did not
    declare (audit of 17 Oct 2026): this reads what each /admin route raises
    and asks that it be in its `responses`."""

    # The helpers of api/admin.py that answer 404 for an id that is not the
    # account's, and the one that answers 409 for an archived project.
    _RAISES = [
        (re.compile(r"\bHTTP_(4\d\d|5\d\d)_"), None),
        (re.compile(r"\bstatus_code=(4\d\d|5\d\d)\b"), None),
        (re.compile(r"\b(_missing|_owned|account_project|_within_project|_known_provider|_uuid)\("), 404),
        (re.compile(r"to_hold_keys=True"), 409),
    ]

    def test_every_status_an_admin_route_raises_is_in_its_responses(self, client):
        import inspect

        undeclared = []
        for route in _admin_routes(client.app):
            src = inspect.getsource(route.endpoint)
            raised = set()
            for pattern, status in self._RAISES:
                for m in pattern.finditer(src):
                    raised.add(status or int(m.group(1)))
            declared = {int(k) for k in route.responses}
            for status in sorted(raised - declared):
                undeclared.append(f"{sorted(route.methods)} {route.path}: {status}")
        assert undeclared == [], undeclared


class TestKeys:
    def test_the_raw_key_is_shown_once_and_never_again(self, client, admin, db, tenant):
        r = client.post("/admin/keys", headers=admin, json={"name": "prod agent"})
        assert r.status_code == 201
        raw = r.json()["key"]
        assert raw.startswith("kor_self_"), (
            "a key minted by an install somebody runs themselves must not look "
            "like one from the hosted service; that is how a memory reached our "
            "servers before the 401 came back")

        listing = client.get("/admin/keys", headers=admin).json()
        assert all("key" not in k for k in listing), "the whole key came back in a listing"
        mine = [k for k in listing if k["name"] == "prod agent"][0]
        assert mine["last_four"] == raw[-4:]

    def test_a_new_key_is_not_an_admin_key(self, client, admin, db):
        """Administration is granted on purpose or not at all. A key minted
        through the dashboard must not inherit the power of the key that minted
        it, or the separation lasts exactly one click."""
        r = client.post("/admin/keys", headers=admin, json={"name": "made here"})
        assert key_service.ADMIN_SCOPE not in r.json()["scopes"]

        made = {"Authorization": f"Bearer {r.json()['key']}"}
        assert client.get("/admin/keys", headers=made).status_code == 403

    def test_revoked_keys_stay_in_the_list(self, client, admin):
        """"When did we turn that one off" is an incident question, and a list
        that hides them answers it with silence."""
        made = client.post("/admin/keys", headers=admin, json={"name": "doomed"}).json()
        assert client.post(f"/admin/keys/{made['id']}/revoke", headers=admin).status_code == 200

        listing = client.get("/admin/keys", headers=admin).json()
        doomed = [k for k in listing if k["name"] == "doomed"][0]
        assert doomed["revoked_at"] is not None

    def test_a_revoked_key_stops_working(self, client, admin):
        made = client.post("/admin/keys", headers=admin, json={"name": "doomed"}).json()
        used = {"Authorization": f"Bearer {made['key']}"}
        assert client.get("/v1/ping", headers=used).status_code == 200

        client.post(f"/admin/keys/{made['id']}/revoke", headers=admin)
        assert client.get("/v1/ping", headers=used).status_code == 401

    def test_revoking_twice_is_a_404(self, client, admin):
        made = client.post("/admin/keys", headers=admin, json={"name": "doomed"}).json()
        client.post(f"/admin/keys/{made['id']}/revoke", headers=admin)
        assert client.post(f"/admin/keys/{made['id']}/revoke", headers=admin).status_code == 404

    def test_a_mistyped_id_is_a_404_not_a_500(self, client, admin):
        assert client.post("/admin/keys/not-a-uuid/revoke", headers=admin).status_code == 404


class TestWebhooks:
    def test_the_secret_is_shown_once(self, client, admin):
        r = client.post("/admin/webhooks", headers=admin,
                        json={"url": "https://example.com/hook"})
        assert r.status_code == 201
        assert r.json()["secret"].startswith("whsec_")

        listing = client.get("/admin/webhooks", headers=admin).json()
        assert all("secret" not in h for h in listing), \
            "without the secret nobody can verify a delivery, and with it in a listing anybody can forge one"

    def test_the_listing_says_which_endpoints_sign_the_old_way(self, db, tenant, client, admin):
        """Endpoints made before 0.1.11 keep their old key so their receivers
        keep working; the screen has to be able to say which ones those are,
        or nobody knows what to rotate."""
        import secrets

        from korely_agent.db.models import AgentWebhookEndpoint

        db.add(AgentWebhookEndpoint(tenant_id=tenant.id, url="https://example.com/old",
                                    events=["*"], secret="whsec_" + secrets.token_urlsafe(24)))
        db.commit()
        client.post("/admin/webhooks", headers=admin, json={"url": "https://example.com/new"})
        listing = {h["url"]: h for h in client.get("/admin/webhooks", headers=admin).json()}
        assert listing["https://example.com/old"]["legacy_signature"] is True
        assert listing["https://example.com/new"]["legacy_signature"] is False

    def test_failure_count_is_the_endpoints_own(self, db, tenant, client, admin):
        """A delivery waiting for its first attempt is not a failure, and
        Enable forgets the failures (audit of 17 Oct 2026): the count was the
        deliveries not yet delivered, so a new, healthy endpoint read
        "1 recent failures" and Enable left the number as it was."""
        from korely_agent.db.models import AgentWebhookEndpoint
        from korely_agent.services import webhooks

        made = client.post("/admin/webhooks", headers=admin,
                           json={"url": "https://example.com/hook"}).json()
        webhooks.emit_event(db, tenant_id=tenant.id, event_type="memory.created",
                            data={"id": "mem_1"})
        listing = {h["id"]: h for h in client.get("/admin/webhooks", headers=admin).json()}
        assert listing[made["id"]]["failure_count"] == 0

        ep = db.get(AgentWebhookEndpoint, uuid.UUID(made["id"]))
        ep.failure_count, ep.active = 7, False
        db.commit()
        listing = {h["id"]: h for h in client.get("/admin/webhooks", headers=admin).json()}
        assert listing[made["id"]]["failure_count"] == 7
        enabled = client.post(f"/admin/webhooks/{made['id']}/enable", headers=admin)
        assert enabled.status_code == 200 and enabled.json()["failure_count"] == 0
        listing = {h["id"]: h for h in client.get("/admin/webhooks", headers=admin).json()}
        assert listing[made["id"]]["failure_count"] == 0

    def test_an_unknown_event_is_refused_by_name(self, client, admin):
        """Unknown names used to be dropped in silence, and when none was left
        the endpoint was subscribed to everything. A typo in the one event you
        wanted bought you all of them; `quota.warning`, which this install has
        never sent, got an endpoint that waits for nothing."""
        r = client.post("/admin/webhooks", headers=admin, json={
            "url": "https://example.com/hook",
            "events": ["memory.created", "quota.warning", "memory.craeted"]})
        # 422, a value the route does not take, as an unknown scope is on
        # POST /admin/keys (audit of 17 Oct 2026: it was a 400).
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert "quota.warning" in detail and "memory.craeted" in detail
        assert "key.expiring" in detail, "say which names there are"
        assert client.get("/admin/webhooks", headers=admin).json() == [], "made one anyway"

    def test_an_empty_list_is_refused(self, client, admin):
        """An empty list asks for nothing, which is not an endpoint anybody
        wants; it was read as everything."""
        r = client.post("/admin/webhooks", headers=admin,
                        json={"url": "https://example.com/hook", "events": []})
        assert r.status_code == 422, r.text
        assert "*" in r.json()["detail"], "say how to ask for all of them"
        assert client.get("/admin/webhooks", headers=admin).json() == []

    @pytest.mark.parametrize("events,stored", [
        (["*"], ["*"]),
        (["key.expiring", "key.expired"], ["key.expiring", "key.expired"]),
        (None, ["*"]),     # left out: every event, as it always was
    ])
    def test_what_is_accepted(self, client, admin, events, stored):
        body = {"url": "https://example.com/hook"}
        if events is not None:
            body["events"] = events
        r = client.post("/admin/webhooks", headers=admin, json=body)
        assert r.status_code == 201, r.text
        assert r.json()["events"] == stored

    def test_an_unsafe_url_is_refused(self, client, admin, monkeypatch):
        """A webhook URL is an address this server connects to, chosen by
        whoever holds the credential."""
        from korely_agent.services import webhooks

        def refuse(u):
            raise webhooks.UnsafeWebhookUrl("resolves to a private address")

        monkeypatch.setattr(webhooks, "check_url", refuse)
        r = client.post("/admin/webhooks", headers=admin,
                        json={"url": "http://169.254.169.254/latest/meta-data/"})
        assert r.status_code == 400
        assert "private" in r.json()["detail"]

    def test_delete(self, client, admin):
        made = client.post("/admin/webhooks", headers=admin,
                           json={"url": "https://example.com/h"}).json()
        assert client.delete(f"/admin/webhooks/{made['id']}", headers=admin).status_code == 200
        assert client.get("/admin/webhooks", headers=admin).json() == []
        assert client.delete(f"/admin/webhooks/{made['id']}", headers=admin).status_code == 404


    def test_the_events_offered_are_the_ones_an_endpoint_takes(self, client, admin, agent_key):
        """GET /admin/webhooks/events, which only the screen's own test read
        (audit of 17 Oct 2026): the product's events, each one accepted by
        a new endpoint, and an event another part of the codebase registers
        (the hosted service's `quota.warning`) offered as well."""
        from korely_agent.services import webhooks

        r = client.get("/admin/webhooks/events", headers=admin)
        assert r.status_code == 200
        offered = r.json()["events"]
        assert offered == ["fact.invalidated", "key.expired", "key.expiring", "memory.created"]
        for event in offered:
            made = client.post("/admin/webhooks", headers=admin,
                               json={"url": f"https://example.com/{event}", "events": [event]})
            assert made.status_code == 201, (event, made.text)

        webhooks.add_event("quota.warning", account_wide=True)
        assert "quota.warning" in client.get("/admin/webhooks/events", headers=admin).json()["events"]
        assert client.get("/admin/webhooks/events", headers=agent_key).status_code == 403


class TestProjects:
    def test_create_list_get_rename(self, client, admin):
        made = client.post("/admin/projects", headers=admin,
                           json={"name": "Production"}).json()
        assert made["slug"] == "production"
        assert made["is_default"] is False

        assert client.get(f"/admin/projects/{made['id']}", headers=admin).json()["name"] == "Production"

        renamed = client.patch(f"/admin/projects/{made['id']}", headers=admin,
                               json={"name": "Prod"}).json()
        assert renamed["name"] == "Prod"
        assert renamed["slug"] == "production", "the slug is a stable handle and must survive a rename"

    def test_a_name_is_taken_up_to_its_limit_and_refused_past_it(self, client, admin):
        """One rule for a name's length, the column's (projects.NAME_MAX): a
        name of 65 to 128 characters made a slug the column refused, and the
        create answered 500 (QA of 14 Nov 2026); past 128 the door refuses
        with a 422 that says the field and its limit, which the screen shows."""
        from korely_agent.services import projects

        assert projects.NAME_MAX == 128
        for length in (64, 65, 70, 100, projects.NAME_MAX):
            name = ("Project %d " % length + "a very long name " * 10)[:length]
            r = client.post("/admin/projects", headers=admin, json={"name": name})
            assert r.status_code == 201, (length, r.text)
            assert r.json()["name"] == name.strip()
            assert len(r.json()["slug"]) <= projects.SLUG_MAX
        renamed = client.patch(f"/admin/projects/{r.json()['id']}", headers=admin,
                               json={"name": "b" * projects.NAME_MAX})
        assert renamed.status_code == 200, renamed.text

        too_long = "x" * (projects.NAME_MAX + 1)
        for r in (client.post("/admin/projects", headers=admin, json={"name": too_long}),
                  client.patch(f"/admin/projects/{renamed.json()['id']}", headers=admin,
                               json={"name": too_long})):
            assert r.status_code == 422, r.text
            assert r.json()["message"].startswith("name"), r.json()
            assert str(projects.NAME_MAX) in r.json()["message"]

    def test_a_name_is_measured_without_the_spaces_around_it(self, client, admin):
        """The door counted before the spaces were taken off: a name of spaces
        only was created empty with the slug "project", and a space before
        128 characters was refused (the skeptic's finding of 14 Nov 2026).
        One type at both doors (admin.ProjectName)."""
        from korely_agent.services import projects

        for blank in ("   ", "\t \n"):
            r = client.post("/admin/projects", headers=admin, json={"name": blank})
            assert r.status_code == 422, r.text
        full = "y" * projects.NAME_MAX
        made = client.post("/admin/projects", headers=admin, json={"name": f"  {full} "})
        assert made.status_code == 201, made.text
        assert made.json()["name"] == full
        renamed = client.patch(f"/admin/projects/{made.json()['id']}", headers=admin,
                               json={"name": " z" * 2})
        assert renamed.status_code == 200 and renamed.json()["name"] == "z z"
        blanked = client.patch(f"/admin/projects/{made.json()['id']}", headers=admin,
                               json={"name": "  "})
        assert blanked.status_code == 422, blanked.text

    def test_a_duplicate_name_is_409(self, client, admin):
        client.post("/admin/projects", headers=admin, json={"name": "Staging"})
        r = client.post("/admin/projects", headers=admin, json={"name": "Staging"})
        assert r.status_code == 409
        # The code the screen maps to its own sentence (ProjectsPage).
        assert r.json()["code"] == r.json()["detail"]["code"] == "project_name_taken"

    def test_a_project_with_a_live_key_is_not_archived(self, client, admin):
        """Archived is hidden, and a live key in it goes on writing there: an
        agent filling a project nobody can see (round 13)."""
        proj = client.post("/admin/projects", headers=admin, json={"name": "Busy"}).json()
        k = client.post("/admin/keys", headers=admin,
                        json={"name": "busy bot", "project_id": proj["id"]}).json()
        r = client.post(f"/admin/projects/{proj['id']}/archive", headers=admin)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "project_has_live_keys"
        assert "1 live key" in r.json()["message"]

        assert client.post(f"/admin/keys/{k['id']}/revoke", headers=admin).status_code == 200
        assert client.post(f"/admin/projects/{proj['id']}/archive", headers=admin).status_code == 200

    def test_archived_projects_are_out_of_the_list_unless_asked_for(self, client, admin):
        made = client.post("/admin/projects", headers=admin, json={"name": "Old"}).json()
        client.post(f"/admin/projects/{made['id']}/archive", headers=admin)

        listing = client.get("/admin/projects", headers=admin).json()
        assert "Old" not in [p["name"] for p in listing["projects"]]
        with_archived = client.get("/admin/projects?include_archived=true",
                                   headers=admin).json()
        assert "Old" in [p["name"] for p in with_archived["projects"]]

    def test_an_archived_project_comes_back(self, client, admin):
        """Archiving could not be undone: the service could, nothing reached it
        (the audit of 2026-10-06, row 14)."""
        made = client.post("/admin/projects", headers=admin, json={"name": "Paused"}).json()
        client.post(f"/admin/projects/{made['id']}/archive", headers=admin)
        back = client.post(f"/admin/projects/{made['id']}/unarchive", headers=admin)
        assert back.status_code == 200 and back.json()["archived_at"] is None
        listing = client.get("/admin/projects", headers=admin).json()
        assert "Paused" in [p["name"] for p in listing["projects"]]
        assert client.post(f"/admin/projects/{made['id']}/unarchive",
                           headers=admin).status_code == 200, "twice is no error"

    def test_no_key_is_put_in_an_archived_project(self, client, admin):
        """Archiving refuses a project with live keys, and POST /admin/keys
        and the move made one there the moment after: a live key writing in
        a project the list hides (audit of 17 Oct 2026)."""
        made = client.post("/admin/projects", headers=admin, json={"name": "Shelved"}).json()
        assert client.post(f"/admin/projects/{made['id']}/archive", headers=admin).status_code == 200

        created = client.post("/admin/keys", headers=admin,
                              json={"name": "late bot", "project_id": made["id"]})
        assert created.status_code == 409
        assert created.json()["detail"]["code"] == "project_archived"

        other = client.post("/admin/keys", headers=admin, json={"name": "elsewhere"}).json()
        moved = client.post(f"/admin/keys/{other['id']}/move", headers=admin,
                            json={"project_id": made["id"]})
        assert moved.status_code == 409
        assert moved.json()["detail"]["code"] == "project_archived"

        client.post(f"/admin/projects/{made['id']}/unarchive", headers=admin)
        assert client.post(f"/admin/keys/{other['id']}/move", headers=admin,
                           json={"project_id": made["id"]}).status_code == 200

    def test_the_default_project_cannot_be_removed(self, client, admin, db, tenant):
        default = project_service.get_or_create_default_project(db, tenant.id)
        archived = client.post(f"/admin/projects/{default.id}/archive", headers=admin)
        deleted = client.delete(f"/admin/projects/{default.id}", headers=admin)
        assert archived.status_code == deleted.status_code == 409
        assert archived.json()["detail"]["code"] == "cannot_archive_default"
        assert deleted.json()["detail"]["code"] == "cannot_delete_default"

    def test_every_code_the_screen_maps_is_one_the_api_sends(self):
        """The screen had a sentence for four codes the API never sent, so every
        refusal read "Something went wrong" (round 13)."""
        import re
        from pathlib import Path

        page = (Path(__file__).resolve().parents[1] / "dashboard/src/pages/ProjectsPage.tsx").read_text()
        mapped = set(re.findall(r'case "([a-z_]+)":', page))
        sent = {project_service.DuplicateProjectError.code} | {
            c.code for c in project_service.ProjectRefused.__subclasses__()}
        assert mapped, "no case found: the parse is wrong, not the page"
        assert mapped <= sent, f"the screen maps codes the API does not send: {sorted(mapped - sent)}"

    def test_deleting_a_project_moves_its_keys_rather_than_stranding_them(
            self, client, admin, db, tenant):
        """The foreign key is SET NULL, so without the move the keys would
        survive with no project and vanish from every listing that filters by
        one. A live key nobody can see is worse than a deleted project."""
        proj = client.post("/admin/projects", headers=admin, json={"name": "Temp"}).json()
        made = client.post("/admin/keys", headers=admin,
                           json={"name": "in temp", "project_id": proj["id"]}).json()
        assert made["project_id"] == proj["id"]

        r = client.delete(f"/admin/projects/{proj['id']}", headers=admin)
        assert r.status_code == 200
        assert r.json()["keys_moved"] == 1

        used = {"Authorization": f"Bearer {made['key']}"}
        assert client.get("/v1/ping", headers=used).status_code == 200, "the key died with its project"

        row = db.query(AgentApiKey).filter(AgentApiKey.id == uuid.UUID(made["id"])).one()
        default = project_service.get_or_create_default_project(db, tenant.id)
        assert row.project_id == default.id

    def test_deleting_a_project_moves_what_it_held_to_the_default_one(
            self, client, admin, db, tenant):
        """What a deleted project held has to stay reachable.

        This test asserted the opposite until 2026-09-13: that memories and
        facts came out of a project delete with `project_id is None`. It was
        written when a project was a label, and NULL then meant "filed under
        nothing", which was harmless. Once the project became a wall, NULL is
        an address, and the keys were being sent to a different one: the delete
        moved the keys to the default project's concrete id while the rows kept
        NULL, so they never met again.

        A tester deleted a project and then asked the API to erase what had
        been in it. It answered memories_forgotten=0, facts_invalidated=0 and
        an audit id, over one memory and three facts that were still there.
        Nothing had leaked; it had become unreadable and undeletable, which for
        a product that promises erasure is the worse of the two.

        So this is the third test in this suite found certifying the behaviour
        it was supposed to guard. The name it had, "does not delete what anyone
        remembered", was even true: nothing was deleted. It just could not be
        reached or removed afterwards.
        """
        proj = client.post("/admin/projects", headers=admin, json={"name": "Temp"}).json()
        pid = uuid.UUID(proj["id"])

        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                        content="Maria is on the Priority plan.", project_id=pid)
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                      subject_label="Maria", predicate_raw="is on",
                      predicate_canonical="is_on", object_label="Priority plan",
                      project_id=pid)
        db.add_all([m, f])
        db.commit()

        r = client.delete(f"/admin/projects/{proj['id']}", headers=admin)
        assert r.status_code == 200
        assert r.json()["memories_moved"] == 1
        assert r.json()["facts_moved"] == 1
        db.expire_all()

        default = project_service.get_or_create_default_project(db, tenant.id)
        assert db.query(AgentMemory).filter(AgentMemory.id == m.id).one().project_id == default.id
        assert db.query(AgentFact).filter(AgentFact.id == f.id).one().project_id == default.id

    def test_erasing_a_project_leaves_a_row_in_the_erasure_audit(
            self, client, admin, db, tenant):
        """`?erase=true` is an erasure, and every other erasure here files an
        `agent_forget_audit` row whose id its reply carries. This one wrote
        none, while the service's docstring said it did (2026-09-28)."""
        from korely_agent.db.models import AgentForgetAudit
        from korely_agent.services.ids import audit_public_id

        proj = client.post("/admin/projects", headers=admin, json={"name": "Gone"}).json()
        pid = uuid.UUID(proj["id"])
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                           content="Maria is on the Priority plan.", project_id=pid))
        db.commit()

        r = client.delete(f"/admin/projects/{proj['id']}", headers=admin,
                          params={"erase": "true"})
        assert r.status_code == 200, r.text
        db.expire_all()
        (row,) = db.query(AgentForgetAudit).filter(AgentForgetAudit.tenant_id == tenant.id).all()
        assert (row.scope, row.target_id, row.memories_forgotten) == ("project", str(pid), 1)
        assert r.json()["audit_id"] == audit_public_id(row.id)

    def test_moving_a_project_is_not_an_erasure(self, client, admin, db, tenant):
        from korely_agent.db.models import AgentForgetAudit

        proj = client.post("/admin/projects", headers=admin, json={"name": "Kept"}).json()
        r = client.delete(f"/admin/projects/{proj['id']}", headers=admin)
        assert r.status_code == 200, r.text
        assert r.json().get("audit_id") is None
        # Only the record of the move, which a restore makes again (audit
        # of 20 Oct 2026); no erasure.
        assert [(a.scope, a.target_id, a.end_user_id) for a in db.query(AgentForgetAudit).filter(
            AgentForgetAudit.tenant_id == tenant.id)] == [("move", proj["id"], None)]


class TestIsolation:
    """One install holds several of an agency's own clients. A route that
    forgets the tenant filter hands one of them another's."""

    @pytest.fixture
    def other(self, db):
        t = Tenant(id=uuid.uuid4(), name="Somebody Else")
        db.add(t)
        db.commit()
        _row, full = key_service.create_api_key(
            db, tenant_id=t.id, name="their admin",
            scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
        return t, {"Authorization": f"Bearer {full}"}

    def test_keys(self, client, admin, other):
        _t, their = other
        client.post("/admin/keys", headers=admin, json={"name": "mine"})
        assert [k["name"] for k in client.get("/admin/keys", headers=their).json()] == ["their admin"]

    def test_webhooks(self, client, admin, other):
        _t, their = other
        client.post("/admin/webhooks", headers=admin, json={"url": "https://mine.example/h"})
        assert client.get("/admin/webhooks", headers=their).json() == []

    def test_projects(self, client, admin, other):
        _t, their = other
        mine = client.post("/admin/projects", headers=admin, json={"name": "Mine"}).json()
        assert "Mine" not in [p["name"]
                              for p in client.get("/admin/projects", headers=their).json()["projects"]]
        assert client.get(f"/admin/projects/{mine['id']}", headers=their).status_code == 404
        assert client.delete(f"/admin/projects/{mine['id']}", headers=their).status_code == 404

    def test_one_tenant_cannot_revoke_anothers_key(self, client, admin, other):
        _t, their = other
        made = client.post("/admin/keys", headers=admin, json={"name": "mine"}).json()
        assert client.post(f"/admin/keys/{made['id']}/revoke", headers=their).status_code == 404

        used = {"Authorization": f"Bearer {made['key']}"}
        assert client.get("/v1/ping", headers=used).status_code == 200, \
            "a key was revoked by a tenant that does not own it"

    def test_anothers_memory_and_alias_are_not_found(self, client, admin, other, db, tenant):
        from korely_agent.db.models import AgentEntityAlias
        from korely_agent.services.ids import mem_public_id

        _t, their = other
        mem = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, content="mine",
                          end_user_id="maria")
        alias = AgentEntityAlias(id=uuid.uuid4(), tenant_id=tenant.id,
                                 alias_label="Acme", alias_lower="acme", canonical_label="Globex")
        db.add_all([mem, alias])
        db.commit()
        mid = mem_public_id(mem.id)
        for method, path in (("get", f"/admin/memories/{mid}"),
                             ("get", f"/admin/memories/{mid}/history"),
                             ("delete", f"/admin/memories/{mid}"),
                             ("delete", f"/admin/aliases/{alias.id}")):
            got = getattr(client, method)(path, headers=their)
            assert got.status_code == 404, (method, path)
            assert getattr(client, method)(path, headers=admin).status_code == 200, (method, path)

    def test_every_lookup_by_id_goes_through_the_wall(self):
        """The lookup of a row by the id in the path, and its 404, were
        written by hand in each route: one that forgot `key.tenant_id` would
        read another account's row (audit of 17 Oct 2026). A 404 is raised
        by `_missing`, from `_owned` and `account_project`, and by the routes whose
        404 is not a row of the account (a route the Cloud has not, a
        provider name, no provider key to test)."""
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "korely_agent/api/admin.py").read_text()
        not_a_row = {"_missing", "_not_on_the_service", "_known_provider",
                     "test_provider_key", "check_models_key"}
        by_hand = sorted(
            f.name for f in ast.walk(ast.parse(src))
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
            and f.name not in not_a_row
            and any(isinstance(n, ast.Attribute) and n.attr == "HTTP_404_NOT_FOUND"
                    for n in ast.walk(f)))
        assert by_hand == [], f"a 404 written by hand, not through _owned/account_project: {by_hand}"


class TestEveryAccountHasAProject:
    """A tenant with no project cannot be shown.

    The model has always said "exactly one is_default", and nothing created
    it. A brand-new install answered `/admin/projects` with an empty list, the
    switcher said "Select project" with nothing to select, and every page that
    scopes by project had no scope. Found in a screenshot, not by any of the
    six hundred tests.
    """

    def test_create_tenant_makes_one(self, db, capsys):
        from korely_agent import manage
        from korely_agent.db.models import AgentProject, Tenant

        assert manage.main(["create-tenant", "--name", "Brand New"]) == 0
        t = db.query(Tenant).filter(Tenant.name == "Brand New").one()
        rows = db.query(AgentProject).filter(AgentProject.tenant_id == t.id).all()
        assert len(rows) == 1
        assert rows[0].is_default is True

    def test_an_account_made_before_the_fix_mends_itself(self, db, tenant, client, admin):
        """Rather than needing a command nobody knows to run."""
        from korely_agent.db.models import AgentProject

        db.query(AgentProject).filter(AgentProject.tenant_id == tenant.id).delete()
        db.commit()
        assert db.query(AgentProject).filter(
            AgentProject.tenant_id == tenant.id).count() == 0

        out = client.get("/admin/projects", headers=admin).json()
        assert len(out["projects"]) == 1
        assert out["projects"][0]["is_default"] is True



class TestAliases:
    """2026-09-27: the operator merges two names and undoes it (memory_core)."""

    def test_merge_and_undo(self, client, admin, db, tenant):
        from korely_agent.db.models import AgentFact
        from korely_agent.services.facts import service

        from korely_agent.services import projects

        default = projects.get_or_create_default_project(db, tenant.id).id
        service.write_fact_direct(db, tenant.id, subject="Acme", predicate="based_in",
                                  obj="Milan", end_user_id="maria", subject_type="organization",
                                  project_id=default)
        r = client.post("/admin/aliases", headers=admin,
                        json={"alias": "Acme", "canonical": "Globex", "user_id": "maria"})
        assert r.status_code == 201, r.text
        aid = r.json()["id"]
        assert client.get("/admin/aliases", headers=admin).json()["aliases"][0]["canonical"] == "Globex"
        db.expire_all()
        assert db.query(AgentFact).one().subject_canonical == "Globex"
        r = client.post("/admin/aliases", headers=admin,
                        json={"alias": "Globex", "canonical": "Acme", "user_id": "maria"})
        assert r.status_code == 409
        assert client.delete(f"/admin/aliases/{aid}", headers=admin).status_code == 200
        db.expire_all()
        assert db.query(AgentFact).one().subject_canonical == "Acme"

    def test_the_same_name_twice_is_a_422(self, client, admin):
        """A value the route does not take, the status every route answers
        for one (audit of 17 Oct 2026: it was a 400)."""
        r = client.post("/admin/aliases", headers=admin,
                        json={"alias": "Acme", "canonical": "Acme", "user_id": "maria"})
        assert r.status_code == 422, r.text

    def test_an_alias_with_no_project_lands_in_the_keys_project(self, client, admin, db, tenant):
        """The facts of an install with projects are in the key's project: an
        alias posted with no project_id sat in a memory of no project, was
        accepted (201) and never applied (17 Oct 2026)."""
        from korely_agent.db.models import AgentFact
        from korely_agent.services import projects
        from korely_agent.services.facts import service

        default = projects.get_or_create_default_project(db, tenant.id).id
        service.write_fact_direct(db, tenant.id, subject="Elena Poli", predicate="lives_in",
                                  obj="Milan", end_user_id="elena", subject_type="person",
                                  project_id=default)
        r = client.post("/admin/aliases", headers=admin,
                        json={"alias": "Elena Poli", "canonical": "elena", "user_id": "elena"})
        assert r.status_code == 201, r.text
        assert r.json()["project_id"] == str(default)
        db.expire_all()
        assert db.query(AgentFact).one().subject_canonical == "elena"


class TestOneInputErrorOneStatus:
    """A client decides on the status: the same mistake gets the same one on
    every /admin route (audit of 17 Oct 2026). A project_id that is not one
    of the account's, malformed or another account's, is a 404 wherever it is
    sent, the OAuth consent included (tests/test_oauth.py)."""

    @pytest.mark.parametrize("path,body", [
        ("/admin/keys", {"name": "k"}),
        ("/admin/webhooks", {"url": "https://example.com/hook"}),
        ("/admin/aliases", {"alias": "Acme", "canonical": "Globex"}),
    ])
    # Named, not shown: a random id in a test's name differs in every process
    # that collects it, and a parallel run (pytest-xdist) refuses to start.
    @pytest.mark.parametrize("project_id", ["not-a-uuid", str(uuid.uuid4())],
                             ids=["malformed", "no-project-of-the-account"])
    def test_a_project_id_not_the_accounts_is_a_404(self, client, admin, path, body, project_id):
        r = client.post(path, headers=admin, json={**body, "project_id": project_id})
        assert r.status_code == 404, r.text
        assert r.json()["code"] == "not_found"

    @pytest.mark.parametrize("path,body,model", [
        ("/admin/keys", {"name": "k"}, AgentApiKey),
        ("/admin/webhooks", {"url": "https://example.com/hook"}, AgentWebhookEndpoint),
        ("/admin/aliases", {"alias": "Acme", "canonical": "Globex"}, AgentEntityAlias),
    ], ids=["key", "webhook", "alias"])
    def test_an_empty_project_id_in_a_body_is_a_422_that_makes_nothing(
            self, client, admin, db, tenant, path, body, model):
        """`"project_id": ""` was read as no project at all: a key of the
        Default project, an alias of the key's own project (7 Nov 2026).
        Refused as the reads of the screen refuse `project_id=`
        (tests/test_an_empty_project_is_never_left_out.py)."""
        def made():
            db.expire_all()
            return db.query(model).filter(model.tenant_id == tenant.id).count()

        before = made()
        r = client.post(path, headers=admin, json={**body, "project_id": ""})
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "invalid_request"
        assert made() == before

    def test_an_empty_project_id_never_moves_a_key(self, client, admin, db):
        """A move with `"project_id": ""` took the key out of its project,
        as a move with none does: a key of no project, which reads and writes
        the Default one (keys.key_project) (7 Nov 2026)."""
        made = client.post("/admin/projects", headers=admin, json={"name": "Kept"}).json()
        k = client.post("/admin/keys", headers=admin,
                        json={"name": "kept bot", "project_id": made["id"]}).json()
        moved = client.post(f"/admin/keys/{k['id']}/move", headers=admin,
                            json={"project_id": ""})
        assert moved.status_code == 422, moved.text
        assert moved.json()["code"] == "invalid_request"
        db.expire_all()
        assert str(db.get(AgentApiKey, uuid.UUID(k["id"])).project_id) == made["id"]


def test_a_nul_byte_on_the_admin_doors_is_a_422_not_a_500(client, admin):
    """Audit of 19 Oct 2026: the read-input rule of services/limits.py, which
    the audit of 18 Oct 2026 gave /v1 and MCP, was not asked by the /admin doors that narrow
    on free text; a NUL byte reached the driver and answered 500."""
    for method, path in (("get", "/admin/facts?user_id=a%00b"),
                         ("get", "/admin/facts?entity=a%00b"),
                         ("get", "/admin/memories?user_id=a%00b"),
                         ("get", "/admin/memories?agent_id=a%00b"),
                         ("get", "/admin/memories/search?query=a%00b"),
                         ("delete", "/admin/users/a%00b/memories")):
        r = getattr(client, method)(path, headers=admin)
        assert r.status_code == 422, (path, r.status_code)
        assert "NUL" in r.text, path
