"""The reader note of a block that names no end user (1.0.40).

The block opens with a note for its reader. It said "The facts below are a
compact profile of the user; the memories are the verbatim source of
truth" and asked to apply "the user's stated preferences" to every block,
the memory of a team or of an agent included, read with no end user: a
team's decisions read as one person's tastes (found using Korely as a
team's memory, 6 Nov 2026), and a memory past the first few is shown by the
sentence its write gave it, not by its own words (the review of 10 Nov
2026). A read that names no end user now has a note in words that are true
of such a memory; a read that names one keeps the note the benchmarks
measured, byte for byte. No model is called.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact
from korely_agent.services import context as ctx
from korely_agent.services.memory_engine import AgentMemoryEngine


@pytest.fixture(autouse=True)
def _no_models(monkeypatch):
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: [0.1] * EMBEDDING_DIM)


def _fact(db, tenant, end_user):
    db.add(AgentFact(tenant_id=tenant.id, end_user_id=end_user, subject_label="release",
                     subject_type="concept", predicate_raw="ships_on", predicate_canonical="ships_on",
                     object_label="Friday", object_is_literal=True, confidence=0.9,
                     valid_from=datetime(2026, 11, 1, tzinfo=timezone.utc)))
    db.commit()


@pytest.fixture
def a_memory(monkeypatch):
    rows = [{"id": str(uuid.uuid4()), "content": "The team ships the release on Friday."}]
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, q, **k: list(rows))


def _block(db, tenant, end_user_id, budget=800):
    return ctx.assemble_context(db, tenant.id, query="when does the release ship?",
                                end_user_id=end_user_id, token_budget=budget)


def test_a_read_of_no_end_user_has_the_shared_note(db, tenant, a_memory):
    _fact(db, tenant, None)
    out = _block(db, tenant, None)
    assert out["stable"] == ctx._SHARED_READER_GUIDANCE
    assert "profile of the user" not in out["context"]
    assert "the user's" not in out["stable"]
    assert "verbatim" not in out["stable"]
    assert "## Known facts" in out["volatile"] and "Friday" in out["volatile"]


def test_a_read_of_an_end_user_keeps_the_measured_note(db, tenant, a_memory):
    _fact(db, tenant, "maria")
    out = _block(db, tenant, "maria")
    assert out["stable"] == ctx._READER_GUIDANCE
    assert out["stable"].startswith("_The facts below are a compact profile of the user;")


def test_only_one_named_end_user_gets_the_end_users_note():
    """As `context_line` reads "The user" only for one end user named by a
    string: no end user, or several (a confined key's `EndUsers`), get the
    shared note."""
    from korely_agent.services.sentinels import EVERY_END_USER, EndUsers

    assert ctx.reader_guidance("maria") == ctx._READER_GUIDANCE
    for other in (None, EVERY_END_USER, EndUsers(["maria", "luca"])):
        assert ctx.reader_guidance(other) == ctx._SHARED_READER_GUIDANCE, other


def test_the_shared_note_is_no_longer_than_the_end_users():
    """The blocks of a memory of no end user pack as before: the note takes
    no more of the budget than the one it replaces (`_GUIDANCE_MIN_BUDGET`,
    `_Block.facts_limit`)."""
    assert ctx._cost(ctx._SHARED_READER_GUIDANCE) <= ctx._cost(ctx._READER_GUIDANCE)
    assert ctx._SHARED_READER_GUIDANCE.startswith("_") and ctx._SHARED_READER_GUIDANCE.endswith("_")


def test_below_the_notes_budget_neither_note(db, tenant, a_memory):
    _fact(db, tenant, None)
    out = _block(db, tenant, None, budget=ctx._GUIDANCE_MIN_BUDGET - 1)
    assert out["stable"] == ""
