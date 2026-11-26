"""scripts/bench_locomo.py, the LoCoMo harness, without spending anything.

Every test runs on a small file in LoCoMo's shape written here, with fakes for
the install and for the two chat endpoints (the reader and the judge): no
network (tests/conftest.py closes it anyway), no database, no model. The one
test on the real locomo10.json runs only when LOCOMO_DATA points at it.
"""
import hashlib
import importlib.util
import json
import os
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_locomo.py"

# SHA-256 of ACCURACY_PROMPT as it stands in mem0ai/mem0's
# evaluation/metrics/llm_judge.py at aae5989 (the text between the triple
# quotes, read from Mem0's file on 2026-10-05). Verbatim means this hash.
MEM0_JUDGE_SHA256 = "62395dd312a631dfd9355026a0b69cc936018274c3198b6365b5c2a5c9bca9e0"

# Made-up values the tests look for in the output (gitleaks:allow marks the
# two its generic rule takes for keys).
ENV = {
    "KORELY_API_URL": "http://korely.test:8000",
    "KORELY_API_KEY": "kor_self_SECRET_api_key_0001",  # gitleaks:allow
    "KORELY_ADMIN_KEY": "kor_self_SECRET_admin_key_0002",
    "READER_BASE_URL": "https://reader.test/v1",
    "READER_MODEL": "gpt-oss-120b",
    "READER_API_KEY": "sk-SECRET-reader-0003",
    "JUDGE_BASE_URL": "https://judge.test/v1",
    "JUDGE_MODEL": "gpt-oss-120b",
    "JUDGE_API_KEY": "sk-SECRET-judge-0004",  # gitleaks:allow
}
SECRETS = [ENV[k] for k in ("KORELY_API_KEY", "KORELY_ADMIN_KEY", "READER_API_KEY", "JUDGE_API_KEY")]


@pytest.fixture
def bench(monkeypatch):
    spec = importlib.util.spec_from_file_location("_bench_locomo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _conversation(sample_id, a, b, sessions, qa, orphans=None):
    conv = {"speaker_a": a, "speaker_b": b}
    for n, (date_time, turns) in sessions.items():
        conv[f"session_{n}"] = turns
        conv[f"session_{n}_date_time"] = date_time
    for n, date_time in (orphans or {}).items():
        conv[f"session_{n}_date_time"] = date_time   # as conv-26: a date, no session
    return {"sample_id": sample_id, "conversation": conv, "qa": qa,
            "event_summary": {}, "observation": {}, "session_summary": {}}


DATA = [
    _conversation(
        "conv-1", "Caroline", "Melanie",
        {
            1: ("1:56 pm on 8 May, 2023", [
                {"speaker": "Caroline", "dia_id": "D1:1", "text": " Hey Mel! Good to see you! "},
                {"speaker": "Melanie", "dia_id": "D1:2", "text": "I painted that lake sunrise last year.",
                 "img_url": ["https://example.com/x.jpg"],
                 "blip_caption": "a photo of a painting of a sunrise over a lake",
                 "query": "sunrise lake painting"},
            ]),
            2: ("12:28 am on 25 May, 2023", [
                {"speaker": "Caroline", "dia_id": "D2:1",
                 "text": "I went to an LGBTQ support group yesterday."},
            ]),
        },
        [
            {"question": "When did Caroline go to the LGBTQ support group?", "answer": "24 May 2023",
             "evidence": ["D2:1"], "category": 2},
            {"question": "When did Melanie paint a sunrise?", "answer": 2022, "evidence": ["D1:2"],
             "category": 2},
            {"question": "What did Caroline adopt last week?", "adversarial_answer": "a puppy",
             "evidence": [], "category": 5},
            {"question": "What did Melanie paint?", "answer": "a sunrise over a lake",
             "evidence": ["D1:2"], "category": 4},
        ],
        orphans={3: "4:10 pm on 26 October, 2023"},
    ),
    _conversation(
        "conv-2", "Jon", "Gina",
        {
            1: ("7:55 pm on 9 June, 2023", [
                {"speaker": "Gina", "dia_id": "D1:1", "text": "I opened my clothing store."},
                {"speaker": "Jon", "dia_id": "D1:2", "text": "",
                 "blip_caption": "a photo of a dance studio"},
            ]),
        },
        [
            {"question": "What business did Gina start and what does Jon do?",
             "answer": "a clothing store; dancing", "evidence": ["D1:1", "D1:2"], "category": 1},
            {"question": "Would Gina enjoy fashion school?", "answer": "Likely yes",
             "evidence": ["D1:1"], "category": 3},
            {"question": "Which city did Jon move to?", "adversarial_answer": "Paris",
             "evidence": [], "category": 5},
        ],
    ),
]
CAT5 = ("What did Caroline adopt last week?", "Which city did Jon move to?")


@pytest.fixture
def data_file(tmp_path):
    p = tmp_path / "locomo10.json"
    p.write_text(json.dumps(DATA), encoding="utf-8")
    return p


# ---------------------------------------------------------------- fakes

# What the writer spends on one memory in the fake install, by stage:
# (calls, tokens in, tokens out). Per memory: 4 writer calls, 4,300 in, 450
# out, so 0.000915 EUR at 0.15/0.60 EUR per million.
PER_MEMORY = {"extraction": (1, 2000, 300), "predicates": (2, 1500, 100),
              "contradiction": (1, 800, 50), "embedding": (3, 60, 0)}
STAGE_MODEL = {"embedding": "multilingual-e5-small"}


class FakeWorld:
    """The install's HTTP API and two OpenAI-compatible chat endpoints, in
    memory, behind the transport the harness calls. Thread-safe: the
    questions are asked --workers at a time."""

    def __init__(self, *, judge=None, reader=None, polls=2, health=None):
        self.lock = threading.Lock()
        self.memories = []
        self.requests = []
        self.chats = {"reader": [], "judge": []}
        self.judge = judge or (lambda prompt: '{"label": "CORRECT"}')
        self.reader = reader or (lambda messages: "an answer")
        self.polls = polls
        self.left = {}
        self.health = health or {"version": "0.1.17", "status": "ok",
                                 "extraction": {"state": "working"}, "provider": "mistral"}
        self.noise = []            # (stage, agent_id, calls, in, out) made by somebody else
        self.usage_refused = False
        self.post_hook = None
        self.chat_hook = None
        self.reader_usage = (1000, 100)

    def __call__(self, method, url, headers, body, timeout):
        p = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(p.query))
        with self.lock:
            self.requests.append((method, p.hostname, p.path, q, body, dict(headers)))
        if p.hostname == "korely.test":
            return self.korely(method, p.path, q, body, headers)
        if p.hostname in ("reader.test", "judge.test"):
            return self.chat(p.hostname.split(".")[0], body)
        raise AssertionError(f"a request to {p.hostname}")

    # the install
    def korely(self, method, path, q, body, headers):
        if path == "/health":
            return self.health
        if path == "/v1/memories" and method == "POST":
            with self.lock:
                mem = {"id": f"mem_{len(self.memories) + 1}", "content": body["content"],
                       "user_id": body["user_id"], "agent_id": body.get("agent_id"),
                       "metadata": body.get("metadata") or {}, "timestamp": body["timestamp"],
                       "status": "processing"}
                self.memories.append(mem)
                self.left[body["user_id"]] = self.polls
            if self.post_hook:
                self.post_hook(mem)
            return mem
        if path == "/v1/memories":
            mine = [m for m in self.memories if m["user_id"] == q["user_id"]][::-1]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 50))
            return {"memories": mine[off:off + lim], "total": len(mine)}
        if path == "/v1/events":
            if q.get("status") == "error":
                return {"events": [], "processing": 0}
            user = q.get("user_id")
            with self.lock:
                if self.left.get(user, 0) > 0:
                    self.left[user] -= 1
                    n = sum(1 for m in self.memories if m["user_id"] == user)
                else:
                    n = 0
            return {"events": [], "processing": n}
        if path == "/v1/context":
            mine = [m for m in self.memories if m["user_id"] == q["user_id"]]
            volatile = "## Relevant memories\n" + "\n".join(f"- {m['content']}" for m in mine)
            stable = "_The facts below are a compact profile of the user._"
            return {"context": stable + "\n\n" + volatile, "stable": stable, "volatile": volatile,
                    "tokens": 42, "sources": [m["id"] for m in mine], "degraded": False}
        if path == "/admin/usage":
            if self.usage_refused or headers["Authorization"] != f"Bearer {ENV['KORELY_ADMIN_KEY']}":
                raise _http_error(403, "the key has no admin scope")
            return self.usage()
        if path == "/admin/models":
            return {"active": {"provider": "scaleway", "model": "gpt-oss-120b", "last_four": "WXYZ"},
                    "source": "screen", "embeddings": {"provider": "local",
                                                       "model": "multilingual-e5-small"}}
        raise AssertionError(f"{method} {path}")

    def usage(self):
        rows = []
        for m in self.memories:
            for stage, (calls, tin, tout) in PER_MEMORY.items():
                rows.append((stage, m["agent_id"], calls, tin, tout))
        rows += self.noise

        def group(index, label):
            out = {}
            for r in rows:
                k = STAGE_MODEL.get(r[0], "gpt-oss-120b") if index == "model" else r[index]
                slot = out.setdefault(k, {label: k, "calls": 0, "prompt_tokens": 0,
                                          "completion_tokens": 0, "cost": None})
                slot["calls"] += r[2]
                slot["prompt_tokens"] += r[3]
                slot["completion_tokens"] += r[4]
            return list(out.values())

        return {"totals": {"calls": sum(r[2] for r in rows), "prompt_tokens": sum(r[3] for r in rows),
                           "completion_tokens": sum(r[4] for r in rows), "cost": None},
                "by_stage": group(0, "stage"), "by_agent": group(1, "agent_id"),
                "by_model": group("model", "model"), "currency": None, "rates_configured": False}

    # the chat endpoints
    def chat(self, who, body):
        with self.lock:
            self.chats[who].append(body)
        if self.chat_hook:
            self.chat_hook(who, body)
        if who == "reader":
            text, (tin, tout) = self.reader(body["messages"]), self.reader_usage
        else:
            text, (tin, tout) = self.judge(body["messages"][0]["content"]), (600, 50)
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": tin, "completion_tokens": tout}, "model": body["model"]}

    def posts(self):
        return [r for r in self.requests if r[0] == "POST" and r[2] == "/v1/memories"]


def _http_error(status, text):
    """The harness's own HttpError, from the module the `bench` fixture loaded."""
    return sys.modules["_bench_locomo"].HttpError(status, text)


def no_sleep(_seconds):
    return None


def run_bench(bench, data_file, out, world, *extra, env=ENV):
    return bench.main(["--data", str(data_file), "--out", str(out), "--poll-seconds", "0", *extra],
                      env=dict(env), transport=world, sleep=no_sleep)


# ---------------------------------------------------------------- the dates

class TestTheDates:
    @pytest.mark.parametrize("raw, expected", [
        ("1:56 pm on 8 May, 2023", datetime(2023, 5, 8, 13, 56, tzinfo=timezone.utc)),
        ("12:28 am on 8 November, 2023", datetime(2023, 11, 8, 0, 28, tzinfo=timezone.utc)),
        ("12:06 pm on 1 Jan, 2024", datetime(2024, 1, 1, 12, 6, tzinfo=timezone.utc)),
        ("9:55 AM on 22 October 2023", datetime(2023, 10, 22, 9, 55, tzinfo=timezone.utc)),
        ("10:04 a.m. on 3 Sept, 2023", datetime(2023, 9, 3, 10, 4, tzinfo=timezone.utc)),
        (" 11:59 pm on 31 December, 2022 ", datetime(2022, 12, 31, 23, 59, tzinfo=timezone.utc)),
    ])
    def test_a_session_date(self, bench, raw, expected):
        assert bench.parse_locomo_date(raw) == expected

    @pytest.mark.parametrize("raw", [
        "", None, "2023-05-08", "13:00 pm on 1 May, 2023", "1:60 pm on 1 May, 2023",
        "1:56 pm on 8 Mai, 2023", "1:56 pm on 31 February, 2023", "1:56 pm on 8 Ma, 2023",
    ])
    def test_anything_else_is_refused_by_name(self, bench, raw):
        with pytest.raises(ValueError):
            bench.parse_locomo_date(raw)

    def test_the_timestamp_the_api_gets(self, bench):
        assert bench.iso_utc(bench.parse_locomo_date("1:56 pm on 8 May, 2023")) == "2023-05-08T13:56:00Z"

    def test_the_current_date_is_the_last_session_with_turns(self, bench, data_file):
        """conv-1 lists a date for a session 3 that holds no turn, as conv-26
        does with sixteen: the reader's today is the last real session."""
        convs = bench.load_locomo(data_file)
        assert bench.current_date(convs[0]) == "2023-05-25 (Thursday)"
        assert bench.current_date(convs[1]) == "2023-06-09 (Friday)"


# ---------------------------------------------------------------- the memories

class TestTheMemories:
    def test_a_turn_is_speaker_and_text(self, bench):
        assert bench.turn_text({"speaker": "Caroline", "text": " Hey Mel! "}) == "Caroline: Hey Mel!"

    def test_a_shared_image_brings_its_caption_in_brackets(self, bench):
        turn = DATA[0]["conversation"]["session_1"][1]
        assert bench.turn_text(turn) == (
            "Melanie: I painted that lake sunrise last year. "
            "[shared an image: a photo of a painting of a sunrise over a lake]")

    def test_an_image_alone_is_still_a_memory(self, bench):
        assert bench.turn_text({"speaker": "Jon", "text": "", "blip_caption": "a photo of a dance studio"}) \
            == "Jon: [shared an image: a photo of a dance studio]"

    def test_a_turn_with_nothing_is_not_written(self, bench):
        assert bench.turn_text({"speaker": "Jon", "text": "  "}) == ""

    def test_one_memory_per_turn_in_order_with_its_session_time(self, bench, data_file):
        conv = bench.load_locomo(data_file)[0]
        mems = conv.memories("one")
        assert [m.dia_id for m in mems] == ["D1:1", "D1:2", "D2:1"]
        assert [m.timestamp for m in mems] == ["2023-05-08T13:56:00Z", "2023-05-08T13:56:00Z",
                                               "2023-05-25T00:28:00Z"]
        assert bench.metadata_of(conv, mems[2]) == {"benchmark": "locomo", "sample_id": "conv-1",
                                                    "session": 2, "dia_id": "D2:1",
                                                    "speaker": "Caroline"}

    def test_with_speakers_one_the_run_writes_them_into_one_end_user(self, bench, data_file,
                                                                      tmp_path):
        world = FakeWorld()
        assert run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run",
                         "--speakers", "one") == 0
        sent = [r[4] for r in world.posts()]
        assert [(b["user_id"], b["metadata"]["dia_id"]) for b in sent] == [
            ("locomo-conv-0", "D1:1"), ("locomo-conv-0", "D1:2"), ("locomo-conv-0", "D2:1"),
            ("locomo-conv-1", "D1:1"), ("locomo-conv-1", "D1:2")]
        assert sent[1]["content"] == ("Melanie: I painted that lake sunrise last year. "
                                      "[shared an image: a photo of a painting of a sunrise over a lake]")
        assert sent[2]["timestamp"] == "2023-05-25T00:28:00Z"
        assert sent[2]["metadata"]["session"] == 2
        assert all("agent_id" not in b for b in sent)


# ---------------------------------------------------------------- one end user per speaker

class TestEachSpeaker:
    """--speakers each, the default: a turn is written once, into its
    speaker's end user, after the turn of the other speaker it answers."""

    def test_the_turns_go_to_their_speakers_end_users_once_each(self, bench, data_file, tmp_path):
        world = FakeWorld()
        assert run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run") == 0
        sent = [r[4] for r in world.posts()]
        assert [(b["user_id"], b["metadata"]["dia_id"]) for b in sent] == [
            ("locomo-conv-0-caroline", "D1:1"), ("locomo-conv-0-melanie", "D1:2"),
            ("locomo-conv-0-caroline", "D2:1"),
            ("locomo-conv-1-gina", "D1:1"), ("locomo-conv-1-jon", "D1:2")]
        assert [b["metadata"]["speaker"] for b in sent] == ["Caroline", "Melanie", "Caroline",
                                                            "Gina", "Jon"]
        assert all("agent_id" not in b for b in sent)

    def test_a_memory_is_the_turn_it_answers_then_the_turn(self, bench, data_file):
        conv = bench.load_locomo(data_file)[0]
        sunrise = conv.memories("each")[1]
        assert sunrise.content == (
            "assistant: Caroline: Hey Mel! Good to see you!\n"
            "user: Melanie: I painted that lake sunrise last year. "
            "[shared an image: a photo of a painting of a sunrise over a lake]")
        assert bench.metadata_of(conv, sunrise) == {
            "benchmark": "locomo", "sample_id": "conv-1", "session": 1, "dia_id": "D1:2",
            "speaker": "Melanie", "context_dia_id": "D1:1"}

    def test_a_sessions_first_turn_is_one_message(self, bench, data_file):
        """Nothing before it in its session: the previous session's last turn
        (Melanie's sunrise) is not what Caroline answers."""
        mems = {m.dia_id: m for m in bench.load_locomo(data_file)[0].memories("each")}
        assert mems["D1:1"].content == "user: Caroline: Hey Mel! Good to see you!"
        assert mems["D2:1"].content == "user: Caroline: I went to an LGBTQ support group yesterday."
        assert mems["D1:1"].context_dia_id == mems["D2:1"].context_dia_id == ""
        assert "context_dia_id" not in bench.metadata_of(bench.load_locomo(data_file)[0], mems["D2:1"])

    def test_an_image_alone_answers_too(self, bench, data_file):
        jon = bench.load_locomo(data_file)[1].memories("each")[1]
        assert jon.content == ("assistant: Gina: I opened my clothing store.\n"
                               "user: Jon: [shared an image: a photo of a dance studio]")

    def test_the_run_sends_the_two_messages_as_written(self, bench, data_file, tmp_path):
        world = FakeWorld()
        run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run")
        sent = {b["metadata"]["dia_id"] + "@" + b["metadata"]["sample_id"]: b
                for b in (r[4] for r in world.posts())}
        assert sent["D1:2@conv-1"]["content"].startswith("assistant: Caroline: Hey Mel!")
        assert sent["D1:2@conv-1"]["metadata"]["context_dia_id"] == "D1:1"
        assert sent["D1:1@conv-1"]["content"] == "user: Caroline: Hey Mel! Good to see you!"
        assert sent["D1:2@conv-1"]["timestamp"] == "2023-05-08T13:56:00Z"

    def test_the_end_user_ids(self, bench, data_file):
        conv = bench.load_locomo(data_file)[0]
        assert conv.users("locomo-conv-0", "each") == [("locomo-conv-0-caroline", "Caroline"),
                                                       ("locomo-conv-0-melanie", "Melanie")]
        assert conv.users("locomo-conv-0", "one") == [("locomo-conv-0", None)]
        conv.speaker_a, conv.speaker_b = "Jo Ann", "jo-ann"     # two names, one slug
        assert [u for u, _ in conv.users("x", "each")] == ["x-jo-ann-a", "x-jo-ann-b"]

    def test_a_turn_by_somebody_else_is_refused_at_load(self, bench, tmp_path):
        data = json.loads(json.dumps(DATA))
        data[0]["conversation"]["session_1"][0]["speaker"] = "Mel"
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="neither Caroline nor Melanie"):
            bench.load_locomo(p)

    def test_each_person_gets_half_the_budget(self, bench, data_file, tmp_path):
        world = FakeWorld()
        run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run", "--workers", "1",
                  "--budget", "4001")
        asked = [(q["user_id"], q["token_budget"]) for _m, _h, path, q, _b, _hd in world.requests
                 if path == "/v1/context"]
        assert asked[:2] == [("locomo-conv-0-caroline", "2001"), ("locomo-conv-0-melanie", "2000")]
        assert len(asked) == 2 * 5
        assert bench.split_budget(4000, 2) == [2000, 2000]
        assert bench.split_budget(4000, 1) == [4000]

    def test_with_speakers_one_the_block_has_the_whole_budget(self, bench, data_file, tmp_path):
        world = FakeWorld()
        run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run", "--speakers", "one")
        asked = {(q["user_id"], q["token_budget"]) for _m, _h, path, q, _b, _hd in world.requests
                 if path == "/v1/context"}
        assert asked == {("locomo-conv-0", "4000"), ("locomo-conv-1", "4000")}

    def test_a_budget_a_block_cannot_take_is_refused(self, bench, data_file, capsys):
        assert bench.main(["--data", str(data_file), "--budget", "90"], env={}) == 2
        assert "each block 50 to 8000" in capsys.readouterr().err
        assert bench.main(["--data", str(data_file), "--budget", "90", "--speakers", "one"],
                          env={}) == 2

    def test_the_reader_gets_both_blocks_under_their_names(self, bench, data_file, tmp_path):
        world = FakeWorld()
        run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run", "--workers", "1")
        body = world.chats["reader"][0]
        system, turn = body["messages"][0]["content"], body["messages"][1]["content"]
        assert system == ("You answer questions about Caroline and Melanie from the memory of their "
                          "conversations. Give a short, direct answer.\n"
                          'In each section of the memory, "the user" means the person named in its '
                          "heading.\n\n_The facts below are a compact profile of the user._")
        assert turn == (
            "# Caroline\n## Relevant memories\n- user: Caroline: Hey Mel! Good to see you!\n"
            "- user: Caroline: I went to an LGBTQ support group yesterday.\n\n"
            "# Melanie\n## Relevant memories\n- assistant: Caroline: Hey Mel! Good to see you!\n"
            "user: Melanie: I painted that lake sunrise last year. "
            "[shared an image: a photo of a painting of a sunrise over a lake]\n\n"
            "Current date: 2023-05-25 (Thursday)\n\n"
            "Question: When did Caroline go to the LGBTQ support group?")

    def test_two_profiles_go_under_their_names_and_an_empty_block_says_so(self, bench, data_file):
        conv = bench.load_locomo(data_file)[0]
        msgs = bench.reader_messages(conv, [
            ("Caroline", {"stable": "_note_\n\n## Profile\n- Caroline is a counselor",
                          "volatile": "## Relevant memories\n- x"}),
            ("Melanie", {"stable": "_note_\n\n## Profile\n- Melanie paints", "volatile": ""}),
        ], "Q?")
        system, turn = msgs[0]["content"], msgs[1]["content"]
        assert system.endswith("# Caroline\n_note_\n\n## Profile\n- Caroline is a counselor\n\n"
                               "# Melanie\n_note_\n\n## Profile\n- Melanie paints")
        assert turn.startswith("# Caroline\n## Relevant memories\n- x\n\n# Melanie\n"
                               "(nothing came back for this person)\n\nCurrent date:")

    def test_the_fingerprint_holds_the_sections_line(self, bench, data_file, tmp_path):
        out = tmp_path / "r.jsonl"
        run_bench(bench, data_file, out, FakeWorld(), "--run")
        line = json.loads(out.read_text().splitlines()[0])
        each = bench.reader_prompt_text("each")
        assert bench.READER_SECTIONS in each and bench.READER_SECTIONS not in bench.reader_prompt_text("one")
        assert line["conditions"]["reader_prompt_sha256"] == hashlib.sha256(each.encode()).hexdigest()
        assert line["conditions"]["speakers"] == "each"
        assert line["conditions"]["token_budgets"] == [2000, 2000]
        assert [c["user_id"] for c in line["context"]] == ["locomo-conv-0-caroline",
                                                           "locomo-conv-0-melanie"]
        assert [c["token_budget"] for c in line["context"]] == [2000, 2000]
        assert line["user_ids"] == ["locomo-conv-0-caroline", "locomo-conv-0-melanie"]

    def test_a_cut_ingestion_goes_on_for_each_person(self, bench, data_file, tmp_path):
        """Caroline's first turn stored before the run: the rest follows in
        the order of the conversation, each person's turns in theirs."""
        world = FakeWorld()
        conv = bench.load_locomo(data_file)[0]
        korely = bench.Korely(ENV["KORELY_API_URL"], ENV["KORELY_API_KEY"], transport=world,
                              sleep=no_sleep)
        bench.write_turn(korely, "locomo-conv-0-caroline", conv, conv.memories("each")[0])
        assert run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run") == 0
        sent = [(r[4]["user_id"], r[4]["metadata"]["dia_id"]) for r in world.posts()]
        assert sent[1:4] == [("locomo-conv-0-melanie", "D1:2"), ("locomo-conv-0-caroline", "D2:1"),
                             ("locomo-conv-1-gina", "D1:1")]
        assert len(world.memories) == 5


# ---------------------------------------------------------------- --category

class TestOneCategory:
    """--category N (2026-10-07): a reread of one kind of question, as the 321
    temporal ones for a sentence about dates in the reader's note."""

    def test_only_that_category_is_asked(self, bench, data_file):
        bench.parse_args(["--data", str(data_file), "--category", "2"])
        asked = [q.category for c in bench.load_locomo(data_file) for q in c.asked()]
        assert asked == [2, 2]
        assert bench.scope_of_asked() == "category 2 (temporal)"

    def test_two_of_them(self, bench, data_file):
        bench.parse_args(["--data", str(data_file), "--category", "4", "--category", "1"])
        assert bench.ASKED == (1, 4)
        assert bench.scope_of_asked() == "categories 1 (multi-hop), 4 (single-hop)"

    def test_without_it_all_four(self, bench, data_file):
        bench.parse_args(["--data", str(data_file), "--category", "2"])
        bench.parse_args(["--data", str(data_file)])
        assert bench.ASKED == (1, 2, 3, 4)
        assert bench.scope_of_asked() == "categories 1-4"

    def test_never_the_adversarial_one(self, bench, data_file):
        with pytest.raises(SystemExit):
            bench.parse_args(["--data", str(data_file), "--category", "5"])


# ---------------------------------------------------------------- category 5

class TestCategoryFive:
    def test_it_is_not_asked(self, bench, data_file):
        convs = bench.load_locomo(data_file)
        asked = [q.category for c in convs for q in c.asked()]
        assert sorted(asked) == [1, 2, 2, 3, 4]

    def test_the_keys_keep_the_place_in_the_file(self, bench, data_file):
        conv = bench.load_locomo(data_file)[0]
        assert [q.key for q in conv.asked()] == ["conv-1:0", "conv-1:1", "conv-1:3"]
        assert conv.asked()[1].gold == "2022"     # an int in the file

    def test_no_model_ever_sees_one(self, bench, data_file, tmp_path, capsys):
        world = FakeWorld()
        assert run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run") == 0
        seen = json.dumps(world.chats)
        assert not any(q in seen for q in CAT5)
        assert len(world.chats["reader"]) == len(world.chats["judge"]) == 5
        out = capsys.readouterr().out
        assert "category 5 (adversarial, 2 questions) is skipped, as Mem0, Zep and Hindsight do" in out
        assert "category 5 (adversarial) is skipped, as Mem0, Zep and Hindsight do" in out

    @pytest.mark.parametrize("n, expected", [(None, list(range(10))), (20, list(range(10))),
                                             (3, [0, 3, 6]), (1, [0]), (0, [])])
    def test_a_sample_is_spread_through_the_list(self, bench, n, expected):
        assert bench.evenly_spaced(list(range(10)), n) == expected


# ---------------------------------------------------------------- the evidence

class TestTheEvidence:
    """The ids an evidence list names. locomo10.json has nine entries that are
    not one plain id (read 2026-10-05): conv-26:37 "D8:6; D9:17", conv-49
    "D9:1 D4:4 D4:6" and two more lists, conv-50 "D30:05", conv-43
    "D:11:26", conv-42 "D"."""

    @pytest.mark.parametrize("evidence, ids, unread", [
        (["D8:6; D9:17"], ["D8:6", "D9:17"], []),
        (["D9:1 D4:4 D4:6"], ["D9:1", "D4:4", "D4:6"], []),
        (["D30:05", "D:11:26"], ["D30:5", "D11:26"], []),
        (["D1:3", "D1:3", "D2:1"], ["D1:3", "D2:1"], []),
        (["D", "D2:1"], ["D2:1"], ["D"]),
        ([], [], []),
        (None, [], []),
    ])
    def test_every_id_of_every_entry(self, bench, evidence, ids, unread):
        assert bench.dia_ids(evidence) == (ids, unread)

    def test_two_ids_in_one_entry_are_both_found_whole(self, bench, tmp_path):
        """conv-26:37 names its two turns in one string, "D8:6; D9:17", and
        evidence_whole held neither though the block showed both."""
        data = [_conversation(
            "conv-26", "Caroline", "Melanie",
            {8: ("1:56 pm on 8 May, 2023", [
                {"speaker": "Caroline", "dia_id": "D8:6", "text": "I signed up for a pottery class."}]),
             9: ("10:31 am on 9 June, 2023", [
                 {"speaker": "Melanie", "dia_id": "D9:17", "text": "I finished my first vase today."}])},
            [{"question": "What did Caroline and Melanie make?", "answer": "pottery",
              "evidence": ["D8:6; D9:17"], "category": 1}])]
        path = tmp_path / "locomo10.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        out = tmp_path / "r.jsonl"
        assert run_bench(bench, path, out, FakeWorld(), "--run") == 0
        line = json.loads(out.read_text().splitlines()[0])
        assert line["evidence"] == ["D8:6; D9:17"], "the file's own list, as it is"
        assert line["evidence_whole"] == ["D8:6", "D9:17"]

    def test_an_id_with_a_zero_in_front_finds_its_turn(self, bench, data_file, tmp_path):
        data = json.loads(data_file.read_text())
        data[0]["qa"][0]["evidence"] = ["D2:01"]
        data_file.write_text(json.dumps(data), encoding="utf-8")
        out = tmp_path / "r.jsonl"
        assert run_bench(bench, data_file, out, FakeWorld(), "--run") == 0
        line = json.loads(out.read_text().splitlines()[0])
        assert (line["key"], line["evidence_whole"]) == ("conv-1:0", ["D2:1"])


# ---------------------------------------------------------------- the judge

class TestTheJudge:
    def test_the_prompt_is_mem0s_byte_for_byte(self, bench):
        assert hashlib.sha256(bench.ACCURACY_PROMPT.encode("utf-8")).hexdigest() == MEM0_JUDGE_SHA256
        assert bench.JUDGE_PROMPT_URL.startswith("https://github.com/mem0ai/mem0/blob/")

    def test_it_reads_the_question_the_gold_and_the_answer(self, bench, data_file):
        q = bench.load_locomo(data_file)[0].asked()[1]
        prompt = bench.judge_prompt(q, "In 2022.")
        assert "Question: When did Melanie paint a sunrise?\nGold answer: 2022\nGenerated answer: In 2022.\n" \
            in prompt

    @pytest.mark.parametrize("text, label", [
        ('{"label": "CORRECT"}', "CORRECT"),
        ('```json\n{"label": "WRONG"}\n```', "WRONG"),
        ('The answer names the same year. {"label":"CORRECT"}', "CORRECT"),
        ('{"explanation": "same date", "label": "correct"}', "CORRECT"),
        ('{"label": "WRONG", "explanation": "it is not CORRECT"}', "WRONG"),
        ("CORRECT", "CORRECT"),
        ("Different city entirely. WRONG", "WRONG"),
        ("CORRECT or WRONG, hard to say", None),
        ('{"label": "PARTIAL"}', None),
        ("", None),
        (None, None),
    ])
    def test_its_label(self, bench, text, label):
        assert bench.judge_label(text) == label

    def test_a_provider_without_json_mode_is_asked_again_without_it(self, bench):
        world = FakeWorld(judge=lambda prompt: '{"label": "CORRECT"}')
        tries = []

        def hook(who, body):
            tries.append("response_format" in body)
            if "response_format" in body:
                raise bench.HttpError(400, "response_format is not supported")

        world.chat_hook = hook
        judge = bench.Endpoint("https://judge.test/v1", "gpt-oss-120b", "k" * 8, transport=world,
                               sleep=no_sleep)
        reply = judge.ask([{"role": "user", "content": "x"}], max_tokens=4096, json_mode=True)
        assert reply.text == '{"label": "CORRECT"}' and tries == [True, False]

    def test_the_run_sends_it_as_mem0_does(self, bench, data_file, tmp_path):
        world = FakeWorld()
        run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run")
        body = world.chats["judge"][0]
        assert [m["role"] for m in body["messages"]] == ["user"]
        assert body["messages"][0]["content"].startswith(bench.ACCURACY_PROMPT.split("{question}")[0])
        assert body["temperature"] == 0.0 and body["response_format"] == {"type": "json_object"}
        assert body["reasoning_effort"] == "low" and body["max_tokens"] == 4096


# ---------------------------------------------------------------- the reader

class TestTheReader:
    def test_the_least_reasoning_is_the_write_paths(self, bench):
        """The same fields korely_agent/core/llm.py sends on every write."""
        from korely_agent.core import llm

        assert bench.OPENAI_REASONING == llm._OPENAI_REASONING
        assert bench.GPT_OSS_FAMILY.pattern == llm._GPT_OSS_FAMILY.pattern
        # The bench reads a gpt-oss name as the write path does, the other
        # spellings of one (an Ollama tag, a Bedrock ID, a file name) included.
        names = ("gpt-oss-120b", "openai/gpt-oss-120b:nitro", "GPT-OSS-120B", "gpt-oss-20b",
                 "gpt-oss:120b", "openai.gpt-oss-120b-1:0", "unsloth/gpt-oss-120b-GGUF",
                 "gpt-oss-120b/other", "x:gpt-oss-120b", "/models/gpt-oss-120b/", "xgpt-oss-120b",
                 "gpt-ossx", "gpt-oss120b", "égpt-oss-120b", "", "gpt-5-mini", "o3",
                 "llama-3.3-70b-instruct", "mistral-small-3.2-24b-instruct-2506")
        assert [bench.gpt_oss(m) for m in names] == [llm.gpt_oss_family(m) for m in names]
        # A host is read from the URL, without letter case, and only the host
        # (the review of 4 Nov 2026): a host that holds the name, or a path
        # that does, is another host.
        for base in ("https://api.scaleway.ai/v1", "https://openrouter.ai/api/v1",
                     "https://api.openai.com/v1", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
                     "https://OpenRouter.AI/api/v1", "https://API.OpenAI.com/v1",
                     "https://openrouter.ai.example.net/v1", "https://proxy.example.net/openrouter.ai/v1",
                     "https://proxy.example.net/api.openai.com/v1"):
            for model in names:
                payload = {"model": model, "messages": [], "max_tokens": 4096, "temperature": 0.0}
                theirs = dict(payload, **llm._least_reasoning(base, model))
                llm._openai_reasoning_fields(base, theirs)
                assert bench.least_reasoning(base, payload) == theirs, (base, model)

    def test_its_prompt_has_the_block_where_the_api_says_and_the_date(self, bench, data_file,
                                                                      tmp_path):
        world = FakeWorld()
        run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run", "--workers", "1",
                  "--speakers", "one")
        body = world.chats["reader"][0]
        system, turn = body["messages"][0]["content"], body["messages"][1]["content"]
        assert system.startswith("You answer questions about Caroline and Melanie")
        assert system.endswith("_The facts below are a compact profile of the user._")
        assert turn.startswith("## Relevant memories\n- Caroline: Hey Mel! Good to see you!")
        assert turn.endswith("Current date: 2023-05-25 (Thursday)\n\n"
                             "Question: When did Caroline go to the LGBTQ support group?")
        assert body["max_tokens"] == 4096 and body["temperature"] == 0.0
        assert body["reasoning_effort"] == "low" and body["model"] == "gpt-oss-120b"

    def test_an_install_before_the_two_parts_gets_the_whole_block_in_the_turn(self, bench, data_file):
        conv = bench.load_locomo(data_file)[0]
        msgs = bench.reader_messages(conv, [(None, {"context": "## Known facts\n- x"})], "Q?")
        assert msgs[1]["content"] == "## Known facts\n- x\n\nCurrent date: 2023-05-25 (Thursday)\n\nQuestion: Q?"


# ---------------------------------------------------------------- resume

class TestResume:
    def test_the_last_line_of_a_question_wins_and_a_cut_line_is_counted(self, bench, tmp_path):
        p = tmp_path / "r.jsonl"
        p.write_text("\n".join([
            json.dumps({"key": "conv-1:0", "error": "HTTP 502: bad gateway"}),
            json.dumps({"key": "conv-1:1", "label": "WRONG"}),
            json.dumps({"key": "conv-1:0", "label": "CORRECT"}),
            '{"key": "conv-1:3", "lab',
        ]) + "\n", encoding="utf-8")
        records, bad = bench.read_records(p)
        assert bad == 1
        assert records["conv-1:0"]["label"] == "CORRECT"
        assert [bench.is_done(records.get(k)) for k in ("conv-1:0", "conv-1:1", "conv-1:3")] == \
            [True, True, False]

    def test_a_second_run_writes_and_asks_nothing(self, bench, data_file, tmp_path):
        world, out = FakeWorld(), tmp_path / "r.jsonl"
        assert run_bench(bench, data_file, out, world, "--run") == 0
        posts, chats = len(world.posts()), len(world.chats["reader"])
        assert run_bench(bench, data_file, out, world, "--run") == 0
        assert len(world.posts()) == posts == 5
        assert len(world.chats["reader"]) == chats == 5
        assert len(out.read_text().splitlines()) == 5

    def test_only_the_questions_without_a_verdict_are_asked_again(self, bench, data_file, tmp_path):
        out = tmp_path / "r.jsonl"
        unreadable = {"When did Melanie paint a sunrise?"}

        def judge(prompt):
            if any(q in prompt for q in unreadable):
                return "hmm"
            return '{"label": "WRONG"}' if "Gina" in prompt else '{"label": "CORRECT"}'

        world = FakeWorld(judge=judge)
        assert run_bench(bench, data_file, out, world, "--run") == 3     # one without a verdict
        unreadable.clear()
        assert run_bench(bench, data_file, out, world, "--run") == 0
        assert len(world.chats["reader"]) == 6
        assert "When did Melanie paint a sunrise?" in json.dumps(world.chats["reader"][-1])
        summary = json.loads((tmp_path / "r.summary.json").read_text())["summary"]
        assert (summary["correct"], summary["questions"], summary["without_verdict"]) == (3, 5, 0)

    def test_a_cut_ingestion_goes_on_from_the_next_turn(self, bench, data_file, tmp_path):
        world = FakeWorld()
        conv = bench.load_locomo(data_file)[0]
        korely = bench.Korely(ENV["KORELY_API_URL"], ENV["KORELY_API_KEY"], transport=world,
                              sleep=no_sleep)
        mems = conv.memories("one")
        bench.write_turn(korely, "locomo-conv-0", conv, mems[0])
        todo, twice = bench.turns_to_write(korely, "locomo-conv-0", conv, mems)
        assert [m.dia_id for m in todo] == ["D1:2", "D2:1"] and twice == 0

    def test_each_persons_store_resumes_on_its_own_turns(self, bench, data_file):
        world = FakeWorld()
        conv = bench.load_locomo(data_file)[0]
        korely = bench.Korely(ENV["KORELY_API_URL"], ENV["KORELY_API_KEY"], transport=world,
                              sleep=no_sleep)
        users = conv.users("locomo-conv-0", "each")
        mine = bench.by_user(users, conv.memories("each"))
        assert {u: [m.dia_id for m in ms] for u, ms in mine.items()} == {
            "locomo-conv-0-caroline": ["D1:1", "D2:1"], "locomo-conv-0-melanie": ["D1:2"]}
        bench.write_turn(korely, "locomo-conv-0-caroline", conv, mine["locomo-conv-0-caroline"][0])
        left, _ = bench.turns_to_write(korely, "locomo-conv-0-caroline", conv,
                                       mine["locomo-conv-0-caroline"])
        assert [m.dia_id for m in left] == ["D2:1"]
        left, _ = bench.turns_to_write(korely, "locomo-conv-0-melanie", conv,
                                       mine["locomo-conv-0-melanie"])
        assert [m.dia_id for m in left] == ["D1:2"]

    def test_an_end_user_holding_something_else_is_refused(self, bench, data_file):
        world = FakeWorld()
        convs = bench.load_locomo(data_file)
        korely = bench.Korely(ENV["KORELY_API_URL"], ENV["KORELY_API_KEY"], transport=world,
                              sleep=no_sleep)
        bench.write_turn(korely, "locomo-conv-0", convs[1], convs[1].memories()[0])   # conv-2's
        with pytest.raises(bench.Fatal, match="not turns of conv-1"):
            bench.turns_to_write(korely, "locomo-conv-0", convs[0], convs[0].memories())

    def test_a_gap_is_refused(self, bench, data_file):
        world = FakeWorld()
        conv = bench.load_locomo(data_file)[0]
        korely = bench.Korely(ENV["KORELY_API_URL"], ENV["KORELY_API_KEY"], transport=world,
                              sleep=no_sleep)
        bench.write_turn(korely, "locomo-conv-0", conv, conv.memories()[1])   # D1:2 without D1:1
        with pytest.raises(bench.Fatal, match="not its first 1"):
            bench.turns_to_write(korely, "locomo-conv-0", conv, conv.memories())

    def test_a_file_of_other_conditions_is_refused(self, bench, data_file, tmp_path, capsys):
        world, out = FakeWorld(), tmp_path / "r.jsonl"
        assert run_bench(bench, data_file, out, world, "--run", "--limit-conversations", "1") == 0
        before = len(world.requests)
        assert run_bench(bench, data_file, out, world, "--run", "--budget", "2000") == 2
        assert "token_budget: the file has 4000, this run 2000" in capsys.readouterr().err
        assert len(world.requests) == before

    def test_a_write_that_answered_late_is_not_written_twice(self, bench, data_file):
        world = FakeWorld()
        conv = bench.load_locomo(data_file)[0]

        def stored_then_502(mem):
            world.post_hook = None
            raise bench.HttpError(502, "bad gateway")

        world.post_hook = stored_then_502
        korely = bench.Korely(ENV["KORELY_API_URL"], ENV["KORELY_API_KEY"], transport=world,
                              sleep=no_sleep)
        assert bench.write_turn(korely, "locomo-conv-0", conv, conv.memories()[0]) is None
        assert len(world.memories) == 1

    def test_a_refused_write_stops_the_run(self, bench, data_file):
        world = FakeWorld()
        conv = bench.load_locomo(data_file)[0]

        def refuse(mem):
            world.memories.remove(mem)
            raise bench.HttpError(422, "timestamp is not an ISO 8601 date")

        world.post_hook = refuse
        korely = bench.Korely(ENV["KORELY_API_URL"], ENV["KORELY_API_KEY"], transport=world,
                              sleep=no_sleep)
        with pytest.raises(bench.Fatal, match="refused turn D1:1"):
            bench.write_turn(korely, "locomo-conv-0", conv, conv.memories()[0])


# ---------------------------------------------------------------- the estimate

class TestTheEstimate:
    def test_its_arithmetic(self, bench, data_file):
        convs = bench.load_locomo(data_file)
        e = bench.estimate(convs, speakers="one", budget=1000, write_cost=0.01,
                           reader_price=(1.0, 2.0), judge_price=(3.0, 4.0))
        asked = [q for c in convs for q in c.asked()]
        avg_q = sum(len(q.question) for q in asked) / 5
        avg_gold = sum(len(q.gold) for q in asked) / 5
        system = bench.READER_SYSTEM.format(speaker_a="Caroline", speaker_b="Melanie")
        turn = bench.READER_TURN.format(volatile="", today="2023-10-22 (Sunday)", question="")
        reader_in = int(100 + 1000 + (len(system) + len(turn) + avg_q) / 4)
        judge_in = int(100 + (len(bench.ACCURACY_PROMPT) + avg_q + avg_gold + 240) / 4)
        assert (e["turns"], e["asked"], e["skipped"], e["sessions"]) == (5, 5, 2, 3)
        assert e["by_category"] == {1: 1, 2: 2, 3: 1, 4: 1}
        assert (e["reader_in"], e["judge_in"]) == (reader_in, judge_in)
        assert e["eur"]["writes"] == pytest.approx(0.05)
        assert e["eur"]["reader"] == pytest.approx(5 * (reader_in * 1.0 + 350 * 2.0) / 1e6)
        assert e["eur"]["judge"] == pytest.approx(5 * (judge_in * 3.0 + 200 * 4.0) / 1e6)
        assert e["total"] == pytest.approx(sum(e["eur"].values()))
        assert e["per_question"] == pytest.approx((e["eur"]["reader"] + e["eur"]["judge"]) / 5)
        assert e["eur"]["text"] == 0 and e["per_write"] == 0.01

    # What each end user holds in the two modes, written out by hand.
    ONE = [["Caroline: Hey Mel! Good to see you!",
            "Melanie: I painted that lake sunrise last year. "
            "[shared an image: a photo of a painting of a sunrise over a lake]",
            "Caroline: I went to an LGBTQ support group yesterday."],
           ["Gina: I opened my clothing store.",
            "Jon: [shared an image: a photo of a dance studio]"]]
    EACH = [["user: Caroline: Hey Mel! Good to see you!",
             "user: Caroline: I went to an LGBTQ support group yesterday."],
            ["assistant: Caroline: Hey Mel! Good to see you!\n"
             "user: Melanie: I painted that lake sunrise last year. "
             "[shared an image: a photo of a painting of a sunrise over a lake]"],
            ["user: Gina: I opened my clothing store."],
            ["assistant: Gina: I opened my clothing store.\n"
             "user: Jon: [shared an image: a photo of a dance studio]"]]

    @staticmethod
    def read(stores):
        """What the writer's extraction reads a memory, on average: the
        memory, and the ten before it in its end user, 300 characters each."""
        total = n = 0
        for store in stores:
            for i, text in enumerate(store):
                total += len(text) + sum(min(300, len(p)) for p in store[max(0, i - 10):i])
                n += 1
        return total / n

    def test_with_each_speaker_the_longer_texts_are_priced(self, bench, data_file):
        convs = bench.load_locomo(data_file)
        prices = dict(writer_price=(2.0, 9.0), reader_price=(1.0, 2.0), judge_price=(3.0, 4.0))
        e = bench.estimate(convs, speakers="each", budget=1000, write_cost=0.01,
                           write_cost_speakers="one", **prices)
        # Negative on a file this small: each person's store holds fewer
        # memories before each one than the conversation's. On the real file
        # the ten are always there and twice as long: +308 tokens a write
        # (test_on_the_real_file).
        tokens = (self.read(self.EACH) - self.read(self.ONE)) / 4
        assert e["text_tokens"] == pytest.approx(tokens)
        assert e["per_write"] == pytest.approx(0.01 + tokens * 2.0 / 1e6)
        assert e["eur"]["writes"] == pytest.approx(0.05)          # 5 memories, not 10
        assert e["eur"]["text"] == pytest.approx(5 * tokens * 2.0 / 1e6)
        assert e["memory_chars"] == pytest.approx(sum(len(t) for s in self.EACH for t in s) / 5)
        assert e["writer_reads"] == pytest.approx(self.read(self.EACH) / 4)
        assert (e["budgets"], e["context_calls"]) == ([500, 500], 10)
        asked = [q for c in convs for q in c.asked()]
        system = (bench.READER_SYSTEM.format(speaker_a="Caroline", speaker_b="Melanie")
                  + "\n" + bench.READER_SECTIONS)
        headings = len("# Caroline") + 2 + len("# Melanie") + 2
        turn = bench.READER_TURN.format(volatile="", today="2023-10-22 (Sunday)", question="")
        avg_q = sum(len(q.question) for q in asked) / 5
        assert e["reader_in"] == int(100 + 1000 + (len(system) + headings + len(turn) + avg_q) / 4)
        assert e["total"] == pytest.approx(sum(e["eur"].values()))

    def test_a_write_cost_measured_in_the_same_mode_is_not_adjusted(self, bench, data_file):
        convs = bench.load_locomo(data_file)
        prices = dict(writer_price=(2.0, 9.0), reader_price=(1.0, 2.0), judge_price=(3.0, 4.0))
        same = bench.estimate(convs, speakers="each", budget=1000, write_cost=0.01,
                              write_cost_speakers="each", **prices)
        assert same["text_tokens"] == 0 and same["eur"]["text"] == 0 and same["per_write"] == 0.01
        back = bench.estimate(convs, speakers="one", budget=1000, write_cost=0.01,
                              write_cost_speakers="each", **prices)
        assert back["text_tokens"] == pytest.approx((self.read(self.ONE) - self.read(self.EACH)) / 4)

    def test_the_default_prices_are_scaleways_gpt_oss_in_eur(self, bench, data_file, capsys):
        assert bench.main(["--data", str(data_file)], env={}, transport=None) == 2
        out = capsys.readouterr().out
        assert "at 0.15/0.6 EUR/M" in out and "x 0.00190 EUR" in out
        assert "--calibrate 30 measures it" in out
        assert "Not running: nothing was written and no model was asked" in out

    def test_it_spends_nothing_and_calls_nobody(self, bench, data_file):
        def nobody(*_a, **_k):
            raise AssertionError("the estimate made a request")

        assert bench.main(["--data", str(data_file), "--limit-questions", "1"], env=dict(ENV),
                          transport=nobody) == 2

    def test_limits_shrink_it(self, bench, data_file):
        convs = bench.load_locomo(data_file)[:1]
        e = bench.estimate(convs, budget=4000, write_cost=0.001, reader_price=(0.15, 0.6),
                           judge_price=(0.15, 0.6), limit_questions=2)
        assert (e["turns"], e["asked"]) == (3, 2)

    def test_on_the_real_file(self, bench, capsys):
        """The counts the harness was written against, when the real
        locomo10.json is at hand (LOCOMO_DATA)."""
        path = os.environ.get("LOCOMO_DATA")
        if not path or not Path(path).is_file():
            pytest.skip("LOCOMO_DATA does not point at locomo10.json")
        convs = bench.load_locomo(path)
        assert bench.data_info(path, convs)["known"]
        for speakers in ("each", "one"):
            e = bench.estimate(convs, speakers=speakers, budget=4000, write_cost=0.0019,
                               reader_price=(0.15, 0.60), judge_price=(0.15, 0.60))
            assert (len(convs), e["sessions"], e["turns"], e["questions_in_file"], e["asked"],
                    e["skipped"]) == (10, 272, 5882, 1986, 1540, 446)
            assert e["by_category"] == {1: 282, 2: 321, 3: 96, 4: 841}
            assert e["eur"]["writes"] == pytest.approx(11.1758)
            assert 12 < e["total"] < 14
        # 5,882 memories either way; with each speaker a memory carries the
        # turn it answers, and the writer reads a few hundred tokens more.
        assert sum(len(c.memories("each")) for c in convs) == 5882
        assert 200 < bench.estimate(convs, speakers="each", budget=4000, write_cost=0.0019,
                                    reader_price=(0.15, 0.6), judge_price=(0.15, 0.6)
                                    )["text_tokens"] < 500


# ---------------------------------------------------------------- the run

class TestTheRun:
    def test_it_needs_its_configuration_before_anything(self, bench, data_file, tmp_path, capsys):
        world = FakeWorld()
        env = {k: v for k, v in ENV.items() if k not in ("JUDGE_API_KEY", "READER_MODEL")}
        assert run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run", env=env) == 2
        assert "--run needs, in the environment: READER_MODEL, JUDGE_API_KEY" in capsys.readouterr().err
        assert world.requests == []

    def test_an_install_without_a_writer_is_not_the_products_number(self, bench, data_file, tmp_path,
                                                                     capsys):
        world = FakeWorld(health={"version": "0.1.17", "extraction": {"state": "not configured"}})
        assert run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run") == 2
        assert "no writer" in capsys.readouterr().err and world.posts() == []

    def test_blocks_cost_nothing_and_need_no_reader(self, bench, data_file, tmp_path, capsys):
        world = FakeWorld()
        assert run_bench(bench, data_file, tmp_path / "first.jsonl", world, "--run") == 0
        posts, asked = len(world.posts()), {k: len(v) for k, v in world.chats.items()}
        env = {k: v for k, v in ENV.items() if k.startswith("KORELY_API")}
        capsys.readouterr()
        out = tmp_path / "blocks.jsonl"
        assert run_bench(bench, data_file, out, world, "--blocks", env=env) == 0
        assert len(world.posts()) == posts
        assert {k: len(v) for k, v in world.chats.items()} == asked
        lines = [json.loads(x) for x in out.read_text().splitlines()]
        assert len(lines) == 5 and all(x["blocks_only"] and x["context"] for x in lines)
        assert all("label" not in x for x in lines)
        assert "no model was asked" in capsys.readouterr().out

    def test_blocks_say_how_many_are_the_same_as_before(self, bench, data_file, tmp_path, capsys):
        world = FakeWorld()
        first = tmp_path / "first.jsonl"
        assert run_bench(bench, data_file, first, world, "--run") == 0
        capsys.readouterr()
        assert run_bench(bench, data_file, tmp_path / "b1.jsonl", world, "--blocks",
                         "--compare-blocks", str(first)) == 0
        assert "5 the same, 0 different" in capsys.readouterr().out
        world.memories[0]["content"] += " (edited)"
        assert run_bench(bench, data_file, tmp_path / "b2.jsonl", world, "--blocks",
                         "--compare-blocks", str(first)) == 0
        out = capsys.readouterr().out
        assert " the same, " in out and "0 different" not in out and "different: conv-1:" in out

    def test_blocks_of_a_conversation_not_stored_are_refused(self, bench, data_file, tmp_path,
                                                             capsys):
        world = FakeWorld()
        assert run_bench(bench, data_file, tmp_path / "b.jsonl", world, "--blocks") == 2
        assert "not stored" in capsys.readouterr().err and world.posts() == []

    def test_compare_blocks_goes_with_blocks(self, bench, data_file, tmp_path, capsys):
        first = tmp_path / "first.jsonl"
        first.write_text("")
        assert run_bench(bench, data_file, tmp_path / "r.jsonl", FakeWorld(), "--run",
                         "--compare-blocks", str(first)) == 2
        assert "--compare-blocks goes with --blocks" in capsys.readouterr().err

    def test_an_install_whose_embedder_fails_is_not_run(self, bench, data_file, tmp_path, capsys):
        """trial3 (2026-10-06): the writer worked, the vectors did not, and
        /health said so in a field nobody read."""
        world = FakeWorld(health={"version": "0.1.17", "extraction": {"state": "working"},
                                  "embeddings": {"provider": "local", "state": "failing",
                                                 "detail": "the model is not in /opt/korely-models"}})
        assert run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run") == 2
        err = capsys.readouterr().err
        assert "embedder is failing" in err and "/opt/korely-models" in err
        assert world.posts() == [] and world.chats == {"reader": [], "judge": []}

    def test_nor_reread_when_its_embedder_fails(self, bench, data_file, tmp_path, capsys):
        world = FakeWorld()
        assert run_bench(bench, data_file, tmp_path / "first.jsonl", world, "--run") == 0
        asked = len(world.chats["reader"])
        world.health = {"version": "0.1.17", "extraction": {"state": "not configured"},
                        "embeddings": {"provider": "local", "state": "failing"}}
        capsys.readouterr()
        assert run_bench(bench, data_file, tmp_path / "again.jsonl", world, "--run") == 2
        assert "embedder is failing" in capsys.readouterr().err
        assert len(world.chats["reader"]) == asked

    def test_an_embedder_not_asked_yet_is_no_reason_to_stop(self, bench, data_file, tmp_path):
        world = FakeWorld(health={"version": "0.1.17", "extraction": {"state": "working"},
                                  "embeddings": {"provider": "gemini", "state": "unknown"}})
        assert run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run") == 0

    def test_a_reread_of_stored_turns_needs_no_writer(self, bench, data_file, tmp_path, capsys):
        """The same memories asked again (another block, another reader) on an
        install that cannot write: nothing to write, so nothing is refused, and
        the conditions say it was a reread."""
        world = FakeWorld()
        assert run_bench(bench, data_file, tmp_path / "first.jsonl", world, "--run") == 0
        written = len(world.posts())
        capsys.readouterr()
        world.health = {"version": "0.1.17", "extraction": {"state": "not configured"}}
        assert run_bench(bench, data_file, tmp_path / "again.jsonl", world, "--run") == 0
        out = capsys.readouterr().out
        assert "reread     every turn is stored already" in out
        assert "  overall            5/5" in out
        assert len(world.posts()) == written

    def test_with_turns_still_to_write_it_is_refused_before_any(self, bench, data_file, tmp_path,
                                                                 capsys):
        world = FakeWorld()
        assert run_bench(bench, data_file, tmp_path / "first.jsonl", world, "--run") == 0
        written = len(world.posts())
        world.memories = world.memories[:-1]      # one turn lost
        world.health = {"version": "0.1.17", "extraction": {"state": "not configured"}}
        capsys.readouterr()
        assert run_bench(bench, data_file, tmp_path / "again.jsonl", world, "--run") == 2
        assert "no writer" in capsys.readouterr().err
        assert len(world.posts()) == written

    def test_the_summary_says_the_number_and_its_conditions(self, bench, data_file, tmp_path,
                                                            capsys):
        def judge(prompt):
            return '{"label": "WRONG"}' if "Gina" in prompt else '{"label": "CORRECT"}'

        out = tmp_path / "r.jsonl"
        assert run_bench(bench, data_file, out, FakeWorld(judge=judge), "--run") == 0
        text = capsys.readouterr().out
        assert "  2 temporal         2/2     100.0%" in text
        assert "  overall            3/5      60.0%" in text
        assert "writer scaleway gpt-oss-120b, korely-agent 0.1.17; --speakers each" in text
        assert ("reader gpt-oss-120b (reader.test); judge gpt-oss-120b (judge.test); "
                "token_budget 2,000 + 2,000") in text
        assert "one block per person under a heading with the name" in text
        assert "the writer, measured on the install: 20 calls" in text
        doc = json.loads((tmp_path / "r.summary.json").read_text())
        assert doc["summary"]["categories"]["1"]["correct"] == 0
        assert doc["conditions"]["judge_prompt_sha256"] == MEM0_JUDGE_SHA256
        assert doc["install"]["writer"] == "scaleway gpt-oss-120b"
        assert "WXYZ" not in json.dumps(doc)       # not even the last four of the writer's key
        line = json.loads(out.read_text().splitlines()[0])
        assert line["key"] == "conv-1:0" and line["label"] == "CORRECT" and line["correct"] is True
        assert line["evidence_whole"] == ["D2:1"]
        assert line["current_date"] == "2023-05-25 (Thursday)"
        assert line["eur"] == pytest.approx((1000 * 0.15 + 100 * 0.6 + 600 * 0.15 + 50 * 0.6) / 1e6)

    def test_no_secret_is_ever_printed_or_written(self, bench, data_file, tmp_path, capsys):
        world = FakeWorld()

        def leak(who, body):
            if who == "reader":
                raise bench.HttpError(401, f"Incorrect API key provided: {ENV['READER_API_KEY']}")

        world.chat_hook = leak
        out = tmp_path / "r.jsonl"
        assert run_bench(bench, data_file, out, world, "--run") == 3
        printed = capsys.readouterr()
        everything = printed.out + printed.err + (tmp_path / "r.summary.json").read_text()
        if out.exists():
            everything += out.read_text()
        assert "refused" in everything and "***" in everything
        assert not any(s in everything for s in SECRETS)
        endpoint = bench.Endpoint("https://r.test", "m", ENV["READER_API_KEY"])
        assert ENV["READER_API_KEY"] not in repr(endpoint)
        # The keys travel in the Authorization header, and only there.
        for _method, _host, _path, query, body, _headers in world.requests:
            assert not any(s in json.dumps([query, body]) for s in SECRETS)

    def test_the_cap_stops_before_a_conversation_is_written(self, bench, data_file, tmp_path, capsys):
        world = FakeWorld()
        assert run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run",
                         "--eur-cap", "0.001") == 3
        assert world.posts() == [] and world.chats["reader"] == []
        assert "--eur-cap 0.001 EUR: conv-1 needs about" in capsys.readouterr().out

    def test_the_cap_stops_the_questions_when_they_cost_more_than_thought(self, bench, data_file,
                                                                          tmp_path, capsys):
        world = FakeWorld()
        world.reader_usage = (1_000_000, 100)     # 0.15 EUR an answer
        assert run_bench(bench, data_file, tmp_path / "r.jsonl", world, "--run", "--workers", "1",
                         "--write-cost", "0", "--eur-cap", "0.1") == 3
        assert len(world.chats["reader"]) == 1
        assert "the next question would pass it" in capsys.readouterr().out

    def test_a_wait_that_outlasts_the_hours_stops(self, bench):
        class Busy:
            def processing(self, user):
                return 5

        ticks = iter(range(0, 10**6, 1000))
        with pytest.raises(bench.Fatal, match="--wait-hours"):
            bench.wait_digested(Busy(), "locomo-conv-0", poll=0, deadline=3600, sleep=no_sleep,
                                clock=lambda: next(ticks))

    def test_the_real_transport_against_a_server_on_this_machine(self, bench):
        """urllib_transport, the one function that talks to the network, on a
        server at 127.0.0.1 (the only address the suite leaves open)."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def answer(self, status, body, headers=()):
                raw = body.encode()
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                routes = {"/ok": (200, '{"a": 1}', ()), "/empty": (200, "", ()),
                          "/html": (200, "<html>maintenance</html>", ()),
                          "/slow": (429, "slow down", (("Retry-After", "7"),))}
                self.answer(*routes[self.path])

            def do_POST(self):
                sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.answer(201, json.dumps({"got": sent, "auth": self.headers["Authorization"]}))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            t = bench.urllib_transport
            assert t("GET", base + "/ok", {}, None, 5) == {"a": 1}
            assert t("GET", base + "/empty", {}, None, 5) == {}
            assert t("POST", base + "/memories", {"Authorization": "Bearer k", "Content-Type":
                                                  "application/json"}, {"content": "Mel: hi"}, 5) \
                == {"got": {"content": "Mel: hi"}, "auth": "Bearer k"}
            with pytest.raises(bench.HttpError) as refused:
                t("GET", base + "/slow", {}, None, 5)
            assert (refused.value.status, refused.value.text, refused.value.retry_after) == \
                (429, "slow down", "7")
            with pytest.raises(bench.Unreachable, match="not JSON"):
                t("GET", base + "/html", {}, None, 5)
        finally:
            server.shutdown()
            server.server_close()

    def test_a_refused_key_is_not_asked_again_and_a_busy_one_is(self, bench):
        seen = []

        def refused(*_a):
            seen.append(1)
            raise bench.HttpError(401, "invalid key")

        with pytest.raises(bench.Fatal):
            bench.call(refused, "GET", "https://x.test/v1", {}, sleep=no_sleep)
        assert len(seen) == 1
        answers = iter([bench.HttpError(503, "busy"), bench.HttpError(429, "slow", "0"), {"ok": 1}])

        def busy(*_a):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a

        assert bench.call(busy, "GET", "https://x.test/v1", {}, sleep=no_sleep) == {"ok": 1}


# ---------------------------------------------------------------- the calibration

class TestTheCalibration:
    def test_it_reads_back_what_the_writer_spent(self, bench, data_file, capsys):
        world = FakeWorld()
        world.noise = [("extraction", None, 7, 9000, 900)]     # before the calibration
        assert bench.main(["--data", str(data_file), "--calibrate", "2", "--poll-seconds", "0"],
                          env=dict(ENV), transport=world, sleep=no_sleep) == 0
        out = capsys.readouterr().out
        assert "per memory  4.0 writer calls, 4,300 tokens in, 450 out = 0.000915 EUR at 0.15/0.6 EUR/M" in out
        assert ("pass --write-cost 0.000915 --write-cost-speakers each to the estimate and to "
                "--run") in out
        assert "(the embedder, not the writer)" in out
        assert "nothing else was counted" in out
        # As --run writes them: each turn into its speaker's end user, the
        # second after the first; one agent id marks them all.
        sent = [r[4] for r in world.posts()]
        assert len(sent) == 2 and sent[0]["agent_id"].startswith("locomo-calibrate-")
        assert sent[0]["agent_id"] == sent[1]["agent_id"]
        assert [b["user_id"] for b in sent] == [sent[0]["agent_id"] + "-caroline",
                                                sent[0]["agent_id"] + "-melanie"]
        assert [b["metadata"]["dia_id"] for b in sent] == ["D1:1", "D1:2"]
        assert sent[1]["content"].startswith("assistant: Caroline: Hey Mel!")

    def test_with_speakers_one_it_writes_one_end_user(self, bench, data_file, capsys):
        world = FakeWorld()
        assert bench.main(["--data", str(data_file), "--calibrate", "3", "--speakers", "one",
                           "--poll-seconds", "0"], env=dict(ENV), transport=world,
                          sleep=no_sleep) == 0
        sent = [r[4] for r in world.posts()]
        assert {b["user_id"] for b in sent} == {sent[0]["agent_id"]}
        assert sent[1]["content"].startswith("Melanie: I painted")
        out = capsys.readouterr().out
        assert "--write-cost 0.000915 --write-cost-speakers one" in out
        assert "nothing else was counted" in out

    def test_somebody_else_on_the_tenant_is_said(self, bench, data_file, capsys):
        world = FakeWorld()

        def meanwhile(mem):
            world.noise.append(("extraction", "another-agent", 3, 3000, 300))

        world.post_hook = meanwhile
        assert bench.main(["--data", str(data_file), "--calibrate", "2", "--poll-seconds", "0"],
                          env=dict(ENV), transport=world, sleep=no_sleep) == 0
        assert "something else used the tenant meanwhile" in capsys.readouterr().out

    def test_with_a_failing_embedder_it_writes_nothing(self, bench, data_file, capsys):
        world = FakeWorld(health={"version": "0.1.17", "extraction": {"state": "working"},
                                  "embeddings": {"provider": "local", "state": "failing"}})
        assert bench.main(["--data", str(data_file), "--calibrate", "2"], env=dict(ENV),
                          transport=world, sleep=no_sleep) == 2
        assert "embedder is failing" in capsys.readouterr().err and world.posts() == []

    def test_without_the_admin_scope_it_writes_nothing(self, bench, data_file, capsys):
        world = FakeWorld()
        world.usage_refused = True
        assert bench.main(["--data", str(data_file), "--calibrate", "2"], env=dict(ENV),
                          transport=world, sleep=no_sleep) == 2
        assert "admin scope" in capsys.readouterr().err and world.posts() == []

    def test_the_delta_is_by_stage_model_and_agent(self, bench):
        before = {"totals": {"calls": 2, "prompt_tokens": 10, "completion_tokens": 1, "cost": 0.5},
                  "by_stage": [{"stage": "extraction", "calls": 2, "prompt_tokens": 10,
                                "completion_tokens": 1}],
                  "by_model": [], "by_agent": []}
        after = {"totals": {"calls": 5, "prompt_tokens": 40, "completion_tokens": 4, "cost": 0.75},
                 "by_stage": [{"stage": "extraction", "calls": 3, "prompt_tokens": 20,
                               "completion_tokens": 2},
                              {"stage": "embedding", "calls": 2, "prompt_tokens": 20,
                               "completion_tokens": 2}],
                 "by_model": [], "by_agent": [{"agent_id": "a", "calls": 3, "prompt_tokens": 30,
                                               "completion_tokens": 3}], "currency": "EUR"}
        d = bench.usage_delta(before, after)
        assert d["by_stage"] == {"extraction": {"calls": 1, "prompt_tokens": 10, "completion_tokens": 1},
                                 "embedding": {"calls": 2, "prompt_tokens": 20, "completion_tokens": 2}}
        assert d["totals"] == {"calls": 3, "prompt_tokens": 30, "completion_tokens": 3, "cost": 0.25}
        r = bench.calibration_report(d, memories=1, agent="a", writer_price=(1.0, 1.0))
        assert r["writer"] == {"calls": 1, "prompt_tokens": 10, "completion_tokens": 1}
        assert r["per_memory_eur"] == pytest.approx(11 / 1e6)
        assert (r["agent_calls"], r["tenant_calls"], r["currency"]) == (3, 3, "EUR")


# ---------------------------------------------------------------- the summary

def test_the_accuracy_counts_a_question_without_a_verdict_as_wrong(bench):
    scope = [("a", 1), ("b", 1), ("c", 2), ("d", 4)]
    records = {"a": {"label": "CORRECT"}, "b": {"label": "WRONG"}, "c": {"error": "HTTP 502"},
               "d": {"label": "CORRECT"}}
    s = bench.summarize(records, scope)
    assert (s["correct"], s["questions"], s["judged"], s["without_verdict"]) == (2, 4, 3, 1)
    assert s["accuracy"] == 0.5
    assert s["categories"][1]["accuracy"] == 0.5 and s["categories"][2]["accuracy"] == 0.0
    assert s["category_mean"] == pytest.approx((0.5 + 0.0 + 1.0) / 3)
    lo, hi = s["ci95"]
    assert 0.0 < lo < 0.5 < hi < 1.0
    assert bench.wilson(0, 0) == (0.0, 0.0)


class TestCarryUnchanged:
    """--carry-unchanged (2026-10-06): a reread pays only for the questions
    whose blocks changed, right and wrong alike; the others keep the earlier
    run's verdict. Asking the wrong ones alone again would turn some into
    right by chance, the reader not being perfectly repeatable, and would
    never see the right ones a change breaks."""

    def _first(self, bench, data_file, tmp_path, world, code=0):
        first = tmp_path / "first.jsonl"
        assert run_bench(bench, data_file, first, world, "--run") == code
        return first

    def _summary(self, tmp_path, name="again"):
        return json.loads((tmp_path / f"{name}.summary.json").read_text())["summary"]

    def test_unchanged_blocks_keep_their_verdicts_and_nobody_is_asked(self, bench, data_file,
                                                                      tmp_path, capsys):
        world = FakeWorld()
        first = self._first(bench, data_file, tmp_path, world)
        asked = {k: len(v) for k, v in world.chats.items()}
        capsys.readouterr()
        out = tmp_path / "again.jsonl"
        assert run_bench(bench, data_file, out, world, "--run", "--carry-unchanged", str(first)) == 0
        assert {k: len(v) for k, v in world.chats.items()} == asked
        lines = [json.loads(x) for x in out.read_text().splitlines()]
        assert len(lines) == 5 and all(x["carried_from"] == "first.jsonl" for x in lines)
        assert all(x["eur"] == 0 for x in lines)
        s = self._summary(tmp_path)
        assert s["correct"] == 5
        assert (s["carry_unchanged"]["carried"], s["carry_unchanged"]["reread"]) == (5, 0)
        assert "5 verdicts from" in capsys.readouterr().out

    def test_only_the_questions_whose_blocks_changed_are_read_again(self, bench, data_file, tmp_path):
        world = FakeWorld(judge=lambda p: '{"label": "WRONG"}' if "Gina" in p else '{"label": "CORRECT"}')
        first = self._first(bench, data_file, tmp_path, world)
        readers = len(world.chats["reader"])
        next(m for m in world.memories if "clothing store" in m["content"])["content"] += " Jackets."
        world.judge = lambda p: '{"label": "CORRECT"}'
        out = tmp_path / "again.jsonl"
        assert run_bench(bench, data_file, out, world, "--run", "--carry-unchanged", str(first)) == 0
        assert len(world.chats["reader"]) - readers == 2
        lines = {json.loads(x)["key"]: json.loads(x) for x in out.read_text().splitlines()}
        assert sorted(k for k, v in lines.items() if not v.get("carried_from")) == ["conv-2:0",
                                                                                     "conv-2:1"]
        c = self._summary(tmp_path)["carry_unchanged"]
        assert (c["carried"], c["reread"], c["wrong_then_right"], c["right_then_wrong"]) == (3, 2, 2, 0)

    def test_a_question_the_earlier_run_could_not_judge_is_read(self, bench, data_file, tmp_path):
        unreadable = {"When did Melanie paint a sunrise?"}
        world = FakeWorld(judge=lambda p: "hmm" if any(q in p for q in unreadable)
                          else '{"label": "CORRECT"}')
        first = self._first(bench, data_file, tmp_path, world, code=3)
        unreadable.clear()
        readers = len(world.chats["reader"])
        assert run_bench(bench, data_file, tmp_path / "again.jsonl", world, "--run",
                         "--carry-unchanged", str(first)) == 0
        assert len(world.chats["reader"]) - readers == 1
        assert "When did Melanie paint a sunrise?" in json.dumps(world.chats["reader"][-1])

    def test_an_earlier_run_under_other_conditions_is_refused(self, bench, data_file, tmp_path,
                                                              capsys):
        world = FakeWorld()
        first = self._first(bench, data_file, tmp_path, world)
        capsys.readouterr()
        assert run_bench(bench, data_file, tmp_path / "again.jsonl", world, "--run",
                         "--carry-unchanged", str(first), "--budget", "3000") == 2
        assert "cannot stand for this run's" in capsys.readouterr().err

    def test_it_goes_with_run(self, bench, data_file, tmp_path, capsys):
        first = tmp_path / "first.jsonl"
        first.write_text("")
        assert run_bench(bench, data_file, tmp_path / "b.jsonl", FakeWorld(), "--blocks",
                         "--carry-unchanged", str(first)) == 2
        assert "--carry-unchanged goes with --run" in capsys.readouterr().err

    @pytest.mark.parametrize("b, c, p", [(0, 0, 1.0), (14, 4, 0.031), (10, 0, 0.002), (5, 5, 1.0)])
    def test_the_paired_test_is_mcnemars_exact(self, bench, b, c, p):
        assert round(bench.mcnemar_exact(b, c), 3) == p


# ---------------------------------------------------------------- reread, full context

# The reader and judge of the two published rereads: gpt-4o-mini, a model
# that gets no reasoning field, so its request body is exactly what is sent.
MINI_ENV = {"READER_BASE_URL": "https://reader.test/v1", "READER_MODEL": "openai/gpt-4o-mini",
            "READER_API_KEY": "sk-SECRET-reader-0003", "JUDGE_BASE_URL": "https://judge.test/v1",
            "JUDGE_MODEL": "openai/gpt-4o-mini",
            "JUDGE_API_KEY": "sk-SECRET-judge-0004"}  # made up, gitleaks:allow
ASKED_KEYS = ["conv-1:0", "conv-1:1", "conv-1:3", "conv-2:0", "conv-2:1"]

# The messages the script that produced the published 86.9% and 79.9% sends
# for the small file above, computed by running its own code on it: the
# whole conversation of conv-2 as its full-context reader read it, and a
# saved block of conv-1 as its reread read it (a note and a memory under
# Caroline, nothing under Melanie).
FULL_CONTEXT_CONV2 = [
    {"role": "system", "content": "You answer questions about Jon and Gina from the memory of their "
                                  "conversations. Give a short, direct answer."},
    {"role": "user", "content": "## Session on 7:55 pm on 9 June, 2023\nGina: I opened my clothing "
                                "store.\nJon: [shared an image: a photo of a dance studio]\n\nCurrent "
                                "date: 2023-06-09 (Friday)\n\nQuestion: Would Gina enjoy fashion school?"},
]
WHOLE_CONV1 = ("## Session on 1:56 pm on 8 May, 2023\nCaroline: Hey Mel! Good to see you!\nMelanie: I "
               "painted that lake sunrise last year. [shared an image: a photo of a painting of a sunrise "
               "over a lake]\n\n## Session on 12:28 am on 25 May, 2023\nCaroline: I went to an LGBTQ "
               "support group yesterday.")
SAVED_CAROLINE = ("_A note, do not abstain when it is present._\n\n## Relevant memories\n"
                  "- Caroline: Hey Mel!")
REREAD_CONV1_3 = [
    {"role": "system", "content": "You answer questions about Caroline and Melanie from the memory of "
                                  "their conversations. Give a short, direct answer.\nIn each section of "
                                  "the memory, \"the user\" means the person named in its heading."},
    {"role": "user", "content": "# Caroline\n" + SAVED_CAROLINE + "\n\n# Melanie\n(nothing came back "
                                "for this person)\n\nCurrent date: 2023-05-25 (Thursday)\n\nQuestion: "
                                "What did Melanie paint?"},
]


def _saved(key, label="CORRECT", *, caroline=SAVED_CAROLINE, degraded=False, **extra):
    """One line of an earlier --run, as answer_one writes it."""
    sid = key.split(":")[0]
    a, b = ("Caroline", "Melanie") if sid == "conv-1" else ("Jon", "Gina")
    rec = {"key": key, "sample_id": sid, "user_ids": [f"x-{a}", f"x-{b}"],
           "context": [{"heading": a, "text": caroline if a == "Caroline" else f"{a}'s block",
                        "degraded": degraded},
                       {"heading": b, "text": "", "degraded": False}],
           "conditions": {"data_sha256": None}}
    if label:
        rec.update(label=label, correct=label == "CORRECT")
    rec.update(extra)
    return rec


class TestRereadAndFullContext:
    """--reread RESULTS and --full-context: the two LoCoMo numbers read by
    gpt-4o-mini (86.9%, the saved blocks read again; 79.9%, the whole
    conversation) with the prompts, questions and counting of the script that
    produced them, and no install asked."""

    def _source(self, tmp_path, data_file, lines):
        sha = hashlib.sha256(data_file.read_bytes()).hexdigest()
        p = tmp_path / "earlier.jsonl"
        for rec in lines:
            rec["conditions"] = {"data_sha256": sha}
        p.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
        return p

    def _run(self, bench, data_file, out, world, *extra, env=MINI_ENV):
        return bench.main(["--data", str(data_file), "--out", str(out), "--workers", "1", *extra],
                          env=dict(env), transport=world, sleep=no_sleep)

    def _summary(self, out):
        return json.loads(out.with_name(out.stem + ".summary.json").read_text())

    # the full context

    def test_full_context_asks_the_questions_of_run_with_the_whole_conversation(
            self, bench, data_file, tmp_path):
        world = FakeWorld()
        out = tmp_path / "full.jsonl"
        assert self._run(bench, data_file, out, world, "--full-context", "--run") == 0
        assert [json.loads(x)["key"] for x in out.read_text().splitlines()] == ASKED_KEYS
        assert not any(r[1] == "korely.test" for r in world.requests)
        bodies = world.chats["reader"]
        assert bodies[3]["messages"] == [
            FULL_CONTEXT_CONV2[0],
            {"role": "user", "content": FULL_CONTEXT_CONV2[1]["content"].replace(
                "Would Gina enjoy fashion school?", "What business did Gina start and what does Jon do?")}]
        assert bodies[4]["messages"] == FULL_CONTEXT_CONV2
        assert bodies[0]["messages"][1]["content"] == (
            WHOLE_CONV1 + "\n\nCurrent date: 2023-05-25 (Thursday)\n\n"
            "Question: When did Caroline go to the LGBTQ support group?")
        assert bench.whole_conversation(bench.load_locomo(data_file)[0]) == WHOLE_CONV1
        assert bodies[0] == {"model": "openai/gpt-4o-mini", "messages": bodies[0]["messages"],
                             "max_tokens": 1024, "temperature": 0.0}

    def test_the_judge_is_mem0s_as_the_published_rereads_sent_it(self, bench, data_file, tmp_path):
        world = FakeWorld(reader=lambda m: "A lake at sunrise.")
        assert self._run(bench, data_file, tmp_path / "f.jsonl", world, "--full-context", "--run") == 0
        body = world.chats["judge"][2]
        assert body == {"model": "openai/gpt-4o-mini", "max_tokens": 300, "temperature": 0.0,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "user", "content": body["messages"][0]["content"]}]}
        prompt = body["messages"][0]["content"]
        assert hashlib.sha256(bench.ACCURACY_PROMPT.encode()).hexdigest() == MEM0_JUDGE_SHA256
        assert prompt.startswith(bench.ACCURACY_PROMPT.split("{question}")[0])
        assert prompt.endswith(
            "Question: What did Melanie paint?\nGold answer: a sunrise over a lake\n"
            "Generated answer: A lake at sunrise.\n\nFirst, provide a short (one sentence) explanation "
            "of your reasoning, then finish with CORRECT or WRONG. \nDo NOT include both CORRECT and "
            "WRONG in your response, or it will break the evaluation script.\n\nJust return the label "
            "CORRECT or WRONG in a json format with the key as \"label\".\n")

    def test_it_counts_per_category_over_the_questions_asked(self, bench, data_file, tmp_path,
                                                            capsys):
        wrong = ("When did Melanie paint a sunrise?", "Would Gina enjoy fashion school?")
        world = FakeWorld(judge=lambda p: '{"label": "WRONG"}' if any(q in p for q in wrong)
                          else '{"label": "CORRECT"}')
        out = tmp_path / "f.jsonl"
        assert self._run(bench, data_file, out, world, "--full-context", "--run") == 0
        s = self._summary(out)["summary"]
        assert (s["correct"], s["questions"], s["judged"]) == (3, 5, 5)
        assert {c: (v["correct"], v["questions"]) for c, v in s["categories"].items()} == {
            "1": (1, 1), "2": (1, 2), "3": (0, 1), "4": (1, 1)}
        assert "overall            3/5      60.0%" in capsys.readouterr().out

    def test_a_category_and_the_limits_choose_as_run_does(self, bench, data_file, tmp_path):
        world = FakeWorld()
        out = tmp_path / "f.jsonl"
        assert self._run(bench, data_file, out, world, "--full-context", "--run", "--category", "2",
                         "--limit-conversations", "1") == 0
        assert [json.loads(x)["key"] for x in out.read_text().splitlines()] == ["conv-1:0", "conv-1:1"]

    # the reread

    def test_reread_reads_the_saved_blocks_whole_in_the_turn(self, bench, data_file, tmp_path):
        source = self._source(tmp_path, data_file, [_saved(k) for k in ASKED_KEYS])
        world = FakeWorld()
        out = tmp_path / "again.jsonl"
        assert self._run(bench, data_file, out, world, "--reread", str(source), "--run") == 0
        assert not any(r[1] == "korely.test" for r in world.requests)
        assert world.chats["reader"][2]["messages"] == REREAD_CONV1_3
        assert world.chats["reader"][2]["max_tokens"] == 1024

    def test_reread_takes_only_lines_judged_with_their_blocks(self, bench, data_file, tmp_path,
                                                               capsys):
        """As the script behind 86.9%: a line with a verdict and blocks. A
        --blocks line (no answer, nothing degraded) is read too; a line with
        no verdict or a degraded block is not."""
        source = self._source(tmp_path, data_file, [
            _saved("conv-1:0"), _saved("conv-1:1", label=None, error="HTTP 502"),
            _saved("conv-1:3", degraded=True, label=None),
            _saved("conv-2:0", label=None, blocks_only=True), _saved("conv-2:1", "WRONG")])
        world = FakeWorld()
        out = tmp_path / "again.jsonl"
        assert self._run(bench, data_file, out, world, "--reread", str(source), "--run") == 0
        assert [json.loads(x)["key"] for x in out.read_text().splitlines()] == [
            "conv-1:0", "conv-2:0", "conv-2:1"]
        doc = self._summary(out)
        assert doc["summary"]["questions"] == 3
        assert doc["missing_from_source"] == ["conv-1:1", "conv-1:3"]
        assert "2 asked questions have no judged line" in capsys.readouterr().out

    def test_reread_compares_with_the_earlier_verdicts(self, bench, data_file, tmp_path, capsys):
        source = self._source(tmp_path, data_file, [
            _saved("conv-1:0", "CORRECT"), _saved("conv-1:1", "WRONG"), _saved("conv-1:3", "WRONG"),
            _saved("conv-2:0", "CORRECT"), _saved("conv-2:1", "CORRECT")])
        world = FakeWorld(judge=lambda p: '{"label": "WRONG"}' if "Gina enjoy" in p
                          else '{"label": "CORRECT"}')
        out = tmp_path / "again.jsonl"
        assert self._run(bench, data_file, out, world, "--reread", str(source), "--run") == 0
        s = self._summary(out)["summary"]
        assert s["correct"] == 4
        assert s["against_source"] == {"right_then_wrong": 1, "wrong_then_right": 2,
                                       "mcnemar_p": 1.0}
        assert "1 right there and wrong here, 2 wrong there and right here" in capsys.readouterr().out

    def test_reread_refuses_blocks_of_another_data_file(self, bench, data_file, tmp_path, capsys):
        p = tmp_path / "earlier.jsonl"
        rec = _saved("conv-1:0")
        rec["conditions"] = {"data_sha256": "0" * 64}
        p.write_text(json.dumps(rec) + "\n")
        world = FakeWorld()
        assert self._run(bench, data_file, tmp_path / "a.jsonl", world, "--reread", str(p), "--run") == 2
        assert "another data file" in capsys.readouterr().err and not world.requests

    # spending, keys, resume

    @pytest.mark.parametrize("mode", ["--full-context", "--reread"])
    def test_without_run_it_only_estimates(self, bench, data_file, tmp_path, capsys, mode):
        source = self._source(tmp_path, data_file, [_saved(k) for k in ASKED_KEYS])
        extra = [mode] + ([str(source)] if mode == "--reread" else [])
        world = FakeWorld()
        assert self._run(bench, data_file, tmp_path / "a.jsonl", world, *extra, env={}) == 2
        printed = capsys.readouterr().out
        assert "5 to ask, about" in printed and "Not running: no model was asked" in printed
        assert not world.requests and not (tmp_path / "a.jsonl").exists()

    @pytest.mark.parametrize("missing", ["READER_API_KEY", "JUDGE_API_KEY", "READER_MODEL"])
    def test_it_refuses_to_run_without_the_reader_and_judge(self, bench, data_file, tmp_path,
                                                             capsys, missing):
        env = {k: v for k, v in MINI_ENV.items() if k != missing}
        world = FakeWorld()
        assert self._run(bench, data_file, tmp_path / "a.jsonl", world, "--full-context", "--run",
                         env=env) == 2
        assert f"needs, in the environment: {missing}" in capsys.readouterr().err
        assert not world.requests

    def test_it_needs_no_install_key(self, bench, data_file, tmp_path):
        assert "KORELY_API_KEY" not in MINI_ENV
        assert self._run(bench, data_file, tmp_path / "a.jsonl", FakeWorld(), "--full-context",
                         "--run") == 0

    def test_the_same_command_resumes_and_other_conditions_are_refused(self, bench, data_file,
                                                                      tmp_path, capsys):
        world = FakeWorld()
        out = tmp_path / "f.jsonl"
        assert self._run(bench, data_file, out, world, "--full-context", "--run") == 0
        asked = len(world.chats["reader"])
        assert self._run(bench, data_file, out, world, "--full-context", "--run") == 0
        assert len(world.chats["reader"]) == asked
        capsys.readouterr()
        assert self._run(bench, data_file, out, world, "--full-context", "--run",
                         env=dict(MINI_ENV, READER_MODEL="openai/gpt-4o")) == 2
        assert "written under other conditions" in capsys.readouterr().err

    def test_a_secret_never_reaches_the_lines(self, bench, data_file, tmp_path):
        world = FakeWorld()
        world.chat_hook = lambda who, body: (_ for _ in ()).throw(
            _http_error(500, f"bad key {MINI_ENV['READER_API_KEY']}")) if who == "judge" else None
        out = tmp_path / "f.jsonl"
        assert self._run(bench, data_file, out, world, "--full-context", "--run") == 3
        text = out.read_text()
        assert MINI_ENV["READER_API_KEY"] not in text and "HTTP 500" in text

    @pytest.mark.parametrize("argv, message", [
        (["--full-context", "--reread", "x.jsonl"], "one or the other"),
        (["--full-context", "--blocks"], "--full-context goes with --run"),
        (["--full-context", "--calibrate", "3"], "--full-context goes with --run"),
        (["--reread", "nowhere.jsonl"], "--reread: no file at nowhere.jsonl"),
    ])
    def test_the_command_line(self, bench, data_file, tmp_path, capsys, argv, message):
        assert self._run(bench, data_file, tmp_path / "a.jsonl", FakeWorld(), *argv) == 2
        assert message in capsys.readouterr().err

    def test_run_keeps_its_own_max_tokens(self, bench):
        args = bench.parse_args(["--run"])
        assert (args.reader_max_tokens, args.judge_max_tokens) == (4096, 4096)
        args = bench.parse_args(["--full-context", "--reader-max-tokens", "64"])
        assert (args.reader_max_tokens, args.judge_max_tokens) == (64, 300)
