"""The metadata filter of a search finds booleans, numbers and null (2026-10-06).

The filter compared `metadata ->> key` with Python's `str()` of the value, so
`{"done": true}` became the text "True", which no stored value reads as
(PostgreSQL's `->>` says "true"): it found nothing. `{"x": null}` looked for
the string "None" instead of a null, and `{"n": 5}` missed a stored 5.0. The
same defect in the hosted product (code audit of 6 Oct); the rule is the same
here, PostgreSQL only.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.services import keys as key_service
from korely_agent.services.memory_engine import AgentMemoryEngine



@pytest.fixture
def eng(db, tenant):
    return AgentMemoryEngine(db, tenant.id)


def _found(eng, metadata):
    return sorted(m["content"] for m in eng.get_all(end_user_id="u1", metadata=metadata)[0])


def _store(eng, db, **memories):
    for content, meta in memories.items():
        eng.add(content, end_user_id="u1", metadata=meta)
    db.commit()


class TestTheValuesJsonCanCarry:
    def test_a_boolean_finds_the_stored_boolean(self, eng, db):
        _store(eng, db, done={"done": True}, open={"done": False}, missing={"other": 1})
        assert _found(eng, {"done": True}) == ["done"]
        assert _found(eng, {"done": False}) == ["open"]

    def test_null_finds_a_stored_null_and_not_the_word_none(self, eng, db):
        _store(eng, db, null={"x": None}, word={"x": "None"}, absent={"y": 1})
        assert _found(eng, {"x": None}) == ["null"]

    def test_a_number_finds_an_equal_number_written_another_way(self, eng, db):
        _store(eng, db, five={"n": 5}, five_point_zero={"n": 5.0}, six={"n": 6},
               big={"n": 100000000000000000000})
        assert _found(eng, {"n": 5}) == ["five", "five_point_zero"]
        assert _found(eng, {"n": 1e20}) == ["big"]

    def test_a_stored_object_or_array_never_matches(self, eng, db):
        _store(eng, db, listed={"tags": ["a"]}, nested={"tags": {"a": 1}})
        assert _found(eng, {"tags": '["a"]'}) == []
        assert _found(eng, {"tags": '{"a": 1}'}) == []


class TestWhatAlreadyWorkedStillDoes:
    """The contract says the values are compared as strings."""

    def test_a_string_finds_the_string(self, eng, db):
        _store(eng, db, pro={"tier": "pro"}, free={"tier": "free"})
        assert _found(eng, {"tier": "pro"}) == ["pro"]

    def test_the_text_of_a_number_or_a_boolean_finds_it(self, eng, db):
        _store(eng, db, five={"n": 5}, done={"done": True})
        assert _found(eng, {"n": "5"}) == ["five"]
        assert _found(eng, {"done": "true"}) == ["done"]

    def test_the_keys_are_anded(self, eng, db):
        _store(eng, db, both={"tier": "pro", "done": True}, one={"tier": "pro", "done": False})
        assert _found(eng, {"tier": "pro", "done": True}) == ["both"]


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth(db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="t")
    return {"Authorization": f"Bearer {full}"}


@pytest.mark.parametrize("value", ['["a"]', '{"a": 1}', "NaN", "Infinity"])
def test_a_filter_value_that_is_not_a_json_scalar_is_refused(client, auth, value):
    """An object or an array matched nothing, and a 200 with no results said
    "no such memory" instead of "this filter cannot match". NaN and Infinity
    are what Python's JSON parser accepts and no stored JSON holds. The body
    is sent as text: the test client refuses to write NaN itself."""
    key = f"k{uuid.uuid4().hex[:6]}"
    r = client.post("/v1/memories/search",
                    headers={**auth, "Content-Type": "application/json"},
                    content='{"query": "anything", "metadata": {"%s": %s}}' % (key, value))
    assert r.status_code == 422, r.text
    assert key in r.text


class TestAFilterIsBounded:
    """Any number of keys was taken, and each is a condition the database
    plans: 4,000 keys in one 47 KB request held it 4.35 s, and the 2 MiB body
    allowed 157,730 (audit of 18 Oct 2026). At most
    limits.MAX_METADATA_FILTER_KEYS keys and what stored metadata may hold."""

    @pytest.fixture
    def searching(self, client, auth, monkeypatch):
        from korely_agent.services import memory_engine

        monkeypatch.setattr(memory_engine, "embed", lambda *a, **k: [0.1] * 768)

        def search(metadata):
            return client.post("/v1/memories/search", headers=auth,
                               json={"query": "anything", "metadata": metadata})
        return search

    def test_one_key_past_the_most_is_a_422_naming_it(self, searching):
        from korely_agent.services import limits

        most = limits.MAX_METADATA_FILTER_KEYS
        r = searching({f"k{i}": 0 for i in range(most + 1)})
        assert r.status_code == 422, r.text
        assert str(most) in r.json()["message"]
        assert searching({f"k{i}": 0 for i in range(most)}).status_code == 200

    def test_a_filter_larger_than_stored_metadata_may_be_is_a_422(self, searching):
        from korely_agent.services import limits

        r = searching({"note": "a" * limits.MAX_METADATA_BYTES})
        assert r.status_code == 422, r.text
        assert f"{limits.MAX_METADATA_BYTES}" in r.json()["message"]

    def test_a_nul_byte_is_a_422_not_a_503(self, searching):
        r = searching({"tier": "pro\u0000"})
        assert r.status_code == 422, r.text

    def test_the_engine_refuses_it_for_every_caller(self, eng):
        from korely_agent.services import limits

        too_many = {f"k{i}": 0 for i in range(limits.MAX_METADATA_FILTER_KEYS + 1)}
        with pytest.raises(ValueError):
            eng.get_all(end_user_id="u1", metadata=too_many)
        with pytest.raises(ValueError):
            eng.search("anything", metadata=too_many)
