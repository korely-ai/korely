"""Every `from X import name` in the product names something X has.

A function-level import is only run when its function is: the batch worker's
erasure path imported `facts.service.erase_what_it_leaves`, which one branch
had renamed and another had moved, and nothing failed until a job reached
it, as an ImportError written on the job, with the full suite hanging on the
lock the failed job held (the merges of 18 Oct 2026). pyflakes cannot see
it: the name is checked against the module only at run time.
"""
import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "korely_agent"


def _imports():
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
        if path.name == "__init__.py":
            package = module[: -len(".__init__")]
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.ImportFrom):
                base = (importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                        if node.level else node.module)
                yield path, node.lineno, base, [a.name for a in node.names if a.name != "*"]


def test_every_name_imported_from_the_product_exists():
    missing, read = [], 0
    for path, line, base, names in _imports():
        if not base.startswith("korely_agent"):
            continue
        module = importlib.import_module(base)
        read += 1
        for name in names:
            if hasattr(module, name):
                continue
            try:
                importlib.import_module(f"{base}.{name}")
            except ImportError:
                missing.append(f"{path.relative_to(ROOT)}:{line}: from {base} import {name}")
    assert read > 300, "found almost no imports; the scan is broken"
    assert missing == [], "\n".join(missing)
