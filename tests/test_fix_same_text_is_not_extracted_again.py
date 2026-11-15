"""A PATCH with the text the memory already has is not an edit (E2 of the
engine list, 4 Nov 2026; Notion row, in English: "a PATCH whose extraction
answers no fact closes the facts only that memory stated").

It queued the extraction again, and a second read that answered no fact
closed every fact the memory stated (`worker.write_memory(retire=)`), though
nothing had changed: a model's blackout cost the facts, in silence. Now the
same text, already extracted, writes nothing, calls no model and closes
nothing. A text that changed is read again and retires what it no longer
states (tests/test_deletion_does_not_come_back.py, "edit_away").
"""
from korely_agent.db.models import AgentFact
from tests.deletion_world import Fact

pytest_plugins = ["tests.deletion_world"]

TEXT = "Anna works in Milan and has two children."


def open_objects(db):
    db.expire_all()
    return {f.object_label for f in db.query(AgentFact) if f.invalid_at is None}


def test_the_same_text_keeps_the_facts_and_calls_no_model(world, db, tenant):
    w = world
    w.reads[TEXT] = [Fact("u1", "works_in", "Milan"), Fact("u1", "has", "two children")]
    mid = w.write(TEXT)
    assert open_objects(db) == {"Milan", "two children"}

    w.reads[TEXT] = []                      # the model's blackout at a second read
    calls = len(w.prompts)
    r = w.client.patch(f"/v1/memories/{mid}", json={"content": TEXT})
    w.drain()

    assert r.status_code == 200, r.text
    assert open_objects(db) == {"Milan", "two children"}
    assert len(w.prompts) == calls          # no extraction, no judge, no vocabulary


def test_a_text_that_changed_still_retires_what_it_no_longer_states(world, db, tenant):
    w = world
    w.reads[TEXT] = [Fact("u1", "works_in", "Milan"), Fact("u1", "has", "two children")]
    mid = w.write(TEXT)
    new = "Nothing to say any more."
    w.reads[new] = []
    r = w.client.patch(f"/v1/memories/{mid}", json={"content": new})
    w.drain()
    assert r.status_code == 200, r.text
    assert open_objects(db) == set()
