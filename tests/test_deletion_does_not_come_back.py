"""What a user deleted does not come back at the next write.

Backs the claim against mem0 issue #7452 (mem0ai/mem0): after
`delete_all()` / `delete(id)` the deleted value came back 19 times of 24,
because the library kept recent raw messages and sent them to the model again
on the next `add()`. The model then wrote the deleted value as a new memory.

Here a memory's extraction reads the same kind of context (the end user's
earlier memories, `facts.service.previous_memories`), and the judge and the
vocabulary prompts read facts and examples. So the question is the same:
after a deletion, does any later model call still SEE the deleted words, and
does any fact derived from them still exist?

The scripted writer (tests/deletion_world.py) records every prompt of every
model call. 6 deletion paths x 8 phrasings = 48 cases (mem0's 24, doubled):

- forget one memory          DELETE /v1/memories/{id}
- erase one memory           DELETE /v1/memories/{id}?erase=true
- erase the end user         DELETE /v1/users/{id}/memories
- delete the agent namespace DELETE /v1/agents/{id}
- erase a project            services.projects.delete_project(erase=True)
- edit the text away         PATCH  /v1/memories/{id}

After the deletion the same user writes new memories (one on the same
relation, one on another, one left to the drain), and no prompt written
from then on may hold the deleted words. A guard first proves the harness
can see a leak: before the deletion, the words ARE in the prompts of the
memory written after them.
"""
import pytest

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import projects as project_service
from tests.deletion_world import Fact, dump_rows_with

# the `world` fixture
pytest_plugins = ["tests.deletion_world"]

# (text, predicate, object). The object is the marker looked for everywhere.
PHRASINGS = [
    ("I am allergic to jackfruit.", "allergic_to", "jackfruit"),
    ("My home address is 12 Quillon Street.", "lives_at", "12 Quillon Street"),
    ("I take methotrexate every week.", "takes", "methotrexate"),
    ("My employer is Zorblax Industries.", "works_at", "Zorblax Industries"),
    ("My turtle is called Pemberton.", "has_pet", "Pemberton"),
    ("I was diagnosed with narcolepsy.", "has_condition", "narcolepsy"),
    ("My salary is 91000 euros.", "earns", "91000 euros"),
    ("I voted for the Pineapple Party.", "voted_for", "Pineapple Party"),
]
PATHS = ["forget_memory", "erase_memory", "erase_user", "delete_agent", "erase_project",
         "edit_away"]
# The paths that delete the row for good: nothing of the words stays in any table.
PHYSICAL = {"erase_memory", "erase_user", "delete_agent", "erase_project"}
# The paths that take the neighbours of the memory too.
TAKES_NEIGHBOURS = {"erase_user", "delete_agent", "erase_project"}

BEFORE = [("I live in Lisbon.", Fact("u1", "lives_in", "Lisbon"), "Lisbon"),
          ]
AFTER_NEIGHBOUR = ("I like hiking in the mountains.", Fact("u1", "likes", "hiking"), "hiking")


def open_facts_with(db, marker):
    db.expire_all()
    return [f for f in db.query(AgentFact)
            if f.invalid_at is None and marker.lower() in " ".join(
                str(x) for x in (f.subject_label, f.predicate_raw, f.object_label)).lower()]


def the_reads(w, marker, user="u1"):
    """Every read a customer has, as the text it returns."""
    c = w.client
    out = {
        "list": c.get("/v1/memories", params={"user_id": user}).text,
        "facts": c.get("/v1/facts", params={"user_id": user}).text,
        "facts+history": c.get("/v1/facts", params={"user_id": user,
                                                    "include_invalidated": "true"}).text,
        "search": c.post("/v1/memories/search", json={"query": marker, "user_id": user}).text,
        "context": c.get("/v1/context", params={"user_id": user, "query": marker}).text,
    }
    return out


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("phrasing", PHRASINGS, ids=[p[2] for p in PHRASINGS])
def test_a_deleted_value_is_not_sent_to_the_model_again(world, db, tenant, path, phrasing):
    """mem0 #7452: the deleted value came back 19 of 24 times. Here no prompt
    written after the deletion holds it, no open fact derives from it, and the
    paths that erase leave it in no table at all."""
    w = world
    text, predicate, marker = phrasing
    agent = "bot-7" if path == "delete_agent" else None
    project = None
    if path == "erase_project":
        project = project_service.create_project(db, tenant_id=tenant.id, name="Beta")
        w.use(project.id)

    # ── before: a neighbour, the memory, a memory after it ─────────────────
    w.reads[text] = [Fact("u1", predicate, marker)]
    for t, f, _ in BEFORE:
        w.reads[t] = [f]
    w.reads[AFTER_NEIGHBOUR[0]] = [AFTER_NEIGHBOUR[1]]
    w.write(BEFORE[0][0], agent=agent)
    target = w.write(text, agent=agent)
    w.write(AFTER_NEIGHBOUR[0], agent=agent)
    # The guard: the words are in the prompt of the memory written after them
    # (the earlier memories of the end user, read for pronouns), so this
    # harness does see a leak when there is one.
    assert len(w.seen(marker)) >= 2, "the harness cannot see the words in a prompt"
    assert open_facts_with(db, marker), "the scripted writer wrote no fact for the words"

    # ── the deletion ───────────────────────────────────────────────────────
    c = w.client
    if path == "forget_memory":
        r = c.delete(f"/v1/memories/{target}")
    elif path == "erase_memory":
        r = c.delete(f"/v1/memories/{target}", params={"erase": "true"})
    elif path == "erase_user":
        r = c.delete("/v1/users/u1/memories")
    elif path == "delete_agent":
        r = c.delete(f"/v1/agents/{agent}")
    elif path == "edit_away":
        w.reads["Nothing to say any more."] = []
        r = c.patch(f"/v1/memories/{target}", json={"content": "Nothing to say any more."})
    if path == "erase_project":
        project_service.delete_project(db, tenant.id, project, erase=True)
        gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
        w.use(gamma.id)
        r = None
    if r is not None:
        assert r.status_code == 200, r.text
    mark = len(w.prompts)

    # ── after: the same user writes again ──────────────────────────────────
    after = [("Update from today: now it is Followup-Alpha.", Fact("u1", predicate, "Followup-Alpha")),
             ("I like green tea in the evening.", Fact("u1", "likes", "green tea")),
             ("I started learning Basque last week.", Fact("u1", "learns", "Basque"))]
    for t, f in after:
        w.reads[t] = [f]
    agent_after = agent
    w.write(after[0][0], agent=agent_after)
    w.write(after[1][0], agent=agent_after)
    # The third is left to the drain, as a write with WRITE_ASYNC=true is.
    from tests.conftest import seeded_project_id
    import uuid
    pid = (db.query(AgentMemory.project_id).filter(AgentMemory.tenant_id == tenant.id).first() or [None])[0] \
        or seeded_project_id(db, tenant.id)
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id="u1",
                       agent_id=agent_after, content=after[2][0], graph_sync_status="pending"))
    db.commit()
    w.drain()

    # ── the claim ──────────────────────────────────────────────────────────
    assert len(w.prompts) > mark, "the new writes reached no model: nothing was checked"
    leaked = w.seen(marker, since=mark)
    assert not leaked, f"{len(leaked)} prompt(s) after the deletion hold the deleted value"
    assert not open_facts_with(db, marker), "a fact derived from the deleted words is open"
    db.expire_all()
    assert not any(marker in (m.content or "") + (m.observation or "")
                   for m in db.query(AgentMemory).filter(AgentMemory.deleted_at.is_(None))), \
        "a live memory holds the deleted words"
    if path in TAKES_NEIGHBOURS:
        # The neighbours went with it, and the model never hears of them either.
        for _t, _f, word in BEFORE + [AFTER_NEIGHBOUR]:
            assert not w.seen(word, since=mark), f"{word!r}, an erased neighbour, was sent again"
    if path in PHYSICAL:
        assert dump_rows_with(db, marker) == [], "the words stay in a table after the erasure"
    for name, body in the_reads(w, marker).items():
        if name == "facts+history" and path not in PHYSICAL:
            continue  # a forget keeps the closed fact in history, by contract
        assert marker not in body, f"the read {name!r} returns the deleted value"
