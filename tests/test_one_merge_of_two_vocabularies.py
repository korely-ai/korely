"""One merge of two vocabularies, for an account claim and a project's
deletion (audit of 19 Oct 2026): predicates.merge_vocabulary.

Both were written line for line in hosted/claim._vocabulary and
services/projects._move_vocabulary, and both were edited by hand each time
the vocabulary's scope changed (twice on 18 Oct 2026). Here the one function,
in the two shapes its callers use: each end user's relations join the same
end user's, never another's; a phrase the target already resolves goes; the
rest moves whole only when asked.
"""
import uuid


from korely_agent.db.models import PredicateAlias, PredicateCluster, Tenant
from korely_agent.services import predicates
from korely_agent.services import projects as project_service


def _relation(db, tenant_id, project_id, end_user, canonical, *phrases):
    c = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant_id, project_id=project_id,
                         end_user_id=end_user, canonical=canonical)
    db.add(c)
    db.flush()
    db.add_all([PredicateAlias(tenant_id=tenant_id, project_id=project_id, end_user_id=end_user,
                               surface=p, cluster_id=c.id) for p in (canonical, *phrases)])
    db.commit()
    return c.id


def _vocabulary(db, tenant_id, project_id):
    db.expire_all()
    rels = {c.id: (c.end_user_id, c.canonical) for c in db.query(PredicateCluster).filter(
        PredicateCluster.tenant_id == tenant_id,
        PredicateCluster.project_id.is_not_distinct_from(project_id))}
    return {(eu, canonical): sorted(a.surface for a in db.query(PredicateAlias).filter(
        PredicateAlias.cluster_id == cid, PredicateAlias.tenant_id == tenant_id,
        PredicateAlias.project_id.is_not_distinct_from(project_id)))
        for cid, (eu, canonical) in rels.items()}


def _two_scopes(db, source, target):
    """Anna has `subscribes_to` in both scopes, and `is on plan` in both;
    Bruno has it only in the source; Anna's `mentored_by` only in the source."""
    kept = _relation(db, *target, "anna", "subscribes_to", "is on plan")
    _relation(db, *source, "anna", "subscribes_to", "is on plan", "has plan")
    _relation(db, *source, "bruno", "subscribes_to", "is on plan")
    _relation(db, *source, "anna", "mentored_by")
    return kept


def test_a_project_deletion_joins_twins_and_moves_the_rest(db, tenant):
    default = project_service.get_or_create_default_project(db, tenant.id)
    gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
    source, target = (tenant.id, gamma.id), (tenant.id, default.id)
    kept = _two_scopes(db, source, target)

    assert predicates.merge_vocabulary(db, source=source, target=target, move_the_rest=True) == 2
    db.commit()

    assert _vocabulary(db, *source) == {}
    assert _vocabulary(db, *target) == {
        ("anna", "subscribes_to"): ["has plan", "is on plan", "subscribes_to"],
        ("bruno", "subscribes_to"): ["is on plan", "subscribes_to"],
        ("anna", "mentored_by"): ["mentored_by"],
    }
    assert db.get(PredicateCluster, kept) is not None, "the target's row is the one kept"


def test_a_claim_joins_twins_and_leaves_the_rest_to_its_caller(db, tenant):
    s = Tenant(id=uuid.uuid4(), name="Made without a login")
    db.add(s)
    db.commit()
    source, target = (s.id, None), (tenant.id, None)
    kept = _two_scopes(db, source, target)

    assert predicates.merge_vocabulary(db, source=source, target=target, move_the_rest=False) == 0
    db.commit()

    assert _vocabulary(db, *target) == {
        ("anna", "subscribes_to"): ["has plan", "is on plan", "subscribes_to"]}
    assert _vocabulary(db, *source) == {
        ("bruno", "subscribes_to"): ["is on plan", "subscribes_to"],
        ("anna", "mentored_by"): ["mentored_by"],
    }, "another end user's relation, and one with no twin, stay for the claim's move"
    assert db.get(PredicateCluster, kept) is not None
