"""The region of a project: where its memories are read by a model (8 Oct 2026).

The one codebase: the hosted product has given each project a region since
6 Oct, "eu" (Scaleway in Paris) or "global" (Gemini). This install now keeps
one writer per region and a project may choose; one that never chose follows
the install's default writer, which is the behaviour of before.
services/regions.py has the rules; the one that matters most is that a
region never writes with the other region's writer.
"""
import uuid

import pytest

from korely_agent.core import extensions, llm
from korely_agent.db.models import AgentMemory, AgentProject
from korely_agent.services import keys as key_service
from korely_agent.services import model_choice, regions
from korely_agent.services.facts import drain as facts_drain
from korely_agent.services.facts import service as facts_service


SCW = ("scaleway", "https://api.scaleway.ai/v1", "scw-key", "gpt-oss-120b")
GEM = ("google", "https://generativelanguage.googleapis.com/v1beta", "AIza-key", "gemini-3.1-flash-lite")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(llm, "current_google_key", lambda: None)
    monkeypatch.setattr(facts_drain, "_pauses", {})
    llm.reset_provider_cache()
    yield
    llm.reset_provider_cache()
    extensions.reset()


def writers(monkeypatch, default, **by_region):
    """The writers chosen on the Models page, as core/llm reads them."""
    monkeypatch.setattr(llm, "screen_writers", lambda: {"default": default, **by_region})
    llm.reset_provider_cache()


def project(db, tenant, region=None, name=None):
    p = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name=name or f"p-{uuid.uuid4().hex[:6]}",
                     slug=uuid.uuid4().hex[:8], region=region)
    db.add(p)
    db.commit()
    return p


# ── the writer of a region ──────────────────────────────────────────────────

def test_each_region_its_own_writer(monkeypatch):
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    assert llm.get_provider("eu").vendor == "scaleway"
    assert llm.get_provider("eu").region == "eu"
    assert llm.get_provider("global").vendor == "google"
    assert llm.get_provider().vendor == "google", "the default writer, outside any write"


def test_no_fallback_to_the_other_region(monkeypatch):
    writers(monkeypatch, "global", **{"global": GEM})
    assert llm.get_provider("eu") is None
    with llm.writing_in("eu"):
        assert llm.get_provider() is None, "a write for Europe never goes to Gemini"
    assert llm.regions_with_writer() == {"global"}


def test_a_write_in_progress_picks_its_region(monkeypatch):
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    with llm.writing_in("eu"):
        assert llm.get_provider().vendor == "scaleway"
    assert llm.get_provider().vendor == "google"


def test_the_files_writer_at_an_unknown_address_still_writes(monkeypatch):
    """An install configured in .env with a model of its own behind the
    OpenAI-compatible door: no region we know, and the projects that follow
    the install keep writing with it, as before."""
    writers(monkeypatch, None)
    s = llm.get_settings()
    monkeypatch.setattr(s, "extraction_provider", "mistral")
    monkeypatch.setattr(s, "mistral_api_key", "secret")
    monkeypatch.setattr(s, "mistral_api_base", "https://llm.example.com/v1")
    llm.reset_provider_cache()
    assert llm.default_region() is None
    assert llm.provider_configured()
    assert llm.get_provider() is not None
    assert llm.get_provider("eu") is None and llm.get_provider("global") is None


# ── the model choice keeps one writer per region ────────────────────────────

def test_saving_a_provider_makes_it_the_writer_of_its_region(db, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-test")
    from korely_agent import config

    config.get_settings.cache_clear()
    monkeypatch.setattr(model_choice, "check", lambda provider, key: {"ok": True, "models": ["gpt-oss-120b", "gemini-3.1-flash-lite"]})
    model_choice.save(db, "google", key="AIza-key", model="gemini-3.1-flash-lite")
    model_choice.save(db, "scaleway", key="scw-key", model="gpt-oss-120b")
    described = model_choice.describe(db)
    assert described["active"]["provider"] == "scaleway", "saved last: the install's default"
    assert described["by_region"]["eu"]["provider"] == "scaleway"
    assert described["by_region"]["global"]["provider"] == "google", "still the writer of Global"
    config.get_settings.cache_clear()


def test_a_choice_stored_before_regions_has_one_writer():
    data = {"active": "scaleway", "providers": {"scaleway": {"key": "k", "model": "m"},
                                                 "google": {"key": "g", "model": "n"}}}
    assert model_choice._writer_of(data, "eu")[0].id == "scaleway"
    assert model_choice._writer_of(data, "global") is None, "never guessed from a saved key"


def test_every_provider_has_a_region_we_know():
    assert {p.region for p in model_choice.PROVIDERS.values()} <= set(regions.REGIONS)


def test_every_place_that_lists_the_regions_lists_regions_REGIONS():
    """The regions are written as values where a constant cannot go: the
    OpenAPI enums of /admin/projects and of the Cloud's sign-up, and the
    table's check. Held equal to services/regions.REGIONS, so a region added
    or renamed there cannot be refused, or accepted, by one of them alone
    (audit of 17 Oct 2026)."""
    import typing

    from korely_agent.api import admin

    assert set(typing.get_args(admin.Region)) == set(regions.REGIONS)
    check = next(c for c in AgentProject.__table__.constraints
                 if "region" in str(getattr(c, "sqltext", "")))
    assert set(str(check.sqltext).split("IN (", 1)[1].rstrip(")").replace("'", "").replace(" ", "")
               .split(",")) == set(regions.REGIONS)


@pytest.mark.cloud
def test_the_cloud_sign_up_names_the_regions_there_are():
    """The Cloud's sign-up enum, held to the same list as above."""
    import typing

    from korely_agent.hosted import signup

    body = signup.InitBody.model_fields["processing_region"].annotation
    assert {a for arg in typing.get_args(body) for a in typing.get_args(arg)} == set(regions.REGIONS)


@pytest.mark.cloud
def test_the_sign_up_names_no_provider_for_a_region():
    """Which provider writes a region is the superadmin's saved choice
    (services/model_choice), so the sign-up's schema cannot promise one: it
    said Google Gemini and Scaleway in Paris, and that held only while those
    were the ones saved (audit of 17 Oct 2026)."""
    from korely_agent.hosted import signup
    from korely_agent.services.model_choice import PROVIDERS

    texts = (signup.InitBody.model_fields["processing_region"].description,
             signup.InitResult.model_fields["processing_region"].description)
    for p in PROVIDERS.values():
        for t in texts:
            assert p.label not in t, (p.label, t)


# ── a project's region ──────────────────────────────────────────────────────

def test_a_project_that_never_chose_follows_the_install(db, tenant, monkeypatch):
    writers(monkeypatch, "eu", eu=SCW, **{"global": GEM})
    p = project(db, tenant)
    assert regions.of_project(db, tenant.id, p.id) == "eu"
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    assert regions.of_project(db, tenant.id, p.id) == "global", "it follows the default when it moves"


def test_a_project_that_chose_keeps_its_region(db, tenant, monkeypatch):
    writers(monkeypatch, "eu", eu=SCW, **{"global": GEM})
    p = project(db, tenant, region="global")
    assert regions.of_project(db, tenant.id, p.id) == "global"
    assert regions.processing(db, tenant.id, p.id) == "global"


def test_a_region_with_no_writer_is_not_processing(db, tenant, monkeypatch):
    writers(monkeypatch, "global", **{"global": GEM})
    p = project(db, tenant, region="eu")
    assert regions.of_project(db, tenant.id, p.id) == "eu"
    assert regions.processing(db, tenant.id, p.id) is None


def test_another_accounts_project_is_not_read(db, tenant, monkeypatch):
    from korely_agent.db.models import Tenant

    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    other = Tenant(id=uuid.uuid4(), name="Other")
    db.add(other)
    db.commit()
    p = project(db, other, region="eu")
    assert regions.of_project(db, tenant.id, p.id) == "global"


def test_an_unknown_region_is_refused():
    with pytest.raises(regions.UnknownRegion):
        regions.checked("us")
    assert regions.checked(" EU ") == "eu" and regions.checked(None) is None


# ── the drain claims only regions that can write ────────────────────────────

def pending(db, tenant, project_row, n):
    for i in range(n):
        db.add(AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project_row.id,
                           end_user_id=f"u{i}", content=f"memory {i}", graph_sync_status="pending"))
    db.commit()


def claimed_projects(db):
    return {m.project_id for m in db.query(AgentMemory)
            .filter(AgentMemory.graph_sync_status == "processing").all()}


def test_a_region_with_no_writer_waits_in_the_queue(db, tenant, monkeypatch):
    writers(monkeypatch, "global", **{"global": GEM})
    europe, world = project(db, tenant, region="eu"), project(db, tenant, region="global")
    pending(db, tenant, europe, 3)
    pending(db, tenant, world, 3)
    facts_drain._claim(db, 20)
    assert claimed_projects(db) == {world.id}


def test_a_resting_region_waits_and_the_other_goes_on(db, tenant, monkeypatch):
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    europe, world = project(db, tenant, region="eu"), project(db, tenant)
    pending(db, tenant, europe, 3)
    pending(db, tenant, world, 3)
    facts_drain._provider_refused("eu")
    assert facts_drain._drain_paused("eu") > 0 and facts_drain._drain_paused("global") == 0
    facts_drain._claim(db, 20)
    assert claimed_projects(db) == {world.id}, "the project that follows the install writes in Global"


def test_every_writer_resting_claims_nothing(db, tenant, monkeypatch):
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    pending(db, tenant, project(db, tenant), 2)
    facts_drain._provider_refused("eu")
    facts_drain._provider_refused("global")
    r = facts_service.process_pending_memories(db, limit=20)
    assert r["claimed"] == 0 and r["paused"] > 0


def test_each_memory_is_written_in_its_projects_region(db, tenant, monkeypatch):
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    europe, world = project(db, tenant, region="eu"), project(db, tenant)
    pending(db, tenant, europe, 1)
    pending(db, tenant, world, 1)
    seen = {}

    def drain_one(gdb, m, region=None, when=None):
        seen[m.project_id] = (region, llm.get_provider(region).vendor)
        m.graph_sync_status = "done"
        gdb.commit()
        return True

    monkeypatch.setattr(facts_drain, "_drain_one_memory", drain_one)
    facts_service.process_pending_memories(db, limit=20)
    assert seen == {europe.id: ("eu", "scaleway"), world.id: ("global", "google")}


def test_the_extraction_runs_inside_its_projects_region(db, tenant, monkeypatch):
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    europe = project(db, tenant, region="eu")
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=europe.id,
                    end_user_id="maria", content="I moved to Lisbon.", graph_sync_status="pending")
    db.add(m)
    db.commit()
    asked = []

    def extract(text, **kw):
        asked.append(llm.get_provider().vendor)
        raise RuntimeError("stop here")

    monkeypatch.setattr(facts_service, "extract_facts_and_observation", extract)
    facts_service.extract_and_persist_facts(db, tenant.id, memory=m)
    assert asked == ["scaleway"]


# ── the gate before every model call ────────────────────────────────────────

def test_the_gate_sees_vendor_model_and_region_and_can_refuse(monkeypatch):
    calls = []

    def gate(vendor, model, region):
        calls.append((vendor, model, region))
        raise llm.NotNow("the daily cap is reached")

    extensions.register("before_model_call", gate)
    p = llm.MistralProvider("k", "https://api.scaleway.ai/v1", force_model="gpt-oss-120b",
                            vendor="scaleway", region="eu")

    def never(*a, **k):
        raise AssertionError("the call was sent")

    monkeypatch.setattr("urllib.request.urlopen", never)
    llm.forget_failure_here()
    with pytest.raises(llm.NotNow):
        p.complete([{"role": "user", "content": "hi"}])
    assert calls == [("scaleway", "gpt-oss-120b", "eu")]
    assert llm.last_kind_here() in facts_drain._NOT_THE_MEMORYS, \
        "the drain reads it as a refusal: the memory waits, the region rests"


def test_with_no_hook_the_gate_lets_every_call_through(monkeypatch):
    sent = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}],"usage":{}}'

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: (sent.append(1), Answer())[1])
    monkeypatch.setattr(llm, "_bill", lambda *a, **k: None)
    p = llm.MistralProvider("k", "https://api.scaleway.ai/v1", force_model="gpt-oss-120b",
                            vendor="scaleway", region="eu")
    assert p.complete([{"role": "user", "content": "hi"}]).text == "ok" and sent == [1]


# ── the API ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def admin(db, tenant):
    _r, full = key_service.create_api_key(
        db, tenant_id=tenant.id, name=f"op-{uuid.uuid4().hex[:6]}",
        scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE], expires_in_days=None)
    return {"Authorization": f"Bearer {full}"}


def test_a_project_is_created_and_moved_between_regions(db, tenant, client, monkeypatch):
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    h = admin(db, tenant)
    r = client.post("/admin/projects", json={"name": "Clinics", "region": "eu"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["region"] == "eu" and r.json()["processing_region"] == "eu"
    pid = r.json()["id"]
    r = client.patch(f"/admin/projects/{pid}", json={"name": "Clinics EU"}, headers=h)
    assert r.json()["region"] == "eu", "a rename leaves the region alone"
    r = client.patch(f"/admin/projects/{pid}", json={"region": None}, headers=h)
    assert r.json()["region"] is None and r.json()["processing_region"] == "global"
    r = client.post("/admin/projects", json={"name": "US", "region": "us"}, headers=h)
    assert r.status_code == 422


def test_ping_answers_the_region_of_the_keys_project(db, tenant, client, monkeypatch):
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    europe = project(db, tenant, region="eu")
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name="agent",
                                          project_id=europe.id, expires_in_days=None)
    body = client.get("/v1/ping", headers={"Authorization": f"Bearer {full}"}).json()
    assert body["processing_region"] == "eu"


def test_the_late_judgement_of_a_vector_runs_in_its_projects_region(db, tenant, monkeypatch):
    """The vector drain judged a fact whose vector came late with the install's
    default writer, whatever its project's region (audit of 17 Oct 2026)."""
    from datetime import datetime, timezone

    from korely_agent.core.llm import EMBEDDING_DIM
    from korely_agent.db.models import AgentFact
    from korely_agent.services import vectors
    from korely_agent.services.facts import contradiction

    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    europe = project(db, tenant, region="eu")
    db.add(AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=europe.id,
                     end_user_id="maria", subject_label="maria", subject_canonical="maria",
                     predicate_raw="lives in", predicate_canonical="lives_in",
                     object_label="Lisbon", object_canonical="lisbon", confidence=0.9,
                     valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc), tense="current",
                     embedding=None, embedding_status=vectors.PENDING))
    db.commit()
    monkeypatch.setattr(llm, "embedding_configured", lambda: True)
    monkeypatch.setattr("korely_agent.services.facts.service.fact_vector",
                        lambda text: [1.0] + [0.0] * (EMBEDDING_DIM - 1))
    asked = []

    def judge(db_, tenant_id, fact, vector):
        asked.append(llm.get_provider().vendor)
        return {}

    monkeypatch.setattr(contradiction, "judge_after_the_vector", judge)
    assert vectors.process_pending_vectors(db)["processed"] == 1
    assert asked == ["scaleway"]


# ── a Cloud project stored with no region, edited from the dashboard ────────
# (audits of 20 and 27 Oct 2026). dashboard RegionSelect shows such a project as
# "Service default" and sends no region unless the choice changed; a choice
# of Eu or Global is what is stored.

@pytest.mark.cloud
def test_the_dashboard_save_of_a_cloud_project_with_no_region(db, tenant, client, monkeypatch):
    from korely_agent import config

    real = config.get_settings()
    cloud = real.model_copy(update={"korely_mode": "hosted"})
    writers(monkeypatch, "global", eu=SCW, **{"global": GEM})
    h = admin(db, tenant)
    p = project(db, tenant, region=None)
    assert p.region is None
    assert regions.of_project(db, tenant.id, p.id) == "global", "read: the service default writer"

    monkeypatch.setattr(config, "get_settings", lambda: cloud)
    # A save that leaves the choice alone sends only what changed: no region.
    r = client.patch(f"/admin/projects/{p.id}", json={"name": "Renamed"}, headers=h)
    assert r.status_code == 200, r.text
    db.refresh(p)
    assert p.region is None, "a rename must not store a region"
    # Choosing Europe, or Global, stores exactly that.
    for chosen in ("eu", "global"):
        r = client.patch(f"/admin/projects/{p.id}", json={"region": chosen}, headers=h)
        assert r.status_code == 200 and r.json()["region"] == chosen, r.text
        db.refresh(p)
        assert p.region == chosen
    # A project made without a region on the Cloud is stored Global, which is
    # what the create dialog's select shows there.
    r = client.post("/admin/projects", json={"name": "Fresh"}, headers=h)
    assert r.status_code == 201 and r.json()["region"] == "global", r.text
