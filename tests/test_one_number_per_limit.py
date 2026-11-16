"""One number per limit, read by every route and service that applies it
(audit of 17 Oct 2026).

The largest page (200) was a constant two routes imported and a literal
everywhere else, retrieve_agent_facts' own clamp included: raising GET
/v1/facts' `le=` alone would have brought back the silent half page of
2026-09-28. The operator's audit screen clamped at 500 where GET /v1/audit
took 1000. The 255 characters of an id lived in two constants and some
twenty-five literals. Each is now one constant (services/limits.py,
user_scope.MAX_ENTRIES), and this holds the routes and the columns to it.
"""
import json
import re
from pathlib import Path

import pytest

from korely_agent.services import limits, user_scope


PACKAGE = Path(__file__).resolve().parent.parent / "korely_agent"


@pytest.fixture
def spec(db, monkeypatch):
    from korely_agent.main import create_app
    from korely_agent.mcp import server as mcp_server

    monkeypatch.setenv("RUN_WORKER", "false")
    mcp_server._agent_mcp_singleton = None
    mcp_server._agent_streamable_app_singleton = None
    try:
        yield json.loads(json.dumps(create_app().openapi()))
    finally:
        mcp_server._agent_mcp_singleton = None
        mcp_server._agent_streamable_app_singleton = None


def _query(spec, path, name):
    (p,) = [p for p in spec["paths"][path]["get"]["parameters"] if p["name"] == name]
    schema = p["schema"]
    # Optional[str] is spelled anyOf [str, null].
    return next((s for s in schema.get("anyOf", [schema]) if s.get("type") != "null"), schema)


def _body_field(spec, schema_name, field):
    schema = spec["components"]["schemas"][schema_name]["properties"][field]
    return next((s for s in schema.get("anyOf", [schema]) if s.get("type") != "null"), schema)


class TestThePages:
    @pytest.mark.parametrize("path", ["/v1/memories", "/v1/events", "/v1/users", "/v1/agents",
                                      "/v1/facts"])
    def test_every_listing_takes_the_page_its_service_returns(self, spec, path):
        assert _query(spec, path, "limit")["maximum"] == limits.MAX_PAGE

    def test_the_engines_clamp_at_the_same_page(self):
        from korely_agent.services import memory_engine
        from korely_agent.services.facts import reads

        assert memory_engine.MAX_PAGE is limits.MAX_PAGE
        assert reads.MAX_PAGE is limits.MAX_PAGE

    def test_a_search_returns_what_it_accepts(self, spec):
        import inspect

        from korely_agent.services.memory_engine import AgentMemoryEngine

        assert _body_field(spec, "SearchBody", "limit")["maximum"] == limits.MAX_SEARCH_RESULTS
        widest = inspect.signature(AgentMemoryEngine.search).parameters["widest"].default
        assert widest == limits.MAX_SEARCH_RESULTS

    def test_the_audit_trail_has_one_page(self, spec):
        assert _query(spec, "/v1/audit", "limit")["maximum"] == limits.MAX_AUDIT_PAGE


class TestTheIds:
    @pytest.mark.parametrize("path,name", [
        ("/v1/memories", "user_id"), ("/v1/memories", "run_id"), ("/v1/events", "user_id"),
        ("/v1/facts", "user_id"), ("/v1/context", "user_id"), ("/v1/context", "agent_id"),
        ("/v1/audit", "user_id"),
    ])
    def test_every_read_bounds_an_id_by_the_one_number(self, spec, path, name):
        assert _query(spec, path, name)["maxLength"] == limits.MAX_ID_CHARS

    @pytest.mark.parametrize("schema", ["AddBody", "SearchBody"])
    def test_every_body_bounds_an_id_by_the_one_number(self, spec, schema):
        for field in ("user_id", "agent_id", "run_id"):
            assert _body_field(spec, schema, field)["maxLength"] == limits.MAX_ID_CHARS, field

    def test_the_namespace_rule_says_the_same_number(self):
        from korely_agent.services import ids

        assert not hasattr(ids, "NAMESPACE_ID_MAX")
        assert f"1 to {limits.MAX_ID_CHARS} characters" in ids.NAMESPACE_ID_RULE
        assert ids.namespace_id_problem("a" * limits.MAX_ID_CHARS) is None
        assert ids.namespace_id_problem("a" * (limits.MAX_ID_CHARS + 1))

    def test_the_columns_hold_what_the_routes_accept(self):
        """Raising MAX_ID_CHARS needs a migration first: an id the routes
        accept and a column cuts, or refuses, is the defect."""
        from korely_agent.db.models import (AgentFact, AgentForgetAudit, AgentMemory,
                                            AuditEvent)

        for column in (AgentMemory.end_user_id, AgentMemory.agent_id, AgentMemory.run_id,
                       AgentFact.end_user_id, AgentFact.agent_id, AuditEvent.end_user_id,
                       AuditEvent.target_id, AgentForgetAudit.target_id):
            assert column.type.length == limits.MAX_ID_CHARS, column


def test_the_users_of_a_key_are_bounded_once():
    """A key's list and an app's consent: the bound user_scope.checked holds."""
    from korely_agent.api.admin import KeyNew
    from korely_agent.api.oauth import Consent

    for model in (KeyNew, Consent):
        schema = model.model_json_schema()["properties"]["users"]
        (listed,) = [s for s in schema["anyOf"] if s.get("type") == "array"]
        assert listed["maxItems"] == user_scope.MAX_ENTRIES, model.__name__


# The literals each limit used to be written as. A new one is a second copy
# of a number that has a name. 200 and 500 as a `max_length` are a fact's
# name and value (MAX_FACT_NAME_CHARS, MAX_FACT_OBJECT_CHARS; audit of
# 19 Oct 2026: the names door, the dashboard's alias and correction).
_LITERALS = re.compile(r"max_length=(255|50|200|500)\b|\[:255\]|\ble=(200|1000|50)\b|, 500\)\)\)")
# Lines whose 200 bounds something other than a fact: a model's name
# (ModelChoiceBody) and the key an account claim brings (hosted ClaimBody).
_NOT_A_FACT = ("    model: str = Field(", "    api_key: str = Field(")


def test_no_route_or_service_writes_the_numbers_again():
    found = []
    for path in PACKAGE.rglob("*.py"):
        if "memory_core" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # A User-Agent header is not an id: its column is cut at 255.
            if (_LITERALS.search(line) and "user_agent" not in line
                    and not line.startswith(_NOT_A_FACT)):
                found.append(f"{path.relative_to(PACKAGE)}:{n}: {line.strip()}")
    assert not found, "\n".join(found)
