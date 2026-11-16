"""Authentication for the REST surface.

Two credentials, and they are for two different things.

An API key is what a program runs with: it carries a tenant and scopes, lives
in an environment variable, and expires on the day chosen when it was made (90
days unless somebody said otherwise; never, if somebody said so). It begins
`kor_self_` on an install of one's own and `kor_live_` on the Cloud
(KORELY_MODE=hosted, the same code: services/keys.minted_prefix).

A `kor_sess_` token is a person who logged in to the screen. It expires, it can
be taken back before it expires, and it does not live in a file. On an install
of one's own it comes from a password, or from the identity provider in front
of the box (`TRUST_AUTH_HEADER` is how you say so). On the Cloud it comes from
a Firebase sign-in (hosted/firebase.py, `POST /hosted/session`). On both, an
MCP client such as ChatGPT or Claude may get a key of one project through
OAuth (api/oauth.py), with a person's consent. (This said "no Firebase and no
OAuth of our own" until the audit of 17 Oct 2026: both are here.)

The tenant is never a request parameter. It comes from the credential, so a
caller cannot ask for another tenant's data by changing a field, and no endpoint
has to remember to filter by it. The same is true of the project: a key belongs
to one, and `key_project` below is where that is read. Neither is ever taken
from the request.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db import AgentApiKey, Operator, get_db
from ..config import get_settings
from . import threads
from ..services import keys as key_service
from ..services import operators as operator_service
from ..services.memory_core import log_safe
from ..services.rate_limit import UNITS, RateLimited, check_and_increment

logger = logging.getLogger(__name__)

_scheme = HTTPBearer(auto_error=False)

_INVALID = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or missing API key",
    headers={"WWW-Authenticate": "Bearer"},
)


def _invalid(why: str) -> HTTPException:
    """A 401 that says what is wrong (2026-10-07, as the hosted product's): the
    same beginning and the same code `invalid_key` for every case, and a reason
    read from what the caller sent, never from the database."""
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                         detail=f"Invalid or missing API key: {why}",
                         headers={"WWW-Authenticate": "Bearer"})


def _no_key() -> HTTPException:
    # The prefix of the keys this process mints, `kor_live_` on the hosted
    # service: until 0.1.28 this named `kor_self_` there too, a key no Cloud
    # customer has (seen on api.korely.ai, 12 Oct 2026).
    return _invalid(f"send it in the header Authorization: Bearer {key_service.minted_prefix()}...")

# Every dependency in this module is a plain `def`, and must stay one. They all
# talk to Postgres through a synchronous session, and FastAPI runs a `def`
# dependency in its threadpool but awaits an `async def` one on the event loop.
# They used to be `async def`: every authenticated request then did its key
# lookup, its `last_used_at` write and its rate-limit upsert with the loop held,
# and the image runs one worker, so a slow query or a lock wait stopped every
# request in the process, `/health` included. With the pool exhausted it was
# worse than slow: the connection it waited for is released by another
# request's teardown, which needs the loop it was holding.
# tests/test_the_event_loop_stays_free.py measures it.



def key_store_unavailable() -> HTTPException:
    """The 503 of a credential that could not be checked because the database
    did not answer: `unavailable`, with Retry-After. Never the 401 of an
    unknown key (audit of 17 Oct 2026): over MCP a Postgres blip answered
    every request `invalid_token`, an OAuth client (ChatGPT, Claude) threw
    its token away and asked for a new authorization, and the holder of a
    key read it as revoked. REST and MCP answer this same refusal."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "unavailable",
                "message": "The key could not be checked: the database did not answer. "
                           "Nothing was done; retry."},
        # The retry of every 503 `unavailable` of the store, one number
        # (core/threads.BUSY_RETRY_SECONDS; audit of 27 Oct 2026).
        headers={"Retry-After": str(threads.BUSY_RETRY_SECONDS)})


def credential_lookup(fn, db: Session, presented: str):
    """`fn(db, presented)`, a lookup of a key or a session, with a database
    error turned into `key_store_unavailable`. The lookup itself never caches
    a failure as "unknown" (services/keys.py remembers a hash only once a
    query has answered no row). The stack goes to the log without its message
    (`log_safe.stack`); the session is rolled back for whoever closes it."""
    try:
        return fn(db, presented)
    except SQLAlchemyError as e:
        logger.error("[auth] the credential store did not answer: %s", log_safe.stack(e))
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 - the 503 is the answer either way
            pass
        raise key_store_unavailable() from None


def verify_api_key(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_scheme),
    db: Session = Depends(get_db),
) -> AgentApiKey:
    """Resolve a bearer token to its live key row, or 401.

    `keys.verify_api_key` decides, the one place REST, /admin and MCP look a
    key up: a key gets in by its hash in this install's table, except a
    `kor_live_` key on an install of one's own, refused on its prefix before
    any lookup since 0.2.0 (`keys.refused_here`; until 0.1.42 the keys that
    install had minted before 0.1.3 were let in). On the Cloud every key is
    `kor_live_`, the prefix it mints.

    The same error for a missing key, a malformed key, an unknown key and a
    revoked one: distinguishing them tells an attacker which guesses were close.
    A refused `kor_live_` key is told whose prefix it is (`refusal_for`).
    """
    if creds is None or not creds.credentials:
        # No Authorization header, or one that is not `Bearer`.
        raise _no_key()
    row = credential_lookup(key_service.the_requests_key, db, creds.credentials)
    if row is None:
        raise refusal_for(creds.credentials)
    return row


def refusal_for(presented: str) -> HTTPException:
    """The 401 for a key this install does not accept.

    A key that begins `kor_live_` and is not a live key here is almost always a
    key of the hosted service pasted into the wrong client, and the caller is
    told so: asked for by a tester who watched both directions answer the same
    opaque 401 ("the data to say it is already on the key, it is literally the
    first field").

    It reveals nothing about what is stored. It is chosen from the presented
    string alone, by the rule that refused it (`keys.refused_here`): on an
    install of one's own every `kor_live_` key is refused on its prefix since
    0.2.0, before any lookup, so every one gets these words whatever the
    table holds. It does not say "this key belongs to the hosted service":
    a string can carry the prefix without being anybody's key.

    The others (2026-10-07, the hosted product's wording): a string that is no
    key of any install is told the shape of one; a well-formed key that is not
    a live key here, unknown, revoked or expired, gets one sentence for the
    three (`keys.NOT_LIVE`, the words of every door), so they cannot be told
    apart, and the page where this install's keys are.
    """
    # On the hosted service (KORELY_MODE=hosted) `kor_live_` is its own prefix,
    # and an unknown one is just an unknown key.
    if key_service.refused_here(presented):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "This key is not valid on this install. Keys of the hosted "
                f"Korely service begin with {key_service.PREFIX_LIVE} and do not work on an "
                f"install of your own; keys minted here begin with {key_service.PREFIX_SELF}."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not key_service.has_the_shape_of_a_key(presented):
        # In no deployment's words, and with the step after it: "a key of
        # this install" on the Cloud, and no way forward, for the commonest
        # first-day error, a key cut or pasted with a space (pre-tester
        # check of 15 Nov 2026).
        return _invalid(f"a key is {key_service.minted_prefix()} followed by 43 characters and "
                        f"this one is not: check that it was pasted whole; {_where_the_keys_are()}")
    return _not_live()


def _where_the_keys_are() -> str:
    """The way forward of a 401 for a key: the page with this deployment's keys."""
    from ..config import dashboard_url

    return f"your keys are at {dashboard_url()}/keys"


def _not_live() -> HTTPException:
    return _invalid(f"{key_service.NOT_LIVE}; {_where_the_keys_are()}")


def missing_scopes(have, *needed: str) -> list[str]:
    """The scopes in `needed` that a key holding `have` lacks.

    The one rule, read by the REST dependencies below and by the MCP tools
    (mcp/server.py), so that a key cannot do over one door what the other
    refuses. A key with no scopes holds none: nothing is granted by default.
    """
    held = set(have or ())
    return [s for s in needed if s not in held]


def scope_refusal(missing) -> str:
    return f"API key missing required scope(s): {', '.join(missing)}"


def require_scopes(*needed: str):
    """Dependency factory: the key must carry every listed scope.

    Least privilege. A read endpoint asks for `memories:read` and nothing more,
    so a key issued for reading cannot be used to write even if the endpoint
    behind it changes later.
    """

    def _dep(creds: Optional[HTTPAuthorizationCredentials] = Depends(_scheme),
             key: AgentApiKey = Depends(verify_api_key)) -> AgentApiKey:
        missing = missing_scopes(key.scopes, *needed)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=scope_refusal(missing),
            )
        # The old secret of a rotation holds the key's scopes but `admin`
        # (services/keys.scopes_of); `verify_api_key` let it in, so `creds`
        # is there.
        if missing_scopes(key_service.scopes_of(key, creds.credentials), *needed):
            raise previous_secret_refused()
        return key

    return _dep


def require_read_with_quota(*needed: str):
    """Scope check for a read endpoint that counts as a read, which the route
    then counts with `count_the_read` once its input is checked.

    The hosted service meters reads against a monthly allowance and answers
    429 past it (korely_agent/hosted/, through `extensions.hooks.before_query`).
    On an install of one's own the hook does nothing: you are reading your own
    database. The distinction between a read dependency and a write dependency
    is worth seeing at the call site either way.

    The count was here, and a dependency runs before FastAPI checks the
    route's own query and body: a read refused 422 (`as_of=yesterday`, a
    limit out of bounds) or 403 (a confined key naming another end user)
    took a query of the month (audit of 17 Oct 2026).
    tests/test_a_refused_read_counts_nothing.py holds every route that takes
    this dependency to calling `count_the_read`.
    """
    dep = require_scopes(*needed)
    dep.counts_a_read = True
    return dep


def count_the_read(db: Session, key) -> None:
    """Count this read, on the Cloud against the month (`before_query`, 429
    `quota_exceeded` past it); on an install of one's own nothing. Called by
    a read route after its input is checked and the key's end users placed,
    right before it reads: a refused read costs nothing, a read answered
    (a 404 included: the lookup was made) counts once."""
    from .extensions import hooks

    hooks.before_query(db, key.tenant_id)


def give_back_the_read(tenant_id) -> None:
    """The read `count_the_read` counted ended in 503 `search_unavailable`:
    neither answered nor refused before it was counted, so on the Cloud its
    query is given back (`query_not_answered`). Called where that 503 is
    raised, REST's and the MCP's (audit of 27 Oct 2026: a Free account under
    the embedder's pressure spent its month on failures)."""
    from .extensions import hooks

    hooks.query_not_answered(tenant_id)


def ready_to_wait(db: Session, key):
    """The account of a read about to wait on the loop for its turn at the
    embedder (`a_turn_for_its_question`), read on the request's thread; and
    whatever the read's checks left open on its session ends here, so the
    wait holds no pooled connection (a hosted hook's read, a key row reloaded
    after its stamp was rolled back). What they wrote they committed
    themselves; this only lets the connection go."""
    tenant = key.tenant_id
    if db.in_transaction():
        db.commit()
    return tenant


@contextlib.asynccontextmanager
async def a_turn_for_its_question(account):
    """A read's turn to ask the local embedder for its question's vector,
    waited for on the event loop (core/threads.waits_for_the_embedder) once
    the read's own checks have passed and before it takes a thread for its
    search, and held until the vector has been computed. The routes that ask
    for one (`POST /v1/memories/search`, `GET /v1/context`, the screen's
    search) are async for this, their synchronous parts on request threads
    before and after the wait (core/threads.on_a_request_thread), as the
    MCP's korely_search and korely_get_context wait on the loop and run their
    bodies off it.

    The finding of 31 Oct 2026 (scripts/concurrent_agents.sh, 400294b3): a
    sync route is already on its thread when it asks for the vector, so the
    embedder refused an account's fifth search at once with 503
    `search_unavailable`, to keep one account from holding the threads every
    account's requests share (audit of 19 Oct 2026); under one account's 30
    agents, 549 searches were refused, and a real agent does not retry.
    Waiting here costs another account nothing: no thread, and no pooled
    connection (`ready_to_wait`). Everything a read is refused for is
    decided before it waits (the review of c203d330: a malformed body, a
    blank question, a confined key naming another end user and the Cloud's
    monthly 429 waited up to 20 s for a place they did not need): FastAPI
    checks an async route's body and query before it calls it, and the
    route's own checks and its count of the month run on the first thread.
    Past core/threads.WAIT_SECONDS the read goes on without a place, and its
    question is refused at the embedder as before: 503 with Retry-After for
    a search, the facts with `degraded` for a context.

    Only with EMBEDDING_PROVIDER=local: the places are the local embedder's,
    and a Google vector, a read's or a write's, takes none."""
    if get_settings().embedding_provider != "local":
        yield True
        return
    async with threads.waits_for_the_embedder(account) as got:
        yield got


def enforce_rate_limit(
    key: AgentApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db),
) -> None:
    """Router-level ceiling, off unless configured.

    Attached to the router rather than to each endpoint: a limit that has to be
    remembered on every new route is a limit that will eventually be forgotten
    on one.
    """
    # The counts went into request.state for a response middleware that was
    # never written (gone 2026-10-06, the audit of 6 October, row 14): a 429
    # says what a caller needs, with Retry-After.
    try:
        check_and_increment(db, key.tenant_id)
    except RateLimited as e:
        raise rate_limited(e) from None


def rate_limited(e: RateLimited) -> HTTPException:
    """The 429 of a tenant past its ceiling, on every door: /v1, /admin, an
    MCP tool call (mcp/server.refusal) and a request that opens an MCP
    session (mcp/server._count_the_opening). Until 17 Oct 2026 it was built
    in three places with two wordings, and MCP's named neither the code nor
    the window's unit. The window is the one that refused: the minute of
    RATE_LIMIT_PER_MINUTE, or the hosted plan's minute, hour or day; a
    ceiling with a sentence of its own (`RateLimited.message`) says it. The
    code is named, so a refusal kept in the trail carries it
    (services/memory_write._kept_in_the_trail) as the body does."""
    from ..api.errors import CodedHTTPException

    return CodedHTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        e.message or f"Rate limit exceeded ({e.limit}/{UNITS[e.window]}): try again in "
                     f"{e.reset_in} s",
        code="rate_limit_exceeded",
        headers={"Retry-After": str(max(1, e.reset_in))},
    )


# ── administration: a person, or a key that was issued for it ───────────────

_FORBIDDEN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Administration requires an operator session or a key with the "
           "'admin' scope.",
)


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _same_site_only(request: Request) -> None:
    """A gateway's header comes with the browser's cookie on whatever page
    sent the request, so a change made under it must come from this site
    (audit of 17 Oct 2026: a form on another site revoked a key). The
    browser says where a request came from in Sec-Fetch-Site (Fetch Metadata,
    W3C) or, failing that, Origin: a write is refused when either names
    another site; a client that sends neither (a script, not a page) is not a
    cross-site page and goes on. The sessions and keys of the screen are
    unaffected: a page of another site cannot set an Authorization header."""
    if request.method.upper() in _SAFE_METHODS:
        return
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        raise _CROSS_SITE
    origin = (request.headers.get("origin") or "").strip()
    if not site and origin and origin != "null":
        from urllib.parse import urlsplit

        if urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower():
            raise _CROSS_SITE
    elif not site and origin == "null":
        raise _CROSS_SITE


_CROSS_SITE = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail={"code": "cross_site_request",
            "message": "A change made through the sign-in gateway must come from this site."})


def _operator_from_trusted_header(request: Request, db: Session):
    """The gateway in front said who this is.

    Off unless `TRUST_AUTH_HEADER` names a header. This is the answer to "do
    you support SSO" for a service with no screen of its own: the customer's
    own gateway does SAML or OIDC and passes the result down.

    It is also the setting that hands the whole account to anybody who can
    reach this service directly, if the gateway does not strip the header on
    the way in. That sentence is in the example config, in capitals.
    """
    header = (get_settings().trust_auth_header or "").strip()
    if not header:
        return None
    username = request.headers.get(header)
    if not username:
        return None
    _same_site_only(request)
    op = operator_service.by_username(db, username)
    if op is None or op.disabled_at is not None:
        return None
    return op


def admin_principal(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_scheme),
    db: Session = Depends(get_db),
):
    """Whoever is administering, as something with a `tenant_id`.

    An operator session and an admin key resolve to different rows and the
    routes do not care which: both carry the tenant, which is the only thing
    they read. That is what lets the login arrive without touching a route.
    """
    return _administrator(request, creds, db, key_service.the_requests_key)


def administrator_unstamped(request: Request, creds: Optional[HTTPAuthorizationCredentials],
                            db: Session):
    """`admin_principal`'s answer with nothing written: a key's last use is
    not stamped (services/keys.live_key). Who a request to /admin is, read
    before its route lets it in (main._the_caller); the route's own
    `admin_principal` stamps it once it does."""
    return _administrator(request, creds, db, key_service.live_key)


def _administrator(request: Request, creds: Optional[HTTPAuthorizationCredentials],
                   db: Session, key_lookup):
    op = _operator_from_trusted_header(request, db)
    if op is not None:
        return op

    token = creds.credentials if creds else ""
    if not token:
        raise _INVALID

    if token.startswith(operator_service.SESSION_PREFIX):
        op = credential_lookup(operator_service.resolve, db, token)
        if op is None:
            raise _INVALID
        return op

    row = credential_lookup(key_lookup, db, token)
    if row is None:
        raise refusal_for(token)
    if missing_scopes(row.scopes, key_service.ADMIN_SCOPE):
        raise _FORBIDDEN
    # The old secret of a rotation holds no `admin` (services/keys.scopes_of).
    if missing_scopes(key_service.scopes_of(row, token), key_service.ADMIN_SCOPE):
        raise previous_secret_refused()
    return row


def still_admin(db: Session, principal) -> None:
    """The `admin` a key was let in with, read again in the transaction that
    is about to write with it, and held there to its commit.

    A key's scopes are read when the request comes in (`admin_principal`,
    `require_scopes`), and a route that waits before it writes (a webhook's
    address looked up, up to threads.LOOKUP_SECONDS, from a DNS whoever sent
    the address may answer late) wrote with what was true then. On the Cloud
    an adoption takes `admin` from every key of the account it adopts
    (hosted/claim), and a request let in before it made a key with `admin`
    for an account a person now owns, switched a webhook the adoption had
    switched off back on, and closed the adopted account (the skeptic's
    review of 5 Nov 2026). So the route calls this where it writes: a share
    lock of the account's row (`tenants`), which a change of the account's
    keys takes for an update first (claim._hold_the_account), then the key's
    row as it is now. One of the two comes first, and the other sees what it
    did: a write that came first is in the account the adoption changes; one
    that comes after finds the key without `admin` (403, the words of every
    admin door) or no longer live (401, the words of every door).

    The lock is the transaction's: the caller writes and commits in the same
    one, with nothing between that ends it. A person's session (an operator)
    is the account's own and is not read again. Rolls back before it
    refuses.

    The key is found by its id alone, which its row object keeps when a
    rollback has expired the rest: `principal.tenant_id` would load the row
    again, and a key whose account was erased meanwhile raised
    ObjectDeletedError, a 500 (the skeptic's round 2 of 5 Nov 2026, two
    DELETE /v1/account of one key at once). Its account is read from the
    row as it is, locked, and the row read again under the lock."""
    from datetime import datetime, timezone

    from sqlalchemy import inspect, select

    from ..db import Tenant
    from .times import aware

    if is_signed_in_person(principal):
        return
    state = inspect(principal, raiseerr=False)
    key_id = state.identity[0] if state is not None and state.identity else principal.id
    key = db.query(AgentApiKey).filter(AgentApiKey.id == key_id)
    tenant_id = key.with_entities(AgentApiKey.tenant_id).scalar()
    if tenant_id is not None:
        db.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update(read=True))
    row = key.populate_existing().first() if tenant_id is not None else None
    live = (row is not None and row.tenant_id == tenant_id
            and row.revoked_at is None
            and (row.expires_at is None or aware(row.expires_at) > datetime.now(timezone.utc)))
    if not live:
        db.rollback()
        raise _not_live()
    if missing_scopes(row.scopes, key_service.ADMIN_SCOPE):
        db.rollback()
        raise _FORBIDDEN


def is_signed_in_person(principal) -> bool:
    """A person signed in to the dashboard (an operator's session; on the
    Cloud, the account's), as opposed to an API key with the admin scope."""
    return isinstance(principal, Operator)


def signed_in_person(principal=Depends(admin_principal)):
    """Only a person: closing an account, adopting a key, paying, connecting
    an app over OAuth (api/oauth.py). Not an API key, even one with the admin
    scope: a leaked key must not be able to hand itself more."""
    if not is_signed_in_person(principal):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={
            "code": "dashboard_only", "message": "Sign in to the dashboard to do this."})
    return principal


def previous_secret_refused(code: str = key_service.PREVIOUS_SECRET_NO_ADMIN) -> HTTPException:
    """The 403 to the old secret of a rotation (services/keys.scopes_of says
    why): `admin` it does not hold, or (`PREVIOUS_SECRET_CODE`) an act that
    would end or change a key."""
    said = (key_service.PREVIOUS_SECRET_NO_ADMIN_SAID if code == key_service.PREVIOUS_SECRET_NO_ADMIN
            else key_service.PREVIOUS_SECRET_REFUSED)
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": code, "message": said})


def presented_secret(creds: Optional[HTTPAuthorizationCredentials] = Depends(_scheme)) -> Optional[str]:
    """The secret the request's `Authorization: Bearer` presents, or None: for
    a door that must know which of a key's two secrets let the request in
    (services/keys.is_the_previous_secret). FastAPI resolves `_scheme` once per
    request, so it is the one the key was looked up with."""
    return creds.credentials if creds is not None and creds.credentials else None


def enforce_admin_rate_limit(
    principal=Depends(admin_principal),
    db: Session = Depends(get_db),
) -> None:
    """The same ceiling as `/v1`, counted per tenant, for whoever is
    administering.

    A separate function because the one on `/v1` resolves an API key, and an
    operator's session is not one. Attaching that to the admin router meant a
    session was refused by the router before any route saw it, and the login
    route needed a key in order to hand out a session. FastAPI resolves
    `admin_principal` once per request, so this costs nothing twice.

    A person signed in counts in the control plane's own bucket, with the
    same ceilings (`rate_limit.CONTROL_PLANE`): no key of the account,
    whatever its scope, can spend the allowance the operator needs to see and
    revoke it (audit of 20 Oct 2026). An admin-scope key is a key, and counts in the
    account's bucket with the others.
    """
    from ..services.rate_limit import CONTROL_PLANE

    door = CONTROL_PLANE if is_signed_in_person(principal) else ""
    try:
        check_and_increment(db, principal.tenant_id, door=door)
    except RateLimited as e:
        raise rate_limited(e) from None


def key_end_users(key) -> Optional[list]:
    """The end users a key is confined to inside its project
    (services/user_scope.py), or None: the whole project. An operator's
    session and the dashboard are never confined."""
    return getattr(key, "end_users", None) or None


class OutOfScopeRefusal(HTTPException):
    """The 403 of `user_refusal`, with the end user it named (`user_id`,
    None when the write named none) and its `code`: what a door keeping the
    refusal in the trail (api/errors.refused, services/memory_write.write)
    reads from it, as fields."""

    code = "forbidden"

    def __init__(self, user_id: Optional[str], detail: str):
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
        self.user_id = user_id


def user_refusal(user_id: Optional[str]) -> OutOfScopeRefusal:
    """The 403 of a confined key that names an end user outside its list, or
    writes without naming one when its list has no single id to go to."""
    if user_id is None:
        detail = ("this key writes for some end users only: give a user_id "
                  "(it may not write the shared memory)")
    else:
        detail = f"this key may not read or write user_id {user_id!r}"
    return OutOfScopeRefusal(user_id, detail)


def _out_of_scope(db: Session, key, endpoint: str, user_id: Optional[str]) -> HTTPException:
    """`user_refusal`, kept in the trail as the call `endpoint` with the end
    user it named: the refused attempt on another person's data is the one
    an access request must find (audit of 19 Oct 2026; api/errors.refused)."""
    from ..api.errors import refused

    return refused(db, key, endpoint, user_refusal(user_id), end_user_id=user_id)


def reading(key, user_id: Optional[str], every=None, *, db: Session, endpoint: str):
    """What a read of this key filters its end users on (user_scope.for_read):
    the user_id asked for, or every end user, or the key's list. `every` is
    the caller's value for "every end user" (EVERY_END_USER where the service
    takes it, None where None means all). A refusal is kept in the trail as
    the call `endpoint`."""
    from ..services import user_scope

    try:
        return user_scope.for_read(key_end_users(key), user_id, every)
    except user_scope.OutOfScope as e:
        raise _out_of_scope(db, key, endpoint, e.user_id) from None


def writing(key, user_id: Optional[str], *, db: Session, endpoint: str) -> Optional[str]:
    """The end user a write of this key goes to (user_scope.for_write); a
    refusal is kept in the trail as the call `endpoint`."""
    from ..services import user_scope

    try:
        return user_scope.for_write(key_end_users(key), user_id)
    except user_scope.OutOfScope as e:
        raise _out_of_scope(db, key, endpoint, e.user_id) from None


def sees_row(key, end_user_id: Optional[str]) -> bool:
    """Whether a row of this end user, asked for by its id, exists for this
    key: a confined key is told "not found" about the others, as a key is
    about another project's rows."""
    from ..services import user_scope

    return user_scope.allows(key_end_users(key), end_user_id)


# The project a key is confined to is the keys' domain (services/keys.py), not
# this module's: services/audit.py reached into this FastAPI module for it,
# against the layers (audit of 17 Oct 2026). Kept here by name for the routes
# and the MCP server, which import their dependencies from here.
key_project = key_service.key_project
