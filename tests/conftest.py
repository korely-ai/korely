"""Test fixtures.

One real Postgres, one schema created from the models, one transaction per test
rolled back at the end. No SQLite: this service ships on Postgres with pgvector
and a vector column has no SQLite equivalent, so testing elsewhere would mean
testing something else.
"""
import json
import os
import re
import socket
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Hermetic tests (2026-09-28). The development image installs `google-genai`
# with the rest of the product's requirements, so a test that sets a key and
# leaves one model call real reaches Google with it: money on every run, and a
# result that depends on somebody else's uptime. Two walls. The provider keys
# are emptied here, before anything imports the settings, so a key in the
# environment or in a `.env` next to the code (the repository is mounted as the
# working directory, and the settings read `.env` from there; an environment
# variable wins over it) cannot reach a test: every path takes its "no
# provider" branch unless the test sets a key itself. And the network is closed
# below to anything but this machine, for whatever does not ask the settings.
for _provider_key in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY"):
    os.environ[_provider_key] = ""
# The suite's embedder is Gemini with no key, so no vectors unless a test
# fakes them: the default since 0.1.19 is the local model, which a test run has
# no copy of, and the image built with it says local in its environment. The
# tests of the local embedder and of the default set it themselves.
os.environ["EMBEDDING_PROVIDER"] = "gemini"
# `make test` runs with no database and no DATABASE_URL, and the settings
# cannot be read without one: every unit test that built the app or read a
# setting was red there, 21 failures and 37 errors (audit of 17 Oct 2026).
# So a run without one gets an address where nothing answers: reading the
# settings works, and a test that does reach a database is refused at once
# and shows itself, instead of passing on whatever the first test cached in
# the process's engine (db/base.py). A run with a database keeps its own.
if not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "postgresql://nobody@127.0.0.1:1/nothing"

TEST_DB = os.getenv(
    "TEST_DATABASE_URL", "postgresql://korely:test@localhost:55433/korely_test"
)


# The fixtures that reach a database: the test database (`engine`, behind
# `db`), and the scratch databases the migration tests make for themselves.
# This mark alone decides the lane: no file marks itself whole (audit of
# 20 Oct 2026: a file's own mark kept its source-only guards out of `make test`;
# tests/test_the_unit_lane_runs_what_needs_no_database.py holds it).
_DATABASE_FIXTURES = {"engine", "scratch", "scratch_db"}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """A test that uses the test database is a `db` test, marked or not.

    Every way to it goes through the `engine` fixture below, so a test whose
    fixtures reach it is marked here, before `-m "not db"` deselects: a file
    that forgot its `pytestmark` (tests/test_user_scope.py, 25 tests) no
    longer turns `make test` red, and its tests of the rules alone still run
    there (audit of 17 Oct 2026)."""
    for item in items:
        if _DATABASE_FIXTURES & set(getattr(item, "fixturenames", ())) \
                and not item.get_closest_marker("db"):
            item.add_marker(pytest.mark.db)
        if not CLOUD and item.get_closest_marker("cloud"):
            item.add_marker(_NO_CLOUD)


# The Cloud's package (korely_agent/hosted/) is optional (tests/cloud.py): a
# test that needs it is marked `cloud` (a test, a whole file with
# `pytestmark`, or a case with `pytest.param(..., marks=pytest.mark.cloud)`),
# and skips where the package is not, since KORELY_MODE=hosted is refused
# there (core/extensions.cloud).
from tests.cloud import HERE as CLOUD  # noqa: E402

_NO_CLOUD = pytest.mark.skip(reason="needs the Cloud's package (korely_agent/hosted/), "
                                    "which this tree does not carry")

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "testserver"}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _host_of(address):
    if isinstance(address, tuple) and address:
        return address[0]
    return address


def _is_local(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode()
    # A unix socket is a path, not a host.
    return not isinstance(host, str) or host in _LOCAL_HOSTS or host.startswith("/")


@pytest.fixture(autouse=True)
def _no_external_network(request, monkeypatch):
    """Close the network to anything but this machine, unless the test is
    marked real_network (and then it runs only with RUN_REAL_NETWORK=1).

    A refused connection raises at once, so a code path that forgot to check
    for a provider fails loudly here instead of spending money and time on
    every run. Postgres is reached by libpq, below Python's socket module,
    and on localhost anyway; the webhook tests use local receivers."""
    if request.node.get_closest_marker("real_network"):
        if os.environ.get("RUN_REAL_NETWORK") != "1":
            pytest.skip("talks to a real external service: set RUN_REAL_NETWORK=1 to run it")
        yield
        return

    def guarded_connect(sock, address):
        if not _is_local(_host_of(address)):
            raise ConnectionRefusedError(
                f"hermetic tests: connection to {_host_of(address)!r} refused "
                f"(mark the test real_network)")
        return _real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not _is_local(_host_of(address)):
            raise ConnectionRefusedError(
                f"hermetic tests: connection to {_host_of(address)!r} refused "
                f"(mark the test real_network)")
        return _real_connect_ex(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_local(host):
            raise socket.gaierror(
                f"hermetic tests: lookup of {host!r} refused (mark the test real_network)")
        return _real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield


# The names RFC 2606 keeps for documentation, and a public address for them.
# The webhook tests register endpoints on these, and a webhook URL is accepted
# only if its host resolves to a public address (services/webhooks.check_url),
# so those tests asked a real resolver: they passed or failed on the DNS of
# whatever machine ran them. The address is example.com's long-standing one;
# the point is only that it is global.
_DOCUMENTATION_NAMES = {name: "93.184.216.34"
                        for name in ("example.com", "example.org", "example.net")}


@pytest.fixture
def public_dns(monkeypatch):
    """The documentation names resolve to a public address, without a
    resolver; every other name still goes through the closed network."""
    closed = socket.getaddrinfo

    def resolve(host, port=None, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name in _DOCUMENTATION_NAMES:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                     (_DOCUMENTATION_NAMES[name], port or 0))]
        return closed(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB, future=True)
    with eng.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    # From the package and not from db.base: the package imports every model,
    # which is what puts the tables on this metadata.
    from korely_agent.db import Base

    # Tables that left the models: a test database made before still holds
    # them, and their foreign keys to `tenants` stop drop_all (`agents` and
    # `agent_daily_usage`, 2026-10-06).
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS agents, agent_daily_usage CASCADE"))
    Base.metadata.drop_all(bind=eng)
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """A real session, with the tables emptied afterwards.

    The obvious fixture wraps each test in a transaction and rolls it back, and
    it is wrong here. The service layer commits: `write_fact_direct` persists,
    runs contradiction detection against what is now durable, and commits again.
    Under an outer transaction that sequence executes differently from the way
    it executes in production, so the fixture would be testing a program we do
    not ship. Truncating is slower and tests the real thing.
    """
    from korely_agent.db.base import Base

    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    try:
        yield s
    finally:
        # Roll back anything the test left open before closing: a session with a
        # live transaction still holds locks, and the TRUNCATE below would then
        # block or silently do nothing, leaking rows into the next test.
        s.rollback()
        s.close()
        with engine.begin() as c:
            names = ", ".join(f'"{t}"' for t in reversed(Base.metadata.sorted_tables))
            c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def tenant(db):
    from korely_agent.db.models import Tenant

    t = Tenant(id=uuid.uuid4(), name="Acme Insurance")
    db.add(t)
    # Committed, not flushed. The audit trail writes in its own session by
    # design, and a session that is not this one cannot see an uncommitted row:
    # every audit insert would fail its foreign key and be swallowed, which is
    # a test suite that passes while the feature does nothing.
    db.commit()
    return t


@pytest.fixture(autouse=True)
def _fresh_settings_and_no_worker(monkeypatch):
    """Every test starts from settings read now, with the worker off.

    `get_settings()` is cached for the process and `create_app` reads it. The
    fixtures set RUN_WORKER=false and then built a TestClient, which is right
    only while nothing has cached the settings yet: once an earlier test had,
    its `run_worker=True` won, and the worker started with the client. Its
    first drain then raced the rows the test had just seeded (49 tests did,
    counted 2026-09-28; tests/test_the_worker_stays_off.py).

    So the environment says no worker and the cache is emptied, before and
    after each test, and nothing a test sets leaks into the next one. A test
    that wants the worker sets RUN_WORKER=true and clears the cache itself.
    Reading the settings needs DATABASE_URL, and a run without a database
    has the placeholder set at the top of this file.
    """
    from korely_agent.config import get_settings

    monkeypatch.setenv("RUN_WORKER", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_round_outlives_its_test():
    """A worker's round leaves an attempt that runs long to end in its own
    thread and write its own outcome (core/threads.Lanes): the next test's
    tables must not receive it. Only when the modules are loaded."""
    import sys

    yield
    for name, attr in (("korely_agent.services.webhooks", "_sending"),
                       ("korely_agent.services.facts.drain", "_draining")):
        module = sys.modules.get(name)
        lanes = getattr(module, attr, None) if module is not None else None
        if lanes is not None:
            from tests.thread_helpers import wait_idle

            wait_idle(lanes, 30)


@pytest.fixture(autouse=True)
def _a_fresh_mcp_server_for_every_test():
    """Every test that builds the app gets an MCP transport that starts.

    The MCP server is one instance per process (mcp/server.py), which is
    right for the one app a process serves. Its session manager runs once per
    instance ("StreamableHTTPSessionManager .run() can only be called once
    per instance. Create a new instance if you need to run again", the
    library's own words), so the first TestClient of the suite started it and
    every later one logged "MCP session manager failed to start" and ran with
    no MCP transport at all: a request to /mcp in those tests would have
    failed, and nothing looked at it. A handful of fixtures reset the two
    singletons by hand; this does it for every test, before and after, so
    the next `create_app` builds a new instance, the way the library asks.
    Only when the module is loaded: importing it is not this fixture's job.
    """
    import sys

    def reset():
        server = sys.modules.get("korely_agent.mcp.server")
        if server is not None:
            server._agent_mcp_singleton = None
            server._agent_streamable_app_singleton = None

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _no_identity_from_the_test_before():
    """Every test starts and ends with no caller in the context variables.

    The MCP's identity (the tenant, the project, the scopes, the key, the end
    users a key may name; mcp/server.py), the region of a write in progress
    (core/llm.py), the model-usage scope (services/llm_usage.py), the OAuth
    caller's network (services/oauth.py) and the request's scope (main.py)
    live in ContextVars, and pytest runs every test in one context: a test
    that set the tenant outside a task of its own (tests/test_memory_engine.py
    did) handed it to every test after it (audit of 17 Oct 2026). Each one's
    default is None. Only when the module is loaded, as for the MCP server
    above."""
    import sys

    names = {
        "korely_agent.mcp.server": ("_agent_user_id", "_agent_project", "_agent_scopes",
                                    "_agent_key_id", "_agent_end_users"),
        "korely_agent.core.llm": ("_region",),
        "korely_agent.services.llm_usage": ("_active",),
        "korely_agent.services.oauth": ("NETWORK",),
        "korely_agent.main": ("_REQUEST",),
    }

    def reset():
        for module, variables in names.items():
            loaded = sys.modules.get(module)
            if loaded is not None:
                for name in variables:
                    var = getattr(loaded, name)
                    if var.get() is not None:
                        var.set(None)

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _the_minute_holds(monkeypatch):
    """The rate limiter's clock stands still for the test, half way through
    the minute it starts in.

    A test that counts calls against RATE_LIMIT_PER_MINUTE (or a hosted
    plan's minute, hour or day) and sees a 429 on the third failed whenever
    the minute turned between its calls: the counter started again at one
    (tests/test_mcp_scopes.py, audit of 17 Oct 2026). Held, not slept on: the
    windows are still the limiter's own buckets of that instant."""
    from datetime import datetime, timezone

    from korely_agent.services import rate_limit

    held = datetime.now(timezone.utc).replace(second=30, microsecond=0)
    monkeypatch.setattr(rate_limit, "_now", lambda: held)
    yield held


@pytest.fixture(autouse=True)
def _the_core_as_it_ships():
    """Every test starts and ends with no hook of the hosted service.

    `hosted.install` registers its hooks in the core (core/extensions.py) and
    its account events in the webhooks (`quota.warning`), and they are the
    process's, not the app's: only the next `create_app` took them away. A
    test that built a hosted app last (tests/test_hosted_mode.py) left the
    Cloud's spending brake on every model call after it until some test built
    an app again, so tests/test_llm.py run right after it failed five tests
    on "no price for mistral-small-latest in LLM_RATES" (audit of 17 Oct
    2026). The whole suite was green only by the alphabetical order of its
    files. Only when the modules are loaded, as for the MCP server above."""
    import sys

    def reset():
        extensions = sys.modules.get("korely_agent.core.extensions")
        if extensions is not None:
            extensions.reset()
        webhooks = sys.modules.get("korely_agent.services.webhooks")
        if webhooks is not None:
            webhooks.reset_events()

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _the_thresholds_start_from_the_default():
    """memory_core keeps, for the process, which embedding model makes the
    vectors (`thresholds.use_model`, 2.4.0): `create_app` sets it, and so do the
    tests of the thresholds. A test must not hand its model to the next one."""
    from korely_agent.services.memory_core import thresholds

    yield
    thresholds.use_model("gemini-embedding-2")


@pytest.fixture(autouse=True)
def _no_provider_leaks_between_tests():
    """Drop the memoized provider around every test.

    `get_provider()` caches, deliberately: constructing a client per write would
    pay for a TLS handshake on the write path. The cache outlives the
    environment variable that filled it, so one test setting GOOGLE_API_KEY to a
    placeholder left a real Gemini client installed for every test that ran
    after it, and those tests then made actual calls to Google with an invalid
    key.

    Nobody noticed because the two calls that reached it swallowed their
    exceptions. The moment they stopped swallowing, eight unrelated tests turned
    red at setup and the suite showed what it had been doing all along. A test
    suite that reaches the network is not slow, it is untrustworthy: it passes
    or fails on somebody else's uptime.
    """
    from korely_agent.core import llm

    llm.reset_provider_cache()
    yield
    llm.reset_provider_cache()


def seeded_project_id(db, tenant_id):
    """The project a row seeded straight into the database belongs to.

    Since keys are bound to the account's default project when they are made,
    a row put in the database with no project is a row no key can see. That is
    correct behaviour and it makes the obvious fixture wrong: seeding with no
    project models an install that cannot exist any more.

    So a fixture that writes rows by hand asks for this and passes it, which is
    what a write through the API would have stamped anyway.
    """
    from korely_agent.services import projects as project_service

    return project_service.get_or_create_default_project(db, tenant_id).id


def stub_extraction(monkeypatch, module, facts, observation=None):
    """Answer the write's extraction the way `facts`, a stand-in for the
    model's facts, would: called with the memory's text and the same
    keywords, returning its facts or raising. Since memory_core 2.6.0 the
    write asks for the facts and the observation in one call
    (`extract_facts_and_observation`); `observation` is what that call gives
    besides, None when the model gave no such field."""
    from korely_agent.services.memory_core.fact_extractor import Extraction

    def extraction(content, **kw):
        kw.pop("previous", None)
        kw.pop("date", None)
        return Extraction(list(facts(content, **kw)), observation)

    monkeypatch.setattr(module, "extract_facts_and_observation", extraction)


# The contradiction judge as a scripted model meets it (2026-10-07). One fact is
# asked with "EXISTING facts:" and answers {"contradictions": [...]}; a write
# judged in one call (`_BATCHED_JUDGE`, on by default as on the hosted product)
# is asked in blocks "[new_id=...] NEW: ... candidates:" and answers
# {"results": [{"new_id": ..., "contradictions": [...]}]}, every new_id present.
_NEW_ID = re.compile(r"\[new_id=([^\]]+)\] NEW: ")


def is_the_judge(prompt: str) -> bool:
    return "EXISTING facts:" in prompt or "[new_id=" in prompt


def judge_reply(prompt: str, contradictions_for) -> str:
    """The judge's answer in the shape `prompt` asks. `contradictions_for(text)`
    gets the candidates' lines of one new fact and returns its list of
    {"existing_id", "reason"}."""
    if "[new_id=" not in prompt:
        existing = prompt.split("EXISTING facts:", 1)[1].split("NEW fact", 1)[0]
        return json.dumps({"contradictions": contradictions_for(existing)})
    parts = _NEW_ID.split(prompt)
    results = [{"new_id": parts[i], "contradictions": contradictions_for(parts[i + 1])}
               for i in range(1, len(parts) - 1, 2)]
    return json.dumps({"results": results})


def in_the_judges_shape(prompt: str, answer):
    """A one-fact verdict, `{"contradictions": [...]}`, given to every new fact
    of a batch, which keeps only the ids among its own candidates. Anything
    that is not such a verdict (an exception, an empty or broken answer)
    passes as it is: a failure stays a failure in both shapes."""
    if "[new_id=" not in prompt or not isinstance(answer, str):
        return answer
    try:
        verdict = json.loads(answer)
    except ValueError:
        return answer
    if not isinstance(verdict, dict) or not isinstance(verdict.get("contradictions"), list):
        return answer
    return judge_reply(prompt, lambda _text: verdict["contradictions"])
