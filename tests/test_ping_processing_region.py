"""/v1/ping says where a memory is read by a model (2026-10-07).

`region` is where the data is (what the operator declared). Which model reads
a memory, and where, is the writer this install uses: "eu" (Mistral, Scaleway)
or "global" (Gemini, OpenAI, Claude, Grok, OpenRouter), null with no writer.
The hosted product answers the same `processing_region` for the project of a
key (2026-10-07): the SDKs said "data stored and processed in the
EU" for every project, which was false for a Global one, and now read one
field on both products.
"""
import uuid

import pytest

from korely_agent.services import keys as key_service
from korely_agent.services import model_choice



@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    from korely_agent import config
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    config.get_settings.cache_clear()
    try:
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()
    finally:
        config.get_settings.cache_clear()


@pytest.fixture
def head(db, tenant):
    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}")
    return {"Authorization": f"Bearer {full}"}


def chosen(monkeypatch, provider):
    p = model_choice.PROVIDERS[provider]
    monkeypatch.setattr("korely_agent.core.llm.screen_writers",
                        lambda: {"default": p.region, p.region: (p.id, p.base, "k", "m")})


def ping(client, head):
    r = client.get("/v1/ping", headers=head)
    assert r.status_code == 200, r.text
    return r.json()


def test_no_writer_says_null(client, head, monkeypatch):
    monkeypatch.setattr("korely_agent.core.llm.screen_writers", lambda: {})
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: None)
    body = ping(client, head)
    assert body["processing_region"] is None
    assert body["region"] == "eu", "the operator's REGION label, unchanged"


@pytest.mark.parametrize("provider, region", [("scaleway", "eu"), ("mistral", "eu"),
                                              ("google", "global"), ("openrouter", "global")])
def test_the_writer_chosen_at_first_login(client, head, monkeypatch, provider, region):
    chosen(monkeypatch, provider)
    assert ping(client, head)["processing_region"] == region


def test_a_google_key_in_the_file_is_global(client, head, monkeypatch):
    monkeypatch.setattr("korely_agent.core.llm.screen_writers", lambda: {})
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "AIza-test")
    assert ping(client, head)["processing_region"] == "global"


def test_the_file_s_openai_compatible_writer_by_its_address(client, head, monkeypatch):
    """Through /v1/ping (services/regions.processing), not through a function
    of its own: model_choice.processing_region answered this and no route
    called it (audit of 18 Oct 2026)."""
    from korely_agent.config import get_settings

    monkeypatch.setattr("korely_agent.core.llm.screen_writers", lambda: {})
    s = get_settings()
    monkeypatch.setattr(s, "extraction_provider", "mistral")
    monkeypatch.setattr(s, "mistral_api_key", "secret")
    monkeypatch.setattr(s, "mistral_api_base", "https://api.scaleway.ai/v1/")
    assert ping(client, head)["processing_region"] == "eu"
    monkeypatch.setattr(s, "mistral_api_base", "https://llm.example.com/v1")
    assert ping(client, head)["processing_region"] is None, "an address that is no provider's"


def test_a_ping_is_in_the_trail(db, client, head):
    """Every /v1 route writes the trail (CONTRIBUTING.md); /v1/ping wrote
    nothing (audit of 17 Oct 2026)."""
    from korely_agent.db.models import AuditEvent

    ping(client, head)
    rows = db.query(AuditEvent).all()
    assert [(r.action, (r.meta or {}).get("endpoint")) for r in rows] == [("read", "ping:get")]
    assert rows[0].api_key_id is not None
