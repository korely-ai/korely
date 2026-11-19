"""Hostile cases for the gpt-oss rules and the writer setting (3 Nov 2026).

`core.llm.measured_gpt_oss` gives the extraction rules to the gpt-oss-120b
weights under any host's spelling, `_gpt_oss` gives the least reasoning to
every gpt-oss, and `EXTRACTION_PROVIDER` reads as `mistral` or `gemini`, the
writer `get_provider` builds for it. Here they are attacked from the side the builder did not look at: odd
characters in a model name, a name a million characters long, two regions in
one process, a call that fails and the one after it, the same call repeated,
and the values an old `.env` may hold. No network, no database."""
import importlib.util
import json
import random
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from korely_agent.config import AgentSettings
from korely_agent.core import llm
from korely_agent.core.llm import MistralProvider
from korely_agent.services.facts import extractor
from korely_agent.services.model_choice import PROVIDERS

DB = "postgresql://u:p@localhost:5432/korely"
SCALEWAY = PROVIDERS["scaleway"].base
OPENROUTER = PROVIDERS["openrouter"].base


# ------------------------------------------------------------ odd names

@pytest.mark.parametrize("model,weights", [
    # the size is the size, written however the host writes the rest
    ("gpt-oss-120b", "gpt-oss-120b"),
    ("GPT-OSS-120B", "gpt-oss-120b"),
    ("Gpt-Oss:120B", "gpt-oss-120b"),
    ("openai_gpt-oss-120b", "gpt-oss-120b"),       # an underscore is not part of a word here
    ("openai.gpt-oss-120b", "gpt-oss-120b"),
    ("gpt-oss-120b_q4", "gpt-oss-120b"),
    ("gpt-oss-120b\n", "gpt-oss-120b"),
    ("\tgpt-oss-120b\t", "gpt-oss-120b"),
    ("modèle/gpt-oss-120b", "gpt-oss-120b"),       # "/" ends the word "modèle"
    ("gpt-oss-120b/modèle", "gpt-oss-120b"),
    ("gpt-oss-20b", "gpt-oss-20b"),
    # not the same weights, or no weights named
    ("gpt-oss-0120b", "gpt-oss-0120b"),            # another number, never read as 120
    ("gpt-oss-1200b", "gpt-oss-1200b"),
    ("gpt-oss-120b2", None),
    ("gpt-oss-120bx", None),
    ("gpt-oss-12", None),
    ("gpt-oss-b", None),
    ("gpt-oss--120b", None),
    ("gpt oss 120b", None),
    ("gpt_oss_120b", None),
    ("gptoss-120b", None),
    ("agpt-oss-120b", None),
    ("9gpt-oss-120b", None),
    ("gpt-oss-safeguard-120b", None),
    ("gpt-oss-120", None),
    ("120b", None),
    # a letter or digit of any script glued to the name makes it another word
    ("égpt-oss-120b", None),
    ("жgpt-oss-120b", None),
    ("gpt-oss-120bé", None),
    ("gpt-oss-120bж", None),
    ("gpt-oss-120b１", None),                      # fullwidth 1
    ("", None),
    (None, None),
    # fullwidth digits are digits to \d: they name a size that is not 120
    ("gpt-oss-１２０b", "gpt-oss-１２０b"),
])
def test_a_name_is_read_for_the_weights_it_names(model, weights):
    assert llm._gpt_oss_weights(model) == weights


@pytest.mark.parametrize("model,family", [
    ("gpt-oss", True),
    ("GPT-OSS-20B", True),
    ("gpt-oss-safeguard-120b", True),
    ("openai_gpt-oss-120b", True),
    ("modèle/gpt-oss", True),
    ("gpt-oss_120b", True),
    # what follows "gpt-oss" does not matter, as up to 1.0.7
    ("gpt-oss120b", True),
    ("gpt-ossx", True),
    ("gpt-oss9", True),
    # "gpt-oss" inside another word, read the same way as for the weights
    ("xgpt-oss-120b", False),
    ("9gpt-oss-120b", False),
    ("égpt-oss-120b", False),
    ("жgpt-oss", False),
    ("１gpt-oss", False),
    ("gptoss", False),
    ("", False),
    (None, False),
])
def test_a_name_is_of_the_family_by_the_same_word_as_the_weights(model, family):
    assert llm._gpt_oss(model) is family


@pytest.mark.parametrize("model", [
    "gpt-oss-１２０b",                 # fullwidth 120
    "gpt-oss-١٢٠b",                 # Arabic-Indic 120
    "gpt-oss-120b2", "gpt-oss-120bx", "gpt-oss-0120b", "gpt-oss-1200b",
    "gpt-oss-20b", "gpt-oss", "", None,
])
def test_a_name_that_is_not_the_measured_weights_gets_no_rules(model):
    assert llm.measured_gpt_oss(model) is False


def test_the_two_names_of_one_request_are_the_first_one():
    """A name that names two sizes is read once, deterministically: the first.
    (Nothing real is named like this; it must not raise or flip.)"""
    a = "gpt-oss-20b+gpt-oss-120b"
    b = "gpt-oss-120b+gpt-oss-20b"
    assert [llm.measured_gpt_oss(a), llm.measured_gpt_oss(b)] == [False, True]
    assert [llm.measured_gpt_oss(a) for _ in range(5)] == [False] * 5


@pytest.mark.parametrize("name", ["a" * 1_000_000,
                                  "gpt-oss-" * 100_000,
                                  "gpt-oss-1" + "2" * 1_000_000,
                                  "-" * 1_000_000 + "gpt-oss-120b",
                                  "gpt-oss-120" + "b" * 1_000_000])
def test_a_huge_name_is_read_in_linear_time(name):
    start = time.perf_counter()
    llm.measured_gpt_oss(name)
    llm._gpt_oss(name)
    llm._least_reasoning(SCALEWAY, name)
    assert time.perf_counter() - start < 2.0


def test_a_measured_name_is_always_a_gpt_oss_whatever_the_name():
    """The rules (a narrow set) are inside the least reasoning (the family):
    no random name can get the rules and miss the floor. The bench script reads
    a gpt-oss name as the write path does, on the same names."""
    spec = importlib.util.spec_from_file_location(
        "bench_locomo_p05", Path(__file__).resolve().parent.parent / "scripts" / "bench_locomo.py")
    bench = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bench           # its dataclasses look their module up
    spec.loader.exec_module(bench)

    rng = random.Random(305)
    pieces = ["gpt-oss", "GPT-OSS", "-", ":", "/", ".", "_", " ", "120b", "20b", "120B", "1", "0",
              "b", "x", "openai", "nitro", "GGUF", "ſ", "ß", "İ", "１", "é", "\n"]
    for _ in range(20_000):
        name = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 8)))
        measured = llm.measured_gpt_oss(name)
        assert not measured or llm._gpt_oss(name), repr(name)
        assert bench.gpt_oss(name) is llm._gpt_oss(name), repr(name)
        assert bool(llm._least_reasoning(SCALEWAY, name)) is llm._gpt_oss(name), repr(name)


def test_the_table_of_measured_weights_cannot_be_changed_by_a_caller():
    """MEASURED_GPT_OSS is the data the rules are read from: a tuple, so a
    caller cannot add a size to it by accident."""
    assert isinstance(llm.MEASURED_GPT_OSS, tuple)
    with pytest.raises(AttributeError):
        llm.MEASURED_GPT_OSS.append("gpt-oss-20b")


# ------------------------------------------------------------ the fields

def test_the_least_reasoning_returned_is_never_shared():
    """Two calls get two dicts: one writer editing its payload cannot change
    the next writer's."""
    for base in (SCALEWAY, OPENROUTER):
        first = llm._least_reasoning(base, "gpt-oss-120b")
        first.clear()
        assert llm._least_reasoning(base, "gpt-oss-120b")
        nested = llm._least_reasoning(OPENROUTER, "gpt-oss-120b")
        nested["reasoning"]["effort"] = "high"
        assert llm._least_reasoning(OPENROUTER, "gpt-oss-120b") == {"reasoning": {"effort": "low"}}


@pytest.mark.parametrize("base", ["", None, "not a url", "http://openrouter.ai/x/openrouter.ai"])
def test_an_odd_base_never_raises_for_a_gpt_oss(base):
    assert llm._least_reasoning(base or "", "gpt-oss-120b") in (
        {"reasoning_effort": "low"}, {"reasoning": {"effort": "low"}})


@pytest.mark.parametrize("model", [None, ""])
def test_no_model_gets_nothing(model):
    assert llm._least_reasoning(SCALEWAY, model) == {}


# ------------------------------------------------------------ the request

class _Reply:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _host(monkeypatch, outcomes):
    """A host answering `outcomes` in turn (an exception is raised, a bytes
    body is read); the bodies it was sent are in the list returned."""
    sent = []
    todo = list(outcomes)

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data))
        outcome = todo.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Reply(outcome)

    monkeypatch.setattr(llm, "_last_call", dict(llm._last_call))
    monkeypatch.setattr(llm, "_before_the_call", lambda *a, **k: None)
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    return sent


OK = b'{"choices":[{"message":{"content":"x"}}],"usage":{}}'
ASK = [{"role": "user", "content": "hi"}]


def test_a_call_that_fails_leaves_the_next_one_as_it_was(monkeypatch):
    writer = MistralProvider("k", SCALEWAY, force_model="gpt-oss-120b", vendor="scaleway")
    refused = urllib.error.HTTPError("http://x", 400, "bad", {}, None)
    sent = _host(monkeypatch, [refused, OK])
    with pytest.raises(urllib.error.HTTPError):
        writer.complete(ASK, reasoning=False)
    assert writer.complete(ASK, reasoning=True).text == "x"
    assert len(sent) == 2 and sent[0] == sent[1]
    assert sent[1]["reasoning_effort"] == "low" and sent[1]["model"] == "gpt-oss-120b"
    assert writer.forced_model == "gpt-oss-120b"


def test_a_call_repeated_sends_the_same_body_and_leaves_the_messages_alone(monkeypatch):
    writer = MistralProvider("k", OPENROUTER, force_model="openai/gpt-oss-120b:nitro", vendor="openrouter")
    sent = _host(monkeypatch, [OK] * 4)
    messages = [{"role": "user", "content": "hi"}]
    before = json.dumps(messages)
    for _ in range(4):
        writer.complete(messages, json_only=True, max_tokens=77, temperature=0.0)
    assert all(body == sent[0] for body in sent)
    assert sent[0]["reasoning"] == {"effort": "low"} and "reasoning_effort" not in sent[0]
    assert sent[0]["max_tokens"] == 77 and sent[0]["response_format"] == {"type": "json_object"}
    assert json.dumps(messages) == before


def test_the_extraction_reads_the_model_the_next_call_will_send(monkeypatch):
    """Whatever model the stage asks for, the writer sends its forced one and
    the extraction rules read that same one: the two cannot part."""
    writer = MistralProvider("k", SCALEWAY, force_model="gpt-oss-20b", vendor="scaleway")
    sent = _host(monkeypatch, [OK])
    monkeypatch.setattr(extractor, "_select_provider", lambda m: (writer, "gpt-oss-120b"))
    assert extractor._measured_writer(None) is None
    writer.complete(ASK, model="gpt-oss-120b")
    assert sent[0]["model"] == "gpt-oss-20b"


# ------------------------------------------------------------ two accounts

def _two_regions(monkeypatch, eu_model, us_model):
    monkeypatch.setattr(llm, "_saved_elsewhere", lambda: None)
    monkeypatch.setattr(llm, "screen_writers", lambda: {
        "default": "eu",
        "eu": ("scaleway", SCALEWAY, "k", eu_model),
        "us": ("openrouter", OPENROUTER, "k", us_model)})
    llm.reset_provider_cache()


def test_two_regions_in_one_process_each_keep_their_own_rules(monkeypatch):
    """An account in eu (gpt-oss-120b) and one in us (gpt-oss-20b), writing
    one after the other and inside each other: each is judged by its own
    writer, and a write in one never changes the other's."""
    _two_regions(monkeypatch, "gpt-oss-120b", "openai/gpt-oss-20b")
    try:
        with llm.writing_in("eu"):
            assert extractor._measured_writer(None) == "gpt-oss"
            with llm.writing_in("us"):
                assert extractor._measured_writer(None) is None
            assert extractor._measured_writer(None) == "gpt-oss"
        with llm.writing_in("us"):
            assert extractor._measured_writer(None) is None
            assert llm.get_provider().forced_model == "openai/gpt-oss-20b"
    finally:
        llm.reset_provider_cache()


def test_two_regions_at_once_in_threads_never_cross(monkeypatch):
    _two_regions(monkeypatch, "gpt-oss-120b", "openai/gpt-oss-20b")
    try:
        def read(region):
            with llm.writing_in(region):
                return [extractor._measured_writer(None) for _ in range(200)]

        with ThreadPoolExecutor(8) as pool:
            eu = [pool.submit(read, "eu") for _ in range(8)]
            us = [pool.submit(read, "us") for _ in range(8)]
        assert all(set(f.result()) == {"gpt-oss"} for f in eu)
        assert all(set(f.result()) == {None} for f in us)
    finally:
        llm.reset_provider_cache()


def test_a_writer_changed_on_the_models_page_is_read_at_its_next_write(monkeypatch):
    """The Models page swaps the size of the same vendor's writer: the rules
    follow the new weights at once, the old ones are not remembered."""
    _two_regions(monkeypatch, "gpt-oss-120b", "openai/gpt-oss-20b")
    try:
        with llm.writing_in("eu"):
            assert extractor._measured_writer(None) == "gpt-oss"
        _two_regions(monkeypatch, "gpt-oss-20b", "openai/gpt-oss-120b:nitro")
        with llm.writing_in("eu"):
            assert extractor._measured_writer(None) is None
        with llm.writing_in("us"):
            assert extractor._measured_writer(None) == "gpt-oss"
    finally:
        llm.reset_provider_cache()


def test_the_name_is_read_the_same_from_many_threads():
    names = ["gpt-oss-120b", "openai/gpt-oss-120b:nitro", "gpt-oss-20b", "gpt-oss", "llama", "", "GPT-OSS-120B"] * 300
    serial = [llm.measured_gpt_oss(n) for n in names]
    with ThreadPoolExecutor(16) as pool:
        assert list(pool.map(llm.measured_gpt_oss, names)) == serial


# ------------------------------------------------------------ old data: the .env

@pytest.mark.parametrize("value,read", [
    ("mistral", "mistral"), ("gemini", "gemini"),
    # anything else was the Gemini writer up to 1.0.7 and still is, now said so
    ("Mistral", "gemini"), ("MISTRAL", "gemini"), (" mistral ", "gemini"), ("mistral\n", "gemini"),
    ("\tGemini", "gemini"), ("", "gemini"), (" ", "gemini"), ("\n", "gemini"), ("openai", "gemini"),
    ("google", "gemini"), ("gemini,mistral", "gemini"), ("mistral-small-latest", "gemini"),
    ("null", "gemini"), ("none", "gemini"), ("0", "gemini"), ("gémini", "gemini"),
])
def test_an_old_env_value_means_the_writer_get_provider_builds_for_it(monkeypatch, value, read):
    assert AgentSettings(database_url=DB, extraction_provider=value).extraction_provider == read
    monkeypatch.setenv("EXTRACTION_PROVIDER", value)
    assert AgentSettings(database_url=DB).extraction_provider == read


@pytest.mark.parametrize("value", ["mistral", "Mistral", "openai", ""])
def test_the_setting_and_get_provider_name_the_same_writer(monkeypatch, value):
    """The reading of the line is the branch `get_provider` takes: the
    OpenAI-compatible door for "mistral", Gemini for the rest."""
    s = AgentSettings(database_url=DB, extraction_provider=value, google_api_key="k",
                      mistral_api_key="k", mistral_api_base=SCALEWAY)
    monkeypatch.setattr(llm, "get_settings", lambda: s)
    monkeypatch.setattr(llm, "_saved_elsewhere", lambda: None)
    monkeypatch.setattr(llm, "screen_writers", lambda: {})
    monkeypatch.setattr(llm, "current_google_key", lambda: "k")
    llm.reset_provider_cache()
    try:
        built = llm.get_provider()
    finally:
        llm.reset_provider_cache()
    want = MistralProvider if value == "mistral" else llm.GeminiProvider   # 1.0.7's branches
    assert isinstance(built, want), (value, type(built))
    assert s.extraction_provider == ("mistral" if want is MistralProvider else "gemini")


def test_a_key_pasted_on_the_writer_line_is_not_kept_as_the_setting():
    """The setting is what `GET /health` answers as `provider`: up to 1.0.7 it
    answered the line as written, a key pasted there by mistake included.
    It is now the writer's name and nothing of the line."""
    pasted = "FAKE-sk-or-v1-NOTASECRET-0123456789"
    s = AgentSettings(database_url=DB, extraction_provider=pasted)
    assert s.extraction_provider == "gemini"
    assert pasted.lower() not in repr(s.model_dump()).lower()


@pytest.mark.parametrize("value", [None, 0, False, [], {}, 1.5])
def test_a_value_of_another_type_is_refused_not_read_as_gemini(value):
    with pytest.raises(ValidationError):
        AgentSettings(database_url=DB, extraction_provider=value)


def test_no_line_at_all_is_still_gemini(monkeypatch):
    monkeypatch.delenv("EXTRACTION_PROVIDER", raising=False)
    assert AgentSettings(database_url=DB, _env_file=None).extraction_provider == "gemini"
