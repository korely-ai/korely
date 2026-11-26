"""A fact posted directly can say which fact it replaces (found on 6 Nov
2026, fixed in 1.0.40): `korely add-fact LoCoMo
has_score "86.9%"` left the wrong `LoCoMo has_score 93.6%` true beside it,
since only the contradiction judge closed facts on a direct write and it
did not close that one. `POST /v1/facts` with `replaces` closes the named
fact where the new one begins, keeps it in the history and names it in
`invalidated`, as `PATCH /v1/facts/{id}` does from the old fact's words
(Mem0's update of a memory by its id, without losing the old value).

The fact replaced must be this project's (404), of the same end user and
agent (422), still current (409), and the new fact a current one that does
not begin before it (422). Without `replaces` nothing changes.

No model key: no judge runs, so what closes a fact here is `replaces`."""
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from korely_agent.db.models import AgentFact, AgentProject
from korely_agent.services import keys as key_service
from korely_agent.services.ids import parse_fact_id


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


def _key(db, tenant, **kw):
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id,
                                          name=f"k-{uuid.uuid4().hex[:6]}",
                                          scopes=["memories:read", "memories:write"], **kw)
    return {"Authorization": f"Bearer {full}"}


@pytest.fixture
def head(db, tenant):
    return _key(db, tenant)


SCORE = {"subject": "LoCoMo", "predicate": "has_score", "user_id": "team"}


def _post(client, head, **body):
    return client.post("/v1/facts", headers=head, json={**SCORE, **body})


def _row(db, public_id):
    db.expire_all()
    return db.get(AgentFact, parse_fact_id(public_id))


def _current(client, head, user_id="team"):
    r = client.get("/v1/facts", headers=head, params={"user_id": user_id})
    assert r.status_code == 200, r.text
    return sorted(f["object"] for f in r.json()["facts"])


def test_the_findings_case_closes_the_fact_it_replaces(db, client, head):
    old = _post(client, head, object="93.6%")
    assert old.status_code == 201, old.text
    new = _post(client, head, object="86.9%", replaces=old.json()["id"])
    assert new.status_code == 201, new.text
    assert new.json()["invalidated"] == [old.json()["id"]]
    assert _current(client, head) == ["86.9%"]
    # Closed, not deleted: where the new one begins, pointing at it.
    row = _row(db, old.json()["id"])
    assert row is not None and row.invalid_at is not None
    assert row.invalid_at.isoformat() == new.json()["valid_from"]
    assert row.invalidated_by_id == parse_fact_id(new.json()["id"])
    r = client.get("/v1/facts", headers=head, params={"user_id": "team",
                                                      "include_invalidated": "true"})
    assert sorted(f["object"] for f in r.json()["facts"]) == ["86.9%", "93.6%"]
    # Without the field, a write closes nothing on its own (no judge here).
    third = _post(client, head, object="88.0%")
    assert third.status_code == 201 and third.json()["invalidated"] == [], third.text
    assert _current(client, head) == ["86.9%", "88.0%"]


def test_the_fact_replaced_is_dated_where_the_new_one_begins(db, client, head):
    old = _post(client, head, object="93.6%", valid_from="2026-11-01")
    new = _post(client, head, object="86.9%", valid_from="2026-11-06", replaces=old.json()["id"])
    assert new.status_code == 201, new.text
    assert _row(db, old.json()["id"]).invalid_at.date().isoformat() == "2026-11-06"
    r = client.get("/v1/facts", headers=head, params={"user_id": "team",
                                                      "as_of": "2026-11-03"})
    assert [f["object"] for f in r.json()["facts"]] == ["93.6%"]


def test_another_end_users_fact_is_a_422_and_nothing_is_written(db, client, head):
    theirs = _post(client, head, object="93.6%", user_id="someone-else")
    assert theirs.status_code == 201, theirs.text
    before = db.query(AgentFact).count()
    r = _post(client, head, object="86.9%", replaces=theirs.json()["id"])
    assert r.status_code == 422 and r.json()["code"] == "invalid_request", r.text
    assert "same user_id and agent_id" in r.json()["message"]
    assert db.query(AgentFact).count() == before
    assert _current(client, head, "someone-else") == ["93.6%"]


def test_another_agents_fact_is_a_422(db, client, head):
    theirs = _post(client, head, object="93.6%", agent_id="planner")
    r = _post(client, head, object="86.9%", replaces=theirs.json()["id"])
    assert r.status_code == 422 and "same user_id and agent_id" in r.json()["message"], r.text


def test_another_projects_fact_is_not_found(db, tenant, client, head):
    other = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="other", slug="other")
    db.add(other)
    db.commit()
    theirs = _post(client, _key(db, tenant, project_id=other.id), object="93.6%")
    assert theirs.status_code == 201, theirs.text
    before = db.query(AgentFact).count()
    r = _post(client, head, object="86.9%", replaces=theirs.json()["id"])
    assert r.status_code == 404 and r.json()["code"] == "not_found", r.text
    assert db.query(AgentFact).count() == before
    assert _row(db, theirs.json()["id"]).invalid_at is None


@pytest.mark.parametrize("known", [True, False], ids=["a-fact-id", "not-an-id"])
def test_an_unknown_fact_is_not_found(client, head, known):
    from korely_agent.services.ids import fact_public_id

    r = _post(client, head, object="86.9%",
              replaces=fact_public_id(uuid.uuid4()) if known else "not-an-id")
    assert r.status_code == 404, r.text


def test_a_confined_key_cannot_reach_another_end_users_fact(db, tenant, client, head):
    """Another person's fact, for a key confined to its end users, is not
    found, as it is for PATCH: a 422 would say the id is real."""
    theirs = _post(client, head, object="93.6%", user_id="someone-else")
    confined = _key(db, tenant, end_users=["team"])
    r = _post(client, confined, object="86.9%", replaces=theirs.json()["id"])
    assert r.status_code == 404, r.text


def test_a_fact_that_is_history_is_a_409(client, head):
    old = _post(client, head, object="93.6%")
    assert client.post(f"/v1/facts/{old.json()['id']}/forget", headers=head).status_code == 200
    r = _post(client, head, object="86.9%", replaces=old.json()["id"])
    assert r.status_code == 409 and r.json()["code"] == "fact_not_current", r.text


@pytest.mark.parametrize("tense", ["past", "planned"])
def test_only_a_current_fact_replaces_one(client, head, tense):
    old = _post(client, head, object="93.6%")
    r = _post(client, head, object="86.9%", tense=tense, replaces=old.json()["id"])
    assert r.status_code == 422 and "takes a current fact" in r.json()["message"], r.text
    assert _current(client, head) == ["93.6%"]


def test_a_fact_that_begins_before_the_one_it_replaces_is_a_422(client, head):
    old = _post(client, head, object="93.6%", valid_from="2026-11-06")
    r = _post(client, head, object="86.9%", valid_from="2026-11-01", replaces=old.json()["id"])
    assert r.status_code == 422, r.text
    assert "earlier" in r.json()["message"]
    assert _current(client, head) == ["93.6%"]


def test_a_day_replaces_a_fact_begun_later_that_same_day(db, client, head):
    # A start given as a day is the same valid time as any start of that UTC
    # day: the fact it replaces closes at its own start, never before it began.
    old = _post(client, head, object="93.6%", valid_from="2026-11-06T20:56:00Z")
    new = _post(client, head, object="86.9%", valid_from="2026-11-06", replaces=old.json()["id"])
    assert new.status_code == 201, new.text
    row = _row(db, old.json()["id"])
    assert row.invalid_at == row.valid_from
    assert _current(client, head) == ["86.9%"]
    later = _post(client, head, object="80.0%", valid_from="2026-11-07T10:00:00Z")
    r = _post(client, head, object="81.0%", valid_from="2026-11-06", replaces=later.json()["id"])
    assert r.status_code == 422 and "earlier" in r.json()["message"], r.text


def test_the_same_fact_restated_replaces_nothing(db, client, head):
    old = _post(client, head, object="93.6%")
    again = _post(client, head, object="93.6%", replaces=old.json()["id"])
    assert again.status_code == 201, again.text
    assert again.json()["id"] == old.json()["id"] and again.json()["invalidated"] == []
    assert _current(client, head) == ["93.6%"]


def test_a_new_value_already_true_from_earlier_retracts_the_one_it_replaces(db, client, head):
    """The new triple is one already true since before the fact it replaces
    began (the write reconfirms it): the replaced fact is closed where it
    began, true at no moment, never before it began."""
    earlier = _post(client, head, object="86.9%", valid_from="2026-11-01")
    old = _post(client, head, object="93.6%", valid_from="2026-11-06")
    again = _post(client, head, object="86.9%", replaces=old.json()["id"])
    assert again.status_code == 201, again.text
    assert again.json()["id"] == earlier.json()["id"]
    assert again.json()["invalidated"] == [old.json()["id"]]
    row = _row(db, old.json()["id"])
    assert row.invalid_at == row.valid_from


def test_an_end_still_ahead_is_brought_to_where_the_new_fact_begins(db, client, head):
    old = _post(client, head, object="93.6%", valid_from="2026-11-01")
    row = _row(db, old.json()["id"])
    row.invalid_at = row.valid_from.replace(year=2030)
    db.commit()
    new = _post(client, head, object="86.9%", valid_from="2026-11-06", replaces=old.json()["id"])
    assert new.status_code == 201 and new.json()["invalidated"] == [old.json()["id"]], new.text
    row = _row(db, old.json()["id"])
    assert row.invalid_at.date().isoformat() == "2026-11-06"
    assert row.invalidated_by_id == parse_fact_id(new.json()["id"])


def test_the_closing_is_announced_kept_by_hand_and_in_the_trail(db, tenant, client, head,
                                                               monkeypatch):
    """Its `fact.invalidated` (closures.announce), its record of a caller's
    word (agent_forget_audit, so no deletion gives the fact back) and the
    trail's row of the write, with the id of the fact replaced and no text."""
    from korely_agent.db.models import AgentForgetAudit, AuditEvent
    from korely_agent.services.facts import closures

    announced = []
    real = closures.announce
    monkeypatch.setattr(closures, "announce",
                        lambda db_, t, fid, by, at, **k: announced.append((fid, by))
                        or real(db_, t, fid, by, at, **k))
    old = _post(client, head, object="93.6%")
    new = _post(client, head, object="86.9%", replaces=old.json()["id"])
    assert (old.json()["id"], new.json()["id"]) in announced
    db.expire_all()
    kept = db.query(AgentForgetAudit).filter(AgentForgetAudit.tenant_id == tenant.id,
                                             AgentForgetAudit.target_id == old.json()["id"]).all()
    assert len(kept) == 1 and kept[0].facts_invalidated == 1
    rows = [r for r in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).all()
            if (r.meta or {}).get("endpoint") == "facts:create" and r.result == "ok"]
    assert [(r.target_id, r.meta.get("facts_closed"), r.meta.get("replaces")) for r in rows
            if r.target_id == new.json()["id"]] == [(new.json()["id"], 1, old.json()["id"])]
    assert "93.6" not in str([r.meta for r in rows]) and "86.9" not in str([r.meta for r in rows])


def test_the_409_says_what_to_name_instead(client, head):
    old = _post(client, head, object="93.6%")
    newer = _post(client, head, object="86.9%", replaces=old.json()["id"])
    r = _post(client, head, object="88.0%", replaces=old.json()["id"])
    assert r.status_code == 409, r.text
    assert r.json()["current_fact_id"] == newer.json()["id"]
    assert r.json()["message"].startswith("The fact `replaces` names is no longer current")
    assert newer.json()["id"] in r.json()["message"]


# ── two at once (review of fix/limiti-dns-replaces, 25 Nov 2026) ─────────
def _the_first_waits_at_its_commit(monkeypatch):
    """The first write to reach its commit (emit_invalidations, just before
    it) waits there until `go` is set: the moment a second write runs while
    the first holds what it closed."""
    from korely_agent.services.facts import service as fsvc

    holding, go = threading.Event(), threading.Event()
    real = fsvc.emit_invalidations

    def held(*a, **k):
        if not holding.is_set():
            holding.set()
            go.wait(20)
        return real(*a, **k)

    monkeypatch.setattr(fsvc, "emit_invalidations", held)
    return holding, go


def _race(engine, first, second, monkeypatch):
    """`first` and `second` (each given its own session) at once, the second
    started while the first waits at its commit. Returns (outcomes, whether
    the second was still waiting while the first held)."""
    from sqlalchemy.orm import sessionmaker

    holding, go = _the_first_waits_at_its_commit(monkeypatch)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    out = {}

    def run(name, fn):
        s = Session()
        try:
            out[name] = fn(s)
        except BaseException as e:  # noqa: BLE001 - what each write got
            out[name] = e
            s.rollback()
        finally:
            s.close()

    a = threading.Thread(target=run, args=("first", first), daemon=True)
    a.start()
    assert holding.wait(20)
    b = threading.Thread(target=run, args=("second", second), daemon=True)
    b.start()
    b.join(1.0)
    waited = b.is_alive()
    go.set()
    a.join(20)
    b.join(20)
    return out, waited


def _seeded(db, tenant, obj="93.6%"):
    from korely_agent.services import facts as agent_facts
    from tests.conftest import seeded_project_id

    return agent_facts.write_fact_direct(
        db, tenant.id, subject="LoCoMo", predicate="has_score", obj=obj,
        end_user_id="team", project_id=seeded_project_id(db, tenant.id))


def test_two_writes_replacing_one_fact_at_once_close_it_once(db, engine, tenant, monkeypatch):
    from korely_agent.services import facts as agent_facts
    from tests.conftest import seeded_project_id

    old = _seeded(db, tenant)
    project = seeded_project_id(db, tenant.id)

    def replacing(obj):
        return lambda s: agent_facts.write_fact_direct(
            s, tenant.id, subject="LoCoMo", predicate="has_score", obj=obj,
            end_user_id="team", project_id=project, replaces=parse_fact_id(old["id"]))

    out, waited = _race(engine, replacing("86.9%"), replacing("88.0%"), monkeypatch)
    assert waited, "the second write did not wait for the first to commit"
    assert out["first"]["invalidated"] == [old["id"]]
    assert isinstance(out["second"], agent_facts.FactNotCurrent), out["second"]
    db.expire_all()
    current = [f.object_label for f in db.query(AgentFact).filter(
        AgentFact.tenant_id == tenant.id, AgentFact.invalid_at.is_(None)).all()]
    assert current == ["86.9%"]


def test_two_corrections_of_one_fact_at_once_close_it_once(db, engine, tenant, monkeypatch):
    """PATCH /v1/facts/{id} (correct_fact) had the same gap."""
    from korely_agent.services import facts as agent_facts
    from tests.conftest import seeded_project_id

    old = _seeded(db, tenant)
    project = seeded_project_id(db, tenant.id)

    def correcting(obj):
        return lambda s: agent_facts.correct_fact(
            s, tenant.id, parse_fact_id(old["id"]), obj=obj, project_id=project,
            write_shape=True)

    out, waited = _race(engine, correcting("86.9%"), correcting("88.0%"), monkeypatch)
    assert waited, "the second correction did not wait for the first to commit"
    assert out["first"]["invalidated"] == [old["id"]]
    assert isinstance(out["second"], agent_facts.FactNotCurrent), out["second"]


# ── a deadlock, and no lock through the model calls (review of 26 Nov 2026) ─
def test_a_deadlock_postgres_broke_is_a_503_to_retry(client, head, monkeypatch):
    """Two facts posted at once, each with `replaces` naming the other's twin,
    can deadlock: each closes its fact after the write reconfirmed the
    other's twin (memory_core holds that row). Postgres rolls one back with
    40P01, which answered 500; it is the 503 of a busy database, retry
    (core/threads.gave_way). Not retried by the server: the write may have
    called a model."""
    from sqlalchemy.exc import OperationalError

    from korely_agent.core import threads
    from korely_agent.services import facts as agent_facts

    class Orig(Exception):
        pgcode = "40P01"

    def deadlocked(*a, **k):
        raise OperationalError("UPDATE agent_facts ...", {}, Orig("deadlock detected"))

    monkeypatch.setattr(agent_facts, "write_fact_direct", deadlocked)
    r = _post(client, head, object="86.9%")
    assert r.status_code == 503 and r.json()["code"] == "unavailable", r.text
    assert r.headers["Retry-After"] == str(threads.BUSY_RETRY_SECONDS)
    assert threads.gave_way(OperationalError("x", {}, Orig()))
    assert not threads.deadline_passed(OperationalError("x", {}, Orig()))


@pytest.mark.parametrize("door", ["replaces", "correction"])
def test_the_fact_is_not_held_through_the_writes_model_calls(db, engine, tenant, monkeypatch,
                                                              door):
    """While a write that closes a fact is in its model calls (the
    vocabulary, the judge: `_persist_one`), the fact is not locked: a
    forget of it by another transaction goes through at once. The write then
    sees the fact changed since it read it and answers 409, nothing
    written. Locked from its first read (d0636f64), the forget waited for
    the whole of the write."""
    from sqlalchemy.orm import sessionmaker

    from korely_agent.services import facts as agent_facts
    from korely_agent.services.facts import service as fsvc
    from tests.conftest import seeded_project_id

    old = _seeded(db, tenant)
    project = seeded_project_id(db, tenant.id)
    in_its_calls, go = threading.Event(), threading.Event()
    real = fsvc._persist_one

    def slow(*a, **k):
        in_its_calls.set()
        go.wait(20)
        return real(*a, **k)

    monkeypatch.setattr(fsvc, "_persist_one", slow)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    out = {}

    def write():
        s = Session()
        try:
            if door == "replaces":
                out["write"] = agent_facts.write_fact_direct(
                    s, tenant.id, subject="LoCoMo", predicate="has_score", obj="86.9%",
                    end_user_id="team", project_id=project,
                    replaces=parse_fact_id(old["id"]))
            else:
                out["write"] = agent_facts.correct_fact(
                    s, tenant.id, parse_fact_id(old["id"]), obj="86.9%", project_id=project,
                    write_shape=True)
        except BaseException as e:  # noqa: BLE001 - what the write got
            out["write"] = e
            s.rollback()
        finally:
            s.close()

    def forget():
        s = Session()
        try:
            out["forget"] = agent_facts.forget_fact(s, tenant.id, parse_fact_id(old["id"]),
                                                    project_id=project)
        finally:
            s.close()

    w = threading.Thread(target=write, daemon=True)
    w.start()
    assert in_its_calls.wait(20)
    f = threading.Thread(target=forget, daemon=True)
    f.start()
    f.join(3.0)
    held = f.is_alive()
    go.set()
    w.join(20)
    f.join(20)
    assert not held, "the forget waited for the write's model calls"
    assert out["forget"]["status"] == "forgotten", out["forget"]
    assert isinstance(out["write"], agent_facts.FactNotCurrent), out["write"]
    db.expire_all()
    assert [f.object_label for f in db.query(AgentFact).filter(
        AgentFact.tenant_id == tenant.id).all()] == ["93.6%"]


# ── values swapped at once, a fact deleted meanwhile (the review, 26 Nov 2026)
def test_two_writes_swapping_values_at_once_leave_a_value_true(db, engine, tenant, monkeypatch):
    """X is "A", Y is "B". One write posts "B" replacing X (it lands on Y,
    reconfirmed), the other posts "A" replacing Y (it lands on X). Each
    closed the fact the other landed on, both answered 201 and no value was
    left true (P8b). The row a write lands on is locked with the one it
    closes and must still be open: the second answers 409."""
    from korely_agent.services import facts as agent_facts
    from tests.conftest import seeded_project_id

    x, y = _seeded(db, tenant, "A-value"), _seeded(db, tenant, "B-value")
    project = seeded_project_id(db, tenant.id)

    def posting(obj, replaced):
        return lambda s: agent_facts.write_fact_direct(
            s, tenant.id, subject="LoCoMo", predicate="has_score", obj=obj,
            end_user_id="team", project_id=project, replaces=parse_fact_id(replaced["id"]))

    out, _waited = _race(engine, posting("B-value", x), posting("A-value", y), monkeypatch)
    db.expire_all()
    current = [f.object_label for f in db.query(AgentFact).filter(
        AgentFact.tenant_id == tenant.id, AgentFact.invalid_at.is_(None)).all()]
    assert current == ["B-value"], (current, out)
    assert out["first"]["id"] == y["id"] and out["first"]["invalidated"] == [x["id"]]
    assert isinstance(out["second"], agent_facts.RestatedFactClosed), out["second"]


def test_the_409_of_a_value_closed_meanwhile_says_so():
    from korely_agent.api.facts import replaces_message
    from korely_agent.services import facts as agent_facts

    text = replaces_message(agent_facts.RestatedFactClosed("fct_x", None))
    assert text.startswith("The value written is the fact fct_x, which another write closed "
                           "or changed")


@pytest.mark.parametrize("door", ["replaces", "correction"])
def test_a_fact_deleted_while_the_write_runs_is_not_found(db, engine, tenant, monkeypatch, door):
    """Deleted while the write was in its model calls (an erasure of its
    memory or its end user): the closing found no row and answered 500
    (NoResultFound). Now nothing is written and the caller answers 404."""
    from sqlalchemy.orm import sessionmaker

    from korely_agent.services import facts as agent_facts
    from korely_agent.services.facts import service as fsvc
    from tests.conftest import seeded_project_id

    old = _seeded(db, tenant)
    oid = parse_fact_id(old["id"])
    project = seeded_project_id(db, tenant.id)
    real = fsvc._persist_one
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def deleted_meanwhile(*a, **k):
        other = Session()
        other.query(AgentFact).filter(AgentFact.id == oid).delete()
        other.commit()
        other.close()
        return real(*a, **k)

    monkeypatch.setattr(fsvc, "_persist_one", deleted_meanwhile)
    s = Session()
    try:
        if door == "replaces":
            out = agent_facts.write_fact_direct(
                s, tenant.id, subject="LoCoMo", predicate="has_score", obj="86.9%",
                end_user_id="team", project_id=project, replaces=oid)
        else:
            out = agent_facts.correct_fact(s, tenant.id, oid, obj="86.9%", project_id=project,
                                           write_shape=True)
    finally:
        s.close()
    assert out is None
    db.expire_all()
    assert db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).count() == 0


def test_the_writes_own_judge_closing_the_fact_is_its_own(db, tenant, monkeypatch):
    """Mutant N2: the judge of this very write closes the fact replaced (for
    the new fact) before the closing: that change is the write's, not
    another's, and the write goes through, the fact named once."""
    from korely_agent.services import facts as agent_facts
    from korely_agent.services.facts import service as fsvc
    from tests.conftest import seeded_project_id

    old = _seeded(db, tenant)
    oid = parse_fact_id(old["id"])
    real = fsvc._persist_one

    def judged(dbs, *a, **k):
        fact, invalidated = real(dbs, *a, **k)
        closed = dbs.get(AgentFact, oid)
        closed.invalid_at, closed.invalidated_by_id = fact.valid_from, fact.id
        return fact, list(invalidated) + [oid]

    monkeypatch.setattr(fsvc, "_persist_one", judged)
    out = agent_facts.write_fact_direct(
        db, tenant.id, subject="LoCoMo", predicate="has_score", obj="86.9%",
        end_user_id="team", project_id=seeded_project_id(db, tenant.id), replaces=oid)
    assert out["invalidated"] == [old["id"]]


def test_a_deadlock_inside_an_mcp_tool_is_its_503(db, tenant, monkeypatch):
    """Mutant N8: the MCP's door answers a deadlock broken by Postgres as
    REST does, `unavailable`, never an internal error."""
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError
    from sqlalchemy.exc import OperationalError

    from korely_agent.mcp import server as mcp_server
    from korely_agent.services import facts as agent_facts

    old = _seeded(db, tenant)
    _r, key = key_service.create_api_key(db, tenant_id=tenant.id, name="k-mcp",
                                         scopes=["memories:read", "memories:write"])

    class Orig(Exception):
        pgcode = "40P01"

    def deadlocked(*a, **k):
        raise OperationalError("UPDATE agent_facts ...", {}, Orig("deadlock detected"))

    monkeypatch.setattr(agent_facts, "correct_fact", deadlocked)

    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        with pytest.raises(ToolError) as e:
            await mcp_server._get_agent_mcp().call_tool(
                "korely_correct_fact", {"fact_id": old["id"], "object": "86.9%"})
        return str(e.value)

    assert asyncio.run(go()).startswith("error: unavailable, ")


# ── the row landed on, compared with what the write left (the review, 26 Nov)
@pytest.mark.parametrize("door", ["replaces", "correction"])
def test_a_restated_fact_with_an_end_still_ahead_is_true_and_restated(db, tenant, door):
    """P11: the value written is a fact true now with an end ahead (forgotten
    from tomorrow); memory_core reconfirms it. It is not "closed meanwhile":
    the write goes through."""
    from datetime import datetime, timedelta, timezone

    from korely_agent.services import facts as agent_facts
    from tests.conftest import seeded_project_id

    project = seeded_project_id(db, tenant.id)
    x, y = _seeded(db, tenant, "A-value"), _seeded(db, tenant, "B-value")
    agent_facts.forget_fact(db, tenant.id, parse_fact_id(y["id"]), project_id=project,
                            at=datetime.now(timezone.utc) + timedelta(days=1))
    if door == "replaces":
        out = agent_facts.write_fact_direct(
            db, tenant.id, subject="LoCoMo", predicate="has_score", obj="B-value",
            end_user_id="team", project_id=project, replaces=parse_fact_id(x["id"]))
    else:
        out = agent_facts.correct_fact(db, tenant.id, parse_fact_id(x["id"]), obj="B-value",
                                       project_id=project, write_shape=True)
    assert out["id"] == y["id"] and out["invalidated"] == [x["id"]], out


def test_the_writes_own_judge_closing_the_new_fact_is_its_own(db, tenant, monkeypatch):
    """P12: this write's judge closes the new fact as history (it began
    before a later value). That closing is the write's own: it goes through."""
    from datetime import datetime, timedelta, timezone

    from korely_agent.services import facts as agent_facts
    from korely_agent.services.facts import service as fsvc
    from tests.conftest import seeded_project_id

    project = seeded_project_id(db, tenant.id)
    now = datetime.now(timezone.utc)
    x = agent_facts.write_fact_direct(
        db, tenant.id, subject="LoCoMo", predicate="has_score", obj="A-value",
        end_user_id="team", project_id=project, valid_from=now - timedelta(days=10))
    z = agent_facts.write_fact_direct(
        db, tenant.id, subject="LoCoMo", predicate="has_score", obj="C-value",
        end_user_id="team", project_id=project, valid_from=now - timedelta(days=2))
    real = fsvc._persist_one

    def judged(dbs, *a, **k):
        fact, invalidated = real(dbs, *a, **k)
        fact.invalid_at, fact.invalidated_by_id = now - timedelta(days=2), parse_fact_id(z["id"])
        dbs.flush()
        return fact, invalidated

    monkeypatch.setattr(fsvc, "_persist_one", judged)
    out = agent_facts.write_fact_direct(
        db, tenant.id, subject="LoCoMo", predicate="has_score", obj="B-value",
        end_user_id="team", project_id=project, valid_from=now - timedelta(days=5),
        replaces=parse_fact_id(x["id"]))
    assert out["invalidated"] == [x["id"]], out


def test_the_409_of_a_value_changed_meanwhile_on_patch_and_the_mcp(db, tenant, client, head,
                                                                    monkeypatch):
    """Mutant V6: PATCH and korely_correct_fact say the words of a value
    changed meanwhile, not "this fact is no longer current"."""
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError

    from korely_agent.mcp import server as mcp_server
    from korely_agent.services import facts as agent_facts

    old = _post(client, head, object="93.6%")

    def changed(*a, **k):
        raise agent_facts.RestatedFactClosed("fct_landed", None)

    monkeypatch.setattr(agent_facts, "correct_fact", changed)
    r = client.patch(f"/v1/facts/{old.json()['id']}", headers=head, json={"object": "86.9%"})
    assert r.status_code == 409 and r.json()["code"] == "fact_not_current", r.text
    words = "The value written is the fact fct_landed, which another write closed or changed"
    assert r.json()["message"].startswith(words)
    key = head["Authorization"].split(" ", 1)[1]

    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(key) is not None
        with pytest.raises(ToolError) as e:
            await mcp_server._get_agent_mcp().call_tool(
                "korely_correct_fact", {"fact_id": old.json()["id"], "object": "86.9%"})
        return str(e.value)

    # FastMCP puts "Error executing tool <name>: " before a tool's own refusal.
    assert "error: fact_not_current, " + words in asyncio.run(go())
