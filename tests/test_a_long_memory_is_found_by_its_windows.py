"""A vector for each window of a long memory (1.0.33, MEMORY_WINDOWS, off by
default; services/memory_windows.py).

The case, from a team's own memory read with twenty questions of known
answer (17 Nov 2026): the note that answers "does the founder's silence
count as a yes?" was sixth by its one vector, the answer at its character
835 of 867; another note was found and shown by an opening that did not
hold the answer. With the setting on, a memory is ranked by the better of
its own vector and its best window's (Callan, SIGIR 1994, section 2.3; Dai
and Callan, SIGIR 2019, section 3, MaxP), and a memory too long for its
room is shown by the whole sentences of that window. Off, the search and
the block are those of 1.0.31.

These need no database: the windows themselves, the choice of sentences,
the packing. tests/test_memory_windows_go_with_the_words.py has the store.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from korely_agent.services import context, excerpt, memory_windows
from korely_agent.services.context import BYTES_PER_TOKEN
from korely_agent.services.ids import mem_public_id
from korely_agent.services.memory_core import sentences

DAY = datetime(2026, 11, 7, tzinfo=timezone.utc)
FILLER = ("The release went out after the suite passed on the private repository. "
          "The digest was told to the coordinator the same hour. "
          "Nothing else changed in the installer that day. ")
ANSWER = "Before a public push the founder is told and the team waits for the answer: silence is never a yes."
RULES = FILLER * 4 + ANSWER + " " + FILLER


def _window_of_the_answer(text):
    """The window of `text` a question about ANSWER would find: the first
    that holds it whole."""
    at = text.index(ANSWER)
    return next((s, e) for s, e in memory_windows.windows_of(text)
                if s <= at and at + len(ANSWER) - 1 <= e)


# ── the windows ─────────────────────────────────────────────────────────────

def test_a_text_of_one_window_or_less_has_none():
    words = " ".join(f"w{i}" for i in range(memory_windows.WINDOW_WORDS))
    assert memory_windows.windows_of(words) == []
    assert memory_windows.windows_of("") == []
    assert memory_windows.windows_of(None) == []


def test_the_windows_overlap_by_half_and_cover_every_word():
    """Callan 1994, section 2.3: a new window every n/2 words, so a stretch
    cut by one window is whole in the next; the last ends at the last word."""
    size = memory_windows.WINDOW_WORDS
    text = " ".join(f"w{i}." for i in range(size * 3 + 7))
    spans = memory_windows.windows_of(text)
    units = excerpt.units(text)
    starts = [s for s, _e in spans]
    assert starts == [units[i][0] for i in range(0, len(units), memory_windows.STRIDE_WORDS)
                      ][:len(spans)]
    assert spans[-1][1] == units[-1][1]
    for s, e in spans[:-1]:
        assert len(excerpt.units(text[s:e])) == size
    covered = set()
    for s, e in spans:
        covered.update(i for i, (a, b) in enumerate(units) if s <= a and b <= e)
    assert covered == set(range(len(units)))


def test_a_script_without_spaces_is_counted_by_pairs_of_letters():
    """No dictionary and no list of languages (excerpt.units): a script
    written without spaces is counted by pairs of letters next to each
    other (Kwok 1997, the unit services/excerpt.py already uses for them),
    Chinese and Thai alike, and both get windows when long."""
    size = memory_windows.WINDOW_WORDS
    zh = "记忆" * (size + 10)
    th = "ความจำ" * 20
    assert len(excerpt.units(zh)) == size + 10
    assert memory_windows.windows_of(zh)
    letters = sum(1 for ch in th if ch.isalpha())
    assert len(excerpt.units(th)) == (letters + 1) // 2
    # A Latin word glued to ideographs is two runs; an odd letter is alone.
    assert excerpt.units("Korely记忆") == [(0, 6), (6, 8)]
    assert excerpt.units("记忆在") == [(0, 2), (2, 3)]


def test_the_number_of_windows_of_the_longest_memory():
    """The cost of the longest memory (limits.MAX_CONTENT_CHARS), in windows:
    159 for 16,000 ideographs, 39 for 16,000 characters of Latin words of
    seven letters (docs/guide/limits.md says both)."""
    assert len(memory_windows.windows_of("東" * 16000)) == 159
    assert len(memory_windows.windows_of(("abcdefg " * 2000)[:16000])) == 39


def test_the_terms_are_the_ones_of_1_0_31():
    """`terms` was split into `_runs` for `units`: the same terms."""
    for text in ("Before a public push, the founder is told.", "İstanbul and istanbul",
                 "记忆在这里 and Korely记忆", "a­b été"):
        words = [(a, b) for kind, starts, a, b in excerpt._runs(text) if not kind]
        assert [(t[1], t[2]) for t in excerpt.terms(text) if (t[1], t[2]) in words] == words


# ── the choice of sentences ─────────────────────────────────────────────────

def test_the_sentences_of_the_window_come_first():
    text = RULES.strip()
    ends = [e for e in sentences.ends(text) if context.graphemes.may_cut(text, e)]
    spans = excerpt.sentence_spans(text, ends)
    s, e = _window_of_the_answer(text)
    score = [max(0, min(b, e) - max(a, s)) for a, b in spans]
    runs = excerpt.sentences_scored(text, spans, score, 160)
    shown = excerpt.joined(text, runs)
    assert ANSWER in shown and len(shown.encode()) <= 160, shown
    assert excerpt.sentences_scored(text, spans, [0] * len(spans), 160) is None
    assert excerpt.sentences_scored(text, spans, score[:-1], 160) is None


# ── the packing ─────────────────────────────────────────────────────────────

def _pack(monkeypatch, contents, budget, *, windows=(), question="", end_user=None, used=0):
    hits = []
    for i, c in enumerate(contents):
        h = {"id": f"00000000-0000-0000-0000-{i:012d}", "content": c}
        if i in windows:
            h["_window"] = windows[i]
        hits.append(h)
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


@pytest.mark.parametrize("end_user", [None, "maria"], ids=["a-note", "an-end-users-memory"])
def test_a_memory_too_long_for_its_room_shows_its_best_window(monkeypatch, end_user):
    """The first memory, too long for the room: with its window, the
    sentence that answers; without, its opening, as in 1.0.31."""
    window = {0: _window_of_the_answer(RULES.strip())}
    _b, lines, left, _ids = _pack(monkeypatch, [RULES], 120, windows=window, end_user=end_user)
    assert len(lines) == 1 and not left and ANSWER in lines[0], lines
    assert lines[0].startswith("- [2026-11-07] " + excerpt.LEFT_OUT_BEFORE)
    _b, before, _l, _i = _pack(monkeypatch, [RULES], 120, end_user=end_user)
    assert len(before) == 1 and ANSWER not in before[0]


def test_a_note_in_its_share_shows_its_window_before_the_question_words(monkeypatch):
    """A note after the first, in its share: by the window found by meaning,
    even when the question shares no word with the answer (the limit of
    1.0.31's choice by words)."""
    contents = ["The first note is short.", FILLER * 6, RULES]
    # With the setting on every long note has its best window: the second
    # note's is its first.
    window = {1: memory_windows.windows_of((FILLER * 6).strip())[0],
              2: _window_of_the_answer(RULES.strip())}
    _b, lines, left, _ids = _pack(monkeypatch, contents, 300, windows=window,
                                  question="Che cosa vale il silenzio?")
    assert len(lines) == 3 and not left and ANSWER in lines[2], lines
    _b, before, _l, _i = _pack(monkeypatch, contents, 300, question="Che cosa vale il silenzio?")
    assert not any(ANSWER in line for line in before)


def test_never_a_piece_of_a_sentence_and_every_memory_shown_or_named(monkeypatch):
    """The rule of 1.0.30 for every cut, with windows: whole sentences of
    the memory only, at least _LEAST_PIECE of its characters, and a memory
    the block chose and does not show is in `memories_left_out`."""
    contents = ["The first note is short.", RULES, FILLER * 6 + ANSWER, RULES, "A fifth line.",
                "A sixth line."]
    windows = {1: _window_of_the_answer(RULES.strip()), 3: _window_of_the_answer(RULES.strip()),
               2: _window_of_the_answer((FILLER * 6 + ANSWER).strip())}
    for end_user in (None, "maria"):
        for budget in range(60, 800, 19):
            block, lines, left, ids = _pack(monkeypatch, contents, budget, windows=windows,
                                            end_user=end_user)
            assert sorted(block.sources + left) == sorted(ids), budget
            assert block.pack.used <= budget * BYTES_PER_TOKEN
            for line in lines:
                body = line[len("- [2026-11-07] "):]
                if body in [c.strip() for c in contents]:
                    continue
                if body.startswith(excerpt.LEFT_OUT_BEFORE):
                    body = body[len(excerpt.LEFT_OUT_BEFORE):]
                for stretch in body.split(excerpt.LEFT_OUT_BETWEEN):
                    source = next((c.strip() for c in contents if stretch in c), None)
                    assert source is not None, (budget, stretch)
                    ends = [0] + list(sentences.ends(source))
                    wholes = {source[a:b].strip() for i, a in enumerate(ends) for b in ends[i + 1:]}
                    assert stretch in wholes, (budget, stretch)
                assert context._shows_the_least(body), (budget, line)


def test_a_window_that_cannot_be_placed_leaves_the_line_of_1_0_31(monkeypatch):
    """A form feed inside a sentence is a line break for the line
    (memory_core in_its_line), so a sentence's end there, and a space in
    the text: the two count different sentences, so the window is not
    placed and the line is cut as before."""
    text = FILLER.replace("suite passed", "suite\fpassed") + RULES
    window = {0: _window_of_the_answer(text)}
    _b, lines, _l, _i = _pack(monkeypatch, [text], 120, windows=window)
    _b, before, _l2, _i2 = _pack(monkeypatch, [text], 120)
    assert lines == before


def test_without_windows_no_line_is_read_by_a_window(monkeypatch):
    """The setting off gives no hit a window (memory_engine.search fills
    none), and then no line is given one nor cut by one: the lines are the
    1.0.31 ones, which its own tests hold
    (tests/test_the_block_shows_the_sentences_of_a_note_that_answer.py,
    tests/test_benchmark_blocks_unchanged.py)."""
    def never(*a, **k):
        raise AssertionError("a line was read by a window with no window")

    monkeypatch.setattr(context, "_windowed_line", never)
    contents = ["The first note is short.", RULES, FILLER * 6, RULES, FILLER * 5, "A sixth line."]
    for end_user in (None, "maria"):
        for budget in range(60, 900, 37):
            _b, lines, _l, _i = _pack(monkeypatch, contents, budget, end_user=end_user)
            assert all(getattr(x, "about", None) is None for x in lines)


def test_the_setting_is_off_by_default(monkeypatch):
    from korely_agent.config import get_settings

    monkeypatch.delenv("MEMORY_WINDOWS", raising=False)
    get_settings.cache_clear()
    assert get_settings().memory_windows is False
    assert memory_windows.enabled() is False


def test_a_window_never_shows_fewer_than_the_least_piece():
    """A window whose only sentence that fits is shorter than _LEAST_PIECE
    shows nothing (the caller then cuts as in 1.0.31, or names the memory):
    the rule of 1.0.30 for every cut."""
    from korely_agent.services.memory_core import presentation

    short = "Silence: no."
    text = (FILLER * 4 + short + " " + FILLER * 2).strip()
    at = text.index(short)
    window = next((s, e) for s, e in memory_windows.windows_of(text) if s <= at < e)
    row = {"content": text, "observation": None, "event_time": None, "created_at": DAY,
           "relative_times": None}
    line = context._windowed_line(presentation.block_line(row, 0),
                                  presentation.block_line(dict(row, content=""), 0), text, window)
    assert line.about is not None
    weigh = excerpt.weights("Silence?", [text])
    lead = len("- [2026-11-07] ")
    piece = context._in_the_window(line, lead + len(excerpt.LEFT_OUT_BEFORE.encode())
                                   + len(short) + 2, weigh)
    assert piece is None
    piece = context._in_the_window(line, 400, weigh)
    assert piece is not None and short in piece and context._shows_the_least(piece[lead:])
