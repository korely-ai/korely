"""One judge call for a memory's write, as the hosted product (2026-10-07).

The hosted product judges a write in one call in production (memory_core
`judge.batch`); this engine judged each new fact on its own.
`_BATCHED_JUDGE` (services/facts/service.py, on by default since
0.1.22) gives this engine the same path. What must hold either
way: the candidates are the per-fact path's (scope and BUG-123 source), a
past statement is still judged as it is written, and a judge that gives no
answer rolls the whole write back.

The judges here are fake and record what they are shown.
"""
import uuid

import pytest

from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import contradiction, service
from korely_agent.services.memory_core.fact_extractor import ExtractedFact



class _Judges:
    """`_classify` (one fact) and `_classify_batch` (one write). `close`:
    the old objects the judge says each new fact replaces."""

    def __init__(self, close=None):
        self.close = close or {}
        self.single = []
        self.batches = []

    def one(self, candidates, new_subject, new_predicate_raw, new_object, *a, **k):
        self.single.append((new_object, k.get("new_past"), [c.object_label for c in candidates]))
        return [c.id for c in candidates if c.object_label in self.close.get(new_object, ())], {}

    def batch(self, groups, db, tenant_id, model_name=None):
        self.batches.append([(g["new_text"], [c.object_label for c in g["candidates"]]) for g in groups])
        out = {}
        for g in groups:
            first_line = g["new_text"].split("\n")[0].split(" (since")[0].split(" [")[0]
            new = next((n for n in self.close if first_line.endswith(" " + n)), None)
            out[g["new_id"]] = [c.id for c in g["candidates"] if new and c.object_label in self.close[new]]
        return out


@pytest.fixture
def judges(monkeypatch):
    j = _Judges()
    monkeypatch.setattr(contradiction, "_classify", j.one)
    monkeypatch.setattr(contradiction, "_classify_batch", j.batch)
    return j


@pytest.fixture
def batched(monkeypatch):
    monkeypatch.setattr(service, "_BATCHED_JUDGE", True)


def _memory(db, tenant, text):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="acme", content=text)
    db.add(m)
    db.commit()
    return m


def _f(pred, obj, tense="current"):
    return ExtractedFact(subject="Acme", predicate_raw=pred, object=obj,
                         confidence=0.9, subject_type="organization", tense=tense)


def _write(db, tenant, text, facts):
    return service._persist_extracted(db, tenant.id, memory=_memory(db, tenant, text), extracted=facts)


def _open(db):
    return {f.object_label for f in db.query(AgentFact).filter(AgentFact.invalid_at.is_(None))}


def test_one_call_for_the_whole_write(db, tenant, judges, batched):
    _write(db, tenant, "Acme is on hobby and uses Jira", [_f("on_plan", "hobby"), _f("uses", "Jira")])
    judges.close = {"team": ["hobby"], "Linear": ["Jira"]}
    out = _write(db, tenant, "Acme moved to team and to Linear", [_f("on_plan", "team"), _f("uses", "Linear")])

    assert judges.single == []
    assert len(judges.batches) == 1 and len(judges.batches[0]) == 2
    assert _open(db) == {"team", "Linear"}
    closed = {o["object"]: o["invalidated"] for o in out}
    assert len(closed["team"]) == 1 and len(closed["Linear"]) == 1


def test_the_facts_of_one_memory_never_judge_each_other(db, tenant, judges, batched):
    judges.close = {"developer": ["hobby"], "team": ["hobby", "developer"]}
    _write(db, tenant, "Agents plans: hobby, developer, team",
           [_f("on_plan", "hobby"), _f("on_plan", "developer"), _f("on_plan", "team")])
    assert _open(db) == {"hobby", "developer", "team"}
    assert all(cands == [] for batch in judges.batches for _t, cands in batch)


def test_a_past_statement_is_still_judged_as_it_is_written(db, tenant, judges, batched):
    _write(db, tenant, "Acme uses Salesforce", [_f("uses", "Salesforce")])
    _write(db, tenant, "Salesforce serves as Acme's CRM",
           [ExtractedFact(subject="Salesforce", predicate_raw="serves_as_crm_for", object="Acme",
                          confidence=0.9, subject_type="organization")])
    judges.single.clear()
    judges.batches.clear()
    judges.close = {"Salesforce": ["Acme"]}
    _write(db, tenant, "Acme no longer uses Salesforce", [_f("uses", "Salesforce", tense="past")])

    assert [s[:2] for s in judges.single] == [("Salesforce", True)]
    assert judges.batches == []
    assert "Salesforce" not in _open(db) and "Acme" not in _open(db)


def test_a_judge_with_no_answer_rolls_the_write_back(db, tenant, judges, batched, monkeypatch):
    _write(db, tenant, "Acme is on hobby", [_f("on_plan", "hobby")])

    def silent(*a, **k):
        raise contradiction.JudgeUnavailable("no answer")

    monkeypatch.setattr(contradiction, "_classify_batch", silent)
    m = _memory(db, tenant, "Acme moved to team")
    m.graph_sync_status = "processing"
    db.commit()
    out = service._persist_extracted(db, tenant.id, memory=m, extracted=[_f("on_plan", "team")])

    assert out == []
    db.expire_all()
    assert _open(db) == {"hobby"}
    assert db.get(AgentMemory, m.id).graph_sync_status == "processing"


def test_on_by_default(db, tenant, judges):
    """2026-10-07: on, as the hosted product judges in production."""
    assert service._BATCHED_JUDGE is True


def test_off_each_fact_is_judged_on_its_own(db, tenant, judges, monkeypatch):
    monkeypatch.setattr(service, "_BATCHED_JUDGE", False)
    _write(db, tenant, "Acme is on hobby and uses Jira", [_f("on_plan", "hobby"), _f("uses", "Jira")])
    judges.close = {"team": ["hobby"], "Linear": ["Jira"]}
    _write(db, tenant, "Acme moved to team and to Linear", [_f("on_plan", "team"), _f("uses", "Linear")])

    assert judges.batches == []
    assert [s[0] for s in judges.single if s[2]] == ["team", "Linear"]
    assert _open(db) == {"team", "Linear"}
