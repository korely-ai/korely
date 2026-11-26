"""scripts/release.sh refuses a release whose CHANGELOG entry is not on top
under its own heading.

v1.0.17 was tagged with "## 1.0.17 (Unreleased)" as the CHANGELOG's newest
heading: release.sh writes VERSION, pyproject.toml and install.sh, never the
CHANGELOG's text, and the public copy made from the tag carried the heading
until the security reading before the push saw it (11 Nov 2026). The check
runs before the script touches anything, so these tests run its first lines
only, in a folder of their own, with a git that answers as a clean checkout
without the tag would.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

GIT = r"""#!/bin/sh
case "$1" in
  rev-parse) exit 1 ;;   # the tag does not exist yet
  status) exit 0 ;;      # a clean working tree
esac
exit 0
"""


def _run(tmp_path, newest_heading):
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "release.sh", work / "scripts" / "release.sh")
    (work / "install.sh").write_text('PUBLIC_REPO="example/example"\n')
    (work / "VERSION").write_text("9.9.8-dev\n")
    (work / "CHANGELOG.md").write_text(
        "Releases, newest first.\n\n" + newest_heading + "\n\n- A change.\n\n## 9.9.8\n\n- Before.\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "git").write_text(GIT)
    (bin_ / "git").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}"}
    r = subprocess.run(["bash", str(work / "scripts" / "release.sh"), "9.9.9"],
                       cwd=work, env=env, capture_output=True, text=True, timeout=60)
    return r, (work / "VERSION").read_text()


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash here")
@pytest.mark.parametrize("heading", ["## 9.9.9 (Unreleased)", "## Unreleased", "## 9.9.8 (Unreleased)"],
                         ids=["the release, unreleased", "a bare unreleased", "the release missing"])
def test_a_release_without_its_heading_on_top_is_refused_before_anything_changes(tmp_path, heading):
    r, version = _run(tmp_path, heading)
    assert r.returncode == 1
    assert f'newest entry is "{heading}", not "## 9.9.9"' in r.stderr
    assert version == "9.9.8-dev\n"


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash here")
def test_the_releases_own_heading_on_top_passes_the_check(tmp_path):
    # Past the check the script goes on to write VERSION, then stops in this
    # bare folder (no pyproject.toml): far enough to show the check let it by.
    r, version = _run(tmp_path, "## 9.9.9")
    assert "newest entry" not in r.stderr
    assert version == "9.9.9\n"
