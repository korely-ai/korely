"""DIALOGUE_ORDER: the whole memories of one exchange in the order said (2026-10-08).

context_experiments.DIALOGUE_ORDER, bench-only until measured (an
analysis of the full LoCoMo blocks): the first WHOLE memories are the same,
chosen by relevance; consecutive turns of one end user among them are printed
as they were said, at the rank of the best of them. Off, the block is the one
of before, byte for byte.
"""
import math
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services import context as ctx
from korely_agent.services import context_experiments as experiments
from korely_agent.services import vectors
from tests.conftest import seeded_project_id


T0 = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)


def vec(cos):
    v = [0.0] * EMBEDDING_DIM
    v[0], v[1] = cos, math.sqrt(1 - cos * cos)
    return v


def memory(db, tenant, text, cos, minutes, user="maria"):
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                    end_user_id=user, content=text, embedding=vec(cos),
                    embedding_status=vectors.DONE, created_at=T0 + timedelta(minutes=minutes))
    db.add(m)
    db.commit()
    return m


def block(db, tenant):
    return ctx.assemble_context(db, tenant.id, query="Which posters?", end_user_id="maria",
                                token_budget=2000)


@pytest.fixture(autouse=True)
def query_vector(monkeypatch):
    q = [0.0] * EMBEDDING_DIM
    q[0] = 1.0
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: q)


def order(text, *needles):
    return sorted(needles, key=text.index)


@pytest.fixture
def exchange(db, tenant):
    """Three consecutive turns of Maria, the last one the most like the question."""
    memory(db, tenant, "user: I painted three posters for the parade.", 0.6, 0)
    memory(db, tenant, "user: The rainbow one took me a week.", 0.5, 1)
    memory(db, tenant, "user: Those posters are what I am proudest of.", 0.9, 2)
    memory(db, tenant, "user: My brother lives in Turin.", 0.7, 30, user="maria")


def test_off_the_relevance_order(db, tenant, exchange, monkeypatch):
    assert experiments.DIALOGUE_ORDER is True, "on since 0.1.23"
    monkeypatch.setattr(experiments, "DIALOGUE_ORDER", False)
    out = block(db, tenant)["volatile"]
    assert order(out, "proudest", "Turin", "three posters", "rainbow") == \
        ["proudest", "Turin", "three posters", "rainbow"]


def test_on_one_exchange_is_printed_as_it_was_said(db, tenant, exchange, monkeypatch):
    monkeypatch.setattr(experiments, "DIALOGUE_ORDER", True)
    out = block(db, tenant)["volatile"]
    # Turin, at minute 30, has nothing of Maria's written between it and the
    # exchange, so it is the run's next turn; the run goes at the rank of its
    # best member ("proudest", the first by relevance).
    assert order(out, "three posters", "rainbow", "proudest", "Turin") == \
        ["three posters", "rainbow", "proudest", "Turin"]


def test_a_turn_written_in_between_breaks_the_run(db, tenant, monkeypatch):
    """A turn the search did not bring (no vector yet) still sits between the
    two: they are not one run, and keep the relevance order."""
    memory(db, tenant, "user: I painted three posters for the parade.", 0.6, 0)
    db.add(AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                       end_user_id="maria", content="user: Something about the weather.",
                       created_at=T0 + timedelta(minutes=1)))
    db.commit()
    memory(db, tenant, "user: Those posters are what I am proudest of.", 0.9, 2)
    monkeypatch.setattr(experiments, "DIALOGUE_ORDER", True)
    out = block(db, tenant)["volatile"]
    assert order(out, "proudest", "three posters") == ["proudest", "three posters"]


def test_another_end_users_turns_never_join(db, tenant, monkeypatch):
    memory(db, tenant, "user: I painted three posters for the parade.", 0.6, 0)
    memory(db, tenant, "user: Those posters are what I am proudest of.", 0.9, 2)
    memory(db, tenant, "user: Luca's turn, written in between.", 0.1, 1, user="luca")
    monkeypatch.setattr(experiments, "DIALOGUE_ORDER", True)
    out = block(db, tenant)["volatile"]
    assert order(out, "three posters", "proudest") == ["three posters", "proudest"]
    assert "Luca's turn" not in out


def test_the_only_difference_is_the_order(db, tenant, exchange, monkeypatch):
    monkeypatch.setattr(experiments, "DIALOGUE_ORDER", False)
    off = block(db, tenant)
    monkeypatch.setattr(experiments, "DIALOGUE_ORDER", True)
    on = block(db, tenant)
    assert on["stable"] == off["stable"]
    assert sorted(on["sources"]) == sorted(off["sources"])
    assert sorted(on["volatile"].splitlines()) == sorted(off["volatile"].splitlines())


# ---------------------------------------------------------------- 1.0.22
# A run is one end user's exchange: a memory with no end user, an agent's
# notes or an assistant's through MCP with no ids, is in no run and keeps the
# search's order (memory_core `owner.keeps_to_its_subject`, the judge's rule
# for the same reason). The team's memory of 13 Nov 2026 had the note just
# written printed behind the one written before it. Each test sets the switch
# itself: what it proves is the switch on.

LESSON_A = ("Lesson of the first agent: the drain hands back whole persons, never part of one, "
            "and a group stops before the next memory that waits again.")
LESSON_B = ("Lesson of the second agent: a dates line under the heading is printed only when a "
            "memory of the block carries resolved times.")
TURN_A = "user: I painted three posters for the parade."
TURN_B = "user: Those posters are what I am proudest of."


@pytest.fixture
def dialogue_order_on(monkeypatch):
    monkeypatch.setattr(experiments, "DIALOGUE_ORDER", True)


def note(db, tenant, text, cos, minutes, *, agent="planner", user=None, run=None, client=None):
    """A memory; with `cos` None it has no vector yet, so no search brings it."""
    found = {} if cos is None else {"embedding": vec(cos), "embedding_status": vectors.DONE}
    m = AgentMemory(tenant_id=tenant.id, project_id=seeded_project_id(db, tenant.id),
                    end_user_id=user, agent_id=agent, run_id=run, client=client, content=text,
                    created_at=T0 + timedelta(minutes=minutes), **found)
    db.add(m)
    db.commit()
    return m


def team_block(db, tenant, budget=2000):
    return ctx.assemble_context(db, tenant.id, query="Which line under the heading?",
                                token_budget=budget)


# Who wrote two notes with no end user: two agents, one agent, an id of
# spaces (it names nobody, `owner.agent_of`), and an assistant through MCP
# with no ids (stored under its client's name since 1.0.19).
NO_END_USER = {"two-agents": [{"agent": "planner"}, {"agent": "reviewer"}],
               "one-agent": [{"agent": "planner"}, {"agent": "planner"}],
               "blank-agent": [{"agent": "   "}, {"agent": "   "}],
               "mcp-no-ids": [{"agent": None, "client": "claude-code"},
                              {"agent": None, "client": "claude-code"}]}


@pytest.mark.parametrize("writers", NO_END_USER.values(), ids=NO_END_USER.keys())
def test_notes_with_no_end_user_keep_the_search_order(db, tenant, dialogue_order_on, writers):
    """Two notes written one after the other, with nothing between them: the
    newer, the closer to the question, comes first, whoever wrote them."""
    note(db, tenant, LESSON_A, 0.6, 0, **writers[0])
    note(db, tenant, LESSON_B, 0.9, 1, **writers[1])
    out = team_block(db, tenant)["volatile"]
    assert order(out, "second agent", "first agent") == ["second agent", "first agent"]


def test_a_run_id_makes_no_run_of_an_agents_notes(db, tenant, dialogue_order_on):
    """The choice written in context_experiments: a `run_id` is a scope, not
    an order for the reader."""
    note(db, tenant, LESSON_A, 0.6, 0, run="session-1")
    note(db, tenant, LESSON_B, 0.9, 1, run="session-1")
    out = team_block(db, tenant)["volatile"]
    assert order(out, "second agent", "first agent") == ["second agent", "first agent"]


@pytest.mark.parametrize("writer", [{"agent": "reviewer"}, {"agent": None, "client": "claude-code"}],
                         ids=["agent", "mcp-no-ids"])
def test_the_note_just_written_is_in_a_400_token_block(db, tenant, dialogue_order_on, writer):
    """The defect as the team met it: a note written just now, the closest to
    the question, in a 400-token block where one memory fits. Before 1.0.22
    the note written before it was printed first and took the room."""
    from korely_agent.services.ids import mem_public_id

    note(db, tenant, " ".join([LESSON_A] * 4), 0.6, 0, **writer)  # fills the room
    newer = note(db, tenant, LESSON_B, 0.9, 1, **writer)
    out = team_block(db, tenant, budget=400)
    memories = [s for s in out["sources"] if s.startswith("mem_")]
    assert memories[0] == mem_public_id(newer.id), out["context"]
    assert LESSON_B in out["context"], "shown whole"
    assert out["tokens"] <= 400


@pytest.mark.parametrize("agent", [None, "support-bot", "   "], ids=["no-agent", "agent", "blank-agent"])
def test_an_end_users_turns_still_make_a_run(db, tenant, dialogue_order_on, agent):
    """An end user's turns, written by an agent or not: the runs of before
    (LoCoMo and LongMemEval write with an end user)."""
    note(db, tenant, TURN_A, 0.6, 0, user="maria", agent=agent)
    note(db, tenant, TURN_B, 0.9, 1, user="maria", agent=agent)
    out = block(db, tenant)["volatile"]
    assert order(out, "three posters", "proudest") == ["three posters", "proudest"]


# One owner for both checks: the run groups an end user's memories, and only
# that end user's writes, whoever wrote them, stand between two of its turns.
BETWEEN = {"an-agents-note": ({"agent": "planner"}, ["three posters", "proudest"]),
           "an-mcp-note": ({"agent": None, "client": "claude-code"}, ["three posters", "proudest"]),
           "another-end-user": ({"user": "luca", "agent": None}, ["three posters", "proudest"]),
           "hers-by-another-agent": ({"user": "maria", "agent": "sales-bot"},
                                     ["proudest", "three posters"])}


@pytest.mark.parametrize("between, expected", BETWEEN.values(), ids=BETWEEN.keys())
def test_only_the_end_users_own_writes_stand_between_her_turns(db, tenant, dialogue_order_on,
                                                               between, expected):
    note(db, tenant, TURN_A, 0.6, 0, user="maria", agent="support-bot")
    note(db, tenant, "Something written in between.", None, 1, **between)
    note(db, tenant, TURN_B, 0.9, 2, user="maria", agent="support-bot")
    out = block(db, tenant)["volatile"]
    assert order(out, "three posters", "proudest") == expected
