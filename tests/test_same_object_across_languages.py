"""One thing named in two languages or forms is one object (memory_core 6.1.0, `objects`).

"lives in Italia" and "lives in Italy" were two objects: the extraction keeps
the memory's own words. With `facts.objects.SAME_OBJECT` on, a write's new
object is offered with the memory's open objects of the same relation family
its fact reads alike to, each in its context; the writer describes each, then
answers twice, framed two ways, and two "same" write a LINK: no fact is
rewritten, merged or reconfirmed, the reads show the two as one, and removing
the link leaves the memory exactly as before (decided on 30 Oct 2026, after
the review that found the first build's alias destructive).

No model is called: the writer is a fake that answers from a script and
writes down every prompt; the vectors are a fake that puts every fact of one
subject near the same place, so the shortlist is the memory's objects.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid

import pytest

from korely_agent.core.llm import LLMResult
from korely_agent.db.models import AgentEntityAlias, AgentFact, AgentMemory, AgentObjectLink
from korely_agent.services.facts import objects as product_objects
from korely_agent.services.facts import service as fsvc
from korely_agent.services.memory_core import objects as O
from korely_agent.services.memory_core import vocabulary
from korely_agent.services.memory_core.fact_extractor import ExtractedFact
from korely_agent.services.sentinels import EVERY_PROJECT
from tests.conftest import in_the_judges_shape, is_the_judge, stub_extraction


# ---------------------------------------------------------------------------
# the questions and their answers, no database
# ---------------------------------------------------------------------------

def _m(name, subject="Marco", relation="lives_in", family="places", words=None, description=None):
    return O.Mention(name=name, subject=subject, relation=relation, family=family, words=words,
                     description=description)


def _c(name, **kw):
    return O.Candidate(name=name, mention=_m(name, **kw))


def _answers(*entries):
    return json.dumps({"answers": [dict(mention=i, verdict=v, same_as=s) for i, (v, s) in enumerate(entries, 1)]})


class TestTheAnswers:
    def test_same_different_unsure(self):
        text = _answers(("same", 2), ("different", None), ("unsure", None))
        assert O.read_same_object(text, [3, 2, 2], severe=True) == [1, O.DIFFERENT, O.UNSURE]

    def test_the_verdict_decides_not_the_number(self):
        """A number under "different" unites nothing: the categorical field
        decides (CONTRIBUTING.md), `same_as` only says which."""
        assert O.read_same_object(_answers(("different", 1)), [2], severe=True) == [O.DIFFERENT]

    def test_a_boolean_is_not_a_candidate(self):
        with pytest.raises(vocabulary.UnreadableAnswer):
            O.read_same_object(_answers(("same", True)), [2], severe=True)
        assert O.read_same_object(_answers(("same", True)), [2]) == [O.UNSURE]

    def test_a_candidate_out_of_range_is_no_answer(self):
        with pytest.raises(vocabulary.UnreadableAnswer):
            O.read_same_object(_answers(("same", 3)), [2], severe=True)

    def test_a_number_written_as_digits_is_that_number(self):
        assert O.read_same_object(_answers(("same", "2")), [2], severe=True) == [1]

    def test_a_mention_left_out_is_no_answer(self):
        with pytest.raises(vocabulary.UnreadableAnswer):
            O.read_same_object(_answers(("same", 1)), [2, 2], severe=True)
        assert O.read_same_object(_answers(("same", 1)), [2, 2]) == [0, O.UNSURE]

    def test_two_different_answers_for_one_mention_are_no_answer(self):
        text = json.dumps({"answers": [{"mention": 1, "verdict": "same", "same_as": 1},
                                       {"mention": 1, "verdict": "different", "same_as": None}]})
        with pytest.raises(vocabulary.UnreadableAnswer):
            O.read_same_object(text, [2], severe=True)

    def test_another_word_is_no_answer(self):
        with pytest.raises(vocabulary.UnreadableAnswer):
            O.read_same_object(_answers(("probably", 1)), [2], severe=True)

    def test_no_answer_at_all_is_never_different(self):
        """The review's P3.12: an absent answer is no answer, never "apart"."""
        for nothing in (None, "", "{}", "not json"):
            with pytest.raises(vocabulary.UnreadableAnswer):
                O.read_same_object(nothing, [1], severe=True)
            assert O.read_same_object(nothing, [1]) == [O.UNSURE]
            with pytest.raises(vocabulary.UnreadableAnswer):
                O.read_pairs(nothing, 1, severe=True)
            with pytest.raises(vocabulary.UnreadableAnswer):
                O.read_descriptions(nothing, 1, severe=True)

    def test_the_descriptions(self):
        text = json.dumps({"descriptions": [{"mention": 2, "description": "the country"},
                                            {"mention": 1, "description": "a city in Bavaria"}]})
        assert O.read_descriptions(text, 2, severe=True) == ["a city in Bavaria", "the country"]
        with pytest.raises(vocabulary.UnreadableAnswer):
            O.read_descriptions(text, 3, severe=True)

    def test_the_pairs(self):
        text = json.dumps({"pairs": [{"pair": 1, "verdict": "different"}, {"pair": 2, "verdict": "same"}]})
        assert O.read_pairs(text, 2, severe=True) == [O.DIFFERENT, O.SAME]
        with pytest.raises(vocabulary.UnreadableAnswer):
            O.read_pairs(json.dumps({"pairs": [{"pair": 1, "verdict": "maybe"}]}), 1, severe=True)


class TestTheQuestions:
    def test_the_description_question_carries_every_context(self):
        q = O.describe_question([_m("Monaco", relation="ha vissuto a", words="Ho vissuto a Monaco per l'Erasmus"),
                                 _m("Principality of Monaco", relation="visited")])
        assert 'Mention 1: "Monaco"' in q and 'Mention 2: "Principality of Monaco"' in q
        assert "fact: Marco | ha vissuto a | Monaco (family: places)" in q
        assert 'memory\'s words: "Ho vissuto a Monaco per l\'Erasmus"' in q
        assert '{"descriptions": [{"mention": <number>, "description"' in q

    def test_the_selection_carries_contexts_and_descriptions(self):
        q = O.select_question([(
            _m("Parigi", relation="visited", words="Sono stato a Parigi per il weekend", description="the French capital"),
            [_c("Paris", relation="is_friends_with", family="people", words="My friend Paris helped me move",
                description="a friend of Marco")],
        )])
        assert 'New mention 1: "Parigi"' in q
        assert "fact: Marco | visited | Parigi (family: places)" in q
        assert 'description: "the French capital"' in q and 'description: "a friend of Marco"' in q
        assert '"verdict": "same" or "different" or "unsure"' in q

    def test_the_second_reading_is_framed_the_other_way_round(self):
        """Not the first reading's prompt (Peeters et al. 2025): the known
        object first, and the question is whether they are two things."""
        q = O.pair_question([(_c("Monaco", description="Munich, in Bavaria"),
                              _m("Principality of Monaco", description="the city-state"))])
        assert q.index('A: "Monaco"') < q.index('B: "Principality of Monaco"')
        assert "are A and B two different things?" in q
        assert '"verdict": "different" or "same" or "unsure"' in q
        assert "New mention" not in q

    def test_the_words_stay_on_one_line_and_short(self):
        q = O.select_question([(_m("Italy", words="line one\nline two " + "x" * 500), [_c("Italia")])])
        line = [x for x in q.splitlines() if x.strip().startswith("memory's words")][0]
        assert "line one line two" in line and len(line) < O.WORDS_MAX + 40


class TestTheWordsAround:
    def test_a_window_on_the_name(self):
        text = "a" * 300 + " Sono nato in Italia, vicino a Treviso. " + "b" * 300
        w = O.words_around(text, "italia", 80)
        assert "Italia" in w and len(w) <= 82

    def test_any_script(self):
        text = "x" * 300 + " いつか日本に行きたい " + "y" * 300
        assert "日本" in O.words_around(text, "日本", 60)

    def test_a_name_not_written_as_such_gives_the_start(self):
        assert O.words_around("short text", "Italy") == "short text"
        assert O.words_around("z" * 400, "Italy", 50).startswith("z" * 50)
        assert O.words_around(None, "Italy") is None


class _Script:
    """A writer that answers the three questions of `objects.decide` from a
    script, and every other question as a quiet vocabulary and judge would.

    `verdicts`: new mention's name -> the candidate's name (the selection's
    "same"), or "different" / "unsure". `pairs`: new mention's name -> the
    second reading's verdict ("same" unless said). `families`: relation as
    written -> its family ("places" unless said)."""

    def __init__(self, verdicts=None, pairs=None, families=None):
        self.verdicts = verdicts or {}
        self.pairs = pairs or {}
        self.families = families or {}
        self.questions = []       # every same-object prompt, in order
        self.regions = []

    def complete(self, messages, **kw):
        from korely_agent.core import llm

        prompt = messages[0]["content"]
        for marker in ('{"descriptions"', "New mention 1:", "Pair 1:"):
            if marker in prompt:
                self.questions.append(prompt)
                self.regions.append(llm._region.get())
                return LLMResult(text=self.answer(prompt))
        if is_the_judge(prompt):
            return LLMResult(text=in_the_judges_shape(prompt, '{"contradictions": []}'))
        if '"family"' in prompt:
            rel = re.search(r'Relation: "([^"]+)"', prompt).group(1)
            return LLMResult(text=json.dumps({"family": self.families.get(rel, "places")}))
        if "many_values" in prompt:
            return LLMResult(text='{"many_values": false}')
        if '"present"' in prompt:
            return LLMResult(text='{"present": null}')
        return LLMResult(text='{"same_as": null}')

    def answer(self, prompt):
        if '{"descriptions"' in prompt:
            names = re.findall(r'^Mention (\d+): "(.*)"$', prompt, re.M)
            return json.dumps({"descriptions": [{"mention": int(i), "description": f"{n}, as its words say"}
                                                for i, n in names]})
        if "Pair 1:" in prompt:
            news = re.findall(r'^  B: "(.*)"$', prompt, re.M)
            return json.dumps({"pairs": [{"pair": i, "verdict": self.pairs.get(n, "same")}
                                         for i, n in enumerate(news, 1)]})
        out = []
        for i, (name, cands) in enumerate(parse(prompt), 1):
            want = self.verdicts.get(name, "different")
            if want in cands:
                out.append({"mention": i, "verdict": "same", "same_as": cands.index(want) + 1})
            else:
                out.append({"mention": i, "verdict": want if want in ("different", "unsure") else "different",
                            "same_as": None})
        return json.dumps({"answers": out})

    def selections(self):
        return [q for q in self.questions if "New mention 1:" in q]


def parse(prompt):
    """[(new mention's name, [its candidates' names])] from the selection."""
    out = []
    for line in prompt.splitlines():
        m = re.match(r'^New mention \d+: "(.*)"$', line)
        if m:
            out.append((m.group(1), []))
            continue
        c = re.match(r'^    \d+\. "(.*)"$', line)
        if c and out:
            out[-1][1].append(c.group(1))
    return out


def _ask(w):
    return lambda p, t: w.complete([{"content": p}]).text


class TestDecide:
    def test_describe_select_then_the_pair_alone(self):
        w = _Script({"Italy": "Italia"})
        got = O.decide(_ask(w), [(_m("Italy"), [_c("Padova"), _c("Italia")]), (_m("Bobby"), [_c("Padova")])])
        assert [d.answer for d in got] == [1, O.DIFFERENT]
        assert len(w.questions) == 4
        assert '{"descriptions"' in w.questions[0] and '{"descriptions"' in w.questions[1]
        assert 'B: "Italy"' in w.questions[3] and 'A: "Italia"' in w.questions[3]
        assert "Bobby" not in w.questions[3], "the second reading sees the chosen pair alone"

    def test_each_context_is_described_once_and_never_beside_its_match(self):
        """The new mentions in one question, the memory's objects in another:
        a description written beside the name it is compared with took that
        name's meaning (gpt-oss, the bench of 30 Oct 2026)."""
        w = _Script({})
        O.decide(_ask(w), [(_m("Italy"), [_c("Italia")]), (_m("Milan"), [_c("Italia"), _c("Milano")])])
        assert re.findall(r'^Mention \d+: "(.*)"$', w.questions[0], re.M) == ["Italy", "Milan"]
        assert re.findall(r'^Mention \d+: "(.*)"$', w.questions[1], re.M) == ["Italia", "Milano"]

    def test_the_descriptions_reach_the_selection(self):
        w = _Script({"Italy": "Italia"})
        O.decide(_ask(w), [(_m("Italy"), [_c("Italia")])])
        assert 'description: "Italia, as its words say"' in w.questions[2]
        assert 'description: "Italy, as its words say"' in w.questions[2]

    def test_two_readings_that_disagree_unite_nothing(self):
        w = _Script({"Italy": "Italia"}, pairs={"Italy": "different"})
        [d] = O.decide(_ask(w), [(_m("Italy"), [_c("Italia")])])
        assert d.answer == O.UNSURE
        assert [r["framing"] for r in d.readings] == ["select", "pair"]

    def test_nothing_chosen_no_second_reading(self):
        w = _Script({})
        O.decide(_ask(w), [(_m("Italy"), [_c("Italia")])])
        assert len(w.questions) == 3

    def test_the_reason_keeps_both_descriptions_and_both_readings(self):
        w = _Script({"Italy": "Italia"})
        [d] = O.decide(_ask(w), [(_m("Italy"), [_c("Italia")])])
        r = O.reason_of(d)
        assert r["descriptions"] == {"name": "Italy, as its words say", "canonical": "Italia, as its words say"}
        assert [x["verdict"] for x in r["readings"]] == ["same", "same"]


# ---------------------------------------------------------------------------
# the write, on the database
# ---------------------------------------------------------------------------

def _vector(text):
    """Every fact of one subject near the same place, each object a little
    apart (a fixed order): the shortlist is the memory's objects."""
    words = (text or "x").split()
    h = int(hashlib.sha256(words[0].lower().encode()).hexdigest(), 16)
    o = int(hashlib.sha256(" ".join(words[-2:]).lower().encode()).hexdigest(), 16)
    v = [0.0] * 768
    v[h % 768] = 1.0
    v[(h // 768) % 768] += 0.5
    v[o % 768] += 0.05
    return v


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(product_objects, "SAME_OBJECT", True)
    monkeypatch.setattr(fsvc, "fact_vector", _vector)


@pytest.fixture
def writer(monkeypatch):
    w = _Script()
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: w)
    return w


def write(db, tenant, monkeypatch, text, *facts, user="marco"):
    """One memory of `user` with `text`, extracted as `facts` (subject,
    relation, object), written by the product's path."""
    m = AgentMemory(tenant_id=tenant.id, end_user_id=user, content=text, graph_sync_status="pending")
    db.add(m)
    db.commit()
    stub_extraction(monkeypatch, fsvc, lambda *a, **k: [
        ExtractedFact(s, r, o, 0.9, subject_type="person", evidence=text) for s, r, o in facts])
    fsvc.extract_and_persist_facts(db, tenant.id, memory=m)
    db.expire_all()
    return m


def objects_of(db, tenant, user="marco"):
    return sorted((f.object_label, f.object_canonical, int(f.observation_count or 1))
                  for f in db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id,
                                                      AgentFact.end_user_id == user))


def links(db, tenant):
    tid = tenant if isinstance(tenant, uuid.UUID) else tenant.id
    return [(r.name_label, r.canonical_label, r.decided_by)
            for r in db.query(AgentObjectLink).filter(AgentObjectLink.tenant_id == tid)
                       .order_by(AgentObjectLink.name_label)]


_CLOCKS = ("id", "created_at", "valid_from", "last_confirmed_at", "embedding_next_at", "first_memory_id",
           "source_memory_id", "source_memory_ids", "end_user_id", "invalid_at", "closed_known_at",
           "invalidated_by_id", "embedding", "embedding_status", "embedding_attempts", "embedded_at",
           "updated_at")


def rows(db, tenant, user=None, *, skip=()):
    """Every fact row, every column (but `skip`), in a stable order."""
    db.expire_all()
    cols = [c.name for c in AgentFact.__table__.columns if c.name not in skip]
    q = db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id)
    if user is not None:
        q = q.filter(AgentFact.end_user_id == user)
    out = [tuple((c, getattr(f, c)) for c in cols) for f in q]
    return sorted(out, key=repr)


class TestOff:
    def test_off_two_objects_and_no_question(self, db, tenant, monkeypatch, writer):
        monkeypatch.setattr(fsvc, "fact_vector", _vector)
        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert writer.questions == []
        assert [o for o, _, _ in objects_of(db, tenant)] == ["Italia", "Italy"]
        assert links(db, tenant) == []


class TestOn:
    def test_the_union_is_a_link_and_both_facts_stay_as_written(self, db, tenant, monkeypatch, on, writer):
        """The review's P1.2: the alias of the first build reconfirmed the
        new fact away. Now the fact is written with its own label, start,
        tense and evidence, and a link row says the two are one thing."""
        writer.verdicts = {"Italy": "Italia"}
        first = write(db, tenant, monkeypatch, "Vivo in Italia, a Padova.", ("Marco", "lives_in", "Italia"))
        second = write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert objects_of(db, tenant) == [("Italia", "Italia", 1), ("Italy", "Italy", 1)]
        assert links(db, tenant) == [("Italy", "Italia", O.BY_THE_WRITER)]
        row = db.query(AgentObjectLink).one()
        italia = db.query(AgentFact).filter(AgentFact.object_label == "Italia").one()
        assert row.source_memory_id == second.id and row.source_fact_id == italia.id
        assert row.canonical_memory_id == first.id
        assert row.reason["descriptions"] == {"name": "Italy, as its words say",
                                              "canonical": "Italia, as its words say"}
        assert [r["verdict"] for r in row.reason["readings"]] == ["same", "same"]
        assert db.query(AgentEntityAlias).count() == 0, "never an alias"

    def test_the_facts_are_exactly_the_switch_offs(self, db, tenant, monkeypatch, writer):
        """A link changes no write: the same memories written with the switch
        on and off store the same facts."""
        monkeypatch.setattr(fsvc, "fact_vector", _vector)
        writer.verdicts = {"Italy": "Italia"}
        said = [("Vivo in Italia.", ("Marco", "lives_in", "Italia")),
                ("I live in Italy since 2020.", ("Marco", "lives_in", "Italy")),
                ("Sono nato in Italia.", ("Marco", "born_in", "Italia"))]
        for text, fact in said:
            write(db, tenant, monkeypatch, text, fact, user="off")
        monkeypatch.setattr(product_objects, "SAME_OBJECT", True)
        for text, fact in said:
            write(db, tenant, monkeypatch, text, fact, user="on")
        assert links(db, tenant) == [("Italy", "Italia", O.BY_THE_WRITER)]
        assert rows(db, tenant, "on", skip=_CLOCKS) == rows(db, tenant, "off", skip=_CLOCKS)

    def test_the_question_carries_both_contexts(self, db, tenant, monkeypatch, on, writer):
        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Sono nato in Italia, vicino a Treviso.", ("Marco", "born_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy now.", ("Marco", "lives_in", "Italy"))
        q = writer.selections()[0]
        assert "fact: Marco | lives in | Italy" in q and 'memory\'s words: "I live in Italy now."' in q
        assert "fact: Marco | born in | Italia" in q
        assert 'memory\'s words: "Sono nato in Italia, vicino a Treviso."' in q

    @pytest.mark.parametrize("existing, new, verdict", [
        (("lives_in", "Georgia", "I live in Atlanta, Georgia."),
         ("visited", "Géorgie", "J'ai visité la Géorgie, Tbilissi."), "different"),
        (("grew_up_in", "Washington", "I grew up in Washington state, near Seattle."),
         ("flew_to", "Washington DC", "I flew to Washington DC for a conference."), "unsure"),
        (("lived_in", "Monaco", "Ho vissuto a Monaco per l'Erasmus."),
         ("visited", "Principality of Monaco", "We visited the Principality of Monaco."), "different"),
    ])
    def test_false_friends_stay_apart(self, db, tenant, monkeypatch, on, writer, existing, new, verdict):
        writer.verdicts = {new[1]: verdict}
        write(db, tenant, monkeypatch, existing[2], ("Marco", existing[0], existing[1]))
        write(db, tenant, monkeypatch, new[2], ("Marco", new[0], new[1]))
        assert len(writer.selections()) == 1
        assert parse(writer.selections()[0]) == [(new[1], [existing[1]])]
        assert sorted(o for o, _, _ in objects_of(db, tenant)) == sorted([existing[1], new[1]])
        assert links(db, tenant) == []

    def test_a_disagreeing_second_reading_keeps_them_apart(self, db, tenant, monkeypatch, on, writer):
        writer.verdicts = {"Principality of Monaco": "Monaco"}
        writer.pairs = {"Principality of Monaco": "different"}
        write(db, tenant, monkeypatch, "Ho vissuto a Monaco.", ("Marco", "lived_in", "Monaco"))
        write(db, tenant, monkeypatch, "I moved to the Principality of Monaco.",
              ("Marco", "moved_to", "Principality of Monaco"))
        assert len(writer.questions) == 4 and links(db, tenant) == []

    def test_people_are_never_asked(self, db, tenant, monkeypatch, on, writer):
        """The review's P2.3: two people who share a name, or a nickname, are
        not a translation. The family "people" is never asked, on either
        side."""
        writer.families = {"has brother": "people", "has_brother": "people"}
        writer.verdicts = {"Beppe": "Giuseppe"}
        write(db, tenant, monkeypatch, "Mio fratello Giuseppe.", ("Luca", "has_brother", "Giuseppe"), user="luca")
        write(db, tenant, monkeypatch, "My brother Beppe.", ("Luca", "has_brother", "Beppe"), user="luca")
        assert writer.questions == [] and links(db, tenant) == []

    def test_only_the_same_family_is_a_candidate(self, db, tenant, monkeypatch, on, writer):
        writer.families = {"works for": "work", "works_for": "work"}
        writer.verdicts = {"Paris": "Parigi"}
        write(db, tenant, monkeypatch, "Lavoro per Parigi SpA.", ("Marco", "works_for", "Parigi"))
        write(db, tenant, monkeypatch, "I visited Paris.", ("Marco", "visited", "Paris"))
        assert writer.questions == [] and links(db, tenant) == []

    def test_a_closed_fact_is_not_a_candidate(self, db, tenant, monkeypatch, on, writer):
        """The review's P3.11: only what is true now is offered."""
        from datetime import datetime, timezone

        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        db.query(AgentFact).update({AgentFact.invalid_at: datetime(2020, 1, 1, tzinfo=timezone.utc)})
        db.commit()
        write(db, tenant, monkeypatch, "I visited Italy.", ("Marco", "visited", "Italy"))
        assert writer.questions == []

    def test_a_forgotten_memorys_fact_is_not_a_candidate(self, db, tenant, monkeypatch, on, writer):
        from datetime import datetime, timezone

        m = write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        db.query(AgentMemory).filter(AgentMemory.id == m.id).update(
            {AgentMemory.deleted_at: datetime.now(timezone.utc)})
        db.commit()
        write(db, tenant, monkeypatch, "I visited Italy.", ("Marco", "visited", "Italy"))
        assert writer.questions == []

    def test_the_shortlist_is_read_on_the_index_with_its_depth(self, db, tenant, monkeypatch, on, writer):
        """The review's P2.5 (BUG-320 class): `vector_search.nearest`, which
        sets the HNSW depth first."""
        from korely_agent.services.memory_core import vector_search

        seen = []
        real = vector_search.hnsw_depth
        monkeypatch.setattr(vector_search, "hnsw_depth", lambda db, pool: (seen.append(pool), real(db, pool)))
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert O.NEAREST_FACTS in seen

    def test_an_object_the_memory_holds_is_not_asked(self, db, tenant, monkeypatch, on, writer):
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "Sono nato in Italia.", ("Marco", "born_in", "Italia"))
        assert writer.questions == []

    def test_a_linked_name_is_not_asked_again(self, db, tenant, monkeypatch, on, writer):
        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        asked = len(writer.questions)
        write(db, tenant, monkeypatch, "I work in Italy.", ("Marco", "works_in", "Italy"))
        assert len(writer.questions) == asked

    def test_a_declared_alias_decides_and_nothing_is_asked(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services.facts import entities

        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        entities.declare_alias(db, tenant.id, None, "marco", None, "Italy", "Repubblica Italiana")
        db.commit()
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert writer.questions == [] and links(db, tenant) == []

    def test_a_name_that_cannot_be_an_alias_is_not_asked(self, db, tenant, monkeypatch, on, writer):
        """`identity.valid_name`, the rule of every name: no comma, at most
        five words. "Washington, D.C." stays an object of its own."""
        write(db, tenant, monkeypatch, "Washington state.", ("Marco", "grew_up_in", "Washington"))
        write(db, tenant, monkeypatch, "Washington, D.C.", ("Marco", "flew_to", "Washington, D.C."))
        assert writer.questions == []

    def test_a_literal_is_not_a_thing(self, db, tenant, monkeypatch, on, writer):
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        m = AgentMemory(tenant_id=tenant.id, end_user_id="marco", content="I am 38.", graph_sync_status="pending")
        db.add(m)
        db.commit()
        stub_extraction(monkeypatch, fsvc, lambda *a, **k: [
            ExtractedFact("Marco", "has_age", "38", 0.9, object_is_literal=True, subject_type="person")])
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)
        assert writer.questions == []

    def test_another_end_users_objects_are_not_candidates(self, db, tenant, monkeypatch, on, writer):
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"), user="giulia")
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert writer.questions == []

    def test_a_direct_write_names_its_own_object(self, db, tenant, monkeypatch, on, writer):
        assert product_objects.uniter(db, tenant.id, object(), model_name="direct", text="") is None
        assert product_objects.uniter(db, tenant.id, object(), model_name="correction", text="") is None

    def test_a_name_another_write_linked_meanwhile_is_read_again_under_the_lock(
            self, db, tenant, monkeypatch, on, writer):
        """The review's P3.10: the check and the link are serialized per
        memory, and the objects are read again once the lock is held. Here
        the "other write" links the name while this one waits for the lock."""
        from korely_agent.services.facts import entities

        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        taken = []

        def lock_of(db_, tenant_id, *mem):
            def lock():
                taken.append(mem)
                O.add_link(db_, entities.TABLES, entities.memory_of(tenant_id, *mem), "Italy", "Italia",
                           decided_by="writer", family="places")
            return lock
        monkeypatch.setattr(product_objects, "lock_of", lock_of)
        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert taken == [(None, "marco", None)]
        assert writer.questions == [], "read again under the lock: no longer new"
        assert len(links(db, tenant)) == 1


class TestTheReads:
    """The links resolved by the reads (facts/links.py): one object, under
    the name the memory held first, each fact with its own words."""

    def _linked(self, db, tenant, monkeypatch, writer):
        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I was born in Italy.", ("Marco", "born_in", "Italy"))
        return db.query(AgentObjectLink).one()

    def test_get_facts_shows_one_name_and_each_label(self, db, tenant, monkeypatch, on, writer):
        link = self._linked(db, tenant, monkeypatch, writer)
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", project_id=EVERY_PROJECT)
        shown = sorted((s["object"], s["object_canonical"], s["object_link_id"])
                       for s in (fsvc.serialize_fact(f) for f in got))
        assert shown == [("Italia", "Italia", None), ("Italy", "Italia", str(link.id))]

    @pytest.mark.parametrize("asked", ["Italy", "italia", "ITALIA"])
    def test_an_entity_finds_both_names(self, db, tenant, monkeypatch, on, writer, asked):
        self._linked(db, tenant, monkeypatch, writer)
        got, total = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", entity=asked,
                                               project_id=EVERY_PROJECT)
        assert total == 2 and sorted(f.object_label for f in got) == ["Italia", "Italy"]

    def test_another_end_user_is_not_read_through_the_link(self, db, tenant, monkeypatch, on, writer):
        self._linked(db, tenant, monkeypatch, writer)
        monkeypatch.setattr(product_objects, "SAME_OBJECT", False)
        write(db, tenant, monkeypatch, "I live in Italy.", ("Giulia", "lives_in", "Italy"), user="giulia")
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, entity="Italia", project_id=EVERY_PROJECT)
        assert sorted((f.end_user_id, f.object_label) for f in got) == [("marco", "Italia"), ("marco", "Italy")]
        [g] = [fsvc.serialize_fact(f) for f in db.query(AgentFact).filter(AgentFact.end_user_id == "giulia")]
        assert g["object_canonical"] == "Italy" and g["object_link_id"] is None

    def test_the_lines_say_written(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services.memory_core.presentation import mcp_line

        self._linked(db, tenant, monkeypatch, writer)
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", predicate="born_in",
                                           project_id=EVERY_PROJECT)
        assert mcp_line(fsvc.serialize_fact(got[0])).startswith("Marco · born_in · Italia (written: Italy)")

    def test_known_as_of_before_the_link_reads_two_objects(self, db, tenant, monkeypatch, on, writer):
        from datetime import timedelta

        link = self._linked(db, tenant, monkeypatch, writer)
        before = link.created_at - timedelta(microseconds=1)
        italy = db.query(AgentFact).filter(AgentFact.object_label == "Italy").one()
        assert fsvc.serialize_fact(italy, before)["object_canonical"] == "Italy"
        assert fsvc.serialize_fact(italy, link.created_at)["object_canonical"] == "Italia"

    def test_the_context_says_one_thing_once(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services.facts.links import one_line_each
        from korely_agent.services.memory_core.presentation import context_line

        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        facts = db.query(AgentFact).order_by(AgentFact.created_at).all()
        lines = one_line_each(db, facts)
        assert len(lines) == 1 and sorted(lines[0][1]) == sorted(f.id for f in facts)
        assert context_line(lines[0][0], None) == "- Marco lives_in Italia (since " + \
            context_line(facts[0], None).split("(since ")[1]

    def test_without_links_the_context_lines_are_as_before(self, db, tenant, monkeypatch, writer):
        from korely_agent.services.facts.links import one_line_each
        from korely_agent.services.memory_core.presentation import context_line

        monkeypatch.setattr(fsvc, "fact_vector", _vector)
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        facts = db.query(AgentFact).order_by(AgentFact.created_at).all()
        assert [context_line(v, None) for v, _ in one_line_each(db, facts)] == \
            [context_line(f, None) for f in facts]


class TestTheModelAndTheRegion:
    def test_a_failed_question_rolls_the_write_back(self, db, tenant, monkeypatch, on, writer):
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))

        def broken(prompt):
            raise RuntimeError("the writer is down")
        monkeypatch.setattr(writer, "answer", broken)
        m = write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert [o for o, _, _ in objects_of(db, tenant)] == ["Italia"], "nothing of the write was kept"
        assert db.get(AgentMemory, m.id).graph_sync_status != "done"

    def test_an_empty_answer_is_not_different(self, db, tenant, monkeypatch, on, writer):
        """The review's P3.12: an absent answer rolls the write back; it is
        asked again at the next try, never left apart for good."""
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        monkeypatch.setattr(writer, "answer", lambda prompt: "")
        m = write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert [o for o, _, _ in objects_of(db, tenant)] == ["Italia"]
        assert db.get(AgentMemory, m.id).graph_sync_status != "done"

    def test_a_vector_that_cannot_be_computed_is_not_different(self, db, tenant, monkeypatch, on, writer):
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        monkeypatch.setattr(fsvc, "fact_vector", lambda text: None)
        m = write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert [o for o, _, _ in objects_of(db, tenant)] == ["Italia"]
        assert db.get(AgentMemory, m.id).graph_sync_status != "done"

    def test_the_question_goes_to_the_projects_region(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services import regions

        monkeypatch.setattr(regions, "of_project", lambda *a, **k: "eu")
        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert writer.regions and set(writer.regions) == {"eu"}

    def test_the_logs_carry_no_names(self, db, tenant, monkeypatch, on, writer, caplog):
        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        with caplog.at_level(logging.DEBUG):
            write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert "same object" in caplog.text and "object link created" in caplog.text
        assert "Italy" not in caplog.text and "Italia" not in caplog.text and "italy" not in caplog.text


class TestTheWayBack:
    def test_undo_after_a_write_leaves_every_fact_row_as_it_was(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services.facts import entities

        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        linked = rows(db, tenant)
        O.remove_link(db, entities.TABLES, db.query(AgentObjectLink).one())
        db.commit()
        assert rows(db, tenant) == linked and links(db, tenant) == []
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", project_id=EVERY_PROJECT)
        assert sorted(s["object_canonical"] for s in (fsvc.serialize_fact(f) for f in got)) == ["Italia", "Italy"]

    def test_a_review_and_its_undo_change_no_fact_row(self, db, tenant, monkeypatch, writer):
        """The condition: every union reversible without loss. The
        full rows before the union, after it and after the undo."""
        from korely_agent.services.facts import entities

        monkeypatch.setattr(fsvc, "fact_vector", _vector)
        write(db, tenant, monkeypatch, "Vivo in Italia dal 2019.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I moved to Italy last year.", ("Marco", "lives_in", "Italy"))
        before = rows(db, tenant)
        writer.verdicts = {"Italy": "Italia"}
        r = product_objects.review(db, tenant.id, project_id=None, end_user_id="marco", agent_id=None)
        records = [product_objects.proposal_record(tenant.id, None, "marco", None, p) for p in r["proposals"]]
        out = product_objects.apply(db, records)
        db.commit()
        assert [o["outcome"] for o in out] == ["created"]
        assert rows(db, tenant) == before, "the union rewrote nothing"
        O.remove_link(db, entities.TABLES, db.query(AgentObjectLink).one())
        db.commit()
        assert rows(db, tenant) == before, "the undo gave back exactly the memory before"

    def test_the_admin_route_undoes_it(self, db, tenant, monkeypatch, on, writer):
        from fastapi.testclient import TestClient

        from korely_agent.db import get_db
        from korely_agent.main import create_app
        from korely_agent.services import keys as key_service

        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        _row, raw = key_service.create_api_key(
            db, tenant_id=tenant.id, name="admin",
            scopes=list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        c = TestClient(app)
        h = {"Authorization": f"Bearer {raw}"}
        listed = c.get("/admin/object-links", headers=h).json()["object_links"]
        assert [(x["name"], x["same_as"], x["decided_by"]) for x in listed] == [("Italy", "Italia", "writer")]
        assert listed[0]["source_memory_id"].startswith("mem_") and listed[0]["source_fact_id"].startswith("fct_")
        assert listed[0]["reason"]["readings"][1]["framing"] == "pair"
        assert c.delete(f"/admin/object-links/{listed[0]['id']}", headers=h).json()["deleted"] is True
        db.expire_all()
        assert links(db, tenant) == []


class TestErasure:
    """The review's P1.1: an erasure reaches every copy, the link and its
    reason included, whichever of the two memories is erased."""

    def test_the_reviews_probe_erasing_the_first_leaves_its_word_nowhere(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services.facts.erasure import erase_memory
        from tests.test_end_user_places import _rows_naming

        writer.verdicts = {"Italy": "Italia"}
        first = write(db, tenant, monkeypatch, "Vivo in Italia", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy", ("Marco", "lives_in", "Italy"))
        assert links(db, tenant)
        erase_memory(db, tenant.id, first.id, project_id=EVERY_PROJECT)
        db.expire_all()
        assert links(db, tenant) == []
        assert objects_of(db, tenant) == [("Italy", "Italy", 1)]
        assert _rows_naming(db, "Italia") == {}

    def test_erasing_the_second_takes_the_link_and_keeps_the_first(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services.facts.erasure import erase_memory
        from tests.test_end_user_places import _rows_naming

        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia", ("Marco", "lives_in", "Italia"))
        second = write(db, tenant, monkeypatch, "I live in Italy", ("Marco", "lives_in", "Italy"))
        erase_memory(db, tenant.id, second.id, project_id=EVERY_PROJECT)
        db.expire_all()
        assert links(db, tenant) == [] and objects_of(db, tenant) == [("Italia", "Italia", 1)]
        assert _rows_naming(db, "Italy") == {}

    def test_an_end_users_erasure_takes_their_links(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services.facts.erasure import forget_end_user

        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy", ("Marco", "lives_in", "Italy"))
        forget_end_user(db, tenant.id, "marco", project_id=EVERY_PROJECT)
        db.expire_all()
        assert links(db, tenant) == []


class TestReviewTheObjectsAlreadyThere:
    """`manage review-objects`: the objects written before 6.1.0, or with the
    switch off, asked as a write would ask them; a dry run of proposals, then
    exactly those written."""

    def _two(self, db, tenant, monkeypatch, writer):
        monkeypatch.setattr(fsvc, "fact_vector", _vector)
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        assert writer.questions == [] and len(objects_of(db, tenant)) == 2

    def test_a_review_writes_nothing(self, db, tenant, monkeypatch, writer):
        self._two(db, tenant, monkeypatch, writer)
        writer.verdicts = {"Italy": "Italia"}
        r = product_objects.review(db, tenant.id, project_id=None, end_user_id="marco", agent_id=None)
        db.commit()
        assert [(p.name, p.canonical) for p in r["proposals"]] == [("Italy", "Italia")]
        assert r["objects"] == 2 and r["asked"] == 1 and links(db, tenant) == []
        assert parse(writer.selections()[0]) == [("Italy", ["Italia"])], "only the objects named before it"

    def test_apply_writes_exactly_the_proposals(self, db, tenant, monkeypatch, writer):
        self._two(db, tenant, monkeypatch, writer)
        writer.verdicts = {"Italy": "Italia"}
        r = product_objects.review(db, tenant.id, project_id=None, end_user_id="marco", agent_id=None)
        records = [product_objects.proposal_record(tenant.id, None, "marco", None, p) for p in r["proposals"]]
        asked = len(writer.questions)
        out = product_objects.apply(db, json.loads(json.dumps(records)))
        db.commit()
        assert len(writer.questions) == asked, "the apply calls no model"
        assert [o["outcome"] for o in out] == ["created"]
        assert links(db, tenant) == [("Italy", "Italia", O.BY_A_REVIEW)]
        assert objects_of(db, tenant) == [("Italia", "Italia", 1), ("Italy", "Italy", 1)]

    def test_a_name_gone_since_the_dry_run_is_not_written(self, db, tenant, monkeypatch, writer):
        self._two(db, tenant, monkeypatch, writer)
        writer.verdicts = {"Italy": "Italia"}
        r = product_objects.review(db, tenant.id, project_id=None, end_user_id="marco", agent_id=None)
        records = [product_objects.proposal_record(tenant.id, None, "marco", None, p) for p in r["proposals"]]
        db.query(AgentFact).filter(AgentFact.object_label == "Italy").delete()
        db.commit()
        assert [o["outcome"] for o in product_objects.apply(db, records)] == ["gone"]

    def test_different_proposes_nothing(self, db, tenant, monkeypatch, writer):
        self._two(db, tenant, monkeypatch, writer)
        r = product_objects.review(db, tenant.id, project_id=None, end_user_id="marco", agent_id=None)
        assert r["proposals"] == [] and r["apart"] == ["Italy"]

    def test_the_cap_stops_before_the_question(self, db, tenant, monkeypatch, writer):
        self._two(db, tenant, monkeypatch, writer)
        r = product_objects.review(db, tenant.id, project_id=None, end_user_id="marco", agent_id=None,
                                   before_each=lambda: False)
        assert r["stopped"] and r["asked"] == 0 and writer.questions == []

    def test_the_memories_it_reads(self, db, tenant, monkeypatch, writer):
        self._two(db, tenant, monkeypatch, writer)
        assert product_objects.memories_with_objects(db, tenant.id) == [(None, "marco", None)]
        assert product_objects.memories_with_objects(db, tenant.id, end_user_id="giulia") == []


class TestTheCommand:
    def _two(self, db, tenant, monkeypatch, writer):
        from korely_agent import manage

        monkeypatch.setattr(manage, "_session", lambda: db)
        monkeypatch.setattr(fsvc, "fact_vector", _vector)
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        return manage

    def test_off_it_refuses(self, db, tenant, monkeypatch, writer, capsys):
        manage = self._two(db, tenant, monkeypatch, writer)
        assert manage.main(["review-objects", "--tenant", str(tenant.id), "--cap", "1", "--out", "x.json"]) == 1
        assert "switch is off" in capsys.readouterr().err and writer.questions == []

    def test_a_dry_run_needs_a_scope_a_cap_and_a_file(self, db, tenant, monkeypatch, writer, capsys, tmp_path):
        manage = self._two(db, tenant, monkeypatch, writer)
        tid = str(tenant.id)
        assert manage.main(["review-objects", "--while-off", "--cap", "1", "--out", str(tmp_path / "p.json")]) == 1
        assert manage.main(["review-objects", "--while-off", "--tenant", tid]) == 1
        assert writer.questions == []

    def test_dry_run_then_apply(self, db, tenant, monkeypatch, writer, capsys, tmp_path):
        from korely_agent.config import get_settings
        from korely_agent.db.models import AgentLlmUsage, AuditEvent

        manage = self._two(db, tenant, monkeypatch, writer)
        tid = tenant.id
        monkeypatch.setenv("LLM_RATES", json.dumps(
            {"currency": "EUR", "models": {"m": {"input_per_million": 1, "output_per_million": 1}}}))
        get_settings.cache_clear()
        writer.verdicts = {"Italy": "Italia"}
        out = tmp_path / "p.json"
        assert manage.main(["review-objects", "--while-off", "--tenant", str(tid), "--cap", "1",
                            "--out", str(out)]) == 0
        assert links(db, tid) == []
        [p] = json.loads(out.read_text())["proposals"]
        assert (p["name"], p["same_as"], p["user_id"]) == ("Italy", "Italia", "marco")
        assert {r.stage for r in db.query(AgentLlmUsage)} <= {"operator"}, "never the account's own use"
        asked = len(writer.questions)
        assert manage.main(["review-objects", "--while-off", "--apply", str(out)]) == 0
        assert len(writer.questions) == asked
        assert links(db, tid) == [("Italy", "Italia", O.BY_A_REVIEW)]
        trail = db.query(AuditEvent).filter(AuditEvent.tenant_id == tid,
                                            AuditEvent.action == "setting_change").all()
        assert [(e.actor, e.meta["setting"], e.end_user_id) for e in trail] == [("manage", "object_link", "marco")]


# ---------------------------------------------------------------------------
# the re-review of the rebuild (30 Oct 2026)
# ---------------------------------------------------------------------------

class TestALinkIsReadInItsFamilyOnly:
    """P2.1: a link decided among a memory's places is not read on a fact of
    another family ("Florence" the city, "Florence" the aunt), and a family
    never asked (people) is never read through one."""

    def _probe(self, db, tenant, monkeypatch, writer):
        writer.verdicts = {"Florence": "Firenze"}
        writer.families = {"has aunt": "people", "has_aunt": "people"}
        write(db, tenant, monkeypatch, "Vivo a Firenze.", ("Marco", "lives_in", "Firenze"))
        write(db, tenant, monkeypatch, "I moved to Florence.", ("Marco", "moved_to", "Florence"))
        assert [(r[0], r[1]) for r in links(db, tenant)] == [("Florence", "Firenze")]
        assert db.query(AgentObjectLink).one().family == "places"
        write(db, tenant, monkeypatch, "My aunt Florence visits on Sundays.", ("Marco", "has_aunt", "Florence"))
        return db.query(AgentFact).filter(AgentFact.predicate_canonical == "has_aunt").one()

    def test_the_aunt_keeps_her_name(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services.memory_core.presentation import context_line

        aunt = self._probe(db, tenant, monkeypatch, writer)
        s = fsvc.serialize_fact(aunt)
        assert (s["object_canonical"], s["object_link_id"]) == ("Florence", None)
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", project_id=EVERY_PROJECT)
        shown = {f.predicate_canonical: fsvc.serialize_fact(f)["object_canonical"] for f in got}
        assert shown == {"lives_in": "Firenze", "moved_to": "Firenze", "has_aunt": "Florence"}
        assert "written" not in context_line(aunt, None)

    def test_the_entity_filter_does_not_bring_the_aunt(self, db, tenant, monkeypatch, on, writer):
        self._probe(db, tenant, monkeypatch, writer)
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", entity="Firenze",
                                           project_id=EVERY_PROJECT)
        assert sorted(f.predicate_canonical for f in got) == ["lives_in", "moved_to"]

    def test_the_context_does_not_fold_the_aunt(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services.facts.links import one_line_each

        self._probe(db, tenant, monkeypatch, writer)
        facts = db.query(AgentFact).order_by(AgentFact.created_at).all()
        lines = one_line_each(db, facts)
        assert len(lines) == 3
        aunt = [v for v, _ in lines if v["predicate_canonical"] == "has_aunt"][0]
        assert aunt["object_canonical"] == "Florence" and aunt["object_link_id"] is None

    def test_an_employer_called_paris_is_not_the_city(self, db, tenant, monkeypatch, on, writer):
        writer.verdicts = {"Paris": "Parigi"}
        writer.families = {"works for": "work", "works_for": "work"}
        write(db, tenant, monkeypatch, "Sono stato a Parigi.", ("Marco", "visited", "Parigi"))
        write(db, tenant, monkeypatch, "I flew to Paris.", ("Marco", "flew_to", "Paris"))
        write(db, tenant, monkeypatch, "I work for Paris, the design studio.", ("Marco", "works_for", "Paris"))
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", project_id=EVERY_PROJECT)
        shown = {f.predicate_canonical: fsvc.serialize_fact(f)["object_canonical"] for f in got}
        assert shown == {"visited": "Parigi", "flew_to": "Parigi", "works_for": "Paris"}
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", entity="Parigi",
                                           project_id=EVERY_PROJECT)
        assert sorted(f.predicate_canonical for f in got) == ["flew_to", "visited"]

    def test_a_family_never_asked_is_never_resolved(self, db, tenant, monkeypatch, on, writer):
        """Even a link row in "people" (none is ever written) changes no read."""
        from korely_agent.services.facts import entities

        writer.families = {"has aunt": "people", "has_aunt": "people"}
        monkeypatch.setattr(product_objects, "SAME_OBJECT", False)
        write(db, tenant, monkeypatch, "My aunt Florence.", ("Marco", "has_aunt", "Florence"))
        db.add(AgentObjectLink(tenant_id=tenant.id, end_user_id="marco", name_label="Florence",
                               canonical_label="Firenze", family="people", decided_by="writer"))
        db.commit()
        aunt = db.query(AgentFact).one()
        assert fsvc.serialize_fact(aunt)["object_canonical"] == "Florence"
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", entity="Firenze",
                                           project_id=EVERY_PROJECT)
        assert got == []
        assert entities.TABLES.Link is AgentObjectLink


class TestAForgetTakesTheLink:
    """P2.2: a forgotten memory's words no longer decide how the memory's
    names read."""

    def test_forgetting_the_first(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services.facts.erasure import forget_memory

        writer.verdicts = {"Italy": "Italia"}
        first = write(db, tenant, monkeypatch, "Vivo in Italia", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I was born in Italy", ("Marco", "born_in", "Italy"))
        assert links(db, tenant)
        forget_memory(db, tenant.id, first.id, project_id=EVERY_PROJECT)
        db.expire_all()
        assert links(db, tenant) == []
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", project_id=EVERY_PROJECT)
        assert [(f.object_label, fsvc.serialize_fact(f)["object_canonical"]) for f in got] == [("Italy", "Italy")]

    def test_forgetting_the_second(self, db, tenant, monkeypatch, on, writer):
        from korely_agent.services.facts.erasure import forget_memory

        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia", ("Marco", "lives_in", "Italia"))
        second = write(db, tenant, monkeypatch, "I was born in Italy", ("Marco", "born_in", "Italy"))
        forget_memory(db, tenant.id, second.id, project_id=EVERY_PROJECT)
        db.expire_all()
        assert links(db, tenant) == []


class TestOneResponseOneMoment:
    """P2.3: with `known_as_of`, the filter and the names of one response
    read the links of the same moment."""

    def test_before_and_after_the_link(self, db, tenant, monkeypatch, on, writer):
        from datetime import timedelta

        writer.verdicts = {"Italy": "Italia"}
        write(db, tenant, monkeypatch, "Vivo in Italia", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I was born in Italy", ("Marco", "born_in", "Italy"))
        link = db.query(AgentObjectLink).one()
        before = link.created_at - timedelta(microseconds=1)
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", entity="Italia",
                                           known_as_of=before, include_invalidated=True,
                                           project_id=EVERY_PROJECT)
        assert [(f.object_label, fsvc.serialize_fact(f, before)["object_canonical"]) for f in got] == \
            [("Italia", "Italia")], "the link was not known then: neither in the filter nor in the names"
        got, _ = fsvc.retrieve_agent_facts(db, tenant.id, end_user_id="marco", entity="Italia",
                                           known_as_of=link.created_at, include_invalidated=True,
                                           project_id=EVERY_PROJECT)
        assert sorted((f.object_label, fsvc.serialize_fact(f, link.created_at)["object_canonical"])
                      for f in got) == [("Italia", "Italia"), ("Italy", "Italia")]


class TestTheSmallerFixes:
    def test_the_questions_have_a_stage_of_their_own(self, monkeypatch, writer):
        """P3.2: the same-object calls are not the vocabulary's."""
        from contextlib import contextmanager

        from korely_agent.services import llm_usage, predicates

        stages = []
        real = llm_usage.stage

        @contextmanager
        def stage(name):
            stages.append(name)
            with real(name):
                yield
        monkeypatch.setattr(llm_usage, "stage", stage)
        predicates.writers_ask()('{"descriptions"} Mention 1: "x"', False)
        assert stages == [llm_usage.OBJECTS] and llm_usage.OBJECTS == "objects"

    def test_a_projects_link_that_would_cycle_stays_out(self, db, tenant, monkeypatch, writer):
        """P3.4: "Italy -> Italia" moving where "Italia -> Italy" stands."""
        from korely_agent.services import projects as project_service

        default = project_service.get_or_create_default_project(db, tenant.id)
        gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
        for pid, name, same in ((gamma.id, "Italy", "Italia"), (default.id, "Italia", "Italy"),
                                (gamma.id, "Milan", "Milano")):
            db.add(AgentObjectLink(tenant_id=tenant.id, project_id=pid, end_user_id="marco",
                                   name_label=name, canonical_label=same, family="places",
                                   decided_by="writer"))
        db.commit()
        out = project_service.delete_project(db, tenant.id, gamma)
        db.expire_all()
        assert out["object_links_moved"] == 1
        assert sorted((r.project_id == default.id, r.name_label, r.canonical_label)
                      for r in db.query(AgentObjectLink)) == [(True, "Italia", "Italy"), (True, "Milan", "Milano")]

    def test_an_apply_that_loses_the_name_to_another_write_reports_taken(self, db, tenant, monkeypatch, writer):
        """P3.7: the unique name lost to a write at once is one link "taken",
        never the whole apply aborted."""
        monkeypatch.setattr(fsvc, "fact_vector", _vector)
        write(db, tenant, monkeypatch, "Vivo in Italia.", ("Marco", "lives_in", "Italia"))
        write(db, tenant, monkeypatch, "I live in Italy.", ("Marco", "lives_in", "Italy"))
        write(db, tenant, monkeypatch, "Lavoro a Milano.", ("Marco", "works_in", "Milano"))
        write(db, tenant, monkeypatch, "I work in Milan.", ("Marco", "works_in", "Milan"))
        records = [{"account_id": str(tenant.id), "project_id": None, "user_id": "marco", "agent_id": None,
                    "name": n, "same_as": c, "family": "places", "reason": {}}
                   for n, c in (("Italy", "Italia"), ("Milan", "Milano"))]
        # The other write's row, which this apply's check cannot see yet.
        db.add(AgentObjectLink(tenant_id=tenant.id, end_user_id="marco", name_label="Italy",
                               canonical_label="Italia", family="places", decided_by="writer"))
        db.flush()
        real = O.link_rows
        monkeypatch.setattr(O, "link_rows", lambda db_, t, m, **kw: [r for r in real(db_, t, m, **kw)
                                                                     if r.name_label != "Italy"])
        out = product_objects.apply(db, records)
        assert [o["outcome"] for o in out] == ["taken", "created"]
        db.commit()
        assert sorted(r[0] for r in links(db, tenant)) == ["Italy", "Milan"]

    def test_a_cap_of_zero_asks_nothing(self, db, tenant, monkeypatch, writer, capsys, tmp_path):
        """P3.8."""
        from korely_agent import manage

        monkeypatch.setattr(manage, "_session", lambda: db)
        tid = str(tenant.id)
        assert manage.main(["review-objects", "--while-off", "--tenant", tid, "--cap", "0",
                            "--out", str(tmp_path / "p.json")]) == 1
        assert "--cap must be above 0" in capsys.readouterr().err and writer.questions == []
