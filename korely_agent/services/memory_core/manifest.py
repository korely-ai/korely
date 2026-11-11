"""The check that the engine was not changed without the step it requires.

Every change to memory_core bumps VERSION and regenerates MANIFEST.json
(`scripts/memory_core_manifest.py`) in the same change, and only after
review (__init__.py, "Maintenance rule"): a file that does not
match MANIFEST.json means a change that skipped that step, and
`tests/test_memory_core_shared_rules.py` says so on the first test run. Until 11
October 2026 it guarded a byte-for-byte copy in another repository.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FOLDER = Path(__file__).resolve().parent


def differences_from_the_manifest() -> list:
    """The files that do not match MANIFEST.json (empty = all in order)."""
    manifest = json.loads((FOLDER / "MANIFEST.json").read_text(encoding="utf-8"))
    expected = manifest.get("files", {})
    present = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(FOLDER.glob("*.py"))
    }
    different = sorted(set(expected) ^ set(present))
    different += sorted(k for k in expected.keys() & present.keys() if expected[k] != present[k])
    return different
