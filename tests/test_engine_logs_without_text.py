"""memory_core 3.0.0: the end users' words do not reach the logs.

Code audit of 6 October: dropped facts
written whole at INFO, model answers at ERROR, alias names, dropped relative
times, and the `%s` of a database error, whose message carries the statement's
parameters and PostgreSQL's DETAIL (the row's values). For every log line
touched: a content with a marker goes in, the line is written (one knows what
happened and where), the marker is not there. The same holds for the messages
of the exceptions the engine raises, which the adapters write in their logs.

No model call.
"""
# Port notes: the engine's loggers are named after this package
# (`korely_agent.services.memory_core.*`); the DB tests run on korely-agent's
# tables (tenant_id, `facts.entities.TABLES`, `memory_scope`) and on
# PostgreSQL, where the failed write of TestWorker is a NOT NULL violation
# on `agent_facts.tenant_id` (psycopg2), not SQLite's error.
from __future__ import annotations

import json
import logging
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentFact, AgentMemory
from korely_agent.services.facts import entities
from korely_agent.services.facts.temporal import true_at
from korely_agent.services.memory_core import store, candidates, fact_extractor, judge, log_safe, vocabulary
from korely_agent.services.memory_core import worker
from korely_agent.services.memory_core.store import Relation
from korely_agent.services.memory_core.fact_extractor import ExtractedFact
from korely_agent.services.memory_core.writing import write_fact

ENGINE = "korely_agent.services.memory_core"

# The end user's words, as a memory would carry them. Never in a log.
SECRET = "Zanzibar7781"
ILLNESS = f"type 2 diabetes {SECRET}"


@pytest.fixture
def engine_log(caplog):
    caplog.set_level(logging.DEBUG, logger=ENGINE)
    return caplog


def _clean(caplog):
    assert SECRET not in caplog.text, caplog.text
    for r in caplog.records:
        if r.name.startswith(ENGINE):
            assert r.exc_info is None, "exc_info prints the message of the error"


# ---------------------------------------------------------------- log_safe

class _Diag:
    constraint_name = "uq_agent_facts_one_open_per_triple"
    table_name = "agent_facts"
    column_name = None
    message_detail = f"Key (subject)=({SECRET}) already exists."


class _UniqueViolation(Exception):
    """psycopg2's error as SQLAlchemy wraps it: pgcode and diag."""
    pgcode = "23505"
    diag = _Diag()


def _database_error():
    orig = _UniqueViolation(f'duplicate key value violates unique constraint\nDETAIL: {_Diag.message_detail}')
    try:
        try:
            raise orig
        except _UniqueViolation as e:
            raise IntegrityError("INSERT INTO agent_facts (subject_label) VALUES (%(s)s)", {"s": SECRET}, e) from e
    except IntegrityError as e:
        return e


class TestLogSafe:
    def test_a_database_error_without_values(self):
        e = _database_error()
        assert SECRET in str(e), "the hazard: SQLAlchemy's message carries the parameters"
        line = log_safe.error(e)
        assert SECRET not in line
        assert "IntegrityError" in line and "_UniqueViolation" in line
        assert "SQLSTATE 23505" in line and "constraint uq_agent_facts_one_open_per_triple" in line
        assert "table agent_facts" in line and "characters withheld" in line

    def test_the_stack_without_messages_or_lines_of_code(self):
        e = _database_error()
        stack = log_safe.stack(e)
        assert SECRET not in stack and "_database_error" in stack and "line " in stack
        assert "raise" not in stack, "frames only: no line of code"

    def test_an_http_status_and_a_providers_code(self):
        class ClientError(Exception):
            code, status = 429, "RESOURCE_EXHAUSTED"

        line = log_safe.error(ClientError(f"quota for prompt '{SECRET}'"))
        assert "HTTP 429" in line and "RESOURCE_EXHAUSTED" in line and SECRET not in line

    def test_an_answer_by_its_shape(self):
        raw = f'{{"facts": [{{"subject": "{SECRET}"}}]'
        assert log_safe.answer(raw, None) == f"a reply of {len(raw)} characters, ends with a closing brace, not JSON"
        parsed = {"contradictions": "none", "why": SECRET}
        line = log_safe.answer(json.dumps(parsed), parsed, "contradictions")
        assert "JSON object of 2 keys" in line and "'contradictions': string of 4 characters" in line
        assert SECRET not in line
        assert log_safe.answer("x" * 10) == "a reply of 10 characters, no closing brace at the end"

    def test_quoted_values_and_fields_outside_the_schema(self):
        assert log_safe.without_values(f"anchor '{SECRET}' is not YYYY-MM-DD") == "anchor <value> is not YYYY-MM-DD"
        assert log_safe.without_values(f'unit "{SECRET}" is not one of day, week') == "unit <value> is not one of day, week"
        line = log_safe.fields({"text": SECRET, "direction": "last", SECRET: 1}, ("text", "direction"))
        assert line == "fields direction, text (+1 not in the schema)"
        assert log_safe.length(SECRET) == "<12 characters>" and log_safe.length(None) == "<none>"


# -------------------------------------------------------------- extractor

class TestExtractor:
    def test_the_dropped_fact_says_why_not_what(self, engine_log):
        facts = fact_extractor.read_facts(
            [{"subject": f"Maria {SECRET}", "predicate": "stores", "object": f"/home/{SECRET}/notes.md",
              "confidence": 0.9}],
            f"Maria {SECRET} keeps notes",
        )
        assert facts == []
        assert "dropped a triple" in engine_log.text and "object_is_a_path" in engine_log.text
        _clean(engine_log)

    def test_the_triple_without_an_anchor(self, engine_log):
        f = ExtractedFact(subject=SECRET, predicate_raw="likes", object="tea", confidence=0.9)
        assert fact_extractor.keep_grounded([f], "I like tea", "u1") == []
        assert "ungrounded" in engine_log.text
        _clean(engine_log)

    def test_the_unreadable_answer_by_its_shape(self, engine_log):
        with pytest.raises(fact_extractor.ExtractionFailed) as e:
            fact_extractor.read_json(lambda p, n: f"Sure! {ILLNESS} {{", "prompt", 100)
        assert "not JSON" in engine_log.text and SECRET not in str(e.value)
        _clean(engine_log)

    def test_a_failed_call_without_the_message(self, engine_log):
        def call(p, n):
            raise RuntimeError(f"400 the prompt '{ILLNESS}' was refused")

        with pytest.raises(fact_extractor.ExtractionFailed) as e:
            fact_extractor.read_json(call, "prompt", 100)
        assert "RuntimeError" in engine_log.text and SECRET not in str(e.value)
        _clean(engine_log)

    def test_the_dropped_relative_times(self, engine_log):
        assert fact_extractor._times_of_the_answer({"relative_times": f"{SECRET} yesterday"}, "2023-05-08",
                                                "yesterday") is None
        fact_extractor._times_of_the_answer(
            {"relative_times": [{"text": f"{SECRET} last week", "direction": "last", "unit": "week"},
                                {"text": "yesterday", "direction": SECRET}]},
            "2023-05-08", "yesterday I went out",
        )
        text = engine_log.text
        assert "is a string of" in text and "the words are not in the memory" in text
        assert "direction <value> is not one of" in text and "fields direction, text" in text
        _clean(engine_log)


# ------------------------------------------------------------------ judge

class TestJudge:
    def _judge(self, answer):
        candidate = AgentFact(id=uuid.uuid4(), subject_label="Maria", subject_canonical="Maria",
                              predicate_raw="has", object_label="flu", valid_from=None)
        return judge.judge(answer, [candidate], "Maria", "has", ILLNESS,
                               single_value=lambda c: (False, None))

    def test_the_unreadable_answer(self, engine_log):
        with pytest.raises(judge.JudgementUnreadable):
            self._judge(lambda p, n: f"I think {ILLNESS} replaces the flu")
        assert "not JSON" in engine_log.text
        _clean(engine_log)

    def test_the_answer_without_the_list(self, engine_log):
        with pytest.raises(judge.JudgementUnreadable):
            self._judge(lambda p, n: json.dumps({"verdict": f"{ILLNESS} replaces it"}))
        assert "no 'contradictions'" in engine_log.text
        _clean(engine_log)

    def test_the_failed_call(self, engine_log):
        def call(p, n):
            raise RuntimeError(f"refused: {ILLNESS}")

        with pytest.raises(judge.JudgementMissing) as e:
            self._judge(call)
        assert SECRET not in str(e.value) and "RuntimeError" in str(e.value)
        _clean(engine_log)


# ----------------------------------------------------------- vocabulary

class TestVocabulary:
    def test_split_votes_and_no_majority(self, engine_log):
        answers = iter(['{"many_values": false}', '{"many_values": true}', '{"many_values": false}'])
        assert vocabulary.decide_single_value(lambda d, p: next(answers), "has_condition", ILLNESS) is True
        answers = iter(['{"many_values": false}', '{"many_values": true}', "maybe"])
        assert vocabulary.decide_single_value(lambda d, p: next(answers), "has_condition", ILLNESS) is None
        assert "answers split" in engine_log.text and "no majority" in engine_log.text
        assert "has_condition" in engine_log.text, "the relation is the vocabulary's word"
        _clean(engine_log)

    def test_the_unusable_answer_in_the_exception(self):
        with pytest.raises(vocabulary.UnreadableAnswer) as e:
            vocabulary.read_single_value(f'{{"many_values": "{SECRET}"}}', severe=True)
        assert SECRET not in str(e.value) and "'many_values': string of" in str(e.value)


# ----------------------------------------------------------------- store

class TestStore:
    def test_aliases_by_rows_not_by_names(self, db, tenant, engine_log):
        mem = entities.memory_of(tenant.id, None, "u1", None)
        row, outcome = store.add_alias(db, entities.TABLES, mem, f"{SECRET} Labs", f"Globex {SECRET}")
        assert outcome == "created"
        _, outcome = store.add_alias(db, entities.TABLES, mem, f"Globex {SECRET}", f"{SECRET} Labs")
        assert outcome == "conflict"
        assert f"row {row.id}" in engine_log.text and "refused" in engine_log.text
        _clean(engine_log)

    def test_the_vocabulary_that_does_not_answer(self, db, tenant, engine_log):
        def ask(prompt, think):
            raise RuntimeError(f"503 while reading '{ILLNESS}'")

        with pytest.raises(store.ResolutionUnavailable) as e:
            store.resolve_predicate(db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None),
                                       "has_condition", ILLNESS, ask=ask, embed=None, strict=True)
        assert SECRET not in str(e.value) and "RuntimeError" in str(e.value)
        _clean(engine_log)


# ------------------------------------------------- same object (6.1.0)

class TestSameObject:
    """memory_core 6.1.0 `objects`: the names it unites and the memory's words
    it shows the writer are the end user's; the lines say counts."""

    def _fact(self, db, tenant, obj):
        f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, end_user_id="u1", subject_label="Maria",
                      subject_canonical="Maria", predicate_raw="lives_in", predicate_canonical="lives_in",
                      predicate_family="places", object_label=obj, object_canonical=obj, confidence=0.9,
                      source_memory_id=self._memory(db, tenant).id,
                      embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1))
        db.add(f)
        db.commit()
        return f

    def _memory(self, db, tenant):
        m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content=ILLNESS, graph_sync_status="done")
        db.add(m)
        db.commit()
        return m

    @staticmethod
    def _answers(select, pair='{"pairs": [{"pair": 1, "verdict": "same"}]}'):
        def ask(prompt, think):
            if '{"descriptions"' in prompt:
                return ('{"descriptions": [{"mention": 1, "description": "d"}, '
                        '{"mention": 2, "description": "d"}]}')
            return pair if "Pair 1:" in prompt else select
        return ask

    def _unite(self, db, tenant, ask, **kw):
        from korely_agent.services.facts.temporal import true_at
        from korely_agent.services.memory_core import objects

        return objects.unite_new_objects(
            db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None),
            [objects.Mention(name=f"Italy {SECRET}", subject="Maria", relation="lives_in",
                             family="places", words=ILLNESS)],
            ask=ask, embed=lambda m: [1.0] + [0.0] * (EMBEDDING_DIM - 1),
            words_of=lambda ids: {i: ILLNESS for i in ids}, is_open=true_at(), **kw)

    def test_a_union_by_counts(self, db, tenant, engine_log):
        self._fact(db, tenant, f"Italia {SECRET}")
        answer = '{"answers": [{"mention": 1, "verdict": "same", "same_as": 1}]}'
        out = self._unite(db, tenant, self._answers(answer))
        assert out["linked"] == 1 and "same object" in engine_log.text
        assert "object link created" in engine_log.text
        _clean(engine_log)

    def test_a_question_that_fails(self, db, tenant, engine_log):
        self._fact(db, tenant, f"Italia {SECRET}")

        def ask(prompt, think):
            raise RuntimeError(f"503 while reading '{ILLNESS}'")

        with pytest.raises(store.ResolutionUnavailable) as e:
            self._unite(db, tenant, ask)
        assert SECRET not in str(e.value) and "RuntimeError" in str(e.value)
        assert self._unite(db, tenant, ask, strict=False)["linked"] == 0
        assert "same-object question failed" in engine_log.text
        _clean(engine_log)

    def test_an_answer_that_cannot_be_used(self, db, tenant, engine_log):
        self._fact(db, tenant, f"Italia {SECRET}")
        bad = f'{{"answers": [{{"mention": 1, "verdict": "{SECRET}", "same_as": 1}}]}}'
        with pytest.raises(store.ResolutionUnusable) as e:
            self._unite(db, tenant, self._answers(bad))
        assert SECRET not in str(e.value)
        assert "same-object answer unusable" in engine_log.text
        _clean(engine_log)


# --------------------------------------------------- writing and candidates

def _memory(db, tenant):
    m = AgentMemory(tenant_id=tenant.id, end_user_id="u1", content=ILLNESS, graph_sync_status="processing")
    db.add(m)
    db.commit()
    return m


def _write(db, tenant, obj, *, columns=None, relation=None, predicate="has", subject="Maria"):
    return write_fact(
        db, entities.TABLES, entities.memory_of(tenant.id, None, "u1", None),
        columns=columns or dict(tenant_id=tenant.id, project_id=None, end_user_id="u1", agent_id=None, run_id=None),
        relation=relation or Relation("has_condition", False, "health"), is_open=true_at(),
        embed=lambda f: None, judge=lambda g: [], subject=subject, subject_type="organization",
        predicate_raw=predicate, obj=obj, object_is_literal=False, confidence=0.9,
        source_memory_id=_memory(db, tenant).id, model_used=None,
    )


class TestWritingAndCandidates:
    def test_the_identity_alias_that_fails(self, db, tenant, engine_log, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError(f"alias {SECRET} locked")

        monkeypatch.setattr(store, "add_alias", broken)
        fact, _ = _write(db, tenant, f"Globex {SECRET}", subject=f"Acme {SECRET}",
                           predicate="renamed_to", relation=Relation("renamed_to", None, "identity"))
        assert f"identity alias of fact {fact.id} failed" in engine_log.text
        _clean(engine_log)

    def test_the_semantic_stage_that_fails(self, db, tenant, engine_log, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError(f"[parameters: ('{SECRET}',)]")

        monkeypatch.setattr(candidates, "_pg", lambda db: True)
        monkeypatch.setattr(candidates, "nearest", broken)
        with pytest.raises(candidates.CandidatesUnread) as e:
            candidates.for_the_judge(
                db, entities.TABLES, entities.memory_scope(tenant.id, None, "u1"),
                exclude_id=uuid.uuid4(), subject_label="Maria", predicate_canonical="has_condition",
                is_open=true_at(), vector=[0.1] * EMBEDDING_DIM,
            )
        assert SECRET not in str(e.value) and "semantic candidate retrieval failed" in engine_log.text
        assert "in broken" in engine_log.text, "the frames say where"
        _clean(engine_log)


# --------------------------------------------------------------- worker

class TestWorker:
    def test_the_write_that_fails_on_the_database(self, db, tenant, engine_log):
        """A real error of the database: SQLAlchemy writes the row's values
        in its message ("[parameters: ...]") unless the engine hides them;
        the log has the classes, SQLite's error name and the frames."""
        m = _memory(db, tenant)
        seen = []

        def write_one(ef, defer):
            try:
                return _write(db, tenant, ILLNESS,
                               columns=dict(tenant_id=None, project_id=None, end_user_id="u1", agent_id=None,
                                            run_id=None))
            except IntegrityError as e:
                seen.append(str(e))
                raise

        assert worker.write_memory(db, m, ["ef"], write_one=write_one, shape_of=lambda f, c: {}) == []
        assert seen
        bind = db.get_bind()
        if not getattr(getattr(bind, "engine", bind), "hide_parameters", False):
            # An engine made with hide_parameters=True hides them;
            # one made without may not, and PostgreSQL's DETAIL stays.
            assert SECRET in seen[0], "the hazard is real on this database"
        text = engine_log.text
        assert "persist failed for memory" in text and "IntegrityError" in text and "in write_one" in text
        _clean(engine_log)

    def test_the_failed_drain(self, db, tenant, engine_log):
        m = _memory(db, tenant)
        m.graph_sync_status = "pending"
        db.commit()
        worker.claim_pending(db, AgentMemory, limit=10, expiry_minutes=15)

        def do_it(mm, ev):
            raise _database_error()

        assert worker.drain_one(db, m, do_it) is False
        text = engine_log.text
        assert "async pending process failed" in text and "SQLSTATE 23505" in text
        _clean(engine_log)
