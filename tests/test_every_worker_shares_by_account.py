"""Every background worker takes its work by account, in turns (audit of 27
Oct 2026).

The audit of 20 Oct 2026 made the facts drain, the batch claim and the
webhook round fair among accounts, and the next one found that each sibling still had a way past it: the
vector drain was one install-wide FIFO by the row's own age, the batch claim
shared by job count while one thread ran each claimed job to its end, and the
facts drain's share did not stop one person's second group from starting
while the first ran. No guard asked the question, so each queue was checked
by its author only.

Two halves, because a share can be absent in two ways:

  * the SCAN reads korely_agent as a syntax tree and fails on a function that
    claims work (`skip_locked`, `worker.claim_pending`) and is not listed in
    CLAIMS with the helper that shares it by account. A new queue cannot be
    added without saying how it is fair;
  * the BEHAVIOUR gives each of the three workers one account with a deep,
    older queue and one with a single newer item, and asks that the second is
    served in the first pass. A listed helper that does not do its job fails
    here.

What neither can see: whether a new waiting room (a lock, a semaphore) is fair
among accounts. That stays the review's.
"""
from __future__ import annotations

import ast
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory, Tenant

PACKAGE = Path(__file__).resolve().parent.parent / "korely_agent"

# (file, function that claims) -> the name that shares the claim by account.
# The name must appear in that function or in a function of the same file that
# calls it.
CLAIMS = {
    ("services/facts/drain.py", "_claim"): "_each_account_its_share",
    ("services/batch.py", "process_batch_jobs"): "_each_account_its_share",
    ("services/vectors.py", "_claim_one"): "_each_account_its_share",
    ("services/webhooks.py", "_claim"): "partition_by",  # keyword of the ranking window
}


# Functions that lock rows to keep a write correct, not to take work from a
# queue for a worker: one line each, and why it is not a queue.
LOCKS_NOT_QUEUES = {
    ("db/bounded.py", "lock_rows"): "locks given ids for a request's own write",
    ("hosted/billing.py", "_in_order"): "locks one account's billing rows in id order",
    ("hosted/billing.py", "hold_account"): "locks one account's row while its plan is decided",
    ("hosted/claim.py", "_still_live"): "locks the key presented until its adoption commits",
    ("hosted/claim.py", "_hold_the_account"): "locks the adopted account's row until it commits",
    ("hosted/closing.py", "_erase"): "locks the account's row it is about to delete",
    ("core/auth.py", "still_admin"): "shares the account's row while a request writes with admin",
    ("services/batch.py", "_item_now"): "reads one item under its job's lock, inside a claimed job",
    ("services/batch.py", "_scrub"): "erasure rewrites a job's payload under its lock",
    ("services/end_user_places.py", "rename_in_lists"): "an end user's rename under lock",
    ("services/keys.py", "verify_api_key"): "its own key's last_used_at, skipped while another holds the row",
    ("services/facts/erasure.py", "erase_memory"): "one memory's erasure under lock",
    ("services/facts/erasure.py", "forget_memory"): "one memory's forget under lock",
    ("services/memory_engine.py", "_claimed_now"): "re-reads one claimed memory under lock",
    ("services/oauth.py", "_exchange_code"): "one grant, spent once",
    ("services/projects.py", "_hold_the_project"): "locks one project for its own change",
    ("services/projects.py", "get_project"): "locks one project row for a change",
    ("services/vectors.py", "_ended_while_judged"): "locks the facts of one judgement, by id",
    ("services/webhooks.py", "_write_outcome"): "one delivery's outcome under its row",
    ("services/webhooks.py", "_to_send"): "one claimed delivery's drop under its row, "
                                          "inside a round `_claim` shared",
    ("services/webhooks.py", "enable_endpoint"): "one endpoint's change under its row",
}


def _is_claim(node) -> bool:
    """A node that claims work: a row lock of any kind (`with_for_update`,
    `skip_locked`), memory_core's `claim_pending`, or a write that sets a status to
    'processing' (an assignment, or a value in an update's dict)."""
    if isinstance(node, ast.keyword) and node.arg == "skip_locked":
        return True
    if isinstance(node, ast.Attribute) and node.attr in ("with_for_update", "claim_pending"):
        return True
    value = None
    if isinstance(node, ast.Assign):
        value = node.value
    elif isinstance(node, ast.Dict):
        return any(_is_processing(v) for v in node.values)
    return value is not None and _is_processing(value)


def _is_processing(v) -> bool:
    return (isinstance(v, ast.Constant) and v.value == "processing") or (
        isinstance(v, ast.Name) and v.id == "STATUS_PROCESSING")


def _claiming_functions():
    """[(relative file, function name, source)] of every function that claims
    rows for a worker."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith("services/memory_core/"):
            continue  # the engine; its own claim is shared by the caller's filter
        source = path.read_text()
        for fn in ast.walk(ast.parse(source)):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    _is_claim(n) for n in ast.walk(fn)):
                found.append((rel, fn.name, ast.get_source_segment(source, fn) or ""))
    return found


def _functions_of(rel: str) -> dict:
    return {fn.name: fn for fn in ast.walk(ast.parse((PACKAGE / rel).read_text()))
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _uses(fn, helper: str) -> bool:
    """The function CALLS `helper` (a name, an attribute) or passes it as a
    keyword: a word in a comment or a docstring is not a share."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if (isinstance(f, ast.Name) and f.id == helper) or (
                    isinstance(f, ast.Attribute) and f.attr == helper):
                return True
            if any(k.arg == helper for k in n.keywords):
                return True
    return False


def _calls(fn, name: str) -> bool:
    return _uses(fn, name)


def test_every_claim_of_work_is_listed_with_its_share():
    unlisted = sorted({(rel, name) for rel, name, _s in _claiming_functions()} - set(CLAIMS) - set(LOCKS_NOT_QUEUES))
    assert not unlisted, (
        "These functions claim work for a worker and are not in CLAIMS, so nothing says how "
        f"they are fair among accounts: {unlisted}")


def test_every_listed_claim_calls_its_share():
    missing = []
    for (rel, name), helper in CLAIMS.items():
        functions = _functions_of(rel)
        if name not in functions:
            missing.append((rel, name, "no such function"))
            continue
        callers = [fn for other, fn in functions.items() if other != name and _calls(fn, name)]
        if not any(_uses(fn, helper) for fn in [functions[name], *callers]):
            missing.append((rel, name, f"neither it nor a caller of it calls {helper}"))
    assert not missing, f"A claim of work without a per-account share: {missing}"


def _partitions_by(fn, attr: str) -> bool:
    """The function ranks with a window `over(partition_by=X.<attr>)`: the
    keyword alone would pass a window split by anything else (30 Oct 2026)."""
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "over"):
            for k in n.keywords:
                if (k.arg == "partition_by" and isinstance(k.value, ast.Attribute)
                        and k.value.attr == attr):
                    return True
    return False


def test_the_webhook_round_takes_its_turns_by_account():
    """`partition_by` in CLAIMS says only that the round has a window; the
    turns are per account only when the window is split by `tenant_id`. A
    split by endpoint would give an account one turn per endpoint and let it
    fill the round (30 Oct 2026)."""
    assert _partitions_by(_functions_of("services/webhooks.py")["_claim"], "tenant_id"), (
        "webhooks._claim no longer ranks its deliveries over(partition_by=<model>.tenant_id): "
        "its turns are not one per account")
    by_endpoint = ast.parse(
        "def _claim(db):\n"
        "    return func.row_number().over(partition_by=D.endpoint_id, order_by=D.id)\n")
    assert not _partitions_by(by_endpoint, "tenant_id")


def test_the_scan_sees_what_it_claims_to_see():
    """The detector itself: a lock of any kind, a status set to 'processing',
    and a helper named only in a comment are told apart."""
    src = ast.parse(
        "def a(q):\n    return q.with_for_update(of=X)\n"
        "def b(j):\n    j.status = 'processing'\n"
        "def c(q):\n    q.update({'graph_sync_status': 'processing'})\n"
        "def d(q):\n    # _each_account_its_share\n    return q.all()\n")
    claims = {f.name for f in ast.walk(src) if isinstance(f, ast.FunctionDef)
              and any(_is_claim(n) for n in ast.walk(f))}
    assert claims == {"a", "b", "c"}
    d = next(f for f in ast.walk(src) if isinstance(f, ast.FunctionDef) and f.name == "d")
    assert not _uses(d, "_each_account_its_share")


# ── behaviour ───────────────────────────────────────────────────────────────


def _another_account(db) -> Tenant:
    t = Tenant(id=uuid.uuid4(), name="Second account")
    db.add(t)
    db.commit()
    return t


NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def test_the_facts_drain_serves_the_account_with_one_newer_memory(db, tenant, monkeypatch):
    from korely_agent.services.facts import drain

    other = _another_account(db)
    for k in range(12):
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=f"deep{k % 4}",
                           content=f"old {k}", graph_sync_status="pending",
                           created_at=NOW + timedelta(seconds=k), event_time=NOW + timedelta(seconds=k)))
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=other.id, end_user_id="lone", content="new",
                       graph_sync_status="pending", created_at=NOW + timedelta(hours=1),
                       event_time=NOW + timedelta(hours=1)))
    db.commit()
    claimed = drain._claim(db, 4)
    assert other.id in {m.tenant_id for m in claimed}, "the newer account waited behind the older"


def test_the_vector_drain_serves_the_account_with_one_newer_row(db, tenant, monkeypatch):
    from korely_agent.services import vectors

    other = _another_account(db)
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
    seen = []

    def embed(text, **kw):
        seen.append(text)
        return [0.1] * EMBEDDING_DIM

    monkeypatch.setattr("korely_agent.core.llm.embed", embed)
    monkeypatch.setattr("korely_agent.services.memory_engine.embed", embed)
    for k in range(12):
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="deep",
                           content=f"old {k}", embedding_status="pending",
                           created_at=NOW + timedelta(seconds=k)))
    db.add(AgentMemory(id=uuid.uuid4(), tenant_id=other.id, end_user_id="lone", content="new",
                       embedding_status="pending", created_at=NOW + timedelta(hours=1)))
    db.commit()
    vectors.process_pending_vectors(db, limit=4)
    assert "new" in seen, "the newer account's vector waited behind the older account's backlog"


@pytest.fixture
def imports(monkeypatch):
    """The items the batch worker writes, as (account, content) in order."""
    from korely_agent.services import batch

    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])
    order = []

    def one(db, tenant_id, item, **kw):
        order.append((tenant_id, item["content"]))
        return str(uuid.uuid4()), None

    monkeypatch.setattr(batch, "_import_one", one)
    return order


def _job(db, tenant_id, size, tag, age):
    from korely_agent.services import batch

    job = batch.enqueue_batch(db, tenant_id, [{"content": f"{tag} {i}", "user_id": "u"}
                                              for i in range(size)])
    job.created_at = NOW + timedelta(minutes=age)
    db.commit()
    return job


def test_the_batch_worker_serves_a_small_job_of_another_account_first(db, tenant, imports):
    from korely_agent.services import batch

    other = _another_account(db)
    _job(db, tenant.id, 12, "a1", 0)
    _job(db, tenant.id, 12, "a2", 1)
    _job(db, other.id, 1, "b", 2)
    batch.process_batch_jobs(db, limit=5)
    first_of_b = next(i for i, (t, _c) in enumerate(imports) if t == other.id)
    assert first_of_b < 12, f"the small import was written after {first_of_b} items of the other account"


def test_the_batch_worker_claims_every_ready_account_even_past_its_limit(db, tenant, imports):
    from korely_agent.services import batch

    accounts = [tenant] + [_another_account(db) for _ in range(5)]
    for n, t in enumerate(accounts):
        _job(db, t.id, 2, f"t{n}", n)
    batch.process_batch_jobs(db, limit=5)
    assert {t for t, _c in imports} == {t.id for t in accounts}, (
        "the newest account's job was left out of every claim behind the older ones")


def test_the_webhook_round_serves_the_account_with_one_newer_delivery(db, tenant, monkeypatch):
    """The deep account's deliveries go to five endpoints and it is first in
    turn 1 whatever the uuids (the rotation starts after `other`): turns split
    by endpoint would give it five places of turn 1 and fill the round of
    four, so only turns by account pass (30 Oct 2026)."""
    from korely_agent.db.models import AgentWebhookDelivery, AgentWebhookEndpoint
    from korely_agent.services import webhooks

    other = _another_account(db)
    endpoints = {}
    for t, count in ((tenant, 5), (other, 1)):
        for _n in range(count):
            e = AgentWebhookEndpoint(id=uuid.uuid4(), tenant_id=t.id, url="https://example.invalid/h",
                                     secret="whsec_x", events=["*"])
            db.add(e)
            endpoints.setdefault(t.id, []).append(e.id)
    db.commit()
    monkeypatch.setattr(webhooks, "_served_last", other.id)
    for k in range(10):
        db.add(AgentWebhookDelivery(id=uuid.uuid4(), tenant_id=tenant.id,
                                    endpoint_id=endpoints[tenant.id][k % 5],
                                    event_type="memory.created", payload={}, status="pending",
                                    created_at=NOW + timedelta(seconds=k)))
    db.add(AgentWebhookDelivery(id=uuid.uuid4(), tenant_id=other.id, endpoint_id=endpoints[other.id][0],
                                event_type="memory.created", payload={}, status="pending",
                                created_at=NOW + timedelta(hours=1)))
    db.commit()
    rows, _attempts = webhooks._claim(db, 4)
    assert other.id in {d.tenant_id for d in rows}


def test_the_person_exclusion_and_the_account_share_work_together(db, tenant):
    """Review of C2: a person with a memory in hand is left out, the person's
    other memories of the account too, and the second account is still served."""
    from sqlalchemy import func
    from korely_agent.services.facts import drain

    other = _another_account(db)
    for k in range(6):
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id=f"p{k % 2}",
                           content=f"a {k}", graph_sync_status="processing" if k == 0 else "pending",
                           graph_sync_claimed_at=func.now() if k == 0 else None,
                           created_at=NOW + timedelta(seconds=k), event_time=NOW + timedelta(seconds=k)))
    for k in range(3):
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=other.id, end_user_id="q",
                           content=f"b {k}", graph_sync_status="pending",
                           created_at=NOW + timedelta(hours=1, seconds=k),
                           event_time=NOW + timedelta(hours=1, seconds=k)))
    db.commit()
    got = sorted((m.tenant_id == tenant.id, m.end_user_id) for m in drain._claim(db, 20))
    assert {e for a, e in got if a} == {"p1"} and {e for a, e in got if not a} == {"q"}
