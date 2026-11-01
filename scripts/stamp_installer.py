#!/usr/bin/env python3
"""Write into install.sh what only a release knows (29 Oct 2026).

    python3 scripts/stamp_installer.py release 1.0.1
    python3 scripts/stamp_installer.py digest sha256:<64 hex>
    python3 scripts/stamp_installer.py check 1.0.1 --installer X --root R [--tagged T]

`release` (scripts/release.sh runs it, at the tag): THIS_RELEASE, and the
sha256 of each file install.sh downloads (RELEASE_FILES), read here, where
they are the tag's. install.sh checks them after every download, so a file
changed on GitHub after the tag, or between GitHub and the user, installs
nothing. It also empties THIS_IMAGE_DIGEST, which was the previous release's.

`digest`, the step after the release: the image's digest exists only once CI
has pushed it to ghcr.io. Read it with

    docker buildx imagetools inspect ghcr.io/korely-ai/korely-agent:<version>

(the `Digest:` line), stamp it, commit, push. install.sh then runs the image
as `<tag>@sha256:...`, and a tag moved on the registry is refused by Docker.
Until it is stamped, install.sh pulls by the tag and says so.

`check`, for what ships a copy of install.sh (scripts/make_mac_installer.sh
bundles one in Korely Installer.app): passes only if the copy is exactly the
one `release` stamps at that version from the files under --root (the
tag's), with or without the digest stamped after it, and, with --tagged,
only if the rest of it is the tag's install.sh too; it prints its sha256.
An unstamped copy, another release's, a stale sum or a changed line is
refused.

Runs with the system's python3, without the product's packages, like
choose_models.py. `--installer` and `--root` are for the tests.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = re.compile(r"^\d+\.\d+\.\d+$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def release_files(installer: str) -> list[str]:
    m = re.search(r'^RELEASE_FILES="([^"]+)"$', installer, re.M)
    if not m:
        raise SystemExit("install.sh names no RELEASE_FILES")
    return m.group(1).split()


def sums_block(installer: str, root: Path) -> str:
    """`sha256  path` lines of RELEASE_FILES as they are under `root`."""
    lines = [f"{hashlib.sha256((root / f).read_bytes()).hexdigest()}  {f}"
             for f in release_files(installer)]
    return "\n".join(lines)


def _set(text: str, pattern: str, line: str) -> str:
    new, n = re.subn(pattern, lambda _m: line, text, count=1, flags=re.M | re.S)
    if n != 1:
        raise SystemExit(f"install.sh has no line matching {pattern!r}")
    return new


def stamp_release(installer: str, version: str, root: Path) -> str:
    if not RELEASE.match(version):
        raise SystemExit(f"{version!r} is not a release, x.y.z")
    text = _set(installer, r'^THIS_RELEASE="[^"]*"$', f'THIS_RELEASE="{version}"')
    text = _set(text, r'^RELEASE_SHA256="[^"]*"$',
                f'RELEASE_SHA256="\n{sums_block(installer, root)}\n"')
    return _set(text, r'^THIS_IMAGE_DIGEST="[^"]*"$', 'THIS_IMAGE_DIGEST=""')


def stamp_digest(installer: str, digest: str) -> str:
    if not DIGEST.match(digest):
        raise SystemExit(f"{digest!r} is not sha256:<64 hex>")
    return _set(installer, r'^THIS_IMAGE_DIGEST="[^"]*"$', f'THIS_IMAGE_DIGEST="{digest}"')


def _digest_of(installer: str) -> str:
    m = re.search(r'^THIS_IMAGE_DIGEST="([^"]*)"$', installer, re.M)
    return m.group(1) if m else ""


def release_copy_disagreement(installer: str, version: str, root: Path,
                              tagged: str | None = None) -> str:
    """Why `installer` is not the copy release.sh stamps for `version` from
    the files under `root` (plus, maybe, the digest stamped after the push).
    The stamps alone say nothing of the rest of the script: `tagged`, the
    install.sh of the tag v<version>, is what the whole copy must be, its
    digest aside. Empty is pass."""
    try:
        expected = stamp_release(installer, version, root)
        digest = _digest_of(installer)
        if digest:
            expected = stamp_digest(expected, digest)
    except SystemExit as e:
        return str(e)
    except OSError as e:
        return f"a release file is missing under {root}: {e.filename}"
    if installer != expected:
        return (f"install.sh is not the copy release.sh stamps for {version} "
                f"(THIS_RELEASE and the sha256 of {', '.join(release_files(installer))} "
                f"as they are under {root})")
    if tagged is not None and _set(installer, r'^THIS_IMAGE_DIGEST="[^"]*"$',
                                   'THIS_IMAGE_DIGEST=""') != tagged:
        return f"install.sh is not the copy of the tag v{version}"
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=("release", "digest", "check"))
    ap.add_argument("value")
    ap.add_argument("--installer", type=Path, default=ROOT / "install.sh")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--tagged", type=Path, help="check: the tag's install.sh")
    a = ap.parse_args(argv)
    text = a.installer.read_text()
    if a.what == "check":
        why = release_copy_disagreement(text, a.value, a.root,
                                        a.tagged.read_text() if a.tagged else None)
        if why:
            print(f"{a.installer}: {why}", file=sys.stderr)
            return 1
        print(hashlib.sha256(a.installer.read_bytes()).hexdigest())
        return 0
    text = (stamp_release(text, a.value, a.root) if a.what == "release"
            else stamp_digest(text, a.value))
    a.installer.write_text(text)
    print(f"install.sh: {a.what} {a.value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
