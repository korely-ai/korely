"""Planned facts in the facts section of /v1/context: ranked like every fact.

A fact the writer read as an intention (tense "planned") stays true until a
newer statement closes it, and one plan restated in many phrasings can fill
the facts section: nine of the ten fact lines of "How many weddings have I
attended in this year?" were the user's own wedding plans, on the 180 saved
LongMemEval blocks. The cap that would have kept at most three of them
(context_experiments.PLAN_CAP, 2026-10-05) was measured on 12 Oct 2026 and
removed: on the 91 LongMemEval questions whose block it changed, three reads
per arm with gpt-oss-120b, 68.0 answers against 68.7 without it (5 questions
better, 8 worse), counting not better (10.3 against 10.7 of 17). These tests
keep what the block does: plans ranked by the question, or by recency, as
every fact is, each line marked "[planned, not done]".

The question's vector is stubbed and each fact gets a vector at a chosen
cosine from it, so the ranking is the one these tests write down.
"""
import math

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact
from korely_agent.services import context as ctx
from korely_agent.services.facts.service import write_fact_direct
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id



def _walsh(k):
    """Row k of a Walsh matrix over the vector's 768 dimensions: +1 and -1,
    orthogonal to every other row below 256 (768 is three periods of 256),
    all of the same length."""
    return [(-1.0) ** bin(i & k).count("1") for i in range(EMBEDDING_DIM)]


QUESTION = _walsh(0)  # every component 1


def at(cos, side):
    """A vector at cosine `cos` from the question's, leaning to its own
    `side` (1 to 255): facts on two sides are cos1 * cos2 apart, two on the
    same side are near twins."""
    s = math.sqrt(1.0 - cos * cos)
    return [cos * q + s * x for q, x in zip(QUESTION, _walsh(side))]


# The bench's wedding block, shrunk: eight phrasings of the user's own plan
# rank above the four weddings the question is about.
PLANS = ["outdoor wedding", "small outdoor wedding ceremony", "orchard wedding",
         "post-wedding celebration", "wedding reception", "outdoor wedding ceremony",
         "dinner party-style reception", "post-wedding brunch"]
ATTENDED = ["Rachel and Mike's wedding", "Emily and Sarah's wedding",
            "Jen and Tom's wedding", "my cousin's wedding"]
PLANNED = "[planned, not done]"  # how memory_core's `context_line` marks a plan


@pytest.fixture(autouse=True)
def _a_block_of_facts(monkeypatch):
    """The question's vector, and no memory: the block is its facts."""
    monkeypatch.setattr(ctx, "embed", lambda *a, **k: QUESTION)
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, q, **k: [])


def write(db, tenant, predicate, obj, tense, vector=None):
    out = write_fact_direct(db, tenant.id, subject="maria", predicate=predicate, obj=obj,
                            end_user_id="maria", project_id=seeded_project_id(db, tenant.id),
                            tense=tense)
    if vector is not None:
        row = db.query(AgentFact).filter(AgentFact.object_label == obj).one()
        row.embedding, row.embedding_status = vector, "done"
        db.commit()
    return out["id"]


def wedding_store(db, tenant, *, vectors=True):
    """Eight plans ranked first to eighth, four weddings attended ranked
    ninth to twelfth. Without vectors the facts fall back to recency, and
    the plans, written last, are the most recent."""
    ids = {}
    for i, obj in enumerate(ATTENDED):
        ids[obj] = write(db, tenant, "attended", obj, "current",
                         at(0.91 - 0.01 * i, 9 + i) if vectors else None)
    for i, obj in enumerate(PLANS):
        ids[obj] = write(db, tenant, "plans", obj, "planned",
                         at(0.99 - 0.01 * i, 1 + i) if vectors else None)
    # The premise: no write closed another (no model, so no judge).
    assert db.query(AgentFact).filter(true_at()).count() == 12
    return ids


def block(db, tenant, query="How many weddings have I attended in this year?"):
    return ctx.assemble_context(db, tenant.id, query=query, end_user_id="maria",
                                token_budget=4000, project_id=seeded_project_id(db, tenant.id))


def fact_lines(out):
    """The facts section, one line per fact, without the "(since ...)" date."""
    section = out["context"].split(ctx.KNOWN_FACTS_HEADING + "\n", 1)[1]
    lines = section.split("\n\n", 1)[0].split("\n")
    return [line.split(" (since")[0] + (" [planned]" if line.endswith(PLANNED) else "")
            for line in lines]


def plans(objects):
    return [f"- The user plans {o} [planned]" for o in objects]


def attended(objects):
    return [f"- The user attended {o}" for o in objects]


class TestPlansRankLikeEveryFact:
    def test_by_the_question(self, db, tenant):
        wedding_store(db, tenant)
        assert fact_lines(block(db, tenant)) == plans(PLANS) + attended(ATTENDED[:2])

    def test_by_recency(self, db, tenant):
        wedding_store(db, tenant, vectors=False)
        assert fact_lines(block(db, tenant)) == plans(PLANS[::-1]) + attended(ATTENDED[:1:-1])
