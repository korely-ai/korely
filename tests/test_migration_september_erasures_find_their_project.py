"""f6a3d9c1b8e2: the September erasures find their project, or say it is not
recorded (audit of 27 Oct 2026).

The end-user and agent erasures of 0.1.1-0.1.11 wrote their record with no
project. d8b4e1a7c3f5 recovered it for the keyed end-user records it could
match by pseudonym, and left two kinds as they were:

- without SECRET_KEY, a5d81f3c7e64 had already taken the id off the record,
  so it matched nothing, and the trail rows filed under the key's project
  kept the erased id in clear;
- a record still with no project replays in every project of the account,
  where it erased a namesake.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import TEST_DB
from tests.test_migrations import _alembic

_BEFORE, _THIS = "d8b2f4a6c1e7", "f6a3d9c1b8e2"
_NIL = uuid.UUID(int=0)


def _account(c):
    t, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    c.execute(text("INSERT INTO tenants (id, name) VALUES (:id, 'Acme')"), {"id": t})
    for pid, name, default in ((a, "Default", True), (b, "Beta", False)):
        c.execute(text("INSERT INTO agent_projects (id, tenant_id, name, slug, is_default)"
                       " VALUES (:id, :t, :n, :s, :d)"),
                  {"id": pid, "t": t, "n": name, "s": name.lower(), "d": default})
    return t, a, b


def _trail(c, t, project, user, ts, action, endpoint=None):
    rid = uuid.uuid4()
    c.execute(text("INSERT INTO audit_events (id, ts, tenant_id, project_id, actor, action,"
                   " result, end_user_id, meta) VALUES (:id, :ts, :t, :pr, 'rest', :a, 'ok',"
                   " :u, CAST(:m AS json))"),
              {"id": rid, "ts": ts, "t": t, "pr": project, "a": action, "u": user,
               "m": '{"endpoint": "%s"}' % endpoint if endpoint else None})
    return rid


def _record(c, t, scope, at, user=None, target=None):
    rid = uuid.uuid4()
    c.execute(text("INSERT INTO agent_forget_audit (id, tenant_id, end_user_id, scope, target_id,"
                   " created_at) VALUES (:id, :t, :u, :s, :tg, :c)"),
              {"id": rid, "t": t, "u": user, "s": scope, "tg": target, "c": at})
    return rid


@pytest.fixture
def scratch():
    """(url, engine) of an empty database with pgvector, dropped afterwards
    (as tests/test_migrations.scratch)."""
    name = f"korely_migrations_{uuid.uuid4().hex[:8]}"
    admin = create_engine(TEST_DB, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(TEST_DB).set(database=name).render_as_string(hide_password=False)
    eng = create_engine(url)
    try:
        with eng.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        yield url, eng
    finally:
        eng.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def keyless(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "")
    from korely_agent.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_a_keyless_september_erasure_takes_the_id_off_its_project(scratch, keyless):
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, default, beta = _account(c)
        # As 0.1.11 wrote it and a5d81f3c7e64 left it without a key: no id,
        # no project; the trail in Beta, the key's project, in clear.
        rec = _record(c, t, "user", at)
        before = _trail(c, t, beta, "sept-user", at - timedelta(days=1), "write")
        erase = _trail(c, t, beta, "sept-user", at + timedelta(seconds=1), "erase",
                       "users:delete")
        later = _trail(c, t, beta, "sept-user", at + timedelta(days=5), "read")
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        def who(rid):
            return c.execute(text("SELECT end_user_id FROM audit_events WHERE id = :id"),
                             {"id": rid}).scalar_one()

        assert (who(before), who(erase)) == (None, None), "the September trail kept the id"
        assert who(later) == "sept-user", "a row after the erasure is not this one's"
        made_in = c.execute(text("SELECT erased_in_project_id FROM agent_forget_audit"
                                 " WHERE id = :id"), {"id": rec}).scalar_one()
        assert made_in == beta
    _alembic(url, "downgrade", _BEFORE)


def test_a_september_agent_erasure_takes_its_project_or_says_it_is_unknown(scratch, keyless):
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, default, beta = _account(c)
        found = _record(c, t, "agent", at, target="bot-1")
        _trail(c, t, beta, None, at + timedelta(seconds=2), "erase", "agents:delete")
        # No request on file: no door erases an agent in the whole account.
        lost = _record(c, t, "agent", at + timedelta(days=2), target="bot-2")
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        made_in = dict(c.execute(text("SELECT id, erased_in_project_id FROM agent_forget_audit"))
                       .all())
    assert made_in == {found: beta, lost: _NIL}
    _alembic(url, "downgrade", _BEFORE)
    with eng.connect() as c:
        assert c.execute(text("SELECT erased_in_project_id FROM agent_forget_audit"
                              " WHERE id = :id"), {"id": lost}).scalar_one() is None


def _account_door(c, t, user, ts):
    """The trail row of DELETE /admin/users/{id}/memories (0.1.40 on): no
    project, `scope` account, the end user as its record names them."""
    c.execute(text("INSERT INTO audit_events (id, ts, tenant_id, project_id, actor, action,"
                   " result, end_user_id, meta) VALUES (:id, :ts, :t, NULL, 'admin', 'erase',"
                   " 'ok', :u, CAST(:m AS json))"),
              {"id": uuid.uuid4(), "ts": ts, "t": t, "u": user,
               "m": '{"memories": 1, "facts": 0, "scope": "account"}'})


def _made_in(c, rid):
    return c.execute(text("SELECT erased_in_project_id FROM agent_forget_audit WHERE id = :id"),
                     {"id": rid}).scalar_one()


def test_an_account_wide_erasure_stays_the_whole_accounts(scratch, keyless):
    """The admin door's record, as 0.1.40 on files it: no project, and its
    own trail row of no project in the minute after it. An earlier fixture
    dated it 22 Sep 2026, before the door existed (audit before 1.0.0)."""
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, default, beta = _account(c)
        rec = _record(c, t, "user", at)
        _account_door(c, t, None, at + timedelta(seconds=1))
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        assert _made_in(c, rec) is None
    _alembic(url, "downgrade", _BEFORE)


# A restore's replay reached namesakes (audit before 1.0.0): an end user's
# record of 0.1.1-0.1.11 whose request row is not on file (a trail kept shorter
# than the record's age, a row written more than a minute after it) stayed
# account-wide, "as the admin door files them"; but the only door that wrote
# those records then was DELETE /v1/users/{id}/memories, confined to its key's
# project, and a restore repeated it in every project, erasing a namesake.

@pytest.mark.parametrize("trail", ["none", "account door's row"])
def test_a_september_erasure_with_no_request_on_file_is_not_recorded(scratch, keyed, trail):
    """No account-wide door existed in September: whatever the minute holds,
    the record was made in one project, which nothing says. Marked, so a
    restore counts it for an erasure by hand and erases no namesake."""
    from korely_agent.services.pseudonyms import pseudonym

    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, default, beta = _account(c)
        p = pseudonym(t, "acceptance+r6@korely.ai")
        rec = _record(c, t, "user", at, user=p)
        if trail != "none":
            _account_door(c, t, p, at + timedelta(seconds=1))
        # The control: a record whose request row is on file, in Beta.
        q = pseudonym(t, "acceptance+r6-ctrl@korely.ai")
        ctrl = _record(c, t, "user", at + timedelta(hours=1), user=q)
        _trail(c, t, beta, q, at + timedelta(hours=1, seconds=1), "erase", "users:delete")
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        assert (_made_in(c, rec), _made_in(c, ctrl)) == (_NIL, beta)
    _alembic(url, "downgrade", _BEFORE)


def test_a_late_upgrades_erasure_with_no_request_on_file_is_not_recorded(scratch, keyless):
    """An install that ran 0.1.11 past 17 Oct: a record of its /v1 door after
    that date, with no row of the account door next to it, is no account-wide
    erasure either."""
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, default, beta = _account(c)
        rec = _record(c, t, "user", at)
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        assert _made_in(c, rec) == _NIL
    _alembic(url, "downgrade", _BEFORE)


# A resolved September erasure hid the next request (audit before 1.0.0):
# the probe for a request's own record read the
# table as this pass was rewriting it, so a keyless record it had just
# placed in Beta looked like the next request's own record, and that
# request was dropped: the second record stayed account-wide and the second
# person's trail rows kept the id in clear.

@pytest.mark.parametrize("sibling", [True, False])
def test_a_record_placed_in_this_pass_is_no_other_requests_own(scratch, keyless, sibling):
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, default, beta = _account(c)
        if sibling:
            first = _record(c, t, "user", at)
            _trail(c, t, beta, "acceptance+r6-x@korely.ai", at + timedelta(seconds=1), "erase",
                   "users:delete")
        second = _record(c, t, "user", at + timedelta(seconds=59))
        wrote = _trail(c, t, beta, "acceptance+r6-y@korely.ai", at - timedelta(days=1), "write")
        erased = _trail(c, t, beta, "acceptance+r6-y@korely.ai", at + timedelta(seconds=61),
                        "erase", "users:delete")
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        assert _made_in(c, second) == beta, "the second request was taken for the first's"
        if sibling:
            assert _made_in(c, first) == beta
        left = c.execute(text("SELECT count(*) FROM audit_events WHERE id IN (:a, :b)"
                              " AND end_user_id IS NOT NULL"), {"a": wrote, "b": erased}).scalar_one()
        assert left == 0, "the second person's id stayed in clear in the trail"
    _alembic(url, "downgrade", _BEFORE)


def test_the_deliveries_about_a_deleted_project_say_the_default_one(scratch, keyless):
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    gone, erased = str(uuid.uuid4()), str(uuid.uuid4())
    with eng.begin() as c:
        t, default, beta = _account(c)
        ep = uuid.uuid4()
        c.execute(text("INSERT INTO agent_webhook_endpoints (id, tenant_id, url, secret, events)"
                       " VALUES (:id, :t, 'http://127.0.0.1:9/h', 'whsec_x', '[\"*\"]')"),
                  {"id": ep, "t": t})
        c.execute(text("INSERT INTO agent_forget_audit (id, tenant_id, scope, target_id)"
                       " VALUES (:id, :t, 'project', :g)"), {"id": uuid.uuid4(), "t": t, "g": erased})
        for about in (gone, erased, str(beta)):
            c.execute(text("INSERT INTO agent_webhook_deliveries (id, tenant_id, endpoint_id,"
                           " event_type, payload) VALUES (:id, :t, :e, 'memory.created',"
                           " CAST(:p AS json))"),
                      {"id": uuid.uuid4(), "t": t, "e": ep,
                       "p": '{"project_id": "%s", "data": {"user_id": "u"}}' % about})
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        about = sorted(r for (r,) in c.execute(text(
            "SELECT payload->>'project_id' FROM agent_webhook_deliveries")))
        flags = {f for (f,) in c.execute(text("SELECT erased_user FROM agent_webhook_deliveries"))}
    assert about == sorted([str(default), erased, str(beta)])
    assert flags == {False}
    _alembic(url, "downgrade", _BEFORE)


def test_a_keyless_september_erasure_with_a_delivered_webhook_upgrades(scratch, keyless):
    """Review of the audit of 27 Oct 2026: without a key the delivered
    webhook's id was set with jsonb_set(..., to_jsonb(NULL)), which is NULL,
    into the NOT NULL payload, and the upgrade failed (the container could not
    start)."""
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, default, beta = _account(c)
        _record(c, t, "user", at)
        _trail(c, t, beta, "sept-user", at + timedelta(seconds=1), "erase", "users:delete")
        ep = uuid.uuid4()
        c.execute(text("INSERT INTO agent_webhook_endpoints (id, tenant_id, url, secret, events)"
                       " VALUES (:id, :t, 'http://127.0.0.1:9/h', 'whsec_x', '[\"*\"]')"),
                  {"id": ep, "t": t})
        c.execute(text("INSERT INTO agent_webhook_deliveries (id, tenant_id, endpoint_id,"
                       " event_type, status, payload, created_at) VALUES (:id, :t, :e,"
                       " 'memory.created', 'delivered', CAST(:p AS json), :c)"),
                  {"id": uuid.uuid4(), "t": t, "e": ep, "c": at - timedelta(hours=1),
                   "p": '{"project_id": "%s", "data": {"user_id": "sept-user"}}' % beta})
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        payload = c.execute(text("SELECT payload::text FROM agent_webhook_deliveries")).scalar_one()
    assert "sept-user" not in payload and '"user_id": null' in payload
    _alembic(url, "downgrade", _BEFORE)


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "k" * 48)
    from korely_agent.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_an_account_wide_erasure_next_to_a_projects_own_stays_account_wide(scratch, keyed):
    """Review of the audit of 27 Oct 2026: an admin-door record (no project)
    and, within the minute, the same person's /v1 erasure in Beta with its own
    record: the account-wide record was given Beta, and a restore repeated it
    there only."""
    from korely_agent.services.pseudonyms import pseudonym

    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, default, beta = _account(c)
        p = pseudonym(t, "dana")
        admin = _record(c, t, "user", at, user=p)
        _account_door(c, t, p, at + timedelta(seconds=1))
        c.execute(text("INSERT INTO agent_forget_audit (id, tenant_id, project_id, end_user_id,"
                       " scope, created_at) VALUES (:id, :t, :p, :u, 'user', :c)"),
                  {"id": uuid.uuid4(), "t": t, "p": beta, "u": p, "c": at + timedelta(seconds=20)})
        _trail(c, t, beta, p, at + timedelta(seconds=21), "erase", "users:delete")
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        made_in = c.execute(text("SELECT erased_in_project_id FROM agent_forget_audit"
                                 " WHERE id = :id"), {"id": admin}).scalar_one()
    assert made_in is None, "an account-wide erasure was narrowed to one project"
    _alembic(url, "downgrade", _BEFORE)


# The review of the audit before 1.0.0: the account door's record kept its
# scope only with its trail row in the minute after it. The record's time is
# the start of the erasure's transaction and the row is written after its
# commit; an erasure may wait 60 s for a lock and run 900 s (core/threads.
# longer_deadlines). A row 90 s after the record marked it "not recorded", and
# a restore erased that person nowhere.

def _longest_seconds():
    from korely_agent.core import threads

    return (threads.LONG_LOCK_WAIT_SECONDS + threads.LONG_STATEMENT_SECONDS
            + threads.POOL_CHECKOUT_SECONDS)


@pytest.mark.parametrize("after,kept", [(30, True), (90, True), ("longest", True),
                                        ("past the longest", False)])
@pytest.mark.parametrize("key", ["keyed", "keyless"])
def test_a_slow_account_wide_erasure_stays_the_whole_accounts(scratch, request, key, after,
                                                              kept):
    """Its own row, as late as the longest an erasure takes: the whole
    account's. Past that, with nothing else on file, nothing says it is
    its door's."""
    from korely_agent.services.pseudonyms import pseudonym

    request.getfixturevalue(key)
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    late = {"longest": _longest_seconds(),
            "past the longest": _longest_seconds() + 60}.get(after, after)
    with eng.begin() as c:
        t, default, beta = _account(c)
        p = pseudonym(t, "acceptance+r6-slow@korely.ai")
        rec = _record(c, t, "user", at, user=p)
        _account_door(c, t, p, at + timedelta(seconds=late))
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        assert _made_in(c, rec) == (None if kept else _NIL)
    _alembic(url, "downgrade", _BEFORE)


def test_an_account_door_row_of_someone_else_is_not_the_records(scratch, keyed):
    """With a key the row names its person: another's, with nothing earlier
    on file, is no proof (an install that ran 0.1.11 past 17 Oct and erased
    someone else at the account door minutes after its upgrade)."""
    from korely_agent.services.pseudonyms import pseudonym

    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, default, beta = _account(c)
        rec = _record(c, t, "user", at, user=pseudonym(t, "acceptance+r6-one@korely.ai"))
        _account_door(c, t, pseudonym(t, "acceptance+r6-two@korely.ai"),
                      at + timedelta(seconds=30))
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        assert _made_in(c, rec) == _NIL
    _alembic(url, "downgrade", _BEFORE)


@pytest.mark.parametrize("mark", ["a record filed with its project", "a row of the account door"])
def test_an_account_wide_erasure_whose_row_is_lost_stays_the_whole_accounts(scratch, keyed, mark):
    """The trail never raises, so a row can be lost. A record of no project
    made once the install was past 0.1.11, which files the /v1 door's
    project, is the account door's: kept the whole account's. The same
    record with the mark only after it is an install that ran 0.1.11 late
    (test_a_late_upgrades_erasure_with_no_request_on_file_is_not_recorded)."""
    from korely_agent.services.pseudonyms import pseudonym

    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, default, beta = _account(c)
        p = pseudonym(t, "acceptance+r6-lost@korely.ai")
        before_it = _record(c, t, "user", at, user=p)
        q = pseudonym(t, "acceptance+r6-early@korely.ai")
        earlier = _record(c, t, "user", at - timedelta(days=2), user=q)
        when = at - timedelta(days=1)
        if mark == "a record filed with its project":
            c.execute(text("INSERT INTO agent_forget_audit (id, tenant_id, project_id, scope,"
                           " target_id, created_at) VALUES (:id, :t, :p, 'memory', 'mem_x', :c)"),
                      {"id": uuid.uuid4(), "t": t, "p": beta, "c": when})
        else:
            _account_door(c, t, pseudonym(t, "acceptance+r6-someone@korely.ai"), when)
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        assert _made_in(c, before_it) is None, "an account-wide erasure was marked not recorded"
        assert _made_in(c, earlier) == _NIL, "a record before the mark was widened"
    _alembic(url, "downgrade", _BEFORE)


def test_a_record_placed_before_a_downgrade_is_no_other_requests_own(scratch, keyless):
    """The review of the audit before 1.0.0: the pass told its own records by a
    list of their ids, and a record a first run had placed was not in it after
    a downgrade: it looked like the next request's own record."""
    url, eng = scratch
    _alembic(url, "upgrade", _BEFORE)
    at = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    with eng.begin() as c:
        t, default, beta = _account(c)
        first = _record(c, t, "user", at)
        _trail(c, t, beta, "acceptance+r6-x@korely.ai", at + timedelta(seconds=1), "erase",
               "users:delete")
        second = _record(c, t, "user", at + timedelta(seconds=59))
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        assert (_made_in(c, first), _made_in(c, second)) == (beta, _NIL)
    _alembic(url, "downgrade", _BEFORE)
    with eng.begin() as c:
        # The second request's row, found since (a trail restored apart).
        erased = _trail(c, t, beta, "acceptance+r6-y@korely.ai", at + timedelta(seconds=61),
                        "erase", "users:delete")
    _alembic(url, "upgrade", _THIS)
    with eng.connect() as c:
        assert _made_in(c, second) == beta, "the second request was taken for the first's"
        assert c.execute(text("SELECT end_user_id FROM audit_events WHERE id = :id"),
                         {"id": erased}).scalar_one() is None
    _alembic(url, "downgrade", _BEFORE)
