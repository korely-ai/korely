"""The OpenAPI document names the families and the subject types a fact can
carry, the engine's own (review of the documentation, 1 Nov 2026).

`predicate_family` was described with `location` and `role`, families that
do not exist: a reader filtered GET /v1/facts by a family that is not one and
got an empty page with no error. The names are read from memory_core
(`families.FAMILY_ORDER`, `fact_extractor.ALLOWED_SUBJECT_TYPES`), so the
spec says what the engine writes, all of it and nothing else. A new fact's
subject type is stored as its writer sent it, and a restatement keeps the one
first stored: the spec names the types extraction gives and
`self`, which extraction never gives but GET /v1/profile lists first
(`context.is_about_the_end_user`), and says that any other may be there.
"""
import re
import uuid
from types import SimpleNamespace

import pytest

from korely_agent.db.models import AgentFact
from korely_agent.main import create_app
from korely_agent.services.context import SELF_SUBJECT_TYPE, is_about_the_end_user
from korely_agent.services.facts.reads import FAMILY_FILTER
from korely_agent.services.limits import MAX_FACT_NAME_CHARS
from korely_agent.services.memory_core.fact_extractor import ALLOWED_SUBJECT_TYPES
from korely_agent.services.memory_core.families import FAMILY_ORDER

_NAMED = re.compile(r"`([^`]+)`")


def _named(text: str) -> set:
    """The names a description quotes, in backticks."""
    return set(_NAMED.findall(text))


@pytest.fixture(scope="module")
def spec():
    """The document once for the module: building the app is most of the time."""
    return create_app().openapi()


def test_a_facts_family_is_one_of_the_engines(spec):
    # The read shape, and the answer of POST and PATCH /v1/facts. A memory's
    # facts[] is a list of dicts in the spec, with no schema to describe.
    for schema in ("Fact", "FactWriteResponse"):
        text = spec["components"]["schemas"][schema]["properties"]["predicate_family"]["description"]
        assert _named(text) == set(FAMILY_ORDER), (schema, text)
        assert "location" not in text and "role" not in text, (schema, text)


def test_a_facts_subject_type_names_the_known_types_and_lets_in_any(spec):
    schemas = spec["components"]["schemas"]
    most = AgentFact.__table__.c.subject_type.type.length
    # The read shape, and the write body, whose field lets in any type (FactBody).
    for schema in ("Fact", "FactBody"):
        text = schemas[schema]["properties"]["subject_type"]["description"]
        assert set(ALLOWED_SUBJECT_TYPES) | {SELF_SUBJECT_TYPE} <= _named(text), (schema, text)
        assert f"any type of up to {most} characters" in text, (schema, text)
        assert "GET /v1/profile" in text, (schema, text)
        # test_the_context_block_does_not_read_the_subject_type holds it.
        assert "The context block of GET /v1/context does not read the type" in text, \
            (schema, text)
        # test_a_restated_fact_keeps_the_type_it_was_stored_with holds it.
        assert "restates a fact already true keeps the type the fact was first stored with" \
            in text, (schema, text)
    assert "POST /v1/facts" in schemas["Fact"]["properties"]["subject_type"]["description"]
    assert schemas["FactBody"]["properties"]["subject_type"]["maxLength"] == most


def test_self_is_a_fact_about_the_end_user_whatever_its_subject():
    """What the description says `self` does: GET /v1/profile puts a fact
    first by this rule (api/facts.get_profile)."""
    mine = SimpleNamespace(subject_type=SELF_SUBJECT_TYPE, subject_label="I", subject_canonical="I")
    assert SELF_SUBJECT_TYPE not in ALLOWED_SUBJECT_TYPES
    assert is_about_the_end_user(mine, "maria")
    # "lower case, matched exactly": no other spelling of it is.
    for other in ("person", "Self", " self"):
        assert not is_about_the_end_user(SimpleNamespace(**{**vars(mine), "subject_type": other}),
                                         "maria"), other


def test_the_context_block_does_not_read_the_subject_type():
    """What the description says of the block: a fact typed `self` whose
    subject is not the end user reads as any other type does
    (memory_core.presentation.context_line), and a subject that is the end
    user reads "The user" whatever its type (review of 4 Nov 2026)."""
    from korely_agent.services.memory_core.presentation import context_line

    def line(subject, subject_type):
        f = SimpleNamespace(subject_label=subject, subject_canonical=subject.lower(),
                            subject_type=subject_type, predicate_raw="lives_in",
                            object_label="Rome", object_canonical="rome")
        return context_line(f, "maria")

    for subject in ("Luca", "maria"):
        assert line(subject, SELF_SUBJECT_TYPE) == line(subject, "person"), subject
    assert line("Luca", SELF_SUBJECT_TYPE) == "- Luca lives_in Rome"
    assert line("maria", "person") == "- The user lives_in Rome"


def test_a_restated_fact_keeps_the_type_it_was_stored_with(db, tenant, monkeypatch):
    """The engine reconfirms a triple already true and writes nothing new
    (memory_core.writing, step 2), so a `self` sent then is not stored: the
    descriptions say so (review of 3 Nov 2026). No model is asked."""
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.services import keys as key_service

    monkeypatch.setenv("RUN_WORKER", "false")
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="restated",
                                          scopes=["memories:read", "memories:write"])
    head = {"Authorization": f"Bearer {full}"}
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    body = {"subject": "Maria", "predicate": "lives_in", "object": "Rome", "user_id": "u1",
            "subject_type": "person"}
    with TestClient(app) as client:
        first = client.post("/v1/facts", headers=head, json=body)
        again = client.post("/v1/facts", headers=head,
                            json={**body, "subject_type": SELF_SUBJECT_TYPE})
        read = client.get("/v1/facts", headers=head, params={"user_id": "u1"})
    assert first.status_code == again.status_code == 201, (first.text, again.text)
    assert again.json()["id"] == first.json()["id"]
    assert [f["subject_type"] for f in read.json()["facts"]] == ["person"], read.text


def test_the_family_filter_names_the_families_it_finds(spec):
    """GET /v1/facts?predicate_family= answers an empty page for a name that
    is not a family (services/facts/reads.py): the parameter says which are."""
    params = spec["paths"]["/v1/facts"]["get"]["parameters"]
    text = next(p for p in params if p["name"] == "predicate_family").get("description", "")
    assert _named(text) == set(FAMILY_ORDER), text
    # As reads.py reads it: stripped and lower-cased before the check; and as
    # limits.name_problem bounds it (test_the_filter_reads_a_family_as_the_sentence_says
    # and tests/test_every_read_input_by_the_rule.py hold the code to each clause).
    assert "case and surrounding spaces ignored" in text and "not an error" in text, text
    assert "An empty value is no filter; one made only of spaces is no family" in text, text
    assert f"longer than {MAX_FACT_NAME_CHARS} characters, or with a NUL byte" in text, text


def test_rest_the_admin_and_mcp_describe_the_filter_by_one_sentence(spec):
    """GET /admin/facts and korely_get_facts read the filter as GET /v1/facts
    does (services/facts/reads.py) and named no family."""
    from korely_agent.mcp import server as mcp_server

    for path in ("/v1/facts", "/admin/facts"):
        params = spec["paths"][path]["get"]["parameters"]
        assert next(p for p in params if p["name"] == "predicate_family").get("description") \
            == FAMILY_FILTER, path
    tool = mcp_server._get_agent_mcp()._tool_manager.get_tool("korely_get_facts")
    assert tool.parameters["properties"]["predicate_family"].get("description") == FAMILY_FILTER
    assert "predicate_family" in tool.description


def test_the_filter_reads_a_family_as_the_sentence_says(db, tenant):
    """Case and surrounding spaces ignored; a name that is not a family an
    empty result; an empty value no filter, and one of only spaces no family."""
    from korely_agent.services.facts import retrieve_agent_facts
    from korely_agent.services.sentinels import EVERY_PROJECT

    def fact(family, where):
        return AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1",
                         subject_label="Luca", predicate_raw="lives_in",
                         predicate_canonical="lives_in", predicate_family=family,
                         object_label=where)

    def found(family):
        return retrieve_agent_facts(db, tenant.id, predicate_family=family,
                                    project_id=EVERY_PROJECT)[1]

    db.add(fact("places", "Rome"))
    db.commit()
    assert found("places") == found(" Places ") == 1
    # Stripped, it is empty, and the empty name is no family.
    assert found("   ") == 0
    # A row that keeps a stored name that is not a family is shown under its
    # relation's family (families.family_of_the_fact), and the filter finds it
    # under no family: not under the stored name, and not under the family it
    # is shown with, since memory_core.store.family_filter reads the stored
    # name. The engine's two rules disagree on such a row, a defect of the
    # engine left out of this test (review of 3 Nov 2026), logged as a
    # finding on 4 Nov 2026 for memory_core to fix.
    db.add(fact("location", "Milan"))
    db.commit()
    assert found("location") == 0
    assert found("") == found(None) == 2
