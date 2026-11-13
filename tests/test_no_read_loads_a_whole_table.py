"""No request or worker path reads a whole large table into the one API
process (audit of 20 Oct 2026; db/bounded.py).

The API is one process, with the worker inside it, under one memory limit,
for every account of the install. That audit found nine reads that brought a
whole table, account or end user into it: `.all()` over the facts, the trail,
the vocabulary, the batch jobs. This scan reads the product's source: every
whole read (`.all()`, `.fetchall()`, a loop over a query) of a large table
carries a LIMIT or a `yield_per` (or ends in `.first()`, `.one()`,
`.scalar()`), or is named below with the reason it is bounded. A new one fails
here until its author bounds it or says, in one line, why it is bounded
already.

Out of the scan: memory_core (the engine, whose reads are reported
there) and the command-line tools, which run in a process of their own
(`python -m korely_agent.manage`, `korely_agent.hosted.cli`), never in the API's.

A second test holds the models to the other half of the rule: a vector or a
stored batch payload is deferred, so loading a row never brings it.
"""
import ast
import pathlib

from tests import cloud

ROOT = pathlib.Path(__file__).resolve().parents[1]

def _models():
    """The mapped classes of the product, by name and by table (the Cloud's
    too, where the tree has them: korely_agent.db loads its models)."""
    from korely_agent.db import Base
    return {m.class_.__name__: m.local_table.name for m in Base.registry.mappers}


#: The tables that stay small by their own structure, one line each: why.
#: Every other model is large (decided from the models, not from a hand list: a
#: table added tomorrow is large until someone says here why it is not).
SMALL = {
    "Tenant": "one row per account, read by the operator's own loops and the install's single owner",
    "InstallSetting": "one row per setting key, a fixed vocabulary",
    "InstallState": "one row per install",
    "Operator": "the install's operators, a handful created by hand",
    "OperatorSession": "one session per signed-in operator, pruned on expiry",
    "LoginAttempt": "pruned on expiry, one row per recent attempt of the throttle",
    "AgentRateLimit": "one row per account and window, a fixed set",
    "AgentWebhookEndpoint": "capped per account (webhooks.MAX_ENDPOINTS)",
}
MODELS = _models()
LARGE = set(MODELS) - set(SMALL)
_TABLES = {table: name for name, table in MODELS.items()}
_WHOLE = {"all", "fetchall"}
#: Wrappers that walk a query the same as `.all()` does.
_WALKERS = {"list", "set", "sorted", "tuple", "dict", "frozenset"}
_BOUNDED = {"limit", "yield_per", "first", "one", "one_or_none", "scalar", "get", "stream"}
_QUERIED = {"query", "join", "outerjoin", "select_from", "execute", "select", "with_entities",
            "scalars"}
#: A model the scan cannot name (a parameter, a subscript): unknown is large.
UNKNOWN = "<a model the scan cannot name>"

#: Processes of their own, never the API's.
CLI = {
    "korely_agent/manage.py": "the operator's command line",
    "korely_agent/hosted/cli.py": "the Cloud operator's command line",
    "korely_agent/hosted/reconcile.py": "imported by hosted/cli.py only",
    "korely_agent/services/credential_replay.py": "imported by manage.py only",
}

#: Whole reads of a large table that are bounded, one line each: why.
BOUNDED = {
    # Bounded by a page or a list the caller already holds.
    "korely_agent/api/admin.py::audit_read": "at most 100 ids of each, cut before the query",
    "korely_agent/api/admin.py::list_namespaces": "the facts of the page's agents only (`in_` of the page)",
    "korely_agent/services/context.py::_memory_rows": "the memories a search returned, by id",
    "korely_agent/services/mmr.py::order": "the vectors of a search's hits, by id",
    "korely_agent/services/context_experiments.py::in_dialogue_order": "a search's hits, by id",
    "korely_agent/services/facts/reads.py::_as_known": "the sources of one fact, by id",
    "korely_agent/services/memory_engine.py::families_of": "the live facts of one page of memories",
    "korely_agent/services/vectors.py::_ended_while_judged": "one fact and the few its judgement closes",
    "korely_agent/services/facts/previous_version.py::copies": "the copies one memory's previous text shared, by id",
    "korely_agent/services/facts/changes.py::holding_now": "the fact true now down each chain of one page of changes, at most one per change (the walk stops at the first true now)",
    "korely_agent/services/facts/changes.py::words_kept": "the live source memories of one page of changes, by id, one column",
    "korely_agent/services/facts/changes.py::_lookup": "the facts of one page of changes and what holds now for each, by id: at most twice MAX_CHANGES",
    # Grouped by the database: a row per group, the groups bounded by the vocabulary.
    "korely_agent/api/admin.py::list_webhooks": "one row per webhook endpoint of the account",
    "korely_agent/services/usage_report.py::requests_summary": "one row per verb, outcome and endpoint",
    "korely_agent/services/usage_report.py::requests_daily": "one row per day and verb, 400 days at most",
    "korely_agent/services/usage_report.py::memory_categories": "one row per (family, relation) pair and per family",
    "korely_agent/services/usage_report.py::report": "one row per model, the models an install is set to use",
    "korely_agent/services/usage_report.py::_grouped_with_cost": "the stages (a fixed vocabulary), or the MAX_AGENTS_LISTED agents with the most output and one row per model of the rest",
    "korely_agent/api/admin.py::list_projects": "one row per project of the account (GROUP BY project_id)",
    # An account's projects: projects.MAX_PROJECTS at most from its doors,
    # archived ones included; a Cloud claim adds a key-born account's, capped
    # the same.
    "korely_agent/services/projects.py::list_projects": "the projects of one account, at most projects.MAX_PROJECTS from its doors, and a claimed key-born account's, capped the same",
    "korely_agent/hosted/claim.py::_projects": "the projects of a key-born account, made by its doors only: at most projects.MAX_PROJECTS",
    "korely_agent/hosted/api_billing.py::list_brakes": "one row per provider (the table's key)",
    "korely_agent/hosted/billing.py::_customers_of": "a customer id per live subscription of one account, written by Polar's webhook",
    "korely_agent/hosted/billing.py::unrecorded": "the rows of the subscriptions Polar lists for one account, by their ids",
    "korely_agent/hosted/claim.py::_sum_counters": "one account's counter rows: a bucket or a month each, pruned",
    "korely_agent/hosted/replay.py::exported": "the erasure records, one per account closure, for the backup",
    "korely_agent/services/predicates.py::recompute_centroids": "run by `manage recompute-vectors` only, in the command line's own process: the ids and names of the relations",
    "korely_agent/services/vector_state.py::requeue_given_up": "run by `manage reextract` only, in the command line's own process: ids, up to `--limit`",
    "korely_agent/hosted/spending.py::spent_since": "one row per model priced",
    # One memory's, one fact's, one project's or one erasure's own rows.
    "korely_agent/services/projects.py::_hold_the_project": "the project's own row, by primary key, locked",
    "korely_agent/services/facts/erasure.py::erase_memory": "the facts that name one memory",
    "korely_agent/services/facts/erasure.py::recount_session_only": "the sources of the facts of one memory",
    "korely_agent/services/facts/erasure.py::stated_by_the_previous_version": "the open facts one memory stated",
    "korely_agent/services/facts/erasure.py::_aliases_learned_from": "the aliases one memory's facts taught",
    "korely_agent/services/facts/reads.py::facts_for_memory": "the facts one memory asserted",
    "korely_agent/services/facts/links.py::_load": "the object links of the memories of one page of facts, 200 memories a statement; a memory holds at most one link per object it names",
    "korely_agent/services/facts/links.py::names_condition": "the distinct memories (DISTINCT) whose links name one name, then their links through _load",
    "korely_agent/services/facts/erasure.py::_forget": "the open facts of one forgotten memory",
    "korely_agent/services/facts/given_back.py::what_they_closed": "the closed facts whose closer is one retracted now (one memory's facts, or one agent namespace's at its erasure), in that memory's scope or the erasure's project, and those closers by id; at most 50 links of a chain",
    "korely_agent/services/facts/given_back.py::_vouching": "the source memories of one fact, by id, three columns",
    "korely_agent/services/facts/given_back.py::give_back": "the facts what_they_closed found, by id",
    "korely_agent/services/facts/given_back.py::_begins": "the ends of the few rows that closed one fact (its closer and the chain to the fact retracted), by id",
    "korely_agent/services/facts/given_back.py::ended_in_place_by": "the closed facts one memory ended in place (its forget, erasure or edit), by the index of agent_fact_ended_by on the memory, in its scope",
    "korely_agent/services/facts/given_back.py::_the_words_retracted": "the relations of the facts those rows point at, by id, two columns",
    "korely_agent/services/batch.py::_scrub": "the ids of the jobs in reach; each job is then read alone",
    "korely_agent/services/batch.py::_item_end_users_one_by_one": "the ids of the jobs in reach; each payload is then read alone, as _scrub does",
    "korely_agent/services/webhooks.py::_to_send": "the dropped deliveries of one round's claim, by id: at most the round's limit",
    "korely_agent/services/facts/closures.py::_open_when_committed": "the facts this transaction closed, by id, two columns each: rows the session already holds",
    "korely_agent/services/pseudonyms.py::_record_for":"the user erasure records of one end user's pseudonyms, one per erasure of them",
    # What an erasure acts on one at a time, and the erasures' own records for
    # the backup: each one is needed, so all of them are returned, as many as
    # there are; of the rows they are read from, only the ids (30 Oct 2026:
    # "with no row kept" was said of reads that collect and return the ids).
    "korely_agent/services/erasure_replay.py::export": "the erasure records, one per erasure, for the backup",
    "korely_agent/services/batch.py::item_end_users": "distinct end users of the items, read by the database",
    "korely_agent/hosted/quotas.py::_unfinished_job_projects": "one row per project with a job not yet done (DISTINCT in the database): at most the account's projects",
    "korely_agent/services/end_user_places.py::_confined_lists": "distinct end-user lists the trail recorded for key changes (DISTINCT in the database)",
    "korely_agent/services/end_user_places.py::find": "the distinct end-user ids of one project's places, ids only, as many as its end users: the erasure needs each of them",
    # Bounded by a plan on the Cloud.
    "korely_agent/services/usage.py::distinct_agent_ids": "the Cloud's agent cap (hosted/quotas, billing): at most the plan's agents, one per project and name (DISTINCT in the database)",
}


def _base(node):
    """The Name at the root of `m.col`, `m[name]`, `*cols`, or None."""
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred)):
        node = node.value
    return node if isinstance(node, ast.Name) else None


def _names(node, alias, scope, seen=frozenset()):
    """The models (and tables, named in a string) a query argument names. A
    variable is followed to what it was assigned and what it loops over; a
    parameter, or a name that leads nowhere, is UNKNOWN, and unknown is large."""
    out = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Name):
            out.add(x.id)
        elif isinstance(x, ast.Attribute):
            out.add(x.attr)
        elif isinstance(x, ast.Constant) and isinstance(x.value, str):
            out |= {m for t, m in _TABLES.items() if t in x.value}
    out |= {alias[o] for o in out if o in alias}
    assigns, loops, params = scope[:3]
    root = _base(node)
    for name in {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} - set(seen):
        for v in [x for x, _u in assigns.get(name, ())] + list(loops.get(name, ())):
            out |= _names(v, alias, scope, seen | {name})
    if root is not None and root.id in params and root.id not in assigns and root.id not in loops:
        out.add(UNKNOWN)
    top = node.value if isinstance(node, ast.Starred) else node
    while isinstance(top, ast.Attribute):
        top = top.value
    if isinstance(top, ast.Subscript) and isinstance(top.value, ast.Name) and top.value.id not in MODELS:
        out.add(UNKNOWN)  # MODELS[name].id: a table picked at run time
    return out


#: Statements under which an assignment may not happen: a bound it makes
#: (`if n: q = q.limit(n)`) is not one.
_CONDITIONAL = (ast.If, ast.For, ast.While, ast.IfExp, ast.ExceptHandler,
                ast.AsyncFor, ast.comprehension)


def _helpers(extra_tree=None):
    """Every function of the product by name (followed when the name is
    unique): a query returned by a helper
    (`def _mine(db): return db.query(...)`) is followed to what it returns."""
    found = {}
    sources = [ast.parse(src) for _rel, src in _scanned()] + ([extra_tree] if extra_tree else [])
    for tree in sources:
        for f in ast.walk(tree):
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.setdefault(f.name, []).append(f)
    return found


def _chain(node, scope, helpers, cond=False, seen=None):
    """(method, call, conditional) triples of a query chain, back to its start,
    through the variables it was built in, the helpers that wrap it and the
    ones that return it. A call by name (`stream(q)`) is a method of its name."""
    seen = set() if seen is None else seen
    assigns = scope[0]
    out = []
    while True:
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                out.append((node.func.attr, node, cond))
                node = node.func.value
                continue
            if isinstance(node.func, ast.Name):
                out.append((node.func.id, node, cond))
                if len(helpers.get(node.func.id, ())) == 1 and ("fn", node.func.id) not in seen:
                    seen.add(("fn", node.func.id))
                    for f in helpers[node.func.id]:
                        inner = _scope(f, scope[3])
                        for r in ast.walk(f):
                            if isinstance(r, ast.Return) and r.value is not None:
                                out += _chain(r.value, inner, helpers, cond, seen)
            # within_project(q, ...), within_key_project(db, q, ...): the query
            # is whichever argument holds one, not only the first.
            for arg in node.args:
                out += _chain(arg, scope, helpers, cond, seen)
            return out
        if isinstance(node, ast.IfExp):
            # `q.limit(n) if n else q` bounds one branch only; `None if x is
            # None else q.first()` has a query in one branch and bounds it.
            body = _chain(node.body, scope, helpers, cond, set(seen))
            other = _chain(node.orelse, scope, helpers, cond, set(seen))
            shown = [c for n, c in ((node.body, body), (node.orelse, other))
                     if not isinstance(n, ast.Constant)]
            if len({frozenset(m for m, _c, _u in c) & _BOUNDED for c in shown}) > 1:
                body = [(m, c, True) for m, c, _u in body]
                other = [(m, c, True) for m, c, _u in other]
            return out + body + other
        if isinstance(node, ast.Attribute):
            node = node.value
            continue
        if isinstance(node, ast.Name) and node.id in assigns and node.id not in seen:
            seen.add(node.id)
            for v, under in assigns[node.id]:
                out += _chain(v, scope, helpers, cond or under, seen)
        return out


def _scope(fn, module=None):
    """(assignments, loop sources, parameters, the module's) of one function.
    An assignment is (value, made under a condition)."""
    assigns = {k: list(v) for k, v in (module or {}).items()}
    loops = {}

    def visit(node, under):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and child is not fn:
                continue
            here = under or isinstance(child, _CONDITIONAL)
            if isinstance(child, ast.Assign):
                for t in child.targets:
                    if isinstance(t, ast.Name):
                        assigns.setdefault(t.id, []).append((child.value, under))
            elif isinstance(child, (ast.For, ast.comprehension)):
                # for model in (AgentMemory, AgentFact): each is queried.
                if isinstance(child.iter, (ast.Tuple, ast.List)):
                    tgt = child.target.elts[0] if isinstance(child.target, ast.Tuple) else child.target
                    if isinstance(tgt, ast.Name):
                        loops.setdefault(tgt.id, []).extend(
                            e.elts[0] if isinstance(e, ast.Tuple) else e for e in child.iter.elts)
            visit(child, here)

    visit(fn, False)
    params = set()
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        every = fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs
        params = {x.arg for x in every} - {"self", "cls", "db", "session", "s"}
    return assigns, loops, params, module


def _root(node):
    """The Name a chain starts from, or None."""
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        else:
            return node if isinstance(node, ast.Name) else None


def _walked(node):
    """The query a whole read walks, or None: `q.all()`, `for r in q`,
    `list(q)`, `[x for x in q]`."""
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr in _WHOLE:
            return node.func.value
        if isinstance(node.func, ast.Name) and node.func.id in _WALKERS and node.args:
            return node.args[0]
    elif isinstance(node, (ast.For, ast.comprehension)):
        return node.iter
    return None


def whole_reads(rel: str, source: str) -> list:
    """(function, line, large tables) of each unbounded whole read."""
    tree = ast.parse(source, filename=rel)
    alias = {}
    module = {}  # the module's own assignments: `F = AgentFact`, `_COLS = (...)`
    for a in tree.body:
        if isinstance(a, ast.Assign):
            for t in a.targets:
                if isinstance(t, ast.Name):
                    module.setdefault(t.id, []).append((a.value, False))
                    if isinstance(a.value, (ast.Name, ast.Attribute)):
                        alias[t.id] = getattr(a.value, "id", getattr(a.value, "attr", None))
    for a in ast.walk(tree):
        if isinstance(a, ast.ImportFrom):
            alias.update({n.asname: n.name for n in a.names if n.asname})
        if (isinstance(a, ast.Assign) and isinstance(a.value, ast.Call) and a.value.args
                and getattr(a.value.func, "id", getattr(a.value.func, "attr", None)) == "aliased"):
            first = a.value.args[0]
            name = getattr(first, "id", getattr(first, "attr", None))
            alias.update({t.id: alias.get(name, name) for t in a.targets if isinstance(t, ast.Name)})
    helpers = _helpers(tree)
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    found = []
    for node in ast.walk(tree):
        target = _walked(node)
        if target is None:
            continue
        line = node.lineno if isinstance(node, ast.Call) else target.lineno
        fn = node
        while fn in parents and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = parents[fn]
        scope = _scope(fn, module)
        chain = _chain(target, scope, helpers)
        methods = {m for m, _c, _u in chain}
        whole_call = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        unknown = False
        if not any(m in _QUERIED for m in methods):
            # A query handed in as a parameter: `def f(q): return q.all()`.
            root = _root(target)
            if not (whole_call and root is not None and root.id in scope[2]
                    and root.id not in scope[0]):
                continue
            unknown = True
        if isinstance(node, (ast.For, ast.comprehension)) and methods & _WHOLE:
            continue  # the `.all()` it walks is the read, and is judged itself
        queried, bounded = set(), {m for m, _c, under in chain if not under}
        for m, call, _u in chain:
            if m in _QUERIED:
                for arg in call.args:
                    queried |= _names(arg, alias, scope)
                    # A bound on the argument's own chain (`execute(select(X).limit(3))`),
                    # never one nested in its filters.
                    bounded |= {m2 for m2, _c2, under in _chain(arg, scope, helpers) if not under}
        if whole_call:
            bounded.discard("yield_per")  # `.yield_per(n).all()` still loads every row
        large = queried & LARGE | (queried & {UNKNOWN}) | ({UNKNOWN} if unknown else set())
        if large and not bounded & _BOUNDED:
            name = fn.name if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
            found.append((name, line, tuple(sorted(large))))
    return sorted(set(found), key=lambda f: f[1])


def _scanned():
    for path in sorted((ROOT / "korely_agent").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        if "memory_core" in path.parts or "__pycache__" in path.parts or rel in CLI:
            continue
        yield rel, path.read_text()


def test_no_whole_read_of_a_large_table_is_unbounded():
    unexplained = []
    for rel, source in _scanned():
        for fn, line, large in whole_reads(rel, source):
            if f"{rel}::{fn}" not in BOUNDED:
                unexplained.append(f"{rel}:{line} {fn} reads {', '.join(large)} whole")
    assert unexplained == [], (
        "a whole read of a large table: cut it in SQL (LIMIT, GROUP BY, COUNT), stream it "
        "(db.bounded.stream) or name it in BOUNDED with why it is bounded\n" + "\n".join(unexplained))


def test_every_reason_names_a_read_that_is_still_there():
    there = {f"{rel}::{fn}" for rel, source in _scanned() for fn, _l, _t in whole_reads(rel, source)}
    assert sorted(set(cloud.in_this_tree(BOUNDED)) - there) == [], \
        "a reason for a read that is gone, or bounded now"
    # A reason says what the bound is; a branch or a rewrite is no bound.
    assert [k for k, why in BOUNDED.items()
            if len(why.split()) < 3 or any(w in why.lower() for w in ("window b", "branch", "rewrite"))] == []
    assert all(path in {str(p.relative_to(ROOT)) for p in (ROOT / "korely_agent").rglob("*.py")}
               for path in cloud.in_this_tree(CLI))


def test_the_scan_sees_each_shape():
    probe = '''
from ..db import AgentFact as F
def a(db):
    return db.query(AgentFact).filter(AgentFact.tenant_id == 1).all()
def b(db):
    q = db.query(F.id)
    q = q.filter(F.x == 1)
    return q.all()
def c(db):
    for model in (AgentMemory, Tenant):
        model_rows = db.query(model.end_user_id).distinct().all()
def d(db):
    return [r for r in db.query(AuditEvent.id)]
def e(db):
    return db.execute(text("SELECT * FROM agent_facts")).all()
def f(db):
    return within_project(db.query(PredicateCluster), 1, 2).all()
def g(db, model):
    return db.query(model).filter(model.tenant_id == 1).all()
def h(db, name):
    return db.query(MODELS[name].id).all()
def i(db):
    cols = (AgentApiKey.id, AgentApiKey.name)
    return db.query(*cols).all()
def j(db):
    return list(db.query(AgentApiKey.id))
def k(db):
    return db.scalars(select(AgentApiKey)).all()
def l(db):
    rows = db.query(AgentApiKey.id)
    for r in rows:
        pass
def m(db):
    return db.query(OAuthGrant).all()
def n(db):
    return set(db.query(AgentWebhookDelivery.id))
def o(db, tenant_id):
    return within_key_project(db, db.query(AgentApiKey).filter(AgentApiKey.tenant_id == tenant_id),
                              AgentApiKey.project_id, tenant_id, 1).all()
F2 = AgentFact
_COLS = (AgentFact.id, AgentFact.tenant_id)
def p(db):
    return db.query(F2.id).all()
def q(db):
    return db.query(*_COLS).all()
def r(db, n):
    rows = db.query(AgentFact.id)
    if n:
        rows = rows.limit(n)
    return rows.all()
def s2(db, n):
    rows = db.query(AgentFact.id)
    rows = rows.limit(n) if n else rows
    return rows.all()
def t(db, d):
    return db.execute(select(AgentFact.id).where(AgentFact.id == d.get(1))).all()
def u(db):
    return db.query(AgentFact.id).yield_per(10).all()
def mine(db):
    return db.query(AgentFact.id)
def v(db):
    return mine(db).all()
def w(q):
    return q.filter(AgentFact.id == 1).all()
def ok(db):
    for r in stream(db.query(AgentFact.id)): pass
    x = None if db is None else db.query(AgentFact.id).first()
    db.query(AgentFact).limit(5).all()
    db.query(AgentFact).first()
    for r in db.query(AgentFact.id).yield_per(100): pass
    db.query(Tenant).all()
    db.execute(select(AgentFact.id).limit(3)).all()
    db.query(InstallSetting).all()
    return list(db.query(AgentApiKey.id).limit(4))
'''
    assert [fn for fn, _l, _t in whole_reads("p.py", probe)] == ["a","b","c","d","e","f","g","h","i","j","k","l","m","n","o","p","q","r","s2","t","u","v","w"]


def test_large_is_decided_by_the_models():
    """A new model is large until SMALL says why not; SMALL names real models."""
    assert set(SMALL) <= set(MODELS), "SMALL names a model that is gone"
    assert {"AgentApiKey", "OAuthGrant"} | ({"HostedBrake"} if cloud.HERE else set()) <= LARGE
    assert not {"Tenant"} & LARGE
    assert all(len(reason.split()) >= 3 for reason in SMALL.values())


def test_vectors_and_payloads_are_deferred():
    """Loading a row never brings its vector or a stored batch payload
    (db/models.py): a read that needs one names it."""
    from pgvector.sqlalchemy import Vector

    from korely_agent.db import Base
    from korely_agent.db.models import AgentBatchJob

    heavy = [(m.class_.__name__, prop.key) for m in Base.registry.mappers
             for prop in m.column_attrs
             if any(isinstance(c.type, Vector) for c in prop.columns)]
    heavy.append(("AgentBatchJob", "payload"))
    assert len(heavy) >= 4
    for cls, key in heavy:
        mapper = AgentBatchJob.__mapper__ if cls == "AgentBatchJob" else next(
            m for m in Base.registry.mappers if m.class_.__name__ == cls)
        assert mapper.column_attrs[key].deferred, f"{cls}.{key} loads with every row"
