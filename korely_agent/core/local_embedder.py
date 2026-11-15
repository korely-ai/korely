"""Vectors computed inside the installation: EMBEDDING_PROVIDER=local.

EmbeddingGemma-300m at full precision (core/embedding_model.py says which files
and why), run by ONNX Runtime on this machine's CPU. The text of a memory does
not leave the machine to become a vector, and semantic search works with no
Google key and on a network with no way out. Without a key it did not work at
all: `embed` answered None and search answered 503 `search_unavailable`.

The decision of 2026-09-29: the same model everywhere, to take Google out of
the vectors.

The default since 0.1.19 (`EMBEDDING_PROVIDER=local`). Two distances in
memory_core are compared with a threshold:
the judge's candidates by meaning and the vocabulary's shortlist. Another model
puts the same texts at other distances, so since memory_core 2.4.0 the
thresholds belong to the model (`thresholds`): 0.67 and 0.67 for this one at full
precision, measured on the hosted product's data with the facts stored as
documents, where Gemini's are 0.45 and 0.65. The process tells the engine
which model at startup (core/llm.pick_distance_thresholds).

Which prompt for which text (the model card, "Prompt instructions",
https://ai.google.dev/gemma/docs/embeddinggemma/model_card), as set on
2026-09-29:

- a question: "task: search result | query: ";
- a stored text that questions look for, a memory or a fact: a document with
  no title, "title: none | text: ". Also where it is compared with texts of its
  own kind (the judge's candidates are facts against facts), because the one
  vector it has must answer questions;
- a text only ever compared with texts of its own kind, the vocabulary's
  relation phrases and the centroids made of them: "task: sentence similarity |
  query: " on both sides.

One inference at a time, questions first. The calls come from the request
threads and from the worker, and several 2,048-token sequences at once would
each hold their activations at the same moment, on a machine that also runs
the database. ONNX Runtime already spreads one call over the cores it is given
(`threads`), so the queue costs little throughput and bounds the memory. A
memory's vector is computed inside its write, and a long one takes seconds
(2.8 s at 2,048 tokens on four cores, measured on 2026-09-29), so a question that waits
takes the next turn, ahead of the documents queued before it: its worst wait
is the one inference already running, and one document that has waited past
DOCUMENT_LEAD_SECONDS, since 2026-09-29; the cloud gives
questions a lane of their own for the same reason. A question is not always
short, so a document's wait behind questions is bounded, and the accounts
take turns (`_Turns`, audit of 18 Oct 2026). A caller waits for its
turn on a request thread, and the share among accounts reaches only callers
that already hold one, so an account has at most
core/threads.CALLS_PER_ACCOUNT requests here at once (audit of 19 Oct 2026):
since 31 Oct 2026 a read waits for one of those places on the event loop
before it takes a thread (core/threads.waits_for_the_embedder), and any
other request's vector takes one at once or is refused (`Busy`,
core/threads.at_the_embedder). A second
session would be a second copy of the weights. The model is loaded once per
process: when the API starts (so that a missing model shows in /health before
a write needs a vector) and on first use anywhere else (a `manage` command).
While the API's start loads it, /health says `warming` and a request's vector
is refused at once (`Warming`, since 1.0.27).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import embedding_model as M
from .threads import CALLS_PER_ACCOUNT, WAIT_SECONDS, at_the_embedder, on_a_request
from ..services.memory_core import log_safe

logger = logging.getLogger(__name__)

NAME = M.NAME
# Where the image puts the model (Dockerfile: stage `model`, then
# `COPY --from=model /models /opt/korely-models`). Not a setting: the image
# decides it, and an operator has nothing to choose here.
MODEL_DIR = f"/opt/korely-models/{M.NAME}"

QUERY = "task: search result | query: "
DOCUMENT = "title: none | text: "
SIMILARITY = "task: sentence similarity | query: "

# tokenizer_config.json of the pinned revision: add_bos_token and add_eos_token
# are both true, and its tokenizer.json writes <bos> $A <eos>. The ids are the
# SentencePiece model's own (bos_id 2, eos_id 1).
BOS, EOS = 2, 1


class Unavailable(RuntimeError):
    """This process cannot compute a vector here: the model is not where the
    settings say, it would not load, or the call failed."""


class Busy(Unavailable):
    """A request's vector with no place of its account at the embedder: it
    held none (core/threads.waits_for_the_embedder) and none was free at once
    (core/threads.at_the_embedder), so it is refused before it waits; or a
    request's caller waited `core/threads.WAIT_SECONDS` for its turn. Not a
    failure of the embedder, which /health does not hear about; a search
    answers 503 `search_unavailable`, retry, as for any vector it could not
    have, and a write is stored with its vector left to the worker
    (memory_engine._vector_of)."""


class Warming(Busy):
    """A request's vector while this process is loading the model at its
    start (`warm_up`), refused at once and answered as a `Busy` one (a fact
    write whose relation needs it: 503 `unavailable`, api/errors.
    embedder_busy): up to
    1.0.26 it waited for its turn behind the load, 7.5 s with the files'
    cache cold (1.3 s warm, measured in Docker with two CPUs on 7 Nov
    2026), and a client with a shorter time limit got nothing (the Claude
    Code hook's 8 s at the start of a session, a code review finding of 15 Nov
    2026). The worker's vectors wait for the model as before."""


#: `embeddings.state` of /health while the model loads at the start.
WARMING = "warming"


class _Ticket:
    """One caller waiting for the slot."""

    __slots__ = ("question", "account", "since", "seq", "granted")

    def __init__(self, question: bool, account: Any, since: float, seq: int) -> None:
        self.question, self.account, self.since, self.seq = question, account, since, seq
        self.granted = False


# How long a waiting document lets questions go first (audit of 18 Oct
# 2026). Questions went first by their kind alone, with no bound on a
# document's wait, on the assumption that a question costs 15-50 ms; a
# 2,000-character question of digits, CJK or emoji reaches the model's 2,048
# tokens and took 5-6 s on two cores (the release-0.1.41 image, measured with
# the pinned tokenizer and weights), the cost of the longest memory. One
# account's long searches kept every other account's writes waiting, each
# holding a request thread and a pooled connection. A wait in seconds, not a
# count of turns, so a long question uses it up as fast as it costs: past it,
# the oldest document takes the next turn, then questions go first again.
DOCUMENT_LEAD_SECONDS = 1.0
# Accounts remembered for the share below; past this the oldest are forgotten,
# which costs them nothing but their place in one rotation.
_ACCOUNTS_KEPT = 10_000


class _Turns:
    """One inference at a time.

    The next turn: a waiting question before waiting documents, until the
    oldest document has waited DOCUMENT_LEAD_SECONDS; then that document, and
    after it a question again, so neither kind waits on the other for ever.
    Inside a kind, the account served longest ago goes first, and inside an
    account the caller who came first: one account's many searches (or
    writes) take turns with another's single one instead of overtaking it
    (fair queueing, Demers, Keshav and Shenker, SIGCOMM 1989, as the facts
    drain shares its claim among accounts, services/facts/drain.py). The
    account is the one the call is billed to (`llm_usage.current_tenant`);
    unbilled calls share one place. The slot is handed to the chosen caller
    when it is released, never left for the waiters to race for.

    A request's caller waits at most `wait` seconds for its turn, then is
    refused (`Busy`); the worker's callers hold no request's thread and are
    not timed: refused there, the drain would read the embedder as down and
    rest the region. How many of one account's requests are here at once is
    core/threads' to bound, before they come (`at_the_embedder` in `embed`).
    The audit of 19 Oct 2026: a REST search waited here on one of the
    process's request threads (AnyIO's 40, one FIFO queue in front of this
    one), with no bound on how many one account had waiting and still
    waiting after its client went away. One read key's 12 searches with a
    0.3 s inference and four threads delayed another account's unrelated GET
    by 2.5 s, (12 - 4) inferences, before the share here could apply; with
    2,048-token questions at 5-6 s each, a backlog of a hundred was minutes.
    So an account had `per_account` callers here that counted, its questions
    and (audit of 20 Oct 2026) its requests' documents, and its next one was
    refused at once. That count moved to core/threads on 31 Oct 2026, where a
    read waits for its place on the event loop before it takes a thread
    (the finding of scripts/concurrent_agents.sh: REST's refused searches,
    and REST's and the MCP's places adding up here), and a request's
    document takes one at once or is refused, as before. Until the audit of
    20 Oct 2026 a write's document vector, on its request's thread and
    holding the request's pooled connection, also waited here with no
    deadline; it waits at most core/threads.WAIT_SECONDS, under the pool's
    checkout wait."""

    def __init__(self, lead: Optional[float] = None, wait: float = WAIT_SECONDS) -> None:
        self._wait = wait
        self._cv = threading.Condition()
        self._busy = False
        self._holder: Optional[_Ticket] = None
        self._waiting: List[_Ticket] = []
        self._seq = 0
        self._served: Dict[Any, int] = {}
        self._turns_given = 0
        self._question_owed = False
        self._lead = DOCUMENT_LEAD_SECONDS if lead is None else lead

    def _next(self) -> Optional[_Ticket]:
        if not self._waiting:
            return None
        questions = [t for t in self._waiting if t.question]
        documents = [t for t in self._waiting if not t.question]
        if questions and documents:
            overdue = time.monotonic() - documents[0].since >= self._lead
            kind = documents if overdue and not self._question_owed else questions
        else:
            kind = questions or documents
        return min(kind, key=lambda t: (self._served.get(t.account, -1), t.seq))

    def _here(self, account: Any) -> int:
        """This account's callers here: the one running and those waiting."""
        held = self._holder is not None and self._holder.account == account
        return int(held) + sum(1 for t in self._waiting if t.account == account)

    def _grant(self, ticket: _Ticket) -> None:
        self._busy = True
        self._holder = ticket
        ticket.granted = True
        self._turns_given += 1
        self._served.pop(ticket.account, None)
        self._served[ticket.account] = self._turns_given
        while len(self._served) > _ACCOUNTS_KEPT:
            self._served.pop(next(iter(self._served)))

    def _hand_on(self) -> None:
        """The slot is free: give it to the next caller, or leave it free."""
        nxt = self._next()
        if nxt is None:
            self._busy = False
            self._holder = None
            return
        questions_waiting = any(t.question for t in self._waiting if t is not nxt)
        self._waiting.remove(nxt)
        self._question_owed = (not nxt.question) and questions_waiting
        self._grant(nxt)
        self._cv.notify_all()

    @contextlib.contextmanager
    def turn(self, question: bool, account: Any = None, request: bool = False):
        """The slot, for a question or a document billed to `account`;
        `request`: the caller is a request's thread (core/threads.on_a_request)."""
        with self._cv:
            me = _Ticket(question, account, time.monotonic(), self._seq)
            self._seq += 1
            if not self._busy and not self._waiting:
                self._grant(me)
            else:
                self._waiting.append(me)
                until = time.monotonic() + self._wait if request else None
                try:
                    while not me.granted:
                        if until is None:
                            self._cv.wait()
                            continue
                        left = until - time.monotonic()
                        if left <= 0:
                            raise Busy(f"no turn at the embedder within {self._wait:.0f} s")
                        self._cv.wait(left)
                except BaseException:
                    # Interrupted while waiting: a slot handed over meanwhile
                    # goes on to the next caller, never kept by nobody.
                    if me.granted:
                        self._hand_on()
                    else:
                        self._waiting.remove(me)
                    raise
        try:
            yield
        finally:
            with self._cv:
                self._hand_on()


_turns = _Turns()
_model: Optional[Tuple[Any, Any, List[str]]] = None
_last: Dict[str, Any] = {"ok": None, "at": None, "error": None}
# When `warm_up` began, while it runs; None before and after. One record for
# what /health says (`state`) and what a request's vector does (`embed`).
_warming: Dict[str, Any] = {"since": None}


def _record(ok: bool, error: Optional[str] = None) -> None:
    _last["ok"] = ok
    _last["at"] = time.time()
    _last["error"] = None if ok else error


def model_dir() -> str:
    return MODEL_DIR


def missing_files(folder: Optional[str] = None) -> List[str]:
    """The model's files that are not in the folder: all of them in an image
    built without EMBEDDING_PROVIDER=local."""
    folder = folder or model_dir()
    return [p for p in M.FILES if not os.path.isfile(os.path.join(folder, p))]


def threads() -> int:
    """Up to four of the CPUs this process may use, for one inference.

    Counted with the affinity mask rather than os.cpu_count(), which in a
    container pinned to two CPUs of a sixteen-core host says sixteen; to give
    the embedder fewer, pin the container (`cpuset` in docker-compose.yml). At
    most four because past that one text gains little, and the cores go to
    the database and to the requests."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:  # not Linux
        cpus = os.cpu_count() or 1
    return max(1, min(4, cpus))


def _load() -> Tuple[Any, Any, List[str]]:
    """The session, the tokenizer and the model's input names. Called during
    a turn, so by one thread at a time."""
    global _model
    if _model is not None:
        return _model
    folder = model_dir()
    missing = missing_files(folder)
    if missing:
        raise Unavailable(
            f"EMBEDDING_PROVIDER=local and the model is not in {folder} (missing "
            f"{', '.join(missing)}): this image was built without it. Set "
            f"EMBEDDING_PROVIDER=local in .env and rebuild: docker compose up -d --build")
    manifest = os.path.join(folder, M.MANIFEST)
    if os.path.isfile(manifest):
        with open(manifest) as f:
            revision = json.load(f).get("revision")
        if revision != M.REVISION:
            raise Unavailable(
                f"the model in {folder} is revision {revision}, this version of the "
                f"code expects {M.REVISION}: rebuild the image")

    import onnxruntime as ort
    import sentencepiece as spm

    options = ort.SessionOptions()
    options.intra_op_num_threads = threads()
    options.inter_op_num_threads = 1
    options.log_severity_level = 3
    session = ort.InferenceSession(os.path.join(folder, "onnx", "model.onnx"),
                                   sess_options=options,
                                   providers=["CPUExecutionProvider"])
    inputs = [i.name for i in session.get_inputs()]
    outputs = [o.name for o in session.get_outputs()]
    if "input_ids" not in inputs or set(inputs) - {"input_ids", "attention_mask"} \
            or "sentence_embedding" not in outputs:
        raise Unavailable(f"the model in {folder} takes {inputs} and gives {outputs}: "
                          f"not the pinned export")
    tokenizer = spm.SentencePieceProcessor(model_file=os.path.join(folder, "tokenizer.model"))
    _model = (session, tokenizer, inputs)
    return _model


def token_ids(tokenizer: Any, prompt: str, text: str) -> List[int]:
    """<bos> prompt + text <eos>, cut at the end of the text when the whole
    would pass the model's 2,048 tokens. Cut, not refused, like the model's own
    tokenizer with truncation on: the prompt stays whole, and the beginning of a
    text says more about it than its end."""
    ids = tokenizer.encode(prompt + text)
    return [BOS] + list(ids[: M.MAX_TOKENS - 2]) + [EOS]


def _account() -> Any:
    """The account this vector is billed to, for the turns' share; None
    outside a billing scope."""
    from ..services import llm_usage

    return llm_usage.current_tenant()


def _infer(text: str, prompt: str) -> Tuple[List[int], Any]:
    """The token ids and the model's output for one text, in a turn."""
    import numpy as np

    try:
        session, tokenizer, inputs = _load()
        ids = token_ids(tokenizer, prompt, text or "")
        feed = {"input_ids": np.asarray([ids], dtype=np.int64)}
        if "attention_mask" in inputs:
            feed["attention_mask"] = np.ones((1, len(ids)), dtype=np.int64)
        return ids, session.run(["sentence_embedding"], feed)[0][0]
    except Unavailable as e:
        _record(False, str(e))
        raise
    except Exception as e:
        # By its class and codes (`log_safe.error`), never the library's
        # message: this exception's text went on to a log line, to /health
        # and to the drain, and a third party's message is text nobody
        # here chose (CONTRIBUTING.md, logs; audit of 17 Oct 2026). The ones
        # above are this module's own words.
        _record(False, log_safe.error(e))
        raise Unavailable(f"the local embedder failed: {log_safe.error(e)}") from e


def embed(text: str, prompt: str) -> Tuple[List[float], int]:
    """The vector of `text` asked with `prompt`, of unit length, and the number
    of tokens the model read. Raises `Unavailable`.

    Normalized here even though the export already normalizes: the columns are
    compared by cosine distance either way, and a vector of length one is what
    the thresholds and the tests assume."""
    import numpy as np

    if _warming["since"] is not None and on_a_request():
        raise Warming("the model is still loading at this process's start")
    account = _account()
    # The account's place first (core/threads.at_the_embedder): held by a
    # read that waited for it on the loop, else taken at once or refused,
    # never waited for on a request's thread.
    with at_the_embedder(account) as placed:
        if not placed:
            raise Busy(f"this account already has its {CALLS_PER_ACCOUNT} places at the "
                       f"embedder, or a read of its waits for one")
        with _turns.turn(question=prompt == QUERY, account=account, request=on_a_request()):
            ids, out = _infer(text, prompt)
    vec = np.asarray(out, dtype=np.float64)
    norm = float(np.linalg.norm(vec))
    if vec.shape != (M.DIMENSIONS,) or not np.isfinite(norm) or norm == 0.0:
        detail = f"the model gave a vector of shape {vec.shape} and length {norm}"
        _record(False, detail)
        raise Unavailable(detail)
    _record(True)
    return (vec / norm).tolist(), len(ids)


def state() -> Dict[str, Any]:
    """What this process's embedder last did, for /health. The extraction's
    `provider_state` used to cover the vectors too, because both went to
    Google with one key; now they go to two places and each says its own."""
    out: Dict[str, Any] = {"provider": "local", "model": M.NAME, "revision": M.REVISION[:7]}
    since = _warming["since"]
    if since is not None:
        from datetime import datetime, timezone

        return {**out, "state": WARMING,
                "since": datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds"),
                "detail": "the model is loading at this process's start: until its first vector "
                          "a search answers 503 search_unavailable with Retry-After, a context "
                          "its facts with degraded, a memory is stored with its vector left to "
                          "the worker, and a fact written or corrected with a relation new to the "
                          "vocabulary answers 503 unavailable with Retry-After"}
    if _last["ok"] is None:
        return {**out, "state": "unknown", "detail": "no vector has been asked for yet"}
    age = int(time.time() - (_last["at"] or 0))
    if _last["ok"]:
        return {**out, "state": "working", "seconds_ago": age}
    return {**out, "state": "failing", "seconds_ago": age,
            "detail": (_last["error"] or "")[:300]}


def warm_up() -> threading.Thread:
    """Load the model and compute one vector, off the startup path. /health
    then says `working`, or `failing` with the reason (a missing model is the
    likely one), before a write needs a vector, and the first search does not
    wait for the load. Until then it says `warming` (with `ready` false), and
    a request's vector is refused at once (`Warming`): the port answers
    during the load, and what it answers is true. A readiness probe is for
    exactly this, "waiting for an application to perform time-consuming
    initial tasks, such as ... loading files, and warming caches", told
    apart from an app that failed (Kubernetes, Liveness, Readiness, and
    Startup Probes); the image's HEALTHCHECK reads `ready`."""
    _warming["since"] = time.time()

    def run() -> None:
        try:
            _warm()
        finally:
            # Ready, or failing with the reason `state` says: never warming
            # once the load has ended, whichever way.
            _warming["since"] = None

    def _warm() -> None:
        started = time.monotonic()
        try:
            embed("warm-up", QUERY)
        except Unavailable as e:
            # Where the model should be and what is missing there, from the
            # folder itself; the error by its class (`log_safe.error`), as
            # every log line (audit of 17 Oct 2026: this one printed the
            # message, by an exception the log test listed).
            folder = model_dir()
            logger.error("[embeddings] the local embedder cannot run (%s): model folder %s, "
                         "files missing there: %s. /health, asked from this machine, says "
                         "why under `embeddings`", log_safe.error(e), folder,
                         ", ".join(missing_files(folder)) or "none")
            return
        logger.info("[embeddings] local embedder ready: %s at %s, %d threads, %.1f s",
                    M.NAME, M.REVISION[:7], threads(), time.monotonic() - started)

    t = threading.Thread(target=run, name="local-embedder-warm-up", daemon=True)
    t.start()
    return t
