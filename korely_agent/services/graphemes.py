"""Where a text may be cut without breaking a character as a reader sees it.

A cut between two code points can still split one user-perceived character:
an emoji made of several joined by U+200D ZERO WIDTH JOINER ("👩\u200d💻"), an
emoji with its skin-tone modifier or its variation selector, a flag (two
regional indicators), a keycap, a letter and its combining marks ("é"
written e + U+0301), an Indic consonant and its vowel sign or its virama, a
Hangul syllable written in conjoining jamo. Cut there, a piece ends with
half of a character: a joiner alone, a bare base, one regional indicator,
which a renderer shows as a letter in a box.

The rule is the extended grapheme cluster of Unicode Standard Annex #29,
"Unicode Text Segmentation" (§3.1.1, table 2, the Grapheme_Cluster_Break
rules), read from the properties Python's `unicodedata` carries, the same
for every script:

* GB3: CR LF is one character.
* GB6 to GB8: conjoining Hangul jamo, L, V and T, and the precomposed
  syllables LV and LVT, told apart by the arithmetic of the Unicode
  Standard §3.12 (Conjoining Jamo Behavior).
* GB9, GB9a: no cut before a mark (general categories Mn, Mc, Me: combining
  and spacing marks, enclosing marks, the variation selectors), before
  U+200C or U+200D, before the characters the annex adds to Extend and
  SpacingMark beyond those categories: the halfwidth katakana sound marks
  (U+FF9E, U+FF9F), the emoji modifiers (Unicode Technical Standard #51,
  §1.4.4: U+1F3FB to U+1F3FF), the tag characters (UTS #51, §2.3: U+E0020
  to U+E007F), THAI CHARACTER SARA AM and LAO VOWEL SIGN AM (U+0E33,
  U+0EB3).
* GB9c (Unicode 15.1): no cut before a letter that a virama (canonical
  combining class 9) joins, marks and joiners between them included.
* GB11: no cut after U+200D.
* GB12, GB13: regional indicators pair from the start of their run.

It is stricter than the annex in a few places: it never cuts after a
joiner, nor between a virama and a letter, even where the annex would (a
joiner not between two pictographs, a virama of a script that makes no
conjunct), nor before a mark that follows a control character; and the
Spacing_Mark exceptions of GB9a (some Myanmar and Tai Tham signs) stay with
the character before them. It is looser in one: GB9b is not read, the few
Prepend characters (U+0600 to U+0605 and some others) after which the
annex does not cut and this does, so such a sign can end a piece.
"""
import unicodedata

_ZWNJ, _ZWJ = "\u200c", "\u200d"
# Canonical combining class of the viramas (UAX #44, Canonical_Combining_Class
# = Virama).
_VIRAMA = 9
# What may follow each Hangul syllable type without a cut (GB6 to GB8).
_HANGUL_JOINS = {"L": ("L", "V", "LV", "LVT"), "V": ("V", "T"), "LV": ("V", "T"),
                 "T": ("T",), "LVT": ("T",)}


def _hangul(ch: str) -> str:
    """The Hangul_Syllable_Type of `ch` (the Unicode Standard §3.12), or ""."""
    cp = ord(ch)
    if 0x1100 <= cp <= 0x115F or 0xA960 <= cp <= 0xA97C:
        return "L"
    if 0x1160 <= cp <= 0x11A7 or 0xD7B0 <= cp <= 0xD7C6:
        return "V"
    if 0x11A8 <= cp <= 0x11FF or 0xD7CB <= cp <= 0xD7FB:
        return "T"
    if 0xAC00 <= cp <= 0xD7A3:
        return "LV" if (cp - 0xAC00) % 28 == 0 else "LVT"
    return ""


# Extend and SpacingMark beyond the mark categories (UAX #29, table 2):
# the halfwidth katakana sound marks, SARA AM and LAO AM.
_ALSO_EXTENDING = ("\uff9e", "\uff9f", "\u0e33", "\u0eb3")


def _extends(ch: str) -> bool:
    """Whether `ch` belongs to the character before it (GB9, GB9a)."""
    cp = ord(ch)
    return (unicodedata.category(ch) in ("Mn", "Mc", "Me") or ch in (_ZWNJ, _ZWJ)
            or ch in _ALSO_EXTENDING
            or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F)


def _regional(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def may_cut(text: str, i: int) -> bool:
    """Whether `text[:i]` ends between two characters as a reader sees them.
    The start and the end of the text always do."""
    if i <= 0 or i >= len(text):
        return True
    a, b = text[i - 1], text[i]
    if a == "\r" and b == "\n":
        return False
    if _extends(b) or a == _ZWJ:
        return False
    if unicodedata.category(b).startswith("L"):
        j = i - 1
        while j >= 0 and _extends(text[j]):
            if unicodedata.combining(text[j]) == _VIRAMA:
                return False
            j -= 1
    if _hangul(b) and _hangul(b) in _HANGUL_JOINS.get(_hangul(a), ()):
        return False
    if _regional(a) and _regional(b):
        run = 0
        while i - 1 - run >= 0 and _regional(text[i - 1 - run]):
            run += 1
        return run % 2 == 0
    return True


def cut_back(text: str, i: int) -> int:
    """The last place at or before `i` where `text` may be cut."""
    i = max(0, min(i, len(text)))
    while i > 0 and not may_cut(text, i):
        i -= 1
    return i


def cut_after(text: str, i: int) -> int:
    """The first place after `i` where `text` may be cut: the end of the
    character, as a reader sees it, that holds the code point at `i`."""
    i = max(0, i) + 1
    while i < len(text) and not may_cut(text, i):
        i += 1
    return min(i, len(text))
