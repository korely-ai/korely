"""The application.

Five routers and one MCP endpoint over one store. The service this was
extracted from mounted thirty-four routers, a scheduler with eleven jobs and two
MCP servers in the same process, because it was also a note-taking app, a
meeting recorder and a chat product.

What is here is what a memory service needs and nothing that only made sense
when it shared a process with something else.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import quote

import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.utils import is_body_allowed_for_status_code
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import admin, audit, batch, context, facts, memories, ping
from .api.errors import error_body, validation_errors_shown, validation_message
from . import __version__
from .config import get_settings
from .core.threads import busy_refusal, deadline_passed, serving_a_request
from .core.times import aware
from .services.memory_core import log_safe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)



def _suggest_the_right_field(request, errors) -> None:
    """Turn "unknown field" into "did you mean user_id".

    The moment a developer sees this error is the moment they are stuck, and
    the name they meant is one edit away from the name they typed. Best effort
    by construction: this runs inside an error handler, so anything unexpected
    here must produce a plain 422 rather than a 500.
    """
    import difflib

    try:
        route = request.scope.get("route")
        field = getattr(route, "body_field", None)
        # FastAPI's own wrapper, not the pydantic model: the class is one hop
        # further in, and which hop depends on the version. Ask for both.
        model = getattr(getattr(field, "field_info", None), "annotation", None) \
            or getattr(field, "type_", None)
        known = list(getattr(model, "model_fields", {}) or {})
        if not known:
            return
        for e in errors:
            if e.get("type") != "extra_forbidden":
                continue
            sent = str(e.get("loc", ["", ""])[-1])
            close = difflib.get_close_matches(sent, known, n=1, cutoff=0.6)
            e["msg"] = (
                f"{sent} is not a field here. Did you mean {close[0]}?"
                if close else
                f"{sent} is not a field here. This endpoint takes: "
                + ", ".join(sorted(known))
            )
    except Exception:  # pragma: no cover - an error handler never raises
        return

# Memories waiting this long with no extraction answering in as long: nothing
# is mining them. The drain ticks every ten seconds; a backlog it is working
# through shows extractions, so it never reads as this.
_STUCK_SECONDS = 300

# `extraction` with no writer anywhere: the memories are kept and wait.
NOT_CONFIGURED = {"state": "not configured",
                  "detail": "no API key: memories are stored, readable and waiting; the "
                            "facts are mined once a key is set"}
# With the database unreadable no writer chosen on the Models page can be
# found (it is kept there), so "not configured" would be a guess.
WRITER_UNREADABLE = {"state": "unknown",
                     "detail": "the database, where the writer chosen on the Models page is "
                               "kept, could not be read"}


def _extraction_now(prov: dict):
    """(`extraction` of /health, `database` or None). The process's own
    calls say what the provider did; the store says whether anything is
    mining at all (2026-10-06, the audit of 6 October, row 3): with the
    worker in another process (RUN_WORKER=false) this process never extracts
    and `extraction` stayed `unknown`, and a drain that stopped without a
    failed call looked healthy."""
    from datetime import datetime, timezone

    from .db import get_sessionmaker
    from .services.facts.service import extraction_progress

    try:
        db = get_sessionmaker()()
        try:
            progress = extraction_progress(db)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - /health answers whatever the store does
        return prov, {"state": "failing", "detail": f"{type(e).__name__}: {e}"[:200]}

    def seconds_since(when):
        if when is None:
            return None
        return int((datetime.now(timezone.utc) - aware(when)).total_seconds())

    waited = seconds_since(progress["oldest_waiting"])
    silent = seconds_since(progress["last_extraction"])
    if prov["state"] == NOT_CONFIGURED["state"]:
        # With no writer the memories wait by design (README): nothing to
        # mine them is the answer, not a failure.
        return prov, None
    if (waited is not None and waited > _STUCK_SECONDS
            and (silent is None or silent > _STUCK_SECONDS)):
        return {"state": "failing", "seconds_ago": silent,
                "detail": f"memories waiting for {waited} s and no extraction answered "
                          f"for {'ever' if silent is None else f'{silent} s'}: nothing is "
                          f"mining them (the worker, or the provider; `docker compose "
                          f"logs api`)"}, None
    if prov["state"] == "unknown" and silent is not None:
        return {"state": "working", "seconds_ago": silent,
                "detail": "the last extraction, by whichever process runs the worker"}, None
    return prov, None


def from_the_internet(request) -> bool:
    """Whether the caller is at a global address (RFC 6890's registries,
    `ipaddress.is_global`): somebody on the internet, through the proxy,
    rather than this machine, its containers or its own network. The
    address is the one uvicorn took from `X-Forwarded-For`, which the
    shipped Caddy writes itself (README, "Your own proxy in front"). A
    caller with no address, or one that is not an address, is not."""
    import ipaddress

    host = getattr(getattr(request, "client", None), "host", None)
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_global


# What /health tells an address on the internet of each part (audit of 17
# Oct 2026): its state and how long ago, never why. The why is a
# provider's words ("prepayment credits are depleted"), a database
# driver's (its host and port), the paths of the embedder's model, and the
# shipped Caddyfile serves /health to anybody, with no key.
_PUBLIC_FIELDS = ("state", "seconds_ago", "since")
# The dashboard reads mode, key_prefix and mcp_path from here at start, and
# its browser is an address on the internet; none of the three is a why.
_PUBLIC_TOP = ("version", "status", "provider", "mode", "key_prefix", "mcp_path")
WITHHELD = ("withheld from an address on the internet: ask this machine "
            "(curl 127.0.0.1:8000/health) for why")


def states_only(out: dict) -> dict:
    """`out` of /health with each part's state and nothing else."""
    def part(v: dict) -> dict:
        if "state" in v:
            return {k: v[k] for k in _PUBLIC_FIELDS if k in v}
        # A part of parts: `jobs`, `writers`.
        return {k: part(x) for k, x in v.items() if isinstance(x, dict)}

    kept = {k: out[k] for k in _PUBLIC_TOP if k in out}
    kept.update({k: part(v) for k, v in out.items() if isinstance(v, dict)})
    kept["details"] = WITHHELD
    return kept


def loggable_path(path: str, scope: dict | None = None) -> str:
    """A request path as a log may keep it (the container's log is kept, and
    an erasure never reaches it): no query string, and of the route that took
    the request its template, `/v1/users/{end_user}/memories`. What a path
    carries is the caller's own string (an end user, an agent namespace), a
    secret (the hosted service's billing alert), an id: none of it stays.
    Nothing the client wrote stays: a parameter becomes its name, and the
    screen's (`loggable`) becomes the file of the dashboard's folder the
    screen served, when it served one (spa.OWN_FILE). A path no route took
    (a 404, the redirect of a path one slash away from a route) keeps its
    first segment only when a route of the app begins with it, `/v1/...`;
    any other is `/...` (audit of 27 Oct 2026: `/<end user>/memories` was
    logged raw).
    `path` is quoted, as uvicorn writes it; `scope` is the request's, where
    the router wrote the route it picked."""
    from .api.spa import OWN_FILE

    path = (path or "").split("?", 1)[0]
    scope = scope or {}
    route = scope.get("route")
    convertors = getattr(route, "param_convertors", None)
    if isinstance(convertors, dict) and isinstance(getattr(route, "path_format", None), str):
        if not convertors:
            return path
        # The screen's catch-all takes `//v1/users/<id>/memories`,
        # `/<id>/memories`, `/api/v1/...` too: its value is never written,
        # only the name of the file it served (a page of the screen is
        # index.html, served for any path: its template).
        served = scope.get(OWN_FILE)
        return route.path_format.format(**{
            name: (quote(served) if getattr(conv, "loggable", False) and served is not None
                   else "{" + name + "}")
            for name, conv in convertors.items()})
    return _first_segment(path, scope.get(_APP, scope.get("app")))


def _first_segment(path: str, app) -> str:
    """`/v1/...`: what follows the first segment is where the ids are, and
    the first segment is kept only when a route of `app` begins with it
    (its route table, never a list of names), once the address the MCP
    documents is routed (`/agent/mcp` is the mount's `/mcp`); `/...`
    otherwise, since then it is the client's alone."""
    from .mcp.server import routed_path

    parts = path.split("/", 2)
    if parts == ["", ""]:
        return path
    routed = routed_path(path).split("/", 2)
    if len(parts) < 2 or len(routed) < 2 or routed[1] not in _first_segments(app):
        return "/..."
    return path if len(parts) == 2 else "/".join(parts[:2]) + "/..."


def _first_segments(app) -> set:
    """The fixed first segment of each route of `app` (`v1`, `health`,
    `mcp`); a route that begins with a parameter (the screen's) has none."""
    from .api.errors import every_route

    firsts = set()
    for route in every_route(getattr(app, "routes", None) or ()):
        first = (getattr(route, "path_format", None) or "").split("/")[1:2]
        if first and "{" not in first[0]:
            firsts.add(first[0])
    return firsts


# The request being answered, for the access line: uvicorn writes it while
# the response starts, in the request's own context, and passes the log only
# the path. The router writes the route it picked into this same scope.
_REQUEST: ContextVar[dict | None] = ContextVar("korely_request_scope", default=None)
# The app the request came in to, whose route table names a first segment:
# a mounted app (the MCP's) writes itself over the scope's `app`.
_APP = "korely_app"


class _RememberTheRequest:
    """The outermost middleware: puts the request's scope where the access
    line can read it. Never reset: each request is a task of its own, and the
    line of a 500 is written after the exception has left this frame."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            scope[_APP] = scope.get("app")
            _REQUEST.set(scope)
            # A request's work: the per-account caps and the deadlines of
            # core/threads.py apply to it, and never to the worker's.
            serving_a_request()
        await self.app(scope, receive, send)


async def _answer_refusal(send, status_code: int, sentence: str, *, close: bool = True) -> None:
    """A refusal answered by a middleware, before any route: the error shape
    of every other answer, sent by the one sender of such refusals
    (api/errors.send_refusal). `close`: the body was not read, so the
    connection cannot carry another request."""
    from fastapi import HTTPException

    from .api.errors import send_refusal

    await send_refusal(send, HTTPException(status_code, sentence), close=close)


class _NulInTheAddress:
    """A NUL byte (U+0000) in a request's path or query is a 422 that names
    where it is, by the rule of every door (services/limits.nul_problem),
    before any route reads it. Each route's own check of its query was
    written one by one, and each audit found doors without it: GET
    /admin/audit, /admin/aliases and /admin/memories passed the byte to the
    driver and answered 500 (audit of 20 Oct 2026, after the audit of 19 Oct
    2026 closed their neighbours). Here every path and query of the install is read
    once, the Cloud's, OAuth's and the MCP's included."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        from urllib.parse import parse_qsl

        from .services.limits import nul_problem

        problem = nul_problem("the path", scope.get("path") or "")
        if problem is None:
            query = (scope.get("query_string") or b"").decode("latin-1")
            problem = next((p for name, value in parse_qsl(query, keep_blank_values=True)
                            if (p := nul_problem("", {name: value}))), None)
        if problem:
            return await _answer_refusal(send, 422, problem)
        return await self.app(scope, receive, send)


class _BodyCap:
    """No request body past its route's ceiling (services/limits.body_cap) is
    read (audit of 17 Oct 2026): every JSON body was read and decoded whole
    before the key was checked, and nothing (this app, uvicorn, Caddy) bounded
    its size, so a request with no key could fill the memory of the one
    process that serves every tenant. A declared length past the ceiling is
    refused before a byte is read; a body without one (chunked) is counted as
    it arrives, held until it ends, and refused at the first piece past it.
    413 `payload_too_large`, the error shape of every other answer.

    The ceiling is the one of the path the router will see
    (mcp/server.routed_path): /agent/mcp, the Cloud's address of the MCP, had
    REST's ceiling where /mcp had the MCP's (audit of 18 Oct 2026). And a
    ceiling above the common one that belongs to a write key (the batch's) is
    granted only once the request's bearer is found to be one, and such a
    body waits for one of LARGE_BODIES_AT_ONCE: a batch ceiling given by the
    path alone was 78 MB decoded for a request with no key at all.

    A place bounds the memory the raw large bodies take, so the body is read
    inside it; and the client sets the pace of that read. Since the audit of
    19 Oct 2026 (one write key's two stalled uploads held both places, and every other account's large batch
    waited with no answer until that sender stopped) an account holds at
    most LARGE_BODIES_PER_ACCOUNT of them, its next large body waiting for
    its own without taking one of the process's, and a body that has not
    arrived whole LARGE_BODY_READ_SECONDS after taking its place gives it
    back with 408 `request_timeout`. Reading first and taking a place after
    would give the memory bound up: any number of free write keys could
    each hold 78 MB at once.

    Every other body is read here too, whatever its type, and has
    BODY_READ_SECONDS to arrive whole (408); what those bodies hold at once,
    across the process, is at most BODIES_IN_FLIGHT_BYTES, and one that
    would take more is answered 503 with Retry-After at once (audit of 27
    Oct 2026: a keyless body under the common ceiling was read with no deadline and
    nothing counted them, and a form body went to the app unread)."""

    def __init__(self, app):
        self.app = app
        # The large bodies' places: an account's own share, then one of the
        # process's (core/threads.Gates, the MCP's door's kind of table).
        from .core.threads import Gates
        from .services.limits import LARGE_BODIES_AT_ONCE, LARGE_BODIES_PER_ACCOUNT

        self._places = Gates(LARGE_BODIES_PER_ACCOUNT, total=LARGE_BODIES_AT_ONCE)
        # What the bodies under the common ceiling cost now: a network's
        # share, then the process's (core/threads.Budget, Gates' kin).
        from .core.threads import Budget
        from .services.limits import BODIES_IN_FLIGHT_BYTES, BODIES_PER_NETWORK

        self._budget = Budget(BODIES_PER_NETWORK, total=BODIES_IN_FLIGHT_BYTES)

    def _large_place(self, account):
        """One of the LARGE_BODIES_AT_ONCE places for a body past the common
        ceiling, once the account's own share (LARGE_BODIES_PER_ACCOUNT) is
        free."""
        return self._places.place(account)

    @staticmethod
    async def _refuse(send, status_code: int, sentence: str) -> None:
        """Refused while the body may still be arriving: closed after."""
        await _answer_refusal(send, status_code, sentence)

    @classmethod
    async def _too_large(cls, send, cap: int) -> None:
        await cls._refuse(send, 413, f"The request body is larger than {cap:,} bytes, "
                                     f"the most this route takes.")

    @classmethod
    async def _too_slow(cls, send, seconds: float) -> None:
        await cls._refuse(send, 408, f"The request body did not arrive whole within "
                                     f"{seconds:.0f} s; send it again.")

    @staticmethod
    async def _busy(send, sentence: str) -> None:
        """503 `unavailable` with Retry-After, while the body may still be
        arriving: closed after."""
        from fastapi import HTTPException

        from .api.errors import send_refusal
        from .core.threads import BUSY_RETRY_SECONDS

        await send_refusal(send, HTTPException(
            503, {"code": "unavailable", "message": sentence},
            headers={"Retry-After": str(BUSY_RETRY_SECONDS)}), close=True)

    @classmethod
    async def _full(cls, send) -> None:
        await cls._busy(send, "The server holds as many request bodies from this network, or "
                              "from all, as it reads at once: retry in a few seconds.")

    @staticmethod
    async def _weighed(scope, whole: bytes, past: bool, batch: bool, writer) -> tuple:
        """((the status and sentence refusing `whole` before it is decoded) or
        None; what decoding it costs at most (limits.body_weighed); the
        bearer's account, once looked up). Counted as the batch's most items
        past the common ceiling (`past`), else as what items at their largest
        fill of it; a body to a batch's route (`batch`) denser than that is
        counted again as the batch it is, once its bearer is found to be a
        write key (`writer`, looked up if not yet)."""
        from .services.batch import MAX_BATCH_ITEMS
        from .services.limits import COMMON_ITEMS, body_encoding_problem, body_weighed

        problem = body_encoding_problem(whole)
        if problem:
            return (400, problem), 0, writer
        items = MAX_BATCH_ITEMS if past else COMMON_ITEMS
        dense, cost = body_weighed(whole, items)
        if (dense and items < MAX_BATCH_ITEMS and batch
                and (writer := writer or await _writer_account(scope)) is not None):
            dense, cost = body_weighed(whole, MAX_BATCH_ITEMS)
        return ((413, dense) if dense else None), cost, writer

    async def _a_place_by(self, held, account, deadline) -> bool:
        """One of the large places for `account`'s body, held in `held` and
        waited for no longer than `deadline`; False past it, nothing held.
        A body that waits for one holds a connection and what it has read:
        a chunked one waited with no end, and one write key's 32 held the
        whole of the count (the review of the audit of 27 Oct 2026); a
        declared one still did (the audit before 1.0.0)."""
        import asyncio

        try:
            async with asyncio.timeout_at(deadline):
                await held.enter_async_context(self._large_place(account))
        except TimeoutError:
            return False
        return True

    @classmethod
    async def _no_place(cls, send) -> None:
        await cls._busy(send, "The server is reading as many large request bodies as it "
                              "reads at once, or this account's: retry in a few seconds.")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        from .mcp.server import routed_path
        from .services.limits import body_cap

        path = routed_path(scope.get("path") or "")
        cap = body_cap(path)
        writer_cap = body_cap(path, writer=True)
        declared = None
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                declared = value
                break
        writer = None
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                size = 0  # the server refuses a length it cannot read
            if size > cap and size <= writer_cap:
                writer = await _writer_account(scope)
            if size > writer_cap or (size > cap and writer is None):
                return await self._too_large(send, writer_cap if size > writer_cap else cap)
        import asyncio

        from .services.limits import BODY_READ_SECONDS, LARGE_BODY_READ_SECONDS

        loop = asyncio.get_running_loop()

        async with contextlib.AsyncExitStack() as held:
            # Every body has BODY_READ_SECONDS to arrive whole; past the
            # common ceiling it holds a place, and from then on it has
            # LARGE_BODY_READ_SECONDS.
            seconds = BODY_READ_SECONDS
            large = declared is not None and size > cap
            if large:
                # A write key's large body: read whole, so its shape is
                # counted before anything decodes it, once it holds its
                # place, which it waits for no longer than a body has to
                # arrive.
                if not await self._a_place_by(held, writer, loop.time() + seconds):
                    return await self._no_place(send)
                seconds = LARGE_BODY_READ_SECONDS
            deadline = loop.time() + seconds
            # What this body holds of its network's share (BODIES_PER_NETWORK)
            # and of the process's, given back when its request ends.
            from .services.rate_limit import network_of

            client = scope.get("client")
            network = network_of(client[0] if client else None)
            kept = 0

            def give_back():
                self._budget.give_back(network, kept)
            held.callback(give_back)
            pieces, total = [], 0
            while True:
                try:
                    async with asyncio.timeout_at(deadline):
                        message = await receive()
                except TimeoutError:
                    return await self._too_slow(send, seconds)
                if message["type"] != "http.request":
                    break
                piece = message.get("body") or b""
                before = total
                total += len(piece)
                if declared is None and before <= cap < total:
                    if writer_cap <= cap or (writer := await _writer_account(scope)) is None:
                        return await self._too_large(send, cap)
                    # It waits for its place no longer than its deadline,
                    # holding what it has read of its network's share; let
                    # in, its place bounds it, and the share is given back.
                    if not await self._a_place_by(held, writer, deadline):
                        return await self._no_place(send)
                    self._budget.give_back(network, kept)
                    kept = 0
                    large, seconds = True, LARGE_BODY_READ_SECONDS
                    deadline = loop.time() + seconds
                if total > writer_cap:
                    return await self._too_large(send, writer_cap)
                if not large:
                    # Under the common ceiling: counted in its network's
                    # share (a large one is bounded by its place).
                    if not self._budget.take(network, len(piece)):
                        return await self._full(send)
                    kept += len(piece)
                pieces.append(piece)
                if not message.get("more_body"):
                    break
            whole = b"".join(pieces)
            del pieces
            # Bytes do not bound what decoding a body costs: a batch of empty
            # objects decodes at some 21 times its bytes, and two at the
            # batch's ceiling restarted an API held to 1.5 GB (audit of
            # 18 Oct 2026); one long string with one character past U+FFFF
            # at 9 times (audit of 20 Oct 2026). Its values, strings and characters
            # are counted first, against what lawful items can hold: a
            # write key's large batch the batch's most items, any other
            # body what items at their largest fill of the common ceiling
            # (bodies under it were decoded unread, before any key, at up
            # to some 26 times their bytes; audit of 20 Oct 2026). A batch under the
            # common ceiling that is denser than that is counted again as
            # the batch it is, once its bearer is found to be a write key.
            # Whatever its type, which each decoder reads its own way: a
            # tools/call typed `application/json , text/html` and a keyless
            # /register typed text/plain were decoded unweighed (the audit
            # before 1.0.0).
            refused, cost, writer = await self._weighed(scope, whole, total > cap,
                                                        writer_cap > cap, writer)
            if refused:
                return await self._refuse(send, *refused)
            if not large and cost > self._budget.per_key:
                # More than a network's share: no retry lets it in, and
                # a lawful batch of 1.4 to 2 MiB was answered 503 on an
                # idle server, every retry the same (the audit before
                # 1.0.0). A write key's batch is decoded in a
                # large place, as one past the ceiling is; no other
                # body under the ceiling may cost so much.
                if writer_cap <= cap or (writer := writer or await _writer_account(scope)) is None:
                    return await self._refuse(
                        send, 413, f"The request body would take more than "
                                   f"{self._budget.per_key:,} bytes once decoded, the most a "
                                   f"body under {cap:,} bytes may: send fewer or smaller values.")
                if not await self._a_place_by(held, writer, deadline):
                    return await self._no_place(send)
                self._budget.give_back(network, kept)
                kept, large = 0, True
            if not large:
                # What decoding it costs at most, in its network's share
                # with its bytes: bytes alone did not bound it, a body of
                # small objects decoding at some 25 times its bytes.
                more = max(0, cost - kept)
                if not self._budget.take(network, more):
                    return await self._full(send)
                kept += more
            replayed = False

            async def replay():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": whole, "more_body": False}
                return await receive()

            return await self.app(scope, replay, send)


async def _writer_account(scope) -> str | None:
    """The account of the request's one `Authorization: Bearer` when it is a
    live key with memories:write, the scope of the routes whose ceiling is
    larger (`_BodyCap`), for its share of the large-body places; else None.
    Looked up as the route looks it up (core/auth.verify_api_key: the key's
    shape, the cache of unknown keys, the store), off the loop; any refusal
    or a store that does not answer is None, the common ceiling. On the
    admission pool, never behind an MCP tool's body (core/threads.py)."""
    given = [value for name, value in scope.get("headers") or () if name == b"authorization"]
    if len(given) != 1:
        return None
    kind, _, token = given[0].decode("latin-1").partition(" ")
    token = token.strip()
    if kind.lower() != "bearer" or not token:
        return None
    from .core.auth import credential_lookup, missing_scopes
    from .core.threads import admission_off_the_loop
    from .db import get_sessionmaker
    from .services import keys

    def look() -> str | None:
        db = get_sessionmaker()()
        try:
            row = credential_lookup(keys.verify_api_key, db, token)
            if row is None or missing_scopes(row.scopes, "memories:write"):
                return None
            return str(row.tenant_id)
        finally:
            db.close()

    try:
        return await admission_off_the_loop(look)
    except Exception:  # noqa: BLE001 - the store's outage: the common ceiling
        return None


class _LoggableAccessLine(logging.Filter):
    """uvicorn's access line as a log may keep it. The query string went first
    (2026-10-06: GET /v1/context?query=...&user_id=... wrote the question and
    the end user into the container's log, where an erasure never reaches),
    then the values a path carries (11 Oct 2026, the legal review: DELETE
    /v1/users/maria@example.com/memories stayed there too), `loggable_path`.
    The record's arguments are (client, method, path, http version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (args[0], args[1], loggable_path(args[2], _REQUEST.get()),
                           args[3], args[4])
        return True


class _StackWithoutMessages(logging.Filter):
    """uvicorn's "Exception in ASGI application" as a log may keep it: its
    traceback printed the exception's message, and a database error's message
    carries the row (PostgreSQL's DETAIL: an end user's id, an alias's text;
    audit of 17 Oct 2026). The stack stays, frames only (`log_safe.stack`)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[1] is not None:
            record.msg = f"{record.getMessage()} {log_safe.stack(record.exc_info[1])}"
            record.args = None
            record.exc_info = None
            record.exc_text = None
        return True


class _NoUpgradeWarnings(logging.Filter):
    """uvicorn's two warnings on a request with an Upgrade header ("Unsupported
    upgrade request." and "No supported WebSocket library detected..."),
    dropped: the image starts it with `--ws none` on purpose (Dockerfile), so
    the request is an HTTP one with its access line, and the warnings, which
    anybody can repeat with a header, read as a broken install. Recognised by
    the uvicorn function that writes both, never by their text."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.funcName != "_unsupported_upgrade_warning"


def _quiet_access_log() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _LoggableAccessLine) for f in access.filters):
        access.addFilter(_LoggableAccessLine())
    errors = logging.getLogger("uvicorn.error")
    for kind in (_StackWithoutMessages, _NoUpgradeWarnings):
        if not any(isinstance(f, kind) for f in errors.filters):
            errors.addFilter(kind())


# Swagger UI's own files, in the package (korely_agent/docs_ui/README.md: the
# version and where they come from). The page used to load them from
# cdn.jsdelivr.net in the browser, and on a network with no way out it stayed
# blank (2026-10-06).
_DOCS_UI = Path(__file__).resolve().parent / "docs_ui"
DOCS_UI_PATH = "/docs-ui"


def _serve_the_api_page(app: FastAPI) -> None:
    """/docs, with Swagger UI's script, style and icon served by this machine."""
    from fastapi.openapi.docs import get_swagger_ui_html
    from fastapi.staticfiles import StaticFiles

    app.mount(DOCS_UI_PATH, StaticFiles(directory=_DOCS_UI), name="docs-ui")

    # The app is read from the request, never closed over: FastAPI keeps
    # every endpoint function in a process-wide cache
    # (fastapi.dependencies.models), so a closure over `app` kept each app
    # ever built alive, about 3 MB each, and a test process grew past 1.4 GB
    # (audit of 17 Oct 2026; tests/test_apps_are_let_go.py).
    @app.get("/docs", include_in_schema=False)
    def api_page(request: Request):
        app = request.app
        return get_swagger_ui_html(
            openapi_url=app.openapi_url, title=f"{app.title} - API",
            swagger_js_url=f"{DOCS_UI_PATH}/swagger-ui-bundle.js",
            swagger_css_url=f"{DOCS_UI_PATH}/swagger-ui.css",
            swagger_favicon_url=f"{DOCS_UI_PATH}/favicon-32x32.png",
        )


# The two APIs, as sections of /docs and of the spec the site publishes
# (scripts/dump_openapi.py --public). /admin is not only the dashboard's: a
# key issued with the `admin` scope (`manage issue-key --admin`, or the API
# keys page) calls it from a script, and nothing said so where a developer
# reads the API (audit of 17 Oct 2026).
API_SECTIONS = [
    {"name": "v1",
     "description": "The memory API: what an agent calls, with any key of the project "
                    "(`memories:read` to read, `memories:write` to write)."},
    {"name": "admin",
     "description": "The administration API: projects, keys, webhooks, the requests and the "
                    "usage of the account, its audit trail and its model settings. The "
                    "dashboard calls it with its session, and a program with a key issued "
                    "with the `admin` scope (an ordinary key gets 403). Signing in "
                    "(`/admin/session`) is the dashboard's."},
]


def _lifespan(s, mcp_door: dict):
    """The app's start and stop: the vectors' model checked, the MCP session
    manager (its outcome in `mcp_door`, for /health), the worker, the local
    embedder's warm-up."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from contextlib import AsyncExitStack

        # Before the worker writes and the server answers: stored vectors of
        # another model stop the start, with what to do (services/vectors.py).
        from .services.vectors import refuse_vectors_of_another_model

        refuse_vectors_of_another_model()
        task = None
        stack = AsyncExitStack()
        try:
            from .mcp.server import agent_mcp_lifespan

            await stack.enter_async_context(agent_mcp_lifespan())
            logger.info("[mcp] MCP surface listening on /mcp")
            if mcp_door["state"] != "failing":
                mcp_door.update(state="working", detail=None)
        except Exception as e:
            logger.error("[mcp] MCP session manager failed to start; REST is unaffected: %s",
                         log_safe.stack(e))
            # A mount that failed is the cause, and stays the detail.
            if mcp_door["state"] != "failing":
                mcp_door.update(state="failing",
                                detail=f"the MCP session manager did not start "
                                       f"({log_safe.error(e)}): /mcp opens no session")

        if s.run_worker:
            import asyncio

            from .worker import run_forever

            stop = asyncio.Event()
            task = asyncio.create_task(run_forever(stop))
            app.state.worker_stop = stop
        else:
            logger.info("[worker] Worker disabled (run_worker=false). Facts will not be "
                        "extracted unless `python -m korely_agent.worker` runs "
                        "somewhere.")

        if s.embedding_provider == "local":
            from .core import local_embedder

            local_embedder.warm_up()
        try:
            yield
        finally:
            if task is not None:
                app.state.worker_stop.set()
                task.cancel()
            await stack.aclose()

    return lifespan


def _health(request, s, mcp_door: dict) -> dict:
    """The body of GET /health (its docstring is the route's, in /docs)."""
    from .core.llm import embedding_state, provider_configured, provider_state
    from .mcp import server as mcp_server
    from .services import keys as key_service

    # The store is read on every check, with or without a writer (audit
    # of 17 Oct 2026): it was read only with one, and with Postgres down
    # the writer saved on the Models page could not be found either, so
    # a restart with the database stopped answered "ok, not configured:
    # memories are stored, readable and waiting".
    configured = provider_configured()
    extraction, database = _extraction_now(
        provider_state() if configured else dict(NOT_CONFIGURED))
    if database is not None and not configured:
        extraction = dict(WRITER_UNREADABLE)

    out = {
        "version": __version__,
        "status": "ok" if extraction["state"] != "failing" else "degraded",
        "extraction": extraction,
        "provider": s.extraction_provider,
        # self_hosted or hosted (8 Oct 2026, the one codebase): the
        # dashboard asks here which screen to show, instead of asking a
        # route that is not there and leaving a 404 in every console.
        "mode": s.korely_mode,
        # What a key of this install begins with and where its MCP door
        # is, for the dashboard's snippets: it wrote both by hand, and a
        # change here would not have reached it (audit of 17 Oct 2026).
        "key_prefix": key_service.minted_prefix(),
        "mcp_path": mcp_server.published_path(),
    }
    if database is not None:
        out["database"] = database
        out["status"] = "degraded"
    # A region whose writer is resting after a refusal, with the time it
    # began (15 Oct 2026: the Gemini credit ran out and this said
    # "working"). One refused for payment waits for a person: degraded.
    from .services.facts.service import writers_refused

    refused = writers_refused()
    if refused:
        out["writers"] = refused
        if any(w["payment_required"] for w in refused.values()):
            out["status"] = "degraded"
    # Only when an export was asked for: an install that never set
    # FACTS_EXPORT_DIR has nothing to report and should not read degraded.
    if s.facts_export_dir:
        from .worker import export_state

        out["export"] = export_state()
        if out["export"]["state"] == "failing":
            out["status"] = "degraded"
    # The worker's other jobs, which used to record nothing: a webhook
    # queue that failed on every tick read "ok" (audit of 17 Oct 2026).
    # `unknown` until each has run here, and with RUN_WORKER=false.
    from .worker import jobs_state

    out["jobs"] = jobs_state()
    if any(j["state"] == "failing" for j in out["jobs"].values()):
        out["status"] = "degraded"
    # The cost rows and the audit trail, written apart from the work and
    # never failing it: a write of either that failed was a log line only,
    # and the Cloud's daily stop sums the cost rows (audit of 17 Oct 2026).
    from .services import bookkeeping

    out["records"] = bookkeeping.state()
    if any(r["state"] == "failing" for r in out["records"].values()):
        out["status"] = "degraded"
    # Only when the vectors are computed here. With Gemini they go through
    # the key the extraction uses, and `extraction` above already says
    # what that provider last did.
    if s.embedding_provider == "local":
        from .core import local_embedder

        out["embeddings"] = local_embedder.state()
    else:
        # Gemini's vectors have their own state since 2026-10-06: they
        # used to answer for the extraction (core/llm._last_embedding).
        out["embeddings"] = embedding_state()
    if out["embeddings"]["state"] == "failing":
        out["status"] = "degraded"
    out["mcp"] = {k: v for k, v in mcp_door.items() if v is not None}
    if mcp_door["state"] == "failing":
        out["status"] = "degraded"
    if from_the_internet(request):
        return states_only(out)
    return out


def _answer_errors(app: FastAPI) -> None:
    """The error answers of every route: a 500 without its message, every
    refusal with `code` and `message`, a 422 that never reads the body
    back."""
    @app.exception_handler(500)
    def _500(request, exc):
        # The stack without the messages (log_safe.stack), never
        # logger.exception: run in the thread pool it found no exception and
        # wrote "NoneType: None", and the day it found one it would print the
        # message (audit of 17 Oct 2026).
        logger.error("[api] unhandled error on %s %s",
                     loggable_path(quote(request.url.path), request.scope), log_safe.stack(exc))
        return JSONResponse(error_body(500, "internal error"), status_code=500)

    @app.exception_handler(OperationalError)
    def _deadline(request, exc):
        """A deadline of a request's transaction passed (a lock waited on
        past core/threads.LOCK_WAIT_SECONDS, a statement past
        STATEMENT_SECONDS): 503 `unavailable` with Retry-After, never a 500.
        Any other OperationalError is the 500 it was."""
        if deadline_passed(exc):
            logger.warning("[api] %s %s: a database deadline passed (%s)",
                           request.method, loggable_path(quote(request.url.path), request.scope),
                           getattr(getattr(exc, "orig", None), "pgcode", None))
            return _http(request, busy_refusal())
        return _500(request, exc)

    @app.exception_handler(StarletteHTTPException)
    def _http(request, exc):
        """Every refusal a route raises, with `code` and `message` next to
        `detail` (api/errors.py). What FastAPI's own handler does otherwise:
        the same status, the same headers (`Retry-After`, `WWW-Authenticate`)
        and no body where HTTP allows none."""
        headers = getattr(exc, "headers", None)
        if not is_body_allowed_for_status_code(exc.status_code):
            return Response(status_code=exc.status_code, headers=headers)
        return JSONResponse(
            error_body(exc.status_code, exc.detail, code=getattr(exc, "code", None)),
            status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    def _422(request, exc):
        """A validation error must not read the request back out loud.

        Pydantic v2 puts the offending value in `input`, and FastAPI's default
        handler passes it straight through, so a 422 returns the body that
        caused it. Usually that is harmless and useful. On PUT /admin/provider
        it is the customer's model key: mistype the field name and the key comes
        back in the response.

        Found while pasting a key into a real install, by sending `api_key`
        where the endpoint wants `key`:

            {"detail":[{"type":"missing","loc":["body","key"],
                        "msg":"Field required",
                        "input":{"api_key":"AQ.Ab8RN6...'"}]}

        The caller already knows what they sent, so nothing is disclosed to a
        stranger. What happens instead is that the secret lands wherever the
        caller's errors land: a client log, a terminal, a bug report pasted into
        a chat. The acceptance suite already checks that the key never comes
        back from this endpoint; this was the hole in that property.

        `loc` and `msg` say which field is wrong, which is the part that helps.

        Bodies also refuse fields they do not know (see api/_bodies.py), and a
        refusal is only useful if it says what to write instead. `userId` is a
        typo for `user_id`, not a mystery, so the error says so.
        """
        errors = exc.errors()
        safe = validation_errors_shown(errors)
        _suggest_the_right_field(request, safe)
        # `message` is built from `loc` and `msg` of the list above, which has
        # `input` removed: the same property holds for it.
        body = error_body(422, safe)
        body["message"] = validation_message(safe, total=len(errors))
        return JSONResponse(body, status_code=422)


def _mount_routes(app: FastAPI, mcp_door: dict) -> None:
    """The routers, OAuth's, the hosted service's when it is on, and the MCP
    surface (a mount that fails goes into `mcp_door`, for /health)."""
    for r in (memories.router, facts.router, context.router, batch.router,
              ping.router, audit.router, admin.router, admin.public):
        app.include_router(r)
    # OAuth for the MCP (16 Oct 2026, services/oauth.py): the authorization
    # server at the host's root, and the consent the dashboard answers.
    from .api import oauth as oauth_api

    app.include_router(oauth_api.router)
    app.router.routes.extend(oauth_api.auth_routes())
    # The hosted service (KORELY_MODE=hosted) mounts its routes and registers
    # its hooks; on an install of one's own nothing, and every hook is back to
    # its default even in a process that built a hosted app before. The mode
    # is refused at start on a tree without the Cloud's package
    # (config.Settings), so here it always finds it.
    from .core import extensions
    from .services import webhooks

    extensions.reset()
    webhooks.reset_events()
    if get_settings().hosted:
        extensions.cloud().install(app)

    # The MCP surface, mounted alongside REST rather than in its own process.
    # Same store, same keys, same tenant isolation: an agent that speaks MCP and
    # a service that speaks HTTP are looking at one memory.
    try:
        from .mcp.server import mount_agent_mcp

        mount_agent_mcp(app)
    except Exception as e:
        logger.error(
            "[mcp] MCP surface failed to mount; REST is unaffected. Agents that "
            "connect over MCP will not reach this server: %s", log_safe.stack(e)
        )
        mcp_door.update(state="failing",
                        detail=f"the MCP surface did not mount ({log_safe.error(e)}): "
                               f"/mcp answers 404")


def create_app() -> FastAPI:
    s = get_settings()
    _quiet_access_log()

    # The engine's distance thresholds are the embedding model's
    # (memory_core.thresholds, 2.4.0). Before anything runs the engine: a model
    # nobody measured stops the start here, with what to set instead.
    from .core.llm import embedding_model_name, pick_distance_thresholds

    cut = pick_distance_thresholds()
    logger.info("[embeddings] %s, %s: distance thresholds %.2f (judge) and %.2f "
                "(relations)", s.embedding_provider, embedding_model_name(),
                cut.semantic, cut.shortlist)

    # What the MCP door did at start, for /health (audit of 17 Oct 2026): a
    # mount or a session manager that failed went to the log alone, REST
    # went on, /health said "ok" and the image's HEALTHCHECK stayed green
    # while every MCP client met a 404. `unknown` until the lifespan runs.
    mcp_door = {"state": "unknown", "detail": "the MCP session manager has not started yet"}

    app = FastAPI(
        lifespan=_lifespan(s, mcp_door),
        title="Korely Agent",
        description="Memory for AI agents that knows what was true when.",
        # The VERSION file, like /health and the MCP handshake. This said
        # "0.1.0" in every release: /docs and /openapi.json were the one place
        # the product reported a number, and it was the wrong one.
        version=__version__,
        # /docs is the route below, served with its own files.
        docs_url=None,
        # No /redoc either: FastAPI's page loads redoc@2 from jsDelivr, fonts
        # from Google and an icon from fastapi.tiangolo.com, the same third
        # parties /docs stopped calling (audit of 17 Oct 2026). /docs is the
        # one page of the API, served with its own files.
        redoc_url=None,
        openapi_url="/openapi.json",
        openapi_tags=API_SECTIONS,
    )

    _mount_routes(app, mcp_door)

    @app.get("/health", tags=["ops"])
    def health(request: Request):
        """Liveness, plus whether this thing can actually do its job.

        `extraction` reports what the provider last DID, in four states.
        `working`: the provider answered, `seconds_ago` says when; before
        this process has called it, the last extraction saved in the store,
        by whichever process made it. `unknown`: a key is set and no
        extraction has ever answered, or the database cannot be read.
        `not configured`: no key. `failing`: the last call was refused, or
        memories have waited more than five minutes with no extraction
        answering. Nothing is probed: a model call on every check would be
        billed every thirty seconds forever. Put `extraction.state` on your
        dashboard, not `status`.
        \f
        It said `unknown` "until something calls it" until the audit of
        18 Oct 2026, long after the store had been read
        for it (2026-10-06): the README has the four states.

        `extraction` used to report whether a key was SET. On 2026-09-11 the
        prepaid credit ran out and this endpoint answered `available` for hours
        while every write was stored and no fact was ever mined. A store that
        cannot learn and says it is fine is the failure mode this whole codebase
        keeps finding, and it was in the health check.
        """
        return _health(request, s, mcp_door)

    _answer_errors(app)
    # The 413 of _BodyCap in the document, on every operation with a body
    # (audit of 18 Oct 2026).
    from .api.errors import declare_the_body_ceiling

    declare_the_body_ceiling(app)

    from .core.llm import provider_configured

    if not provider_configured():
        # A warning, not an error: the README's own path pastes the key into
        # the dashboard after the first start, so every correct install begins
        # here, and two ERROR lines on a correct first boot sent a tester
        # looking for what they had broken.
        logger.warning(
            "[llm] No model key yet (extraction_provider=%s). Memories are stored and "
            "readable, and NO facts are mined until there is one: paste it in "
            "the dashboard (Settings) or set GOOGLE_API_KEY (or MISTRAL_API_KEY).",
            s.extraction_provider,
        )

    _serve_the_api_page(app)

    # Last, always. This one answers `/{anything}`, so anything registered
    # after it would never be reached.
    from .api import spa

    spa.mount(app, get_settings().dashboard_dir)

    # Before every other middleware and every route: a body past its ceiling
    # is refused before anything reads it, the key included.
    app.add_middleware(_BodyCap)
    # Outside it, so a NUL byte in the address is refused before any byte of
    # the body is read.
    app.add_middleware(_NulInTheAddress)
    # Added last, so it wraps every other middleware: the access line reads
    # the request's route through it (`loggable_path`).
    app.add_middleware(_RememberTheRequest)

    return app


app = create_app()
