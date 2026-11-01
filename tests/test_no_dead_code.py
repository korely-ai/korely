"""Dead code, kept at zero where it can be seen.

In the product, two kinds pyflakes can see:

A name defined twice, the first never used: when both are routes, both are
registered, the first one serves, and /openapi.json carries two operations
with one id. That was `/admin/memories/categories`, in api/admin.py twice.

A local assigned and never read: `since` in services/context.py was computed
for every fact of every context and thrown away, because the line that used it
had moved into memory_core, which renders the date itself.

Only those two from pyflakes. Unused imports are not counted in the product:
some of them are the public names of a module (re-exports), and telling those
apart is a separate job. memory_core is not scanned for these two kinds
(the last test below reads it with pyflakes in full): it is modified only
after review (its `__init__.py`, maintenance rule; until 11 Oct 2026 it was
a copy edited elsewhere), so a finding there is a proposal for the engine, not
a fix for this test. Some of its functions served only another engine, which
has since left.

And one kind pyflakes cannot see, because it looks at one module at a time: a
private function nothing calls, and a ContextVar nothing reads. `_tier()` in
mcp/server.py was the hosted product's billing tier, kept on every MCP request
in a ContextVar of its own for a write quota this install does not have; the
function had no caller and the variable no other reader.

In tests/ and scripts/ there are no re-exports, so there pyflakes has nothing
to say at all.
"""
import ast
from pathlib import Path

from pyflakes import checker, messages

ROOT = Path(__file__).resolve().parents[1]
DEAD = (messages.RedefinedWhileUnused, messages.UnusedVariable)

# Dead code the scan below has found and that is known and not yet removed,
# as (file, function). Listed rather than skipped, so the scan still fails on
# anything new; delete a line together with its function. Empty since
# 2026-09-28, when `_complete_json` (called only by a test of its own) and
# `_persist_from_batch` (called by nothing) went.
REPORTED_NOT_YET_REMOVED: set = set()


def _product_modules():
    for path in sorted((ROOT / "korely_agent").rglob("*.py")):
        if "memory_core" not in path.parts:
            yield path, ast.parse(path.read_text(), filename=str(path))


def _is_context_var(value) -> bool:
    if not isinstance(value, ast.Call):
        return False
    f = value.func
    return (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) == "ContextVar"


def test_nothing_defined_twice_or_computed_for_nobody():
    found = []
    for path in sorted((ROOT / "korely_agent").rglob("*.py")):
        if "memory_core" in path.parts:
            continue
        rel = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(), filename=rel)
        found += [str(m) for m in checker.Checker(tree, filename=rel).messages
                  if isinstance(m, DEAD)]
    assert found == [], "\n".join(found)


def test_no_private_function_nobody_calls_and_no_context_var_nobody_reads():
    """Across the whole product, not module by module: a private function
    imported and called from another module is used. A function only a test
    calls is not, because the product never runs it."""
    modules = list(_product_modules())
    functions, context_vars = {}, set()
    referenced, read_with_get = set(), set()
    for path, tree in modules:
        rel = path.relative_to(ROOT).as_posix()
        for node in tree.body:
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name.startswith("_") and not node.name.startswith("__")):
                functions[(rel, node.name)] = node.lineno
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and _is_context_var(node.value):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                context_vars |= {(rel, t.id) for t in targets if isinstance(t, ast.Name)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                referenced.add(n.id)
            elif isinstance(n, ast.Attribute):
                referenced.add(n.attr)
                if n.attr == "get" and isinstance(n.value, ast.Name):
                    read_with_get.add(n.value.id)
            elif isinstance(n, ast.alias):
                referenced.add(n.name.rsplit(".", 1)[-1])
    uncalled = {(rel, name) for rel, name in functions if name not in referenced}
    unread = sorted(f"{rel}: ContextVar {name} is set and never read"
                    for rel, name in context_vars if name not in read_with_get)
    new = sorted(f"{rel}:{functions[(rel, name)]}: {name} is called by nothing"
                 for rel, name in uncalled - REPORTED_NOT_YET_REMOVED)
    gone = sorted(f"{rel}: {name} is no longer dead, take it off REPORTED_NOT_YET_REMOVED"
                  for rel, name in REPORTED_NOT_YET_REMOVED - uncalled)
    assert new + unread + gone == [], "\n".join(new + unread + gone)


def _names_used(trees) -> set:
    used = set()
    for tree in trees:
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                used.add(n.id)
            elif isinstance(n, ast.Attribute):
                used.add(n.attr)
            elif isinstance(n, ast.alias):
                used.add(n.name.rsplit(".", 1)[-1])
    return used


def test_no_public_function_nobody_calls_anywhere():
    """The scan above sees only names that start with '_' (audit of 17 Oct
    2026: `seconds_to_tomorrow` in hosted/spending.py and `screen_writer` in
    core/llm.py were called by nothing). A public function may be a hook the
    tests call (`forget`, `reset_...`) or a script's door, so here it is dead
    only when nothing calls it: not the product, not a test, not a script. A
    decorated one (a route, a command, a fixture) is called by its decorator."""
    modules = list(_product_modules())
    others = [ast.parse(p.read_text(), filename=str(p))
              for folder in ("tests", "scripts")
              for p in sorted((ROOT / folder).rglob("*.py"))
              if "__pycache__" not in p.parts and "node_modules" not in p.parts]
    used = _names_used([t for _p, t in modules] + others)
    dead = sorted(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}: {node.name} is called by nothing"
                  for path, tree in modules for node in tree.body
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and not node.name.startswith("_") and not node.decorator_list
                  and node.name not in used)
    assert dead == [], "\n".join(dead)


def test_pyflakes_has_nothing_to_say_about_tests_and_scripts():
    """Imports left behind by a refactor, a local computed for nobody: in a
    test the second is usually an assertion somebody meant to write. The
    product too since 2026-10-06, when an import of `settings` was found in
    the contradiction adapter, unused for days.

    memory_core and the migrations too since the audit of 17 Oct 2026: this
    skipped memory_core as "checked elsewhere", where there has been no copy
    of it since 11 Oct, and skipped alembic/, where three unused imports
    and an f-string with nothing to format had gathered. An import kept on
    purpose says so with `# noqa: F401` (the migration template's
    `pgvector.sqlalchemy`, alembic/script.py.mako, says why), and that is the
    only exception."""
    paths = sorted(p for p in (ROOT / "tests").rglob("*.py") if "__pycache__" not in p.parts)
    paths += sorted(p for p in (ROOT / "scripts").rglob("*.py")
                    if "__pycache__" not in p.parts and "node_modules" not in p.parts)
    paths += sorted(p for p in (ROOT / "korely_agent").rglob("*.py")
                    if "__pycache__" not in p.parts)
    paths += sorted(p for p in (ROOT / "alembic").rglob("*.py") if "__pycache__" not in p.parts)
    found = []
    for path in paths:
        rel = str(path.relative_to(ROOT))
        source = path.read_text()
        lines = source.splitlines()
        tree = ast.parse(source, filename=rel)
        found += [str(m) for m in checker.Checker(tree, filename=rel).messages
                  if not (isinstance(m, messages.UnusedImport)
                          and "# noqa: F401" in lines[m.lineno - 1])]
    assert found == [], "\n".join(found)
