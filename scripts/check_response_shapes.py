#!/usr/bin/env python3
"""Do the routes send the fields the screen declares it reads?

    python scripts/check_response_shapes.py --base http://127.0.0.1:8000 --key <admin key>

`check_dashboard_endpoints.py` asks whether the paths exist. They did, all of
them, while five endpoints answered 200 with the wrong fields in them and the
pages went blank or rendered nothing at all:

    /admin/projects       a bare list where {projects, total} was read
    /admin/namespaces     two of the four numbers
    /admin/requests       `ts` for a field read as `at`
    /admin/requests/daily two names for one list
    /admin/keys           no `masked`, no `tier`, no `revoked`

Every one of them is the same mistake and none of them raises anything. The
browser check catches the ones that throw; the ones that quietly render an
empty cell it cannot see. This compares the TypeScript interface, which is what
the page believes, against the JSON, which is what it gets.

Optional fields (`name?:`) are not required to be present. Exits non-zero when
a required one is missing.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _endpoints_check():
    """check_dashboard_endpoints.py, beside this file: one rule for which of
    the screen's pages an install serves, read by both checks."""
    path = Path(__file__).resolve().with_name("check_dashboard_endpoints.py")
    spec = importlib.util.spec_from_file_location("check_dashboard_endpoints", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# `apiClient.get<Thing>("/admin/x")` and `.get<{ a: A[]; total: number }>(...)`
CALL = re.compile(
    r"""apiClient\s*\.\s*get\s*<\s*([^>]+?)\s*>\s*\(\s*["'`](/admin/[^"'`${]*)["'`]""",
    re.S,
)
FIELD = re.compile(r"^\s*(\w+)(\?)?\s*:", re.M)


def _top_level(body: str) -> str:
    """The interface's own lines: what sits inside a nested `{ ... }` (an inline
    object type, `active: { provider: string; ... } | null`) belongs to that
    field, not to the reply. Read as top-level fields they made /admin/models
    look like it lacked five fields it sends inside `active` (the acceptance of
    0.1.41, 18 Oct 2026)."""
    out, depth = [], 0
    for ch in body:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def interfaces(src: Path) -> dict:
    """name -> {field: required} for every exported interface."""
    out = {}
    text = "\n".join(f.read_text() for f in src.rglob("*.ts"))
    for m in re.finditer(r"export interface (\w+)\s*\{(.*?)\n\}", text, re.S):
        name, body = m.group(1), m.group(2)
        # Strip comments, or a `// name?: thing` line becomes a field.
        body = re.sub(r"//[^\n]*", "", body)
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        out[name] = {f: (opt != "?") for f, opt in FIELD.findall(_top_level(body))}
    return out


def wanted(src: Path, ifaces: dict, *, hosted: bool = True) -> list:
    """(path, kind, fields) for each GET the dashboard makes; without
    `hosted`, the hosted service's own pages are left out, as
    check_dashboard_endpoints.py leaves them out against an install of one's
    own, where their routes are not served (two parts' reads made the 1.0.22
    acceptance fail with two 404s on a self-hosted install, 14 Nov 2026)."""
    hosted_dir = _endpoints_check().HOSTED_DIR
    found = []
    for f in sorted(src.rglob("*.ts")) + sorted(src.rglob("*.tsx")):
        if not hosted and f.relative_to(src).parts[0] == hosted_dir:
            continue
        for generic, path in CALL.findall(f.read_text()):
            generic = " ".join(generic.split())
            # `Thing[]` -> a list of Thing
            m = re.fullmatch(r"(\w+)\[\]", generic)
            if m and m.group(1) in ifaces:
                found.append((path, "list", ifaces[m.group(1)]))
                continue
            if generic in ifaces:
                found.append((path, "object", ifaces[generic]))
                continue
            # An inline `{ projects: AgentProject[]; total: number }`
            m = re.fullmatch(r"\{\s*(\w+)\s*:\s*(\w+)\[\]\s*;\s*total\s*:\s*number;?\s*\}", generic)
            if m:
                key, inner = m.group(1), m.group(2)
                found.append((path, f"envelope:{key}", ifaces.get(inner, {})))
    # De-duplicate, keeping the first shape seen for a path.
    seen, out = set(), []
    for item in found:
        if item[0] in seen:
            continue
        seen.add(item[0])
        out.append(item)
    return out


def fetch(base: str, path: str, key: str):
    req = urllib.request.Request(base.rstrip("/") + path,
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8000")
    p.add_argument("--key", required=True, help="an admin-scoped API key")
    p.add_argument("--src", default=str(Path(__file__).resolve().parent.parent
                                       / "dashboard" / "src"))
    a = p.parse_args(argv)

    src = Path(a.src)
    ifaces = interfaces(src)
    # Hosted or not, read as check_dashboard_endpoints.py reads it: from the
    # routes the install publishes. Unknown (no /openapi.json): every page.
    hosted = True
    try:
        doc = fetch(a.base, "/openapi.json", a.key)
        hosted = any(path.startswith("/hosted/") for path in (doc.get("paths") or {}))
    except Exception:
        pass
    checks = wanted(src, ifaces, hosted=hosted)
    if not checks:
        print("found no typed GETs at all: the parser is broken, not the routes",
              file=sys.stderr)
        return 2

    problems = []
    checked = 0
    for path, kind, fields in checks:
        required = {f for f, req in fields.items() if req}
        if not required:
            continue
        try:
            body = fetch(a.base, path, a.key)
        except urllib.error.HTTPError as e:
            # 404 with a path parameter in it is this script's fault, not the
            # route's: those are skipped by the regex, but be explicit.
            problems.append((path, f"HTTP {e.code}"))
            continue
        except Exception as e:
            problems.append((path, str(e)[:60]))
            continue

        if kind.startswith("envelope:"):
            key = kind.split(":", 1)[1]
            if not isinstance(body, dict) or key not in body:
                problems.append((path, f"no '{key}' in the reply"))
                continue
            sample = body[key][0] if body[key] else None
        elif kind == "list":
            if not isinstance(body, list):
                problems.append((path, f"a {type(body).__name__} where a list is read"))
                continue
            sample = body[0] if body else None
        else:
            sample = body

        checked += 1
        if sample is None:
            print(f"  {path}: nothing in it yet, so nothing to compare")
            continue
        missing = sorted(required - set(sample.keys()))
        if missing:
            problems.append((path, "missing " + ", ".join(missing)))
        else:
            print(f"  ok  {path}")

    print(f"\n{checked} shapes compared against what the screen declares")
    if problems:
        print(f"\n{len(problems)} that do not match:")
        for path, why in problems:
            print(f"    {path}: {why}")
        return 1
    print("every field the screen reads is in the reply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
