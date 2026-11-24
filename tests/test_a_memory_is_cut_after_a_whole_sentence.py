"""A memory enters the context block whole, or cut only after a complete
sentence, never inside its first sentence (1.0.30, memory_core 6.17.0).

Until 1.0.29 a memory too long for the room left was cut at the last ". ",
"! ", "? " or line break of the room, and when the room held none, after a
word, with "…": at 400 tokens the first note of a project of long notes
showed its heading and a few words (a review of the team's memory, 13 Nov
2026), and a memory in a script whose full stop is not the Latin one ("。",
"।") was cut after any character. Now where a sentence ends is Unicode's
default sentence boundary (UAX #29 section 5, memory_core `sentences`); a
memory whose first sentence does not fit is left out, the packing goes on
with the next ones, and the answer names every memory left out in
`memories_left_out` (the MCP's text says how many). A cut shows at least
`_LEAST_PIECE` characters, as a reader sees them, ("Dr." alone is no line), and a memory none
of whose sentences ends in its first 1,200 characters is cut after a word
and marked "…", never left out for that alone (the second round of 15 Nov
2026: never mislead, never lose a memory in silence).
"""
import pathlib
import time
import unicodedata
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentMemory
from korely_agent.services import context
from korely_agent.services.context import BYTES_PER_TOKEN, assemble_context
from korely_agent.services.ids import mem_public_id
from korely_agent.services.memory_core import presentation, sentences
from korely_agent.services.memory_engine import AgentMemoryEngine
from tests.conftest import seeded_project_id


# ── where a sentence ends, for every alphabet ──────────────────────────────

def test_the_tables_are_of_the_unicode_version_of_this_python():
    """The lists by code point in memory_core/sentences.py are Unicode
    14.0.0's; the categories and cases come from `unicodedata`. A Python of
    another Unicode version needs the lists read again from its files."""
    assert unicodedata.unidata_version == "14.0.0"


def _cut(text):
    """`text` with " ÷ " at each boundary but the last."""
    ends = sentences.boundaries(text)
    out, start = [], 0
    for end in ends:
        out.append(text[start:end])
        start = end
    return " ÷ ".join(out)


# Cases of SentenceBreakTest-14.0.0.txt (the Unicode Consortium's test of
# UAX #29), by their line in that file: ÷ a boundary, the text as code points.
UNICODE_CASES = [
    # 489: "etc.)’" then NBSP then a lower-case word: no boundary (SB8).
    ("etc.)’ the", [10]),
    # 490: the same before a capital: a boundary after the spaces (SB11).
    ("etc.)’ The", [7, 10]),
    # 481: "U.S.A\u0300. is": full stops between capitals (SB7), a mark
    # that belongs to its letter (SB5), a lower-case word after (SB8).
    ("U.S.A\u0300. is", [10]),
    # 482: the same with "?" before a capital: a boundary (SB11).
    ("U.S.A\u0300? He", [8, 10]),
    # 484: a full stop before a digit (SB6).
    ("3.4", [3]),
]


@pytest.mark.parametrize("text, ends", UNICODE_CASES, ids=["489-sb8", "490-sb11", "481-sb7",
                                                         "482-sb11", "484-sb6"])
def test_the_boundaries_of_the_unicode_test(text, ends):
    assert sentences.boundaries(text) == ends


@pytest.mark.parametrize("text, expected", [
    ("I live in Paris. I like cats.", "I live in Paris.  ÷ I like cats."),
    ("我住在巴黎。我喜欢猫。", "我住在巴黎。 ÷ 我喜欢猫。"),
    ("東京に住んでいます。猫が好きです。", "東京に住んでいます。 ÷ 猫が好きです。"),
    ("मैं पेरिस में रहता हूँ। मुझे बिल्लियाँ पसंद हैं।", "मैं पेरिस में रहता हूँ।  ÷ मुझे बिल्लियाँ पसंद हैं।"),
    ("أعيش في باريس؟ أحب القطط.", "أعيش في باريس؟  ÷ أحب القطط."),
    ("Ապրում եմ Փարիզում։ Սիրում եմ կատուներ։", "Ապրում եմ Փարիզում։  ÷ Սիրում եմ կատուներ։"),
    ("በፓሪስ እኖራለሁ። ድመቶችን እወዳለሁ።", "በፓሪስ እኖራለሁ።  ÷ ድመቶችን እወዳለሁ።"),
    ("Version 1.0.26 is out, e.g. today. Next one soon!", "Version 1.0.26 is out, e.g. today.  ÷ Next one soon!"),
    ("He said “Stop.” Then he left.", "He said “Stop.”  ÷ Then he left."),
    ("first line\nsecond line", "first line\n ÷ second line"),
    ("ฉันอาศัยอยู่ในปารีส ฉันชอบแมว", "ฉันอาศัยอยู่ในปารีส ฉันชอบแมว"),
], ids=["latin", "chinese", "japanese", "hindi", "arabic", "armenian", "amharic", "numbers",
        "quotes", "lines", "thai"])
def test_a_sentence_ends_where_its_script_ends_it(text, expected):
    """The terminators of each script (Sentence_Terminal), the closing
    quotes and the spaces after them in the sentence, a full stop inside a
    number or before a lower-case word no end, a line break an end. Thai
    ends a sentence with a space, which the default rules do not read: one
    sentence, as the annex says."""
    assert _cut(text) == expected


def test_the_longest_whole_sentences_that_fit():
    text = "One two three. Four five six. Seven eight nine."
    assert sentences.whole_sentences(text, lambda p: len(p) <= 30) == "One two three. Four five six."
    assert sentences.whole_sentences(text, lambda p: len(p) <= 13) == ""
    # `least`: a first sentence shorter takes the next with it.
    assert sentences.whole_sentences(text, lambda p: len(p) <= 30, least=20) == \
        "One two three. Four five six."
    assert sentences.whole_sentences(text, lambda p: len(p) <= 20, least=20) == ""


UNICODE_TEST = pathlib.Path(__file__).parent / "data" / "unicode" / "SentenceBreakTest-14.0.0.txt"


def test_every_case_of_the_unicode_sentence_test():
    """All the cases of the Unicode Consortium's own test of UAX #29's
    sentence boundaries, SentenceBreakTest-14.0.0.txt (in tests/data, its
    notice kept), for the Unicode version of this Python's `unicodedata`."""
    assert UNICODE_TEST.read_text(encoding="utf-8").startswith("# SentenceBreakTest-14.0.0.txt")
    cases = wrong = 0
    for line in UNICODE_TEST.read_text(encoding="utf-8").splitlines():
        body = line.split("#")[0].strip()
        if not body:
            continue
        text, ends, pos = "", [], 0
        for t in body.split()[1:]:
            if t == "\u00f7":
                ends.append(pos)
            elif t != "\u00d7":
                text += chr(int(t, 16))
                pos += 1
        cases += 1
        wrong += sentences.boundaries(text) != ends
    assert (cases, wrong) == (502, 0)


# ── the block ──────────────────────────────────────────────────────────────

def _pack(monkeypatch, contents, budget):
    hits = [{"id": f"00000000-0000-0000-0000-{i:012d}", "content": c}
            for i, c in enumerate(contents)]
    monkeypatch.setattr(context, "_memory_rows", lambda db, h: {})
    monkeypatch.setattr(context.experiments, "DIALOGUE_ORDER", False)
    block = context._Block(budget)
    left_out = []
    lines = context._pack_memory_lines(block, context._memory_lines(None, hits, hits), left_out)
    return block, lines, left_out, [mem_public_id(h["id"]) for h in hits]


def _characters(s):
    """How many characters, as a reader sees them, `s` holds (Unicode's
    extended grapheme clusters, the block's own rule, services/graphemes.py)."""
    return sum(1 for i in range(1, len(s) + 1) if i == len(s) or context.graphemes.may_cut(s, i))


def _a_whole_sentence_or_more(line, text):
    """The line is the whole text, or whole sentences of it."""
    return line[2:] in {text[:end].rstrip() for end in sentences.boundaries(text)}


def _a_proper_line(line, text):
    """Whole, whole sentences of at least `_LEAST_PIECE` characters, as a reader sees them,,
    or, for a text with no sentence in its first 1,200 characters, cut and
    marked "…"."""
    if line[2:] == text:
        return True
    if not context._a_sentence_in_the_head(text):
        return (line.endswith("\u2026") and text.startswith(line[2:-1].rstrip())
                and _characters(line[2:-1]) >= context._LEAST_PIECE)
    return _a_whole_sentence_or_more(line, text) and _characters(line[2:]) >= context._LEAST_PIECE


# One sentence of about 800 characters, then a short one: longer than the
# room a memory has at 200 tokens, inside the first 1,200 characters.
LONG_FIRST = ("The decision of the morning, " + ", ".join(
    ["taken after a long discussion with every agent of the team about the release, the "
     "documents, the measures, the costs, the Cloud and the order of the pieces"] * 5)
    + ", is that nothing ships before the founder reads the plan. Then the team went on.")


def test_a_memory_whose_first_sentence_does_not_fit_is_left_out_and_named(monkeypatch):
    """The finding: a long note at a small budget. Its first sentence is
    longer than the room: it is left out, named, and the shorter memory after
    it comes in whole. Until 1.0.29 its first words came, cut after a word."""
    assert 600 < len(LONG_FIRST.split(". ")[0]) < 1200
    block, lines, left_out, ids = _pack(monkeypatch, [LONG_FIRST, "Anna likes tea."], 200)
    assert left_out == [ids[0]]
    assert lines == ["- Anna likes tea."]
    assert block.sources == [ids[1]]


def test_a_memory_left_out_does_not_end_the_packing(monkeypatch):
    """Safe first (the second round, 15 Nov 2026): after a memory shown, one
    left out does not end the memories; the next one that fits comes in."""
    block, lines, left_out, ids = _pack(monkeypatch,
                                        ["Anna likes tea.", LONG_FIRST, "Luca owns a boat."], 200)
    assert lines == ["- Anna likes tea.", "- Luca owns a boat."]
    assert left_out == [ids[1]]


def test_every_memory_not_shown_is_named(monkeypatch):
    """Every memory the block chose is shown or named, none lost in silence."""
    texts = [LONG_FIRST] + [f"Memory number {i} says the plan waits for the founder." for i in range(30)]
    block, lines, left_out, ids = _pack(monkeypatch, texts, 200)
    assert lines and left_out
    assert sorted(block.sources + left_out) == sorted(ids)
    assert not set(left_out) & set(block.sources)


def test_a_short_first_sentence_takes_the_next_with_it(monkeypatch):
    """A cut shows at least `_LEAST_PIECE` characters, as a reader sees them,: "Dr." alone is
    no line (a boundary of the annex after an abbreviation), the next
    sentence comes with it, or the memory is left out."""
    text = ("Dr. Rossi said the dose is five milligrams twice a day, after meals. "
            "The patient must call him if the fever comes back. ") * 6
    for budget in (40, 60, 100, 200):
        block, lines, left_out, ids = _pack(monkeypatch, [text], budget)
        assert lines != ["- Dr."], budget
        for line in lines:
            assert line.startswith("- Dr. Rossi") and _a_proper_line(line, text), (budget, line)
    # A room of 38 bytes holds "Dr." and not the first sentence of 40 or more:
    # nothing, rather than "Dr."; 78 bytes hold the first sentence.
    assert context._whole_sentences_within(text, 10) == ""
    piece = context._whole_sentences_within(text, 20)
    assert piece == "Dr. Rossi said the dose is five milligrams twice a day, after meals."


def test_a_long_memory_is_cut_after_a_whole_sentence(monkeypatch):
    text = " ".join(f"Sentence number {i} is about the weekend away at the lake." for i in range(30))
    block, lines, left_out, ids = _pack(monkeypatch, [text, "Anna likes tea."], 200)
    assert _a_whole_sentence_or_more(lines[0], text) and lines[0] != "- " + text
    # The short one after it, whole if the cut left room for it, else named.
    assert ("- Anna likes tea." in lines) != (left_out == [ids[1]])
    assert block.pack.used <= 200 * BYTES_PER_TOKEN


def test_a_chinese_memory_is_cut_after_its_full_stop(monkeypatch):
    """"。" ends a sentence as "." does: the cut falls after it, never in the
    middle of the next sentence (until 1.0.29, after any character)."""
    text = "我们今天决定先写计划再花钱。" * 40 + "最后一句话。"
    block, lines, left_out, _ids = _pack(monkeypatch, [text], 200)
    assert len(lines) == 1 and _a_whole_sentence_or_more(lines[0], text)
    assert lines[0] != "- " + text and lines[0].endswith("。")


def test_a_block_that_fits_everything_leaves_nothing_out(monkeypatch):
    block, lines, left_out, ids = _pack(monkeypatch, ["Anna likes tea.", "Luca owns a boat."], 800)
    assert lines == ["- Anna likes tea.", "- Luca owns a boat."]
    assert left_out == []


NO_SENTENCE_IN_THE_HEAD = {
    "thai": ("\u0e09\u0e31\u0e19\u0e2d\u0e32\u0e28\u0e31\u0e22\u0e2d\u0e22\u0e39\u0e48"
             "\u0e43\u0e19\u0e1b\u0e32\u0e23\u0e35\u0e2a " * 900)[:16000],
    "unpunctuated": ("a list of things without punctuation one two three four five six " * 260)[:16000],
    "lower_chat": ("ok. then i left. we went home. it was late. " * 400)[:16000],
    "emoji": ("Great job \U0001f44d\U0001f3fd the team shipped it \U0001f389 and we had cake "
              "\U0001f370 " * 300)[:16000],
}


@pytest.mark.parametrize("budget", [100, 200, 400])
@pytest.mark.parametrize("name", sorted(NO_SENTENCE_IN_THE_HEAD))
def test_no_sentence_in_the_head_is_cut_after_a_word_and_marked(monkeypatch, name, budget):
    """The second round's (b): a memory none of whose sentences ends in its
    first 1,200 characters is never left out for that alone: it is cut after
    a word, or after a character, and marked "…", with at least 40
    characters. At 100 tokens the room (86 bytes) holds 40 Latin characters
    and not 40 Thai ones (120 bytes): the Thai memory is left out there, and
    named, for its room, not for its missing full stops."""
    text = NO_SENTENCE_IN_THE_HEAD[name]
    assert not context._a_sentence_in_the_head(text)
    block, lines, left_out, ids = _pack(monkeypatch, [text], budget)
    if (name, budget) == ("thai", 100):
        assert lines == [] and left_out == [ids[0]]
        return
    assert left_out == [] and len(lines) == 1, (name, budget)
    assert _a_proper_line(lines[0], text) and lines[0].endswith("\u2026")
    assert block.pack.used <= budget * BYTES_PER_TOKEN


@pytest.mark.parametrize("budget", [60, 100, 200, 400, 800])
def test_no_memory_is_ever_cut_inside_a_sentence(monkeypatch, budget):
    """Whatever the room, every line is a whole memory, whole sentences of
    it, or the marked cut of a text with no sentence in its head; the block
    keeps to its budget, and every memory is shown or named."""
    texts = [LONG_FIRST, "Anna likes tea. She drinks it at five.", "我住在巴黎。我喜欢猫。" * 30,
             " ".join(f"Point {i} of the plan is written down." for i in range(40)),
             NO_SENTENCE_IN_THE_HEAD["lower_chat"]]
    block, lines, left_out, ids = _pack(monkeypatch, texts, budget)
    by_id = dict(zip(ids, texts))
    for line, mid in zip(lines, block.sources):
        assert _a_proper_line(line, by_id[mid]), (budget, line[:80])
    assert block.pack.used <= budget * BYTES_PER_TOKEN
    assert sorted(block.sources + left_out) == sorted(ids)


@pytest.mark.parametrize("text", [
    "A long first sentence that says enough of it, here! \u06001 then more words that follow.",
    "A long first sentence that says enough of it, here!\u06001 then more words that follow.",
], ids=["after-a-space", "after-the-mark"])
def test_a_prepend_sign_never_ends_a_cut(text):
    """The annex's boundary falls before "1", after U+0600 ARABIC NUMBER
    SIGN, which belongs to the number (UAX #29 GB9b, a Prepend sign): the cut
    leaves the sign out and ends after "here!" (services/graphemes.py reads
    GB9b since 1.0.30)."""
    piece = context._whole_sentences_within(text, 15)
    assert piece == "A long first sentence that says enough of it, here!"
    assert context.graphemes.may_cut(text, len(piece))


def test_the_first_memory_floor_is_its_first_sentence(monkeypatch):
    """The changes' room keeps what the first memory needs: its heading and
    its first whole sentence, not a fragment of it, read from the first
    boundary."""
    monkeypatch.setattr(context, "_memory_rows", lambda db, h: {})
    monkeypatch.setattr(context.experiments, "DIALOGUE_ORDER", False)
    text = "Anna likes tea and drinks it every afternoon at five. " + "Then more. " * 50
    hits = [{"id": "00000000-0000-0000-0000-000000000001", "content": text}]
    block = context._Block(800)
    lines = context._memory_lines(None, hits, hits)
    first = "Anna likes tea and drinks it every afternoon at five."
    assert context._first_memory_floor(block, lines) == \
        1 + block.before_the_first_memory() + -(-(context._cost(first) + 2) // 4) * 4


# Texts with no sentence end in their head: every cut of them shows at
# least `_LEAST_PIECE` characters, then the ellipsis, as a cut after a
# sentence does.
MARKED = {
    "thai_words": ("\u0e09\u0e31\u0e19\u0e2d\u0e32\u0e28\u0e31\u0e22\u0e2d\u0e22\u0e39\u0e48"
                   "\u0e43\u0e19\u0e1b\u0e32\u0e23\u0e35\u0e2a\u0e21\u0e32\u0e2a\u0e34\u0e1a"
                   "\u0e1b\u0e35\u0e41\u0e25\u0e49\u0e27 ") * 60,
    "thai_nospace": "\u0e01" * 3000,
    "cjk_nospace": "\u6771\u4eac\u90fd" * 800,
    "latin_nopunct": "alpha beta gamma delta epsilon zeta eta theta iota kappa " * 40,
    "latin_longword": "supercalifragilistic" * 100,
    "short_then_long_word": "hi " + "x" * 3000,
    "dot_then_spaces": "A." + " " * 3000 + "x",
}


@pytest.mark.parametrize("name", sorted(MARKED))
def test_every_cut_shows_at_least_forty_characters(monkeypatch, name):
    """At every budget from 11 to 119 tokens the memory is whole, or cut and
    marked with at least `_LEAST_PIECE` characters, as a reader sees them, before the
    ellipsis, or left out and named: never 15 characters and "…" (the
    re-check's finding: the marked cut counted 40 bytes)."""
    text = MARKED[name]
    for budget in range(11, 120, 9):
        block, lines, left_out, ids = _pack(monkeypatch, [text, "Anna likes tea."], budget)
        mine = [line for line, mid in zip(lines, block.sources) if mid == ids[0]]
        assert mine or ids[0] in left_out, (name, budget)
        for line in mine:
            assert line == "- " + text or (line.endswith("\u2026")
                                           and _characters(line[2:-1]) >= context._LEAST_PIECE), \
                (name, budget, line[:60])


# Texts where code points and characters as a reader sees them differ:
# "e" and a combining accent, Thai with its marks, a family emoji of four
# people joined, pairs of regional indicators, a Prepend sign before a
# letter.
CLUSTERS = {
    "decomposed": "e\u0301" * 3000,
    "decomposed_words": "e\u0301e\u0301e\u0301 " * 600,
    "thai_marks": "\u0e01\u0e34\u0e49" * 1500,
    "family_zwj": "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466" * 700,
    "family_zwj_words": "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466 " * 700,
    "ri_pairs": "\U0001f1e6\U0001f1e7" * 1500,
    "prepend_letter": "\u0600a" * 1500,
    "latin": "alpha beta gamma delta epsilon zeta " * 60,
}


@pytest.mark.parametrize("name", sorted(CLUSTERS))
def test_every_cut_counts_characters_as_a_reader_sees_them(monkeypatch, name):
    """At every budget from 11 to 119 tokens, a cut of the memory shows at
    least `_LEAST_PIECE` characters as a reader sees them, not code points:
    in a first form "e" and its accent counted two, and a cut showed
    21 characters at 96 tokens, a family emoji 6."""
    text = CLUSTERS[name]
    for budget in range(11, 120):
        block, lines, left_out, ids = _pack(monkeypatch, [text, "Anna likes tea."], budget)
        mine = [line for line, mid in zip(lines, block.sources) if mid == ids[0]]
        assert mine or ids[0] in left_out, (name, budget)
        for line in mine:
            body = line[2:]
            if body == text:
                continue
            core = body[:-1] if body.endswith("\u2026") else body
            assert text.startswith(core), (name, budget)
            assert _characters(core.rstrip()) >= context._LEAST_PIECE, (name, budget, _characters(core))


# The first memory's floor is the room in which the block shows it, and not
# one byte less (without these cases three mutants of `_shortest_piece`
# and `_on_a_character` were not caught). Spaces at the 40th character,
# then a letter with its accent; a family emoji; letters with accents; Thai.
FLOORS = {
    "spaces_then_accent": "a" * 30 + " " * 20 + "e\u0301" * 1000,
    "family_zwj": "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466" * 300,
    "decomposed": "e\u0301" * 2000,
    "thai_marks": "\u0e01\u0e34\u0e49" * 1500,
    "prepend_after_a_sentence": ("A long first sentence that says enough of it, here!\u06001 then "
                                 "more words that follow. ") * 20,
}


@pytest.mark.parametrize("name", sorted(FLOORS))
@pytest.mark.parametrize("budget", [100, 200, 400])
def test_the_floor_is_the_room_that_shows_the_first_memory(monkeypatch, name, budget):
    """With exactly the floor's room left, the first memory is shown; with a
    byte less, it is not: the floor is what the packing needs, counted the
    same way (characters as a reader sees them, a space run skipped, a cut
    that never ends inside a character or on a Prepend sign)."""
    monkeypatch.setattr(context, "_memory_rows", lambda db, h: {})
    monkeypatch.setattr(context.experiments, "DIALOGUE_ORDER", False)
    hits = [{"id": "00000000-0000-0000-0000-000000000001", "content": FLOORS[name]}]
    lines = context._memory_lines(None, hits, hits)
    floor = context._first_memory_floor(context._Block(budget), lines)
    for room, shown in ((floor, 1), (floor - 1, 0)):
        b = context._Block(budget)
        if room > b.pack.cap:
            continue
        b.pack.used = b.pack.cap - room
        assert len(context._pack_memory_lines(b, lines)) == shown, (name, budget, room, floor)


# ── hostile texts take milliseconds (a review of 15 Nov 2026) ──────────────

HOSTILE = {
    "spaces": "I live in Paris." + " " * 15982 + "x",
    "closing": "I live in Paris." + ")" * 15982 + " x",
    "full_stops": ". " * 8000,
    "no_stop": "x" * 16000,
    # One character as a reader sees it, 16,000 code points long (the third
    # review of 15 Nov 2026: the floor searched token by token and took 9 s
    # and 26 s).
    "combining": "a" + "\u0301" * 15999,
    "joined": "\U0001f468\u200d" * 8000,
}


@pytest.mark.serial  # a bound of time: alone on the machine, not beside the parallel suite
@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_a_hostile_text_of_the_longest_length_is_read_in_milliseconds(monkeypatch, name):
    """The first form of the splitter walked back over the left context and
    forward for SB8 at every position: "I live in Paris." then 15,900 spaces
    took 204 s for the floor, and "." then 8,000 ")" 48 s. One pass now:
    measured 3 to 8 ms on 16,000 characters in the test image; the bound is
    a hundred times the measure, a hundred times below the old one."""
    from korely_agent.services import limits

    text = HOSTILE[name]
    assert len(text) <= limits.MAX_CONTENT_CHARS
    sentences.ends.cache_clear()
    t0 = time.perf_counter()
    sentences.boundaries(text)
    _line(text)
    block, lines, left_out, ids = _pack(monkeypatch, [text, "Anna likes tea."], 800)
    context._first_memory_floor(context._Block(800), context._memory_lines(
        None, [{"id": ids[0], "content": text}], [{"id": ids[0], "content": text}]))
    assert time.perf_counter() - t0 < 0.5, name


# ── the answer ─────────────────────────────────────────────────────────────

VEC = [0.1] * EMBEDDING_DIM


def _stored(db, tenant, monkeypatch, contents):
    monkeypatch.setattr(context, "embed", lambda *a, **k: VEC)
    project = seeded_project_id(db, tenant.id)
    rows = [AgentMemory(tenant_id=tenant.id, project_id=project, end_user_id="u1",
                        graph_sync_status="done", content=c) for c in contents]
    db.add_all(rows)
    db.commit()
    found = [{"id": str(m.id), "content": m.content} for m in rows]
    monkeypatch.setattr(AgentMemoryEngine, "search", lambda self, *a, **k: found)
    return project, rows


def test_the_answer_names_the_memories_left_out(db, tenant, monkeypatch):
    """assemble_context answers `memories_left_out` (REST's `ContextResponse`
    carries it): the long note at 400 tokens is named, the short one shown."""
    from korely_agent.api.context import ContextResponse

    project, rows = _stored(db, tenant, monkeypatch, [LONG_FIRST, "Anna likes tea."])

    out = assemble_context(db, tenant.id, query="the decision", end_user_id="u1",
                           token_budget=400, project_id=project)

    assert out["memories_left_out"] == [mem_public_id(rows[0].id)]
    assert mem_public_id(rows[1].id) in out["sources"]
    assert "Anna likes tea." in out["context"] and "The decision of the morning" not in out["context"]
    assert ContextResponse(**out).memories_left_out == out["memories_left_out"]


def _mcp_context(key: str, **kw) -> str:
    import asyncio

    from korely_agent.mcp import server as mcp_server

    async def go():
        await mcp_server.KorelyAgentKeyVerifier().verify_token(key)
        tool = mcp_server._get_agent_mcp()._tool_manager._tools["korely_get_context"].fn
        return await tool(**kw)

    return asyncio.run(go())


def test_the_mcp_text_says_how_many_memories_were_left_out(db, tenant, monkeypatch):
    """The MCP answers the block's text alone: when memories were left out
    it says how many after the block, and when nothing else fits it says so
    in place of the block, never "nothing remembered yet"."""
    from korely_agent.services import keys as key_service

    _stored(db, tenant, monkeypatch, [LONG_FIRST, "Anna likes tea."])
    _row, key = key_service.create_api_key(db, tenant_id=tenant.id, name="mcp-left")

    text = _mcp_context(key, query="the decision", user_id="u1", token_budget=400)
    assert "Anna likes tea." in text
    assert text.endswith(context.left_out_note(1))

    _stored(db, tenant, monkeypatch, [LONG_FIRST])
    text = _mcp_context(key, query="the decision", user_id="u1", token_budget=100)
    assert "nothing remembered" not in text
    assert context.left_out_note(1) in text or context.left_out_note(2) in text


# ── the engine's line of a memory with an empty observation ────────────────

def _line(text):
    r = SimpleNamespace(content=text, observation="", event_time=None, relative_times=None,
                        created_at=datetime(2026, 11, 15, tzinfo=timezone.utc))
    return presentation.memory_line(r)


def test_the_start_of_a_long_memory_is_whole_sentences():
    """memory_core 6.17.0: the start of a memory with an empty observation
    is the whole sentences of its first 1,200 characters, then "...";
    until 6.16.0 its first 1,200 characters, cut inside a word."""
    text = " ".join(f"Sentence {i} of a long turn about the trip." for i in range(60))
    line = _line(text)
    assert line.startswith("- [2026-11-15] ") and line.endswith("...")
    shown = line[len("- [2026-11-15] "):-3]
    assert shown in {text[:end].rstrip() for end in sentences.boundaries(text)}
    assert len(shown) <= 1200


def test_a_first_sentence_longer_than_the_start_is_cut_after_a_word():
    """The second round's (b): with no sentence end in the first 1,200
    characters the start is cut after a word, then "...", never the whole
    first sentence (16,000 characters in one line) nor a cut inside a word."""
    text = "word " * 400 + "end. And a second sentence."
    shown = _line(text)[len("- [2026-11-15] "):-3]
    assert len(shown) <= 1200 and text.startswith(shown) and shown.endswith("word")
