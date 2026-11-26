"""The provider and the models are the install's: on the hosted service no account touches them.

On an install of one's own the operator sets the model provider, its key and
the models from the screen, for every account in the install. On the hosted
service (KORELY_MODE=hosted) every customer is an account of one install, so
the same routes would let one customer's admin key switch the model, and the
key, of all the others. There the service sets them in its configuration and
the routes refuse (8 Oct 2026, the first safety of the one codebase). The
refusal is the mode's, so it holds with no hosted hook registered.
"""
from tests import cloud
import pytest
from fastapi.testclient import TestClient

from korely_agent import config
from korely_agent.core import extensions
from korely_agent.services import keys as key_service


# Every route that reads or changes what is the install's, with a body that
# would pass validation, so the refusal is the only thing that can stop it.
INSTALL_WIDE = [
    ("get", "/admin/provider", None),
    ("put", "/admin/provider", {"key": "AIzaSyFAKEfakefakefakefakefakefake1234"}),
    ("post", "/admin/provider/test", {"key": "AIzaSyFAKEfakefakefakefakefakefake1234"}),
    ("delete", "/admin/provider", None),
    ("get", "/admin/models", None),
    ("post", "/admin/models/openai_compatible/check", {}),
    ("put", "/admin/models/openai_compatible", {}),
    ("delete", "/admin/models", None),
]


@pytest.fixture
def mode(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-test")

    def set_(value):
        monkeypatch.setenv("KORELY_MODE", value)
        config.get_settings.cache_clear()
    yield set_
    config.get_settings.cache_clear()
    extensions.reset()


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("RUN_WORKER", "false")
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def admin_of(db, tenant):
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name="operator",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE],
        expires_in_days=None)
    return {"Authorization": f"Bearer {full}"}


def call(client, method, path, body, headers):
    return client.request(method.upper(), path, json=body, headers=headers)


@pytest.mark.cloud
@pytest.mark.parametrize("method,path,body", INSTALL_WIDE)
def test_an_account_of_the_service_is_refused(db, tenant, client, mode, method, path, body):
    mode("hosted")
    r = call(client, method, path, body, admin_of(db, tenant))
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "set_by_the_service"


def test_the_operator_of_an_install_of_ones_own_still_reads_them(db, tenant, client, mode):
    mode("self_hosted")
    headers = admin_of(db, tenant)
    assert client.get("/admin/provider", headers=headers).status_code == 200
    assert client.get("/admin/models", headers=headers).status_code == 200


@pytest.mark.cloud
def test_a_hook_that_answers_nothing_does_not_open_it(db, tenant, client, mode):
    mode("hosted")
    extensions.register("can_change_install_settings", lambda db, principal: None)
    assert client.get("/admin/provider", headers=admin_of(db, tenant)).status_code == 403


@pytest.mark.cloud
def test_the_service_may_let_in_a_principal_it_trusts(db, tenant, client, mode):
    mode("hosted")
    seen = []

    def trusted(db, principal):
        seen.append(principal.tenant_id)
        return True
    extensions.register("can_change_install_settings", trusted)
    assert client.get("/admin/provider", headers=admin_of(db, tenant)).status_code == 200
    assert seen == [tenant.id]


@pytest.mark.cloud
def test_without_a_credential_it_is_still_a_401(client, mode):
    mode("hosted")
    assert client.get("/admin/provider").status_code == 401


# ── what the service does not have ──────────────────────────────────────────

@pytest.mark.cloud
def test_no_password_login_on_the_service(client, mode):
    """The service signs people in with their provider (/hosted/session); an
    account's operator has no password. On an install of one's own the route
    is there and answers a wrong password with its 401."""
    body = {"username": "someone", "password": "not-the-password"}
    mode("self_hosted")
    assert client.post("/admin/session", json=body).status_code == 401
    mode("hosted")
    r = client.post("/admin/session", json=body)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


@pytest.mark.cloud
def test_no_model_spend_page_on_the_service(db, tenant, client, mode):
    """/admin/usage prices every model call at LLM_RATES: the operator's bill
    on an install of one's own, the vendor's cost on the service."""
    headers = admin_of(db, tenant)
    mode("self_hosted")
    assert client.get("/admin/usage", headers=headers).status_code == 200
    assert client.get("/admin/usage/export", headers=headers).status_code == 200
    mode("hosted")
    assert client.get("/admin/usage", headers=headers).status_code == 404
    assert client.get("/admin/usage/export", headers=headers).status_code == 404


@pytest.mark.cloud
def test_the_trusted_header_stops_the_service_at_start(monkeypatch):
    monkeypatch.setenv("TRUST_AUTH_HEADER", "X-Forwarded-User")
    monkeypatch.setenv("KORELY_MODE", "self_hosted")
    config.get_settings.cache_clear()
    assert config.get_settings().trust_auth_header == "X-Forwarded-User"
    monkeypatch.setenv("KORELY_MODE", "hosted")
    config.get_settings.cache_clear()
    with pytest.raises(Exception, match="TRUST_AUTH_HEADER"):
        config.get_settings()
    config.get_settings.cache_clear()


@pytest.mark.cloud
def test_the_facts_export_stops_the_service_at_start(monkeypatch, tmp_path):
    monkeypatch.setenv("FACTS_EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("KORELY_MODE", "self_hosted")
    config.get_settings.cache_clear()
    assert config.get_settings().facts_export_dir == str(tmp_path)
    monkeypatch.setenv("KORELY_MODE", "hosted")
    config.get_settings.cache_clear()
    with pytest.raises(Exception, match="FACTS_EXPORT_DIR"):
        config.get_settings()
    config.get_settings.cache_clear()


@pytest.mark.cloud
@pytest.mark.parametrize("variable", ["WEBHOOK_ALLOW_PRIVATE_URLS", "WEBHOOK_ALLOW_HTTP"])
def test_the_webhook_escape_hatches_stop_the_service_at_start(monkeypatch, variable):
    """For a local test of an install of one's own; on the service any account
    registers webhooks, and either would reach the service's own network or
    send events in clear (12 Oct 2026)."""
    monkeypatch.setenv(variable, "true")
    monkeypatch.setenv("KORELY_MODE", "self_hosted")
    config.get_settings.cache_clear()
    assert getattr(config.get_settings(), variable.lower()) is True
    monkeypatch.setenv("KORELY_MODE", "hosted")
    config.get_settings.cache_clear()
    with pytest.raises(Exception, match="WEBHOOK_ALLOW"):
        config.get_settings()
    config.get_settings.cache_clear()


# ── an account's admin administers its own account ──────────────────────────

# The routes that read nothing an account owns: the scopes a key may carry,
# the lifetimes a key may have, the webhook events there are, and the
# operator's own sessions.
_OWN_NOTHING = {"list_scopes", "key_lifetimes", "webhook_events", "log_out", "log_out_everywhere",
                # The engine's relation families, the same for every account.
                "fact_families",
                # Signing in finds the operator by name, before there is an asker.
                "log_in",
                # An OAuth request belongs to no account until the person
                # signed in answers it (oauth_grants.tenant_id is empty until
                # consent), and its id is unguessable: the consent page reads
                # it, and the yes binds it to the asker's account.
                "show_request", "deny",
                # A part of the product's (core/extensions.Part).
                *cloud.of_the_parts("OWN_NOTHING", tuple)}

# Where each router names the asker: an API key on /admin, the person signed
# in on /admin/oauth.
_ADMIN_ROUTERS = {"korely_agent/api/admin.py": "key", "korely_agent/api/oauth.py": "principal",
                  # A part of the product's (core/extensions.Part).
                  **cloud.of_the_parts("ADMIN_ROUTERS")}


def test_every_admin_route_reads_the_tenant_of_who_asks():
    """On the service every customer is an account of one install, so an
    admin route that does not narrow to the tenant of who asks hands one
    customer another's data. Read from the code (8 Oct 2026): each route
    reads the asker's `tenant_id` or goes through the wall that does
    (`_owned`, `_project`, audit of 17 Oct 2026), or is the install's (closed
    on the service), or reads nothing an account owns. A new route that is
    none of these fails. Async routes count, and so does the /admin/oauth
    router; a `tenant_id` read from anywhere but the asker does not (audit of
    17 Oct 2026: the guard skipped async routes, took any `tenant_id` and
    never read api/oauth.py)."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    loose, seen = [], 0
    # A route may hand its work to a shared body (the search's,
    # api/admin.answer_search, which a part's route calls too): read with it.
    shared = {}
    for rel in _ADMIN_ROUTERS:
        src = (root / rel).read_text()
        shared.update({f.name: ast.get_source_segment(src, f) for f in ast.parse(src).body
                       if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and not f.decorator_list})
    for rel, asker in _ADMIN_ROUTERS.items():
        src = (root / rel).read_text()
        for f in ast.parse(src).body:
            if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) or not f.decorator_list:
                continue
            deco = ast.get_source_segment(src, f.decorator_list[0])
            if not deco.startswith(("router.", "public.")):
                continue
            seen += 1
            body = ast.get_source_segment(src, f)
            import re

            body += "".join(shared.get(n, "") for n in re.findall(r"\bawait (?:\w+\.)?(\w+)\(", body))
            walled = f"_owned(db, {asker}," in body or f"_project(db, {asker}," in body
            if (f"{asker}.tenant_id" in body or walled or "Depends(_install_wide)" in body
                    or f.name in _OWN_NOTHING):
                continue
            loose.append(f"{rel}:{f.name}")
    assert seen > 50, "found almost no admin routes; the scan is broken"
    assert loose == [], f"admin routes that do not narrow to the asker's tenant: {loose}"


@pytest.mark.cloud
def test_one_accounts_admin_cannot_read_anothers_memory(db, client, mode):
    import uuid

    from korely_agent.db.models import Tenant
    from tests.conftest import seeded_project_id

    mode("hosted")
    mine, theirs = (Tenant(id=uuid.uuid4(), name=n) for n in ("Mine", "Theirs"))
    db.add_all([mine, theirs])
    db.commit()
    seeded_project_id(db, theirs.id)
    _r, their_key = key_service.create_api_key(db, tenant_id=theirs.id, name="agent",
                                               expires_in_days=None)
    r = client.post("/v1/memories", json={"content": "I moved to Lisbon.", "user_id": "maria"},
                    headers={"Authorization": f"Bearer {their_key}"})
    assert r.status_code in (200, 201, 202), r.text
    memory_id = (r.json().get("id") or r.json()["memories"][0]["id"])
    r = client.get(f"/admin/memories/{memory_id}", headers=admin_of(db, mine))
    assert r.status_code == 404


# ── an install of one's own with several accounts (audit of 17 Oct 2026) ───

def _second_account(db):
    import uuid as _uuid

    from korely_agent.db.models import Tenant

    t = Tenant(id=_uuid.uuid4(), name="Second client")
    db.add(t)
    db.commit()
    return t


def _signed_in(client, db, tenant, username, *, install_admin=False):
    from korely_agent.services import operators

    row, password = operators.create(db, tenant_id=tenant.id, username=username)
    row.install_admin = install_admin
    db.commit()
    r = client.post("/admin/session", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.mark.parametrize("method,path,body", INSTALL_WIDE)
def test_another_accounts_admin_is_refused_on_a_shared_install(db, tenant, client, mode,
                                                              method, path, body):
    mode("self_hosted")
    other = _second_account(db)
    for headers in (admin_of(db, other), _signed_in(client, db, other, "bea")):
        r = call(client, method, path, body, headers)
        assert r.status_code == 403, r.text
        assert r.json()["code"] == "install_admin_only"


def test_the_named_person_sets_them_and_every_account_reads_it(db, tenant, client, mode):
    from korely_agent.db.models import AuditEvent

    mode("self_hosted")
    other = _second_account(db)
    headers = _signed_in(client, db, tenant, "anna", install_admin=True)
    assert client.get("/admin/provider", headers=headers).status_code == 200
    assert client.delete("/admin/provider", headers=headers).status_code == 200
    db.expire_all()
    trail = {(e.tenant_id, e.target_id) for e in db.query(AuditEvent)
             .filter(AuditEvent.action == "setting_change", AuditEvent.target_id == "provider")}
    assert trail == {(tenant.id, "provider"), (other.id, "provider")}
    # Not even the named person's admin key: a program is not a person.
    assert client.get("/admin/provider", headers=admin_of(db, tenant)).status_code == 403
