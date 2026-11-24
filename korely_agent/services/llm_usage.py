"""Which agent caused each model call, and what it cost in tokens.

`core/llm.py` has always computed the token counts. Nothing read them, so on a
machine the operator pays for, the question "which of my agents is spending the
money" had no answer. This module is the missing half: a scope that says who is
responsible, and a row per call.

**Tokens, not euros.** See `AgentLlmUsage`. A rate card changes; a token count
does not.

The scope is a context variable rather than a parameter threaded through ten
call sites, because the alternative is changing the signature of every function
between the drain and the provider, and the version of this that gets written
under that much friction is the version that records nothing.

    with llm_usage.attributed(tenant_id, agent_id=..., memory_id=...):
        with llm_usage.stage("extraction"):
            client.complete(...)

Outside a scope, recording is a no-op: scripts, tests and the odd internal call
must not crash for want of an attribution. That silence is the dangerous half,
so the calls are counted and `unattributed()` returns the count. A test asserts
it stays zero across the drain, which is what stops the next model call from
being added somewhere that never reaches this table.
"""
from __future__ import annotations

import contextlib
import logging
from .memory_core import log_safe
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import List, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# extraction | contradiction | predicates | objects | embedding | operator. Kept short and stable:
# a report groups by this and a renamed stage splits a column in two.
EXTRACTION = "extraction"
CONTRADICTION = "contradiction"
PREDICATES = "predicates"
EMBEDDING = "embedding"
# The questions of a write about its new objects (memory_core 6.1.0
# `objects`, off by default): "is this the same thing as one the memory
# holds?", in a column of their own beside the vocabulary's.
OBJECTS = "objects"
# A run an operator started from the shell on an account's data (`manage
# review-objects`): recorded under the account it read, so the service's
# ceilings count it (hosted/spending.py), in a stage of its own, so the
# account's report never reads it as the account's own use.
OPERATOR = "operator"
UNKNOWN = "unknown"


@dataclass
class _Call:
    stage: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int = 0
    vendor: Optional[str] = None
    region: Optional[str] = None
    # 1.0.36: what the provider said the call cost, in its own currency
    # (OpenRouter's `usage.cost`, US dollars), when it said it; and whether
    # the answer measured the call at all (tokens or a cost). Read by a run
    # with a spending cap (manage._Spending), never stored: the row keeps the
    # tokens, which the rate card prices.
    cost: Optional[float] = None
    measured: bool = True


@dataclass
class _Scope:
    tenant_id: UUID
    agent_id: Optional[str] = None
    memory_id: Optional[UUID] = None
    stage: str = UNKNOWN
    calls: List[_Call] = field(default_factory=list)


_active: ContextVar[Optional[_Scope]] = ContextVar("llm_usage_scope", default=None)

# Calls that happened with nobody to bill. Not an error on its own, but a
# number that should be zero on the paths that matter.
_unattributed = 0


def current_tenant() -> Optional[UUID]:
    """The account the calls made here are billed to; None outside a scope.
    Read by the local embedder to share its one slot among accounts
    (core/local_embedder._Turns)."""
    scope = _active.get()
    return scope.tenant_id if scope is not None else None


def unattributed() -> int:
    """How many model calls this process made outside any scope."""
    return _unattributed


def reset_unattributed() -> None:
    global _unattributed
    _unattributed = 0


@contextlib.contextmanager
def attributed(tenant_id: UUID, *, agent_id: Optional[str] = None,
               memory_id: Optional[UUID] = None, stage: str = UNKNOWN):
    """Everything inside belongs to this tenant, and is written out on exit.

    One scope per unit of work that a person would recognise: one memory being
    drained, one API request. Wrapping a whole batch would lose the batch on a
    crash and would attribute twenty memories to one id.

    No end user: a cost does not need whose (audit of 19 Oct 2026). A
    row that named one could be written while the end user's erasure was
    open, after it had taken them off the cost rows, and keep the erased id.
    """
    scope = _Scope(tenant_id=tenant_id, agent_id=agent_id, memory_id=memory_id, stage=stage)
    token = _active.set(scope)
    try:
        yield scope
    finally:
        _active.reset(token)
        flush(scope)


def billed_to_the_account(fn):
    """A function whose arguments are `(db, tenant_id, ...)` and whose model
    calls are that account's: they run in a scope of their own, so each lands
    in agent_llm_usage and counts toward the Cloud's daily stop, the usage
    report and the embedder's per-account share (audit of 27 Oct 2026: POST
    and PATCH /v1/facts had none)."""
    import functools

    @functools.wraps(fn)
    def run(db, tenant_id, *args, **kwargs):
        from ..core import llm

        llm.forget_failure_here()  # an earlier request's refusal on this thread is not this one's
        with attributed(tenant_id, agent_id=kwargs.get("agent_id")):
            return fn(db, tenant_id, *args, **kwargs)

    return run


@contextlib.contextmanager
def stage(name: str):
    """Narrow the stage for the calls inside. No-op outside a scope."""
    scope = _active.get()
    if scope is None:
        yield
        return
    previous = scope.stage
    scope.stage = name
    try:
        yield
    finally:
        scope.stage = previous


def record(provider: str, model: str, prompt_tokens: int, completion_tokens: int,
           *, stage: Optional[str] = None, cached_tokens: int = 0,
           vendor: Optional[str] = None, region: Optional[str] = None,
           cost: Optional[float] = None, measured: bool = True) -> None:
    """One model call. Never raises: a cost row is not worth a failed write.

    `stage` overrides the scope's current stage. An embedding is an embedding
    whichever part of the pipeline asked for one, and reporting it as
    "extraction" would put the cheap calls and the expensive ones in the same
    column.
    """
    global _unattributed
    scope = _active.get()
    if scope is None:
        _unattributed += 1
        logger.debug("[usage] %s/%s call with no attribution scope", provider, model)
        return
    scope.calls.append(_Call(
        stage=stage or scope.stage, provider=provider or "", model=model or "",
        prompt_tokens=int(prompt_tokens or 0),
        completion_tokens=int(completion_tokens or 0),
        cached_tokens=int(cached_tokens or 0),
        vendor=vendor, region=region, cost=cost, measured=measured,
    ))


def flush(scope: _Scope) -> int:
    """Write the scope's calls. Own session, own transaction, never raises.

    Same rule as the audit trail: the work that succeeded must not be reported
    as failed because the bookkeeping could not be stored, and the failure must
    not be quiet either.
    """
    if not scope.calls:
        return 0

    from sqlalchemy.exc import IntegrityError

    from ..db import AgentLlmUsage, get_own_sessionmaker
    from .bookkeeping import COSTS

    def rows(memory_id):
        return [
            AgentLlmUsage(
                id=uuid.uuid4(),
                tenant_id=scope.tenant_id,
                stage=c.stage[:24],
                provider=c.provider[:24],
                model=c.model[:100],
                prompt_tokens=c.prompt_tokens,
                completion_tokens=c.completion_tokens,
                cached_tokens=c.cached_tokens,
                vendor=(c.vendor or None) and c.vendor[:24],
                region=(c.region or None) and c.region[:16],
                agent_id=(scope.agent_id or None),
                memory_id=memory_id,
            )
            for c in scope.calls
        ]

    db = get_own_sessionmaker()()   # the caller holds a session of the main pool
    try:
        try:
            db.add_all(rows(scope.memory_id))
            db.commit()
        except IntegrityError:
            # The memory went while its calls were running: forgotten, or its
            # account closed (15 Oct 2026: an account closed during
            # its last extraction lost the cost row). The calls were made and
            # paid: kept for the account, without the memory. If the account
            # is gone too, its rows went with it and so do these.
            db.rollback()
            if scope.memory_id is None:
                raise
            try:
                db.add_all(rows(None))
                db.commit()
            except IntegrityError:
                db.rollback()
                logger.info("[usage] %d model call(s) not recorded: the account closed during "
                            "them", len(scope.calls))
                COSTS.dropped(len(scope.calls))
                return 0
        COSTS.kept()
        return len(scope.calls)
    except Exception as e:
        # Logged, and counted for /health (services/bookkeeping.py): the
        # Cloud's daily stop sums these rows, so a lost one is spending it
        # does not see (audit of 17 Oct 2026).
        logger.error("[usage] could not record %d model call(s): %s", len(scope.calls),
                     log_safe.stack(e))
        COSTS.lost(len(scope.calls), e)
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    finally:
        scope.calls.clear()
        db.close()


def purge_expired(db, keep_days: int) -> int:
    """Drop rows older than the retention window. 0 keeps everything."""
    if not keep_days or keep_days <= 0:
        return 0
    from datetime import datetime, timedelta, timezone

    from ..core.times import at_most_a_century
    from ..db import AgentLlmUsage

    keep = at_most_a_century("llm_usage_retention_days", keep_days, 1)
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep)
    n = (db.query(AgentLlmUsage)
           .filter(AgentLlmUsage.ts < cutoff)
           .delete(synchronize_session=False))
    if n:
        db.commit()
    return int(n or 0)
