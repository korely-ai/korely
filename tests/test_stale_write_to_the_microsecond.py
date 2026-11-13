"""`expected_updated_at` is the exact version, up to the write (2026-10-06).

Two defects in the one guard of PATCH /v1/memories/{id}, the same as the
hosted product's (code audit of 6 Oct):

- a tolerance of one second, "to absorb serialization noise": two edits of
  the same memory in the same second, the common case of two agents answering
  the same turn, both passed, and the second, made on the version before,
  overwrote the first with a 200. There is no noise to absorb: the client
  sends back the string the API returned, which reads back to the stored
  instant to the microsecond;
- the check read the memory and the engine wrote afterwards: an edit landing
  in between was overwritten all the same.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from korely_agent.db.models import AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services.ids import parse_mem_id
from korely_agent.services.memory_engine import AgentMemoryEngine, StaleVersion



@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: None)
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


def _written(client, auth):
    m = client.post("/v1/memories", headers=auth,
                    json={"content": "Maria lives in Rome.", "user_id": "maria"}).json()
    held = client.get(f"/v1/memories/{m['id']}", headers=auth).json()
    return m["id"], held["updated_at"] or held["created_at"]


def _edit(client, auth, mid, content, version):
    return client.patch(f"/v1/memories/{mid}", headers=auth,
                        json={"content": content, "expected_updated_at": version})


class TestTheVersionIsExact:
    def test_two_edits_in_the_same_second_the_second_is_refused(self, client, auth):
        mid, version = _written(client, auth)
        first = _edit(client, auth, mid, "Maria lives in Milan.", version)
        assert first.status_code == 200, first.text
        second = _edit(client, auth, mid, "Maria lives in Turin.", version)
        assert second.status_code == 409, "the second edit overwrote the first"
        assert second.json()["code"] == "stale_write"
        now = client.get(f"/v1/memories/{mid}", headers=auth).json()
        assert now["content"] == "Maria lives in Milan."

    def test_a_version_rounded_to_the_second_is_another_version(self, client, auth, db):
        mid, _version = _written(client, auth)
        # The stored instant is set to one with microseconds, so there is
        # always something to round: until the audit of 17 Oct 2026 a stored
        # instant on a whole second (one run in a million) made the "rounded"
        # version the exact one, and the test failed red for nothing.
        db.execute(text("UPDATE agent_memories SET updated_at = "
                        "date_trunc('second', now()) + interval '123456 microseconds' "
                        "WHERE id = :id"), {"id": parse_mem_id(mid)})
        db.commit()
        held = client.get(f"/v1/memories/{mid}", headers=auth).json()
        exact = datetime.fromisoformat(held["updated_at"])
        assert exact.microsecond == 123456
        rounded = exact.replace(microsecond=0).isoformat()
        r = _edit(client, auth, mid, "Maria lives in Milan.", rounded)
        assert r.status_code == 409, r.text
        # And the exact version still writes: the refusal is the rounding.
        assert _edit(client, auth, mid, "Maria lives in Milan.",
                     held["updated_at"]).status_code == 200

    def test_the_same_instant_written_with_another_offset_is_the_same_version(self, client, auth):
        mid, version = _written(client, auth)
        rome = datetime.fromisoformat(version).astimezone(timezone(timedelta(hours=2)))
        r = _edit(client, auth, mid, "Maria lives in Milan.", rome.isoformat())
        assert r.status_code == 200, r.text

    def test_an_unreadable_version_is_refused(self, client, auth):
        # 422, the caller's mistake, since the audit of 17 Oct 2026: a 409
        # `stale_write` made a client that retries on it loop for ever.
        mid, _version = _written(client, auth)
        assert _edit(client, auth, mid, "Maria lives in Milan.", "yesterday").status_code == 422

    def test_the_returned_version_writes_again(self, client, auth):
        mid, version = _written(client, auth)
        first = _edit(client, auth, mid, "Maria lives in Milan.", version).json()
        again = _edit(client, auth, mid, "Maria lives in Turin.", first["updated_at"])
        assert again.status_code == 200, again.text

    def test_a_missing_memory_is_still_a_404(self, client, auth):
        r = _edit(client, auth, "mem_00000000-0000-0000-0000-000000000000",
                  "Maria lives in Milan.", "2026-10-06T10:00:00+00:00")
        assert r.status_code == 404


class TestTheVersionHoldsUntilTheWrite:
    def test_an_edit_committed_between_the_check_and_the_write_wins(
            self, db, tenant, engine, monkeypatch):
        """Another request commits its edit while this one computes its vector:
        this one finds the memory moved and writes nothing."""
        eng = AgentMemoryEngine(db, tenant.id)
        row = eng.add("Maria lives in Rome.", end_user_id="maria")
        db.commit()
        mid = str(row.id)
        version = row.created_at.isoformat()

        def the_other_request_lands(m, text_, **kw):
            with engine.begin() as c:
                c.execute(text("UPDATE agent_memories SET content = 'Maria lives in Naples.', "
                               "updated_at = now() WHERE id = :id"), {"id": m.id})

        monkeypatch.setattr(AgentMemoryEngine, "_vector_of",
                            lambda self, m, text_, **kw: the_other_request_lands(m, text_))
        with pytest.raises(StaleVersion):
            eng.update(mid, "Maria lives in Milan.", version=version)

        db.expire_all()
        assert db.get(AgentMemory, row.id).content == "Maria lives in Naples."

    def test_through_the_api_the_edit_in_between_is_not_overwritten(
            self, client, auth, db, engine, monkeypatch):
        """The route's check passes, another request commits, then the write:
        until 6 Oct the write overwrote it with a 200."""
        mid, version = _written(client, auth)

        def the_other_request_lands(self_, m, text_, **kw):
            with engine.begin() as c:
                c.execute(text("UPDATE agent_memories SET content = 'Maria lives in Naples.', "
                               "updated_at = now() WHERE id = :id"), {"id": m.id})

        monkeypatch.setattr(AgentMemoryEngine, "_vector_of", the_other_request_lands)
        r = _edit(client, auth, mid, "Maria lives in Milan.", version)
        assert r.status_code == 409, r.text
        db.expire_all()
        assert client.get(f"/v1/memories/{mid}", headers=auth).json()["content"] == "Maria lives in Naples."
