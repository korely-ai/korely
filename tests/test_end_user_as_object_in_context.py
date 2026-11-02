"""The end user as the object of a fact reads "the user" in /v1/context
(memory_core 3.1.4, 2026-10-06).

The subject that IS the end user has read "The user" since the LongMemEval
failure of an opaque handle the reader could not bind to "I/my". The object
did not: on the 3.1 bench (LoCoMo conversation 26) "Melanie supports
gb-0-caroline" and "family motivates gn-0-melanie" were in 142 blocks of 152,
and in every case the handle was the block's own end user. In the product it
is "Her manager mentors c4821". memory_core `presentation.context_line`
now writes "the user" for an object whose label is the end user's id, the
same criterion as the subject; another person keeps their name, and a block
with no end user changes nothing.
"""
import pytest

from korely_agent.services import context as ctx
from korely_agent.services.facts.service import write_fact_direct
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id



@pytest.fixture(autouse=True)
def _a_block_of_facts(monkeypatch):
    """No vectors and no memories: the block is its facts, by recency."""
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: None)
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, q, **k: [])


def write(db, tenant, subject, predicate, obj, end_user_id):
    write_fact_direct(db, tenant.id, subject=subject, predicate=predicate, obj=obj,
                      end_user_id=end_user_id, project_id=seeded_project_id(db, tenant.id))


def fact_lines(db, tenant, end_user_id):
    out = ctx.assemble_context(db, tenant.id, query="Who supports me at work?", end_user_id=end_user_id,
                               token_budget=2000, project_id=seeded_project_id(db, tenant.id))
    section = out["context"].split(ctx.KNOWN_FACTS_HEADING + "\n", 1)[1]
    return sorted(line.split(" (since")[0] for line in section.split("\n\n", 1)[0].split("\n"))


def test_the_end_user_as_object_reads_the_user(db, tenant):
    write(db, tenant, "Her manager", "mentors", "c4821", "c4821")
    write(db, tenant, "c4821", "works_at", "Acme", "c4821")
    assert fact_lines(db, tenant, "c4821") == ["- Her manager mentors the user",
                                               "- The user works_at Acme"]


def test_another_person_keeps_their_name(db, tenant):
    write(db, tenant, "Her manager", "mentors", "Luca", "c4821")
    assert fact_lines(db, tenant, "c4821") == ["- Her manager mentors Luca"]
