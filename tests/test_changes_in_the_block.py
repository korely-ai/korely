"""Agent notices in the answers (1.0.15): the section "Changed since you last
read" at the head of `volatile`, and `changed` / `changed_more` in REST's
answer, for a read that names its reader only.

Held here: without a reader, or with nothing changed, the block is the one of
before (the fields are absent without a reader); `stable` and `stable_hash`
never change; at most 3 lines, the rest counted, each told once; the budget
holds at 400, 800, 1,500 and 4,000 tokens; REST and MCP show the same lines;
a degraded block keeps its closing note; in an agent's memory with no end
user only the explicit closures by default, every kind with `changes=all`;
in a memory with an end user every kind; a block of the past tells nothing.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact
from korely_agent.mcp import server as mcp_server
from korely_agent.services import context as ctx
from korely_agent.services import keys as key_service
from korely_agent.services import projects as project_service
from korely_agent.services import read_marks, vectors

VECTOR = [0.1] * EMBEDDING_DIM
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
QUESTION = "Where does Maria live?"


@pytest.fixture(autouse=True)
def _vector(monkeypatch):
    from korely_agent.services import memory_engine

    for module in (ctx, memory_engine):
        monkeypatch.setattr(module, "embed", lambda *a, **k: VECTOR)


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
def default(db, tenant):
    return project_service.get_or_create_default_project(db, tenant.id)


@pytest.fixture
def key(db, tenant, default):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="k")
    return full


def fact(db, tenant, project, *, user="u1", agent=None, obj="Rome", model="gemini-test",
         subject="Maria", fid=None):
    f = AgentFact(id=fid or uuid.uuid4(), tenant_id=tenant.id, project_id=project.id,
                  end_user_id=user, agent_id=agent, subject_label=subject, predicate_raw="lives in",
                  predicate_canonical="lives_in", object_label=obj, valid_from=START,
                  model_used=model, embedding=VECTOR, embedding_status=vectors.DONE)
    db.add(f)
    db.commit()
    return f


def replaced(db, tenant, project, old, *, obj, model="gemini-test", minutes=0):
    new = fact(db, tenant, project, user=old.end_user_id, agent=old.agent_id, obj=obj,
               model=model, subject=old.subject_label)
    old.invalid_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    old.invalidated_by_id = new.id
    db.commit()
    old.closed_known_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    db.commit()
    return new


def ask(client, key, **params):
    r = client.get("/v1/context", params={"query": QUESTION, "user_id": "u1", **params},
                   headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    return r.json()


def mcp(full, args):
    async def go():
        assert await mcp_server.KorelyAgentKeyVerifier().verify_token(full) is not None
        try:
            res = await mcp_server._get_agent_mcp().call_tool("korely_get_context", args)
        except ToolError as e:
            return str(e)
        content = res[0] if isinstance(res, tuple) else res
        return "".join(getattr(c, "text", "") for c in content)
    return asyncio.run(go())


def _section(block: str) -> list:
    """The section's lines, wherever it is (REST's `volatile` opens with it;
    the MCP's block has the reader note before it)."""
    at = block.find(ctx.CHANGED_HEADING)
    if at < 0:
        return []
    return block[at:].split("\n\n", 1)[0].split("\n")[1:]


def test_without_a_reader_nothing_of_it(client, db, tenant, default, key):
    old = fact(db, tenant, default)
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    replaced(db, tenant, default, old, obj="Milan")
    out = ask(client, key)
    assert "changed" not in out and "changed_more" not in out
    assert ctx.CHANGED_HEADING not in out["context"]
    assert ask(client, key, reader="bot")["changed"], "the change still waits for its reader"


def test_a_change_heads_the_volatile_part_once(client, db, tenant, default, key):
    old = fact(db, tenant, default)
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    before = ask(client, key)
    replaced(db, tenant, default, old, obj="Milan")
    after = ask(client, key)
    told = ask(client, key, reader="bot")
    assert told["stable"] == after["stable"] == before["stable"]
    assert told["stable_hash"] == after["stable_hash"]
    (line,) = _section(told["volatile"])
    assert line.startswith("- was: Maria lives in Rome") and "-> now: Maria lives in Milan" in line
    assert line.endswith("read from a memory)")
    assert told["volatile"].split("\n\n", 1)[1] == after["volatile"], \
        "the rest of the block is the block without the section"
    assert [c["fact_id"] for c in told["changed"]] == [f"fct_{old.id.hex}"]
    assert told["changed_more"] == 0
    again = ask(client, key, reader="bot")
    assert again["changed"] == [] and again["changed_more"] == 0
    assert again["context"] == after["context"], "told once, the block is the one of before"


def test_three_at_most_and_the_rest_counted(client, db, tenant, default, key):
    olds = [fact(db, tenant, default, obj=f"place {i}") for i in range(5)]
    read_marks.mark(tenant.id, default.id, "bot", [f.id for f in olds])
    for i, f in enumerate(olds):
        replaced(db, tenant, default, f, obj=f"town {i}", minutes=i)
    first = ask(client, key, reader="bot")
    assert len(_section(first["volatile"])) == 3 and first["changed_more"] == 2
    assert [c["fact_id"] for c in first["changed"]] == [f"fct_{f.id.hex}" for f in olds[::-1][:3]]
    second = ask(client, key, reader="bot")
    assert len(second["changed"]) == 2 and second["changed_more"] == 0


@pytest.mark.parametrize("budget", [400, 800, 1500, 4000])
def test_the_budget_holds(client, db, tenant, default, key, budget):
    olds = [fact(db, tenant, default, obj=f"a rather long name of a place number {i} " * 3)
            for i in range(4)]
    for i in range(30):
        fact(db, tenant, default, obj=f"another fact {i}", subject=f"Someone {i}")
    read_marks.mark(tenant.id, default.id, "bot", [f.id for f in olds])
    for i, f in enumerate(olds):
        replaced(db, tenant, default, f, obj=f"somewhere else entirely {i} " * 3, minutes=i)
    out = ask(client, key, reader="bot", token_budget=budget)
    assert out["tokens"] <= budget, out["tokens"]
    assert len(out["changed"]) + out["changed_more"] == 4
    assert len(_section(out["volatile"])) == len(out["changed"])


def test_rest_and_mcp_say_the_same_lines(client, db, tenant, default, key):
    old = fact(db, tenant, default)
    read_marks.mark(tenant.id, default.id, "rest-bot", [old.id])
    read_marks.mark(tenant.id, default.id, "mcp-bot", [old.id])
    replaced(db, tenant, default, old, obj="Milan")
    told = ask(client, key, reader="rest-bot")
    rest = _section(told["volatile"])
    assert told["volatile"].startswith(ctx.CHANGED_HEADING)
    said = mcp(key, {"query": QUESTION, "user_id": "u1", "reader": "mcp-bot"})
    assert rest and _section(said) == rest
    assert said == told["context"], "the MCP's block is REST's `context`"
    assert _section(mcp(key, {"query": QUESTION, "user_id": "u1", "reader": "mcp-bot"})) == []


def test_a_degraded_block_keeps_its_note(client, db, tenant, default, key, monkeypatch):
    old = fact(db, tenant, default)
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    replaced(db, tenant, default, old, obj="Milan")
    from korely_agent.services import memory_engine

    for module in (ctx, memory_engine):
        monkeypatch.setattr(module, "embed", lambda *a, **k: None)
    out = ask(client, key, reader="bot")
    assert out["degraded"] and out["changed"]
    assert out["volatile"].startswith(ctx.CHANGED_HEADING)
    assert out["volatile"].rsplit("\n\n", 1)[1].startswith("NOTE")


def test_in_an_agents_memory_only_the_explicit_ones_unless_all(client, db, tenant, default, key):
    judged = fact(db, tenant, default, user=None, agent="planner", obj="Rome")
    stated = fact(db, tenant, default, user=None, agent="planner", obj="Acme",
                  subject="the release")
    read_marks.mark(tenant.id, default.id, "bot", [judged.id, stated.id])
    read_marks.mark(tenant.id, default.id, "bot-all", [judged.id, stated.id])
    replaced(db, tenant, default, judged, obj="Milan")
    replaced(db, tenant, default, stated, obj="Globex", model="direct")
    head = {"Authorization": f"Bearer {key}"}

    def changed(**params):
        return client.get("/v1/context", params={"query": QUESTION, **params}, headers=head).json()

    by_default = changed(reader="bot")
    assert [c["origin"] for c in by_default["changed"]] == ["stated"]
    assert by_default["changed_more"] == 0, "a change not told is not counted either"
    every = changed(reader="bot-all", changes="all")
    assert sorted(c["origin"] for c in every["changed"]) == ["read_from_memory", "stated"]


def test_in_an_end_users_memory_every_kind(client, db, tenant, default, key):
    old = fact(db, tenant, default)
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    replaced(db, tenant, default, old, obj="Milan")
    assert [c["origin"] for c in ask(client, key, reader="bot")["changed"]] == ["read_from_memory"]


def test_a_block_of_the_past_tells_nothing(client, db, tenant, default, key):
    old = fact(db, tenant, default)
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    replaced(db, tenant, default, old, obj="Milan")
    out = ask(client, key, reader="bot", known_as_of=datetime.now(timezone.utc).isoformat())
    assert "changed" not in out and ctx.CHANGED_HEADING not in out["context"]
    assert ask(client, key, reader="bot")["changed"], "nothing was consumed"


def test_a_forgotten_memorys_words_are_not_in_the_block(client, db, tenant, default, key):
    """The fact replaced, then its memory forgotten: the block tells the
    change without its words (review of piece 2: "was: ..." in a read of
    now, after the forget)."""
    from korely_agent.db.models import AgentMemory
    from korely_agent.services.facts import erasure

    mem = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                      content="Maria lives in Secret-Rome", graph_sync_status="done")
    db.add(mem)
    db.commit()
    old = fact(db, tenant, default, obj="Secret-Rome")
    old.source_memory_id, old.source_memory_ids = mem.id, [str(mem.id)]
    db.commit()
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    replaced(db, tenant, default, old, obj="Milan")
    erasure.forget_memory(db, tenant.id, mem.id, project_id=default.id)
    out = ask(client, key, reader="bot")
    (line,) = _section(out["volatile"])
    assert line.startswith(f"- was: {ctx.FORGOTTEN_WORDS} -> now: Maria lives in Milan")
    assert "Secret-Rome" not in out["context"] and "Secret-Rome" not in str(out["changed"])
    assert out["changed"][0]["was"] is None


@pytest.mark.parametrize("reserved, more", [(set(), 0), (None, 1)],
                         ids=["another-answer-reserved-it", "the-reservation-gave-way"])
def test_only_what_this_answer_reserved_is_in_the_block(client, db, tenant, default, key,
                                                       monkeypatch, reserved, more):
    """The block reserves the changes it packed before it is written
    (read_marks.claim), and tells only those (review of piece 2: two
    answers at once told the same 30). Reserved by another answer at the
    same moment: not told here, and no longer waiting. Not reserved at all
    (a lock): not told, and still waiting."""
    old = fact(db, tenant, default)
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    replaced(db, tenant, default, old, obj="Milan")
    plain = ask(client, key)
    monkeypatch.setattr(read_marks, "claim", lambda *a, **k: reserved)
    out = ask(client, key, reader="bot")
    assert _section(out["volatile"]) == [] and out["changed"] == []
    assert out["changed_more"] == more
    assert out["context"] == plain["context"]


# ── review of piece 3 (9 Nov 2026) ──────────────────────────────────────────

def _memories(db, tenant, default, monkeypatch, count, words="A long story. " * 20,
              head="Maria told us, day {i}: "):
    """`count` memories of u1 the block's search finds, as the budget tests
    stub it (tests/test_context_budget_small.py). Short sentences, each
    begun with a capital: "a long story. a long story." is one sentence by
    Unicode's rule SB8 (a full stop before a lower-case word ends none), and
    since 1.0.30 a memory is cut only after a whole sentence."""
    from korely_agent.db.models import AgentMemory
    from korely_agent.services.memory_engine import AgentMemoryEngine

    rows = []
    for i in range(count):
        m = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                        graph_sync_status="done", content=head.format(i=i) + words)
        db.add(m)
        rows.append(m)
    db.commit()
    found = [{"id": str(m.id), "content": m.content} for m in rows]
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, *a, **k: found)
    return rows


@pytest.mark.parametrize("agent", [None, "  ", "planner"], ids=["no-id", "blank-agent", "agent"])
def test_a_memory_with_no_ids_tells_every_kind(client, db, tenant, default, key, agent):
    """No end user and no agent id (the MCP or an SDK on its own, the most
    common install): not an agent's memory, so every kind of change is told.
    The filter of the kinds read the agent rule as NULL there, and its NOT
    left out every closure read from a memory (review of piece 3)."""
    judged = fact(db, tenant, default, user=None, agent=agent, obj="Rome")
    stated = fact(db, tenant, default, user=None, agent=agent, obj="Acme", subject="the release")
    read_marks.mark(tenant.id, default.id, "bot", [judged.id, stated.id])
    replaced(db, tenant, default, judged, obj="Milan")
    replaced(db, tenant, default, stated, obj="Globex", model="direct")
    out = client.get("/v1/context", params={"query": QUESTION, "reader": "bot"},
                     headers={"Authorization": f"Bearer {key}"}).json()
    origins = sorted(c["origin"] for c in out["changed"])
    if agent == "planner":
        assert origins == ["stated"] and out["changed_more"] == 0
    else:
        assert origins == ["read_from_memory", "stated"], out["changed"]


def test_the_rule_of_an_agents_memory_is_the_python_rule_on_every_row(db, tenant, default):
    """One rule: the condition the notices read says, for every row, what
    memory_core's `owner.agent_scoped` says of its two ids, NULL included."""
    from sqlalchemy import select

    from korely_agent.services.facts import changes as notices
    from korely_agent.services.memory_core import owner

    cases = [(None, None), (None, "  "), (None, "　"), (None, "planner"), ("u1", None),
             ("u1", "planner"), ("u1", "  ")]
    rows = {fact(db, tenant, default, user=u, agent=a, subject=f"S{i}").id: (u, a)
            for i, (u, a) in enumerate(cases)}
    said = dict(db.execute(select(AgentFact.id, notices.of_an_agents_memory())
                           .where(AgentFact.id.in_(list(rows)))).all())
    assert {rows[i]: v for i, v in said.items()} == \
        {(u, a): owner.agent_scoped(u, a) for u, a in cases}


def test_a_read_that_failed_tells_no_change(client, db, tenant, default, key, monkeypatch):
    """The search down and nothing to show from the memory: no block of
    changes alone, which came without its NOTE and which the MCP answered as
    a success (review of piece 3). The changes wait, and are told once the
    memory can be read."""
    from korely_agent.services import memory_engine

    old = fact(db, tenant, default)
    for r in ("bot", "bot-mcp"):
        read_marks.mark(tenant.id, default.id, r, [old.id])
    old.invalid_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    for module in (ctx, memory_engine):
        monkeypatch.setattr(module, "embed", lambda *a, **k: None)
    failed = ask(client, key, reader="bot")
    assert failed["degraded"] and failed["context"] == ""
    assert failed["changed"] == [] and failed["changed_more"] == 1
    said = mcp(key, {"query": QUESTION, "user_id": "u1", "reader": "bot-mcp"})
    assert "search_unavailable" in said, said
    for module in (ctx, memory_engine):
        monkeypatch.setattr(module, "embed", lambda *a, **k: VECTOR)
    assert [c["fact_id"] for c in ask(client, key, reader="bot")["changed"]] == \
        [f"fct_{old.id.hex}"]


def test_what_now_shows_is_marked_and_recorded(client, db, tenant, default, key):
    """The fact the section shows as "now" is read: in the block's sources,
    in the audit trail, marked for the reader, and its own change is told in
    its turn (review of piece 3: never marked, its change never told). The
    block reads the agent namespace of the fact the reader had, and the fact
    that replaced it is another agent's: only the section shows it."""
    from korely_agent.db.models import AgentReadMark, AuditEvent

    old = fact(db, tenant, default, agent="support")
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    new = fact(db, tenant, default, agent="planner", obj="Milan")
    old.invalid_at, old.invalidated_by_id = datetime.now(timezone.utc), new.id
    db.commit()
    first = ask(client, key, reader="bot", agent_id="support")
    assert [c["now"]["fact_id"] for c in first["changed"]] == [f"fct_{new.id.hex}"]
    assert ctx.KNOWN_FACTS_HEADING not in first["volatile"], "only the section shows Milan"
    assert first["sources"][:2] == [f"fct_{old.id.hex}", f"fct_{new.id.hex}"]
    db.expire_all()
    assert db.query(AgentReadMark).filter(AgentReadMark.reader == "bot",
                                          AgentReadMark.fact_id == new.id).count() == 1
    read = [e.read_ids for e in db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id,
                                                            AuditEvent.read_ids.isnot(None))]
    assert any(f"fct_{new.id.hex}" in str(r) for r in read), read
    newer = replaced(db, tenant, default, new, obj="Naples")
    second = ask(client, key, reader="bot", agent_id="planner")
    assert [(c["fact_id"], c["now"]["fact_id"]) for c in second["changed"]] == \
        [(f"fct_{new.id.hex}", f"fct_{newer.id.hex}")]


def test_a_block_of_every_end_user_names_each_one(client, db, tenant, default, key):
    """The lines of the section write a fact as the known facts do: "The
    user" only for the block's own end user (review of piece 3: in a block
    of every end user each change said "The user" of somebody else)."""
    a = fact(db, tenant, default, user="u1", subject="u1", obj="Rome")
    b = fact(db, tenant, default, user="u2", subject="u2", obj="Turin")
    for r in ("all", "one"):
        read_marks.mark(tenant.id, default.id, r, [a.id, b.id])
    replaced(db, tenant, default, a, obj="Milan")
    replaced(db, tenant, default, b, obj="Genoa", minutes=1)
    every = client.get("/v1/context", params={"query": QUESTION, "reader": "all"},
                       headers={"Authorization": f"Bearer {key}"}).json()
    lines = _section(every["volatile"])
    assert len(lines) == 2 and not any("The user" in line for line in lines), lines
    assert any(line.startswith("- was: u1 lives in Rome") for line in lines)
    assert any(line.startswith("- was: u2 lives in Turin") for line in lines)
    (one,) = _section(ask(client, key, reader="one")["volatile"])
    assert one.startswith("- was: The user lives in Rome"), one


@pytest.mark.parametrize("budget", [400, 450, 500, 600, 800])
def test_a_reader_never_takes_the_known_facts_nor_stable(client, db, tenant, default, key,
                                                         budget):
    """The changes come after the known facts and out of the memories' room:
    with `reader` the block has the known facts, `stable` and its hash of the
    block without it (review of piece 3: at 500 and 600 tokens three long
    changes left the block without its facts and `stable` empty)."""
    fact(db, tenant, default, obj="x" * 400, subject="Maria")
    olds = [fact(db, tenant, default, obj=("place " * 40)[:230] + str(i), subject=f"P{i}")
            for i in range(3)]
    read_marks.mark(tenant.id, default.id, "bot", [f.id for f in olds])
    for i, f in enumerate(olds):
        replaced(db, tenant, default, f, obj=("town " * 40)[:230] + str(i), minutes=i)
    plain = ask(client, key, token_budget=budget)
    told = ask(client, key, reader="bot", token_budget=budget)
    assert (told["stable"], told["stable_hash"]) == (plain["stable"], plain["stable_hash"])
    known = plain["volatile"].split(ctx.KNOWN_FACTS_HEADING, 1)
    assert len(known) == 2, "the block without reader has its facts"
    assert ctx.KNOWN_FACTS_HEADING + known[1].split("\n\n")[0] in told["volatile"]
    assert told["tokens"] <= budget


def test_the_changes_never_take_the_last_memory(client, db, tenant, default, key, monkeypatch):
    """No known fact, memories that fill the block, changes: the changes take
    part of the memories' room, so `stable` (the reader note comes with a
    memory) is the block's without `reader`."""
    _memories(db, tenant, default, monkeypatch, 3)
    olds = [fact(db, tenant, default, obj=("place " * 60)[:200] + str(i), subject=f"P{i}")
            for i in range(3)]
    for budget in (400, 500, 800):
        read_marks.mark(tenant.id, default.id, f"bot-{budget}", [f.id for f in olds])
    for f in olds:
        f.invalid_at = datetime.now(timezone.utc) - timedelta(seconds=1)   # no longer true
    db.commit()
    for budget in (400, 500, 800):
        plain = ask(client, key, token_budget=budget)
        told = ask(client, key, reader=f"bot-{budget}", token_budget=budget)
        assert told["changed"], "changes were told"
        assert told["stable"] == plain["stable"] and told["stable_hash"] == plain["stable_hash"]
        assert (ctx.MEMORIES_HEADING in told["context"]) == \
            (ctx.MEMORIES_HEADING in plain["context"]), budget
        assert told["tokens"] <= budget


def test_the_changes_leave_the_last_memory_its_place(db, tenant, default):
    """Where half of the room the facts leave is too little for a memory, a
    change that would fill that half takes only what the first memory
    leaves (its heading and its shortest fragment, `_first_memory_floor`),
    written short: the block never loses its last memory to the changes
    (and so neither its reader note in `stable`), and the change is still
    told. Without memories the changes take the room, the change whole."""
    from korely_agent.db.models import AgentMemory

    mem = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                      graph_sync_status="done", content="Maria told us about her week. " * 10)
    db.add(mem)
    db.commit()
    hits = [{"id": str(mem.id), "content": mem.content}]
    overhead = 1 + ctx._Block(400).before_the_first_memory()
    half = overhead + ctx._MIN_FRAGMENT_CHARS - 4          # too little for a fragment
    base = {"fact_id": "fct_1", "was": "", "now": None, "known_at": "2026-11-08",
            "origin": "stated"}
    n = half - (1 + ctx._cost(ctx.CHANGED_HEADING) + ctx._cost(ctx._SEPARATOR)) \
        - ctx._cost(ctx.change_line(base))
    assert n > 0, "a change can fill half of that room"
    change = {**base, "was": "x" * n}

    def block():
        b = ctx._Block(400)
        b.pack.used = b.pack.cap - 2 * half
        return b

    b = block()
    floor = ctx._first_memory_floor(b, ctx._memory_lines(db, hits, hits))
    assert half < floor < 2 * half, "half of the room is too little for the memory"
    lines, packed, mems = _in_the_room(db, b, [change], hits)
    assert len(mems) == 1 and packed == [change]
    (line,) = lines
    assert "…" in line, "written short"
    section = 1 + ctx._cost(ctx.CHANGED_HEADING) + ctx._cost(ctx._SEPARATOR) + ctx._cost(line)
    assert section <= 2 * half - floor, "the change took at most the room less the floor"
    assert b.pack.used <= b.pack.cap
    lines, packed, mems = _in_the_room(db, block(), [change], [])
    assert packed == [change] and lines == [ctx.change_line(change)] and mems == []


def _in_the_room(db, b, changes, hits):
    """(the changes' lines, the changes packed, the memories' lines), as the
    block packs them: the changes before the memories of `hits`, then the
    memories."""
    lines = ctx._memory_lines(db, hits, hits)
    change_lines, packed = ctx._changes_in_the_memories_room(b, changes, lines)
    return change_lines, packed, ctx._pack_memory_lines(b, lines)


def _long_change(i: int, minutes: int = 0) -> dict:
    return {"fact_id": f"fct_{i}", "was": LONG_WAS + str(i),
            "now": {"fact_id": f"fct_n{i}", "text": LONG_NOW + str(i)},
            "known_at": "2026-11-09", "origin": "corrected"}


def _short_change(i: int) -> dict:
    return {"fact_id": f"fct_s{i}", "was": f"Q{i} lives in S{i}",
            "now": {"fact_id": f"fct_t{i}", "text": f"Q{i} lives in T{i}"},
            "known_at": "2026-11-09", "origin": "corrected"}


def _section_cost(lines: list) -> int:
    return ctx._cost(ctx._section(ctx.CHANGED_HEADING, lines)) + ctx._cost(ctx._SEPARATOR) \
        if lines else 0


def test_the_changes_take_at_most_half_of_the_memories_room(db, tenant, default):
    """With memories to follow and a small first memory, the changes still
    take at most half of the room the facts leave; with a first memory that
    does not fit the room even alone (the block shows none either way), and
    with no memory, they may take all of it."""
    from korely_agent.db.models import AgentMemory

    short = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                        graph_sync_status="done", content="Maria: ok.")
    huge = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                       graph_sync_status="done", content="Maria told us about her week. " * 40)
    db.add_all([short, huge])
    db.commit()
    changes = [_long_change(i) for i in range(3)]
    room = 1500
    for hits, most in (([short], room // 2), ([], room)):
        hits = [{"id": str(m.id), "content": m.content} for m in hits]
        b = ctx._Block(800)
        b.pack.used = b.pack.cap - room
        lines, packed, mems = _in_the_room(db, b, changes, hits)
        assert packed and room // 2 - 200 < _section_cost(lines) <= most, (len(hits), lines)
        assert bool(mems) == bool(hits)
    # The first memory needs more than the whole room: none is shown with
    # or without the changes, and the changes take the room.
    hits = [{"id": str(huge.id), "content": huge.content}]
    b = ctx._Block(800)
    b.pack.used = b.pack.cap - 120
    assert ctx._first_memory_floor(b, ctx._memory_lines(db, hits, hits)) > 120
    small = [_short_change(0)]
    lines, packed, mems = _in_the_room(db, b, small, hits)
    assert packed == small and mems == [] and _section_cost(lines) > 60


def test_a_long_change_passed_over_is_told_after_the_others_in_order(client):
    """A long change that does not fit its share is passed over while the
    short ones behind it are packed, then told in what they leave, alone if
    need be; the section keeps the newest first."""
    changes = [_long_change(0), _short_change(0), _short_change(1)]
    b = ctx._Block(800)
    room = 420
    b.pack.used = b.pack.cap - room
    lines, packed = ctx._pack_changes(b, changes, b.pack.cap)
    assert [c["fact_id"] for c in packed] == ["fct_0", "fct_s0", "fct_s1"], "newest first"
    assert lines[1:] == [ctx.change_line(c) for c in changes[1:]], "the short ones whole"
    assert "…" in lines[0] and _section_cost(lines) <= room
    assert ctx.change_line(changes[0], most=room // 3 - 34) is None, \
        "the long one did not fit its share: it was told in what the others left"


def test_a_long_change_shares_the_room_with_the_changes_still_waiting(client):
    """With one change behind it, a long one is written short in half of
    the room, not in a third: its share counts the changes still waiting,
    not the three places of a full section, and the other is told beside
    it (the skeptic's mutant that counted the three places lived)."""
    changes = [_long_change(0), _short_change(0)]
    b = ctx._Block(800)
    room = 420
    b.pack.used = b.pack.cap - room
    before = 1 + ctx._cost(ctx.CHANGED_HEADING) + ctx._cost(ctx._SEPARATOR)
    assert ctx.change_line(changes[0], most=room // 3 - before) is None, "a third holds nothing"
    lines, packed = ctx._pack_changes(b, changes, b.pack.cap)
    assert [c["fact_id"] for c in packed] == ["fct_0", "fct_s0"]
    assert before + ctx._cost(lines[0]) <= room // 2 < before + ctx._cost(ctx.change_line(changes[0]))
    assert lines[1] == ctx.change_line(changes[1])


def test_the_first_memory_floor_is_what_the_packing_needs(db, tenant, default):
    """`_first_memory_floor` is the fewest bytes in which the memories show
    their first line: at the floor the packing shows it, one byte less it
    shows none, for a memory cut to a fragment and for one short enough to
    go whole."""
    from korely_agent.db.models import AgentMemory

    for content in ("Maria told us about her week. " * 10, "Maria: ok."):
        mem = AgentMemory(tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                          graph_sync_status="done", content=content)
        db.add(mem)
        db.commit()
        hits = [{"id": str(mem.id), "content": mem.content}]
        lines = ctx._memory_lines(db, hits, hits)
        floor = ctx._first_memory_floor(ctx._Block(400), lines)
        for room, shown in ((floor, 1), (floor - 1, 0)):
            b = ctx._Block(400)
            b.pack.used = b.pack.cap - room
            assert len(ctx._pack_memory_lines(b, lines)) == shown, (content, room)
    assert ctx._first_memory_floor(ctx._Block(400), []) is None


@pytest.mark.parametrize("budget", [100, 800])
def test_a_long_change_is_told_short_and_holds_none_back(client, db, tenant, default, key,
                                                          budget):
    """Twenty-two long changes ahead of two short ones: each is told, the
    long ones written short (each fact cut with the ellipsis, the frame
    whole), and every block inside its budget, until none waits (review of
    the corrections, 9 Nov 2026: past twenty long ones the short ones were
    never told, at 100 tokens as at 800)."""
    short = [fact(db, tenant, default, obj=f"S{i}", subject=f"Q{i}") for i in range(2)]
    long = [fact(db, tenant, default, obj=("a very long name of a place " * 17)[:480] + str(i),
                 subject=f"L{i}") for i in range(22)]
    read_marks.mark(tenant.id, default.id, "bot", [f.id for f in short + long])
    for i, f in enumerate(short):
        replaced(db, tenant, default, f, obj=f"T{i}", minutes=-30 - i)
    for i, f in enumerate(long):
        replaced(db, tenant, default, f, obj=("another long place " * 26)[:490] + str(i),
                 minutes=i)
    told, cut = set(), 0
    for _ in range(30):
        # A block of another namespace: its known facts do not fill it (a
        # block its known facts fill tells no change, by the quota), and the
        # section is the reader's whatever the namespace.
        out = ask(client, key, reader="bot", token_budget=budget, agent_id="nobody")
        assert out["tokens"] <= budget
        told |= {c["fact_id"] for c in out["changed"]}
        cut += sum("\u2026" in line for line in _section(out["volatile"]))
        if out["changed_more"] == 0:
            break
    assert told == {f"fct_{f.id.hex}" for f in short + long}
    assert cut > 0, "the long ones were written short"


LONG_WAS = ("a very long name of a place " * 17)[:480]
LONG_NOW = ("another long place " * 26)[:490]


@pytest.mark.parametrize("budget", [300, 350, 400, 450, 500])
@pytest.mark.parametrize("longs", [1, 3])
def test_long_changes_hold_no_short_one_back_in_a_block_with_facts_and_memories(
        client, db, tenant, default, key, monkeypatch, budget, longs):
    """The end user's block as a client reads it, its known facts and its
    memories, at 300 to 500 tokens: behind one to three long changes, a
    short one is left out of a read only when the section has its three
    lines, every change is told within one read more than the long ones,
    and every read keeps the memories the block without `reader` has, its
    `stable` and its budget (the fourth review of piece 3, 9 Nov 2026: the
    short ones were never told there, at every read, because the long one
    written short took the whole half of the memories' room and the guard
    dropped the section).

    Since 1.0.30 the first memory keeps its first whole sentence, at least
    40 characters, not a fragment of about 40 bytes, so the memories here
    open with a sentence of 41 characters, as long as the fragment was; a
    first sentence of 48 leaves the section one line less at 400 tokens
    (`test_a_long_first_sentence_leaves_the_changes_one_line_less`)."""
    _memories(db, tenant, default, monkeypatch, 3, head="Maria told us about her day. Day {i}: ")
    for i in range(4):
        fact(db, tenant, default, obj=f"Acme {i}", subject=f"K{i}")
    shorts = [fact(db, tenant, default, obj=f"S{i}", subject=f"Q{i}") for i in range(2)]
    olds = [fact(db, tenant, default, obj=LONG_WAS + str(i), subject=f"L{i}")
            for i in range(longs)]
    read_marks.mark(tenant.id, default.id, "bot", [f.id for f in shorts + olds])
    for i, f in enumerate(shorts):
        replaced(db, tenant, default, f, obj=f"T{i}", minutes=-30 - i)
    for i, f in enumerate(olds):
        now = replaced(db, tenant, default, f, obj=LONG_NOW + str(i), minutes=i)
        # Less relevant to the question than the short facts: the known facts
        # are the same at every run (with one vector for all, the ties fall
        # by the random ids, and a long fact taken into the facts' share
        # leaves the changes room for one short line).
        now.embedding = VECTOR[:-1] + [0.9]
    db.commit()
    plain = ask(client, key, token_budget=budget)
    assert ctx.MEMORIES_HEADING in plain["context"], "the block has memories"
    assert LONG_NOW not in plain["volatile"], "the long facts are not among the known facts"
    told, waiting = [], {f"fct_{f.id.hex}" for f in shorts}
    left = {f"fct_{f.id.hex}" for f in shorts + olds}
    for _ in range(1 + longs):
        out = ask(client, key, reader="bot", token_budget=budget)
        assert ctx.MEMORIES_HEADING in out["context"], "the changes never take the last memory"
        assert (out["stable"], out["stable_hash"]) == (plain["stable"], plain["stable_hash"])
        assert out["tokens"] <= budget
        told.append({c["fact_id"] for c in out["changed"]})
        assert told[-1] or not left, "at least one a read while some wait"
        left -= told[-1]
        # A short change waits only behind a full section (its newest three
        # lines), never behind a long one that does not fit its share.
        assert waiting <= told[-1] or len(told[-1]) == ctx.CHANGES_IN_THE_BLOCK, told
        waiting -= told[-1]
    assert not waiting and set().union(*told) == {f"fct_{f.id.hex}" for f in shorts + olds}, told
    assert out["changed_more"] == 0


def test_a_long_first_sentence_leaves_the_changes_one_line_less(client, db, tenant, default, key,
                                                               monkeypatch):
    """The price of a memory whole or cut after a complete sentence
    (1.0.30), pinned: the changes never take
    what the first memory needs, and that is now its first whole sentence
    ("[day] Maria told us, day 0: A long story.", 48 bytes) where it was a
    fragment of about 40 bytes. At 400 tokens, with the known facts and
    three memories, the section has room for one short change instead of
    two: the second waits for the next read, and both are told within two
    reads; the memory is shown, cut after a whole sentence."""
    _memories(db, tenant, default, monkeypatch, 3)
    for i in range(4):
        fact(db, tenant, default, obj=f"Acme {i}", subject=f"K{i}")
    shorts = [fact(db, tenant, default, obj=f"S{i}", subject=f"Q{i}") for i in range(2)]
    read_marks.mark(tenant.id, default.id, "bot", [f.id for f in shorts])
    for i, f in enumerate(shorts):
        replaced(db, tenant, default, f, obj=f"T{i}", minutes=-30 - i)
    db.commit()
    told = []
    for _ in range(2):
        out = ask(client, key, reader="bot", token_budget=400)
        assert ctx.MEMORIES_HEADING in out["context"] and out["tokens"] <= 400
        assert "Maria told us, day 0: A long story." in out["context"]
        told.append({c["fact_id"] for c in out["changed"]})
    assert [len(t) for t in told] == [1, 1]
    assert set().union(*told) == {f"fct_{f.id.hex}" for f in shorts}


def test_a_change_of_any_length_fits_a_block_with_memories(client, db, tenant, default, key,
                                                          monkeypatch):
    """One change of some 1,300 bytes, memories in the block, the default
    budget: told short at the first block (review of the corrections: never
    told)."""
    _memories(db, tenant, default, monkeypatch, 3)
    old = fact(db, tenant, default, obj="東京都港区六本木" * 25)
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    replaced(db, tenant, default, old, obj="大阪府大阪市北区梅田" * 22)
    out = ask(client, key, reader="bot", token_budget=800)
    (line,) = _section(out["volatile"])
    assert line.startswith("- was: Maria lives in 東京都") and "\u2026" in line
    assert line.endswith("read from a memory)") and out["tokens"] <= 800
    assert out["changed"][0]["was"].startswith("Maria lives in " + "東京都港区六本木" * 25), \
        "REST keeps the facts whole"


def test_a_change_line_written_short(client):
    """The short form keeps the frame and cuts each fact at an equal share;
    None when a share would hold no fragment."""
    change = {"fact_id": "fct_1", "was": "w" * 500, "now": {"fact_id": "fct_2", "text": "n" * 500},
              "known_at": "2026-11-09T10:00:00", "origin": "corrected"}
    full = ctx.change_line(change)
    short = ctx.change_line(change, most=300)
    assert ctx._cost(full) > 1000 and ctx._cost(short) <= 300
    assert short.startswith("- was: www") and " -> now: nnn" in short
    assert short.endswith("\u2026 (2026-11-09, corrected)")
    assert ctx.change_line(change, most=60) is None
    assert ctx.change_line({**change, "now": None}, most=300).startswith("- no longer true: www")


def _chars(text: str) -> list:
    """`text` as characters a reader sees, for texts whose only multi code
    point characters are the ones given whole in the list below."""
    return list(text)


_FAMILY_GIRL = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
_FAMILY_BOY = "\U0001F468\u200d\U0001F469\u200d\U0001F466"
_CODER = "\U0001F469\u200d\U0001F4BB"
_FLAGS = ["\U0001F1EE\U0001F1F9", "\U0001F1EB\U0001F1F7", "\U0001F1E9\U0001F1EA"]
_JAPAN, _JAMAICA = "\U0001F1EF\U0001F1F5", "\U0001F1EF\U0001F1F2"
# "उपयोगकर्ता की पसंदीदा दुकान मुंबई में है: " character by character, as a
# reader sees them (a consonant with its vowel signs, its virama, its nasal
# signs).
_HINDI = ["उ", "प", "यो", "ग", "क", "र्ता", " ", "की", " ", "प", "सं", "दी", "दा", " ", "दु",
          "का", "न", " ", "मुं", "ब", "ई", " ", "में", " ", "है", ":", " "]
# Two facts of one change that begin the same, as characters (the skeptic's
# probe of 9 Nov 2026 for the first three: two identical pieces in 72 of 133
# rooms for the address), in five scripts and with emoji, flags and marks.
SAME_START = {
    "address": (_chars("The user's primary shipping address for the EU warehouse is located "
                       "at Via Roma 1, Milan (since 2026-01-01)"),
                _chars("The user's primary shipping address for the EU warehouse is located "
                       "at Via Garibaldi 3, Turin (since 2026-11-09)")),
    "plan": (_chars("Acme's subscription is the Business plan with 60 seats (since 2026-01-01)"),
             _chars("Acme's subscription is the Business plan with 50 seats (since 2026-11-09)")),
    "procedure": (_chars("The release procedure of the main service requires a backup, the digest, "
                         "checks from outside and a rollback ready (since 2026-11-01)"),
                  _chars("The release procedure of the main service requires a backup, the "
                         "digest, checks from outside, a rollback ready and the owner's yes "
                         "(since 2026-11-09)")),
    "Japanese": (_chars("東京都港区六本木一丁目十二番地、森ビル三十二階、株式会社コレリー本社"),
                 _chars("東京都港区六本木一丁目十二番地、森ビル四十五階、株式会社コレリー本社")),
    "a family emoji": (_chars("The on-call team is ") + [_CODER, " ", _CODER, " ", _CODER]
                       + _chars(" and the contact is ") + [_FAMILY_GIRL] + _chars(" at home"),
                       _chars("The on-call team is ") + [_CODER, " ", _CODER, " ", _CODER]
                       + _chars(" and the contact is ") + [_FAMILY_BOY] + _chars(" at home")),
    "flags": (_chars("The offices are in ") + _FLAGS + _chars(" and, since this year, in ")
              + [_JAPAN], _chars("The offices are in ") + _FLAGS
              + _chars(" and, since this year, in ") + [_JAMAICA]),
    "a combining mark": (_chars("La ville de résidence de l'utilisateur est Montr") + ["e\u0301"]
                         + _chars("al, Québec"),
                         _chars("La ville de résidence de l'utilisateur est Montr") + ["e\u0300"]
                         + _chars("al, Québec")),
    "a space": (_chars("The shipping address of the user for the EU warehouse is Via Roma 1, "
                       "Milan"),
                _chars("The shipping address of the user for the EU warehouse is Via Roma  1, "
                       "Milan")),
    "a run of spaces": (_chars("The note of the user for the courier reads: leave it at the door")
                        + ["A"] + [" "] * 120 + ["x"],
                        _chars("The note of the user for the courier reads: leave it at the door")
                        + [" "] * 120 + ["B"]),
    "a vowel sign": (_HINDI + ["\u0915\u093f", "ता", "ब", " ", "घ", "र"],
                     _HINDI + ["\u0915\u0940", "ता", "ब", " ", "घ", "र"]),
}


def _cuts(chars: list) -> set:
    out, at = {0}, 0
    for c in chars:
        at += len(c)
        out.add(at)
    return out


def _pieces(line: str):
    was, _, now = line[len("- was: "):].partition(" -> now: ")
    return was, now[:now.rindex(" (2026-11-09")]


def _shows_where_they_differ(piece: str, text: str, cuts: set, differs: int) -> bool:
    """Every part of `piece` between its ellipses is a run of whole
    characters of `text`, in order, and one of them holds the first
    character that differs (or ends the text, when it is the other's
    beginning)."""
    if piece == text:
        return True
    at, shows = 0, False
    for part in piece.split("…"):
        if not part:
            continue
        x = text.find(part, at)
        assert x >= at and x in cuts and x + len(part) in cuts, (piece, part)
        shows |= x <= differs < x + len(part) or (differs == len(text) == x + len(part))
        at = x + len(part)
    return shows


@pytest.mark.parametrize("name", list(SAME_START))
def test_a_change_written_short_shows_what_changed(name):
    """Two facts that begin the same, written short in every room from the
    first one that holds them up to the whole line: the two pieces always
    differ and each shows where they differ, cut only between characters
    as a reader sees them, the ellipsis where something is left out; and a
    room large enough once stays large enough."""
    was_chars, now_chars = SAME_START[name]
    was, now = "".join(was_chars), "".join(now_chars)
    differs = next(i for i, (a, b) in enumerate(zip(was + "\0", now + "\0")) if a != b)
    change = {"fact_id": "fct_1", "was": was, "now": {"fact_id": "fct_2", "text": now},
              "known_at": "2026-11-09T10:00:00", "origin": "corrected"}
    whole = ctx.change_line(change)
    rooms = [m for m in range(60, ctx._cost(whole)) if ctx.change_line(change, most=m)]
    assert rooms and rooms == list(range(rooms[0], ctx._cost(whole))), "no hole once told"
    assert rooms[0] < 140, "a short room is enough"
    for most in rooms:
        line = ctx.change_line(change, most=most)
        assert ctx._cost(line) <= most
        a, b = _pieces(line)
        assert a != b, (name, most)
        assert _shows_where_they_differ(a, was, _cuts(was_chars), differs), (name, most, a)
        assert _shows_where_they_differ(b, now, _cuts(now_chars), differs), (name, most, b)


def test_a_change_whose_difference_does_not_fit_is_not_told_short():
    """Two facts that differ only inside one character (as a reader sees
    it) longer than the room a piece has: nothing of the difference fits,
    so the change is not written short (and so neither told nor consumed)
    rather than two pieces that read the same; with room, it is told."""
    big = "a" + "\u0301" * 30
    change = {"fact_id": "fct_1", "was": "x" * 200 + big + "\u0300",
              "now": {"fact_id": "fct_2", "text": "x" * 200 + big + "\u0302"},
              "known_at": "2026-11-09T10:00:00", "origin": "corrected"}
    for most in range(60, 170):
        assert ctx.change_line(change, most=most) is None, most
    a, b = _pieces(ctx.change_line(change, most=400))
    assert a != b and a.endswith("\u0300") and b.endswith("\u0302"), "told with room"
    # Where they differ is the very first character of one of them, and the
    # character there is longer than a piece: not written short either.
    first = {"fact_id": "fct_1", "was": "e\u0301" + "Z" + "\u0301" * 25 + "z" * 300,
             "now": {"fact_id": "fct_2", "text": "e\u0301\u0300" + "z" * 300},
             "known_at": "2026-11-09T10:00:00", "origin": "corrected"}
    assert ctx.change_line(first, most=130) is None


@pytest.mark.parametrize("door", ["rest", "mcp"])
@pytest.mark.parametrize("budget", [200, 400])
def test_the_doors_tell_what_changed_in_a_long_fact(client, db, tenant, default, key, monkeypatch,
                                                    door, budget):
    """A plan whose long description is the same before and after, only
    the seats changed, in a block with memories: the section's line shows
    both seat counts, on REST and on the MCP, which has no `changed` to
    read them whole (the skeptic's probe: two identical pieces at 200 and
    400 tokens, the change consumed)."""
    prefix = ("the Business plan, billed yearly to the Milan office, with priority support, "
              "single sign-on, audit export and a dedicated account manager, for ") * 3
    _memories(db, tenant, default, monkeypatch, 3)
    old = fact(db, tenant, default, obj=prefix + "60 seats", subject="Acme")
    read_marks.mark(tenant.id, default.id, door, [old.id])
    replaced(db, tenant, default, old, obj=prefix + "50 seats", model="direct")
    if door == "rest":
        block = ask(client, key, reader=door, token_budget=budget)["volatile"]
    else:
        block = mcp(key, {"query": QUESTION, "user_id": "u1", "reader": door,
                          "token_budget": budget})
    (line,) = _section(block)
    was, _, now = line.partition(" -> now: ")
    assert "60 seats" in was and "50 seats" in now and "…" in line, line


def test_the_block_looks_at_the_twenty_newest(db, tenant, default):
    """The block looks at the 20 newest waiting changes (and counts them
    all): enough to pass over the ones even their short form does not fit,
    not all of them."""
    from korely_agent.services.facts import changes as notices

    olds = [fact(db, tenant, default, obj=f"place {i}", subject=f"P{i}") for i in range(25)]
    read_marks.mark(tenant.id, default.id, "bot", [f.id for f in olds])
    for i, f in enumerate(olds):
        replaced(db, tenant, default, f, obj=f"town {i}", minutes=i)
    pending = notices.for_a_read(tenant.id, default.id, "bot", "u1")
    assert len(pending.pending) == ctx.CHANGES_LOOKED_AT == 20 and pending.waiting == 25
    assert [c["fact_id"] for c in pending.pending] == \
        [f"fct_{f.id.hex}" for f in olds[::-1][:20]], "newest first"


def test_the_section_is_the_readers_whatever_the_blocks_agent(client, db, tenant, default, key):
    """`agent_id` narrows what the block reads, `reader` names who is told:
    the reader is told every change of what it was handed, by a read of any
    namespace or of none, and of what it was told holds now, whoever wrote
    it (review of the corrections, 9 Nov 2026: a section that followed
    agent_id told, and counted, none of them)."""
    head = {"Authorization": f"Bearer {key}"}
    theirs = fact(db, tenant, default, agent="billing", obj="Acme", subject="the plan")
    fact(db, tenant, default, agent="support", obj="Rome")
    assert client.get("/v1/facts", params={"user_id": "u1", "reader": "support"},
                      headers=head).status_code == 200
    replaced(db, tenant, default, theirs, obj="Globex")
    first = ask(client, key, reader="support", agent_id="support")
    assert [c["fact_id"] for c in first["changed"]] == [f"fct_{theirs.id.hex}"]
    globex = first["changed"][0]["now"]["fact_id"]
    newer = replaced(db, tenant, default, db.get(AgentFact, uuid.UUID(globex[4:])), obj="Initech")
    second = ask(client, key, reader="support", agent_id="support")
    assert [(x["fact_id"], x["now"]["fact_id"]) for x in second["changed"]] == \
        [(globex, f"fct_{newer.id.hex}")], "what it was told holds now, in its turn"


# ── the MCP door passes what REST passes (review of the corrections, 9 Nov) ──
#
# Every argument of the MCP's look at the changes, held by a twin of its REST
# test: a mutant that dropped one of them (the key's end users, known_as_of,
# changes=all) lived on the MCP side. And the one it must not pass, agent_id
# (the twin of test_the_section_is_the_readers_whatever_the_blocks_agent):
# the MCP door passed it until the same review, and no test of the MCP said
# what a reader with an agent_id is told.

def test_the_mcp_door_keeps_the_keys_end_users(client, db, tenant, default):
    """A key confined to u1, no user_id: u2's change is neither told nor in
    the text, on REST and on the MCP alike."""
    a = fact(db, tenant, default, user="u1", obj="Rome")
    b = fact(db, tenant, default, user="u2", obj="Turin", subject="Luca")
    for r in ("rest", "mcp"):
        read_marks.mark(tenant.id, default.id, r, [a.id, b.id])
    replaced(db, tenant, default, a, obj="Milan")
    replaced(db, tenant, default, b, obj="Genoa", minutes=1)
    _row, confined = key_service.create_api_key(db, tenant_id=tenant.id, name="c",
                                                end_users=["u1"])
    rest = client.get("/v1/context", params={"query": QUESTION, "reader": "rest"},
                      headers={"Authorization": f"Bearer {confined}"}).json()
    said = mcp(confined, {"query": QUESTION, "reader": "mcp"})
    for lines, text in ((_section(rest["volatile"]), rest["context"]), (_section(said), said)):
        assert len(lines) == 1 and "Rome" in lines[0], lines
        assert "Turin" not in text and "Genoa" not in text
    assert rest["changed_more"] == 0


def test_the_mcp_door_keeps_a_block_of_the_past_silent(db, tenant, default, key):
    """known_as_of on the MCP: nothing told, nothing reserved; the next block
    of now tells it."""
    old = fact(db, tenant, default)
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    replaced(db, tenant, default, old, obj="Milan")
    past = mcp(key, {"query": QUESTION, "user_id": "u1", "reader": "bot",
                     "known_as_of": datetime.now(timezone.utc).isoformat()})
    assert ctx.CHANGED_HEADING not in past
    assert len(_section(mcp(key, {"query": QUESTION, "user_id": "u1", "reader": "bot"}))) == 1


def test_the_mcp_door_takes_changes_all(db, tenant, default, key):
    """changes=all on the MCP, in an agent's memory with no end user: the
    closures read from memories too, as on REST."""
    judged = fact(db, tenant, default, user=None, agent="planner", obj="Rome")
    stated = fact(db, tenant, default, user=None, agent="planner", obj="Acme",
                  subject="the release")
    for r in ("default", "all"):
        read_marks.mark(tenant.id, default.id, r, [judged.id, stated.id])
    replaced(db, tenant, default, judged, obj="Milan")
    replaced(db, tenant, default, stated, obj="Globex", model="direct")
    assert len(_section(mcp(key, {"query": QUESTION, "reader": "default"}))) == 1
    assert len(_section(mcp(key, {"query": QUESTION, "reader": "all", "changes": "all"}))) == 2


def test_the_mcp_door_tells_the_reader_whatever_the_blocks_agent(client, db, tenant,
                                                                default, key):
    """agent_id on the MCP narrows what the block reads, never what its
    reader is told, as on REST: the change of a fact the reader was handed
    outside that namespace, then of what it was told holds now, both
    written by another agent."""
    theirs = fact(db, tenant, default, agent="billing", obj="Acme", subject="the plan")
    fact(db, tenant, default, agent="support", obj="Rome")
    assert client.get("/v1/facts", params={"user_id": "u1", "reader": "support"},
                      headers={"Authorization": f"Bearer {key}"}).status_code == 200
    globex = replaced(db, tenant, default, theirs, obj="Globex")
    args = {"query": QUESTION, "user_id": "u1", "reader": "support", "agent_id": "support"}
    (first,) = _section(mcp(key, args)) or [None]
    assert first and first.startswith("- was: the plan lives in Acme") \
        and "-> now: the plan lives in Globex" in first, first
    replaced(db, tenant, default, globex, obj="Initech")
    (second,) = _section(mcp(key, args)) or [None]
    assert second and second.startswith("- was: the plan lives in Globex") \
        and "-> now: the plan lives in Initech" in second, \
        "what it was told holds now, in its turn"


# ── the last minor findings (review of the corrections, 9 Nov) ─────────────

def test_the_sections_ids_head_the_sources(client, db, tenant, default, key):
    """The ids the section shows come first in `sources`, before the known
    facts', in the order of the block."""
    kept = fact(db, tenant, default, obj="Acme", subject="the plan")
    old = fact(db, tenant, default)
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    new = replaced(db, tenant, default, old, obj="Milan")
    out = ask(client, key, reader="bot")
    assert out["sources"][:2] == [f"fct_{old.id.hex}", f"fct_{new.id.hex}"]
    assert f"fct_{kept.id.hex}" in out["sources"][2:]


def test_a_block_whose_facts_degraded_tells_with_its_memories(client, db, tenant, default, key,
                                                             monkeypatch):
    """The facts degraded and none to show, the memories there: the read
    worked, it tells its changes, with its NOTE (a read failed only when
    neither facts nor memories came)."""
    _memories(db, tenant, default, monkeypatch, 2)
    old = fact(db, tenant, default)
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    old.invalid_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    monkeypatch.setattr(ctx, "_rank_facts", lambda *a, **k: ([], False))
    out = ask(client, key, reader="bot")
    assert out["degraded_parts"] == ["facts"] and len(out["changed"]) == 1
    assert "NOTE" in out["context"] and ctx.MEMORIES_HEADING in out["context"]


@pytest.mark.parametrize("door", ["rest", "mcp"])
def test_the_hourly_purge_between_reserving_and_marking_tells_nothing_twice(
        client, db, tenant, default, key, monkeypatch, door):
    """The purge takes a told mark; the read that told it then marks only
    what it handed over as true, never the fact it told the reader had, so
    the change is not told again (review of the corrections: told twice)."""
    from korely_agent.api import context as api_context
    from korely_agent.db import get_sessionmaker
    from korely_agent.db.models import AgentReadMark

    old = fact(db, tenant, default)
    read_marks.mark(tenant.id, default.id, "bot", [old.id])
    replaced(db, tenant, default, old, obj="Milan")

    def purge():
        s = get_sessionmaker()()
        try:
            read_marks.purge_expired(s, 90)
        finally:
            s.close()

    where = (api_context, "log_v1_request") if door == "rest" else (mcp_server, "_audit")
    real = getattr(*where)

    def purging(*a, **k):
        purge()
        return real(*a, **k)

    monkeypatch.setattr(*where, purging)
    if door == "rest":
        assert len(ask(client, key, reader="bot")["changed"]) == 1
    else:
        assert len(_section(mcp(key, {"query": QUESTION, "user_id": "u1", "reader": "bot"}))) == 1
    monkeypatch.setattr(*where, real)
    db.expire_all()
    assert db.query(AgentReadMark).filter(AgentReadMark.fact_id == old.id).count() == 0
    assert ask(client, key, reader="bot")["changed"] == []


def test_a_change_written_short_stays_short_when_another_was_not_reserved(
        client, db, tenant, default, key, monkeypatch):
    """Two changes packed, the long one written short; another answer took
    the short one at the same moment: the block keeps the line as it was
    packed, short, inside the budget."""
    long = fact(db, tenant, default, obj=("a very long name of a place " * 17)[:480],
                subject="L")
    short = fact(db, tenant, default, obj="S", subject="Q")
    for r in ("bot", "both"):
        read_marks.mark(tenant.id, default.id, r, [long.id, short.id])
    replaced(db, tenant, default, short, obj="T", minutes=-5)
    replaced(db, tenant, default, long, obj=("another long place " * 26)[:490])
    both = ask(client, key, reader="both", token_budget=200, agent_id="nobody")
    assert len(both["changed"]) == 2, "the short line left room for the next one"
    real = read_marks.claim
    monkeypatch.setattr(read_marks, "claim", lambda t, p, r, ids: real(t, p, r, ids[:1]))
    out = ask(client, key, reader="bot", token_budget=200, agent_id="nobody")
    assert [c["fact_id"] for c in out["changed"]] == [f"fct_{long.id.hex}"]
    (line,) = _section(out["volatile"])
    assert "…" in line and out["tokens"] <= 200


# ── the fifth review of piece 3 (9 Nov 2026) ────────────────────────────────

# A difference inside one character (as a reader sees it) of some 300 bytes,
# a letter and 150 accents, after a common beginning: a change of such facts
# is told short only in a room of some 700 bytes (the skeptic's probe W2).
_BIG = "a" + "́" * 150
_HIDDEN_WAS = "x" * 100 + _BIG + "̀"
_HIDDEN_NOW = "x" * 100 + _BIG + "̂"


def _hidden_ahead_of_short_ones(db, tenant, default, hidden: int, shorts: int = 2,
                               alone: str = "", tied: bool = False):
    """`hidden` changes whose words cannot be told in a small room, newer than
    `shorts` short ones, all of facts the reader "bot" was handed; the
    reader `alone`, when named, was handed the short ones only. `tied`: all
    learned at the same moment (one transaction closes many facts), the
    short ones last by their ids, so the order is the ids' alone."""
    olds = [fact(db, tenant, default, obj=f"S{i}", subject=f"Q{i}",
                 fid=uuid.UUID(int=(1 << 128) - 1 - i) if tied else None) for i in range(shorts)]
    if alone:
        read_marks.mark(tenant.id, default.id, alone, [f.id for f in olds])
    us = [AgentFact(id=uuid.UUID(int=i + 1) if tied else uuid.uuid4(), tenant_id=tenant.id,
                    project_id=default.id, end_user_id="u1",
                    subject_label=f"U{i}", predicate_raw="lives in", predicate_canonical="lives_in",
                    object_label=_HIDDEN_WAS + str(i), valid_from=START, model_used="gemini-test",
                    embedding=VECTOR, embedding_status=vectors.DONE) for i in range(hidden)]
    db.add_all(us)
    db.commit()
    read_marks.mark(tenant.id, default.id, "bot", [f.id for f in olds + us])
    for i, f in enumerate(olds):
        replaced(db, tenant, default, f, obj=f"T{i}", minutes=-30 - i)
    news = [AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=default.id, end_user_id="u1",
                      subject_label=f.subject_label, predicate_raw="lives in",
                      predicate_canonical="lives_in", object_label=_HIDDEN_NOW + str(i),
                      valid_from=START, model_used="gemini-test",
                      embedding=VECTOR[:-1] + [0.9], embedding_status=vectors.DONE)
            for i, f in enumerate(us)]
    db.add_all(news)
    db.commit()
    ended = datetime.now(timezone.utc) - timedelta(seconds=1)
    for f, new in zip(us, news):
        f.invalid_at, f.invalidated_by_id = ended, new.id
    db.commit()
    moment = datetime.now(timezone.utc)
    for i, f in enumerate(us):
        f.closed_known_at = moment if tied else moment + timedelta(minutes=i)
    if tied:
        for f in olds:
            f.closed_known_at = moment
    db.commit()
    return olds, us


def _count_lookups(monkeypatch):
    from korely_agent.services.facts import changes as notices

    calls = []
    real = notices._lookup
    monkeypatch.setattr(notices, "_lookup", lambda *a, **k: calls.append(k.get("after")) or
                        real(*a, **k))
    return calls


@pytest.mark.parametrize("budget", [200, 300, 400])
@pytest.mark.parametrize("world", ["u1", "nobody", "tied"])
def test_changes_whose_words_cannot_be_told_keep_no_older_one_out(
        client, db, tenant, default, key, monkeypatch, budget, world):
    """Twenty-two changes that cannot be told short in the block's room,
    newer than two short ones, in a block with memories (and the end user's
    facts, or another namespace's block, or all learned at one moment, so
    that the lookup's pages part on the ids): the first read tells the short
    ones the block of a reader handed those two alone tells, the others
    still wait and are counted, with one lookup more (the fifth review of
    piece 3: the block looked at the twenty newest only, and told nothing
    in 25 reads at 200 to 400 tokens)."""
    _memories(db, tenant, default, monkeypatch, 3)
    if world == "u1":
        for i in range(4):
            fact(db, tenant, default, obj=f"Acme {i}", subject=f"K{i}")
    shorts, us = _hidden_ahead_of_short_ones(db, tenant, default, 22, alone="alone",
                                             tied=world == "tied")
    kw = {"agent_id": "nobody"} if world == "nobody" else {}
    calls = _count_lookups(monkeypatch)
    out = ask(client, key, reader="bot", token_budget=budget, **kw)
    assert len(calls) == 2 and calls[0] is None and calls[1] is not None, calls
    alone = ask(client, key, reader="alone", token_budget=budget, **kw)
    told = {c["fact_id"] for c in out["changed"]}
    assert told and told == {c["fact_id"] for c in alone["changed"]}, (out["changed"], budget)
    assert out["changed_more"] == 24 - len(told) and out["tokens"] <= budget
    assert ctx.MEMORIES_HEADING in out["context"]


def test_the_block_looks_at_most_at_the_changes_any_read_returns(client, db, tenant, default,
                                                                 key, monkeypatch):
    """Past MAX_CHANGES (200) changes that cannot be told in the room, a
    short older one waits, and is counted: the block looks no further than
    any read of the changes returns (a limit, written in the guide)."""
    from korely_agent.services.facts.changes import MAX_CHANGES

    _memories(db, tenant, default, monkeypatch, 3)
    shorts, us = _hidden_ahead_of_short_ones(db, tenant, default, MAX_CHANGES, shorts=1)
    out = ask(client, key, reader="bot", token_budget=300)
    assert out["changed"] == [] and out["changed_more"] == MAX_CHANGES + 1


def test_the_block_looks_further_when_the_room_left_holds_the_smallest_line(client):
    """After the first pass, the changes that wait after the ones looked at
    are asked for once when a place is free, a change was passed over and
    the room left holds the smallest line a change can take
    (`_smallest_line`: a change that no longer holds, with no words); never
    when the room left is smaller, when the places are taken, or when none
    was passed over (the sixth review of piece 3: the rule asked for room
    for two fragments of the change passed over, and a short line that
    fitted in less waited for good behind 20 long ones)."""
    town = "Q0 lives in a quiet street of the old town, number "
    long = {**_short_change(0), "was": town + "12" * 200, "now": {"fact_id": "fct_t0",
                                                                  "text": town + "14" * 200}}
    older = {"fact_id": "fct_o", "was": "Q9 lives in S9", "now": None,
             "known_at": "2026-11-09", "origin": "corrected"}
    least = ctx._smallest_line([long])
    assert least == ctx._cost("- no longer true:  (2026-11-09, forgotten)")
    assert ctx._cost(ctx.change_line(older)) > least

    def packed(changes, room, more):
        asked = []
        b = ctx._Block(800)
        b.pack.used = b.pack.cap - room
        lines, told = ctx._pack_changes(b, changes, b.pack.cap,
                                        further=lambda: asked.append(1) or more)
        return [c["fact_id"] for c in told], len(asked)

    head = 1 + ctx._cost(ctx.CHANGED_HEADING) + ctx._cost(ctx._SEPARATOR)
    fits = head + ctx._cost(ctx.change_line(older))          # the older one whole
    assert ctx.change_line(long, most=fits - head) is None, "the long one cannot be told there"
    assert packed([long], fits, [older]) == (["fct_o"], 1), "looked further, and told it"
    assert packed([long], head + least - 1, [older]) == ([], 0), "less than the smallest line"
    three = [_short_change(i) for i in range(3)]
    assert packed(three + [long], 2000, [older])[1] == 0, "the places are taken"
    assert packed(three[:2], 2000, [older]) == (["fct_s0", "fct_s1"], 0), "none passed over"


def test_a_full_section_looks_no_further(client, db, tenant, default, key, monkeypatch):
    """Twenty-five short changes in a block of 800 tokens with facts and
    memories: three are told, with the one lookup of a read."""
    _memories(db, tenant, default, monkeypatch, 3)
    for i in range(4):
        fact(db, tenant, default, obj=f"Acme {i}", subject=f"K{i}")
    olds = [fact(db, tenant, default, obj=f"S{i}", subject=f"Q{i}") for i in range(25)]
    read_marks.mark(tenant.id, default.id, "bot", [f.id for f in olds])
    for i, f in enumerate(olds):
        replaced(db, tenant, default, f, obj=f"T{i}", minutes=i)
    calls = _count_lookups(monkeypatch)
    out = ask(client, key, reader="bot", token_budget=800)
    assert len(out["changed"]) == 3 and out["changed_more"] == 22
    assert len(calls) == 1, calls


@pytest.mark.parametrize("shorts", ["replaced", "no longer true"])
@pytest.mark.parametrize("world", ["u1", "nobody"])
def test_long_changes_ahead_keep_no_short_one_out_at_any_budget(
        client, db, tenant, default, key, monkeypatch, shorts, world):
    """Twenty-two ordinary long changes (objects of 480 characters) ahead of
    two short ones, in a block with memories (and the end user's facts, or
    another namespace's block), at every budget from 100 to 450 tokens two
    by two: the first read tells at least as many changes as the block of a
    reader handed the two short ones alone, inside its budget (the sixth
    review of piece 3: at 164 to 174 tokens, and 120 to 130 with no facts,
    that reader was told one and this one none, for good)."""
    _memories(db, tenant, default, monkeypatch, 3)
    if world == "u1":
        for i in range(4):
            fact(db, tenant, default, obj=f"Acme {i}", subject=f"K{i}")
    budgets = list(range(100, 452, 2))
    olds = [fact(db, tenant, default, obj=f"S{i}", subject=f"Q{i}") for i in range(2)]
    longs = [fact(db, tenant, default, obj=LONG_WAS + str(i), subject=f"L{i}") for i in range(22)]
    for b in budgets:
        read_marks.mark(tenant.id, default.id, f"bot{b}", [f.id for f in olds + longs])
        read_marks.mark(tenant.id, default.id, f"alone{b}", [f.id for f in olds])
    for i, f in enumerate(olds):
        if shorts == "replaced":
            replaced(db, tenant, default, f, obj=f"T{i}", minutes=-30 - i)
        else:
            f.invalid_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
            f.closed_known_at = datetime.now(timezone.utc) - timedelta(minutes=30 + i)
            db.commit()
    for i, f in enumerate(longs):
        now = replaced(db, tenant, default, f, obj=LONG_NOW + str(i), minutes=i)
        now.embedding = VECTOR[:-1] + [0.9]
    db.commit()
    kw = {"agent_id": "nobody"} if world == "nobody" else {}
    fewer = []
    for b in budgets:
        bot = ask(client, key, reader=f"bot{b}", token_budget=b, **kw)
        alone = ask(client, key, reader=f"alone{b}", token_budget=b, **kw)
        assert bot["tokens"] <= b and ctx._cost(bot["context"]) <= b * ctx.BYTES_PER_TOKEN, b
        if len(bot["changed"]) < len(alone["changed"]):
            fewer.append((b, len(bot["changed"]), len(alone["changed"])))
    assert fewer == [], fewer


@pytest.mark.parametrize("world", ["memories", "none"])
def test_a_section_made_by_the_second_pass_alone_keeps_the_budget(
        client, db, tenant, default, key, monkeypatch, world):
    """Two long changes that do not fit their share and one that cannot be
    told even alone: the only line comes from the second pass, which pays
    the section's heading, so the block keeps its budget at every one from
    100 to 410 tokens (the skeptic's probe H1: without the heading, 630
    bytes for 600 at 150 tokens)."""
    if world == "memories":
        _memories(db, tenant, default, monkeypatch, 3)
    budgets = list(range(100, 420, 10))
    u = fact(db, tenant, default, obj=_HIDDEN_WAS, subject="U")
    longs = [fact(db, tenant, default, obj=LONG_WAS + str(i), subject=f"L{i}") for i in range(2)]
    for b in budgets:
        read_marks.mark(tenant.id, default.id, f"r{b}", [f.id for f in [u] + longs])
    replaced(db, tenant, default, u, obj=_HIDDEN_NOW, minutes=-30)
    for i, f in enumerate(longs):
        replaced(db, tenant, default, f, obj=LONG_NOW + str(i), minutes=i)
    told = 0
    for b in budgets:
        out = ask(client, key, reader=f"r{b}", token_budget=b, agent_id="nobody")
        assert out["tokens"] <= b and ctx._cost(out["context"]) <= b * ctx.BYTES_PER_TOKEN, b
        told += bool(out["changed"])
    assert told > 10, "the budgets where a line is told"


def _fuzz_pairs():
    """Pairs of facts that begin the same, seeded: an emoji family with skin
    tones, a flag, a keycap, marks, CJK, Devanagari, spaces, at the end, in
    the middle, at the start, one the beginning of the other."""
    import random

    rnd = random.Random(9112026)
    alphabet = (list("aeZ ,.") + ["\xe9", "́", "̀", "ि", "्", "क",
                                   "ष", "ก", "ำ", "‍", "️", "\U0001F468",
                                   "\U0001F469", "\U0001F467", "❤", "\U0001F3FB",
                                   "\U0001F1EE", "\U0001F1F9", "⃣", "1", "東", "ا",
                                   "ᄀ", "ᅡ", "ᆨ", "가"])
    family = "\U0001F468\U0001F3FB‍\U0001F469\U0001F3FB‍\U0001F467\U0001F3FB"

    def word(k):
        return "".join(rnd.choice(alphabet) for _ in range(k))

    out = []
    for _ in range(25):
        base = word(rnd.randint(20, 160))
        out += [(base + word(3), base + word(4)), (word(2) + base, word(3) + base),
                (base + family + word(5), base + family[:-2] + "\U0001F466\U0001F3FB" + word(5)),
                (base, base + word(rnd.randint(1, 30))), (base + " x" + base, base + "  x" + base)]
    return out


def test_a_room_large_enough_stays_large_enough_on_a_fuzz():
    """125 seeded pairs that begin the same: once a change is told short in
    a room, it is told in every larger room up to the whole line, never in
    more bytes than the room, and its two pieces never read the same (the
    fifth review of piece 3: 35 pairs of 720 had a room told and one 1 to 8
    bytes larger not, most with emoji)."""
    for was, now in _fuzz_pairs():
        change = {"fact_id": "fct_1", "was": was, "now": {"fact_id": "fct_2", "text": now},
                  "known_at": "2026-11-09T10:00:00", "origin": "corrected"}
        whole = ctx._cost(ctx.change_line(change))
        rooms = []
        for most in range(60, whole):
            line = ctx.change_line(change, most=most)
            if line is None:
                continue
            rooms.append(most)
            assert ctx._cost(line) <= most
            a, b = _pieces(line)
            assert a != b, (ascii(was[:30]), most)
        assert rooms == list(range(rooms[0], whole)) if rooms else whole <= 140, \
            (ascii(was[:40]), ascii(now[:40]), rooms[:5])


# ── the sixth review of piece 3 (9 Nov 2026): the second lookup's walls,
# kinds and packing ──────────────────────────────────────────────────────

def _end_with(db, f, now_obj, known, *, model="gemini-test", vec=None):
    """End `f`, replaced by a fact of `now_obj` (its user, agent and project)
    written by `model`, learned at `known`, with the vector `vec` (by
    default one less relevant to the question than the facts')."""
    new = AgentFact(id=uuid.uuid4(), tenant_id=f.tenant_id, project_id=f.project_id,
                    end_user_id=f.end_user_id, agent_id=f.agent_id, subject_label=f.subject_label,
                    predicate_raw="lives in", predicate_canonical="lives_in", object_label=now_obj,
                    valid_from=START, model_used=model, embedding=vec or VECTOR[:-1] + [0.9],
                    embedding_status=vectors.DONE)
    db.add(new)
    db.commit()
    f.invalid_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    f.invalidated_by_id = new.id
    db.commit()
    f.closed_known_at = known
    db.commit()


@pytest.mark.parametrize("door", ["u1", "confined", "project", "every"])
def test_the_second_lookup_keeps_the_walls_of_the_first(client, db, tenant, default, key,
                                                       monkeypatch, door):
    """Twenty-two changes that cannot be told in the room, ahead of short
    ones of u1, of u2, of no end user, and of a fact of another project
    (its mark written under this one by hand, so the fact's own wall is what
    keeps it out): what the second lookup brings is what a reader handed
    the short ones alone is told, behind the same walls: the block's end
    user, a key confined to u1, a key of another project, every end user
    (the sixth review of piece 3: a second lookup of every end user told u1
    the change of u2, its words with it, and no test saw it)."""
    from korely_agent.db.models import AgentReadMark

    _memories(db, tenant, default, monkeypatch, 3)
    other = project_service.create_project(db, tenant_id=tenant.id, name="Other")
    home, away = (other, default) if door == "project" else (default, other)
    hidden = [fact(db, tenant, home, obj=_HIDDEN_WAS + str(i), subject=f"U{i}") for i in range(22)]
    s_u1 = fact(db, tenant, home, obj="S-u1", subject="Q-u1")
    s_u2 = fact(db, tenant, home, obj="S-u2", subject="Q-u2", user="u2")
    s_none = fact(db, tenant, home, obj="S-none", subject="Q-none", user=None)
    s_away = fact(db, tenant, away, obj="S-away", subject="Q-away")
    shorts = [s_u1, s_u2, s_none, s_away]
    for reader, ids in (("bot", hidden + shorts), ("alone", shorts)):
        for f in ids:
            db.add(AgentReadMark(tenant_id=tenant.id, project_id=home.id, reader=reader,
                                 fact_id=f.id, end_user_id=f.end_user_id))
    db.commit()
    base = datetime.now(timezone.utc)
    for i, f in enumerate([s_u2, s_away, s_none, s_u1]):
        _end_with(db, f, f"T-{i}", base - timedelta(minutes=30 + i))
    for i, f in enumerate(hidden):
        _end_with(db, f, _HIDDEN_NOW + str(i), base + timedelta(minutes=i))
    if door == "confined":
        _row, k = key_service.create_api_key(db, tenant_id=tenant.id, name="c", end_users=["u1"])
    elif door == "project":
        _row, k = key_service.create_api_key(db, tenant_id=tenant.id, name="p", project_id=other.id)
    else:
        k = key
    calls = _count_lookups(monkeypatch)

    def read(reader):
        q = {"query": QUESTION, "reader": reader, "token_budget": 400}
        if door != "every":
            q["user_id"] = "u1"
        r = client.get("/v1/context", params=q, headers={"Authorization": f"Bearer {k}"})
        assert r.status_code == 200, r.text
        return r.json()

    bot = read("bot")
    assert len(calls) == 2, "the second lookup was asked for"
    alone = read("alone")
    assert alone["changed"], "the reader of the short ones alone is told some"
    assert [(c["fact_id"], c["was"], c["now"]) for c in bot["changed"]] == \
        [(c["fact_id"], c["was"], c["now"]) for c in alone["changed"]]
    told = {c["fact_id"] for c in bot["changed"]}
    assert f"fct_{s_away.id.hex}" not in told
    if door != "every":
        assert told == {f"fct_{s_u1.id.hex}"}, (door, told)


def test_the_second_lookup_tells_the_kinds_of_the_first(client, db, tenant, default, key,
                                                       monkeypatch):
    """In an agent's memory with no end user only the explicit changes are
    told by default: behind twenty-two changes stated by a caller that
    cannot be told in the room, the second lookup brings the change stated
    by a caller and not the newer one read from a memory, as a reader handed
    those two alone is told (the sixth review of piece 3: a second lookup of
    every kind lived)."""
    _memories(db, tenant, default, monkeypatch, 3)
    base = datetime.now(timezone.utc)
    hidden = [fact(db, tenant, default, obj=_HIDDEN_WAS + str(i), subject=f"U{i}", user=None,
                   agent="planner") for i in range(22)]
    judged = fact(db, tenant, default, obj="S-judged", subject="Q-judged", user=None,
                  agent="planner")
    stated = fact(db, tenant, default, obj="S-stated", subject="Q-stated", user=None,
                  agent="planner")
    read_marks.mark(tenant.id, default.id, "bot", [f.id for f in hidden + [judged, stated]])
    read_marks.mark(tenant.id, default.id, "alone", [judged.id, stated.id])
    _end_with(db, judged, "T-judged", base - timedelta(minutes=30))
    _end_with(db, stated, "T-stated", base - timedelta(minutes=40), model="direct")
    for i, f in enumerate(hidden):
        _end_with(db, f, _HIDDEN_NOW + str(i), base + timedelta(minutes=i), model="direct")
    calls = _count_lookups(monkeypatch)

    def read(reader):
        r = client.get("/v1/context", params={"query": QUESTION, "reader": reader,
                                              "token_budget": 400, "agent_id": "planner"},
                       headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 200, r.text
        return r.json()

    bot = read("bot")
    assert len(calls) == 2, "the second lookup was asked for"
    alone = read("alone")
    assert [c["fact_id"] for c in bot["changed"]] == [c["fact_id"] for c in alone["changed"]] \
        == [f"fct_{stated.id.hex}"]
    assert bot["changed_more"] == 22


@pytest.mark.parametrize("budget", [250, 300, 400, 600])
@pytest.mark.parametrize("world", ["u1", "nobody"])
def test_a_block_that_looked_further_is_the_one_of_its_short_changes(
        client, db, tenant, default, key, monkeypatch, budget, world):
    """A short change newer than nineteen that cannot be told in the room,
    and an older short one behind them, past the first twenty: the block,
    which looked further, is the block of a reader handed the two short ones
    alone, byte for byte, the first page's change with the one the second
    lookup brought, in their order (at 400 tokens with the end user's facts
    the room holds the first one only, in both blocks) (the sixth review of
    piece 3: a packing again that kept the first one's count, that packed
    the further ones alone, or put them first, lived)."""
    _memories(db, tenant, default, monkeypatch, 3)
    if world == "u1":
        for i in range(4):
            fact(db, tenant, default, obj=f"Acme {i}", subject=f"K{i}")
    s_new = fact(db, tenant, default, obj="S-new", subject="Q-new")
    s_old = fact(db, tenant, default, obj="S-old", subject="Q-old")
    hidden = [fact(db, tenant, default, obj=_HIDDEN_WAS + str(i), subject=f"U{i}")
              for i in range(19)]
    read_marks.mark(tenant.id, default.id, "bot", [f.id for f in [s_new, s_old] + hidden])
    read_marks.mark(tenant.id, default.id, "alone", [s_new.id, s_old.id])
    base = datetime.now(timezone.utc)
    _end_with(db, s_new, "T-new", base + timedelta(minutes=60))
    _end_with(db, s_old, "T-old", base - timedelta(minutes=60))
    for i, f in enumerate(hidden):
        # Less relevant still than what the short ones hold now: the ten known
        # facts are the same at every run (ties fall by the random ids).
        _end_with(db, f, _HIDDEN_NOW + str(i), base + timedelta(minutes=i),
                  vec=VECTOR[:-2] + [0.9, 0.9])
    kw = {"agent_id": "nobody"} if world == "nobody" else {}
    calls = _count_lookups(monkeypatch)
    bot = ask(client, key, reader="bot", token_budget=budget, **kw)
    assert len(calls) == 2, "the second lookup was asked for"
    alone = ask(client, key, reader="alone", token_budget=budget, **kw)
    told = [c["fact_id"] for c in bot["changed"]]
    assert told == [c["fact_id"] for c in alone["changed"]]
    assert told == [f"fct_{s_new.id.hex}", f"fct_{s_old.id.hex}"][:len(told)] and told
    assert bot["context"] == alone["context"] and bot["changed_more"] == 21 - len(told)
    if (world, budget) != ("u1", 400):
        assert len(told) == 2, "both, the second from the second lookup"
