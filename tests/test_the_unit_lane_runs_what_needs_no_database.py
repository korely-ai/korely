"""The unit lane (`make test`, -m "not db") runs every test that needs no
database: a test is a `db` test only because a fixture of it reaches one
(tests/conftest.py, _DATABASE_FIXTURES), never because its file says so
(audit of 20 Oct 2026).

145 files marked themselves whole with `pytestmark = pytest.mark.db`, so
their source-only guards (the literal guard of
test_one_number_per_limit.py, six of test_audit.py, and more) ran only in the
lane with a database, and a developer's `make test` missed the regression
they exist for. Here no file marks itself, and no test marks itself `db`
by hand: the fixtures decide.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _marks_db(node) -> bool:
    return "pytest.mark.db" in ast.unparse(node)


def test_no_test_file_or_test_marks_itself_db():
    found = []
    for path in sorted((ROOT / "tests").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "pytestmark"
                                                    for t in node.targets) and _marks_db(node.value):
                found.append(f"{path.name}:{node.lineno}: pytestmark")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                if any(_marks_db(d) for d in node.decorator_list):
                    found.append(f"{path.name}: {node.name}")
    assert found == [], found


def test_a_source_only_guard_is_in_the_unit_lane():
    """The guard of the literals, the finding's own example: collected
    with no database, it is not deselected."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--collect-only",
         "-m", "not db", "tests/test_one_number_per_limit.py", "tests/test_audit.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert "test_no_route_or_service_writes_the_numbers_again" in out.stdout, out.stdout[-2000:]
    assert "test_no_label_is_unclassified" in out.stdout, out.stdout[-2000:]
