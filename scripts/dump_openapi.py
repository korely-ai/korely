#!/usr/bin/env python3
"""The OpenAPI spec of this code, as a file: what /openapi.json answers,
without a server and without a database.

    python scripts/dump_openapi.py                  # an install of one's own
    python scripts/dump_openapi.py --hosted         # the Cloud (KORELY_MODE=hosted)
    python scripts/dump_openapi.py --hosted --out spec.json
    python scripts/dump_openapi.py --public --out agents-openapi.json   # the site's

In the test image, from the repository:

    docker run --rm -v "$PWD":/w -w /w -e PYTHONPATH=/w korely-agent-dev:latest \\
        python scripts/dump_openapi.py --hosted > korely-hosted-openapi.json

The Cloud's spec has the routes only the service mounts (/v1/account,
/v1/agents/init, /hosted/...); the site's public spec is its /v1 and /admin
part (`--public`). Dump it from the tag the Cloud runs, so the site describes
what answers. Both need the Cloud's package (korely_agent/hosted/): a tree
without it, the public repository, refuses them and dumps its own spec. The same code gives the same
bytes: keys sorted, two spaces, a newline at the end.

`--public` is that public spec itself, made here (17 Oct 2026): the Cloud's
memory API (/v1) and its administration API (/admin), each a section with its
description (main.API_SECTIONS). A spec of /v1 alone left out
/admin, which a key with the `admin` scope calls from a script as the
dashboard does, was in no document a developer reads (audit of 17 Oct 2026).
The dashboard's own routes (/hosted: the sign-in, the billing) and the OAuth
consent a person answers stay out. Its keys keep the app's order, which is
the order the site's reference page shows.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def spec(hosted: bool) -> dict:
    # Before the settings are read, which is at the first import of the app.
    os.environ["KORELY_MODE"] = "hosted" if hosted else "self_hosted"
    os.environ["RUN_WORKER"] = "false"
    # The app is built, never started: nothing connects to this address.
    os.environ.setdefault("DATABASE_URL", "postgresql://openapi:dump@localhost:1/none")
    logging.disable(logging.CRITICAL)
    from korely_agent.main import create_app

    return create_app().openapi()


# The sections the site publishes, by the tags the routers carry: the memory
# API, the account calls the Cloud adds to it (/v1/agents/init,
# /v1/account) and the administration API.
PUBLIC_SECTIONS = ("v1", "account", "admin")
_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _refs(obj, acc: set) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "$ref" and isinstance(v, str) and v.startswith("#/components/schemas/"):
                acc.add(v.rsplit("/", 1)[-1])
            else:
                _refs(v, acc)
    elif isinstance(obj, list):
        for x in obj:
            _refs(x, acc)


def public(full: dict) -> dict:
    """The site's spec from the Cloud's: the operations of PUBLIC_SECTIONS,
    the schemas they reach, and one security scheme, `bearerAuth`, on every
    operation the app declares a credential for (`security: []` on the
    others: the sign-up, the list of scopes, the sign-in)."""
    paths = {}
    for path, item in full["paths"].items():
        ops = {}
        for method, op in item.items():
            if method in _METHODS and set(op.get("tags") or ()) & set(PUBLIC_SECTIONS):
                ops[method] = {**op, "security": [{"bearerAuth": []}] if op.get("security") else []}
        if ops:
            paths[path] = ops
    schemas = full.get("components", {}).get("schemas", {})
    needed: set = set()
    _refs(paths, needed)
    while True:
        before = len(needed)
        for name in list(needed):
            _refs(schemas.get(name, {}), needed)
        if len(needed) == before:
            break
    return {
        "openapi": full["openapi"],
        "info": {
            "title": "Korely Agents API",
            "version": full["info"]["version"],
            "description": (
                "Memory for AI agents that knows what was true when, on the Cloud. Two APIs: "
                "the memory API under /v1, which an agent calls with a key of its project, and "
                "the administration API under /admin, which the dashboard calls and a key with "
                "the `admin` scope can call from a script. Every call that needs a credential "
                "carries it as `Authorization: Bearer`: a `kor_live_` key, or the dashboard's "
                "session."),
        },
        "servers": [{"url": "https://api.korely.ai"}],
        "security": [{"bearerAuth": []}],
        "tags": [t for t in full.get("tags", []) if t["name"] in PUBLIC_SECTIONS],
        "paths": paths,
        "components": {
            "schemas": {k: v for k, v in schemas.items() if k in needed},
            "securitySchemes": {"bearerAuth": {
                "type": "http", "scheme": "bearer",
                "description": "A `kor_live_` key (the `admin` scope for /admin), or a "
                               "dashboard session."}},
        },
    }


def _without_the_cloud():
    """core/extensions.NO_CLOUD when this tree does not carry the Cloud's
    package, else None."""
    from korely_agent.core import extensions

    return extensions.NO_CLOUD if extensions.cloud() is None else None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--hosted", action="store_true", help="the Cloud's spec (KORELY_MODE=hosted)")
    p.add_argument("--public", action="store_true",
                   help="the spec the site publishes: the Cloud's /v1 and /admin")
    p.add_argument("--out", help="write here instead of standard output")
    args = p.parse_args(argv)
    refused = (args.hosted or args.public) and _without_the_cloud()
    if refused:
        # The settings refuse the mode at start too; said here, not as a traceback.
        print(f"dump_openapi: {refused}", file=sys.stderr)
        return 2
    if args.public:
        text = json.dumps(public(spec(True)), indent=2, ensure_ascii=False) + "\n"
    else:
        text = json.dumps(spec(args.hosted), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
