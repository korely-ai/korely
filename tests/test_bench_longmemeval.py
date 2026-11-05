"""scripts/bench_longmemeval.py, offline: the corpus it writes and the
benchmark's own prompts (LongMemEval at commit 9e0b455)."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "bench_longmemeval", Path(__file__).resolve().parents[1] / "scripts" / "bench_longmemeval.py")
B = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(B)

ITEM = {
    "question_id": "q1", "question_type": "temporal-reasoning", "question": "When?",
    "answer": "May", "question_date": "2023/05/30 (Tue) 22:10",
    "haystack_dates": ["2023/05/20 (Sat) 02:21"],
    "haystack_sessions": [[{"role": "user", "content": "I moved to Rome in May."},
                           {"role": "assistant", "content": "ok"}]],
}


def test_a_turn_is_a_memory_dated_by_its_session_and_short_turns_are_skipped():
    assert B.memories_of(ITEM) == [("user: I moved to Rome in May.", "2023-05-20T02:21:00Z")]


def test_the_judge_prompt_follows_the_type_and_abstention():
    assert "off-by-one" in B.judge_prompt(ITEM, "In May")
    pref = dict(ITEM, question_type="single-session-preference")
    assert "Rubric: May" in B.judge_prompt(pref, "x")
    abs_ = dict(ITEM, question_id="q1_abs")
    assert "unanswerable" in B.judge_prompt(abs_, "x")


def test_the_reader_prompt_is_the_benchmarks():
    p = B.READER_PROMPT.format("BLOCK", ITEM["question_date"], ITEM["question"])
    assert p.startswith("I will give you several history chats between you and a user, as well "
                        "as the relevant user facts")
    assert p.endswith("Current Date: 2023/05/30 (Tue) 22:10\nQuestion: When?\nAnswer (step by step):")
