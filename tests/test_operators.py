"""Logging in.

The first idea was "for now even just a username and a password". The
substance is right and one detail is not: there is no default password here.
`admin/admin` is how a self-hosted product ends up indexed, and every install
that ships with one has somebody who meant to change it and did not. So the
password is generated, shown once, and stored as a scrypt hash.

The rest of this file is the things a login box has to survive: somebody
pointing a script at it, somebody keeping a token after being told to go away,
and somebody in one account trying to see another.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from korely_agent.db.models import OperatorSession, Tenant
from korely_agent.services import keys as key_service
from korely_agent.services import operators as ops
from korely_agent.services.integrity import violated



@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def anna(db, tenant):
    row, password = ops.create(db, tenant_id=tenant.id, username="anna")
    return row, password


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class TestThereIsNoDefaultPassword:
    def test_no_password_is_written_into_the_code(self):
        """Read as a syntax tree, not as text.

        The first version of this grepped for the string "admin/admin" and
        failed on the three docstrings that say there is no admin/admin. A
        check that a comment can fool is a check that will one day pass while
        the thing it guards is false. This looks for an assignment whose name
        is about a password and whose value is a literal string, which is what
        a convenience default actually looks like when somebody adds one during
        a demo.
        """
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "korely_agent"
        offenders = []
        for f in sorted(root.rglob("*.py")):
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                    value = node.value
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    targets = [node.target.id]
                    value = node.value
                else:
                    continue
                if not targets or not isinstance(value, ast.Constant):
                    continue
                if not isinstance(value.value, str) or not value.value:
                    continue
                if any("password" in t.lower() or "passwd" in t.lower() for t in targets):
                    offenders.append(f"{f.name}:{node.lineno} {targets} = {value.value!r}")
        assert offenders == [], f"a password is written into the source: {offenders}"

    def test_a_generated_password_is_long_and_not_guessable(self):
        a, b = ops.generate_password(), ops.generate_password()
        assert len(a) >= 20
        assert a != b

    def test_the_password_is_never_stored(self, db, tenant):
        row, password = ops.create(db, tenant_id=tenant.id, username="anna")
        assert password not in row.password_hash
        assert row.password_hash.startswith("scrypt$"), \
            "a password digested like an API key is a lookup table away from plaintext"

    def test_a_corrupt_hash_is_a_no_not_a_crash(self):
        assert ops.verify_password("x", "") is False
        assert ops.verify_password("x", "nonsense") is False
        assert ops.verify_password("x", "scrypt$a$b$c$d$e") is False


class TestLoggingIn:
    def test_the_happy_path(self, client, anna):
        _row, password = anna
        r = client.post("/admin/session", json={"username": "anna", "password": password})
        assert r.status_code == 200
        body = r.json()
        assert body["token"].startswith("kor_sess_")
        assert body["username"] == "anna"

    def test_the_token_administers(self, client, anna):
        _row, password = anna
        token = client.post("/admin/session",
                            json={"username": "anna", "password": password}).json()["token"]
        assert client.get("/admin/keys", headers=bearer(token)).status_code == 200
        who = client.get("/admin/whoami", headers=bearer(token)).json()
        assert who == {"kind": "operator", "name": "anna", "tenant_id": who["tenant_id"]}

    def test_a_wrong_password_and_an_unknown_name_answer_the_same(self, client, anna):
        """Two different answers here is a way of finding out which usernames
        exist."""
        a = client.post("/admin/session", json={"username": "anna", "password": "wrong"})
        b = client.post("/admin/session", json={"username": "nobody", "password": "wrong"})
        assert a.status_code == b.status_code == 401
        assert a.json() == b.json()

    def test_a_disabled_operator_cannot_log_in(self, client, db, anna):
        row, password = anna
        row.disabled_at = datetime.now(timezone.utc)
        db.commit()
        assert client.post("/admin/session",
                           json={"username": "anna", "password": password}).status_code == 401

    def test_ten_wrong_answers_lock_the_account(self, client, db, anna):
        _row, password = anna
        for _ in range(10):
            client.post("/admin/session", json={"username": "anna", "password": "wrong"})

        r = client.post("/admin/session", json={"username": "anna", "password": password})
        assert r.status_code == 429, "the right password still worked after ten wrong ones"
        assert "Too many attempts" in r.json()["detail"]

    def test_the_lock_is_a_row_so_a_restart_does_not_clear_it(self, db, anna):
        """A lockout you can clear by waiting for a deploy is not a lockout."""
        from korely_agent.db.models import LoginAttempt

        for _ in range(10):
            ops.login(db, username="anna", password="wrong", ip="198.51.100.7")
        # A fresh session object reading the same row, which is what a restarted
        # process would see.
        db.expire_all()
        row = db.get(LoginAttempt, ("198.51.100.7", "anna"))
        assert row is not None and row.locked_until is not None
        with pytest.raises(ops.LockedOut):
            ops.login(db, username="anna", password=anna[1], ip="198.51.100.7")

    def test_a_good_login_clears_the_counter(self, db, anna):
        from korely_agent.db.models import LoginAttempt

        _row, password = anna
        for _ in range(3):
            ops.login(db, username="anna", password="wrong", ip="198.51.100.7")
        assert db.get(LoginAttempt, ("198.51.100.7", "anna")).failures == 3
        assert ops.login(db, username="anna", password=password, ip="198.51.100.7") is not None
        db.expire_all()
        assert db.get(LoginAttempt, ("198.51.100.7", "anna")) is None


class TestTheLoginCannotBeGroundOrUsedToLockSomebodyOut:
    """The audit of 2026-10-06, row 7: every attempt costs a scrypt of 16 MiB
    and nothing bounded them, and ten wrong passwords locked the operator out
    from everywhere for whoever knew the name."""

    def test_a_lock_from_one_address_does_not_lock_the_operator_out(self, db, anna):
        _row, password = anna
        for _ in range(10):
            ops.login(db, username="anna", password="wrong", ip="198.51.100.7")
        with pytest.raises(ops.LockedOut):
            ops.login(db, username="anna", password=password, ip="198.51.100.7")
        assert ops.login(db, username="anna", password=password, ip="203.0.113.9") is not None

    def test_an_address_that_tries_many_names_is_refused_before_any_hash(self, db, anna,
                                                                         monkeypatch):
        for i in range(30):
            ops.login(db, username=f"guess{i}", password="wrong", ip="198.51.100.7")
        hashed = []
        monkeypatch.setattr(ops, "hash_password", lambda p: hashed.append(p) or "x")
        monkeypatch.setattr(ops, "verify_password", lambda p, h: hashed.append(p) or False)
        with pytest.raises(ops.LockedOut):
            ops.login(db, username="anna", password=anna[1], ip="198.51.100.7")
        assert hashed == [], "the refused address still cost a hash"

    def test_an_ipv6_network_counts_as_one_address(self, db, anna, monkeypatch):
        """An IPv6 caller holds a whole /64 (rate_limit.network_of): a fresh
        address of it for every attempt started both counts at zero, so
        neither the pair lock nor the address's ceiling ever fired (audit
        of 19 Oct 2026)."""
        _row, password = anna
        for i in range(10):
            ops.login(db, username="anna", password="wrong", ip=f"2001:db8:aa:bb::{i + 1:x}")
        with pytest.raises(ops.LockedOut):
            ops.login(db, username="anna", password=password, ip="2001:db8:aa:bb::ffff")
        # Another /64 is another network.
        assert ops.login(db, username="anna", password=password, ip="2001:db8:aa:cc::1") is not None
        for i in range(30):
            ops.login(db, username=f"guess{i}", password="wrong", ip=f"2001:db8:dd:ee::{i + 1:x}")
        hashed = []
        monkeypatch.setattr(ops, "hash_password", lambda p: hashed.append(p) or "x")
        monkeypatch.setattr(ops, "verify_password", lambda p, h: hashed.append(p) or False)
        with pytest.raises(ops.LockedOut):
            ops.login(db, username="anna", password=password, ip="2001:db8:dd:ee::abcd")
        assert hashed == [], "the refused network still cost a hash"

    def test_hashes_at_once_have_a_ceiling(self, db, anna, monkeypatch):
        from korely_agent.core import threads

        monkeypatch.setattr(ops, "_hashing", threads.Places(1, refusal=lambda n: ops.TooBusy()))
        with ops._hashing.held(ops._HASHING):
            with pytest.raises(ops.TooBusy):
                ops.login(db, username="anna", password=anna[1], ip="198.51.100.7")

    def test_the_reply_says_when_to_come_back(self, client, db, anna):
        for _ in range(10):
            client.post("/admin/session", json={"username": "anna", "password": "wrong"})
        r = client.post("/admin/session", json={"username": "anna", "password": anna[1]})
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0

    def test_a_new_password_lifts_the_locks(self, db, anna):
        for _ in range(10):
            ops.login(db, username="anna", password="wrong", ip="198.51.100.7")
        ops.set_password(db, ops.by_username(db, "anna"), "a new long password 42")
        assert ops.login(db, username="anna", password="a new long password 42",
                         ip="198.51.100.7") is not None

    def test_old_attempts_are_purged(self, db, anna):
        from korely_agent.db.models import LoginAttempt

        ops.login(db, username="anna", password="wrong", ip="198.51.100.7")
        db.query(LoginAttempt).update({"window_start": datetime.now(timezone.utc) - timedelta(days=2)})
        db.commit()
        ops.purge_expired(db)
        assert db.query(LoginAttempt).count() == 0


class TestSessions:
    def test_an_expired_session_stops_working(self, client, db, anna):
        _row, password = anna
        token = client.post("/admin/session",
                            json={"username": "anna", "password": password}).json()["token"]
        assert client.get("/admin/keys", headers=bearer(token)).status_code == 200

        row = db.query(OperatorSession).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        assert client.get("/admin/keys", headers=bearer(token)).status_code == 401
        assert db.query(OperatorSession).count() == 0, \
            "the expired row is still holding a token hash"

    def test_a_live_session_is_resolved_without_a_write(self, db, anna):
        """Audit of 17 Oct 2026: every request of the dashboard stamped
        `last_seen_at` with a commit, and nothing read it. The column is gone,
        and resolving a session commits nothing."""
        from sqlalchemy import event

        op, _password = anna
        _session, token = ops.open_session(db, op)
        commits = []

        def committed(_session):
            commits.append(1)

        event.listen(db, "after_commit", committed)
        try:
            assert ops.resolve(db, token).id == op.id
        finally:
            event.remove(db, "after_commit", committed)
        assert commits == []
        assert "last_seen_at" not in OperatorSession.__table__.c
        assert "deleted_at" not in Tenant.__table__.c, "no soft delete anybody honours"

    def test_logging_out_takes_the_token_back_at_once(self, client, anna):
        _row, password = anna
        token = client.post("/admin/session",
                            json={"username": "anna", "password": password}).json()["token"]
        assert client.delete("/admin/session", headers=bearer(token)).status_code == 200
        assert client.get("/admin/keys", headers=bearer(token)).status_code == 401

    def test_resetting_a_password_ends_every_session(self, client, db, anna, monkeypatch, capsys):
        """Resetting because it was shared is pointless if what it opened stays
        open. Through the command, `manage reset-password`, the documented way
        out of a shared or stolen password: this test called set_password and
        logout_everywhere itself, so the command losing its logout left the
        suite green and a stolen session valid for its 12 hours (audit of 17
        Oct 2026)."""
        from korely_agent import manage

        _row, password = anna
        token = client.post("/admin/session",
                            json={"username": "anna", "password": password}).json()["token"]
        assert db.query(OperatorSession).count() == 1
        monkeypatch.setattr(manage, "_session", lambda: db)
        assert manage.reset_password("anna") == 0
        assert "1 open session(s) ended" in capsys.readouterr().out
        assert db.query(OperatorSession).count() == 0
        assert client.get("/admin/keys", headers=bearer(token)).status_code == 401
        assert client.post("/admin/session", json={"username": "anna",
                                                   "password": password}).status_code == 401

    def test_signing_out_of_all_devices_ends_every_session(self, client, anna):
        """The hosted product's "Sign out of all devices": the laptop left
        signed in elsewhere stops working at once, and so does this tab."""
        _row, password = anna
        login = {"username": "anna", "password": password}
        here = client.post("/admin/session", json=login).json()["token"]
        laptop = client.post("/admin/session", json=login).json()["token"]
        r = client.delete("/admin/sessions", headers=bearer(here))
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert client.get("/admin/keys", headers=bearer(laptop)).status_code == 401
        assert client.get("/admin/keys", headers=bearer(here)).status_code == 401

    def test_signing_out_everywhere_is_filed_as_a_logout(self, client, db, anna):
        """Audit of 17 Oct 2026: it was filed as `key_revoke` of "sessions",
        and the trail showed a key revoked where none was."""
        from korely_agent.db.models import AuditEvent

        _row, password = anna
        here = client.post("/admin/session",
                           json={"username": "anna", "password": password}).json()["token"]
        assert client.delete("/admin/sessions", headers=bearer(here)).status_code == 200
        out = db.query(AuditEvent).filter(AuditEvent.action == "logout").one()
        assert out.target_id == "anna" and out.meta["ended"] == 1
        assert db.query(AuditEvent).filter(AuditEvent.action == "key_revoke").count() == 0

    def test_signing_out_is_filed_as_a_logout(self, client, db, anna):
        """The trail had every sign-in and none of the sign-outs: only
        "Sign out of all devices" wrote one (functional test of 28 Oct 2026).
        A token that ends no session writes nothing."""
        from korely_agent.db.models import AuditEvent

        _row, password = anna
        here = client.post("/admin/session",
                           json={"username": "anna", "password": password}).json()["token"]
        r = client.delete("/admin/session", headers=bearer(here))
        assert r.status_code == 200 and r.json()["deleted"] is True
        out = db.query(AuditEvent).filter(AuditEvent.action == "logout").one()
        assert out.target_id == "anna" and out.actor == "dashboard"
        assert out.meta["what"] == "operator_session" and out.meta["ended"] == 1
        assert client.delete("/admin/session", headers=bearer(here)).json()["deleted"] is False
        assert db.query(AuditEvent).filter(AuditEvent.action == "logout").count() == 1

    def test_a_key_has_no_sessions_to_end(self, client, db, tenant):
        from korely_agent.services import keys as key_service

        _r, full = key_service.create_api_key(
            db, tenant_id=tenant.id, name="ops",
            scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE],
            expires_in_days=None)
        assert client.delete("/admin/sessions", headers=bearer(full)).status_code == 409

    def test_a_session_token_is_not_an_api_key(self, client, anna):
        """It administers, it does not carry the memory API."""
        _row, password = anna
        token = client.post("/admin/session",
                            json={"username": "anna", "password": password}).json()["token"]
        assert client.get("/v1/facts", headers=bearer(token)).status_code == 401

    def test_rubbish_tokens(self, client):
        for bad in ("kor_sess_nonsense", "kor_live_nonsense", "", "Bearer"):
            assert client.get("/admin/keys", headers=bearer(bad)).status_code in (401, 403)

    def test_expired_sessions_are_purged(self, db, anna):
        row, password = anna
        ops.login(db, username="anna", password=password)
        s = db.query(OperatorSession).one()
        s.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
        assert ops.purge_expired(db) == 1


class TestIsolation:
    def test_an_operator_sees_only_their_own_account(self, client, db, tenant, anna):
        other = Tenant(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()
        _r, their_key = key_service.create_api_key(db, tenant_id=other.id, name="theirs")

        _row, password = anna
        token = client.post("/admin/session",
                            json={"username": "anna", "password": password}).json()["token"]
        names = [k["name"] for k in client.get("/admin/keys", headers=bearer(token)).json()]
        assert "theirs" not in names

    def test_usernames_are_unique_across_the_install(self, db, tenant):
        """Somebody typing a username at a login box has not said which account
        they are in, and asking them would be asking them to know something
        they have no way of knowing."""
        other = Tenant(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()
        ops.create(db, tenant_id=tenant.id, username="anna")
        # The unique constraint on the username, and only that one. A bare
        # `Exception` also passed on a foreign key failure, or on a typo in
        # the call, which is a test that cannot tell the rule is gone.
        with pytest.raises(IntegrityError) as refused:
            ops.create(db, tenant_id=other.id, username="anna")
        db.rollback()
        from sqlalchemy import inspect

        unique_on_username = [
            u["name"] for u in inspect(db.get_bind()).get_unique_constraints("operators")
            if u["column_names"] == ["username"]]
        assert unique_on_username, "no unique constraint on operators.username"
        assert violated(refused.value, *unique_on_username)


class TestTheProxyHeader:
    """The answer to "do you support SSO" for a service with no screen of its
    own: the customer's gateway does the SAML and passes the result down."""

    def test_it_is_off_unless_configured(self, client, db, anna):
        r = client.get("/admin/keys", headers={"X-Forwarded-User": "anna"})
        assert r.status_code == 401, "a header nobody configured authenticated somebody"

    def test_when_configured_the_header_logs_you_in(self, db, anna, monkeypatch):
        from korely_agent import config

        monkeypatch.setenv("TRUST_AUTH_HEADER", "X-Forwarded-User")
        config.get_settings.cache_clear()
        try:
            from korely_agent.db import get_db
            from korely_agent.main import create_app

            app = create_app()
            app.dependency_overrides[get_db] = lambda: db
            with TestClient(app) as c:
                assert c.get("/admin/keys",
                             headers={"X-Forwarded-User": "anna"}).status_code == 200
                who = c.get("/admin/whoami",
                            headers={"X-Forwarded-User": "anna"}).json()
                assert who["name"] == "anna"
            app.dependency_overrides.clear()
        finally:
            config.get_settings.cache_clear()

    @pytest.mark.parametrize("came_from,expected", [
        ({"Sec-Fetch-Site": "cross-site", "Origin": "https://attacker.invalid"}, 403),
        ({"Sec-Fetch-Site": "same-site"}, 403),
        ({"Origin": "https://attacker.invalid"}, 403),
        ({"Origin": "null"}, 403),
        ({"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}, 404),
        ({}, 404),  # a script, not a page of another site
    ])
    def test_a_change_under_the_header_must_come_from_this_site(self, db, anna, monkeypatch,
                                                                came_from, expected):
        """A form on another site revoked a key: the gateway's cookie rode
        along and the header named the operator (audit of 17 Oct 2026). 404
        is this site's own answer (no such key), 403 the cross-site refusal."""
        from korely_agent import config

        monkeypatch.setenv("TRUST_AUTH_HEADER", "X-Forwarded-User")
        config.get_settings.cache_clear()
        try:
            from korely_agent.db import get_db
            from korely_agent.main import create_app

            app = create_app()
            app.dependency_overrides[get_db] = lambda: db
            with TestClient(app) as c:
                r = c.post("/admin/keys/00000000-0000-4000-8000-000000000000/revoke",
                           headers={"X-Forwarded-User": "anna", **came_from})
                assert r.status_code == expected, r.text
                # Reading stays open: a cross-site page cannot read the answer.
                assert c.get("/admin/keys", headers={"X-Forwarded-User": "anna",
                                                     "Sec-Fetch-Site": "cross-site"}).status_code == 200
            app.dependency_overrides.clear()
        finally:
            config.get_settings.cache_clear()

    def test_an_unknown_name_in_the_header_is_still_refused(self, db, anna, monkeypatch):
        from korely_agent import config

        monkeypatch.setenv("TRUST_AUTH_HEADER", "X-Forwarded-User")
        config.get_settings.cache_clear()
        try:
            from korely_agent.db import get_db
            from korely_agent.main import create_app

            app = create_app()
            app.dependency_overrides[get_db] = lambda: db
            with TestClient(app) as c:
                assert c.get("/admin/keys",
                             headers={"X-Forwarded-User": "nobody"}).status_code == 401
            app.dependency_overrides.clear()
        finally:
            config.get_settings.cache_clear()
