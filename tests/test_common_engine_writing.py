"""Piece 3b of the common facts engine: writing one fact is memory_core's
(`writing.write_fact`). This file calls `_persist_one` as a direct write
does (POST /v1/facts, a correction from the screen), with `defer=False`:
the fact is judged as it is written. A memory's write leaves its current facts
to the batched judge of the whole write instead (`_BATCHED_JUDGE`, on since
0.1.22; tests/test_engine_judge.py and tests/test_engine_writing.py), and
judges only a past statement as it is written (audit of 17 Oct 2026: this
said the engine had no batched judge). The rules of a write: canonical names
from the aliases, reconfirmation, the past tense, the insert in a savepoint,
the identity alias, the vector before the judge, the judge.

Until 2026-10-06 this file also held `_persist_one` as it was on 0.1.15-dev,
copied verbatim (230 lines), and wrote every scenario once with it and once
with the code of today, comparing the stores. The equivalence held in every
release, except the two changes memory_core 3.0.0 made on purpose, already
written below as behaviour; the copy kept internal names alive and proved
nothing about a deliberate change (the code audit of 6 October). The
scenarios, the scripted model and the embedder are the same, and what each
write must return and leave in the store is said here.
"""
import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from korely_agent.core.llm import EMBEDDING_DIM, LLMResult
from korely_agent.db.models import AgentEntityAlias, AgentFact, AgentMemory, Tenant
from korely_agent.services.facts import service as S


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class Model:
    """The vocabulary answered the way a model would; the judge closes every
    candidate whose line names something `stale`."""
    stale = ("Priority", "Acme")

    def complete(self, messages, **kw):
        p = messages[0]["content"]
        if "EXISTING facts:" in p:
            existing = p.split("EXISTING facts:", 1)[1].split("NEW fact", 1)[0]
            ids = [UUID_RE.search(line).group(0) for line in existing.splitlines()
                   if UUID_RE.search(line) and any(s in line for s in self.stale)]
            return LLMResult(text='{"contradictions": [%s]}' % ", ".join(
                '{"existing_id": "%s", "reason": "stale"}' % i for i in ids))
        if '"family"' in p:
            return LLMResult(text='{"family": "other"}')
        if "many_values" in p:
            return LLMResult(text='{"many_values": false}')
        if '{"past": false}' in p:
            return LLMResult(text='{"past": false}')
        return LLMResult(text='{"same_as": null}')


def vector(text):
    h = hashlib.sha256(text.encode()).digest()
    return [((h[i % 32] / 255.0) - 0.5) for i in range(EMBEDDING_DIM)]


@pytest.fixture(autouse=True)
def model(monkeypatch):
    m = Model()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: m)
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: vector(text))
    return m


SCENARIOS = {
    "a new fact, then the same one again": [
        dict(subject="Maria", predicate_raw="lives_in", obj="Milan", m=0),
        dict(subject="Maria", predicate_raw="lives_in", obj="Milan", m=1),
    ],
    "a change the judge closes, dated by the write": [
        dict(subject="c4821", predicate_raw="subscribes_to", obj="Priority Support plan", m=0,
             valid_from=NOW - timedelta(days=200)),
        dict(subject="c4821", predicate_raw="subscribes_to", obj="Basic plan", m=1,
             valid_from=NOW - timedelta(days=100)),
    ],
    "a past statement closes its true twin": [
        dict(subject="Globex", predicate_raw="uses", obj="Salesforce", m=0,
             valid_from=NOW - timedelta(days=90)),
        dict(subject="Salesforce", predicate_raw="serves_as_crm_for", obj="Globex", m=1),
        dict(subject="Globex", predicate_raw="uses", obj="Salesforce", tense="past", m=2,
             valid_from=NOW - timedelta(days=10)),
    ],
    "a past statement reconfirms the closed history": [
        dict(subject="Globex", predicate_raw="uses", obj="Jira", m=0, valid_from=NOW - timedelta(days=90)),
        dict(subject="Globex", predicate_raw="uses", obj="Jira", tense="past", m=1),
        dict(subject="Globex", predicate_raw="uses", obj="Jira", tense="past", m=2),
    ],
    "an identity statement makes the alias, and the old name finds the facts": [
        dict(subject="Acme", predicate_raw="based_in", obj="Milan", m=0),
        dict(subject="Acme", predicate_raw="renamed_to", obj="Globex", subject_type="organization", m=1),
        dict(subject="Globex", predicate_raw="based_in", obj="Milan", m=2),
    ],
    "a list in one memory does not close itself (BUG-123)": [
        dict(subject="Globex", predicate_raw="sells", obj="Priority Radar", m=0),
        dict(subject="Globex", predicate_raw="sells", obj="Beacon", m=0),
    ],
}


def play(db, fn, steps):
    t = Tenant(id=uuid.uuid4(), name=f"t-{uuid.uuid4().hex[:6]}")
    db.add(t)
    db.commit()
    mems = []
    for i in range(3):
        m = AgentMemory(id=uuid.uuid4(), tenant_id=t.id, content=f"memory {i}", end_user_id="u1")
        db.add(m)
        mems.append(m)
    db.commit()
    returned = []
    for st in steps:
        st = dict(st)
        fact, closed = fn(
            db, t.id, end_user_id="u1", agent_id="bot", run_id=None,
            subject=st["subject"], subject_type=st.get("subject_type", "unknown"),
            predicate_raw=st["predicate_raw"], obj=st["obj"], object_is_literal=False,
            confidence=0.9, source_memory_id=mems[st["m"]].id, model_used="m",
            project_id=None, valid_from=st.get("valid_from"), tense=st.get("tense", "current"),
        )
        db.commit()
        returned.append((_triple(fact), sorted(_triple(db.get(AgentFact, c)) for c in closed)))
    return returned, _snapshot(db, t.id, {m.id: i for i, m in enumerate(mems)})


def _triple(f):
    return (f.subject_canonical, f.predicate_canonical, f.object_canonical)


def _day(d):
    return None if d is None else (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).isoformat()[:16]


def _snapshot(db, tenant_id, mem_index):
    facts = db.query(AgentFact).filter(AgentFact.tenant_id == tenant_id).all()
    by_id = {f.id: f for f in facts}
    # Sorted on their repr: two rows can share a triple (a plan and the fact
    # it became, since memory_core 3.0.0), and a None next to a string does
    # not compare.
    rows = sorted((
        (_triple(f), f.subject_label, f.tense, f.predicate_family, f.observation_count,
         sorted(mem_index.get(UUID(s)) for s in (f.source_memory_ids or [])),
         mem_index.get(f.source_memory_id),
         f.invalid_at is not None,
         _triple(by_id[f.invalidated_by_id]) if f.invalidated_by_id in by_id else None,
         f.embedding is not None)
        for f in facts), key=repr)
    # Dates the write chose (valid_from given, the closing at the closer's
    # valid time) compare exactly; "now" differs between the two runs by the
    # time they took, so it compares to the minute.
    dates = sorted(((_triple(f), _day(f.valid_from), _day(f.invalid_at)) for f in facts), key=repr)
    aliases = sorted((a.alias_label, a.canonical_label) for a in
                     db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant_id).all())
    return rows, dates, aliases


MARIA = ("Maria", "lives_in", "Milan")
PRIORITY = ("c4821", "subscribes_to", "Priority Support plan")
BASIC = ("c4821", "subscribes_to", "Basic plan")
USES_SF = ("Globex", "uses", "Salesforce")
SF_CRM = ("Salesforce", "serves_as_crm_for", "Globex")
JIRA = ("Globex", "uses", "Jira")

# Per scenario: what each write returns (the fact, the facts it closed), the
# rows left in the store (triple, subject as written, tense, family, times
# seen, the memories that stated it, the first one, closed, closed by, has a
# vector), the dates the write was given, and the aliases. The family is
# "other" but for our own schema's `renamed_to`: since memory_core 3.3.0 no
# static map of English predicates names it (the rule of 8 Oct 2026),
# the learned vocabulary asks the model, and here no model answers.
EXPECTED = {
    "a new fact, then the same one again": dict(
        returned=[(MARIA, []), (MARIA, [])],
        rows=[(MARIA, "Maria", "current", "other", 2, [0, 1], 0, False, None, True)],
        dates={}, aliases=[]),
    "a change the judge closes, dated by the write": dict(
        returned=[(PRIORITY, []), (BASIC, [PRIORITY])],
        rows=[(BASIC, "c4821", "current", "other", 1, [1], 1, False, None, True),
              (PRIORITY, "c4821", "current", "other", 1, [0], 0, True, BASIC, True)],
        # The old plan ends where the new one began, not when it was written.
        dates={BASIC: (NOW - timedelta(days=100), None),
               PRIORITY: (NOW - timedelta(days=200), NOW - timedelta(days=100))},
        aliases=[]),
    "a past statement closes its true twin": dict(
        returned=[(USES_SF, []), (SF_CRM, []), (USES_SF, [])],
        rows=[(USES_SF, "Globex", "past", "other", 1, [0], 0, True, None, True),
              (SF_CRM, "Salesforce", "current", "other", 1, [1], 1, False, None, True)],
        dates={USES_SF: (NOW - timedelta(days=90), NOW - timedelta(days=10))},
        aliases=[]),
    "a past statement reconfirms the closed history": dict(
        returned=[(JIRA, []), (JIRA, []), (JIRA, [])],
        rows=[(JIRA, "Globex", "past", "other", 2, [0, 2], 0, True, None, True)],
        dates={}, aliases=[]),
    "an identity statement makes the alias, and the old name finds the facts": dict(
        # "based_in" stays itself: until memory_core 3.3.0 a hand-written
        # synonym made a company "live in" Milan (the rule of 8 Oct).
        # The fact embedded as "Acme based_in Milan" is renamed in place by
        # the alias: its vector goes and it waits for the drain's vector of
        # "Globex based_in Milan" (no vector, audit of 20 Oct 2026).
        returned=[(("Acme", "based_in", "Milan"), []), (("Acme", "renamed_to", "Globex"), []),
                  (("Globex", "based_in", "Milan"), [])],
        rows=[(("Acme", "renamed_to", "Globex"), "Acme", "current", "identity", 1, [1], 1, False, None, True),
              (("Globex", "based_in", "Milan"), "Acme", "current", "other", 2, [0, 2], 0, False, None, False)],
        dates={}, aliases=[("Acme", "Globex")]),
    "a list in one memory does not close itself (BUG-123)": dict(
        returned=[(("Globex", "sells", "Priority Radar"), []), (("Globex", "sells", "Beacon"), [])],
        rows=[(("Globex", "sells", "Beacon"), "Globex", "current", "other", 1, [0], 0, False, None, True),
              (("Globex", "sells", "Priority Radar"), "Globex", "current", "other", 1, [0], 0, False, None, True)],
        dates={}, aliases=[]),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_each_write_returns_and_leaves_what_it_should(db, name):
    want = EXPECTED[name]
    returned, (rows, dates, aliases) = play(db, S._persist_one, SCENARIOS[name])
    assert returned == want["returned"], "a write returned another fact or closed other facts"
    assert rows == want["rows"], "the store differs"
    assert aliases == want["aliases"]
    by_triple = {d[0]: d[1:] for d in dates}
    for triple, (began, ended) in want["dates"].items():
        assert by_triple[triple] == (_day(began), _day(ended)), f"the dates of {triple}"


# Two behaviours memory_core 3.0.0 changed on purpose (decisions of
# 2026-10-06), so they are written down as what the
# engine does instead of compared with 0.1.15-dev, which did otherwise.
INTENDED = {
    "planned, then current": [
        dict(subject="Maria", predicate_raw="moves_to", obj="Rome", tense="planned", m=0),
        dict(subject="Maria", predicate_raw="moves_to", obj="Rome", m=1),
    ],
    "a past statement with nothing before it": [
        dict(subject="Maria", predicate_raw="worked_at", obj="Initech", tense="past", m=0,
             valid_from=NOW - timedelta(days=400)),
    ],
}


def test_a_plan_that_comes_true_stays_in_the_history(db):
    """The plan's row closes when it comes true, closed by the fact it became,
    which is a row of its own, true from then (Zep/Graphiti, arXiv:2501.13956
    2.2.3: only t_invalid is written, the history stays). Until 2.11.0 one
    row, promoted in place with the plan's date: `as_of` before it came true
    answered with a fact that was still a plan."""
    returned, (rows, _dates, _aliases) = play(db, S._persist_one, INTENDED["planned, then current"])
    maria = ("Maria", "moves_to", "Rome")
    plan = [r for r in rows if r[2] == "planned"]
    fact = [r for r in rows if r[2] == "current"]
    assert len(plan) == len(fact) == 1
    assert plan[0][7] is True and plan[0][8] == maria, "the plan is closed by the fact it became"
    assert fact[0][7] is False, "the fact is true from then"
    assert returned[1] == (maria, [maria]), "the write returns the new row and names the plan closed"


def test_a_past_statement_ends_when_it_is_said(db):
    """A sentence in the past tense ends when it is said, not on the day it
    is written (memory_core 2.12.0, from the code audit of 6 Oct 2026): 0.1.15-dev
    closed it at the write, so it read as true for the year in between."""
    _returned, (rows, dates, _aliases) = play(db, S._persist_one,
                                             INTENDED["a past statement with nothing before it"])
    assert [r[2] for r in rows] == ["past"]
    ((_triple_, began, ended),) = dates
    assert began == ended == _day(NOW - timedelta(days=400))
