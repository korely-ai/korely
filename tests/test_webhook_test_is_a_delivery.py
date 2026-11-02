"""The Webhooks page's Send a test posts what a delivery is (audit of 17 Oct
2026): it sent {id, type: "ping", test, created_at, data} while every
real delivery is {event, data, project_id}, so a receiver that decides on
`event`, the key of the page's own example, saw nothing it knew and the test
failed where the deliveries went through.
"""
import uuid

import pytest

from korely_agent.db.models import AgentProject, AgentWebhookDelivery
from korely_agent.services import webhooks


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


@pytest.fixture
def endpoint(db, tenant, monkeypatch):
    monkeypatch.setattr(webhooks, "check_url", lambda u: u.strip())
    p = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="Staging", slug="staging")
    db.add(p)
    db.commit()
    return webhooks.create_endpoint(db, tenant.id, url="https://example.com/h",
                                    events=["*"], project_id=p.id)


def test_the_test_is_a_deliverys_envelope_with_ping(db, tenant, endpoint, monkeypatch):
    webhooks.emit_event(db, tenant.id, "fact.invalidated", {"fact_id": None},
                        project_id=endpoint.project_id, commit=True)
    real = db.query(AgentWebhookDelivery).one().payload

    sent = {}
    monkeypatch.setattr(webhooks, "_deliver",
                        lambda url, secret, did, payload: (sent.update(payload), (True, 200, None))[1])
    out = webhooks.send_test(db, tenant.id, endpoint.id)

    assert out["delivered"] is True
    assert set(sent) == set(real) | {"test"}, (sent, real)
    assert sent["event"] == webhooks.TEST_EVENT and sent["test"] is True
    assert webhooks.TEST_EVENT not in webhooks.VALID_EVENTS, \
        "a test must never be an event somebody's handler acts on"
    assert sent["project_id"] == real["project_id"] == str(endpoint.project_id)


def test_the_page_and_the_readme_say_so():
    from tests import docs_text

    page = (ROOT / "dashboard/src/pages/WebhooksPage.tsx").read_text()
    assert '"event":"ping"' in page and '"event": "ping"' in docs_text.text()
