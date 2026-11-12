"""The context block cuts a text between two characters as a reader sees
them (services/graphemes.py, Unicode's extended grapheme clusters, UAX #29),
never between two code points of one: an emoji of joined code points, an
emoji and its modifier or its variation selector, a flag, a keycap, a letter
and its combining marks, an Indic consonant and its vowel sign or its
virama, a Hangul syllable in conjoining jamo (1.0.15; until then the cut fell
between two code points).

The texts are built from their characters, so the test knows where each one
begins without asking the rule it tests.
"""
import pytest

from korely_agent.services import context as ctx
from korely_agent.services import graphemes

# One user-perceived character each (UAX #29 extended grapheme clusters).
CHARACTERS = {
    "combining acute": "e\u0301",
    "two marks": "a\u0308\u0301",
    "woman technologist": "\U0001F469\u200d\U0001F4BB",
    "family": "\U0001F468\u200d\U0001F469\u200d\U0001F467\u200d\U0001F466",
    "skin tone": "\U0001F44D\U0001F3FD",
    "red heart": "\u2764\ufe0f",
    "keycap": "1\ufe0f\u20e3",
    "flag of Italy": "\U0001F1EE\U0001F1F9",
    "flag of Scotland": "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F",
    "Devanagari vowel sign": "\u0915\u093f",
    "Devanagari conjunct": "\u0915\u094d\u0937",
    "Devanagari conjunct with a joiner": "\u0915\u094d\u200d\u0937",
    "Thai sara am": "\u0e01\u0e33",
    "Hangul jamo": "\u1100\u1161\u11a8",
    "halfwidth voiced katakana": "\uff76\uff9e",
    "CR LF": "\r\n",
}


def _boundaries(chars):
    out, at = {0}, 0
    for c in chars:
        at += len(c)
        out.add(at)
    return out


def _mixed(n=6):
    """A text of every character above, with Latin, CJK and Arabic letters
    between them, repeated."""
    chars = []
    for _ in range(n):
        for c in CHARACTERS.values():
            chars += [c, "x", "水", "ب"]
    return chars


@pytest.mark.parametrize("name", list(CHARACTERS))
def test_no_cut_inside_one_character(name):
    c = CHARACTERS[name]
    text = "a" + c + "b"
    for i in range(len(text) + 1):
        assert graphemes.may_cut(text, i) == (i in (0, 1, 1 + len(c), len(text))), (name, i)


def test_flags_pair_from_the_start_of_their_run():
    flags = ["\U0001F1EE\U0001F1F9", "\U0001F1EF\U0001F1F5", "\U0001F1EB\U0001F1F7"]
    text = "".join(flags)
    for i in range(len(text) + 1):
        assert graphemes.may_cut(text, i) == (i % 2 == 0), i


def test_a_prefix_within_ends_between_two_characters():
    chars = _mixed()
    text = "".join(chars)
    cuts = _boundaries(chars)
    for room in range(0, ctx._cost(text) + 2):
        head = ctx._prefix_within(text, room)
        assert ctx._cost(head) <= max(room, 0) and text.startswith(head)
        assert len(head) in cuts, room
        # The longest such beginning: the next character would not fit.
        nxt = min(c for c in cuts if c > len(head)) if len(head) < len(text) else None
        assert nxt is None or ctx._cost(text[:nxt]) > room, room


def test_a_memory_cut_to_fit_keeps_whole_characters():
    chars = _mixed(20)
    text = "".join(chars)
    cuts = _boundaries(chars)
    for budget in range(11, 160):
        frag = ctx._truncate_to_budget(text, budget)
        assert frag and ctx._cost(frag) <= budget * ctx.BYTES_PER_TOKEN - 2, budget
        kept = (frag[:-1] if frag.endswith("…") else frag).rstrip()
        assert text.startswith(kept) and len(kept) in cuts, budget


def test_a_character_longer_than_the_room_gives_no_fragment():
    """One character, as a reader sees it, of more bytes than the room: no
    fragment at all, rather than an ellipsis alone or half of it."""
    zalgo = "a" + "\u0301" * 60
    assert ctx._truncate_to_budget(zalgo * 3, 11) == ""
    assert ctx._prefix_within(zalgo, 100) == ""


@pytest.mark.parametrize("name", list(CHARACTERS))
def test_the_end_of_the_character_that_holds_a_code_point(name):
    """`cut_after` gives the end of the character, as a reader sees it, that
    holds a code point: the short form of a change keeps that much of the
    part that differs first."""
    c = CHARACTERS[name]
    text = "a" + c + "b"
    for i in range(1, 1 + len(c)):
        assert graphemes.cut_after(text, i) == 1 + len(c), (name, i)
    assert graphemes.cut_after(text, 0) == 1 and graphemes.cut_after(text, len(text) - 1) == len(text)
