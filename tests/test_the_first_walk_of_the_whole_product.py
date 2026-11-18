"""The six defects of the review of 14 Sep 2026, each with the hole
reproduced.

That review was the first to walk the whole product instead of five criteria.
Five of the six defects were in the two thirds nobody had ever used.
"""
import uuid

import pytest

from korely_agent.services import keys as key_service



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


def _key(db, tenant, project_id=None, name=None, admin=False):
    scopes = ["memories:read", "memories:write"] + (["admin"] if admin else [])
    _row, plain = key_service.create_api_key(
        db, tenant_id=tenant.id, name=name or f"k-{uuid.uuid4().hex[:6]}",
        project_id=project_id, scopes=scopes)
    return {"Authorization": f"Bearer {plain}"}


class TestTheDatabaseComesBackAfterAReboot:
    """Not a runtime test: the policy is in the file or the machine stays down.

    Caddy and the API had `restart: unless-stopped` and the database did not, so
    a reboot brought back two services out of three. The API sat in a restart
    loop, Caddy answered, and the address returned 502 — the failure shape a
    check that only asks "is 443 alive" reports as healthy.
    """

    def test_every_service_restarts_by_itself(self):
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[1]
        text = (root / "docker-compose.yml").read_text()
        body = text.split("\nservices:", 1)[1].split("\nvolumes:", 1)[0]
        # Split on the two-space service headings. Read by hand rather than with
        # a YAML parser so the test needs nothing the image does not already
        # have; the shape being checked is one line per service.
        blocks = re.split(r"\n  (?=[a-z][a-z0-9_-]*:\s*$)", body, flags=re.M)
        found = 0
        for block in blocks:
            m = re.match(r"\s*([a-z][a-z0-9_-]*):\s*$", block.splitlines()[0] if block.strip() else "")
            if not m:
                continue
            found += 1
            # A one-shot step (the export directory's owner) runs before the
            # API and exits; `unless-stopped` would run it again for ever. It
            # is allowed only as a step some service waits for, so a service
            # that was meant to stay up cannot hide here.
            if 'restart: "no"' in block:
                waited_for = re.search(
                    rf"{re.escape(m.group(1))}:\s*\{{condition: service_completed_successfully\}}", body)
                assert waited_for, (
                    f"{m.group(1)} does not restart and nothing waits for it to finish: "
                    f"either it is a step and something depends on it, or it must come back")
                continue
            assert "restart: unless-stopped" in block, (
                f"{m.group(1)} does not come back after a reboot, and the other "
                f"services do, so the box comes up half-working"
            )
        assert found >= 3, f"only found {found} services; the parse is wrong, not the file"


class TestCorrectingAFactDoesNotHideIt:
    """A correction used to land outside every project, so no key could read it.

    The human saw the old value closed and no replacement: the correction did
    not change the truth, it made the truth disappear.
    """

    def test_the_correction_stays_in_the_project(self, db, tenant, client):
        from korely_agent.db.models import AgentProject

        proj = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Alpha",
                            slug="alpha", is_default=False)
        db.add(proj); db.commit()
        head = _key(db, tenant, project_id=proj.id, admin=True)

        r = client.post("/v1/facts", headers=head, json={
            "user_id": "u1", "subject": "u1", "predicate": "is_on",
            "object": "Gold plan"})
        assert r.status_code == 201, r.text
        fid = r.json()["id"]

        r = client.patch(f"/admin/facts/{fid}", headers=head,
                         json={"object": "Bronze plan"})
        assert r.status_code == 200, r.text

        out = client.get("/v1/facts?user_id=u1", headers=head)
        assert out.status_code == 200
        objects = [f["object"] for f in out.json()["facts"]]
        assert "Bronze plan" in objects, (
            f"the corrected value is unreachable through the API: {objects}")

    def test_nobody_can_write_a_fact_without_saying_where(self):
        """The class of defect, closed at the door rather than one caller at a time."""
        import inspect

        from korely_agent.services.facts import service as facts_service

        sig = inspect.signature(facts_service._persist_one)
        p = sig.parameters["project_id"]
        assert p.default is inspect.Parameter.empty, (
            "project_id has a default again; a default of None means a row no "
            "key can read, and one caller out of three already forgot it once")


class TestTheListsRespectTheWall:
    """Memories and facts were behind the wall. The listings were not."""

    def _two_projects(self, db, tenant, client):
        from korely_agent.db.models import AgentProject

        a = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Alpha",
                         slug="alpha", is_default=False)
        b = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Beta",
                         slug="beta", is_default=False)
        db.add_all([a, b]); db.commit()
        ka = _key(db, tenant, project_id=a.id, name="alpha-key")
        kb = _key(db, tenant, project_id=b.id, name="beta-key")
        client.post("/v1/facts", headers=ka, json={
            "user_id": "alpha-person", "agent_id": "alpha-bot",
            "subject": "alpha-person", "predicate": "likes", "object": "tea"})
        return ka, kb

    def test_beta_does_not_read_alphas_end_users(self, db, tenant, client):
        ka, kb = self._two_projects(db, tenant, client)
        mine = [u["user_id"] for u in client.get("/v1/users", headers=ka).json()["users"]]
        theirs = [u["user_id"] for u in client.get("/v1/users", headers=kb).json()["users"]]
        assert "alpha-person" in mine
        assert "alpha-person" not in theirs, (
            "a key confined to Beta read back Alpha's end user ids")

    def test_beta_does_not_read_alphas_agents(self, db, tenant, client):
        ka, kb = self._two_projects(db, tenant, client)
        mine = [a["agent_id"] for a in client.get("/v1/agents", headers=ka).json()["agents"]]
        theirs = [a["agent_id"] for a in client.get("/v1/agents", headers=kb).json()["agents"]]
        assert "alpha-bot" in mine
        assert "alpha-bot" not in theirs, (
            "a key confined to Beta read back Alpha's agent names")

    def test_deleting_someone_elses_agent_does_not_confirm_it_exists(
            self, db, tenant, client):
        ka, kb = self._two_projects(db, tenant, client)
        r = client.delete("/v1/agents/alpha-bot", headers=kb)
        assert r.status_code == 404, (
            "Beta got past the existence check on a name that only lives in "
            "Alpha, which both deletes nothing and confirms the name")


class TestAKeyIsNeverMintedDead:
    """`scopes: ["read"]` used to mint a key that authenticated and did nothing."""

    def test_an_unknown_scope_is_refused_and_the_real_ones_are_named(
            self, db, tenant, client):
        head = _key(db, tenant, admin=True)
        r = client.post("/admin/keys", headers=head,
                        json={"name": "guessed", "scopes": ["read"]})
        assert r.status_code == 422, r.text
        assert "memories:read" in r.text, "the error has to name the real scopes"

    def test_the_valid_scopes_are_written_down_somewhere_reachable(self, client):
        r = client.get("/admin/scopes")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()["scopes"]]
        assert {"memories:read", "memories:write", "admin"} <= set(names)


class TestANulByteIsARefusalNotACrash:
    """The only 500 in a battery that took 280k characters, emoji and Japanese."""

    def test_a_single_write(self, db, tenant, client):
        head = _key(db, tenant)
        r = client.post("/v1/memories", headers=head,
                        json={"content": "before\x00after", "user_id": "u"})
        assert r.status_code == 422, f"got {r.status_code}: {r.text[:200]}"
        assert "NUL" in r.text

    def test_and_inside_a_batch_before_the_worker_sees_it(self, db, tenant, client):
        head = _key(db, tenant)
        r = client.post("/v1/batch", headers=head, json={"memories": [
            {"content": "fine", "user_id": "u"},
            {"content": "bad\x00here", "user_id": "u"},
        ]})
        assert r.status_code == 422, f"got {r.status_code}: {r.text[:200]}"
        assert "memories[1]" in r.text, "it has to say which row"
