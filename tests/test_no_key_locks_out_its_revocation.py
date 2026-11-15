"""No key of an account can keep its operator from revoking it.

Audit of 20 Oct 2026: every door of an account counted in one bucket per
(account, window), the operator's signed-in /admin included, and a refused
request still added to every window. Any live key of the account, a leaked
read-only one confined to one end user, past the ceiling made the operator's
POST /admin/keys/{id}/revoke answer 429, and on the Cloud the day window kept
the dashboard refused until midnight, the key live.

Now a signed-in person counts in the control plane's own bucket
(rate_limit.CONTROL_PLANE), and a refused request spends no window.
"""
import pytest
from fastapi.testclient import TestClient

from korely_agent.core.extensions import hooks
from korely_agent.db.models import AgentApiKey, AgentRateLimit
from korely_agent.services import keys as key_service
from korely_agent.services import operators as ops
from korely_agent.services.rate_limit import RateLimited, check_and_increment

# The Cloud plan's shape, scaled down: minute, hour and day.
WINDOWS = {"minute": 4, "hour": 50, "day": 8}


@pytest.fixture
def plan_windows(monkeypatch):
    monkeypatch.setattr(hooks, "rate_limits", lambda db, tenant_id: dict(WINDOWS))


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


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_a_flooding_key_does_not_keep_the_operator_from_revoking_it(db, tenant, client,
                                                                    plan_windows):
    _op, password = ops.create(db, tenant_id=tenant.id, username="anna")
    _s, session = ops.login(db, username="anna", password=password, ip="198.51.100.7")
    row, key = key_service.create_api_key(db, tenant_id=tenant.id, name="leaked",
                                          scopes=["memories:read"], end_users=["anna"])
    answers = [client.get("/v1/ping", headers=_bearer(key)).status_code for _ in range(12)]
    assert answers.count(200) == WINDOWS["minute"] and answers.count(429) == 8
    r = client.post(f"/admin/keys/{row.id}/revoke", headers=_bearer(session))
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(AgentApiKey, row.id).revoked_at is not None
    assert client.get("/v1/ping", headers=_bearer(key)).status_code == 401


def test_a_refused_request_spends_no_window(db, tenant, plan_windows):
    for _ in range(WINDOWS["minute"]):
        check_and_increment(db, tenant.id)
    for _ in range(10):
        with pytest.raises(RateLimited):
            check_and_increment(db, tenant.id)
    counts = {r.window_type: r.count for r in db.query(AgentRateLimit)
              .filter(AgentRateLimit.tenant_id == tenant.id)}
    assert counts == {w: WINDOWS["minute"] for w in WINDOWS}, counts


def test_an_admin_key_still_counts_in_the_accounts_bucket(db, tenant, client, plan_windows):
    _row, admin = key_service.create_api_key(db, tenant_id=tenant.id, name="admin",
                                             scopes=[key_service.ADMIN_SCOPE])
    _r2, other = key_service.create_api_key(db, tenant_id=tenant.id, name="r",
                                            scopes=["memories:read"])
    for _ in range(WINDOWS["minute"]):
        assert client.get("/v1/ping", headers=_bearer(other)).status_code == 200
    assert client.get("/admin/keys", headers=_bearer(admin)).status_code == 429
