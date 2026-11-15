"""The fact engine, against real Postgres.

These are the behaviours the product is sold on: a fact has a validity window, a
newer fact supersedes an older one instead of overwriting it, `as_of` answers
over the world's timeline, and erasure erases. Everything else in the repository
is scaffolding around these.

No LLM is involved. Extraction is exercised elsewhere; what is tested here is
the store, which is the part that has to be right on a client's server at three
in the morning.
"""
from datetime import datetime, timedelta, timezone

import pytest

from korely_agent.db.models import AgentFact
from korely_agent.services import facts
from korely_agent.services.sentinels import EVERY_PROJECT


NOW = datetime.now(timezone.utc)


def _read(db, tenant_id, **kw):
    """retrieve_agent_facts returns (rows, total); tests care about the rows."""
    kw.setdefault("project_id", EVERY_PROJECT)
    rows, _total = facts.retrieve_agent_facts(db, tenant_id, **kw)
    return rows


def _write(db, tenant, subject, predicate, obj, *, end_user="customer-4821", when=None):
    return facts.write_fact_direct(
        db, tenant.id, subject=subject, predicate=predicate, obj=obj,
        end_user_id=end_user, valid_from=when or NOW,
    )


class TestWriteAndRead:
    def test_a_written_fact_comes_back(self, db, tenant):
        _write(db, tenant, "Customer 4821", "is_on_plan", "Business")
        out = _read(db, tenant.id, end_user_id="customer-4821")
        assert len(out) == 1
        f = out[0]
        assert (f.subject_label, f.object_label) == ("Customer 4821", "Business")

    def test_the_raw_predicate_survives_canonicalization(self, db, tenant):
        """Both forms are kept, and they are not the same thing: the words the
        extraction produced are what an operator reads when auditing why a
        fact exists. The canonical form changes the spelling only (memory_core
        3.3.0: no English prefix is stripped any more, so "is_on_plan" stays
        itself; that it means "on_plan" is the learned vocabulary's question)."""
        _write(db, tenant, "Customer 4821", "Is On Plan", "Business")
        f = _read(db, tenant.id, end_user_id="customer-4821")[0]
        assert f.predicate_raw == "Is On Plan"
        assert f.predicate_canonical == "is_on_plan"

    def test_spelling_variants_collapse_to_one_key(self, db, tenant):
        """The property the contradiction engine depends on: one phrase in two
        spellings is one key."""
        _write(db, tenant, "A", "on_plan", "Business", end_user="u1")
        _write(db, tenant, "B", "On Plan", "Business", end_user="u2")
        keys = {f.predicate_canonical for f in _read(db, tenant.id)}
        assert keys == {"on_plan"}

    def test_tenants_cannot_see_each_other(self, db, tenant):
        """The isolation an agency is buying.

        One install, several of their clients. If a missing filter leaked rows
        across tenants, the failure would be a data breach reported by a
        customer, not a test.
        """
        from korely_agent.db.models import Tenant
        import uuid

        other = Tenant(id=uuid.uuid4(), name="Other Agency Client")
        db.add(other); db.flush()

        _write(db, tenant, "Customer 4821", "is_on_plan", "Business")
        assert _read(db, other.id, end_user_id="customer-4821") == []

    def test_end_users_are_isolated_within_a_tenant(self, db, tenant):
        _write(db, tenant, "A", "likes", "peaches", end_user="alice")
        _write(db, tenant, "B", "likes", "bananas", end_user="bob")
        got = _read(db, tenant.id, end_user_id="alice")
        assert [f.object_label for f in got] == ["peaches"]


class TestBiTemporal:
    """What was true when, which is the whole reason this store exists."""

    def test_nothing_supersedes_until_the_relation_is_classified(self, db, tenant):
        """An unclassified relation never retires a fact.

        Cardinality is not a list in this codebase any more: it is learned per
        tenant and stored on the predicate cluster. Until the classification has
        run, `is_functional` is NULL, and NULL means leave both facts standing.

        That is the safe direction and it is deliberate. Two facts coexisting is
        a state you can inspect and repair; a true fact deleted because a
        relation was wrongly assumed single-valued is not recoverable, and
        nobody would notice it happening.
        """
        _write(db, tenant, "Customer 4821", "on_plan", "Business",
               when=NOW - timedelta(days=120))
        _write(db, tenant, "Customer 4821", "on_plan", "Standard",
               when=NOW - timedelta(days=30))
        active = _read(db, tenant.id, end_user_id="customer-4821")
        assert {f.object_label for f in active} == {"Business", "Standard"}

    def test_a_classified_single_valued_relation_goes_to_the_judge_typed(self, db, tenant, monkeypatch):
        """Once the vocabulary knows the relation holds one value, the judge is
        told so, together with the value the cardinality was measured on.

        It used to close the old value by rule, without a judge. Removed on
        2026-09-27: cardinality is typed by the value (Lin, Mausam, Etzioni,
        EMNLP 2010 §2: "published in" is single-valued for a year and
        multi-valued for a language), and on functional relations most apparent
        contradictions are not real (Ritter et al., EMNLP 2008: 1.2% genuine).
        The hosted MCP acceptance run showed it: "subscribes_to Slack" closed by
        "subscribes_to Starter plan". The judge sees both values and decides.
        """
        from korely_agent.db import PredicateCluster
        from korely_agent.services.facts import contradiction as contra

        _write(db, tenant, "Customer 4821", "on_plan", "Business",
               when=NOW - timedelta(days=120))
        cluster = (
            db.query(PredicateCluster)
            .filter(PredicateCluster.tenant_id == tenant.id)
            .order_by(PredicateCluster.created_at.desc())
            .first()
        )
        assert cluster is not None, "writing a fact must create its predicate cluster"
        assert cluster.example_object == "Business"
        cluster.is_functional = True
        db.commit()

        seen = {}

        def judge(candidates, new_subject, new_predicate_raw, new_object,
                  db_, tenant_id_, model_name, **kw):
            seen["new"] = (kw.get("new_functional"), kw.get("new_example"))
            seen["tags"] = [contra._single_value(db_, tenant_id_, c.predicate_canonical,
                                                  c.project_id, c.end_user_id) for c in candidates]
            return [c.id for c in candidates], {}

        monkeypatch.setattr(contra, "_classify", judge)
        _write(db, tenant, "Customer 4821", "on_plan", "Standard",
               when=NOW - timedelta(days=30))

        assert seen == {"new": (True, "Business"), "tags": [(True, "Business")]}
        active = _read(db, tenant.id, end_user_id="customer-4821")
        assert [f.object_label for f in active] == ["Standard"]
        everything = _read(db, tenant.id, end_user_id="customer-4821",
                           include_invalidated=True)
        old = next(f for f in everything if f.object_label == "Business")
        assert old.invalid_at is not None, (
            "the superseded fact must keep the moment it stopped being true"
        )

    def test_without_a_model_even_a_single_valued_change_keeps_both(self, db, tenant):
        """No judge, no closing: two facts standing is recoverable, a true fact
        closed by a rule that misread the kind of value is not."""
        from korely_agent.db import PredicateCluster

        _write(db, tenant, "Customer 4821", "on_plan", "Business",
               when=NOW - timedelta(days=120))
        cluster = db.query(PredicateCluster).filter(PredicateCluster.tenant_id == tenant.id).first()
        cluster.is_functional = True
        db.commit()
        _write(db, tenant, "Customer 4821", "on_plan", "Standard",
               when=NOW - timedelta(days=30))
        active = _read(db, tenant.id, end_user_id="customer-4821")
        assert {f.object_label for f in active} == {"Business", "Standard"}

    def test_multi_valued_relations_still_accumulate(self, db, tenant):
        """The other half: liking two things is not a contradiction.

        A rule that superseded here would delete true information, which is the
        expensive direction to be wrong in.
        """
        _write(db, tenant, "Maria", "likes", "fish", end_user="maria")
        _write(db, tenant, "Maria", "likes", "meat", end_user="maria")
        active = _read(db, tenant.id, end_user_id="maria")
        assert {f.object_label for f in active} == {"fish", "meat"}

    def test_without_a_model_a_non_functional_change_is_left_alone(self, db, tenant):
        """No API key means no judgement for the relations that need it.

        Stage one narrows candidates in SQL; stage two asks a model whether the
        new fact actually contradicts them. With no provider the second stage
        cannot run, and the store keeps both facts rather than guessing which to
        retire. Worth pinning: an operator running without a key gets an
        accumulating store, not a silently wrong one, and `as_of` still works
        because both facts carry their own valid_from.
        """
        # A relation the schema says nothing about: whether "prefers Milan" is
        # replaced by "prefers Rome" is a judgement call, and without a provider
        # nobody makes it.
        _write(db, tenant, "Customer 4821", "prefers_office", "Milan",
               when=NOW - timedelta(days=120))
        _write(db, tenant, "Customer 4821", "prefers_office", "Rome",
               when=NOW - timedelta(days=30))
        active = _read(db, tenant.id, end_user_id="customer-4821")
        assert {f.object_label for f in active} == {"Milan", "Rome"}
        assert all(f.invalid_at is None for f in active)

    def test_a_new_fact_supersedes_rather_than_overwrites(self, db, tenant, monkeypatch):
        """With a model available, the older fact is retired, not deleted."""
        from korely_agent.services.facts import contradiction as contra

        def judge(candidates, new_subject, new_predicate_raw, new_object,
                  db_, tenant_id_, model_name, **kw):
            # Stand in for stage two, which is the only part that needs a model.
            # Stage one has already narrowed to same-subject, same-predicate
            # candidates in SQL; here we say yes to all of them.
            return [c.id for c in candidates], {}

        monkeypatch.setattr(contra, "_classify", judge)
        march = NOW - timedelta(days=120)
        june = NOW - timedelta(days=30)
        _write(db, tenant, "Customer 4821", "is_on_plan", "Business", when=march)
        _write(db, tenant, "Customer 4821", "is_on_plan", "Standard", when=june)

        active = _read(db, tenant.id, end_user_id="customer-4821")
        assert [f.object_label for f in active] == ["Standard"]

        everything = _read(db, tenant.id, end_user_id="customer-4821", include_invalidated=True
        )
        assert {f.object_label for f in everything} == {"Business", "Standard"}

        superseded = next(f for f in everything if f.object_label == "Business")
        assert superseded.invalid_at is not None, (
            "the old fact must carry the moment it stopped being true, or the "
            "record cannot answer why the agent said what it said"
        )

    def test_as_of_answers_over_the_worlds_timeline(self, db, tenant):
        """The support-ticket question: was the bot wrong, or right at the time?"""
        march = NOW - timedelta(days=120)
        june = NOW - timedelta(days=30)
        _write(db, tenant, "Customer 4821", "is_on_plan", "Business", when=march)
        _write(db, tenant, "Customer 4821", "is_on_plan", "Standard", when=june)

        back_then = _read(db, tenant.id, end_user_id="customer-4821",
            as_of=NOW - timedelta(days=90),
        )
        assert [f.object_label for f in back_then] == ["Business"]

    def test_as_of_before_anything_existed_is_empty(self, db, tenant):
        _write(db, tenant, "Customer 4821", "is_on_plan", "Business",
               when=NOW - timedelta(days=10))
        assert _read(db, tenant.id, end_user_id="customer-4821",
            as_of=NOW - timedelta(days=365),
        ) == []


class TestErasure:
    def test_forget_end_user_actually_removes_the_rows(self, db, tenant):
        """Article 17, verified by counting rather than asserted.

        Read it back the most permissive way available and count. Anything above
        zero would mean the data was hidden, not erased, and the published
        example invites customers to run exactly this check.
        """
        _write(db, tenant, "Customer 4821", "is_on_plan", "Business")
        _write(db, tenant, "Customer 4821", "lives_in", "Milan")

        facts.forget_end_user(db, tenant.id, "customer-4821", project_id=EVERY_PROJECT)

        left = _read(db, tenant.id, end_user_id="customer-4821", include_invalidated=True
        )
        assert left == []
        rows = db.query(AgentFact).filter(
            AgentFact.tenant_id == tenant.id,
            AgentFact.end_user_id == "customer-4821",
        ).count()
        assert rows == 0, "rows still present: this was a hide, not an erase"

    def test_erasure_does_not_touch_other_end_users(self, db, tenant):
        _write(db, tenant, "A", "likes", "peaches", end_user="alice")
        _write(db, tenant, "B", "likes", "bananas", end_user="bob")
        facts.forget_end_user(db, tenant.id, "alice", project_id=EVERY_PROJECT)
        assert len(_read(db, tenant.id, end_user_id="bob")) == 1


class TestFilters:
    def test_by_predicate(self, db, tenant):
        _write(db, tenant, "Customer 4821", "is_on_plan", "Business")
        _write(db, tenant, "Customer 4821", "lives_in", "Milan")
        got = _read(db, tenant.id, end_user_id="customer-4821", predicate="lives_in"
        )
        assert [f.object_label for f in got] == ["Milan"]

    def test_entity_matches_subject_or_object(self, db, tenant):
        _write(db, tenant, "Customer 4821", "lives_in", "Milan")
        _write(db, tenant, "Milan", "is_in", "Italy")
        got = _read(db, tenant.id, end_user_id="customer-4821", entity="Milan"
        )
        assert len(got) == 2, "entity must match both sides of the triple"


class TestExtractionFailureIsNotEmptiness:
    """A broken reply must not look like a memory with nothing in it.

    Second instance of the same conflation, found by an end-to-end run: the
    model's JSON came back truncated because thinking tokens had eaten the
    output budget, the parser returned None, the caller read that as [] and the
    memory was marked done with zero facts. From outside, a successful write
    that learned nothing.
    """

    @staticmethod
    def _replying(text):
        class _Client:
            name = "stub"

            def complete(self, **kw):
                class R:
                    pass
                R.text = text
                return R()
        return _Client()

    @pytest.mark.parametrize("reply", ['{"facts": [{"subject": "acme",', ""])
    def test_the_caller_does_not_read_it_as_no_facts(self, monkeypatch, reply):
        """What the docstring above is about: the parser returned None and the
        CALLER read that as []. So the assertion is on
        `extract_facts_and_observation`, the entry the write uses, for a
        reply cut off by the token ceiling and for an empty one (which went on slipping through as "no facts" until
        2026-09-27).

        There was a test here of `_complete_json`, a helper that read one
        reply the same way and that nothing in the product called any more:
        it passed on a path no write takes. The helper went on 2026-09-28."""
        from korely_agent.services.facts import extractor
        from korely_agent.services.memory_core import fact_extractor

        monkeypatch.setattr(extractor, "_select_provider",
                            lambda model_name: (self._replying(reply), "m"))
        with pytest.raises(fact_extractor.ExtractionFailed):
            extractor.extract_facts_and_observation("Acme moved to the Basic plan.",
                                                    end_user_id="c4821", db=None, tenant_id=None)

    def test_token_ceiling_is_not_the_default(self):
        """1024 was observed truncating a two-line memory."""
        from korely_agent.services.memory_core.fact_extractor import MAX_TOKENS

        assert MAX_TOKENS >= 4096, (
            "thinking tokens count against the output budget; a ceiling this "
            "low truncates the JSON before the fact list closes"
        )
