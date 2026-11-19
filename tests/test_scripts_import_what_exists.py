"""Every product module a script under scripts/ imports exists, and so does
every name it takes from one.

The scripts run outside the suite (at a release, by hand, on the Cloud), so a
module moved by a refactor leaves them broken with nothing red until the day
they run: on 14 Nov 2026 a release stopped on a script that still imported a
module from where it was before a refactor moved it. This reads each script's
imports with
`ast` (imports inside functions too, where scripts put the heavy ones) and
resolves them against the package as it is, without running the scripts.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "korely_agent"
SCRIPTS = sorted((ROOT / "scripts").rglob("*.py"))


def _imports(path: Path):
    """(line, module, name or None) for each import of the product package."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PACKAGE or alias.name.startswith(PACKAGE + "."):
                    yield node.lineno, alias.name, None
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and (
                node.module == PACKAGE or node.module.startswith(PACKAGE + ".")):
            for alias in node.names:
                yield node.lineno, node.module, alias.name


def _missing(module: str, name):
    """Why the import cannot resolve, or None when it does."""
    try:
        if importlib.util.find_spec(module) is None:
            return f"no module {module}"
    except ModuleNotFoundError:
        return f"no module {module}"
    if name is None or name == "*":
        return None
    try:
        if importlib.util.find_spec(f"{module}.{name}") is not None:
            return None
    except ModuleNotFoundError:
        pass
    if hasattr(importlib.import_module(module), name):
        return None
    return f"{module} has no {name}"


def test_the_scripts_are_found():
    assert SCRIPTS, "no script under scripts/: the walk reads the wrong folder"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_script_imports_what_exists(path):
    wrong = [f"line {line}: {why}" for line, module, name in _imports(path)
             if (why := _missing(module, name)) is not None]
    assert not wrong, f"{path.relative_to(ROOT)} imports what is not there: {wrong}"


def test_a_moved_module_is_seen(tmp_path):
    """The check itself fails on the import the release met (a check never
    seen to fail is no evidence)."""
    script = tmp_path / "old.py"
    script.write_text("def main():\n    from korely_agent.services.no_such_module import thing\n")
    assert [_missing(m, n) for _l, m, n in _imports(script)] == [
        "no module korely_agent.services.no_such_module"]
    script.write_text("from korely_agent.services import no_such_name\n")
    assert [_missing(m, n) for _l, m, n in _imports(script)] == [
        "korely_agent.services has no no_such_name"]
