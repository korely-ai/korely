"""Every resource the accounts share is made, capped and timed in one place.

Audit of 20 Oct 2026: each audit found one more resource of
the one process that one account could hold while every other waited, the
loop's pool, the embedder's slot, the MCP's door, the request threads of a
webhook's lookup, a row lock, the rate-limit bucket, a worker's round, and
each fix closed the resource it was shown. So core/threads.py is where a
pool, a semaphore, a lock, a condition, a thread, a task, a socket, the
engines and the database's deadlines are made, with their per-account caps
and their waits, and this file fails on one made anywhere else in
korely_agent.

Audit of 27 Oct 2026: the scan read names as they are written,
so `from threading import Thread`, `import threading as t`, a Lock, a Timer,
a multiprocessing Pool, `asyncio.create_task`, a socket and a deadline
written as `"lock_" + "timeout"` were all missed, and a process-wide lock
taken with no deadline on a request's thread (services/export_md) sat
outside. Now every name is resolved through the module's imports, the
makers are the whole family, and a string is read once its constant pieces
are joined. And a request held one pooled connection while the trail asked
the same pool for a second: a session of the main pool is opened
only by a unit of work that holds no other (`UNITS_OF_WORK`, by qualified
name; any reference to get_sessionmaker, get_engine or get_own_engine counts,
under whatever name or alias it is imported, and so does a connection taken
from an engine the scan cannot name, `x.connect()` or `x.raw_connection()`:
the review of 13 Nov 2026 found the facts drain's holder asking the tick's
own session's engine, `db.get_bind()`, for a second connection), and every
session opened while another may be held comes from the own sessions' pool
(db/base.get_own_sessionmaker), at most one per function.

Read as a syntax tree, not as text: a comment or a docstring that names
`ThreadPoolExecutor` is not one. What a scan of the source cannot see: a
name reached through `getattr` or `importlib`, a string built at run time
from non-constant pieces, a sync route that blocks on the network through a
library with no timeout of its own, whether a new wait is fair among
accounts, the order locks are taken in, whether a lock is held across a wait
(each ALLOWED lock says why it is not), a unit of work in `UNITS_OF_WORK`
that is called from inside another, an own session opened by a function
called while its caller holds another own session (the scan reads one
function at a time, not who calls whom; each opener today holds its session
over its own statements and calls no other opener meanwhile), and a loop
that waits for its slowest job without a pool. Those stay the review's.
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests import cloud

PACKAGE = Path(__file__).resolve().parent.parent / "korely_agent"
THE_PLACE = "core/threads.py"

# What makes a shared resource: the call's name once the module's imports
# are resolved (`import threading as t; t.Lock()` is threading.Lock).
_MAKERS = {
    "concurrent.futures.ThreadPoolExecutor", "concurrent.futures.ProcessPoolExecutor",
    "threading.Thread", "threading.Timer", "threading.Lock", "threading.RLock",
    "threading.Semaphore", "threading.BoundedSemaphore", "threading.Condition",
    "threading.Event", "threading.Barrier",
    "_thread.start_new_thread", "_thread.allocate_lock",
    "asyncio.Semaphore", "asyncio.BoundedSemaphore", "asyncio.Condition", "asyncio.Lock",
    "asyncio.Event", "asyncio.Queue", "asyncio.to_thread", "asyncio.create_task",
    "asyncio.ensure_future", "asyncio.new_event_loop", "asyncio.run_coroutine_threadsafe",
    "anyio.to_thread.run_sync", "anyio.CapacityLimiter", "anyio.create_task_group",
    "anyio.Lock", "anyio.Semaphore", "anyio.Condition", "anyio.Event",
    "multiprocessing.Pool", "multiprocessing.Process", "multiprocessing.pool.ThreadPool",
    "multiprocessing.Lock", "multiprocessing.Semaphore",
    "queue.Queue", "queue.SimpleQueue", "queue.LifoQueue", "queue.PriorityQueue",
    "sqlalchemy.create_engine", "sqlalchemy.orm.sessionmaker", "sqlalchemy.orm.Session",
    "psycopg2.connect", "psycopg.connect",
    "socket.socket", "socket.create_connection", "socket.create_server",
    "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
    # A pool of connections to other machines (30 Oct 2026: the webhooks'
    # sender moved to httpcore and the scan did not know it).
    "httpcore.ConnectionPool",
    # A process: its memory and its CPU are the machine's, shared by every
    # account (1 Nov 2026).
    "subprocess.Popen", "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput",
    "os.fork", "os.forkpty", "os.posix_spawn", "os.posix_spawnp", "os.system", "os.popen",
    "multiprocessing.get_context",
}
# A method call that is one whatever its object (`loop.run_in_executor`).
_METHODS = {"run_in_executor", "create_task"}
# Words of a database deadline, in any string, once its constant pieces are joined.
_DEADLINES = ("lock_timeout", "statement_timeout", "idle_in_transaction_session_timeout")

# One line per exception, and why it is not core/threads.py's.
ALLOWED = {
    ("core/proxies.py", "concurrent.futures.ThreadPoolExecutor"):
        "one thread per process for the lookups of the trusted proxy's name, one at "
        "a time and at most every LOOKUP_EVERY_S, so a resolver that hangs holds "
        "it alone; only the process's first lookup is waited for, LOOKUP_TIMEOUT_S",
    ("core/proxies.py", "socket.getaddrinfo"):
        "the trusted proxy's name, on that one thread, at most every LOOKUP_EVERY_S "
        "for the whole process and never for an account: no request waits on it "
        "but the process's first, LOOKUP_TIMEOUT_S",
    ("core/proxies.py", "threading.Lock"):
        "the names' last answers, held around a dict and the reader's rebuild, "
        "never across the lookup",
    ("core/local_embedder.py", "threading.Condition"):
        "the embedder's fair queue; its wait is core/threads' WAIT_SECONDS and "
        "on_a_request, and who may come to it core/threads.at_the_embedder, an "
        "account's CALLS_PER_ACCOUNT places (checked below)",
    ("core/local_embedder.py", "threading.Thread"):
        "the warm-up: one thread at the process's start, nobody's request",
    ("hosted/spending.py", "threading.Thread"):
        "an alert mail to the operator, one per alert, holding no account's resource",
    ("hosted/spending.py", "threading.Lock"):
        "the day's spend cache and the alerts sent: held around a dict, never "
        "across the sum's query (`_cached` computes outside it)",
    ("db/base.py", "sqlalchemy.create_engine"):
        "the two engines, with core/threads.engine_pool / own_engine_pool and "
        "with_deadlines (checked below)",
    ("db/base.py", "sqlalchemy.orm.sessionmaker"):
        "the sessionmakers of those two engines",
    ("hosted/api_identity.py", "threading.Lock"):
        "the sign-ins per network this hour: held around a dict",
    ("hosted/firebase.py", "threading.Lock"):
        "the certificates' cache, held around a dict; and the single flight of "
        "their download, held for one fetch at most (CERTS_TIMEOUT_SECONDS), "
        "whose outcome every waiter takes",
    ("services/keys.py", "threading.Lock"):
        "the unknown keys' cache: held around an OrderedDict",
    ("services/bookkeeping.py", "threading.Lock"):
        "the counters /health reads: held around integers",
    ("services/facts/drain.py", "threading.Lock"):
        "the drain's pause by region: held around a dict and a clock",
    ("services/holder.py", "threading.Lock"):
        "one process's key as the holder of its claims: held by the facts tick's "
        "thread around the stamp of its claim, and once at the stop around the "
        "giving back; never on a request's thread",
    ("core/llm.py", "threading.RLock"):
        "the providers' memo: held around dicts and a client's construction; the "
        "install_settings reads it is built from run before it is taken (checked below)",
    ("services/export_md.py", "threading.Lock"):
        "one export at a time, and the owed tenants' set: a request waits for the "
        "first at most LOCK_WAIT_SECONDS (core/threads.taken), then leaves its files "
        "to the export under way, which writes them before it ends (checked below)",
    ("worker.py", "asyncio.Event"):
        "the worker's stop signal, one per process",
    ("main.py", "asyncio.Event"):
        "the in-process worker's stop signal, one per process",
    ("main.py", "asyncio.create_task"):
        "the in-process worker's loop, started once at the app's start",
    ("services/webhooks.py", "httpcore.ConnectionPool"):
        "one pool per delivery attempt (`_post`), closed when the attempt ends; "
        "its every connection, TLS handshake, read and write ends by the attempt's "
        "deadline (`AttemptBackend`, `Deadline`), and the attempts run in "
        "core/threads.Lanes (30 Oct 2026)",
    # A part of the product's (core/extensions.Part).
    **cloud.of_the_parts("SHARED_ALLOWED"),
}

# Who reaches the main pool (db.get_sessionmaker, db.get_engine,
# db.get_own_engine, by any name or alias): a unit of work that holds no
# other connection while it does, one line each, by its qualified name. Every
# other session opened while one may be held is an own session
# (db.get_own_sessionmaker()), from a pool the main pool's holders never
# wait on, so no two of them wait on each other.
UNITS_OF_WORK = {
    ("worker.py", "_run"): "a worker job's tick, on its own thread",
    ("manage.py", "_session"): "the command line, one command",
    ("hosted/cli.py", "main"): "the Cloud's command line, one command",
    ("main.py", "_extraction_now"): "/health's read, on the loop's thread pool",
    ("main.py", "_writer_account.look"): "the pre-route key lookup, an admission job",
    ("main.py", "_ThePartsCallers._served.look"):
        "the pre-route lookup of a gated part's caller, an admission job",
    ("mcp/server.py", "_look_up_key"): "the MCP's key check, an admission job",
    ("mcp/server.py", "_count_the_call"): "the MCP's per-call counter, an admission job",
    ("mcp/server.py", "_count_the_opening"): "the MCP session's counter, an admission job",
    ("mcp/server.py", "_register_add.korely_add.run"): "a tool's body: only this session",
    ("mcp/server.py", "_register_get_context.korely_get_context.run"):
        "a tool's body: only this session",
    ("mcp/server.py", "_register_get_facts.korely_get_facts.run"): "a tool's body: only this session",
    ("mcp/server.py", "answer_search.run"): "a tool's body: only this session",
    ("mcp/server.py", "answer_search.checked"):
        "a read tool's checks before its turn at the embedder: only this session, closed "
        "before the wait",
    ("mcp/server.py", "_register_get_context.korely_get_context.checked"):
        "a read tool's checks before its turn at the embedder: only this session, closed "
        "before the wait",
    ("mcp/server.py", "_register_correct_fact.korely_correct_fact.run"):
        "a tool's body: only this session",
    ("mcp/server.py", "_register_forget_memory.korely_forget_memory.run"):
        "a tool's body: only this session",
    ("services/oauth.py", "_session"): "OAuth's doors, admission jobs, each with this session only",
    ("services/vectors.py", "refuse_vectors_of_another_model"): "the start's check",
    ("services/webhooks.py", "_send_one"):
        "a delivery attempt in core/threads.Lanes; the round committed before it "
        "started (webhooks._to_send), so it holds no connection meanwhile",
    ("services/facts/drain.py", "_run_group_in_own_session"):
        "a drain group in core/threads.Lanes; the tick committed its claim "
        "(drain._claim) before the groups start",
    ("services/holder.py", "Holder._take"):
        "the process's lock connection, asked for by a tick that holds a connection "
        "of the main pool: so it comes from the own sessions' engine, whose holders "
        "never wait on the main pool, and is detached at once, no pool's afterwards",
    # A part of the product's (core/extensions.Part).
    **cloud.of_the_parts("UNITS_OF_WORK"),
}


def _imports(tree) -> dict:
    """{local name: what it stands for}, from the module's imports."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    head = a.name.split(".", 1)[0]
                    out.setdefault(head, head)
        elif isinstance(node, ast.ImportFrom):
            # A relative import by the module it names inside the package
            # (`from ..db import x` is db.x, `from .. import db` is db).
            module = (node.module or "").removeprefix("korely_agent.")
            if node.module == "korely_agent":
                module = ""
            for a in node.names:
                out[a.asname or a.name] = f"{module}.{a.name}" if module else a.name
    return out


def _dotted(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else node.attr
    return ""


def _resolved(name: str, imports: dict) -> str:
    head, _, rest = name.partition(".")
    full = imports.get(head, head)
    return f"{full}.{rest}" if rest else full


# Re-exports the makers are reached through (`sqlalchemy.orm.sessionmaker`
# is `sqlalchemy.orm.session.sessionmaker`, `from concurrent import futures`).
_ALIASES = {"sqlalchemy.orm.session.sessionmaker": "sqlalchemy.orm.sessionmaker",
            "sqlalchemy.orm.session.Session": "sqlalchemy.orm.Session",
            "sqlalchemy.engine.create_engine": "sqlalchemy.create_engine",
            "concurrent.futures.thread.ThreadPoolExecutor": "concurrent.futures.ThreadPoolExecutor"}


def _joined(node):
    """The string a `+` of constant strings makes, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _joined(node.left), _joined(node.right)
        if left is not None and right is not None:
            return left + right
    return None


# What reaches the main pool (a session of it, the engine itself), and what
# opens an own session, by the db package's name once imports are resolved.
_MAIN_POOL = {"get_sessionmaker", "get_engine", "get_own_engine"}
_OWN = {"get_own_sessionmaker"}
# A connection taken from an engine whatever it is (SQLAlchemy's Engine and
# Pool): `db.get_bind().connect()` is the main pool's when the session is,
# and nothing in its text says so; `begin()` takes one too, with its
# transaction (the review of 13 Nov 2026). Called with no argument, which
# tells it from a socket's or a driver's `connect(address)`. A session's or
# a connection's own `begin()` reads the same and is counted too: the
# package calls none (`begin_nested` is another name).
_CHECKOUTS = {"connect", "begin", "raw_connection"}


def _of_the_db(resolved: str, names: set):
    """The name of `names` that `resolved` reaches in the db package, or None:
    `db.get_sessionmaker`, `db.base.get_engine`, or the bare name."""
    head, _, last = resolved.rpartition(".")
    if last in names and (not head or head.rsplit(".", 1)[-1] in ("db", "base")):
        return last
    return None


def _names_the_db(node, imports) -> bool:
    """`node` is one of the db package's engines or sessionmakers, or a call
    of one (`get_engine()`): the scan counts it by that name already."""
    if isinstance(node, ast.Call):
        node = node.func
    written = _dotted(node)
    resolved = _resolved(written, imports) if written else ""
    return bool(written) and bool(_of_the_db(resolved, _MAIN_POOL | _OWN))


def _scan(source: str):
    """[(maker, line)], [(qualified function, line, name) of each reference to
    the main pool], [(qualified function, line) of each own session opened]."""
    tree = ast.parse(source)
    imports = _imports(tree)
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr)
                  and isinstance(n.body[0].value, ast.Constant)}
    makers, sessions, own = [], [], []

    def visit(node, function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            function = f"{function}.{node.name}" if function else node.name
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            written = _dotted(node)
            if written:
                resolved = _resolved(written, imports)
                hit = _of_the_db(resolved, _MAIN_POOL)
                if hit:
                    sessions.append((function, node.lineno, hit))
                elif _of_the_db(resolved, _OWN):
                    own.append((function, node.lineno))
                if isinstance(node, ast.Attribute) and (hit or _of_the_db(resolved, _OWN)):
                    return      # its inner names are not read again
        if isinstance(node, ast.Call):
            written = _dotted(node.func)
            name = _resolved(written, imports) if written else ""
            name = _ALIASES.get(name, name)
            if (isinstance(node.func, ast.Attribute) and node.func.attr in _CHECKOUTS
                    and not node.args and not _names_the_db(node.func.value, imports)):
                sessions.append((function, node.lineno, node.func.attr))
            if name in _MAKERS:
                makers.append((name, node.lineno))
            elif written.rsplit(".", 1)[-1] in _METHODS and "." in written:
                makers.append((written.rsplit(".", 1)[-1], node.lineno))
        if isinstance(node, ast.BinOp) or (isinstance(node, ast.Constant)
                                           and id(node) not in docstrings):
            text = _joined(node)
            if text is not None:
                for word in _DEADLINES:
                    if word in text:
                        makers.append((word, node.lineno))
                return     # the pieces of a joined string are not read again
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, None)
    return makers, sessions, own


def _found():
    """{(file, maker): [lines]} over korely_agent, core/threads.py aside."""
    out: dict = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel == THE_PLACE:
            continue
        for maker, line in _scan(path.read_text(encoding="utf-8"))[0]:
            out.setdefault((rel, maker), []).append(line)
    return out


def _sessions_opened():
    """{(file, qualified function): [lines]} of every reference to the main
    pool (get_sessionmaker, get_engine, get_own_engine), db/base.py aside."""
    out: dict = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel == "db/base.py":
            continue
        for function, line, _name in _scan(path.read_text(encoding="utf-8"))[1]:
            out.setdefault((rel, function), []).append(line)
    return out


def _own_sessions_opened():
    """{(file, qualified function): [lines]} of every own session opened."""
    out: dict = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel == "db/base.py":
            continue
        for function, line in _scan(path.read_text(encoding="utf-8"))[2]:
            out.setdefault((rel, function), []).append(line)
    return out


def test_no_shared_resource_is_made_outside_the_one_place():
    found = _found()
    stray = {k: v for k, v in found.items() if k not in ALLOWED}
    assert not stray, (
        "a pool, a semaphore, a lock, a condition, a thread, a task, a socket, an "
        "engine, a name lookup or a database deadline made outside core/threads.py: "
        "make it there, with its per-account cap and its wait, or add one line to "
        f"ALLOWED saying why it is nobody's resource: {stray}")


def test_every_exception_is_still_there():
    found = _found()
    stale = [k for k in cloud.in_this_tree(ALLOWED) if k not in found]
    assert not stale, f"ALLOWED names what the code no longer makes: {stale}"


def test_a_session_of_the_main_pool_is_opened_only_by_a_unit_of_work():
    opened = _sessions_opened()
    stray = {k: v for k, v in opened.items() if k not in UNITS_OF_WORK}
    assert not stray, (
        "a session of the main pool opened where the caller may hold another "
        "connection of it: with every connection held by such callers each waits "
        "for a second one until the checkout gives up (audit of 27 Oct 2026). Open it with "
        "db.get_own_sessionmaker(), or add one line to UNITS_OF_WORK saying why "
        f"nothing else is held: {stray}")
    stale = [k for k in cloud.in_this_tree(UNITS_OF_WORK) if k not in opened]
    assert not stale, f"UNITS_OF_WORK names what the code no longer opens: {stale}"


def test_an_own_session_is_never_opened_inside_another():
    """The own sessions' holders ask for nothing else while they hold one
    (core/threads.OWN_POOL_SIZE): one that opened a second would wait on its
    own pool, and with the pool full on itself. Held here within a function:
    each opens one own session at most. Across calls, see the docstring."""
    twice = {k: v for k, v in _own_sessions_opened().items() if len(v) > 1}
    assert not twice, f"a function that opens two own sessions: {twice}"


def test_the_scan_sees_what_it_missed_before():
    """The shapes the audit of 27 Oct 2026 found missed, each seen now."""
    probes = {
        "from threading import Thread\nThread(target=f)": "threading.Thread",
        "from threading import Condition as C\nC()": "threading.Condition",
        "from asyncio import Condition\nCondition()": "asyncio.Condition",
        "import threading as t\nt.Thread()": "threading.Thread",
        "import threading\nthreading.Timer(1, f)": "threading.Timer",
        "import multiprocessing\nmultiprocessing.Pool(2)": "multiprocessing.Pool",
        "import threading\nthreading.Lock()": "threading.Lock",
        "import asyncio\nasyncio.create_task(c())": "asyncio.create_task",
        "loop.create_task(c())": "create_task",
        "import socket\nsocket.create_connection(('h', 1))": "socket.create_connection",
        "x = 'SET LOCAL lock_' + 'timeout = 1'": "lock_timeout",
        "from sqlalchemy.orm import Session\nSession(bind=e)": "sqlalchemy.orm.Session",
    }
    for source, maker in probes.items():
        assert maker in {m for m, _l in _scan(source)[0]}, (source, _scan(source))
    assert _scan("def f(db):\n    get_sessionmaker()()")[1] == [("f", 2, "get_sessionmaker")]
    # The review's variants of the main pool reached,
    # each seen, by the qualified name of the function it is in.
    pool = {
        "from ..db import get_sessionmaker as gs\ndef f():\n    return gs()()":
            ("f", "get_sessionmaker"),
        "from .. import db as d\ndef f():\n    maker = d.get_sessionmaker\n    return maker()()":
            ("f", "get_sessionmaker"),
        "from ..db import get_engine\ndef f(db):\n    with get_engine().connect() as c:\n"
        "        return c": ("f", "get_engine"),
        "from korely_agent.db.base import get_own_engine\ndef f():\n    get_own_engine()":
            ("f", "get_own_engine"),
        "from ..db import get_sessionmaker\ndef outer(db):\n    def run():\n"
        "        return get_sessionmaker()()\n    return run()": ("outer.run", "get_sessionmaker"),
        "from ..db import get_sessionmaker\nclass K:\n    def m(self):\n"
        "        get_sessionmaker()()": ("K.m", "get_sessionmaker"),
        # A connection asked of an engine the text does not name (13 Nov 2026).
        "def f(db):\n    bind = db.get_bind()\n    return bind.connect()": ("f", "connect"),
        "class H:\n    def take(self, engine):\n        return engine.raw_connection()":
            ("H.take", "raw_connection"),
        "def f(db):\n    with db.get_bind().begin() as c:\n        return c": ("f", "begin"),
    }
    for source, (function, name) in pool.items():
        assert [(f, n) for f, _l, n in _scan(source)[1]] == [(function, name)], source
    # A socket's or a driver's connect names its address: not a pool's.
    assert _scan("def f(s):\n    s.connect(('h', 1))")[1] == []
    twice = ("from ..db import get_own_sessionmaker\ndef f():\n"
             "    with get_own_sessionmaker()() as a:\n"
             "        with get_own_sessionmaker()() as b:\n            return a, b")
    assert [f for f, _l in _scan(twice)[2]] == ["f", "f"]
    assert not _scan('def f():\n    "a docstring naming threading.Lock() and lock_timeout"')[0]


def test_every_outbound_call_has_its_timeout():
    """A call to another machine on a request's thread is waited for as long
    as its timeout says, so each one says it (a missing one is urllib's
    forever).

    httpcore takes its timeouts per request (`extensions={"timeout": ...}`),
    not on the pool, so a `timeout` keyword does not fit it: its pool is
    asked for the `network_backend` that ends every operation by a deadline
    (webhooks.AttemptBackend; 30 Oct 2026)."""
    calls = {"urllib.request.urlopen", "httpx.Client", "httpx.AsyncClient", "httpx.get",
             "httpx.post", "httpx.put", "httpx.delete", "httpx.request", "httpx.stream"}
    with_backend = {"httpcore.ConnectionPool"}
    missing = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = _imports(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _resolved(_dotted(node.func), imports)
            keywords = {k.arg for k in node.keywords}
            if (name in calls and "timeout" not in keywords) or (
                    name in with_backend and "network_backend" not in keywords):
                missing.append((path.relative_to(PACKAGE).as_posix(), node.lineno))
    assert not missing, f"an outbound call with no timeout of its own: {missing}"


def _calls_in(rel: str) -> set:
    tree = ast.parse((PACKAGE / rel).read_text(encoding="utf-8"))
    return {_dotted(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}


def test_the_engines_take_their_pools_and_their_deadlines_from_the_one_place():
    calls = _calls_in("db/base.py")
    assert {"threads.engine_pool", "threads.own_engine_pool",
            "threads.with_deadlines"} <= calls, calls


def test_the_embedders_queue_takes_its_bounds_from_the_one_place():
    tree = ast.parse((PACKAGE / "core/local_embedder.py").read_text(encoding="utf-8"))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
             and n.module == "threads" for a in n.names}
    assert {"CALLS_PER_ACCOUNT", "WAIT_SECONDS", "on_a_request", "at_the_embedder"} <= names, names


def test_every_lock_a_request_takes_has_its_deadline():
    """The lock-taking paths (FOR UPDATE, advisory locks, the upserts) are
    many and each is a decision of its own; none sets its deadline, because
    the engine gives every transaction a request begins one
    (threads.with_deadlines). Held here: a request's transaction on the real
    engine runs with lock_timeout, the worker's without."""
    import contextvars

    from sqlalchemy import create_engine, event

    from korely_agent.core import threads

    seen = []

    class _Conn:
        dialect = type("D", (), {"name": "postgresql"})()

        def exec_driver_sql(self, sql):
            seen.append(sql)

    engine = create_engine("postgresql://nobody@127.0.0.1:1/nothing")
    threads.with_deadlines(engine)
    assert event.contains(engine, "begin", engine.dispatch.begin.listeners[0])
    listener = engine.dispatch.begin.listeners[0]

    def as_a_request():
        threads.serving_a_request()
        listener(_Conn())

    contextvars.copy_context().run(as_a_request)
    contextvars.copy_context().run(lambda: listener(_Conn()))
    assert len(seen) == 1 and "lock_timeout" in seen[0] and "statement_timeout" in seen[0]


def test_a_settings_read_holds_no_lock_every_model_call_takes(monkeypatch):
    """Audit of 27 Oct 2026: core/llm held its memo's lock across
    model_choice.read_writers, a read of the database that waits for a
    connection of the pool; every embedding client and every provider lookup
    of the process waited behind it. The read blocks here until the test
    lets it go: another caller of the lock must not wait for it."""
    import threading

    from korely_agent.core import llm
    from korely_agent.services import model_choice

    entered, release = threading.Event(), threading.Event()

    def slow_read():
        entered.set()
        assert release.wait(10)
        return {}

    monkeypatch.setattr(model_choice, "read_writers", slow_read)
    monkeypatch.setattr(llm, "_gemini_client", lambda key: object())
    monkeypatch.setattr(llm, "_next_check", float("inf"))
    llm.reset_provider_cache()
    monkeypatch.setattr(llm, "_next_check", float("inf"))
    reader = threading.Thread(target=llm.screen_writers)
    reader.start()
    try:
        assert entered.wait(5)
        other = threading.Thread(target=llm._embed_client, args=("AIza-probe",))
        other.start()
        other.join(2)
        assert not other.is_alive(), "an embedding client waited behind a settings read"
    finally:
        release.set()
        reader.join(5)
        llm.reset_provider_cache()


def test_an_erasure_waits_for_the_export_lock_no_longer_than_for_a_row(monkeypatch, tmp_path):
    """Audit of 27 Oct 2026: services/export_md's one-at-a-time lock was
    taken with a plain `with` by the request that erased (after_an_erasure)
    while the worker's pass held it for a whole walk of every tenant: the
    request waited with no end, holding its thread and its connection. It
    waits a row lock's wait now (core/threads.taken), then leaves the files
    to the pass."""
    import contextvars
    import threading
    import uuid

    from korely_agent.config import get_settings
    from korely_agent.core import threads
    from korely_agent.services import export_md

    monkeypatch.setattr(threads, "LOCK_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(export_md, "_owed", set())
    monkeypatch.setenv("FACTS_EXPORT_DIR", str(tmp_path))
    get_settings.cache_clear()
    done = threading.Event()

    def the_erasures_request():
        threads.serving_a_request()
        export_md.after_an_erasure(None, uuid.uuid4())
        done.set()

    export_md._ONE_AT_A_TIME.acquire()     # the worker's pass, walking every tenant
    request = threading.Thread(target=contextvars.copy_context().run,
                               args=(the_erasures_request,), daemon=True)
    try:
        request.start()
        assert done.wait(3), "the erasure's request waited for the whole export pass"
    finally:
        export_md._ONE_AT_A_TIME.release()
        request.join(5)
        get_settings.cache_clear()


def test_the_waits_keep_their_order_and_one_retry():
    """Audit of 27 Oct 2026: the order between the waits lived in comments
    only, and the 503 `unavailable` of a store that did not answer
    (core/auth.key_store_unavailable) told its own retry, a second constant
    beside the one of a deadline (threads.busy_refusal)."""
    from korely_agent.core import auth, threads

    # A request that waits at the embedder gives its connection back before
    # another's checkout gives up; a statement outlives both.
    assert threads.WAIT_SECONDS < threads.POOL_CHECKOUT_SECONDS < threads.STATEMENT_SECONDS
    assert threads.ADMISSION_LOCK_WAIT_SECONDS < threads.LOCK_WAIT_SECONDS \
        < threads.LONG_LOCK_WAIT_SECONDS
    assert threads.STATEMENT_SECONDS < threads.LONG_STATEMENT_SECONDS
    assert threads.TOOL_BODIES_AT_ONCE <= threads.TOOL_BODY_THREADS
    assert threads.CALLS_PER_ACCOUNT < threads.TOOL_BODIES_AT_ONCE

    import pytest

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(threads, "BUSY_RETRY_SECONDS", 7)
        told = {auth.key_store_unavailable().headers["Retry-After"],
                threads.busy_refusal().headers["Retry-After"]}
    finally:
        mp.undo()
    assert told == {"7"}, told


def test_an_erasure_that_leaves_the_export_to_the_pass_is_written_by_that_pass(monkeypatch,
                                                                               tmp_path):
    """Review of the audit of 27 Oct 2026: an erasure's request that gave up
    the export lock left its files to the worker's next pass, a whole cadence
    with the erased person's file on disk. The tenant is marked owed now, and
    whoever holds the lock writes the owed tenants before giving it back."""
    import contextvars
    import threading
    import uuid

    from korely_agent.config import get_settings
    from korely_agent.core import threads
    from korely_agent.services import export_md

    monkeypatch.setattr(threads, "LOCK_WAIT_SECONDS", 0.2)
    monkeypatch.setenv("FACTS_EXPORT_DIR", str(tmp_path))
    get_settings.cache_clear()
    held, go_on, written = threading.Event(), threading.Event(), []
    the_pass, erased = uuid.uuid4(), uuid.uuid4()

    def export_one(db, tenant_id, base):
        written.append(tenant_id)
        if tenant_id == the_pass:
            held.set()
            assert go_on.wait(10)
        return 0

    monkeypatch.setattr(export_md, "_export_tenant", export_one)
    monkeypatch.setattr(export_md, "_owed", set())
    worker = threading.Thread(target=export_md.export_tenant, args=(None, the_pass, tmp_path))

    def the_erasures_request():
        threads.serving_a_request()
        export_md.after_an_erasure(None, erased)

    try:
        worker.start()
        assert held.wait(5)
        request = threading.Thread(target=contextvars.copy_context().run,
                                   args=(the_erasures_request,))
        request.start()
        request.join(3)
        assert not request.is_alive(), "the erasure's request waited for the pass"
        assert written == [the_pass]
    finally:
        go_on.set()
        worker.join(5)
        get_settings.cache_clear()
    assert written == [the_pass, erased], "the erased account's files waited for the next pass"
