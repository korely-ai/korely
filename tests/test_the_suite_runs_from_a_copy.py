"""The suite and the migrations run from a copy of the checkout made inside
their container, never from the checkout (scripts/from_a_copy.sh).

On a Mac the checkout reaches a container through Docker Desktop's file
sharing, which loses the working directory of a process in it for a moment
whenever a container starts or stops, and a process that asked for it then
died: an alembic process of tests/test_migrations.py, in a gate of
8 Nov 2026. The script copies the checkout, mounted read-only at /src, into
the container's own /w, and runs the command there; every recipe of the
Makefile that runs the suite or the migrations goes through it.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "from_a_copy.sh"
MAKEFILE = ROOT / "Makefile"

# What runs Python from the tree in a recipe: the suite, its script, the
# migrations' check (`clean` names .pytest_cache, and runs nothing).
_RUNS_THE_TREE = ("python -m pytest", "scripts/test_all.sh", "scripts/check_migration.py")


def _recipes() -> dict[str, str]:
    """Each target of the Makefile and its recipe, the tab-indented lines
    under it."""
    found = {}
    for m in re.finditer(r"^([a-z][a-z-]*):[^\n=]*\n((?:\t[^\n]*\n)+)", MAKEFILE.read_text(), re.M):
        found[m.group(1)] = m.group(2)
    return found


def test_every_recipe_that_runs_the_tree_runs_it_from_the_copy():
    """Found from the recipes themselves: a new target that runs the suite
    or the migrations is held as these are."""
    text = MAKEFILE.read_text()
    assert "\nCHECKOUT_RO := -v $(CURDIR):/src:ro -w /w -e PYTHONPATH=/w\n" in text
    assert "\nFROM_A_COPY := sh /src/scripts/from_a_copy.sh\n" in text
    running = {t: r for t, r in _recipes().items() if any(s in r for s in _RUNS_THE_TREE)}
    assert {"test", "test-all", "migrate-check"} <= set(running), sorted(running)
    for target, recipe in running.items():
        assert "$(CHECKOUT_RO)" in recipe and "$(IMG) $(FROM_A_COPY) " in recipe, target
        assert ":/w" not in recipe, f"{target} mounts the checkout as the folder it runs in"


def _files(top: Path) -> list[str]:
    return sorted(str(p.relative_to(top)) for p in top.rglob("*") if p.is_file())


@pytest.mark.skipif(shutil.which("tar") is None, reason="no tar here")
@pytest.mark.parametrize("git", ["folder", "file"], ids=["a checkout's .git", "a worktree's .git"])
def test_the_copy_leaves_out_what_it_says_and_runs_the_command_in_it(tmp_path, git):
    src, to = tmp_path / "src", tmp_path / "w"
    kept = {"pkg/mod.py": "X = 1\n", ".github/ci.yml": "on: push\n", ".env.example": "A=1\n",
            ".gitignore": "x\n", "not_tracked.txt": "u\n", "dashboard/src/main.ts": "t\n",
            "tests/data/answers.json": "{}\n", "docs/dist.md": "d\n"}
    left_out = [".claude/worktrees/other/pkg/mod.py", ".claude/settings.local.json",
                "dashboard/node_modules/vite/package.json", "scripts/node_modules/x/index.js",
                "dashboard/dist/index.js", "pkg/__pycache__/mod.cpython-311.pyc",
                ".pytest_cache/v/cache/nodeids",
                ".git/objects/1b/loose" if git == "folder" else ".git"]
    for rel in [*kept, *left_out]:
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(kept.get(rel, "x"))
    before = _files(src)
    r = subprocess.run(["sh", str(SCRIPT), "sh", "-c", "pwd"], cwd=tmp_path, capture_output=True,
                       text=True, timeout=60, env={**os.environ, "COPY_FROM": str(src), "COPY_TO": str(to)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(to)
    assert _files(to) == sorted(kept)
    assert all((to / rel).read_text() == text for rel, text in kept.items())
    assert _files(src) == before


@pytest.mark.skipif(shutil.which("tar") is None, reason="no tar here")
def test_a_copy_that_fails_runs_nothing(tmp_path):
    """Without the checkout to read, the copy fails and the command does not
    run: a half copy would run part of the suite and pass."""
    marker = tmp_path / "ran"
    r = subprocess.run(["sh", str(SCRIPT), "touch", str(marker)], cwd=tmp_path, capture_output=True,
                       text=True, timeout=60,
                       env={**os.environ, "COPY_FROM": str(tmp_path / "missing"), "COPY_TO": str(tmp_path / "w")})
    assert r.returncode != 0 and not marker.exists(), (r.returncode, r.stderr)
