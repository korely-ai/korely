"""memory_core 2.6.0: the observation and the memories before (2026-10-02).

The write asks, in the same call as the facts, for one short sentence about
the end user, reads the earlier memories for pronouns and the memory's date
for relative times, and leaves general knowledge out. The functions of before
and their prompts stay byte for byte. No model: a fake `call` records the
prompt and answers a fixed JSON.
"""
from __future__ import annotations

import json

import pytest

from korely_agent.services.memory_core import fact_extractor as e
from korely_agent.services.memory_core import presentation as p


class _Fake:
    def __init__(self, answer):
        self.answer = answer
        self.prompt = None
        self.max_tokens = None

    def __call__(self, prompt, max_tokens):
        self.prompt = prompt
        self.max_tokens = max_tokens
        return json.dumps(self.answer)


FACT = {"subject": "u1", "subject_type": "person", "predicate": "redeemed", "object": "a $5 coupon",
         "object_is_literal": True, "tense": "past", "confidence": 0.9}


class TestThePromptsOfBefore:
    def test_extract_does_not_ask_for_the_observation(self):
        fake = _Fake({"facts": []})
        e.extract(fake, "I redeemed a $5 coupon on coffee creamer last Sunday.", "u1")
        assert "observation" not in fake.prompt
        assert "Earlier memories" not in fake.prompt
        assert e.RULES_OBSERVATION.splitlines()[0] not in fake.prompt

    def test_the_templates_of_before_do_not_change(self):
        assert "observation" not in e.PROMPT_SINGLE
        assert "observation" not in e.PROMPT_BATCH
        assert "{PREVIOUS}" not in e.PROMPT_SINGLE and "{PREVIOUS}" not in e.PROMPT_BATCH


class TestOneMemory:
    def test_the_sentence_the_earlier_memories_and_the_date(self):
        fake = _Fake({"observation": "  The user redeemed a $5 coupon on coffee creamer on 2023-05-21. ",
                        "facts": [FACT]})
        out = e.extract_with_observation(
            fake, "I actually redeemed a $5 coupon on coffee creamer last Sunday", "u1",
            previous=["I go to Target every week.", "It has the best creamer."], memory_date="2023-05-22",
        )
        assert out.observation == "The user redeemed a $5 coupon on coffee creamer on 2023-05-21."
        assert [f.predicate_raw for f in out.facts] == ["redeemed"]
        assert '"observation"' in fake.prompt
        assert "Earlier memories of the same end user" in fake.prompt
        assert "- I go to Target every week." in fake.prompt
        assert "Memory (date: 2023-05-22):" in fake.prompt
        assert "general knowledge" in fake.prompt

    def test_without_the_field_is_none_empty_is_the_empty_string(self):
        assert e.extract_with_observation(_Fake({"facts": []}), "ok thanks", "u1").observation is None
        assert e.extract_with_observation(_Fake({"observation": "", "facts": []}), "ok thanks", "u1").observation == ""

    def test_the_sentence_has_a_cap(self):
        long_one = "word " * 300
        out = e.extract_with_observation(_Fake({"observation": long_one, "facts": []}), "text", "u1")
        assert len(out.observation) <= e.MAX_OBSERVATION_CHARS

    def test_at_most_ten_earlier_memories_the_most_recent_like_mem0(self):
        fake = _Fake({"observation": "", "facts": []})
        e.extract_with_observation(fake, "now", "u1", previous=[f"memory {i}" for i in range(14)])
        assert "- memory 13" in fake.prompt and "- memory 4" in fake.prompt
        assert "- memory 3" not in fake.prompt

    def test_every_earlier_memory_cut_at_300_characters(self):
        fake = _Fake({"observation": "", "facts": []})
        e.extract_with_observation(fake, "now", "u1", previous=["x" * 1000])
        assert "- " + "x" * 300 + "\n" in fake.prompt and "x" * 301 not in fake.prompt

    def test_free_text_stays_as_it_is(self):
        fake = _Fake({"observation": "", "facts": []})
        e.extract_with_observation(fake, "my note says {END_USER} and {CONTENT}", "u1",
                                  previous=["earlier {CONTENT} text"])
        assert "my note says {END_USER} and {CONTENT}" in fake.prompt
        assert "earlier {CONTENT} text" in fake.prompt

    def test_empty_text_no_call(self):
        fake = _Fake({"facts": []})
        out = e.extract_with_observation(fake, "   ", "u1")
        assert out.facts == [] and out.observation is None and fake.prompt is None


class TestABatch:
    def test_one_sentence_per_memory_and_the_dates_in_the_blocks(self):
        fake = _Fake({"memories": [
            {"mem_id": "a", "observation": "The user lives in Rome.", "facts": [
                {"subject": "u1", "subject_type": "person", "predicate": "lives_in", "object": "Rome",
                 "object_is_literal": False, "tense": "current", "confidence": 0.9}]},
            {"mem_id": "b", "observation": "", "facts": []},
        ]})
        out = e.extract_batch_with_observation(
            fake, [("a", "I live in Rome."), ("b", "ok thanks")], "u1",
            date={"a": "2023-05-01", "b": "2023-05-02"},
        )
        assert out["a"].observation == "The user lives in Rome."
        assert [f.object for f in out["a"].facts] == ["Rome"]
        assert out["b"].observation == "" and out["b"].facts == []
        assert "[mem_id=a date=2023-05-01]" in fake.prompt
        assert '"observation"' in fake.prompt

    def test_a_single_memory_goes_to_the_single_call(self):
        fake = _Fake({"observation": "The user lives in Rome.", "facts": []})
        out = e.extract_batch_with_observation(fake, [("a", "I live in Rome.")], "u1", date={"a": "2023-05-01"})
        assert out["a"].observation == "The user lives in Rome."
        assert "Memory (date: 2023-05-01):" in fake.prompt

    def test_a_memory_left_out_is_not_there(self):
        fake = _Fake({"memories": [{"mem_id": "a", "observation": "x", "facts": []}]})
        out = e.extract_batch_with_observation(fake, [("a", "one"), ("b", "two")], "u1")
        assert set(out) == {"a"}

    def test_an_answer_without_memories_is_an_error(self):
        with pytest.raises(e.ExtractionFailed):
            e.extract_batch_with_observation(_Fake({"facts": []}), [("a", "one"), ("b", "two")], "u1")


class TestTheMemoryLine:
    def test_without_a_sentence_the_text_of_before(self):
        assert p.memory_line({"content": "I live in Rome."}) == "- I live in Rome."

    def test_with_the_sentence_the_dated_sentence(self):
        r = {"content": "I live in Rome.", "observation": "The user lives in Rome.",
             "created_at": "2023-05-01T10:00:00Z"}
        assert p.memory_line(r) == "- [2023-05-01] The user lives in Rome."

    def test_the_event_time_wins(self):
        r = {"content": "x", "observation": "The user moved.", "event_time": "2023-04-30T23:00:00Z",
             "created_at": "2026-10-02T00:00:00Z"}
        assert p.memory_line(r) == "- [2023-04-30] The user moved."

    def test_empty_sentence_the_start_of_the_text(self):
        """6.17.0: the start is whole sentences of the first 1,200
        characters; a text with no end of sentence in them is cut, after a
        word when it has one, and "..." follows."""
        long_text = "a" * 2000
        assert p.memory_line({"content": long_text, "observation": ""}) == "- " + "a" * 1200 + "..."
        text = "Ab cd. " * 300
        line = p.memory_line({"content": text, "observation": ""})
        assert line.endswith("Ab cd....") and len(line) <= 2 + 1200 + 3
        assert line[2:-3] == ("Ab cd. " * 171).rstrip()

    def test_with_the_excerpt_the_memorys_words_below(self):
        r = {"content": "I live in Rome, near the Pantheon.", "observation": "The user lives in Rome.",
             "created_at": "2023-05-01T10:00:00Z"}
        line = p.memory_line(r, excerpt_chars=12)
        assert line == "- [2023-05-01] The user lives in Rome.\n  said: I live in Ro..."


class TestTheDateOnTheWholeText:
    def test_without_a_sentence_the_text_with_the_day(self):
        """2.8.0: the day in front of every whole memory, even without a sentence."""
        r = {"content": "I live in Rome.", "created_at": "2023-05-01T10:00:00Z"}
        assert p.memory_line(r) == "- [2023-05-01] I live in Rome."

    def test_the_first_of_the_block_whole_with_the_day(self):
        r = {"content": "I visited Lisbon.", "observation": "The user visited Lisbon.",
             "event_time": "2023-06-02T09:00:00Z"}
        assert p.block_line(r, 0) == "- [2023-06-02] I visited Lisbon."
        assert p.block_line(r, p.WHOLE - 1) == "- [2023-06-02] I visited Lisbon."
        assert p.block_line(r, p.WHOLE) == "- [2023-06-02] The user visited Lisbon."


class TestTheStandingInstructions:
    """2.8.0 (work item 3): the rule on standing instructions, measured with no
    gain and gone with 5.0.0: the parameter is no longer there."""

    def test_the_parameter_is_gone(self):
        f = _Fake({"observation": "", "facts": []})
        with pytest.raises(TypeError):
            e.extract_with_observation(f, "Always reply in Italian.", "u1", instructions=True)
        with pytest.raises(TypeError):
            e.extract_batch_with_observation(f, [("m1", "a"), ("m2", "b")], "u1", instructions=True)
        e.extract_with_observation(f, "Always reply in Italian.", "u1", memory_date="2026-10-05")
        assert "STANDING INSTRUCTIONS" not in f.prompt


class TestTheRelativeTimesSentence:
    """2.8.0 (dates: LoCoMo conversation 26 of 5 October): the firmer rule on
    relative times (`absolute_dates`) converted 1 time in 40 and left the code
    with 4.0.0; the sentence of 2.6.0 stays. The events said in passing
    (EVENTS_RULES) were measured worse and removed from the code (3.3.0)."""

    TEXT = "Before the trip I went to Anna's graduation party last Saturday."

    def _prompt(self, **options):
        f = _Fake({"observation": "", "facts": []})
        e.extract_with_observation(f, self.TEXT, "u1", memory_date="2023-05-22", **options)
        return f.prompt

    def test_off_the_prompt_is_the_one_before(self):
        before = self._prompt()
        assert "EVENTS SAID IN PASSING" not in before
        assert 'Turn a relative time ("last Sunday", "two weeks ago") into a date' in before
        # The day to count from is the memory's, which the prompt carries.
        assert "Memory (date: 2023-05-22):" in before

    def test_the_absolute_dates_are_gone(self):
        """4.0.0: the parameter and its constants left the code."""
        assert not hasattr(e, "ABSOLUTE_DATES_SENTENCE") and not hasattr(e, "OBSERVATION_RULES_ABSOLUTE_DATES")
        with pytest.raises(TypeError):
            self._prompt(absolute_dates=True)

    def test_the_batch_has_the_sentence_and_the_dates_in_the_blocks(self):
        f = _Fake({"memories": []})
        e.extract_batch_with_observation(
            f, [("m1", self.TEXT), ("m2", "I returned the blue blazer two weeks ago.")], "u1",
            date={"m1": "2023-05-22", "m2": "2023-06-03"},
        )
        assert e._RELATIVE_DATES_SENTENCE in f.prompt
        assert "[mem_id=m2 date=2023-06-03]" in f.prompt
        assert "EVENTS SAID IN PASSING" not in f.prompt

    EVENT = {"subject": "u1", "subject_type": "person", "predicate": "attended",
              "object": "Anna's graduation party", "object_is_literal": True,
              "tense": "current", "valid_from": "2023-05-20", "confidence": 0.9}

    def test_an_event_that_happened_stays_current_with_its_day(self):
        """Reading the answer does not touch the tense the model gives: the
        event that happened reaches the write as 'current', dated by valid_from
        when the text writes the date (TimeML: an occurrence does not stop
        being true)."""
        f = _Fake({"observation": "The user attended Anna's graduation party on 2023-05-20.",
                    "facts": [self.EVENT]})
        out = e.extract_with_observation(f, "On 2023-05-20 I went to Anna's graduation party.", "u1",
                                        memory_date="2023-05-22")
        [fact] = out.facts
        assert (fact.predicate_raw, fact.tense) == ("attended", "current")
        assert str(fact.valid_from)[:10] == "2023-05-20"

    def test_a_date_counted_from_the_memorys_day_stays_in_the_observation(self):
        """The guard on invented dates (fact_time.date_from_text: the year must
        be written in the text) stays as it is: a date counted from "last
        Saturday" does not become the fact's valid_from, which at write time
        takes the memory's day; the observation carries it."""
        f = _Fake({"observation": "The user attended Anna's graduation party on 2023-05-20.",
                    "facts": [self.EVENT]})
        out = e.extract_with_observation(f, self.TEXT, "u1", memory_date="2023-05-22")
        [fact] = out.facts
        assert fact.valid_from is None
        assert out.observation == "The user attended Anna's graduation party on 2023-05-20."


class TestThe2_8_0Prompts:
    """2.9.0: with the new parameters off the prompts are those of 2.8.0 byte
    for byte. The sha256 fingerprints were computed on 5 October on the 2.8.0
    code (the fact extractor at commit 5da2a66, before any change of 2.9.0), with these
    texts and these options: a prompt that changes by one byte with the
    switches off fails this test.

    4.0.0: the fingerprints of `extract_batch_answers` (the batch without
    observation; PROMPT_BATCH stays the base of the one with the observation,
    pinned by "obs_batch") and of `absolute_dates` are gone, they left the
    code with `turns`; the others stay, unchanged."""

    T1 = "user: I like turbinado sugar in my coffee.\nassistant: Yesterday you said so; try a cast-iron skillet."
    T2 = "I went to a support group yesterday and last week I spoke at a school."
    FINGERPRINTS = {
        "extract": "34a544fe4831cf7336a2d023b54cca31658e371a7a3b882170ef959d13404f21",
        "obs": "b4d95bb077d0d222452059dd8f553052bd5200b038e10655e16a0955fb081e14",
        "obs_batch": "b9b24886948ae855b0bcd7b0e9c1dc38a698ad333ac3ede0a942c0a1b19073e8",
    }
    # The switches of 2.9.0 that remain, explicitly off: the same prompt
    # (`turns`, the other, left with 4.0.0).
    OFF_EXTRACT: dict = {}
    OFF_OBSERVATION: dict = {"times": False}

    @staticmethod
    def _fingerprint(prompt):
        import hashlib
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def _prompt(self, call, answer, *a, **k):
        f = _Fake(answer)
        call(f, *a, **k)
        return self._fingerprint(f.prompt)

    @pytest.mark.parametrize("off", [False, True])
    def test_the_three_extractions(self, off):
        o = self.OFF_OBSERVATION if off else {}
        s = self.OFF_EXTRACT if off else {}
        assert self._prompt(e.extract, {"facts": []}, self.T1, "u1", **s) == self.FINGERPRINTS["extract"]
        assert self._prompt(e.extract_with_observation, {"observation": "", "facts": []}, self.T1, "u1",
                            previous=["earlier one"], memory_date="2023-05-08", **o) == self.FINGERPRINTS["obs"]
        assert self._prompt(e.extract_batch_with_observation, {"memories": []},
                            [("m1", self.T1), ("m2", self.T2)], "u1", previous=["earlier one"],
                            date={"m1": "2023-05-08", "m2": "2023-06-09"}, **o) == self.FINGERPRINTS["obs_batch"]


class TestTheDatesComputedByTheCode:
    """2.9.0 (korely-agent f673ab3): with `times` the model describes
    every relative time of the memory ("relative_times") and the code dates it
    from the memory's day (`relative_time.annotate`); the result travels on
    `ExtractedFact.times`. Off, the prompt is that of 2.8.0 (above) and `times`
    stays None."""

    TEXT = "I went to a support group yesterday and last week I spoke at a school."
    YESTERDAY = {"text": "yesterday", "direction": "last", "unit": "day"}
    LAST_WEEK = {"text": "last week", "direction": "last", "unit": "week"}
    ANSWER = {"observation": "The user went to a support group yesterday.",
                "relative_times": [YESTERDAY, LAST_WEEK], "facts": []}
    TITLE = 'RELATIVE TIMES (mandatory field "relative_times")'

    def _extract(self, answer=None, **options):
        f = _Fake(self.ANSWER if answer is None else answer)
        options.setdefault("memory_date", "2023-05-08")
        out = e.extract_with_observation(f, self.TEXT, "u1", **options)
        return f.prompt, out

    def test_on_the_paragraph_the_field_and_the_observation_sentence(self):
        before, _ = self._extract()
        after, _ = self._extract(times=True)
        assert "RELATIVE TIMES" not in before and '"relative_times"' not in before
        assert "RELATIVE TIMES (mandatory field \"relative_times\")" in after
        assert '\n  "relative_times": [<one object per relative time' in after
        assert e.TIMES_IN_THE_TEXT_SENTENCE in after and e._RELATIVE_DATES_SENTENCE not in after
        # Nothing else changes: remove the field, the paragraph and the sentence, and the prompt is the one before.
        rebuilt = (after.replace("\n  " + e._TIMES_FIELD, "")
                       .replace("\n\n" + e._relative_time.RULES_RELATIVE_TIMES, "")
                       .replace(e.TIMES_IN_THE_TEXT_SENTENCE, e._RELATIVE_DATES_SENTENCE))
        assert rebuilt == before
        # The paragraph comes after the observation rules, before the common rules.
        assert after.index("An empty string when the memory holds nothing") < after.index(self.TITLE)
        assert after.index(self.TITLE) < after.index(e.RULES_ALL.splitlines()[0])

    def test_the_times_dated_from_the_memorys_day(self):
        _, out = self._extract(times=True)
        assert out.observation == "The user went to a support group yesterday."
        assert out.times["v"] == 1 and out.times["anchor"] == "2023-05-08"
        assert [(t["text"], t["value"], t["label"]) for t in out.times["times"]] == [
            ("yesterday", "2023-05-07", "2023-05-07"),
            ("last week", "2023-W18", "the week of 2023-05-01 to 2023-05-07"),
        ]
        assert out.times["times"][0]["expr"] == self.YESTERDAY

    def test_off_no_time_even_if_the_model_sends_them(self):
        _, out = self._extract()
        assert out.times is None

    def test_an_answer_without_the_field_is_not_no_time(self):
        """Nobody answered: None (the column stays empty), not "no relative
        time"; an empty list is the answer "none"."""
        _, without = self._extract({"observation": "x", "facts": []}, times=True)
        _, null = self._extract({"observation": "x", "relative_times": None, "facts": []}, times=True)
        _, empty = self._extract({"observation": "x", "relative_times": [], "facts": []}, times=True)
        _, listed = self._extract([], times=True)
        _, crooked = self._extract({"observation": "x", "relative_times": "none", "facts": []}, times=True)
        assert without.times is None and null.times is None and listed.times is None and crooked.times is None
        assert empty.times == {"v": 1, "anchor": "2023-05-08", "times": []}

    def test_words_that_are_not_in_the_memory_stay_out(self, caplog):
        import logging

        answer = {**self.ANSWER, "relative_times": [self.YESTERDAY, {"text": "tomorrow", "direction": "next",
                                                                       "unit": "day"}, {"text": "last month"}]}
        with caplog.at_level(logging.INFO, logger="korely_agent.services.memory_core.fact_extractor"):
            _, out = self._extract(answer, times=True)
        assert [t["text"] for t in out.times["times"]] == ["yesterday"]
        assert "the words are not in the memory" in caplog.text
        assert "direction is missing" in caplog.text

    @pytest.mark.parametrize("day", [None, "", "8 May 2023"])
    def test_without_the_memorys_day_no_dates(self, day):
        _, out = self._extract(times=True, memory_date=day)
        assert out.times is None
        assert out.observation == "The user went to a support group yesterday."

    def test_the_valid_from_of_the_facts_does_not_take_the_day(self):
        """Out of v1 (decided with 2.9.0): the day counted from
        "yesterday" sits in the memory's column, not in the fact's valid_from;
        the guard on invented dates stays as it is."""
        fact = {"subject": "u1", "subject_type": "person", "predicate": "attended",
                 "object": "a support group", "object_is_literal": True, "tense": "current",
                 "valid_from": "2023-05-07", "confidence": 0.9}
        _, out = self._extract({**self.ANSWER, "facts": [fact]}, times=True)
        [f] = out.facts
        assert f.valid_from is None
        assert out.times["times"][0]["value"] == "2023-05-07"

    def test_in_the_batch_too_every_memory_from_its_own_day(self):
        f = _Fake({"memories": [
            {"mem_id": "a", "observation": "x", "relative_times": [self.YESTERDAY], "facts": []},
            {"mem_id": "b", "observation": "y", "relative_times": [
                {"text": "yesterday", "direction": "last", "unit": "day"}], "facts": []},
            {"mem_id": "c", "observation": "z", "facts": []},
        ]})
        out = e.extract_batch_with_observation(
            f, [("a", "I ran yesterday."), ("b", "Yesterday I swam."), ("c", "I ski.")], "u1",
            date={"a": "2023-05-08", "b": "2024-03-01", "c": "2023-01-01"}, times=True,
        )
        assert out["a"].times["times"][0]["value"] == "2023-05-07"
        assert out["b"].times["times"][0]["value"] == "2024-02-29"
        assert out["c"].times is None
        assert "RELATIVE TIMES" in f.prompt
        assert '\n      "relative_times": [<one object per relative time' in f.prompt
        assert "[mem_id=b date=2024-03-01]" in f.prompt

    def test_the_batch_off_no_times(self):
        f = _Fake({"memories": [{"mem_id": "a", "observation": "x", "relative_times": [self.YESTERDAY], "facts": []},
                                 {"mem_id": "b", "observation": "y", "facts": []}]})
        out = e.extract_batch_with_observation(f, [("a", "I ran yesterday."), ("b", "ok")], "u1",
                                              date={"a": "2023-05-08"})
        assert out["a"].times is None
        assert "RELATIVE TIMES" not in f.prompt

    def test_a_batch_of_one_memory_goes_to_the_single_call_with_the_times(self):
        f = _Fake({"observation": "x", "relative_times": [self.YESTERDAY], "facts": []})
        out = e.extract_batch_with_observation(f, [("a", "I ran yesterday.")], "u1", date={"a": "2023-05-08"},
                                              times=True)
        assert out["a"].times["times"][0]["value"] == "2023-05-07"
        assert "Memory (date: 2023-05-08):" in f.prompt and "RELATIVE TIMES" in f.prompt


class TestTheDatesInTheBlock:
    """2.9.0: a row that carries its relative times (the column of
    `relative_time.annotate`) has the date beside the words, in the whole text
    and in the observation, without ever changing them; without times the row
    is that of 2.8.0 byte for byte (verified on 5 October on the 2.8.0 code,
    0b54d0f, with 2,250 combinations of rows without times: none different)."""

    from korely_agent.services.memory_core import relative_time as _tr

    TEXT = "I went to a support group yesterday and last week I spoke at a school."
    COLUMN = _tr.annotate([{"text": "yesterday", "direction": "last", "unit": "day"},
                          {"text": "last week", "direction": "last", "unit": "week"}],
                         "2023-05-08", text=TEXT)[0]
    WHEN = {"event_time": "2023-05-08T09:00:00Z", "created_at": "2026-10-05T00:00:00Z"}

    @pytest.mark.parametrize("without", [None, {"v": 1, "anchor": "2023-05-08", "times": []}, "broken",
                                       {"v": 1, "anchor": "2023-05-08"}])
    @pytest.mark.parametrize("obs", [None, "", "The user went to a support group yesterday."])
    def test_without_times_the_2_8_0_row(self, without, obs):
        r = {"content": self.TEXT, "observation": obs, **self.WHEN}
        with_times = {**r, "relative_times": without}
        assert p.memory_line(with_times) == p.memory_line(r)
        assert p.memory_line(with_times, excerpt_chars=20) == p.memory_line(r, excerpt_chars=20)
        for pos in (0, p.WHOLE):
            assert p.block_line(with_times, pos) == p.block_line(r, pos)
        if obs is None:
            assert p.memory_line(r) == f"- [2023-05-08] {self.TEXT}"

    def test_the_whole_text_with_the_dates_beside_the_words(self):
        r = {"content": self.TEXT, "observation": None, "relative_times": self.COLUMN, **self.WHEN}
        assert p.memory_line(r) == (
            "- [2023-05-08] I went to a support group yesterday [2023-05-07] and last week "
            "[the week of 2023-05-01 to 2023-05-07] I spoke at a school.")
        # The first of the block, whole, in the same way; the day in front stays.
        r["observation"] = "The user went to a support group yesterday."
        assert p.block_line(r, 0) == p.memory_line({**r, "observation": None})

    def test_the_observation_with_the_dates(self):
        r = {"content": self.TEXT, "observation": "The user went to a support group yesterday.",
             "relative_times": self.COLUMN, **self.WHEN}
        # "last week" is not in the sentence: at the end, once only.
        assert p.block_line(r, p.WHOLE) == (
            "- [2023-05-08] The user went to a support group yesterday [2023-05-07]. "
            "(last week: the week of 2023-05-01 to 2023-05-07)")

    def test_an_observation_in_another_language_has_the_date_at_the_end(self):
        # Deliberately not English: the memory is in Italian, the observation in English.
        text = "Ieri sono andata al gruppo di sostegno."
        column = self._tr.annotate([{"text": "Ieri", "direction": "last", "unit": "day"}], "2023-05-08",
                                  text=text)[0]
        r = {"content": text, "observation": "The user went to a support group yesterday.",
             "relative_times": column, **self.WHEN}
        assert p.memory_line(r) == "- [2023-05-08] The user went to a support group yesterday. (Ieri: 2023-05-07)"
        assert p.block_line(r, 0) == "- [2023-05-08] Ieri [2023-05-07] sono andata al gruppo di sostegno."

    def test_the_memorys_words_do_not_change(self):
        r = {"content": self.TEXT, "observation": None, "relative_times": self.COLUMN, **self.WHEN}
        line = p.memory_line(r)
        import re
        prefix = "- [2023-05-08] "
        assert line.startswith(prefix)
        assert re.sub(r" \[[^\]]*\]", "", line[len(prefix):]) == self.TEXT

    def test_the_empty_observation_the_start_of_the_text_with_the_dates(self):
        # 6.17.0: a second sentence begun with a capital ("ran. xxx" would be
        # one sentence, Unicode's rule SB8).
        long_text = "Yesterday I ran. Then " + "x" * 2000
        column = self._tr.annotate([{"text": "Yesterday", "direction": "last", "unit": "day"}], "2023-05-08",
                                  text=long_text)[0]
        line = p.memory_line({"content": long_text, "observation": "", "relative_times": column, **self.WHEN})
        # 6.17.0: whole sentences of the start, the dates with their words.
        assert line == "- [2023-05-08] Yesterday [2023-05-07] I ran...."

    def test_the_excerpt_below_stays_the_memorys_words(self):
        r = {"content": self.TEXT, "observation": "The user went to a support group yesterday.",
             "relative_times": self.COLUMN, **self.WHEN}
        assert p.memory_line(r, excerpt_chars=40).endswith(f"\n  said: {self.TEXT[:40]}...")

    def test_the_dates_counted_from_the_printed_day(self):
        """The memory changed day after the write (timestamp modified): the
        dates are counted again from the one printed in front, without a model
        (`relative_time.times_of_the_day`)."""
        r = {"content": self.TEXT, "observation": None, "relative_times": self.COLUMN,
             "event_time": "2023-06-01T09:00:00Z"}
        assert p.memory_line(r).startswith(
            "- [2023-06-01] I went to a support group yesterday [2023-05-31] and last week "
            "[the week of 2023-05-22 to 2023-05-28]")
