"""The drain: the memories whose extraction was deferred, written in the
background (`process_pending_memories`, the worker's facts job).

It claims the pending memories (memory_core `worker.claim_pending`), groups
them by account, project and end user, and writes each group in event order
through the same pipeline as a synchronous write
(`service.extract_and_persist_facts`). A region whose writer is down rests
here, and a memory the provider times out on or refuses every time, while
the region writes the others, is filed here.

Out of service.py since the audit of 17 Oct 2026 (that file was 1,651
lines). Served there under its names of before, `facts.service
.process_pending_memories` and `.writers_refused`.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime
from http import HTTPStatus
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import and_, case, exists, func, literal, or_, select, tuple_
from sqlalchemy.orm import Session, aliased
from sqlalchemy.orm.attributes import set_committed_value

from ...config import settings
from ...core import llm as _llm
from ...core import threads as _threads
from ...db.models import AgentMemory
from .. import holder as _holders
from .. import regions as _regions
from ..memory_core import worker as _worker
from ..memory_core import log_safe

logger = logging.getLogger(__name__)

# The order a group is written in, the memories' event time and then the order
# of writing (memory_core `worker.order_key`): the later episode after
# the earlier, so the judge sees the earlier fact and closes it at the right
# date (Graphiti §2.2.3).
_memory_order_key = _worker.order_key


# The provider refusing the install as a whole: a key it rejects, credit or a
# quota run out, too many requests, or no answer at all. That is no fault of
# the memory being written, so the memory goes back to the queue and the drain
# rests (the hosted product's BUG-351, 2026-10-05). Until then a customer whose
# card expired found every memory 'error', twenty every ten seconds, and they
# stayed so after the card was fixed until somebody ran `manage reextract`.
# A 400 or an answer that cannot be used is about the memory, and still ends
# in 'error'.
#
# Two more of the same kind (2026-10-06). A provider that is down or
# overloaded: 502, 503 and 504 say so about the server and its gateways, never
# about the request (RFC 9110, sections 15.6.3 to 15.6.5; 503 is "temporary
# overload or scheduled maintenance"), and 529 is the overload of the
# Anthropic API. A 500 stays the memory's: a server that crashes on one input
# crashes on it every time, and back in the queue that memory, claimed oldest
# first, would hold every other one behind it. And this machine's embedder
# when it cannot give a vector (core/llm.EMBEDDER_UNAVAILABLE).
#
# Decided on the failure's category (core/llm.failure_kind: the provider's
# status as a field, or the class of the exception when it answered nothing),
# never on its words: until 17 Oct 2026 an expression searched the error's
# text, and a failure worded otherwise ("Server disconnected without sending
# a response", a name that did not resolve in another language) sent the
# memory to 'error' (CONTRIBUTING.md: a refusal is a token or a field).
#
# An answer the writer cut at its output ceiling (core/llm.CUT_OFF, 1.0.38)
# goes back to the queue too, counted against the memory on every try
# (`_times_out_on_it`) and never resting a region: the writer answered.
_NOT_THE_MEMORYS = frozenset({_llm.REFUSED, _llm.TIMED_OUT, _llm.UNREACHABLE, _llm.NOT_CALLED,
                              _llm.PAST_ITS_SHARE, _llm.CUT_OFF})
# How long the drain rests after each refusal in a row: one minute, then
# longer, at most half an hour. The first memory that goes through resets it.
_PAUSES = (60, 120, 300, 600, 1800)
# How long the writer must answer with no refusal for its trouble to be over
# on /health (`since` of `writers_refused`): the longest rest. A memory that
# goes through ends the rest at once, and the drain tries the region again;
# but a refusal that comes back within that time is the same trouble, and
# /health keeps when it began. On 25 Nov 2026 a writer refused from
# 14:55:41 UTC, one memory went through at 15:03:46, the next refusal came
# at 15:08:46, and /health said "since 15:08:46" (code review finding).
_TROUBLE_OVER_AFTER = _PAUSES[-1]
# One rest per region (8 Oct 2026, services/regions.py): Scaleway out of
# credit rests the projects that write in Europe, never the ones that write
# with Gemini. The key None is the rest of a drain with no writer at all.
# Each rest also keeps the accounts whose memories failed in the region since
# its last write ("failing", `_the_writers_failure`).
_pauses: dict = {}
_pause_lock = threading.Lock()


# A rest is the writer's that refused, by `writer` (core/llm.writer_identity):
# a writer saved in its place, or none at all, ends it, with everything it
# kept (its step, its failing and convicted accounts). On 5 Nov 2026 Europe's
# writer was refused and then forgotten, and the region went on reading
# `refused` on /health while it had no writer; a new key saved after a
# refusal waited out the old key's rest. The same writer saved again keeps
# its rest. A writer that cannot be told (a setting it is built from
# unreadable) changes nothing. Told by `core/llm.replaced`, the rule /health
# follows for the last call too.
_UNTOLD = object()


def _writer_now(region: Optional[str]) -> Any:
    try:
        return _llm.writer_identity(region)
    except Exception:  # noqa: BLE001 - the drain never stops on this
        return _UNTOLD


def _no_longer_in_force(region: Optional[str], writer: Optional[str]) -> bool:
    """Whether `writer`, the digest of the writer that made a call (None: no
    writer made it), is no longer the one in force for `region`. A writer
    that cannot be told changes nothing."""
    return writer is not None and _llm.replaced(region, writer)


def _rest_of(region: Optional[str]) -> Optional[dict]:
    """The rest of `region`, None when it has none; a rest of a writer no
    longer in force is dropped here."""
    p = _pauses.get(region)
    if p is None or "writer" not in p or not _llm.replaced(region, p["writer"]):
        return p
    with _pause_lock:
        if _pauses.get(region) is p:
            del _pauses[region]
    return None


def _pause_of(region: Optional[str]) -> dict:
    p = _rest_of(region)
    if p is not None:
        return p
    now = _writer_now(region)
    with _pause_lock:
        return _pauses.setdefault(region, {"until": 0.0, "step": 0,
                                           **({} if now is _UNTOLD else {"writer": now})})


def _drain_paused(region: Optional[str] = None) -> float:
    """Seconds the drain still rests in `region`, 0 when it does not."""
    p = _rest_of(region)
    return max(0.0, p["until"] - time.monotonic()) if p else 0.0


# The refusals that wait for a person: the provider's answer about the
# install's own standing with it, which no retry and no other memory ends
# (RFC 9110): 401, the credentials it was sent (15.5.2); 402, the payment or
# the credit (15.5.3); 403, credentials it "considers insufficient to grant
# access" (15.5.4), a key revoked or without the permission. On 15 Oct 2026
# the Gemini prepaid credit ran out (402); on 4 Nov 2026 Europe's key was
# revoked at Scaleway, its writer answered 403 from 21:08 UTC, and for about
# eleven hours nothing said so but this module's log line and `refused` on
# /health, because only a 402 was told. A 403 is also a moderated model
# refusing one input: it rests the region, and so is told, only when
# `_the_writers_failure` reads it as the writer's (the failures of two
# accounts, or nobody else waiting). 429 and the 5xx are a rate or a server,
# which end by themselves: they rest the region, untold.
_WAITS_FOR_A_PERSON = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.PAYMENT_REQUIRED,
                                 HTTPStatus.FORBIDDEN})


def waits_for_a_person(status: Optional[int]) -> bool:
    """Whether a rest that began with `status` waits for a person (above):
    told through the `writer_refused` hook, and /health `degraded`."""
    return status in _WAITS_FOR_A_PERSON


def _provider_refused(region: Optional[str] = None, status: Optional[int] = None,
                      kind: Optional[str] = None) -> None:
    """The region's writer refused: the drain rests it, longer each time.
    The first refusal of a trouble is remembered with its time for /health
    (`writers_refused`), kept across a memory that goes through and a
    refusal soon after (`_TROUBLE_OVER_AFTER`); one that waits for a person (`waits_for_a_person`:
    401, 402, 403, a field of the answer, `core.llm.last_status_here`) is
    also told, once per rest, through the `writer_refused` hook: on 15 Oct
    2026 the Gemini prepaid credit ran out and the only trace was this log
    line, with /health saying "working"; until 10 Nov 2026 only a 402 was
    told, and Europe's revoked key of 4 Nov answered 403 in silence.

    Told by a mark of the rest (`told`), not by its first refusal: a rest
    begun by a refusal that ends by itself (a 429, a 503) and joined by one
    that waits for a person, after its pause or during it, takes that one's
    status for /health and is told then (the sceptic's review of 10 Nov
    2026: a 429 just before a revoked key, the eleven hours of 4 Nov, was
    told nothing, the rest keeping its 429). A refusal during the pause
    still does not lengthen it. The mark goes with the rest, when a memory
    goes through (`_provider_answered`) or another writer is saved
    (`_rest_of`); the rests are this process's, so each process tells its
    own.

    The refusal is named by its category and status (`core.llm.failure_kind`)
    in the log line and to the hook, never by the provider's own words
    (`core.llm.last_failure_here`, audit of 17 Oct 2026): a provider's error
    can quote the request, which carries the end user's memory, and the
    container's log and an alert's email are kept where no erasure reaches
    them."""
    p = _pause_of(region)
    with _pause_lock:
        resting = p["until"] > time.monotonic()
        if not resting:
            step = min(p["step"], len(_PAUSES) - 1)
            p["until"] = time.monotonic() + _PAUSES[step]
            p["step"] = step + 1
            if step == 0:
                # A new trouble, unless the writer answered only a moment
                # ago after this one began (`_TROUBLE_OVER_AFTER`).
                if not p.get("since") or time.time() - p.get("answered", 0.0) >= _TROUBLE_OVER_AFTER:
                    p["since"] = time.time()
                p["status"] = status
        # A rest a memory has ended meanwhile (`_provider_answered`, step 0)
        # is told at its next refusal, which begins the next one.
        tell = bool(p["step"]) and waits_for_a_person(status) and not p.get("told")
        if tell:
            p["told"] = True
            p["status"] = status
    if not resting:
        logger.warning("[agent_facts] the %s writer refused (%s, HTTP %s): its memories stay in "
                       "the queue and the drain rests it %d s", region or "default",
                       kind or "unknown", status if status is not None else "-", _PAUSES[step])
    elif tell:
        logger.warning("[agent_facts] the %s writer refused (%s, HTTP %s) during its rest: it "
                       "waits for a person", region or "default", kind or "unknown", status)
    if tell:
        from ...core.extensions import hooks

        try:
            hooks.writer_refused(region, status, kind or "")
        except Exception as e:  # noqa: BLE001 - an alert never stops the drain
            logger.warning("[agent_facts] the refusal could not be told: %s", log_safe.error(e))


def writers_refused() -> dict:
    """The regions whose writer is resting after a refusal, for /health:
    {region: {"state": "refused", "since": ISO time, "status": HTTP status or
    None, "payment_required": bool}}. Empty when every writer answers. This
    process's drain only, as `core.llm.provider_state` is this process's."""
    from datetime import datetime, timezone

    out = {}
    current = {r: p for r in list(_pauses) if (p := _rest_of(r)) is not None}
    with _pause_lock:
        for region, p in current.items():
            if p.get("step") and p.get("since"):
                out[region or "default"] = {
                    "state": "refused",
                    "since": datetime.fromtimestamp(p["since"], timezone.utc).isoformat(timespec="seconds"),
                    "status": p.get("status"),
                    "payment_required": p.get("status") == HTTPStatus.PAYMENT_REQUIRED,
                }
    return out


def _provider_answered(region: Optional[str] = None) -> None:
    """A memory of `region` went through: its rest ends (the next refusal
    rests a minute again, and is told again), and the moment is kept, so a
    refusal soon after keeps the trouble's `since` (`_TROUBLE_OVER_AFTER`).
    /health lists the region no more until a refusal (`writers_refused`
    reads a rest, `step`)."""
    p = _pauses.get(region)
    if p and p["step"]:
        with _pause_lock:
            p["step"] = 0
            p["answered"] = time.time()
            p.pop("status", None)
            p.pop("told", None)


# A memory the provider times out on every time (2026-10-06). A timeout or a
# 504 is the provider's as a rule, and the memory
# waits (above). But a memory whose extraction always takes longer than the
# provider's gateway allows, or than LLM_TIMEOUT_SECONDS, timed out on every
# attempt: back in the queue, claimed again, timed out again, and the whole
# drain rested each time, for ever. A timeout now counts against the memory
# when another memory was written since its last one, so a provider slow for
# everybody makes nobody's count grow and an outage files nothing. The fifth
# such timeout files it 'error', as any memory that failed inside: counted
# in /health, re-drivable with `manage reextract` once the cause is fixed.
# Five, not three: LoCoMo whole (5,882 memories, 2026-10-06) had 31 timeouts
# and 504s from Scaleway, and two memories met one twice. A memory alone in
# the queue cannot be told from an outage and keeps waiting, at most once
# every half hour (_PAUSES). Counted in this process, as the pause is: a
# restart forgets the counts, which costs a few more attempts.
#
# A refusal and a call with no answer too (audit of 18 Oct 2026). A
# status says who refused, not why: a moderated model answers 403 to one
# input, a key on a low tier 429 to one request above its tokens per minute,
# a gateway 502 to an input its upstream crashes on. Such a memory came back
# on every pass, claimed oldest first, and every refusal rested its region
# for every account, for as long as the memory existed. The same rule as a
# timeout: counted only when other memories of the SAME region were written
# since its last attempt, so a key refused, a credit run out or a provider
# down for everybody counts nothing (nobody is written there), and a region
# that answers its other memories is no reason to stop them for this one.
# Until the audit of 19 Oct 2026 the region still rested on a memory's first
# refusal, "it may be the start of an outage": one account writing one fresh
# memory per pass that the writer refuses rested the region for every
# account, each memory counted once and never filed. Whether a first refusal
# rests the region is now `_the_writers_failure`'s.
# Counted per region since then: a timeout in Europe while Gemini writes the
# Global projects was counted against the European memory too. A call this
# machine did not make (NOT_CALLED: a brake, a cap, its own embedder) is
# never the memory's.

_TIMEOUTS_PER_MEMORY = 5
_TIMEOUTS_KEPT = 10_000
_timeouts: "OrderedDict[Any, tuple[int, int]]" = OrderedDict()
_written: dict = {}  # region -> memories written there by this process's drain
#
# An answer cut at the writer's output ceiling (core/llm.CUT_OFF, 1.0.38)
# by the same count, on every try: the writer answered, so it is no outage,
# and the memory comes back at the next pass even alone in its region. Five
# tries, as a timeout, each with one ask at twice the room
# (`core.llm.complete_whole`). Until 1.0.38 the first cut answer filed the
# memory 'error' for good: 10 of the 15 memories without facts of the date
# test of 24 Nov 2026 (gpt-oss-120b through OpenRouter).
_COUNTED_AGAINST_THE_MEMORY = frozenset({_llm.TIMED_OUT, _llm.REFUSED, _llm.UNREACHABLE,
                                         _llm.CUT_OFF})


def _a_memory_was_written(region: Optional[str] = None, account: Any = None) -> None:
    with _pause_lock:
        _written[region] = _written.get(region, 0) + 1
        p = _pauses.get(region)
        if p is not None:
            p.pop("failing", None)  # the writer answers: no run of failures
            convicted = p.setdefault("convicted", OrderedDict())
            suspects = p.pop("suspects", None)
            if suspects and account not in suspects:
                # The probe (`_the_probe`): another account's memory written
                # while the region rested on these accounts' failures. The
                # writer is up; their failures are their memories' own.
                for a in suspects:
                    convicted.pop(a, None)
                    convicted[a] = True
                while len(convicted) > _TIMEOUTS_KEPT:
                    convicted.popitem(last=False)
            convicted.pop(account, None)  # its memories are written: as anybody's again


# Which failure is the writer's, and rests its region at once, and which may
# be one memory's, and rests it only on evidence that the writer is down
# (audit of 19 Oct 2026; decided then: a refused memory goes to 'error'
# after five attempts, and its region's drain does not stop). A status says
# who refused, not why, so the line is drawn on what a memory's content can
# cause, read as a field (`core.llm.last_status_here`, `last_kind_here`):
#
# - The writer's at once. 401 is the key the install sends and 402 its
#   payment or credit: the same for every memory, whatever its text (the
#   hosted product's BUG-351; the Gemini prepaid credit of 15 Oct 2026, whose
#   402 the `writer_refused` hook tells). NOT_CALLED is this machine's own: a
#   brake or a cap of the service, per vendor and region, or its embedder.
# - Possibly the memory's. 403 is also a moderated model refusing one input
#   (OpenRouter), 429 one request above a tier's tokens per minute, 502, 503
#   and 529 a gateway or an upstream that crashes or gives up on one input,
#   408, 504 and a client timeout an extraction that runs long, and a call
#   with no answer a server that drops one request. One such failure is no
#   evidence about the writer. The region rests when the run of failures
#   since its last write spans memories of two accounts (the writer fails for
#   more than one of them: an outage, which rests after its second failure
#   instead of its first), or when no other account has a memory waiting or
#   in flight in the region (the rest then costs nobody else anything, and a
#   lone memory or a lone account rests at its first failure exactly as
#   before, so an outage is never called again every tick). One account,
#   whatever it writes, never buys a rest for the others; its memories count
#   against themselves (`_times_out_on_it`) and the fifth failure files each.
_THE_WRITERS_OWN = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.PAYMENT_REQUIRED})


def _region_of_memory() -> Any:
    """The region a pending memory is written in, as a column expression: its
    project's, else the install's default writer's (`regions.of_project`)."""
    from ...db.models import AgentProject

    own = (select(AgentProject.region).where(AgentProject.id == AgentMemory.project_id)
           .scalar_subquery())
    default = _llm.default_region()
    return func.coalesce(own, default) if default else own


def _another_account_waits(db: Session, region: Optional[str], account: Any) -> bool:
    """Whether a memory of another account waits, or is being written, in
    `region`: a rest there would stop it."""
    effective = _region_of_memory()
    q = db.query(AgentMemory.id).filter(
        AgentMemory.graph_sync_status.in_(("pending", "processing")),
        AgentMemory.deleted_at.is_(None), AgentMemory.tenant_id != account,
        effective.is_(None) if region is None else effective == region)
    return q.first() is not None


def _the_writers_failure(db: Session, region: Optional[str], account: Any,
                         status: Optional[int], kind: Optional[str]) -> bool:
    """Whether this failure of a memory of `account`, its first or one with
    nothing of the region written since its last, rests the region (above).

    The audit of 20 Oct 2026 (the same record, the residual of e105c0c): two
    accounts' refused memories, dated early, were claimed first and filled every
    group, so the region rested on "two accounts failing" at every pass;
    nothing was written, so nothing that ends the rest ever happened (the
    run of failures, the pause's step and a memory's count all move only on
    a write), and the memories were never filed. Now a rest on failures a
    memory's content can cause names its accounts (`suspects`), and the first
    claim after it leaves their memories out (`_the_probe`): another
    account's memory written then says the writer is up, the suspects'
    failures are their memories' own from then on (`convicted`: they no
    longer rest the region while another account waits), and each of their
    memories counts against itself, filed at its fifth. Failing too, its
    account joins the run, and the outage rests longer."""
    if kind not in _COUNTED_AGAINST_THE_MEMORY or status in _THE_WRITERS_OWN:
        return True
    p = _pause_of(region)
    with _pause_lock:
        convicted = account in p.get("convicted", ())
    if convicted:
        return not _another_account_waits(db, region, account)
    with _pause_lock:
        failing = p.setdefault("failing", set())
        failing.add(account)
        suspects = set(failing)
    if len(suspects) > 1 or not _another_account_waits(db, region, account):
        with _pause_lock:
            p["suspects"] = suspects
        return True
    return False


def _the_probe(db: Session) -> Any:
    """The claim's condition that leaves out, in each region that rested on
    possibly-content failures and rests no more, the memories of the
    accounts it rested on (`suspects`, above), while another account's
    memory waits there; None when no region is probing. A region whose only
    waiting memories are the suspects' claims them as before."""
    current = {r: p for r in list(_pauses) if (p := _rest_of(r)) is not None}
    with _pause_lock:
        probing = {r: set(p["suspects"]) for r, p in current.items()
                   if p.get("suspects") and p["until"] <= time.monotonic()}
    conditions = []
    effective = _region_of_memory()
    for region, suspects in probing.items():
        here = effective.is_(None) if region is None else effective == region
        others = db.query(AgentMemory.id).filter(
            AgentMemory.graph_sync_status == "pending", AgentMemory.deleted_at.is_(None),
            AgentMemory.tenant_id.notin_(suspects), here).first()
        if others is None:
            continue
        elsewhere = (effective.isnot(None) if region is None
                     else or_(effective.is_(None), effective != region))
        conditions.append(or_(elsewhere, AgentMemory.tenant_id.notin_(suspects)))
    return and_(*conditions) if conditions else None


def _times_out_on_it(memory_id: Any, kind: Optional[str], region: Optional[str] = None) -> Optional[bool]:
    """Count this refusal against the memory (above). True for the one that
    files it; None when it was counted again with other memories of its
    region written since its last one, the memory's and not the region's
    (the drain does not rest for it); False otherwise."""
    if kind not in _COUNTED_AGAINST_THE_MEMORY:
        return False
    answered = kind == _llm.CUT_OFF  # the writer answered: every try counts
    with _pause_lock:
        written = _written.get(region, 0)
        count, seen = _timeouts.pop(memory_id, (0, -1))
        again = count > 0 and written > seen
        if written > seen or answered:
            count += 1
        if count >= _TIMEOUTS_PER_MEMORY:
            return True
        _timeouts[memory_id] = (count, written)
        while len(_timeouts) > _TIMEOUTS_KEPT:
            _timeouts.popitem(last=False)
    return None if again or answered else False


def _forget_timeouts(memory_id: Any) -> None:
    with _pause_lock:
        _timeouts.pop(memory_id, None)


def _drain_one_memory(db: Session, m, region: Optional[str] = None,
                      when: Optional[datetime] = None) -> Optional[bool]:
    """One claimed memory: extract and write it, with the event time stored on
    the row (memory_core `worker.drain_one`). True when it ends 'done';
    a memory still 'processing' afterwards failed inside and goes to 'error',
    counted and re-drivable, never looped forever. None when the provider
    refused or did not answer (above): the memory is back to 'pending', and
    its region rests when the failure is the writer's (`_the_writers_failure`).

    `when`: the claim time of the tick's claim (`_run_group`'s `claims`).
    The claim is read fresh twice, by `drain_one` and by the write below,
    and each time only that claim is this drain's: one claimed again since
    by another drain is skipped with no model call (memory_core 6.27.0; the
    review of 13 Nov 2026: in the few milliseconds between the group's
    check and these reads, a memory taken by another drain cost one more
    call). None: any claim, as before."""
    # The write is service.py's, the synchronous write's own pipeline, read
    # through its module at the call: service.py imports this module, and a
    # stand-in for the write (the tests') is set there.
    from . import service as _service

    refused = []
    # Whether the writer was asked at all: a memory skipped as no longer
    # this drain's (edited, forgotten, or claimed since by another drain)
    # called no model, so it says nothing of the provider and ends no rest
    # of its region (`_provider_answered`).
    asked = []

    def write(mem, ev):
        # memory_core 2.11.0: the claim read fresh, the model sent its text,
        # 'done' written on it; a memory edited or forgotten since the claim
        # is no longer this drain's.
        p = _worker.claim(db, mem)
        if p is None or (when is not None and p.when != when):
            return []
        asked.append(True)
        _llm.forget_failure_here()
        account = mem.tenant_id
        out = _service.extract_and_persist_facts(db, account, memory=mem, valid_from=ev,
                                                 claim=p)
        if p.outcome == "done":
            _a_memory_was_written(region, account)
        failure = _llm.last_failure_here()
        status = _llm.last_status_here()
        kind = _llm.last_kind_here()
        by = _llm.last_writer_here()
        if p.outcome is None and failure and kind in _NOT_THE_MEMORYS:
            counted = _times_out_on_it(mem.id, kind, region)
            if counted and kind == _llm.CUT_OFF:
                logger.error("[agent_facts] memory %s: the writer's answer stopped at its output "
                             "ceiling on %d tries: filed as an error (`manage reextract` puts it "
                             "back)", mem.id, _TIMEOUTS_PER_MEMORY)
                return out
            if counted:
                # Left 'processing': drain_one files it as an error, and
                # the drain does not rest for a memory that is the cause.
                # By its kind and status, never the provider's words
                # (`_provider_refused`).
                logger.error("[agent_facts] memory %s failed %d times (%s) while other memories "
                             "of its region were written: filed as an error (`manage reextract` "
                             "puts it back): HTTP %s", mem.id, _TIMEOUTS_PER_MEMORY, kind,
                             status if status is not None else "-")
                return out
            # Back to the queue before drain_one can file it as an error,
            # with a compare-and-set on the claim: written on the session's
            # object it overwrote a forgetting that came meanwhile.
            if _worker.release(db, p):
                set_committed_value(mem, "graph_sync_status", "pending")
            # Refused again while its region wrote the others: the memory's,
            # and the region goes on (above).
            refused.append((status, kind, counted is None, account, by))
        return out

    ok = _worker.drain_one(db, m, write, when=when)
    if refused:
        status, kind, again, account, by = refused[0]
        if kind == _llm.CUT_OFF:
            # The writer answered: its region goes on (`_times_out_on_it`).
            logger.warning("[agent_facts] memory %s: the writer's answer stopped at its output "
                           "ceiling (try %d of %d): it waits for the next pass, the region does "
                           "not rest", m.id, _timeouts.get(m.id, (0, 0))[0], _TIMEOUTS_PER_MEMORY)
            return None
        if _no_longer_in_force(region, by):
            # The writer that refused was replaced while the memory was
            # written (a client kept for the whole memory): the one in
            # force now is not refused, and a rest would be its own.
            logger.warning("[agent_facts] memory %s was refused (%s, HTTP %s) by a writer of the "
                           "%s region saved over since: it waits, the region does not rest",
                           m.id, kind or "unknown", status if status is not None else "-",
                           region or "default")
        elif again:
            logger.warning("[agent_facts] memory %s refused again (%s, HTTP %s) while other "
                           "memories of the %s region were written: it waits, the region does "
                           "not rest", m.id, kind or "unknown",
                           status if status is not None else "-", region or "default")
        elif _the_writers_failure(db, region, account, status, kind):
            _provider_refused(region, status, kind)
        else:
            logger.warning("[agent_facts] memory %s refused (%s, HTTP %s) while no other "
                           "account's memory of the %s region failed: it waits, the region "
                           "does not rest", m.id, kind or "unknown",
                           status if status is not None else "-", region or "default")
        return None
    _forget_timeouts(m.id)  # written, filed or no longer this drain's
    if ok and asked:
        _provider_answered(region)
    return ok


# How many memories of one end user a claim takes at most (2026-10-06). The
# claim takes the oldest pending memories, and a backlog is one end user's:
# an import, or a benchmark that writes one conversation at a time. Oldest
# first alone, the twenty memories a tick claims were all that user's (two
# end users of LoCoMo's first conversation while eighteen others waited with
# everything pending, LoCoMo whole, 2026-10-06), the groups below ran two at
# a time instead of four, and every other end user's facts waited hours
# behind one import. Five each: four end users fill a claim of twenty, each
# keeps its own order (its oldest five, then its next five), and nobody's
# queue holds anybody else's. The cap is memory_core's since 2.10.0
# (`claim_pending(person=, per_person=)`, the same in the hosted product);
# a person here is an end user of a tenant.
_PER_END_USER = 5
_PERSON = (AgentMemory.tenant_id, AgentMemory.end_user_id)


def _writable() -> Any:
    """The claim's condition that leaves in the queue the memories whose
    region cannot write now (8 Oct 2026, services/regions.py): no writer
    for it, or its writer resting after a refusal. Never written in the
    other region instead. None when every region can write. A memory whose
    project follows the install with no writer at all has no region, and
    is claimed as before (the worker claims nothing then, `provider_configured`)."""
    writable = _llm.regions_with_writer()
    blocked = [r for r in _regions.REGIONS if r not in writable or _drain_paused(r)]
    if not blocked:
        return None
    effective = _region_of_memory()
    return or_(effective.is_(None), effective.notin_(blocked))


def _persons_in_hand() -> Any:
    """The claim's condition that leaves out a person (account, project, end
    user) any of whose memories is 'processing': one person is worked by one
    group at a time, in event order (audit of 27 Oct 2026: two groups of one
    person ran at once).

    A tick collects its groups for `_DRAIN_PATIENCE` and a group still
    running goes on in its thread, so the next tick claimed the same person's
    next five while the first five were being extracted: two sessions on one
    person's facts and vocabulary, and later episodes judged before earlier
    ones, which `worker.claim_pending` ('the order inside a person never
    changes') and `_run_group` ('groups are independent') both assume cannot
    happen. The database holds the truth for every worker process: a group
    whose process has ended is given back at the next drain of any process
    (`_the_ended_ones_back`, 1.0.22), and an unstamped one by the reaper
    after `drain_stale_minutes`, so a person is never held longer than its
    rows are."""
    other = aliased(AgentMemory)
    return ~exists().where(
        other.graph_sync_status == "processing",
        other.tenant_id == AgentMemory.tenant_id,
        other.project_id.is_not_distinct_from(AgentMemory.project_id),
        other.end_user_id.is_not_distinct_from(AgentMemory.end_user_id),
    ).correlate(AgentMemory)


def _condition_sharing_by_account(db: Session, limit: int, writable: Any = None,
                            chosen: Optional[list] = None):
    """The claim's condition that shares it among accounts, or None when one
    account (or none) has memories waiting (8 Oct 2026, the one codebase).

    The cap per end user keeps one end user's backlog from taking everybody
    else's turn, but only inside an account: an account with many end users,
    or one that imports a backlog dated years back (the claim goes by event
    time), filled every claim while another account's newest memory waited
    behind all of it. On the hosted service each customer is an account of
    one install. The same fair queueing one level up (Demers, Keshav and
    Shenker, SIGCOMM 1989, applied hierarchically as in Bennett and Zhang's
    H-WF2Q, SIGCOMM 1996): each account with memories waiting gets an equal
    share of the claim, `limit // accounts` and one more for the first
    `limit % accounts` of the turn, taken from its end users'
    capped queues in event order, so the order inside an end user never
    changes. One account waiting, the claim is the one of before; a share an
    account leaves unused shortens the claim, and the drain claims again
    (`more`).

    With more accounts waiting than the claim holds, the accounts are the
    first `limit` of the rotation (`_turns`, core/threads.Rotation; the
    drain says which it served once their groups started,
    `process_pending_memories`), one memory each: within ceil(accounts /
    limit) claims each account is served. Until 1.0.40 every account had a
    share of one and memory_core's claim cut them by event time, which a
    client sets: an account importing memories dated years back took every
    place until its backlog was gone. The event order stays inside an
    account, where the judge needs it. `writable` is every other condition
    of the claim, so an account chosen here has a memory the claim can take.
    The accounts chosen are added to `chosen` when given (`_claim`)."""
    alive = (AgentMemory.graph_sync_status == "pending", AgentMemory.deleted_at.is_(None))
    if writable is not None:
        alive = alive + (writable,)
    present = [t for (t,) in db.query(AgentMemory.tenant_id).filter(*alive).distinct()]
    if len(present) <= 1:
        return None
    first = _turns.first(present, max(1, int(limit)))
    if chosen is not None:
        chosen.extend(first)
    # The shares add up to the claim, no more: `limit // accounts` each, and
    # one more for the first `limit % accounts` of the turn. With a share of
    # ceil(limit / accounts) each they added up to more, and memory_core's
    # claim, which cuts at `limit` by event time, gave the places of the
    # accounts dated now to the ones importing a backlog dated years back:
    # with 19 accounts and a claim of 20, nine had nothing in two claims (the
    # review of 25 Nov 2026). An account that leaves part of its share unused
    # shortens the claim, and the drain claims again (`more`).
    base, extra = divmod(max(1, int(limit)), len(first))
    share = case({a: base + 1 for a in first[:extra]}, value=AgentMemory.tenant_id,
                 else_=base) if extra else base
    alive = alive + (AgentMemory.tenant_id.in_(first),)
    when = _worker.event_order(AgentMemory)
    by_person = func.row_number().over(partition_by=list(_PERSON),
                                       order_by=(when.asc(), AgentMemory.created_at.asc(), AgentMemory.id.asc()))
    queued = (select(AgentMemory.id.label("id"), AgentMemory.tenant_id.label("account"),
                     when.label("ev"), AgentMemory.created_at.label("arrival"),
                     by_person.label("in_person"), share.label("share") if extra else
                     literal(base).label("share"))
              .where(*alive).subquery())
    by_account = func.row_number().over(partition_by=queued.c.account,
                                        order_by=(queued.c.ev.asc(), queued.c.arrival.asc(), queued.c.id.asc()))
    capped = (select(queued.c.id, by_account.label("in_account"), queued.c.share)
              .where(queued.c.in_person <= _PER_END_USER).subquery())
    return AgentMemory.id.in_(select(capped.c.id).where(capped.c.in_account <= capped.c.share))


# Whose turn it is among the accounts waiting (`_condition_sharing_by_account`).
_turns = _threads.Rotation()


def _by_what_they_took(groups: dict, started: list) -> list:
    """The accounts of the `started` groups, in the order `_turns.served`
    takes them: the fewest memories taken first, so one that had the extra
    place of a claim (`_condition_sharing_by_account`) goes after the others and
    the extra place moves round. Served in the order of the turn, every
    account kept its place when all were served, and the extra place went
    to the same account claim after claim (the review of 25 Nov 2026). The
    credit of deficit round robin (Shreedhar and Varghese, SIGCOMM 1995),
    kept as the order of the turn."""
    taken: dict = {}
    for key in started:
        taken[key[0]] = taken.get(key[0], 0) + len(groups[key])
    return sorted(taken, key=lambda a: (taken[a], _turns.place(a)))


def _event_times_read_back(db: Session) -> int:
    """Bring the event time of a waiting memory that the driver cannot read
    back to the nearest moment it can (limits.EARLIEST_MOMENT /
    LATEST_MOMENT), in SQL, before the claim loads any row; how many.

    The claim loads its rows earliest event first, and one memory dated the
    year 0 in UTC ("0001-01-01T00:00:00+14:00", accepted until the audit of
    20 Oct 2026) made it raise at every tick: no memory of any account on the
    install was mined again until the row was mended by hand. The doors
    refuse such a moment now (limits.moment_problem) and the migration
    mended the rows already stored; this keeps one written any other way
    from stopping the drain, and leaves the memory readable."""
    from .. import limits

    ev = AgentMemory.event_time
    waiting = AgentMemory.graph_sync_status.in_(("pending", "processing"))
    mended = 0
    for out_of_range, bound in ((ev < limits.EARLIEST_MOMENT, limits.EARLIEST_MOMENT),
                                (ev > limits.LATEST_MOMENT, limits.LATEST_MOMENT)):
        mended += db.query(AgentMemory).filter(waiting, out_of_range).update(
            {ev: bound}, synchronize_session=False)
    if mended:
        db.commit()
        logger.warning("[agent_facts] %d waiting memories had an event time that cannot be "
                       "read back: set to the nearest one that can", mended)
    return mended


def _claim(db: Session, limit: int, skip: Optional[list] = None) -> list:
    """Claim the next pending memories: only regions that can write
    (`_writable`), each end user at most `_PER_END_USER`, each account its
    share (`_condition_sharing_by_account`), the suspects of a rest left out of
    the claim that probes the writer (`_the_probe`), none of the persons
    whose group is still running (`_persons_in_hand`), and none of the
    accounts in `skip` (those holding their share of the drain's threads);
    a waiting memory whose event time the driver cannot read back is
    mended first (`_event_times_read_back`), and the claims of a process
    that has ended go back to the queue first (`_the_ended_ones_back`). The
    claim is stamped with this process's key (`_stamped`).

    This process's key is taken again before anything is given back: a
    process whose own lock session was cut (the database restarted, an
    administrator, `idle_session_timeout`) reads as ended until it takes its
    key again, and giving back first, its drain gave back its own claims,
    claimed them again and ran a second group on the same person beside the
    first (the review of 13 Nov 2026: three memories, six model calls). A
    process that holds no key gives back nothing: it cannot tell its own
    claims from those of a process that has ended."""
    mine = this_process_key(db)
    _event_times_read_back(db)
    if mine is not None:
        _the_ended_ones_back(db, limit)
    writable = _writable()
    free_person = _persons_in_hand()
    writable = free_person if writable is None else and_(writable, free_person)
    if skip:
        left_out = AgentMemory.tenant_id.notin_(skip)
        writable = left_out if writable is None else and_(writable, left_out)
    probe = _the_probe(db)
    if probe is not None:
        writable = probe if writable is None else and_(writable, probe)
    chosen: list = []
    share = _condition_sharing_by_account(db, limit, writable, chosen)
    conditions = [c for c in (writable, share) if c is not None]
    claimed = _stamped(db, _worker.claim_pending(
        db, AgentMemory, limit=limit, expiry_minutes=_stale_minutes(),
        filter_condition=(and_(*conditions) if conditions else None),
        person=_PERSON, per_person=_PER_END_USER))
    # An account chosen with nothing the claim could take (another drain
    # holds its rows) has had its turn: it goes to the end, not to the head
    # again (the review of 25 Nov 2026). One claimed is served when its group
    # starts (`process_pending_memories`).
    took = {m.tenant_id for m in claimed}
    _turns.served([a for a in chosen if a not in took])
    return claimed


# This process's key as the holder of its claims (services/holder.py): one
# per process, as the drain's threads and pauses are.
_holder = _holders.Holder()


def this_process_key(db: Session) -> Optional[int]:
    """This process's key as the holder of its claims, taken again when its
    session no longer holds it: None when the process has given its claims
    back (`give_back`), the database is not PostgreSQL or no key is free (the
    claims are then the clock's). The facts drain stamps its claims with it,
    the /v1/batch worker its jobs (services/batch.process_batch_jobs) and an
    inline extraction the claim written with its memory (memory_engine
    `_claimed_now`, WRITE_ASYNC=false): one key per process, so one
    session's end frees them all, and a claim whose process ends on the way
    goes back at the next drain of any process (`_the_ended_ones_back`)
    instead of after `drain_stale_minutes` (services/holder.py)."""
    with _holder.lock:
        return None if _holder.closed else _holder.key(db.get_bind())


def _stamped(db: Session, rows: list) -> list:
    """`rows`, just claimed, stamped with this process's key: a claim a
    process holds is told from one whose process has ended by whether a
    session of the database still holds that key (services/holder.py), and
    the latter goes back to the queue at once (`_the_ended_ones_back`)
    instead of after `drain_stale_minutes`.

    The UPDATE holds only while each row is still this claim (the claim's
    time: every claim stamps its own), so it never stamps a claim made since
    by another process. Without a key (no PostgreSQL, no free key) the rows stay
    unstamped and the clock decides, as before. A process that is giving its
    claims back (`give_back`) keeps none: the rows go back to the queue here,
    with memory_core's `release`, and the drain works nothing."""
    if not rows:
        return rows
    with _holder.lock:
        if _holder.closed:
            for m in rows:
                _worker.release(db, _worker.Claim(AgentMemory, m.id, m.graph_sync_claimed_at,
                                                  m.content))
            return []
        key = _holder.key(db.get_bind())
        if key is None:
            return rows
        try:
            for when in {m.graph_sync_claimed_at for m in rows}:
                (db.query(AgentMemory)
                 .filter(AgentMemory.id.in_([m.id for m in rows
                                             if m.graph_sync_claimed_at == when]),
                         AgentMemory.graph_sync_claimed_at == when)
                 .update({AgentMemory.graph_sync_claimed_by: key}, synchronize_session=False))
            db.commit()
        except Exception as e:  # noqa: BLE001 - unstamped, the claim is worked as before
            db.rollback()
            logger.error("[agent_facts] could not stamp a claim, the clock gives it back if "
                         "this process ends: %s", log_safe.error(e))
    return rows


def _back_to_the_queue(*conditions):
    """The UPDATE that gives claims back: 'pending', no claim time (the
    database clears the stamp, db/triggers.py), for the claims that meet
    `conditions` and are still 'processing' and not forgotten (a forgotten
    one is laid to rest by memory_core's claim, never queued again)."""
    from sqlalchemy import update

    return (update(AgentMemory)
            .where(AgentMemory.graph_sync_status == "processing",
                   AgentMemory.deleted_at.is_(None), *conditions)
            .values(graph_sync_status="pending", graph_sync_claimed_at=None)
            .execution_options(synchronize_session=False))


# A person as a group works it (`_run_group`) and as the claim holds it
# (`_persons_in_hand`): the account, the project and the end user.
_GROUP = (AgentMemory.tenant_id, AgentMemory.project_id, AgentMemory.end_user_id)


def _the_ended_ones_back(db: Session, limit: int) -> int:
    """The claims stamped with a key no session of this database holds: their
    process has ended (a deploy, a restart, a crash), and they go back to the
    queue now, not after `drain_stale_minutes`, with the person they held
    (`_persons_in_hand`). A claim of a live process, and an unstamped one,
    are left alone. Whole persons, at most `limit` at a time, the rest at
    the next drain: all of a person's ended claims are read, and given back
    together in one statement. The read is capped in SQL at a claim's worth
    per person (`_PER_END_USER`, what one claim takes of a person, and a
    person in hand is never claimed again, `_persons_in_hand`), ordered by
    person, so the cap would cut only the last person, whose group stops at
    the first memory given back (`_run_group`). How many; commits when any.

    A process whose lock session was only cut is read as ended until it
    takes its key again, and its group may still be running: a give-back of
    part of its person (a bare LIMIT, the review of 13 Nov 2026) left the
    group the later memories, written before the earlier one given back.
    Now the group stops at its next memory when that one, or the one it
    has just extracted, was given back (`_run_group`).

    In three statements: the claims read; their rows locked, in the order
    of their ids so two give-backs never deadlock, each only if it
    is still that claim (the same stamp and claim time: a claim given back
    and taken again since is left); then given back if their key is still
    held by no session. Under READ COMMITTED a statement that waited on a
    row lock evaluates its WHERE again on the updated row (PostgreSQL 17
    documentation, section 13.2.1), but the probes of the review saw the
    `pg_locks` side kept from before the wait, and gave back a claim whose
    key came back meanwhile. With the rows locked first, the statement that
    reads `pg_locks` waits on nothing."""
    if db.get_bind().dialect.name != "postgresql":
        return 0
    stamp, when = AgentMemory.graph_sync_claimed_by, AgentMemory.graph_sync_claimed_at
    try:
        person = func.dense_rank().over(order_by=_GROUP).label("person")
        read = (select(AgentMemory.id, stamp, when, person)
                .where(AgentMemory.graph_sync_status == "processing",
                       AgentMemory.deleted_at.is_(None), stamp.isnot(None),
                       ~_holders.alive(stamp))
                .subquery())
        persons = max(1, int(limit))
        ended = db.execute(select(read.c.id, read.c.graph_sync_claimed_by,
                                  read.c.graph_sync_claimed_at)
                           .where(read.c.person <= persons)
                           .order_by(read.c.person, read.c.id)
                           .limit(persons * _PER_END_USER)).all()
        n = 0
        if ended:
            locked = db.execute(
                select(AgentMemory.id)
                .where(tuple_(AgentMemory.id, stamp, when).in_([tuple(r) for r in ended]))
                # Skipped, not waited for, when another transaction holds
                # one (an account's erasure keeps the rows it erases to its
                # commit, and this claim is every account's): given back at a
                # later drain, or erased (audit of 27 Oct 2026).
                .order_by(AgentMemory.id).with_for_update(skip_locked=True)
                .limit(len(ended))).scalars().all()
            if locked:
                n = db.execute(_back_to_the_queue(AgentMemory.id.in_(locked),
                                                  ~_holders.alive(stamp))).rowcount
        db.commit()
    except Exception as e:  # noqa: BLE001 - the claim goes on; the clock gives them back
        db.rollback()
        logger.error("[agent_facts] could not tell the claims of a process that has ended, "
                     "the clock gives them back: %s", log_safe.error(e))
        return 0
    if n:
        logger.info("[agent_facts] %d memories claimed by a process that has ended are back "
                    "in the queue", n)
    return int(n or 0)


def give_back() -> int:
    """The last act of this process's worker (worker.give_back, at a clean
    stop): every claim it holds back in the queue at once, and nothing more
    claimed or worked here. The memory being extracted at that moment is
    given back too; its write, if it ends before the process does, is
    discarded by its claim token, and the next drain extracts it again. How
    many; never raises."""
    def back(conn, key) -> int:
        n = conn.execute(_back_to_the_queue(AgentMemory.graph_sync_claimed_by == key)).rowcount
        conn.commit()
        return int(n or 0)

    n = _holder.give_back(back)
    if n:
        logger.info("[agent_facts] %d claimed memories given back to the queue at the stop", n)
    return n


def _still_pending(db: Session) -> bool:
    return db.query(AgentMemory.id).filter(AgentMemory.graph_sync_status == "pending").first() is not None


# How many end users' groups the drain works at once (2026-10-02:
# twenty people chatting together queued behind one another, about seven
# memories a minute with llama on Scaleway and eighteen with gpt-oss :nitro,
# one memory at a time). Four, not a setting: each group holds one database
# connection for its run, and four of them plus the API's requests and the
# other jobs stay inside SQLAlchemy's default pool of fifteen. The order
# inside a group never changes.
_GROUPS_AT_ONCE = 4
# The groups run in core/threads.Lanes since the audit of 20 Oct 2026 (the
# class of a webhook round that waited for its slowest account): a tick started
# its groups on a pool of its own and waited for the last, so one account's
# group of long memories, or a slow region's, set the pace of every account's
# next claim while the other threads sat idle. Now each account holds at most
# its share of the threads (all four when it is alone), a tick collects its
# groups for `_DRAIN_PATIENCE`, and a group still running then goes on in its
# thread and writes its own memories, as it always did; the next tick claims
# as soon as a thread is free (`_ROOM_SECONDS`), never claiming for an account
# that holds its share. A group never started goes back to the queue.
_draining = _threads.Lanes("facts-drain")
_DRAIN_PATIENCE = 2.0
_ROOM_SECONDS = 10.0


def _stale_minutes() -> float:
    """`drain_stale_minutes`, at most a century (core/times.at_most_a_century):
    the claim's cutoff is taken from now, and a longer value made a moment
    no clock holds at every tick."""
    from ...core.times import at_most_a_century

    return at_most_a_century("drain_stale_minutes", settings.drain_stale_minutes, 24 * 60)


def _group_timeout() -> float:
    """How long a group's thread may run before the drain gives it up and the
    reaper takes its 'processing' rows back: `drain_stale_minutes`, never
    under a minute. A tick itself collects its groups for `_DRAIN_PATIENCE`
    only; the group goes on in its thread."""
    return max(60.0, settings.drain_stale_minutes * 60.0)


def _claims_of(rows: list) -> dict:
    """{memory id: claim time} of rows a tick has just claimed: what its
    groups work, and nothing claimed since by another (`_run_group`)."""
    return {m.id: m.graph_sync_claimed_at for m in rows}


def _still_the_ticks(claims: dict):
    """SQL: the row is still one of `claims` ({memory id: claim time}), the
    claim its tick made: 'processing' with that claim time."""
    by_time: dict = {}
    for mid, when in claims.items():
        by_time.setdefault(when, []).append(mid)
    return and_(AgentMemory.graph_sync_status == "processing",
                or_(*(and_(AgentMemory.id.in_(ids), AgentMemory.graph_sync_claimed_at == when)
                      for when, ids in by_time.items())))


def _run_group_in_own_session(claims: dict, tenant_id: UUID, project_id: Optional[UUID],
                              end_user_id: Optional[str]) -> tuple[int, int]:
    """One group in its own thread and its own session. The rows were claimed
    by the drain's session, so they are loaded again here, by id: an ORM row
    and its session never cross a thread. `claims` is what the tick claimed
    ({memory id: claim time}), which the group works and nothing else."""
    from ...db import get_sessionmaker

    gdb = get_sessionmaker()()
    try:
        rows = [m for m in (gdb.get(AgentMemory, i) for i in claims) if m is not None]
        return _run_group(gdb, tenant_id, project_id, end_user_id, rows, claims)
    except BaseException:
        # A group that raises outside its memories leaves its rows
        # 'processing', and the person would be held until the reaper (the
        # claim leaves out a person with a memory in hand): filed as errors,
        # as the engine files a memory that failed, `manage reextract` puts
        # them back (the review of the audit of 27 Oct 2026).
        _file_as_errors(gdb, claims)
        raise
    finally:
        gdb.close()


def _file_as_errors(gdb: Session, claims: dict) -> None:
    """The rows of `claims` still the tick's claim, filed 'error' (one
    claimed since by another process is that one's). Never raises."""
    try:
        gdb.rollback()
        (gdb.query(AgentMemory)
         .filter(_still_the_ticks(claims))
         .update({AgentMemory.graph_sync_status: "error"}, synchronize_session=False))
        gdb.commit()
    except Exception as e:  # noqa: BLE001 - the reaper is the net behind this
        gdb.rollback()
        logger.error("[agent_facts] could not file a failed group's rows: %s", log_safe.stack(e))


def _run_group(gdb: Session, tenant_id: UUID, project_id: Optional[UUID],
               end_user_id: Optional[str], rows: list, claims: dict) -> tuple[int, int]:
    """Process one (owner, project, end_user) group with the given session, one
    memory at a time in EVENT-TIME order so the later episode supersedes the
    earlier (F3). Groups are independent (F3 candidates are scoped to the
    account, the project and the end user, `memory_scope`), so callers
    may run several concurrently, each with its own session. A group is one
    project, so one region (8 Oct 2026). Returns (processed, failed).

    `claims` ({memory id: claim time}) is the claim the tick made, and the
    group works that claim only. memory_core's `drain_one` reads the claim
    fresh for each memory, so a group whose memories were given back and
    claimed again by another process (this process read as ended while its
    lock session was cut, services/holder.py) took the new claim as its own
    and extracted the person's memories beside the new group, out of order
    (the review of 13 Nov 2026). Before each memory the group reads whether
    that memory, or the one it has just extracted, waits to be extracted
    again (back in the queue, or another claim than its tick's: given back,
    edited, taken since; the write of the one just extracted was then
    discarded), and stops there: the rest of the group still its tick's
    goes back to the queue, so the person's memories are taken again in
    their order, and a later one is not written while an earlier one waits.
    The same when this process gave its claims back (`give_back`), or the
    provider rests."""
    processed = failed = 0
    region = _regions.of_project(gdb, tenant_id, project_id)
    ordered = sorted(rows, key=_memory_order_key)
    # Read once: an erased memory's row can no longer be read (ObjectDeletedError).
    ids = [m.id for m in ordered]
    extracted = None   # the memory just worked, unless the drain sent it back itself
    for i, m in enumerate(ordered):
        if (_holder.closed or _drain_paused(region)
                or _waits_again(gdb, {x: claims[x] for x in (extracted, ids[i]) if x})):
            # Given back by this process at its stop, this memory or the one
            # just extracted waiting to be extracted again, or the provider
            # refused, here or in another group: the rest of this group goes
            # back to the queue untouched, those still the tick's claim and
            # not forgotten.
            _rest_back(gdb, {x: claims[x] for x in ids[i:]})
            break
        ok = _drain_one_of_the_group(gdb, m, region, claims)
        # A memory the provider refused alone is back in the queue by the
        # drain's own hand, counted towards its error (`_drain_one_memory`),
        # and the group goes on, as before.
        extracted = None if ok is None else ids[i]
        if ok is None:
            continue
        processed += int(ok)
        failed += int(not ok)
    return processed, failed


def _waits_again(gdb: Session, claims: dict) -> bool:
    """One of the memories of `claims` ({memory id: claim time}) waits to be
    extracted again: 'pending' (given back, edited) or 'processing' under
    another claim than the tick's (claimed since). One that is done, failed,
    forgotten or erased waits for nothing, and one still the tick's claim is
    the group's to work (`_run_group`)."""
    return gdb.query(exists().where(
        AgentMemory.id.in_(list(claims)),
        or_(AgentMemory.graph_sync_status == "pending",
            and_(AgentMemory.graph_sync_status == "processing",
                 ~_still_the_ticks(claims))))).scalar()


def _rest_back(gdb: Session, claims: dict) -> int:
    """The rows of `claims` still the tick's claim, and not forgotten, back in
    the queue. Commits. How many."""
    n = gdb.execute(_back_to_the_queue(_still_the_ticks(claims))).rowcount
    gdb.commit()
    return int(n or 0)


def _drain_one_of_the_group(gdb: Session, m, region: Optional[str],
                            claims: dict) -> Optional[bool]:
    """`_drain_one_memory`, with what escapes it kept to its own memory.

    A memory erased for good while it was extracted (DELETE /v1/agents/{id},
    an end user's erasure) is read again by memory_core's `drain_one`, and
    the ORM raises ObjectDeletedError, from its error branch too: it left the
    group, and the group's other memories, the same person's in other agent
    namespaces included, stayed 'processing' until the reaper took them back
    after `drain_stale_minutes` (audit of 17 Oct 2026). The erased one is no
    longer this drain's, as a lost claim is (True); anything else is that
    memory's failure, and the group goes on. The root, the row read again in
    `drain_one`, is memory_core's."""
    from sqlalchemy.orm.exc import ObjectDeletedError

    mid = m.id
    try:
        return _drain_one_memory(gdb, m, region, claims[mid])
    except ObjectDeletedError:
        gdb.rollback()
        logger.info("[agent_facts] memory %s was erased while it was extracted", mid)
        return True
    except Exception as e:  # noqa: BLE001 - one memory's failure is not its group's
        gdb.rollback()
        logger.error("[agent_facts] memory %s failed outside the engine's handling, its "
                     "group goes on: %s", mid, log_safe.stack(e))
        # Filed as an error, as the engine files one: left 'processing' it
        # would hold its person until the reaper took it back.
        _file_as_errors(gdb, {mid: claims[mid]})
        return False


def process_pending_memories(db: Session, *, limit: int = 20) -> dict:
    """Async write worker: process AgentMemory rows whose extraction was DEFERRED by the
    async write path (graph_sync_status='pending'). Claims a bounded batch (mark
    'processing' + commit so the next tick won't re-grab them), then runs the SAME
    extract_and_persist pipeline — facts + contradiction outcome IDENTICAL to the
    synchronous path, just deferred. On success the row ends 'done'; on failure 'error'.

    Throughput: a memory is several model calls in series (3 to 8 s with the
    writers measured on 2026-10-02), so one memory at a time was 7 to 18 a
    minute for the whole install. The claimed rows are grouped by (owner,
    end_user) and up to `_GROUPS_AT_ONCE` groups run concurrently, each in its
    own thread and session: F3 candidates are end_user-scoped, so groups are
    independent, and within a group the order stays event-time so F3
    supersession is unchanged. Two groups resolving the same new relation at
    once are memory_core's to settle (the unique index decides, the loser
    reads the winner's row). Every group runs in core/threads.Lanes
    (`_draining`), a lone one too: a tick never waits for its slowest group,
    and the next claims for the threads that are free.

    `more` says the tick claimed a full batch, or left a backlog, and failed
    and gave back nothing, so the worker starts the next one at once (which
    waits for a free thread) instead of resting ten seconds on a backlog; a
    failing provider never earns that, so a backlog cannot be burned into
    'error' rows at full speed."""
    # The reaper and the claim are memory_core's (`worker.claim_pending`): rows
    # left 'processing' past `drain_stale_minutes` go back to 'pending' (the
    # claim time is the clock, never the developer's `updated_at`), and the
    # claim selects with FOR UPDATE SKIP LOCKED, so two drains never extract
    # the same memory twice (two model calls billed for one memory).
    # Every writer refused a moment ago (each region rests apart, 8 Oct
    # 2026): nothing is claimed, so nothing can be filed as an error while
    # they do. With no writer at all, the rest of the drain itself (None).
    waits = [_drain_paused(r) for r in (_llm.regions_with_writer() or {None})]
    if all(waits):
        return {"processed": 0, "failed": 0, "claimed": 0, "paused": int(min(waits))}
    parallel = db.get_bind().dialect.name == "postgresql"
    skip = None
    if parallel:
        # A thread free, waited for: the groups of earlier ticks may hold them.
        free = _draining.wait_for_room(_GROUPS_AT_ONCE, _ROOM_SECONDS)
        if not free:
            return {"processed": 0, "failed": 0, "claimed": 0, "busy": _GROUPS_AT_ONCE}
        running = _draining.running()
        if running:
            share = _draining.share(_GROUPS_AT_ONCE, ())
            skip = [a for a, n in running.items() if n >= share] or None
        limit = min(limit, free * _PER_END_USER)
    pending = _claim(db, limit, skip)
    if not pending:
        out = {"processed": 0, "failed": 0, "claimed": 0}
        if parallel and _draining.running() and _still_pending(db):
            # What waits is a person's next memories behind the group that
            # is still running (`_persons_in_hand`), or an account at its
            # share: rest on the threads, not for the worker's ten seconds,
            # so a lone backlog goes on as soon as its group has ended.
            if _draining.wait_idle(_ROOM_SECONDS):
                out["more"] = True
        return out
    groups = _worker.by_group(pending, lambda m: (m.tenant_id, m.project_id, m.end_user_id))

    processed = failed = refused = 0
    started = len(groups)
    never: list = []

    # Upstream, a parallel path hung under concurrent execution and its cause
    # was never found, so this one shares nothing between threads: each group
    # loads its rows in a session of its own and closes it. And nothing waits
    # on a group for ever: past `drain_stale_minutes` its thread is given up
    # on (core/threads.Lanes logs it, by count), and the reaper puts its rows
    # back in the queue, the way it does for a drain that died. A hang costs
    # one thread and its rows' delay, never the drain.
    if not parallel:
        _turns.served(_by_what_they_took(groups, list(groups)))
        for (owner, project, eu), mems in groups.items():
            p, f = _run_group(db, owner, project, eu, mems, _claims_of(mems))
            processed += p
            failed += f
            refused += len(mems) - p - f
    else:
        import functools

        # Started in the order of the turn (`_turns`), not of the claim's
        # event time: with fewer threads free than groups claimed, the ones
        # that wait go back to the queue keeping their place, and the order
        # of the claim started the earliest events' accounts every tick
        # while the others waited (the review of 25 Nov 2026).
        keys = sorted(groups, key=lambda k: _turns.place(k[0]))
        jobs = [(owner, functools.partial(_run_group_in_own_session,
                                          _claims_of(groups[(owner, project, eu)]),
                                          owner, project, eu))
                for owner, project, eu in keys]
        share = _draining.share(_GROUPS_AT_ONCE, {owner for owner, _p, _e in keys})
        ended, not_started = _draining.run(
            jobs, slots=_GROUPS_AT_ONCE, share=share,
            patience=min(_DRAIN_PATIENCE, _group_timeout()), give_up=_group_timeout())
        for i, result in ended.items():
            n = len(groups[keys[i]])
            if isinstance(result, BaseException):
                logger.error("[agent_facts] a drain group failed outside its memories: %s",
                             log_safe.stack(result))
                p, f = 0, n
            else:
                p, f = result
            processed += p
            failed += f
            refused += n - p - f
        # Served: the accounts with a group started; one whose groups all
        # went back keeps its turn.
        back = set(not_started)
        _turns.served(_by_what_they_took(groups, [k for i, k in enumerate(keys) if i not in back]))
        if not_started:
            never = [m for i in not_started for m in groups[keys[i]]]
            _worker.requeue(db, never)
            started -= len(not_started)
        still = len(keys) - len(ended) - len(not_started)
        if still:
            logger.info("[agent_facts] %d drain group(s) still writing, in their own threads",
                        still)
    out = {"processed": processed, "failed": failed, "claimed": len(pending)}
    if processed + failed < len(pending):
        out["deferred"] = len(pending) - processed - failed
    if failed == 0 and refused == 0 and started and (
            len(pending) >= limit or never or _still_pending(db)):
        # A claim can be short of the limit and the queue still long: one end
        # user's backlog is claimed `_PER_END_USER` at a time. Resting between
        # those would make a lone backlog slower than before. A failing
        # provider never earns it, and the next tick waits for a free thread.
        out["more"] = True
    return out
