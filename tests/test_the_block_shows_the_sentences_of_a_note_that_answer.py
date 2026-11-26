"""A note too long for its share is shown by the whole sentences the
question is about, and the notes after the first share the room (1.0.31).

The case, from a team's own memory read with twenty questions of known
answer (16 Nov 2026): at the default 800 tokens the block showed one note
whole and the opening of another; the note with the answer was third to
fifth by the search, its answer in the middle or at the end of it. The rule
and its sources are in services/excerpt.py (Tombros and Sanderson, SIGIR
1998; Turpin et al., SIGIR 2007; Sparck Jones, 1972; Kwok, 1997).

It keeps the rule of 1.0.30 for every cut of a memory: whole sentences
only, at least `_LEAST_PIECE` of the memory's own characters, and a memory
the block chose and does not show is named. A memory of an end user is
packed as in 1.0.30 (tests/test_benchmark_blocks_unchanged.py).
"""
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from korely_agent.services import context, excerpt
from korely_agent.services.context import BYTES_PER_TOKEN
from korely_agent.services.ids import mem_public_id
from korely_agent.services.memory_core import sentences

DAY = datetime(2026, 11, 7, tzinfo=timezone.utc)
FILLER = ("The release went out after the suite passed on the private repository. "
          "The digest was told to the coordinator the same hour. "
          "Nothing else changed in the installer that day. ")
ANSWER = "Before a public push the founder is told and the team waits for the answer: silence is never a yes."
RULES = FILLER * 3 + ANSWER + " " + FILLER * 2
QUESTION = "Does the founder's silence count as a yes for a public push?"


def _pack(monkeypatch, contents, budget, *, question=QUESTION, end_user=None, used=0,
          superseded=()):
    hits = [{"id": f"00000000-0000-0000-0000-{i:012d}", "content": c,
             **({"_superseded": True} if i in superseded else {})}
            for i, c in enumerate(contents)]
    rows = {h["id"]: SimpleNamespace(observation=None, event_time=None, relative_times=None,
                                     created_at=DAY, end_user_id=end_user) for h in hits}
    monkeypatch.setattr(context, "_memory_rows", lambda db, h: rows)
    monkeypatch.setattr(context.experiments, "DIALOGUE_ORDER", False)
    block = context._Block(budget)
    block.pack.take(used)
    left_out = []
    lines = context._pack_memory_lines(block, context._memory_lines(None, hits, hits),
                                       left_out, question)
    return block, lines, left_out, [mem_public_id(h["id"]) for h in hits]


def _characters(s):
    return sum(1 for i in range(1, len(s) + 1) if i == len(s) or context.graphemes.may_cut(s, i))


def _note(text, lead="- [2026-11-07] "):
    note = context._Note(lead + text)
    note.lead = len(lead)
    return note


def _own_words(shown, lead="- [2026-11-07] "):
    """The memory's own words of a line: without what leads it and the marks
    of sentences left out."""
    body = shown[len(lead):]
    if body.startswith(excerpt.LEFT_OUT_BEFORE):
        body = body[len(excerpt.LEFT_OUT_BEFORE):]
    return body.split(excerpt.LEFT_OUT_BETWEEN)


def _whole_sentences_of(text):
    """Every stretch of `text` from a sentence's start to a sentence's end."""
    ends = [0] + list(sentences.ends(text))
    return {text[a:b].strip() for i, a in enumerate(ends) for b in ends[i + 1:]}


# ── the block ───────────────────────────────────────────────────────────────

def test_the_third_note_shows_the_sentence_that_answers(monkeypatch):
    """A short first note and two long ones in a room that held one of them:
    both are in the block, and the one with the answer by that sentence."""
    block, lines, left_out, _ids = _pack(
        monkeypatch, ["The first note is short.", FILLER * 6, RULES], 300)
    assert len(lines) == 3 and not left_out, (lines, left_out)
    assert ANSWER in lines[2] and lines[2].startswith("- [2026-11-07] ")
    assert excerpt.LEFT_OUT_BETWEEN in lines[2] or excerpt.LEFT_OUT_BEFORE in lines[2], \
        "the mark of the sentences left out"
    assert block.pack.used <= 300 * BYTES_PER_TOKEN


def test_the_block_of_1_0_30_showed_the_opening_of_that_note(monkeypatch):
    """The same block with no question to read by: the cut of 1.0.30, the
    note's first sentences, which do not hold the answer."""
    _b, lines, _l, _i = _pack(monkeypatch, ["The first note is short.", FILLER * 6, RULES], 300,
                              question="")
    assert not any(ANSWER in line for line in lines)


def test_a_note_is_never_shown_by_a_piece_of_a_sentence(monkeypatch):
    """At every room, each stretch of a note's own words is whole sentences
    of it (the rule of 1.0.30 for every cut of a memory)."""
    contents = ["The first note is short.", RULES, FILLER * 2 + ANSWER, RULES + " And one more sentence here."]
    for budget in range(120, 700, 23):
        _b, lines, _l, ids = _pack(monkeypatch, contents, budget)
        for line in lines[1:]:
            source = next(c for c in contents if _own_words(line)[0] in c)
            wholes = _whole_sentences_of(source.strip())
            for stretch in _own_words(line):
                assert stretch in wholes, (budget, stretch)


def test_every_memory_is_shown_or_named(monkeypatch):
    """None lost in silence: a memory the block chose and does not show is in
    `memories_left_out`, at every room (the short lines after the notes too)."""
    contents = ["The first note is short.", RULES, FILLER * 6, RULES, FILLER * 5,
                "A sixth line.", "A seventh line."]
    for budget in range(100, 900, 31):
        block, lines, left_out, ids = _pack(monkeypatch, contents, budget)
        assert sorted(block.sources + left_out) == sorted(ids), budget
        assert len(block.sources) == len(lines)
        assert block.pack.used <= budget * BYTES_PER_TOKEN


def test_a_memory_of_an_end_user_is_packed_as_in_1_0_30(monkeypatch):
    """The same texts as an end user's turns, with and without a question:
    the same lines, none read by the question."""
    contents = ["The first note is short.", FILLER * 6, RULES]
    for budget in (200, 300, 500):
        _b, asked, lo_a, _i = _pack(monkeypatch, contents, budget, end_user="u-1")
        _b, plain, lo_p, _i = _pack(monkeypatch, contents, budget, end_user="u-1", question="")
        assert asked == plain and lo_a == lo_p
        assert not any(excerpt.LEFT_OUT_BEFORE in line for line in asked)


def test_the_first_note_is_whole_as_it_always_was(monkeypatch):
    _b, lines, _l, _i = _pack(monkeypatch, [RULES, FILLER * 6, FILLER * 5], 400)
    assert lines[0] == "- [2026-11-07] " + RULES.strip()


def test_a_note_that_fits_its_share_is_whole_and_so_are_all_with_room(monkeypatch):
    _b, lines, _l, _i = _pack(monkeypatch, ["A short first note.", "A short second one.", RULES], 400)
    assert lines[1] == "- [2026-11-07] A short second one."
    contents = [FILLER * 2, RULES, FILLER]
    _b, notes, _l, _i = _pack(monkeypatch, contents, 2000)
    assert notes == ["- [2026-11-07] " + c.strip() for c in contents]


def test_no_word_of_the_question_in_the_note_packs_it_as_before(monkeypatch):
    """A question in other words than the notes' chooses no sentence: the
    block of 1.0.30."""
    contents = ["A short first note.", FILLER * 6, RULES]
    _b, asked, lo_a, _i = _pack(monkeypatch, contents, 250, question="¿Dónde está el servidor?")
    _b, plain, lo_p, _i = _pack(monkeypatch, contents, 250, question="")
    assert asked == plain and lo_a == lo_p


def test_the_mark_of_a_note_changed_later_stays_in_front(monkeypatch):
    _b, lines, _l, _i = _pack(monkeypatch, ["A short first note.", RULES, FILLER * 6], 220,
                              superseded=(1,))
    assert lines[1].startswith("- [2026-11-07] (true when said, changed later) ")
    assert ANSWER in lines[1] and len(lines[1]) < len(RULES)


# ── a note in its share ─────────────────────────────────────────────────────

def test_the_share_is_never_passed_whatever_the_note_begins_with():
    """A note that begins with signs and no word, at every share: the line
    is within the share or not shown (a share of 312 bytes showed 606)."""
    weigh = excerpt.weights(QUESTION, [RULES])
    for opening in ("", "——— ::: ——— ", "…… ", "«» ", "12) "):
        note = _note(opening + RULES)
        for share in range(0, 900, 7):
            shown = context._note_in_its_share(note, share, weigh)
            assert shown is None or context._cost(shown) <= share, (opening, share)


def test_a_cut_shows_at_least_forty_of_the_memory_s_own_characters():
    """Counted on the note's words, not on its day or its marks: a sentence
    of the question shorter than that comes with its neighbours or not at
    all."""
    text = FILLER + "Silence: no. " + FILLER * 3
    weigh = {"silence": 1.0}
    note = _note(text, "- [2026-11-07] (true when said, changed later) ")
    seen_some = False
    for share in range(0, 700, 5):
        shown = context._note_in_its_share(note, share, weigh)
        if shown is None or shown == str(note):
            continue
        seen_some = True
        words = "".join(_own_words(shown, "- [2026-11-07] (true when said, changed later) "))
        assert _characters(words) >= context._LEAST_PIECE, (share, shown)
    assert seen_some
    # A share that holds the short sentence and its marks, and nothing next to it.
    lead = "- [2026-11-07] "
    tight = context._cost(lead + excerpt.LEFT_OUT_BEFORE + "Silence: no.") + 4
    assert context._note_in_its_share(_note(text), tight, weigh) is None


def test_a_sentence_end_inside_a_character_is_no_end():
    """U+0600 belongs to the character after it (GB9b); the annex's sentence
    boundary falls between the two. The note is never begun there."""
    text = FILLER + "And then. ؀Silence is never a yes for a public push of ours. " + FILLER * 2
    assert any(not context.graphemes.may_cut(text, e) for e in sentences.ends(text)), \
        "the case: a sentence boundary that is no boundary of characters"
    weigh = {"silence": 1.0}
    for share in range(60, 500, 9):
        shown = context._note_in_its_share(_note(text), share, weigh)
        if shown and "Silence" in shown and shown != "- [2026-11-07] " + text:
            assert "؀Silence" in shown, share
            assert not _own_words(shown)[0].startswith("Silence"), share


# ── services/excerpt.py ─────────────────────────────────────────────────────

def _about(text, weigh, most):
    return excerpt.sentences_about(text, sentences.ends(text), weigh, most)


def test_the_sentences_with_the_most_of_the_question_come_first():
    weigh = excerpt.weights(QUESTION, [RULES, FILLER, FILLER * 2])
    runs = _about(RULES, weigh, 140)
    assert [RULES[a:b] for a, b in runs] == [ANSWER]
    wider = excerpt.joined(RULES, _about(RULES, weigh, 400))
    assert ANSWER in wider and len(wider.encode()) <= 400
    # One word of the question, in one sentence: the sentences next to it
    # take the room left, the ones after it first, and the mark says that
    # sentences before them were left out.
    alone = excerpt.joined(RULES, _about(RULES, {"silence": 1.0}, 300))
    assert alone.startswith(excerpt.LEFT_OUT_BEFORE + ANSWER + " The release went out")
    assert len(alone.encode()) <= 300 and alone.endswith(".")


def test_two_sentences_apart_are_joined_with_the_mark():
    text = "Alpha is the first word here. " + FILLER + "Omega is the last word here. " + FILLER
    runs = _about(text, {"alpha": 1.0, "omega": 1.0}, 75)
    assert excerpt.joined(text, runs) == "Alpha is the first word here. … Omega is the last word here."


def test_a_word_said_twice_in_a_sentence_counts_once():
    """A sentence scores by the distinct words of the question it holds
    (Tombros and Sanderson, section 2.4: the query terms included in the
    sentence): one word repeated does not beat two different ones."""
    twice = "Silence, silence, silence all day."   # shorter: it fits the room alone too
    both = "The founder kept his silence that whole morning."
    text = FILLER + twice + " " + FILLER + both + " " + FILLER
    runs = _about(text, {"silence": 1.0, "founder": 0.9}, len(both.encode()) + 4)
    assert [text[a:b] for a, b in runs] == [both]


def test_no_sentence_of_the_question_chooses_nothing():
    assert _about(FILLER, {"silence": 1.0}, 200) is None
    assert _about("", {"silence": 1.0}, 200) is None and _about(RULES, {}, 200) is None
    assert _about(RULES, {"silence": 1.0}, 0) is None
    assert _about(RULES, {"silence": 1.0}, 30) is None, "the sentence does not fit: no piece of it"


def test_a_word_every_text_holds_weighs_least_and_none_is_negative():
    weigh = excerpt.weights("the founder silence", ["the founder said", "the silence", "the push"])
    assert weigh["the"] < weigh["founder"] and weigh["the"] < weigh["silence"]
    assert all(v > 0 for v in weigh.values())
    assert "push" not in weigh, "only the question's words"
    assert excerpt.weights("zebra", ["the founder"]) == {}


def test_words_of_every_script():
    def forms(s):
        return [t for t, _a, _b in excerpt.terms(s)]

    assert forms("Тишина — не «да». हाँ नहीं") == ["тишина", "не", "да", forms("हाँ")[0], forms("नहीं")[0]]
    assert forms("STRASSE straße")[0] == forms("STRASSE straße")[1]
    assert forms("Ｆｕｌｌ") == ["full"]
    assert forms("İstanbul") == forms("istanbul") == forms("ISTANBUL")
    assert forms("مُحَمَّد") == forms("محمد"), "an Arabic word with and without its vowel marks"
    assert forms("می‌خواهم") == forms("میخواهم"), "a zero width non-joiner inside a word"
    assert forms("記憶システム") == ["記憶", "憶シ", "シス", "ステ", "テム"], "pairs of characters, no dictionary"
    assert forms("Korely記憶") == ["korely", "記憶"], "a Latin name glued to ideographs is two"
    assert forms("ภาษาไทย")[:2] == ["ภา", "าษ"] and len(forms("ภาษาไทย")) == 6
    assert forms("水") == ["水"], "a character with no neighbour"
    for term, a, b in excerpt.terms("Korely記憶システム می‌خواهم ภาษาไทย"):
        assert a < b


def test_a_question_in_chinese_chooses_the_sentence_in_chinese():
    filler = "发布在私有仓库的测试通过之后完成。摘要在同一小时告诉了协调人。那天安装程序没有其他变化。"
    answer = "公开推送之前要先告诉创始人并等待答复，沉默不等于同意。"
    text = filler * 2 + answer + filler
    weigh = excerpt.weights("创始人的沉默算同意吗", [text, filler])
    runs = _about(text, weigh, len(answer.encode()) + 8)
    assert [text[a:b] for a, b in runs] == [answer]


def test_a_question_in_thai_and_in_turkish_choose_their_sentences():
    thai = "ระบบถูกปล่อยหลังจากทดสอบผ่านแล้ว. " * 3 + "ความเงียบของผู้ก่อตั้งไม่ใช่การอนุมัติ. " + "ไม่มีอะไรเปลี่ยนแปลงในวันนั้น. " * 3
    runs = _about(thai, excerpt.weights("ความเงียบของผู้ก่อตั้ง", [thai, "ระบบ"]), 130)
    assert "ความเงียบของผู้ก่อตั้งไม่ใช่การอนุมัติ." in excerpt.joined(thai, runs)
    turkish = FILLER + "Sunucu İSTANBUL bölgesinde çalışıyor ve başka yerde değil. " + FILLER
    runs = _about(turkish, excerpt.weights("istanbul sunucu", [turkish, FILLER]), 90)
    assert "İSTANBUL" in excerpt.joined(turkish, runs)


HOSTILE = {
    "tiny sentences": "a. " * 5300 + "then silence is never a yes.",
    "one terminator and spaces": "Silence." + " " * 15900 + "then silence is never a yes.",
    "a letter and marks": "a" + "́" * 15900 + ". silence is never a yes.",
    "line breaks": "silence\n" * 2000,
    "ideographs": "沉默" * 8000,
    "signs only": "—" * 16000,
}


@pytest.mark.serial  # a bound of time: alone on the machine, not beside the parallel suite
@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_a_hostile_note_of_the_longest_length_is_read_in_milliseconds(name):
    """16,000 characters, the longest a memory can be: every pass is over
    the text once (the first form walked back over the spaces before every
    word: 1.5 s for one note)."""
    text = HOSTILE[name]
    weigh = {"silence": 1.0, "yes": 1.0, "沉默": 1.0}
    t0 = time.perf_counter()
    for share in (120, 600, 4000):
        shown = context._note_in_its_share(_note(text), share, weigh)
        assert shown is None or context._cost(shown) <= share
    assert time.perf_counter() - t0 < 1.0, name
