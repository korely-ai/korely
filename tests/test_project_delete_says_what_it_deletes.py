"""Deleting a project without `erase` moves what it remembered to Default, and
deletes its webhook endpoints: their foreign key is ON DELETE CASCADE
(db/models.AgentWebhookEndpoint.project_id; SET NULL would turn one into an
endpoint of every project), and the deliveries go with their endpoint.

The dashboard's dialog said everything the project held moved to Default and
"Nothing is erased", while the endpoints, their signing secrets and every
delivery still waiting went (audit of 18 Oct 2026). The dialog now
names them on both roads, and this file holds the code to what it says.
"""
from pathlib import Path

import pytest

from korely_agent.db.models import AgentWebhookDelivery, AgentWebhookEndpoint
from korely_agent.services import projects as project_service

ROOT = Path(__file__).resolve().parent.parent
DIALOG = ROOT / "dashboard" / "src" / "components" / "DeleteProjectDialog.tsx"


def _endpoint(db, tenant, project=None):
    e = AgentWebhookEndpoint(tenant_id=tenant.id, url="https://hooks.example.com/k",
                             secret="whsec_" + "a" * 32, events=["*"],
                             project_id=project.id if project is not None else None)
    db.add(e)
    db.flush()
    db.add(AgentWebhookDelivery(tenant_id=tenant.id, endpoint_id=e.id,
                                event_type="memory.created", payload={"event": "memory.created"}))
    db.commit()
    return e.id


@pytest.mark.parametrize("erase", [False, True])
def test_a_deleted_project_takes_its_endpoints_and_their_deliveries(db, tenant, erase):
    project_service.get_or_create_default_project(db, tenant.id)
    gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
    mine = _endpoint(db, tenant, gamma)
    everyones = _endpoint(db, tenant)

    project_service.delete_project(db, tenant.id, gamma, erase=erase)

    db.expire_all()
    left = {e.id: e.project_id for e in db.query(AgentWebhookEndpoint)
            .filter(AgentWebhookEndpoint.tenant_id == tenant.id)}
    assert left == {everyones: None}
    pending = {d.endpoint_id for d in db.query(AgentWebhookDelivery)
               .filter(AgentWebhookDelivery.tenant_id == tenant.id)}
    assert pending == {everyones}
    assert mine not in pending


def test_the_dialog_names_the_webhooks_on_both_roads():
    text = " ".join(DIALOG.read_text(encoding="utf-8").split())
    move, erase = text.split('Erase its data instead', 1)
    assert "Nothing is erased." not in text
    for step in (move, erase):
        assert "webhook endpoints" in step
        assert "signing secrets" in step
