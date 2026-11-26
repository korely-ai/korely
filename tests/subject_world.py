"""A memory with no end user, as a team's: the world of the tests of memory_core
6.20.0 (`objects.review_subjects`, services/facts/subjects.py).

One product under several names ("Cloud", born first, "Korely Cloud", "Cloud
di Korely"), its version facts on different names, a vector per fact on the
same axis for one thing, and the fakes: a judge that closes what it is shown
and a decisions port that answers from a script. No model, no network.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from korely_agent.core.llm import EMBEDDING_DIM, LLMResult
from korely_agent.db.models import AgentFact, AgentMemory, AgentObjectLink
from tests.conftest import judge_reply

NOW = datetime.now(timezone.utc)


def vec(axis: int, *also) -> list:
    """A vector on `axis`, with a little of each (axis, weight) of `also`."""
    v = [0.0] * EMBEDDING_DIM
    v[axis] = 1.0
    for a, w in also:
        v[a] += w
    return v


def memory(db, tenant, text: str, *, agent: str = "notes-agent", days_ago: int = 3,
           project_id=None) -> AgentMemory:
    m = AgentMemory(tenant_id=tenant.id, project_id=project_id, end_user_id=None, agent_id=agent,
                    content=text, graph_sync_status="done",
                    created_at=NOW - timedelta(days=days_ago))
    db.add(m)
    db.commit()
    return m


def fact(db, tenant, subject: str, pred: str, obj: str, *, mem=None, typ: str = "product",
         agent: str = "notes-agent", minutes_ago: int = 60, valid_days_ago: int = 10, vector=None,
         user=None, project_id=None) -> AgentFact:
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project_id, end_user_id=user,
                  agent_id=agent, subject_label=subject, subject_canonical=subject, subject_type=typ,
                  predicate_raw=pred, predicate_canonical=pred, object_label=obj, object_canonical=obj,
                  confidence=0.9, tense="current", valid_from=NOW - timedelta(days=valid_days_ago),
                  created_at=NOW - timedelta(minutes=minutes_ago), embedding=vector,
                  embedding_status="done" if vector is not None else "pending",
                  source_memory_id=mem.id if mem is not None else None,
                  source_memory_ids=[str(mem.id)] if mem is not None else None)
    db.add(f)
    db.commit()
    return f


def wall_link(db, tenant, name: str, canonical: str, *, source=None, canonical_memory=None,
              project_id=None) -> AgentObjectLink:
    """A link of the wall between two subjects, as `subjects.apply` writes it."""
    from korely_agent.services.facts.entities import TABLES, wall_of
    from korely_agent.services.memory_core import objects as O

    row, outcome = O.add_link(db, TABLES, wall_of(tenant.id, project_id), name, canonical,
                              decided_by=O.BY_A_SUBJECT_REVIEW, family=O.SUBJECT_FAMILY,
                              source_memory_id=source.id if source is not None else None,
                              canonical_memory_id=canonical_memory.id if canonical_memory is not None else None)
    assert outcome == "created", outcome
    db.commit()
    return row


class Judge:
    """The contradiction judge, faked: closes every candidate whose line
    `closes(line)` accepts (all by default), in the shape the prompt asks.
    `prompts` keeps what it was asked."""

    def __init__(self, closes=lambda line: True):
        self.closes = closes
        self.prompts: list = []

    def complete(self, messages, **kw):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)

        def verdict(text):
            return [{"existing_id": m.group(1), "reason": "a newer value"}
                    for line in text.splitlines()
                    for m in [re.search(r"id=([0-9a-f-]{36})", line)] if m and self.closes(line)]
        return LLMResult(text=judge_reply(prompt, verdict), prompt_tokens=10, completion_tokens=5, model="fake")


class Port:
    """The decisions port, faked: `answers` is a list of choices given in
    order (a callable gets (state, instructions, criteria)); `calls` keeps
    what it was asked."""

    model = "typesafe/jev-1.13"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list = []

    def choose(self, state, instructions, criteria):
        self.calls.append((json.loads(json.dumps(state)), instructions, dict(criteria)))
        a = self.answers.pop(0) if self.answers else "unsure"
        return a(state, instructions, criteria) if callable(a) else a


def snapshot(db, tenant) -> list:
    """Every fact and link row of the account, every column, in a stable order."""
    db.expire_all()
    out = []
    for model in (AgentFact, AgentObjectLink):
        cols = [c.name for c in model.__table__.columns]
        out += sorted((tuple((c, repr(getattr(r, c))) for c in cols)
                       for r in db.query(model).filter(model.tenant_id == tenant.id)), key=repr)
    return out
