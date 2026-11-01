"""No log line of this install prints an exception's message (2026-10-06).

memory_core 3.0.0's rule (`log_safe`), here too: a log line leaves the store,
goes to files and collectors kept by other rules than the memories, and no
forget reaches it. An exception's message is text nobody chose: a model's
reply carries the memory's words, a database error the values of the row
(PostgreSQL's DETAIL), a provider's error the request. So an error is
described by `log_safe.error` (classes, codes, the length of the message) and
a stack by `log_safe.stack` (file, line, function), never by `%s` of the
exception, `logger.exception` or `exc_info=True`, whose formatter prints the
message.

No exceptions (audit of 17 Oct 2026): the rule of CONTRIBUTING.md has none, and
the list this test kept (the local embedder's own words, the operator's
LLM_RATES) lived here alone. Those lines now say what they need from the
folder and the parser's position, and the error by its class.
"""
import ast
import pathlib

from tests.log_calls import is_log_call, logger_names

ROOT = pathlib.Path(__file__).resolve().parents[1]

# (file, the type its handler catches) a log line may print the message of.
# Empty, and kept empty: a new entry is a change of CONTRIBUTING.md first.
ALLOWED: set = set()


def _caught(handler: ast.ExceptHandler) -> str:
    return ast.unparse(handler.type) if handler.type is not None else ""


def _offences(path: pathlib.Path) -> list:
    rel = str(path.relative_to(ROOT))
    out = []
    tree = ast.parse(path.read_text(), filename=rel)
    names = logger_names(tree)
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or (rel, _caught(handler)) in ALLOWED:
            continue
        for call in ast.walk(handler):
            if not is_log_call(call, names):
                continue
            prints = handler.name is not None and any(
                isinstance(n, ast.Name) and n.id == handler.name
                and not _inside_log_safe(call, n) for n in ast.walk(call))
            stack = call.func.attr == "exception" or any(
                k.arg == "exc_info" for k in call.keywords)
            if prints or stack:
                out.append(f"{rel}:{call.lineno}")
    return out


def _exception_parameters(fn) -> set:
    """The parameters of `fn` annotated as an exception: an exception handed
    to a helper is printed outside any `except` block (audit of 17 Oct 2026,
    `_unreadable` in core/llm.py, which the handler scan above cannot see)."""
    args = fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
    return {a.arg for a in args if a.annotation is not None
            and "Exception" in ast.unparse(a.annotation)}


def _offences_of_helpers(path: pathlib.Path) -> list:
    rel = str(path.relative_to(ROOT))
    out = []
    tree = ast.parse(path.read_text(), filename=rel)
    names = logger_names(tree)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = _exception_parameters(fn)
        if not params:
            continue
        for call in ast.walk(fn):
            if (is_log_call(call, names)
                    and any(isinstance(n, ast.Name) and n.id in params
                            and not _inside_log_safe(call, n) for n in ast.walk(call))):
                out.append(f"{rel}:{call.lineno}")
    return out


def _inside_log_safe(call: ast.Call, name: ast.Name) -> bool:
    """`name` only appears as the argument of log_safe.error / log_safe.stack."""
    for c in ast.walk(call):
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and isinstance(c.func.value, ast.Name) and c.func.value.id == "log_safe"
                and any(a is name for a in c.args)):
            return True
    return False


def test_no_log_line_prints_an_exceptions_message():
    found = []
    for path in sorted((ROOT / "korely_agent").rglob("*.py")):
        if "memory_core" in path.parts or "__pycache__" in path.parts:
            continue
        found += _offences(path)
    assert found == [], "\n".join(found)


def test_nothing_is_allowed():
    """The rule has no exceptions; an entry here would be one."""
    assert ALLOWED == set()


def test_no_helper_prints_the_exception_it_is_handed():
    found = []
    for path in sorted((ROOT / "korely_agent").rglob("*.py")):
        if "memory_core" in path.parts or "__pycache__" in path.parts:
            continue
        found += _offences_of_helpers(path)
    assert found == [], "\n".join(found)


# Where a failed call's words come out of core/llm (audit of 17 Oct 2026):
# `last_failure_here()` is the failure as text, the provider's own
# message in it, and that is text nobody chose, as an exception's is. The
# scans above look for an exception; this value is a string by then, handed
# through variables, lists and calls, and they could not see it. A log line
# says the category instead (`last_kind_here()`, a token of core/llm), or the
# status (`last_status_here()`, a field).
_TEXT_OF_A_FAILURE = {"last_failure_here"}
_COLLECTS = {"append", "extend", "add", "insert", "appendleft"}


def _called_name(call: ast.Call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None


def _carries(node, tainted: set) -> bool:
    return any((isinstance(n, ast.Call) and _called_name(n) in _TEXT_OF_A_FAILURE)
               or (isinstance(n, ast.Name) and n.id in tainted) for n in ast.walk(node))


def _stored_names(target) -> set:
    """The plain names an assignment binds: `a`, `a, b`, `*a`; never the
    container of `a[k] = ...` or `a.k = ...`, which keeps its other values."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_stored_names(t) for t in target.elts)) if target.elts else set()
    if isinstance(target, ast.Starred):
        return _stored_names(target.value)
    return set()


def _offences_of_failure_text(rel: str, source: str) -> list:
    """Log lines that print the text of a failure, followed from
    `last_failure_here()` through assignments, a list it is put in, and the
    parameters of this module's functions it is handed to."""
    tree = ast.parse(source, filename=rel)
    fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    by_name = {}
    for fn in fns:
        by_name.setdefault(fn.name, []).append(fn)
    tainted = {id(fn): set() for fn in fns}
    changed = True
    while changed:
        changed = False
        for fn in fns:
            t = tainted[id(fn)]
            before = len(t)
            for node in ast.walk(fn):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) \
                        and node.value is not None and _carries(node.value, t):
                    for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                        t |= _stored_names(target)
                elif isinstance(node, (ast.For, ast.AsyncFor)) and _carries(node.iter, t):
                    t |= _stored_names(node.target)
                elif isinstance(node, ast.Call):
                    f = node.func
                    if (isinstance(f, ast.Attribute) and f.attr in _COLLECTS
                            and isinstance(f.value, ast.Name)
                            and any(_carries(a, t) for a in node.args)):
                        t.add(f.value.id)
                    for callee in by_name.get(_called_name(node), []) if isinstance(f, ast.Name) else []:
                        params = [a.arg for a in callee.args.posonlyargs + callee.args.args]
                        handed = {params[i] for i, a in enumerate(node.args)
                                  if i < len(params) and _carries(a, t)}
                        handed |= {k.arg for k in node.keywords if k.arg and _carries(k.value, t)}
                        if not handed <= tainted[id(callee)]:
                            tainted[id(callee)] |= handed
                            changed = True
            if len(t) != before:
                changed = True
    out = set()
    names = logger_names(tree)
    for fn in fns:
        for call in ast.walk(fn):
            if (is_log_call(call, names)
                    and any(_carries(a, tainted[id(fn)])
                            for a in list(call.args) + [k.value for k in call.keywords])):
                out.add(f"{rel}:{call.lineno}")
    return sorted(out)


def test_no_log_line_prints_the_text_of_a_failed_call():
    found = []
    for path in sorted((ROOT / "korely_agent").rglob("*.py")):
        if "memory_core" in path.parts or "__pycache__" in path.parts:
            continue
        found += _offences_of_failure_text(str(path.relative_to(ROOT)), path.read_text())
    assert found == [], "\n".join(found)


def test_the_failure_text_scan_sees_one():
    """The scan above finds the shape it is for: the text taken in one
    function, put in a list, unpacked, handed to another that logs it."""
    import textwrap

    probe = textwrap.dedent('''
        def drain():
            refused = []
            failure = llm.last_failure_here()
            kind = llm.last_kind_here()
            logger.info("[x] kind %s", kind)
            refused.append((failure, kind))
            text, _ = refused[0]
            rest(1, text)

        def rest(n, why):
            logger.warning("[x] rested %d: %s", n, why[:200])
    ''')
    assert _offences_of_failure_text("probe.py", probe) == ["probe.py:12"]


def test_the_allowed_ones_still_exist():
    """An entry whose handler is gone is a hole for the next one."""
    seen = set()
    for rel, caught in ALLOWED:
        tree = ast.parse((ROOT / rel).read_text())
        if any(isinstance(h, ast.ExceptHandler) and _caught(h) == caught for h in ast.walk(tree)):
            seen.add((rel, caught))
    assert seen == ALLOWED


def test_every_logging_method_and_logger_is_seen(tmp_path):
    """Audit of 20 Oct 2026: `logger.log(level, ...)` and the loggers bound
    under other names (main.py's `access` and `errors`) were outside the
    scans: an exception's text through them stayed green."""
    import textwrap

    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent('''
        import logging
        errors = logging.getLogger("errors")

        def go():
            try:
                pass
            except Exception as e:
                logger.log(logging.ERROR, "[x] failed: %s", e)
                errors.warning("[x] failed", exc_info=True)
                logger.warn("[x] %s", e)
    '''))
    import tests.test_logs_carry_no_exception_text as me

    old = me.ROOT
    me.ROOT = tmp_path
    try:
        assert me._offences(probe) == ["probe.py:9", "probe.py:10", "probe.py:11"]
    finally:
        me.ROOT = old
