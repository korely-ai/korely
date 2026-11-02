"""Every /v1 and /admin route counts against the account's ceiling.

The ceiling is a dependency each router declares (`enforce_rate_limit` on
/v1, `enforce_admin_rate_limit` on /admin), written by hand on every router:
a new router that forgets it is a door with no ceiling, and nothing said so
(audit of 17 Oct 2026, first wave's leftover). This walks the routes of the
app as it is built, on an install of one's own and on the Cloud, whichever
file declared them and wherever they were included, so a route nobody
remembered fails here. The ones with no ceiling on purpose are named, each
with why.

Built in a process of its own, as tests/test_dump_openapi.py builds the spec:
the mode is read with the settings, and this process has read them already.
"""
import functools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import cloud

ROOT = Path(__file__).resolve().parent.parent

# No credential to count against, or a check of the credential itself.
WITHOUT_A_CEILING = {
    # GET /v1/ping is not here since the audit of 18 Oct 2026: it
    # writes an audit row per call, and a route with no ceiling that writes
    # per request lets a key past its ceiling keep growing the shared table.
    # Logging in and out, and the scope names: no session yet to count
    # against. The login has its own lockouts (services/operators.py).
    "POST /admin/session", "DELETE /admin/session", "GET /admin/scopes",
}
# The Cloud's own, where the tree carries its package.
CLOUD_WITHOUT_A_CEILING = {
    # The Cloud's sign-up without a login: there is no account yet.
    "POST /v1/agents/init",
}
# The modes this tree can build: KORELY_MODE=hosted is refused without the
# Cloud's package (tests/cloud.py).
MODES = ("self_hosted", "hosted") if cloud.HERE else ("self_hosted",)

_WALK = r"""
import json, os, sys
os.environ["KORELY_MODE"] = sys.argv[1]
os.environ["RUN_WORKER"] = "false"
os.environ.setdefault("DATABASE_URL", "postgresql://ratelimit:walk@localhost:1/none")
import logging
logging.disable(logging.CRITICAL)
from fastapi.routing import APIRoute
from korely_agent.core.auth import enforce_admin_rate_limit, enforce_rate_limit
from korely_agent.main import create_app

LIMITS = {enforce_rate_limit, enforce_admin_rate_limit}

def calls(dependant):
    out = {dependant.call}
    for sub in dependant.dependencies:
        out |= calls(sub)
    return out

def walk(routes, inherited):
    for r in routes:
        # An included router (FastAPI >= 0.140 keeps it as one node, with
        # the dependencies given at include time) or a plain route.
        inner = getattr(r, "original_router", None)
        if inner is not None:
            ctx = getattr(r, "include_context", None)
            extra = {d.dependency for d in getattr(ctx, "dependencies", []) or []}
            yield from walk(inner.routes, inherited | extra)
        elif isinstance(r, APIRoute):
            yield r, inherited | calls(r.dependant)

found, bare = [], []
for route, deps in walk(create_app().router.routes, set()):
    if not route.path.startswith(("/v1", "/admin")):
        continue
    for m in sorted(route.methods - {"HEAD", "OPTIONS"}):
        found.append(f"{m} {route.path}")
        if not deps & LIMITS:
            bare.append(f"{m} {route.path}")
print(json.dumps({"found": found, "bare": bare}))
"""


@functools.lru_cache(maxsize=None)
def _routes(mode):
    done = subprocess.run([sys.executable, "-c", _WALK, mode], capture_output=True, text=True,
                          check=True, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)})
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_every_route_of_an_install_of_ones_own_has_the_ceiling():
    got = _routes("self_hosted")
    assert {"POST /v1/memories", "GET /v1/audit", "POST /admin/keys"} <= set(got["found"]), \
        "the walk missed the routers: it is broken, not the app"
    assert set(got["bare"]) - WITHOUT_A_CEILING == set()


@pytest.mark.cloud
def test_every_route_of_the_cloud_has_the_ceiling():
    got = _routes("hosted")
    assert {"DELETE /v1/account", "POST /v1/memories"} <= set(got["found"])
    assert set(got["bare"]) - WITHOUT_A_CEILING - CLOUD_WITHOUT_A_CEILING == set()


def test_no_exception_is_left_over():
    """A route named here that is gone, or that has the ceiling now, is
    taken out of the list, so it never covers a new route by its name."""
    assert WITHOUT_A_CEILING <= set(_routes("self_hosted")["bare"]), \
        sorted(WITHOUT_A_CEILING - set(_routes("self_hosted")["bare"]))
    if "hosted" in MODES:
        bare = set(_routes("hosted")["bare"])
        assert CLOUD_WITHOUT_A_CEILING <= bare, sorted(CLOUD_WITHOUT_A_CEILING - bare)
