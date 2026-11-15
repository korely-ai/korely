"""OAuth's doors (services/oauth.py has the reasons and the flow).

`auth_routes()`: the authorization server, the MCP SDK's routes
(`create_auth_routes`) over this install's provider, at the root of the host:
/.well-known/oauth-authorization-server, /authorize, /token, /register,
/revoke. The issuer is PUBLIC_BASE_URL, the address the MCP's protected
resource metadata names (mcp/server.py).

`router`: the consent the dashboard's /oauth/consent page shows and answers,
behind the login that exists. Only a person answers it (`signed_in_person`:
an operator's session, on the Cloud the account's), never an API key, so a
leaked key cannot hand itself new keys through somebody's app.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AnyHttpUrl, BaseModel, Field
from sqlalchemy.orm import Session

from ._bodies import Checked, declared
from ..config import api_url, dashboard_url
from ..core import extensions
from ..core.auth import enforce_admin_rate_limit, signed_in_person
from ..db import get_db
from ..core.threads import admission_off_the_loop
from ..mcp.server import published_path
from ..services import audit as audit_service
from ..services import limits, user_scope
from ..services import oauth as oauth_service
from ..services.rate_limit import network_of
from ..services.usage import log_admin_action
from .admin import GivenProjectId, account_project
from .errors import ADMIN_ROUTE_ERRORS, admin_errors

# The envelope of api/errors.py and the ceiling of every /admin route.
router = APIRouter(prefix="/admin/oauth", tags=["oauth"], responses=ADMIN_ROUTE_ERRORS,
                   dependencies=[Depends(enforce_admin_rate_limit)])


def issuer() -> str:
    """The MCP's public address, which its protected resource metadata names
    as the authorization server (mcp/server.py)."""
    return api_url()


def issuer_identifier() -> str:
    """The issuer as the metadata spells it (the SDK's AnyHttpUrl adds the
    trailing slash): RFC 9207 has the client compare `iss` to it as strings."""
    return str(AnyHttpUrl(issuer()))


def consent_url() -> str:
    """The dashboard's consent page: on the Cloud the dashboard's own domain,
    on an install of one's own the same address as the API
    (config.dashboard_url)."""
    return dashboard_url() + "/oauth/consent"


def provider() -> oauth_service.KorelyOAuthProvider:
    return oauth_service.KorelyOAuthProvider(issuer=issuer(), resource=issuer() + published_path(),
                                             consent_url=consent_url())


class _PerNetwork:
    """One of the two doors that answer anyone, /register and /authorize,
    bounded per network (services/oauth.too_many, the choice of 16 Oct
    2026: in the service, not in Caddy): past the hour's allowance a
    429, else the SDK's handler, with the network known to the provider,
    which writes it on the row it adds. An ASGI app, as the SDK's routes
    are, so Starlette calls it as it is."""

    def __init__(self, app, kind: str):
        self.app = app
        self.kind = kind

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        network = network_of(client[0] if client else None)
        if await admission_off_the_loop(oauth_service.too_many, self.kind, network):
            await self._refused(scope, receive, send)
            return
        token = oauth_service.NETWORK.set(network)
        try:
            await self.app(scope, receive, send)
        except oauth_service.TooMany:
            # The count taken again where the row is written, under its lock
            # (services/oauth._within_allowance): the early count above lets a
            # burst through together (audit of 18 Oct 2026). Raised
            # before the SDK answers anything, so the answer is still ours.
            await self._refused(scope, receive, send)
        finally:
            oauth_service.NETWORK.reset(token)

    @staticmethod
    async def _refused(scope, receive, send):
        from starlette.responses import JSONResponse

        # The window the count is over, not a second copy of it (audit
        # of 17 Oct 2026: it was "3600" beside NETWORK_WINDOW).
        wait = int(oauth_service.NETWORK_WINDOW.total_seconds())
        refused = JSONResponse({"error": "temporarily_unavailable",
                                "error_description": "too many requests from this network: "
                                                     "try again later"},
                               status_code=429, headers={"Retry-After": str(wait)})
        await refused(scope, receive, send)


class _RegistrationByTheRule:
    """POST /register's JSON read by the rule of every door
    (limits.nul_problem) before the SDK's handler reads it: a refusal is the
    SDK's own `invalid_client_metadata`, naming the place. The provider asks
    the rule of what it keeps (services/oauth.register_client), but the SDK
    answers some fields before asking it, and echoes them: a lone surrogate
    in `scope` came back in its error_description, which no JSON encoder
    writes, and the answer was a 500 (the audit before 1.0.0). Only the
    fields the SDK's model
    declares are read (api/_bodies.declared): it ignores any other, and one
    of millions of values was walked whole on the event loop (the review of
    the same finding). The body is under the ceiling of every body already
    (main._BodyCap), and goes on to the SDK as it came; one that is not
    JSON is the SDK's to refuse."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        import json

        chunks, more = [], True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, _replayed([message]), send)
                return
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        from mcp.shared.auth import OAuthClientMetadata

        try:
            problem = limits.nul_problem("", declared(OAuthClientMetadata, json.loads(body)))
        except ValueError:
            problem = None
        if problem:
            from starlette.responses import JSONResponse

            refused = JSONResponse({"error": "invalid_client_metadata",
                                    "error_description": problem}, status_code=400)
            await refused(scope, receive, send)
            return
        await self.app(scope, _replayed([{"type": "http.request", "body": body,
                                          "more_body": False}], then=receive), send)


class _OnlyToTheConsentPage:
    """/authorize sends the browser to one place: the consent page the
    provider chose for this request (services/oauth.CONSENT_PAGE). Every
    other redirect of the SDK's handler is answered here instead, as the
    SDK answers when it cannot redirect: 400, the error in JSON, no-store.

    The SDK sends its errors back to the client's registered redirect URI
    (RFC 6749 section 4.1.2.1), and registration is open to any https
    address (RFC 7591), so an invalid scope, a missing challenge or a
    refusal of the provider was a 302 from this server's address to a site
    anyone registered, without a consent page: the first attack of RFC 9700
    section 4.11.2, "Intentionally send an erroneous authorization request,
    e.g., by using an invalid scope value, thus instructing the
    authorization server to redirect the user agent to its phishing site".
    The RFC: "The authorization server SHOULD only automatically redirect
    the user agent if it trusts the redirection URI. If the URI is not
    trusted, the authorization server MAY inform the user and rely on the
    user to make the correct decision." A URI that anybody registered is
    not trusted, and the app's address is reached only from the consent
    page, after a person signed in and saw its host
    (services/oauth.approve, deny). Decided on what the handler answers,
    not on a list of its errors: one the SDK adds later stays here too (the
    security review of 12 Nov 2026)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        chosen: list = []
        answered = False

        async def only_there(message):
            nonlocal answered
            if message["type"] == "http.response.start" and 300 <= message["status"] < 400:
                where = dict(message.get("headers") or ()).get(b"location", b"").decode("latin-1")
                if where not in {_as_sent(page) for page in chosen}:
                    answered = True
                    await _answered_here(where)(scope, receive, send)
                    return
            if not answered:
                await send(message)

        token = oauth_service.CONSENT_PAGE.set(chosen)
        try:
            await self.app(scope, receive, only_there)
        finally:
            oauth_service.CONSENT_PAGE.reset(token)


def _as_sent(page: str) -> str:
    """The consent page as the SDK's answer spells it in `Location`: the
    SDK answers with Starlette's RedirectResponse, which quotes the address
    (an IDN host or a space in PUBLIC_BASE_URL is percent-encoded), so the
    address the provider returned is compared in the same spelling, made by
    the same class (the review of 12 Nov 2026: compared as returned, every
    valid request of such an install was refused)."""
    from starlette.responses import RedirectResponse

    return RedirectResponse(url=page).headers["location"]


def _answered_here(redirect: str):
    """The SDK's error answer for a request it would have sent back to the
    client (`redirect`, built by mcp.server.auth.provider.
    construct_redirect_uri: the client's own query first, then `error`,
    `error_description` and `state`). Its fields are read from the last
    `error` on, so nothing the client registered in its URI's query is
    shown here as this server's words."""
    from urllib.parse import parse_qsl, urlparse

    from mcp.server.auth.handlers.authorize import AuthorizationErrorResponse
    from mcp.server.auth.json_response import PydanticJSONResponse

    pairs = parse_qsl(urlparse(redirect).query, keep_blank_values=True)
    last = max((i for i, (k, _v) in enumerate(pairs) if k == "error"), default=len(pairs))
    said = dict(pairs[last:])
    try:
        body = AuthorizationErrorResponse(error=said.get("error"),
                                          error_description=said.get("error_description"),
                                          state=said.get("state"))
    except ValueError:
        body = AuthorizationErrorResponse(error="server_error", error_description=None,
                                          state=said.get("state"))
    return PydanticJSONResponse(status_code=400, content=body, headers={"Cache-Control": "no-store"})


def _replayed(messages: list, then=None):
    """A `receive` that gives `messages` first, then what `then` gives."""
    async def receive():
        if messages:
            return messages.pop(0)
        return await then() if then is not None else {"type": "http.disconnect"}
    return receive


# The doors that write a row for anyone, and what each counts.
_BOUNDED = {"/register": "client", "/authorize": "request"}


def auth_routes() -> list:
    """The SDK's routes, with one change to its metadata: it lists only the
    two client-secret methods, and the clients that matter here register as
    public clients with PKCE (`none`, which the SDK's own registration and
    token handlers accept). Claude reads that list to choose how to
    authenticate (claude.com/docs/connectors/building/authentication)."""
    from mcp.server.auth.handlers.metadata import MetadataHandler
    from mcp.server.auth.routes import build_metadata, create_auth_routes, cors_middleware
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions

    registration = ClientRegistrationOptions(enabled=True, valid_scopes=oauth_service.VALID_SCOPES,
                                             default_scopes=oauth_service.VALID_SCOPES)
    revocation = RevocationOptions(enabled=True)
    url = AnyHttpUrl(issuer())
    routes = create_auth_routes(provider(), url, client_registration_options=registration,
                                revocation_options=revocation)
    metadata = build_metadata(url, None, registration, revocation)
    metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post",
                                                      "client_secret_basic"]
    from starlette.routing import Route

    def served(r):
        if r.path == "/.well-known/oauth-authorization-server":
            return Route(r.path, endpoint=cors_middleware(MetadataHandler(metadata).handle,
                                                          ["GET", "OPTIONS"]),
                         methods=["GET", "OPTIONS"])
        if r.path in _BOUNDED:
            endpoint = (_RegistrationByTheRule(r.endpoint) if r.path == "/register" else
                        _OnlyToTheConsentPage(r.endpoint))
            return Route(r.path, endpoint=_PerNetwork(endpoint, _BOUNDED[r.path]),
                         methods=sorted(r.methods or ()))
        return r

    return [served(r) for r in routes]


class Consent(Checked):
    project_id: GivenProjectId = Field(description="The project the app may read and write.")
    scopes: list[str] = Field(default_factory=lambda: list(oauth_service.OFFERED_SCOPES),
                              max_length=limits.MAX_LISTED_NAMES)
    users: Optional[list[str]] = Field(
        default=None, max_length=user_scope.MAX_ENTRIES,
        description="Confine the app to these end users of the project: exact user_ids and "
                    "prefixes ending in '*'. Omitted or null: the whole project.")


class Redirect(BaseModel):
    redirect: str = Field(description="Where the browser goes next: back to the app.")


@router.get("/requests/{request_id}", responses=admin_errors(404))
def show_request(request_id: str, principal=Depends(signed_in_person), db: Session = Depends(get_db)) -> dict:
    """What an app connecting over OAuth asks for, for the consent page of
    the person signed in: the app's name, where it sends the browser back
    to, the scopes. 404 once it expired or was answered."""
    out = oauth_service.describe_request(db, request_id)
    if out is None:
        raise HTTPException(status_code=404, detail="this request expired or was already answered")
    return out


@router.post("/requests/{request_id}/approve", response_model=Redirect,
             responses=admin_errors(404, 409))
def approve(request_id: str, body: Consent, principal=Depends(signed_in_person),
            db: Session = Depends(get_db)) -> Redirect:
    """The person's yes: an authorization code for the chosen project only,
    with the scopes and end users chosen here, which the app exchanges for
    its key; the answer is the address to send the browser back to. 404 for
    a project that is not this account's and 409 `project_archived` for an
    archived one, as when a key is made by hand (POST /admin/keys); 409 when
    the request expired or was already answered; 422 with no permission
    chosen, or end users a key cannot hold."""
    # The yes is what gives the app its key (the exchange at /token mints
    # it): the account may make one here only where POST /admin/keys may.
    extensions.hooks.before_new_key(db, principal.tenant_id)
    # The project read as every /admin route reads one (audit of 17 Oct
    # 2026: a malformed id was a 422 here and a 404 everywhere else, and
    # another account's project a 409).
    project = account_project(db, principal, body.project_id, to_hold_keys=True).id
    try:
        url = oauth_service.approve(db, principal.tenant_id, request_id, project_id=project,
                                    scopes=body.scopes, issuer=issuer_identifier(),
                                    end_users=body.users)
    except oauth_service.ConsentInvalid as e:
        raise HTTPException(status_code=422, detail=str(e))
    except oauth_service.ConsentError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # The person's yes; the key the app then gets is filed by the provider.
    # The list as the code holds it (`user_scope.checked`, which approve has
    # just accepted), the form an erasure rewrites: the request's own
    # spelling kept an erased id in clear (audit of 20 Oct 2026).
    from ..services.end_user_places import recorded_users

    users = recorded_users(body.users)
    log_admin_action(db, principal, audit_service.SETTING_CHANGE, target_id=request_id,
                     meta={"setting": "oauth_consent", "scopes": body.scopes,
                           **({"users": users} if users else {})},
                     project_id=project)
    return Redirect(redirect=url)


@router.post("/requests/{request_id}/deny", response_model=Redirect,
             responses=admin_errors(409))
def deny(request_id: str, principal=Depends(signed_in_person), db: Session = Depends(get_db)) -> Redirect:
    """The person's no: the app gets an `access_denied`, and the answer is the
    address to send the browser back to. 409 when the request expired or was
    already answered."""
    try:
        return Redirect(redirect=oauth_service.deny(db, request_id, issuer=issuer_identifier()))
    except oauth_service.ConsentError as e:
        raise HTTPException(status_code=409, detail=str(e))

