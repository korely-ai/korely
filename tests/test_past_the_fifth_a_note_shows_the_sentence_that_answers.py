"""After the first WHOLE memories of the block, a note of no end user is shown
by its own sentences that hold the question's words, in the bytes of its
observation's line, instead of the observation (1.0.35).

The case, from a team's own memory read with twenty questions of known
answer (19 Nov 2026): the note with the answer was sixth by the search, and
the block showed it by its observation, an English summary written by the
model ("The writer defines strict rules for ..."), which had dropped the
very sentence that answered. The rule of 1.0.31 for notes
(`_note_in_its_share`: Tombros and Sanderson, SIGIR 1998, section 2.4;
Turpin et al., SIGIR 2007, section 4) now also chooses what such a note
shows, in at most the bytes of the line it replaces; the observation stays
when no sentence holds a word of the question.

A memory of an end user is shown by its observation as before
(tests/test_benchmark_blocks_unchanged.py keeps the benchmarks' blocks).
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from korely_agent.services import context, excerpt
from korely_agent.services.ids import mem_public_id
from korely_agent.services.memory_core import presentation

DAY = datetime(2026, 11, 7, tzinfo=timezone.utc)
LEAD = "- [2026-11-07] "
MARK = presentation.SUPERSEDED_MEMORY_MARK
FILLER = ("The release went out after the suite passed on the private repository. "
          "The digest was told to the coordinator the same hour. "
          "Nothing else changed in the installer that day. ")
ANSWER = "Before a public push the owner is told and the team waits for the answer: silence is never a yes."
RULES = FILLER * 3 + ANSWER + " " + FILLER * 2
QUESTION = "Does the owner's silence count as a yes for a public push?"
# What the write gave the note: a summary that left the answer out.
OBSERVATION = ("The writer defines strict rules for releases and for the public "
               "repository, with checks before every step and a record of who "
               "approved each one. The writer keeps the installer and its digest "
               "in step with each release.")
FIRST = ["The first note is short.", "A second short note.", "A third short note.",
         "A fourth short note.", "A fifth short note."]


def _hits(contents, superseded=()):
    return [{"id": f"00000000-0000-0000-0000-{i:012d}", "content": c,
             **({"_superseded": True} if i in superseded else {})}
            for i, c in enumerate(contents)]


def _pack(monkeypatch, contents, budget, *, question=QUESTION, end_user=None,
          observations=None, superseded=()):
    """(the block's lines, the memories' observation lines, the ids left
    out, the memories' ids): `contents` packed as `assemble_context` packs
    them, every memory with its observation (the note's by default)."""
    hits = _hits(contents, superseded)
    observations = observations or {}
    rows = {h["id"]: SimpleNamespace(
        observation=observations.get(i, OBSERVATION), event_time=None, relative_times=None,
        created_at=DAY, end_user_id=end_user) for i, h in enumerate(hits)}
    monkeypatch.setattr(context, "_memory_rows", lambda db, h: rows)
    monkeypatch.setattr(context.experiments, "DIALOGUE_ORDER", False)
    block = context._Block(budget)
    left_out = []
    marked = context._memory_lines(None, hits, hits)
    lines = context._pack_memory_lines(block, marked, left_out, question)
    shown = dict(zip(block.sources, lines))
    return shown, [str(line) for line, _h in marked], left_out, [mem_public_id(h["id"]) for h in hits]


def _characters(s):
    return sum(1 for i in range(1, len(s) + 1) if i == len(s) or context.graphemes.may_cut(s, i))


def _own_words(line, lead=LEAD):
    body = line[len(lead):]
    if body.startswith(excerpt.LEFT_OUT_BEFORE):
        body = body[len(excerpt.LEFT_OUT_BEFORE):]
    return "".join(body.split(excerpt.LEFT_OUT_BETWEEN))


SIXTH = 5  # the first memory after presentation.WHOLE


def test_the_sixth_is_after_the_whole_ones():
    assert presentation.WHOLE == SIXTH


def test_a_note_after_the_fifth_shows_the_sentence_that_answers(monkeypatch):
    shown, observed, left_out, ids = _pack(monkeypatch, FIRST + [RULES], 4000)
    assert observed[SIXTH] == LEAD + OBSERVATION, "1.0.34 showed the observation"
    line = shown[ids[SIXTH]]
    assert ANSWER in line and line.startswith(LEAD) and OBSERVATION not in line
    assert excerpt.LEFT_OUT_BEFORE in line or excerpt.LEFT_OUT_BETWEEN in line
    assert not left_out


def test_the_line_never_takes_more_bytes_than_its_observation(monkeypatch):
    """At every length of the observation and every room, the note's line is
    within the bytes of the observation's line it replaces."""
    replaced = 0
    for words in range(4, 70, 3):
        observation = " ".join(OBSERVATION.split()[:words]) + "."
        for budget in (300, 450, 700, 1500, 4000):
            shown, observed, _l, ids = _pack(monkeypatch, FIRST + [RULES, FILLER * 4], budget,
                                             observations={SIXTH: observation})
            line = shown.get(ids[SIXTH])
            if line is None:
                continue
            assert context._cost(line) <= context._cost(observed[SIXTH]), (words, budget, line)
            replaced += ANSWER in line
    assert replaced, "some observation was long enough for the answer"


def test_with_no_word_of_the_question_the_observation_stays(monkeypatch):
    for question in ("¿Dónde está el servidor?", ""):
        shown, observed, _l, ids = _pack(monkeypatch, FIRST + [RULES], 4000, question=question)
        assert shown[ids[SIXTH]] == observed[SIXTH] == LEAD + OBSERVATION, question


def test_the_mark_of_a_note_changed_later_stays(monkeypatch):
    shown, observed, _l, ids = _pack(monkeypatch, FIRST + [RULES], 4000, superseded=(SIXTH,))
    assert observed[SIXTH] == LEAD + MARK + OBSERVATION
    line = shown[ids[SIXTH]]
    assert line.startswith(LEAD + MARK) and ANSWER in line


def test_a_cut_shows_at_least_forty_of_the_note_s_own_characters(monkeypatch):
    """The only sentence with the question's words is shorter than
    `_LEAST_PIECE`: it comes with its neighbours, or the observation stays."""
    text = FILLER + "Silence: no. " + FILLER * 3
    seen_words = seen_observation = False
    for words in range(2, 60, 2):
        observation = " ".join(OBSERVATION.split()[:words]) + "."
        shown, observed, _l, ids = _pack(monkeypatch, FIRST + [text], 4000, question="Silence?",
                                         observations={SIXTH: observation})
        line = shown[ids[SIXTH]]
        if line == observed[SIXTH]:
            seen_observation = True
            continue
        seen_words = True
        assert "Silence: no." in line
        assert _characters(_own_words(line)) >= context._LEAST_PIECE, (words, line)
    assert seen_words and seen_observation
    # Room for the short sentence and its marks alone: the observation stays.
    observation = "x" * (context._cost(excerpt.LEFT_OUT_BEFORE + "Silence: no.") + 4)
    shown, observed, _l, ids = _pack(monkeypatch, FIRST + [text], 4000, question="Silence?",
                                     observations={SIXTH: observation})
    assert shown[ids[SIXTH]] == observed[SIXTH] == LEAD + observation


def test_a_note_no_longer_than_its_observation_is_shown_whole(monkeypatch):
    """The rule of 1.0.31: whole when it fits; here the bytes of the
    observation's line."""
    note = "Silence is never a yes: wait for the owner's answer."
    shown, _o, _l, ids = _pack(monkeypatch, FIRST + [note], 4000, question="Where is the server?")
    assert shown[ids[SIXTH]] == LEAD + note


def test_an_end_user_s_memory_after_the_fifth_keeps_its_observation(monkeypatch):
    shown, observed, _l, ids = _pack(monkeypatch, FIRST + [RULES], 4000, end_user="u-1")
    assert shown[ids[SIXTH]] == observed[SIXTH] == LEAD + OBSERVATION


def test_a_memory_with_no_observation_is_shown_as_before(monkeypatch):
    """No observation, or an empty one: the line is the memory's own text,
    or its start (memory_core `memory_line`: at most 1,200 characters), as
    in 1.0.34, and no sentence is chosen by the question."""
    long_note = FILLER * 8 + ANSWER
    for observation in (None, "  "):
        shown, observed, _l, ids = _pack(monkeypatch, FIRST + [long_note], 4000,
                                         observations={SIXTH: observation})
        assert shown[ids[SIXTH]] == observed[SIXTH]
        assert OBSERVATION not in observed[SIXTH]
        assert (ANSWER in observed[SIXTH]) == (observation is None), observation


def test_the_first_five_are_packed_as_before(monkeypatch):
    """The memories before the sixth are the notes of 1.0.31 whatever their
    observations say: the same lines with and without one."""
    contents = [RULES, FILLER * 4, RULES, FILLER * 3, RULES, RULES]
    for budget in (400, 800, 1500):
        with_obs, _o, _l, ids = _pack(monkeypatch, contents, budget)
        without, _o, _l, _i = _pack(monkeypatch, contents, budget,
                                    observations={i: None for i in range(len(contents))})
        assert [with_obs.get(x) for x in ids[:SIXTH]] == [without.get(x) for x in ids[:SIXTH]], budget


def test_every_memory_is_shown_or_named(monkeypatch):
    contents = FIRST + [RULES, FILLER * 5, RULES, "A short line."]
    for budget in range(100, 1600, 37):
        shown, _o, left_out, ids = _pack(monkeypatch, contents, budget)
        assert sorted(list(shown) + left_out) == sorted(ids), budget
