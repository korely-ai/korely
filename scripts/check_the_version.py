#!/usr/bin/env python3
"""One version, said the same way everywhere, or the check goes red.

    python scripts/check_the_version.py
    python scripts/check_the_version.py --self-test

A tester tried to upgrade from one release to the next and could not, because
there was nothing saying which commit was which release: no tags, no VERSION
file, `pyproject.toml` frozen at 0.0.0, and the only version the product
reported anywhere was the MCP library's own, arriving in the handshake as if it
were ours.

The second consequence is the one that matters: somebody who upgrades into a
fault has nowhere to go back to, and "read ninety-three commits" is not
somewhere.

So the number lives in VERSION and everything else agrees with it. This is the
check that they do.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# A release is x.y.z. A checkout that has moved past one is x.y.z-dev, so that
# `cat VERSION` gives different answers in the two situations the documentation puts
# side by side: the tag you could go back to, and where you are now.
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-dev)?$")
# install.sh carries the release it installs (29 Oct 2026). The copy on main is
# what the README's one command downloads, and main is x.y.z-dev between
# releases, which has no image and no tag: so it names the last release, set
# by scripts/stamp_installer.py at the tag and left alone by the -dev that
# follows, never one below MIN_RELEASE (the oldest it can install: 1.0.0
# had no docker-compose.image.yml and no `manage --json`, and the images
# before 1.0.9 are under the repository's old name).
INSTALLER_LINE = r'^{}="([^"]*)"$'
SHA256_LINE = re.compile(r"^([0-9a-f]{64})  (\S+)$")


def _numbers(v: str) -> tuple:
    return tuple(int(x) for x in v.split("-")[0].split("."))


def _installer_value(installer: str, name: str):
    found = re.findall(INSTALLER_LINE.format(name), installer, re.M)
    return found[0] if len(found) == 1 else None


def installer_disagreements(version: str, installer: str, read=None) -> list[str]:
    """Why install.sh would install something other than this checkout's
    release (VERSION x.y.z) or its last one (VERSION x.y.z-dev), or could not
    check what it downloads. `read(path) -> bytes` reads this checkout's files,
    for a release's sha256. Empty is pass."""
    release = _installer_value(installer, "THIS_RELEASE")
    least = _installer_value(installer, "MIN_RELEASE")
    if release is None or least is None:
        return ['install.sh names THIS_RELEASE="x.y.z" and MIN_RELEASE="x.y.z" once each']
    for name, value in (("THIS_RELEASE", release), ("MIN_RELEASE", least)):
        if not re.fullmatch(r"\d+\.\d+\.\d+", value):
            return [f"install.sh's {name} is {value!r}, which is not a release"]
    if _numbers(release) < _numbers(least):
        return [f"install.sh installs {release}, below its MIN_RELEASE {least}: it cannot"]
    bad = []
    files = re.search(r'^RELEASE_FILES="([^"]+)"$', installer, re.M)
    block = re.search(r'^RELEASE_SHA256="([^"]*)"$', installer, re.M | re.S)
    digest = _installer_value(installer, "THIS_IMAGE_DIGEST")
    if not files or not block or digest is None:
        return ["install.sh names RELEASE_FILES, RELEASE_SHA256 and THIS_IMAGE_DIGEST"]
    lines = [l for l in block.group(1).splitlines() if l.strip()]
    sums = {}
    for line in lines:
        m = SHA256_LINE.match(line)
        if not m:
            bad.append(f"install.sh's RELEASE_SHA256 has a line that is not `sha256  path`: {line!r}")
        else:
            sums[m.group(2)] = m.group(1)
    if sums and set(sums) != set(files.group(1).split()):
        bad.append("install.sh's RELEASE_SHA256 does not name exactly the RELEASE_FILES")
    if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        bad.append(f"install.sh's THIS_IMAGE_DIGEST is {digest!r}, not sha256:<64 hex>")
    if not SEMVER.match(version):
        return bad   # disagreements() says it
    if version.endswith("-dev"):
        # Before the first release that ships install.sh, main names that
        # release and carries no sha256: such a copy installs nothing
        # without --unverified (install.sh says so).
        first_not_out = release == least == version[:-len("-dev")]
        if _numbers(release) >= _numbers(version) and not first_not_out:
            bad.append(f"install.sh installs {release}, and the checkout is {version}, "
                       f"which is not out yet: it names the last release")
        if not sums and not first_not_out:
            bad.append(f"install.sh carries no sha256 of {release}'s files")
    else:
        if release != version:
            bad.append(f"install.sh installs {release}, VERSION says {version}")
        elif read is not None:
            for path in files.group(1).split():
                want = hashlib.sha256(read(path)).hexdigest()
                if sums.get(path) != want:
                    bad.append(f"install.sh's sha256 of {path} is not this release's: "
                               f"python3 scripts/stamp_installer.py release {version}")
    return bad


def disagreements(version: str, pyproject: str, readme: str) -> list[str]:
    """Every reason these do not tell one story. Empty is pass. `readme` is
    the documentation, README.md and docs/guide read as one text."""
    bad = []
    if not SEMVER.match(version):
        bad.append(f"VERSION is {version!r}, which is not a version anyone can compare")
    m = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    if not m:
        bad.append("pyproject.toml declares no version")
    elif m.group(1) != version:
        bad.append(f"pyproject.toml says {m.group(1)}, VERSION says {version}")
    # Any case: it is the middle of a heading in sentence case.
    if "how to go back" not in readme.lower():
        bad.append("the documentation says how to upgrade and not how to go back, "
                   "which is the half somebody needs while something is broken")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        cases = [
            (("0.1.0", 'version = "0.1.0"', "and how to go back"), 0, "everything agrees"),
            (("0.1.0", 'version = "0.1.0"', "How to go back"), 0, "whatever the case"),
            (("0.1.0", 'version = "0.0.0"', "how to go back"), 1, "pyproject drifted"),
            (("dev", 'version = "dev"', "how to go back"), 1, "not a comparable version"),
            (("0.1.0", 'version = "0.1.0"', "nothing here"), 1, "no way back written down"),
        ]
        sums = 'RELEASE_FILES="a b"\nRELEASE_SHA256="\n%s  a\n%s  b\n"\nTHIS_IMAGE_DIGEST=""' % ("1" * 64, "2" * 64)
        none = 'RELEASE_FILES="a b"\nRELEASE_SHA256="\n"\nTHIS_IMAGE_DIGEST=""'

        def script(release, least="1.0.1", tail=sums):
            return f'THIS_RELEASE="{release}"\nMIN_RELEASE="{least}"\n{tail}'

        installer_cases = [
            (("1.0.1", script("1.0.1")), 0, "the release installs itself"),
            (("1.0.2-dev", script("1.0.1")), 0, "main installs the last release"),
            (("1.0.1-dev", script("1.0.1", tail=none)), 0, "the first release, not out yet"),
            (("1.0.2-dev", script("1.0.1", tail=none)), 1, "a release's copy without its sha256"),
            (("1.0.1", script("1.0.0", least="1.0.0")), 1, "a release that installs the one before"),
            (("1.0.1-dev", script("1.0.0", least="1.0.1")), 1, "below MIN_RELEASE"),
            (("1.0.2-dev", script("1.0.2")), 1, "a release not out yet"),
            (("1.0.1-dev", script("1.0.1-dev")), 1, "a -dev has no image"),
            (("1.0.1", script("latest")), 1, "latest is no version"),
            (("1.0.1", "no line"), 1, "no release named"),
            (("1.0.2-dev", script("1.0.1", tail=sums.replace('THIS_IMAGE_DIGEST=""',
                                                              'THIS_IMAGE_DIGEST="latest"'))),
             1, "a digest that is not one"),
        ]
        bad = []
        for args_, expected, what in cases:
            got = len(disagreements(*args_))
            if (got > 0) != (expected > 0):
                bad.append(f"{what}: expected {'a complaint' if expected else 'silence'}")
        for args_, expected, what in installer_cases:
            got = len(installer_disagreements(*args_))
            if (got > 0) != (expected > 0):
                bad.append(f"{what}: expected {'a complaint' if expected else 'silence'}")
        for b in bad:
            print("SELF-TEST FAIL:", b)
        total = len(cases) + len(installer_cases)
        print(f"self-test: {total - len(bad)}/{total}")
        return 1 if bad else 0

    version = (ROOT / "VERSION").read_text().strip()
    bad = disagreements(version,
                        (ROOT / "pyproject.toml").read_text(),
                        "\n".join(p.read_text() for p in
                                  [ROOT / "README.md", *sorted((ROOT / "docs/guide").glob("*.md"))]))
    bad += installer_disagreements(version, (ROOT / "install.sh").read_text(),
                                   lambda path: (ROOT / path).read_bytes())

    # And the tag, which is the thing a customer actually needs to go back to.
    # A missing tag is a warning rather than a failure: the tag is made at
    # release time, and this check runs before that.
    try:
        tags = subprocess.run(["git", "tag", "--list", f"v{version}"],
                              cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        tags = ""
    if bad:
        print("The version does not tell one story:")
        for b in bad:
            print("  -", b)
        return 1
    print(f"version {version}, said the same way in VERSION, pyproject, the documentation "
          f"and install.sh")
    if not tags:
        print(f"  (no tag v{version} yet — `git tag v{version}` at release, "
              f"or nobody can go back to it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
