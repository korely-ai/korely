"""The learned vocabulary.

`services/predicates.py` is the piece the contradiction detection rests on:
matching candidates happens on the canonical predicate, so if two phrasings of
one relation never meet, a superseded fact is never superseded and the store
answers the old value forever. It had no tests, for a reason worth writing down:
the development image once omitted `google-genai`, so `make test-all` could not
reach the two model calls at all - they were dead to the suite while being the
whole mechanism. The image installs it now, with the rest of the product's
requirements, so a test that set a key and left a model call real would reach
Google with it; tests/conftest.py empties the provider keys and closes the
network to anything but this machine, so such a test fails instead.

The fix is not to put a network in the test suite. It is to stub the provider
and assert the decisions around it: what is asked, how often, and what happens
to every answer the model can give, including the answers it should not give.

Nothing here asserts that the model is right. That is measured separately,
against a real one.
"""
import uuid

import pytest

from korely_agent.core.llm import EMBEDDING_DIM, LLMResult
from korely_agent.db.models import PredicateAlias, PredicateCluster
from korely_agent.services import predicates
from korely_agent.services.memory_core.store import ResolutionUnavailable, ResolutionUnusable
from korely_agent.services.memory_core.predicate_forms import canonicalize_predicate


# memory_core 2.1.0: whether a relation holds one value is decided by a
# majority of three answers, and two that agree end it (I1, blind install of
# 0.1.13). A script gives every vote, so an answer a test means as one decision
# is written twice.
AGREE = 2


def votes(answer, n=AGREE):
    return [answer] * n


class FakeProvider:
    """Answers from a script, and remembers what it was asked.

    The call log is the point: the module claims each unseen phrase costs one
    question and is then cached forever, and that claim is a cost claim.
    """

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []
        self.thinking = []

    def complete(self, messages, **kw):
        self.prompts.append(messages[0]["content"])
        self.thinking.append(kw.get("reasoning"))
        # 2026-09-27 (BUG-109): a new relation the static map does not know is
        # also asked which FAMILY it belongs to. Answered here without touching
        # the script, so every scripted sequence below still means what it
        # meant: the family question is not what these tests are about.
        if '"family"' in messages[0]["content"]:
            return LLMResult(text='{"family": "other"}')
        text = self.answers.pop(0) if self.answers else '{"same_as": null}'
        if isinstance(text, Exception):
            raise text
        return LLMResult(text=text)

    @property
    def resolution_calls(self):
        return [p for p in self.prompts if "Known relations:" in p]

    @property
    def functional_calls(self):
        return [p for p in self.prompts if "many_values" in p]


@pytest.fixture
def vec():
    """A deterministic non-degenerate embedding.

    Every phrase gets the same vector, which makes distance useless and forces
    every decision onto the model. That is deliberate: the measurement in the
    module docstring is that distance cannot decide, so a test that leaned on it
    would be testing a mechanism we established does not work.
    """
    return [0.1] * EMBEDDING_DIM


def an_embedder_is_configured(monkeypatch):
    """A Google key is set, so the vocabulary has an embedder. Without one
    `predicates.vocabulary_embedder()` is None (no embedder) and a stubbed `embed` is
    never called: since 2026-09-28 the adapter tells "no key" from "the call
    failed", and before that these tests reached the shortlist on a vector
    no install without a key could produce."""
    monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")


@pytest.fixture(autouse=True)
def _votes_at_birth(monkeypatch):
    """These tests are about the questions a phrase is asked when it is born,
    the single-value votes included. Since 0.1.23 those votes wait for the
    first judge that meets two facts of the relation (`LAZY_SINGLE_VALUE`,
    tests/test_memory_core_3_3_adapter.py); here they are asked at birth, the
    path memory_core keeps for a store with the lazy votes off."""
    monkeypatch.setattr(predicates, "LAZY_SINGLE_VALUE", False)


@pytest.fixture
def stub(monkeypatch, vec):
    """Install a fake provider and a fixed embedder. Returns the provider.
    `embedding=None` is an install with no embedder (no key)."""
    def install(answers=(), embedding=vec):
        p = FakeProvider(answers)
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: p)
        monkeypatch.setattr("korely_agent.core.llm.embed",
                            lambda text, **kw: embedding)
        if embedding is not None:
            an_embedder_is_configured(monkeypatch)
        return p
    return install


def clusters(db, tenant):
    return (db.query(PredicateCluster)
            .filter(PredicateCluster.tenant_id == tenant.id).all())


class TestWithoutAProvider:
    """The store must still work when no model is reachable."""

    def test_every_phrase_stands_alone(self, db, tenant, monkeypatch, vec):
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
        monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: vec)

        for surface in ("has_plan", "uses_plan", "subscribes_to"):
            canonical, functional = predicates.resolve(
                db, tenant.id, surface, object_label="Business plan")
            assert canonical == canonicalize_predicate(surface)
            assert functional is None      # unknown, so never supersede

        assert len(clusters(db, tenant)) == 3

    def test_no_provider_is_not_an_error(self, db, tenant, monkeypatch, vec):
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
        monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: vec)
        canonical, _ = predicates.resolve(db, tenant.id, "covered_under")
        assert canonical == "covered_under"


class TestTheQuestionIsAskedOnce:
    """Cost: one question per unseen phrase, then the alias table forever."""

    def test_repeat_of_the_same_surface_asks_nothing(self, db, tenant, stub):
        p = stub(votes('{"many_values": false}'))
        first, functional = predicates.resolve(db, tenant.id, "has_plan",
                                               object_label="Business plan")
        asked_after_first = len(p.prompts)

        for _ in range(5):
            again, again_functional = predicates.resolve(
                db, tenant.id, "has_plan", object_label="Business plan")
            assert again == first
            assert again_functional == functional

        assert len(p.prompts) == asked_after_first, "cached phrase asked again"
        assert len(clusters(db, tenant)) == 1

    def test_first_phrase_is_not_asked_to_choose(self, db, tenant, stub):
        """Nothing to choose between: the shortlist is empty."""
        p = stub(votes('{"many_values": true}'))
        predicates.resolve(db, tenant.id, "likes", object_label="fish")
        assert p.resolution_calls == []
        assert len(p.functional_calls) == AGREE

    def test_functionality_is_asked_once_per_cluster(self, db, tenant, stub):
        p = stub([*votes('{"many_values": false}'), '{"same_as": 1}'])
        predicates.resolve(db, tenant.id, "has_plan", object_label="Business")
        predicates.resolve(db, tenant.id, "uses_plan", object_label="Business")
        assert len(p.functional_calls) == AGREE, "re-classified an existing cluster"


class TestEveryAnswerTheModelCanGive:
    """Including the ones it should not."""

    def test_same_as_reuses_the_cluster(self, db, tenant, stub):
        p = stub([*votes('{"many_values": false}'), '{"same_as": 1}'])
        first, _ = predicates.resolve(db, tenant.id, "has_plan", object_label="Business")
        second, functional = predicates.resolve(db, tenant.id, "subscribes_to",
                                                object_label="Business")
        assert second == first
        # inherited from the cluster: many_values false means one value at a
        # time, which is what functional means
        assert functional is True
        # and inherited, not asked again: one functional question for the
        # cluster, one merge question for the second phrase
        assert len(p.functional_calls) == AGREE, "asked again instead of inheriting"
        assert len(p.resolution_calls) == 1
        assert len(clusters(db, tenant)) == 1
        assert clusters(db, tenant)[0].member_count == 2

    def test_null_makes_a_new_cluster(self, db, tenant, stub):
        stub([*votes('{"many_values": true}'), '{"same_as": null}', *votes('{"many_values": true}')])
        a, _ = predicates.resolve(db, tenant.id, "likes", object_label="fish")
        b, _ = predicates.resolve(db, tenant.id, "lives_in", object_label="Milan")
        assert a != b
        assert len(clusters(db, tenant)) == 2

    BAD = [
        '{"same_as": 0}',        # 1-based, so 0 is out of range
        '{"same_as": 99}',       # past the end of the shortlist
        '{"same_as": "yes"}',    # not a number
        '{"same_as": true}',     # bool is an int in Python; must not index
        '{}',                    # key absent
        'not json at all',
        '',
    ]

    @pytest.mark.parametrize("answer", BAD)
    def test_a_bad_answer_is_asked_again_and_a_good_one_decides(self, db, tenant, stub, answer):
        """Never merge on an answer we do not understand, and never cache it as
        a decision either (severe reads, 0.1.15). It used to be read as "a new
        relation" and kept: the recoverable side for merging, and still a
        decision nobody made. Now it is asked again, thinking, and the answer
        that can be read decides."""
        p = stub([*votes('{"many_values": true}'), answer, '{"same_as": null}',
                  *votes('{"many_values": true}')])
        a, _ = predicates.resolve(db, tenant.id, "likes", object_label="fish")
        b, _ = predicates.resolve(db, tenant.id, "works_at", object_label="Apple")
        assert a != b
        assert len(clusters(db, tenant)) == 2
        asked = [t for q, t in zip(p.prompts, p.thinking) if "Known relations:" in q]
        assert asked == [False, True], "the question again, and this time thinking"

    @pytest.mark.parametrize("answer", BAD)
    def test_bad_answers_every_time_refuse_the_write_and_cache_nothing(self, db, tenant, stub, answer):
        stub([*votes('{"many_values": true}'), answer, answer, answer])
        predicates.resolve(db, tenant.id, "likes", object_label="fish")
        db.commit()
        with pytest.raises(ResolutionUnusable):
            predicates.resolve(db, tenant.id, "works_at", object_label="Apple")
        db.rollback()
        assert [c.canonical for c in clusters(db, tenant)] == ["likes"]
        assert "works_at" not in [a.surface for a in db.query(PredicateAlias)
                                  .filter(PredicateAlias.tenant_id == tenant.id).all()]

    def test_a_raising_provider_refuses_the_write(self, db, tenant, stub):
        """A call that never answered is not an answer.

        This test used to be called `test_a_raising_provider_is_treated_as_new`
        and it asserted the opposite, which is how the defect survived a green
        suite: the behaviour was wrong and the test certified it. What the old
        name called "treated as new" is a decision, and the code had no way of
        telling a decision from an outage.
        """
        stub([*votes('{"many_values": true}'), RuntimeError("502 upstream")])
        predicates.resolve(db, tenant.id, "likes", object_label="fish")
        with pytest.raises(ResolutionUnavailable):
            predicates.resolve(db, tenant.id, "works_at", object_label="Apple")

    def test_a_failed_resolution_leaves_nothing_behind(self, db, tenant, stub):
        """The part that makes this worse than one lost write.

        `_assign` writes a PredicateAlias, and an alias is consulted before the
        model on every later write for that phrase. So a single failed call
        does not cost the write it happened on: it answers the question
        permanently, wrongly, without anything ever re-asking. That is why the
        failure has to happen before anything is written, and this asserts the
        table is untouched rather than just that the call raised.
        """
        stub([*votes('{"many_values": true}'), RuntimeError("429 quota exhausted")])
        predicates.resolve(db, tenant.id, "subscribes_to", object_label="Priority plan")
        # Committed on purpose: this stands for a vocabulary an earlier write
        # already built. Left in flush it would be rolled back together with
        # the failure, and the test would prove nothing about what survives.
        db.commit()
        before = {c.canonical for c in clusters(db, tenant)}

        with pytest.raises(ResolutionUnavailable):
            predicates.resolve(db, tenant.id, "moved_to", object_label="Basic plan")
        db.rollback()

        assert {c.canonical for c in clusters(db, tenant)} == before
        surfaces = [a.surface for a in db.query(PredicateAlias)
                    .filter(PredicateAlias.tenant_id == tenant.id).all()]
        assert "moved_to" not in surfaces

    def test_a_failed_functionality_call_refuses_the_write(self, db, tenant, stub):
        """Undecided is recoverable, unasked is not.

        The classifier is allowed to answer "I cannot tell", and None means
        "never supersede", which leaves both facts standing and is the safe
        direction. But that answer is stored once per cluster and never
        revisited, so a call that fails and is recorded as None disables the
        supersede for that relation for the life of the install.
        """
        stub([RuntimeError("429 quota exhausted")])
        with pytest.raises(ResolutionUnavailable):
            predicates.resolve(db, tenant.id, "on_plan", object_label="Business plan")
        db.rollback()
        assert clusters(db, tenant) == []

    @pytest.mark.parametrize("answer,expected", [
        ('{"many_values": false}', True),    # one value at a time -> functional
        ('{"many_values": true}', False),
    ])
    def test_functionality_answers(self, db, tenant, stub, answer, expected):
        stub(votes(answer))
        _, functional = predicates.resolve(db, tenant.id, "on_plan",
                                           object_label="Business plan")
        assert functional is expected

    @pytest.mark.parametrize("answer", ['{"many_values": "no"}', '{}', 'garbage'])
    def test_an_unreadable_cardinality_is_never_kept_as_undecided(self, db, tenant, stub, answer):
        """"Undecided" was stored on the relation and never asked again, and an
        undecided relation never closes an old value: the road to I1. Now an
        unreadable answer is no vote, it is asked again, and three in a row
        refuse the write with nothing cached."""
        stub([answer, answer, answer])
        with pytest.raises(ResolutionUnusable):
            predicates.resolve(db, tenant.id, "on_plan", object_label="Business plan")
        db.rollback()
        assert clusters(db, tenant) == []

    def test_an_unreadable_vote_is_asked_again_and_the_majority_stands(self, db, tenant, stub):
        p = stub(['garbage', *votes('{"many_values": false}')])
        _, functional = predicates.resolve(db, tenant.id, "on_plan", object_label="Business plan")
        assert functional is True
        assert len(p.functional_calls) == 3, "garbage, then two readable votes"


class TestBoundaries:
    def test_unknown_short_circuits(self, db, tenant, stub):
        """A degenerate predicate is not a relation: do not spend a call on it."""
        p = stub()
        canonical, functional = predicates.resolve(db, tenant.id, "a")
        assert canonical == "unknown"
        assert functional is None
        assert p.prompts == []
        assert clusters(db, tenant) == []

    def test_one_tenant_never_sees_another_vocabulary(self, db, stub):
        from korely_agent.db.models import Tenant

        a = Tenant(id=uuid.uuid4(), name="A")
        b = Tenant(id=uuid.uuid4(), name="B")
        db.add_all([a, b])
        db.commit()

        p = stub([*votes('{"many_values": false}'), *votes('{"many_values": false}')])
        predicates.resolve(db, a.id, "covered_under", object_label="Policy 7")
        predicates.resolve(db, b.id, "covered_under", object_label="Policy 7")

        # B was never offered A's cluster, so it was never asked to choose.
        assert p.resolution_calls == []
        assert len(clusters(db, a)) == 1
        assert len(clusters(db, b)) == 1
        assert (db.query(PredicateAlias)
                .filter(PredicateAlias.tenant_id == b.id).count()) == 1

    def test_a_missing_embedding_still_resolves(self, db, tenant, stub):
        """No embedder (no Google key set, as in this suite) means no
        shortlist, not a crash: the store works, it does not merge."""
        p = stub(votes('{"many_values": true}'), embedding=None)
        canonical, _ = predicates.resolve(db, tenant.id, "likes", object_label="fish")
        assert canonical == "likes"
        assert p.resolution_calls == []

    @pytest.mark.parametrize("failure", ["returns-none", "raises"])
    def test_a_failed_embedding_refuses_the_write(self, db, tenant, stub, monkeypatch, failure):
        """With a key set, an embedding that fails is a failed resolution.

        memory_core 1.2.0: on Postgres the vector is the
        shortlist of the relations a phrase could be and the centroid of a new
        one, so it raises ResolutionUnavailable when `embed` raises, and
        reads an `embed` returning None as "no embedder". `core.llm.embed`
        returns None when its call fails, so the adapter has to turn that into
        a raise: it did not, and the phrase was filed as a new relation with no
        centroid, which no later phrase can ever join. `raises` passed before
        the fix too; `returns-none` is what the real `embed` does.
        """
        def failing(text, **kw):
            if failure == "raises":
                raise RuntimeError("503 embedding backend")
            return None

        stub(votes('{"many_values": true}'))
        monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
        monkeypatch.setattr("korely_agent.core.llm.embed", failing)
        with pytest.raises(ResolutionUnavailable):
            predicates.resolve(db, tenant.id, "likes", object_label="fish")
        db.rollback()
        assert clusters(db, tenant) == []
        assert (db.query(PredicateAlias)
                .filter(PredicateAlias.tenant_id == tenant.id).count()) == 0

    def test_distant_clusters_are_not_offered(self, db, tenant, monkeypatch):
        """Beyond the cutoff the shortlist would be noise rather than a shortlist."""
        p = FakeProvider([*votes('{"many_values": true}'), *votes('{"many_values": true}')])
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: p)
        # With no embedder there is no shortlist at all, and the assertion
        # below would hold without ever measuring a distance.
        an_embedder_is_configured(monkeypatch)

        # Two orthogonal unit vectors: cosine distance 1.0, past the shortlist's
        # cut on any model (memory_core.thresholds).
        first = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
        second = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)
        seq = iter([first, second])
        monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: next(seq))

        predicates.resolve(db, tenant.id, "likes", object_label="fish")
        predicates.resolve(db, tenant.id, "works_at", object_label="Apple")

        assert p.resolution_calls == [], "offered a cluster past the distance cutoff"
        assert len(clusters(db, tenant)) == 2


class TestTheShortlistCutIsTheModels:
    """memory_core 2.4.0: the distance past which a known relation is not
    offered belongs to the model that made the vectors. The same two phrases,
    0.66 apart: past Gemini's 0.65, inside EmbeddingGemma's 0.67."""

    @pytest.mark.parametrize("model, offered", [
        ("gemini-embedding-2-preview", False),
        ("embeddinggemma-300m", True),
    ])
    def test_the_same_distance_on_two_models(self, db, tenant, monkeypatch, model, offered):
        import math

        from korely_agent.services.memory_core import thresholds

        script = [*votes('{"many_values": true}')]
        script += ['{"same_as": null}'] if offered else []
        script += [*votes('{"many_values": true}')]
        p = FakeProvider(script)
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: p)
        an_embedder_is_configured(monkeypatch)
        cos = 1.0 - 0.66
        first = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
        second = [cos, math.sqrt(1.0 - cos * cos)] + [0.0] * (EMBEDDING_DIM - 2)
        seq = iter([first, second])
        monkeypatch.setattr("korely_agent.core.llm.embed", lambda text, **kw: next(seq))
        thresholds.use_model(model)

        predicates.resolve(db, tenant.id, "likes", object_label="fish")
        predicates.resolve(db, tenant.id, "lives_in", object_label="Milan")

        assert bool(p.resolution_calls) is offered, (
            f"on {model} a relation 0.66 away was {'not ' if offered else ''}offered")
        assert len(clusters(db, tenant)) == 2


def _question(surface, object_label, shortlist):
    """The question the vocabulary puts to the model, from the engine this
    product ships (memory_core.vocabulary); the product's wrapper of it,
    used by these tests alone, went on 2026-10-06."""
    from korely_agent.services.memory_core import vocabulary

    return vocabulary.same_relation_question(surface, object_label,
                                                [c.canonical for c in shortlist])


class TestTheQuestionItself:
    """The prompt is a tuning decision with a measured consequence."""

    def test_the_bias_toward_not_merging_is_present(self):
        """Ten runs produced zero false merges, and this line is why.

        It is asserted rather than assumed because removing it should be a
        deliberate act with a re-measurement behind it, not something that
        disappears in an edit. The counterfactual arm of
        the vocabulary's measurement drops exactly this line.
        """
        cluster = PredicateCluster(canonical="plan")
        prompt = _question("subscribes_to", "Business plan",
                                              [cluster])
        assert "Prefer null when unsure" in prompt
        assert "loses\ninformation permanently" in prompt or \
               "loses information permanently" in prompt

    def test_the_options_are_numbered_from_one(self):
        """`same_as` is 1-based; an off-by-one here merges the wrong cluster."""
        a = PredicateCluster(canonical="plan")
        b = PredicateCluster(canonical="lives_in")
        prompt = _question("subscribes_to", "", [a, b])
        assert "1. plan" in prompt
        assert "2. lives_in" in prompt

    def test_the_argument_is_offered_as_side_information(self):
        """"has plan" alone is a worse question than "has plan Business plan"."""
        cluster = PredicateCluster(canonical="plan")
        with_arg = _question("has_plan", "Business plan", [cluster])
        without = _question("has_plan", "", [cluster])
        assert "Business plan" in with_arg
        assert "Business plan" not in without


class TestWhichCallIsAllowedToStopThinking:
    """One of these two calls may skip reasoning and the other may not.

    Both were switched off together on 2026-09-10 after measuring merging and
    separation, and the third axis, whether a relation holds one value at a
    time, was not measured. It degrades: 18/18 relations classified correctly
    with thinking, 16/18 without, and the two lost are `on_plan` and
    `signed_up_for`. A subscription plan classified as multi-valued means the
    deterministic supersede never fires and one customer is on three plans at
    once.

    Asserted here rather than left in a comment because the two calls sit forty
    lines apart and look identical, so the next person to tidy them will make
    them consistent.
    """

    def _reasoning_used(self, db, tenant, stub, surface, object_label, answers):
        seen = {}
        p = stub(answers)
        real = p.complete

        def spy(messages, **kw):
            seen[_which(messages[0]["content"])] = kw.get("reasoning", True)
            return real(messages, **kw)

        p.complete = spy
        predicates.resolve(db, tenant.id, surface, object_label=object_label)
        return seen

    def test_the_functional_question_keeps_its_reasoning(self, db, tenant, stub):
        seen = self._reasoning_used(db, tenant, stub, "on_plan", "Business plan",
                                    votes('{"many_values": false}'))
        assert seen.get("functional") is True, (
            "thinking was turned off for the call that decides whether a "
            "relation is single-valued; measured, that costs on_plan and "
            "signed_up_for, and with them the supersede"
        )

    def test_the_merge_question_does_not(self, db, tenant, stub):
        seen = self._reasoning_used(db, tenant, stub, "has_plan", "Business",
                                    votes('{"many_values": false}'))
        # First phrase: nothing to choose between, so no merge question was
        # asked at all. Drive a second one to see how it is asked.
        assert "resolution" not in seen, seen
        seen2 = self._reasoning_used(db, tenant, stub, "subscribes_to", "Business",
                                     ['{"same_as": 1}'])
        assert seen2.get("resolution") is False, (
            "the merge question is paying for a chain of thought it does not "
            "need: measured at 8.2s and 1608 thinking tokens to answer '1'"
        )


def _which(prompt: str) -> str:
    if "many_values" in prompt:
        return "functional"
    if "Known relations:" in prompt:
        return "resolution"
    return "other"


class TestTheCardinalityIsDecidedByMajority:
    """I1, the blind install of 0.1.13: whether `subscribes_to` holds one value
    was asked once, came back "many" about once in five, and was kept, so no
    plan change in that tenant ever closed. memory_core 2.1.0 asks about the
    kind of value seen and decides by majority; this module passes the votes."""

    def test_two_that_agree_are_enough(self, db, tenant, stub):
        p = stub(votes('{"many_values": false}') + ['{"many_values": true}'])
        _, functional = predicates.resolve(db, tenant.id, "subscribes_to",
                                           object_label="Priority Support plan")
        assert functional is True
        assert len(p.functional_calls) == 2, "asked a third time after two agreed"

    def test_a_split_is_settled_by_a_third(self, db, tenant, stub):
        p = stub(['{"many_values": true}', '{"many_values": false}', '{"many_values": false}'])
        _, functional = predicates.resolve(db, tenant.id, "subscribes_to",
                                           object_label="Priority Support plan")
        assert functional is True
        assert len(p.functional_calls) == 3

    def test_the_question_names_the_value_it_was_seen_with(self, db, tenant, stub):
        p = stub(votes('{"many_values": false}'))
        predicates.resolve(db, tenant.id, "subscribes_to", object_label="Priority Support plan")
        assert 'of the same kind as "Priority Support plan"' in p.functional_calls[0]


class TestReclassifyingWhatAnAccountAlreadyLearned:
    """`manage reclassify-relations`: an install that learned "many values" for
    a plan before memory_core 2.1.0 does not heal by itself."""

    @pytest.fixture(autouse=True)
    def wired(self, db, monkeypatch):
        from korely_agent import manage

        monkeypatch.setattr(manage, "_session", lambda: db)
        monkeypatch.setattr(db, "close", lambda: None)   # the command closes its session

    def learned(self, db, tenant, canonical, functional, example):
        c = PredicateCluster(tenant_id=tenant.id, canonical=canonical, is_functional=functional,
                             family="ownership", member_count=1, example_object=example)
        db.add(c)
        db.commit()
        return c

    def test_a_dry_run_says_what_would_change_and_writes_nothing(self, db, tenant, stub, capsys):
        from korely_agent import manage

        c = self.learned(db, tenant, "subscribes_to", False, "Priority Support plan")
        stub(votes('{"many_values": false}'))
        assert manage.main(["reclassify-relations", "--tenant", str(tenant.id)]) == 0
        out = capsys.readouterr().out
        assert "subscribes_to" in out and "many values -> one value" in out and "would change" in out
        db.refresh(c)
        assert c.is_functional is False, "a dry run wrote"

    def test_apply_writes_the_new_answer(self, db, tenant, stub, capsys):
        from korely_agent import manage

        c = self.learned(db, tenant, "subscribes_to", False, "Priority Support plan")
        stub(votes('{"many_values": false}'))
        assert manage.main(["reclassify-relations", "--tenant", str(tenant.id), "--apply"]) == 0
        db.refresh(c)
        assert c.is_functional is True
        assert "1 changed" in capsys.readouterr().out

    def test_a_relation_learned_without_a_value_is_not_asked(self, db, tenant, stub):
        from korely_agent import manage

        c = self.learned(db, tenant, "knows", True, None)
        p = stub(votes('{"many_values": true}'))
        assert manage.main(["reclassify-relations", "--tenant", str(tenant.id), "--apply"]) == 0
        assert p.functional_calls == [], "asked the general question, the one that was wrong"
        db.refresh(c)
        assert c.is_functional is True

    def test_with_no_model_key_it_stops_and_says_so(self, db, tenant, monkeypatch, capsys):
        from korely_agent import manage

        self.learned(db, tenant, "subscribes_to", False, "Priority Support plan")
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
        assert manage.main(["reclassify-relations", "--tenant", str(tenant.id)]) == 1
        assert "no model key" in capsys.readouterr().err


class TestTheVocabularyModel:
    """VOCABULARY_MODEL (2026-10-01): the relation vocabulary on a model of its
    own, as the hosted product runs it (extraction and judge on Flash Lite,
    the vocabulary on Flash). Unset, the call names no model and the
    provider's EXTRACTION_MODEL answers, exactly as before."""

    class _Recording:
        def __init__(self):
            self.models = []

        def complete(self, messages, **kw):
            self.models.append(kw.get("model"))
            return LLMResult(text='{"same_as": null}')

    def _ask(self, monkeypatch):
        from korely_agent.services import predicates

        p = self._Recording()
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: p)
        predicates._ask()("which relation is this?", False)
        return p.models

    def test_unset_it_names_no_model(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost:5432/z")
        monkeypatch.delenv("VOCABULARY_MODEL", raising=False)
        assert self._ask(monkeypatch) == [None]

    def test_set_it_is_the_one_asked(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@localhost:5432/z")
        monkeypatch.setenv("VOCABULARY_MODEL", "gemini-3.6-flash")
        assert self._ask(monkeypatch) == ["gemini-3.6-flash"]


class TestTheAnswerIsReadWhole:
    """The vocabulary's answer went through `core.llm.extract_json` before the
    engine's reader, and that cut starts at the first bracket of either kind:
    'Answer [see note]: {"same_as": 2}' became '[see note]', no answer, and
    with `severe` a write refused for an answer that could be read (audit of
    17 Oct 2026). Read with the engine's rule for a model's JSON
    (memory_core.model_json.read_object, CONTRIBUTING.md)."""

    class _Answering:
        def __init__(self, text):
            self.text = text

        def complete(self, messages, **kw):
            return LLMResult(text=self.text)

    def _read(self, monkeypatch, text):
        from korely_agent.services.memory_core import vocabulary

        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: self._Answering(text))
        return vocabulary._json(predicates._ask()("which relation is this?", False))

    def test_a_bracket_before_the_object_does_not_hide_it(self, monkeypatch):
        assert self._read(monkeypatch, 'Answer [see note]: {"same_as": 2}') == {"same_as": 2}

    def test_the_shapes_read_before_still_read(self, monkeypatch):
        assert self._read(monkeypatch, '{"same_as": null}') == {"same_as": None}
        assert self._read(monkeypatch, '```json\n{"same_as": 1}\n```') == {"same_as": 1}
        assert self._read(monkeypatch, 'Sure:\n{"many_values": false}\nDone.') == {"many_values": False}

    def test_two_different_objects_are_no_answer(self, monkeypatch):
        assert self._read(monkeypatch, '{"same_as": 1} or {"same_as": 2}') == {}


class TestAProjectsVocabularyIsAskedInItsRegion:
    """`manage reclassify-relations` and `manage review-vocabulary` asked the
    model with no region, so the install's default writer read the phrases and
    example values of an account whose projects write in EU (audit of 17 Oct
    2026; CONTRIBUTING.md: every door that calls a model reads its region,
    never global by forgetting). The vocabulary is one per project since
    18 Oct 2026, so each is asked in its project's region (until then the
    account's: EU when any project wrote in EU)."""

    @pytest.fixture
    def asked(self, monkeypatch):
        from korely_agent.core import llm

        regions_asked = []

        def provider():
            regions_asked.append(llm._region.get())
            return FakeProvider(votes('{"many_values": false}'))

        monkeypatch.setattr(llm, "get_provider", provider)
        monkeypatch.setattr(llm, "default_region", lambda: "global")
        return regions_asked

    def project(self, db, tenant, region):
        from korely_agent.db.models import AgentProject

        p = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name=f"p-{uuid.uuid4().hex[:6]}",
                         slug=uuid.uuid4().hex[:8], region=region)
        db.add(p)
        db.commit()
        return p.id

    def test_each_project_is_asked_in_its_own_region(self, db, tenant, asked):
        world = self.project(db, tenant, "global")
        europe = self.project(db, tenant, "eu")
        predicates.reclassify(db, tenant.id, project_id=world, end_user_id=None)
        predicates.review_variants(db, tenant.id, project_id=europe, end_user_id=None)
        assert asked == ["global", "eu"]

    def test_an_account_all_global_is_asked_in_global(self, db, tenant, asked):
        self.project(db, tenant, "global")
        predicates.reclassify(db, tenant.id, end_user_id=None)
        assert asked == ["global"]

    def test_a_project_that_follows_an_install_writing_in_eu_is_asked_in_eu(
            self, db, tenant, asked, monkeypatch):
        from korely_agent.core import llm

        monkeypatch.setattr(llm, "default_region", lambda: "eu")
        self.project(db, tenant, "global")
        follows = self.project(db, tenant, None)
        predicates.reclassify(db, tenant.id, project_id=follows, end_user_id=None)
        assert asked == ["eu"]

    def test_a_region_with_no_writer_asks_nobody_else(self, db, tenant, monkeypatch, capsys):
        from korely_agent import manage
        from korely_agent.core import llm

        from korely_agent.db.models import PredicateCluster

        europe = self.project(db, tenant, "eu")
        db.add(PredicateCluster(tenant_id=tenant.id, project_id=europe, canonical="subscribes_to"))
        db.commit()
        monkeypatch.setattr(llm, "default_region", lambda: "global")
        monkeypatch.setattr(llm, "get_provider",
                            lambda: None if llm._region.get() == "eu" else FakeProvider([]))
        monkeypatch.setattr(manage, "_session", lambda: db)
        monkeypatch.setattr(db, "close", lambda: None)
        assert manage.main(["reclassify-relations", "--tenant", str(tenant.id)]) == 1
        err = capsys.readouterr().err
        assert "no model key" in err and "'eu'" in err
