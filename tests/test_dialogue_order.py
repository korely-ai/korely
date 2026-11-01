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
