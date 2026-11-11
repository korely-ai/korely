"""/docs says the answers the code gives (audit of 18 Oct 2026):
GET /v1/ping declared only 200, 401 and 500 although a key checked
with the database down is a 503 (and, since the same audit, past the ceiling
a 429); no operation declared the 413 `payload_too_large` the 0.1.40 notes
announced; and /health said `extraction` is `unknown` "until something
calls it", where the README has four states and the store is read for it.
"""
import functools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from korely_agent.services.limits import body_cap
from tests import cloud

ROOT = Path(__file__).resolve().parents[1]

# The Cloud answers things the install does not (audit of 27 Oct 2026: its
# OpenAPI omitted the 401, 409 and 503 of POST /hosted/session because only
# the self-hosted app was built). Each mode is built in a process of its own,
# as test_every_input walks the routes: the mode is read once at start.
_WORLD = r"""
import json, os, sys
os.environ["KORELY_MODE"] = sys.argv[1]
os.environ["RUN_WORKER"] = "false"
os.environ.setdefault("HOSTED_FIREBASE_PROJECT_ID", "walk")
os.environ.setdefault("DATABASE_URL", "postgresql://walk:walk@localhost:1/none")
import logging
logging.disable(logging.CRITICAL)
from korely_agent.main import create_app
from tests.test_the_docs_say_every_answer import _raised_and_declared
spec = json.loads(json.dumps(create_app().openapi()))
rows = [[m, p, sorted(r), sorted(d)] for m, p, r, d in _raised_and_declared(spec)]
print(json.dumps({"spec": spec, "rows": rows}))
"""


@functools.lru_cache(maxsize=None)
def _world(mode):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KORELY_", "HOSTED_"))}
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run([sys.executable, "-c", _WORLD, mode], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def spec():
    return _world("self-hosted")["spec"]


@pytest.fixture(scope="module", params=cloud.MODES)
def world(request):
    return _world(request.param)


def test_ping_declares_the_ceiling_and_the_store_down(spec):
    codes = set(spec["paths"]["/v1/ping"]["get"]["responses"])
    assert {"200", "401", "429", "500", "503"} <= codes, codes


def test_every_operation_with_a_body_declares_its_ceiling(world):
    spec = world["spec"]
    seen = 0
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            answer = op.get("responses", {}).get("413")
            if "requestBody" not in op:
                assert answer is None, (method, path)
                continue
            seen += 1
            assert answer is not None, (method, path)
            assert answer["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ApiError"}
            assert "`payload_too_large`" in answer["description"]
            # The ceiling of a key that may write, and, for the batch, the
            # smaller one of a request without such a key (limits.body_cap).
            assert f"{body_cap(path, writer=True):,} bytes" in answer["description"], (path, answer)
            if body_cap(path) != body_cap(path, writer=True):
                assert f"{body_cap(path):,} without one" in answer["description"], (path, answer)
    assert seen > 10
    batch = spec["paths"]["/v1/batch"]["post"]["responses"]["413"]["description"]
    memories = spec["paths"]["/v1/memories"]["post"]["responses"]["413"]["description"]
    assert batch != memories


def test_health_has_the_four_states_of_the_readme(spec):
    text = spec["paths"]["/health"]["get"]["description"]
    assert "until something calls it" not in text
    for state in ("`working`", "`unknown`", "`not configured`", "`failing`"):
        assert state in text, state


def test_the_texts_say_what_happens_in_the_products_words(spec):
    """A, from the public docs of 18 Oct 2026: a route that takes no key was
    told "with a key that may write", and the erase endpoint named a
    regulation, which the site never does (a rule of the docs): it says what
    the call does."""
    for path, item in spec["paths"].items():
        for op in item.values():
            text = (op.get("responses") or {}).get("413", {}).get("description", "")
            if text and body_cap(path) == body_cap(path, writer=True):
                assert "key" not in text, (path, text)
    erase = spec["paths"]["/v1/users/{end_user}/memories"]["delete"]
    assert "GDPR" not in json.dumps(erase) and "Art. 17" not in json.dumps(erase)
    assert "everything about one end user in one call" in erase["description"]


# ── every refusal a route raises is declared (audit of 20 Oct 2026) ────────
#
# The status a route raises, read from its source: `status.HTTP_4xx/5xx_*`
# and `HTTPException(4xx, ...)` in the route's function and, through them,
# in the helpers of its own module it calls by name. A route that raises a
# status its `responses=` (or its router's) does not declare fails here.
# What a service raises and a route maps (webhooks.TooManyTests to 429,
# UnsafeWebhookUrl to 400) is the route's own raise, so it is seen.

#: A helper that raises a status only on a branch these routes never take,
#: one line each: (method, path, status) and why.
_NOT_RAISED = {
    ("GET", "/admin/projects/{project_id}", 409):
        "account_project's 409 `project_archived` is for `to_hold_keys=True` only",
    ("POST", "/admin/projects/{project_id}/unarchive", 409):
        "account_project's 409 `project_archived` is for `to_hold_keys=True` only",
    **{("GET", path, 409): "_within_project calls account_project without `to_hold_keys`"
       for path in ("/admin/memories/search", "/admin/facts", "/admin/memories",
                    "/admin/memories/count",
                    "/admin/memories/categories", "/admin/namespaces", "/admin/requests",
                    "/admin/requests/summary", "/admin/requests/daily", "/admin/audit")},
}


def _raised_by(node, funcs, seen) -> set:
    import ast
    import re

    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "status":
            m = re.match(r"HTTP_(\d{3})_", n.attr)
            if m and int(m.group(1)) >= 400:
                out.add(int(m.group(1)))
        if isinstance(n, ast.Call):
            f = n.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name in ("HTTPException", "CodedHTTPException"):
                given = n.args[:1] + [k.value for k in n.keywords if k.arg == "status_code"]
                out |= {g.value for g in given
                        if isinstance(g, ast.Constant) and isinstance(g.value, int) and g.value >= 400}
            if isinstance(f, ast.Name) and f.id in funcs and f.id not in seen:
                seen.add(f.id)
                out |= _raised_by(funcs[f.id], funcs, seen)
    return out


def _raised_and_declared(spec):
    """Run inside the process that built `spec` (see _WORLD)."""
    import ast
    import inspect
    import sys

    from korely_agent.api.errors import api_routes
    from korely_agent.main import create_app

    modules = {}
    for route in api_routes(create_app().routes):
        mod = sys.modules[route.endpoint.__module__]
        if mod.__name__ not in modules:
            tree = ast.parse(inspect.getsource(mod))
            modules[mod.__name__] = {n.name: n for n in tree.body
                                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        funcs = modules[mod.__name__]
        node = funcs.get(route.endpoint.__name__)
        if node is None:
            continue
        raised = _raised_by(node, funcs, {route.endpoint.__name__})
        for method in sorted(route.methods or ()):
            op = spec["paths"].get(route.path_format, {}).get(method.lower())
            if isinstance(op, dict):
                declared = {int(c) for c in op.get("responses", {}) if c.isdigit()}
                yield method, route.path_format, raised, declared


def test_every_status_a_route_raises_is_declared(world):
    seen, missing = 0, []
    for method, path, raised, declared in world["rows"]:
        seen += bool(raised)
        missing += [(method, path, s) for s in sorted(set(raised) - set(declared))
                    if (method, path, s) not in _NOT_RAISED]
    assert seen > 40, seen  # the scan sees the routes' raises (48 on 20 Oct 2026)
    assert missing == [], ("a route raises a status /docs does not declare: add it to its "
                           f"responses= or, if it cannot happen there, to _NOT_RAISED: {missing}")


def test_every_reason_not_to_declare_still_applies(world):
    found = {(m, p, s) for m, p, raised, declared in world["rows"]
             for s in set(raised) - set(declared)}
    assert sorted(set(_NOT_RAISED) - found) == []


def test_an_add_on_completes_the_description_of_a_refusal():
    """api/errors.describe_also, the door through which the Cloud's package
    (hosted.install) adds the 429s of its quotas to its own document: the
    sentence lands on the operations the function names, once, and on no
    other; the core's own document never calls it."""
    from fastapi import FastAPI

    from korely_agent.api.errors import describe_also

    app = FastAPI()

    @app.get("/a", responses={429: {"description": "Too many."}})
    def a():
        return {}

    @app.get("/b", responses={429: {"description": "Too many."}})
    def b():
        return {}

    describe_also(app, 429, lambda route: "And the month's." if route.path == "/a" else None)
    for _ in range(2):
        paths = app.openapi()["paths"]
        assert paths["/a"]["get"]["responses"]["429"]["description"] == "Too many. And the month's."
        assert paths["/b"]["get"]["responses"]["429"]["description"] == "Too many."


@pytest.mark.cloud
def test_the_cloud_says_the_answers_of_its_sign_in():
    """Audit of 27 Oct 2026: a sign-in client reads /docs to learn the 409 it
    must handle."""
    answers = _world("hosted")["spec"]["paths"]["/hosted/session"]["post"]["responses"]
    assert {"401", "409", "503"} <= set(answers), sorted(answers)


def test_the_webhooks_say_their_outbound_places_and_the_lookups_deadline(spec):
    """Audit of 20 Oct 2026: creating, enabling and testing a webhook answer
    429 past the account's places for outside waits (core/threads.
    OUTBOUND_PER_ACCOUNT), and creating and enabling 400 for a host that did
    not resolve within LOOKUP_SECONDS; /docs said only the request ceiling and
    a refused address."""
    from korely_agent.core.threads import LOOKUP_SECONDS, OUTBOUND_PER_ACCOUNT

    for method, path in (("post", "/admin/webhooks"),
                         ("post", "/admin/webhooks/{endpoint_id}/enable"),
                         ("post", "/admin/webhooks/{endpoint_id}/test")):
        answers = spec["paths"][path][method]["responses"]
        text = answers["429"]["description"]
        assert f"{OUTBOUND_PER_ACCOUNT} webhook tests or address checks" in text, (path, text)
        if not path.endswith("/test"):
            text = answers["400"]["description"]
            assert f"did not resolve within {LOOKUP_SECONDS:.0f} s" in text, (path, text)


def test_every_503_says_a_requests_deadlines(world):
    """Audit of 20 Oct 2026: any request that
    waits more than LOCK_WAIT_SECONDS for a lock answers 503 `unavailable`
    (main's handler, core/threads.busy_refusal), on every route that reads or
    writes the database, whatever else its 503 is."""
    from korely_agent.core.threads import LOCK_WAIT_SECONDS

    spec = world["spec"]
    seen = 0
    for path, item in spec["paths"].items():
        for method, op in item.items():
            answer = op.get("responses", {}).get("503") if isinstance(op, dict) else None
            if answer is not None:
                seen += 1
                assert f"more than {LOCK_WAIT_SECONDS} s" in answer["description"], (method, path)
    assert seen > 50, seen


def test_the_admin_routes_that_call_a_model_say_the_pause(spec):
    """PATCH /admin/facts/{fact_id} and GET /admin/memories/search answer
    `service_paused` too, and /docs said only the generic 503 (audit of
    30 Oct 2026)."""
    for path, method in (("/admin/facts/{fact_id}", "patch"), ("/admin/memories/search", "get")):
        said = spec["paths"][path][method]["responses"]["503"]["description"]
        assert "service_paused" in said and "unavailable" in said, (path, said)
