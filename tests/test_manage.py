"""The operator's commands.

Provisioning happens here and nowhere else, and keys and webhook endpoints can
be handled here for any account, from a shell, where the dashboard handles the
one you signed in to. It is also where a client's first hour is spent, which
is why a mistyped id has to produce a sentence rather than a traceback.
"""
import uuid

import pytest

from korely_agent import manage
from korely_agent.db.models import AgentWebhookEndpoint, Tenant



@pytest.fixture(autouse=True)
def wired(db, monkeypatch):
    """Point the commands at the test session."""
    monkeypatch.setattr(manage, "_session", lambda: db)
    monkeypatch.setattr(manage.webhook_service, "check_url", lambda u: u.strip())
    yield


class TestProvisioning:
    def test_create_tenant_prints_a_key_once(self, db, capsys):
        assert manage.main(["create-tenant", "--name", "Acme"]) == 0
        out = capsys.readouterr().out
        # `kor_self_`, not `kor_live_`: a key minted here must be
        # distinguishable at a glance from one issued by the hosted service, so
        # that putting it in a client with no address fails visibly instead of
        # arriving at api.korely.ai and being refused after the fact.
        assert "kor_self_" in out
        assert "kor_live_" not in out
        assert "shown once" in out
        assert db.query(Tenant).filter(Tenant.name == "Acme").one()

    def test_issue_key_for_an_unknown_tenant_says_so(self, capsys):
        assert manage.main(["issue-key", "--tenant", str(uuid.uuid4())]) == 1
        assert "no tenant" in capsys.readouterr().err

    def test_a_mistyped_id_is_a_sentence_not_a_traceback(self, capsys):
        assert manage.main(["issue-key", "--tenant", "not-a-uuid"]) == 1
        err = capsys.readouterr().err
        assert "not a valid tenant id" in err
        assert "Traceback" not in err


class TestTheListings:
    """`list-tenants` and `list-operators`, which no test ran (audit of 17
    Oct 2026): where an operator finds the ids every other command asks for."""

    def test_no_tenants_says_how_to_make_one(self, capsys):
        assert manage.main(["list-tenants"]) == 0
        assert "create-tenant" in capsys.readouterr().out

    def test_each_tenant_with_its_id_and_name_oldest_first(self, db, capsys):
        from datetime import datetime, timezone

        older = Tenant(id=uuid.uuid4(), name="Older",
                       created_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
        newer = Tenant(id=uuid.uuid4(), name="Newer",
                       created_at=datetime(2026, 3, 4, tzinfo=timezone.utc))
        db.add_all([newer, older])
        db.commit()
        assert manage.main(["list-tenants"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines == [f"{older.id}  2026-01-02  Older", f"{newer.id}  2026-03-04  Newer"]

    def test_no_operators_says_how_to_make_one(self, capsys):
        assert manage.main(["list-operators"]) == 0
        assert "create-operator" in capsys.readouterr().out

    def test_each_operator_with_state_last_login_and_lock(self, db, tenant, capsys):
        from datetime import datetime, timedelta, timezone

        from korely_agent.db.models import LoginAttempt
        from korely_agent.services import operators as ops

        anna, _ = ops.create(db, tenant_id=tenant.id, username="anna")
        bruno, _ = ops.create(db, tenant_id=tenant.id, username="bruno")
        anna.last_login_at = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)
        bruno.disabled_at = datetime.now(timezone.utc)
        db.add(LoginAttempt(ip="203.0.113.7", username="anna", failures=10,
                            locked_until=datetime.now(timezone.utc) + timedelta(minutes=15)))
        db.commit()
        assert manage.main(["list-operators"]) == 0
        lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
        assert set(lines) == {"anna", "bruno"}
        assert "active" in lines["anna"] and "last login 2026-10-01 09:30" in lines["anna"]
        assert "LOCKED from 1 address until" in lines["anna"] and str(tenant.id) in lines["anna"]
        assert "disabled" in lines["bruno"] and "last login never" in lines["bruno"]
        assert "LOCKED" not in lines["bruno"]

    def test_operators_of_one_tenant(self, db, tenant, capsys):
        from korely_agent.services import operators as ops

        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        ops.create(db, tenant_id=tenant.id, username="anna")
        ops.create(db, tenant_id=other.id, username="otto")
        assert manage.main(["list-operators", "--tenant", str(other.id)]) == 0
        assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == ["otto"]

    def test_operators_of_a_mistyped_tenant_is_a_sentence(self, capsys):
        assert manage.main(["list-operators", "--tenant", "not-a-uuid"]) == 1
        err = capsys.readouterr().err
        assert "not a valid tenant id" in err and "Traceback" not in err


class TestTheAnswersAProgramReads:
    """`--json` (29 Oct 2026): install.sh makes the account and the login and
    prints them once at the end, so it reads these commands' answers. One JSON
    value on stdout and nothing else, the same values the text shows, and the
    refusals still sentences on stderr."""

    @staticmethod
    def _one_json(capsys):
        import json

        out = capsys.readouterr().out
        assert len(out.splitlines()) == 1, f"more than the JSON on stdout: {out!r}"
        return json.loads(out)

    def test_create_tenant_answers_the_tenant_and_a_key_that_works(self, db, capsys):
        from korely_agent.services import keys as key_service

        assert manage.main(["create-tenant", "--name", "Acme", "--json"]) == 0
        got = self._one_json(capsys)
        assert set(got) == {"tenant", "name", "key", "expires"}
        t = db.query(Tenant).filter(Tenant.name == "Acme").one()
        assert got["tenant"] == str(t.id) and got["name"] == "Acme"
        assert got["key"].startswith("kor_self_")
        row = key_service.verify_api_key(db, got["key"])
        assert row is not None and row.tenant_id == t.id
        assert got["expires"] == f"{row.expires_at:%Y-%m-%d}"

    def test_a_key_that_never_expires_says_null(self, capsys):
        assert manage.main(["create-tenant", "--name", "Acme", "--expires-in", "never",
                            "--json"]) == 0
        assert self._one_json(capsys)["expires"] is None

    def test_create_operator_answers_a_password_that_logs_in(self, db, tenant, capsys):
        from korely_agent.services import operators as ops

        assert manage.main(["create-operator", "--tenant", str(tenant.id),
                            "--username", "Anna", "--json"]) == 0
        got = self._one_json(capsys)
        assert got == {"username": "anna", "password": got["password"],
                       "tenant": str(tenant.id), "install_admin": True}
        op = ops.by_username(db, "anna")
        assert ops.verify_password(got["password"], op.password_hash)

    def test_issue_key_answers_the_key(self, db, tenant, capsys):
        from korely_agent.services import keys as key_service

        assert manage.main(["issue-key", "--tenant", str(tenant.id), "--name", "laptop",
                            "--json"]) == 0
        got = self._one_json(capsys)
        assert got["tenant"] == str(tenant.id) and got["name"] == "laptop"
        assert got["admin"] is False
        assert key_service.verify_api_key(db, got["key"]).tenant_id == tenant.id

    def test_the_listings_are_lists_empty_or_not(self, db, capsys):
        from korely_agent.services import operators as ops

        assert manage.main(["list-tenants", "--json"]) == 0
        assert self._one_json(capsys) == []
        assert manage.main(["list-operators", "--json"]) == 0
        assert self._one_json(capsys) == []
        t = Tenant(id=uuid.uuid4(), name="Acme")
        db.add(t)
        db.commit()
        ops.create(db, tenant_id=t.id, username="anna")
        assert manage.main(["list-tenants", "--json"]) == 0
        [row] = self._one_json(capsys)
        assert row["id"] == str(t.id) and row["name"] == "Acme"
        assert manage.main(["list-operators", "--json"]) == 0
        assert self._one_json(capsys) == [{"username": "anna", "tenant": str(t.id),
                                           "active": True, "last_login": None}]

    def test_a_refusal_is_still_a_sentence_and_no_json(self, db, tenant, capsys):
        from korely_agent.services import operators as ops

        ops.create(db, tenant_id=tenant.id, username="anna")
        assert manage.main(["create-operator", "--tenant", str(tenant.id),
                            "--username", "anna", "--json"]) == 1
        got = capsys.readouterr()
        assert got.out == "" and "already an operator called 'anna'" in got.err
        assert manage.main(["issue-key", "--tenant", "not-a-uuid", "--json"]) == 1
        got = capsys.readouterr()
        assert got.out == "" and "not a valid tenant id" in got.err


class TestWebhookRegistration:
    """Until these existed there was no way to add a webhook endpoint at all:
    `emit_event` queried a table nothing could write to, so every event in the
    shipped product was emitted for nobody."""

    def test_add_and_list(self, db, tenant, capsys):
        rc = manage.main(["add-webhook", "--tenant", str(tenant.id),
                          "--url", "https://example.com/hook"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "whsec_" in out, "the secret must be printed, it is unrecoverable"
        assert db.query(AgentWebhookEndpoint).count() == 1

        assert manage.main(["list-webhooks"]) == 0
        assert "https://example.com/hook" in capsys.readouterr().out

    def test_the_events_actually_reach_it(self, db, tenant):
        from korely_agent.services import webhooks
        from korely_agent.db.models import AgentWebhookDelivery

        manage.main(["add-webhook", "--tenant", str(tenant.id),
                     "--url", "https://example.com/hook",
                     "--events", "memory.created"])
        assert webhooks.emit_event(db, tenant.id, "memory.created", {"id": "m"}) == 1
        assert db.query(AgentWebhookDelivery).count() == 1

    def test_an_unknown_event_name_is_refused_with_the_list(self, tenant, capsys):
        rc = manage.main(["add-webhook", "--tenant", str(tenant.id),
                          "--url", "https://example.com/h",
                          "--events", "memory.exploded"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "memory.exploded" in err
        assert "memory.created" in err, "say what the valid names are"

    def test_an_unsafe_url_is_refused_with_the_reason(self, tenant, capsys, monkeypatch):
        from korely_agent.services import webhooks

        def boom(u):
            raise webhooks.UnsafeWebhookUrl("resolves to 127.0.0.1")
        monkeypatch.setattr(manage.webhook_service, "check_url", boom)

        rc = manage.main(["add-webhook", "--tenant", str(tenant.id),
                          "--url", "http://localhost/h"])
        assert rc == 1
        assert "127.0.0.1" in capsys.readouterr().err

    def test_an_unknown_tenant_is_refused(self, capsys):
        rc = manage.main(["add-webhook", "--tenant", str(uuid.uuid4()),
                          "--url", "https://example.com/h"])
        assert rc == 1
        assert "no tenant" in capsys.readouterr().err

    def test_delete(self, db, tenant, capsys):
        manage.main(["add-webhook", "--tenant", str(tenant.id),
                     "--url", "https://example.com/h"])
        ep = db.query(AgentWebhookEndpoint).one()
        assert manage.main(["delete-webhook", "--tenant", str(tenant.id),
                            "--id", str(ep.id)]) == 0
        assert db.query(AgentWebhookEndpoint).count() == 0

    def test_one_tenant_cannot_delete_anothers_endpoint(self, db, tenant, capsys):
        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        manage.main(["add-webhook", "--tenant", str(tenant.id),
                     "--url", "https://example.com/h"])
        ep = db.query(AgentWebhookEndpoint).one()

        rc = manage.main(["delete-webhook", "--tenant", str(other.id),
                          "--id", str(ep.id)])
        assert rc == 1
        assert db.query(AgentWebhookEndpoint).count() == 1

    def test_listing_nothing_says_what_to_do(self, capsys):
        assert manage.main(["list-webhooks"]) == 0
        assert "add-webhook" in capsys.readouterr().out


class TestWebhookSecrets:
    """0.1.11: secrets follow the Standard Webhooks spec, and an endpoint made
    before keeps signing with its whole secret string as the key, so that its
    receiver does not break. Moving one of those to a spec secret is a
    command, not a delete and a re-add: the id, the URL, the events and any
    delivery still queued stay (tests/test_webhook_signatures.py)."""

    @staticmethod
    def _old(db, tenant, url="https://example.com/old"):
        import secrets

        ep = AgentWebhookEndpoint(tenant_id=tenant.id, url=url, events=["memory.created"],
                                  secret="whsec_" + secrets.token_urlsafe(24), active=True)
        db.add(ep)
        db.commit()
        return ep

    def test_add_says_how_the_key_is_made(self, tenant, capsys):
        manage.main(["add-webhook", "--tenant", str(tenant.id), "--url", "https://example.com/h"])
        out = capsys.readouterr().out
        assert "base64-decoded" in out and "standardwebhooks" in out

    def test_list_names_the_endpoints_on_the_old_key(self, db, tenant, capsys):
        old = self._old(db, tenant)
        manage.main(["add-webhook", "--tenant", str(tenant.id), "--url", "https://example.com/new"])
        capsys.readouterr()
        manage.main(["list-webhooks"])
        lines = capsys.readouterr().out.splitlines()
        flagged = [ln for ln in lines if "rotate-webhook-secret" in ln]
        assert len(flagged) == 1 and str(old.id) in flagged[0], lines

    def test_rotate_gives_a_spec_secret_and_keeps_the_endpoint(self, db, tenant, capsys):
        from korely_agent.services import webhooks

        old = self._old(db, tenant)
        eid, before = old.id, old.secret
        rc = manage.main(["rotate-webhook-secret", "--tenant", str(tenant.id),
                          "--id", str(eid)])
        assert rc == 0
        out = capsys.readouterr().out
        now = db.get(AgentWebhookEndpoint, eid)   # the command closed the session
        assert now.secret != before and now.secret in out, "printed once, like a new one"
        assert not webhooks.is_legacy_secret(now.secret)
        assert (now.url, now.events) == ("https://example.com/old", ["memory.created"])
        assert db.query(AgentWebhookEndpoint).count() == 1

    def test_rotating_another_accounts_endpoint_is_refused(self, db, tenant, capsys):
        old = self._old(db, tenant)
        eid, before = old.id, old.secret
        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        rc = manage.main(["rotate-webhook-secret", "--tenant", str(other.id),
                          "--id", str(eid)])
        assert rc == 1
        assert "no such endpoint" in capsys.readouterr().err
        assert db.get(AgentWebhookEndpoint, eid).secret == before


class TestKeyLifecycle:
    """`revoke_api_key` existed in the service layer from the first commit and
    nothing could call it. On a self-hosted box the only way to stop a leaked
    key was an UPDATE typed by hand, which is not something you ask of somebody
    at the moment they have just discovered a key in a public repository."""

    def _mint(self, db, tenant, name="prod"):
        from korely_agent.services import keys as key_service

        row, raw = key_service.create_api_key(db, tenant_id=tenant.id, name=name)
        db.commit()
        return row, raw

    def test_a_revoked_key_stops_authenticating(self, db, tenant, capsys):
        from korely_agent.services import keys as key_service

        row, raw = self._mint(db, tenant)
        assert key_service.verify_api_key(db, raw) is not None

        assert manage.main(["revoke-key", "--tenant", str(tenant.id), "--id", str(row.id)]) == 0
        db.commit()

        assert key_service.verify_api_key(db, raw) is None, \
            "the key still works after being revoked"

    def test_revoking_twice_is_an_error_not_a_silent_success(self, db, tenant, capsys):
        row, _raw = self._mint(db, tenant)
        assert manage.main(["revoke-key", "--tenant", str(tenant.id), "--id", str(row.id)]) == 0
        db.commit()
        capsys.readouterr()
        assert manage.main(["revoke-key", "--tenant", str(tenant.id), "--id", str(row.id)]) == 1
        assert "no live key" in capsys.readouterr().err

    def test_one_tenant_cannot_revoke_another_tenants_key(self, db, tenant, capsys):
        from korely_agent.db.models import Tenant as T
        from korely_agent.services import keys as key_service

        other = T(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()

        row, raw = self._mint(db, tenant)
        rc = manage.main(["revoke-key", "--tenant", str(other.id), "--id", str(row.id)])
        db.commit()

        assert rc == 1
        assert key_service.verify_api_key(db, raw) is not None, \
            "a key was revoked by a tenant that does not own it"

    def test_the_refusal_does_not_confirm_the_key_exists(self, db, tenant, capsys):
        """Not found, not owned and already revoked share one message. A wrong
        tenant id must not be a way to learn that a key id is real."""
        from korely_agent.db.models import Tenant as T

        other = T(id=uuid.uuid4(), name="Somebody Else")
        db.add(other)
        db.commit()
        row, _ = self._mint(db, tenant)

        manage.main(["revoke-key", "--tenant", str(other.id), "--id", str(row.id)])
        owned = capsys.readouterr().err
        manage.main(["revoke-key", "--tenant", str(other.id), "--id", str(uuid.uuid4())])
        invented = capsys.readouterr().err

        assert owned == invented

    def test_list_keys_shows_the_last_four_and_the_state(self, db, tenant, capsys):
        row, raw = self._mint(db, tenant, name="staging box")
        manage.main(["list-keys", "--tenant", str(tenant.id)])
        out = capsys.readouterr().out

        assert raw[-4:] in out, "no way to tell which key this is"
        assert raw not in out, "the whole key was printed back out"
        # A live key shows when it ends. It never prints the word "live": this
        # line used to assert that word and pass only because the prefix was
        # `kor_live_`, so it checked nothing about the state it was named after.
        # The prefix changed and the test went red, which is how it was found.
        assert "until " in out, out
        assert "revoked" not in out and "expired" not in out
        assert "never used" in out
        assert "staging box" in out

        manage.main(["revoke-key", "--tenant", str(tenant.id), "--id", str(row.id)])
        db.commit()
        capsys.readouterr()
        manage.main(["list-keys", "--tenant", str(tenant.id)])
        assert "revoked" in capsys.readouterr().out

    def test_list_keys_on_a_tenant_with_none(self, db, tenant, capsys):
        assert manage.main(["list-keys", "--tenant", str(tenant.id)]) == 0
        assert "no keys" in capsys.readouterr().out

    def test_list_keys_with_a_mistyped_id(self, capsys):
        assert manage.main(["list-keys", "--tenant", "nope"]) == 1
        assert "not a valid tenant id" in capsys.readouterr().err


class TestReextractSaysWhichQueues:
    """`--errors` was `store_true` with `default=True`: on whether or not it
    was typed, so `reextract --empty` re-queued the failures as well and there
    was no way to ask for the empty ones alone. An on/off flag now, on by
    default: `--no-errors` turns it off."""

    def _memory(self, db, tenant, state):
        from korely_agent.db.models import AgentMemory

        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                        content=f"a memory that is {state}", graph_sync_status=state)
        db.add(m)
        db.commit()
        return m.id

    def _state(self, db, mid):
        from korely_agent.db.models import AgentMemory

        db.expire_all()
        return db.get(AgentMemory, mid).graph_sync_status

    def test_the_errors_alone_by_default(self, db, tenant):
        failed = self._memory(db, tenant, "error")
        empty = self._memory(db, tenant, "done")
        assert manage.main(["reextract"]) == 0
        assert (self._state(db, failed), self._state(db, empty)) == ("pending", "done")

    def test_no_errors_turns_them_off(self, db, tenant, capsys):
        failed = self._memory(db, tenant, "error")
        empty = self._memory(db, tenant, "done")
        assert manage.main(["reextract", "--no-errors", "--empty"]) == 0
        assert (self._state(db, failed), self._state(db, empty)) == ("error", "pending")
        assert "failed -> queued" not in capsys.readouterr().out

    def test_both_when_both_are_asked(self, db, tenant):
        failed = self._memory(db, tenant, "error")
        empty = self._memory(db, tenant, "done")
        assert manage.main(["reextract", "--errors", "--empty"]) == 0
        assert (self._state(db, failed), self._state(db, empty)) == ("pending", "pending")

    def test_and_nothing_when_nothing_is_asked(self, db, tenant, capsys):
        failed = self._memory(db, tenant, "error")
        assert manage.main(["reextract", "--no-errors"]) == 0
        assert self._state(db, failed) == "error"
        assert "nothing to redo" in capsys.readouterr().out


class TestStatus:
    """The question an operator asks every morning, answered without psql.

    The counts are the easy half. The half that matters is extraction: a store
    that accepts every write and mines nothing looks exactly like a healthy one
    from the outside, which is how a depleted credit went unnoticed for hours
    on a running server.
    """

    def _memory(self, db, tenant, state, content="Luca lives in Milan."):
        from korely_agent.db.models import AgentMemory

        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                        content=content, graph_sync_status=state)
        db.add(m)
        db.commit()
        return m

    def test_counts_what_is_waiting_and_what_gave_up(self, db, tenant, capsys):
        self._memory(db, tenant, "done")
        self._memory(db, tenant, "pending")
        self._memory(db, tenant, "processing")
        self._memory(db, tenant, "error")

        assert manage.main(["status"]) == 0
        out = capsys.readouterr().out

        assert "memories    4" in out
        assert "2 waiting" in out
        assert "1 gave up extracting" in out, \
            "memories that will never become facts are invisible"

    def test_silence_when_nothing_is_stuck(self, db, tenant, capsys):
        self._memory(db, tenant, "done")
        manage.main(["status"])
        out = capsys.readouterr().out
        assert "waiting" not in out
        assert "gave up" not in out

    def test_a_closed_fact_is_counted_apart(self, db, tenant, capsys):
        from datetime import datetime, timezone

        from korely_agent.db.models import AgentFact

        live = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                         subject_label="Luca", predicate_raw="lives in",
                         predicate_canonical="lives_in", object_label="Rome")
        closed = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                           subject_label="Luca", predicate_raw="lives in",
                           predicate_canonical="lives_in", object_label="Milan",
                           invalid_at=datetime(2026, 6, 20, tzinfo=timezone.utc))
        db.add_all([live, closed])
        db.commit()

        manage.main(["status"])
        assert "facts       1 true now, 1 closed" in capsys.readouterr().out

    def test_without_a_key_it_says_nothing_is_being_mined(self, db, tenant, capsys, monkeypatch):
        from korely_agent import config

        from korely_agent.core import llm

        # `has_llm_provider` only ever saw the file. The question is now
        # "is there a key anywhere", which includes one pasted into the screen.
        monkeypatch.setattr(llm, "provider_configured", lambda: False)
        config.get_settings.cache_clear()
        try:
            manage.main(["status"])
            out = capsys.readouterr().out
            assert "NOT CONFIGURED" in out
            assert "nothing is being mined" in out
        finally:
            config.get_settings.cache_clear()

    def test_a_stuck_drain_is_called_stuck(self, db, tenant, capsys, monkeypatch):
        """The drain runs every ten seconds. Something waiting for an hour is
        not waiting."""
        from datetime import datetime, timedelta, timezone

        from korely_agent import config

        m = self._memory(db, tenant, "pending")
        m.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()

        monkeypatch.setattr("korely_agent.core.llm.provider_configured", lambda: True)
        config.get_settings.cache_clear()
        try:
            manage.main(["status"])
            out = capsys.readouterr().out
            assert "STUCK" in out
            assert "1h ago" in out
            assert "docker compose logs" in out, "no next step for whoever reads this"
        finally:
            config.get_settings.cache_clear()

    def test_a_recent_arrival_is_not_stuck(self, db, tenant, capsys, monkeypatch):
        from korely_agent import config

        self._memory(db, tenant, "pending")
        monkeypatch.setattr("korely_agent.core.llm.provider_configured", lambda: True)
        config.get_settings.cache_clear()
        try:
            manage.main(["status"])
            assert "STUCK" not in capsys.readouterr().out
        finally:
            config.get_settings.cache_clear()

    def test_it_reads_the_store_and_not_this_process(self, db, tenant, capsys, monkeypatch):
        """The first version of this command printed `provider_state()`, which
        is module state in whichever process is asking. `manage` runs in a new
        process every time, so on a server that had been mining facts all
        morning it answered "nothing has called the model yet". Caught by
        running it, not by a test: the test exists so it stays caught."""
        from datetime import datetime, timezone

        from korely_agent import config
        from korely_agent.core import llm
        from korely_agent.db.models import AgentFact

        db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                         subject_label="Luca", predicate_raw="lives in",
                         predicate_canonical="lives_in", object_label="Rome",
                         created_at=datetime.now(timezone.utc)))
        db.commit()

        monkeypatch.setattr("korely_agent.core.llm.provider_configured", lambda: True)
        monkeypatch.setattr(llm, "provider_state",
                            lambda: {"state": "unknown", "detail": "nothing has called the model yet"})
        config.get_settings.cache_clear()
        try:
            manage.main(["status"])
            out = capsys.readouterr().out
            assert "last fact" in out
            assert "unknown" not in out, "it is reporting this process instead of the store"
        finally:
            config.get_settings.cache_clear()

    def test_configured_but_nothing_mined_yet(self, db, tenant, capsys, monkeypatch):
        from korely_agent import config

        monkeypatch.setattr("korely_agent.core.llm.provider_configured", lambda: True)
        config.get_settings.cache_clear()
        try:
            manage.main(["status"])
            assert "no fact has ever been mined" in capsys.readouterr().out
        finally:
            config.get_settings.cache_clear()


class TestStatusShowsWhatTheModelCost:
    """On a machine the operator pays for, this is where the bill appears
    before it appears at Google."""

    def _usage(self, db, tenant, stage, tin, tout, agent=None):
        from korely_agent.db.models import AgentLlmUsage

        db.add(AgentLlmUsage(id=uuid.uuid4(), tenant_id=tenant.id, stage=stage,
                             provider="gemini", model="gemini-2.5-flash",
                             prompt_tokens=tin, completion_tokens=tout,
                             agent_id=agent))
        db.commit()

    def test_broken_down_by_stage_most_expensive_first(self, db, tenant, capsys):
        self._usage(db, tenant, "embedding", 0, 0)
        self._usage(db, tenant, "extraction", 1191, 1513)
        self._usage(db, tenant, "contradiction", 552, 397)

        manage.main(["status"])
        out = capsys.readouterr().out

        assert "model, last 30 days" in out
        lines = [l for l in out.splitlines() if "calls" in l]
        assert "extraction" in lines[0], "the expensive stage is not first"
        assert "1191 in" in lines[0] and "1513 out" in lines[0]

    def test_the_per_agent_bill(self, db, tenant, capsys):
        self._usage(db, tenant, "extraction", 100, 1172, agent="support-bot")
        self._usage(db, tenant, "extraction", 100, 712, agent="hr-bot")

        manage.main(["status"])
        out = capsys.readouterr().out
        assert "by agent:" in out
        assert out.index("support-bot") < out.index("hr-bot"), "not ordered by spend"

    def test_it_says_why_the_per_agent_bill_is_empty(self, db, tenant, capsys):
        """agent_id is optional on a write, so an empty breakdown is not a
        failure. But the per-agent bill is the reason the table exists, and
        printing nothing would look like nothing was spent."""
        self._usage(db, tenant, "extraction", 100, 200, agent=None)

        manage.main(["status"])
        out = capsys.readouterr().out
        assert "no agent_id on any write" in out
        assert "pass agent_id=" in out

    def test_old_calls_are_out_of_the_window(self, db, tenant, capsys):
        from datetime import datetime, timedelta, timezone

        from korely_agent.db.models import AgentLlmUsage

        self._usage(db, tenant, "extraction", 999, 999)
        row = db.query(AgentLlmUsage).one()
        row.ts = datetime.now(timezone.utc) - timedelta(days=45)
        db.commit()

        manage.main(["status"])
        assert "model, last 30 days" not in capsys.readouterr().out


class TestKeyLifetimesFromTheShell:
    """The conformance check of 2026-09-29: `--expires-in` said "0 means never"
    while 0 had come to mean expired, and there was no way left to mint a key
    that does not expire."""

    def test_never_mints_a_key_with_no_end_date(self, db, capsys):
        from korely_agent.db.models import AgentApiKey

        t = Tenant(id=uuid.uuid4(), name="Life")
        db.add(t)
        db.commit()
        assert manage.main(["issue-key", "--tenant", str(t.id), "--name", "forever", "--expires-in", "never"]) == 0
        row = db.query(AgentApiKey).filter(AgentApiKey.tenant_id == t.id, AgentApiKey.name == "forever").one()
        assert row.expires_at is None
        assert "expires never" in capsys.readouterr().err

    def test_zero_expires_it_at_once_as_the_help_says(self, db, capsys):
        from datetime import datetime, timezone

        from korely_agent.db.models import AgentApiKey

        t = Tenant(id=uuid.uuid4(), name="Gone")
        db.add(t)
        db.commit()
        assert manage.main(["issue-key", "--tenant", str(t.id), "--name", "dead", "--expires-in", "0"]) == 0
        row = db.query(AgentApiKey).filter(AgentApiKey.tenant_id == t.id, AgentApiKey.name == "dead").one()
        exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
        assert exp <= datetime.now(timezone.utc)

    def test_the_help_no_longer_promises_never_for_zero(self, capsys):
        with pytest.raises(SystemExit):
            manage.main(["issue-key", "--help"])
        out = capsys.readouterr().out
        assert "0 means never" not in out and "never" in out and "0 expires it at once" in out

    def test_a_number_out_of_range_is_refused(self, capsys):
        with pytest.raises(SystemExit):
            manage.main(["issue-key", "--tenant", str(uuid.uuid4()), "--expires-in", "4000"])
        assert "between 0 and 3650" in capsys.readouterr().err

    def test_a_name_already_taken_is_a_sentence_not_a_traceback(self, db, capsys):
        t = Tenant(id=uuid.uuid4(), name="Twice")
        db.add(t)
        db.commit()
        assert manage.main(["issue-key", "--tenant", str(t.id), "--name", "same"]) == 0
        capsys.readouterr()
        assert manage.main(["issue-key", "--tenant", str(t.id), "--name", "same"]) == 1
        err = capsys.readouterr().err
        assert "already has a key called 'same'" in err and "Traceback" not in err
