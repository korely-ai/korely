"""The contradiction judge follows the provider (2026-10-02).

Found on the EU bench: `detect_and_invalidate` resolved its model to
`settings.extraction_model` BY NAME, so with EXTRACTION_PROVIDER=mistral
pointed at Scaleway the judge asked for "gemini-3.1-flash-lite" and every
write that had a candidate died on 422. No model asked for means None all
the way down: each provider already picks its own default.
"""
import uuid
from datetime import datetime, timezone


from korely_agent.db.models import AgentFact
from korely_agent.services.facts import contradiction
from tests.conftest import seeded_project_id


def test_no_model_name_stays_none(db, tenant, monkeypatch):
    project = seeded_project_id(db, tenant.id)
    old = AgentFact(tenant_id=tenant.id, project_id=project, end_user_id="u1",
                    subject_label="Maria", subject_canonical="maria",
                    predicate_raw="lives in", predicate_canonical="lives_in",
                    object_label="Rome", object_canonical="rome",
                    valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc))
    db.add(old)
    db.commit()

    seen = {}

    def classify(candidates, subj, pred, obj, db_, tenant_id, model, **kw):
        seen["model"] = model
        return [], {}

    monkeypatch.setattr(contradiction, "_classify", classify)
    contradiction.detect_and_invalidate(
        db, tenant.id, project_id=project, end_user_id="u1",
        new_fact_id=uuid.uuid4(), new_subject="Maria",
        new_predicate_raw="lives in", new_predicate_canonical="lives_in",
        new_object="Milan", new_subject_canonical="maria",
        new_object_canonical="milan", agent_id=None,
    )
    assert "model" in seen, "stage 1 found no candidate: the test seeded none"
    assert seen["model"] is None, (
        f"the judge asked for {seen['model']!r} by name: with another "
        f"provider that name does not exist and the write dies")
