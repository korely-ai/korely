"""memory_core 2.12.0: the write order is not the time order.

Found by the code audit of 6 October. A /v1/batch
exported "most recent first" wrote "Maria works at Globex" (2025-06) before
"Maria works at Acme" (2024-01); Acme's judge closed Globex in 2024, before it
began (`invalid_at < valid_from`, the state `ClosedBeforeItBegan` refuses), and
Acme stayed open like the present.

The rule (`judge.close`, from Graphiti's reference implementation, cited in the
code): a fact is never closed before its `valid_from`; the new fact older than
the one it contradicts is history, closed where the other begins; on equal
dates the new information wins. And the drain's claim goes in event order
(`worker.event_order`).

No model call: extraction and judge are fake.
"""
# Port notes: on korely-agent's tables (tenant_id). The two "from the cloud" tests
# run through korely-agent's write (`facts.service.extract_and_persist_facts`):
# the extraction faked with tests.conftest.stub_extraction, the fake judge
# behind this product's provider (`core.llm.get_provider`), the batched judge
# switched by `service._BATCHED_JUDGE`, the vocabulary without a model
# (`predicate_vocabulary_model=False`), and the reads
# by `facts.retrieve_agent_facts`.
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services import predicates
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import contradiction, judge
from korely_agent.services.memory_core import worker
from tests.conftest import stub_extraction
from korely_agent.services.sentinels import EVERY_PROJECT

ACME = datetime(2024, 1, 1, tzinfo=timezone.utc)
GLOBEX = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _aware(v):
    return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc))


def _fact_row(db, tenant, o, since, *, closed=None, tense="current"):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1", subject_label="Maria",
                  subject_canonical="Maria", predicate_raw="works_at", predicate_canonical="works_at",
                  object_label=o, object_canonical=o, valid_from=since, invalid_at=closed, tense=tense)
    db.add(f)
    db.commit()
    return f


def _never_closed_before_beginning(db, tenant):
    rows = db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id).all()
    for r in rows:
        if r.invalid_at is not None:
            assert _aware(r.invalid_at) >= _aware(r.valid_from), (
                f"{r.object_label}: closed on {r.invalid_at} before beginning on {r.valid_from}")
    return rows


class TestClose:
    def test_the_new_older_than_the_open_one_becomes_history(self, db, tenant):
        """Before: Globex closed on 2024-01-01, before its 2025-06-01."""
        globex = _fact_row(db, tenant, "Globex", GLOBEX)
        acme = _fact_row(db, tenant, "Acme", ACME)
        closed_ids = judge.close(db, AgentFact, [globex.id], acme.id, ACME, is_open=true_at())
        assert closed_ids == []
        assert globex.invalid_at is None and globex.invalidated_by_id is None
        assert _aware(acme.invalid_at) == GLOBEX and acme.invalidated_by_id == globex.id
        _never_closed_before_beginning(db, tenant)

    def test_the_newer_closes_the_old_where_it_begins(self, db, tenant):
        acme = _fact_row(db, tenant, "Acme", ACME)
        globex = _fact_row(db, tenant, "Globex", GLOBEX)
        assert judge.close(db, AgentFact, [acme.id], globex.id, GLOBEX, is_open=true_at()) == [acme.id]
        assert _aware(acme.invalid_at) == GLOBEX and acme.invalidated_by_id == globex.id
        assert globex.invalid_at is None

    def test_on_equal_dates_the_new_information_wins(self, db, tenant):
        """Graphiti §2.2.3: "consistently prioritizes new information"."""
        acme = _fact_row(db, tenant, "Acme", GLOBEX)
        globex = _fact_row(db, tenant, "Globex", GLOBEX)
        assert judge.close(db, AgentFact, [acme.id], globex.id, GLOBEX, is_open=true_at()) == [acme.id]
        assert _aware(acme.invalid_at) == GLOBEX and globex.invalid_at is None

    def test_one_before_and_one_after(self, db, tenant):
        """Initech (2023) ends where Acme (2024) begins; Acme ends where Globex
        (2025) begins, the first of the facts after it."""
        initech = _fact_row(db, tenant, "Initech", ACME - timedelta(days=365))
        globex = _fact_row(db, tenant, "Globex", GLOBEX)
        hooli = _fact_row(db, tenant, "Hooli", GLOBEX + timedelta(days=90))
        acme = _fact_row(db, tenant, "Acme", ACME)
        closed_ids = judge.close(db, AgentFact, [hooli.id, initech.id, globex.id], acme.id, ACME,
                                is_open=true_at())
        assert closed_ids == [initech.id]
        assert _aware(initech.invalid_at) == ACME and initech.invalidated_by_id == acme.id
        assert _aware(acme.invalid_at) == GLOBEX and acme.invalidated_by_id == globex.id
        assert globex.invalid_at is None and hooli.invalid_at is None
        _never_closed_before_beginning(db, tenant)

    def test_a_new_fact_already_ended_before_is_not_touched(self, db, tenant):
        """A phrase in the past is already history: a fact that began after does
        not reopen it or move it."""
        globex = _fact_row(db, tenant, "Globex", GLOBEX)
        said = ACME + timedelta(days=30)
        past = _fact_row(db, tenant, "Acme", ACME, closed=said, tense="past")
        assert judge.close(db, AgentFact, [globex.id], past.id, said) == []
        assert _aware(past.invalid_at) == said and past.invalidated_by_id is None
        assert globex.invalid_at is None

    def test_a_new_id_that_is_not_a_row(self, db, tenant):
        globex = _fact_row(db, tenant, "Globex", GLOBEX)
        assert judge.close(db, AgentFact, [globex.id], uuid.uuid4(), ACME, is_open=true_at()) == []
        assert globex.invalid_at is None


class TestTheJudgeReadsTheDate:
    def test_the_new_fact_carries_its_date_like_the_earlier_ones(self):
        c = NS(id=uuid.uuid4(), subject_label="Maria", subject_canonical="Maria", predicate_raw="works_at",
               object_label="Globex", valid_from=GLOBEX, predicate_canonical="works_at", project_id=None)
        seen = []

        def call(prompt, n):
            seen.append(prompt)
            return json.dumps({"contradictions": []})

        judge.judge(call, [c], "Maria", "works at", "Acme", single_value=lambda c: (False, None), since=ACME)
        new_part = seen[0].split("NEW fact:")[1]
        assert "Maria works at Acme (since 2024-01-01)" in new_part
        assert "(since 2025-06-01)" in seen[0].split("NEW fact:")[0]
        assert contradiction.RULE_DATES in seen[0]

    def test_a_phrase_in_the_past_carries_no_date(self):
        """The date of a phrase in the past is when it was said, not when it
        began: it must not be written as "since"."""
        c = NS(id=uuid.uuid4(), subject_label="Globex", subject_canonical="Globex", predicate_raw="uses",
               object_label="Salesforce", valid_from=GLOBEX, predicate_canonical="uses", project_id=None)
        seen = []
        judge.judge(lambda p, n: seen.append(p) or json.dumps({"contradictions": []}), [c],
                        "Globex", "uses", "Salesforce", single_value=lambda c: (False, None), past=True,
                        since=ACME)
        new_part = seen[0].split("NEW fact:")[1]
        assert contradiction.PAST_MARK.strip() in new_part and "since" not in new_part

    def test_the_batch_carries_the_date_of_every_new_fact(self, db, tenant):
        globex = _fact_row(db, tenant, "Globex", GLOBEX)
        new_id = uuid.uuid4()
        seen = []
        judge.batch(
            db, AgentFact,
            [dict(id=new_id, subject="Maria", predicate_raw="works at", predicate_canonical="works_at",
                  object="Acme", valid_from=ACME)],
            gather=lambda nf: [globex], single_value_of_new=lambda nf: (False, None),
            classify=lambda groups: seen.extend(groups) or {},
        )
        assert seen[0]["new_text"] == "Maria works at Acme (since 2024-01-01)"

    def test_the_rule_is_in_both_agents_judges(self):
        # memory_core 4.0.0: the Human judge, which never had it, left the code.
        assert contradiction.RULE_DATES in contradiction.AGENTS_JUDGE
        assert contradiction.RULE_DATES in contradiction.AGENTS_JUDGE_BATCH
        assert "{DATE}" not in contradiction.AGENTS_JUDGE + contradiction.AGENTS_JUDGE_BATCH


# --------------------------------------------- two memories in reverse, from the cloud

def _judge_that_sees_contradictions(prompt, max_tokens):
    """A fake judge: every earlier fact shown to it is contradicted (in the
    single call and in the batch)."""
    if "[new_id=" in prompt:
        results = []
        for block in prompt.split("[new_id=")[1:]:
            nid = block.split("]", 1)[0]
            ids = re.findall(r"- id=([0-9a-f-]{36})", block)
            results.append({"new_id": nid, "contradictions": [{"existing_id": i} for i in ids]})
        return json.dumps({"results": results})
    ids = re.findall(r"- id=([0-9a-f-]{36})", prompt)
    return json.dumps({"contradictions": [{"existing_id": i} for i in ids]})


class _Provider:
    """The fake judge behind this product's provider (`core.llm`)."""

    def complete(self, messages, **kw):
        return LLMResult(text=_judge_that_sees_contradictions(messages[0]["content"], kw.get("max_tokens")))


@pytest.fixture
def cloud(monkeypatch):
    from korely_agent.services.facts import service as svc
    from korely_agent.services.memory_core.fact_extractor import ExtractedFact

    monkeypatch.setattr(svc, "_embed_fact", lambda *a, **k: None)
    provider = _Provider()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: provider)
    monkeypatch.setattr(predicates, "_ask", lambda: None)

    def extract(text, **k):
        company = text.split()[-1].strip(".")
        return [ExtractedFact(subject="Maria", predicate_raw="works_at", object=company, confidence=0.9,
                              subject_type="person")]

    stub_extraction(monkeypatch, svc, extract)
    return svc


def _write(svc, db, tenant, text, event):
    m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content=text, event_time=event,
                    graph_sync_status="pending")
    db.add(m)
    db.commit()
    db.refresh(m)
    return svc.extract_and_persist_facts(db, tenant.id, memory=m, valid_from=event)


@pytest.mark.parametrize("batched", [False, True], ids=["single_judge", "batched_judge"])
def test_two_memories_exported_most_recent_first(cloud, db, tenant, monkeypatch, batched):
    """Globex (2025-06) written before Acme (2024-01): no row closed before
    beginning, Globex open, Acme history up to 2025-06-01. The same state as
    the right order, read with as_of."""
    from korely_agent.services import facts as agent_facts

    monkeypatch.setattr(cloud, "_BATCHED_JUDGE", batched)
    _write(cloud, db, tenant, "Maria works at Globex.", GLOBEX)
    _write(cloud, db, tenant, "Maria works at Acme.", ACME)

    rows = {r.object_label: r for r in _never_closed_before_beginning(db, tenant)}
    assert set(rows) == {"Globex", "Acme"}
    assert rows["Globex"].invalid_at is None
    assert _aware(rows["Acme"].invalid_at) == GLOBEX and rows["Acme"].invalidated_by_id == rows["Globex"].id

    def as_it_was(when):
        rows, _ = agent_facts.retrieve_agent_facts(db, tenant.id, as_of=when, project_id=EVERY_PROJECT)
        return {r.object_label for r in rows}

    assert as_it_was(datetime(2024, 6, 1, tzinfo=timezone.utc)) == {"Acme"}
    assert as_it_was(datetime(2025, 9, 1, tzinfo=timezone.utc)) == {"Globex"}
    rows, _ = agent_facts.retrieve_agent_facts(db, tenant.id, project_id=EVERY_PROJECT)
    assert {r.object_label for r in rows} == {"Globex"}


@pytest.mark.parametrize("batched", [False, True], ids=["single_judge", "batched_judge"])
def test_in_the_right_order_as_before(cloud, db, tenant, monkeypatch, batched):
    monkeypatch.setattr(cloud, "_BATCHED_JUDGE", batched)
    _write(cloud, db, tenant, "Maria works at Acme.", ACME)
    _write(cloud, db, tenant, "Maria works at Globex.", GLOBEX)
    rows = {r.object_label: r for r in _never_closed_before_beginning(db, tenant)}
    assert rows["Globex"].invalid_at is None
    assert _aware(rows["Acme"].invalid_at) == GLOBEX and rows["Acme"].invalidated_by_id == rows["Globex"].id


# ------------------------------------------------- the claim in event order

class TestTheClaimInEventOrder:
    def _memories(self, db, tenant):
        """Arrived most recent first: Globex created first, Acme after."""
        earlier = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
        globex = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content="Maria works at Globex.",
                             event_time=GLOBEX, graph_sync_status="pending", created_at=earlier)
        acme = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content="Maria works at Acme.",
                           event_time=ACME, graph_sync_status="pending", created_at=earlier + timedelta(seconds=1))
        db.add_all([globex, acme])
        db.commit()
        return globex, acme

    def test_with_the_per_person_cap_it_takes_the_oldest_event_first(self, db, tenant):
        globex, acme = self._memories(db, tenant)
        taken = worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=10,
                           person=(AgentMemory.tenant_id, AgentMemory.end_user_id), per_person=1)
        assert [m.id for m in taken] == [acme.id]

    def test_without_a_cap_in_event_order(self, db, tenant):
        globex, acme = self._memories(db, tenant)
        taken = worker.claim_pending(db, AgentMemory, limit=1, expiry_minutes=10)
        assert [m.id for m in taken] == [acme.id]

    def test_without_an_event_time_the_arrival_counts(self, db, tenant):
        earlier = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
        a = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content="a", graph_sync_status="pending",
                        created_at=earlier)
        b = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content="b", graph_sync_status="pending",
                        created_at=earlier + timedelta(seconds=1), event_time=earlier - timedelta(days=1))
        db.add_all([a, b])
        db.commit()
        taken = worker.claim_pending(db, AgentMemory, limit=2, expiry_minutes=10)
        assert [m.id for m in taken] == [b.id, a.id]
