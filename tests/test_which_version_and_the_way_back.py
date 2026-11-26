"""What version am I running, and can I get back.

The tester tried to upgrade from one release to the next and could not, because
nothing said which commit was which release. The second half is worse than the
first: somebody who upgrades into a fault has nowhere to return to.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestTheProductKnowsWhichVersionItIs:

    def test_there_is_a_version_and_it_is_comparable(self):
        import korely_agent

        assert re.match(r"^\d+\.\d+\.\d+(-dev)?$", korely_agent.__version__), (
            f"version is {korely_agent.__version__!r}: nobody can tell whether "
            f"that is newer or older than what they were running")

    def test_pyproject_says_the_same_thing(self):
        import korely_agent

        declared = re.search(r'^version = "([^"]+)"',
                             (ROOT / "pyproject.toml").read_text(), re.M).group(1)
        assert declared == korely_agent.__version__, (
            "two places that can disagree are one place that lies")

    def test_the_readme_says_how_to_go_back(self):
        """Since the README became a landing page (29 Oct 2026) upgrading and
        going back are docs/guide/operations.md, "Keeping it running"."""
        from tests import docs_text

        operations = docs_text.page("docs/guide/operations.md").lower()
        assert "how to go back" in operations, (
            "the documentation says how to upgrade and not how to go back, which is "
            "the half somebody needs while something is broken")
        i = operations.index("how to go back")
        nearby = operations[i:i + 1400]
        assert "backup" in nearby.lower(), (
            "going back across a migration needs the dump, and the recipe has "
            "to say so where somebody reads it in a hurry")

    def test_the_mcp_handshake_does_not_answer_with_the_librarys_version(self):
        # Marked `db` not because it touches one, but because building the MCP
        # server reads the settings, and the settings require a DATABASE_URL.
        # Without the mark it runs in the no-database suite and fails there for
        # a reason that has nothing to do with what it checks.
        """It used to say 1.30.0, which is the MCP SDK's number, not ours."""
        import korely_agent
        from korely_agent.mcp import server as mcp_server

        mcp_server._agent_mcp_singleton = None
        mcp_server._agent_streamable_app_singleton = None
        try:
            mcp = mcp_server._get_agent_mcp()
            assert mcp._mcp_server.version == korely_agent.__version__
        finally:
            mcp_server._agent_mcp_singleton = None
            mcp_server._agent_streamable_app_singleton = None

    def test_the_published_api_document_says_the_same_version(self, monkeypatch):
        """`/openapi.json` and `/docs` said 0.1.0 whatever was running: the
        number was typed into `FastAPI(version=...)` when the service was
        extracted and never moved, while `/health` said 0.1.8. A client
        generated from the document, or a person reading `/docs`, got the one
        number in the product that was wrong. (`db` for the same reason as the
        test above: building the app reads the settings.)"""
        import korely_agent
        from korely_agent.main import create_app
        from korely_agent.mcp import server as mcp_server

        monkeypatch.setenv("RUN_WORKER", "false")
        mcp_server._agent_mcp_singleton = None
        mcp_server._agent_streamable_app_singleton = None
        try:
            assert create_app().openapi()["info"]["version"] == korely_agent.__version__
        finally:
            mcp_server._agent_mcp_singleton = None
            mcp_server._agent_streamable_app_singleton = None


class TestTheScopesListStaysGeneric:
    """The tester asked what happens if this list ever became install-specific.

    It answers without credentials on purpose: three fixed names that are in the
    public documentation anyway, and not knowing them is how somebody mints a
    key with a scope nobody serves. That holds only while the list says nothing
    about this install, so that is the thing to hold.
    """

    def test_it_returns_the_fixed_vocabulary_and_nothing_else(self):
        from korely_agent.services import keys as key_service

        assert set(key_service.KNOWN_SCOPES) == {
            "memories:read", "memories:write", "admin"}, (
            "the scope vocabulary changed; if it ever becomes per-install, the "
            "endpoint that serves it without credentials has to stop doing that")


class TestOneOpenFactPerTriple:
    """Five identical writes at once used to leave five identical open facts.

    The dedup is a read followed by a write. Under concurrency every worker
    reads "nothing there" and every worker writes. A tester saw it one time in
    five, and the worst case is an idle machine: with the queue already full the
    workers serialise themselves and the defect hides.

    A race is not closed by reading more carefully. It is closed by taking the
    decision away from the reader, so the test is on the thing that decides.
    """

    def test_the_database_refuses_the_second_one(self, db, tenant):
        import uuid as _uuid

        from sqlalchemy.exc import IntegrityError

        from korely_agent.db.models import AgentFact

        def one():
            return AgentFact(
                id=_uuid.uuid4(), tenant_id=tenant.id, project_id=None,
                end_user_id="cc", agent_id=None, subject_label="cc",
                subject_type="person", predicate_raw="plan",
                predicate_canonical="plan", object_label="Silver plan",
                object_is_literal=False, confidence=0.9)

        db.add(one()); db.commit()
        db.add(one())
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_but_history_may_hold_the_same_triple_many_times(self, db, tenant):
        """The constraint is on what is true NOW. Closed facts repeat freely —
        that is the product."""
        import uuid as _uuid
        from datetime import datetime, timezone

        from korely_agent.db.models import AgentFact

        for _ in range(3):
            db.add(AgentFact(
                id=_uuid.uuid4(), tenant_id=tenant.id, project_id=None,
                end_user_id="hh", agent_id=None, subject_label="hh",
                subject_type="person", predicate_raw="plan",
                predicate_canonical="plan", object_label="Gold plan",
                object_is_literal=False, confidence=0.9,
                invalid_at=datetime.now(timezone.utc)))
        db.commit()  # three closed copies: allowed


class TestZeroDaysMeansExpired:
    """`expires_in_days: 0` used to mean "never", the most permissive of three
    readings and the opposite of what it looks like."""

    def test_zero_expires_it_now(self):
        from korely_agent.services.keys import _expiry
        from datetime import datetime, timezone

        got = _expiry(0)
        assert got is not None, "0 still means never, which is the trap"
        assert got <= datetime.now(timezone.utc)

    def test_none_is_the_only_way_to_say_never(self):
        from korely_agent.services.keys import _expiry

        assert _expiry(None) is None

    def test_a_negative_is_refused_rather_than_rounded_up(self):
        from korely_agent.services.keys import _expiry

        with pytest.raises(ValueError):
            _expiry(-1)


class TestTheOutboxIsTrimmed:
    """Every other table with a retention window was being trimmed and this one
    was not, so the webhook outbox grew forever."""

    def test_finished_deliveries_older_than_the_window_go(self, db, tenant):
        import uuid as _uuid
        from datetime import datetime, timedelta, timezone

        from korely_agent.db.models import (AgentWebhookDelivery,
                                            AgentWebhookEndpoint)
        from korely_agent.services import webhooks

        ep = AgentWebhookEndpoint(id=_uuid.uuid4(), tenant_id=tenant.id,
                                  url="https://x.example/a", secret="whsec_x",
                                  events=["memory.created"], active=True)
        db.add(ep); db.commit()
        old = datetime.now(timezone.utc) - timedelta(days=40)

        def add(status, attempts, when):
            db.add(AgentWebhookDelivery(
                id=_uuid.uuid4(), tenant_id=tenant.id, endpoint_id=ep.id,
                event_type="memory.created", payload={}, status=status,
                attempts=attempts, max_attempts=5, created_at=when))

        add("delivered", 1, old)                       # finished and old: goes
        add("failed", 5, old)                          # exhausted and old: goes
        add("failed", 2, old)                          # still retrying: stays
        add("delivered", 1, datetime.now(timezone.utc))  # recent: stays
        db.commit()

        gone = webhooks.purge_expired(db, keep_days=30)
        assert gone == 2, gone
        left = db.query(AgentWebhookDelivery).filter(
            AgentWebhookDelivery.tenant_id == tenant.id).all()
        assert len(left) == 2
        assert any(d.attempts == 2 for d in left), (
            "a delivery still waiting for its next retry was deleted on age; "
            "that is live state, not history")

    def test_a_window_of_zero_keeps_everything(self, db, tenant):
        from korely_agent.services import webhooks

        assert webhooks.purge_expired(db, keep_days=0) == 0


def _what_a_customer_reads(root):
    """The files somebody installing this reads: the product's source and its
    docstrings, the screen's source, the examples, the install files."""
    from tests import docs_text

    files = [root / n for n in ("Dockerfile", "docker-compose.yml",
                                ".env.example", "dashboard/README.md")]
    files += docs_text.pages()
    files += sorted((root / "korely_agent").rglob("*.py"))
    files += sorted((root / "examples").rglob("*.py"))
    files += sorted((root / "examples").rglob("*.md"))
    files += sorted(p for p in (root / "dashboard" / "src").rglob("*")
                    if p.suffix in (".ts", ".tsx"))
    # The hosted service's own code and pages (KORELY_MODE=hosted, the one
    # codebase of 8 Oct 2026) are not what somebody installing this reads.
    return [f for f in files if f.is_file() and not is_hosted_only(root, f)]


def is_hosted_only(root, path) -> bool:
    """korely_agent/hosted/ and dashboard/src/hosted/: loaded only with
    KORELY_MODE=hosted."""
    rel = path.relative_to(root).parts
    return rel[:2] == ("korely_agent", "hosted") or rel[:3] == ("dashboard", "src", "hosted")


class TestAKeySaysWhereItCameFrom:
    """`kor_live_` is the hosted service. A key minted here says `kor_self_`.

    One incident is the whole reason: a self-hosted key went into a client with
    no address, the client used its default, and the memory reached our servers
    before the 401 came back. The refusal was correct and it was too late. Two
    keys that look identical cannot be told apart by eye, in a log, or in a
    screenshot pasted into a chat.
    """

    def test_what_this_product_mints_is_not_what_the_hosted_service_mints(self):
        from korely_agent.services import keys as key_service

        assert key_service.PREFIX_DEFAULT == "kor_self_"
        assert key_service.PREFIX_DEFAULT != key_service.PREFIX_LIVE

    def test_an_older_key_no_longer_verifies(self, db, tenant):
        """0.2.0, as the CHANGELOG said from 0.1.41: on an install of one's
        own a `kor_live_` key, even one minted here before 0.1.3, is refused:
        that prefix is the Cloud's alone."""
        from korely_agent.services import keys as key_service

        _row, raw = key_service.create_api_key(
            db, tenant_id=tenant.id, name="old one",
            prefix=key_service.PREFIX_LIVE)
        assert raw.startswith("kor_live_")
        assert key_service.verify_api_key(db, raw) is None

    def test_no_example_anywhere_tells_somebody_to_expect_the_hosted_prefix(self):
        """A marker the documentation contradicts is worse than no marker."""
        import pathlib

        import re

        root = pathlib.Path(__file__).resolve().parents[1]
        # What is forbidden is a hosted key used as an EXAMPLE — the form
        # somebody copies. Naming `kor_live_` in a sentence that explains the
        # difference is the opposite of the problem, and a first version of this
        # test forbade that too and went red on the paragraph that exists to
        # prevent the mistake.
        looks_like_an_example = re.compile(r"kor_live_(\.\.\.|[A-Za-z0-9_\-]{6,})")
        # Every file a customer reads, not only the README and the screen: the
        # first version of this looked at those two and passed while /v1/ping,
        # the MCP module and the example script all showed `kor_live_...`.
        # tests/ and scripts/ are left out on purpose: they send hosted keys to
        # this install to watch it refuse them.
        offenders = []
        for f in _what_a_customer_reads(root):
            if looks_like_an_example.search(f.read_text()):
                offenders.append(str(f.relative_to(root)))
        assert not offenders, (
            f"these show a hosted-service key as an example on a product "
            f"somebody installs themselves: {offenders}")
