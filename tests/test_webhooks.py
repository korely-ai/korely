"""Outbound webhooks: where the server connects out, and who decides.

Two properties matter here and neither had a test. The first is that a URL
supplied by whoever holds an API key does not become a way to make this server
talk to things behind its own firewall. The second is that a delivery goes out
once, including when two drains are running, which is the default: the API
process runs a worker and the documentation also offers a standalone one.
"""
import threading
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from korely_agent.db.models import AgentWebhookDelivery, AgentWebhookEndpoint
from korely_agent.services import webhooks
from korely_agent.services.sentinels import EVERY_PROJECT
from tests.thread_helpers import wait_idle

# The public URLs here are on the documentation names, resolved without a
# resolver (conftest.public_dns): these tests asked the real DNS until the
# suite closed the network (2026-09-28).
pytestmark = pytest.mark.usefixtures("public_dns")


@pytest.fixture(autouse=True)
def public_only(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_URLS", "false")
    monkeypatch.setenv("WEBHOOK_ALLOW_HTTP", "false")
    from korely_agent import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


class TestWhereItIsAllowedToConnect:
    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "https://169.254.169.254/",
        "http://localhost:8000/hook",
        "https://127.0.0.1/hook",
        "https://[::1]/hook",
        "https://10.0.0.5/internal",
        "https://192.168.1.1/admin",
        "https://172.16.0.1/",
        "file:///etc/passwd",
        "gopher://internal/",
        "",
        "   ",
    ])
    def test_refused(self, db, tenant, url):
        with pytest.raises(webhooks.UnsafeWebhookUrl):
            webhooks.create_endpoint(db, tenant.id, url=url, events=["*"])
        assert db.query(AgentWebhookEndpoint).count() == 0

    def test_plain_http_is_refused_even_to_a_public_host(self, db, tenant):
        with pytest.raises(webhooks.UnsafeWebhookUrl) as e:
            webhooks.create_endpoint(db, tenant.id, url="http://example.com/h",
                                     events=["*"])
        assert "clear text" in str(e.value)

    def test_a_public_https_url_is_accepted(self, db, tenant):
        ep = webhooks.create_endpoint(db, tenant.id, url="https://example.com/h",
                                      events=["memory.created"])
        assert ep.id is not None
        assert ep.secret.startswith("whsec_")

    def test_the_switch_exists_for_local_testing(self, db, tenant, monkeypatch):
        monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_URLS", "true")
        monkeypatch.setenv("WEBHOOK_ALLOW_HTTP", "true")
        from korely_agent import config

        config.get_settings.cache_clear()
        ep = webhooks.create_endpoint(db, tenant.id, url="http://localhost:9999/h",
                                      events=["*"])
        assert ep.id is not None

    def test_delivery_checks_again(self, db, tenant, monkeypatch):
        """A name that resolved to a public address at registration can resolve
        to a private one afterwards. One check at the start only catches honest
        mistakes."""
        ok, code, err = webhooks._deliver(
            "https://127.0.0.1/h", "whsec_x", str(uuid.uuid4()), {"a": 1}
        )
        assert ok is False
        assert "refused" in (err or "")


class TestSigning:
    def test_the_signature_covers_id_timestamp_and_body(self):
        a = webhooks.sign_payload("s", "id1", 100, '{"a":1}')
        assert a.startswith("v1,")
        assert a != webhooks.sign_payload("s", "id2", 100, '{"a":1}')
        assert a != webhooks.sign_payload("s", "id1", 101, '{"a":1}')
        assert a != webhooks.sign_payload("s", "id1", 100, '{"a":2}')
        assert a != webhooks.sign_payload("other", "id1", 100, '{"a":1}')


class TestDeliveredOnce:
    """The property the docstring claimed and the code did not have."""

    def _endpoint(self, db, tenant, monkeypatch):
        monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE_URLS", "true")
        monkeypatch.setenv("WEBHOOK_ALLOW_HTTP", "true")
        from korely_agent import config

        config.get_settings.cache_clear()
        return webhooks.create_endpoint(db, tenant.id, url="http://localhost:1/h",
                                        events=["*"])

    def test_two_drains_do_not_send_the_same_delivery_twice(
        self, db, engine, tenant, monkeypatch
    ):
        ep = self._endpoint(db, tenant, monkeypatch)
        for _ in range(6):
            db.add(AgentWebhookDelivery(
                tenant_id=tenant.id, endpoint_id=ep.id,
                event_type="memory.created", payload={"x": 1},
                status=webhooks.STATUS_PENDING,
            ))
        db.commit()

        sent, lock = [], threading.Lock()

        def fake_deliver(url, secret, delivery_id, payload):
            with lock:
                sent.append(delivery_id)
            return True, 200, None

        monkeypatch.setattr(webhooks, "_deliver", fake_deliver)

        Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        start = threading.Barrier(2)
        errors = []

        def drain():
            # Six rounds each, more than the six deliveries need: whatever
            # the two drains share, none is sent twice.
            s = Session()
            try:
                start.wait(timeout=10)
                for _ in range(6):
                    webhooks.process_deliveries(s, limit=10)
            except Exception as e:
                errors.append(e)
            finally:
                s.rollback()
                s.close()

        ts = [threading.Thread(target=drain) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        # A round's attempts may still run in their threads when it returns.
        assert wait_idle(webhooks._sending, 30)

        assert not errors, errors[:2]
        assert len(sent) == len(set(sent)), f"delivered twice: {sent}"
        assert len(set(sent)) == 6

    def test_a_claim_is_not_picked_up_again_while_it_is_in_flight(
        self, db, engine, tenant, monkeypatch
    ):
        ep = self._endpoint(db, tenant, monkeypatch)
        d = AgentWebhookDelivery(
            tenant_id=tenant.id, endpoint_id=ep.id, event_type="memory.created",
            payload={"x": 1}, status=webhooks.STATUS_PENDING,
        )
        db.add(d)
        db.commit()

        seen, second = [], []
        Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

        def slow(url, secret, delivery_id, payload):
            # While this one is in flight, a second drain, on its own session,
            # must find nothing: the claim's lease holds it. Until the audit
            # of 17 Oct 2026 the second drain ran after the delivery had been
            # written down as delivered, so the lease was never what it met.
            seen.append(delivery_id)
            if len(seen) == 1:
                s = Session()
                try:
                    second.append(webhooks.process_deliveries(s, limit=10))
                finally:
                    s.rollback()
                    s.close()
            return True, 200, None

        monkeypatch.setattr(webhooks, "_deliver", slow)
        webhooks.process_deliveries(db, limit=10)
        # Read once the attempt has ended, not from the round's counts: the
        # second drain inside it can take longer than the round's patience.
        assert wait_idle(webhooks._sending, 30)
        db.refresh(d)
        assert d.status == webhooks.STATUS_DELIVERED
        assert second == [{"delivered": 0, "failed": 0}]
        assert len(seen) == 1, f"picked up again while in flight: {seen}"

    def test_a_failure_backs_off_and_stops_at_max_attempts(
        self, db, tenant, monkeypatch
    ):
        ep = self._endpoint(db, tenant, monkeypatch)
        d = AgentWebhookDelivery(
            tenant_id=tenant.id, endpoint_id=ep.id, event_type="memory.created",
            payload={"x": 1}, status=webhooks.STATUS_PENDING, max_attempts=3,
        )
        db.add(d)
        db.commit()

        monkeypatch.setattr(webhooks, "_deliver",
                            lambda *a, **k: (False, 500, "HTTP 500"))

        for expected in (1, 2, 3):
            # Make it eligible again, as the backoff would after its wait
            db.query(AgentWebhookDelivery).filter(
                AgentWebhookDelivery.id == d.id
            ).update({"next_retry_at": None})
            db.commit()
            webhooks.process_deliveries(db, limit=10)
            # The attempt writes its next wait when it ends: written after the
            # update below makes the delivery due, it would keep it from the
            # next round.
            assert wait_idle(webhooks._sending, 30)
            db.refresh(d)
            assert d.attempts == expected

        assert d.status == webhooks.STATUS_FAILED
        # Exhausted: never selected again, whatever next_retry_at says
        assert webhooks.process_deliveries(db, limit=10) == {"delivered": 0,
                                                            "failed": 0}

    def test_an_inactive_endpoint_is_not_retried_forever(
        self, db, tenant, monkeypatch
    ):
        ep = self._endpoint(db, tenant, monkeypatch)
        d = AgentWebhookDelivery(
            tenant_id=tenant.id, endpoint_id=ep.id, event_type="memory.created",
            payload={"x": 1}, status=webhooks.STATUS_PENDING,
        )
        db.add(d)
        ep.active = False
        db.commit()

        r = webhooks.process_deliveries(db, limit=10)
        assert r["failed"] == 1
        assert webhooks.process_deliveries(db, limit=10)["failed"] == 0


class TestEmit:
    def test_an_unknown_event_type_enqueues_nothing(self, db, tenant):
        assert webhooks.emit_event(db, tenant.id, "not.an.event", {}) == 0

    def test_only_subscribed_endpoints_get_it(self, db, tenant):
        a = webhooks.create_endpoint(db, tenant.id, url="https://example.com/a",
                                     events=["memory.created"])
        b = webhooks.create_endpoint(db, tenant.id, url="https://example.org/b",
                                     events=["fact.invalidated"])
        n = webhooks.emit_event(db, tenant.id, "memory.created", {"id": "m1"})
        assert n == 1
        rows = db.query(AgentWebhookDelivery).all()
        assert [r.endpoint_id for r in rows] == [a.id]
        assert b.id not in [r.endpoint_id for r in rows]

    def test_one_tenant_does_not_receive_anothers_events(self, db, tenant):
        from korely_agent.db.models import Tenant

        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        webhooks.create_endpoint(db, other.id, url="https://example.net/other",
                                 events=["*"])
        assert webhooks.emit_event(db, tenant.id, "memory.created", {"id": "m"}) == 0


class TestEndpointsOfAProject:
    """An endpoint may belong to one project (8 Oct 2026, the hosted
    product's r64): it then receives that project's events and the
    account's, so a staging receiver no longer gets production's end users.
    One with no project receives every project's, as before."""

    def _projects(self, db, tenant):
        from korely_agent.db.models import AgentProject

        rows = [AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name=n, slug=n.lower())
                for n in ("Production", "Staging")]
        db.add_all(rows)
        db.commit()
        return rows

    def _memory(self, db, tenant, project):
        from korely_agent.db.models import AgentMemory
        from korely_agent.services.ids import mem_public_id

        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project.id,
                        end_user_id="maria", content="I moved to Lisbon.")
        db.add(m)
        db.commit()
        return mem_public_id(m.id)

    def _received(self, db):
        return {(r.endpoint_id, r.payload.get("project_id")) for r in db.query(AgentWebhookDelivery).all()}

    def test_a_projects_endpoint_hears_only_its_project(self, db, tenant):
        prod, staging = self._projects(db, tenant)
        every = webhooks.create_endpoint(db, tenant.id, url="https://example.com/all", events=["*"])
        mine = webhooks.create_endpoint(db, tenant.id, url="https://example.org/staging",
                                        events=["*"], project_id=staging.id)
        webhooks.emit_event(db, tenant.id, "memory.created", {"id": self._memory(db, tenant, prod)})
        assert self._received(db) == {(every.id, str(prod.id))}
        webhooks.emit_event(db, tenant.id, "memory.created", {"id": self._memory(db, tenant, staging)})
        assert (mine.id, str(staging.id)) in self._received(db)
        assert (every.id, str(staging.id)) in self._received(db)

    def test_the_project_given_wins_over_the_data(self, db, tenant):
        prod, staging = self._projects(db, tenant)
        mine = webhooks.create_endpoint(db, tenant.id, url="https://example.org/prod",
                                        events=["*"], project_id=prod.id)
        n = webhooks.emit_event(db, tenant.id, "key.expiring", {"key_id": "k"}, project_id=prod.id)
        assert n == 1 and self._received(db) == {(mine.id, str(prod.id))}

    def test_an_event_of_no_project_reaches_only_every_project_endpoints(self, db, tenant):
        prod, _ = self._projects(db, tenant)
        every = webhooks.create_endpoint(db, tenant.id, url="https://example.com/all", events=["*"])
        webhooks.create_endpoint(db, tenant.id, url="https://example.org/prod",
                                 events=["*"], project_id=prod.id)
        webhooks.emit_event(db, tenant.id, "memory.created", {"id": "mem_unknown"})
        assert self._received(db) == {(every.id, None)}

    def test_an_account_event_reaches_every_endpoint(self, db, tenant):
        prod, _ = self._projects(db, tenant)
        webhooks.create_endpoint(db, tenant.id, url="https://example.com/all", events=["*"])
        webhooks.create_endpoint(db, tenant.id, url="https://example.org/prod",
                                 events=["*"], project_id=prod.id)
        webhooks.add_event("account.notice", account_wide=True)
        try:
            assert webhooks.emit_event(db, tenant.id, "account.notice", {"n": 1}) == 2
            assert all("project_id" not in r.payload for r in db.query(AgentWebhookDelivery).all())
        finally:
            webhooks.reset_events()
        assert "account.notice" not in webhooks.VALID_EVENTS

    def test_deleting_the_project_takes_its_endpoints(self, db, tenant):
        _, staging = self._projects(db, tenant)
        ep_id = webhooks.create_endpoint(db, tenant.id, url="https://example.org/staging",
                                         events=["*"], project_id=staging.id).id
        db.delete(staging)
        db.commit()
        db.expire_all()
        assert db.query(AgentWebhookEndpoint).filter(AgentWebhookEndpoint.id == ep_id).count() == 0


class TestTheCostRowSaysWhoAnswered:
    """A cost row's `provider` is the client; every OpenAI-compatible host
    shares one. The vendor and the region of the writer tell them apart (8
    Oct 2026), for a cap on one vendor's spend."""

    def test_vendor_and_region_reach_the_row(self, db, tenant):
        from korely_agent.db.models import AgentLlmUsage
        from korely_agent.services import llm_usage

        with llm_usage.attributed(tenant.id):
            llm_usage.record("mistral", "gpt-oss-120b", 100, 20, stage="extraction",
                             vendor="scaleway", region="eu")
            llm_usage.record("local", "embeddinggemma", 10, 0, stage="embedding")
        rows = {(r.provider, r.vendor, r.region) for r in db.query(AgentLlmUsage).all()}
        assert rows == {("mistral", "scaleway", "eu"), ("local", None, None)}


class TestTheEventCommitsWithItsWrite:
    """An event is enqueued in the transaction of the write it is about, so
    the two commit together (audit of 17 Oct 2026). It was enqueued
    after the write's commit, with a commit of its own, and committed or
    rolled back whatever the caller had open: a failure between the two
    commits kept the write and lost its event for good."""

    @staticmethod
    def _began(db, table, row_id):
        """When the transaction that inserted a row began: `created_at` is
        the server's now(), the same for every row of one transaction (and
        of its savepoints), different for any other."""
        from sqlalchemy import text

        return db.execute(text(f"SELECT created_at FROM {table} WHERE id = :i"),
                          {"i": row_id}).scalar()

    def _delivery(self, db, event):
        return db.query(AgentWebhookDelivery).filter(AgentWebhookDelivery.event_type == event).one()

    def test_emit_never_commits_what_the_caller_has_open(self, db, tenant):
        from korely_agent.db.models import Tenant

        webhooks.create_endpoint(db, tenant.id, url="https://example.com/a", events=["*"])
        other = Tenant(id=uuid.uuid4(), name="Not yet")
        db.add(other)
        assert webhooks.emit_event(db, tenant.id, "memory.created", {"id": "m"}) == 1
        db.rollback()
        assert db.get(Tenant, other.id) is None, "the caller's work was committed by the event"
        assert db.query(AgentWebhookDelivery).count() == 0, "the event outlived its write"

    def test_a_failed_enqueue_undoes_only_itself(self, db, tenant, monkeypatch):
        from korely_agent.db.models import Tenant

        webhooks.create_endpoint(db, tenant.id, url="https://example.com/a", events=["*"])
        other = Tenant(id=uuid.uuid4(), name="Kept")
        db.add(other)

        def broken(*a, **k):
            db.add(AgentWebhookDelivery(tenant_id=tenant.id, endpoint_id=uuid.uuid4(),
                                        event_type="memory.created", payload={},
                                        status="pending", max_attempts=1))
            db.flush()   # a foreign key nobody has: the insert fails

        monkeypatch.setattr(webhooks, "_enqueue", broken)
        assert webhooks.emit_event(db, tenant.id, "memory.created", {"id": "m"}) is None
        db.commit()
        assert db.get(Tenant, other.id) is not None

    def test_a_key_notice_that_failed_to_enqueue_is_sent_next_round(self, db, tenant, monkeypatch):
        """The audit's reproduction: the row was stamped and committed before
        the enqueue, so a failed insert was never tried again and the
        README's two weeks of notice were lost."""
        from datetime import datetime, timedelta, timezone

        from korely_agent.db.models import AgentApiKey
        from korely_agent.services import keys as key_service

        webhooks.create_endpoint(db, tenant.id, url="https://example.com/a", events=["key.expiring"])
        row, _ = key_service.create_api_key(db, tenant_id=tenant.id, name="soon")
        row.expires_at = datetime.now(timezone.utc) + timedelta(days=3)
        db.commit()

        real = webhooks._enqueue
        monkeypatch.setattr(webhooks, "_enqueue",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db hiccup")))
        assert key_service.warn_about_expiring(db) == {}
        assert db.get(AgentApiKey, row.id).expiry_warned_at is None
        monkeypatch.setattr(webhooks, "_enqueue", real)

        assert key_service.warn_about_expiring(db) == {"warned": 1, "expired": 0}
        assert self._delivery(db, "key.expiring").payload["data"]["key_id"] == str(row.id)
        assert db.get(AgentApiKey, row.id).expiry_warned_at is not None

    def test_a_rest_write_and_its_event_are_one_transaction(self, db, tenant, monkeypatch):
        from fastapi.testclient import TestClient

        from korely_agent.db import get_db
        from korely_agent.main import create_app
        from korely_agent.services import keys as key_service
        from korely_agent.services.ids import parse_mem_id

        monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
        webhooks.create_endpoint(db, tenant.id, url="https://example.com/a", events=["memory.created"])
        _, full = key_service.create_api_key(db, tenant_id=tenant.id, name="w",
                                             scopes=["memories:read", "memories:write"])
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c:
            r = c.post("/v1/memories", headers={"Authorization": f"Bearer {full}"},
                       json={"content": "Maria moved to Lisbon.", "user_id": "maria"})
        app.dependency_overrides.clear()
        assert r.status_code == 201, r.text
        d = self._delivery(db, "memory.created")
        assert d.payload["data"] == {"id": r.json()["id"], "user_id": "maria", "agent_id": None}
        mid = parse_mem_id(r.json()["id"])
        assert self._began(db, "agent_memories", mid) == self._began(db, "agent_webhook_deliveries", d.id)

    def test_a_batch_item_and_its_event_are_one_transaction(self, db, tenant, monkeypatch):
        from korely_agent.db.models import AgentMemory
        from korely_agent.services import batch

        monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                            lambda *a, **k: [])
        monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
        webhooks.create_endpoint(db, tenant.id, url="https://example.com/a", events=["memory.created"])
        batch.enqueue_batch(db, tenant.id, [{"content": "one", "user_id": "u1"}])
        batch.process_batch_jobs(db, limit=1)
        m = db.query(AgentMemory).filter(AgentMemory.tenant_id == tenant.id).one()
        d = self._delivery(db, "memory.created")
        assert self._began(db, "agent_memories", m.id) == self._began(db, "agent_webhook_deliveries", d.id)

    def test_a_forgotten_fact_and_its_event_are_one_transaction(self, db, tenant):
        from datetime import datetime, timezone

        from korely_agent.db.models import AgentFact
        from korely_agent.services.facts import service as fsvc

        webhooks.create_endpoint(db, tenant.id, url="https://example.com/a", events=["fact.invalidated"])
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="maria",
                      subject_label="Maria", subject_canonical="maria", subject_type="person",
                      predicate_raw="lives_in", predicate_canonical="lives_in",
                      object_label="Rome", object_canonical="rome", confidence=0.9,
                      valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc), tense="current")
        db.add(f)
        db.commit()
        out = fsvc.forget_fact(db, tenant.id, f.id, project_id=EVERY_PROJECT)
        assert out["status"] == "forgotten"
        from korely_agent.db.models import AgentForgetAudit

        audit = db.query(AgentForgetAudit).filter(AgentForgetAudit.tenant_id == tenant.id).one()
        d = self._delivery(db, "fact.invalidated")
        assert self._began(db, "agent_forget_audit", audit.id) == \
            self._began(db, "agent_webhook_deliveries", d.id)
