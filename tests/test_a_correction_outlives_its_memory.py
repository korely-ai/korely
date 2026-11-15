"""A correction stays when the memory of the fact it corrected is edited or
forgotten, and goes when that memory is erased (code review finding, P2,
15 Nov 2026, and its review).

"Sofia works at Acme Corp" is read from a memory; the caller corrects the
fact to Beacon Labs (PATCH /v1/facts/{id}). Until 1.0.26 the corrected fact
was written with the memory's id as its source (services/facts.correct_fact,
`source_memory_id=old.source_memory_id`), so every rule that asks "which
facts did this memory say?" (memory_core `store.source_condition`) counted
the correction among them: an edit of the memory retired it
(`store.retire_the_previous_version`), a forget closed it
(facts/erasure._forget) and an erasure deleted it. Acme stayed closed by
the correction, so after any of the three nothing said where Sofia works.

A correction is the caller's statement, as a fact written directly is
(`write_fact_direct`, no source memory): it is written with none, and the
memory's later life touches only what the memory said. Graphiti removes
with an episode only the edges that episode created
(`Graphiti.remove_episode`: "We should only delete edges created by the
episode"), and an edge added by hand (`add_triplet`) is created by none.

But the correction is made of the corrected fact's words (its subject, and
its relation or object when the caller gave only one of them), so its first
writer stays the memory, as the database recorded it up to 1.0.26
(`first_memory_id`, db/triggers.py), and the memory's erasure deletes it as
it did: what an erasure took up to 1.0.26 it still takes (the review's
privacy finding: an erasure never takes less than it did).
"""
import pytest

from korely_agent.db.models import AgentFact
from korely_agent.services.ids import fact_public_id
from tests.deletion_world import Fact
from tests.test_deleting_a_memory_gives_back_what_it_closed import _delete, _open

# the `world` fixture
pytest_plugins = ["tests.deletion_world"]

SAID = "Sofia works at Acme Corp."
EDITED = "Sofia works at Acme Corp. She likes green tea."
ACME = Fact("Sofia", "works_at", "Acme Corp")
TEA = Fact("Sofia", "likes", "green tea")

#: What the caller sends: the object only (the subject stays the
#: extraction's, memory_core 6.6.0), or the subject too (the caller's).
CORRECTIONS = {"object only": {"object": "Beacon Labs"},
               "subject and object": {"subject": "Sofia", "object": "Beacon Labs"}}


def _row(db, obj):
    db.expire_all()
    return [f for f in db.query(AgentFact) if f.object_label == obj]


def _corrected(w, db, body):
    """Acme read from SAID, then corrected by `body`; returns the memory's
    id, the corrected row and the correction's row."""
    w.reads[SAID] = [ACME]
    mid = w.write(SAID)
    [acme] = _row(db, "Acme Corp")
    r = w.client.patch(f"/v1/facts/{fact_public_id(acme.id)}", json=body)
    assert r.status_code == 200, r.text
    [beacon] = _row(db, "Beacon Labs")
    return mid, acme, beacon


@pytest.mark.parametrize("body", CORRECTIONS.values(), ids=CORRECTIONS.keys())
def test_forgetting_the_memory_leaves_the_correction(world, db, body):
    mid, acme, beacon = _corrected(world, db, body)
    _delete(world, mid, False)
    assert "Beacon Labs" in _open(db), "the memory's forget undid the correction"
    assert "Acme Corp" not in _open(db)


@pytest.mark.parametrize("body", CORRECTIONS.values(), ids=CORRECTIONS.keys())
@pytest.mark.parametrize("forgotten_first", [False, True], ids=["live", "forgotten first"])
def test_erasing_the_memory_erases_the_correction(world, db, body, forgotten_first):
    """As up to 1.0.26: the corrected fact was the memory's alone, and the
    correction is made of its words; both go, and nothing is given back."""
    mid, acme, beacon = _corrected(world, db, body)
    if forgotten_first:
        _delete(world, mid, False)
    out = _delete(world, mid, True)
    assert _row(db, "Beacon Labs") == [], "the erasure kept a row made of the memory's words"
    assert _row(db, "Acme Corp") == []
    assert out["facts_deleted"] == 2 and out["facts_reopened"] == 0, out


def test_a_correction_of_a_correction_goes_with_the_same_erasure(world, db):
    mid, _acme, beacon = _corrected(world, db, CORRECTIONS["object only"])
    r = world.client.patch(f"/v1/facts/{fact_public_id(beacon.id)}", json={"object": "Delta Inc"})
    assert r.status_code == 200, r.text
    _delete(world, mid, False)
    assert "Delta Inc" in _open(db)
    _delete(world, mid, True)
    assert _row(db, "Delta Inc") == [] and _row(db, "Beacon Labs") == []


def test_a_correction_given_back_still_goes_with_the_erasure(world, db):
    """The correction closed by a later memory, which is then forgotten: the
    correction comes back as a new row (facts/given_back.py), still made of
    the first memory's words, and that memory's erasure takes it."""
    w = world
    mid, acme, beacon = _corrected(w, db, CORRECTIONS["object only"])
    delta = "Sofia works at Delta Inc now."
    w.reads[delta] = [Fact("Sofia", "works_at", "Delta Inc")]
    w.stale = ("Beacon Labs",)
    other = w.write(delta)
    assert "Beacon Labs" not in _open(db), "the scripted judge did not close the correction"
    w.stale = ()
    _delete(w, other, False)
    [back] = [f for f in _row(db, "Beacon Labs") if f.invalid_at is None]
    assert back.id != beacon.id and back.source_memory_id is None
    assert back.first_memory_id == acme.source_memory_id, "given back without its first writer"
    _delete(w, mid, True)
    assert [f for f in _row(db, "Beacon Labs")] == []


def test_a_correction_another_memory_states_stays_after_the_erasure(world, db):
    """A memory that says the corrected value restates the correction (memory_core
    `store.reconfirm`): it is that memory's too, and stays, as every fact
    other memories still assert."""
    w = world
    mid, _acme, beacon = _corrected(w, db, CORRECTIONS["object only"])
    again = "Sofia works at Beacon Labs."
    w.reads[again] = [Fact("Sofia", "works_at", "Beacon Labs")]
    w.write(again)
    [restated] = _row(db, "Beacon Labs")
    assert restated.id == beacon.id and restated.source_memory_ids, "not restated"
    _delete(w, mid, True)
    [kept] = _row(db, "Beacon Labs")
    assert kept.id == beacon.id and kept.invalid_at is None, "the erasure took another memory's fact"


#: The memory's new text: the old value said again with a sentence more, or
#: gone from it.
EDITS = {"old value said again": (EDITED, [ACME, TEA]),
         "old value gone": ("Sofia likes green tea.", [TEA])}


@pytest.mark.parametrize("edit", EDITS.values(), ids=EDITS.keys())
@pytest.mark.parametrize("body", CORRECTIONS.values(), ids=CORRECTIONS.keys())
def test_an_edit_of_the_memory_leaves_the_correction(world, db, body, edit):
    w = world
    text, reads = edit
    mid, _acme, beacon = _corrected(w, db, body)
    w.reads[text] = reads
    r = w.client.patch(f"/v1/memories/{mid}", json={"content": text})
    assert r.status_code == 200, r.text
    [after] = _row(db, "Beacon Labs")
    assert after.id == beacon.id and after.invalid_at is None, \
        "the edit retired the correction as a fact of the memory"
    assert "green tea" in _open(db)
    # A new text that still says Acme Corp writes it again as a new row of
    # the memory, which the judge weighs against the correction like any new
    # fact; this scripted judge closes nothing. One that closes it: the next
    # test.


def test_an_edit_that_says_the_old_value_again_can_close_the_correction(world, db):
    """The honest limit (docs/guide/limits.md): an edited text is extracted
    whole and its facts are dated at the edit (memory_core `worker.arrival`),
    so the old value said again is newer than the correction, and a judge
    that reads the relation as one value at a time closes the correction.
    The engine's rule, not changed here; this pins what the docs say."""
    w = world
    mid, _acme, beacon = _corrected(w, db, CORRECTIONS["object only"])
    w.reads[EDITED] = [ACME, TEA]
    w.stale = ("Beacon Labs",)
    r = w.client.patch(f"/v1/memories/{mid}", json={"content": EDITED})
    assert r.status_code == 200, r.text
    assert "Acme Corp" in _open(db) and "Beacon Labs" not in _open(db)


@pytest.mark.parametrize("body", CORRECTIONS.values(), ids=CORRECTIONS.keys())
def test_the_correction_names_no_memory(world, db, body):
    """The correction's row has no source memory, as a fact posted by hand,
    keeps the corrected fact's memory as its first writer, and says whose
    words it carries by `model_used`: "correction" when the
    caller named the subject, the extraction's model when it inherited the
    extraction's subject (memory_core 6.6.0, unchanged). The corrected row
    keeps its memory and points at the correction."""
    _mid, acme, beacon = _corrected(world, db, body)
    assert beacon.source_memory_id is None and not beacon.source_memory_ids
    # Its first writer: the memory of the corrected fact, whose erasure takes it.
    assert beacon.first_memory_id == acme.source_memory_id is not None
    assert beacon.model_used == ("correction" if "subject" in body else acme.model_used)
    db.expire_all()
    old = db.get(AgentFact, acme.id)
    assert old.source_memory_id is not None and old.invalidated_by_id == beacon.id
    [shown] = [f for f in world.client.get("/v1/facts", params={"user_id": "u1"}).json()["facts"]
               if f["object"] == "Beacon Labs"]
    assert shown["source_memory_id"] is None and shown["source_memory_ids"] == []
