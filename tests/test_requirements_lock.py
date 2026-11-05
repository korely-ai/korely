"""The dependency locks cannot drift in silence (scripts/requirements_lock.py).

requirements.txt and requirements-dev.txt are edited by hand; the locks next to
them are generated, and both images install only a lock, in pip's
hash-checking mode. Before that the product image installed requirements.txt
with every transitive package floating, the test image installed a separate
list with looser ranges (`sqlalchemy>=2`, the range BUG-114 came through), and
the test image was built once and reused: the suite that vouched for 0.1.8 ran
on SQLAlchemy 2.0.52 and Starlette 1.6 while the 0.1.8 image carried 2.0.54
and 1.7.

Here, in the test image (it installs requirements-dev.lock.txt):
- each lock still satisfies its requirements file, and the dev lock agrees
  with the product lock on every package they share;
- this environment is exactly the dev lock (nothing installed by hand,
  nothing left over, nothing stale) and `pip check` is clean;
- each lock holds exactly what its requirements need, walking the installed
  packages' own metadata.
The product image is checked from outside by `make image-check IMAGE=...`,
which `scripts/release.sh` runs on the image it is about to tag.

When a check fails the message says which fix: regenerate the locks
(`make lock`), or rebuild this image (`make image`, which the Makefile does by
itself when the lock changes). The checkout is mounted, so an edit on the host
is seen at once; what is installed changes only with a rebuild.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement

# The layer the test image installs (Dockerfile.dev).
TEST_IMAGE_LAYER = "dev"


@pytest.fixture(scope="module")
def guard():
    path = Path(__file__).resolve().parents[1] / "scripts" / "requirements_lock.py"
    spec = importlib.util.spec_from_file_location("_requirements_lock", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _report(problems: list[str]) -> str:
    return "\n" + "\n".join(problems)


class TestTheLocksAndThisEnvironment:
    def test_every_lock_satisfies_its_requirements_file(self, guard):
        problems = [p for layer in guard.LAYERS for p in guard.lock_problems(layer)]
        assert not problems, _report(problems)

    def test_this_environment_is_exactly_the_lock(self, guard):
        problems = guard.installed_versus_lock(TEST_IMAGE_LAYER)
        assert not problems, _report(problems)

    def test_pip_check_is_clean(self, guard):
        problems = guard.pip_check()
        assert not problems, _report(problems)

    @pytest.mark.parametrize("layer", ["dev", "product"])
    def test_the_lock_holds_exactly_what_the_requirements_need(self, guard, layer):
        if guard.installed_versus_lock(TEST_IMAGE_LAYER):
            # The walk reads the installed packages' metadata: on a stale image
            # it would blame the lock. The test above fails and says why.
            pytest.skip("this environment is not the lock yet: rebuild the test image")
        # The product layer is inside the dev one (-r, and held to the same
        # versions by -c), so its packages are all installed here.
        problems = guard.lock_versus_needs(layer)
        assert not problems, _report(problems)

    def test_every_package_the_product_imports_is_declared(self, guard):
        """requirements.txt says it: a dependency we import is a dependency we
        declare, or the day the package that brings it drops it, an image
        build drops it too. google-auth (the Cloud's sign-in and the removal
        of a closed account's Firebase user) came only through google-genai
        (audit of 17 Oct 2026). Every import in korely_agent/, read
        from the code, is traced to the installed distribution that ships
        the module."""
        import ast
        import importlib.metadata as metadata

        from packaging.utils import canonicalize_name

        root = Path(__file__).resolve().parents[1]
        modules = set()
        for path in (root / "korely_agent").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(), str(path))):
                if isinstance(node, ast.Import):
                    modules.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    modules.add(node.module)
        owner = {}
        for dist in metadata.distributions():
            for f in dist.files or ():
                owner.setdefault(str(dist.locate_file(f)), canonicalize_name(dist.metadata["Name"]))
        entries, _ = guard.read_requirements(guard.requirements_file("product"))
        declared = {canonicalize_name(e.requirement.name) for e in entries}
        undeclared = {}
        for name in sorted(modules):
            top = name.split(".")[0]
            if top in sys.stdlib_module_names or top == "korely_agent":
                continue
            try:
                spec = importlib.util.find_spec(name)
            except (ImportError, ValueError):
                spec = None
            # A namespace package (`google`) is no one's: its modules are.
            if spec is None or spec.origin in (None, "namespace"):
                continue
            dist = owner.get(spec.origin)
            if dist and dist not in declared:
                undeclared.setdefault(dist, []).append(name)
        assert not undeclared, (
            f"imported by korely_agent/ but not in requirements.txt: {undeclared} "
            f"(declare them, then `make lock`)")

    def test_the_test_client_runs_on_httpx2(self):
        """Starlette's TestClient imports httpx2 and falls back to httpx with a
        StarletteDeprecationWarning, printed at the end of every run. A
        warning everybody has learnt to scroll past is where the next one
        hides, and a fallback that is deprecated is one release from gone."""
        from starlette import testclient

        assert testclient.httpx.__name__ == "httpx2", (
            "starlette.testclient fell back to httpx: httpx2 is not installed "
            "in this image (requirements-dev.txt, then `make lock` and `make image`)")

    def test_both_images_install_a_lock_with_hashes(self):
        """The guard is only worth something if the images read the files it guards."""
        root = Path(__file__).resolve().parents[1]
        product = (root / "Dockerfile").read_text()
        dev = (root / "dev" / "Dockerfile.dev").read_text()
        assert "--require-hashes -r requirements.lock.txt" in product
        assert "--require-hashes -r requirements-dev.lock.txt" in dev
        for text in (product, dev):
            assert "-r requirements.txt" not in text and "-r requirements-dev.txt" not in text


class TestTheGuardNamesEachKindOfDrift:
    """The guard itself, on small files written for the occasion."""

    @staticmethod
    def _write(guard, directory: Path, layer: str, requirements: str, lock: str) -> None:
        guard.requirements_file(layer, directory).parent.mkdir(exist_ok=True)
        guard.requirements_file(layer, directory).write_text(requirements)
        guard.lock_file(layer, directory).write_text(lock)

    def test_a_package_added_without_regenerating(self, guard, tmp_path):
        self._write(guard, tmp_path, "product", "fastapi\nbrandnew\n", "fastapi==1.0 --hash=sha256:aa\n")
        problems = guard.lock_problems("product", tmp_path)
        assert len(problems) == 1 and "brandnew" in problems[0]

    def test_a_range_the_pin_no_longer_satisfies(self, guard, tmp_path):
        # BUG-114's shape: the range says <2.1, something wrote 2.1.
        self._write(guard, tmp_path, "product", "sqlalchemy>=2.0,<2.1\n", "sqlalchemy==2.1.1 --hash=sha256:aa\n")
        problems = guard.lock_problems("product", tmp_path)
        assert len(problems) == 1 and "2.1.1" in problems[0]

    def test_a_lock_entry_that_is_not_pinned_and_hashed(self, guard, tmp_path):
        self._write(guard, tmp_path, "product", "fastapi\nhttpx\n", "fastapi==1.0\nhttpx>=0.25 --hash=sha256:aa\n")
        problems = guard.lock_problems("product", tmp_path)
        assert any("fastapi==1.0 has no --hash" in p for p in problems)
        assert any("httpx>=0.25 is not pinned" in p for p in problems)

    def test_the_dev_lock_disagreeing_with_the_product_lock(self, guard, tmp_path):
        product_lock = guard.lock_file("product", tmp_path)
        self._write(guard, tmp_path, "product", "fastapi\n", "fastapi==1.0 --hash=sha256:aa\n")
        self._write(
            guard, tmp_path, "dev",
            f"-r {guard.requirements_file('product', tmp_path).name}\n-c {product_lock.name}\npytest\n",
            "fastapi==1.1 --hash=sha256:bb\npytest==9.0 --hash=sha256:cc\n",
        )
        problems = guard.lock_problems("dev", tmp_path)
        assert len(problems) == 1 and "fastapi==1.1" in problems[0] and product_lock.name in problems[0]

    def test_a_dev_layer_that_drops_the_product_constraint(self, guard, tmp_path):
        # The old requirements-dev.txt: its own ranges, nothing tying it to
        # what ships. Held to the product lock whether or not it says so.
        product_lock = guard.lock_file(guard.PRODUCT, tmp_path)
        self._write(guard, tmp_path, guard.PRODUCT, "sqlalchemy<2.1\n", "sqlalchemy==2.0.54 --hash=sha256:aa\n")
        self._write(guard, tmp_path, "dev", "sqlalchemy>=2\n", "sqlalchemy==2.0.52 --hash=sha256:bb\n")
        problems = guard.lock_problems("dev", tmp_path)
        assert len(problems) == 2
        assert any(f"-c {product_lock.name}" in p for p in problems)
        assert any("sqlalchemy==2.0.52" in p and product_lock.name in p for p in problems)

    def test_a_package_installed_by_hand_or_left_over(self, guard, tmp_path, monkeypatch):
        self._write(guard, tmp_path, "product", "fastapi\n", "fastapi==1.0 --hash=sha256:aa\n")
        monkeypatch.setattr(guard, "_installed", lambda: {"fastapi": "1.0", "stripe": "15.2.0", "pip": "24.0"})
        problems = guard.installed_versus_lock("product", tmp_path)
        assert len(problems) == 1 and "stripe" in problems[0]

    def test_a_stale_version_in_the_environment(self, guard, tmp_path, monkeypatch):
        self._write(guard, tmp_path, "product", "fastapi\n", "fastapi==1.0 --hash=sha256:aa\n")
        monkeypatch.setattr(guard, "_installed", lambda: {"fastapi": "0.9"})
        problems = guard.installed_versus_lock("product", tmp_path)
        assert len(problems) == 1 and "0.9" in problems[0]

    def test_an_image_checked_from_its_freeze(self, guard, tmp_path):
        # What `make image-check` feeds it: another environment's pip freeze.
        self._write(guard, tmp_path, "product", "sqlalchemy<2.1\n", "sqlalchemy==2.0.54 --hash=sha256:aa\n")
        freeze = "SQLAlchemy==2.0.52\npip==24.0\nsetuptools==79.0.1\nwheel==0.46.3\npackaging==26.3\n"
        problems = guard.installed_versus_lock("product", tmp_path, guard.parse_freeze(freeze))
        assert len(problems) == 1 and "2.0.52" in problems[0]
        same = guard.installed_versus_lock("product", tmp_path, guard.parse_freeze("SQLAlchemy==2.0.54\n"))
        assert same == []

    def test_a_package_the_requirements_no_longer_need(self, guard, tmp_path):
        # Real metadata: pytest is installed here, six is not one of its dependencies.
        pulled_in = guard.needed([Requirement("pytest")])
        assert "pluggy" in pulled_in and "six" not in pulled_in
        lock = "".join(f"{name}==0 --hash=sha256:aa\n" for name in sorted(pulled_in | {"six"}))
        self._write(guard, tmp_path, "product", "pytest\n", lock)
        problems = guard.lock_versus_needs("product", tmp_path)
        assert len(problems) == 1 and problems[0].startswith("six ")
