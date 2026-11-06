#!/usr/bin/env python3
"""Every URL the screen calls must exist on the machine serving it.

Without a browser, and without a person clicking fourteen pages. The dashboard
is a bundle of TypeScript that names its endpoints as string literals; the
install publishes an OpenAPI document that names the ones it serves. Comparing
the two catches the failure that otherwise shows up as a blank page on the one
route nobody opened before shipping.

    python scripts/check_dashboard_endpoints.py --openapi http://127.0.0.1:8000/openapi.json

Exits non-zero and prints what is missing. A path the screen calls and the
server does not serve is the whole point; a path the server serves and nobody
calls is fine and is not reported.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

# `apiClient.get("/admin/keys")` and `apiClient.post(`/admin/keys/${id}/revoke`)`
CALL = re.compile(r"""apiClient\s*\.\s*(get|post|patch|put|delete)\s*(?:<[^>]*>)?\s*\(\s*(["'`])(/[^"'`]*)\2""")

# `${anything}` is a path parameter. So is `{anything}` in the OpenAPI document.
PARAM = re.compile(r"\$\{[^}]*\}")
OPENAPI_PARAM = re.compile(r"\{[^}]*\}")


def shape(path: str) -> str:
    """A path with every parameter flattened, so the two sides can be compared."""
    path = PARAM.sub("*", path)
    path = OPENAPI_PARAM.sub("*", path)
    return path.rstrip("/") or "/"


# The hosted service's own pages (one codebase, 8 Oct 2026): the screen
# calls them only where the server says `"mode": "hosted"`, and only there are
# their routes served. Checked against a hosted install, left out against an
# install of one's own.
HOSTED_DIR = "hosted"


def calls_in(src: Path, *, hosted: bool = True) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for f in sorted(src.rglob("*.ts")) + sorted(src.rglob("*.tsx")):
        if not hosted and f.relative_to(src).parts[0] == HOSTED_DIR:
            continue
        for method, _q, path in CALL.findall(f.read_text()):
            found.add((method.lower(), shape(path)))
    return found


def served(document: dict) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for path, methods in (document.get("paths") or {}).items():
        for method in methods:
            if method.lower() in ("get", "post", "patch", "put", "delete"):
                out.add((method.lower(), shape(path)))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--openapi", default="http://127.0.0.1:8000/openapi.json")
    p.add_argument("--src", default=str(Path(__file__).resolve().parent.parent / "dashboard" / "src"))
    a = p.parse_args(argv)

    src = Path(a.src)
    if not src.is_dir():
        print(f"no dashboard source at {src}", file=sys.stderr)
        return 2

    try:
        with urllib.request.urlopen(a.openapi, timeout=20) as r:
            doc = json.loads(r.read().decode())
    except Exception as e:
        print(f"could not read {a.openapi}: {e}", file=sys.stderr)
        return 2

    have = served(doc)
    hosted = any(path.startswith("/hosted/") for _m, path in have)
    wanted = calls_in(src, hosted=hosted)
    if not wanted:
        # An empty result means the regex stopped matching, not that the screen
        # stopped calling anything. Fail rather than report success.
        print("found no API calls in the dashboard source at all: the parser "
              "is broken, not the dashboard", file=sys.stderr)
        return 2

    missing = sorted(w for w in wanted if w not in have)

    print(f"the screen calls {len(wanted)} endpoints; this install serves {len(have)}"
          + (" (the hosted service's pages included)" if hosted else ""))
    if missing:
        print(f"\n{len(missing)} the screen calls and this install does not serve:")
        for method, path in missing:
            print(f"    {method.upper():<6} {path}")
        return 1
    print("every one of them exists here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
