"""The shared memory rules, on the self-hosted product (2026-09-27).

`services/memory_core/` is the facts engine, and since 11 Oct 2026 it lives
only here (until then it was the same folder, byte for byte, as in another
product's repository, which ran it too). These tests pin what it does,
on real Postgres where a database is needed:

  * the folder was not edited without its step (manifest, version);
  * the same triple restated is RECONFIRMED, not duplicated;
  * a past-tense statement closes the true twin instead of adding a row;
  * an explicit rename ("Acme renamed_to Globex") creates the alias by itself,
    and reads by the old or the new name find the same facts;
  * forgetting one memory detaches a fact other memories still assert;
  * the context and MCP lines show the current name and the plans;
  * a vocabulary that cannot ask its model refuses the write with a 503.
"""
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent.db.models import AgentEntityAlias, AgentFact, AgentMemory
from korely_agent.services import facts as agent_facts
from korely_agent.services.facts import service
from korely_agent.services.sentinels import EVERY_PROJECT

# No module-wide `db` mark: the tests that reach the database are marked by
# tests/conftest.py from their fixtures, and the two that do not (the
# manifest, the lines) run in `make test` too. Until the audit of 17 Oct 2026
# the mark covered the whole file, so `make test` (-m "not db") skipped the
# manifest check that CONTRIBUTING.md and manifest.py say runs "at the first run
# of tests".


def test_the_folder_matches_the_manifest():
    from korely_agent.services.memory_core.manifest import differences_from_the_manifest

    assert differences_from_the_manifest() == [], (
        "memory_core changed without its manifest. It is modified only after "
        "review, with VERSION updated and `python scripts/memory_core_manifest.py` "
        "run in the same change (memory_core/__init__.py, maintenance rule)."
    )


def _write(db, tenant, s, p, o, **kw):
    kw.setdefault("end_user_id", "maria")
    kw.setdefault("subject_type", "person")
    return service.write_fact_direct(db, tenant.id, subject=s, predicate=p, obj=o, **kw)


def test_the_same_triple_restated_is_reconfirmed(db, tenant):
    a = _write(db, tenant, "Maria", "likes", "peaches")
    b = _write(db, tenant, "maria", "likes", "Peaches")
    assert a["id"] == b["id"] and b["observation_count"] == 2
    row = db.query(AgentFact).one()
    assert row.observation_count == 2 and row.last_confirmed_at is not None


def test_the_past_closes_the_twin(db, tenant):
    _write(db, tenant, "Maria", "works_at", "Acme")
    # the tense is said by the writer (Graphiti §2.2.3), not guessed from the verb
    out = _write(db, tenant, "Maria", "works_at", "Acme", tense="past")
    assert out["tense"] == "past" and out["invalid_at"] is not None
    assert db.query(AgentFact).count() == 1
    rows, _ = agent_facts.retrieve_agent_facts(db, tenant.id, end_user_id="maria", project_id=EVERY_PROJECT)
    assert rows == []


def test_a_rename_creates_the_alias(db, tenant):
    _write(db, tenant, "Acme", "based_in", "Milan", subject_type="organization")
    _write(db, tenant, "Acme", "renamed_to", "Globex", subject_type="organization", confidence=0.95)
    alias = db.query(AgentEntityAlias).one()
    assert (alias.alias_lower, alias.canonical_label, alias.source) == ("acme", "Globex", "rename_fact")
    for name in ("Globex", "acme"):
        rows, _ = agent_facts.retrieve_agent_facts(
            db, tenant.id, end_user_id="maria", subject=name, predicate="based_in", project_id=EVERY_PROJECT)
        assert [r.object_label for r in rows] == ["Milan"], name
    s = service.serialize_fact(rows[0])
    assert s["subject"] == "Acme" and s["subject_canonical"] == "Globex"


def test_the_alias_stays_in_the_end_users_memory(db, tenant):
    _write(db, tenant, "Acme", "renamed_to", "Globex", subject_type="organization", confidence=0.95)
    _write(db, tenant, "Acme", "based_in", "Rome", subject_type="organization", end_user_id="luca")
    rows, _ = agent_facts.retrieve_agent_facts(db, tenant.id, end_user_id="luca", project_id=EVERY_PROJECT)
    assert rows[0].subject_canonical == "Acme"


def test_forgetting_a_memory_detaches_the_shared_fact(db, tenant):
    m1 = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="maria", content="Maria likes peaches")
    m2 = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="maria", content="Maria loves peaches")
    db.add_all([m1, m2]); db.commit()
    for m in (m1, m2):
        service._persist_one(
            db, tenant.id, end_user_id="maria", agent_id=None, run_id=None,
            subject="Maria", subject_type="person", predicate_raw="likes", obj="peaches",
            object_is_literal=False, confidence=0.9, source_memory_id=m.id, model_used="t",
            project_id=None,
        )
    db.commit()
    f = db.query(AgentFact).one()
    assert f.observation_count == 2
    assert {x.id for x in service.facts_for_memory(db, tenant.id, m2.id, project_id=EVERY_PROJECT)} == {f.id}
    assert service.forget_memory(db, tenant.id, m1.id, project_id=EVERY_PROJECT)["facts_invalidated"] == 0
    db.refresh(f)
    assert f.invalid_at is None and f.observation_count == 1 and f.source_memory_id == m2.id
    assert service.forget_memory(db, tenant.id, m2.id, project_id=EVERY_PROJECT)["facts_invalidated"] == 1


def test_the_lines_for_the_agent():
    from korely_agent.services.memory_core.presentation import context_line, mcp_line

    f = {
        "subject": "Acme", "subject_canonical": "Globex", "predicate": "based_in",
        "object": "Milan", "valid_from": "2026-06-01T00:00:00+00:00",
        "last_confirmed_at": "2026-09-20T00:00:00+00:00", "observation_count": 3,
        "tense": "current", "invalid_at": None,
    }
    assert mcp_line(f) == "Globex (then: Acme) · based_in · Milan  (since 2026-06-01, confirmed 2026-09-20, seen 3x)"

    class R:
        subject_label = "Acme"; subject_canonical = "Globex"; predicate_raw = "adopts"
        object_label = "SSO"; object_canonical = "SSO"; tense = "planned"
        valid_from = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert context_line(R, "maria") == "- Globex (then: Acme) adopts SSO (since 2026-09-01) [planned, not done]"


def test_without_the_models_answer_the_write_stops_and_writes_nothing(db, tenant, monkeypatch):
    """The service raises and keeps nothing; the 503 the API answers with is
    tests/test_a_model_that_does_not_answer.py's (TestDirectWrites). This
    name promised the 503 and checked only the raise (audit of 17 Oct 2026)."""
    from korely_agent.services.memory_core.store import ResolutionUnavailable

    def _boom(*a, **k):
        raise ResolutionUnavailable("429")
    monkeypatch.setattr("korely_agent.services.predicates.resolve_relation", _boom)
    with pytest.raises(ResolutionUnavailable):
        _write(db, tenant, "Maria", "subscribes_to", "Pro plan")
    db.rollback()
    assert db.query(AgentFact).count() == 0
