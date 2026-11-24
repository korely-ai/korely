"""/openapi.json, the document clients are generated from.

`GET /admin/memories/categories` was registered twice in api/admin.py, with the
same function name, so both routes got the same operation id. FastAPI serves
the first and warns "Duplicate Operation ID" while building the document; the
document itself then shows one operation, because the second overwrites the
first under the same path and method, which is why reading the ids back out of
it could not see the problem. The warning is where it shows, so the warning is
what this checks.

`db` for the reason test_which_version_and_the_way_back gives: building the
app reads the settings, and at boot it looks for a provider key in the
database.
"""
import warnings

import pytest



@pytest.fixture
def built(monkeypatch):
    from korely_agent.mcp import server as mcp_server

    monkeypatch.setenv("RUN_WORKER", "false")
    mcp_server._agent_mcp_singleton = None
    mcp_server._agent_streamable_app_singleton = None
    try:
        from korely_agent.main import create_app

        app = create_app()
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            doc = app.openapi()
        yield doc, [str(w.message) for w in seen]
    finally:
        mcp_server._agent_mcp_singleton = None
        mcp_server._agent_streamable_app_singleton = None


def test_no_two_operations_share_an_id(built):
    doc, seen = built
    dupes = [m for m in seen if "Duplicate Operation ID" in m]
    assert dupes == [], dupes
    ids = [op["operationId"] for item in doc["paths"].values()
           for op in item.values() if isinstance(op, dict) and "operationId" in op]
    assert ids and len(ids) == len(set(ids))


def test_the_categories_are_still_published(built):
    doc, _ = built
    assert "get" in doc["paths"]["/admin/memories/categories"]


def test_it_describes_no_agent_cap(built):
    """/docs is what a developer reads. It explained an `agent_cap_exceeded`
    403 and how to free a "cap slot", for a cap that only the hosted plans
    have: nothing here raises it, and `cap` in /v1/agents is 0."""
    import json
    import re

    doc, _ = built
    text = json.dumps(doc)
    found = sorted(set(re.findall(r"agent_cap_exceeded|cap slot|agent-cap|tier agent",
                                  text, re.IGNORECASE)))
    assert found == [], f"the published document still says: {found}"


def test_ping_publishes_region_as_the_operators_label(built):
    """Audit of 19 Oct 2026: /docs said GET /v1/ping answers "the `region` its
    data is in", but `region` is the REGION the operator set, `eu` when
    unset (config.Settings.region), which the service cannot check: every
    install that never set it told its clients their data was in the EU."""
    doc, _ = built
    text = " ".join(doc["paths"]["/v1/ping"]["get"]["description"].split())
    assert "its data is in" not in text
    assert "a label the operator set with REGION" in text


def test_removing_an_alias_is_not_published_as_an_undo(built):
    """Audit of 19 Oct 2026: DELETE /admin/aliases/{id} was published as "Undo:
    the alias goes, the facts go back to their written names", but two open
    facts the alias merged stay one row and a plan it closed stays closed
    (memory_core.store.remove_alias); since 0.1.42 it is also the way
    back from POST /v1/users/{id}/names."""
    doc, _ = built
    removal = " ".join(doc["paths"]["/admin/aliases/{alias_id}"]["delete"]["description"].split())
    assert "Undo" not in removal and "go back to their written names" not in removal
    assert "stay one row" in removal and "stays closed" in removal
    names = " ".join(doc["paths"]["/v1/users/{end_user}/names"]["post"]["description"].split())
    assert "DELETE /admin/aliases/{id}" in names
