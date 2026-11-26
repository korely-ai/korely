"""The three levels of a charter, written with no end user, do not close one
another (memory_core 6.4.0, through this product's write).

A team of agents writes its memory with an `agent_id` and no `user_id`. On 6
November 2026 three of its memories said what each level of a delegation
charter covers (GREEN, YELLOW, RED). The extraction was asked the prompts of
an end user's memory, with the handle "user", and wrote "user
has_delegation_level GREEN", then YELLOW, then RED: one subject and one
relation, and the judge closed GREEN with YELLOW and YELLOW with RED. A chain
of closures out of three facts that are all true.

Here the write runs as the product runs it (`facts.service
.extract_and_persist_facts`, the real memory_core extraction and judge), with
a scripted model and a scripted judge, the judge one that closes an earlier
fact of the same subject and relation, as a judge does on a relation that
holds one value at a time. Two models: one that writes the subject its frame
asks for (END_USER when told the memory is an end user's, the level the text
names when told it belongs to nobody), and one that writes "user" whatever it
is told, which the grounding filter is the net for.

Invented texts; no real model is called.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM, LLMResult
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import service as fsvc
from tests.conftest import seeded_project_id

LEVELS = {
    "GREEN": "Team policy, level GREEN: branches, worktrees and tests are done without asking.",
    "YELLOW": "Team policy, level YELLOW: a push to the private main is decided by the lead.",
    "RED": "Team policy, level RED: any spending is decided by the owner alone.",
}
RELATIONS = ("has_delegation_level", "covers")
_ID = re.compile(r"id=([0-9a-f-]{36}) \| (.*)")


def _level(text):
    return next(level for level, said in LEVELS.items() if said in text)


def _facts_of(prompt, *, follows_the_frame):
    level = _level(prompt.rsplit("Memory", 1)[-1])
    told_an_end_user = "END_USER in the rules above is the end user" in prompt
    if follows_the_frame and not told_an_end_user:
        # The subject the text names, as the frame of no end user asks.
        return [{"subject": f"level {level}", "subject_type": "concept", "predicate": "covers",
                 "object": LEVELS[level].split(": ", 1)[1].split(" are ")[0].split(" is ")[0],
                 "tense": "current", "evidence": LEVELS[level]}]
    # What gpt-oss-120b wrote on 6 November: the end user's handle.
    return [{"subject": "END_USER" if told_an_end_user else "user", "subject_type": "person",
             "predicate": "has_delegation_level", "object": level, "object_is_literal": True,
             "tense": "current", "evidence": LEVELS[level]}]


def _head(row):
    """`<subject> <relation>` of a judge's line."""
    for relation in RELATIONS:
        at = row.find(f" {relation} ")
        if at >= 0:
            return row[:at + len(relation) + 1]
    return None


def _closes(new_row, candidates):
    """The scripted judge: an earlier fact of the same subject and relation is
    no longer true (a relation of one value at a time)."""
    head = _head(new_row)
    out = []
    for line in candidates.splitlines():
        m = _ID.search(line)
        if m and head and m.group(2).startswith(head + " "):
            out.append({"existing_id": m.group(1), "reason": "one value at a time"})
    return out


def _judge(prompt):
    if "[new_id=" in prompt:
        results = []
        for block in prompt.split("[new_id=")[1:]:
            new_id, rest = block.split("] NEW: ", 1)
            new_row, candidates = rest.split("\n", 1)
            results.append({"new_id": new_id, "contradictions": _closes(new_row, candidates)})
        return json.dumps({"results": results})
    existing, new = prompt.split("EXISTING facts:", 1)[1].split("NEW fact:", 1)
    return json.dumps({"contradictions": _closes(new.strip().splitlines()[0], existing)})


class _Model:
    name = "scripted"

    def __init__(self, follows_the_frame):
        self.follows_the_frame = follows_the_frame
        self.extraction_prompts = []

    def complete(self, messages, **kw):
        p = messages[0]["content"]
        if "EXISTING facts:" in p or "[new_id=" in p:
            return LLMResult(text=_judge(p))
        if "You extract knowledge-graph facts" in p:
            self.extraction_prompts.append(p)
            return LLMResult(text=json.dumps({"observation": "", "relative_times": [],
                                              "facts": _facts_of(p, follows_the_frame=self.follows_the_frame)}))
        if '"family"' in p:
            return LLMResult(text='{"family": "other"}')
        if "many_values" in p:
            # One value at a time: the judge may close.
            return LLMResult(text='{"many_values": false}')
        if '{"past": false}' in p:
            return LLMResult(text='{"past": false}')
        return LLMResult(text='{"same_as": null}')


def _write_the_charter(db, tenant, monkeypatch, model, *, end_user=None, agent="lead-agent"):
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: model)
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: [0.1] * EMBEDDING_DIM)
    project = seeded_project_id(db, tenant.id)
    for day, text in enumerate(LEVELS.values(), start=4):
        m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project, end_user_id=end_user,
                        agent_id=agent, content=text, graph_sync_status="pending",
                        event_time=datetime(2026, 11, day, tzinfo=timezone.utc))
        db.add(m)
        db.commit()
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)
    db.expire_all()
    return db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).all()


@pytest.mark.parametrize("follows_the_frame", [True, False], ids=["follows-the-frame", "writes-user-anyway"])
def test_the_levels_of_a_charter_do_not_close_one_another(db, tenant, monkeypatch, follows_the_frame):
    model = _Model(follows_the_frame)
    facts = _write_the_charter(db, tenant, monkeypatch, model)
    closed = [(f.subject_label, f.object_label) for f in facts if f.invalid_at is not None]
    assert closed == [], f"a level of the charter closed another: {closed}"
    # A memory whose every declared fact the filters refuse (a "user" the
    # text does not name) is asked for once more with the engine's reason
    # (memory_core 6.26.0), the same prompt and frame: twice per memory then.
    assert len(model.extraction_prompts) == (3 if follows_the_frame else 6)
    for p in model.extraction_prompts:
        assert "END_USER" not in p and "NO end user" in p
        assert 'WRITER in the rules above is the agent "lead-agent"' in p
    assert all(f.subject_label != "user" for f in facts)
    if follows_the_frame:
        assert sorted(f.subject_label for f in facts) == ["level GREEN", "level RED", "level YELLOW"]
    else:
        assert facts == [], "the net: a user the text does not name is no subject"


def test_the_scripted_judge_does_close_a_chain(db, tenant, monkeypatch):
    """The control: the same three memories of an END USER, whose facts are
    about one subject and one relation, close one another with this judge.
    The test above is not green because the judge never closes."""
    facts = _write_the_charter(db, tenant, monkeypatch, _Model(True), end_user="ana-77")
    closed = sorted(f.object_label for f in facts if f.invalid_at is not None)
    assert closed == ["GREEN", "YELLOW"]


def test_a_memory_of_no_ids_is_written_as_before(db, tenant, monkeypatch):
    """Neither a user_id nor an agent_id: the write of 6.3.0, an end user
    named "user" (one person using the product with no ids keeps the facts
    of their first person). Here that is the chain of 6 November too: the
    frame of 6.4.0 needs the agent_id."""
    model = _Model(True)
    facts = _write_the_charter(db, tenant, monkeypatch, model, agent=None)
    for p in model.extraction_prompts:
        assert 'END_USER in the rules above is the end user "user"' in p and "NO end user" not in p
    assert {f.subject_label for f in facts} == {"user"}
    assert sorted(f.object_label for f in facts if f.invalid_at is not None) == ["GREEN", "YELLOW"]
