"""`manage review-vocabulary`: the phrases merged at birth, asked again (8 Oct 2026).

memory_core 3.3.0 `review_the_variants`, 8 Oct: a phrase
the vocabulary merged into a relation the first time it saw it ("mentors" into
`receives_support_from`, the direction reversed) stays there for good unless
somebody asks again. Here the adapter (`predicates.review_variants`) and the
command: what is asked, what moves with --apply and what a dry run leaves
alone. The vocabulary is one per end user of a project (18 Oct 2026; it was
one per tenant, then one per project), so the facts that move are that end
user's, never another end user's or another project's.

Nothing here says the model is right; a bench does (the LoCoMo
fn database, its separations read one by one).
"""
import uuid

import pytest

from korely_agent import manage
from korely_agent.db.models import AgentFact, PredicateAlias, PredicateCluster
from korely_agent.services import predicates
from korely_agent.services.memory_core.predicate_forms import canonicalize_predicate
from tests.conftest import seeded_project_id
from tests.test_predicates import FakeProvider, votes


PHRASE = canonicalize_predicate("mentors")
RELATION = "receives_support_from"


@pytest.fixture
def provider(monkeypatch):
    def install(answers=()):
        p = FakeProvider(answers)
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: p)
        return p
    return install


def fact(db, tenant, raw, canonical, obj, *, tense="current", end_user="maria", project=None):
    f = AgentFact(tenant_id=tenant.id, project_id=project or seeded_project_id(db, tenant.id),
                  end_user_id=end_user, subject_label="Caroline", predicate_raw=raw,
                  predicate_canonical=canonical, object_label=obj, tense=tense)
    db.add(f)
    db.flush()
    return f


@pytest.fixture
def merged(db, tenant):
    """`receives_support_from` with "mentors" merged into it, and a fact of each."""
    pid = seeded_project_id(db, tenant.id)
    rel = PredicateCluster(tenant_id=tenant.id, project_id=pid, end_user_id="maria", canonical=RELATION,
                           family="other",
                           is_functional=False, example_object="her family", member_count=2)
    db.add(rel)
    db.flush()
    db.add(PredicateAlias(tenant_id=tenant.id, project_id=pid, end_user_id="maria", surface=PHRASE,
                          cluster_id=rel.id))
    own = fact(db, tenant, "receives support from", RELATION, "her family")
    phrase = fact(db, tenant, "mentors", RELATION, "a young artist")
    db.commit()
    return rel, own, phrase


def aliases(db, tenant_id):
    """surface -> the relation it resolves to, for this tenant."""
    return {a.surface: c.canonical for a, c in
            db.query(PredicateAlias, PredicateCluster).join(PredicateCluster, PredicateCluster.id == PredicateAlias.cluster_id)
            .filter(PredicateAlias.tenant_id == tenant_id)}


def review(db, tenant, **kw):
    """The review of Maria's vocabulary in the project the facts above are written in."""
    return predicates.review_variants(db, tenant.id, project_id=seeded_project_id(db, tenant.id),
                                      end_user_id="maria", **kw)


def review_prompts(p):
    return [x for x in p.prompts if "Known relations:" in x]


def test_a_dry_run_lists_the_separation_and_writes_nothing(db, tenant, merged, provider):
    _rel, own, phrase = merged
    p = provider(['{"same_as": null}', '{"same_as": null}'])
    r = review(db, tenant)
    db.rollback()
    assert r["separated"] == [(PHRASE, RELATION, 1)]
    assert len(review_prompts(p)) == 2, "two answers of three that agree decide"
    asked = review_prompts(p)[0]
    assert "a young artist" in asked and "her family" in asked, "each side carries its own example"
    assert aliases(db, tenant.id)[PHRASE] == RELATION
    db.refresh(phrase)
    assert phrase.predicate_canonical == RELATION


def test_apply_gives_the_phrase_its_own_relation_and_moves_its_facts(db, tenant, merged, provider):
    _rel, own, phrase = merged
    provider(['{"same_as": null}', '{"same_as": null}', *votes('{"many_values": true}')])
    r = review(db, tenant, apply=True)
    db.commit()
    assert r["separated"] == [(PHRASE, RELATION, 1)]
    assert aliases(db, tenant.id)[PHRASE] == PHRASE, "the phrase resolves to its own relation now"
    db.refresh(phrase)
    db.refresh(own)
    assert phrase.predicate_canonical == PHRASE
    assert own.predicate_canonical == RELATION, "the relation's own facts stay"
    assert db.query(PredicateCluster).filter(PredicateCluster.tenant_id == tenant.id,
                                             PredicateCluster.canonical == PHRASE).count() == 1


def test_one_different_is_not_a_majority(db, tenant, merged, provider):
    """b882dad: a separation is kept for good, so it needs a majority of the
    votes (3 by default); "different" then "same" twice keeps the phrase."""
    p = provider(['{"same_as": null}', '{"same_as": 1}', '{"same_as": 1}'])
    r = review(db, tenant)
    assert r["separated"] == [] and r["same"] == [(PHRASE, RELATION)]
    assert len(review_prompts(p)) == 3


def test_the_same_relation_keeps_everything(db, tenant, merged, provider):
    provider(['{"same_as": 1}'])
    r = review(db, tenant, apply=True)
    db.commit()
    assert r["same"] == [(PHRASE, RELATION)] and r["separated"] == []
    assert aliases(db, tenant.id)[PHRASE] == RELATION


def test_a_phrase_said_only_in_the_past_is_not_asked(db, tenant, provider):
    pid = seeded_project_id(db, tenant.id)
    rel = PredicateCluster(tenant_id=tenant.id, project_id=pid, end_user_id="maria", canonical="visits",
                           family="other",
                           is_functional=False, example_object="Paris", member_count=2)
    db.add(rel)
    db.flush()
    past = canonicalize_predicate("visited")
    db.add(PredicateAlias(tenant_id=tenant.id, project_id=pid, end_user_id="maria", surface=past,
                          cluster_id=rel.id))
    fact(db, tenant, "visited", "visits", "Rome", tense="past")
    db.commit()
    p = provider()
    r = review(db, tenant)
    assert r["to_the_past"] == [(past, "visits")]
    assert review_prompts(p) == []


def test_only_the_end_users_facts_move_never_another_end_users_or_projects(db, tenant, merged,
                                                                            provider):
    """One vocabulary per end user of a project (18 Oct 2026): a phrase
    separated in Maria's leaves it for Maria's facts alone; Luca has his own
    vocabulary, in this project and in another, and his facts stay as they
    are, and his values reach no prompt."""
    from korely_agent.db.models import AgentProject

    name = f"p-{uuid.uuid4().hex[:6]}"
    other = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name=name, slug=name)
    db.add(other)
    db.flush()
    luca = fact(db, tenant, "mentors", RELATION, "a student", end_user="luca")
    elsewhere = fact(db, tenant, "mentors", RELATION, "an intern", end_user="luca", project=other.id)
    db.commit()
    p = provider(['{"same_as": null}', '{"same_as": null}', *votes('{"many_values": true}')])
    r = review(db, tenant, apply=True)
    db.commit()
    assert r["separated"] == [(PHRASE, RELATION, 1)]
    db.refresh(luca)
    db.refresh(elsewhere)
    assert luca.predicate_canonical == RELATION
    assert elsewhere.predicate_canonical == RELATION
    assert "a student" not in "\n".join(p.prompts) and "an intern" not in "\n".join(p.prompts)


def test_a_closure_across_the_two_sides_is_listed_not_reopened(db, tenant, merged, provider):
    from datetime import datetime, timezone

    _rel, own, phrase = merged
    own.invalid_at = datetime.now(timezone.utc)
    own.invalidated_by_id = phrase.id
    db.commit()
    provider(['{"same_as": null}', '{"same_as": null}', *votes('{"many_values": true}')])
    r = review(db, tenant, apply=True)
    db.commit()
    assert r["closings_to_reread"] == [(own.id, phrase.id)]
    db.refresh(own)
    assert own.invalid_at is not None, "the judge may have been right: a reader decides"


def test_no_model_key_is_an_error_that_says_so(db, tenant, merged, monkeypatch):
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
    with pytest.raises(RuntimeError, match="no model key"):
        review(db, tenant)


class TestTheCommand:
    @pytest.fixture(autouse=True)
    def wired(self, db, monkeypatch):
        monkeypatch.setattr(manage, "_session", lambda: db)

    def test_dry_run_prints_what_would_move(self, db, tenant, merged, provider, capsys):
        tid = tenant.id
        provider(['{"same_as": null}', '{"same_as": null}'])
        assert manage.review_vocabulary(str(tid)) == 0
        out = capsys.readouterr().out
        assert "would be separated" in out and PHRASE in out and RELATION in out
        assert "nothing was written" in out
        assert aliases(db, tid)[PHRASE] == RELATION

    def test_apply_writes(self, db, tenant, merged, provider, capsys):
        tid = tenant.id
        provider(['{"same_as": null}', '{"same_as": null}', *votes('{"many_values": true}')])
        assert manage.review_vocabulary(str(tid), apply=True) == 0
        assert "1 separated." in capsys.readouterr().out
        assert aliases(db, tid)[PHRASE] == PHRASE

    def test_without_a_key_it_says_what_to_do(self, db, tenant, merged, monkeypatch, capsys):
        tid = tenant.id
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
        assert manage.review_vocabulary(str(tid)) == 1
        assert "no model key" in capsys.readouterr().err


def test_another_tenants_facts_stay_as_they_are(db, tenant, merged, provider):
    """`facts=AgentFact.tenant_id == tenant_id` is the only tenant limit on
    the facts the review loads and moves: widened by one token, --apply
    rewrote another customer's facts and put its labels in this tenant's
    prompt, and every test above stayed green (audit of 17 Oct 2026)."""
    from korely_agent.db.models import Tenant

    other = Tenant(id=uuid.uuid4(), name="Globex")
    db.add(other)
    db.commit()
    theirs = fact(db, other, "mentors", RELATION, "Globex's apprentice")
    db.commit()
    p = provider(['{"same_as": null}', '{"same_as": null}', *votes('{"many_values": true}')])
    r = review(db, tenant, apply=True)
    db.commit()
    assert r["separated"] == [(PHRASE, RELATION, 1)], "this tenant's one fact only"
    assert "Globex's apprentice" not in "\n".join(p.prompts)
    db.refresh(theirs)
    assert theirs.predicate_canonical == RELATION
