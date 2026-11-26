"""Every free-text input of every door follows one rule (services/limits.py):
a NUL byte is a 422 that names where it is, and a moment outside what can be
stored and read back is a 422, never a 500 and never a stopped drain (audit
of 20 Oct 2026).

The rule was asked door by door, and every audit found the doors left: the
fact names, the declared names and the /admin labels, /admin/audit,
/admin/aliases and /admin/memories' `family` (after the audit of 19 Oct 2026
closed their neighbours), a timestamp of the year 0 in UTC that stopped the
facts drain for every account. It is asked now in four places only:
every request body (api/_bodies.Checked), every path and query
(main._NulInTheAddress), every MCP call (mcp/server._arguments_checked), and
the OAuth forms the MCP SDK parses (services/oauth.KorelyOAuthProvider): POST
/authorize's form was read by none of the first three and took a NUL in
`state` to the driver (audit of 27 Oct 2026). The first tests here walk the
routes, the doors that are not routes, every field of the SDK's forms and
every argument of every tool, so a body that is not a Checked, a door no one
reads, or a field the rule never sees, fails here; the others are the
findings, each failing before its fix.
"""
from __future__ import annotations

import ast
import asyncio
import functools
import json
import os
import subprocess
import sys
import typing
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from korely_agent.services import limits
from tests import cloud

ROOT = Path(__file__).resolve().parent.parent
NUL = "a\x00b"
YEAR_ZERO = "0001-01-01T00:00:00+14:00"   # the year 0 in UTC
PAST_9999 = "9999-12-31T23:59:59-14:00"   # the year 10000 in UTC


# ── the rule itself ────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,where", [
    ("a\x00b", "f"),
    (["ok", "a\x00b"], "f[1]"),
    ({"tier": "a\x00b"}, "f.tier"),
    ({"a\x00b": 1}, "f"),
    ([{"x": ["ok", {"y": "a\x00b"}]}], "f[0].x[1].y"),
])
def test_a_nul_byte_is_found_wherever_it_is(value, where):
    assert limits.nul_problem("f", value) == f"{where} contains a NUL byte (U+0000)"


LONE = "a\ud800b"   # a lone UTF-16 surrogate, which JSON can spell ("\ud800")
_LONE_SAID = "contains a lone UTF-16 surrogate (U+D800), which is not text and cannot be stored"


@pytest.mark.parametrize("value,where", [
    (LONE, "f"),
    (["ok", LONE], "f[1]"),
    ({"tier": LONE}, "f.tier"),
    ({LONE: 1}, "f"),
    ([{"x": ["ok", {"y": "\udfff"}]}], "f[0].x[1].y"),
])
def test_a_lone_surrogate_is_found_wherever_it_is(value, where):
    said = limits.nul_problem("f", value)
    assert said is not None and said.startswith(f"{where} contains a lone UTF-16 surrogate"), said


def test_a_surrogate_pair_is_text():
    """Two escapes that make one character past U+FFFF are that character."""
    assert limits.nul_problem("f", json.loads('"\\ud83d\\ude00"')) is None


def test_the_fields_of_a_body_are_named_from_its_root():
    from korely_agent.api.batch import BatchBody

    with pytest.raises(Exception) as e:
        BatchBody(memories=[{"content": "fine"}, {"content": "x", "run_id": NUL}])
    assert e.value.detail == "memories[1].run_id contains a NUL byte (U+0000)"


@pytest.mark.parametrize("moment", [
    "0001-01-01T00:00:00+14:00", "0001-01-01T00:00:00+01:00", "0001-01-01T00:00:00Z",
    "9999-12-31T23:59:59-14:00", "9999-12-31T00:00:00Z"])
def test_a_moment_past_the_years_that_read_back_is_refused(moment):
    dt = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    assert "outside the moments that can be stored" in limits.moment_problem("timestamp", dt)


@pytest.mark.parametrize("moment", [
    "0001-01-02T00:00:00Z", "0001-01-02T10:00:00+09:00", "1970-01-01", "9999-12-30T12:00:00Z"])
def test_a_moment_inside_them_is_not(moment):
    raw = moment.replace("Z", "+00:00")
    dt = (datetime.fromisoformat(raw) if "T" in raw
          else datetime.fromisoformat(raw + "T00:00:00+00:00"))
    assert limits.moment_problem("timestamp", dt) is None


def test_no_zone_shows_a_bound_outside_the_years_python_reads():
    """The bounds are a day inside: Postgres shows a moment in the session's
    zone, and no zone is a day away from UTC."""
    for hours in (-15, 15):
        zone = timezone(timedelta(hours=hours))
        assert limits.EARLIEST_MOMENT.astimezone(zone).year >= 1
        assert limits.LATEST_MOMENT.astimezone(zone).year <= 9999


# ── the routes, walked ─────────────────────────────────────────────────────

_WALK = r"""
import json, os, sys, typing
os.environ["KORELY_MODE"] = sys.argv[1]
os.environ["RUN_WORKER"] = "false"
os.environ.setdefault("HOSTED_FIREBASE_PROJECT_ID", "walk")
os.environ.setdefault("DATABASE_URL", "postgresql://walk:walk@localhost:1/none")
import logging
logging.disable(logging.CRITICAL)
from pydantic import BaseModel
from korely_agent.api._bodies import Checked
from korely_agent.api.errors import api_routes
from korely_agent.main import create_app

def models(annotation):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in typing.get_args(annotation):
        yield from models(arg)

def body_params(dep):
    yield from dep.body_params
    for sub in dep.dependencies:
        yield from body_params(sub)

def unbounded(model):
    # The fields of `model` holding many values of a type, with no
    # max_length: each value of the wrong type is one more validation error.
    from annotated_types import MaxLen

    import collections.abc as abc

    def many(annotation):
        # Any iterable but a string, any mapping (list, Sequence, Mapping,
        # Iterable...), holding values of a type; one of Any holds nothing
        # a value of the wrong type could be.
        origin = typing.get_origin(annotation) or annotation
        if isinstance(origin, type) and not issubclass(origin, (str, bytes, bytearray, BaseModel)):
            args = typing.get_args(annotation)
            if issubclass(origin, abc.Mapping):
                return args[1:] not in ((), (typing.Any,))
            if issubclass(origin, abc.Iterable):
                return args not in ((), (typing.Any,), (typing.Any, Ellipsis))
        return any(many(a) for a in typing.get_args(annotation))

    for name, f in model.model_fields.items():
        if many(f.annotation) and not any(isinstance(m, MaxLen) for m in f.metadata):
            yield f"{model.__module__}.{model.__qualname__}.{name}"

def nested(model, seen):
    for f in model.model_fields.values():
        for m in models(f.annotation):
            if m not in seen:
                seen.add(m)
                yield m
                yield from nested(m, seen)

def unread(model):
    # Each field of `model` sent alone with what no column can store (a NUL
    # byte, a lone UTF-16 surrogate), whatever its type: the rule's 422
    # naming the field, before any check of the field's own. A field the
    # rule reached only after Pydantic's checks passed took a lone surrogate
    # to the driver (NamespaceId, plain str, list[str], dict), or answered
    # Pydantic's own refusal instead of the rule's (the audit before 1.0.0).
    from fastapi import HTTPException

    for value, said in (("a\x00b", "contains a NUL byte"),
                        ("a\ud800b", "contains a lone UTF-16 surrogate")):
        for name, f in model.model_fields.items():
            sent = f.alias or name
            try:
                model.model_validate({sent: value})
                got = "taken"
            except HTTPException as e:
                got = (None if e.status_code == 422 and str(e.detail).startswith(f"{sent} {said}")
                       else f"{e.status_code} {str(e.detail)[:60]}")
            except Exception as e:  # noqa: BLE001 - what answered instead of the rule
                got = type(e).__name__
            if got:
                yield f"{model.__module__}.{model.__qualname__}.{name} {said[9:]}: {got}"

out = {"bodies": [], "nested": [], "plain": [], "doors": [], "unbounded": [], "unread": []}
app = create_app()
# The doors that are not FastAPI's routes (OAuth's, the MCP's, the pages):
# their bodies are read by no Checked, each is named in _OTHER_DOORS.
for r in app.router.routes:
    # A part's routes served to a list of accounts are routes all the same,
    # read below (api_routes goes into them: core/extensions.PartRoutes).
    if (getattr(r, "original_router", None) is None
            and type(r).__name__ not in ("APIRoute", "PartRoutes")):
        out["doors"].append(r.path)
for r in api_routes(app.router.routes):
    for p in body_params(r.dependant):
        name = f"{sorted(r.methods - {'HEAD'})[0]} {r.path}"
        found = list(models(p.field_info.annotation))
        if not found:
            out["plain"].append(f"{name}: {p.name}")
        for m in found:
            (out["bodies"] if issubclass(m, Checked) else out["plain"]).append(
                f"{name}: {m.__module__}.{m.__qualname__}")
            out["unbounded"].extend(f"{name}: {u}" for u in unbounded(m))
            if issubclass(m, Checked):
                out["unread"].extend(f"{name}: {u}" for u in unread(m))
            for inner in nested(m, set()):
                out["unbounded"].extend(f"{name}: {u}" for u in unbounded(inner))
                if issubclass(inner, Checked):
                    out["nested"].append(f"{name}: {inner.__qualname__} in {m.__qualname__}")
print(json.dumps(out))
"""


@functools.lru_cache(maxsize=None)
def _bodies(mode):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KORELY_", "HOSTED_"))}
    out = subprocess.run([sys.executable, "-c", _WALK, mode], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("mode", cloud.MODES)
def test_every_body_of_every_route_is_read_by_the_rule(mode):
    """A body that is not a Checked takes a NUL byte to the driver. No
    exception: the Cloud's own bodies (its sign-up, its pages) tolerate
    unknown fields and are Checked all the same."""
    got = _bodies(mode)
    assert got["bodies"], "the walk found no body: it walks nothing"
    assert got["plain"] == [], got["plain"]


# Every door of the app that is not one of FastAPI's routes, and what reads
# its input by the rule. The address of each is read by _NulInTheAddress;
# this says who reads its body. A new door is named here, with its reader.
_OTHER_DOORS = {
    # GET only: no body, no input but the address.
    "/openapi.json": "no body",
    "/.well-known/oauth-authorization-server": "no body",
    "/docs-ui": "no body (static files)",
    # The SDK's OAuth forms: no Checked reads them, the provider does
    # (services/oauth.KorelyOAuthProvider), each field walked below
    # (test_every_field_of_the_sdk_forms_is_read_by_the_rule).
    "/authorize": "sdk form",
    "/token": "sdk form",
    "/revoke": "sdk form",
    # Its JSON read by the rule before the SDK reads it too
    # (api/oauth._RegistrationByTheRule): the SDK echoes `scope`.
    "/register": "sdk form",
    # The tool calls: mcp/server._arguments_checked, every argument of every
    # tool walked below (test_every_argument_of_every_tool_is_read_by_the_rule);
    # an initialize's params, mcp/server._opening_read (tests/
    # test_mcp_sessions_are_bounded).
    "/mcp": "mcp arguments and initialize's params",
}


#: The doors the Cloud does not open (main.create_app): the API's live
#: document and its page's files.
_NOT_ON_THE_CLOUD = {"/openapi.json", "/docs-ui"}


@pytest.mark.parametrize("mode", cloud.MODES)
def test_every_door_that_is_not_a_route_is_named(mode):
    doors = set(_OTHER_DOORS) - (_NOT_ON_THE_CLOUD if mode == "hosted" else set())
    assert set(_bodies(mode)["doors"]) == doors, _bodies(mode)["doors"]


# A body a route reads by hand, past FastAPI's models: no Checked reads it,
# so each one says why the rule has nothing to read there.
_HAND_READ_BODIES = {
    # Polar's webhook: its signature is checked over the raw bytes first,
    # and a body it signed is Polar's, not a caller's.
    ("korely_agent/hosted/api_billing.py", "polar_webhook"),
    # A provider's spending alert, once its secret matched: quoted in the
    # operator's alert mail, never written to the database.
    ("korely_agent/hosted/api_billing.py", "_pull_on_alert"),
    # Not a request's body: the answers of the services this code calls,
    # read by the client that called them (`.json()` of an httpx answer).
    # The Cloud's acceptance run, against its own deployment:
    ("korely_agent/hosted/acceptance.py", "_code"),
    ("korely_agent/hosted/acceptance.py", "_remove_test_user"),
    ("korely_agent/hosted/acceptance.py", "part_signup"),
    ("korely_agent/hosted/acceptance.py", "make_test_user"),
    ("korely_agent/hosted/acceptance.py", "part_session"),
    ("korely_agent/hosted/acceptance.py", "part_claim"),
    ("korely_agent/hosted/acceptance.py", "part_checkout"),
    ("korely_agent/hosted/acceptance.py", "part_polar_endpoint"),
    ("korely_agent/hosted/acceptance.py", "part_polar_events"),
    ("korely_agent/hosted/acceptance.py", "part_close"),
    ("korely_agent/hosted/acceptance.py", "_account_id"),
    # Google's public keys and its sign-in errors:
    ("korely_agent/hosted/firebase.py", "fetch_certs"),
    ("korely_agent/hosted/firebase.py", "_error_code"),
    # GitHub's answer about a person's addresses:
    ("korely_agent/hosted/github.py", "vouches_for"),
    # Polar's API:
    ("korely_agent/hosted/polar.py", "_customer_missing"),
    ("korely_agent/hosted/polar.py", "create_checkout"),
    ("korely_agent/hosted/polar.py", "portal_url"),
    ("korely_agent/hosted/polar.py", "change_plan"),
    ("korely_agent/hosted/polar.py", "change_preview"),
    ("korely_agent/hosted/polar.py", "clear_pending_update"),
    ("korely_agent/hosted/polar.py", "set_cancel_at_period_end"),
    ("korely_agent/hosted/polar.py", "latest_order"),
    ("korely_agent/hosted/polar.py", "customer_by_external_id"),
    ("korely_agent/hosted/polar.py", "orders"),
    ("korely_agent/hosted/polar.py", "_subscriptions_of_customer"),
    ("korely_agent/hosted/polar.py", "subscriptions"),
    ("korely_agent/hosted/polar.py", "customer"),
    # A webhook delivery's own POST, streamed out (`pool.stream`), and the
    # database's rows streamed a thousand at a time (`bounded.stream`):
    ("korely_agent/services/webhooks.py", "_post"),
    ("korely_agent/services/export_md.py", "_facts_of"),
    # A part of the product's (core/extensions.Part).
    *cloud.of_the_parts("HAND_READ_BODIES", tuple),
}


def test_every_body_read_by_hand_is_known():
    found = set()
    for path in sorted((ROOT / "korely_agent").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/memory_core/" in rel:
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                # On any receiver: a route that names its Request
                # otherwise (`req`, an alias) read its body unseen.
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr in ("body", "json", "form", "stream")):
                    found.add((rel, fn.name))
    known = cloud.in_this_tree(_HAND_READ_BODIES)
    assert found == known, sorted(found ^ known)


@pytest.mark.parametrize("mode", cloud.MODES)
def test_every_list_in_a_body_has_a_most(mode):
    """A validation error per value of the wrong type: a list with no most
    took a few MB of `[0,0,...]` where strings belong and answered with as
    many errors, each kept in the heap and rendered (audit of 27 Oct
    2026). With a most,
    a list too long is one error, whatever it holds."""
    assert sorted(set(_bodies(mode)["unbounded"])) == [], _bodies(mode)["unbounded"]


@pytest.mark.parametrize("mode", cloud.MODES)
def test_a_model_inside_a_body_is_a_part(mode):
    """A Checked inside a body would refuse first, with its own field's name,
    and the sentence would lose its place (`memories[3].run_id`)."""
    assert _bodies(mode)["nested"] == []


@pytest.mark.parametrize("mode", cloud.MODES)
def test_every_field_of_every_body_is_read_before_its_own_checks(mode):
    """Every field of every body, sent alone with a NUL byte and with a lone
    surrogate: the rule's 422 naming it, whatever the field's type and
    whatever else the body lacks. The rule read a body once Pydantic had
    passed it, so a value Pydantic keeps as it came (a NamespaceId, a plain
    str, a list of them, a dict) reached the driver with a lone surrogate
    (500), and a constrained one answered Pydantic's refusal, not the
    rule's (the audit before 1.0.0)."""
    got = _bodies(mode)
    assert got["bodies"], "the walk found no body: it walks nothing"
    assert got["unread"] == [], got["unread"]


def _address_calls():
    """(method, url, the field the sentence names) for every query parameter
    and every path of every route of the app as it is built."""
    from korely_agent.api.errors import api_routes
    from korely_agent.main import create_app

    def params(dep, kind):
        yield from getattr(dep, kind)
        for sub in dep.dependencies:
            yield from params(sub, kind)

    calls = []
    for r in api_routes(create_app().router.routes):
        method = sorted(r.methods - {"HEAD"})[0].lower()
        names = [p.name for p in params(r.dependant, "path_params")]
        path = r.path
        for n in names:
            path = path.replace("{" + n + ":path}", "x").replace("{" + n + "}", "x")
        for q in {p.name for p in params(r.dependant, "query_params")}:
            calls.append((method, f"{path}?{q}=a%00b", q))
        if names:
            first = names[0]
            bad = r.path.replace("{" + first + ":path}", "a%00b").replace("{" + first + "}", "a%00b")
            for n in names[1:]:
                bad = bad.replace("{" + n + ":path}", "x").replace("{" + n + "}", "x")
            calls.append((method, bad, "the path"))
    return calls


def test_every_query_and_path_is_read_by_the_rule_before_the_route(monkeypatch):
    """Walked from the app: a NUL byte in any query parameter or path segment
    of any route is a 422 naming it, with no key and no database (the
    middleware answers before the route, its key and its session)."""
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.main import create_app

    calls = _address_calls()
    assert len(calls) > 50, calls
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        for method, url, field in calls:
            r = c.request(method.upper(), url)
            assert r.status_code == 422, (method, url, r.status_code, r.text[:200])
            assert r.json() == {"detail": f"{field} contains a NUL byte (U+0000)",
                                "code": "invalid_request",
                                "message": f"{field} contains a NUL byte (U+0000)"}, url
        # The doors that are not FastAPI's routes: OAuth's and the MCP's.
        for url in ("/authorize?client_id=a%00b", "/mcp?x=a%00b", "/.well-known/x%00"):
            assert c.get(url).status_code == 422, url


# Every place in korely_agent that reads a date out of a string, outside
# memory_core (whose own parsers are asked from here: require_event_time,
# parse_as_of). Each moment a caller sends goes through the rule; each line
# here says why it is that, or why it is not a caller's.
_MOMENT_PARSERS = {
    # A memory's `timestamp` (every write door): memory_write.require_event_time
    # asks limits.moment_problem of what it parses.
    # `as_of`, `known_as_of`, `valid_from`, `at`, `since`, `until`: parsed
    # here, refused in api/facts.parse_as_of by limits.moment_problem.
    ("korely_agent/services/facts/temporal.py", "parse_moment"),
    # Whether a `valid_from` string is a day with no time, so a correction
    # given as a day wins that day: it answers True or False and stores no
    # moment; the moment itself is parsed by parse_moment and refused by
    # api/facts.parse_as_of above.
    ("korely_agent/services/facts/temporal.py", "is_a_day"),
    # PATCH's `expected_updated_at`: memory_engine.version_problem asks
    # limits.moment_problem; the stored side is read back from the database.
    ("korely_agent/services/memory_engine.py", "_instant"),
    # Polar's signed event times, not a caller's.
    ("korely_agent/hosted/billing.py", "_when"),
    # The init key's `expires_at` in the Cloud's own answer, read by its
    # acceptance run (a malformed one is a failed check), not a caller's.
    ("korely_agent/hosted/acceptance.py", "_expires_as_promised"),
    # Keys of rows in the limiter's table, written by this code
    # (rate_limit.INSTANT), not a caller's: the one reading of them, for the
    # trial's strikes, the sliding windows and a part's own rates.
    ("korely_agent/services/rate_limit.py", "moment_of"),
    # The install's own export files (manage.py restore), written by this code.
    ("korely_agent/services/erasure_replay.py", "_moment"),
    ("korely_agent/hosted/replay.py", "close_again"),
    ("korely_agent/services/credential_replay.py", "moment"),
}


def _date_parsers():
    found = set()
    for path in sorted((ROOT / "korely_agent").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if "/memory_core/" in rel:
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr in ("fromisoformat", "strptime", "isoparse")):
                    found.add((rel, fn.name))
    return found


def test_every_parser_of_a_moment_is_known():
    """A new place that reads a date out of a string is a new door for the
    year 0: it is named above, with what refuses its moments."""
    assert _date_parsers() == cloud.in_this_tree(_MOMENT_PARSERS)


def test_the_parsers_of_a_callers_moments_ask_the_rule():
    for rel, fn in (("korely_agent/services/memory_write.py", "require_event_time"),
                    ("korely_agent/api/facts.py", "parse_as_of"),
                    ("korely_agent/services/memory_engine.py", "version_problem")):
        tree = ast.parse((ROOT / rel).read_text())
        [node] = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn]
        names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        assert "moment_problem" in names, (rel, fn)


# ── the findings, through the routes ───────────────────────────────────────

@pytest.fixture
def app_client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def heads(db, tenant):
    from korely_agent.services import keys as key_service

    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="all",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
    return {"Authorization": f"Bearer {full}"}


def _fact(client, heads) -> str:
    r = client.post("/v1/facts", headers=heads, json={
        "subject": "proj-x", "predicate": "uses", "object": "Postgres", "user_id": "u1"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _nul_422(r, field):
    assert r.status_code == 422, (field, r.status_code, r.text[:300])
    assert r.json()["detail"] == f"{field} contains a NUL byte (U+0000)", r.text


@pytest.mark.parametrize("field", ["subject", "predicate", "object", "subject_type"])
def test_c7_06_a_nul_byte_in_a_direct_fact(app_client, heads, field):
    body = {"subject": "proj-x", "predicate": "uses", "object": "Postgres", "user_id": "u1"}
    body[field] = NUL
    _nul_422(app_client.post("/v1/facts", headers=heads, json=body), field)


@pytest.mark.parametrize("prefix", ["/v1/facts", "/admin/facts"])
def test_c7_06_a_nul_byte_in_a_correction(app_client, heads, prefix):
    fid = _fact(app_client, heads)
    _nul_422(app_client.patch(f"{prefix}/{fid}", headers=heads, json={"object": NUL}), "object")


@pytest.mark.parametrize("method,path,body,field", [
    ("post", "/v1/users/u1/names", {"name": NUL}, "name"),
    ("post", "/admin/keys", {"name": NUL}, "name"),
    ("post", "/admin/projects", {"name": NUL}, "name"),
    ("post", "/admin/projects", {"name": "ok", "description": NUL}, "description"),
    ("post", "/admin/session", {"username": NUL, "password": "x"}, "username"),
])
def test_c7_06_a_nul_byte_in_a_label(app_client, heads, method, path, body, field):
    r = getattr(app_client, method)(path, headers=heads, json=body)
    _nul_422(r, field)


def test_c7_06_a_nul_byte_in_a_project_patch(app_client, heads):
    r = app_client.post("/admin/projects", headers=heads, json={"name": "Beta"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    _nul_422(app_client.patch(f"/admin/projects/{pid}", headers=heads, json={"name": NUL}), "name")


@pytest.mark.parametrize("url,field", [
    ("/admin/audit?user_id=a%00b", "user_id"),
    ("/admin/audit?action=a%00b", "action"),
    ("/admin/aliases?user_id=a%00b", "user_id"),
    ("/admin/memories?family=a%00b", "family"),
])
def test_c6_02_a_nul_byte_on_the_admin_reads_left(app_client, heads, url, field):
    _nul_422(app_client.get(url, headers=heads), field)


def test_c7_06_mcp_refuses_it_in_any_argument(db, tenant):
    """The MCP's twin of the bodies: a NUL in any argument of any tool, by
    the same sentence (korely_add's `kind` had no check of its own)."""
    from mcp.server.fastmcp.exceptions import ToolError

    from korely_agent.mcp import server as mcp_server
    from korely_agent.services import keys as key_service

    _r, key = key_service.create_api_key(db, tenant_id=tenant.id, name="m",
                                         scopes=["memories:read", "memories:write"])

    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        try:
            return str(await mcp_server._get_agent_mcp().call_tool(
                "korely_add", {"content": "Maria moved.", "kind": NUL}))
        except ToolError as e:
            return str(e)
    assert "invalid_request" in asyncio.run(go())


@pytest.mark.parametrize("moment", [YEAR_ZERO, "0001-01-01T00:00:00+01:00"])
def test_c7_07_a_memory_of_the_year_zero_is_refused_at_every_door(app_client, heads, db, moment):
    from korely_agent.db.models import AgentMemory

    item = {"content": "Old note.", "user_id": "u1", "timestamp": moment}
    r = app_client.post("/v1/memories", headers=heads, json=item)
    assert r.status_code == 422 and "outside the moments" in r.json()["message"], r.text
    r = app_client.post("/v1/batch", headers=heads, json={"memories": [item]})
    assert r.status_code == 422 and r.json()["detail"].startswith("memories[0].timestamp"), r.text
    db.rollback()
    assert db.query(AgentMemory).count() == 0


@pytest.mark.parametrize("moment", [YEAR_ZERO, PAST_9999])
def test_c7_07_a_fact_of_the_year_zero_is_refused(app_client, heads, moment):
    r = app_client.post("/v1/facts", headers=heads, json={
        "subject": "proj-x", "predicate": "uses", "object": "Postgres", "user_id": "u1",
        "valid_from": moment})
    assert r.status_code == 422 and "valid_from is outside" in r.json()["message"], r.text
    r = app_client.get("/v1/facts", headers=heads, params={"as_of": moment})
    assert r.status_code == 422 and "as_of is outside" in r.json()["message"], r.text


def test_c7_07_a_stored_one_no_longer_stops_the_drain(db, tenant):
    """A row written past the doors (before the rule, or by hand): the claim
    loaded it and raised at every tick, so another account's memory was
    never mined. Mended to the nearest moment that reads back, and the
    claim goes on."""
    from korely_agent.db.models import AgentMemory, Tenant
    from korely_agent.services.facts import drain

    other = Tenant(id=uuid.uuid4(), name="Other")
    db.add(other)
    db.commit()
    bad, fine = uuid.uuid4(), uuid.uuid4()
    db.add(AgentMemory(id=fine, tenant_id=other.id, end_user_id="anna", content="Anna's note.",
                       graph_sync_status="pending"))
    db.commit()
    db.execute(text("INSERT INTO agent_memories (id, tenant_id, end_user_id, content, "
                    "graph_sync_status, event_time) VALUES (:id, :t, 'u1', 'Old note.', "
                    "'pending', TIMESTAMPTZ '0001-01-01 00:00:00+14:00')"),
               {"id": bad, "t": tenant.id})
    db.commit()
    claimed = {m.id for m in drain._claim(db, 10)}
    assert fine in claimed and bad in claimed
    db.expire_all()
    assert db.get(AgentMemory, bad).event_time == limits.EARLIEST_MOMENT


# ── the doors that are not FastAPI's, walked (audit of 27 Oct 2026) ────────
# The guard probed the SDK's doors with a GET query only, which the address
# rule reads; POST /authorize's form, which no Checked and no middleware
# reads, took a NUL in `state` to the driver (server_error). Every field of
# every form the SDK parses is walked from the SDK's own models, so a field
# it adds is probed too.

_CALLBACK = "https://example.com/cb"


def _registered(client) -> str:
    r = client.post("/register", json={
        "client_name": "walk", "redirect_uris": [_CALLBACK], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _sdk_forms(client_id: str):
    """(path, the SDK's model of its form, a form the door takes whole)."""
    from mcp.server.auth.handlers.authorize import AuthorizationRequest
    from mcp.server.auth.handlers.revoke import RevocationRequest
    from mcp.server.auth.handlers.token import AuthorizationCodeRequest, RefreshTokenRequest

    return [
        ("/authorize", AuthorizationRequest, {
            "client_id": client_id, "redirect_uri": _CALLBACK, "response_type": "code",
            "code_challenge": "c" * 43, "code_challenge_method": "S256", "state": "s",
            "scope": "memories:read", "resource": None}),
        ("/token", AuthorizationCodeRequest, {
            "grant_type": "authorization_code", "code": "c", "redirect_uri": _CALLBACK,
            "client_id": client_id, "client_secret": None, "code_verifier": "v" * 50,
            "resource": None}),
        ("/token", RefreshTokenRequest, {
            "grant_type": "refresh_token", "refresh_token": "r", "scope": "memories:read",
            "client_id": client_id, "client_secret": None, "resource": None}),
        ("/revoke", RevocationRequest, {
            "token": "t", "token_type_hint": None, "client_id": client_id, "client_secret": None}),
    ]


def _sdk_error(r):
    """The OAuth error of an answer: in the JSON body, or in the query of the
    redirect to the client's callback."""
    from urllib.parse import parse_qs, urlparse

    if r.status_code in (302, 303, 307):
        q = parse_qs(urlparse(r.headers["location"]).query)
        return q.get("error", [None])[0], q.get("error_description", [""])[0]
    try:
        b = r.json()
    except ValueError:
        return None, ""
    return b.get("error"), b.get("error_description") or ""


# The fields of /authorize the provider keeps (KorelyOAuthProvider.authorize):
# refused there by the rule, naming the field. The others the SDK refuses
# itself, before the provider is asked (`scope` against the client's own
# scopes: `invalid_scope`).
_AUTHORIZE_KEPT = {"state", "code_challenge", "resource"}


def test_every_field_of_the_sdk_forms_is_read_by_the_rule(app_client):
    cid = _registered(app_client)
    missed = []
    for path, model, base in _sdk_forms(cid):
        assert set(base) == set(model.model_fields), (path, model.__name__)
        for field in model.model_fields:
            form = {k: v for k, v in {**base, field: NUL}.items() if v is not None}
            r = app_client.post(path, data=form, follow_redirects=False)
            error, said = _sdk_error(r)
            if r.status_code >= 500 or error == "server_error" or (
                    path == "/authorize" and field in _AUTHORIZE_KEPT
                    and (error != "invalid_request" or "contains a NUL byte" not in said)):
                missed.append((path, field, r.status_code, error))
    assert missed == [], missed


@pytest.mark.parametrize("bad", [NUL, LONE])
def test_every_field_of_the_registration_is_read_by_the_rule(app_client, db, bad):
    """A lone surrogate in `client_name` was stored and the answer was a
    500 (the audit before 1.0.0)."""
    from mcp.shared.auth import OAuthClientMetadata

    from korely_agent.db.models import OAuthClient

    before = db.query(OAuthClient).count()
    base = {"client_name": "walk", "redirect_uris": [_CALLBACK], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    missed = []
    for field, info in OAuthClientMetadata.model_fields.items():
        listed = "list" in str(info.annotation).lower()
        r = _post_escaped(app_client, "/register", {**base, field: [bad] if listed else bad})
        if r.status_code != 400:
            missed.append((field, r.status_code, r.text[:80]))
    assert missed == [], missed
    db.rollback()
    assert db.query(OAuthClient).count() == before, "a refused client was stored"


# The types a tool's argument may be: one value each.
_SCALARS = (str, int, float, bool, type(None))


def _scalar(annotation) -> bool:
    """`annotation` is one of _SCALARS, a Literal of them, or a union (an
    Optional) or Annotated of such; never a container, whatever it is
    named."""
    import types

    origin = typing.get_origin(annotation)
    if origin is None:
        return annotation in _SCALARS
    if origin is typing.Literal:
        return all(isinstance(v, _SCALARS) for v in typing.get_args(annotation))
    if origin is typing.Annotated:
        return _scalar(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        return all(_scalar(a) for a in typing.get_args(annotation))
    return False


def test_scalar_names_what_a_tool_may_take():
    assert _scalar(str) and _scalar(typing.Optional[int]) and _scalar(typing.Literal["a", 1])
    for many in (list, typing.List[str], typing.Sequence[str], typing.Mapping[str, int],
                 dict, typing.Optional[typing.Iterable[str]], bytes):
        assert not _scalar(many), many


def test_every_argument_of_every_tool_is_read_by_the_rule(db, tenant, monkeypatch):
    """Every declared argument of every tool with a NUL byte, and one
    argument no tool declares: each an `invalid_request` before the tool
    runs. An undeclared one was taken and walked whole on the event loop
    (audit of 27 Oct 2026)."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    from korely_agent.mcp import server as mcp_server
    from korely_agent.services import keys as key_service

    ran = []

    async def the_tool(self, name, arguments):
        ran.append((name, sorted(arguments)))
        return "ran"

    monkeypatch.setattr(FastMCP, "call_tool", the_tool)
    _r, key = key_service.create_api_key(db, tenant_id=tenant.id, name="m",
                                         scopes=["memories:read", "memories:write"])

    async def answer(mcp, name, arguments):
        try:
            return str(await mcp.call_tool(name, arguments))
        except ToolError as e:
            return str(e)

    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        mcp = mcp_server._get_agent_mcp()
        out = []
        for tool in mcp._tool_manager.list_tools():
            fields = tool.fn_metadata.arg_model.model_fields
            base = {n: "x" for n, f in fields.items() if f.is_required()}
            for n, f in fields.items():
                # Every declared argument is a scalar: the rule walks a
                # handful of values (mcp/server._arguments_checked).
                assert _scalar(f.annotation), (tool.name, n, f.annotation)
                text_arg = "str" in str(f.annotation)
                for bad, expected in ((NUL, "contains a NUL byte"),
                                      (LONE, "contains a lone UTF-16 surrogate")):
                    said = await answer(mcp, tool.name, {**base, n: bad})
                    out.append((tool.name, n, expected if text_arg else None, said))
            out.append((tool.name, "undeclared", None,
                        await answer(mcp, tool.name, {**base, "undeclared": [0, 0]})))
        return out

    walked = asyncio.run(go())
    assert len(walked) > 20, walked
    missed = [(tool, arg, said[:80]) for tool, arg, expected, said in walked
              if not said.startswith("error: invalid_request")
              or (expected and expected not in said)
              or (arg == "undeclared" and "undeclared" not in said)]
    assert missed == [], missed
    assert ran == []


def test_c7_06_the_oauth_doors_of_the_sdk(app_client):
    """/token and /revoke are the SDK's, with a form body the address rule
    does not read: the client id reached the driver (500). /register kept a
    NUL in the name shown on the consent page."""
    r = app_client.post("/token", data={"grant_type": "authorization_code", "code": "c",
                                        "client_id": NUL, "code_verifier": "v" * 50,
                                        "redirect_uri": "https://example.com/cb"})
    assert r.status_code == 401, r.text
    r = app_client.post("/revoke", data={"token": "t", "client_id": NUL})
    assert r.status_code == 401, r.text
    r = app_client.post("/register", json={
        "client_name": NUL, "redirect_uris": ["https://example.com/cb"],
        "token_endpoint_auth_method": "none"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_client_metadata", r.text
    assert "client_name contains a NUL byte" in r.json()["error_description"]


def test_a_dense_undeclared_argument_is_refused_before_the_rule_walks_it(db, tenant, monkeypatch):
    """An argument no tool declares, of 200,000 elements, was walked whole
    by nul_problem on the one event loop. Refused by its name, before any
    walk."""
    from mcp.server.fastmcp.exceptions import ToolError

    from korely_agent.mcp import server as mcp_server
    from korely_agent.services import keys as key_service

    walked = []
    real = limits.nul_problem
    monkeypatch.setattr(limits, "nul_problem", lambda f, v: walked.append(1) or real(f, v))
    _r, key = key_service.create_api_key(db, tenant_id=tenant.id, name="m",
                                         scopes=["memories:read", "memories:write"])

    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        try:
            return str(await mcp_server._get_agent_mcp().call_tool(
                "korely_search", {"query": "x", "padding": [0] * 200_000}))
        except ToolError as e:
            return str(e)
    said = asyncio.run(go())
    assert said.startswith("error: invalid_request, padding is not an argument"), said
    assert walked == []


# ── a refusal costs what a refusal says (audit of 27 Oct 2026) ─────────────
# A write key's batch
# that passed every count of _BodyCap was refused with one error per unknown
# key, each some 1.4 KB of heap, and the 422 rendered them all.

def _errors_counted(r) -> int:
    """How many validation errors the 422 counts: the first, and `(and N
    more)` of its message."""
    import re as _re

    m = _re.search(r"\(and (\d+) more\)", r.json()["message"])
    return 1 + (int(m.group(1)) if m else 0)


def test_unknown_fields_are_one_error_per_model(app_client, heads):
    item = {"content": "x", **{f"k{i:05d}": 0 for i in range(300)}}
    body = {"memories": [item] * 40, **{f"top{i}": 0 for i in range(300)}}
    r = app_client.post("/v1/batch", headers=heads, json=body)
    assert r.status_code == 422, r.text
    assert _errors_counted(r) == 41, r.json()["message"]   # the body's, then one per item
    assert len(r.json()["detail"]) == limits.MAX_VALIDATION_ERRORS_SHOWN
    first = r.json()["detail"][0]
    assert first["type"] == "extra_forbidden" and first["loc"][-1] == "k00000", first


def test_the_first_unknown_field_still_gets_its_suggestion(app_client, heads):
    r = app_client.post("/v1/memories", headers=heads,
                        json={"content": "x", "userId": "anna", "zzz": 1, "yyy": 2})
    assert r.status_code == 422
    assert r.json()["message"] == "userId is not a field here. Did you mean user_id?", r.text


def test_a_list_of_the_wrong_values_is_one_error(app_client, heads):
    r = app_client.post("/admin/keys", headers=heads, json={"name": "k", "scopes": [0] * 5000})
    assert r.status_code == 422, r.text
    assert _errors_counted(r) == 1, r.json()["message"]


def test_the_422_lists_its_first_errors_and_counts_them_all(app_client, heads):
    """Many errors by a road the unknown fields do not take: a wrong type
    in every field of every item."""
    item = {"content": 1, "user_id": 2, "agent_id": 3, "run_id": 4, "timestamp": 5}
    r = app_client.post("/v1/batch", headers=heads, json={"memories": [item] * 30})
    assert r.status_code == 422, r.text
    assert _errors_counted(r) > limits.MAX_VALIDATION_ERRORS_SHOWN
    assert len(r.json()["detail"]) == limits.MAX_VALIDATION_ERRORS_SHOWN
    assert all("input" not in e for e in r.json()["detail"])


# ── depth and the sentence of the moments (audit of 27 Oct 2026) ───────────

def _nested(levels: int):
    value = 1
    for _ in range(levels):
        value = {"a": value}
    return value


def test_the_rule_walks_any_depth_without_the_stack():
    deep = _nested(5000)
    assert limits.nul_problem("metadata", deep) is None
    inner = deep
    for _ in range(4999):
        inner = inner["a"]
    inner["a"] = NUL
    assert limits.nul_problem("metadata", deep) == "metadata" + ".a" * 5000 + " contains a NUL byte (U+0000)"


@pytest.mark.parametrize("levels", [400, 700, 900])
def test_c9_08_deep_metadata_is_a_422(app_client, heads, levels):
    """Nested a few hundred levels (2 to 5 KB, under MAX_METADATA_BYTES) it
    answered 500; a batch row nested so took the job down in the worker."""
    item = {"content": "x", "user_id": "u1", "metadata": _nested(levels)}
    r = app_client.post("/v1/memories", headers=heads, json=item)
    assert r.status_code == 422 and "nests deeper than" in r.json()["message"], r.text
    r = app_client.post("/v1/batch", headers=heads, json={"memories": [{"content": "y", "user_id": "u1"}, item]})
    assert r.status_code == 422 and "nests deeper than" in r.json()["message"], r.text


def test_c9_08_metadata_at_its_most_depth_is_taken():
    assert limits.metadata_problem(_nested(limits.MAX_METADATA_DEPTH)) is None
    assert limits.metadata_problem(_nested(limits.MAX_METADATA_DEPTH + 1))


def test_c9_03_the_bounds_are_iso_dates(app_client, heads):
    r = app_client.post("/v1/memories", headers=heads,
                        json={"content": "x", "user_id": "u1", "timestamp": "0001-01-01"})
    assert r.status_code == 422, r.text
    assert r.json()["message"] == ("timestamp is outside the moments that can be stored: "
                                   "from 0001-01-02 to 9999-12-30, in UTC"), r.text


def test_d5_04_the_rule_is_in_the_standing_docs():
    """The rule was in the CHANGELOG only (audit of 27 Oct 2026): the README
    says it with the numbers of services/limits, and the document's moment
    fields and 422 say the moments that can be stored. Since the README became
    a landing page (29 Oct 2026) the section is "What every door refuses" of
    docs/guide/api.md, one heading level up."""
    from korely_agent.main import create_app
    from tests import docs_text

    api = docs_text.page("docs/guide/api.md")
    section = " ".join(api.split("\n## What every door refuses\n", 1)[1].split("\n## ", 1)[0].split())
    for said in ("NUL byte (U+0000)", "lone UTF-16 surrogate", limits.EARLIEST_MOMENT.date().isoformat(),
                 limits.LATEST_MOMENT.date().isoformat(), f"{limits.MAX_METADATA_BYTES} bytes",
                 f"{limits.MAX_METADATA_DEPTH} levels", f"at most {limits.MAX_VALIDATION_ERRORS_SHOWN} errors",
                 f"{limits.BODY_READ_SECONDS:.0f} s", f"{limits.LARGE_BODY_READ_SECONDS:.0f} s",
                 f"at most {limits.BODIES_IN_FLIGHT_BYTES // 2**20} MiB",
                 f"at most {limits.BODIES_PER_NETWORK // 2**20} MiB of it"):
        assert said in section, said
    doc = create_app().openapi()
    write = doc["paths"]["/v1/memories"]["post"]
    assert limits.MOMENTS_STORED in write["responses"]["422"]["description"]
    assert "NUL byte" in write["responses"]["422"]["description"]
    assert "lone UTF-16 surrogate" in write["responses"]["422"]["description"]
    schema = doc["components"]["schemas"]
    assert limits.MOMENTS_STORED in str(schema)


# ── a lone surrogate (the audit before 1.0.0) ──────────────────────────────
# The rule refused a
# NUL byte but not a lone UTF-16 surrogate, which JSON spells "\ud800". A
# write's user_id or agent_id took it to the driver (500); a batch item took
# it to the json column of the job, where every de-escaping read of the
# payload failed: the project's erasure answered 500 and the account's
# erasure replay stopped.

def _post_escaped(client, path, body, headers=None):
    """POST `body` as JSON with every non-ASCII character escaped, as a
    client's encoder writes a lone surrogate (`"\\ud800"`): the test
    client's own encoder refuses one."""
    return client.post(path, headers={**(headers or {}), "Content-Type": "application/json"},
                       content=json.dumps(body))


def _lone_422(r, field):
    assert r.status_code == 422, (field, r.status_code, r.text[:300])
    assert r.json()["code"] == "invalid_request", r.text
    assert r.json()["detail"] == f"{field} {_LONE_SAID}", r.text


@pytest.mark.parametrize("path,body,field", [
    ("/v1/memories", {"content": "ok", "user_id": LONE}, "user_id"),
    ("/v1/memories", {"content": "ok", "agent_id": LONE}, "agent_id"),
    ("/v1/memories", {"content": LONE, "user_id": "anna"}, "content"),
    ("/v1/memories", {"content": "ok", "metadata": {"tier": LONE}}, "metadata.tier"),
    ("/v1/batch", {"memories": [{"content": "x", "user_id": "anna", "agent_id": LONE}]},
     "memories[0].agent_id"),
    ("/v1/batch", {"memories": [{"content": "x"}, {"content": "y", "user_id": LONE}]},
     "memories[1].user_id"),
    ("/v1/facts", {"subject": "proj-x", "predicate": "uses", "object": "Postgres",
                   "user_id": LONE}, "user_id"),
    ("/v1/memories/search", {"query": LONE}, "query"),
])
def test_a_lone_surrogate_in_a_write_is_the_rules_422(app_client, heads, db, path, body, field):
    from korely_agent.db.models import AgentBatchJob, AgentMemory

    _lone_422(_post_escaped(app_client, path, body, heads), field)
    db.rollback()
    assert db.query(AgentBatchJob).count() == 0 and db.query(AgentMemory).count() == 0


def test_the_write_rule_refuses_it_in_an_id_too():
    """memory_write.checked, which the batch worker and the MCP write ask:
    the content and the run were read by the rule, user_id and agent_id
    only by the namespace rule, which knew control characters only."""
    from korely_agent.services import memory_write

    for kwargs in ({"user_id": LONE}, {"agent_id": LONE}, {"run_id": LONE}):
        with pytest.raises(Exception) as e:
            memory_write.checked("ok", **kwargs)
        assert "surrogate" in str(getattr(e.value, "detail", e.value)), kwargs
    with pytest.raises(Exception):
        memory_write.checked(LONE)


def _poisoned_job(db, tenant, project_id, user="anna"):
    """A job as one queued before the rule left it: its payload spells a
    lone surrogate, which the json column keeps as the escape."""
    jid = uuid.uuid4()
    db.execute(text(
        "INSERT INTO agent_batch_jobs (id, tenant_id, project_id, status, received, payload)"
        " VALUES (:id, :t, :p, 'pending', 2, CAST(:payload AS json))"),
        {"id": jid, "t": tenant.id, "p": project_id,
         "payload": '[{"content": "x", "user_id": "%s", "agent_id": "\\ud800"},'
                    ' {"content": "y\\udfffz", "user_id": "bea", "agent_id": "bot"}]' % user})
    db.commit()
    return jid


def test_a_job_stored_with_one_before_the_rule_does_not_stop_an_erasure(app_client, heads, db,
                                                                        tenant):
    from korely_agent.db.models import AgentBatchJob
    from korely_agent.services import batch, end_user_places

    r = app_client.post("/admin/projects", headers=heads, json={"name": "Poisoned"})
    assert r.status_code == 201, r.text
    pid = uuid.UUID(r.json()["id"])
    jid = _poisoned_job(db, tenant, pid)

    assert batch.item_end_users(db, tenant.id, pid) == {"anna", "bea"}
    assert batch.item_end_users(db, tenant.id, pid, field="agent_id") == {"bot"}, \
        "an id no door could have taken is nobody to erase"
    # The Cloud's agent cap reads the names of the jobs not yet done by it:
    # its read of its own raised, and every write of a new name was a 500.
    if cloud.HERE:
        from korely_agent.hosted import quotas

        assert quotas._queued_agents(db, tenant.id) == {(pid, "bot")}
    assert {"anna", "bea"} <= end_user_places.found(db, tenant.id)
    assert end_user_places.held_in(db, tenant.id, pid) == ["anna", "bea"]
    db.commit()

    r = app_client.delete(f"/admin/projects/{pid}?erase=true", headers=heads)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(AgentBatchJob, jid) is None


def test_an_end_users_erasure_scrubs_such_a_job(app_client, heads, db, tenant):
    from korely_agent.db.models import AgentBatchJob

    jid = _poisoned_job(db, tenant, seeded_project_id_of(db, tenant))
    r = app_client.delete("/v1/users/anna/memories", headers=heads)
    assert r.status_code == 200, r.text
    db.expire_all()
    payload = db.get(AgentBatchJob, jid).payload
    assert payload[0] == {"erased": True} and payload[1]["user_id"] == "bea"


def seeded_project_id_of(db, tenant):
    from tests.conftest import seeded_project_id

    return seeded_project_id(db, tenant.id)


# ── what the rule reads, and what that costs (the review of the audit before 1.0.0) ─
# The fix of the lone surrogate read a
# body's strings before Pydantic, every value of the raw body: an undeclared
# field and a value of the wrong type too, each with a walk that names its
# place. One unknown field of 2.6 million values held the event loop some
# 12 s on /v1/batch (0.27 s before the fix), 72 thousand some 0.5 s on POST
# /admin/session with no credential. Counted here in the work, never in
# seconds: what the walk is given, and whether it runs at all.

def _values(v) -> int:
    """How many values `v` holds, itself included."""
    n, stack = 0, [v]
    while stack:
        x = stack.pop()
        n += 1
        if isinstance(x, dict):
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return n


def test_the_rule_names_the_place_only_where_there_is_one(monkeypatch):
    """A value is read in one pass of the encoder, and the place found in
    that text; the walk value by value runs only for what the encoder
    cannot write (a key that is not a name)."""
    walked = []
    real = limits.first_in

    def counted(field, value, problem, **how):
        walked.append(field)
        return real(field, value, problem, **how)

    monkeypatch.setattr(limits, "first_in", counted)
    clean = {"memories": [{"content": "é" * 3, "metadata": {"k": [1, 2.5, None, True]}}] * 500,
             "emoji": json.loads('"\\ud83d\\ude00"')}
    assert limits.nul_problem("", clean) is None
    # A text that only looks like the escape is not one.
    assert limits.nul_problem("f", ["a\\u0000 written out"]) is None
    assert limits.nul_problem("f", {"a": [0] * 1000 + [{"b": "x\x00"}]}) == \
        "f.a[1000].b contains a NUL byte (U+0000)"
    assert limits.nul_problem("f", ["ok", LONE]).startswith("f[1] contains a lone UTF-16 surrogate")
    assert limits.nul_problem("f", {LONE: 1}).startswith("f contains a lone UTF-16 surrogate")
    # An object's names before what it holds, as the walk reads them.
    assert limits.nul_problem("f", {"a": {"b": "x\x00"}, LONE: 1}).startswith(
        "f contains a lone UTF-16 surrogate")
    assert walked == [], walked
    # A key JSON cannot write: walked.
    assert limits.nul_problem("f", {("a",): "x\x00"}) == "f.('a',) contains a NUL byte (U+0000)"
    assert walked == ["f"], walked


def test_an_undeclared_field_is_not_read_by_the_rule(monkeypatch):
    """What the rule is given of a body is its declared fields' values,
    whatever the undeclared ones hold: at the top of a body, inside a
    Part, and in a body that tolerates unknown fields."""
    from fastapi import HTTPException
    from pydantic import ValidationError

    from korely_agent.api import _bodies
    from korely_agent.api.batch import BatchBody

    given = []
    real = _bodies.nul_problem

    def recorded(field, value):
        given.append(_values(value))
        return real(field, value)

    monkeypatch.setattr(_bodies, "nul_problem", recorded)
    many = [0] * 100_000
    with pytest.raises(ValidationError):
        BatchBody.model_validate({"memories": [{"content": "x"}], "junk": many})
    with pytest.raises(ValidationError):
        BatchBody.model_validate({"memories": [{"content": "x", "junk": many}]})
    if cloud.HERE:
        # A body of the Cloud's that tolerates unknown fields.
        from korely_agent.hosted.api_identity import SessionBody

        SessionBody.model_validate({"id_token": "t", "junk": many})
    assert given and max(given) < 20, given
    # The names sent are still read: a refusal of an unknown field names it.
    with pytest.raises(HTTPException) as e:
        BatchBody.model_validate({"memories": [{"content": "x", LONE: many}]})
    assert "memories[0] contains a lone UTF-16 surrogate" in e.value.detail
    with pytest.raises(HTTPException) as e:
        BatchBody.model_validate({"memories": [{"content": "x"}], "a\x00b": many})
    assert e.value.detail == "a name contains a NUL byte (U+0000)"


def test_an_undeclared_field_of_the_registration_is_not_read(app_client, monkeypatch):
    """/register's JSON before the SDK reads it (api/oauth.
    _RegistrationByTheRule): its declared fields only, the SDK ignoring
    any other."""
    given = []
    real = limits.nul_problem

    def recorded(field, value):
        given.append(_values(value))
        return real(field, value)

    monkeypatch.setattr(limits, "nul_problem", recorded)
    base = {"client_name": "walk", "redirect_uris": [_CALLBACK], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    r = app_client.post("/register", json={**base, "junk": [0] * 50_000})
    assert r.status_code == 201, r.text[:200]
    assert given and max(given) < 100, given


# ── where it is, found by halves (the review of 138b8cb) ───────────────────
# A declared field still cost some 5 s of the event loop on /v1/batch: one
# item whose metadata held 2.6 million values. When the encoder's pass said
# "maybe", the walk that names the place went value by value in Python, and
# a NUL at the end of the metadata made it say so; the moments walk went
# into the metadata's every value, looking for a date a dict never holds;
# and the metadata's own check walked it twice more before it weighed it.
# Counted here in the steps Python takes (every call it makes, its own
# functions' and C's), never in seconds.

def _python_steps(fn, *args):
    """(what `fn(*args)` answers, how many calls Python made to answer it).

    With the garbage collector collected first and off while it counts, as
    the standard library's timeit turns it off while it times (its docs,
    Timer.timeit): the profile counts every call on the thread, and a
    collection that runs inside `fn` (the 100,000 pairs the encoder makes of
    a dict of 100,000 keys start a hundred of them) calls the weakref
    callbacks of whatever earlier tests left to collect. In a parallel run
    of 7 Nov 2026 the dict's 21 calls were counted as 289; leaving 2,000
    such objects before it counts 303, and 21 with the collector off."""
    import gc

    steps = 0

    def count(frame, event, arg):
        nonlocal steps
        steps += 1

    gc.collect()
    was_on = gc.isenabled()
    gc.disable()
    sys.setprofile(count)
    try:
        out = fn(*args)
    finally:
        sys.setprofile(None)
        if was_on:
            gc.enable()
    return out, steps


_MANY = 100_000


def test_no_collection_runs_while_the_counter_counts():
    """A collection that ran inside the counted call counted the callbacks
    of what earlier tests left (`_python_steps`): while it counts the
    collector is off, and after it the collector is on again."""
    import gc

    assert gc.isenabled()
    on_inside, _steps = _python_steps(gc.isenabled)
    assert on_inside is False
    assert gc.isenabled()


@pytest.mark.parametrize("value,where,what", [
    ([0] * _MANY + ["x\x00"], f"f[{_MANY}]", "a NUL byte"),
    ([0] * _MANY + [LONE], f"f[{_MANY}]", "a lone UTF-16 surrogate"),
    ({f"k{i}": i for i in range(_MANY)} | {"last": "x\x00"}, "f.last", "a NUL byte"),
    ({f"k{i}": i for i in range(_MANY)} | {"x\x00": 1}, "f", "a NUL byte"),
    ({"a": [{"b": [0] * _MANY + ["x\x00"]}]}, f"f.a[0].b[{_MANY}]", "a NUL byte"),
    # Text that only looks like the escape is not one, however much of it.
    (["a\\u0000"] * _MANY + ["x\x00"], f"f[{_MANY}]", "a NUL byte"),
])
def test_the_place_is_found_by_halves_not_value_by_value(value, where, what):
    said, steps = _python_steps(limits.nul_problem, "f", value)
    assert said.startswith(f"{where} contains {what}"), said
    assert steps < 3_000, f"{steps:,} steps for one place among {_MANY:,} values"


def test_the_moments_walk_never_enters_a_dict_or_any_field():
    """A body's moments are in the fields whose type can hold one (a date,
    or a Part that has one): the contents of a `dict` field never do."""
    from korely_agent.api.batch import BatchBody

    data = {"memories": [{"content": "x", "metadata": {"k": [0] * _MANY,
                                                        "d": {str(i): "v" for i in range(_MANY)}}}]}
    body, steps = _python_steps(BatchBody.model_validate, data)
    assert len(body.memories[0].metadata["k"]) == _MANY
    assert steps < 3_000, f"{steps:,} steps to read one body"


def test_oversized_metadata_is_weighed_before_it_is_walked():
    """The 8 KB of metadata are counted in one pass of the encoder, before
    the walks for its depth, its NaN and its NUL, which an oversized one
    spared none of."""
    for metadata in ({"k": [0] * _MANY}, {"k": [0] * _MANY + [float("nan"), "x\x00"]},
                     {str(i): i for i in range(_MANY)}):
        said, steps = _python_steps(limits.metadata_problem, metadata)
        assert said == f"metadata exceeds {limits.MAX_METADATA_BYTES} bytes", said
        assert steps < 100, f"{steps:,} steps to weigh it"
    # Under it, each is named as before, in the same order.
    assert "NaN" in limits.metadata_problem({"k": [0, float("nan"), "x\x00"]})
    assert limits.metadata_problem({"k": [0, "x\x00"]}) == "metadata.k[1] contains a NUL byte (U+0000)"


# ── and in one pass of the text, whatever its shape (the review of 1818649) ─
# Found by halves, each half re-encoded: a bad value first in every half and
# heavy, at the bottom of a deep value, was encoded again at every halving
# of every level. 82 s on /v1/batch, 6.9 s on /register with no key. Counted
# here in the characters encoded, against one encoding of the whole value.

class _Encoded:
    """limits' json, counting the characters it writes."""

    def __init__(self):
        self.chars = 0

    def __getattr__(self, name):
        return getattr(json, name)

    def dumps(self, *a, **k):
        out = json.dumps(*a, **k)
        self.chars += len(out)
        return out


def _chain(depth: int, bottom, width: int = 2):
    """[[[...[bottom, 0, ...]..., 0, ...], 0, ...]: `width` items at every
    level, the bad value first."""
    value = [bottom] + [0] * (width - 1)
    for _ in range(depth - 1):
        value = [value] + [0] * (width - 1)
    return value


@pytest.mark.parametrize("value,where", [
    (_chain(300, "y" * 100_000 + "\x00"), "f" + "[0]" * 300),
    (_chain(200, "y" * 50_000 + "\x00", 200), "f" + "[0]" * 200),
    ([_chain(200, "y" * 500 + "\x00") for _ in range(200)], "f" + "[0]" * 201),
    ({"a": {"b": _chain(250, {"k": "y" * 50_000 + "\ud800"})}}, "f.a.b" + "[0]" * 250 + ".k"),
], ids=["300-deep", "200-deep-200-wide", "200-chains-200-deep", "under-names"])
def test_a_heavy_bad_value_first_at_every_level_is_encoded_once(monkeypatch, value, where):
    encoded = _Encoded()
    monkeypatch.setattr(limits, "json", encoded)
    said = limits.nul_problem("f", value)
    assert said is not None and said.startswith(f"{where} contains "), said
    whole = len(json.dumps(value, ensure_ascii=False))
    assert encoded.chars <= 3 * whole, f"{encoded.chars:,} characters encoded for {whole:,}"


def test_a_heavy_bad_value_in_the_jwks_of_register_is_encoded_once(app_client, monkeypatch):
    """/register, no key: `jwks`, a field of the SDK's typed Any."""
    encoded = _Encoded()
    monkeypatch.setattr(limits, "json", encoded)
    jwks = _chain(300, "y" * 100_000 + "\x00", 100)
    base = {"client_name": "walk", "redirect_uris": [_CALLBACK], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    r = app_client.post("/register", json={**base, "jwks": jwks})
    assert r.status_code == 400 and "NUL" in r.text, r.text[:300]
    whole = len(json.dumps(jwks, ensure_ascii=False))
    assert encoded.chars <= 3 * whole, f"{encoded.chars:,} characters encoded for {whole:,}"


def _random_value(rng, depth=0):
    bad = ["x\x00y", "\x00", "a\ud800", "\udfff", "a\\u0000", "\\\\u0000", "é\x00",
           json.loads('"\\ud83d\\ude00"'), "\\", '"', "", "ok"]
    roll = rng.random()
    if depth > 4 or roll < 0.35:
        return rng.choice([0, 1.5, None, True, rng.choice(bad), rng.choice(bad)])
    if roll < 0.7:
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {rng.choice(bad) + str(i): _random_value(rng, depth + 1) for i in range(rng.randrange(4))}


def test_the_place_is_the_walks_own_for_any_value():
    """The place named is the one the walk value by value names (first_in
    over each value, the names of an object before what it holds)."""
    import random

    rng = random.Random(1818649)
    found = 0
    for _ in range(5_000):
        value = _random_value(rng)
        expected = limits.first_in("f", value, limits._nul_here)
        assert limits.nul_problem("f", value) == expected, value
        found += expected is not None
    assert found > 1_000, found


def test_oversized_metadata_with_a_nul_names_the_nul_at_the_door(app_client, heads):
    """The body's rule reads a NUL or a lone surrogate before the metadata's
    own check weighs it: at a door, it is still named first."""
    r = app_client.post("/v1/memories", headers=heads,
                        json={"content": "x", "metadata": {"k": "y" * 9_000 + "\x00"}})
    assert r.status_code == 422 and r.json()["detail"] == "metadata.k contains a NUL byte (U+0000)", r.text
    r = app_client.post("/v1/memories", headers=heads, json={"content": "x", "metadata": {"k": "y" * 9_000}})
    assert r.status_code == 422 and "exceeds 8192 bytes" in r.json()["detail"], r.text


# ── deeper than the stack it is read on (the review of 5b9e806) ────────────
# The body's rule runs some frames deeper than the parser that read the body,
# so a value the parser took could be deeper than the encoder had room for
# there: then it was walked value by value, 2.6 million of them beside a
# chain 950 deep, some 5 s. Counted here in the walks, never in seconds.

def _on_a_deep_stack(frames: int, fn, *args):
    if frames:
        return _on_a_deep_stack(frames - 1, fn, *args)
    return fn(*args)


def _nested_list(levels: int, bottom):
    value = bottom
    for _ in range(levels):
        value = [value]
    return value


def test_a_value_deeper_than_the_stack_has_room_for_is_not_walked(monkeypatch):
    deep = _nested_list(950, "x")
    clean = {"deep": deep, "wide": [0] * _MANY}
    with pytest.raises(RecursionError):
        _on_a_deep_stack(100, json.dumps, clean)

    def no_walk(*a, **k):
        raise AssertionError("walked value by value")

    monkeypatch.setattr(limits, "first_in", no_walk)
    monkeypatch.setattr(limits, "_depth", no_walk)
    assert _on_a_deep_stack(100, limits.nul_problem, "metadata", clean) is None
    bad = {"deep": deep, "wide": [0] * _MANY + ["x\x00"]}
    assert _on_a_deep_stack(100, limits.nul_problem, "metadata", bad) == \
        f"metadata.wide[{_MANY}] contains a NUL byte (U+0000)"
    assert _on_a_deep_stack(100, limits.metadata_problem, clean) == \
        f"metadata exceeds {limits.MAX_METADATA_BYTES} bytes"
