"""The audit trail.

The tests that matter here are not "does it write a row". They are the two
properties that make the trail worth having: it records what happened even when
recording is inconvenient, and it never becomes a second copy of the data.
"""
import ast
import logging
import pathlib
import uuid


from korely_agent.db import AuditEvent
from korely_agent.services import audit
from tests import cloud


class TestNeverContent:
    """An audit row holding content makes erasure a claim rather than a fact.

    Delete the memory, and if its text is in the log you have not erased
    anything. A feature bought for compliance becomes the thing that breaks it,
    and nobody notices because the log is not where anyone looks for personal
    data.
    """

    def test_long_strings_in_meta_are_dropped(self, db, tenant):
        audit.record(
            db, tenant.id, audit.WRITE,
            meta={"facts": 3, "content": "x" * 500},
        )
        row = db.query(AuditEvent).one()
        assert row.meta["facts"] == 3
        assert "content" not in row.meta, (
            "a 500-character meta value is content, and content must never "
            "reach the audit table"
        )

    def test_counts_and_short_labels_survive(self, db, tenant):
        audit.record(
            db, tenant.id, audit.WRITE,
            meta={"facts": 3, "invalidated": 1, "endpoint": "memories:add"},
        )
        row = db.query(AuditEvent).one()
        assert row.meta == {"facts": 3, "invalidated": 1, "endpoint": "memories:add"}

    def test_the_target_is_an_id_not_a_thing(self, db, tenant):
        """We record which row was touched, never what was in it."""
        mem_id = f"mem_{uuid.uuid4().hex}"
        audit.record(db, tenant.id, audit.WRITE, target_id=mem_id)
        assert db.query(AuditEvent).one().target_id == mem_id


class TestNeverFailsTheCaller:
    def test_a_broken_write_does_not_raise(self, db, tenant, monkeypatch, caplog):
        """The operation happened. Reporting it as failed would be worse.

        Loud in the log, silent to the caller: a request that succeeded must not
        return an error because a bookkeeping row could not be stored.
        """
        from korely_agent import db as dbpkg

        class Broken:
            """The session audit.record opens for itself (it never writes
            into the caller's): breaking the caller's `add`, as this test did
            until 2026-10-06, left the trail working and the error branch
            never ran (the audit of 6 October, row 18)."""

            def add(self, row):
                raise RuntimeError("table is gone")

            def rollback(self):
                self.rolled_back = True

            def close(self):
                self.closed = True

        broken = Broken()
        monkeypatch.setattr(dbpkg, "get_own_sessionmaker", lambda: (lambda: broken))
        with caplog.at_level(logging.ERROR, logger=audit.logger.name):
            audit.record(db, tenant.id, audit.WRITE)   # must not raise
        assert broken.rolled_back and broken.closed
        # "But not quietly either" (the module's first rule): the hole in the
        # trail is in the log at ERROR, and the error's own words are not
        # (log_safe.stack). Until the audit of 17 Oct 2026 nothing checked the
        # log, and a silenced logger.error passed.
        errors = [r for r in caplog.records
                  if r.name == audit.logger.name and r.levelno >= logging.ERROR]
        assert errors, "an audit row was lost and nothing said so at ERROR"
        assert any("could not record" in r.getMessage() for r in errors)
        assert "table is gone" not in caplog.text

    def test_an_unknown_action_is_still_recorded(self, db, tenant):
        """Losing an event is worse than recording an odd one.

        A typo in a verb should show up in the log as a typo, not as silence in
        a report that looks complete.
        """
        audit.record(db, tenant.id, "something_new")
        assert db.query(AuditEvent).one().action == "something_new"


class TestScopeAndRetention:
    def test_events_are_scoped_to_a_tenant(self, db, tenant):
        """Two tenants, an event each, each under its own. The second tenant
        is committed, as the `tenant` fixture is: record() writes in its own
        session, which cannot see a row only flushed in this one, so its
        event failed the foreign key, was swallowed, and the test ran in a
        world with one tenant, green with the tenants swapped (audit of 17
        Oct 2026)."""
        from korely_agent.db.models import Tenant

        other = Tenant(id=uuid.uuid4(), name="Other")
        db.add(other)
        db.commit()
        audit.record(db, tenant.id, audit.READ)
        audit.record(db, other.id, audit.WRITE)
        db.expire_all()
        by_tenant = {}
        for e in db.query(AuditEvent).all():
            by_tenant.setdefault(e.tenant_id, []).append(e.action)
        assert by_tenant == {tenant.id: [audit.READ], other.id: [audit.WRITE]}

    def test_retention_zero_keeps_everything(self, db, tenant):
        """0 means forever, and forever has to be chosen rather than fallen into."""
        audit.record(db, tenant.id, audit.READ)
        assert audit.purge_expired(db, 0) == 0
        assert db.query(AuditEvent).count() == 1

    def test_old_events_are_purged(self, db, tenant):
        """Past the window it goes, inside it stays: without an event inside
        the window this test could not tell a purge that keeps 400 days from
        one that empties audit_events at the worker's hourly round (audit of
        17 Oct 2026)."""
        from datetime import datetime, timedelta, timezone

        for days, action in ((500, audit.READ), (399, audit.WRITE), (0, audit.ERASE)):
            audit.record(db, tenant.id, action)
            row = db.query(AuditEvent).filter(AuditEvent.action == action).one()
            row.ts = datetime.now(timezone.utc) - timedelta(days=days)
            db.commit()
        assert audit.purge_expired(db, 400) == 1
        db.expire_all()
        assert sorted(e.action for e in db.query(AuditEvent)) == sorted([audit.WRITE, audit.ERASE])


class TestTheTwentyTwoCallSites:
    def test_log_v1_request_actually_writes(self, db, tenant):
        """The regression this whole module exists for.

        The previous implementation imported a table from the other product,
        failed on every one of twenty-two calls, and swallowed it as
        best-effort telemetry.
        """
        from korely_agent.services.usage import log_v1_request

        log_v1_request(db, tenant.id, "memories:add")
        row = db.query(AuditEvent).one()
        assert row.action == audit.WRITE
        assert row.meta["endpoint"] == "memories:add"

    def test_reads_and_writes_are_not_the_same_verb(self, db, tenant):
        """Collapsing them gives a report that says an account wrote a thousand
        times when it read a thousand times."""
        from korely_agent.services.usage import log_v1_request

        log_v1_request(db, tenant.id, "memories:get")
        log_v1_request(db, tenant.id, "memories:add")
        log_v1_request(db, tenant.id, "users:delete")
        actions = {r.action for r in db.query(AuditEvent).all()}
        assert actions == {audit.READ, audit.WRITE, audit.ERASE}


class TestEveryEndpointLabelIsClassified:
    """A new write endpoint must not record itself as a read.

    usage.py falls through to `read` for anything not in _ACTION_FOR, and the
    docstring calls that safe because a write mislabelled as a read "shows up as
    suspiciously busy reading, which is a mistake you notice". Nobody notices
    that. What the audit trail is for is answering a data subject's access
    request, and answering it with the wrong verb is answering it wrongly.

    So the classification is checked against the labels the code actually uses,
    read out of the source with the parser, through the wrappers that pass a
    label on (the MCP's `_audit`, audit of 17 Oct 2026). Adding an endpoint
    without deciding what it is now fails here instead of in a report six
    months later.
    """

    # Labels that are reads on purpose. Anything not here and not in
    # _ACTION_FOR is unclassified, which is the thing being caught.
    KNOWN_READS = {
        "agents:list", "batch:get", "context", "events:list",
        "facts:list", "memories:get", "memories:history",
        "memories:list", "memories:search", "profile", "users:list", "ping:get",
        # GET /v1/audit: written for a refused read of another end user's
        # trail only (audit of 19 Oct 2026).
        "audit:read",
        # The screen's reads (/admin), written with the operator as principal.
        "admin:audit:read", "admin:facts:list", "admin:memories:get",
        "admin:memories:history", "admin:memories:list", "admin:memories:search",
        # A part of the product's (core/extensions.Part).
        *cloud.of_the_parts("KNOWN_READS", tuple),
    }

    def _labels_in_source(self):
        labels, unreadable = _endpoint_labels()
        assert not unreadable, (
            f"an endpoint label the scan cannot read (not a literal): {unreadable}")
        return labels

    def test_no_label_is_unclassified(self):
        from korely_agent.services.usage import _ACTION_FOR, FORGETS_OR_ERASES

        labels = self._labels_in_source()
        assert labels, "found no log_v1_request labels; the scan is broken"

        # A part of the product classifies its own (core/extensions.Part.actions).
        from korely_agent.core import extensions

        parts = {a for part in extensions.parts() for a in part.actions()}
        unclassified = (labels - set(_ACTION_FOR) - FORGETS_OR_ERASES - parts
                        - self.KNOWN_READS)
        assert not unclassified, (
            f"these endpoints record as 'read' by default and nobody decided "
            f"that: {sorted(unclassified)}. Add each to _ACTION_FOR, or to "
            f"KNOWN_READS if it really is a read."
        )

    def test_an_mcp_tool_writes_the_label_of_its_rest_twin(self):
        """One act, one label: a read of the context was `context` over REST
        and `context:get` over the MCP, the facts `facts:list` and
        `facts:get`, so the Requests page and a filter on the endpoint split
        the same act in two (audit of 17 Oct 2026)."""
        import pathlib
        import re

        pkg = pathlib.Path(__file__).resolve().parents[1] / "korely_agent"
        rest = set()
        for f in (pkg / "api").glob("*.py"):
            rest |= set(re.findall(r'log_v1_request\(\s*db,\s*[\w.]+,\s*"([^"]+)"',
                                   f.read_text()))
        mcp = set(re.findall(r'_audit\(db, uid, "([^"]+)"',
                             (pkg / "mcp" / "server.py").read_text()))
        assert mcp, "found no MCP labels; the scan is broken"
        assert mcp <= rest, f"MCP labels no REST route writes: {sorted(mcp - rest)}"

    def test_the_scan_reads_through_a_wrapper(self, tmp_path):
        """A label handed to a function that passes it on to log_v1_request
        (the MCP's `_audit`) is a label: until the audit of 17 Oct 2026 the
        scan read only the literals inside log_v1_request(...), so a new MCP
        write tool would have been recorded as a read with this class green."""
        (tmp_path / "tool.py").write_text(
            "def _audit(db, uid, endpoint, **kw):\n"
            "    log_v1_request(db, uid, endpoint, actor='mcp', **kw)\n"
            "def _wrap_again(db, uid, label):\n"
            "    _audit(db, uid, label)\n"
            "def wipe(db, uid):\n"
            "    _audit(db, uid, 'memories:wipe', end_user_id='u')\n"
            "    _wrap_again(db, uid, label='users:forget_all')\n"
            "def unknown(db, uid, which):\n"
            "    log_v1_request(db, uid, 'facts:' + which)\n")
        labels, unreadable = _endpoint_labels(tmp_path)
        assert {"memories:wipe", "users:forget_all"} <= labels
        assert unreadable == ["tool.py:9"]

    def test_the_map_has_no_labels_the_code_stopped_using(self):
        from korely_agent.services.usage import _ACTION_FOR, CLOUD_ACTIONS, FORGETS_OR_ERASES

        # The Cloud's labels are written in its package, where the tree has it.
        stale = (set(_ACTION_FOR) | FORGETS_OR_ERASES) - self._labels_in_source() - (
            set() if cloud.HERE else set(CLOUD_ACTIONS))
        assert not stale, f"_ACTION_FOR classifies endpoints nobody calls: {sorted(stale)}"

    def test_every_verb_is_one_the_audit_module_knows(self):
        from korely_agent.services import audit
        from korely_agent.services.usage import _ACTION_FOR

        known = {getattr(audit, n) for n in dir(audit)
                 if n.isupper() and isinstance(getattr(audit, n), str)}
        unknown = set(_ACTION_FOR.values()) - known
        assert not unknown, f"verbs the audit module does not define: {sorted(unknown)}"


def _endpoint_labels(root=None):
    """Every endpoint label that reaches `log_v1_request`, read with the parser:
    written at a call of it, or at a call of a function that hands one of its
    own parameters on as the endpoint (a wrapper, as the MCP's `_audit`; a
    wrapper of a wrapper too, as api/errors.refused and core/auth.reading,
    whose labels are written in other modules: audit of 19 Oct 2026). Returns
    the labels, and the places where the endpoint is an expression with no
    literal to read ("file:line"), which a label decided at run time would be."""
    root = pathlib.Path(root or pathlib.Path(__file__).resolve().parents[1] / "korely_agent")
    trees = {path: ast.parse(path.read_text()) for path in sorted(root.rglob("*.py"))}
    # name -> (position or None for a keyword-only one, name) of the endpoint
    # parameter. A public wrapper is called from any module; a private one
    # (`_audit`) only from its own, where another module's `_audit` is
    # another function.
    public = {"log_v1_request": (2, "endpoint")}
    private = {path: {} for path in trees}
    while True:  # a wrapper of a wrapper is a wrapper
        grew = False
        for path, tree in trees.items():
            found = _Calls({**public, **private[path]})
            found.visit(tree)
            for f, spec in found.forwarding.items():
                into = private[path] if f.startswith("_") else public
                if f not in into:
                    into[f] = spec
                    grew = True
        if not grew:
            break
    labels, unreadable = set(), []
    for path, tree in trees.items():
        found = _Calls({**public, **private[path]})
        found.visit(tree)
        for where, arg in found.labels:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                labels.add(arg.value)
            elif isinstance(arg, ast.IfExp) and all(
                    isinstance(b, ast.Constant) and isinstance(b.value, str) for b in (arg.body, arg.orelse)):
                labels |= {arg.body.value, arg.orelse.value}
            else:
                unreadable.append(f"{path.relative_to(root)}:{where}")
    return labels, unreadable


class _Calls(ast.NodeVisitor):
    """One pass over a module: the endpoint of every call to a known wrapper,
    and the functions that forward a parameter of their own as one."""

    def __init__(self, wrappers):
        self.wrappers = wrappers
        self.stack = []
        self.labels = []       # (line, the endpoint expression)
        self.forwarding = {}   # function name -> (position, name) of its parameter

    def visit_FunctionDef(self, fn):
        self.stack.append(fn)
        self.generic_visit(fn)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, call):
        f = call.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        spec = self.wrappers.get(name)
        if spec is not None:
            index, kw_name = spec
            arg = next((k.value for k in call.keywords if k.arg == kw_name),
                       call.args[index] if index is not None and len(call.args) > index else None)
            fn = self.stack[-1] if self.stack else None
            own = [a.arg for a in fn.args.posonlyargs + fn.args.args] if fn else []
            own_kw = [a.arg for a in fn.args.kwonlyargs] if fn else []
            if isinstance(arg, ast.Name) and arg.id in own:
                self.forwarding[fn.name] = (own.index(arg.id), arg.id)
            elif isinstance(arg, ast.Name) and arg.id in own_kw:
                self.forwarding[fn.name] = (None, arg.id)
            elif arg is not None:
                self.labels.append((call.lineno, arg))
        self.generic_visit(call)

def test_every_actor_written_is_one_the_trail_documents():
    """GET /v1/audit documented `actor` as five values while the trail also
    wrote `oauth` (an app's key made or revoked, since 0.1.37) and `install`
    (audit of 17 Oct 2026): a client validating on the list refused
    those rows. Every actor the code writes, as a keyword, a constant or the
    `actor` of a dict, is in services/audit.ACTORS, and the document names
    each one."""
    import ast
    from pathlib import Path

    from korely_agent.api.audit import AuditRow
    from korely_agent.services.audit import ACTORS

    def literal(value):
        return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None

    root = Path(__file__).resolve().parents[1] / "korely_agent"
    written = set()
    for f in root.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Call):            # record(..., actor="mcp")
                pairs = [(k.arg, k.value) for k in n.keywords]
            elif isinstance(n, ast.Dict):          # {"actor": "dashboard", ...}
                pairs = [(literal(k), v) for k, v in zip(n.keys, n.values)]
            elif isinstance(n, ast.Assign):        # ACTOR = "oauth"
                pairs = [(t.id, n.value) for t in n.targets if isinstance(t, ast.Name)]
            elif isinstance(n, ast.arguments):     # def record(..., actor="rest")
                named = n.args[len(n.args) - len(n.defaults):] + n.kwonlyargs
                pairs = list(zip((a.arg for a in named), list(n.defaults) + list(n.kw_defaults)))
            else:
                continue
            written |= {literal(v) for k, v in pairs if k in ("actor", "ACTOR") and literal(v)}
    assert {"rest", "mcp", "dashboard", "oauth", "install"} <= written, \
        f"the scan found too little, it is broken: {sorted(written)}"
    assert written <= set(ACTORS), sorted(written - set(ACTORS))
    said = AuditRow.model_json_schema()["properties"]["actor"]["description"]
    assert all(f"`{a}`" in said for a in ACTORS), said
