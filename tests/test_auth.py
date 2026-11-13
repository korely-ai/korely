"""Authentication and tenant isolation.

The product's whole proposition is that you run this on your own machine and
your data stays yours. A defect here is not a bug, it is the proposition
failing, so this file asks the unfriendly questions: can a key reach a tenant
that did not issue it, does a revoked key still work, does a read-only key
write, and does a malformed header produce a 500 with a stack trace in it.

None of this had a test before today.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentApiKey, Tenant
from korely_agent.services import keys as key_service



@pytest.fixture
def client(db, monkeypatch):
    """The real app, wired to the test session.

    The worker and the MCP mount are turned off: this file is about who is
    allowed through the door, and a background thread mining facts while the
    assertions run only adds noise.
    """
    monkeypatch.setenv("RUN_WORKER", "false")

    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def new_tenant(db, name="T"):
    t = Tenant(id=uuid.uuid4(), name=name)
    db.add(t)
    db.commit()
    return t


def issue(db, tenant, *, scopes=None, name=None):
    row, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name=name, scopes=scopes
    )
    return row, full


class TestTheDoor:
    def test_a_valid_key_gets_in_and_carries_its_own_tenant(self, db):
        t = new_tenant(db)
        _, full = issue(db, t)
        row = key_service.verify_api_key(db, full)
        assert row is not None
        assert row.tenant_id == t.id

    def test_an_unknown_key_is_refused(self, db):
        assert key_service.verify_api_key(db, "kor_live_nope") is None

    def test_an_empty_key_is_refused(self, db):
        assert key_service.verify_api_key(db, "") is None

    def test_a_revoked_key_is_refused(self, db):
        t = new_tenant(db)
        row, full = issue(db, t)
        assert key_service.verify_api_key(db, full) is not None
        assert key_service.revoke_api_key(db, key_id=row.id, tenant_id=t.id) is True
        assert key_service.verify_api_key(db, full) is None

    def test_revoking_twice_reports_that_it_did_nothing(self, db):
        t = new_tenant(db)
        row, _ = issue(db, t)
        assert key_service.revoke_api_key(db, key_id=row.id, tenant_id=t.id) is True
        assert key_service.revoke_api_key(db, key_id=row.id, tenant_id=t.id) is False

    def test_one_tenant_cannot_revoke_another_tenants_key(self, db):
        a, b = new_tenant(db, "A"), new_tenant(db, "B")
        row, full = issue(db, a)
        assert key_service.revoke_api_key(db, key_id=row.id, tenant_id=b.id) is False
        assert key_service.verify_api_key(db, full) is not None, "B revoked A's key"

    def test_the_plaintext_is_never_stored(self, db):
        t = new_tenant(db)
        row, full = issue(db, t)
        stored = db.query(AgentApiKey).filter(AgentApiKey.id == row.id).one()
        assert stored.key_hash != full
        assert full not in stored.key_hash
        # last_four is a display convenience and is the documented exception
        assert stored.last_four == full[-4:]

    def test_two_keys_are_never_the_same(self, db):
        t = new_tenant(db)
        seen = {issue(db, t, name=f"k{i}")[1] for i in range(20)}
        assert len(seen) == 20

    def test_last_used_at_is_stamped(self, db):
        t = new_tenant(db)
        row, full = issue(db, t)
        assert row.last_used_at is None
        key_service.verify_api_key(db, full)
        db.refresh(row)
        assert row.last_used_at is not None


class TestOverHttp:
    def test_no_header_is_401_not_500(self, client):
        r = client.get("/v1/memories")
        assert r.status_code == 401
        assert "stack" not in r.text.lower() and "traceback" not in r.text.lower()

    @pytest.mark.parametrize("header", [
        "",
        "Bearer",
        "Bearer ",
        "Basic kor_live_x",
        "kor_live_x",                      # no scheme
        "Bearer kor_live_" + "x" * 500,
    ])
    def test_a_malformed_header_is_401_not_500(self, client, header):
        r = client.get("/v1/memories", headers={"Authorization": header})
        assert r.status_code == 401, r.text

    def test_the_refusal_does_not_say_which_guess_was_close(self, client, db):
        """Same message for missing, malformed, unknown, revoked and expired.

        The property is sharper than "one message for every 401", and the
        difference is worth writing down because this test used to assert the
        blunt version and went red on a change that does not weaken it.

        What must never differ is anything that depends on the keys we STORE:
        an unknown key and a revoked one have to be indistinguishable, or the
        difference tells whoever is guessing which guesses were close.

        What may differ is what can be read off the presented string alone. A
        key beginning `kor_live_` was issued by the hosted service and will
        never work on an install of your own, and saying so hands back only what
        the caller already holds in their hand.
        """
        t = new_tenant(db)
        row, full = issue(db, t)
        key_service.revoke_api_key(db, key_id=row.id, tenant_id=t.id)
        _live, unknown_same_world = issue(db, new_tenant(db))
        made_up = "kor_self_" + "Q" * 43                      # well formed, never minted
        _gone, expired = key_service.create_api_key(db, tenant_id=t.id, name="gone",
                                                    expires_in_days=0)

        def detail(h):
            r = client.get("/v1/memories", headers=h)
            assert r.status_code == 401 and r.json()["code"] == "invalid_key"
            return r.json()["detail"]

        # Since 2026-10-07 the 401 says what is wrong, from the presented
        # string alone (the hosted product's wording): no key, no shape of a
        # key, no live key. The last one is the same for unknown, revoked and
        # expired.
        stored = {detail({"Authorization": f"Bearer {k}"}) for k in (made_up, full, expired)}
        assert len(stored) == 1, (
            f"an unknown key, a revoked one and an expired one differ, so the "
            f"refusal says which keys exist: {stored}")
        one = stored.pop()
        # The words of every door (keys.NOT_LIVE), which name the three: up to
        # 1.0.9 they named revocation alone, also to an expired key.
        assert key_service.NOT_LIVE in one and one.endswith("/keys"), one
        malformed = {detail({"Authorization": "Bearer " + k})
                     for k in (unknown_same_world + "x", "kor_self_nothing_like_it")}
        assert malformed == {"Invalid or missing API key: a key of this install is "
                             "kor_self_ followed by 43 characters"}
        assert "Authorization: Bearer kor_self_" in detail({})

    def test_a_key_from_the_other_world_is_told_so(self, client):
        """The one thing worth saying, and it costs nothing to say it."""
        r = client.get("/v1/memories",
                       headers={"Authorization": "Bearer kor_live_whatever"})
        assert r.status_code == 401
        detail = r.json()["detail"]
        assert "kor_live_" in detail and "kor_self_" in detail, detail

    def test_and_it_says_nothing_about_what_is_stored(self, client, db):
        """The wording is chosen after the lookup has failed, from the string
        alone. So a `kor_live_` key this install really minted (before 0.1.3)
        and then revoked answers exactly like one somebody made up: the words
        cannot be used to probe what is stored."""
        t = new_tenant(db)
        row, old = key_service.create_api_key(db, tenant_id=t.id, name="old",
                                              prefix=key_service.PREFIX_LIVE)
        key_service.revoke_api_key(db, key_id=row.id, tenant_id=t.id)
        made_up = "kor_live_" + "z" * 40
        also_made_up = "kor_live_" + "q" * 40
        answers = [client.get("/v1/memories", headers={"Authorization": "Bearer " + k})
                   for k in (old, made_up, also_made_up)]
        assert {r.status_code for r in answers} == {401}
        assert len({r.text for r in answers}) == 1, [r.text for r in answers]


class TestAKeyMintedHereBeforeKorSelf:
    """Before 0.1.3 this product minted `kor_live_` keys, the prefix the hosted
    service uses. services/keys.py promised they keep working: the check is on
    the hash, never on the prefix. From 0.1.4 REST refused every `kor_live_`
    key before looking it up, while MCP and /admin went on accepting them, so
    the same key worked through two doors and not the third, and
    scripts/acceptance_survives.sh, which upgrades an install made before the
    change and asks its key for /v1/ping, could not pass.

    The key's hash in this install decides, at every door. A hosted key is not
    in the table, so it is still refused everywhere, and REST still says why.
    """

    @pytest.fixture
    def old(self, db):
        t = new_tenant(db)
        row, full = key_service.create_api_key(
            db, tenant_id=t.id, name="from 0.1.2", prefix=key_service.PREFIX_LIVE,
            scopes=["memories:read", "memories:write", "admin"])
        assert full.startswith("kor_live_")
        return row, full, t

    @staticmethod
    def _mcp_accepts(key):
        import asyncio

        from korely_agent.mcp import server as mcp_server

        return asyncio.run(mcp_server.KorelyAgentKeyVerifier().verify_token(key)) is not None

    def test_since_0_2_0_it_is_refused_through_every_door(self, client, old):
        """0.2.0, as the CHANGELOG said from 0.1.41: `kor_live_` is the
        Cloud's alone, and the refusal says whose it is."""
        _row, full, _t = old
        h = {"Authorization": f"Bearer {full}"}
        for path in ("/v1/ping", "/v1/memories", "/admin/whoami"):
            r = client.get(path, headers=h)
            assert r.status_code == 401, (path, r.text)
        assert "kor_self_" in client.get("/v1/ping", headers=h).json()["detail"]
        assert not self._mcp_accepts(full)

    def test_revoked_it_is_refused_through_every_door(self, client, db, old):
        row, full, t = old
        key_service.revoke_api_key(db, key_id=row.id, tenant_id=t.id)
        h = {"Authorization": f"Bearer {full}"}
        assert client.get("/v1/ping", headers=h).status_code == 401
        assert client.get("/admin/whoami", headers=h).status_code == 401
        assert not self._mcp_accepts(full)

    def test_a_hosted_key_is_refused_through_every_door(self, client):
        hosted = "kor_live_" + "h" * 43
        h = {"Authorization": f"Bearer {hosted}"}
        for path in ("/v1/ping", "/admin/whoami"):
            r = client.get(path, headers=h)
            assert r.status_code == 401
            assert "kor_self_" in r.json()["detail"], (path, r.text)
        assert not self._mcp_accepts(hosted)

    def test_no_key_is_told_the_prefix_this_install_mints(self, client, db, monkeypatch):
        """kor_self_ on an install of one's own, kor_live_ on the hosted
        service: the hint names a key the caller can have (until 0.1.28 the
        hosted service named kor_self_ too)."""
        assert client.get("/v1/ping").json()["detail"].endswith("Authorization: Bearer kor_self_...")

    @pytest.mark.cloud
    def test_no_key_on_the_cloud_is_told_the_prefix_it_mints(self, db, monkeypatch):
        from tests.hosted_helpers import hosted_client

        with hosted_client(db, monkeypatch) as c:
            r = c.get("/v1/ping")
            assert r.status_code == 401
            assert r.json()["detail"].endswith("Authorization: Bearer kor_live_..."), r.text

    def test_a_read_only_key_cannot_write(self, client, db):
        t = new_tenant(db)
        _, full = issue(db, t, scopes=["memories:read"])
        h = {"Authorization": f"Bearer {full}"}

        assert client.get("/v1/memories", headers=h).status_code == 200
        r = client.post("/v1/memories", headers=h,
                        json={"content": "x", "user_id": "u1"})
        assert r.status_code == 403, r.text
        assert "memories:write" in r.text

    def test_a_key_with_no_scopes_can_do_nothing(self, client, db):
        t = new_tenant(db)
        _, full = issue(db, t, scopes=[])
        h = {"Authorization": f"Bearer {full}"}
        assert client.get("/v1/memories", headers=h).status_code == 403


class TestIsolation:
    """The claim that sells the product, asked as a question."""

    def test_a_key_cannot_read_another_tenants_memories(self, client, db):
        a, b = new_tenant(db, "A"), new_tenant(db, "B")
        _, ka = issue(db, a)
        _, kb = issue(db, b)

        r = client.post("/v1/memories", headers={"Authorization": f"Bearer {ka}"},
                        json={"content": "A's secret", "user_id": "u1"})
        assert r.status_code in (200, 201), r.text
        mem_id = r.json()["id"]

        hb = {"Authorization": f"Bearer {kb}"}
        listed = client.get("/v1/memories", headers=hb, params={"user_id": "u1"})
        assert listed.status_code == 200
        ids = [m["id"] for m in listed.json().get("memories", [])]
        assert mem_id not in ids, "B listed A's memory"

        assert client.get(f"/v1/memories/{mem_id}", headers=hb).status_code == 404

    def test_a_key_cannot_delete_another_tenants_memory(self, client, db):
        a, b = new_tenant(db, "A"), new_tenant(db, "B")
        _, ka = issue(db, a)
        _, kb = issue(db, b)

        r = client.post("/v1/memories", headers={"Authorization": f"Bearer {ka}"},
                        json={"content": "A's secret", "user_id": "u1"})
        mem_id = r.json()["id"]

        d = client.delete(f"/v1/memories/{mem_id}",
                          headers={"Authorization": f"Bearer {kb}"})
        assert d.status_code == 404, d.text

        still = client.get(f"/v1/memories/{mem_id}",
                           headers={"Authorization": f"Bearer {ka}"})
        assert still.status_code == 200, "B deleted A's memory"

    def test_the_tenant_cannot_be_named_in_the_request(self, client, db):
        """The tenant comes from the key. Sending one must not change anything."""
        a, b = new_tenant(db, "A"), new_tenant(db, "B")
        _, ka = issue(db, a)

        r = client.post("/v1/memories", headers={"Authorization": f"Bearer {ka}"},
                        json={"content": "x", "user_id": "u1",
                              "tenant_id": str(b.id)})
        assert r.status_code in (200, 201, 422), r.text
        if r.status_code in (200, 201):
            from korely_agent.db.models import AgentMemory
            from korely_agent.services.ids import parse_mem_id

            row = db.query(AgentMemory).filter(
                AgentMemory.id == parse_mem_id(r.json()["id"])).one()
            assert row.tenant_id == a.id, "a request field moved the tenant"


class TestKeyCreation:
    def test_a_duplicate_name_is_a_duplicate_name(self, db):
        t = new_tenant(db)
        issue(db, t, name="ci")
        with pytest.raises(key_service.DuplicateKeyNameError):
            issue(db, t, name="ci")

    def test_the_same_name_under_two_tenants_is_fine(self, db):
        a, b = new_tenant(db, "A"), new_tenant(db, "B")
        issue(db, a, name="ci")
        issue(db, b, name="ci")     # must not raise

    def test_an_unnamed_key_is_not_a_duplicate_of_another_unnamed_one(self, db):
        t = new_tenant(db)
        issue(db, t)
        issue(db, t)                # NULL names are distinct

    def test_a_bad_prefix_is_refused(self):
        with pytest.raises(ValueError):
            key_service.generate_api_key("sk_live_")

    def test_creating_a_key_for_a_tenant_that_does_not_exist(self, db):
        """This must not be reported as a duplicate name.

        `create_api_key` catches IntegrityError and raises DuplicateKeyNameError,
        but a missing tenant violates the foreign key and raises the same
        IntegrityError. The router turns that into 409 "a key with that name
        already exists", which is a false statement about a name nobody used,
        sent in response to a completely different mistake.
        """
        with pytest.raises(Exception) as e:
            key_service.create_api_key(db, tenant_id=uuid.uuid4(), name="x")
        assert not isinstance(e.value, key_service.DuplicateKeyNameError), (
            "a missing tenant is reported as a duplicate key name"
        )
