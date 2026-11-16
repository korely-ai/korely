"""What one module of the package may take from another (audit of 17 Oct 2026).

A name that starts with `_` is its module's own. Imported from elsewhere it
becomes a contract nobody declared: the OAuth service took `_off_the_loop`
from the MCP's door (a service depending on the surface above it), the OAuth
routes took `_audit` from api/admin.py, and fifteen more `_` names crossed
modules, among them the end-user sentinel's alias `_ALL`, which
services/sentinels.py exists to retire. A helper another module needs
is made public in its module, or moves to where both can read it.

The hosted service's package is loaded by an install of one's own only where
it has to be: nothing failed if a new module began to load the
Cloud's code on every install.

Read from the source, no database and no app: the whole package, but the
facts engine (`services/memory_core/`, which changes only after review and
has its own guards).
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "korely_agent"


def _modules():
    for f in sorted(PACKAGE.rglob("*.py")):
        rel = f.relative_to(ROOT)
        if "memory_core" in rel.parts:
            continue
        parts = list(rel.with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        yield f, ".".join(parts), is_package


def _is_module(dotted: str) -> bool:
    path = ROOT.joinpath(*dotted.split("."))
    return path.with_suffix(".py").exists() or (path / "__init__.py").exists()


def _imports(tree, name: str, is_package: bool):
    """Every `from X import y` and `import X` of the file, anywhere in it (the
    package imports a lot inside functions), as (line, module, imported
    name or None)."""
    here = name.split(".") if is_package else name.split(".")[:-1]
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            base = here[:len(here) - n.level + 1] if n.level else []
            module = ".".join(base + ([n.module] if n.module else [])) if n.level else n.module
            for a in n.names:
                yield n.lineno, module, a.name
        elif isinstance(n, ast.Import):
            for a in n.names:
                yield n.lineno, a.name, None


def _private(name) -> bool:
    return bool(name) and name.startswith("_") and not name.startswith("__")


def test_no_module_takes_another_ones_private_name():
    found = []
    for f, name, is_package in _modules():
        tree = ast.parse(f.read_text())
        bound = {}
        for line, module, imported in _imports(tree, name, is_package):
            if not (module or "").startswith("korely_agent"):
                continue
            if imported is None:
                continue
            if _is_module(f"{module}.{imported}"):
                bound[imported] = f"{module}.{imported}"
            elif _private(imported) and module != name:
                found.append(f"{f.relative_to(ROOT)}:{line}: from {module} import {imported}")
        aliases = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name in bound:
                        aliases[a.asname or a.name] = bound[a.name]
        for n in ast.walk(tree):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                    and n.value.id in aliases and _private(n.attr)):
                found.append(f"{f.relative_to(ROOT)}:{n.lineno}: {aliases[n.value.id]}.{n.attr}")
    assert found == [], "a private name read from another module:\n" + "\n".join(found)


# The layers under the doors. A module here is called by api/ and mcp/, never
# the other way (audit of 27 Oct 2026: core/auth.py imported api.errors and
# only services/ was scanned).
BELOW_THE_DOORS = ("korely_agent.services", "korely_agent.core", "korely_agent.db")

# Where a lower layer may still import a door, and why.
DOOR_IMPORTED_BY = {
    # `refused` builds the HTTP answer and its trail row from the door's error
    # shape; moving it down is a change of its own to core/auth.py.
    ("korely_agent.core.auth", "korely_agent.api.errors"),
}


def test_no_lower_layer_imports_a_door():
    """A service, the core or the database layer is called by the doors (api/,
    mcp/), never the other way: the OAuth service imported the MCP server for
    a helper."""
    found = set()
    for f, name, is_package in _modules():
        if not name.startswith(BELOW_THE_DOORS):
            continue
        for _line, module, _ in _imports(ast.parse(f.read_text()), name, is_package):
            if (module or "").startswith(("korely_agent.api", "korely_agent.mcp")):
                found.add((name, module))
    assert found == DOOR_IMPORTED_BY, (
        f"new: {sorted(found - DOOR_IMPORTED_BY)}, gone: {sorted(DOOR_IMPORTED_BY - found)}")


# Where a module outside korely_agent/hosted/ may load the Cloud's code, and
# why. The package is optional (the decision of 28 Oct 2026: the
# public repository does not carry it), so the core loads it at one point,
# `core/extensions.cloud`, which answers None where it is not; every other
# module asks there (create_app for the routes and hooks, db for the tables,
# dashboard_url for the Cloud's domain, the restore for the Cloud's closes and
# merges). An import of the package anywhere else would break the product in
# a tree without it.
HOSTED_LOADED_BY = {
    ("korely_agent.core.extensions", "korely_agent.hosted"),
}


def _loads_of_the_cloud(tree, name: str, is_package: bool):
    """The Cloud's modules the file loads: by an import statement, or by
    naming the package in a string (importlib.import_module)."""
    for _line, module, imported in _imports(tree, name, is_package):
        target = module or ""
        if imported and _is_module(f"{target}.{imported}"):
            target = f"{target}.{imported}"
        if target == "korely_agent.hosted" or target.startswith("korely_agent.hosted."):
            yield target
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and (
                n.value == "korely_agent.hosted" or n.value.startswith("korely_agent.hosted.")):
            yield n.value


def test_only_the_listed_modules_load_the_hosted_package():
    loads = set()
    for f, name, is_package in _modules():
        if name.startswith("korely_agent.hosted"):
            continue
        for target in _loads_of_the_cloud(ast.parse(f.read_text()), name, is_package):
            loads.add((name, target))
    assert loads == HOSTED_LOADED_BY, (
        f"new: {sorted(loads - HOSTED_LOADED_BY)}, gone: {sorted(HOSTED_LOADED_BY - loads)}")


def test_the_guard_sees_both_ways_of_loading_the_cloud():
    """The guard above, on two modules it must catch: one that imports the
    package, one that names it to importlib."""
    by_import = ast.parse("def f():\n    from ..hosted import replay\n")
    by_name = ast.parse("import importlib\nimportlib.import_module('korely_agent.hosted.models')\n")
    # The module itself where the tree has it, the package where it does not.
    found = set(_loads_of_the_cloud(by_import, "korely_agent.services.x", False))
    assert found in ({"korely_agent.hosted.replay"}, {"korely_agent.hosted"}), found
    assert set(_loads_of_the_cloud(by_name, "korely_agent.services.x", False)) == {
        "korely_agent.hosted.models"}


# Which parts of the Cloud each module asks the one load point for
# (`extensions.cloud(part)`, None for the package itself): the scan above sees
# the import inside core/extensions.py only, and a part is a bare string, so a
# new coupling through the loader would pass it unseen. Each one here is a
# place the core reaches the Cloud, and why.
CLOUD_PARTS_ASKED_BY = {
    # The Cloud's mode refused at start without its package.
    ("korely_agent.config", None),
    # dashboard_url(): the Cloud's dashboard domain, behind settings.hosted.
    ("korely_agent.config", "settings"),
    # create_app: the hosted routes and hooks, behind settings.hosted.
    ("korely_agent.main", None),
    # The command line keeps the mode's ceiling on a key's lifetime, behind
    # settings.hosted (the review of 14 Nov 2026: issue-key skipped it).
    ("korely_agent.manage", None),
    # The Cloud's tables on the metadata: one chain of migrations.
    ("korely_agent.db", "models"),
    # A restore makes the Cloud's closes and merges again (audit of 20 Oct
    # 2026).
    ("korely_agent.services.erasure_replay", "replay"),
    # The loader's own: the Cloud's tables without their models.
    ("korely_agent.core.extensions", None),
}

# A part named by an expression, not a string: no list could say what it is.
NOT_A_CONSTANT = "<not a constant>"


def _parts_asked(tree, name: str):
    """(module, part) for every call of the loader in the file:
    `extensions.cloud(...)` (or `_extensions.cloud`), and a bare `cloud(...)`
    inside core/extensions.py itself."""
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        asks = ((isinstance(f, ast.Attribute) and f.attr == "cloud"
                 and isinstance(f.value, ast.Name) and f.value.id in ("extensions", "_extensions"))
                or (isinstance(f, ast.Name) and f.id == "cloud"
                    and name == "korely_agent.core.extensions"))
        if not asks:
            continue
        args = list(n.args) + [k.value for k in n.keywords if k.arg == "part"]
        if not args or (isinstance(args[0], ast.Constant) and args[0].value is None):
            yield name, None
        elif isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
            yield name, args[0].value
        else:
            yield name, NOT_A_CONSTANT


def test_only_the_listed_modules_ask_for_a_part_of_the_cloud():
    asked = set()
    for f, name, _is_package in _modules():
        if name.startswith("korely_agent.hosted"):
            continue
        asked |= set(_parts_asked(ast.parse(f.read_text()), name))
    assert asked == CLOUD_PARTS_ASKED_BY, (
        f"new: {sorted(asked - CLOUD_PARTS_ASKED_BY, key=str)}, "
        f"gone: {sorted(CLOUD_PARTS_ASKED_BY - asked, key=str)}")


def test_the_parts_guard_sees_every_way_of_asking():
    """The guard above, on calls it must catch: a new part, the package, a
    part by keyword or by an expression, the loader's own bare call; and a
    function of another name that is not the loader."""
    src = ("from ..core import extensions\n"
           "from ..core import extensions as _extensions\n"
           "def f(name):\n"
           "    extensions.cloud('polar')\n"
           "    _extensions.cloud()\n"
           "    extensions.cloud(part='claim')\n"
           "    extensions.cloud(name)\n"
           "    other.cloud('nothing')\n")
    assert set(_parts_asked(ast.parse(src), "korely_agent.services.x")) == {
        ("korely_agent.services.x", "polar"), ("korely_agent.services.x", None),
        ("korely_agent.services.x", "claim"), ("korely_agent.services.x", NOT_A_CONSTANT)}
    own = ast.parse("def g():\n    return cloud('models')\n")
    assert set(_parts_asked(own, "korely_agent.core.extensions")) == {
        ("korely_agent.core.extensions", "models")}
    assert set(_parts_asked(own, "korely_agent.services.x")) == set()


def test_the_mode_is_read_through_its_property():
    """`settings.hosted` is the one reading of KORELY_MODE: a comparison with
    the string written by hand can drift from it (api/oauth.py had one)."""
    found = []
    for f, _name, _pkg in _modules():
        if f.name == "config.py":
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Compare) and any(
                    isinstance(x, ast.Attribute) and x.attr == "korely_mode"
                    for x in [n.left, *n.comparators]):
                found.append(f"{f.relative_to(ROOT)}:{n.lineno}")
    assert found == [], found


@pytest.mark.cloud
def test_the_hosted_tables_read_only_the_base_of_the_db_package():
    """db/__init__.py loads hosted/models.py while it is itself being
    imported. That works only while hosted/models.py takes nothing from the
    half-built `korely_agent.db` but its leaf module `db.base`: a name from
    `korely_agent.db` itself would be read before it exists."""
    f = PACKAGE / "hosted" / "models.py"
    taken = {module for _l, module, _n in _imports(ast.parse(f.read_text()),
                                                   "korely_agent.hosted.models", False)
             if (module or "").startswith("korely_agent.db")}
    assert taken == {"korely_agent.db.base"}, taken


@pytest.mark.cloud
def test_the_cloud_tables_and_only_they_carry_the_cloud_prefix():
    """A tree without the Cloud's package tells the Cloud's tables (made by the
    one chain of migrations, declared by no model there) by their prefix
    (core/extensions.cloud_tables_without_models): every table of
    hosted/models.py has it, and no table of the core's."""
    from korely_agent.core import extensions
    from korely_agent.db import Base
    from korely_agent.hosted import models

    cloud = {m.__tablename__ for m in vars(models).values()
             if isinstance(m, type) and issubclass(m, Base) and m is not Base}
    # A part's own tables (core/extensions.Part.tables) carry it too when the
    # part was born in the Cloud's package; a part that came out of the core
    # kept the names its tables had there.
    cloud |= {t for t in extensions.part_tables() if t.startswith(extensions.CLOUD_TABLE_PREFIX)}
    prefixed = {t for t in Base.metadata.tables if t.startswith(extensions.CLOUD_TABLE_PREFIX)}
    assert cloud and prefixed == cloud, (sorted(prefixed - cloud), sorted(cloud - prefixed))
