#!/usr/bin/env python3
"""LoCoMo against a running korely-agent: the first public number (2026-10-05).

LoCoMo (Maharana et al., ACL 2024, https://github.com/snap-research/locomo) is
ten long conversations between two people: 272 sessions spread over months,
5,882 turns, 1,986 questions about what was said. It was chosen as Korely's
first public benchmark number, to be measured after the next release of the
memory engine. This is the harness. It does not spend by default: it
prints what a run would cost and stops.

    # the estimate: no network, nothing written, no model asked
    python3 scripts/bench_locomo.py --data locomo10.json

    # what one write really costs, for a few cents (30 turns through the writer)
    KORELY_API_URL=http://localhost:8000 KORELY_API_KEY=... KORELY_ADMIN_KEY=... \\
        python3 scripts/bench_locomo.py --data locomo10.json --calibrate 30

    # the run: one conversation and 20 questions first, then everything
    READER_BASE_URL=https://api.scaleway.ai/v1 READER_MODEL=gpt-oss-120b READER_API_KEY=... \\
    JUDGE_BASE_URL=https://api.scaleway.ai/v1 JUDGE_MODEL=gpt-oss-120b JUDGE_API_KEY=... \\
        python3 scripts/bench_locomo.py --data locomo10.json --run \\
        --write-cost <calibrated> --write-cost-speakers each \\
        --limit-conversations 1 --limit-questions 20 --eur-cap 1

It runs from a checkout with Python 3.9 or later and the standard library alone:
everything goes over HTTP, to the install (/v1, and /admin for the usage) and
to two OpenAI-compatible chat endpoints, the reader and the judge. Secrets come
only from the environment (KORELY_API_KEY, KORELY_ADMIN_KEY, READER_API_KEY,
JUDGE_API_KEY) and are never printed or written: an error a provider sends back
is printed with any of them blanked out.

What --run does, and what the number means:

- With --speakers each (the default), each person of a conversation has an end
  user of their own (locomo-conv-3-joanna, locomo-conv-3-nate; --user-prefix),
  and each turn is written once, into its speaker's: the writer is anchored on
  the end user, so with both people in one user it called both of them "the
  user" and mixed up who did what. A memory is the turn it answers, then the
  turn: two messages, the other speaker's previous turn as "assistant" and this
  one as "user". POST /v1/memories takes one `content` string and no message
  list, so the two go in as "role: content" lines, the way Mem0 renders a
  message list for its own writer (mem0/memory/utils.py, `parse_messages`).
  A session's first turn has no previous turn and is one message. Still one
  memory per turn: 5,882, not twice that, as Mem0's two-perspective ingestion
  would be.
- With --speakers one, the whole conversation goes into one end user
  (locomo-conv-0 .. locomo-conv-9), one memory per turn.
- Either way a turn reads "Speaker: text", with the caption of the image in
  brackets when the turn shares one; `timestamp` is the session's date_time
  ("1:56 pm on 8 May, 2023" is 2023-05-08T13:56:00Z); `metadata` holds the
  session, the dia_id and the speaker. The worker digests one user's memories
  in the order they were written.
- Once a conversation is digested (GET /v1/events, `processing` 0), every
  question of categories 1-4 gets GET /v1/context (token_budget 4000: with
  --speakers each, 2,000 for each of the two people, the two blocks handed to
  the reader under headings with their names), an answer from the reader with
  "Current date:" set to the conversation's last session day, and Mem0's
  LoCoMo judge, verbatim. Category 5 (adversarial, 446 questions) is skipped,
  as Mem0, Zep and Hindsight do: 1,540 questions remain.
- One JSONL line per question (--out). The same command again resumes: turns
  already stored are not written twice, questions already judged are not asked
  again. A file written under other conditions (reader, judge, budget, data,
  prompts) is refused rather than mixed.
- --eur-cap stops before the estimated spend of this invocation passes it:
  before a conversation is written (its writes and its questions must fit),
  and before each group of questions.
- The end prints the accuracy per category and overall with the conditions it
  must be published with (install, writer, reader, judge, budget, data), and
  writes them next to the JSONL (<out>.summary.json).

What it costs: the writes. A write runs the install's writer, about five model
calls; 0.0019 EUR a memory is the LongMemEval figure with gpt-oss, but LoCoMo
turns are much shorter (124 characters on average), so --calibrate measures the
real figure before anything bigger is spent, in the --speakers mode it is run
in. A two-message memory is longer than a turn alone, and the writer reads it
and the ten memories before it: the estimate adds those tokens when the write
cost was measured in the other mode (--write-cost-speakers).
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------- constants

DATA_URL = "https://github.com/snap-research/locomo/raw/main/data/locomo10.json"
# The file this harness was written and counted against: data/locomo10.json of
# snap-research/locomo on main, read 2026-10-05 (10 conversations, 272
# sessions, 5,882 turns, 1,986 questions). Another file runs too, and is named
# as another file in the output: a number has to say which data it is about.
KNOWN_SHA256 = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"

# The categories of locomo10.json as LoCoMo's own evaluation reads them
# (task_eval/evaluation.py: 1 is scored entity by entity, 3 before the ';', 5
# by the refusal) and as mem0ai/memory-benchmarks names them. The papers order
# their tables differently, so the output prints the number next to the name.
CATEGORY_NAMES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop",
                  5: "adversarial"}
ALL_ASKED = (1, 2, 3, 4)
# The categories this run asks: all four, or those of --category (a reread of
# one kind of question, e.g. the 321 temporal ones for a note about dates).
ASKED = ALL_ASKED
SKIPPED = 5


def scope_of_asked() -> str:
    """"categories 1-4", or the ones --category kept, by number and name."""
    if ASKED == ALL_ASKED:
        return "categories 1-4"
    return "categor" + ("y " if len(ASKED) == 1 else "ies ") + ", ".join(
        f"{c} ({CATEGORY_NAMES[c]})" for c in ASKED)

# EUR per million tokens, in and out: Scaleway Generative APIs, gpt-oss-120b
# (read 2026-10-02), the writer, reader and judge chosen for this run. Pass
# your provider's.
SCALEWAY_GPT_OSS = (0.15, 0.60)
# EUR per memory written: the install's writer on one memory, about five model
# calls. 0.0019 is the LongMemEval figure with gpt-oss, measured on memories of
# one turn each. LoCoMo turns are much shorter (124 characters against whole
# LongMemEval turns), so the real figure is probably lower: --calibrate
# measures it, in the --speakers mode it runs in.
WRITE_COST_EUR = 0.0019
# The plan had about 6-9 EUR for LoCoMo on the Scaleway credit: the default
# cap is its top. --eur-cap 0 runs without one.
DEFAULT_CAP_EUR = 9.0
DEFAULT_BUDGET = 4000

# Who an end user is. "each": one end user per person of a conversation, the
# default; "one": the whole conversation in one end user.
SPEAKERS = ("each", "one")
# What the writer's extraction reads besides the memory: the ten memories of
# the same end user before it, 300 characters of each (memory_core
# fact_extractor.py, MAX_PREVIOUS and MAX_PREVIOUS_CHARS, Mem0's own numbers).
# The estimate prices the text of the two modes with them.
PREVIOUS_MEMORIES = 10
PREVIOUS_MEMORY_CHARS = 300

# The assumptions of the estimate. Tokens are characters / 4, the packer's own
# measure, and the block counts at its full budget: a LoCoMo conversation holds
# 400 to 700 memories, more than any budget takes. Out: gpt-oss-120b on
# Scaleway answered LongMemEval with 215-340 tokens a call at its default
# reasoning (2026-10-02); "low" spends less. The judge answers one line.
READER_OUT_TOKENS = 350
JUDGE_OUT_TOKENS = 200
CHAT_OVERHEAD_TOKENS = 100   # roles and the model's chat template
ANSWER_CHARS = 240           # the answer the judge reads

# What a reasoning model needs to finish: thinking is spent from the same
# budget as the answer, and 512 tokens truncated gpt-oss's answers on
# LongMemEval. A cap, not a cost: only what is generated is billed.
READER_MAX_TOKENS = 4096
JUDGE_MAX_TOKENS = 4096

CATEGORY_HEAD = "category 5 (adversarial) is skipped, as Mem0, Zep and Hindsight do"

# The reader, as a customer uses /v1/context: `stable` (the reader note) in the
# system prompt, `volatile` in the turn, and the date next to the question,
# never in the block (CHANGELOG 0.1.16, "Put the question's date in your
# reader's prompt"). With --speakers each the memory comes in one section per
# person, each under a heading with the person's name, and the reader note,
# which speaks of "the user", is told whom it means. Every text the reader is
# given is fingerprinted in every JSONL line (`reader_prompt_text`).
READER_SYSTEM = ("You answer questions about {speaker_a} and {speaker_b} from the memory of "
                 "their conversations. Give a short, direct answer.")
READER_SECTIONS = 'In each section of the memory, "the user" means the person named in its heading.'
SECTION_HEADING = "# {name}"
EMPTY_SECTION = "(nothing came back for this person)"
READER_TURN = "{volatile}\n\nCurrent date: {today}\n\nQuestion: {question}"

# Mem0's LoCoMo judge, verbatim: ACCURACY_PROMPT in evaluation/metrics/llm_judge.py
# of mem0ai/mem0, the last version before that folder was retired in favour of
# mem0ai/memory-benchmarks (commit 9315e30, 2026-06-13):
# https://github.com/mem0ai/mem0/blob/aae5989e78a6188b3b047c104d960c9ad0927e75/evaluation/metrics/llm_judge.py
# Mem0 sends it as the only message (role user), temperature 0, JSON mode, and
# counts a point when json.loads(...)["label"] == "CORRECT". One string per
# line so that the trailing spaces of four lines are visible and no editor can
# strip them; the curly quotes are Mem0's too. tests/test_bench_locomo.py
# holds its SHA-256, the hash of the text in Mem0's file.
ACCURACY_PROMPT = "\n".join([
    "",
    "Your task is to label an answer to a question as ’CORRECT’ or ’WRONG’. You will be given the following data:",
    "    (1) a question (posed by one user to another user), ",
    "    (2) a ’gold’ (ground truth) answer, ",
    "    (3) a generated answer",
    "which you will score as CORRECT/WRONG.",
    "",
    "The point of the question is to ask about something one user should know about the other user based on their prior conversations.",
    "The gold answer will usually be a concise and short answer that includes the referenced topic, for example:",
    "Question: Do you remember what I got the last time I went to Hawaii?",
    "Gold answer: A shell necklace",
    "The generated answer might be much longer, but you should be generous with your grading - as long as it touches on the same topic as the gold answer, it should be counted as CORRECT. ",
    "",
    "For time related questions, the gold answer will be a specific date, month, year, etc. The generated answer might be much longer or use relative time references (like \"last Tuesday\" or \"next month\"), but you should be generous with your grading - as long as it refers to the same date or time period as the gold answer, it should be counted as CORRECT. Even if the format differs (e.g., \"May 7th\" vs \"7 May\"), consider it CORRECT if it's the same date.",
    "",
    "Now it's time for the real question:",
    "Question: {question}",
    "Gold answer: {gold_answer}",
    "Generated answer: {generated_answer}",
    "",
    "First, provide a short (one sentence) explanation of your reasoning, then finish with CORRECT or WRONG. ",
    "Do NOT include both CORRECT and WRONG in your response, or it will break the evaluation script.",
    "",
    "Just return the label CORRECT or WRONG in a json format with the key as \"label\".",
    "",
])
JUDGE_PROMPT_URL = ("https://github.com/mem0ai/mem0/blob/aae5989e78a6188b3b047c104d960c9ad0927e75/"
                    "evaluation/metrics/llm_judge.py")

SECRET_NAMES = ("KORELY_API_KEY", "KORELY_ADMIN_KEY", "READER_API_KEY", "JUDGE_API_KEY")
NEEDS = {
    "calibrate": ("KORELY_API_URL", "KORELY_API_KEY"),
    "blocks": ("KORELY_API_URL", "KORELY_API_KEY"),
    "run": ("KORELY_API_URL", "KORELY_API_KEY", "READER_BASE_URL", "READER_MODEL",
            "READER_API_KEY", "JUDGE_BASE_URL", "JUDGE_MODEL", "JUDGE_API_KEY"),
}
PAGE = 200                   # the largest page GET /v1/memories returns
API_TIMEOUT = 60.0
CHAT_TIMEOUT = 300.0         # a reasoning model on a long block can take a while
ERRORS_IN_A_ROW = 8          # this many questions failing one after the other stop the run

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# ---------------------------------------------------------------- secrets

_secrets: list = []


def remember_secrets(env) -> None:
    """The values never to print: every key the run was given."""
    found = {env.get(name) for name in SECRET_NAMES if env.get(name)}
    _secrets[:] = sorted((s for s in found if len(s) >= 4), key=len, reverse=True)


def redact(text) -> str:
    """`text` with every secret blanked out. A provider that refuses a key may
    quote it back in its error, and that error is printed and written."""
    s = str(text)
    for secret in _secrets:
        s = s.replace(secret, "***")
    return s


class HarnessError(Exception):
    """An error whose message never carries a secret."""

    def __str__(self):
        return redact(super().__str__())


class Fatal(HarnessError):
    """Stops the run: a refused key, credit or quota gone, a store not ready."""


class Unreachable(HarnessError):
    """No answer, or one that is not JSON."""


class HttpError(Exception):
    def __init__(self, status: int, text: str = "", retry_after=None):
        super().__init__(status, text)
        self.status = int(status)
        self.text = text or ""
        self.retry_after = retry_after

    def __str__(self):
        return redact(f"HTTP {self.status}: {self.text[:300]}")


# ---------------------------------------------------------------- the data

_LOCOMO_DATE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?\s+on\s+(\d{1,2})\s+([a-z]+)\.?,?\s+(\d{4})\s*$",
    re.IGNORECASE)


def parse_locomo_date(s) -> datetime:
    """A session's date_time, "1:56 pm on 8 May, 2023", as an aware datetime.

    LoCoMo gives no time zone. The store reads a naive time as UTC (memory_core
    `read_event_time`), so the timestamp says Z and means the same thing. By
    hand rather than with strptime: 12 am and 12 pm, a month written short
    ("Sept") or a missing comma read the same way, and anything else is a
    ValueError that names the string."""
    m = _LOCOMO_DATE.match(s if isinstance(s, str) else "")
    if not m:
        raise ValueError(f"not a LoCoMo date_time: {s!r}")
    hour, minute, half, day, month, year = m.groups()
    hour, minute, word = int(hour), int(minute), month.lower()
    number = next((i for i, name in enumerate(MONTHS, 1)
                   if len(word) >= 3 and name.startswith(word)), None)
    if number is None or not 1 <= hour <= 12 or minute > 59:
        raise ValueError(f"not a LoCoMo date_time: {s!r}")
    hour = hour % 12 + (12 if half.lower() == "p" else 0)
    return datetime(int(year), number, int(day), hour, minute, tzinfo=timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_DIA = re.compile(r"D\s*:?\s*(\d+)\s*:\s*(\d+)", re.IGNORECASE)


def dia_ids(evidence) -> tuple:
    """(the turns an evidence list names, as "D3:5", each once and in order;
    the entries that name none). An entry is mostly one id, but locomo10.json
    also has two or more in one string ("D8:6; D9:17", "D9:1 D4:4 D4:6"), a
    zero in front ("D30:05") and a colon too many ("D:11:26"): every id of an
    entry is read, and "D" alone names none. The analysis of a results file
    reads its evidence with this same function."""
    ids, bad = [], []
    for raw in evidence or []:
        found = [f"D{int(a)}:{int(b)}" for a, b in _DIA.findall(str(raw))]
        if not found:
            bad.append(str(raw))
        for d in found:
            if d not in ids:
                ids.append(d)
    return ids, bad


def turn_text(turn: dict) -> str:
    """The memory one turn becomes: "Speaker: text", and the caption of the
    image in brackets when the turn shares one (`blip_caption`, which every
    turn with an image carries; LoCoMo's own evaluation writes it as "and
    shared ..."). "" for a turn with nothing in it, which is not written."""
    speaker = str(turn.get("speaker") or "").strip()
    text = str(turn.get("text") or "").strip()
    caption = str(turn.get("blip_caption") or "").strip()
    body = " ".join(p for p in (text, f"[shared an image: {caption}]" if caption else "") if p)
    return f"{speaker}: {body}" if body else ""


def speaker_memory_text(turn: dict, previous=None) -> str:
    """The memory one turn becomes with --speakers each: the turn it answers,
    then the turn, as the two messages of one memory. POST /v1/memories takes
    one `content` string and no list of messages, so each message is a
    "role: content" line, as Mem0 renders a message list for its own writer
    (mem0/memory/utils.py, `parse_messages`): the other speaker's previous
    turn as "assistant", this one as "user", each "Speaker: text". The
    writer, whose end user is this speaker, reads "I" as them, with the
    question they answer in view. No previous turn (a session's first): the
    "user" line alone. "" for a turn with nothing in it, never written."""
    own = turn_text(turn)
    if not own:
        return ""
    context = turn_text(previous) if previous is not None else ""
    return f"assistant: {context}\nuser: {own}" if context else f"user: {own}"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "speaker"


def split_budget(total: int, parts: int) -> list:
    """`total` tokens in `parts` near-equal shares: 4,000 in two is 2,000 + 2,000."""
    return [total // parts + (1 if i < total % parts else 0) for i in range(parts)]


def evenly_spaced(items: list, n) -> list:
    """`n` of `items` spread through the list, in order; all of them when n is
    None or larger. The file groups a conversation's questions by category,
    single-hop mostly last, so the first N would be a harder sample."""
    if n is None or n >= len(items):
        return list(items)
    if n <= 0:
        return []
    return [items[(k * len(items)) // n] for k in range(n)]


@dataclass
class Session:
    number: int
    date_time: str
    when: datetime
    turns: list


@dataclass
class Memory:
    dia_id: str
    session: int
    content: str
    timestamp: str
    speaker: str = ""
    context_dia_id: str = ""    # the turn it answers (--speakers each); "" when none


@dataclass
class Question:
    key: str          # "<sample_id>:<place in the file's qa list>", stable whatever is skipped
    index: int
    category: int
    question: str
    gold: str
    evidence: list


@dataclass
class Conversation:
    index: int
    sample_id: str
    speaker_a: str
    speaker_b: str
    sessions: list
    questions: list

    def memories(self, speakers: str = "one") -> list:
        """One memory per turn, sessions in order, turns in order: the turn
        alone (--speakers one), or after the turn of the other speaker it
        answers, the latest before it in the session (--speakers each; LoCoMo's
        turns alternate, so it is the one just before)."""
        out = []
        for s in self.sessions:
            stamp = iso_utc(s.when)
            for i, t in enumerate(s.turns):
                speaker = str(t.get("speaker") or "")
                previous = None
                if speakers == "each":
                    previous = next((p for p in reversed(s.turns[:i])
                                     if str(p.get("speaker") or "") != speaker), None)
                    content = speaker_memory_text(t, previous)
                else:
                    content = turn_text(t)
                if not content:
                    continue
                answered = (str(previous.get("dia_id") or "")
                            if previous is not None and turn_text(previous) else "")
                out.append(Memory(str(t.get("dia_id") or ""), s.number, content, stamp, speaker,
                                  answered))
        return out

    def users(self, base: str, speakers: str) -> list:
        """[(end user id, speaker or None)]: `base` alone with --speakers one;
        with --speakers each one end user per person, speaker_a first
        (locomo-conv-3-joanna, locomo-conv-3-nate)."""
        if speakers == "one":
            return [(base, None)]
        a, b = slug(self.speaker_a), slug(self.speaker_b)
        if a == b:
            a, b = f"{a}-a", f"{b}-b"
        return [(f"{base}-{a}", self.speaker_a), (f"{base}-{b}", self.speaker_b)]

    def asked(self, limit=None) -> list:
        return evenly_spaced([q for q in self.questions if q.category in ASKED], limit)

    def last_day(self) -> datetime:
        """The day of the last session that has turns. conv-26 also lists the
        date_time of 16 sessions that hold none; they are not its last day."""
        return self.sessions[-1].when


def load_locomo(path) -> list:
    """locomo10.json as Conversations. Raises ValueError, KeyError or
    TypeError on a file of another shape."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list) or not raw:
        raise ValueError("expected the list of conversations of locomo10.json")
    out = []
    for i, item in enumerate(raw):
        conv = item["conversation"]
        sample_id = str(item.get("sample_id") or f"conv-{i}")
        sessions = []
        for n in sorted(int(k.split("_")[1]) for k in conv if re.fullmatch(r"session_\d+", k)):
            turns = conv.get(f"session_{n}") or []
            if turns:
                raw_date = conv.get(f"session_{n}_date_time")
                sessions.append(Session(n, raw_date, parse_locomo_date(raw_date), turns))
        if not sessions:
            raise ValueError(f"{sample_id} has no session with turns")
        speaker_a = str(conv.get("speaker_a") or "speaker A")
        speaker_b = str(conv.get("speaker_b") or "speaker B")
        if speaker_a == speaker_b:
            raise ValueError(f"{sample_id}: both speakers are called {speaker_a!r}")
        for s in sessions:
            for t in s.turns:
                if t.get("speaker") not in (speaker_a, speaker_b):
                    # Its end user, with --speakers each, would be nobody's.
                    raise ValueError(f"{sample_id} {t.get('dia_id')}: spoken by "
                                     f"{t.get('speaker')!r}, neither {speaker_a} nor {speaker_b}")
        questions = []
        for j, q in enumerate(item.get("qa") or []):
            gold = q.get("answer", q.get("adversarial_answer"))
            questions.append(Question(f"{sample_id}:{j}", j, int(q["category"]),
                                      str(q["question"]), "" if gold is None else str(gold),
                                      list(q.get("evidence") or [])))
        out.append(Conversation(i, sample_id, speaker_a, speaker_b, sessions, questions))
    return out


def metadata_of(conv: Conversation, mem: Memory) -> dict:
    meta = {"benchmark": "locomo", "sample_id": conv.sample_id, "session": mem.session,
            "dia_id": mem.dia_id, "speaker": mem.speaker}
    if mem.context_dia_id:
        meta["context_dia_id"] = mem.context_dia_id
    return meta


def current_date(conv: Conversation) -> str:
    d = conv.last_day()
    return f"{d:%Y-%m-%d} ({WEEKDAYS[d.weekday()]})"


def base_user(prefix: str, conv: Conversation) -> str:
    return f"{prefix}{conv.index}"


def user_of(users: list, mem: Memory) -> str:
    """The end user a memory goes to: its speaker's, or the only one."""
    if len(users) == 1:
        return users[0][0]
    return next(u for u, speaker in users if speaker == mem.speaker)


def by_user(users: list, mems: list) -> dict:
    """{end user id: its memories, in order}."""
    out = {u: [] for u, _ in users}
    for m in mems:
        out[user_of(users, m)].append(m)
    return out


def text_sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def data_info(path, convs: list) -> dict:
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return {"file": Path(path).name, "sha256": digest, "known": digest == KNOWN_SHA256,
            "conversations": len(convs)}


# ---------------------------------------------------------------- the estimate

def writer_text(convs: list, speakers: str) -> tuple:
    """(characters of a memory, characters the writer's extraction reads for
    it) on average, in a --speakers mode: the memory itself, and the ten
    memories of its end user before it, 300 characters of each."""
    own = read = n = 0
    for c in convs:
        for mems in by_user(c.users("u", speakers), c.memories(speakers)).values():
            for i, m in enumerate(mems):
                before = mems[max(0, i - PREVIOUS_MEMORIES):i]
                own += len(m.content)
                read += len(m.content) + sum(min(PREVIOUS_MEMORY_CHARS, len(p.content))
                                             for p in before)
                n += 1
    return (own / n, read / n) if n else (0.0, 0.0)


def estimate(convs: list, *, speakers: str = "each", budget: int, write_cost: float,
             write_cost_speakers: str = "one", writer_price=SCALEWAY_GPT_OSS, reader_price,
             judge_price, limit_questions=None) -> dict:
    """What a run would spend, from the data alone (see the assumptions at the
    top). A write costs `write_cost`, as measured on the memories of the
    `write_cost_speakers` mode (--calibrate measures it). When the run is in
    the other mode, the difference in the text the writer reads, the memory
    and the ten before it, is added (or taken off) at the writer's input
    price: a two-message memory is about twice as long as a turn alone."""
    turns = sum(len(c.memories(speakers)) for c in convs)
    chars, read = writer_text(convs, speakers)
    text_tokens = (read - writer_text(convs, write_cost_speakers)[1]) / 4
    per_write = write_cost + text_tokens * writer_price[0] / 1e6
    in_file = [q for c in convs for q in c.questions]
    asked = [q for c in convs for q in c.asked(limit_questions)]
    n = len(asked)
    avg_q = sum(len(q.question) for q in asked) / n if n else 0.0
    avg_gold = sum(len(q.gold) for q in asked) / n if n else 0.0
    sections = 2 if speakers == "each" else 1
    system = READER_SYSTEM.format(speaker_a="Caroline", speaker_b="Melanie")
    headings = 0
    if sections == 2:
        system += "\n" + READER_SECTIONS
        headings = sum(len(SECTION_HEADING.format(name=n)) + 2 for n in ("Caroline", "Melanie"))
    turn = READER_TURN.format(volatile="", today="2023-10-22 (Sunday)", question="")
    reader_in = int(CHAT_OVERHEAD_TOKENS + budget + (len(system) + headings + len(turn) + avg_q) / 4)
    judge_in = int(CHAT_OVERHEAD_TOKENS + (len(ACCURACY_PROMPT) + avg_q + avg_gold + ANSWER_CHARS) / 4)
    per_reader = (reader_in * reader_price[0] + READER_OUT_TOKENS * reader_price[1]) / 1e6
    per_judge = (judge_in * judge_price[0] + JUDGE_OUT_TOKENS * judge_price[1]) / 1e6
    eur = {"writes": turns * write_cost, "text": turns * text_tokens * writer_price[0] / 1e6,
           "reader": n * per_reader, "judge": n * per_judge}
    return {
        "speakers": speakers, "conversations": len(convs),
        "sessions": sum(len(c.sessions) for c in convs), "turns": turns,
        "memory_chars": chars, "writer_reads": read / 4, "text_tokens": text_tokens,
        "per_write": per_write, "questions_in_file": len(in_file), "asked": n,
        "skipped": sum(1 for q in in_file if q.category == SKIPPED),
        "by_category": dict(sorted(Counter(q.category for q in asked).items())),
        "budgets": split_budget(budget, sections), "context_calls": n * sections,
        "reader_in": reader_in, "judge_in": judge_in,
        "per_question": per_reader + per_judge, "eur": eur, "total": sum(eur.values()),
    }


def estimate_of(args, convs: list) -> dict:
    return estimate(convs, speakers=args.speakers, budget=args.budget, write_cost=args.write_cost,
                    write_cost_speakers=args.write_cost_speakers, writer_price=args.writer_price,
                    reader_price=args.reader_price, judge_price=args.judge_price,
                    limit_questions=args.limit_questions)


def print_estimate(e: dict, data: dict, args) -> None:
    note = ("the file of snap-research/locomo" if data["known"]
            else "NOT the file this harness was counted on")
    print(f"bench_locomo: {data['file']} (sha256 {data['sha256'][:12]}, {note}), "
          f"{e['conversations']} conversations, {e['sessions']} sessions, {e['turns']:,} turns",
          flush=True)
    cats = ", ".join(f"{CATEGORY_NAMES.get(c, c)} {k}" for c, k in e["by_category"].items())
    print(f"  questions  {e['questions_in_file']:,} in the file; {e['asked']:,} of {scope_of_asked()} "
          f"are asked ({cats})")
    if args.limit_questions:
        print(f"             at most {args.limit_questions} per conversation, evenly spaced "
              "through its list")
    print(f"             category 5 (adversarial, {e['skipped']:,} questions) is skipped, "
          "as Mem0, Zep and Hindsight do")
    if e["speakers"] == "each":
        print(f"  memories   one per turn, in its speaker's end user, after the turn it answers: "
              f"{e['memory_chars']:.0f} characters on average")
    else:
        print(f"  memories   one per turn, the whole conversation in one end user: "
              f"{e['memory_chars']:.0f} characters on average")
    print(f"             the writer's extraction reads ~{e['writer_reads']:,.0f} tokens of memories a "
          f"write (the memory and the {PREVIOUS_MEMORIES} before it)")
    rp, jp, wp = args.reader_price, args.judge_price, args.writer_price
    print(f"  writes     {e['turns']:6,} memories x {args.write_cost:.5f} EUR, the install's writer "
          f"{'.' * 14} {e['eur']['writes']:7.2f} EUR")
    if e["text_tokens"]:
        print(f"             {e['text_tokens']:+,.0f} tokens read a write over the --speakers "
              f"{args.write_cost_speakers} memories it was measured on, at {wp[0]:g} EUR/M "
              f"{'.' * 3} {e['eur']['text']:+7.2f} EUR")
    blocks = (f" ({' + '.join(f'{b:,}' for b in e['budgets'])} for the two people)"
              if len(e["budgets"]) > 1 else "")
    print(f"  reader     {e['asked']:6,} calls x ~{e['reader_in']:,} tokens in{blocks}, "
          f"~{READER_OUT_TOKENS} out at {rp[0]:g}/{rp[1]:g} EUR/M {'.' * 4} {e['eur']['reader']:7.2f} EUR")
    print(f"  judge      {e['asked']:6,} calls x ~{e['judge_in']:,} tokens in, ~{JUDGE_OUT_TOKENS} out "
          f"at {jp[0]:g}/{jp[1]:g} EUR/M {'.' * 6} {e['eur']['judge']:7.2f} EUR")
    print(f"  vectors    {e['turns'] + e['context_calls']:6,} from the install's embedder, a memory or "
          "a block each (not counted: free with the local one)")
    print(f"  TOTAL      about {e['total']:.2f} EUR (blocks at their full budget of {args.budget} "
          f"tokens; {e['per_question']:.5f} EUR a question)")
    if args.write_cost == WRITE_COST_EUR:
        print("  The write cost is the LongMemEval figure with gpt-oss (about 0.0019 EUR a memory). "
              "LoCoMo turns are much\n  shorter (about 124 characters), so the real one is probably "
              "lower: --calibrate 30 measures it for a few cents.")
    cap = cap_of(args)
    if cap is None:
        print("  No --eur-cap: nothing stops this run but you.")
    elif e["total"] > cap:
        print(f"  --eur-cap {cap:g} EUR is below this estimate: a run stops before the end "
              "(calibrate first, or raise the cap).")
    else:
        print(f"  --eur-cap {cap:g} EUR: a run stops before its estimated spend passes it.")


# ---------------------------------------------------------------- HTTP

def _where(url) -> str:
    """Where a request went, for a message: scheme, host and path, never the
    query (it holds the question) nor a user:password in the address."""
    p = urllib.parse.urlsplit(url or "")
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    return f"{p.scheme}://{host}{p.path}" if p.scheme else host


def _host(url) -> str:
    p = urllib.parse.urlsplit(url or "")
    return p.hostname or ""


def urllib_transport(method: str, url: str, headers: dict, body, timeout: float):
    """One HTTP exchange: the JSON answer, HttpError for a status, Unreachable
    for an answer that is not JSON. Network errors propagate as OSError."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the status is the message
            text = ""
        raise HttpError(e.code, text, e.headers.get("Retry-After") if e.headers else None) from None
    try:
        return json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except ValueError:
        raise Unreachable(f"{method} {_where(url)}: the answer is not JSON") from None


RETRY = frozenset({408, 425, 429, 500, 502, 503, 504})
# A refused key (401, 403) or credit gone (402): asking again changes nothing,
# and OpenRouter's 402 used to look like an empty answer in the harness.
REFUSED = frozenset({401, 402, 403})


def _backoff(retry_after, attempt: int) -> float:
    try:
        if retry_after is not None:
            return min(120.0, max(0.0, float(retry_after)))
    except ValueError:
        pass
    return min(60.0, 2.0 ** attempt)


def call(transport, method: str, url: str, headers: dict, body=None, *, timeout=API_TIMEOUT,
         retries: int = 5, retry_on=RETRY, retry_network: bool = True, sleep=time.sleep):
    """One request, asked again on a status in `retry_on` and on no answer.
    A refusal is Fatal, so is a rate limit that outlasts the retries."""
    for attempt in range(retries + 1):
        try:
            return transport(method, url, headers, body, timeout)
        except HttpError as e:
            if e.status in REFUSED:
                raise Fatal(f"{method} {_where(url)} refused: {e}") from None
            if e.status in retry_on and attempt < retries:
                sleep(_backoff(e.retry_after, attempt))
                continue
            if e.status == 429:
                raise Fatal(f"{method} {_where(url)} still rate-limited after {retries} "
                            f"retries: {e}") from None
            raise
        except (OSError, http.client.HTTPException) as e:
            if retry_network and attempt < retries:
                sleep(_backoff(None, attempt))
                continue
            raise Unreachable(f"{method} {_where(url)}: {type(e).__name__}: {e}") from None
    raise AssertionError("not reached")  # pragma: no cover


# ---------------------------------------------------------------- the models

# OpenAI's reasoning models, as korely_agent/core/llm.py names them.
OPENAI_REASONING = ("gpt-5", "gpt-6", "o1", "o3", "o4")


# "gpt-oss" at the start of a word of a model name (no letter or digit of
# any script right before it), as korely_agent/core/llm.py `_GPT_OSS_FAMILY`
# reads it.
GPT_OSS_FAMILY = re.compile(r"(?<![^\W_])gpt-oss")


def gpt_oss(model: str) -> bool:
    """Whether `model` is of the gpt-oss family, read as korely_agent/core/llm.py
    `gpt_oss_family` reads it (a test holds the two together): "gpt-oss" starting a
    word anywhere in the name normalized as `normalized_model_name` does (NFKC,
    the case folded, NFKC again), whatever follows it."""
    import unicodedata

    name = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", model or "").casefold())
    return GPT_OSS_FAMILY.search(name) is not None


def served_by(base_url: str, host: str) -> bool:
    """Whether `base_url` is `host`'s, read as korely_agent/core/llm.py
    `_served_by` reads it (a test holds the two together): the URL's host
    name, without letter case, equal to `host`; never a substring."""
    try:
        return (urllib.parse.urlsplit(base_url or "").hostname or "").rstrip(".") == host
    except ValueError:
        return False


def least_reasoning(base_url: str, payload: dict) -> dict:
    """The payload with the fields that hold a reasoning model to its least
    thinking, exactly as the write path sends them (korely_agent/core/llm.py,
    `_least_reasoning` then `_openai_reasoning_fields`; a test holds the two
    together). gpt-oss cannot switch thinking off, "low" is its floor, sent
    to every gpt-oss (`gpt_oss`):
    OpenRouter takes its own unified field, the vLLM hosts (Scaleway,
    OVHcloud) OpenAI's. OpenAI's reasoning models refuse `max_tokens` and any
    temperature but their own. Every other model: unchanged."""
    out = dict(payload)
    model = str(out.get("model") or "")
    if gpt_oss(model):
        if served_by(base_url, "openrouter.ai"):
            out["reasoning"] = {"effort": "low"}
        else:
            out["reasoning_effort"] = "low"
    if served_by(base_url, "api.openai.com") and model.startswith(OPENAI_REASONING):
        out["max_completion_tokens"] = out.pop("max_tokens", None)
        out.pop("temperature", None)
        out["reasoning_effort"] = "low"
    return out


def reasoning_note(base_url: str, model: str) -> str:
    """The reasoning fields a model is sent, for the conditions."""
    sent = least_reasoning(base_url, {"model": model, "max_tokens": 1, "temperature": 0.0})
    extra = {k: v for k, v in sent.items() if k in ("reasoning", "reasoning_effort")}
    if "max_completion_tokens" in sent:
        extra["max_tokens_as"] = "max_completion_tokens"
    return json.dumps(extra) if extra else "no reasoning field"


@dataclass
class Reply:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""
    model: str = ""


@dataclass
class Endpoint:
    """An OpenAI-compatible chat endpoint: the reader, or the judge."""
    base_url: str
    model: str
    key: str = field(repr=False)
    transport: object = field(default=urllib_transport, repr=False)
    sleep: object = field(default=time.sleep, repr=False)

    def ask(self, messages: list, *, max_tokens: int, json_mode: bool = False) -> Reply:
        """One completion at temperature 0. A provider that refuses JSON mode
        (400) is asked once more without it: the judge is read leniently."""
        payload = least_reasoning(self.base_url, {"model": self.model, "messages": messages,
                                                  "max_tokens": max_tokens, "temperature": 0.0})
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        try:
            body = call(self.transport, "POST", url, headers, payload, timeout=CHAT_TIMEOUT,
                        sleep=self.sleep)
        except HttpError as e:
            if not (json_mode and e.status == 400):
                raise
            payload.pop("response_format")
            body = call(self.transport, "POST", url, headers, payload, timeout=CHAT_TIMEOUT,
                        sleep=self.sleep)
        choice = (body.get("choices") or [{}])[0] or {}
        usage = body.get("usage") or {}
        return Reply(text=str((choice.get("message") or {}).get("content") or "").strip(),
                     prompt_tokens=int(usage.get("prompt_tokens") or 0),
                     completion_tokens=int(usage.get("completion_tokens") or 0),
                     finish_reason=str(choice.get("finish_reason") or ""),
                     model=str(body.get("model") or self.model))


def reader_messages(conv: Conversation, sections: list, question: str) -> list:
    """The reader's two messages. `sections` is [(heading or None, block)]: one
    block and no heading (--speakers one), or one block per person under
    their name (--speakers each). `stable` goes in the system prompt, once
    when the blocks share it (the reader note, the same text for everybody),
    under the headings when they differ (with the standing instructions on,
    each person's rules); `volatile` goes in the turn, before the date and
    the question."""
    system = READER_SYSTEM.format(speaker_a=conv.speaker_a, speaker_b=conv.speaker_b)
    headed = any(h for h, _ in sections)
    if headed:
        system += "\n" + READER_SECTIONS
    parts = []
    for heading, block in sections:
        stable, volatile = block.get("stable") or "", block.get("volatile") or ""
        if not stable and not volatile:
            volatile = block.get("context") or ""   # an install before 0.1.16: one block
        parts.append((heading, stable, volatile))
    stables = [s for _, s, _ in parts if s]
    if stables and (not headed or all(s == stables[0] for s in stables)):
        system += "\n\n" + stables[0]
    elif stables:
        system += "\n\n" + "\n\n".join(f"{SECTION_HEADING.format(name=h)}\n{s}"
                                       for h, s, _ in parts if s)
    if headed:
        memory = "\n\n".join(f"{SECTION_HEADING.format(name=h)}\n{v or EMPTY_SECTION}"
                             for h, _, v in parts)
    else:
        memory = parts[0][2] if parts else ""
    turn = READER_TURN.format(volatile=memory, today=current_date(conv), question=question)
    return [{"role": "system", "content": system}, {"role": "user", "content": turn.lstrip("\n")}]


def reader_prompt_text(speakers: str) -> str:
    """Every fixed text the reader is given in a --speakers mode: what the
    conditions fingerprint."""
    parts = [READER_SYSTEM]
    if speakers == "each":
        parts += [READER_SECTIONS, SECTION_HEADING, EMPTY_SECTION]
    return "\n".join(parts + [READER_TURN])


def judge_prompt(q: Question, answer: str) -> str:
    return ACCURACY_PROMPT.format(question=q.question, gold_answer=q.gold, generated_answer=answer)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def judge_label(text) -> str | None:
    """CORRECT or WRONG from the judge's answer, None when it cannot be read.

    Mem0 reads json.loads(extract_json(answer))["label"], extract_json taking
    the inside of a ```json fence when there is one; so does this, and also a
    JSON object inside prose, a label in another case, and, failing JSON, the
    one label word the prompt tells the judge to finish with. Both words and
    no JSON is unreadable: the prompt itself says that breaks the script."""
    t = str(text or "").strip()
    candidates = []
    fence = _FENCE.search(t)
    if fence:
        candidates.append(fence.group(1))
    candidates.append(t)
    if "{" in t and "}" in t:
        candidates.append(t[t.find("{"):t.rfind("}") + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        label = obj.get("label") if isinstance(obj, dict) else None
        if isinstance(label, str) and label.strip().upper() in ("CORRECT", "WRONG"):
            return label.strip().upper()
    words = set(re.findall(r"\b(CORRECT|WRONG)\b", t))
    return words.pop() if len(words) == 1 else None


# ---------------------------------------------------------------- the install

class Korely:
    """The install's HTTP API, with its keys: /v1 with `key`, /admin with
    `admin_key` (a key issued with --admin, or the same key if it has the
    scope)."""

    def __init__(self, url: str, key: str, *, admin_key=None, transport=urllib_transport,
                 sleep=time.sleep):
        self.url = url.rstrip("/")
        self._key = key
        self._admin_key = admin_key or key
        self.transport = transport
        self.sleep = sleep

    def _call(self, method, path, params=None, body=None, *, admin=False, **kw):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        headers = {"Authorization": f"Bearer {self._admin_key if admin else self._key}",
                   "Content-Type": "application/json"}
        return call(self.transport, method, self.url + path + query, headers, body,
                    sleep=self.sleep, **kw)

    def health(self) -> dict:
        return call(self.transport, "GET", self.url + "/health", {}, None, sleep=self.sleep)

    def add(self, content: str, user_id: str, timestamp: str, metadata: dict, *,
            agent_id=None) -> dict:
        body = {"content": content, "user_id": user_id, "timestamp": timestamp,
                "metadata": metadata}
        if agent_id:
            body["agent_id"] = agent_id
        # Asked again only on a 429, refused before anything was stored. No
        # answer or a 5xx may come after the memory was stored: write_turn
        # looks before writing it again.
        return self._call("POST", "/v1/memories", body=body, retries=6,
                          retry_on=frozenset({429}), retry_network=False)

    def memories(self, user_id: str) -> list:
        """Every memory of an end user, oldest first (the API pages newest first)."""
        out, offset = [], 0
        while True:
            page = self._call("GET", "/v1/memories", {"user_id": user_id, "limit": PAGE,
                                                      "offset": offset})
            rows = page.get("memories") or []
            out.extend(rows)
            offset += len(rows)
            if not rows or offset >= int(page.get("total") or 0):
                return list(reversed(out))

    def processing(self, user_id=None) -> int:
        params = {"limit": 1}
        if user_id:
            params["user_id"] = user_id
        return int(self._call("GET", "/v1/events", params).get("processing") or 0)

    def errors(self, user_id: str) -> int:
        page = self._call("GET", "/v1/events", {"user_id": user_id, "status": "error",
                                                "limit": PAGE})
        return len(page.get("events") or [])

    def context(self, query: str, user_id: str, budget: int) -> dict:
        return self._call("GET", "/v1/context", {"query": query, "user_id": user_id,
                                                 "token_budget": budget})

    def usage(self, days: int = 1) -> dict:
        """The tenant's model calls of the last `days`, from `agent_llm_usage`
        (GET /admin/usage): tokens by stage, by model and by agent."""
        return self._call("GET", "/admin/usage", {"days": days}, admin=True)

    def models(self) -> dict:
        return self._call("GET", "/admin/models", admin=True)


def writer_problem(health: dict):
    """Why this install cannot give the product's number, or None."""
    ext = health.get("extraction") or {}
    if ext.get("state") == "not configured":
        return ("the install has no writer (GET /health: extraction not configured): memories "
                "would be stored without facts, which is not the product's number")
    if ext.get("state") == "failing":
        return f"the install's writer is failing (GET /health): {ext.get('detail') or ''}".rstrip()
    return None


def embedder_problem(health: dict):
    """Why this install's vectors would not be the product's, or None. An
    embedder that fails stores every memory without a vector, so the search
    finds none of them, and leaves each question without its own: the run
    measures a broken install. LoCoMo trial3, 2026-10-06: the local model's
    folder had been emptied, /health said "failing", nothing looked at it,
    and the writes ran for twenty minutes before a block came back degraded."""
    emb = health.get("embeddings") or {}
    if emb.get("state") == "failing":
        detail = emb.get("detail") or ""
        return ("the install's embedder is failing (GET /health)"
                + (f": {detail}" if detail else ""))
    return None


def turns_left(korely: Korely, convs: list, args, records: dict) -> int:
    """How many turns this run would still write, for the conversations that
    still have a question to ask: none for a reread of conversations already
    stored."""
    n = 0
    for conv in convs:
        if all(is_done(records.get(q.key)) for q in conv.asked(args.limit_questions)):
            continue
        users = conv.users(base_user(args.user_prefix, conv), args.speakers)
        mine = by_user(users, conv.memories(args.speakers))
        for user, _speaker in users:
            n += len(turns_to_write(korely, user, conv, mine[user])[0])
    return n


def describe_install(korely: Korely, health: dict) -> dict:
    """What the install says about itself: version, writer, embedder. The
    writer's name needs the admin scope (GET /admin/models); never its key,
    not even the last four characters the screen shows."""
    out = {"url": _where(korely.url), "version": health.get("version"),
           "provider": health.get("provider"),
           "extraction": (health.get("extraction") or {}).get("state")}
    try:
        models = korely.models()
    except (Fatal, HttpError, Unreachable):
        return out
    active = models.get("active") or {}
    if active:
        out["writer"] = " ".join(str(x) for x in (active.get("provider"), active.get("model")) if x)
    out["writer_source"] = models.get("source")
    emb = models.get("embeddings") or {}
    if emb:
        out["embeddings"] = " ".join(str(x) for x in (emb.get("provider"), emb.get("model")) if x)
    return out


def write_turn(korely: Korely, user: str, conv: Conversation, mem: Memory, *, agent_id=None,
               sleep=time.sleep, attempts: int = 4):
    doubt = None
    for attempt in range(attempts):
        try:
            return korely.add(mem.content, user, mem.timestamp, metadata_of(conv, mem),
                              agent_id=agent_id)
        except HttpError as e:
            if e.status < 500:
                raise Fatal(f"the install refused turn {mem.dia_id} of {conv.sample_id}: "
                            f"{e}") from None
            doubt = e
        except Unreachable as e:
            doubt = e
        # No answer, or a 5xx: the memory may be stored all the same. Look
        # before writing it again, or the store would hold the turn twice.
        sleep(_backoff(None, attempt))
        if any((m.get("metadata") or {}).get("dia_id") == mem.dia_id
               for m in korely.memories(user)):
            return None
    raise Fatal(f"turn {mem.dia_id} of {conv.sample_id} could not be written: {doubt}")


def turns_to_write(korely: Korely, user: str, conv: Conversation, mems: list):
    """(the turns of `mems`, the end user's in order, still to write; how many
    are stored twice). What the end user already holds must be the first of
    them, in order: anything else is refused rather than mixed into the
    number."""
    stored = korely.memories(user)
    seen = [((m.get("metadata") or {}).get("dia_id"), (m.get("metadata") or {}).get("sample_id"))
            for m in stored]
    foreign = [d for d, sample in seen if not d or sample != conv.sample_id]
    if foreign:
        raise Fatal(f"end user {user} holds {len(foreign)} memories that are not turns of "
                    f"{conv.sample_id}: pick another --user-prefix")
    present = {d for d, _ in seen}
    k = len(present)
    if {m.dia_id for m in mems[:k]} != present:
        raise Fatal(f"end user {user} holds {k} turns of {conv.sample_id} that are not its first "
                    f"{k}: erase it (DELETE /v1/users/{user}/memories) or pick another --user-prefix")
    return mems[k:], len(seen) - k


def wait_digested(korely: Korely, user: str, *, poll: float, deadline: float, sleep=time.sleep,
                  clock=time.monotonic, every: float = 300.0) -> None:
    """Until the writer has digested every memory of `user` (its `processing`
    count in GET /v1/events is 0). Digestion is serial per end user and four
    users go at once, so a whole conversation can take hours on a slow writer:
    an install that stops answering for a while is waited for too, and the
    count is printed every `every` seconds, not at every poll."""
    start, said = clock(), None
    while True:
        try:
            left = korely.processing(user)
        except (HttpError, Unreachable) as e:
            left = None
            print(f"  {user}: the install did not answer ({e}); waiting", flush=True)
        if left == 0:
            return
        if clock() - start > deadline:
            raise Fatal(f"{user}: {left if left is not None else 'some'} memories still being "
                        f"digested after {deadline / 3600:.1f} h (--wait-hours)")
        if left is not None and (said is None or clock() - said >= every):
            print(f"  {user}: {left} {'memory' if left == 1 else 'memories'} still being digested",
                  flush=True)
            said = clock()
        sleep(poll)


# ---------------------------------------------------------------- the spend

class Spend:
    """What this invocation has spent, and the cap it must stay under: the
    writes at --write-cost each, the reader and the judge at the tokens their
    providers report, priced at --reader-price and --judge-price."""

    def __init__(self, *, write_cost: float, reader_price, judge_price, cap=None):
        self.write_cost = write_cost
        self.price = {"reader": tuple(reader_price), "judge": tuple(judge_price)}
        self.cap = cap
        self.writes = 0
        self.calls = {"reader": [0, 0, 0], "judge": [0, 0, 0]}
        self._lock = threading.Lock()

    def wrote(self, n: int = 1) -> None:
        with self._lock:
            self.writes += n

    def asked(self, who: str, reply: Reply) -> None:
        with self._lock:
            c = self.calls[who]
            c[0] += 1
            c[1] += reply.prompt_tokens
            c[2] += reply.completion_tokens

    def eur(self, who: str) -> float:
        _n, tin, tout = self.calls[who]
        p = self.price[who]
        return (tin * p[0] + tout * p[1]) / 1e6

    def total(self) -> float:
        return self.writes * self.write_cost + self.eur("reader") + self.eur("judge")

    def room(self, promised: float = 0.0) -> float:
        """EUR left under the cap once `promised` is set aside; inf without one."""
        return math.inf if self.cap is None else self.cap - self.total() - promised


def cap_of(args):
    return args.eur_cap if args.eur_cap and args.eur_cap > 0 else None


def reply_eur(reply: Reply, price) -> float:
    return (reply.prompt_tokens * price[0] + reply.completion_tokens * price[1]) / 1e6


# ---------------------------------------------------------------- the records

def conditions(args, env, data_sha: str) -> dict:
    """What a number depends on. Every JSONL line carries it, and a resume
    under other conditions is refused."""
    return {
        "data_sha256": data_sha,
        "speakers": args.speakers,
        "user_prefix": args.user_prefix,
        "token_budget": args.budget,
        "token_budgets": split_budget(args.budget, 2 if args.speakers == "each" else 1),
        "reader_model": env.get("READER_MODEL"),
        "reader_host": _host(env.get("READER_BASE_URL")),
        "reader_max_tokens": args.reader_max_tokens,
        "reader_prompt_sha256": text_sha256(reader_prompt_text(args.speakers)),
        "judge_model": env.get("JUDGE_MODEL"),
        "judge_host": _host(env.get("JUDGE_BASE_URL")),
        "judge_max_tokens": args.judge_max_tokens,
        "judge_prompt_sha256": text_sha256(ACCURACY_PROMPT),
    }


def read_records(path) -> tuple:
    """({key: the latest line for that question}, unreadable lines). The file
    is append-only, so a question asked again has its last line win; a line
    cut short by a crash is skipped and counted."""
    latest, bad = {}, 0
    p = Path(path)
    if not p.exists():
        return latest, bad
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(rec, dict) and rec.get("key"):
                latest[rec["key"]] = rec
            else:
                bad += 1
    return latest, bad


def is_done(rec) -> bool:
    return bool(rec) and rec.get("label") in ("CORRECT", "WRONG")


def mismatches(records: dict, conds: dict) -> list:
    """The conditions on which the file's lines disagree with this run."""
    out = set()
    for rec in records.values():
        theirs = rec.get("conditions") or {}
        for k, v in conds.items():
            if theirs.get(k) != v:
                out.add(f"{k}: the file has {theirs.get(k)!r}, this run {v!r}")
    return sorted(out)


def append_record(path, rec: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """The 95% Wilson interval of k right out of n."""
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarize(records: dict, scope: list) -> dict:
    """Accuracy per category and overall over `scope`, the (key, category) of
    every question this run is about. A question with no verdict yet counts
    as not right: the number never leaves out what it could not judge."""
    cats = {c: {"questions": 0, "judged": 0, "correct": 0} for c in ASKED}
    for key, cat in scope:
        slot = cats.setdefault(cat, {"questions": 0, "judged": 0, "correct": 0})
        slot["questions"] += 1
        rec = records.get(key)
        if is_done(rec):
            slot["judged"] += 1
            slot["correct"] += int(rec["label"] == "CORRECT")
    for slot in cats.values():
        slot["accuracy"] = slot["correct"] / slot["questions"] if slot["questions"] else None
    n = sum(s["questions"] for s in cats.values())
    k = sum(s["correct"] for s in cats.values())
    judged = sum(s["judged"] for s in cats.values())
    shown = [s["accuracy"] for s in cats.values() if s["accuracy"] is not None]
    return {"categories": cats, "questions": n, "judged": judged, "correct": k,
            "accuracy": k / n if n else None, "ci95": wilson(k, n),
            "category_mean": sum(shown) / len(shown) if shown else None,
            "without_verdict": n - judged}


def mcnemar_exact(b: int, c: int) -> float:
    """McNemar's exact two-sided p: a binomial test on the discordant pairs,
    `b` one way and `c` the other."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def carried_stats(records: dict, carry: dict, scope: list) -> dict:
    """What --carry-unchanged did (2026-10-06): verdicts carried from the
    earlier run, questions read again, and among those, the ones whose
    verdict changed against the earlier run's.

    Why the questions with changed blocks, right and wrong alike, and not the
    wrong ones alone: the reader is not perfectly repeatable (on conversation
    26 about one answer in eight changed between near-identical blocks), so
    asking the wrong ones again turns some into right by chance and shows a
    gain that is not there, and it never sees the right ones a change
    breaks. A question whose blocks did not change is the same question on
    the same input: its verdict stands."""
    carried = reread = wrong_then_right = right_then_wrong = 0
    for key, _category in scope:
        rec, old = records.get(key), carry.get(key)
        if not is_done(rec):
            continue
        if rec.get("carried_from"):
            carried += 1
            continue
        reread += 1
        if is_done(old):
            was, now = old["label"] == "CORRECT", rec["label"] == "CORRECT"
            wrong_then_right += int(now and not was)
            right_then_wrong += int(was and not now)
    return {"carried": carried, "reread": reread, "wrong_then_right": wrong_then_right,
            "right_then_wrong": right_then_wrong,
            "mcnemar_p": mcnemar_exact(wrong_then_right, right_then_wrong)}


# ---------------------------------------------------------------- answering

def fetch_block(korely: Korely, query: str, user: str, budget: int, sleep=time.sleep) -> dict:
    """GET /v1/context, asked twice more when it comes back degraded."""
    block = korely.context(query, user, budget)
    for tries in (1, 2):
        if not block.get("degraded"):
            break
        sleep(5 * tries)
        block = korely.context(query, user, budget)
    return block


def answer_one(q: Question, conv: Conversation, users: list, *, korely: Korely, reader: Endpoint,
               judge: Endpoint, args, spend: Spend, conds: dict, whole: dict,
               carry: dict | None = None, sleep=time.sleep) -> dict:
    """One question: a block from each end user of the conversation (with
    --speakers each, two, the budget split between them), the reader, the
    judge, as one JSONL line. Anything but a Fatal is written down as the
    line's `error`, and the question is asked again by the next run.

    With `carry` (--carry-unchanged), a question whose blocks are byte for
    byte the ones the earlier run read keeps that run's answer and verdict:
    the same question on the same blocks, so nothing is asked or paid."""
    rec = {"key": q.key, "sample_id": conv.sample_id, "conversation": conv.index,
           "user_ids": [u for u, _ in users], "q_index": q.index, "category": q.category,
           "category_name": CATEGORY_NAMES.get(q.category), "question": q.question,
           "gold": q.gold, "evidence": q.evidence, "current_date": current_date(conv)}
    cost = 0.0
    try:
        sections, shown = [], []
        for (user, speaker), budget in zip(users, split_budget(args.budget, len(users))):
            block = fetch_block(korely, q.question, user, budget, sleep)
            sections.append((speaker, block))
            shown.append({"user_id": user, "heading": speaker, "token_budget": budget,
                          "tokens": block.get("tokens"),
                          "sources": len(block.get("sources") or []),
                          "degraded": bool(block.get("degraded")),
                          "degraded_parts": block.get("degraded_parts") or [],
                          "text": block.get("context") or ""})
        rec["context"] = shown
        # The evidence turns shown whole in a block (the first memories of
        # the hybrid block are): whether a loss is reading or retrieval. A
        # turn shown as its dated observation is not counted. Every id of
        # every entry: "D8:6; D9:17" is two turns (dia_ids).
        text = "\n\n".join(s["text"] for s in shown)
        ids, _unread = dia_ids(q.evidence)
        rec["evidence_whole"] = [d for d in ids if whole.get(d) and whole[d][:80] in text]
        degraded = [s for s in shown if s["degraded"]]
        if degraded:
            rec["error"] = "the context came back degraded: " + "; ".join(
                f"{s['user_id']} {', '.join(s['degraded_parts'] or ['?'])}" for s in degraded)
            return rec
        old = (carry or {}).get(q.key)
        if is_done(old) and [b.get("text") for b in old.get("context") or []] == \
                [s["text"] for s in shown]:
            for k in ("answer", "reader", "judge_raw", "judge", "label", "correct"):
                if k in old:
                    rec[k] = old[k]
            rec["carried_from"] = args.carry_unchanged.name
            return rec
        r = reader.ask(reader_messages(conv, sections, q.question),
                       max_tokens=args.reader_max_tokens)
        spend.asked("reader", r)
        cost += reply_eur(r, spend.price["reader"])
        rec["answer"] = r.text
        rec["reader"] = {"model": r.model, "prompt_tokens": r.prompt_tokens,
                         "completion_tokens": r.completion_tokens, "finish_reason": r.finish_reason}
        if not r.text:
            rec["error"] = f"empty answer (finish_reason {r.finish_reason or 'none'})"
            return rec
        j = judge.ask([{"role": "user", "content": judge_prompt(q, r.text)}],
                      max_tokens=args.judge_max_tokens, json_mode=True)
        spend.asked("judge", j)
        cost += reply_eur(j, spend.price["judge"])
        rec["judge_raw"] = j.text[:2000]
        rec["judge"] = {"model": j.model, "prompt_tokens": j.prompt_tokens,
                        "completion_tokens": j.completion_tokens, "finish_reason": j.finish_reason}
        label = judge_label(j.text)
        rec["label"] = label
        if label is None:
            rec["error"] = "the judge's answer has no readable label"
        else:
            rec["correct"] = label == "CORRECT"
        return rec
    except Fatal:
        raise
    except Exception as e:  # noqa: BLE001 - one odd answer must not lose the run's lines
        # HttpError and Unreachable mostly; an answer of an unexpected shape
        # too. Written down and asked again next time; ERRORS_IN_A_ROW of
        # them in a row stop the run.
        rec["error"] = redact(f"{type(e).__name__}: {e}")
        return rec
    finally:
        rec["eur"] = round(cost, 8)
        rec["conditions"] = conds
        rec["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def blocks_only(args, convs: list, env, *, transport=urllib_transport, sleep=time.sleep) -> int:
    """--blocks: the blocks of every asked question, from conversations already
    stored, one JSONL line each with no answer; with --compare-blocks, how many
    are the same as an earlier run's. Nothing is written and no model is
    asked, so it costs nothing, and it says before a paid reread what the
    reread would measure. The night of 2026-10-06 this was done by hand: the
    blocks of trial2 rebuilt with that day's code came back identical byte for
    byte, so the reread with the relative dates measured the dates and nothing
    else."""
    korely = Korely(env["KORELY_API_URL"], env["KORELY_API_KEY"], transport=transport, sleep=sleep)
    try:
        health = korely.health()
        broken = embedder_problem(health)
        left = 0 if broken else turns_left(korely, convs, args, {})
    except (Fatal, HttpError, Unreachable) as e:
        print(f"\nnot fetching: {e}", file=sys.stderr)
        return 2
    if broken:
        print(f"\nnot fetching: {broken}", file=sys.stderr)
        return 2
    if left:
        print(f"\nnot fetching: {left} turns are not stored, and the blocks of a conversation "
              "half written are not the product's (--run writes it)", file=sys.stderr)
        return 2
    earlier = read_records(args.compare_blocks)[0] if args.compare_blocks else {}
    same, differ, unmatched, degraded, n = 0, [], 0, 0, 0
    for conv in convs:
        users = conv.users(base_user(args.user_prefix, conv), args.speakers)
        for q in conv.asked(args.limit_questions):
            shown = []
            for (user, speaker), budget in zip(users, split_budget(args.budget, len(users))):
                block = fetch_block(korely, q.question, user, budget, sleep)
                shown.append({"user_id": user, "heading": speaker, "token_budget": budget,
                              "tokens": block.get("tokens"),
                              "sources": len(block.get("sources") or []),
                              "degraded": bool(block.get("degraded")),
                              "degraded_parts": block.get("degraded_parts") or [],
                              "text": block.get("context") or ""})
            n += 1
            degraded += any(b["degraded"] for b in shown)
            append_record(args.out, {
                "key": q.key, "sample_id": conv.sample_id, "conversation": conv.index,
                "user_ids": [u for u, _ in users], "q_index": q.index, "category": q.category,
                "category_name": CATEGORY_NAMES.get(q.category), "question": q.question,
                "gold": q.gold, "evidence": q.evidence, "current_date": current_date(conv),
                "context": shown, "blocks_only": True,
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
            if args.compare_blocks:
                old = (earlier.get(q.key) or {}).get("context")
                if not old:
                    unmatched += 1
                elif [b.get("text") for b in old] == [b["text"] for b in shown]:
                    same += 1
                else:
                    differ.append(q.key)
    print(f"\nblocks of {n} questions in {args.out}; no model was asked")
    if degraded:
        print(f"  {degraded} came back degraded: their blocks are not the product's")
    if args.compare_blocks:
        print(f"  compared with {args.compare_blocks}: {same} the same, {len(differ)} different"
              + (f", {unmatched} not in it" if unmatched else ""))
        if differ:
            print("  different: " + ", ".join(differ[:20]) + (" ..." if len(differ) > 20 else ""))
    return 3 if degraded else 0


def answer_conversation(conv: Conversation, users: list, pending: list, *, korely: Korely,
                        reader: Endpoint, judge: Endpoint, args, spend: Spend, conds: dict,
                        per_question: float, promised: float, carry: dict | None = None,
                        sleep=time.sleep):
    """The pending questions of one digested conversation, --workers at a time,
    each group only when its estimate fits under the cap. Returns why it
    stopped early, or None. A Fatal propagates once the group's lines are
    written."""
    # Keyed the way the evidence is read (dia_ids), so that "D30:05" in a
    # question finds turn D30:5.
    whole = {}
    for m in conv.memories(args.speakers):
        ids, _unread = dia_ids([m.dia_id])
        whole[ids[0] if ids else m.dia_id] = m.content
    in_a_row, i = 0, 0
    while i < len(pending):
        room = spend.room(promised)
        if room == math.inf or per_question <= 0:
            fit = len(pending) - i if room >= 0 else 0
        else:
            fit = int((room + 1e-9) // per_question)
        if fit <= 0:
            return (f"--eur-cap {spend.cap:g} EUR: {spend.total():.3f} EUR spent and the next "
                    f"question would pass it")
        group = pending[i:i + min(args.workers, fit)]
        fatal = None
        with ThreadPoolExecutor(max_workers=len(group)) as pool:
            futures = [pool.submit(answer_one, q, conv, users, korely=korely, reader=reader,
                                   judge=judge, args=args, spend=spend, conds=conds, whole=whole,
                                   carry=carry, sleep=sleep) for q in group]
            for q, f in zip(group, futures):
                try:
                    rec = f.result()
                except Fatal as e:
                    fatal = fatal or e
                    continue
                append_record(args.out, rec)
                in_a_row = in_a_row + 1 if rec.get("error") else 0
                print(f"  {q.key:10s} {CATEGORY_NAMES.get(q.category, q.category):11s} "
                      f"{rec.get('label') or 'NO VERDICT'}"
                      + ("  (blocks unchanged: carried)" if rec.get("carried_from") else "")
                      + (f"  ({rec['error'][:120]})" if rec.get("error") else ""), flush=True)
        if fatal is not None:
            raise fatal
        if in_a_row >= ERRORS_IN_A_ROW:
            raise Fatal(f"{in_a_row} questions in a row got no verdict; the last: "
                        f"{rec.get('error')}")
        i += len(group)
    return None


# ---------------------------------------------------------------- reread, full context

# Two runs that ask no install: --reread reads again the blocks an earlier
# --run (or --blocks) saved, with another reader and judge; --full-context
# gives the reader the whole conversation instead of the memory, the "full
# context" baseline of the Mem0 paper (Chhikara et al., arXiv:2504.19413,
# Table 2). docs/benchmarks.md publishes one number of each, gpt-4o-mini as
# reader and judge: 86.9% (the blocks of the 0.1.36 run read again) and 79.9%.
# Both use the reader prompt and the judge of --run, with the fixed
# differences below, which are those of the runs that produced the numbers:
#
# - a saved block holds the reader note and the memory together (`text`), so
#   the whole block goes in the user turn under its person's heading, where
#   --run puts the note in the system prompt;
# - the whole conversation is every session that has turns, in order, each
#   under FULL_CONTEXT_SESSION with its date_time as the file writes it, then
#   one line per turn as turn_text renders it, sessions a blank line apart;
#   it goes in the turn, with no heading, in place of the block;
# - the reader may write 1,024 tokens and the judge 300 (--reader-max-tokens
#   and --judge-max-tokens change them; a non-reasoning model's short answer
#   never reaches either).
FULL_CONTEXT_SESSION = "## Session on {date_time}\n"
REREAD_READER_MAX_TOKENS = 1024
REREAD_JUDGE_MAX_TOKENS = 300
READ_ONLY_MODES = ("reread", "full-context")
NEEDS["reread"] = NEEDS["full-context"] = NEEDS["run"][2:]   # the reader and the judge alone


def read_only_mode(args):
    """"reread", "full-context", or None for every other mode."""
    if getattr(args, "reread", None) is not None:
        return "reread"
    return "full-context" if getattr(args, "full_context", False) else None


def max_tokens_defaults(args) -> None:
    """The reader's and the judge's max_tokens when not given: those of the
    mode (a reread and the full context have their own, above)."""
    quiet = read_only_mode(args) is not None
    if args.reader_max_tokens is None:
        args.reader_max_tokens = REREAD_READER_MAX_TOKENS if quiet else READER_MAX_TOKENS
    if args.judge_max_tokens is None:
        args.judge_max_tokens = REREAD_JUDGE_MAX_TOKENS if quiet else JUDGE_MAX_TOKENS


def read_only_problem(args):
    """What is wrong with --reread or --full-context on this command line."""
    mode = read_only_mode(args)
    if mode is None:
        return None
    if args.reread is not None and args.full_context:
        return "--reread and --full-context: one or the other"
    if args.calibrate or args.blocks or args.carry_unchanged is not None:
        return f"--{mode} goes with --run (or alone, for the estimate)"
    if args.reread is not None and not args.reread.is_file():
        return f"--reread: no file at {args.reread}"
    return None


def whole_conversation(conv: Conversation) -> str:
    """The text the full-context reader gets in place of the memory: every
    session with turns, under its date, one turn_text line per turn."""
    return "\n\n".join(FULL_CONTEXT_SESSION.format(date_time=s.date_time)
                       + "\n".join(turn_text(t) for t in s.turns) for s in conv.sessions)


def rereadable(rec) -> bool:
    """A saved line whose blocks can be read again: one the earlier run
    judged (a degraded block is never judged), or one --blocks wrote with no
    block degraded."""
    blocks = (rec or {}).get("context") or []
    if not blocks or any(b.get("degraded") for b in blocks):
        return False
    return is_done(rec) or bool(rec.get("blocks_only"))


def saved_sections(rec: dict) -> list:
    """reader_messages' sections from a saved line: each block whole (note and
    memory, `text`) under its heading, the way the 86.9% reread read them."""
    return [(b.get("heading"), {"context": b.get("text") or ""}) for b in rec["context"]]


def read_only_items(args, convs: list, source) -> tuple:
    """([(conv, question, sections, saved line or None)], keys of the asked
    questions RESULTS has no rereadable line for). The questions are those
    --run would ask (categories 1-4, or --category, and the limits)."""
    items, missing = [], []
    for conv in convs:
        whole = None if source is not None else [(None, {"context": whole_conversation(conv)})]
        for q in conv.asked(args.limit_questions):
            if source is None:
                items.append((conv, q, whole, None))
            elif rereadable(source.get(q.key)):
                items.append((conv, q, saved_sections(source[q.key]), source[q.key]))
            else:
                missing.append(q.key)
    return items, missing


def read_only_prompt_text(mode: str) -> str:
    """Every fixed text the reader is given in a reread or the full context:
    what the conditions fingerprint."""
    parts = [READER_SYSTEM]
    if mode == "reread":
        parts += [READER_SECTIONS, SECTION_HEADING, EMPTY_SECTION]
    else:
        parts += [FULL_CONTEXT_SESSION]
    return "\n".join(parts + [READER_TURN])


def read_only_conditions(args, env, mode: str, data_sha: str) -> dict:
    """What a reread's or a full-context number depends on: every line carries
    it, and a resume under other conditions is refused."""
    conds = {"mode": mode, "data_sha256": data_sha}
    if mode == "reread":
        conds["source"] = args.reread.name
        conds["source_sha256"] = hashlib.sha256(args.reread.read_bytes()).hexdigest()
    conds.update({
        "reader_model": env.get("READER_MODEL"), "reader_host": _host(env.get("READER_BASE_URL")),
        "reader_max_tokens": args.reader_max_tokens,
        "reader_prompt_sha256": text_sha256(read_only_prompt_text(mode)),
        "judge_model": env.get("JUDGE_MODEL"), "judge_host": _host(env.get("JUDGE_BASE_URL")),
        "judge_max_tokens": args.judge_max_tokens,
        "judge_prompt_sha256": text_sha256(ACCURACY_PROMPT)})
    return conds


def read_only_estimate(items: list, args) -> list:
    """The estimated cost of each item, in the unit of --reader-price and
    --judge-price: its reader prompt (characters / 4) and the judge's, with
    the out tokens of the estimate of --run (an upper figure for a model that
    does not reason)."""
    out = []
    for conv, q, sections, _saved in items:
        msgs = reader_messages(conv, sections, q.question)
        r_in = sum(len(m["content"]) for m in msgs) / 4 + CHAT_OVERHEAD_TOKENS
        j_in = len(judge_prompt(q, "x" * ANSWER_CHARS)) / 4 + CHAT_OVERHEAD_TOKENS
        out.append((r_in * args.reader_price[0] + READER_OUT_TOKENS * args.reader_price[1]
                    + j_in * args.judge_price[0] + JUDGE_OUT_TOKENS * args.judge_price[1]) / 1e6)
    return out


def read_only_one(conv: Conversation, q: Question, sections: list, saved, *, reader: Endpoint,
                  judge: Endpoint, args, spend: Spend, conds: dict) -> dict:
    """One question of a reread or of the full context: the reader on the
    given sections, Mem0's judge, one JSONL line in the shape --run writes.
    Anything but a Fatal is written down as `error` and asked again by the
    next run."""
    rec = {"key": q.key, "sample_id": conv.sample_id, "conversation": conv.index,
           "q_index": q.index, "category": q.category,
           "category_name": CATEGORY_NAMES.get(q.category), "question": q.question,
           "gold": q.gold, "evidence": q.evidence, "current_date": current_date(conv),
           "mode": conds["mode"]}
    if saved is not None:
        rec["user_ids"] = saved.get("user_ids")
        rec["source_label"] = saved.get("label")
    cost = 0.0
    try:
        r = reader.ask(reader_messages(conv, sections, q.question), max_tokens=args.reader_max_tokens)
        spend.asked("reader", r)
        cost += reply_eur(r, spend.price["reader"])
        rec["answer"] = r.text
        rec["reader"] = {"model": r.model, "prompt_tokens": r.prompt_tokens,
                         "completion_tokens": r.completion_tokens, "finish_reason": r.finish_reason}
        if not r.text:
            rec["error"] = f"empty answer (finish_reason {r.finish_reason or 'none'})"
            return rec
        j = judge.ask([{"role": "user", "content": judge_prompt(q, r.text)}],
                      max_tokens=args.judge_max_tokens, json_mode=True)
        spend.asked("judge", j)
        cost += reply_eur(j, spend.price["judge"])
        rec["judge_raw"] = j.text[:2000]
        rec["judge"] = {"model": j.model, "prompt_tokens": j.prompt_tokens,
                        "completion_tokens": j.completion_tokens, "finish_reason": j.finish_reason}
        label = judge_label(j.text)
        rec["label"] = label
        if label is None:
            rec["error"] = "the judge's answer has no readable label"
        else:
            rec["correct"] = label == "CORRECT"
        return rec
    except Fatal:
        raise
    except Exception as e:  # noqa: BLE001 - as answer_one: written down, asked again next run
        rec["error"] = redact(f"{type(e).__name__}: {e}")
        return rec
    finally:
        rec["eur"] = round(cost, 8)
        rec["conditions"] = conds
        rec["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def against_source(records: dict, items: list) -> dict:
    """The verdicts of a reread against the earlier run's on the same blocks:
    right there and wrong here, wrong there and right here, McNemar's p."""
    lost = won = 0
    for _conv, q, _sections, saved in items:
        rec = records.get(q.key)
        if saved is None or not is_done(rec) or not is_done(saved):
            continue
        was, now = saved["label"] == "CORRECT", rec["label"] == "CORRECT"
        lost += int(was and not now)
        won += int(now and not was)
    return {"right_then_wrong": lost, "wrong_then_right": won, "mcnemar_p": mcnemar_exact(lost, won)}


def read_only_run(args, convs: list, data: dict, env, *, transport=urllib_transport,
                  sleep=time.sleep) -> int:
    """--reread RESULTS or --full-context: the estimate, and with --run the
    reader and the judge on every question not yet judged in --out. No
    install is asked and nothing is written to one."""
    mode = read_only_mode(args)
    source = None
    if mode == "reread":
        source, _bad = read_records(args.reread)
        theirs = {(r.get("conditions") or {}).get("data_sha256") for r in source.values()} - {None}
        if theirs and theirs != {data["sha256"]}:
            print(f"\n{args.reread} was run on another data file than {data['file']}: its blocks "
                  "answer other questions", file=sys.stderr)
            return 2
    items, missing = read_only_items(args, convs, source)
    if not items:
        print("\nnothing to read: " + (f"{args.reread} holds no judged line with its blocks for "
                                        "the questions asked" if source is not None
                                        else "no question is asked"), file=sys.stderr)
        return 2
    conds = read_only_conditions(args, env, mode, data["sha256"])
    records, bad = read_records(args.out)
    differ = mismatches(records, conds)
    if differ:
        print(f"\n{args.out} was written under other conditions; pass another --out:\n  "
              + "\n  ".join(differ), file=sys.stderr)
        return 2
    pending = [(i, it) for i, it in enumerate(items) if not is_done(records.get(it[1].key))]
    cost = read_only_estimate(items, args)
    print(f"\n{mode}: {data['file']}, sha256 {data['sha256']}"
          + ("" if data["known"] else " (not the file of snap-research/locomo this was counted on)"))
    if mode == "reread":
        print(f"  blocks     saved in {args.reread} ({conds['source_sha256'][:12]}), each whole (reader "
              "note and memory) in the turn under its person's heading; no install is asked")
        if missing:
            print(f"  missing    {len(missing)} asked questions have no judged line with blocks there "
                  "and are not counted")
    else:
        print("  context    the whole conversation, every session under its date, in place of the "
              "memory; no install is asked")
    print(f"  reader     {env.get('READER_MODEL') or '$READER_MODEL'}, temperature 0, max_tokens "
          f"{args.reader_max_tokens}, \"Current date:\" the last session's day")
    print(f"  judge      {env.get('JUDGE_MODEL') or '$JUDGE_MODEL'}, Mem0's LoCoMo ACCURACY_PROMPT "
          f"verbatim, temperature 0, JSON mode, max_tokens {args.judge_max_tokens}")
    print(f"  scope      {len(items):,} questions of {scope_of_asked()}; {CATEGORY_HEAD}")
    print(f"  estimate   {len(pending):,} to ask, about {sum(cost[i] for i, _ in pending):.2f} at "
          f"--reader-price {args.reader_price[0]:g} {args.reader_price[1]:g} and --judge-price "
          f"{args.judge_price[0]:g} {args.judge_price[1]:g} per million tokens", flush=True)
    if not args.run:
        print("\nNot running: no model was asked. --run asks the reader and the judge.")
        return 2
    missing_env = [name for name in NEEDS[mode] if not env.get(name)]
    if missing_env:
        print(f"\n--{mode} --run needs, in the environment: {', '.join(missing_env)}", file=sys.stderr)
        return 2
    if bad:
        print(f"  {bad} unreadable lines in {args.out} are ignored (a run cut short)")
    reader = Endpoint(env["READER_BASE_URL"], env["READER_MODEL"], env["READER_API_KEY"],
                      transport=transport, sleep=sleep)
    judge = Endpoint(env["JUDGE_BASE_URL"], env["JUDGE_MODEL"], env["JUDGE_API_KEY"],
                     transport=transport, sleep=sleep)
    spend = Spend(write_cost=0.0, reader_price=args.reader_price, judge_price=args.judge_price,
                  cap=cap_of(args))
    stopped, in_a_row, at = None, 0, 0
    try:
        while at < len(pending):
            group, room = [], spend.room()
            for i, item in pending[at:at + args.workers]:
                if cost[i] > room:
                    break
                group.append(item)
                room -= cost[i]
            if not group:
                stopped = (f"--eur-cap {spend.cap:g}: {spend.total():.3f} spent and the next question "
                           "would pass it")
                break
            fatal = None
            with ThreadPoolExecutor(max_workers=len(group)) as pool:
                futures = [pool.submit(read_only_one, conv, q, sections, saved, reader=reader,
                                       judge=judge, args=args, spend=spend, conds=conds)
                           for conv, q, sections, saved in group]
                for (_conv, q, _sections, _saved), f in zip(group, futures):
                    try:
                        rec = f.result()
                    except Fatal as e:
                        fatal = fatal or e
                        continue
                    append_record(args.out, rec)
                    in_a_row = in_a_row + 1 if rec.get("error") else 0
                    print(f"  {q.key:10s} {CATEGORY_NAMES.get(q.category, q.category):11s} "
                          f"{rec.get('label') or 'NO VERDICT'}"
                          + (f"  ({rec['error'][:120]})" if rec.get("error") else ""), flush=True)
            if fatal is not None:
                raise fatal
            if in_a_row >= ERRORS_IN_A_ROW:
                raise Fatal(f"{in_a_row} questions in a row got no verdict; the last: "
                            f"{rec.get('error')}")
            at += len(group)
    except (Fatal, HttpError, Unreachable) as e:
        stopped = str(e)
    except KeyboardInterrupt:
        stopped = "interrupted"
    records, _ = read_records(args.out)
    s = summarize(records, [(q.key, q.category) for _c, q, _s, _r in items])
    if mode == "reread":
        s["against_source"] = against_source(records, items)
    print_read_only_summary(s, args, mode, conds, spend, stopped)
    doc = {"benchmark": "LoCoMo", "mode": mode, "categories_asked": list(ASKED),
           "skipped": CATEGORY_HEAD, "judge_prompt": JUDGE_PROMPT_URL, "summary": s, "data": data,
           "conditions": conds, "missing_from_source": missing,
           "limit_conversations": args.limit_conversations, "limit_questions": args.limit_questions,
           "spend": {"reader": spend.calls["reader"], "judge": spend.calls["judge"],
                     "estimated": round(spend.total(), 6)},
           "stopped": stopped, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    summary_path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if stopped is None and s["without_verdict"] == 0 else 3


def print_read_only_summary(s: dict, args, mode: str, conds: dict, spend: Spend, stopped) -> None:
    what = (f"the blocks of {conds.get('source')} read again" if mode == "reread"
            else "the whole conversation in place of the memory")
    print(f"\nLoCoMo, {what}, {scope_of_asked()}, judged with Mem0's LoCoMo judge; {CATEGORY_HEAD}")
    for c, slot in s["categories"].items():
        if slot["questions"]:
            print(f"  {c} {CATEGORY_NAMES.get(c, c):12s} {slot['correct']:5d}/{slot['questions']:<5d} "
                  f"{100 * slot['accuracy']:5.1f}%")
    if s["questions"]:
        lo, hi = s["ci95"]
        print(f"  overall        {s['correct']:5d}/{s['questions']:<5d} {100 * s['accuracy']:5.1f}%  "
              f"(95% CI {100 * lo:.1f}-{100 * hi:.1f}%; mean of the categories "
              f"{100 * s['category_mean']:.1f}%)")
    if s["without_verdict"]:
        print(f"  INCOMPLETE: {s['without_verdict']} of {s['questions']} questions have no verdict yet, "
              "counted as wrong above. Run the same command again to finish them.")
    a = s.get("against_source")
    if a:
        print(f"  against {conds['source']}: {a['right_then_wrong']} right there and wrong here, "
              f"{a['wrong_then_right']} wrong there and right here (McNemar p {a['mcnemar_p']:.3f})")
    print(f"  conditions: reader {conds['reader_model']} ({conds['reader_host']}); judge "
          f"{conds['judge_model']} ({conds['judge_host']}); data sha256 {conds['data_sha256'][:12]}")
    print(f"  spent now:  reader {spend.calls['reader'][0]} calls {spend.eur('reader'):.3f}, judge "
          f"{spend.calls['judge'][0]} calls {spend.eur('judge'):.3f} (estimated from the tokens at "
          "--reader-price and --judge-price)")
    if stopped:
        print(f"  stopped: {stopped}")
    print(f"  results in {args.out}, summary in {summary_path(args.out)}", flush=True)


# ---------------------------------------------------------------- the run

def run(args, convs: list, data: dict, env, *, transport=urllib_transport, sleep=time.sleep,
        clock=time.monotonic) -> int:
    conds = conditions(args, env, data["sha256"])
    records, bad = read_records(args.out)
    differ = mismatches(records, conds)
    if differ:
        print(f"\n{args.out} was written under other conditions; pass another --out:\n  "
              + "\n  ".join(differ), file=sys.stderr)
        return 2
    carry = {}
    if args.carry_unchanged is not None:
        carry, _ = read_records(args.carry_unchanged)
        differ = mismatches(carry, conds)
        if differ:
            print(f"\n{args.carry_unchanged} was read under other conditions: its answers cannot "
                  "stand for this run's\n  " + "\n  ".join(differ), file=sys.stderr)
            return 2
    korely = Korely(env["KORELY_API_URL"], env["KORELY_API_KEY"],
                    admin_key=env.get("KORELY_ADMIN_KEY"), transport=transport, sleep=sleep)
    reader = Endpoint(env["READER_BASE_URL"], env["READER_MODEL"], env["READER_API_KEY"],
                      transport=transport, sleep=sleep)
    judge = Endpoint(env["JUDGE_BASE_URL"], env["JUDGE_MODEL"], env["JUDGE_API_KEY"],
                     transport=transport, sleep=sleep)
    e = estimate_of(args, convs)
    per_question = e["per_question"]
    # A write at the cost measured, with the text of this mode (estimate).
    spend = Spend(write_cost=e["per_write"], reader_price=args.reader_price,
                  judge_price=args.judge_price, cap=cap_of(args))
    scope = [(q.key, q.category) for c in convs for q in c.asked(args.limit_questions)]
    try:
        health = korely.health()
    except (Fatal, HttpError, Unreachable) as e:
        print(f"\nnot running: the install does not answer GET /health: {e}", file=sys.stderr)
        return 2
    broken = embedder_problem(health)
    if broken:
        print(f"\nnot running: {broken}", file=sys.stderr)
        return 2
    # A writer is needed to write. A run whose turns are all stored already (a
    # reread: the same memories asked again with another block or reader) only
    # reads, and its number is that of the writer that stored them.
    problem = writer_problem(health)
    if problem:
        try:
            left = turns_left(korely, convs, args, records)
        except (Fatal, HttpError, Unreachable) as e:
            print(f"\nnot running: {problem} ({e})", file=sys.stderr)
            return 2
        if left:
            print(f"\nnot running: {problem}; {left} turns are still to write", file=sys.stderr)
            return 2
    install = describe_install(korely, health)
    try:
        before = korely.usage(days=1)
    except (Fatal, HttpError, Unreachable):
        before = None   # no admin scope: the writer's real spend is not read back
    notes = {"reader": reasoning_note(env["READER_BASE_URL"], env["READER_MODEL"]),
             "judge": reasoning_note(env["JUDGE_BASE_URL"], env["JUDGE_MODEL"])}
    print_conditions(args, data, install, conds, convs, notes)
    if problem:
        print("  reread     every turn is stored already and this install has no writer: the run "
              "only reads the memories the writer that stored them wrote")
    if bad:
        print(f"  {bad} unreadable lines in {args.out} are ignored (a run cut short)")
    stopped = None
    try:
        # 1. Every conversation written first, so the worker digests four at a
        #    time while the answers of the first ones are asked.
        plan, promised = [], 0.0
        for conv in convs:
            pending = [q for q in conv.asked(args.limit_questions) if not is_done(records.get(q.key))]
            users = conv.users(base_user(args.user_prefix, conv), args.speakers)
            if not pending:
                print(f"{conv.sample_id}: every question already judged in {args.out}")
                continue
            # Each end user's stored turns, then the rest in the order of the
            # conversation: with --speakers each the two users' writes
            # interleave, and each user's stay in order.
            mems = conv.memories(args.speakers)
            mine = by_user(users, mems)
            todo, twice = [], 0
            for user, _speaker in users:
                left, again = turns_to_write(korely, user, conv, mine[user])
                todo += left
                twice += again
            order = {m.dia_id: i for i, m in enumerate(mems)}
            todo.sort(key=lambda m: order[m.dia_id])
            need = len(todo) * spend.write_cost + len(pending) * per_question
            if need > spend.room(promised):
                stopped = (f"--eur-cap {spend.cap:g} EUR: {conv.sample_id} needs about {need:.3f} "
                           f"EUR more ({len(todo)} turns, {len(pending)} questions); nothing of it "
                           "was written")
                break
            if twice:
                print(f"  {conv.sample_id}: {twice} turns are stored twice (a write answered late)")
            for n, mem in enumerate(todo, 1):
                write_turn(korely, user_of(users, mem), conv, mem, sleep=sleep)
                spend.wrote()
                if n % 100 == 0 or n == len(todo):
                    print(f"  {conv.sample_id}: {n}/{len(todo)} turns written", flush=True)
            if not todo:
                print(f"  {conv.sample_id}: all {len(mems)} turns already stored")
            promised += len(pending) * per_question
            plan.append((conv, users, pending))

        # 2. Each conversation once digested, all its end users: its questions.
        for conv, users, pending in plan:
            for user, _speaker in users:
                wait_digested(korely, user, poll=args.poll_seconds,
                              deadline=args.wait_hours * 3600, sleep=sleep, clock=clock)
            errors = sum(korely.errors(user) for user, _speaker in users)
            print(f"{conv.sample_id} ({', '.join(u for u, _ in users)}): digested"
                  + (f", {errors} memories in error (their text is searchable, their facts "
                     "missing; `manage reextract` queues them again)" if errors else "")
                  + f"; {len(pending)} questions", flush=True)
            promised -= len(pending) * per_question
            why = answer_conversation(conv, users, pending, korely=korely, reader=reader,
                                      judge=judge, args=args, spend=spend, conds=conds,
                                      per_question=per_question, promised=max(0.0, promised),
                                      carry=carry, sleep=sleep)
            if why:
                stopped = why
                break
    except (Fatal, HttpError, Unreachable) as e:
        stopped = str(e)
    except KeyboardInterrupt:
        stopped = "interrupted"
    records, _ = read_records(args.out)
    summary = summarize(records, scope)
    if carry:
        summary["carry_unchanged"] = {"from": str(args.carry_unchanged),
                                      **carried_stats(records, carry, scope)}
    measured = writer_measured(korely, before, args) if before is not None else None
    print_summary(summary, args, data, install, conds, spend, measured, stopped)
    if carry:
        c = summary["carry_unchanged"]
        print(f"  carried    {c['carried']} verdicts from {args.carry_unchanged} (blocks unchanged, "
              f"nothing asked); {c['reread']} read again: {c['wrong_then_right']} wrong then right, "
              f"{c['right_then_wrong']} right then wrong (McNemar p {c['mcnemar_p']:.3f})")
    write_summary(args, summary, data, install, conds, spend, measured, stopped)
    return 0 if stopped is None and summary["without_verdict"] == 0 else 3


def print_conditions(args, data: dict, install: dict, conds: dict, convs: list, notes: dict) -> None:
    users = [u for c in convs for u, _ in c.users(base_user(args.user_prefix, c), args.speakers)]
    each = args.speakers == "each"
    print("\nconditions")
    print(f"  data       {data['file']}, sha256 {data['sha256']}"
          + ("" if data["known"] else " (not the file of snap-research/locomo this was counted on)"))
    print(f"  install    {install.get('url')}, korely-agent {install.get('version')}, writer "
          f"{install.get('writer') or install.get('provider') or '?'}"
          + (f", embeddings {install['embeddings']}" if install.get("embeddings") else ""))
    if each:
        print("  memories   --speakers each: one per turn, in its speaker's end user, after the turn "
              "it answers (\"assistant: Speaker: text\", then \"user: Speaker: text [shared an "
              f"image: caption]\"), timestamp = the session's date_time; end users {users[0]} .. "
              f"{users[-1]}")
    else:
        print("  memories   --speakers one: one per turn, \"Speaker: text [shared an image: "
              f"caption]\", timestamp = the session's date_time; end users {users[0]} .. {users[-1]}")
    budgets = " + ".join(f"{b:,}" for b in conds["token_budgets"])
    print(f"  context    GET /v1/context, token_budget {budgets}"
          + (", one block per person under a heading with the name" if each else "")
          + "; stable in the system prompt, volatile in the turn")
    print(f"  reader     {conds['reader_model']} at {conds['reader_host']}, temperature 0, "
          f"max_tokens {args.reader_max_tokens}, {notes['reader']}, \"Current date:\" the last "
          "session's day")
    print(f"  judge      {conds['judge_model']} at {conds['judge_host']}, Mem0's LoCoMo ACCURACY_PROMPT "
          f"verbatim, temperature 0, JSON mode, max_tokens {args.judge_max_tokens}, {notes['judge']}")
    print(f"  scope      {scope_of_asked()}; {CATEGORY_HEAD}")
    print(f"  results    {args.out}", flush=True)


def writer_measured(korely: Korely, before: dict, args):
    """What the install's writer really spent during this invocation: the
    tenant's model calls now against before, writer stages only (the
    question vectors of /v1/context are embeddings, not writes)."""
    try:
        after = korely.usage(days=1)
    except (Fatal, HttpError, Unreachable):
        return None
    delta = usage_delta(before, after)
    w = _writer_tokens(delta)
    w["eur"] = (w["prompt_tokens"] * args.writer_price[0]
                + w["completion_tokens"] * args.writer_price[1]) / 1e6
    w["models"] = sorted(delta["by_model"])
    return w


def print_summary(s: dict, args, data: dict, install: dict, conds: dict, spend: Spend, measured,
                  stopped) -> None:
    print(f"\nLoCoMo, {scope_of_asked()}, judged with Mem0's LoCoMo judge; {CATEGORY_HEAD}")
    for c, slot in s["categories"].items():
        if slot["questions"]:
            print(f"  {c} {CATEGORY_NAMES.get(c, c):12s} {slot['correct']:5d}/{slot['questions']:<5d} "
                  f"{100 * slot['accuracy']:5.1f}%")
    if s["questions"]:
        lo, hi = s["ci95"]
        print(f"  overall        {s['correct']:5d}/{s['questions']:<5d} {100 * s['accuracy']:5.1f}%  "
              f"(95% CI {100 * lo:.1f}-{100 * hi:.1f}%; mean of the categories "
              f"{100 * s['category_mean']:.1f}%)")
    if s["without_verdict"]:
        print(f"  INCOMPLETE: {s['without_verdict']} of {s['questions']} questions have no verdict yet, "
              "counted as wrong above. Run the same command again to finish them.")
    budgets = " + ".join(f"{b:,}" for b in conds["token_budgets"])
    print(f"  conditions: writer {install.get('writer') or install.get('provider') or '?'}, korely-agent "
          f"{install.get('version')}; --speakers {conds['speakers']}; reader {conds['reader_model']} "
          f"({conds['reader_host']}); judge {conds['judge_model']} ({conds['judge_host']}); "
          f"token_budget {budgets}; data sha256 {data['sha256'][:12]}")
    print(f"  spent now:  reader {spend.calls['reader'][0]} calls {spend.eur('reader'):.3f} EUR, judge "
          f"{spend.calls['judge'][0]} calls {spend.eur('judge'):.3f} EUR, writes {spend.writes} x "
          f"{spend.write_cost:.5f} = {spend.writes * spend.write_cost:.3f} EUR (estimated); "
          f"total {spend.total():.3f} EUR")
    if measured:
        print(f"  the writer, measured on the install: {measured['calls']} calls, "
              f"{measured['prompt_tokens']:,} tokens in, {measured['completion_tokens']:,} out = "
              f"{measured['eur']:.3f} EUR at {args.writer_price[0]:g}/{args.writer_price[1]:g} EUR/M "
              f"(models called, the embedder's included: {', '.join(measured['models']) or 'none'})")
    if stopped:
        print(f"  stopped: {stopped}")
    print(f"  results in {args.out}, summary in {summary_path(args.out)}", flush=True)


def summary_path(out) -> Path:
    p = Path(out)
    return p.with_name(p.stem + ".summary.json")


def write_summary(args, s: dict, data: dict, install: dict, conds: dict, spend: Spend, measured,
                  stopped) -> None:
    doc = {"benchmark": "LoCoMo", "categories_asked": list(ASKED), "skipped": CATEGORY_HEAD,
           "judge_prompt": JUDGE_PROMPT_URL, "summary": s, "data": data, "install": install,
           "conditions": conds, "limit_conversations": args.limit_conversations,
           "limit_questions": args.limit_questions,
           "spend": {"reader": spend.calls["reader"], "judge": spend.calls["judge"],
                     "writes": spend.writes, "write_cost": args.write_cost,
                     "write_cost_speakers": args.write_cost_speakers,
                     "per_write": spend.write_cost, "eur": round(spend.total(), 6)},
           "writer_measured": measured, "stopped": stopped,
           "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    summary_path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- the calibration

def _rows(snapshot: dict, group: str, key: str) -> dict:
    return {str(r.get(key) or ""): r for r in (snapshot.get(group) or [])}


def usage_delta(before: dict, after: dict) -> dict:
    """GET /admin/usage after minus before: calls and tokens by stage, by model,
    by agent and in all. The install's own rate card cost too, when it has one."""
    fields = ("calls", "prompt_tokens", "completion_tokens")
    out = {}
    for group, key in (("by_stage", "stage"), ("by_model", "model"), ("by_agent", "agent_id")):
        b, a = _rows(before, group, key), _rows(after, group, key)
        out[group] = {}
        for k in sorted(set(a) | set(b)):
            d = {f: int((a.get(k) or {}).get(f) or 0) - int((b.get(k) or {}).get(f) or 0)
                 for f in fields}
            if any(d.values()):
                out[group][k] = d
    ta, tb = after.get("totals") or {}, before.get("totals") or {}
    out["totals"] = {f: int(ta.get(f) or 0) - int(tb.get(f) or 0) for f in fields}
    cost = None
    if ta.get("cost") is not None:
        cost = float(ta.get("cost") or 0) - float(tb.get("cost") or 0)
    out["totals"]["cost"] = cost
    out["currency"] = after.get("currency")
    return out


def _writer_tokens(delta: dict) -> dict:
    """The writer's calls in a delta: every stage but the embeddings."""
    w = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    for stage, d in delta["by_stage"].items():
        if stage != "embedding":
            for f in w:
                w[f] += d[f]
    return w


def calibration_report(delta: dict, *, memories: int, agent: str, writer_price) -> dict:
    w = _writer_tokens(delta)
    per_memory = ((w["prompt_tokens"] * writer_price[0] + w["completion_tokens"] * writer_price[1])
                  / 1e6 / memories) if memories else 0.0
    return {"memories": memories, "writer": w, "per_memory_eur": per_memory,
            "stages": delta["by_stage"], "models": delta["by_model"],
            "agent_calls": (delta["by_agent"].get(agent) or {}).get("calls", 0),
            "tenant_calls": delta["totals"]["calls"], "install_cost": delta["totals"]["cost"],
            "currency": delta.get("currency")}


def settled_usage(korely: Korely, *, sleep=time.sleep, pause: float = 5.0, reads: int = 12) -> dict:
    """The usage once it stops moving: a memory reads as digested a moment
    before its calls are flushed to the table."""
    last = korely.usage(days=1)
    for _ in range(reads):
        sleep(pause)
        now = korely.usage(days=1)
        if (now.get("totals") or {}).get("calls") == (last.get("totals") or {}).get("calls"):
            return now
        last = now
    return last


def calibrate(args, conv: Conversation, total_turns: int, rest_eur: float, env, *,
              transport=urllib_transport, sleep=time.sleep, clock=time.monotonic) -> int:
    """The first N turns of `conv` written as --run would write them in this
    --speakers mode (with each, into the two people's end users), all under
    one agent id of their own, which is how the tenant's usage tells the
    calibration's calls from anybody else's."""
    mems = conv.memories(args.speakers)[:args.calibrate]
    cap = cap_of(args)
    expected = len(mems) * args.write_cost
    print(f"\ncalibration: the first {len(mems)} turns of {conv.sample_id} with --speakers "
          f"{args.speakers}, about {expected:.3f} EUR at --write-cost {args.write_cost:.5f}",
          flush=True)
    if cap is not None and expected > cap:
        print(f"refused: that passes --eur-cap {cap:g} EUR", file=sys.stderr)
        return 3
    korely = Korely(env["KORELY_API_URL"], env["KORELY_API_KEY"],
                    admin_key=env.get("KORELY_ADMIN_KEY"), transport=transport, sleep=sleep)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    agent = f"locomo-calibrate-{stamp}"
    users = conv.users(agent, args.speakers)
    try:
        health = korely.health()
        problem = writer_problem(health) or embedder_problem(health)
        if problem:
            print(f"not calibrating: {problem}", file=sys.stderr)
            return 2
        try:
            before = korely.usage(days=1)
        except (Fatal, HttpError) as e:
            print("not calibrating: the real cost is read from the tenant's usage (GET /admin/usage), "
                  "which wants a key with the admin scope in KORELY_ADMIN_KEY: `docker compose exec api "
                  "python -m korely_agent.manage issue-key --tenant <id> --admin --name locomo-admin "
                  f"--expires-in 2`. ({e})", file=sys.stderr)
            return 2
        # Nothing has been written yet: everything the tenant's usage moves
        # by from here on is the calibration's, unless something else is
        # using the tenant (the agent's own row tells, below).
        started = clock()
        for mem in mems:
            write_turn(korely, user_of(users, mem), conv, mem, agent_id=agent, sleep=sleep)
        print(f"  wrote {len(mems)} memories under agent {agent} (end users "
              f"{', '.join(u for u, _ in users)}); waiting for the writer", flush=True)
        for user, _speaker in users:
            wait_digested(korely, user, poll=args.poll_seconds, deadline=args.wait_hours * 3600,
                          sleep=sleep, clock=clock)
        minutes = (clock() - started) / 60
        errors = sum(korely.errors(user) for user, _speaker in users)
        after = settled_usage(korely, sleep=sleep)
    except (Fatal, HttpError, Unreachable) as e:
        print(f"calibration stopped: {e}", file=sys.stderr)
        return 3
    r = calibration_report(usage_delta(before, after), memories=len(mems), agent=agent,
                           writer_price=args.writer_price)
    print_calibration(r, args, agent=agent, users=[u for u, _ in users], minutes=minutes,
                      errors=errors, total_turns=total_turns, rest_eur=rest_eur)
    return 0 if r["writer"]["calls"] else 3


def print_calibration(r: dict, args, *, agent: str, users: list, minutes: float, errors: int,
                      total_turns: int, rest_eur: float) -> None:
    n, w, p = r["memories"], r["writer"], args.writer_price
    print("\ncalibration result")
    print(f"  written     {n} memories with --speakers {args.speakers} under agent {agent}, "
          f"digested in {minutes:.1f} min, {errors} in error")
    for i, (stage, d) in enumerate(sorted(r["stages"].items(), key=lambda kv: -kv[1]["calls"])):
        note = "  (the embedder, not the writer)" if stage == "embedding" else ""
        print(f"  {'by stage' if i == 0 else '':10s}  {stage:14s} {d['calls']:6,} calls "
              f"{d['prompt_tokens']:10,} in {d['completion_tokens']:9,} out{note}")
    if r["models"]:
        print("  by model    " + "; ".join(f"{m} {d['calls']:,} calls"
                                           for m, d in sorted(r["models"].items())))
    if not w["calls"]:
        print("  the writer made no call: is a writer configured on this install (GET /health)?")
        return
    print(f"  per memory  {w['calls'] / n:.1f} writer calls, {w['prompt_tokens'] / n:,.0f} tokens in, "
          f"{w['completion_tokens'] / n:,.0f} out = {r['per_memory_eur']:.6f} EUR at {p[0]:g}/{p[1]:g} EUR/M")
    if r["install_cost"] is not None:
        print(f"  rate card   the install's own LLM_RATES price these calls at {r['install_cost']:.4f} "
              f"{r['currency'] or ''}".rstrip())
    full = r["per_memory_eur"] * total_turns
    print(f"  LoCoMo      {total_turns:,} memories x {r['per_memory_eur']:.6f} = {full:.2f} EUR of "
          f"writing; with the reader and the judge ({rest_eur:.2f} EUR) about {full + rest_eur:.2f} EUR")
    print(f"  next        pass --write-cost {r['per_memory_eur']:.6f} --write-cost-speakers "
          f"{args.speakers} to the estimate and to --run")
    if r["agent_calls"] == r["tenant_calls"]:
        print(f"  isolation   the tenant moved by {r['tenant_calls']:,} calls and this calibration's agent "
              f"by {r['agent_calls']:,}: nothing else was counted")
    else:
        print(f"  WARNING     the tenant moved by {r['tenant_calls']:,} calls and this calibration's agent "
              f"by {r['agent_calls']:,}: something else used the tenant meanwhile and is counted above. "
              "Calibrate on a tenant of its own.")
    print("  note        the writer reads the ten memories before each one, so the first turns cost "
          "less than later ones: 30 or more give a figure closer to the run's")
    print("  left        the memories stay for inspection; "
          + ", ".join(f"DELETE /v1/users/{u}/memories" for u in users) + " erases them", flush=True)


# ---------------------------------------------------------------- main

def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("LOCOMO_DATA") or "locomo10.json"),
                    help=f"locomo10.json (default $LOCOMO_DATA, else ./locomo10.json); {DATA_URL}")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true",
                      help="spend: write the conversations, ask the reader and the judge")
    mode.add_argument("--calibrate", type=int, metavar="N",
                      help="write the first N turns of the first conversation and read back what "
                           "the writer spent (30 is a good N; needs the admin scope)")
    mode.add_argument("--blocks", action="store_true",
                      help="free: every question's blocks from conversations already stored, "
                           "written to --out with no answer; no turn is written, no model asked")
    ap.add_argument("--carry-unchanged", type=Path, metavar="RESULTS",
                    help="with --run: a question whose blocks are byte for byte the ones RESULTS "
                         "read keeps RESULTS' answer and verdict, and only the questions whose "
                         "blocks changed are read and judged again (a reread after a change that "
                         "touches some blocks)")
    ap.add_argument("--compare-blocks", type=Path, metavar="RESULTS",
                    help="with --blocks: an earlier results file; say how many blocks are the "
                         "same, before a paid reread")
    ap.add_argument("--reread", type=Path, metavar="RESULTS",
                    help="the blocks RESULTS (an earlier --run or --blocks) saved, read again by "
                         "this reader and judge; no install is asked. Alone: the estimate; with "
                         "--run: spend it")
    ap.add_argument("--full-context", action="store_true",
                    help="the whole conversation given to the reader in place of the memory (the "
                         "full-context baseline); no install is asked. Alone: the estimate; with "
                         "--run: spend it")
    ap.add_argument("--speakers", choices=SPEAKERS, default="each",
                    help="each (default): an end user per person, a turn written once into its "
                         "speaker's after the turn it answers; one: the conversation in one end user")
    ap.add_argument("--limit-conversations", type=int, metavar="N",
                    help="only the first N conversations")
    ap.add_argument("--limit-questions", type=int, metavar="N",
                    help="at most N questions of categories 1-4 per conversation, evenly spaced")
    ap.add_argument("--category", type=int, action="append", choices=ALL_ASKED, metavar="N",
                    help="only the questions of category N (1-4; give it again for more): a "
                         "reread of one kind of question")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                    help="token_budget of /v1/context in all: with --speakers each, half for each "
                         "person")
    ap.add_argument("--user-prefix", default="locomo-conv-",
                    help="end users are <prefix>3-<name> (--speakers each) or <prefix>3; a new "
                         "prefix starts over")
    ap.add_argument("--out", type=Path, default=Path("bench_locomo.jsonl"),
                    help="one JSONL line per question; the same file resumes")
    ap.add_argument("--write-cost", type=float, default=WRITE_COST_EUR,
                    help="EUR per memory written (--calibrate measures it)")
    ap.add_argument("--write-cost-speakers", choices=SPEAKERS, default="one",
                    help="the --speakers mode --write-cost was measured in (default one, the mode "
                         "of the LongMemEval figure); in the other mode the estimate adds the "
                         "difference in the text the writer reads")
    for who in ("writer", "reader", "judge"):
        ap.add_argument(f"--{who}-price", type=float, nargs=2, default=SCALEWAY_GPT_OSS,
                        metavar=("IN", "OUT"), help="EUR per million tokens (default Scaleway gpt-oss-120b)")
    ap.add_argument("--eur-cap", type=float, default=DEFAULT_CAP_EUR,
                    help="stop before this invocation's estimated spend passes it (0: no cap)")
    ap.add_argument("--reader-max-tokens", type=int, default=None,
                    help=f"default {READER_MAX_TOKENS}; {REREAD_READER_MAX_TOKENS} with --reread "
                         "and --full-context")
    ap.add_argument("--judge-max-tokens", type=int, default=None,
                    help=f"default {JUDGE_MAX_TOKENS}; {REREAD_JUDGE_MAX_TOKENS} with --reread "
                         "and --full-context")
    ap.add_argument("--workers", type=int, default=4, help="questions asked at once")
    ap.add_argument("--poll-seconds", type=float, default=20.0)
    ap.add_argument("--wait-hours", type=float, default=24.0,
                    help="how long a conversation may take to be digested")
    args = ap.parse_args(argv)
    global ASKED
    ASKED = tuple(sorted(set(args.category))) if args.category else ALL_ASKED
    args.writer_price, args.reader_price, args.judge_price = (
        tuple(args.writer_price), tuple(args.reader_price), tuple(args.judge_price))
    max_tokens_defaults(args)
    return args


def args_problem(args):
    shares = split_budget(args.budget, 2 if args.speakers == "each" else 1)
    if not all(50 <= b <= 8000 for b in shares):
        return (f"--budget: each block 50 to 8000, the bounds of GET /v1/context (with --speakers "
                f"{args.speakers} that is {' + '.join(map(str, shares))})")
    for name in ("limit_conversations", "limit_questions", "calibrate"):
        v = getattr(args, name)
        if v is not None and v < 1:
            return f"--{name.replace('_', '-')}: at least 1"
    if args.workers < 1:
        return "--workers: at least 1"
    if args.write_cost < 0:
        return "--write-cost: not negative"
    if args.compare_blocks is not None:
        if not args.blocks:
            return "--compare-blocks goes with --blocks"
        if not args.compare_blocks.is_file():
            return f"--compare-blocks: no file at {args.compare_blocks}"
    if args.carry_unchanged is not None:
        if not args.run:
            return "--carry-unchanged goes with --run"
        if not args.carry_unchanged.is_file():
            return f"--carry-unchanged: no file at {args.carry_unchanged}"
    return read_only_problem(args)


def main(argv=None, *, env=None, transport=urllib_transport, sleep=time.sleep,
         clock=time.monotonic) -> int:
    env = dict(os.environ) if env is None else env
    remember_secrets(env)
    args = parse_args(argv)
    problem = args_problem(args)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    if not args.data.is_file():
        print(f"LoCoMo data not found at {args.data}: download {DATA_URL} (CC BY-NC 4.0) and pass "
              "--data PATH, or set LOCOMO_DATA", file=sys.stderr)
        return 2
    try:
        convs = load_locomo(args.data)
    except (ValueError, KeyError, TypeError) as e:
        print(f"{args.data} is not LoCoMo's locomo10.json: {e}", file=sys.stderr)
        return 2
    data = data_info(args.data, convs)
    chosen = convs[:args.limit_conversations] if args.limit_conversations else convs
    if read_only_mode(args):
        return read_only_run(args, chosen, data, env, transport=transport, sleep=sleep)
    e = estimate_of(args, chosen)
    print_estimate(e, data, args)
    mode = ("calibrate" if args.calibrate else "run" if args.run
            else "blocks" if args.blocks else None)
    if mode is None:
        print("\nNot running: nothing was written and no model was asked. --calibrate N measures what "
              "a write costs, for a few cents; --run spends the estimate above.")
        return 2
    missing = [name for name in NEEDS[mode] if not env.get(name)]
    if missing:
        print(f"\n--{mode} needs, in the environment: {', '.join(missing)}", file=sys.stderr)
        return 2
    if mode == "calibrate":
        return calibrate(args, convs[0], e["turns"], e["eur"]["reader"] + e["eur"]["judge"], env,
                         transport=transport, sleep=sleep, clock=clock)
    if mode == "blocks":
        return blocks_only(args, chosen, env, transport=transport, sleep=sleep)
    return run(args, chosen, data, env, transport=transport, sleep=sleep, clock=clock)


if __name__ == "__main__":
    sys.exit(main())
