"""The model layer.

This service calls a model in exactly two places: to mine facts out of a memory,
and to embed text for retrieval. Both happen on writes; a read calls no
generative model, and embeds at most its question, once (a search, a context),
the rest of it being SQL (audit of 17 Oct 2026).

That asymmetry is worth stating because it decides the shape of an operator's
bill: it tracks how much your agents *learn*, not how often they *remember*. On
the hosted product a write measured about $0.001 and a read about $0.0000027,
which is roughly a factor of four hundred. Self-hosted, the same asymmetry means
a read-heavy workload costs you almost nothing beyond the database.

Two providers are supported. Gemini is the default. Mistral exists because some
buyers need every processor in the EU, and swapping the extraction model is the
cheapest way to give them that without moving the data.

Vectors come from Gemini too, or from a model on this machine
(EMBEDDING_PROVIDER=local, core/local_embedder.py), which needs no key and
sends no text anywhere.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
from contextlib import contextmanager
from contextvars import ContextVar
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from ..config import get_settings
from ..services.memory_core import log_safe
from ..services.regions import GLOBAL, HOSTED_DEFAULT, REGIONS
from . import embedding_model

logger = logging.getLogger(__name__)

# What the provider did last, so /health can report what is true rather than
# what is configured. Every call records here; nothing has to ask.
#
# It exists because on 2026-09-11 the prepaid credit ran out and the service
# went on answering `extraction: available` with a straight face for hours: the
# check read whether a key was SET. A store that cannot mine facts and says it
# is fine is the exact failure this codebase keeps finding, and it had it in its
# own health endpoint.
_last_call: Dict[str, Any] = {"ok": None, "at": None, "error": None}
# Gemini's vectors, apart (2026-10-06). They were recorded in `_last_call` as
# well, so a vector that came back after a failed extraction put `extraction`
# back to "working" on /health: with the writer on one provider and the
# vectors on Gemini, a retired model or an expired writer's key hid behind
# searches that worked (the audit of 6 October, row 3; the failure of
# 2026-09-11 by another door).
_last_embedding: Dict[str, Any] = {"ok": None, "at": None, "error": None}


# The last failure of THIS thread, for the drain: with groups in parallel the
# process-wide `_last_call` may already be another thread's by the time a
# drain asks why its own memory failed.
_this_thread = threading.local()


# Why a call failed, as a category the drain decides on: the status the
# provider answered, or the class of the exception when it answered nothing,
# never the words of the failure (CONTRIBUTING.md: a refusal is a token or a field;
# audit of 17 Oct 2026: "Server disconnected without sending a response" was
# in no list of words, and the memory went to 'error'). None: the memory's own
# (a 400, a 500 a server crashes on every time, an answer it cannot use).
REFUSED = "refused"          # the provider answered about itself or the install
TIMED_OUT = "timed_out"      # no answer in time
UNREACHABLE = "unreachable"  # no answer at all: connection, name, protocol
NOT_CALLED = "not_called"    # this machine did not send it (a gate, its embedder)
# An account's question past its share at this machine's embedder
# (local_embedder.Busy): the caller's to retry, nothing failed. Questions
# only, so no drain reads it (audit of 20 Oct 2026: the reads that met it
# logged an error each).
PAST_ITS_SHARE = "past_its_share"
# 401, 403: the key; 402: the payment; 429: a quota or a rate; 502, 503: the
# server and its gateways, never the request (RFC 9110, 15.6.3-15.6.4); 529:
# the Anthropic API overloaded. 408 and 504 are a time limit (15.5.9, 15.6.5).
# Which of them one memory's content can also cause (a moderated model's 403,
# a 429 for one request too large) is the drain's to weigh before it rests a
# region (services/facts/drain.py, `_the_writers_failure`).
_REFUSED_STATUSES = frozenset({401, 402, 403, 429, 502, 503, 529})
_TIME_STATUSES = frozenset({408, 504})


def failure_kind(e: Optional[BaseException], status: Optional[int] = None) -> Optional[str]:
    """The category of a failed call (above), from its status, else from the
    class of its exception."""
    import socket

    if status is not None:
        if status in _TIME_STATUSES:
            return TIMED_OUT
        return REFUSED if status in _REFUSED_STATUSES else None
    if e is None:
        return None
    try:
        import httpx

        if isinstance(e, httpx.TimeoutException):
            return TIMED_OUT
        if isinstance(e, httpx.TransportError):
            return UNREACHABLE
    except ImportError:  # pragma: no cover - httpx is a dependency
        pass
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        reason = getattr(e, "reason", None)
        return TIMED_OUT if isinstance(reason, (socket.timeout, TimeoutError)) else UNREACHABLE
    if isinstance(e, (socket.timeout, TimeoutError)):
        return TIMED_OUT
    if isinstance(e, (ConnectionError, socket.gaierror)):
        return UNREACHABLE
    return None


def _status_of(e: BaseException) -> Optional[int]:
    """The HTTP status a provider answered with, read as a field: `code` on
    google-genai's APIError and on urllib's HTTPError, the two clients this
    module calls through. None for a failure with no answer (a timeout, a
    refused connection). A refusal is a field, never words in a message."""
    code = getattr(e, "code", None)
    return code if isinstance(code, int) else None


# A provider's "too many requests" (429), a conflict or a request timeout
# (409, 408) and its own errors (5xx) are a moment, not an outage: the call
# is asked again, twice at most, after the wait the provider names in
# Retry-After, or a short exponential one with jitter. It is the rule of the
# providers' own SDKs, taken as it is (openai-python and anthropic-sdk-python,
# `_should_retry` and `_calculate_retry_timeout`: those statuses, two
# retries, 0.5 s doubling to at most 8 s, Retry-After honoured). A call that
# timed out on our side is not asked again here: the drain counts those per
# memory (`_times_out_on_it`). Until 16 Oct 2026 one 429 sent the memory back to
# the queue and rested the whole region's drain for 1, 2, 5, 10 and then 30
# minutes (services/facts/drain.py): two writers on one Scaleway account
# wrote 7 memories a minute instead of 25. The drain's rest stays for what
# the retries do not get past: an outage, a quota, a refused key.
#
# A call that got no answer at all is asked again the same way (16 Nov 2026):
# a connection refused, reset or closed before the status line, a name that
# did not resolve (`failure_kind` UNREACHABLE, read from the exception's
# class, never its words). The SDKs whose rule this is retry it as they
# retry a 429: openai-python's `SyncAPIClient.request` asks again on its
# `request_exceptions()` before it raises APIConnectionError, and
# anthropic-sdk-python's `_should_retry_exception` answers yes to an
# APIConnectionError, both with DEFAULT_MAX_RETRIES = 2, INITIAL_RETRY_DELAY
# = 0.5 and MAX_RETRY_DELAY = 8.0 (`_constants.py`). Until then one
# connection closed before the answer (seen on 15 Nov 2026 during a measure
# on a computer that went to sleep) sent its memory back to the queue and
# rested the whole region for a minute. Asked again, the call goes to the same
# writer, with the same key, in the same region: never to another one. What
# the SDKs also retry and this module does not is a call that timed out on
# our side (above).
_RETRIES = 2


def _retry_wait(e: BaseException, attempt: int) -> float:
    """Seconds before asking again, the SDKs' way: the provider's
    Retry-After when it names between 0 and 60 seconds, else 0.5 s doubling
    to 8 s, up to a quarter less at random so that threads refused together
    do not come back together."""
    import random

    headers = getattr(e, "headers", None)
    try:
        named = float(headers.get("Retry-After")) if headers is not None else None
    except (TypeError, ValueError):
        named = None  # absent, or an HTTP date: the exponential wait
    if named is not None and 0 < named <= 60:
        return named
    return min(8.0, 0.5 * 2 ** attempt) * (1 - random.random() / 4)


def _model_call_seconds() -> float:
    """LLM_TIMEOUT_SECONDS as a model call waits it: at most a century
    (core/times.at_most_a_century), past which a socket's timeout is out of
    range for the platform and every call failed."""
    from .times import at_most_a_century

    return at_most_a_century("llm_timeout_seconds", get_settings().llm_timeout_seconds, 86_400)


def _transient(e: BaseException, status: Optional[int]) -> bool:
    """Whether the SDKs ask this failure again (above): a status of a moment,
    or no answer at all."""
    if status is not None:
        return status in (408, 409, 429) or status >= 500
    return failure_kind(e) == UNREACHABLE


def _with_retries(send):
    """`send()`, asked again on a transient failure (`_transient`), at most
    `_RETRIES` more times; any other failure, or the last one, is raised."""
    import time

    attempt = 0
    while True:
        try:
            return send()
        except Exception as e:
            status = _status_of(e)
            if not _transient(e, status) or attempt >= _RETRIES:
                raise
            wait = _retry_wait(e, attempt)
            # The status, or the failure as `log_safe.error` writes it (its
            # classes, never its message): never the provider's words
            # (CONTRIBUTING.md, logs).
            if status is not None:
                logger.info("[llm] the provider answered %s: asking again in %.1f s", status, wait)
            else:
                logger.info("[llm] no answer from the provider, asking again in %.1f s: %s",
                            wait, log_safe.error(e))
            time.sleep(wait)
            attempt += 1


def _failed(e: BaseException, *, state: Optional[Dict[str, Any]] = None,
            region: Optional[str] = None, writer: Optional[str] = None) -> None:
    """A call that raised `e`, recorded with its status and its category.
    Recorded as `log_safe.error` writes it (classes, codes, lengths), never
    the exception's message: a provider's error can quote the request, and
    this record is process-wide, shown on the operator's pages (audit of 18
    Oct 2026; CONTRIBUTING.md, the end users' words never in the logs)."""
    status = _status_of(e)
    _record(False, log_safe.error(e), state=state, status=status,
            kind=failure_kind(e, status), region=region, writer=writer)


def _record(ok: bool, error: Optional[str] = None, *,
            state: Optional[Dict[str, Any]] = None, status: Optional[int] = None,
            kind: Optional[str] = None, region: Optional[str] = None,
            writer: Optional[str] = None) -> None:
    """One call's outcome: into `_last_call` (the model's calls), or into
    `state` (`_last_embedding` for Gemini's vectors), and this thread's.
    `status`: the HTTP status of a failed call (`_status_of`); `kind`: its
    category (`failure_kind`), what the drain decides on. `region` and
    `writer`: the writer that MADE the call, the provider's own
    (`Provider.writer`, taken when it was built), never the writer in force
    when the call ends: a client kept for a whole memory can fail after a
    writer was saved in its place. `_last_call` keeps them, so that /health
    does not carry a failure over to a writer saved since
    (`provider_state`); this thread's keeps the writer for the drain, which
    rests only the writer that refused (`last_writer_here`). Without a
    writer (a provider built by hand) the record is nobody's in particular
    and stands."""
    import time

    st = _last_call if state is None else state
    fields = {"ok": ok, "at": time.time(), "error": error if not ok else None,
              "status": status if not ok else None}
    if state is None:
        fields["writer"] = None if writer is None else (region, writer)
    st.update(fields)   # one step: another thread's record never mixes with this one
    if not ok:
        _note_here(error, status, kind if kind is not None else failure_kind(None, status),
                   writer=writer)
    else:
        forget_failure_here()


def _note_here(error: Optional[str], status: Optional[int] = None,
               kind: Optional[str] = None, not_now: bool = False,
               writer: Optional[str] = None) -> None:
    """This thread's last failure, for the drain, and nothing else: what
    /health says about the extraction model (`_last_call`) is not touched.
    `not_now`: the service's gate refused the call (`NotNow`), a field a door
    reads (`not_now_here`) and never a phrase to look for in `error`."""
    _this_thread.failure = error or "unknown failure"
    _this_thread.status = status
    _this_thread.kind = kind
    _this_thread.not_now = not_now
    _this_thread.writer = writer


# What a drain is told when this machine's embedder cannot give a vector (its
# model missing, unreadable, or failing). Without a vector the vocabulary
# cannot resolve a new relation and the memory's write rolls back; that is
# the install's embedder, never the memory, so the drain puts the memory
# back in the queue and waits (services/facts/drain.py, `_NOT_THE_MEMORYS`).
# LoCoMo trial3, 2026-10-06: the model's folder had been emptied and 55
# memories were filed 'error', recoverable only by `manage reextract`.
EMBEDDER_UNAVAILABLE = "the local embedder is unavailable"

# What a drain is told when the service does not call the model now: a hook
# (core/extensions.py, `before_model_call`) refused the call before it was
# sent, for a spending cap or a brake. The memory is nobody's fault and goes
# back to the queue, and the drain rests that region, as for a provider's 429.
NOT_NOW = "the service does not call the model now"


class NotNow(RuntimeError):
    """Raised by a `before_model_call` hook to keep a call from being sent."""


def _before_the_call(vendor: str, model: str, region: Optional[str]) -> None:
    """The gate every model call passes before it is sent (8 Oct 2026, the
    one codebase): the hosted service's spending caps and brakes. Nothing on
    an install of one's own. A refusal is this thread's failure, in the
    words the drain recognises, and the call is not sent."""
    from .extensions import hooks

    try:
        hooks.before_model_call(vendor, model, region)
    except NotNow as e:
        _note_here(f"{NOT_NOW}: {e}", kind=NOT_CALLED, not_now=True)
        raise


def not_now_here() -> bool:
    """Whether this thread's last model call was refused by the service's own
    gate (the Cloud's daily stop, the European month's ceiling, a brake, a
    model with no price) and no call has gone out or failed since. A door
    whose model gave no usable answer asks it, because a retry cannot succeed
    until the stop lifts: it answers `service_paused` (api/errors.py), not
    the `model_unavailable` of a provider that failed (audit of 27 Oct
    2026). The engine turns the refusal into its own failure on the way up,
    so the flag is read here, where it was set."""
    return bool(getattr(_this_thread, "not_now", False))


def last_failure_here() -> Optional[str]:
    """The provider's words for this thread's last failed call, or None."""
    return getattr(_this_thread, "failure", None)


def last_status_here() -> Optional[int]:
    """The HTTP status of this thread's last failed call, or None."""
    return getattr(_this_thread, "status", None)


def last_kind_here() -> Optional[str]:
    """The category of this thread's last failed call (`failure_kind`), or
    None: none, or the memory's own."""
    return getattr(_this_thread, "kind", None)


def last_writer_here() -> Optional[str]:
    """The writer (`writer_identity`'s digest) whose call failed last in this
    thread, or None: none, or a failure that no writer made (a brake, the
    embedder)."""
    return getattr(_this_thread, "writer", None)


def missing_vector_level() -> int:
    """The level a read logs its missing question vector at: a warning when
    this thread's question was past its account's share (`PAST_ITS_SHARE`),
    which is a refusal of the caller, an error otherwise (no key, a failing
    embedder or provider)."""
    return logging.WARNING if last_kind_here() == PAST_ITS_SHARE else logging.ERROR


def forget_failure_here() -> None:
    """This thread has no failure to report: a call of it answered (the
    failure before it was retried and is not why the write fails later), or a
    new request or memory begins on a reused thread. Kept, a 429 that a retry
    got past made a later failure of the engine look like the provider
    refusing, and the whole drain rested (a review of the same rule,
    2026-10-06)."""
    _this_thread.failure = None
    _this_thread.status = None
    _this_thread.kind = None
    _this_thread.not_now = False
    _this_thread.writer = None


def _bill(provider: str, model: str, prompt: int, completion: int,
          stage: Optional[str] = None, cached: int = 0,
          vendor: Optional[str] = None, region: Optional[str] = None) -> None:
    """Attribute one call to whoever is responsible for it.

    Imported here rather than at module scope: `services.llm_usage` reaches
    into `db`, and this module is imported by things that must not drag the
    schema in with them.
    """
    try:
        from ..services import llm_usage

        llm_usage.record(provider, model, prompt, completion, stage=stage, cached_tokens=cached,
                         vendor=vendor, region=region)
    except Exception as e:  # pragma: no cover - bookkeeping never breaks a call
        logger.error("[llm] could not attribute a model call: %s", log_safe.stack(e))


def _bill_vector(provider: str, model: str, tokens: int, **where: Any) -> None:
    """One vector, billed under the stage `embedding` (`llm_usage.EMBEDDING`,
    which the four calls below used to spell out by hand)."""
    from ..services import llm_usage

    _bill(provider, model, tokens, 0, stage=llm_usage.EMBEDDING, **where)


def _bill_gemini_vector(model: str, response: Any) -> None:
    """One of Gemini's vectors, with the tokens its response counts (zero
    when it counts none), for the vendor and the region its gate names
    (`_gemini_vector_may_go`): until the audit of 17 Oct 2026 only the key's
    test said whose call it was, and a vector's cost row named no vendor."""
    u = getattr(response, "usage_metadata", None)
    _bill_vector("gemini", model, getattr(u, "prompt_token_count", 0) or 0,
                 vendor="google", region=GLOBAL)


def _state_of(st: Dict[str, Any], nothing_yet: str) -> Dict[str, Any]:
    import time

    if st["ok"] is None:
        return {"state": "unknown", "detail": nothing_yet}
    age = int(time.time() - (st["at"] or 0))
    if st["ok"]:
        return {"state": "working", "seconds_ago": age}
    return {"state": "failing", "seconds_ago": age, "detail": (st["error"] or "")[:200]}


def _writer_replaced(st: Dict[str, Any]) -> bool:
    """True when the writer that made the call `st` records is no longer the
    one in force for its region (`replaced`, the drain's rule too). Told
    from memory: /health has just read the same settings
    (`provider_configured`), and one it could not have then is not read
    again here, which would add a read, and a wait for the database's
    timeout, to every /health; that writer cannot be told, and the record
    stands. Until 7 Nov 2026 this asked for the Google key on every preset,
    and on the European one (EXTRACTION_PROVIDER=mistral) nothing reads
    it: the failure of a writer saved over stayed `failing`."""
    w = st.get("writer")     # one read: a record may replace it at any moment
    if w is None:
        return False
    where, then = w
    return replaced(where, then, from_memory=True)


def provider_state() -> Dict[str, Any]:
    """What the provider actually did, last time anything asked it.

    `unknown` until something calls it, which is honest: with no traffic there
    is nothing to report, and inventing a probe on every health check would
    bill a model call every thirty seconds forever.

    What the last call did belongs to the writer that made it: once that
    writer is replaced or removed the record is forgotten, as a rest after a
    refusal is (`drain._rest_of`), and the state is `unknown` until the new
    writer is called (7 Nov 2026: /health read `degraded, failing 402` with
    both new writers `configured`).
    """
    st = _last_call
    seen = st.get("writer")
    if st["ok"] is not None and _writer_replaced(st):
        with _lock:   # not across a read: the record is the one just judged
            if _last_call is st and st.get("writer") == seen:
                st.update({"ok": None, "at": None, "error": None, "status": None,
                           "writer": None})
    return _state_of(st, "nothing has called the model yet")


def embedding_state() -> Dict[str, Any]:
    """What Gemini's embedding calls last did, the same way."""
    return _state_of(_last_embedding, "nothing has asked for a vector yet")

# Embedding task hints. Asymmetric embedding: a stored document and the query
# that should find it are embedded differently, which measurably beats using one
# task type for both.
TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"
# A text only ever compared with texts of its own kind: the vocabulary's
# relation phrases, and the centroids made of them. Gemini is still asked for
# RETRIEVAL_DOCUMENT for these (`_gemini_task`), as it was when the
# vocabulary's threshold was tuned on those vectors; the local embedder asks
# its model for sentence similarity (core/local_embedder.py). A memory or a
# fact is TASK_DOCUMENT everywhere, because questions look for it.
TASK_SIMILARITY = "SIMILARITY"

# The width of the embedding columns (db/models.py) and of EmbeddingGemma's
# own output, written once, in core/embedding_model.py (audit of 17 Oct 2026:
# it was a literal in five places). Gemini's native output is wider (3072 for
# gemini-embedding-2-preview) and it truncates on request; asking for the
# wrong width fails at INSERT, after the model has already been paid for.
EMBEDDING_DIM = embedding_model.DIMENSIONS


@dataclass(frozen=True)
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""


class Provider(Protocol):
    # `reasoning=False` asks the model not to think before answering. It is for
    # the classifier calls on the write path: a yes/no question does not need a
    # chain of thought, and paying for one is paying twice, in latency and in
    # output tokens. Measured on gemini-2.5-flash: 8.2s and 1608 thinking tokens
    # for {"same_as": 1}, versus 0.57s and none, same answer.
    name: str

    @property
    def forced_model(self) -> Optional[str]:
        """The model chosen on the Models page for this provider, which wins
        over the one a call asks for; None when the file's settings decide.

        Declared here, implemented by every provider (1 Nov 2026). Until then
        this body was the implementation, and it reached no provider: a class
        that only matches a Protocol does not inherit its members (PEP 544,
        "Explicitly declaring implementation"), so `getattr(client,
        "forced_model", None)` was None on every real provider
        (tests/test_the_models_page_writer_is_recognized.py)."""
        ...

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        json_only: bool = False,
        reasoning: bool = True,
    ) -> LLMResult: ...


def _gemini_client(api_key: str):
    """A Gemini client with a deadline on it.

    The SDK defaults to no timeout, so a provider that accepts the connection
    and then stalls is indistinguishable from one that is thinking, forever.
    That call is not on a background thread: it runs inside the request that
    wrote the memory, holding an open database transaction, so one stalled
    upstream turns into held locks and a client that never gets an answer.
    `HttpOptions.timeout` is in milliseconds.
    """
    from google import genai
    from google.genai import types

    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=int(_model_call_seconds() * 1000)
        ),
    )


def _least_thinking(model: str):
    """The smallest amount of thinking this model will agree to.

    Gemini 2.5 accepts a budget of zero, which is no thinking at all. Gemini 3
    refuses that and wants a level instead, and says so only as "Request
    contains an invalid argument". Verified against a real key on
    gemini-3.6-flash: thinking_budget=0 -> 400 INVALID_ARGUMENT, and
    thinking_level="low", thinking_budget=128 and no setting at all -> 200.

    This is worth a function rather than a line because getting it wrong is not
    a worse answer, it is no answer. The only caller that asks for it is the one
    deciding whether two relation phrases name the same relation, and when that
    call raises, the phrase is filed as a new relation and the store keeps two
    contradictory facts standing. The install where this was found had been
    doing exactly that since the default model moved from 2.5 to 3.6.
    """
    from google.genai import types

    field, value = _thinking_knob(model)
    return types.ThinkingConfig(**{field: value})


def _thinking_knob(model: str) -> tuple:
    """Which setting to use, as data, so the choice is testable.

    The decision and the SDK object are separated on purpose: the development
    image once left `google-genai` out, and anything that had to build a
    ThinkingConfig was unreachable from `make test-all`. That is exactly how
    the two model calls in services/predicates.py went untested while being
    the whole mechanism. The image installs it now (requirements-dev.txt
    takes the product's requirements whole), and the separation stays: the choice is
    tested as data, and no test needs a request to reach a provider to check
    it, which the suite forbids anyway (tests/conftest.py closes the network).
    """
    if re.match(r"^(models/)?gemini-([3-9]|\d\d)", model or ""):
        return "thinking_level", "low"
    return "thinking_budget", 0


def _rejected_the_knob(e: Exception) -> bool:
    """Whether this is the provider refusing the thinking setting: a 400 with
    the status INVALID_ARGUMENT, read as the two fields google-genai's
    APIError carries (`code`, `status`), the ones `log_safe.error` prints.
    Until the audit of 17 Oct 2026 it was "invalid argument" searched in the
    exception's text (CONTRIBUTING.md: a refusal is a token or a field, never words
    in a message). A prompt malformed for some other reason is the same
    status, so it is asked once more without the knob, which costs one call
    and changes nothing.
    """
    return _status_of(e) == 400 and getattr(e, "status", None) == "INVALID_ARGUMENT"


class GeminiProvider:
    # Which writer this client is (`writer_identity`'s digest of what it was
    # built from), set by `get_provider`; None for one built by hand.
    writer: Optional[str] = None

    name = "gemini"

    def __init__(self, api_key: str, force_model: Optional[str] = None,
                 region: Optional[str] = GLOBAL):
        self._client = _gemini_client(api_key)
        # The writer chosen on the screen (Gemini, at first login): every
        # write call goes to it, as on the compatible door below.
        self._force_model = force_model
        self.vendor = "google"
        self.region = region

    # 1 Nov 2026: stated here, as `Provider` declares it; until then only the
    # protocol had it, and no real provider (see `Provider.forced_model`).
    @property
    def forced_model(self) -> Optional[str]:
        """The model chosen on the Models page for this provider, which wins
        over the one a call asks for; None when the file's settings decide."""
        return self._force_model

    def complete(self, messages, *, model=None, max_tokens=1024,
                 temperature=0.2, json_only=False, reasoning=True) -> LLMResult:
        from google.genai import types

        s = get_settings()
        model = self._force_model or model or s.extraction_model
        # Gemini takes a single prompt plus a system instruction, so the
        # OpenAI-shaped message list is flattened here rather than at every
        # call site.
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        prompt = "\n\n".join(m["content"] for m in messages if m["role"] != "system")

        cfg: Dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            cfg["system_instruction"] = system
        if json_only:
            cfg["response_mime_type"] = "application/json"
        if not reasoning:
            # Do not lower max_tokens to achieve this. Thinking tokens are spent
            # from the same budget before a single character of the answer is
            # emitted, so a smaller budget truncates the thinking and returns an
            # empty string. Measured: 2048 -> the right answer, 256 -> "", 64 ->
            # "Here is". The budget is not the knob; this is.
            cfg["thinking_config"] = _least_thinking(model)

        _before_the_call(self.vendor, model, self.region)
        try:
            resp = _with_retries(lambda: self._client.models.generate_content(
                model=model, contents=prompt, config=types.GenerateContentConfig(**cfg)
            ))
        except Exception as e:
            # A model that will not take the thinking setting is not a reason to
            # lose the answer. Drop the knob, say so once, and ask again; the
            # call costs more thinking tokens than we wanted and still returns
            # the right thing, which is the better of the two failures.
            if "thinking_config" in cfg and _rejected_the_knob(e):
                logger.warning(
                    "[llm] %s refused the thinking setting, retrying without it: %s",
                    model, log_safe.error(e),
                )
                cfg.pop("thinking_config")
                try:
                    resp = _with_retries(lambda: self._client.models.generate_content(
                        model=model, contents=prompt,
                        config=types.GenerateContentConfig(**cfg),
                    ))
                except Exception as e2:
                    _failed(e2, region=self.region, writer=self.writer)
                    raise
            else:
                _failed(e, region=self.region, writer=self.writer)
                raise
        _record(True, region=self.region, writer=self.writer)
        u = getattr(resp, "usage_metadata", None)
        # Thinking tokens are billed as output; leaving them out understates the
        # cost of exactly the models most likely to produce them.
        completion = (getattr(u, "candidates_token_count", 0) or 0) + (
            getattr(u, "thoughts_token_count", 0) or 0
        )
        prompt = getattr(u, "prompt_token_count", 0) or 0
        # The part of the prompt Gemini served from its cache (implicit
        # caching), within `prompt`.
        _bill("gemini", model, prompt, completion, cached=getattr(u, "cached_content_token_count", 0) or 0,
              vendor=self.vendor, region=self.region)
        return LLMResult(
            text=resp.text or "",
            prompt_tokens=prompt,
            completion_tokens=completion,
            model=model,
        )


# "gpt-oss" at the start of a word of a model name: no letter or digit of
# any script right before it (`[^\W_]` is `str.isalnum`, so "_", "/", "." and
# ":" separate words). The family (`_gpt_oss`) and its weights
# (`_GPT_OSS_WEIGHTS`) start from it, so a name that names the weights is
# always of the family.
_GPT_OSS_WORD = r"(?<![^\W_])gpt-oss"
_GPT_OSS_FAMILY = re.compile(_GPT_OSS_WORD)


def _gpt_oss(model: Optional[str]) -> bool:
    """Whether `model` is of the gpt-oss family, any size: what its least
    reasoning (`_least_reasoning`) is sent to, as an API floor of the family
    rather than a measure of one model. "gpt-oss" starting a word anywhere
    in the name, whatever its letter case and whatever follows it
    ("gpt-oss120b" too). Until 3 Nov 2026 it was read with the case of the
    name, and until 4 Nov 2026 inside any word too ("xgpt-oss-120b")."""
    return _GPT_OSS_FAMILY.search((model or "").casefold()) is not None


# The gpt-oss weights a model name names, by their size: OpenAI publishes
# them as "gpt-oss-<size>b" (Hugging Face openai/gpt-oss-120b), and each host
# spells that name inside its own model ID: bare (Scaleway, OVHcloud), after
# an author or a path (OpenRouter's "openai/", Fireworks'
# "accounts/fireworks/models/"), as an Ollama tag ("gpt-oss:120b"), with a
# variant or a file format after it (OpenRouter's ":nitro", a GGUF's
# "-GGUF:Q4_K_M", Bedrock's "openai.gpt-oss-120b-1:0"). The size ends its
# word: no letter or digit of any script right after the "b".
_GPT_OSS_WEIGHTS = re.compile(_GPT_OSS_WORD + r"[-:](\d+)b(?![^\W_])")


def _gpt_oss_weights(model: Optional[str]) -> Optional[str]:
    """The gpt-oss weights `model` names, as OpenAI names them
    ("gpt-oss-120b"), whatever the host's spelling and letter case; None for
    a name that names no size of them (another model, or a served name such
    as "gpt-oss" alone)."""
    found = _GPT_OSS_WEIGHTS.search((model or "").casefold())
    return f"gpt-oss-{found.group(1)}b" if found else None


# The gpt-oss weights the extraction rules of services/facts/extractor
# (`_rules_3_1`, LoCoMo whole, 7 Oct 2026) were measured on: gpt-oss-120b
# alone. Until 3 Nov 2026 the extraction asked for "gpt-oss" in the name, so
# a gpt-oss-20b got them too.
MEASURED_GPT_OSS = ("gpt-oss-120b",)


def measured_gpt_oss(model: Optional[str]) -> bool:
    """Whether `model` names weights of MEASURED_GPT_OSS (`_gpt_oss_weights`),
    on whichever host serves them: the rules follow the weights they were
    measured on, not the host's spelling. A name that adds a word after the
    size ("gpt-oss-120b-GGUF", a fine-tune named "gpt-oss-120b-finetuned")
    is read as the same weights: the name is all the product sees of the
    model."""
    return _gpt_oss_weights(model) in MEASURED_GPT_OSS


def _least_reasoning(base: str, model: str) -> Dict[str, Any]:
    """The fields that ask an OpenAI-compatible host for gpt-oss's least
    thinking, on every call, extraction included. The write path makes about
    five short calls per memory in series, and each one paid a default
    ("medium") chain of thought. gpt-oss cannot switch it off: "low" is its
    floor. Measured that way (2026-10-02, 60 LongMemEval questions): gpt-oss-120b
    through OpenRouter wrote at 3.3 s per memory and $2.03 per thousand, with
    48 answers against Gemini's 51 (p ≈ 0.55). OpenRouter takes its own
    unified field, while the vLLM-based hosts (Scaleway, OVHcloud) take
    OpenAI's. It goes to every gpt-oss (`_gpt_oss`), whose floor it is:
    gpt-oss-20b too, stopped on 2026-10-02 with its reasoning inside its
    answers (docs/misure/osservazioni-e-ibrido-longmemeval-2026-10-02.md).
    Every other model gets nothing."""
    if not _gpt_oss(model):
        return {}
    if "openrouter.ai" in base:
        return {"reasoning": {"effort": "low"}}
    return {"reasoning_effort": "low"}


# OpenAI's reasoning models refuse `max_tokens` ("use max_completion_tokens")
# and any temperature but their own, and think at "medium" unless told. For a
# writer making five short calls per memory, "low" (2026-10-02, the screen's
# OpenAI choice; no key here to measure it with yet).
_OPENAI_REASONING = ("gpt-5", "gpt-6", "o1", "o3", "o4")


def _openai_reasoning_fields(base: str, payload: Dict[str, Any]) -> None:
    if "api.openai.com" not in base or not str(payload["model"]).startswith(_OPENAI_REASONING):
        return
    payload["max_completion_tokens"] = payload.pop("max_tokens")
    payload.pop("temperature", None)
    payload["reasoning_effort"] = "low"


class MistralProvider:
    writer: Optional[str] = None   # as GeminiProvider's

    name = "mistral"

    def __init__(self, api_key: str, api_base: str, force_model: Optional[str] = None,
                 vendor: str = "mistral", region: Optional[str] = None):
        self._key = api_key
        self._base = api_base.rstrip("/")
        # Who answers behind this door (scaleway, openrouter...) and in which
        # region, for the gate before every call.
        self.vendor = vendor
        self.region = region
        # The writer chosen on the Models page: every write call goes to it,
        # whatever model name a stage asks for. A VOCABULARY_MODEL left in
        # .env with another provider's name would otherwise be sent by name
        # to this host and refused on every write (the 422 of 2026-10-02).
        self._force_model = force_model

    # 1 Nov 2026: stated here, as `Provider` declares it; until then only the
    # protocol had it, and no real provider (see `Provider.forced_model`).
    @property
    def forced_model(self) -> Optional[str]:
        """The model chosen on the Models page for this provider, which wins
        over the one a call asks for; None when the file's settings decide."""
        return self._force_model

    def complete(self, messages, *, model=None, max_tokens=1024,
                 temperature=0.2, json_only=False, reasoning=True) -> LLMResult:
        # `reasoning` is accepted and ignored: a gpt-oss and an OpenAI
        # reasoning model behind this door get their least thinking on every
        # call (below), every other model is sent as asked. Callers must not
        # have to know which provider they got.
        s = get_settings()
        payload: Dict[str, Any] = {
            "model": self._force_model or model or s.mistral_extraction_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_only:
            payload["response_format"] = {"type": "json_object"}
        payload.update(_least_reasoning(self._base, payload["model"]))
        _openai_reasoning_fields(self._base, payload)
        _before_the_call(self.vendor, payload["model"], self.region)

        req = urllib.request.Request(
            f"{self._base}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        def send():
            # The same deadline as the Gemini client: LLM_TIMEOUT_SECONDS is
            # documented for "a model call", and this path used to keep its own
            # literal 60 whatever the operator set.
            with urllib.request.urlopen(req, timeout=_model_call_seconds()) as r:
                return json.loads(r.read().decode())

        try:
            body = _with_retries(send)
        except Exception as e:
            _failed(e, region=self.region, writer=self.writer)
            raise
        _record(True, region=self.region, writer=self.writer)
        usage = body.get("usage") or {}
        prompt = usage.get("prompt_tokens", 0) or 0
        completion = usage.get("completion_tokens", 0) or 0
        # The part of the prompt served from the provider's prefix cache,
        # within `prompt` (OpenAI's field; Scaleway caches by itself).
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
        _bill("mistral", payload["model"], prompt, completion, cached=cached or 0,
              vendor=self.vendor, region=self.region)
        return LLMResult(
            text=(body["choices"][0]["message"]["content"] or ""),
            prompt_tokens=prompt,
            completion_tokens=completion,
            model=payload["model"],
        )


# The writer of each region (8 Oct 2026, services/regions.py), memoized: it
# is asked on every model call and changes only when somebody saves, which
# calls `reset_provider_cache`.
_providers: Dict[str, Provider] = {}
# Until when (time.monotonic) a writer of `_providers` built while the
# settings could not all be read is kept, by region (`get_provider`).
_kept_until: Dict[Optional[str], float] = {}
# Reentrant on purpose. `get_provider` holds this and then asks
# `current_google_key`, which wants it too: with a plain Lock that is a
# deadlock, and the symptom is a test suite that runs for ten minutes instead
# of twelve seconds and never finishes.
#
# Never held across a read of the database (audit of 27 Oct 2026): the memo's
# rows are read before it is taken, and stored under it only if no reset came
# in between (`_generation`). Held across `model_choice.read_writers`, every
# model call and every embedding of the process (`_embed_client`) waited for
# a connection of the pool, up to its 30 s checkout.
_lock = threading.RLock()
# Bumped by every drop of the memo: a read begun before a drop is not stored.
_generation = 0

_resolved_key: Optional[str] = None
_key_resolved = False

_screen_writers: Optional[Dict[str, Any]] = None
_writers_resolved = False

# A writer being told (`_telling_the_writer`): whether a setting it is built
# from was not had, because its read failed (`_unreadable`) or because it
# is told from memory and the setting was not there (`_may_read`). Only
# what `_what_writes` asks for counts: the writers of the Models page
# always, the Google key only for the Gemini writer of `.env`. Until 7 Nov
# 2026 a flag of the process said the writers' last read had failed, and
# /health asked besides for the Google key, which on the European preset
# (EXTRACTION_PROVIDER=mistral) nothing reads. A ContextVar: each request
# and each drain thread tells its own writer.
_telling: ContextVar[Optional[Dict[str, bool]]] = ContextVar("korely_telling_writer",
                                                             default=None)

# What the memo above was resolved against: the rows of install_settings it
# reads, by name and `updated_at` (`_saved_elsewhere`). A save calls
# `reset_provider_cache` only in the process that served it; a worker run
# apart (RUN_WORKER=false and `python -m korely_agent.worker`) or another
# replica kept the writer, the key and the default region it had read first
# until it restarted, so moving the install to a European writer left the
# worker sending the memories of the projects that follow the install to the
# Global one, and a removed key went on being used (audit of 18 Oct 2026).
# Each process now compares the stamp at most every
# RECHECK_SECONDS, on the calls that read the memo, and drops it when
# another process saved: one small read every ten seconds, against a model
# call per memory.
RECHECK_SECONDS = 10.0
_stamp: Optional[tuple] = None
_next_check = 0.0

# The region of the write in progress (services/regions.writing_for): every
# `get_provider()` inside it answers that region's writer. A ContextVar, so
# each drain thread and each request has its own.
_region: ContextVar[Optional[str]] = ContextVar("korely_writing_region", default=None)


@contextmanager
def writing_in(region: Optional[str]):
    """Every model call inside goes to the writer of `region` (None: the
    install's default writer)."""
    token = _region.set(region)
    try:
        yield
    finally:
        _region.reset(token)


_warned_unreadable: Dict[str, bool] = {}


def _unreadable(what: str, error: Exception) -> None:
    """A setting the database could not give: once per failing streak at
    WARNING, then at DEBUG until a read succeeds (`reset_provider_cache`).
    The error as `log_safe.error` writes it, never its message, which for a
    database error can carry a row's values (audit of 17 Oct 2026: this
    helper is handed the exception outside its `except`, where the log
    check did not look). A writer being told meanwhile lacks it
    (`_telling`)."""
    told = _telling.get()
    if told is not None:
        told["lacking"] = True
    if _warned_unreadable.get(what):
        logger.debug("[llm] %s still unreadable from the database: %s", what,
                     log_safe.error(error))
        return
    _warned_unreadable[what] = True
    logger.warning("[llm] %s could not be read from the database (%s); the file's "
                   "settings serve until it can", what, log_safe.error(error))


def _may_read() -> bool:
    """Asked by a memoized setting (`screen_writers`, `current_google_key`)
    that is not in memory, before it reads the database: no while a writer
    is told from memory, which then lacks that setting (`_telling`)."""
    told = _telling.get()
    if told is None or not told["from_memory"]:
        return True
    told["lacking"] = True
    return False


@contextmanager
def _telling_the_writer(from_memory: bool = False):
    """Inside: the settings `_what_writes` asks for are noted (`_telling`),
    and with `from_memory` none is read. Gives {"lacking": bool}, true once
    a setting was not had."""
    told = {"from_memory": from_memory, "lacking": False}
    token = _telling.set(told)
    try:
        yield told
    finally:
        _telling.reset(token)


def _settings_stamp() -> tuple:
    """(name, updated_at) of each install_settings row the memo reads: a
    save moves its `updated_at`, a removal drops its row."""
    from ..db import InstallSetting, get_own_sessionmaker
    from ..services.model_choice import ROW
    from ..services.provider_key import GOOGLE_KEY

    db = get_own_sessionmaker()()
    try:
        return tuple(db.query(InstallSetting.name, InstallSetting.updated_at)
                       .filter(InstallSetting.name.in_((ROW, GOOGLE_KEY)))
                       .order_by(InstallSetting.name)
                       .all())
    finally:
        db.close()


def _memoized() -> bool:
    return bool(_providers) or _writers_resolved or _key_resolved


def _saved_elsewhere() -> None:
    """Drop the memo when another process saved a writer or a key since it
    was read (see `_stamp`). At most every RECHECK_SECONDS; the first call
    after a reset reads the stamp before the memo is resolved again, so the
    memo is never older than its stamp. An unreadable database changes
    nothing: the memo serves, as it did."""
    global _stamp, _next_check
    if time.monotonic() < _next_check:
        return
    with _lock:
        now = time.monotonic()
        if now < _next_check:
            return
        _next_check = now + RECHECK_SECONDS
        generation = _generation
    try:
        stamp = _settings_stamp()   # outside the lock: a read of the database
    except Exception:
        return
    with _lock:
        if generation != _generation:
            return              # reset meanwhile: the next check reads again
        if stamp != _stamp and (_stamp is not None or _memoized()):
            logger.info("[llm] the writer or the key was saved by another process; read again")
            _drop_memo()
        _stamp = stamp


def screen_writers() -> Dict[str, Any]:
    """The writers chosen on the Models page, by region, and the default
    region: {"default": ..., "eu": (provider, base URL, key, model) or None,
    "global": ...} (services/model_choice.read_writers); {} with nothing
    readable.

    Memoized like the key, for the same reason: it is asked on every
    `get_provider`, and it changes only when somebody saves, which calls
    `reset_provider_cache` here and `_saved_elsewhere` in every other
    process."""
    global _screen_writers, _writers_resolved
    _saved_elsewhere()
    if _writers_resolved:
        return _screen_writers or {}
    if not _may_read():
        return {}
    from ..services import model_choice

    generation = _generation
    try:
        found = model_choice.read_writers()   # outside the lock (see `_lock`)
    except Exception as e:
        # Not remembered, as for the key (current_google_key): the next
        # call reads again, and the file's provider serves meanwhile; the
        # writer being told cannot be told (`_unreadable`).
        _unreadable("the writer chosen on the screen", e)
        return {}
    with _lock:
        if _writers_resolved:
            return _screen_writers or {}
        if generation == _generation:
            _screen_writers, _writers_resolved = found, True
        return found


def _google_key_of(saved: Optional[str], screen) -> Optional[str]:
    """The Google key made of these settings: `saved` (the provider's own
    screen, services/provider_key), else `.env`'s, else the key of the
    Gemini writer chosen on the Models page, `screen` (a function answering
    what `screen_writers` answers, asked only then). Gemini chosen at first
    login: the key pasted there is a Google AI Studio key, so it makes the
    vectors too (EMBEDDING_PROVIDER gemini), instead of an install that
    writes with Gemini and cannot search by meaning."""
    key = saved or ((get_settings().google_api_key or "").strip() or None)
    if not key:
        chosen = screen().get(GLOBAL)
        if chosen and chosen[0] == "google":
            key = chosen[2]
    return key


def current_google_key() -> Optional[str]:
    """The key in force: the one set from the screen, else the one in `.env`.

    Memoized, because it is read on every embedding and the answer changes
    only when somebody saves a new one, which calls `reset_provider_cache`.

    A failure to reach the database is not a failure here. At boot the schema
    may not exist yet, and an install that has always kept its key in the file
    must not stop working because a table it never heard of could not be read.
    """
    global _resolved_key, _key_resolved
    _saved_elsewhere()
    if _key_resolved:
        return _resolved_key
    # Read outside the lock (see `_lock`), stored under it.
    generation = _generation
    # Told from memory (`_may_read`): not read, what the file and the
    # screen say serves, and it is not remembered.
    from_db, unread = None, not _may_read()
    if not unread:
        try:
            from ..db import get_own_sessionmaker
            from ..services.provider_key import get_google_key

            db = get_own_sessionmaker()()
            try:
                from_db = get_google_key(db)
            finally:
                db.close()
        except Exception as e:
            unread = True
            _unreadable("the provider key", e)
    key = _google_key_of(from_db, screen_writers)
    # A read that failed is not remembered (2026-10-06): a hiccup of
    # Postgres at the first read used to stand for "no key" until the
    # next save or restart, and the extraction stopped for hours with a
    # DEBUG line (the audit of 6 October, row 20). The next call reads
    # again; the file's key serves meanwhile.
    with _lock:
        if _key_resolved:
            return _resolved_key
        if not unread and generation == _generation:
            _resolved_key, _key_resolved = key, True
        return key


def _file_writer() -> tuple:
    """(configured, region) of the writer `.env` configures. The region
    is the OpenAI-compatible door's address's (EXTRACTION_PROVIDER=mistral),
    "global" for Gemini, None for an address that is none of the providers'
    we know (a model of one's own behind the door): that writer has no
    region a project can choose, and it writes for the projects that follow
    the install."""
    return _file_writer_of(current_google_key)


def _file_writer_of(google_key) -> tuple:
    """`_file_writer` of the Google key `google_key()` answers (asked only
    for Gemini)."""
    s = get_settings()
    if s.extraction_provider == "mistral":
        if not (s.mistral_api_key or "").strip():
            return False, None
        from ..services.model_choice import region_of_base

        return True, region_of_base(s.mistral_api_base)
    return (True, GLOBAL) if google_key() else (False, None)


def default_region() -> Optional[str]:
    """The region of the install's default writer, which a project that
    never chose a region follows: the screen's (the provider saved last),
    else the file's; None with no writer, or with the file's writer at an
    address of no known region. On the Cloud it is the service's, Global
    (services/regions.HOSTED_DEFAULT), whichever provider was saved last:
    on 5 Nov 2026 saving Scaleway made Europe the default in silence. With
    no writer in Global the memories that follow it wait; they never go to
    another region (services/regions.py)."""
    if get_settings().hosted:
        return HOSTED_DEFAULT
    return screen_writers().get("default") or _file_writer()[1]


def regions_with_writer() -> set:
    """The regions some writer can write in: the screen's, and the file's.
    None in the set is the file's writer at an address of no known region,
    which writes only for the projects that follow the install. What /health
    and the Models page both say of each region (main._every_region,
    services/model_choice.describe)."""
    return _regions_of(screen_writers(), _file_writer())


def regions_from(screen: Dict[str, Any], saved_google_key: Optional[str]) -> set:
    """`regions_with_writer` of settings that are not in force: `screen` as
    services/model_choice.read_writers answers, `saved_google_key` the key
    of the provider's own screen. The same rules, asked of the install as a
    clear would leave it (model_choice.left_without_writer)."""
    return _regions_of(screen, _file_writer_of(lambda: _google_key_of(saved_google_key,
                                                                      lambda: screen)))


def _regions_of(screen: Dict[str, Any], file_writer: tuple) -> set:
    have = {r for r in REGIONS if screen.get(r)}
    configured, file_region = file_writer
    if configured and not (file_region is None and screen.get("default")):
        have.add(file_region)
    return have


def provider_configured(region: Optional[str] = None) -> bool:
    """Whether extraction can run at all (any region), or in `region`,
    counting the writers chosen on the screen.

    `settings.has_llm_provider` only ever saw the file, so an install whose key
    was pasted into the dashboard reported itself unconfigured while working
    perfectly.
    """
    have = regions_with_writer()
    return bool(have) if region is None else region in have


def get_provider(region: Optional[str] = None) -> Optional[Provider]:
    """The writer of `region`, else of the write in progress (`writing_in`),
    else the install's default writer; None when that region has none.

    None is a real answer, not an error: the store still accepts writes and
    answers reads without a model. What it cannot do is mine facts, and callers
    must treat that as a failure rather than as an empty result. Returning [] for
    "we never looked" is how the hosted product came to report memories as
    processed while extracting nothing at all. Never another region's writer
    instead (services/regions.py: no fallback, ever).
    """
    _saved_elsewhere()
    region = region or _region.get() or default_region()
    if region is None and not _file_writer()[0]:
        return None
    found = _kept(region)
    if found is not None:
        return found
    # What the writer is built from, read before the lock (see `_lock`).
    # Built from settings that could not all be read (the file's writer
    # serving meanwhile, see `screen_writers`), it is kept RECHECK_SECONDS
    # (`_kept_until`), as `_saved_elsewhere` asks the stamp at most that
    # often, and then built again from a new read. Until 7 Nov 2026 it was
    # kept for good, and a blip of the database at the first call after a
    # save left the writer of `.env` writing until the next save or
    # restart, the one of the Models page saved and never used; built again
    # at every call instead, each call of a fault paid the failed reads.
    generation = _generation
    try:
        with _telling_the_writer() as told:
            what = _what_writes(region)
    except Exception as e:
        logger.error("[llm] could not construct the %s writer: %s", region, log_safe.stack(e))
        return None
    if what is None:
        return None
    with _lock:
        found = _kept(region)
        if found is not None:
            return found
        try:
            source, vendor, base, key, model = what
            # Gemini on its own client, the one this product always used;
            # every other provider through the OpenAI-compatible door. The
            # Models page's writer names its model; the file's follows
            # EXTRACTION_MODEL / MISTRAL_EXTRACTION_MODEL.
            if source == "screen":
                found = (GeminiProvider(key, force_model=model, region=region)
                         if vendor == "google"
                         else MistralProvider(key, base, force_model=model,
                                              vendor=vendor, region=region))
            elif vendor == "google":
                found = GeminiProvider(key, region=region)
            else:
                found = MistralProvider(key, base, vendor=vendor, region=region)
        except Exception as e:
            logger.error("[llm] could not construct the %s writer: %s", region, log_safe.stack(e))
            return None
        found.writer = _identity_of(what)
        if generation == _generation:
            _providers[region] = found
            if told["lacking"]:
                _kept_until[region] = time.monotonic() + RECHECK_SECONDS
            else:
                _kept_until.pop(region, None)
        return found


def _kept(region: Optional[str]) -> Optional[Provider]:
    """The writer kept for `region`; None when there is none, or when it was
    built while the settings could not all be read and its RECHECK_SECONDS
    are over (`_kept_until`)."""
    found = _providers.get(region)
    until = _kept_until.get(region)
    if found is None or until is None or time.monotonic() < until:
        return found
    return None


def _what_writes(region: Optional[str]) -> Optional[tuple]:
    """What the writer of `region` is built from: (source, vendor, base URL,
    key, model), `source` "screen" (the Models page wins) or "file"; None
    when the region has none. Reads the settings, builds nothing."""
    chosen = screen_writers().get(region)
    if chosen:
        provider, base, key, model = chosen
        return "screen", provider, base, key, model
    if _file_writer() != (True, region):
        return None
    s = get_settings()
    if s.extraction_provider == "mistral":
        from ..services.model_choice import vendor_of_base

        return ("file", vendor_of_base(s.mistral_api_base) or "mistral", s.mistral_api_base,
                s.mistral_api_key, s.mistral_extraction_model)
    key = current_google_key()
    return ("file", "google", None, key, s.extraction_model) if key else None


class WritersUnread(LookupError):
    """A setting a writer is built from could not be had (the writers chosen
    on the screen, or the Google key of the Gemini writer of `.env`): which
    writer writes a region cannot be told."""


def writer_identity(region: Optional[str], *, from_memory: bool = False) -> Optional[str]:
    """Which writer writes `region` now (None: the default region's), as a
    digest of what it is built from (provider, address, model, key): the
    same while the same writer writes, another once a writer is saved in
    its place, None with no writer. WritersUnread when a setting it is
    built from could not be read, or, `from_memory`, was not in memory
    (then nothing is read): only what `_what_writes` asks for that region
    counts. A digest, so the key itself is kept
    nowhere new. The drain ties a rest after a refusal to it
    (services/facts/drain._pause_of): a refusal is the writer's that
    refused, and a new writer is tried at once (5 Nov 2026)."""
    with _telling_the_writer(from_memory) as told:
        what = _what_writes(region or default_region())
    if told["lacking"]:
        raise WritersUnread("a setting the writer is built from could not be read")
    return None if what is None else _identity_of(what)


def replaced(region: Optional[str], writer: Optional[str], *,
             from_memory: bool = False) -> bool:
    """Whether `writer`, a digest of `writer_identity` (None: no writer), is
    no longer the writer in force for `region`: another saved in its place,
    or none where there was one, or one where there was none. False when
    that cannot be told (`WritersUnread`): what was recorded of it stands.
    The one rule of the drain's rests (services/facts/drain._rest_of), of a
    refusal by a writer saved over while it wrote
    (drain._no_longer_in_force) and of /health's last call
    (`provider_state`, from memory)."""
    try:
        return writer_identity(region, from_memory=from_memory) != writer
    except Exception:  # noqa: BLE001 - cannot be told: the record stands
        return False


def _identity_of(what: tuple) -> str:
    """The digest of what a writer is built from (`_what_writes`)."""
    import hashlib

    return hashlib.sha256(json.dumps(list(what[1:]), default=str).encode()).hexdigest()[:16]


def _drop_memo() -> None:
    global _resolved_key, _key_resolved, _screen_writers, _writers_resolved, _generation
    with _lock:
        _generation += 1
        _providers.clear()
        _kept_until.clear()
        _resolved_key = None
        _key_resolved = False
        _screen_writers = None
        _writers_resolved = False
        _embed_cache.clear()
        _warned_unreadable.clear()


def reset_provider_cache() -> None:
    """Drop the memoized provider and the resolved key.

    Called when somebody saves a key from the screen. Without it the key is
    stored, the page says so, and nothing extracts until the next restart.
    Only in this process: every other one finds the save by its stamp
    (`_saved_elsewhere`), which the next call here reads again first.
    """
    global _stamp, _next_check
    with _lock:
        _drop_memo()
        _stamp = None
        _next_check = 0.0


_embed_cache: Dict[str, Any] = {}


def _embed_client(api_key: str):
    """One client per key, not one per call.

    Building a client opens a connection pool and re-reads credentials.
    Embedding runs on every write and every search, so paying that on each call
    was latency on the hottest path in the service for no benefit.
    """
    with _lock:
        c = _embed_cache.get(api_key)
        if c is None:
            c = _embed_cache[api_key] = _gemini_client(api_key)
        return c


def _gemini_task(task: str) -> str:
    """Gemini's task type for one of ours. A relation phrase stays a
    RETRIEVAL_DOCUMENT there: asking Gemini for SEMANTIC_SIMILARITY would move
    every distance the vocabulary's threshold was measured on."""
    return TASK_DOCUMENT if task == TASK_SIMILARITY else task


def _embed_here(text: str, task: str) -> Optional[List[float]]:
    """`embed` with EMBEDDING_PROVIDER=local. None on a failure, as on the
    Gemini path; the failure is recorded where /health reads it
    (`local_embedder.state`), and a call is billed with its tokens, which is
    all it costs: nothing is paid for."""
    from . import local_embedder

    prompt = {TASK_QUERY: local_embedder.QUERY,
              TASK_DOCUMENT: local_embedder.DOCUMENT,
              TASK_SIMILARITY: local_embedder.SIMILARITY}.get(task)
    if prompt is None:
        raise ValueError(f"unknown embedding task {task!r}")
    try:
        vec, tokens = local_embedder.embed(text, prompt)
    except local_embedder.Unavailable as e:
        # By its class (`log_safe.error`), never its message (audit of 17
        # Oct 2026); local_embedder.state() keeps the reason for /health.
        # An account's question past its share (`Busy`) is a refusal of the
        # caller, not a failure of the embedder: a warning.
        busy = isinstance(e, local_embedder.Busy)
        logger.log(logging.WARNING if busy else logging.ERROR,
                   "[embeddings] local embedding failed: %s", log_safe.error(e))
        _note_here(f"{EMBEDDER_UNAVAILABLE}: {log_safe.error(e)}",
                   kind=PAST_ITS_SHARE if busy else NOT_CALLED)
        return None
    forget_failure_here()
    _bill_vector("local", local_embedder.NAME, tokens)
    return vec


def _gemini_vector_may_go(model: str) -> bool:
    """Whether a call for one of Gemini's vectors may be sent: the gate every
    model call passes (`_before_the_call`). Gemini answers in the Global
    region (services/model_choice, PROVIDERS["google"]). Until the audit of
    17 Oct 2026 the vectors went out without it, billed and counted
    but never refused by the Cloud's price check, brakes or daily stop. A
    refusal is this thread's failure in the drain's words (NOT_CALLED), and
    not Google's: /health's embedding state is left as it was."""
    try:
        _before_the_call("google", model, GLOBAL)
    except NotNow:
        return False
    return True


def try_google_key(raw: str) -> None:
    """Ask Google whether `raw` works, with the cheapest call there is (one
    vector of one word), for the provider page's test (services/provider_key
    .check). Raises what the call raised, or NotNow when the gate refused it.
    Through this layer and its gate like every call (CONTRIBUTING.md; audit
    of 17 Oct 2026: the page built a client of its own, outside both). Not
    recorded where /health reads: a key being tried is not the key in force."""
    from google.genai import types

    model = get_settings().embedding_model
    _before_the_call("google", model, GLOBAL)
    # Held by name for the whole call: a Client dropped in the middle of the
    # expression is collected after `.models` is read, and closes its
    # connection first ("Cannot send a request, as the client has been
    # closed"): every key pasted on the screen was refused (the upgrade check
    # of 0.1.41, 18 Oct 2026).
    client = _gemini_client(raw)
    r = client.models.embed_content(
        model=model, contents="ping",
        config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
    )
    _bill_gemini_vector(model, r)


def embed(text: str, *, task: str = TASK_DOCUMENT) -> Optional[List[float]]:
    """Embed one string, or None when embedding is not available.

    Callers must handle None. A memory whose embedding failed is still a valid
    memory and must stay readable; it simply will not be found by similarity
    until it is re-embedded.

    With EMBEDDING_PROVIDER=local the vector is computed on this machine
    (core/local_embedder.py); otherwise Gemini computes it, with the key in
    force.
    """
    s = get_settings()
    if s.embedding_provider == "local":
        return _embed_here(text, task)
    key = current_google_key()
    if not key:
        return None
    if not _gemini_vector_may_go(s.embedding_model):
        return None
    try:
        from google.genai import types

        r = _embed_client(key).models.embed_content(
            model=s.embedding_model,
            contents=text,
            config=types.EmbedContentConfig(
                task_type=_gemini_task(task), output_dimensionality=EMBEDDING_DIM
            ),
        )
        vec = list(r.embeddings[0].values)
        if len(vec) != EMBEDDING_DIM:
            # Refuse rather than hand the caller a vector the column will reject.
            # A failed INSERT poisons the session mid-drain and leaves the
            # memory stuck until the stale-row reaper picks it up minutes later.
            #
            # And record it as the failure it is (2026-09-28). It returned None
            # with nothing recorded, so `/health` went on saying "working" from
            # the last good call while every embedding was being thrown away:
            # a misconfigured EMBEDDING_MODEL looked healthy for as long as
            # anybody cared to look. It is billed all the same, because the
            # provider was paid for the call.
            detail = (f"embedding model {s.embedding_model} returned {len(vec)} "
                      f"dimensions, expected {EMBEDDING_DIM}: check embedding_model "
                      f"against the Vector width in db/models.py")
            logger.error("[embeddings] %s", detail)
            _record(False, detail, state=_last_embedding)
            _bill_gemini_vector(s.embedding_model, r)
            return None
        _record(True, state=_last_embedding)
        # Embedding responses do not always carry a token count, and this does
        # not invent one: the row then says the call happened and reports zero
        # tokens. Embeddings are the cheap end by three orders of magnitude, so
        # the numbers that decide a bill (extraction, the judge, the predicate
        # classifier) are complete either way.
        _bill_gemini_vector(s.embedding_model, r)
        return vec
    except Exception as e:
        _failed(e, state=_last_embedding)
        logger.error("[embeddings] embedding failed: %s", log_safe.stack(e))
        return None


# ── a failed call is not an answer (2026-09-28) ──────────────────────────────
#
# The fact write asks a model at four points: the extraction, the relation vocabulary
# (a question and an embedding), the embedding of the new fact and the
# contradiction judge. Until today three of them turned a failure into a
# result: the judge's failed or unreadable answer was "nothing contradicts",
# and a failed embedding was "no vector", which the vocabulary read as "no
# relation like this one" and the judge as "no fact like this one". Two
# outcomes, as there, for the extraction, the vocabulary and the judge:
#
#   ModelDidNotAnswer  nobody looked: a failed or refused call, a timeout.
#   UnusableAnswer     the model answered and the answer cannot be used:
#                      empty, not JSON, without the list the prompt asks for.
#
# On this engine both end where a failed extraction always ended
# (memory_core.fact_extractor.ExtractionFailed covers both kinds): nothing of the
# memory's write is kept (one transaction, rolled back), a synchronous write
# leaves the memory `pending`, the worker tries it once more, and a second
# failure files it as `error`, which `manage status` counts and `manage
# reextract` re-drives. A direct write (POST and PATCH /v1/facts, the
# dashboard's correction) answers 503 and writes nothing. They are two
# classes because the reason is, and the log says which.
#
# The new fact's own embedding is the exception: the fact is written, `pending`, and the worker computes
# its vector later with a bounded retry (services/vectors.py). A memory's
# vector has always been kept apart from the write the same way, and is
# retried by the same worker since the same day.
#
# No provider configured is neither: a configuration, with which the store
# keeps working without the model step (README, "leave the provider key
# unset").


class ModelDidNotAnswer(RuntimeError):
    """A model the write needs gave no answer. Retried, nothing written."""


class UnusableAnswer(RuntimeError):
    """A model the write needs answered something that cannot be used.
    Nothing written; the write ends where a failed extraction ends."""


class EmbeddingFailed(ModelDidNotAnswer):
    """An embedder is configured and the call gave no vector."""


def embedding_configured() -> bool:
    """Whether an embedder is configured: the local one, or the Google key
    for Gemini's, whichever provider does the extraction.

    The local one is configured when it is chosen, model or no model. A model
    missing from the image is a failure, and it is reported (/health, the
    startup log, the write that needed a vector), not taken for "no embedder",
    which would store every memory without a vector and say nothing."""
    if get_settings().embedding_provider == "local":
        return True
    return bool(current_google_key())


def embedding_model_name() -> str:
    """The model the vectors are computed with. memory_core chooses its
    distance thresholds by it (`thresholds`, 2.4.0), by family: `gemini-embedding-2`
    (the preview included) or `embeddinggemma-300m`. Any other model is another
    space with no measured thresholds, and stops the engine at startup
    (`pick_distance_thresholds`)."""
    s = get_settings()
    if s.embedding_provider == "local":
        from .embedding_model import NAME

        return NAME
    return s.embedding_model


def pick_distance_thresholds():
    """Tell memory_core which model makes this process's vectors, and get the
    thresholds that come with it (`thresholds.use_model`). Once per process, at
    startup: the API (create_app, the in-process worker included) and the
    worker on its own. No `manage` command compares distances.

    The function and not its answer, so that the thresholds are read off the
    settings at every use and cannot lag behind the vectors. Checked now: a
    model nobody measured raises here, with what to set instead, rather than
    judging contradictions with another model's numbers from the first
    write on."""
    from ..services.memory_core import thresholds

    try:
        return thresholds.use_model(embedding_model_name)
    except thresholds.ModelWithoutThresholds as e:
        raise thresholds.ModelWithoutThresholds(
            f"EMBEDDING_MODEL={get_settings().embedding_model!r} has no measured distance "
            f"thresholds, so contradictions cannot be judged on its vectors. Use the "
            f"default (gemini-embedding-2-preview) or EMBEDDING_PROVIDER=local.") from e


def embed_or_raise(text: str, *, task: str = TASK_DOCUMENT) -> Optional[List[float]]:
    """`embed`, for a caller that decides something on the vector.

    None only when no embedder is configured. `embed` answers None both for
    that and for a call that failed (it has to: a memory is stored whether or
    not its vector came back), and the callers on the fact write path must
    not read a failure as "nothing similar": the vocabulary's shortlist
    would cache a new relation with no centroid that no later phrase can
    join (so its failure fails the write), and a new fact kept with no vector
    would never be found by similarity (so its failure puts the fact in the
    worker's retry, services/vectors.py). So with an embedder configured, no
    vector raises, and the caller decides which of the two it is.
    """
    if not embedding_configured():
        return None
    vec = embed(text, task=task)
    if vec is None:
        raise EmbeddingFailed("the embedding call gave no vector (the log and /health say why)")
    return vec


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def extract_json(raw: str) -> str:
    """Pull the JSON out of a model response.

    Models wrap JSON in markdown fences and sometimes prefix it with a sentence
    of commentary even when asked not to. Rather than trusting the instruction,
    strip the fences and start at the first bracket. Returns "" on nothing
    usable, so the caller decides whether that is an error.
    """
    if not raw or not raw.strip():
        return ""
    s = _FENCE.sub("", raw).strip()
    starts = [i for i in (s.find("{"), s.find("[")) if i != -1]
    if not starts:
        return ""
    start = min(starts)
    closing = "}" if s[start] == "{" else "]"
    end = s.rfind(closing)
    return s[start : end + 1] if end > start else ""
