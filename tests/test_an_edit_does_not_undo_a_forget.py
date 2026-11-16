"""An edit of a memory's text does not make true again a fact of that
memory the caller said is over by hand (memory_core 6.14.0; the founder's
decision of 15 Nov 2026: "a forget is the person's will, and an edit of the
text does not undo it").

"I live in Paris." gives F; the caller forgets F (POST
/v1/facts/{id}/forget), corrects it (PATCH /v1/facts/{id}) or posts it in
the past tense (POST /v1/facts); then the memory's text is edited ("I live
in Paris!", a typo fixed). Until 1.0.26 the edit extracted the text again
and wrote "lives in Paris" as a new open row: the fact was true again (the
skeptic's attack on the forget, 9 Nov 2026). Now the statement of a row of
the memory the caller said is over is not written (memory_core
`writing.write_fact(said_over=)`, given by `given_back.said_over_by_hand`),
the row stays as it was, and the answer of the edit names it in
`kept_forgotten`, with the extraction inline or after the answer
(WRITE_ASYNC). The caller makes it true again by posting it (POST
/v1/facts), and that row is the last word.

No model is called: the writer and the judge are the scripted ones of
tests/deletion_world.py.
"""
import pytest

from korely_agent.db.models import AgentFact
from korely_agent.services.ids import fact_public_id
from tests.deletion_world import Fact
from tests.test_deleting_a_memory_gives_back_what_it_closed import OLD, _facts, _paris

# the `world` fixture
pytest_plugins = ["tests.deletion_world"]

TYPO = "I live in Paris!"
CATS = "I like cats."
BOTH = "I live in Paris! I like cats."

#: The two ways the edit's text is extracted: in the request (WRITE_ASYNC
#: off, the world's default) or by the worker after the answer (on, the
#: product's default).
MODES = ["inline", "worker"]


def _queued(monkeypatch, on):
    from korely_agent.config import get_settings

    monkeypatch.setenv("WRITE_ASYNC", "true" if on else "false")
    get_settings.cache_clear()


def _edit(w, monkeypatch, mid, content, facts, mode="inline"):
    """PATCH /v1/memories/{mid} to `content`, read as `facts`; its answer,
    once the facts are written (the worker drained when `mode` is
    'worker')."""
    w.reads[content] = facts
    if mode == "inline":
        r = w.client.patch(f"/v1/memories/{mid}", json={"content": content})
        assert r.status_code == 200, r.text
        return r.json()
    _queued(monkeypatch, True)
    try:
        r = w.client.patch(f"/v1/memories/{mid}", json={"content": content})
        assert r.status_code == 200, r.text
        w.drain()
    finally:
        _queued(monkeypatch, False)
    return r.json()


def _paris_memory(w):
    """OLD written, read as "u1 lives_in Paris"; (its id, the fact)."""
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    mid = w.write(OLD)
    (f,) = _paris(w.db)
    return mid, f


def _forget(w, f):
    r = w.client.post(f"/v1/facts/{fact_public_id(f.id)}/forget")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "forgotten"


def _objects(w):
    return sorted(x["object"] for x in _facts(w))


def _as_it_was(db, f):
    db.expire_all()
    g = db.get(AgentFact, f.id)
    return (g.invalid_at, g.invalidated_by_id, int(g.observation_count or 1), g.last_confirmed_at,
            sorted(g.source_memory_ids or []), g.tense)


# ── the finding ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", MODES)
def test_fixing_a_typo_does_not_bring_back_a_forgotten_fact(world, db, monkeypatch, mode):
    """The finding of 9 Nov 2026: the forget stands, the row is left as it
    was (not reconfirmed, no new row of Paris), and the answer names it,
    also when the worker extracts after the answer."""
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)
    before = _as_it_was(db, f)

    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")], mode)

    assert "Paris" not in _objects(w), "a forget undone by an edit of the text"
    assert len(_paris(db)) == 1, "no new row of the forgotten fact"
    assert _as_it_was(db, f) == before, "the forgotten row stays as it was"
    assert out["kept_forgotten"] == [fact_public_id(f.id)]
    assert fact_public_id(f.id) not in [x["id"] for x in out["facts"]]


def test_the_rest_of_the_new_text_is_written_as_always(world, db, monkeypatch):
    """Only the forgotten fact is held: what else the new text says is
    written and answered as before."""
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)

    out = _edit(w, monkeypatch, mid, BOTH,
                [Fact("u1", "lives_in", "Paris"), Fact("u1", "likes", "cats")])

    assert _objects(w) == ["cats"]
    assert [x["object"] for x in out["facts"]] == ["cats"]
    assert out["kept_forgotten"] == [fact_public_id(f.id)]


def test_an_edit_that_leaves_it_out_and_one_that_says_it_again(world, db, monkeypatch):
    """The forget outlives an edit that no longer states the fact: the text
    written back says Paris, and Paris stays over."""
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)

    first = _edit(w, monkeypatch, mid, CATS, [Fact("u1", "likes", "cats")])
    assert first["kept_forgotten"] == [fact_public_id(f.id)]
    again = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])

    assert "Paris" not in _objects(w)
    assert len(_paris(db)) == 1
    assert again["kept_forgotten"] == [fact_public_id(f.id)]


# ── every word of the caller that a fact is over ────────────────────────────

def test_a_corrected_fact_stays_corrected_through_an_edit(world, db, monkeypatch):
    """Corrected by hand (Paris -> Lyon), then the text edited to "I live in
    Paris!": Paris stays over and Lyon stays true (the decision of 15 Nov
    2026: a correction is the person's will, as a forget is). The judge,
    asked, would close Lyon: it is not asked, nothing of Paris is written.
    Until 1.0.26 Paris came back and Lyon ended; with the first form of
    6.14.0 Paris stayed over and the edit retired Lyon, which keeps the
    memory as its source (given_back.standing_corrections)."""
    w = world
    mid, f = _paris_memory(w)
    r = w.client.patch(f"/v1/facts/{fact_public_id(f.id)}", json={"object": "Lyon"})
    assert r.status_code == 200, r.text

    w.stale = ("Lyon",)
    try:
        out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])
    finally:
        w.stale = ()

    assert _objects(w) == ["Lyon"]
    assert len(_paris(db)) == 1
    assert out["kept_forgotten"] == [fact_public_id(f.id)]


def test_a_forget_of_a_judged_fact_makes_no_correction(world, db, monkeypatch):
    """A forget of a fact the judge had closed (it points at Zanzibar) is no
    correction: Zanzibar's memory edited to say Rome retires Zanzibar as
    before. Only a correction, its record written with the closing, stands
    through an edit (facts/changes.a_correction)."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads["Now I live in Zanzibar."] = [Fact("u1", "lives_in", "Zanzibar")]
    w.write(OLD)
    w.stale = ("Paris",)
    try:
        zid = w.write("Now I live in Zanzibar.")
    finally:
        w.stale = ()
    (f,) = _paris(db)
    r = w.client.post(f"/v1/facts/{fact_public_id(f.id)}/forget")
    assert r.status_code == 200 and r.json()["status"] == "already_forgotten", r.text

    _edit(w, monkeypatch, zid, "I like cats.", [Fact("u1", "likes", "cats")])

    assert "Zanzibar" not in _objects(w)


def test_a_fact_posted_in_the_past_by_hand_stays_over(world, db, monkeypatch):
    """POST /v1/facts with tense 'past' closes F in place, the caller's word
    (scope 'past'): an edit that says Paris in the present writes nothing."""
    w = world
    mid, f = _paris_memory(w)
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in",
                                         "object": "Paris", "user_id": "u1", "tense": "past"})
    assert r.status_code == 201, r.text
    db.expire_all()
    assert db.get(AgentFact, f.id).invalid_at is not None

    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])

    assert "Paris" not in _objects(w)
    assert out["kept_forgotten"] == [fact_public_id(f.id)]


# ── what the rule leaves as it was ──────────────────────────────────────────

def test_posting_the_fact_again_makes_it_true_and_lifts_the_hold(world, db, monkeypatch):
    """The way back: the caller posts the fact (POST /v1/facts), a new row
    that is the last word on it. The edit then restates that row, as any
    edit restates a true fact, and nothing is kept forgotten."""
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in",
                                         "object": "Paris", "user_id": "u1"})
    assert r.status_code == 201, r.text
    assert "Paris" in _objects(w)

    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])

    assert "Paris" in _objects(w)
    assert out["kept_forgotten"] == []
    (old, posted) = _paris(db)
    assert old.id == f.id and posted.invalid_at is None
    # The edit restates the posted row: the memory joins its sources (a
    # fact posted by hand has none, so its count of sources stays 1).
    assert len(posted.source_memory_ids or []) == 1, "the edit restates the posted row"


def test_a_fact_the_judge_closed_is_written_again_as_before(world, db, monkeypatch):
    """Only the caller's word holds a fact: Paris closed by the judge (a
    later memory says Zanzibar) is written again by the edit, a new row
    judged as any, as before 6.14.0."""
    w = world
    w.reads[OLD] = [Fact("u1", "lives_in", "Paris")]
    w.reads["Now I live in Zanzibar."] = [Fact("u1", "lives_in", "Zanzibar")]
    mid = w.write(OLD)
    w.stale = ("Paris",)
    try:
        w.write("Now I live in Zanzibar.")
    finally:
        w.stale = ()
    (f,) = _paris(db)
    assert f.invalid_at is not None

    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])

    assert out["kept_forgotten"] == []
    assert len(_paris(db)) == 2, "the edit writes the statement again, as before"


def test_a_memory_that_never_stated_it_states_it_as_a_new_memory_would(world, db, monkeypatch):
    """The hold is on the memory's own facts: another memory edited to say
    Paris for the first time states it as a new memory would (POST
    /v1/memories writes a forgotten fact again, as before)."""
    w = world
    _mid, f = _paris_memory(w)
    _forget(w, f)
    w.reads[CATS] = [Fact("u1", "likes", "cats")]
    other = w.write(CATS)

    out = _edit(w, monkeypatch, other, TYPO, [Fact("u1", "lives_in", "Paris")])

    assert out["kept_forgotten"] == []
    assert "Paris" in _objects(w)


def test_a_correction_stays_when_the_new_text_states_it(world, db, monkeypatch):
    """An edit whose text states the corrected value restates the
    correction, which stays true, and the old value stays over."""
    w = world
    mid, f = _paris_memory(w)
    r = w.client.patch(f"/v1/facts/{fact_public_id(f.id)}", json={"object": "Lyon"})
    assert r.status_code == 200, r.text

    out = _edit(w, monkeypatch, mid, "I live in Lyon.", [Fact("u1", "lives_in", "Lyon")])

    assert _objects(w) == ["Lyon"]
    assert out["kept_forgotten"] == [fact_public_id(f.id)]



# ── the last word on the triple, whoever wrote its row (the reviews of 15 Nov) ─

def _post_paris(w):
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in",
                                         "object": "Paris", "user_id": "u1"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _forget_public(w, fid):
    r = w.client.post(f"/v1/facts/{fid}/forget")
    assert r.status_code == 200, r.text


def test_forgotten_posted_again_and_forgotten_again_stays_over(world, db, monkeypatch):
    """Forget Paris, post it again by hand, forget that row too, then fix a
    typo in the memory: the last word is the second forget, of a row the
    memory never stated, and Paris stays over (the skeptic's probe P1a: the
    first form of 6.14.0 read only a last row of the same memory)."""
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)
    posted = _post_paris(w)
    _forget_public(w, posted)

    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])

    assert "Paris" not in _objects(w)
    assert len(_paris(db)) == 2, "no new row"
    assert out["kept_forgotten"] == [posted]


def test_forgotten_restated_by_another_memory_and_forgotten_again_stays_over(world, db,
                                                                           monkeypatch):
    """Forget Paris; another memory says it (a new row, as a new memory
    does); forget that row; fix a typo in the first memory: Paris stays over
    (the skeptic's probe P1b)."""
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)
    w.reads["Paris is where I live."] = [Fact("u1", "lives_in", "Paris")]
    w.write("Paris is where I live.")
    (again,) = [r for r in _paris(db) if r.invalid_at is None]
    _forget(w, again)

    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])

    assert "Paris" not in _objects(w)
    assert len(_paris(db)) == 2, "no new row"
    assert out["kept_forgotten"] == [fact_public_id(again.id)]


def test_a_forget_committed_during_the_extraction_is_seen(world, db, engine, monkeypatch):
    """A forget committed from another session while the edit's text is
    being extracted: the write reads the rows as committed, and Paris stays
    over (the skeptic's probe P5: the row was read from the session, open)."""
    from sqlalchemy.orm import sessionmaker

    from korely_agent.services.facts import service

    w = world
    mid, f = _paris_memory(w)
    fid, project = f.id, f.project_id

    def forget_elsewhere():
        other = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
        try:
            service.forget_fact(other, w.tenant.id, fid, project_id=project)
            other.commit()
        finally:
            other.close()

    w.hooks["extraction"] = forget_elsewhere
    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])

    assert "Paris" not in _objects(w)
    assert len(_paris(db)) == 1
    assert out["kept_forgotten"] == [fact_public_id(f.id)]


# ── what the rule leaves alone (one test per guard of memory_core 6.14.0) ──

def test_a_plan_that_comes_true_is_written_as_before(world, db, monkeypatch):
    """Nothing is held while something of the triple is true now: a plan of
    Paris, written before the forgotten fact and begun since, comes true at
    the edit as a plan does (memory_core 3.0.0), closed by the done fact,
    which is written and true. The guard `not came_true` of step 2-bis."""
    from datetime import datetime, timedelta, timezone

    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)
    db.expire_all()
    f = db.get(AgentFact, f.id)
    plan = AgentFact(tenant_id=f.tenant_id, project_id=f.project_id, end_user_id=f.end_user_id,
                     agent_id=f.agent_id, subject_label=f.subject_label,
                     subject_canonical=f.subject_canonical, subject_type=f.subject_type,
                     predicate_raw=f.predicate_raw, predicate_canonical=f.predicate_canonical,
                     object_label=f.object_label, object_canonical=f.object_canonical,
                     object_is_literal=f.object_is_literal, confidence=0.9, tense="planned",
                     valid_from=datetime.now(timezone.utc) - timedelta(days=1),
                     created_at=f.created_at - timedelta(seconds=1))
    db.add(plan)
    db.commit()

    out = _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])

    db.expire_all()
    assert db.get(AgentFact, plan.id).invalid_at is not None, "the plan came true"
    done = [r for r in _paris(db) if r.invalid_at is None]
    assert len(done) == 1 and done[0].tense == "current"
    assert db.get(AgentFact, plan.id).invalidated_by_id == done[0].id
    assert out["kept_forgotten"] == []


def test_a_past_statement_of_a_forgotten_fact_is_judged_as_before(world, db, monkeypatch):
    """A past statement is not held: "I no longer live in Paris" lands on the
    forgotten row's history and its judgement runs as before, closing what
    the judge closes about the same two entities. The guard `tense !=
    "past"` of step 2-bis."""
    w = world
    mid, f = _paris_memory(w)
    w.reads["I love Paris."] = [Fact("u1", "loves", "Paris")]
    w.write("I love Paris.")
    _forget(w, f)

    w.stale = ("loves",)
    try:
        _edit(w, monkeypatch, mid, "I no longer live in Paris.",
              [Fact("u1", "lives_in", "Paris", tense="past")])
    finally:
        w.stale = ()

    db.expire_all()
    loves = [r for r in db.query(AgentFact) if r.predicate_canonical == "loves"]
    assert loves and all(r.invalid_at is not None for r in loves), \
        "the past statement was judged, as before"


def test_a_held_row_of_a_session_stays_a_sessions(world, db, monkeypatch):
    """The held row comes back as it was: Paris's last row, said by a work
    session (`kind: session`) and forgotten, stays a session's fact when a
    note that stated Paris is edited (the guard on `stated_by_a_note` in
    `_persist_extracted`)."""
    w = world
    mid, f = _paris_memory(w)
    _forget(w, f)
    w.reads["Session: Paris noted."] = [Fact("u1", "lives_in", "Paris")]
    w.write("Session: Paris noted.", kind="session")
    (sess,) = [r for r in _paris(db) if r.invalid_at is None]
    assert sess.session_only
    _forget(w, sess)

    _edit(w, monkeypatch, mid, TYPO, [Fact("u1", "lives_in", "Paris")])

    db.expire_all()
    assert db.get(AgentFact, sess.id).session_only, "the held row was stated again"


def test_a_held_row_is_never_pointed_at_a_successor(world, db, monkeypatch):
    """Posted in the past tense by hand, Paris is closed in place with no
    pointer. The edit says Paris again and Rome: Paris is held, Rome is new,
    and the write's pointer of a past fact to its successor
    (memory_core `worker._point_to_the_successor`) leaves the held row
    alone."""
    w = world
    mid, f = _paris_memory(w)
    r = w.client.post("/v1/facts", json={"subject": "u1", "predicate": "lives_in",
                                         "object": "Paris", "user_id": "u1", "tense": "past"})
    assert r.status_code == 201, r.text
    db.expire_all()
    before = db.get(AgentFact, f.id)
    assert before.invalid_at is not None and before.invalidated_by_id is None

    _edit(w, monkeypatch, mid, "I live in Paris! And in Rome.",
          [Fact("u1", "lives_in", "Paris"), Fact("u1", "lives_in", "Rome")])

    db.expire_all()
    assert db.get(AgentFact, f.id).invalidated_by_id is None
    assert "Rome" in _objects(w) and "Paris" not in _objects(w)
