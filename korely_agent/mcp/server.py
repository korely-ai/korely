"""MCP over the same store as /v1, with the same keys.

A coding assistant (Claude Code, Cursor, Codex, ...) connects a Streamable HTTP
MCP client to `https://your-domain/mcp` with
`Authorization: Bearer kor_self_...` and gets six memory tools over this
install's store. On the Cloud (KORELY_MODE=hosted, the same code) the address
is the service's /agent/mcp (`published_path`) and its keys begin with the
prefix it mints (services/keys.minted_prefix). No package to install, and
OAuth only for a client that asks for it (api/oauth.py): an API key minted
here is the bearer token, and it sees what it would see over REST, its tenant
and its project and nothing else, and may do what its scopes allow there and
nothing more (TOOL_SCOPES below).

- korely_get_context  (the assembled recall block)
- korely_add
- korely_search
- korely_get_facts
- korely_correct_fact  (correct or close one fact: PATCH /v1/facts/{id}, POST /v1/facts/{id}/forget)
- korely_forget_memory (forget or erase one memory: DELETE /v1/memories/{id})

Mounted by `mount_agent_mcp` in main.py at `/mcp` (Streamable HTTP); `/mcp`
without the slash is the same address (`AcceptTheAddressWeDocument`).
"""
from __future__ import annotations

import contextvars
import logging
from typing import Annotated, Any, Literal, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import AnyHttpUrl, Field

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from ..core.auth import (a_turn_for_its_question, credential_lookup, give_back_the_read,
                         key_end_users, key_project, missing_scopes, scope_refusal, user_refusal)
from ..core.threads import (CALLS_PER_ACCOUNT, TOOL_BODIES_AT_ONCE, Gates,
                            admission_off_the_loop, busy_refusal, deadline_passed, tool_body)
from ..db import get_sessionmaker
from ..services import keys as api_key_service
from ..services import limits
from ..services import read_marks
from ..services.facts import changes as notices
from ..services import user_scope
from ..services import facts as agent_facts
from ..services.facts.reads import FAMILY_FILTER
from ..services.facts import search as fact_search
from ..services import memory_write
from ..services.sentinels import EVERY_END_USER
from ..services.context import (BYTES_PER_TOKEN, DEFAULT_TOKEN_BUDGET, MAX_TOKEN_BUDGET,
                                MIN_TOKEN_BUDGET, assemble_context)
from ..services.ids import mem_public_id
# One rule for a text printed one item a line, the MCP's answers and the
# context block alike (memory_core 6.8.0; written here in 1.0.13).
from ..services.memory_core.presentation import in_its_line
from ..services.usage import log_v1_request
from ..services.memory_engine import MAX_PAGE, AgentMemoryEngine, MemorySearchUnavailable

logger = logging.getLogger(__name__)

# Set by the token verifier on each authenticated request; read by the tools.
_agent_user_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "agent_mcp_user_id", default=None
)
# The project of the key on this request. Same shape as the tenant above,
# because the tool functions run far from the verifier and a wall that
# depended on each tool remembering to look it up would be a wall with a hole
# per tool.
_agent_project: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "korely_agent_project", default=None,
)

# The scopes of the key on this request, for `_require_scope_for`. Same shape
# and same reason as the tenant and the project above.
_agent_scopes: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar(
    "korely_agent_scopes", default=None,
)

# The key itself, for the audit trail: REST records which key made each call,
# and until 2026-09-28 a call over MCP recorded none, so "which key did this"
# had no answer for anything an agent did through this door.
_agent_key_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "korely_agent_key_id", default=None,
)

# The end users of a confined key (services/user_scope.py, 16 Oct 2026), or
# None for a key of the whole project. Same shape and same reason as the
# project above: a wall each tool had to remember would have a hole per tool.
_agent_end_users: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar(
    "korely_agent_end_users", default=None,
)

# The scope each tool needs, written here and nowhere else: the scope its REST
# twin requires (`require_scopes` on that route, core/auth.py). Every
# tools/call goes through `_ScopedFastMCP.call_tool`, which checks it with the
# same rule and the same words REST uses.
#
# There was no such check. The transport asked for `memories:read` and nothing
# more, so a key minted read-only could not write over REST (403) and could
# over MCP, with korely_add. And a key with no scopes at all was read here as
# holding read and write, where REST reads it as holding nothing.
#
# A tool that is registered and missing from this table is refused to every
# key: a fifth tool added without its line fails closed, and
# tests/test_mcp_scopes.py names it.
TOOL_SCOPES = {
    "korely_get_context": "memories:read",   # GET  /v1/context
    "korely_add": "memories:write",          # POST /v1/memories
    "korely_search": "memories:read",        # POST /v1/memories/search
    "korely_get_facts": "memories:read",     # GET  /v1/facts
    "korely_correct_fact": "memories:write",   # PATCH /v1/facts/{id}, POST /v1/facts/{id}/forget
    "korely_forget_memory": "memories:write",  # DELETE /v1/memories/{id}
}


class KorelyAgentKeyVerifier(TokenVerifier):
    """Treats the Bearer token as an API key of this install. Validates it against the
    agent_api_keys table and stashes the owning tenant_id for the tools.

    The key's hash decides, through the same lookup as REST and /admin
    (services/keys.verify_api_key): on the Cloud every key is `kor_live_`;
    on an install of one's own a `kor_live_` key, even one it minted before
    0.1.3, is refused before any lookup since 0.2.0, at this door as at the
    others."""

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if not token or not token.strip():
            return None
        # On the admission pool, never behind a tool's body (core/threads.py).
        found = await admission_off_the_loop(_look_up_key, token.strip())
        if found is None:
            return None
        tenant_id, project_id, scopes, key_id, end_users = found
        # Set here, on the request's own context, and not inside the thread
        # above: a ContextVar set in a worker thread stays in that thread.
        # The tools read them again from this request's token at every call
        # (`_bind_the_caller`): a session's tasks keep the context of the
        # request that opened it.
        _bind(tenant_id, project_id, scopes, key_id, end_users)
        # The key, not the tenant, is the client (2026-10-06). The session
        # manager lets a session be used only by the principal that opened it,
        # and compares these fields: with the tenant here, any key of the
        # account could use a session another key had opened, read-only keys
        # included (the same flaw was found in the hosted product).
        # The tenant is the subject: with the key, the one identity the SDK
        # copies into a session's owner (`authorization_context`), which is
        # what `_BoundedSessions` counts a tenant's open sessions by (audit
        # of 18 Oct 2026). The key stays the client, so a session is
        # still bound to the key that opened it.
        return AccessToken(
            token=token, client_id=key_id, subject=tenant_id, scopes=scopes, expires_at=None,
            claims={"tenant": tenant_id, "project": str(project_id) if project_id else None,
                    "end_users": list(end_users) if end_users else None},
        )


def _bind(tenant_id: str, project_id, scopes, key_id: str, end_users=None) -> None:
    _agent_user_id.set(tenant_id)
    _agent_project.set(str(project_id) if project_id else None)
    _agent_scopes.set(tuple(scopes))
    _agent_key_id.set(key_id)
    _agent_end_users.set(tuple(end_users) if end_users else None)


def _bind_the_caller(server: "FastMCP") -> None:
    """The identity of THIS call, from the token its own HTTP request was
    verified with (2026-10-06). The tools ran with the context variables of
    the request that opened the session, because the session's tasks are made
    in that request's context: a key moved to another project went on reading
    the old one for as long as its session lasted, and a second key could
    act as the first. A call with no HTTP request behind it (a test calling
    the tool table, a stdio transport) keeps what verification set."""
    try:
        request = server.get_context().request_context.request
    except (AttributeError, LookupError, ValueError):
        return
    user = getattr(request, "user", None) if request is not None else None
    token = getattr(user, "access_token", None)
    claims = getattr(token, "claims", None) or {}
    if token is None or "tenant" not in claims:
        return
    _bind(claims["tenant"], claims.get("project"), token.scopes or [], token.client_id,
          claims.get("end_users"))


def _look_up_key(token: str):
    """(tenant, project, scopes, key id, end users) for a live key, or None.
    Synchronous, and so run through `admission_off_the_loop`.

    A database that does not answer raises REST's 503
    (core/auth.key_store_unavailable), answered by `_RefusalsAnswered`: until
    17 Oct 2026 every exception here was None, and the SDK answered 401
    `invalid_token` for a key that was fine, through a Postgres blip."""
    db = get_sessionmaker()()
    try:
        return credential_lookup(_the_key, db, token)
    finally:
        db.close()


def _the_key(db, token: str):
    row = api_key_service.verify_api_key(db, token)
    if row is None:
        return None
    # Read what we need WHILE the session is open — the row detaches on close.
    # No scopes means none, as over REST: this used to fill in read and
    # write for a key that had been given nothing.
    return (str(row.tenant_id), key_project(db, row), list(row.scopes or []),
            str(row.id), key_end_users(row))


def _end_users() -> Optional[list]:
    """The end users of this call's key, or None: the whole project."""
    users = _agent_end_users.get()
    return list(users) if users else None


def _within(rule, *args, db, endpoint: str):
    """`rule(this key's end users, *args)` (user_scope.for_read / for_write),
    with REST's refusal (core/auth.user_refusal) as the tool's error, kept
    in the trail as the tool's call `endpoint`, `error` with its code, as
    REST keeps its twin (audit of 19 Oct 2026)."""
    try:
        return rule(_end_users(), *args)
    except user_scope.OutOfScope as e:
        _audit(db, _uid(), endpoint, end_user_id=e.user_id, result="error",
               meta={"code": "forbidden"})
        raise ToolError(f"error: forbidden, {user_refusal(e.user_id).detail}") from None


# Off the event loop, every tool's synchronous work (core/threads.py).
_off_the_loop = tool_body

#: The tool calls one account runs at once; past it a call waits its turn on
#: the event loop, holding no thread (`_running`). The audit of 19 Oct 2026:
#: nothing bounded them, and a tool's body holds a thread of the loop's
#: default pool, min(32, CPUs + 4) threads, while a read waits there for its
#: turn at the local embedder; one key's burst of searches took every thread,
#: and the embedder's fairness among accounts (local_embedder._Turns) reached
#: only callers that already held one. The number is the one rule both doors
#: keep (core/threads.CALLS_PER_ACCOUNT, which says why four); since 31 Oct
#: 2026 REST's reads wait for theirs on the loop too, at the embedder's
#: places (`_a_turn_to_ask`). Per account, not per key, as MAX_SESSIONS_PER_TENANT: an
#: account mints keys without a count.
#:
#: A call past it waits rather than being refused, so the MCP answers nothing
#: new: a burst is served four at a time, in the order it came
#: (core/threads.Gates wakes its waiters first come, first served), and never
#: holds more than the embedder lets one account have. The refusal of an
#: account that calls too much stays the per-minute ceiling
#: (`_count_the_call`), REST's `rate_limit_exceeded`, counted before the wait.
MAX_RUNNING_CALLS_PER_TENANT = CALLS_PER_ACCOUNT

# Each account's places while it has a call running or waiting
# (core/threads.Gates): the table holds the accounts calling now, never every
# account that ever called. A place is held until the call's body has ended,
# a cancelled call's too (core/threads.off_the_loop): the body still holds
# its thread (audit of 20 Oct 2026: the place was given back before the
# thread had ended).
#
# And every account's together at most TOOL_BODIES_AT_ONCE, the threads of
# the bodies' own pool, a freed place going to the account with the fewest
# running (audit of 27 Oct 2026: with no total, a few accounts at their bound filled the loop's
# default pool and another account waited behind their whole backlog).
_running_calls = Gates(MAX_RUNNING_CALLS_PER_TENANT, total=TOOL_BODIES_AT_ONCE)


def _running(tenant: str):
    """One of this account's MAX_RUNNING_CALLS_PER_TENANT places, waited for."""
    return _running_calls.place(tenant)


def _a_turn_to_ask():
    """The account's turn at the embedder for a tool that asks it for a
    question's vector (korely_search, korely_get_context), waited for on the
    loop once the call holds its own place (`_running`) and its checks have
    passed, and held until the vector has been computed
    (core/auth.a_turn_for_its_question): the one count REST's reads draw on
    too. The finding of 31 Oct 2026 (scripts/concurrent_agents.sh): this
    door's four calls and REST's four added up at the embedder, and the
    MCP's searches were refused as well. After the door's place, never
    before it (the review of c203d330): a call waiting for the door, behind
    every account's bodies, held its account's places at the embedder, and
    the account's REST searches answered 503 with the embedder idle. A call
    that waits here holds one of the door's places, which only its own
    account's vectors, a turn each, keep it from; the door bounds how many
    of those an account has (MAX_RUNNING_CALLS_PER_TENANT). A tool that may
    compute a document's vector (korely_add, korely_correct_fact) takes a
    place at once or leaves the vector to the worker, as REST's write."""
    return a_turn_for_its_question(_agent_user_id.get())


def _uid() -> UUID:
    uid = _agent_user_id.get()
    if not uid:
        raise ToolError("error: invalid_key, no API key was verified for this call")
    return UUID(uid)


def _project():
    """The project this MCP request is confined to, resolved at verification."""
    p = _agent_project.get()
    return UUID(p) if p else None


def _audit(db, uid: UUID, endpoint: str, **kw) -> None:
    """The audit row of one tool call, as REST writes it for its twin: the key
    that made the call (and so its project), actor `mcp`. `project_id` is the
    one resolved at verification, used when no key id is on the request (a
    tool called in-process, as the tests do)."""
    k = _agent_key_id.get()
    log_v1_request(db, uid, endpoint, actor="mcp",
                   api_key_id=UUID(k) if k else None, project_id=_project(), **kw)


def _client_of(server) -> Optional[str]:
    """The name this session's client gave in `initialize` (`clientInfo.name`,
    MCP specification 2025-06-18, Lifecycle): Claude Code, Cursor, Codex name
    themselves there. None when there is no session (a tool called in-process)."""
    from ..services.clients import client_name

    try:
        params = server.get_context().request_context.session.client_params
    except (AttributeError, LookupError, ValueError):
        return None
    info = getattr(params, "clientInfo", None) if params is not None else None
    return client_name(getattr(info, "name", None))


def _require_scope_for(tool_name: str) -> None:
    """Refuse the call unless the key on this request holds the tool's scope.

    The rule is core/auth.missing_scopes, the one REST reads, and the refusal
    carries the words of REST's 403. No key verified on this request means no
    scopes, so an unauthenticated call is refused here too.
    """
    need = TOOL_SCOPES.get(tool_name)
    if need is None:
        raise ToolError(f"error: forbidden, {tool_name} declares no scope, so no key may "
                        f"call it (mcp/server.py, TOOL_SCOPES)")
    missing = missing_scopes(_agent_scopes.get(), need)
    if missing:
        raise ToolError(f"error: forbidden, {scope_refusal(missing)}")


def _invalid(problem: str) -> ToolError:
    """The 422 of REST as this door's line: the code an agent reads, then the
    sentence (CONTRIBUTING.md: a refusal is a token or a field)."""
    return ToolError(f"error: invalid_request, {problem}")


def _a_model(annotation) -> bool:
    import typing

    from pydantic import BaseModel

    return any(isinstance(a, type) and issubclass(a, BaseModel)
               for a in (annotation, *typing.get_args(annotation)))


def _opening_read(params: dict) -> tuple:
    """(the sentence refusing an `initialize`'s params, or None; and the
    params the session is given in their place.) The SDK's session keeps an
    initialize's params for its life (30 idle minutes), and its models take
    any member (extra='allow'): an `initialize` of 1 MiB with a member of
    `[{},{},...]` kept some 49 MiB, and a free key's handful of sessions
    held more than the API's memory has to spare (the audit before 1.0.0:
    a dense message was never weighed). So the session keeps only what
    this server reads of them: the protocol's version, the client's name
    (`_client_of`) and version, and which capabilities it named. Every other
    member is dropped, not refused: a later revision of the protocol or a
    client's own extension (`extensions`) adds members, and what is kept is
    bounded without refusing them. What the protocol declares free, the
    capabilities' `experimental`, the client's icons and title, `_meta`, is
    dropped too. The three strings kept are bounded as an id is
    (limits.name_problem), a 422 past it: they are kept whole."""
    from mcp.types import ClientCapabilities

    info, named = params.get("clientInfo"), params.get("capabilities")
    if not isinstance(info, dict) or not isinstance(named, dict):
        return None, params  # the SDK's own model refuses it
    problem = next((p for field, value in (
        ("protocolVersion", params.get("protocolVersion")),
        ("clientInfo.name", info.get("name")), ("clientInfo.version", info.get("version")))
        if isinstance(value, str)
        and (p := limits.name_problem(field, value, limits.MAX_ID_CHARS))), None)
    capabilities = ClientCapabilities.model_fields
    return problem, {
        "protocolVersion": params.get("protocolVersion"),
        # A capability is kept by its name only, empty: what it holds is
        # lost, `roots.listChanged`, `sampling.tools`, `elicitation.url`
        # among it, which this server reads nowhere (it asks a client for
        # none of them). Code that comes to use a capability must keep what
        # it reads of it here, or the SDK's checks
        # (ServerSession.check_client_capability) will say the client lacks it.
        "capabilities": {name: {} for name, value in named.items() if name in capabilities
                         and _a_model(capabilities[name].annotation) and isinstance(value, dict)},
        "clientInfo": {k: info[k] for k in ("name", "version") if k in info}}


def _json_to_the_sdk(scope) -> bool:
    """Whether the SDK's transport decodes the request's body as JSON: its
    first Content-Type with any comma-separated part before a `;` exactly
    `application/json` (mcp 1.30, StreamableHTTPServerTransport.
    _check_content_type, which answers anything else 415 unread)."""
    kind = next((v for n, v in scope.get("headers") or () if n == b"content-type"), b"")
    parts = kind.decode("latin-1").split(";")[0].split(",")
    return any(part.strip() == "application/json" for part in parts)


async def _messages_read(scope, receive):
    """The `receive` the SDK reads a POST to the MCP from: the message as
    sent, but an `initialize`'s params read by `_opening_read` first, raised
    as a 422 (`_SessionRefused`) or given to the SDK as the session keeps
    them. Only a body the SDK decodes (`_json_to_the_sdk`) is decoded here.
    It was counted and charged what decoding it costs by main._BodyCap, as
    every door's body is; it is decoded here once and dropped before the
    SDK decodes it again, so the two never coexist."""
    import json

    if scope.get("method") != "POST" or not _json_to_the_sdk(scope):
        return receive
    pieces, last = [], None
    while True:
        last = await receive()
        if last["type"] != "http.request":
            break
        pieces.append(last.get("body") or b"")
        if not last.get("more_body"):
            last = None
            break
    body = b"".join(pieces)
    del pieces
    try:
        message = json.loads(body)
    except UnicodeDecodeError:
        # Not UTF-8 (a gzip body, say): the SDK's decoder catches only a
        # JSON error and answered 500 (-32603); a 400, as REST answers it.
        raise _SessionRefused(HTTPException(status_code=400, detail=limits.NOT_UTF8)) from None
    except ValueError:
        message = None  # not JSON: the SDK's 400
    if isinstance(message, dict) and message.get("method") == "initialize" \
            and isinstance(message.get("params"), dict):
        problem, kept = _opening_read(message["params"])
        if problem:
            raise _SessionRefused(HTTPException(status_code=422, detail=problem))
        if kept is not message["params"]:
            body = json.dumps({**message, "params": kept}, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8", "surrogatepass")
    del message
    given = False

    async def replay():
        nonlocal given
        if not given:
            given = True
            return {"type": "http.request", "body": body, "more_body": False}
        return last if last is not None else await receive()
    return replay


def _arguments_checked(tool, arguments) -> None:
    """The arguments of a call read against the tool's own signature before
    anything runs, the way REST's body and query are: a refusal is
    `error: invalid_request, <REST's sentence for it>` (api/errors.
    validation_message). The SDK checks them again when it calls the tool;
    left to it, the refusal was its own "1 validation error for ..." text,
    with no code (audit of 17 Oct 2026)."""
    from pydantic import ValidationError

    from ..api.errors import validation_message

    # An argument the tool does not declare is refused, as a body's unknown
    # field is (api/_bodies.Body): the arguments' model ignored it and the
    # rule below walked it whole, on the event loop, whatever its size up to
    # the transport's 4 MiB (audit of 27 Oct 2026). Every declared argument is a scalar, so what is
    # walked is a handful of values.
    fields = tool.fn_metadata.arg_model.model_fields
    accepted = set(fields) | {f.alias for f in fields.values() if f.alias}
    unknown = next((name for name in (arguments or {}) if name not in accepted), None)
    if unknown is not None:
        raise _invalid(f"{unknown} is not an argument of {tool.name}; it takes: "
                       + ", ".join(sorted(fields)))
    try:
        tool.fn_metadata.arg_model.model_validate(arguments or {})
    except ValidationError as e:
        raise _invalid(validation_message(e.errors())) from None
    # A NUL byte anywhere in the call, by the rule REST's bodies and
    # addresses ask (limits.nul_problem): each tool asked it of the fields it
    # remembered to (audit of 20 Oct 2026).
    problem = limits.nul_problem("", arguments or {})
    if problem:
        raise _invalid(problem)


def _count_the_call() -> None:
    """The ceiling REST has (RATE_LIMIT_PER_MINUTE, per tenant), counted in
    the same counter: a tool call is a request. The MCP door had none until
    2026-10-06: with a limit of 2 a minute REST answered 429 to the third
    call and MCP took ten of ten (the audit of 6 October, row 4), and the
    agents most likely to loop are the coding ones, which speak MCP."""
    from ..core.auth import rate_limited
    from ..services.rate_limit import RateLimited, check_and_increment

    db = get_sessionmaker()()
    try:
        check_and_increment(db, _uid())
    except RateLimited as e:
        raise refusal(rate_limited(e)) from None
    finally:
        db.close()


def refusal(e: HTTPException) -> ToolError:
    """A refusal of the core (a hook of core/extensions.py, the rate limit of
    core/auth.rate_limited), as the tool error an agent reads: the code and
    the sentence REST's body carries for it (api/errors.error_body)."""
    from ..api.errors import error_body

    b = error_body(e.status_code, e.detail, code=getattr(e, "code", None))
    return ToolError(f"error: {b['code']}, {b['message']}")


def _count_the_read(db) -> None:
    """A read tool is a read, as GET /v1/context and POST /v1/memories/search
    are: the hosted service counts it against the month (`before_query`); on
    an install of one's own the hook does nothing.

    Called by each read tool once its arguments are checked and the key's end
    users placed, right before it reads, as REST's routes call
    core/auth.count_the_read. It ran in `call_tool`, before any of that: on
    the Cloud a malformed date or a user_id outside a confined key's list
    took a query of the month (audit of 17 Oct 2026).

    In the tool body's session, `db`, as REST counts in the request's: a
    session of its own here was a second connection of the main pool asked
    for while the body held one (audit of 27 Oct 2026)."""
    from ..core.extensions import hooks

    try:
        hooks.before_query(db, _uid())
    except HTTPException as e:
        raise refusal(e) from None


class _ScopedFastMCP(FastMCP):
    """FastMCP with the scope check and the rate limit in front of every
    tool call.

    `call_tool` is what the server registers for tools/call, so this is the
    one door every call comes through, whatever the tool.
    """

    async def call_tool(self, name, arguments):
        _bind_the_caller(self)
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            _require_scope_for(name)
            # Whether a ceiling applies is the limiter's to say (the hosted
            # plan's windows through its hook, or RATE_LIMIT_PER_MINUTE), as
            # it is over REST: this door decided it again from the settings,
            # and a hook registered outside KORELY_MODE=hosted would have
            # been skipped here (audit of 17 Oct 2026).
            # On the admission pool, never behind a tool's body
            # (core/threads.py).
            await admission_off_the_loop(_count_the_call)
            # The month's reads are counted by each read tool, after its
            # checks (`_count_the_read`), never here.
            _arguments_checked(tool, arguments)
            # Counted and checked, the call waits for one of its account's
            # places (MAX_RUNNING_CALLS_PER_TENANT); a refused one never waits.
            async with _running(_agent_user_id.get()):
                try:
                    return await super().call_tool(name, arguments)
                except ToolError as e:
                    # A deadline of the database (core/threads.with_deadlines)
                    # passed in the tool's body: REST's 503, retry.
                    if e.__cause__ is not None and deadline_passed(e.__cause__):
                        raise refusal(busy_refusal()) from None
                    raise
        # An unknown name goes on to the library, which says it is unknown.
        return await super().call_tool(name, arguments)


def _moment_or_error(name: str, value: Optional[str]):
    """(the moment, the string kept for the audit) of a date a tool was
    given, by REST's own parser and sentence (api/facts.parse_as_of): no
    offset is UTC, blank is absent, anything else REST's 422 as this door's
    line. It had its own sentence, and no code (audit of 17 Oct 2026)."""
    from ..api.facts import parse_as_of

    if not value or not value.strip():
        return None, None
    try:
        return parse_as_of(value, name), value
    except HTTPException as e:
        raise refusal(e) from None


def _fact_line(f: Any) -> str:
    """One fact as the agent reads it: memory_core.presentation.mcp_line,
    the same line on the hosted product."""
    from ..services.memory_core.presentation import mcp_line

    return mcp_line(f)


_AGENT_MCP_INSTRUCTIONS = (
    "Korely agent memory over MCP. Persistent, typed, bi-temporal, shared with "
    "the SDK/CLI/REST.\n"
    "1. korely_get_context(query, user_id?) BEFORE answering: recall current facts.\n"
    "2. korely_add(content, user_id?) AFTER a durable decision/preference is settled. "
    "Agents sharing one memory each pass their agent_id.\n"
    "3. korely_get_facts / korely_search to inspect. Scope per end user with user_id.\n"
    "4. korely_correct_fact / korely_forget_memory when a stored fact or memory is wrong: "
    "use the ids korely_get_facts and korely_search return."
)

# The two spellings of the door (`published_path`), written once: the
# middleware below and /health, which tells the dashboard's snippets, read
# them from here (audit of 17 Oct 2026).
MOUNT_PATH = "/mcp"
HOSTED_PATH = "/agent/mcp"

_agent_mcp_singleton: Optional[FastMCP] = None
_agent_streamable_app_singleton = None


def published_path() -> str:
    """The address this MCP server is published at. Both are served
    (`AcceptTheAddressWeDocument`): /mcp, and /agent/mcp, the hosted
    product's, which its clients and SDKs already use (8 Oct 2026, the one
    codebase). The hosted service publishes /agent/mcp because its domain
    keeps /mcp for another server."""
    from ..config import get_settings

    return HOSTED_PATH if get_settings().hosted else MOUNT_PATH


def _auth_settings() -> AuthSettings:
    """The OAuth settings the server advertises: its authorization server and
    its own address (`published_path`)."""
    # api/oauth.py imports this module at its top, so its issuer is read here.
    from ..api.oauth import issuer

    base = issuer()
    # These URLs are advertised to clients, so they have to be the ones a
    # client can actually reach: /mcp on an install of one's own, /agent/mcp
    # on the hosted service, whose domain serves a second, unrelated MCP
    # server at /mcp (`published_path`).
    path = published_path()
    return AuthSettings(
        # The authorization server is this install, at the host's root
        # (api/oauth.py, 16 Oct 2026): the protected resource metadata names
        # it, and ChatGPT and Claude read it there. It named the MCP's own
        # address before, where no authorization server answered.
        issuer_url=AnyHttpUrl(base),
        resource_server_url=AnyHttpUrl(f"{base}{path}"),
        # No scope at the door, and on purpose: which scope is needed depends
        # on the tool, and TOOL_SCOPES checks it per call. `memories:read` here
        # was the only check there was, which let a read-only key write, and
        # refused a write-only key the korely_add its REST twin allows.
        required_scopes=None,
        # False, on purpose, and said out loud: left unset the library warns,
        # and 3.0 turns it on by default. The check compares the token's RFC
        # 8707 resource indicator (`AccessToken.resource`) with the URL above,
        # which protects a server that accepts tokens from an OAuth
        # authorization server shared with other resources. There is none
        # here: the bearer is a key this server minted (kor_self_, or
        # kor_live_ on the Cloud; an OAuth grant mints one too), and
        # `KorelyAgentKeyVerifier` accepts it only if its hash is in THIS
        # installation's key table, unrevoked and unexpired. A key issued for
        # any other server is unknown here and already refused. That lookup is
        # the audience check the library's own docs ask for when this is False
        # ("set it to False when your token verifier checks the token's
        # audience itself", mcp.server.auth.settings). Turning it on would only
        # mean stamping this URL on every key we accept, a check that can
        # never fail, and forgetting that stamp would lock every client out.
        validate_token_resource=False,
    )


def _new_server() -> "_ScopedFastMCP":
    """The server, before its tools: the door's checks (`_ScopedFastMCP`),
    the key verifier and the transport's paths and limits."""
    return _ScopedFastMCP(
        name="korely-agent",
        instructions=_AGENT_MCP_INSTRUCTIONS,
        token_verifier=KorelyAgentKeyVerifier(),
        auth=_auth_settings(),
        debug=False,
        log_level="INFO",
        sse_path="/sse",
        message_path="/messages/",
        # The generated app serves the transport at this path INSIDE itself. It
        # defaults to "/mcp", and mounting that app at /mcp would put the
        # endpoint at /mcp/mcp, which is a 404 for every client configured the
        # obvious way. "/" makes the mount point the address.
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
        max_sessions=MAX_SESSIONS,
        session_idle_timeout=SESSION_IDLE_SECONDS,
    )


def _register_get_context(mcp: FastMCP) -> None:
    """Register `korely_get_context` on `mcp`."""

    @mcp.tool()
    async def korely_get_context(query: str, user_id: Optional[str] = None,
                                 agent_id: Optional[str] = None,
                                 token_budget: int = DEFAULT_TOKEN_BUDGET,
                                 known_as_of: Optional[str] = None,
                                 exclude_sessions: bool = False,
                                 reader: Annotated[Optional[str],
                                                   Field(description=read_marks.READER)] = None,
                                 changes: Annotated[Optional[Literal["all"]],
                                                    Field(description=notices.KINDS)] = None
                                 ) -> str:
        """Recall what Korely knows for one end user as a prompt-ready block (the
        primary recall path). Call before answering. user_id scopes to one end user;
        agent_id to one agent's namespace (what korely_add wrote under it).
        token_budget: 50 to 8000 tokens. known_as_of (ISO date) rebuilds the
        block as Korely knew it then. exclude_sessions leaves out the work
        sessions a coding assistant saved, and the facts only they state.
        reader (your agent's name): the block then opens with what changed
        since you last read, and keeps which facts you were handed; it
        filters nothing. changes=all: every kind of change."""
        # The budget by REST's bounds, refused outside them as GET /v1/context
        # refuses it: 0 was read as the default 800, 9,000 cut to 8,000
        # (audit of 17 Oct 2026).
        # agent_id as GET /v1/context takes it: korely_add wrote under one,
        # and no MCP read could narrow on it (audit of 19 Oct 2026).
        problem = (limits.query_problem(query) or limits.id_problem("user_id", user_id)
                   or limits.id_problem("agent_id", agent_id)
                   or read_marks.reader_problem(reader)
                   or limits.range_problem("token_budget", token_budget,
                                           MIN_TOKEN_BUDGET, MAX_TOKEN_BUDGET))
        if problem:
            raise _invalid(problem)
        known, known_as_of = _moment_or_error("known_as_of", known_as_of)

        def checked():
            # Every refusal before the call waits for its turn at the
            # embedder (`_a_turn_to_ask`), as REST's twin (the review of
            # c203d330): the key, its end users, the month's count.
            db = get_sessionmaker()()
            try:
                _uid()
                end_user = _within(user_scope.for_read, user_id, None, db=db, endpoint="context")
                _count_the_read(db)
            finally:
                db.close()
            return end_user

        def run(end_user) -> str:
            db = get_sessionmaker()()
            try:
                uid = _uid()
                pending = notices.for_a_read(uid, _project(), reader, end_user,
                                             known_as_of=known, every_kind=changes == "all")
                out = assemble_context(db, uid, query=query, end_user_id=end_user,
                                       agent_id=agent_id, token_budget=token_budget,
                                       project_id=_project(), known_as_of=known,
                                       exclude_sessions=exclude_sessions, changes=pending)
                meta = {"known_as_of": known_as_of} if known_as_of else {}
                if not out.get("context") and out.get("degraded"):
                    # Not answered (search_unavailable below): its query
                    # given back, as REST's search gives it.
                    give_back_the_read(uid)
                    # Answered below as search_unavailable: in the trail as
                    # korely_search keeps the same refusal, `error` with its
                    # code; this wrote `ok` (audit of 19 Oct 2026).
                    _audit(db, uid, "context", end_user_id=user_id, result="error",
                           meta={**meta, "code": "search_unavailable"})
                else:
                    # What the agent was handed, by id, as REST records its twin.
                    src = out.get("sources") or []
                    facts_read = [s for s in src if s.startswith("fct_")]
                    _audit(db, uid, "context", end_user_id=user_id,
                           memories_read=[s for s in src if s.startswith("mem_")],
                           facts_read=facts_read,
                           meta=meta or None)
                    # And to a reader that named itself, as REST's twin.
                    read_marks.mark(uid, _project(), reader,
                                    notices.handed_as_true(facts_read, out), known_as_of=known)
            finally:
                db.close()
            if out.get("context"):
                return out["context"]
            if out.get("degraded"):
                # A failed read is not an empty memory: REST says `degraded`,
                # and this said "nothing remembered yet" until 2026-10-06, so
                # the agent concluded it knew nothing about the user from our
                # failure (the audit of 6 October, row 6). Worded as
                # korely_search words the same case, with its code.
                raise ToolError("error: search_unavailable, the memory could not be read "
                                "right now (the retrieval service is unavailable for this "
                                "request). This is not an empty result; do not conclude "
                                "anything from it.")
            return "(nothing remembered yet for this query)"

        end_user = await _off_the_loop(checked)
        async with _a_turn_to_ask():
            return await _off_the_loop(run, end_user)


def _register_add(mcp: FastMCP) -> None:
    """Register `korely_add` on `mcp`."""

    @mcp.tool()
    async def korely_add(content: str, user_id: Optional[str] = None,
                         agent_id: Optional[str] = None, run_id: Optional[str] = None,
                         timestamp: Optional[str] = None, kind: str = "note") -> str:
        """Remember something: stores a memory and extracts typed bi-temporal facts.
        Call after a durable preference, decision, or fact is settled.
        user_id: the end user it belongs to; without user_id or agent_id it is
        kept as before, in the project's memory with no end user, under the
        name your client gave when it connected. Several agents writing one
        memory: each passes its own agent_id.
        timestamp (ISO date or datetime, optional) is when the memory's events
        occurred; its facts inherit it as valid_from, so as_of reads answer what
        was true then. Omitted: now. kind: "note" (default), or "session" for
        the log of a work session, which a read may leave out."""
        # The write of every door (services/memory_write.py), REST's own: the
        # rules (this one took 20,000 characters, a NUL byte and any run_id
        # until the audit of 2026-10-06, row 5), the key's end users, the
        # hosted service's cap on agent names and its monthly quota, the
        # write, the facts inline when WRITE_ASYNC is off, the audit row and
        # the webhook. A refusal is REST's, as this door's error line.
        # The name the client gave when it connected, read here, on the
        # request's own context: the write below runs in a worker thread.
        # It is who writes (1.0.19): every write says who it is, and an
        # assistant says it when it connects (`clientInfo.name`, which the
        # protocol requires), so a call that names no user_id nor agent_id
        # is written where it always was, the project's memory with no end
        # user, under that name (`client`, on the row and in the trail). The
        # reads, the confined keys' walls and the judge see it as before: a
        # person's facts written before 1.0.19 and after it stay one memory.
        # A session whose client gave no name it can be told by is held to
        # the ids, as REST is. The founder's decision of 12 Nov 2026: the
        # assistants' connection sets the identity itself.
        client = _client_of(mcp)

        def run():
            db = get_sessionmaker()()
            try:
                uid = _uid()
                key_id = _agent_key_id.get()
                try:
                    m, facts = memory_write.write(
                        db, uid, _project(), content, user_id=user_id, agent_id=agent_id,
                        run_id=run_id, timestamp=timestamp, client=client,
                        end_users=_end_users(), actor="mcp",
                        api_key_id=UUID(key_id) if key_id else None, kind=kind,
                        identity=client is None)
                except HTTPException as e:
                    raise refusal(e) from None
                # Read identity WHILE the session is open — rows detach on close.
                mem_id = mem_public_id(m.id)
                fact_lines = [f"  · {in_its_line(_fact_line(f))}" for f in facts]
            finally:
                db.close()
            return mem_id, fact_lines

        mem_id, fact_lines = await _off_the_loop(run)
        out = [f"Stored {mem_id}."]
        if fact_lines:
            out.append(f"Extracted {len(fact_lines)} fact(s):")
            out += fact_lines
        return "\n".join(out)


#: The most of the whole texts one korely_search answer holds with
#: `full_text` (1.0.13), counted as they are printed (`in_its_line`, their
#: indented lines included): the largest block korely_get_context gives,
#: MAX_TOKEN_BUDGET tokens of BYTES_PER_TOKEN UTF-8 bytes, as the block counts
#: them. Without it, 50 memories of 16,000 characters made one answer of
#: 802,449 characters (the skeptic's probe of 7 Nov 2026), and counted before
#: the indentation, 50 memories made of line breaks 229,701 bytes (his probe
#: of 8 Nov). Past it, a result shows what is left of it, and never less than
#: REST's `snippet` as the answer without `full_text` shows it: an answer is
#: at most this, plus what the same answer without `full_text` holds, plus
#: its `[cut]` marks and its NOTE.
WHOLE_TEXT_BYTES = MAX_TOKEN_BUDGET * BYTES_PER_TOKEN


def _printed_bytes(text: str) -> int:
    """What `text` weighs as a tool prints it (`in_its_line`), in UTF-8
    bytes, as the context block counts its own."""
    return len(in_its_line(text).encode("utf-8"))


def _printed_within(text: str, room: int) -> str:
    """The longest beginning of `text` whose printed form takes at most
    `room` bytes: the printed form never shrinks as the text grows (a line
    break begins one more indented line, or none at the very end), so the
    cut is found by bisection on the characters, never inside one."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _printed_bytes(text[:mid]) <= room:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _result(score: str, text: str, mark: str, public_id: str) -> str:
    """One result of korely_search: its score, its text kept in its line
    (`in_its_line`), and its id at the end of the text's first line, the one
    that begins with the score. A result's id is the last one on that line
    whatever the text holds: a text whose first line ends with another
    memory's id is followed by its own on the same line. A text on one line
    is `[score] text  (id)`, as in 1.0.12."""
    first, _, rest = in_its_line(text).partition("\n")
    return f"[{score}] {first}{mark}  ({public_id})" + (f"\n{rest}" if rest else "")


def _search_answer(hits: list, full_text: bool) -> str:
    """korely_search's answer: a result per hit (`_result`), its text as
    much as REST's `snippet` shows, or with `full_text` as REST's `content`,
    the whole texts within WHOLE_TEXT_BYTES as printed. The NOTE counts the
    results marked `[cut]`."""
    left, cut, lines = WHOLE_TEXT_BYTES, 0, []
    for h in hits:
        score = h.get("score")
        s = f"{score:.3f}" if isinstance(score, (int, float)) else "—"
        text = h.get("content") or ""
        shown = text[:limits.SNIPPET_CHARS]
        if full_text:
            if _printed_bytes(text) <= left:
                shown = text
            else:
                shown = max(_printed_within(text, left), shown, key=len)
            left = max(0, left - _printed_bytes(shown))
        mark = " [cut]" if full_text and shown != text else ""
        cut += bool(mark)
        lines.append(_result(s, shown, mark, fact_search.public_id(h)))
    if cut:
        lines.append(f"NOTE: {cut} whole text(s) cut where [cut] is: the whole texts of one "
                     f"answer take {MAX_TOKEN_BUDGET} tokens at most as printed, as "
                     "korely_get_context's largest block, and past them a result shows what "
                     f"is left, at least its first {limits.SNIPPET_CHARS} characters. Ask with a "
                     "smaller limit, or read a memory whole with GET /v1/memories/{id}.")
    return "\n".join(lines)


def _register_search(mcp: FastMCP) -> None:
    """Register `korely_search` on `mcp`."""

    @mcp.tool()
    async def korely_search(query: str, user_id: Optional[str] = None,
                            agent_id: Optional[str] = None,
                            limit: int = limits.DEFAULT_SEARCH_RESULTS,
                            min_score: Optional[float] = None,
                            exclude_sessions: bool = False,
                            types: str = fact_search.MEMORIES,
                            full_text: bool = False) -> str:
        """Semantic vector search over raw memories, and the facts when asked:
        ranked hits with a score. agent_id narrows to one agent's namespace.
        limit: 1 to 50. min_score (0 to 1, optional) leaves out the memories
        scored below it; with facts in types it is refused. exclude_sessions
        leaves out the work sessions a coding assistant saved. types:
        memories (default), facts (the facts true now, written directly ones
        included), or memories,facts (both, merged by rank, not by score; at
        equal rank a memory first). Each hit shows 280 characters of its text
        (a fact's line) and its id, mem_ or fct_, the last one on the line
        that begins with its score; the lines of a text after its first are
        indented. full_text shows whole texts, 8000 tokens of them in all as
        printed; past them a hit shows what is left, at least its 280
        characters, marked [cut]. For the assembled block, use
        korely_get_context instead."""
        # REST's default and bounds (POST /v1/memories/search): this said 10
        # and cut 500 to 50 where REST answers 422 (audit of 17 Oct 2026).
        problem = (limits.query_problem(query) or limits.min_score_problem(min_score)
                   or limits.id_problem("user_id", user_id)
                   or limits.id_problem("agent_id", agent_id)
                   or limits.range_problem("limit", limit, 1, limits.MAX_SEARCH_RESULTS)
                   or limits.name_problem("types", types, limits.MAX_ID_CHARS))
        # REST's list as one text, `memories,facts` (a tool's argument is one
        # value), bounded as an id is and read by REST's rules.
        kinds = fact_search.kinds_in(types)
        problem = problem or (fact_search.types_problem(kinds)
                              or fact_search.combination_problem(kinds, min_score=min_score))
        if problem:
            raise _invalid(problem)

        def checked():
            # Every refusal before the call waits for its turn at the
            # embedder (`_a_turn_to_ask`), as REST's twin (the review of
            # c203d330): the key, its end users, the month's count.
            db = get_sessionmaker()()
            try:
                _uid()
                eu: Any = _within(user_scope.for_read, user_id, EVERY_END_USER, db=db,
                                  endpoint="memories:search")
                _count_the_read(db)
            finally:
                db.close()
            return eu

        def run(eu: Any):
            db = get_sessionmaker()()
            try:
                uid = _uid()
                try:
                    hits = fact_search.search(
                        AgentMemoryEngine(db, uid, _project(), end_users=_end_users()), query,
                        types=kinds, end_user_id=eu, agent_id=agent_id, limit=limit,
                        min_score=min_score, exclude_sessions=exclude_sessions)
                except MemorySearchUnavailable as e:
                    # Not answered: its query given back, as REST's.
                    give_back_the_read(uid)
                    # In the trail as REST's twin keeps it (api/errors.refused):
                    # `error`, with its code (audit of 18 Oct 2026).
                    _audit(db, uid, "memories:search", end_user_id=user_id, result="error",
                           meta={"code": "service_paused" if e.paused else "search_unavailable"})
                    return "paused" if e.paused else None
                _audit(db, uid, "memories:search", end_user_id=user_id,
                       **fact_search.reads(hits, kinds))
            finally:
                db.close()
            return hits

        eu = await _off_the_loop(checked)
        async with _a_turn_to_ask():
            hits = await _off_the_loop(run, eu)
        if hits == "paused":
            # REST's 503 `service_paused` for the same case: a retry cannot
            # succeed until the service's own stop lifts.
            raise ToolError("error: service_paused, the service does not call the models "
                            "now (a spending stop or a provider brake). This is not an empty "
                            "result; do not conclude anything from it, and retry in an hour.")
        if hits is None:
            # An MCP client gets a sentence it can act on, not an empty list
            # that reads as "nothing matches". The distinction is the whole
            # point: one of them tells the agent to stop looking.
            # REST's 503 code for the same case (POST /v1/memories/search).
            # Cause-neutral: the provider down, or this account's places at
            # the local embedder taken (REST's 503 says which can be; audit
            # of 27 Oct 2026).
            raise ToolError("error: search_unavailable, memory search is unavailable right "
                            "now (the query could not be embedded: the embedder is busy or "
                            "not reachable). This is not an empty result; do not conclude "
                            "anything from it; retry.")
        if not hits:
            return "no matches."
        return _search_answer(hits, full_text)


def _register_get_facts(mcp: FastMCP) -> None:
    """Register `korely_get_facts` on `mcp`."""

    @mcp.tool()
    async def korely_get_facts(user_id: Optional[str] = None, agent_id: Optional[str] = None,
                               entity: Optional[str] = None,
                               subject: Optional[str] = None, predicate: Optional[str] = None,
                               predicate_family: Annotated[Optional[str],
                                                           Field(description=FAMILY_FILTER)] = None,
                               as_of: Optional[str] = None, include_invalidated: bool = False,
                               limit: int = 50, known_as_of: Optional[str] = None,
                               exclude_sessions: bool = False,
                               reader: Annotated[Optional[str],
                                                 Field(description=read_marks.READER)] = None
                               ) -> str:
        """Read typed (subject, predicate, object) facts: the bi-temporal core.
        agent_id narrows to one agent's namespace; entity matches subject OR
        object; predicate is the relation (works_at); predicate_family narrows to
        one family of relations (its description names them); as_of (ISO date)
        reconstructs what was true then; known_as_of (ISO date) what Korely knew then (a change told
        later is not in it); include_invalidated shows superseded facts;
        exclude_sessions leaves out the facts only work sessions state;
        reader (your agent's name) keeps which facts you were handed, and
        filters nothing; limit: 1 to 200. Deterministic, no model."""
        # The REST parser (GET /v1/facts): a value with no offset is UTC, and a
        # blank one is absent. This kept it naive, and Postgres reads a naive
        # value against timestamptz in the session's time zone, so the same
        # as_of could name a different instant on the two surfaces.
        # The id it narrows on by every door's rule: a NUL byte reached the
        # driver (audit of 17 Oct 2026).
        # The page by REST's bounds (GET /v1/facts), refused outside them: it
        # was cut to 1..200 in silence (audit of 17 Oct 2026).
        # The names too, by GET /v1/facts' own check (api/facts.names_problem):
        # a NUL byte in `entity` or `subject` answered the driver's words
        # (audit of 18 Oct 2026).
        # agent_id and predicate as GET /v1/facts takes them (audit of 19 Oct 2026).
        from ..api.facts import names_problem

        problem = (names_problem(user_id=user_id, agent_id=agent_id, entity=entity,
                                 subject=subject, predicate=predicate,
                                 predicate_family=predicate_family, reader=reader)
                   or limits.range_problem("limit", limit, 1, MAX_PAGE))
        if problem:
            raise _invalid(problem)
        parsed_as_of, as_of = _moment_or_error("as_of", as_of)
        known, known_as_of = _moment_or_error("known_as_of", known_as_of)

        def run():
            db = get_sessionmaker()()
            try:
                uid = _uid()
                eu: Any = _within(user_scope.for_read, user_id, EVERY_END_USER, db=db,
                                  endpoint="facts:list")
                _count_the_read(db)
                rows, _total = agent_facts.retrieve_agent_facts(
                    db, uid, end_user_id=eu, agent_id=agent_id, entity=entity, subject=subject,
                    predicate=predicate, predicate_family=predicate_family, include_invalidated=include_invalidated,
                    as_of=parsed_as_of, limit=limit, project_id=_project(),
                    known_as_of=known, exclude_sessions=exclude_sessions,
                )
                serial = [agent_facts.serialize_fact(f, known) for f in rows]
                moments = {k: v for k, v in (("as_of", as_of), ("known_as_of", known_as_of)) if v}
                _audit(db, uid, "facts:list", end_user_id=user_id,
                       facts_read=[f.id for f in rows],
                       meta=moments or None)
                read_marks.mark(uid, _project(), reader, [f.id for f in rows], as_of=parsed_as_of,
                                known_as_of=known, include_invalidated=include_invalidated)
            finally:
                db.close()
            return serial

        serial = await _off_the_loop(run)
        when = ", ".join(w for w in (f"as of {as_of}" if as_of else "",
                                     f"as known on {known_as_of}" if known_as_of else "") if w)
        if not serial:
            return "no facts." + (f" ({when})" if when else "")
        head = f"# facts {when}\n" if when else ""
        return head + "\n".join(f"- {in_its_line(_fact_line(f))}" for f in serial)


def _refused_in_the_trail(db, endpoint: str, e: HTTPException, **row) -> ToolError:
    """A refusal of a write tool kept in the trail as REST's twin keeps it
    (api/errors.refused): the call's label, `error`, the refusal's `code` in
    `meta`, the target when the tool knows it; handed back as the tool's
    error line, to be raised."""
    from ..api.errors import error_body

    code = error_body(e.status_code, e.detail, code=getattr(e, "code", None))["code"]
    meta = {**(row.pop("meta", None) or {}), "code": code}
    _audit(db, _uid(), endpoint, result="error", meta=meta, **row)
    return refusal(e)


def _register_correct_fact(mcp: FastMCP) -> None:
    """Register `korely_correct_fact` on `mcp`."""

    @mcp.tool()
    async def korely_correct_fact(fact_id: str, subject: Optional[str] = None,
                                  predicate: Optional[str] = None,
                                  object: Optional[str] = None,  # noqa: A002, REST's field name
                                  close: bool = False, at: Optional[str] = None) -> str:
        """Fix or retire ONE stored fact by its id (the fact_ ids korely_get_facts
        and korely_search return). Use it when a fact you read is wrong or no longer
        true, instead of adding a contradicting memory. Give subject, predicate
        and/or object to correct it: the old fact is closed and a new one is
        written, both stay in history (as_of, include_invalidated). Or close=true
        to just end it, with at (ISO date, optional) as when it stopped being
        true. Only a current fact can be corrected."""
        from pydantic import ValidationError

        from ..api.errors import validation_message
        from ..api.facts import (CorrectBody, model_unavailable, no_such_fact, whose,
                                 not_current_refusal)
        from ..services.ids import fact_public_id, parse_fact_id

        # The body of PATCH /v1/facts/{id}: its bounds, its sentences.
        try:
            body = CorrectBody(subject=subject, predicate=predicate, object=object)
        except ValidationError as e:
            raise _invalid(validation_message(e.errors())) from None
        fields = agent_facts.correction_fields(body.subject, body.predicate, body.object)
        if close and fields is not None:
            raise _invalid("close=true ends the fact as it stands; give either close or a "
                           "subject, predicate or object to correct, not both.")
        if not close and fields is None:
            raise _invalid("Give at least one of subject, predicate or object, or close=true.")
        if at and at.strip() and not close:
            raise _invalid("at is the moment a fact stopped being true; it goes with close=true.")
        ended_at, _ = _moment_or_error("at", at)

        def run():
            db = get_sessionmaker()()
            try:
                uid = _uid()
                key_id = _agent_key_id.get()
                fid = parse_fact_id(fact_id)
                if fid is None:
                    raise _refused_in_the_trail(
                        db, "facts:forget" if close else "facts:correct",
                        no_such_fact())
                target = fact_public_id(fid)
                # A confined key is told "not found" about another end user's
                # fact, as about another project's (REST's `_visible`).
                if not user_scope.allows(_end_users(), whose(db, fid)):
                    raise _refused_in_the_trail(
                        db, "facts:forget" if close else "facts:correct",
                        no_such_fact(), target_id=target)
                if close:
                    try:
                        out = agent_facts.forget_fact(db, uid, fid, project_id=_project(),
                                                      at=ended_at)
                    except agent_facts.ClosedBeforeItBegan as e:
                        raise _refused_in_the_trail(
                            db, "facts:forget", HTTPException(422, {"code": "invalid_request",
                                                              "message": str(e)}),
                            end_user_id=whose(db, fid), target_id=target) from None
                else:
                    # A correction is a write for the hosted quota, as over REST.
                    try:
                        memory_write.before_write(
                            db, uid, [""], endpoint="facts:correct", actor="mcp",
                            api_key_id=UUID(key_id) if key_id else None,
                            project_id=_project(), target_id=target)
                    except HTTPException as e:
                        raise refusal(e) from None
                    try:
                        out = agent_facts.correct_fact(db, uid, fid, **fields,
                                                       project_id=_project(), write_shape=True)
                    except agent_facts.FactNotCurrent as e:
                        raise _refused_in_the_trail(
                            db, "facts:correct", not_current_refusal(e),
                            end_user_id=whose(db, fid), target_id=target) from None
                    except agent_facts.MODEL_FAILURES:
                        raise _refused_in_the_trail(
                            db, "facts:correct", model_unavailable(db),
                            end_user_id=whose(db, fid), target_id=target) from None
                if out is None:
                    # Unknown, or another project's: one answer, as REST's.
                    raise _refused_in_the_trail(
                        db, "facts:forget" if close else "facts:correct",
                        no_such_fact(), target_id=target)
                _audit(db, uid, "facts:forget" if close else "facts:correct",
                       end_user_id=whose(db, fid), target_id=target,
                       meta=({"status": out["status"]} if close
                             else {"facts_closed": len(out.get("invalidated") or [])}))
            finally:
                db.close()
            return out

        out = await _off_the_loop(run)
        if close:
            when = f" (stopped being true {out['invalid_at']})" if out.get("invalid_at") else ""
            return f"{out['status']}: {out['id']}{when}. History keeps it."
        closed = ", ".join(out.get("invalidated") or []) or "none"
        return (f"Corrected. New fact {out['id']}: {out['subject']} | {out['predicate']} | "
                f"{out['object']}. Closed (kept in history): {closed}.")


def _register_forget_memory(mcp: FastMCP) -> None:
    """Register `korely_forget_memory` on `mcp`."""

    @mcp.tool()
    async def korely_forget_memory(memory_id: str, erase: bool = False) -> str:
        """Forget ONE stored memory by its id (the mem_ ids korely_add and
        korely_search return). Use it when a memory is wrong or must not be
        remembered: it drops out of every read and the facts only it stated
        are closed (kept in history); the answer counts them. erase=true
        removes it for good (text, vector, the facts only it stated):
        irreversible, so only when asked to erase."""
        from ..services.ids import parse_mem_id

        def run():
            db = get_sessionmaker()()
            try:
                uid = _uid()
                gone = HTTPException(404, "Memory not found")
                mid = parse_mem_id(memory_id)
                if mid is None:
                    raise _refused_in_the_trail(db, "memories:delete", gone)
                mid = str(mid)
                target = mem_public_id(mid)
                # The end user, read before the memory goes and behind the
                # project wall, as REST does; a forgotten memory too for an
                # erasure (it can be erased after its forget).
                engine = AgentMemoryEngine(db, uid, _project(), end_users=_end_users())
                before = engine.get_row_with_history(mid) if erase else engine._get_row(mid)
                if before is None and _end_users() is not None:
                    # Another end user's memory, for a confined key: not found.
                    raise _refused_in_the_trail(db, "memories:delete", gone, target_id=target)
                end_user_id = before.end_user_id if before is not None else None
                act = agent_facts.erase_memory if erase else agent_facts.forget_memory
                out = act(db, uid, UUID(mid), project_id=_project())
                meta = {"erase": True} if erase else {}
                if out is None:
                    raise _refused_in_the_trail(db, "memories:delete", gone,
                                                end_user_id=end_user_id, target_id=target,
                                                meta=meta or None)
                counts = {k: out[k] for k in ("facts_invalidated", "facts_deleted") if k in out}
                if out.get("facts_reopened"):
                    # As DELETE /v1/memories/{id}: the facts the deletion made
                    # true again, a count only (services/facts/given_back.py).
                    counts["facts_reopened"] = int(out["facts_reopened"])
                _audit(db, uid, "memories:delete", end_user_id=end_user_id, target_id=target,
                       meta={**meta, **counts})
            finally:
                db.close()
            return out

        out = await _off_the_loop(run)
        # A fact the deleted memory had closed and that is true again
        # (services/facts/given_back.py), so the agent knows to read it back.
        back = (f" Facts true again: {out['facts_reopened']}."
                if out.get("facts_reopened") else "")
        if out["status"] == "erased":
            return (f"Erased {out['id']} for good. Facts closed: {out['facts_invalidated']}, "
                    f"facts removed: {out['facts_deleted']}.{back}")
        return (f"Forgotten {out['id']}: it drops out of every read. Facts closed (kept in "
                f"history): {out['facts_invalidated']}.{back}")


def _get_agent_mcp() -> FastMCP:
    global _agent_mcp_singleton
    if _agent_mcp_singleton is not None:
        return _agent_mcp_singleton

    # One function per tool since the audit of 17 Oct 2026 (this one was
    # about 300 lines); registered in this order, the order of tools/list.
    mcp = _new_server()
    for register in (_register_get_context, _register_add, _register_search,
                     _register_get_facts, _register_correct_fact, _register_forget_memory):
        register(mcp)

    # The handshake used to answer `"version": "1.30.0"`, which is the MCP
    # library's version, not ours. A client asking what it is talking to got a
    # number that matches nothing in this repository. FastMCP takes no version
    # at construction, so it goes on the server underneath.
    from .. import __version__

    mcp._mcp_server.version = __version__

    _agent_mcp_singleton = mcp
    return mcp


def get_agent_streamable_http_app():
    """Starlette Streamable HTTP app for the agent MCP (mounted at /mcp)."""
    global _agent_streamable_app_singleton
    if _agent_streamable_app_singleton is None:
        inner = _get_agent_mcp().streamable_http_app()
        # Outside the SDK's authentication, inside its error middleware: the
        # one place a refusal raised while the token is verified can still be
        # answered as one.
        inner.add_middleware(_RefusalsAnswered)
        # The ceilings on sessions live in the SDK's own admission
        # (`_BoundedSessions`). The manager is the SDK's, made just above with
        # the server's settings; it holds no state of ours, so it is given our
        # class rather than built a second time with a copy of its arguments,
        # which would drift from the SDK's at its next release.
        manager = _get_agent_mcp().session_manager
        if type(manager) is StreamableHTTPSessionManager:
            manager.__class__ = _BoundedSessions
        _agent_streamable_app_singleton = _OneAuthorization(inner)
    return _agent_streamable_app_singleton


async def _send_refusal(send, e: HTTPException) -> None:
    """A refusal of core/auth (an HTTPException) as this door's answer: the
    status, the headers and the body REST sends for it (api/errors.send_refusal),
    so a client reads the same `code` over either door."""
    from ..api.errors import send_refusal

    await send_refusal(send, e)


class _RefusalsAnswered:
    """A refusal raised before the transport answered, by the token verifier
    (a key store that did not answer: core/auth.key_store_unavailable), is
    sent as REST sends it. Without this it met the SDK's error middleware and
    left as a bare 500; returned as None, as it was until 17 Oct 2026, it was
    a 401 that told OAuth clients to throw a good token away. So is a refusal
    of the session manager (`_SessionRefused`: a ceiling on sessions, the
    tenant's limiter)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        started = False

        async def watched(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, watched)
        except (HTTPException, _SessionRefused) as e:
            if started or scope.get("type") != "http":
                raise
            await _send_refusal(send, e.refusal if isinstance(e, _SessionRefused) else e)


#: The sessions one key may hold open at once. A person's agents and editor
#: windows open a handful; past this a key is a loop or a flood, and the
#: process-wide ceiling (the SDK's max_sessions) would then shut every other
#: tenant out (audit of 17 Oct 2026).
MAX_SESSIONS_PER_KEY = 25
#: The sessions one tenant may hold open at once, whatever its keys: an
#: account mints keys without a count, so the ceiling per key alone let one
#: tenant hold MAX_SESSIONS with enough of them (audit of 18 Oct 2026).
#: Four keys at their ceiling; a hundred tenants, not one, to fill the process.
MAX_SESSIONS_PER_TENANT = 100
#: The SDK's own defaults, written here so the ceiling is a decision.
MAX_SESSIONS = 10_000
SESSION_IDLE_SECONDS = 1800


class _SessionRefused(Exception):
    """A refusal raised inside the session manager, answered by
    `_RefusalsAnswered` with REST's status and body. Not an HTTPException:
    the SDK's app answers those itself, inside, with a bare text body."""

    def __init__(self, refusal: HTTPException):
        super().__init__(refusal.detail)
        self.refusal = refusal


def _session_ceiling(what: str, most: int) -> _SessionRefused:
    return _SessionRefused(HTTPException(
        status_code=429,
        detail=f"This {what} holds {most} open MCP sessions, the most one {what} may: end "
               f"one (DELETE /mcp with its mcp-session-id) or let one go idle.",
        headers={"Retry-After": str(SESSION_IDLE_SECONDS)}))


def _count_the_opening(tenant_id: str) -> None:
    """An opening request is a request: the same counter as a tool call."""
    from ..core.auth import rate_limited
    from ..services.rate_limit import RateLimited, check_and_increment

    db = get_sessionmaker()()
    try:
        check_and_increment(db, tenant_id)
    except RateLimited as e:
        raise _SessionRefused(rate_limited(e)) from None
    finally:
        db.close()


class _BoundedSessions(StreamableHTTPSessionManager):
    """The SDK's session manager, with the ceilings decided where the SDK
    decides a session: on the identity it authenticated, and in the same
    step that registers the session.

    Until the audit of 18 Oct 2026 they were decided in front of the
    SDK, on a second reading of the headers: of two Authorization headers
    that reading kept the last and the SDK the first, so a valid key first
    and anything after it opened sessions uncounted and unbounded; and the
    count was read across awaits before the SDK registered anything, so a
    burst of one key's openings all passed it. Now an opening request is
    counted in the tenant's limiter once the SDK has authenticated it, and
    `_admit_session`, which the SDK calls under its session lock with nothing
    awaited between the count and the registration, refuses a key or a
    tenant at its ceiling."""

    async def _handle_stateful_request(self, scope, receive, send):
        # Every message read by the rule first, an opening's and any other's:
        # the SDK keeps the params of an `initialize` sent on an open session
        # too.
        receive = await _messages_read(scope, receive)
        opening = not any(name == b"mcp-session-id" for name, _ in scope.get("headers") or ())
        token = getattr(scope.get("user"), "access_token", None)
        tenant = ((getattr(token, "claims", None) or {}).get("tenant")
                  if token is not None else None)
        if opening and tenant:
            await admission_off_the_loop(_count_the_opening, tenant)
        await super()._handle_stateful_request(scope, receive, send)

    def _admit_session(self, requestor):
        if requestor is not None:
            owners = list(self._session_owners.values())
            if sum(1 for o in owners
                   if o.get("client_id") == requestor.get("client_id")) >= MAX_SESSIONS_PER_KEY:
                raise _session_ceiling("key", MAX_SESSIONS_PER_KEY)
            if sum(1 for o in owners
                   if o.get("subject") == requestor.get("subject")) >= MAX_SESSIONS_PER_TENANT:
                raise _session_ceiling("account", MAX_SESSIONS_PER_TENANT)
        return super()._admit_session(requestor)


class _OneAuthorization:
    """In front of the transport: a request with more than one Authorization
    header is a 400. Which one counts is a reading, and two readings of the
    same request disagreed once (`_BoundedSessions`); with one header there is
    nothing to read two ways, here or anywhere else."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            given = sum(1 for name, _ in scope.get("headers") or () if name == b"authorization")
            if given > 1:
                return await _send_refusal(send, HTTPException(
                    status_code=400,
                    detail="The request carries more than one Authorization header: send one."))
        return await self.app(scope, receive, send)


def routed_path(path: str) -> str:
    """The path the router sees for `path` (`AcceptTheAddressWeDocument`):
    the hosted spelling and the bare /mcp as the mount's /mcp/, the resource's
    metadata where the mount serves it. main._BodyCap reads the ceiling of
    this one, so every spelling of the door has the MCP's (audit of 18 Oct
    2026: /agent/mcp had REST's 2 MiB and /mcp the SDK's 4 MiB)."""
    if path in (_METADATA + MOUNT_PATH, _METADATA + HOSTED_PATH):
        return MOUNT_PATH + _METADATA + published_path()
    if path == HOSTED_PATH or path.startswith(HOSTED_PATH + "/"):
        path = MOUNT_PATH + path[len(HOSTED_PATH):]
    return MOUNT_PATH + "/" if path == MOUNT_PATH else path


_METADATA = "/.well-known/oauth-protected-resource"


class AcceptTheAddressWeDocument:
    """`/mcp` and `/mcp/` are the same address, because people type the first.

    Starlette's `Mount` matches a prefix only when a slash follows it, so a POST
    to `/mcp` never reached the MCP app at all: it fell through to the screen's
    catch-all, which answers GET and nothing else, and the client got
    `405 Method Not Allowed` with no body and nothing to go on.

    Every piece of our own documentation writes the address without the trailing
    slash, and so does every person configuring a client by hand. The official
    client would have followed a redirect, but not every client does, and a
    redirect is a strange thing to require for an address we publish ourselves.
    Rewriting the path before routing makes both spellings the same request.

    And /agent/mcp is the same address too (8 Oct 2026): the hosted
    product's, which its clients and SDKs already use, served by this one
    codebase. Rewritten to /mcp before routing, so there is one server
    behind two spellings.

    And the resource's metadata is where the 401 says it is (8 Oct 2026). A
    401 points the client at `/.well-known/oauth-protected-resource/mcp` at
    the root of the host (RFC 9728 section 3.1 puts the well-known part
    before the resource's path), and the document lived only inside the
    mount, at /mcp/.well-known/...: the pointer answered 404. Rewritten to
    the document inside the mount, for either spelling of the address.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path") or ""
            routed = routed_path(path)
            if routed != path:
                scope = dict(scope, path=routed, raw_path=routed.encode())
        await self.app(scope, receive, send)


def mount_agent_mcp(app) -> None:
    """Mount the MCP surface on a FastAPI app at /mcp.

    Mounting alone is not enough. `streamable_http_app()` returns a Starlette
    app whose lifespan starts the session manager, and Starlette does not run
    the lifespan of a mounted sub-application, so the transport would accept
    connections and never answer. The manager is started from the host app's
    lifespan instead, which is why this returns nothing and the caller has to
    hold `agent_mcp_lifespan`.
    """
    app.mount(MOUNT_PATH, get_agent_streamable_http_app())
    app.add_middleware(AcceptTheAddressWeDocument)


def agent_mcp_lifespan():
    """Async context manager that runs the MCP session manager."""
    return _get_agent_mcp().session_manager.run()
