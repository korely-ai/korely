"""Where a sentence ends, for every alphabet (6.17.0).

The context block shows a memory whole, or cut only after a complete
sentence: never inside its first
sentence, never after a word in the middle of one. Where a sentence ends is
not read from a list of words or a regular expression over them: it is the
default sentence boundary of Unicode Standard Annex #29, "Unicode Text
Segmentation", revision 39 (Unicode 14.0.0, the version of Python 3.11's
`unicodedata`), section 5, "Sentence Boundaries", rules SB1 to SB998, on the
Sentence_Break property of Table 4. The same rules for every script: a
full stop, "!" and "?", and the sentence terminators of the other scripts
(the ideographic full stop "。", the danda "।", the Arabic question mark
"؟", the Ethiopic full stop "።", ...: Sentence_Terminal of the Unicode
Character Database), followed by closing punctuation and spaces; and a line
break, which ends a sentence by rule SB4.

The property is read as Table 4 defines it, from what `unicodedata` carries
(the general category, case, digits) and from the lists the table and the
Unicode Character Database give by code point, written here as code points:

* ATerm, SContinue and Sep, the lists of Table 4; Sp, White_Space less
  them (PropList-14.0.0.txt), U+00A0 NO-BREAK SPACE included, as
  SentenceBreakProperty-14.0.0.txt files it although Table 4 names it in
  OLetter too;
* STerm, Sentence_Terminal = Yes (PropList-14.0.0.txt), less ATerm;
* Close, General_Category Ps or Pe, or Line_Break = Quotation
  (LineBreak-14.0.0.txt, class QU), less U+05F3 and the terminators;
* Numeric, Line_Break = Numeric: the decimal digits (Nd, the full-width
  ones by Table 4's own addition) and U+066B, U+066C (computed from
  LineBreak-14.0.0.txt: those two are the only Numeric code points outside
  Nd);
* Lower and Upper, `str.islower` and `str.isupper` of one character (the
  Lowercase and Uppercase properties of DerivedCoreProperties, which
  CPython's database carries) and the titlecase letters, less the Georgian
  ranges Table 4 sets apart; OLetter, the other letters (`str.isalpha`), the
  letter numbers (Nl) and U+05F3; Extend, the marks (Mn, Me, Mc),
  U+200C, U+200D and the Grapheme_Extend characters outside the marks
  (PropList-14.0.0.txt, Other_Grapheme_Extend: U+FF9E, U+FF9F, U+E0020 to
  U+E007F); Format, the other Cf.

Checked against the Unicode Consortium's own data, outside the repository
(15 Nov 2026, in the test image, Python 3.11, `unicodedata` 14.0.0): the
property read so is the one of SentenceBreakProperty-14.0.0.txt for every
code point but the surrogates (1,112,064), and every one of the 502 cases
of SentenceBreakTest-14.0.0.txt gives the same boundaries. A Python whose
`unicodedata` is of another version reads the categories and cases of that
version; the lists by code point stay those of 14.0.0
(tests/test_a_memory_is_cut_after_a_whole_sentence.py checks the version).

What the annex says it cannot do, and this does not do either: "They cannot
detect cases such as '...Mr. Jones...'; more sophisticated tailoring would
be required to detect such cases" (section 5.1, the note after the rules).
A full stop after an abbreviation followed by a capital, or after the
number of a list item ("1. Buy milk"), ends a sentence by these rules. A
text with no terminator at all (a list without punctuation on one line, a
script that ends sentences with a space, as Thai does) is one sentence.
"""
from __future__ import annotations

import bisect
import functools
import unicodedata
from typing import List

CR, LF, SEP, EXTEND, FORMAT, SP, LOWER, UPPER, OLETTER, NUMERIC, ATERM, SCONTINUE, STERM, CLOSE, OTHER = range(15)

_PARA_SEP = (SEP, CR, LF)
_SATERM = (STERM, ATERM)

# Table 4, by code point.
_ATERM = frozenset((0x002E, 0x2024, 0xFE52, 0xFF0E))
_SCONTINUE = frozenset((0x002C, 0x002D, 0x003A, 0x055D, 0x060C, 0x060D, 0x07F8, 0x1802, 0x1808,
                        0x2013, 0x2014, 0x3001, 0xFE10, 0xFE11, 0xFE13, 0xFE31, 0xFE32, 0xFE50,
                        0xFE51, 0xFE55, 0xFE58, 0xFE63, 0xFF0C, 0xFF0D, 0xFF1A, 0xFF64))
_SEP = frozenset((0x0085, 0x2028, 0x2029))
# White_Space = Yes (PropList-14.0.0.txt) less Sep, CR and LF. U+00A0 is
# one: Table 4 names it in OLetter too, and SentenceBreakProperty-14.0.0.txt
# files it as Sp.
_SP = frozenset((0x0009, 0x000B, 0x000C, 0x0020, 0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F,
                 0x205F, 0x3000))
# Sentence_Terminal = Yes (PropList-14.0.0.txt), less ATerm: (first, last).
_STERM = (
    (0x21, 0x21), (0x3F, 0x3F), (0x589, 0x589), (0x61D, 0x61F), (0x6D4, 0x6D4), (0x700, 0x702),
    (0x7F9, 0x7F9), (0x837, 0x837), (0x839, 0x839), (0x83D, 0x83E), (0x964, 0x965),
    (0x104A, 0x104B), (0x1362, 0x1362), (0x1367, 0x1368), (0x166E, 0x166E), (0x1735, 0x1736),
    (0x1803, 0x1803), (0x1809, 0x1809), (0x1944, 0x1945), (0x1AA8, 0x1AAB), (0x1B5A, 0x1B5B),
    (0x1B5E, 0x1B5F), (0x1B7D, 0x1B7E), (0x1C3B, 0x1C3C), (0x1C7E, 0x1C7F), (0x203C, 0x203D),
    (0x2047, 0x2049), (0x2E2E, 0x2E2E), (0x2E3C, 0x2E3C), (0x2E53, 0x2E54), (0x3002, 0x3002),
    (0xA4FF, 0xA4FF), (0xA60E, 0xA60F), (0xA6F3, 0xA6F3), (0xA6F7, 0xA6F7), (0xA876, 0xA877),
    (0xA8CE, 0xA8CF), (0xA92F, 0xA92F), (0xA9C8, 0xA9C9), (0xAA5D, 0xAA5F), (0xAAF0, 0xAAF1),
    (0xABEB, 0xABEB), (0xFE56, 0xFE57), (0xFF01, 0xFF01), (0xFF1F, 0xFF1F), (0xFF61, 0xFF61),
    (0x10A56, 0x10A57), (0x10F55, 0x10F59), (0x10F86, 0x10F89), (0x11047, 0x11048),
    (0x110BE, 0x110C1), (0x11141, 0x11143), (0x111C5, 0x111C6), (0x111CD, 0x111CD),
    (0x111DE, 0x111DF), (0x11238, 0x11239), (0x1123B, 0x1123C), (0x112A9, 0x112A9),
    (0x1144B, 0x1144C), (0x115C2, 0x115C3), (0x115C9, 0x115D7), (0x11641, 0x11642),
    (0x1173C, 0x1173E), (0x11944, 0x11944), (0x11946, 0x11946), (0x11A42, 0x11A43),
    (0x11A9B, 0x11A9C), (0x11C41, 0x11C42), (0x11EF7, 0x11EF8), (0x16A6E, 0x16A6F),
    (0x16AF5, 0x16AF5), (0x16B37, 0x16B38), (0x16B44, 0x16B44), (0x16E98, 0x16E98),
    (0x1BC9F, 0x1BC9F), (0x1DA88, 0x1DA88),
)
# Line_Break = Quotation (LineBreak-14.0.0.txt, QU): (first, last).
_QUOTATION = (
    (0x22, 0x22), (0x27, 0x27), (0xAB, 0xAB), (0xBB, 0xBB), (0x2018, 0x2019), (0x201B, 0x201D),
    (0x201F, 0x201F), (0x2039, 0x203A), (0x275B, 0x2760), (0x2E00, 0x2E0D), (0x2E1C, 0x2E1D),
    (0x2E20, 0x2E21), (0x1F676, 0x1F678),
)
# The Georgian letters Table 4 leaves out of Lower (Mkhedruli) and of Upper
# (Mtavruli).
_NOT_LOWER = ((0x10D0, 0x10FA), (0x10FD, 0x10FF))
_NOT_UPPER = ((0x1C90, 0x1CBA), (0x1CBD, 0x1CBF))


def _within(cp: int, ranges) -> bool:
    """Whether `cp` is in one of `ranges`, (first, last) pairs sorted by
    their first code point: a binary search."""
    k = bisect.bisect_right(ranges, (cp, 0x10FFFF)) - 1
    return k >= 0 and ranges[k][0] <= cp <= ranges[k][1]


@functools.lru_cache(maxsize=8192)
def _class(ch: str) -> int:
    """The Sentence_Break value of `ch` (Table 4). Cached by character: a
    text repeats few of them."""
    cp = ord(ch)
    if cp == 0x0D:
        return CR
    if cp == 0x0A:
        return LF
    if cp in _SEP:
        return SEP
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Mc") or cp in (0x200C, 0x200D) or 0xFF9E <= cp <= 0xFF9F \
            or 0xE0020 <= cp <= 0xE007F:
        return EXTEND
    if cat == "Cf":
        return FORMAT
    if cp in _SP:
        return SP
    if ch.islower() and not _within(cp, _NOT_LOWER):
        return LOWER
    if (ch.isupper() or cat == "Lt") and not _within(cp, _NOT_UPPER):
        return UPPER
    if ch.isalpha() or cat == "Nl" or cp == 0x05F3:
        return OLETTER
    if cat == "Nd" or cp in (0x066B, 0x066C):
        return NUMERIC
    if cp in _ATERM:
        return ATERM
    if cp in _SCONTINUE:
        return SCONTINUE
    if _within(cp, _STERM):
        return STERM
    if cat in ("Ps", "Pe") or _within(cp, _QUOTATION):
        return CLOSE
    return OTHER


# What SB8 looks for after "ATerm Close* Sp*": the first character that is
# none of the others it skips.
_SB8_STOPS = frozenset((OLETTER, UPPER, LOWER, *_PARA_SEP, *_SATERM))
# The phases of "SATerm Close* Sp*" (SB8 to SB11), read left to right.
_NONE, _AFTER_TERM, _AFTER_SPACE = 0, 1, 2


def boundaries(text: str) -> List[int]:
    """The offsets (in code points) where a sentence of `text` ends, in
    order, the end of the text last; none for an empty text (SB1, SB2). A
    sentence keeps its closing punctuation, the spaces after it and the
    line break that ends it (SB9 to SB11).

    One pass from left to right (6.17.0; the first form walked back over
    the left context, and SB8 forward, at every position, so a terminator
    followed by thousands of spaces or closing marks took minutes). Each position is decided from what the
    pass carries: the character the rules see before it (SB5: Extend and
    Format belong to the character before them, except after the start of
    the text and after a paragraph separator), the one before that (SB7),
    the phase of "SATerm Close* Sp*" that ends there, with its terminator,
    and, for SB8, the first character from the position on that SB8 does not
    skip, read once from right to left. Linear in the length of the text."""
    n = len(text)
    if not n:
        return []
    cls = [_class(ch) for ch in text]
    # SB8: from each position, the class of the first character SB8 stops at.
    stop = [OTHER] * (n + 1)
    nxt = OTHER
    for k in range(n - 1, -1, -1):
        if cls[k] in _SB8_STOPS:
            nxt = cls[k]
        stop[k] = nxt
    stop[n] = OTHER
    out: List[int] = []
    prev = prev2 = -1          # the character the rules see before i, and before it
    phase, term = _NONE, OTHER  # "SATerm Close* Sp*" ending at `prev`
    for i in range(n):
        c = cls[i]
        if i > 0:
            a = cls[i - 1]
            if a == CR and c == LF:                                  # SB3
                brk = False
            elif a in _PARA_SEP:                                     # SB4
                brk = True
            elif c in (EXTEND, FORMAT):                              # SB5
                brk = False
            elif prev >= 0 and cls[prev] == ATERM and c == NUMERIC:  # SB6
                brk = False
            elif (prev >= 0 and cls[prev] == ATERM and prev2 >= 0 and cls[prev2] in (UPPER, LOWER)
                  and c == UPPER):                                   # SB7
                brk = False
            elif phase == _NONE:                                     # SB998
                brk = False
            elif term == ATERM and stop[i] == LOWER:                 # SB8
                brk = False
            elif c in (SCONTINUE, *_SATERM):                         # SB8a
                brk = False
            elif phase == _AFTER_TERM and c in (CLOSE, SP, *_PARA_SEP):  # SB9
                brk = False
            elif c in (SP, *_PARA_SEP):                              # SB10
                brk = False
            else:                                                    # SB11
                brk = True
            if brk:
                out.append(i)
        # What the next position sees before it (SB5: an Extend or a Format
        # after something else than the start or a separator belongs to it).
        if c in (EXTEND, FORMAT) and i > 0 and cls[i - 1] not in _PARA_SEP and prev >= 0:
            continue
        prev2, prev = prev, i
        if c in _SATERM:
            phase, term = _AFTER_TERM, c
        elif c == CLOSE and phase == _AFTER_TERM:
            pass
        elif c == SP and phase in (_AFTER_TERM, _AFTER_SPACE):
            phase = _AFTER_SPACE
        else:
            phase, term = _NONE, OTHER
    return out + [n]


@functools.lru_cache(maxsize=256)
def ends(text: str) -> tuple:
    """`boundaries(text)` as a tuple, kept for the texts read last: the block
    reads the same memory's boundaries for its floor and for its packing."""
    return tuple(boundaries(text))


def whole_sentences(text: str, fits, *, least: int = 0) -> str:
    """The longest beginning of `text` made of whole sentences, its trailing
    spaces and line breaks dropped, for which `fits(piece)` is true; "" when
    none does. `least`: a boundary before that many characters is no cut, so
    a first sentence shorter takes the next ones with it ("Dr." alone is not
    shown). `fits` must keep anything shorter that it keeps (a
    room in bytes, characters or tokens): a binary search over the
    boundaries, a few slices of the text whatever its length."""
    cuts = [e for e in ends(text) if e >= least]
    lo, hi, best = 0, len(cuts) - 1, ""
    while lo <= hi:
        mid = (lo + hi) // 2
        piece = text[:cuts[mid]].rstrip()
        if piece and fits(piece):
            best, lo = piece, mid + 1
        else:
            hi = mid - 1
    return best


def first_sentence(text: str, *, least: int = 0) -> str:
    """The first whole sentence of `text` (with the ones after it while it
    is shorter than `least` characters), its trailing spaces dropped."""
    for e in ends(text):
        if e >= least:
            return text[:e].rstrip()
    return text.rstrip()


def head(text: str, most: int) -> str:
    """The start of `text` in at most `most` characters: its whole sentences
    that fit (`whole_sentences`); when not even the first does, a cut after
    the last space of the window (a word), or, with no space in it, at the
    last place that splits no character the rules read as one (never before
    an Extend or a Format, SB5, nor after U+200D ZERO WIDTH JOINER). The
    whole text when it is not longer."""
    if len(text) <= most:
        return text
    piece = whole_sentences(text, lambda p: len(p) <= most)
    if piece:
        return piece
    window = text[:most]
    space = max((k for k in range(len(window) - 1, 0, -1) if window[k].isspace()), default=-1)
    if space > 0:
        return window[:space].rstrip()
    cut = len(window)
    while cut > 1 and (_class(text[cut]) in (EXTEND, FORMAT) or text[cut - 1] == "\u200d"):
        cut -= 1
    return text[:cut]
