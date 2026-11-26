"""The workers' turns between accounts, past the size of one claim (the four
rows of the audit of 27 Oct 2026 left as known issues until 1.0.40).

Each queue shared its claim among accounts (tests/test_every_worker_shares_by_account.py)
but cut it, once more accounts waited than a claim holds, by something an
account keeps: the vector drain took the 50 accounts with the oldest row,
the batch claim the oldest jobs past 50 accounts, the facts drain the event
time a client sends, and the webhook round rotated from the last account it
claimed, though the claims past the free threads went back unstarted. A
fixed set of accounts then waited while others had a backlog.

Now every queue takes its accounts from one rotation (core/threads.Rotation):
with `n` accounts waiting and `k` served a round, each is served within
ceil(n / k) rounds. The rounds here are driven by hand, the clock never
decides: every bound is written before the run, from n and k.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from korely_agent.core import threads
from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory, Tenant

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
OLD = datetime(2019, 1, 1, 9, 0, tzinfo=timezone.utc)


def _accounts(db, n, tenant=None):
    out = [tenant] if tenant is not None else []
    while len(out) < n:
        t = Tenant(id=uuid.uuid4(), name=f"Account {len(out)}")
        db.add(t)
        out.append(t)
    db.commit()
    return out


# ── the rotation itself ─────────────────────────────────────────────────────


def test_the_rotation_serves_the_least_recently_served_first():
    r = threads.Rotation()
    a, b, c, d = "a", "b", "c", "d"
    assert r.first([c, a, b], 2) == [a, b]
    r.served([a])                       # b was chosen, never started
    assert r.first([a, b, c], 3) == [b, c, a]
    r.served([b, c])
    # d is new: it joins at the end, after those already waiting.
    assert r.first([a, b, c, d], 4) == [a, b, c, d]
    r.served([a])
    assert r.first([a, b, c, d], 4) == [b, c, d, a]


def test_an_account_with_no_work_leaves_the_rotation():
    r = threads.Rotation()
    r.first(["a", "b"], 2)
    r.served(["a", "b"])
    assert r.first(["b"], 5) == ["b"]
    # a comes back with work: at the end, after b, though b was served last.
    assert r.first(["a", "b"], 2) == ["b", "a"]
    assert set(r._last) == {"a", "b"}


def test_n_accounts_k_a_round_each_within_ceil_n_over_k():
    r = threads.Rotation()
    accounts = [f"t{i:02}" for i in range(30)]
    k = 3
    seen = {}
    for rnd in range(1, math.ceil(30 / k) + 1):
        chosen = r.first(accounts, 20)       # a claim of 20, three of them start
        for a in chosen[:k]:
            seen.setdefault(a, rnd)
        r.served(chosen[:k])
    assert set(seen) == set(accounts)


# ── the webhook round ───────────────────────────────────────────────────────


class _ThreeStart:
    """The webhook round's lanes with slow receivers: three attempts start a
    round, the rest of the claim never does and goes back to the queue (what
    `Lanes.run` does when its free threads are taken by earlier attempts)."""

    def __init__(self, k):
        self.k = k
        self.started = []

    def share(self, slots, accounts, most=None):
        return slots

    def free(self, slots):
        return slots

    def running(self):
        return {}

    def wait_idle(self, seconds):
        return True

    def run(self, jobs, *, slots, share, patience, give_up):
        self.started.append([a for a, _fn in jobs[:self.k]])
        return {}, list(range(self.k, len(jobs)))


def test_the_webhook_round_serves_every_account_within_ceil_n_over_k(db, tenant, monkeypatch):
    """30 accounts with a backlog, a claim of 20, three attempts started a
    round: every account has an attempt started within ceil(30 / 3) = 10
    rounds. Rotating from the last account claimed, accounts 4-10, 14-20
    and 24-30 of each window were never started."""
    from korely_agent.db.models import AgentWebhookDelivery, AgentWebhookEndpoint
    from korely_agent.services import webhooks

    lanes = _ThreeStart(3)
    monkeypatch.setattr(webhooks, "_sending", lanes)
    monkeypatch.setattr(webhooks, "_turns", threads.Rotation(), raising=False)
    accounts = _accounts(db, 30, tenant)
    for i, t in enumerate(accounts):
        e = AgentWebhookEndpoint(id=uuid.uuid4(), tenant_id=t.id, url="https://example.invalid/h",
                                 secret="whsec_x", events=["*"])
        db.add(e)
        db.flush()
        for k in range(12):
            db.add(AgentWebhookDelivery(id=uuid.uuid4(), tenant_id=t.id, endpoint_id=e.id,
                                        event_type="memory.created", payload={}, status="pending",
                                        created_at=NOW - timedelta(hours=30 - i, seconds=-k)))
    db.commit()
    bound = math.ceil(30 / 3)
    for _ in range(bound):
        webhooks.process_deliveries(db, limit=20)
    started = {a for rnd in lanes.started for a in rnd}
    assert started == {t.id for t in accounts}, (
        f"{30 - len(started)} accounts had no attempt started in {bound} rounds")


# ── the vector drain ────────────────────────────────────────────────────────


@pytest.fixture
def embedded(monkeypatch):
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    seen = []

    def embed(text, **kw):
        seen.append(text)
        return [0.1] * EMBEDDING_DIM

    monkeypatch.setattr("korely_agent.core.llm.embed", embed)
    monkeypatch.setattr("korely_agent.services.memory_engine.embed", embed)
    return seen


def test_the_vector_drain_serves_sixty_accounts_within_two_passes(db, tenant, embedded,
                                                                   monkeypatch):
    """60 accounts with rows due, 50 a pass: every account has a vector
    within ceil(60 / 50) = 2 passes, the ten with the newest rows too. By
    the oldest row the same 50 accounts took the second pass as well."""
    from korely_agent.services import vectors

    monkeypatch.setattr(vectors, "_turns", {}, raising=False)
    accounts = _accounts(db, 60, tenant)
    for i, t in enumerate(accounts):
        for k in range(3):
            db.add(AgentMemory(id=uuid.uuid4(), tenant_id=t.id, end_user_id="u",
                               content=f"{i} {k}", embedding_status="pending",
                               created_at=(OLD if i < 50 else NOW) + timedelta(minutes=i, seconds=k)))
    db.commit()
    for _ in range(math.ceil(60 / 50)):
        vectors.process_pending_vectors(db, limit=50)
    served = {int(text.split()[0]) for text in embedded}
    assert served == set(range(60)), f"{sorted(set(range(60)) - served)} waited two passes"


# ── the facts drain ─────────────────────────────────────────────────────────


def test_the_facts_drain_serves_every_account_within_two_claims(db, tenant, monkeypatch):
    """25 accounts waiting, a claim of 20: within ceil(25 / 20) = 2 ticks
    every account has a memory written, the five whose memories are dated
    now too. By the event time the 20 accounts that import memories dated
    2019 took every claim until their backlog was gone (four ticks here)."""
    from korely_agent.services.facts import service as facts_service
    from korely_agent.services.facts import drain

    monkeypatch.setattr(drain, "_turns", threads.Rotation(), raising=False)
    written = []

    def drain_one(gdb, m, region=None, when=None):
        m.graph_sync_status = "done"
        gdb.commit()
        written.append(m.tenant_id)
        return True

    monkeypatch.setattr(drain, "_drain_one_memory", drain_one)
    accounts = _accounts(db, 25, tenant)
    for i, t in enumerate(accounts):
        when = (OLD if i < 20 else NOW) + timedelta(minutes=i)
        for k in range(4):
            db.add(AgentMemory(id=uuid.uuid4(), tenant_id=t.id, end_user_id="u",
                               content=f"{i} {k}", graph_sync_status="pending",
                               created_at=NOW + timedelta(seconds=k),
                               event_time=when + timedelta(seconds=k)))
    db.commit()
    for _ in range(math.ceil(25 / 20)):
        facts_service.process_pending_memories(db, limit=20)
        assert drain._draining.wait_idle(30)
    missing = {t.id for t in accounts} - set(written)
    assert not missing, f"{len(missing)} accounts had no memory written in two ticks"


def test_inside_an_account_the_event_order_stays(db, tenant, monkeypatch):
    """The rotation is between accounts only: inside one, the earliest event
    first (the judge needs the earlier episode first)."""
    from korely_agent.services.facts import drain

    monkeypatch.setattr(drain, "_turns", threads.Rotation(), raising=False)
    other = _accounts(db, 2, tenant)[1]
    for t in (tenant, other):
        for k in range(3):
            db.add(AgentMemory(id=uuid.uuid4(), tenant_id=t.id, end_user_id="u",
                               content=f"{k}", graph_sync_status="pending",
                               event_time=NOW - timedelta(days=k)))
    db.commit()
    got = drain._claim(db, 2)
    assert sorted(m.tenant_id for m in got) == sorted([tenant.id, other.id])
    assert {m.content for m in got} == {"2"}, "an account's earliest memory was not its first"


# ── the batch claim ─────────────────────────────────────────────────────────


def test_the_batch_claim_serves_sixty_accounts_within_two_passes(db, tenant, monkeypatch):
    """60 accounts with jobs ready, 50 a pass (MOST_ACCOUNTS_PER_PASS): every
    account's import is written within ceil(60 / 50) = 2 passes. By the
    jobs' age the 50 accounts with the older jobs took both passes."""
    from korely_agent.services import batch

    monkeypatch.setattr(batch, "_turns", threads.Rotation(), raising=False)
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])
    order = []

    def one(db_, tenant_id, item, **kw):
        order.append(tenant_id)
        return str(uuid.uuid4()), None

    monkeypatch.setattr(batch, "_import_one", one)
    accounts = _accounts(db, 60, tenant)
    for i, t in enumerate(accounts):
        for k in range(3):
            job = batch.enqueue_batch(db, t.id, [{"content": f"{i} {k}", "user_id": "u"}])
            job.created_at = (OLD if i < 50 else NOW) + timedelta(minutes=i, seconds=k)
    db.commit()
    for _ in range(math.ceil(60 / batch.MOST_ACCOUNTS_PER_PASS)):
        batch.process_batch_jobs(db, limit=5)
    missing = {t.id for t in accounts} - set(order)
    assert not missing, f"{len(missing)} accounts had no import written in two passes"


# ── a turn chosen and not used (the review of 25 Nov 2026) ─────────────────


def test_an_account_chosen_with_nothing_to_claim_spends_its_turn(db, tenant, embedded,
                                                                  monkeypatch):
    """A queue whose list of accounts holds some with nothing the claim can
    take (a part's own list of accounts, every row of one resting
    after a failure): five such accounts and a pass of five, and the account
    with a row ready is served within ceil(6 / 5) = 2 passes. Kept at the
    head, the five took every place of every pass."""
    import dataclasses

    from korely_agent.services import vectors

    monkeypatch.setattr(vectors, "_turns", {}, raising=False)
    idle = [uuid.UUID(int=i) for i in range(1, 6)]     # first in id order, nothing due
    ready = _accounts(db, 1, tenant)[0]
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=ready.id, end_user_id="u", content="ready",
                       embedding_status="pending", created_at=NOW))
    db.commit()
    kind = dataclasses.replace(vectors._MEMORIES, name="memories listed with idle accounts",
                               accounts=lambda db_: idle + [ready.id])
    for _ in range(math.ceil(6 / 5)):
        vectors.drain_pass(db, kind, limit=5)
    assert "ready" in embedded, "the account with a row ready waited behind the idle ones"


class _StartsThree:
    """The facts drain's lanes with three threads free: the first three
    groups of a tick start and end, the rest never start."""

    def __init__(self):
        self.started = []

    def wait_for_room(self, slots, seconds):
        return slots

    def running(self):
        return {}

    def share(self, slots, accounts, most=None):
        return slots

    def wait_idle(self, seconds):
        return True

    def run(self, jobs, *, slots, share, patience, give_up):
        ended = {}
        for i, (account, fn) in enumerate(jobs[:3]):
            self.started.append(account)
            ended[i] = fn()
        return ended, list(range(3, len(jobs)))


def test_an_account_whose_group_never_started_keeps_its_turn(db, tenant, monkeypatch):
    """25 accounts, a claim of 20, three groups started a tick: every account
    has a group started within ceil(25 / 3) = 9 ticks. One whose group went
    back to the queue unstarted keeps its place at the head; counted as
    served, it went to the end and waited a whole turn again."""
    from korely_agent.services.facts import drain
    from korely_agent.services.facts import service as facts_service

    monkeypatch.setattr(drain, "_turns", threads.Rotation(), raising=False)
    lanes = _StartsThree()
    monkeypatch.setattr(drain, "_draining", lanes)

    def drain_one(gdb, m, region=None, when=None):
        m.graph_sync_status = "done"
        gdb.commit()
        return True

    monkeypatch.setattr(drain, "_drain_one_memory", drain_one)
    accounts = _accounts(db, 25, tenant)
    for i, t in enumerate(accounts):
        for k in range(12):
            db.add(AgentMemory(id=uuid.uuid4(), tenant_id=t.id, end_user_id="u",
                               content=f"{i} {k}", graph_sync_status="pending",
                               event_time=NOW + timedelta(minutes=i, seconds=k)))
    db.commit()
    for _ in range(math.ceil(25 / 3)):
        facts_service.process_pending_memories(db, limit=20)
    missing = {t.id for t in accounts} - set(lanes.started)
    assert not missing, f"{len(missing)} accounts had no group started in 9 ticks"


# ── the second review of 25 Nov 2026 ─────────────────────────────────────


def test_fewer_accounts_than_the_claim_each_has_its_place(db, tenant, monkeypatch):
    """19 accounts waiting and a claim of 20: nine import memories dated
    2019, ten write now. Every account is in the first claim. With a share
    of ceil(20 / 19) = 2 each, memory_core's cut at 20 by event time gave
    the places to the nine importers, and nine accounts had nothing in two
    claims (the review's probe p3)."""
    from korely_agent.services.facts import drain

    monkeypatch.setattr(drain, "_turns", threads.Rotation(), raising=False)
    accounts = _accounts(db, 19, tenant)
    for i, t in enumerate(accounts):
        when = (OLD if i < 9 else NOW) + timedelta(minutes=i)
        for k in range(12):
            db.add(AgentMemory(id=uuid.uuid4(), tenant_id=t.id, end_user_id="u",
                               content=f"{i} {k}", graph_sync_status="pending",
                               created_at=NOW + timedelta(seconds=k),
                               event_time=when + timedelta(seconds=k)))
    db.commit()
    claimed = drain._claim(db, 20)
    assert len(claimed) == 20
    missing = {t.id for t in accounts} - {m.tenant_id for m in claimed}
    assert not missing, f"{len(missing)} of 19 accounts had nothing in a claim of 20"
    # Inside an account, its earliest memories first.
    for t in accounts:
        mine = sorted(int(m.content.split()[1]) for m in claimed if m.tenant_id == t.id)
        assert mine == list(range(len(mine)))


def _ids(n):
    """Accounts whose turn order is known: new to a rotation, they join in id order."""
    return [uuid.UUID(int=0x1000 + i) for i in range(n)]


def _tenants(db, ids):
    for i in ids:
        db.add(Tenant(id=i, name=f"Account {i.int}"))
    db.commit()


def test_a_chosen_account_whose_rows_another_drain_holds_spends_its_turn(db, engine,
                                                                         monkeypatch):
    """The facts drain, a claim of one: the first account of the turn has its
    one memory held by another session (another drain, an erasure). Within
    two claims the second account is claimed; kept at the head, the first
    took every claim."""
    from sqlalchemy.orm import sessionmaker

    from korely_agent.services.facts import drain

    monkeypatch.setattr(drain, "_turns", threads.Rotation(), raising=False)
    a, b = _ids(2)
    _tenants(db, [a, b])
    held = AgentMemory(id=uuid.uuid4(), tenant_id=a, end_user_id="u", content="held",
                       graph_sync_status="pending")
    db.add_all([held, AgentMemory(id=uuid.uuid4(), tenant_id=b, end_user_id="u", content="free",
                                  graph_sync_status="pending")])
    db.commit()
    other = sessionmaker(bind=engine)()
    try:
        other.execute(text("SELECT id FROM agent_memories WHERE id = :i FOR UPDATE"),
                      {"i": held.id})
        got = [m.content for _ in range(2) for m in drain._claim(db, 1)]
    finally:
        other.rollback()
        other.close()
    assert got == ["free"]


def _batch_world(db, monkeypatch, n):
    from korely_agent.services import batch

    monkeypatch.setattr(batch, "_turns", threads.Rotation(), raising=False)
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])
    written = []
    monkeypatch.setattr(batch, "_import_one",
                        lambda db_, tenant_id, item, **kw: (written.append(tenant_id),
                                                            (str(uuid.uuid4()), None))[1])
    ids = _ids(n)
    _tenants(db, ids)
    jobs = [batch.enqueue_batch(db, t, [{"content": "x", "user_id": "u"}]) for t in ids]
    db.commit()
    return batch, ids, jobs, written


def _held(engine, table, row_id):
    from sqlalchemy.orm import sessionmaker

    other = sessionmaker(bind=engine)()
    other.execute(text(f"SELECT id FROM {table} WHERE id = :i FOR UPDATE"), {"i": row_id})
    return other


def test_a_batch_pass_with_nothing_to_take_spends_the_turns_it_chose(db, engine, monkeypatch):
    """One account a pass: the first account's job is held by another
    session, so the pass takes nothing; the next pass serves the second."""
    batch, ids, jobs, written = _batch_world(db, monkeypatch, 2)
    monkeypatch.setattr(batch, "MOST_ACCOUNTS_PER_PASS", 1)
    other = _held(engine, "agent_batch_jobs", jobs[0].id)
    try:
        for _ in range(2):
            batch.process_batch_jobs(db, limit=1)
    finally:
        other.rollback()
        other.close()
    assert written == [ids[1]]


def test_a_batch_pass_spends_the_turn_of_a_chosen_account_it_could_not_take(db, engine,
                                                                            monkeypatch):
    """Two accounts a pass, four accounts, the first one's job held: the
    three others are served within ceil(4 / 2) = 2 passes. Kept at the
    head, the held one took a place of every pass and the fourth waited."""
    batch, ids, jobs, written = _batch_world(db, monkeypatch, 4)
    monkeypatch.setattr(batch, "MOST_ACCOUNTS_PER_PASS", 2)
    other = _held(engine, "agent_batch_jobs", jobs[0].id)
    try:
        for _ in range(2):
            batch.process_batch_jobs(db, limit=2)
    finally:
        other.rollback()
        other.close()
    assert sorted(written) == sorted(ids[1:])


def test_a_webhook_round_spends_the_turn_of_an_account_whose_deliveries_are_held(
        db, engine, monkeypatch):
    from korely_agent.db.models import AgentWebhookDelivery, AgentWebhookEndpoint
    from korely_agent.services import webhooks

    monkeypatch.setattr(webhooks, "_turns", threads.Rotation(), raising=False)
    ids = _ids(2)
    _tenants(db, ids)
    deliveries = []
    for t in ids:
        e = AgentWebhookEndpoint(id=uuid.uuid4(), tenant_id=t, url="https://example.invalid/h",
                                 secret="whsec_x", events=["*"])
        db.add(e)
        db.flush()
        d = AgentWebhookDelivery(id=uuid.uuid4(), tenant_id=t, endpoint_id=e.id,
                                 event_type="memory.created", payload={}, status="pending",
                                 created_at=NOW)
        db.add(d)
        deliveries.append(d.id)
    db.commit()
    other = _held(engine, "agent_webhook_deliveries", deliveries[0])
    try:
        got = [d.tenant_id for _ in range(2) for d in webhooks._claim(db, 1)[0]]
    finally:
        other.rollback()
        other.close()
    assert got == [ids[1]]


def test_the_extra_place_of_a_claim_moves_round(db, tenant, monkeypatch):
    """Three accounts, a claim of seven: two each and one extra. In three
    ticks each account has the extra once (the review's probe p6, third review
    of 25 Nov 2026). Served in the order of the turn, the order never
    changed and the extra went to the same account every tick."""
    from collections import Counter

    from korely_agent.services.facts import drain
    from korely_agent.services.facts import service as facts_service

    monkeypatch.setattr(drain, "_turns", threads.Rotation(), raising=False)
    tick = []

    def drain_one(gdb, m, region=None, when=None):
        m.graph_sync_status = "done"
        gdb.commit()
        tick.append(m.tenant_id)
        return True

    monkeypatch.setattr(drain, "_drain_one_memory", drain_one)
    accounts = _accounts(db, 3, tenant)
    for i, t in enumerate(accounts):
        for u in range(3):
            for k in range(4):
                db.add(AgentMemory(id=uuid.uuid4(), tenant_id=t.id, end_user_id=f"u{u}",
                                   content=f"{i} {u} {k}", graph_sync_status="pending",
                                   event_time=NOW + timedelta(minutes=i, seconds=10 * u + k)))
    db.commit()
    extra = []
    for _ in range(3):
        tick.clear()
        facts_service.process_pending_memories(db, limit=7)
        assert drain._draining.wait_idle(30)
        counts = Counter(tick)
        assert sorted(counts.values()) == [2, 2, 3], counts
        extra.append(max(counts, key=counts.get))
    assert len(set(extra)) == 3, f"the extra place went to {len(set(extra))} account(s) in 3 ticks"
