"""The upgrade of a folder install.sh made, as docs/guide/operations.md
writes it, run as a person pastes it.

Its first version (15 Nov 2026) was a list of lines: a file that was not
the release's printed FAILED, and the next lines still put it in place of
the real one and deleted the sums, with the block's status 0; and `.env`
got the image by its tag alone, while install.sh writes
`<tag>@sha256:<digest>`, so every folder upgraded lost the pin on the
image's content without a word (`./korely start` is `docker compose up`,
which has none of start_stack's warning). The block now runs in `sh -e`,
checks before it changes anything, and takes the digest from main's
install.sh, the one place it is written (scripts/stamp_installer.py,
`digest`, after CI).

The guide's block runs here in bash, in a folder like install.sh's, with
the repository's files served by a fake curl and the database by a fake
`./korely`. The two install.sh it reads are stamped by
scripts/stamp_installer.py itself, the release's own code.
"""
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "guide" / "operations.md"
FILES = ["docker-compose.yml", "docker-compose.image.yml", ".env.example", "deploy/Caddyfile"]
NEW = "9.9.9"
DIGEST = "sha256:" + "ab" * 32
OLD_IMAGE = "KORELY_IMAGE=ghcr.io/korely-ai/korely:9.9.8@sha256:" + "cd" * 32

# Files from the repository at an address of raw.githubusercontent.com,
# or curl's exit 22 (an HTTP error under -f) for one that is not there.
CURL = """#!/bin/sh
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
path=${url#https://raw.githubusercontent.com/korely-ai/korely/}
[ "$path" != "$url" ] && [ -f "$SITE/$path" ] || exit 22
if [ -n "$out" ]; then cp "$SITE/$path" "$out"; else cat "$SITE/$path"; fi
"""

# The folder's ./korely: a dump that says it is one.
KORELY = """#!/bin/sh
echo "$*" >> "$CALLS"
case "$*" in *pg_dump*) echo "the dump" ;; esac
"""


def _stamp():
    spec = importlib.util.spec_from_file_location("stamp_installer",
                                                  ROOT / "scripts" / "stamp_installer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _block() -> str:
    """The guide's first block in "An install made by install.sh"."""
    text = GUIDE.read_text(encoding="utf-8")
    section = text.split("\n### An install made by install.sh\n", 1)[1]
    m = re.search(r"^```bash\n(.*?)^```", section, re.M | re.S)
    assert m, "the section has no bash block"
    return m.group(1).replace("<new>", NEW)


def _site(tmp_path, *, main_release=NEW, digest=DIGEST, changed=None, tagged=True):
    """The public repository: the tag's four files and its install.sh, and
    main's install.sh, of `main_release`, with `digest` (or none)."""
    site = tmp_path / "site"
    tag = site / f"v{NEW}"
    for f in FILES:
        (tag / f).parent.mkdir(parents=True, exist_ok=True)
        (tag / f).write_text(f"{f} of {NEW}\n")
    stamp = _stamp()
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    at_the_tag = stamp.stamp_release(installer, NEW, tag)
    if tagged:
        (tag / "install.sh").write_text(at_the_tag)
    main = stamp.stamp_release(installer, main_release, tag)
    if digest:
        main = stamp.stamp_digest(main, digest)
    (site / "main").mkdir()
    (site / "main" / "install.sh").write_text(main)
    if changed:  # a file changed on GitHub after the tag
        (tag / changed).write_text((tag / changed).read_text() + "a line\n")
    return site


def _folder(tmp_path):
    """A folder as install.sh leaves it, of the release before."""
    home = tmp_path / "korely"
    (home / "deploy").mkdir(parents=True)
    for f in FILES:
        (home / f).write_text(f"{f} of 9.9.8\n")
    (home / ".env").write_text(f"SECRET_KEY=the-same\nPORT=8000\n{OLD_IMAGE}\n")
    (home / ".env").chmod(0o600)
    (home / ".korely-install").write_text("source=image\nrelease=9.9.8\nproject=korely\n")
    (home / "korely").write_text(KORELY)
    (home / "korely").chmod(0o755)
    return home


def _paste(tmp_path, site, block=None):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "curl").write_text(CURL)
    (bin_ / "curl").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
           "SITE": str(site), "CALLS": str(tmp_path / "calls")}
    return subprocess.run(["bash", "-c", block or _block()], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)


def _unchanged(home):
    for f in FILES:
        assert (home / f).read_text() == f"{f} of 9.9.8\n", f
    assert OLD_IMAGE in (home / ".env").read_text()
    assert "release=9.9.8" in (home / ".korely-install").read_text()
    assert not (home / f"korely-before-{NEW}.tar").exists()


pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("tar") is None,
                                reason="no bash or tar here")


def test_the_upgrade_puts_the_release_in_place_with_its_images_digest(tmp_path):
    home = _folder(tmp_path)
    r = _paste(tmp_path, _site(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    for f in FILES:
        assert (home / f).read_text() == f"{f} of {NEW}\n", f
    env = (home / ".env").read_text().splitlines()
    assert f"KORELY_IMAGE=ghcr.io/korely-ai/korely:{NEW}@{DIGEST}" in env
    assert "SECRET_KEY=the-same" in env and "PORT=8000" in env, "the rest of .env is kept"
    assert stat.S_IMODE((home / ".env").stat().st_mode) & 0o077 == 0, ".env stays its owner's"
    mark = (home / ".korely-install").read_text().splitlines()
    assert mark == ["source=image", f"release={NEW}", "project=korely"]
    assert (home / f"korely-before-{NEW}.dump").read_text() == "the dump\n"
    with tarfile.open(home / f"korely-before-{NEW}.tar") as kept:
        assert sorted(kept.getnames()) == sorted(FILES + [".env", ".korely-install"])
        assert kept.extractfile("deploy/Caddyfile").read() == b"deploy/Caddyfile of 9.9.8\n"
        assert OLD_IMAGE.encode() in kept.extractfile(".env").read()
    left = sorted(p.name for p in home.iterdir())
    assert not [n for n in left if n.endswith(".new") or n in ("release.sha256", "install-main.sh")], left


@pytest.mark.parametrize("changed", ["deploy/Caddyfile", "docker-compose.yml"])
def test_a_file_that_is_not_the_releases_stops_it_with_nothing_changed(tmp_path, changed):
    home = _folder(tmp_path)
    r = _paste(tmp_path, _site(tmp_path, changed=changed))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "FAILED" in r.stdout + r.stderr
    _unchanged(home)
    assert not (home / f"korely-before-{NEW}.dump").exists(), "nothing is done after the check"


def test_a_main_of_another_release_stops_it_before_anything(tmp_path):
    """The digest is written only in main's install.sh: for a release main
    is no longer (or not yet) at, there is none to pin."""
    home = _folder(tmp_path)
    r = _paste(tmp_path, _site(tmp_path, main_release="9.9.10"))
    assert r.returncode != 0
    assert f"main is not {NEW}" in r.stderr
    _unchanged(home)
    assert not list(home.glob("*.new"))


def test_a_release_whose_digest_is_not_stamped_yet_stops_it(tmp_path):
    home = _folder(tmp_path)
    r = _paste(tmp_path, _site(tmp_path, digest=None))
    assert r.returncode != 0
    assert "no digest" in r.stderr
    _unchanged(home)


def test_a_release_that_is_not_published_stops_it(tmp_path):
    home = _folder(tmp_path)
    r = _paste(tmp_path, _site(tmp_path, tagged=False))
    assert r.returncode != 0
    _unchanged(home)


def test_a_folder_that_builds_its_image_is_sent_to_the_checkouts_steps(tmp_path):
    home = _folder(tmp_path)
    (home / ".env").write_text("SECRET_KEY=the-same\n")
    r = _paste(tmp_path, _site(tmp_path))
    assert r.returncode != 0
    assert "names no KORELY_IMAGE" in r.stderr
    assert (home / ".env").read_text() == "SECRET_KEY=the-same\n"
