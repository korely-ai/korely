"""OAuth for the MCP: ChatGPT and Claude connect without an API key.

Added on 16 Oct 2026. ChatGPT cannot present a key to a remote MCP server
("OAuth 2.1 or no
authentication", developers.openai.com/apps-sdk/build/auth) and Claude.ai's
connectors sign in the same way (claude.com/docs/connectors/building/
authentication). The MCP specification's authorization section (2025-11-25
and 2026-07-28) asks for the authorization code flow with PKCE (S256),
authorization server metadata (RFC 8414), dynamic client registration
(RFC 7591; client ID metadata documents are the newer way, not yet in the
SDK), and refresh tokens that rotate for public clients.

This install is its own authorization server, through the routes of the MCP
SDK it already runs (`mcp.server.auth.routes.create_auth_routes`); this module
is the provider those routes call, and the consent the dashboard shows.

- /register stores the client (`oauth_clients`).
- /authorize stores a request (`oauth_grants`, kind `request`) and sends the
  browser to the dashboard's consent page, behind the login that already
  exists (an operator session; on the Cloud, the account's).
- The person picks a project and the permissions; `approve` turns the request
  into an authorization code (single use, ten minutes) and sends the browser
  back to the client.
- /token turns the code into an access token that IS a Korely API key of that
  project, with those scopes and an hour of life (`oauth_client_id` set on the
  row), plus a refresh token (90 days, kept hashed). A refresh gives the same
  key row a new secret (`keys.reissue_secret`) and rotates the refresh token:
  one row per connected app in the API Keys page, revoked there like any key.

Each code and refresh token is spent once, by a compare-and-set (`_spend`), and
one presented again after its use revokes the key it gave and every grant of
that key (`_revoke_family`): a copy is in somebody else's hands (RFC 6749
section 4.1.2 for codes, RFC 9700 section 4.14.2 for refresh tokens).

The MCP door keeps verifying keys as it does today (`KorelyAgentKeyVerifier`):
the token is a key, so nothing there changes, and keys minted by hand keep
working on the same address.
"""
from __future__ import annotations

import json
import logging
import secrets
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import exists, or_, text
from sqlalchemy.orm import Session

from ..core.threads import admission_off_the_loop
from ..core.times import aware
from ..db.bounded import STREAM_ROWS
from ..db.models import AgentApiKey, OAuthClient, OAuthGrant
from . import audit as audit_service
from . import keys as key_service
from . import limits
from . import projects as projects_service
from . import provider_key
from .memory_core import log_safe

logger = logging.getLogger(__name__)

# The permissions a client can be given: a key's own scopes, never `admin`.
OFFERED_SCOPES = [s for s in key_service.KNOWN_SCOPES if s != key_service.ADMIN_SCOPE]
# Asks for a refresh token (OpenID Connect's name; Claude appends it when the
# metadata lists it, ChatGPT may lose access without it). Not a key scope.
OFFLINE = "offline_access"
VALID_SCOPES = OFFERED_SCOPES + [OFFLINE]

ACCESS_SECONDS = 3600
REFRESH_DAYS = 90
CODE_SECONDS = 600
REQUEST_SECONDS = 900
# The column's size; a PKCE S256 challenge is 43 characters (RFC 7636 4.2).
CHALLENGE_MAX = 128
# What a registration and a connection request may keep in the database
# (audit of 17 Oct 2026): both routes are open to anyone, and the hourly
# limit counts rows, not bytes, so one network kept ~440 MiB an hour with
# 4 MiB redirect URIs and 2 MiB `state`s. A person's app registers one or two
# addresses; `state` is the client's opaque round-trip value (RFC 6749
# section 4.1.1), tens of characters for ChatGPT and Claude.
REDIRECT_URIS_MAX = 5
REDIRECT_URI_MAX = 2048
CLIENT_INFO_MAX_BYTES = 8192
STATE_MAX = 2048
# The audit trail's actor for what a connected app does with its grant.
ACTOR = "oauth"
# RFC 8252 section 7.3: a native app listens on the loopback interface.
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
# The network of the request being served (api/oauth.py sets it before the
# SDK's handler runs; the provider's work runs in a thread that copies it).
NETWORK: ContextVar[Optional[str]] = ContextVar("oauth_network", default=None)


# How often one network may register an app, and start a connection, in an
# hour. Not settings (the surface stays small, tests/test_config.py): the
# same for every install, since a person connects a handful of apps and
# reconnects one now and then; a network that registers twenty in an hour
# is a script.
PER_HOUR = {"client": 20, "request": 60}
# The window `too_many` counts in. A row's network is needed for that and
# nothing else, so the worker clears it once the window is past
# (`purge_expired`; audit of 17 Oct 2026: it was kept for as long as the
# client stayed connected, which for Claude Code or Cursor is the person's
# own address).
NETWORK_WINDOW = timedelta(hours=1)


class TooMany(Exception):
    """A network past its hour's allowance, found where the row is written
    (`_within_allowance`); api/oauth.py answers it with its 429."""


def _counted(db: Session, kind: str, network: str) -> int:
    since = _now() - NETWORK_WINDOW
    if kind == "client":
        q = db.query(OAuthClient).filter(OAuthClient.network == network,
                                         OAuthClient.created_at >= since)
    else:
        q = db.query(OAuthGrant).filter(OAuthGrant.kind == "request", OAuthGrant.network == network,
                                        OAuthGrant.created_at >= since)
    return q.count()


def too_many(kind: str, network: Optional[str]) -> bool:
    """Whether this network has already registered (`client`) or asked
    (`request`) as often as an hour allows (`PER_HOUR`): the rows of the
    last hour are counted, as the hosted sign-up counts its accounts
    (hosted/signup.py). A caller with no address is not counted.

    The door's early answer, before the SDK parses anything. It decides
    nothing on its own: requests in flight together all read the same count,
    and a burst from one address wrote forty clients past a cap of twenty
    (audit of 18 Oct 2026). The count that decides is taken again
    where the row is written, under a lock (`_within_allowance`)."""
    if not network:
        return False
    db = _session()
    try:
        return _counted(db, kind, network) >= PER_HOUR[kind]
    finally:
        db.close()


def _within_allowance(db: Session, kind: str, network: Optional[str]) -> bool:
    """The count that decides, in the transaction that adds the row: one
    request of a network at a time per kind, under a transaction-scoped
    advisory lock that the insert's commit releases, so the next one counts
    this row (as hosted/signup.create counts its accounts)."""
    if not network:
        return True
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                   {"k": f"oauth_{kind}:{network}"})
    return _counted(db, kind, network) < PER_HOUR[kind]


def redirect_allowed(uri: str) -> bool:
    """Where an authorization answer may be sent. https; http only to this
    machine's loopback (RFC 8252 section 7.3); or an app's private-use scheme,
    a domain it owns reversed, `com.example.app:/cb` (section 7.1). Never a
    scheme a browser runs inside the page: the consent page navigates to this
    address, and a `javascript:` one would run in the dashboard. Never one
    with a user in it either (`https://claude.ai@evil.example/cb`): the
    consent page names where the answer goes, and such an address reads as
    one host and goes to another (audit of 19 Oct 2026). Checked again
    when the person answers, so a client registered with one before is
    refused there."""
    u = urlparse(uri or "")
    scheme = u.scheme.lower()
    if "@" in u.netloc:
        return False
    if scheme == "https":
        return bool(u.hostname)
    if scheme == "http":
        return u.hostname in _LOOPBACK
    return "." in scheme


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session() -> Session:
    from ..db import get_sessionmaker

    return get_sessionmaker()()


def _client_name(info: dict) -> str:
    return (info.get("client_name") or "an app").strip()[:60]


def _live(grant: Optional[OAuthGrant]) -> Optional[OAuthGrant]:
    """The grant when it is unused and in time, else None."""
    if grant is None or grant.used_at is not None or aware(grant.expires_at) <= _now():
        return None
    return grant


def _key_scopes(scopes) -> list:
    return [s for s in (scopes or []) if s in OFFERED_SCOPES]


def _spend(db: Session, g: OAuthGrant) -> bool:
    """Use the grant once. A compare-and-set on `used_at`, as the engine's
    claims are (memory_core.worker.close_the_claim): of two requests
    racing with one code or refresh token, one wins and the other is refused,
    where a read and a write would let both through."""
    n = (db.query(OAuthGrant)
           .filter(OAuthGrant.id == g.id, OAuthGrant.used_at.is_(None))
           .update({OAuthGrant.used_at: _now()}, synchronize_session=False))
    db.commit()
    return n == 1


def _grant(db: Session, token: str, kind: str) -> Optional[OAuthGrant]:
    return (db.query(OAuthGrant)
              .filter(OAuthGrant.token_hash == key_service.hash_key(token), OAuthGrant.kind == kind)
              .first())


def _live_project(db: Session, tenant_id, project_id):
    """The project a grant writes into, while it is there to write into: an
    archived project is hidden, and a refresh must not bring an app back into
    it (projects.archive_project refuses while live keys exist). Read under
    the project's lock, the one archiving takes, held until the key or the
    code is committed (`projects.get_project(lock=True)`)."""
    project = projects_service.get_project(db, tenant_id, project_id, lock=True)
    return project if project is not None and project.archived_at is None else None


def _audit_key(db: Session, row: AgentApiKey, action: str, **meta) -> None:
    audit_service.record(db, row.tenant_id, action, actor=ACTOR, target_id=str(row.id),
                         meta={"name": row.name, "oauth_client": row.oauth_client_id, **meta},
                         project_id=row.project_id)


def _revoke_family(db: Session, tenant_id, key_id, *, reason: str) -> None:
    """The key a grant gave, and every grant of that key, ended: revoked from
    the app (RFC 7009) or a code or refresh token presented after its use."""
    row = db.get(AgentApiKey, key_id)
    if row is not None and key_service.revoke_api_key(db, key_id=key_id, tenant_id=tenant_id):
        _audit_key(db, row, audit_service.KEY_REVOKE, reason=reason)
    (db.query(OAuthGrant)
       .filter(OAuthGrant.key_id == key_id, OAuthGrant.used_at.is_(None))
       .update({OAuthGrant.used_at: _now()}, synchronize_session=False))
    db.commit()


def _replayed(db: Session, g: Optional[OAuthGrant]) -> bool:
    """A code or refresh token presented after its use: whoever has a copy,
    the client or a thief, loses the key it gave (RFC 6749 section 4.1.2,
    RFC 9700 section 4.14.2: the server cannot tell which of the two it is)."""
    if g is None or g.used_at is None or g.key_id is None:
        return False
    logger.warning("[oauth] a used %s of client %s was presented again: its key is revoked",
                   g.kind, g.client_id)
    _revoke_family(db, g.tenant_id, g.key_id, reason=f"{g.kind} presented again")
    return True


class KorelyOAuthProvider:
    """`mcp.server.auth.provider.OAuthAuthorizationServerProvider` over this
    install's database. The SDK awaits each method; the database work runs
    in a thread of the admission pool, as the MCP's key check does
    (`core.threads.admission_off_the_loop`): it is how a client gets in, and
    on the pool of the MCP tools' bodies it waited behind them."""

    def __init__(self, *, issuer: str, resource: str, consent_url: str):
        self.issuer = issuer.rstrip("/")
        self.resource = resource.rstrip("/")
        self.consent_url = consent_url

    # ── clients (RFC 7591) ───────────────────────────────────────────────
    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        return await admission_off_the_loop(self._get_client, client_id)

    def _get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        # The SDK's /token and /revoke hand the caller's form value here, and
        # a NUL byte reached the driver: 500 (audit of 20 Oct 2026).
        # Such an id names no client, by the rule of every door.
        if limits.nul_problem("client_id", client_id):
            return None
        db = _session()
        try:
            row = db.get(OAuthClient, client_id)
            if row is None:
                return None
            info = dict(row.info)
            if row.secret_encrypted:
                try:
                    info["client_secret"] = provider_key.decrypt_secret(row.secret_encrypted)
                except Exception as e:
                    # SECRET_KEY changed or removed since it registered: a
                    # client this server cannot authenticate is an unknown one
                    # (it registers again), never a 500 at every request.
                    logger.warning("[oauth] the secret of client %s does not decrypt: %s",
                                   client_id, log_safe.error(e))
                    return None
            return OAuthClientInformationFull.model_validate(info)
        finally:
            db.close()

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await admission_off_the_loop(self._register_client, client_info)

    def _register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if len(uris) > REDIRECT_URIS_MAX or any(len(u) > REDIRECT_URI_MAX for u in uris):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description=f"at most {REDIRECT_URIS_MAX} redirect URIs, each at most "
                                  f"{REDIRECT_URI_MAX} characters")
        if not all(redirect_allowed(u) for u in uris):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="a redirect URI is https, http to the loopback, or the app's own "
                                  "reverse-domain scheme (RFC 8252), with no user part")
        info = client_info.model_dump(mode="json", exclude_none=True)
        # The metadata by the rule of every door (limits.nul_problem): the
        # name is shown on the consent page and kept as given.
        problem = limits.nul_problem("", info)
        if problem:
            raise RegistrationError(error="invalid_client_metadata", error_description=problem)
        if len(json.dumps(info).encode("utf-8")) > CLIENT_INFO_MAX_BYTES:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description=f"the client metadata is larger than {CLIENT_INFO_MAX_BYTES} bytes")
        secret = info.pop("client_secret", None)
        try:
            sealed = provider_key.encrypt_secret(secret) if secret else None
        except provider_key.NoEncryptionKey:
            # No SECRET_KEY: nowhere safe to keep a client secret. A public
            # client (PKCE, `none`), which ChatGPT and Claude are, needs none.
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="this server keeps no client secrets: register with "
                                  "token_endpoint_auth_method none")
        db = _session()
        try:
            if not _within_allowance(db, "client", NETWORK.get()):
                db.rollback()
                raise TooMany()
            db.add(OAuthClient(client_id=client_info.client_id, info=info, secret_encrypted=sealed,
                               network=NETWORK.get()))
            db.commit()
        finally:
            db.close()

    # ── authorize: a request waits for the person's consent ─────────────
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # What the request keeps, by the rule of every door (limits.
        # nul_problem): POST /authorize's form is the SDK's, read by no
        # Checked and no middleware, and a NUL in `state` or
        # `code_challenge` reached the driver as server_error (audit of 27 Oct
        # 2026).
        problem = limits.nul_problem("", params)
        if problem:
            raise AuthorizeError(error="invalid_request", error_description=problem)
        if params.resource and params.resource.rstrip("/") not in (self.resource, self.issuer):
            # The tokens issued here are for this server only (RFC 8707): a
            # token is never minted for an audience it could not be checked by.
            raise AuthorizeError(error="invalid_request",
                                 error_description=f"resource must be {self.resource}")
        if len(params.code_challenge or "") > CHALLENGE_MAX:
            raise AuthorizeError(error="invalid_request", error_description="code_challenge is too long")
        if len(params.state or "") > STATE_MAX:
            raise AuthorizeError(error="invalid_request",
                                 error_description=f"state is longer than {STATE_MAX} characters")
        if len(str(params.redirect_uri or "")) > REDIRECT_URI_MAX:
            raise AuthorizeError(error="invalid_request", error_description="redirect_uri is too long")
        return await admission_off_the_loop(self._authorize, client, params)

    def _authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # Each scope once (a repeated one says nothing more and was kept as sent).
        scopes = list(dict.fromkeys(params.scopes or (client.scope or " ".join(OFFERED_SCOPES)).split()))
        db = _session()
        try:
            if not _within_allowance(db, "request", NETWORK.get()):
                db.rollback()
                # The SDK turns any other exception here into server_error.
                raise AuthorizeError(error="temporarily_unavailable",
                                     error_description="too many requests from this network: "
                                                       "try again later")
            grant = OAuthGrant(kind="request", client_id=client.client_id, scopes=scopes,
                               resource=params.resource, redirect_uri=str(params.redirect_uri),
                               redirect_uri_explicit=params.redirect_uri_provided_explicitly,
                               code_challenge=params.code_challenge, state=params.state,
                               expires_at=_now() + timedelta(seconds=REQUEST_SECONDS),
                               network=NETWORK.get())
            db.add(grant)
            db.commit()
            return f"{self.consent_url}?request={grant.id}"
        finally:
            db.close()

    # ── codes ────────────────────────────────────────────────────────────
    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> Optional[AuthorizationCode]:
        return await admission_off_the_loop(self._load_code, client, authorization_code)

    def _load_code(self, client, code: str) -> Optional[AuthorizationCode]:
        db = _session()
        try:
            g = _grant(db, code, "code")
            if _replayed(db, g):
                return None
            g = _live(g)
            if g is None or g.client_id != client.client_id:
                return None
            return AuthorizationCode(
                code=code, scopes=list(g.scopes), expires_at=aware(g.expires_at).timestamp(),
                client_id=g.client_id, code_challenge=g.code_challenge, redirect_uri=g.redirect_uri,
                redirect_uri_provided_explicitly=g.redirect_uri_explicit, resource=g.resource,
                subject=str(g.tenant_id))
        finally:
            db.close()

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        return await admission_off_the_loop(self._exchange_code, client, authorization_code)

    def _exchange_code(self, client, ac: AuthorizationCode) -> OAuthToken:
        db = _session()
        try:
            found = _live(_grant(db, ac.code, "code"))
            # Spent under the row's lock, held until the key is written: an
            # end user's erasure locks the codes it rewrites
            # (end_user_places.rename_in_lists), so the list copied below is the
            # one it left, never the erased id (audit of 19 Oct 2026). Of two
            # exchanges racing with one code the second waits and finds it
            # used, as `_spend`'s compare-and-set has it.
            g = None if found is None else (
                db.query(OAuthGrant)
                  .filter(OAuthGrant.id == found.id, OAuthGrant.used_at.is_(None))
                  .populate_existing().with_for_update().first())
            if g is None:
                db.rollback()
                raise TokenError(error="invalid_grant", error_description="the code was used or expired")
            g.used_at = _now()
            if _live_project(db, g.tenant_id, g.project_id) is None:
                db.commit()
                raise TokenError(error="invalid_grant", error_description="the project is archived or gone")
            name = f"{_client_name(client.model_dump())} via OAuth ({uuid.uuid4().hex[:6]})"
            # An erased end user's pseudonym is nobody the key could see, and
            # no key's list may hold one: it goes, and the people left stay.
            # A code left with nobody was spent by the erasure.
            from .ids import ERASED_PREFIX

            end_users = None if g.end_users is None else [
                u for u in g.end_users if not u.startswith(ERASED_PREFIX)]
            row, access = key_service.create_api_key(
                db, tenant_id=g.tenant_id, name=name, scopes=_key_scopes(g.scopes),
                project_id=g.project_id, expires_at=_now() + timedelta(seconds=ACCESS_SECONDS),
                oauth_client_id=client.client_id, end_users=end_users)
            g.key_id = row.id  # a replay of this code finds the key to revoke
            db.commit()
            _audit_key(db, row, audit_service.KEY_CREATE)
            refresh = self._new_refresh(db, client.client_id, g, row.id)
            return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_SECONDS,
                              refresh_token=refresh, scope=" ".join(g.scopes))
        finally:
            db.close()

    def _new_refresh(self, db: Session, client_id: str, g: OAuthGrant, key_id) -> str:
        token = secrets.token_urlsafe(32)
        db.add(OAuthGrant(kind="refresh", token_hash=key_service.hash_key(token), client_id=client_id,
                          tenant_id=g.tenant_id, project_id=g.project_id, key_id=key_id,
                          scopes=list(g.scopes), resource=g.resource,
                          expires_at=_now() + timedelta(days=REFRESH_DAYS)))
        db.commit()
        return token

    # ── refresh tokens: rotated at every use ─────────────────────────────
    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> Optional[RefreshToken]:
        return await admission_off_the_loop(self._load_refresh, client, refresh_token)

    def _load_refresh(self, client, token: str) -> Optional[RefreshToken]:
        db = _session()
        try:
            g = _grant(db, token, "refresh")
            if _replayed(db, g):
                return None
            g = _live(g)
            if g is None or g.client_id != client.client_id:
                return None
            return RefreshToken(token=token, client_id=g.client_id, scopes=list(g.scopes),
                                expires_at=int(aware(g.expires_at).timestamp()),
                                resource=g.resource, subject=str(g.tenant_id))
        finally:
            db.close()

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken, scopes: list) -> OAuthToken:
        return await admission_off_the_loop(self._exchange_refresh, client, refresh_token, scopes)

    def _exchange_refresh(self, client, rt: RefreshToken, scopes: list) -> OAuthToken:
        db = _session()
        try:
            g = _live(_grant(db, rt.token, "refresh"))
            row = db.get(AgentApiKey, g.key_id) if g is not None and g.key_id else None
            if g is None or row is None or row.revoked_at is not None:
                raise TokenError(error="invalid_grant", error_description="the grant was revoked")
            if _live_project(db, row.tenant_id, row.project_id) is None:
                raise TokenError(error="invalid_grant",
                                 error_description="the project was archived: connect again")
            # RFC 6749 section 6: the new access token may be narrower than
            # the grant, the new refresh token keeps the grant's scope. The
            # access token is the key, so the key's scopes are what was asked.
            asked = [s for s in (scopes or g.scopes) if s in g.scopes]
            if not _key_scopes(asked):
                raise TokenError(error="invalid_scope", error_description="ask for memories:read or memories:write")
            if not _spend(db, g):
                raise TokenError(error="invalid_grant", error_description="the refresh token was already used")
            row.scopes = _key_scopes(asked)
            access = key_service.reissue_secret(db, row, expires_at=_now() + timedelta(seconds=ACCESS_SECONDS))
            refresh = self._new_refresh(db, client.client_id, g, row.id)
            return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_SECONDS,
                              refresh_token=refresh, scope=" ".join(asked))
        finally:
            db.close()

    # ── access tokens are keys ───────────────────────────────────────────
    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        return await admission_off_the_loop(self._load_access, token)

    def _load_access(self, token: str) -> Optional[AccessToken]:
        db = _session()
        try:
            row = key_service.verify_api_key(db, token)
            if row is None:
                return None
            exp = int(aware(row.expires_at).timestamp()) if row.expires_at else None
            return AccessToken(token=token, client_id=row.oauth_client_id or str(row.id),
                               scopes=list(row.scopes or []), expires_at=exp, resource=self.resource)
        finally:
            db.close()

    async def revoke_token(self, token) -> None:
        await admission_off_the_loop(self._revoke, token.token)

    def _revoke(self, token: str) -> None:
        """RFC 7009, the app disconnecting: a refresh token or an access token
        (the key) ends the whole grant. Only a key an app got through OAuth:
        one made by hand is revoked in API Keys."""
        db = _session()
        try:
            g = _grant(db, token, "refresh")
            if g is not None:
                if g.key_id is not None:
                    _revoke_family(db, g.tenant_id, g.key_id, reason="revoked by the app")
                else:
                    _spend(db, g)
                return
            row = key_service.verify_api_key(db, token)
            if row is not None and row.oauth_client_id:
                _revoke_family(db, row.tenant_id, row.id, reason="revoked by the app")
        finally:
            db.close()


# ── the consent page (api/oauth.py) ────────────────────────────────────────

class ConsentError(ValueError):
    """The request cannot be answered as it stands: it expired, was answered
    already, or its project is no longer there to write into (409)."""


class ConsentInvalid(ValueError):
    """The answer itself is refused: no permission chosen, or end users that
    are not a list a key can hold (422, as POST /admin/keys refuses them;
    audit of 17 Oct 2026: they were 409s, the status of an expired request)."""


def pending_request(db: Session, request_id) -> Optional[OAuthGrant]:
    try:
        rid = uuid.UUID(str(request_id))
    except ValueError:
        return None
    return _live(db.query(OAuthGrant).filter(OAuthGrant.id == rid,
                                             OAuthGrant.kind == "request").first())


def _where_it_returns(uri: Optional[str]) -> dict:
    """Who receives the answer, as two fields the consent page words apart:
    `redirect_host`, the web host a browser goes to (https, or http to the
    loopback; the netloc showed a user part as if it were the host, audit of
    19 Oct 2026), else None; `redirect_app_scheme`, for any other scheme, the scheme
    whose app on the person's device opens the address, else None.

    A private-use address is opened by whichever app claims its scheme (RFC
    8252 section 8.6), never by a web host: `ai.claude://claude.ai/cb` (its
    authority) and `claude.ai:/cb` (a dotted scheme spelled as a domain)
    were both shown as "claude.ai", the words of the real
    https://claude.ai/... callback (audit of 20 Oct 2026)."""
    u = urlparse(uri or "")
    scheme = u.scheme.lower()
    if scheme in ("https", "http") and u.hostname:
        host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
        return {"redirect_host": host + (f":{u.port}" if u.port else ""),
                "redirect_app_scheme": None}
    return {"redirect_host": None, "redirect_app_scheme": scheme or None}


def describe_request(db: Session, request_id) -> Optional[dict]:
    """What the consent page shows: who asks, for what, and where it returns."""
    g = pending_request(db, request_id)
    if g is None:
        return None
    client = db.get(OAuthClient, g.client_id)
    return {"client_name": _client_name(client.info if client else {}),
            **_where_it_returns(g.redirect_uri),
            "scopes": [s for s in g.scopes if s in OFFERED_SCOPES]}


def approve(db: Session, tenant_id, request_id, *, project_id, scopes: list, issuer: str,
            end_users=None) -> str:
    """The person said yes: an authorization code for the project and the
    scopes chosen (never more than asked), and the URL back to the client.
    `end_users` confines the app to those end users of the project
    (services/user_scope.py); None: the whole project."""
    g = pending_request(db, request_id)
    if g is None:
        raise ConsentError("this request expired or was already answered: start again from the app")
    project = _live_project(db, tenant_id, project_id)
    if project is None:
        raise ConsentError("no such project in this account")
    chosen = [s for s in scopes if s in OFFERED_SCOPES and s in g.scopes]
    if not chosen:
        raise ConsentInvalid("choose at least one permission")
    if OFFLINE in g.scopes:
        chosen.append(OFFLINE)
    from . import user_scope

    try:
        confined = user_scope.checked(end_users)
    except ValueError as e:
        raise ConsentInvalid(str(e))
    if not redirect_allowed(g.redirect_uri) or not _spend(db, g):
        raise ConsentError("this request expired or was already answered: start again from the app")
    code = secrets.token_urlsafe(32)
    db.add(OAuthGrant(kind="code", token_hash=key_service.hash_key(code), client_id=g.client_id,
                      tenant_id=tenant_id, project_id=project.id, scopes=chosen,
                      resource=g.resource, redirect_uri=g.redirect_uri,
                      redirect_uri_explicit=g.redirect_uri_explicit,
                      code_challenge=g.code_challenge, state=g.state, end_users=confined,
                      expires_at=_now() + timedelta(seconds=CODE_SECONDS)))
    db.commit()
    # RFC 9207: the issuer travels with the code (the MCP spec's SHOULD),
    # the same string as the metadata's `issuer` (compared as strings, 2.4).
    return construct_redirect_uri(g.redirect_uri, code=code, state=g.state, iss=issuer)


def deny(db: Session, request_id, *, issuer: str) -> str:
    g = pending_request(db, request_id)
    if g is None or not redirect_allowed(g.redirect_uri) or not _spend(db, g):
        raise ConsentError("this request expired or was already answered")
    return construct_redirect_uri(g.redirect_uri, error="access_denied",
                                  error_description="the user declined", state=g.state, iss=issuer)


def purge_expired(db: Session) -> int:
    """The worker's housekeeping (worker._purge), hourly.

    Grants a day past their time or their use: a replay comes within the
    hour of a refresh, and one after a day is refused as unknown all the
    same. The network of a client and of a request once the hour that
    counts it is past (`NETWORK_WINDOW`), as the hosted sign-up's network is
    cleared after its three days (hosted/cli.cleanup).

    Clients a day old with no live key and no grant waiting for one (a
    request or a code): /register and /authorize answer anyone, so what they
    write must not pile up, and a client whose key was revoked in API Keys
    is gone for good (it connects again with a new registration). Until the
    audit of 17 Oct 2026 a revoked key, which is kept as a row, held its
    client and the client's network for as long as the account existed.

    An app that stopped coming back holds a key nobody can renew: it expires
    every hour, and with no unused refresh token left (90 days) it never
    works again. A day past both, the key is revoked here, as the app's own
    disconnection would (`_revoke_family`), and its client goes with the
    rule above. A day, not at once: a refresh spends its token before the
    key gets its new hour."""
    now = _now()
    cutoff = now - timedelta(days=1)
    usable = exists().where(OAuthGrant.key_id == AgentApiKey.id, OAuthGrant.kind == "refresh",
                            OAuthGrant.used_at.is_(None), OAuthGrant.expires_at > now)
    abandoned = (db.query(AgentApiKey.tenant_id, AgentApiKey.id)
                   .filter(AgentApiKey.oauth_client_id.isnot(None), AgentApiKey.revoked_at.is_(None),
                           AgentApiKey.expires_at < cutoff, ~usable)
                   # A block a round: the next one takes the rest.
                   .order_by(AgentApiKey.expires_at, AgentApiKey.id)
                   .limit(STREAM_ROWS)
                   .all())
    for tenant_id, key_id in abandoned:
        _revoke_family(db, tenant_id, key_id, reason="the app did not renew it")
    n = len(abandoned)
    n += (db.query(OAuthGrant)
            .filter(or_(OAuthGrant.expires_at < cutoff, OAuthGrant.used_at < cutoff))
            .delete(synchronize_session=False))
    counted = now - NETWORK_WINDOW
    n += (db.query(OAuthClient)
            .filter(OAuthClient.network.isnot(None), OAuthClient.created_at < counted)
            .update({OAuthClient.network: None}, synchronize_session=False))
    n += (db.query(OAuthGrant)
            .filter(OAuthGrant.network.isnot(None), OAuthGrant.created_at < counted)
            .update({OAuthGrant.network: None}, synchronize_session=False))
    n += (db.query(OAuthClient)
            .filter(OAuthClient.created_at < cutoff,
                    # A request or a code not yet turned into a key: the
                    # person may be on the consent page, or the app at /token.
                    ~exists().where(OAuthGrant.client_id == OAuthClient.client_id,
                                    OAuthGrant.key_id.is_(None)),
                    ~exists().where(AgentApiKey.oauth_client_id == OAuthClient.client_id,
                                    AgentApiKey.revoked_at.is_(None)))
            .delete(synchronize_session=False))
    if n:
        db.commit()
    return int(n or 0)
