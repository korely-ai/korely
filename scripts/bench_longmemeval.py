"""LongMemEval on an install of Korely, with the benchmark's own reader prompt
and judge (decided on 15 Oct 2026, the public numbers).

Wu et al., "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive
Memory", ICLR 2025; code at github.com/xiaowu0162/LongMemEval. Each question
is its own world: its chat sessions are written as memories of one end user
(`<prefix><question_id>`), one memory per turn, "{role}: {content}", with the
session's date as the write's `timestamp`, turns under 12 characters skipped
(the rule of the retrieval-recall measure). When the writer has
digested them, the question is asked of GET /v1/context, and the block goes
to the reader in the benchmark's prompt for retrieved chats plus user facts,
step by step (src/generation/run_generation.py, `prepare_prompt`, at commit
9e0b455), with the question's date as "Current Date". The judge is the
benchmark's (src/evaluation/evaluate_qa.py, `get_anscheck_prompt`, same
commit): one prompt per question type and one for the abstention questions,
temperature 0, at most 10 tokens, "yes" in the reply is right. Run with
gpt-4o (2024-08-06) as reader and judge, it is the protocol of the published
rows (Zep, Supermemory, Mastra with GPT-4o).

The plumbing is scripts/bench_locomo.py's, imported: the install's client,
the OpenAI-compatible endpoint, the retries, the append-only results file a
rerun resumes from, the spend, the Wilson interval.

    KORELY_API_URL=... KORELY_API_KEY=... KORELY_ADMIN_KEY=... \\
    READER_BASE_URL=https://openrouter.ai/api/v1 READER_MODEL=openai/gpt-4o-2024-08-06 \\
    READER_API_KEY=... JUDGE_BASE_URL=... JUDGE_MODEL=... JUDGE_API_KEY=... \\
      python scripts/bench_longmemeval.py --data longmemeval_oracle.json --run --out lme.jsonl

--estimate prints the spend and stops; --run writes, waits, reads and judges,
and resumes where a previous run on the same file stopped.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_locomo as L  # noqa: E402

SOURCE = "https://github.com/xiaowu0162/LongMemEval/tree/9e0b455f4ef0e2ab8f2e582289761153549043fc"
MIN_TURN_CHARS = 12
DEFAULT_BUDGET = 4000            # /v1/context's recommendation for questions about the past
GPT_4O = (2.50, 10.00)           # USD per million tokens in and out (OpenAI's list price)
READER_MAX_TOKENS = 1024         # the step-by-step answer
JUDGE_MAX_TOKENS = 10            # evaluate_qa.py

# src/generation/run_generation.py, prepare_prompt: retrieved chats plus user
# facts, step by step (the block of /v1/context holds both).
READER_PROMPT = ("I will give you several history chats between you and a user, as well as the "
                 "relevant user facts extracted from the chat history. Please answer the question "
                 "based on the relevant chat history and the user facts. Answer the question step "
                 "by step: first extract all the relevant information, and then reason over the "
                 "information to get the answer.\n\n\nHistory Chats:\n\n{}\n\nCurrent Date: {}\n"
                 "Question: {}\nAnswer (step by step):")

# src/evaluation/evaluate_qa.py, get_anscheck_prompt, verbatim.
_DEFAULT = ("I will give you a question, a correct answer, and a response from a model. Please "
            "answer yes if the response contains the correct answer. Otherwise, answer no. If the "
            "response is equivalent to the correct answer or contains all the intermediate steps "
            "to get the correct answer, you should also answer yes. If the response only contains "
            "a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\n"
            "Correct Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer "
            "yes or no only.")
JUDGE_PROMPTS = {
    "single-session-user": _DEFAULT,
    "single-session-assistant": _DEFAULT,
    "multi-session": _DEFAULT,
    "temporal-reasoning": (
        "I will give you a question, a correct answer, and a response from a model. Please answer "
        "yes if the response contains the correct answer. Otherwise, answer no. If the response is "
        "equivalent to the correct answer or contains all the intermediate steps to get the correct "
        "answer, you should also answer yes. If the response only contains a subset of the "
        "information required by the answer, answer no. In addition, do not penalize off-by-one "
        "errors for the number of days. If the question asks for the number of days/weeks/months, "
        "etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is "
        "18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\n"
        "Model Response: {}\n\nIs the model response correct? Answer yes or no only."),
    "knowledge-update": (
        "I will give you a question, a correct answer, and a response from a model. Please answer "
        "yes if the response contains the correct answer. Otherwise, answer no. If the response "
        "contains some previous information along with an updated answer, the response should be "
        "considered as correct as long as the updated answer is the required answer.\n\nQuestion: "
        "{}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer "
        "yes or no only."),
    "single-session-preference": (
        "I will give you a question, a rubric for desired personalized response, and a response "
        "from a model. Please answer yes if the response satisfies the desired response. "
        "Otherwise, answer no. The model does not need to reflect all the points in the rubric. "
        "The response is correct as long as it recalls and utilizes the user's personal "
        "information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the "
        "model response correct? Answer yes or no only."),
}
JUDGE_ABSTENTION = (
    "I will give you an unanswerable question, an explanation, and a response from a model. Please "
    "answer yes if the model correctly identifies the question as unanswerable. The model could say "
    "that the information is incomplete, or some other information is given but the asked "
    "information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the "
    "model correctly identify the question as unanswerable? Answer yes or no only.")

_DATE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})(?:\s*\(\w+\))?\s*(\d{2}):(\d{2})")


def session_time(date_str: str) -> str | None:
    """The ISO moment of a LongMemEval date, "2023/05/20 (Sat) 02:21"."""
    m = _DATE.match(date_str or "")
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    return L.iso_utc(datetime(y, mo, d, h, mi, tzinfo=timezone.utc))


def memories_of(item: dict) -> list:
    """[(text, timestamp)] of one question's sessions, in order."""
    out = []
    for date, session in zip(item["haystack_dates"], item["haystack_sessions"]):
        when = session_time(date)
        for turn in session:
            content = str(turn.get("content") or "").strip()
            if len(content) >= MIN_TURN_CHARS:
                out.append((f"{turn.get('role')}: {content}", when))
    return out


def judge_prompt(item: dict, answer: str) -> str:
    abstention = str(item["question_id"]).endswith("_abs")
    template = JUDGE_ABSTENTION if abstention else JUDGE_PROMPTS[item["question_type"]]
    return template.format(item["question"], item["answer"], answer)


def write_question(korely: L.Korely, item: dict, user: str) -> int:
    """Write what is missing of one question's memories; returns how many. A
    rerun counts the texts already stored (a turn said twice is two memories),
    so a write cut short is finished, never doubled."""
    from collections import Counter

    have = Counter(m.get("content") for m in korely.memories(user))
    written = 0
    for text, when in memories_of(item):
        if have[text] > 0:
            have[text] -= 1
            continue
        korely.add(text, user, when, {"bench": "longmemeval", "question_id": item["question_id"]})
        written += 1
    return written


def wait_all(korely: L.Korely, *, poll: float, deadline: float) -> None:
    start, said = time.monotonic(), 0.0
    while True:
        left = korely.processing()
        if left == 0:
            return
        if time.monotonic() - start > deadline:
            raise L.Fatal(f"{left} memories still being digested after {deadline / 3600:.1f} h")
        if time.monotonic() - said > 300:
            print(f"  {left} memories still being digested", flush=True)
            said = time.monotonic()
        time.sleep(poll)


def answer_one(item: dict, user: str, *, korely, reader, judge, budget: int, spend) -> dict:
    block = korely.context(item["question"], user, budget)
    history = block.get("context") or "\n\n".join(
        x for x in (block.get("stable"), block.get("volatile")) if x)
    prompt = READER_PROMPT.format(history, item["question_date"], item["question"])
    reply = reader.ask([{"role": "user", "content": prompt}], max_tokens=READER_MAX_TOKENS)
    spend.asked("reader", reply)
    verdict = judge.ask([{"role": "user", "content": judge_prompt(item, reply.text)}],
                        max_tokens=JUDGE_MAX_TOKENS)
    spend.asked("judge", verdict)
    right = "yes" in verdict.text.strip().lower()
    return {"key": item["question_id"], "question_type": item["question_type"],
            "abstention": str(item["question_id"]).endswith("_abs"),
            "question": item["question"], "gold": item["answer"],
            "question_date": item["question_date"], "context": history,
            "context_tokens": block.get("tokens"), "answer": reply.text,
            "judge_raw": verdict.text, "label": "CORRECT" if right else "WRONG",
            "reader": {"model": reply.model, "in": reply.prompt_tokens, "out": reply.completion_tokens},
            "at": L.iso_utc(datetime.now(timezone.utc))}


def summary(records: dict, items: list) -> dict:
    by_type: dict = {}
    for it in items:
        rec = records.get(it["question_id"])
        if not L.is_done(rec):
            continue
        t = by_type.setdefault(it["question_type"], [0, 0])
        t[0] += rec["label"] == "CORRECT"
        t[1] += 1
    right = sum(v[0] for v in by_type.values())
    judged = sum(v[1] for v in by_type.values())
    lo, hi = L.wilson(right, judged)
    return {"questions": len(items), "judged": judged, "correct": right,
            "accuracy": right / judged if judged else 0.0, "ci95": [lo, hi],
            "task_average": (sum(v[0] / v[1] for v in by_type.values()) / len(by_type)
                             if by_type else 0.0),
            "types": {k: {"correct": v[0], "judged": v[1], "accuracy": v[0] / v[1]}
                      for k, v in sorted(by_type.items())}}


def estimate(items: list) -> dict:
    mems = sum(len(memories_of(it)) for it in items)
    reader_in = len(items) * (DEFAULT_BUDGET + 400)
    usd = (reader_in * GPT_4O[0] + len(items) * 400 * GPT_4O[1]) / 1e6
    judge_usd = (len(items) * 900 * GPT_4O[0] + len(items) * 3 * GPT_4O[1]) / 1e6
    return {"questions": len(items), "memories": mems, "reader_usd": round(usd, 2),
            "judge_usd": round(judge_usd, 2)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", required=True, help="longmemeval_oracle.json (or _s)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--estimate", action="store_true")
    mode.add_argument("--run", action="store_true")
    ap.add_argument("--out", type=Path, default=Path("bench_longmemeval.jsonl"))
    ap.add_argument("--limit", type=int, default=None, help="only the first N questions")
    ap.add_argument("--user-prefix", default="lme-")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    ap.add_argument("--usd-cap", type=float, default=15.0,
                    help="stop before the reader and the judge pass this many USD")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--wait-hours", type=float, default=12.0)
    args = ap.parse_args(argv)

    items = json.load(open(args.data, encoding="utf-8"))[: args.limit]
    est = estimate(items)
    print(f"LongMemEval ({SOURCE}): {est['questions']} questions, {est['memories']} memories to "
          f"write; reader ~{est['reader_usd']} USD, judge ~{est['judge_usd']} USD at gpt-4o prices",
          flush=True)
    if args.estimate:
        return 0

    env = os.environ
    L.remember_secrets(env)
    korely = L.Korely(env["KORELY_API_URL"], env["KORELY_API_KEY"],
                      admin_key=env.get("KORELY_ADMIN_KEY"))
    reader = L.Endpoint(env["READER_BASE_URL"], env["READER_MODEL"], env["READER_API_KEY"])
    judge = L.Endpoint(env["JUDGE_BASE_URL"], env["JUDGE_MODEL"], env["JUDGE_API_KEY"])
    spend = L.Spend(write_cost=0.0, reader_price=GPT_4O, judge_price=GPT_4O, cap=args.usd_cap)
    users = {it["question_id"]: f"{args.user_prefix}{it['question_id']}" for it in items}

    print("writing", flush=True)
    with ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(write_question, korely, it, users[it["question_id"]]): it for it in items}
        done = 0
        for f in as_completed(futs):
            f.result()
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(items)} questions written", flush=True)
    print("waiting for the writer", flush=True)
    wait_all(korely, poll=30.0, deadline=args.wait_hours * 3600)

    records, bad = L.read_records(args.out)
    pending = [it for it in items if not L.is_done(records.get(it["question_id"]))]
    print(f"answering {len(pending)} questions ({len(items) - len(pending)} already done)", flush=True)
    stop = False
    with ThreadPoolExecutor(args.workers) as pool:
        futs = {}
        for it in pending:
            futs[pool.submit(answer_one, it, users[it["question_id"]], korely=korely,
                             reader=reader, judge=judge, budget=args.budget, spend=spend)] = it
        for f in as_completed(futs):
            try:
                rec = f.result()
            except L.Fatal as e:
                print(f"stopped: {e}", flush=True)
                stop = True
                break
            L.append_record(args.out, rec)
            records[rec["key"]] = rec
            if spend.room() <= 0:
                print(f"stopped: the reader and the judge reached the cap of {args.usd_cap} USD",
                      flush=True)
                stop = True
                break
    s = summary(records, items)
    s.update({"benchmark": "LongMemEval", "data": Path(args.data).name, "source": SOURCE,
              "reader": env["READER_MODEL"], "judge": env["JUDGE_MODEL"], "budget": args.budget,
              "spend_usd": {"reader": round(spend.eur("reader"), 3),
                            "judge": round(spend.eur("judge"), 3)},
              "stopped": stop, "at": L.iso_utc(datetime.now(timezone.utc))})
    Path(str(args.out).replace(".jsonl", "") + ".summary.json").write_text(
        json.dumps(s, indent=2, ensure_ascii=False))
    print(json.dumps({k: s[k] for k in ("judged", "correct", "accuracy", "ci95", "task_average",
                                        "types", "spend_usd")}, indent=2), flush=True)
    return 1 if stop else 0


if __name__ == "__main__":
    sys.exit(main())
