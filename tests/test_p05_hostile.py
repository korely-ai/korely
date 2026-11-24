"""A second round of hostile cases for the gpt-oss rules, the Provider
protocol and EXTRACTION_PROVIDER (4 Nov 2026), from sides the first round
(tests/test_the_gpt_oss_rules_hold_against_hostile_input.py) did not take:
the name read against a second, independent reading of the same definition;
every model the Models
page lists, on its own door; the extraction call end to end; two writes
interleaved in one event loop; a writer that cannot be built and the one
after it; a `.env` file written on Windows. No network, no database."""
import asyncio
import json
import random
import re

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


# ------------------------------------------------ a second reading of the name

def _word_char(ch):
    """A letter or digit of any script: what `[^\\W_]` matches."""
    return ch.isalnum()


def _oracle_weights(model):
    """The definition in the comments and docstrings of `_gpt_oss` and
    `_gpt_oss_weights`, written with `str.find` and no regular expression:
    "gpt-oss" with no letter or digit of any script right before it, then
    "-" or ":", the digits of a size, "b", and no letter or digit of any
    script after. The first place that satisfies all of it names the
    weights."""
    name = (model or "").casefold()
    start = name.find("gpt-oss")
    while start != -1:
        end = start + len("gpt-oss")
        before_ok = start == 0 or not _word_char(name[start - 1])
        if before_ok and end < len(name) and name[end] in "-:":
            i = end + 1
            while i < len(name) and name[i].isdecimal():
                i += 1
            digits = name[end + 1:i]
            if digits and name[i:i + 1] == "b" and (i + 1 == len(name) or not _word_char(name[i + 1])):
                return f"gpt-oss-{digits}b"
        start = name.find("gpt-oss", start + 1)
    return None


def _oracle_family(model):
    """"gpt-oss" with no letter or digit of any script right before it,
    whatever follows."""
    name = (model or "").casefold()
    start = name.find("gpt-oss")
    while start != -1:
        if start == 0 or not _word_char(name[start - 1]):
            return True
        start = name.find("gpt-oss", start + 1)
    return False


def test_the_name_is_read_as_a_second_independent_reading_reads_it():
    """40,000 names made of the pieces a host puts in a model ID, and of the
    pieces that break a word: the code and a reading with no regular
    expression agree on the family, on the weights, and on who gets the
    extraction rules and the least reasoning."""
    rng = random.Random(405)
    pieces = ["gpt-oss", "GPT-OSS", "gpt-os", "gpt", "oss", "-", ":", "/", ".", "_", " ", "120b", "120",
              "20b", "b", "B", "1", "0", "9", "x", "openai", "nitro", "GGUF", "é", "ß", "ſ", "İ",
              "ж", "\u0301", "\n", "\t", "１", "٣", "+", "gpt-oss-120b"]
    for _ in range(40_000):
        name = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 9)))
        assert llm._gpt_oss_weights(name) == _oracle_weights(name), repr(name)
        assert llm._gpt_oss(name) is _oracle_family(name), repr(name)
        assert llm.measured_gpt_oss(name) is (_oracle_weights(name) in llm.MEASURED_GPT_OSS), repr(name)
        assert bool(llm._least_reasoning(SCALEWAY, name)) is _oracle_family(name), repr(name)


# ------------------------------------------------ the Models page, on its doors

def test_every_model_the_models_page_lists_is_read_on_every_door(monkeypatch):
    """Not only the vendor's own door: a model the page lists can be served by
    any host (the Models page of one region saved in the database of
    another install), so each listed name is read against each listed base."""
    gpt_oss = [m for v in PROVIDERS.values() for m in v.prefer if "gpt-oss" in m.lower()]
    assert gpt_oss, "the page lists no gpt-oss any more: the case needs a new subject"
    for base in {v.base for v in PROVIDERS.values()}:
        for model in gpt_oss:
            assert llm._gpt_oss(model) and llm.measured_gpt_oss(model), (base, model)
            expect = {"reasoning": {"effort": "low"}} if "openrouter.ai" in base else {"reasoning_effort": "low"}
            assert llm._least_reasoning(base, model) == expect, (base, model)
        for model in (m for v in PROVIDERS.values() for m in v.prefer if "gpt-oss" not in m.lower()):
            assert llm._least_reasoning(base, model) == {}, (base, model)
            assert llm.measured_gpt_oss(model) is False, model


def test_the_measured_note_of_the_models_page_is_about_a_name_the_rules_know():
    """A model the page says it measured as gpt-oss-120b is one the extraction
    gives the rules to; a note that names a writer the code would not
    recognise is a promise the product does not keep."""
    for vendor in PROVIDERS.values():
        for model, note in vendor.measured.items():
            if "gpt-oss-120b" in model.lower():
                assert llm.measured_gpt_oss(model), (vendor.id, model, note)


# ------------------------------------------------ the extraction call, end to end

class _Reply:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


OK = b'{"choices":[{"message":{"content":"{}"}}],"usage":{}}'


def _host(monkeypatch, outcomes):
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


@pytest.mark.parametrize("model,rules", [
    ("gpt-oss-120b", True), ("openai/gpt-oss-120b:nitro", True), ("GPT-OSS-120B", True),
    ("gpt-oss-20b", False), ("gpt-oss", False), ("llama-3.3-70b-instruct", False),
])
def test_the_call_the_extraction_makes_and_the_rules_it_reads_come_from_one_model(monkeypatch, model, rules):
    """`_model_call` is what every extraction goes through. The body it sends
    and the rules the extraction reads are both of the model in force: a
    gpt-oss-20b is sent its floor and read without the rules; a gpt-oss-120b
    both; a model of another family neither."""
    base = OPENROUTER if model.startswith("openai/") else SCALEWAY
    writer = MistralProvider("k", base, force_model=model, vendor="x")
    sent = _host(monkeypatch, [OK])
    monkeypatch.setattr(extractor, "_select_provider", lambda m: (writer, "mistral-small-latest"))
    assert extractor._call_or_raise(None)("hello", 50) == "{}"
    assert sent[0]["model"] == model
    floor = {"reasoning": {"effort": "low"}} if "openrouter.ai" in base else {"reasoning_effort": "low"}
    carries_floor = any(k in sent[0] for k in ("reasoning", "reasoning_effort"))
    assert carries_floor is llm._gpt_oss(model)
    if carries_floor:
        assert all(sent[0][k] == v for k, v in floor.items())
    assert (extractor.measured_writer(None) == "gpt-oss") is rules
    assert extractor._rules_3_1(None) is rules


def test_a_writer_that_cannot_be_built_is_no_rule_and_no_crash_and_the_next_one_is_fine(monkeypatch):
    """A failure in the middle: the Models page cannot be read (the database
    is away) while a write asks which rules it gets. The question answers
    "no rules" instead of raising, and once the page can be read again the
    answer is the writer's own."""
    monkeypatch.setattr(llm, "_saved_elsewhere", lambda: None)
    llm.reset_provider_cache()
    try:
        def broken():
            raise RuntimeError("the database is away")

        monkeypatch.setattr(llm, "screen_writers", broken)
        with llm.writing_in("eu"):
            assert extractor.measured_writer(None) is None
        monkeypatch.setattr(llm, "screen_writers", lambda: {
            "default": "eu", "eu": ("scaleway", SCALEWAY, "k", "gpt-oss-120b")})
        llm.reset_provider_cache()
        with llm.writing_in("eu"):
            assert extractor.measured_writer(None) == "gpt-oss"
    finally:
        llm.reset_provider_cache()


def test_two_writes_interleaved_in_one_event_loop_keep_their_own_writer(monkeypatch):
    """Two accounts written by two coroutines of one loop, each yielding to
    the other in the middle of its write: the writer in force is the one of
    the coroutine asking, at every step."""
    monkeypatch.setattr(llm, "_saved_elsewhere", lambda: None)
    monkeypatch.setattr(llm, "screen_writers", lambda: {
        "default": "eu",
        "eu": ("scaleway", SCALEWAY, "k", "gpt-oss-120b"),
        "us": ("openrouter", OPENROUTER, "k", "openai/gpt-oss-20b")})
    llm.reset_provider_cache()

    async def write(region, rounds):
        seen = []
        with llm.writing_in(region):
            for _ in range(rounds):
                seen.append((extractor.measured_writer(None), llm.get_provider().forced_model))
                await asyncio.sleep(0)
        return seen

    async def both():
        return await asyncio.gather(write("eu", 50), write("us", 50), write("eu", 50), write("us", 50))

    try:
        eu1, us1, eu2, us2 = asyncio.run(both())
    finally:
        llm.reset_provider_cache()
    assert set(eu1 + eu2) == {("gpt-oss", "gpt-oss-120b")}
    assert set(us1 + us2) == {(None, "openai/gpt-oss-20b")}


# ------------------------------------------------ EXTRACTION_PROVIDER

@pytest.mark.parametrize("line,read", [
    ("EXTRACTION_PROVIDER=mistral\r\n", "mistral"),            # a file written on Windows
    ("EXTRACTION_PROVIDER=Mistral\r\n", "gemini"),             # not exactly "mistral", as up to 1.0.7
    ('EXTRACTION_PROVIDER="mistral"\n', "mistral"),
    ("EXTRACTION_PROVIDER='gemini'\n", "gemini"),
    ("EXTRACTION_PROVIDER=mistral # the EU door\n", "mistral"),
    ("export EXTRACTION_PROVIDER=gemini\n", "gemini"),
    ("﻿EXTRACTION_PROVIDER=mistral\n", "mistral"),        # a byte-order mark, as Notepad saves it
])
def test_a_dot_env_file_as_people_write_it_still_means_the_writer(tmp_path, monkeypatch, line, read):
    monkeypatch.delenv("EXTRACTION_PROVIDER", raising=False)
    path = tmp_path / ".env"
    path.write_bytes(line.encode("utf-8"))
    assert AgentSettings(database_url=DB, _env_file=str(path)).extraction_provider == read


def test_a_line_read_as_gemini_meets_a_gemini_vocabulary_without_a_refusal():
    """The check that the vocabulary speaks the provider's language reads the
    writer the line means: `openai` with a Gemini vocabulary starts, as it did
    up to 1.0.7, because the writer is Gemini."""
    s = AgentSettings(database_url=DB, extraction_provider="openai", vocabulary_model="gemini-3.1-flash-lite")
    assert s.extraction_provider == "gemini"
    with pytest.raises(ValidationError):
        AgentSettings(database_url=DB, extraction_provider="mistral", vocabulary_model="gemini-3.1-flash-lite")


def test_the_settings_of_the_install_are_one_object_whatever_the_writer_line_looked_like(monkeypatch):
    """Cached settings: the same call twice gives the same writer, and a
    cache cleared reads the environment again, so a corrected line is
    taken at the next start."""
    from korely_agent.config import get_settings

    monkeypatch.setenv("DATABASE_URL", DB)
    monkeypatch.setenv("EXTRACTION_PROVIDER", " Mistral ")
    get_settings.cache_clear()
    try:
        assert get_settings().extraction_provider == "gemini"
        assert get_settings() is get_settings()
        monkeypatch.setenv("EXTRACTION_PROVIDER", "mistral")
        monkeypatch.setenv("MISTRAL_API_KEY", "k")
        get_settings.cache_clear()
        assert get_settings().extraction_provider == "mistral"
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("value", ["gemini", "mistral"])
def test_the_two_values_are_what_the_provider_branches_of_get_provider_read(value):
    """The line is read into the Literal and `get_provider` branches on it by
    comparing strings: the strings it compares with are the field's."""
    src = open(llm.__file__, encoding="utf-8").read()
    assert re.search(r'extraction_provider\s*==\s*"mistral"', src)
    assert value in AgentSettings.model_fields["extraction_provider"].annotation.__args__
