#!/usr/bin/env python3
"""MANIFEST.json of memory_core, written again from what the folder holds.

    python scripts/memory_core_manifest.py           # write it
    python scripts/memory_core_manifest.py --check   # only say whether it matches

memory_core is modified only here, after review (since 11 Oct 2026 this is
the engine's only home). Every change updates VERSION in the folder's
__init__.py and runs this in the same commit: the manifest is a sha256 of
each file and the version, and tests/test_memory_core_shared_rules.py fails
on a file that does not match it. Until 11 Oct a sync script wrote it while
copying the folder here. Same layout as that script wrote: two spaces, keys
sorted, a newline at the end.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

FOLDER = Path(__file__).resolve().parent.parent / "korely_agent" / "services" / "memory_core"


def version() -> str:
    """VERSION, read from __init__.py without importing the engine."""
    tree = ast.parse((FOLDER / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
            return node.value.value
    raise SystemExit("VERSION not found in memory_core/__init__.py")


def manifest() -> dict:
    return {"files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(FOLDER.glob("*.py"))},
            "version": version()}


def stale_version(path: Path, new: dict):
    """The refusal when a file changed and VERSION did not, or None. The test
    compares the folder with the manifest, so a change that rewrote the
    manifest under the same version passed it: f1ce3ae changed __init__.py
    under 5.2.0 (a code review, 17 Oct 2026)."""
    if not path.exists():
        return None
    old = json.loads(path.read_text(encoding="utf-8"))
    if old.get("version") == new["version"] and old.get("files") != new["files"]:
        changed = sorted(n for n in set(old.get("files", {})) | set(new["files"])
                         if old.get("files", {}).get(n) != new["files"].get(n))
        return (f"memory_core changed ({', '.join(changed)}) and VERSION is still "
                f"{new['version']}: raise it (SemVer) with its entry in __init__.py, then run this again")
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--check", action="store_true", help="do not write, only compare")
    args = p.parse_args(argv)
    text = json.dumps(manifest(), indent=2, sort_keys=True) + "\n"
    path = FOLDER / "MANIFEST.json"
    if args.check:
        same = path.read_text(encoding="utf-8") == text
        print("MANIFEST.json matches the folder" if same else "MANIFEST.json is stale")
        return 0 if same else 1
    stale = stale_version(path, manifest())
    if stale:
        print(stale, file=sys.stderr)
        return 1
    path.write_text(text, encoding="utf-8")
    print(f"MANIFEST.json written: {len(manifest()['files'])} files, version {version()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
