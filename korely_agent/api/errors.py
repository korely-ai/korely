"""The error every route answers, declared once and built once.

Every error body this install sends carries three keys:

    {"detail": "API key missing required scope(s): memories:write",
     "code": "forbidden",
     "message": "API key missing required scope(s): memories:write"}

    {"detail": {"code": "stale_write", "message": "expected_updated_at does not match ..."},
     "code": "stale_write",
     "message": "expected_updated_at does not match ..."}

    {"detail": [{"type": "missing", "loc": ["body", "content"], "msg": "Field required"}],
     "code": "invalid_request",
     "message": "content: Field required"}

`detail` is what this install always sent, unchanged: a sentence, a
`{code, message}` pair where an endpoint names the reason for a program to
branch on, or the list of what failed validation (main.py's 422 handler, which
drops `input` so a secret sent in the wrong field does not come back).

`code` and `message` at the top level are the hosted product's shape
(`{code, message}` on every /v1 error). They were added here on
2026-09-28, alongside `detail` rather than instead of it: the published
clients parse either shape, and a self-hosted client written against
`detail` keeps working. `code` is stable and meant to be branched on; it is the
endpoint's own code when `detail` names one, and otherwise follows the status
(`_DEFAULT_CODES`), with the hosted product's names on /v1. `message` is for a
person: the sentence, the pair's message, or the first field that failed
validation.

Until 2026-09-28 /openapi.json described exactly one error, the 422, and
described it as FastAPI's validation list alone. A client generated from it
parsed `{"detail": "content must not be empty or whitespace"}` as malformed,
and knew nothing of 401, 403, 404, 409, 429, 500 or 503. /admin kept that
description until the same day.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Union

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ..services import idempotency, limits


class ErrorCode(BaseModel):
    """A refusal with a stable reason."""

    code: str = Field(
        description="Stable, machine-readable reason, e.g. `not_found`, `stale_write`, "
                    "`invalid_request`, `search_unavailable`. Branch on this, never on `message`.",
        examples=["not_found"],
    )
    message: str = Field(description="What went wrong, for a person to read. Wording may change.")


class ValidationIssue(BaseModel):
    """One field that failed validation. The value sent is never echoed back."""

    type: str = Field(description="What kind of failure, e.g. `missing`, `extra_forbidden`, "
                                  "`string_too_long`, `invalid_namespace_id`.")
    loc: list[Union[str, int]] = Field(
        description="Where: `body`, `query` or `path`, then the field, e.g. "
                    "[\"body\", \"memories\", 0, \"user_id\"].")
    msg: str = Field(description="What is wrong with it, for a person to read.")
    ctx: Optional[dict] = Field(default=None, description="The limit that was crossed, when there is one.")


class ApiError(BaseModel):
    """The body of every answer that is not a success, whatever the status."""

    detail: Union[str, ErrorCode, list[ValidationIssue]] = Field(
        description="As this install has always sent it: a sentence; or `{code, message}` where "
                    "the endpoint names the reason; or, on a 422 from validation, the list of "
                    "fields that failed.")
    code: str = Field(
        description="Stable, machine-readable reason, the same names as the hosted API: "
                    "`invalid_key`, `forbidden`, `not_found`, `stale_write`, `invalid_request`, "
                    "`identity_required` (a write with neither user_id nor agent_id), "
                    "`rate_limit_exceeded`, `search_unavailable`, `model_unavailable`, `service_paused`, "
                    "`internal_error`... When `detail` is `{code, message}` it is that code. "
                    "Branch on this, never on `message`.",
        examples=["not_found"],
    )
    message: str = Field(
        description="What went wrong, for a person to read: the sentence, the message of "
                    "`detail`, or the first field that failed validation. Wording may change.")
    current_fact_id: Optional[str] = Field(
        default=None,
        description="`fact_not_current`: the fact in force to correct instead, null when "
                    "nothing took the fact's place. Absent on every other refusal.")


# The machine fields a refusal carries next to `code` and `message`, read from
# its `detail` (7 Oct 2026, the hosted product's names and place).
_TOP_FIELDS = ("current_fact_id",)


class CodedHTTPException(HTTPException):
    """An HTTPException that names its `code` while keeping its `detail` a
    sentence: for a refusal whose status alone would name it wrongly (a
    wrong password is a 401, and it is not an invalid key)."""

    def __init__(self, status_code: int, detail: Any, *, code: str,
                 headers: Optional[dict] = None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


# The code a status carries when the endpoint names none. The hosted product's
# names where it has one. A /v1 409 names
# its own code where it has one (`stale_write`, `fact_not_current`); the others,
# the names door's clash and /admin's, are `conflict`.
_DEFAULT_CODES = {
    400: "bad_request",
    401: "invalid_key",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    408: "request_timeout",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_request",
    429: "rate_limit_exceeded",
    500: "internal_error",
    503: "unavailable",
}


def validation_message(errors: list, total: Optional[int] = None) -> str:
    """One sentence for a validation list: the first field that failed, and
    how many more (of `total`, when the list is the first of them only).
    Built from `loc` and `msg` only, never from the value sent (the list it
    comes from has `input` removed already)."""
    total = len(errors) if total is None else total
    if not errors:
        return "invalid request"
    first = errors[0]
    if first.get("type") == "json_invalid":
        # The byte offset lands in `loc` and says nothing to a person.
        text = "body: malformed JSON"
    else:
        path = [str(p) for p in first.get("loc", []) if p != "body"]
        where = ".".join(path) or "body"
        msg = str(first.get("msg") or "invalid")
        # "userId is not a field here. Did you mean user_id?" names the field
        # already; say where it is instead of saying it twice.
        if path and msg.startswith(path[-1] + " "):
            text = where + msg[len(path[-1]):]
        else:
            text = f"{where}: {msg}"
    if total > 1:
        text += f" (and {total - 1} more)"
    return text


def validation_errors_shown(errors: list) -> list:
    """What a 422 from validation lists in `detail`, on every route: the
    first limits.MAX_VALIDATION_ERRORS_SHOWN errors, each without the value
    sent (`input`); its `message` counts them all (`validation_message`'s
    `total`). The list was rendered whole, one object per error (audit of
    27 Oct 2026)."""
    return [{k: v for k, v in e.items() if k != "input"}
            for e in errors[:limits.MAX_VALIDATION_ERRORS_SHOWN]]


def error_body(status_code: int, detail: Any, *, code: Optional[str] = None) -> dict:
    """The body of an error answer: `detail` as given, `code` and `message`
    next to it (see the module docstring)."""
    if isinstance(detail, dict) and detail.get("code"):
        code = code or str(detail["code"])
        message = str(detail.get("message") or detail["code"])
    elif isinstance(detail, list):
        message = validation_message(detail)
    elif isinstance(detail, str) and detail:
        message = detail
    else:
        message = "Request failed"
    body = {"detail": detail,
            "code": code or _DEFAULT_CODES.get(status_code, "error"),
            "message": message}
    if isinstance(detail, dict):
        body.update({k: detail[k] for k in _TOP_FIELDS if k in detail})
    return body


async def send_refusal(send, e: HTTPException, *, close: bool = False) -> None:
    """A refusal sent by an ASGI layer that answers before any route can (the
    body's ceiling, main._BodyCap; the MCP's door, mcp/server): the status,
    the headers and the body a route's refusal has (`error_body`), so a
    client reads the same `code` whoever answered. `close`: the connection
    is closed after it, for a refusal sent while the body is still arriving.
    Written once since the audit of 20 Oct 2026 (four copies of it)."""
    import json

    body = json.dumps(error_body(e.status_code, e.detail, code=getattr(e, "code", None))).encode()
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode())]
    if close:
        headers.append((b"connection", b"close"))
    headers += [(k.lower().encode("latin-1"), str(v).encode("latin-1"))
                for k, v in (e.headers or {}).items()]
    await send({"type": "http.response.start", "status": e.status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def refused(db, key, endpoint: str, refusal: HTTPException, **row) -> HTTPException:
    """Keep a refused /v1 call in the trail and hand the refusal back, to be
    raised: `raise refused(db, key, "facts:forget", HTTPException(404, ...),
    target_id=...)`.

    The row is the call's own label with result `error` and the refusal's
    `code` in `meta` (the token a client branches on, never its words), the
    end user and the target when the route knows them. Every refusal raised
    before the route's one log_v1_request left no row at all, so an access
    request answered from the trail missed every refused attempt on a
    person's data; only memories:get and memories:delete wrote theirs
    (audit of 18 Oct 2026). A malformed request that names nothing
    yet (a 422 of validation) is not an attempt on anything and writes none.
    """
    from ..services.usage import log_v1_request

    code = error_body(refusal.status_code, refusal.detail, code=getattr(refusal, "code", None))["code"]
    log_v1_request(db, key.tenant_id, endpoint, api_key_id=key.id, result="error",
                   meta={**(row.pop("meta", None) or {}), "code": code}, **row)
    return refusal


# What each status means on /v1, and the `code` values it carries. A route
# that can only produce some of them, or produces them for a reason of its
# own, passes its own sentence (v1_errors).
_ERROR_DESCRIPTIONS = {
    401: "`invalid_key`: the `Authorization: Bearer` key is missing, unknown, revoked or "
         "expired.",
    403: "`forbidden`: the key lacks the scope this operation needs; `message` names it.",
    404: "`not_found`: nothing with that id is reachable with this key.",
    409: "`stale_write`: `expected_updated_at` does not match the current record.",
    422: "`invalid_request`: the body, a query parameter or a path parameter is malformed "
         f"(`detail` lists the first {limits.MAX_VALIDATION_ERRORS_SHOWN} fields that failed, "
         "`message` names the first and counts them all), or the "
         "endpoint refuses a value (an empty content, an unreadable timestamp, a NUL byte "
         "or a lone UTF-16 surrogate anywhere in the request, "
         f"a moment that cannot be stored, which is any but "
         f"{limits.MOMENTS_STORED}; `detail` is "
         "a sentence or `{code, message}`).",
    429: "`rate_limit_exceeded`: past the request ceiling, the per-minute one set on this "
         "install (`RATE_LIMIT_PER_MINUTE`) or, on the hosted service, the plan's minute, "
         "hour or day; `message` names the window and `Retry-After` the seconds until it "
         "resets.",
    500: "`internal_error`: the server failed on this request; `message` holds no detail of it.",
    503: "A dependency did not answer: the database while the key was checked "
         "(`unavailable`, see `Retry-After`), or the one `code` names. Nothing was "
         "written; retry.",
}


def describe_also(app, status_code: int,
                  sentence: Union[str, Callable[[Any], Optional[str]]]) -> None:
    """Complete, in `app`'s document, the description of `status_code` on
    every operation that declares it with `sentence`: for a refusal the
    hosted service adds (hosted/quotas.py), which an install of one's own
    never sends and its document does not name. The responses are declared
    once, when the routers are imported, so they cannot tell the two modes
    apart; the document is built per app (audit of 17 Oct 2026: a client
    generated from the Cloud's spec read every 429 as the minute's ceiling,
    and retried a month's quota a minute later).

    `sentence` is the text, or a function of the route (a FastAPI APIRoute)
    that answers the text for that route's operations, None for a route that
    cannot send the refusal. A text for every 429 told a client to expect
    the month's quota and the agent cap on the sign-in, the keys and the
    webhooks, which never send either (audit of 18 Oct 2026)."""
    build = app.openapi

    def openapi():
        done = app.openapi_schema is not None
        spec = build()
        if not done:
            paths = spec.get("paths", {})
            seen = set()
            # The routes the document shows: not those of a part served to
            # a list, whose twins of the core's it does show.
            for route in api_routes(app.routes, gated=False):
                text = sentence(route) if callable(sentence) else sentence
                if not text:
                    continue
                for method in sorted(route.methods or ()):
                    op = paths.get(route.path_format, {}).get(method.lower())
                    answer = op.get("responses", {}).get(str(status_code)) if isinstance(op, dict) else None
                    if answer is not None and id(answer) not in seen:
                        seen.add(id(answer))
                        answer["description"] = f"{answer.get('description', '').rstrip()} {text}".strip()
        return spec

    app.openapi = openapi


def every_route(routes, *, gated: bool = True):
    """Every route under `routes` (an APIRoute, a Route, a Mount), through
    the routers FastAPI >= 0.140 keeps as one node each (`original_router`)
    and, with `gated`, the routes of a part served to a list of accounts,
    one entry of the app's routes that the API's document leaves out
    (core/extensions.PartRoutes), as the tests walk them."""
    from ..core.extensions import PartRoutes

    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from every_route(inner.routes, gated=gated)
        elif isinstance(r, PartRoutes):
            if gated:
                yield from every_route(r.routes)
        else:
            yield r


def api_routes(routes, *, gated: bool = True):
    """Every APIRoute under `routes` (every_route)."""
    from fastapi.routing import APIRoute

    return (r for r in every_route(routes, gated=gated) if isinstance(r, APIRoute))


def declare_the_unknown_parameters(app) -> None:
    """Say in `app`'s document, on every operation whose route refuses a
    query parameter it does not take (api/_bodies.parameters_it_takes), that
    422 (api/_bodies.unknown_parameters_refused): added to the 422 the
    operation declares, or declared where it has none (GET /v1/ping, which
    takes no key). Built per app, as `describe_also` (review of 14 Nov 2026:
    a reader of the reference alone did not know the parameter is refused)."""
    from ._bodies import unknown_parameters_refused

    build = app.openapi

    def openapi():
        done = app.openapi_schema is not None
        spec = build()
        if not done:
            paths = spec.get("paths", {})
            seen = set()
            for route in api_routes(app.routes, gated=False):
                text = unknown_parameters_refused(route)
                if not text:
                    continue
                for method in sorted(route.methods or ()):
                    op = paths.get(route.path_format, {}).get(method.lower())
                    if not isinstance(op, dict):
                        continue
                    answer = op.setdefault("responses", {}).setdefault("422", {
                        "description": "`invalid_request`: a query parameter is malformed.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ApiError"}}}})
                    if id(answer) not in seen:
                        seen.add(id(answer))
                        answer["description"] = f"{answer['description'].rstrip()} {text}"
        return spec

    app.openapi = openapi


def declare_the_body_ceiling(app) -> None:
    """Add to `app`'s document the 413 `payload_too_large` of every operation
    that takes a body, with its route's ceiling (services/limits.body_cap,
    the number main._BodyCap refuses past). The refusal comes from a
    middleware, before any route or key is reached, so no route's
    `responses=` could say it, and no operation declared it although the
    0.1.40 notes told integrators to expect it (audit of 18 Oct 2026).
    Built per app, as `describe_also`."""
    from ..services.limits import (BODIES_PER_NETWORK, BODY_READ_SECONDS, COMMON_ITEMS,
                                   LARGE_BODY_READ_SECONDS,
                                   batch_json_chars_cap, batch_json_strings_cap,
                                   batch_json_text_cap, batch_json_values_cap, body_cap,
                                   upload_door)
    from ..core.threads import BUSY_RETRY_SECONDS

    build = app.openapi

    def openapi():
        done = app.openapi_schema is not None
        spec = build()
        if not done:
            for path, item in spec.get("paths", {}).items():
                for op in item.values():
                    if not isinstance(op, dict) or "requestBody" not in op:
                        continue
                    # The most the route takes; where a key that may write
                    # is allowed more (a batch's ceiling: limits.body_cap),
                    # both. Only there does the text speak of a key: a route
                    # that takes none (signup, POST /v1/agents/init) was told
                    # "with a key that may write" (A, 18 Oct 2026).
                    most, plain = body_cap(path, writer=True), body_cap(path)
                    door = upload_door(path)
                    # An administered upload is larger for whoever
                    # administers the account, not for a key that may write
                    # (main._writer_account).
                    who = ("signed in to the dashboard or with a key that has the admin scope"
                           if door is not None and door.administered
                           else "with a key that may write")
                    size = (f"{most:,} bytes, the most this route takes {who} "
                            f"({plain:,} without one)" if most != plain
                            else f"{most:,} bytes, the most this route takes")
                    dense = ""
                    if door is not None:
                        # A file is streamed to the route, never held whole
                        # nor weighed as JSON (limits.body_is_streamed); a
                        # JSON text there has the common ceiling.
                        dense = (" The larger ceiling is a file's, sent as multipart/form-data "
                                 "(the file and its form); a JSON body here has the common "
                                 f"ceiling, {plain:,} bytes, whoever sends it, and is counted "
                                 "as every JSON body is before it is decoded.")
                    elif most != plain:
                        # A write key's large body is read whole and counted
                        # before it is decoded (limits.body_weighed):
                        # under the byte ceiling, it can still be refused
                        # (audit of 20 Oct 2026).
                        dense = (f" Also a body under {most:,} bytes that holds more JSON "
                                 f"strings ({batch_json_strings_cap():,}), values "
                                 f"({batch_json_values_cap():,}) or characters "
                                 f"({batch_json_chars_cap():,}) than the most items can, or "
                                 f"whose text decodes to more than it weighs and more than "
                                 f"{batch_json_text_cap():,} bytes: read whole, refused before "
                                 "it is decoded; nothing was done.")
                    else:
                        # Every other JSON body is counted too, against what
                        # items at their largest fill of the common ceiling
                        # hold (limits.COMMON_ITEMS, audit of 20 Oct 2026).
                        dense = (f" Also a body that holds more JSON strings "
                                 f"({batch_json_strings_cap(COMMON_ITEMS):,}), values "
                                 f"({batch_json_values_cap(COMMON_ITEMS):,}) or characters "
                                 f"({batch_json_chars_cap(COMMON_ITEMS):,}) than lawful items of "
                                 f"its size can, whose text decodes to more than it weighs "
                                 f"and more than {batch_json_text_cap(COMMON_ITEMS):,} bytes, "
                                 f"or that would take more than {BODIES_PER_NETWORK:,} bytes "
                                 "once decoded: refused before it is decoded; nothing was done.")
                    op.setdefault("responses", {})["413"] = {
                        "description": f"`payload_too_large`: the body is larger than {size}. "
                                       "Refused before it is read; nothing was done." + dense,
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ApiError"}}},
                    }
                    # Every body has a deadline (main._BodyCap,
                    # BODY_READ_SECONDS), a large one its own once let in
                    # (LARGE_BODY_READ_SECONDS); audit of 27 Oct 2026.
                    late = (f" A body past {plain:,} bytes has {LARGE_BODY_READ_SECONDS:.0f} s "
                            "from being let in." if most != plain else "")
                    op["responses"]["408"] = {
                        "description": f"`request_timeout`: the body did not arrive whole "
                                       f"within {BODY_READ_SECONDS:.0f} s.{late} Nothing was "
                                       "done; send it again.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ApiError"}}},
                    }
                    op["responses"].setdefault("400", {
                        "description": "`bad_request`: the body is not well formed.",
                        "content": {"application/json": {
                            "schema": {"$ref": "#/components/schemas/ApiError"}}},
                    })["description"] += (" A JSON body not in UTF-8 (RFC 8259) is "
                                          "refused before it is decoded.")
                    full = ("`unavailable`: the server holds as many request bodies as it "
                            f"reads at once; retry after `Retry-After` ({BUSY_RETRY_SECONDS} s). "
                            "Nothing was done.")
                    served = op["responses"].get("503")
                    if served:
                        served["description"] += " Or " + full
                    else:
                        op["responses"]["503"] = {
                            "description": f"{full} {_deadline_sentence()}",
                            "content": {"application/json": {
                                "schema": {"$ref": "#/components/schemas/ApiError"}}},
                        }
        return spec

    app.openapi = openapi


# How long a client is told to wait when the service itself does not call the
# models now (core.llm.NotNow: the Cloud's daily emergency stop, the European
# month's ceiling, a provider's brake): the stop lifts at the next UTC day at
# the soonest, a brake when the operator lifts it, so an hour, not the seconds
# of a busy database (audit of 27 Oct 2026).
PAUSED_RETRY_SECONDS = 3600

#: The sentence of every route whose 503 can be this answer.
SERVICE_PAUSED = (
    "`service_paused`: the service itself does not call the models now (a spending stop or "
    "a provider brake), which a retry within the hour cannot change; `Retry-After` says "
    "how long to wait. Nothing was written. Not `model_unavailable`, which is a model that "
    "did not answer this request.")


def service_paused() -> HTTPException:
    """The 503 every door answers when the service's own gate kept a model
    call from going out (`core.llm.not_now_here`): one code, one
    `Retry-After`, on every door that calls a model."""
    return HTTPException(
        status_code=503,
        detail={"code": "service_paused",
                "message": "The service does not call the models now. Nothing was written; "
                           "retry later (see Retry-After)."},
        headers={"Retry-After": str(PAUSED_RETRY_SECONDS)})


#: The sentence of every fact write whose 503 can be this answer.
EMBEDDER_BUSY = (
    "`unavailable` with `Retry-After`: a relation new to the vocabulary needed a vector "
    "and this machine's embedder could not give one now, its places taken or its model "
    "still loading at the start (`GET /health` `ready` false). Nothing was written; "
    "retry. Not `model_unavailable`, which is a model that failed.")


def embedder_busy() -> HTTPException:
    """The 503 of a fact write whose vector this machine's embedder refused
    before computing it (core/local_embedder.Busy: the account's places
    taken, or `Warming`, the model loading at the process's start). Nothing
    failed: the write is the caller's to retry, with the wait of every busy
    resource (core/threads.BUSY_RETRY_SECONDS), as a search refused for the
    same reason is told. It answered `model_unavailable`, which sends
    whoever reads it looking for a broken model (code review finding, 15 Nov
    2026: every fact write in the first seconds after a start)."""
    from ..core.threads import BUSY_RETRY_SECONDS

    return HTTPException(
        status_code=503,
        detail={"code": "unavailable",
                "message": "This machine's embedder is busy or still loading its model. "
                           "Nothing was written; retry in a few seconds (see Retry-After)."},
        headers={"Retry-After": str(BUSY_RETRY_SECONDS)})


def not_a_model_that_failed() -> Optional[HTTPException]:
    """The 503 of a fact write a model gave no usable answer for, when the
    call never went out: kept back by the service's own gate
    (`core.llm.not_now_here`, `service_paused`), or refused by this
    machine's embedder (`core.llm.PAST_ITS_SHARE`, `embedder_busy`). Read
    from this thread's record, where the call was refused, as the gate's
    flag is. None when a model failed: `model_unavailable`, every door's
    own words."""
    from ..core import llm

    if llm.not_now_here():
        return service_paused()
    if llm.last_kind_here() == llm.PAST_ITS_SHARE:
        return embedder_busy()
    return None


def _deadline_sentence() -> str:
    """What every 503 can also be, on any route that reads or writes the
    database: one of a request's deadlines given up on (core/threads, audit
    of 20 Oct 2026)."""
    from ..core.threads import LOCK_WAIT_SECONDS, STATEMENT_SECONDS

    return (f"Also `unavailable` when the request waited more than {LOCK_WAIT_SECONDS} s for "
            "another change to the same records (an erasure under way, for example) or its "
            f"statement ran past {STATEMENT_SECONDS} s; see `Retry-After`.")


def _declared(texts: dict, statuses) -> dict:
    out = {}
    for s in statuses:
        text = texts[s]
        if s == 503:
            text = f"{text.rstrip()} {_deadline_sentence()}"
        out[s] = {"model": ApiError, "description": text}
    return out


def v1_errors(*statuses: int, descriptions: Optional[dict] = None) -> dict:
    """FastAPI `responses=` for the given statuses, all with the ApiError body.

    `descriptions` overrides the sentence for a status on one route, e.g. the
    503 of search (`search_unavailable`) versus the 503 of a fact write
    (`model_unavailable`). Every 503 also says the request's deadlines
    (`_deadline_sentence`), whatever the route's own sentence.
    """
    return _declared({**_ERROR_DESCRIPTIONS, **(descriptions or {})}, statuses)


def idempotent_door(made: int) -> dict:
    """FastAPI `responses=` of a door that honours `Idempotency-Key`
    (services/idempotency.py): its 400, 404 and 409, its 422 with
    `idempotency_key_reused` beside the malformed request, and the
    `Idempotent-Replayed` header on `made`, the status it answers a write
    with (201, 202), which a retry answers with too. Both were said only in
    the header parameter's text (code review finding, 15 Nov 2026)."""
    texts = {**idempotency.RESPONSES,
             422: f"{_ERROR_DESCRIPTIONS[422].rstrip()} {idempotency.RESPONSES[422]}"}
    return {**v1_errors(400, 404, 409, 422, descriptions=texts),
            made: {"headers": {idempotency.REPLAYED: idempotency.REPLAYED_HEADER}}}


# The errors any key-authenticated /v1 route can answer: the key (401), its
# scope (403), a malformed request (422), the rate limit (429), a failure of
# ours (500) and a database that did not answer while the key was checked
# (503, audit of 17 Oct 2026). Router-level; routes add 404 / 409 and their
# own 503.
V1_KEY_ROUTE_ERRORS = v1_errors(401, 403, 422, 429, 500, 503)


# The same envelope on /admin, the screen's API, with what each status means
# there: the credential is an operator session or a key with the `admin` scope.
_ADMIN_DESCRIPTIONS = {
    400: "`bad_request`: a well-formed value that something outside the request refused; "
         "`message` says why (a webhook URL that resolves where this server does not "
         "connect, a model key the provider refused).",
    401: "`invalid_key`: no operator session or API key, or one that is unknown, "
         "revoked or expired. On `POST /admin/session`, `invalid_credentials`.",
    403: ("`forbidden`: an API key without the `admin` scope. `previous_secret_has_no_admin`: "
          "the old secret of a rotated key, which works for the key's other calls until it "
          "ends (since 1.0.26)."),
    404: "`not_found`: nothing with that id in this account, whether the id is in the "
         "path or in the body (a `project_id`), malformed or another account's.",
    409: "The change is refused as the account stands; `code` names the case where the "
         "route has one: `project_name_taken`, `project_archived`, `cannot_archive_default`, "
         "`project_has_live_keys`, `cannot_delete_default`, `connected_app_key`, "
         "`key_not_rotatable`, "
         "`too_many_endpoints`, `too_many_keys`, `too_many_names`, `too_many_projects`, "
         "`endpoint_disabled`, `fact_not_current` (with "
         "`current_fact_id`), and otherwise `conflict` (a name already taken, a consent "
         "already answered); `message` says why.",
    422: "`invalid_request`: the body or a parameter is malformed "
         f"(`detail` lists the first {limits.MAX_VALIDATION_ERRORS_SHOWN} fields that failed, "
         "`message` names the first and counts them all), or is not a value the route takes, as on /v1: an unknown "
         "scope or event name, an empty list, end users a key cannot hold (`detail` is a "
         "sentence).",
    429: "`rate_limit_exceeded` (see `Retry-After`); on `POST /admin/session`, "
         "`too_many_attempts`.",
    500: "`internal_error`: the server failed on this request; `message` holds no detail of it.",
    503: "`unavailable`: a dependency is missing or did not answer, the database "
         "included while the credential was checked (see `Retry-After`); `message` says "
         "which.",
}

#: The 503 of an /admin route that calls a model: a dependency, or the
#: service's own pause (audit of 30 Oct 2026: /docs said only the first).
ADMIN_MODEL_503 = f"{_ADMIN_DESCRIPTIONS[503]} Or {SERVICE_PAUSED}"


def admin_errors(*statuses: int, descriptions: Optional[dict] = None) -> dict:
    """`responses=` for /admin, the ApiError body with the /admin sentences;
    `descriptions` as on `v1_errors`."""
    return _declared({**_ADMIN_DESCRIPTIONS, **(descriptions or {})}, statuses)


def outbound_errors(*statuses: int) -> dict:
    """`responses=` of a route that waits on something outside the process
    for its account (a webhook's host looked up, a test delivery): its 429
    also says the account's places for those waits (core/threads.
    OUTBOUND_PER_ACCOUNT), its 400 the lookup's deadline (LOOKUP_SECONDS).
    Audit of 20 Oct 2026: the webhooks' create, enable and test answered both, and
    /docs said only the request ceiling and a refused address."""
    from ..core.threads import LOOKUP_SECONDS, OUTBOUND_PER_ACCOUNT

    texts = {
        429: (f"{_ADMIN_DESCRIPTIONS[429]} Also `rate_limit_exceeded` while the account "
              f"already has {OUTBOUND_PER_ACCOUNT} webhook tests or address checks under way "
              "(see `Retry-After`); nothing was done."),
        400: (f"{_ADMIN_DESCRIPTIONS[400]} For a webhook, also a host that did not resolve "
              f"within {LOOKUP_SECONDS:.0f} s."),
    }
    return admin_errors(*statuses, descriptions={s: texts[s] for s in statuses if s in texts})


# Router-level on /admin: every route there is behind the credential (401, 403,
# and 503 when the database did not answer while it was checked) and the rate
# limit (429), can refuse a malformed request (422) and can fail (500). Declaring 422 here also replaces FastAPI's own, which described the
# validation list alone while half of these routes answer a sentence.
ADMIN_ROUTE_ERRORS = admin_errors(401, 403, 422, 429, 500, 503)
