"""The re-check of 15 Nov 2026 (third review of memory_core 6.14.0), its
probes kept as tests: chains of forgets, posts, memories, erasures,
corrections and edits after which a forgotten fact must stay forgotten, or
a fact posted again must stay true, and a key confined to one end user that
reads none of another's. RC2b is the one the second form of the hold let
through: a forget, another memory that says the fact again, a forget of
that memory, an edit of the first."""
import pytest
from korely_agent.db.models import AgentFact
from korely_agent.services.ids import fact_public_id
from tests.deletion_world import Fact
from tests.test_deleting_a_memory_gives_back_what_it_closed import OLD, NEW, _facts, _paris, _delete
from tests.test_an_edit_does_not_undo_a_forget import _edit, _paris_memory, _forget, _objects, TYPO

pytest_plugins = ["tests.deletion_world"]


def rows(db, label="Paris"):
    db.expire_all()
    return [(fact_public_id(f.id)[-6:], f.object_label, "open" if f.invalid_at is None else "ended")
            for f in sorted(db.query(AgentFact), key=lambda f: f.created_at) if f.object_label == label]


def post(w, obj, **kw):
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in", "object": obj,
                                         "user_id": "u1", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_rc1_forget_judged_then_edit_closer_memory(world, db, monkeypatch):
    """Forget of a judged fact is no correction; and editing the closer memory away must not bring Paris back."""
    w = world
    zid = None
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(OLD)
    w.stale = ("Paris",)
    try:
        zid = w.write(NEW)
    finally:
        w.stale = ()
    (f,) = _paris(db)
    r = w.client.post(f"/v1/facts/{fact_public_id(f.id)}/forget")
    assert r.status_code == 200, r.text
    _edit(w, monkeypatch, zid, "I like cats.", [Fact("u1", "likes", "cats")])
    assert "Zanzibar" not in _objects(w), "judged-forget counted as correction"
    assert "Paris" not in _objects(w), "Paris back through given_back after a forget"
    # the same, closer edited but still stating Zanzibar
    _edit(w, monkeypatch, zid, "Zanzibar again.", [Fact("u1", "lives_in", "Zanzibar")])


def test_rc2_erasure_in_between(world, db, monkeypatch):
    """forget F; M3 states Paris; erase M3; edit M1."""
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)
    w.reads["Paris again."] = [Fact("u1", "lives_in", "Paris")]
    m3 = w.write("Paris again.")
    _delete(w, m3, True)
    _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])
    assert "Paris" not in _objects(w), "RC2 erasure in between lost the forget"


def test_rc2b_forget_memory_in_between(world, db, monkeypatch):
    """forget F; M3 states Paris; FORGET (soft) M3 (record not by hand); edit M1."""
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)
    w.reads["Paris again."] = [Fact("u1", "lives_in", "Paris")]
    m3 = w.write("Paris again.")
    _delete(w, m3, False)
    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])
    assert "Paris" not in _objects(w), "RC2b forget of a restating memory lifted the hold"
    assert out["kept_forgotten"] == [fact_public_id(f.id)], "the caller's forget is the last word"


def test_rc3_correction_of_a_correction(world, db, monkeypatch):
    w = world
    mid, f = _paris_memory(w)
    r = w.client.patch(f"/v1/facts/{fact_public_id(f.id)}", json={"object": "Lyon"})
    assert r.status_code == 200, r.text
    lid = r.json().get("id") or r.json()["fact"]["id"]
    r = w.client.patch(f"/v1/facts/{lid}", json={"object": "Nice"})
    assert r.status_code == 200, r.text
    w.stale = ("Nice",)
    try:
        _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])
    finally:
        w.stale = ()
    assert _objects(w) == ["Nice"]
    _edit(w, monkeypatch, mid, "I like cats.", [Fact("u1", "likes", "cats")])
    _edit(w, monkeypatch, mid, "I live in Lyon.", [Fact("u1", "lives_in", "Lyon")])
    assert "Lyon" not in _objects(w)


@pytest.mark.parametrize("variant", ["case", "spaces", "predicate_alias", "old_valid_from", "tense_current"])
def test_rc4_post_after_forget_lifts_hold(world, db, monkeypatch, variant):
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)
    extra = {}
    obj = "Paris"
    if variant == "case":
        obj = "paris"
    if variant == "spaces":
        obj = "  Paris "
    if variant == "old_valid_from":
        extra = {"valid_from": "2000-01-01"}
    body = {"subject": "u1", "predicate": "lives_in", "object": obj, "user_id": "u1", **extra}
    if variant == "predicate_alias":
        body["predicate"] = "lives in"
    r = w.client.post("/v1/facts", json=body)
    assert r.status_code == 201
    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])
    assert "Paris" in [o for o in _objects(w)] or "paris" in [o.lower() for o in _objects(w)]
    assert out["kept_forgotten"] == [], "RC4 hold wrongly kept"


def test_rc4b_correct_back(world, db, monkeypatch):
    """Paris -> Lyon -> back to Paris by hand, then edit."""
    w = world
    mid, f = _paris_memory(w)
    r = w.client.patch(f"/v1/facts/{fact_public_id(f.id)}", json={"object": "Lyon"})
    lid = r.json().get("id") or r.json()["fact"]["id"]
    r = w.client.patch(f"/v1/facts/{lid}", json={"object": "Paris"})
    _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])
    assert _objects(w) == ["Paris"], "RC4b"


def test_rc5_two_memories_interleaved(world, db, engine, monkeypatch):
    """M2 is edited by a second app with its own session while M1's text is
    being extracted (the hook): both state Paris, which was forgotten."""
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker
    from korely_agent.db import get_db
    from korely_agent.main import create_app
    w = world
    mid1, f = _paris_memory(w)
    w.reads["Paris is home."] = [Fact("u1", "lives_in", "Paris")]
    mid2 = w.write("Paris is home.")
    _forget(w, f)
    w.reads["Paris is home!"] = [Fact("u1", "lives_in", "Paris")]
    seen = {}

    def hook():
        s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
        app2 = create_app()
        app2.dependency_overrides[get_db] = lambda: s
        with TestClient(app2) as c2:
            c2.headers.update(w.client.headers)
            r = c2.patch(f"/v1/memories/{mid2}", json={"content": "Paris is home!"})
            seen["r"] = (r.status_code, r.json().get("kept_forgotten") if r.status_code == 200 else r.text[:200])
        s.close()

    w.hooks["extraction"] = hook
    _edit(w, monkeypatch, mid1, TYPO, [Fact("u1", "lives_in", "Paris")])
    assert "Paris" not in _objects(w)


def test_rc6_confined_key(world, db, monkeypatch):
    from korely_agent.services import keys as key_service
    w = world
    mid, f = _paris_memory(w)           # u1
    w.reads["I live in Paris, says u2."] = [Fact("u2", "lives_in", "Paris")]
    m2 = w.write("I live in Paris, says u2.", user="u2")
    db.expire_all()
    f2 = [x for x in db.query(AgentFact) if x.subject_label == "u2" or getattr(x, "end_user_id", None) == "u2"]
    _forget(w, f)
    for x in f2:
        r = w.client.post(f"/v1/facts/{fact_public_id(x.id)}/forget")
    _r, key = key_service.create_api_key(db, tenant_id=w.tenant.id, name="c1", end_users=["u1"])
    w.client.headers["Authorization"] = f"Bearer {key}"
    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])
    assert all(fact_public_id(x.id) not in out["kept_forgotten"] for x in f2)
    r = w.client.patch(f"/v1/memories/{m2}", json={"content": "I live in Paris!!"})
    assert r.status_code in (403, 404)


# ── the order of the caller's words (the fourth review of 15 Nov 2026) ────

def test_posted_forgotten_and_posted_again_stays_true(world, db, monkeypatch):
    """Paris posted by hand, forgotten, posted by hand again, then the
    memory edited: the second posting stands, Paris stays true, and nothing
    is kept forgotten."""
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)
    first = post(w, "Paris")["id"]
    r = w.client.post(f"/v1/facts/{first}/forget")
    assert r.status_code == 200, r.text
    second = post(w, "Paris")["id"]
    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])
    assert "Paris" in _objects(w)
    assert out["kept_forgotten"] == []
    assert [x["id"] for x in _facts(w)] == [second]


def test_a_forget_between_two_postings_is_not_the_last_word(world, db, monkeypatch):
    """The forget sits between two postings by hand, and the second posting
    then ends by a newer value (the judge, not the caller): nothing of Paris
    is true now, and the caller's last word is the second posting, later
    than the forget, so the edit writes Paris again as an edit writes a fact
    a newer value closed. Read with the first posting (`min` in place of
    `max`), the forget would be the later word and Paris held."""
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)
    first = post(w, "Paris")["id"]
    r = w.client.post(f"/v1/facts/{first}/forget")
    assert r.status_code == 200, r.text
    post(w, "Paris")
    w.reads[NEW] = [Fact("u1", "lives_in", "Zanzibar")]
    w.stale = ("Paris",)
    try:
        w.write(NEW)
    finally:
        w.stale = ()
    assert "Paris" not in _objects(w), "the newer value closed the second posting"
    before = len(_paris(db))

    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])

    assert out["kept_forgotten"] == []
    assert len(_paris(db)) == before + 1, "the statement is written again, not held"
