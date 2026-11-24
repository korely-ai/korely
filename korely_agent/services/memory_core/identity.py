"""Entity identity: when two names are one entity (memory_core).

"Gordon became Korely, and the memory must understand it without deleting
anything" (the requirement of 2026-09-26). Pure rules over a name map; the table that
stores the map and the code that rewrites facts live in each engine.

Paper:
    * Graphiti / Zep (Rasmussen 2025, arXiv:2501.13956 §2.1) — every new
      entity is resolved against the existing ones and merged automatically.
    * HippoRAG (Gutierrez 2024, arXiv:2405.14831 §3.2) — canonicalize at
      write time, not by fan-out at read time.
    * Wikidata — a rename is not a merge of labels: it is ONE entity with
      several dated names. Here: the alias row carries `since`, and every
      fact keeps the label it was written with.

Rule (decision of 2026-09-26): an alias is created on its own ONLY
from an EXPLICIT identity statement (`renamed_to`, `formerly`, `codename`,
`also_known_as`) about an entity type that really gets renamed (product,
organization). Never from similarity: two names that look alike are not a
statement that they are one thing. A person can always merge or undo by
hand, and the hand wins over the automatism.

6.1.0 (`objects`, off by default): one thing named in two languages or
forms ("Italy" and "Italia") is NOT an alias. When the writer, reading both
in their contexts, judges two objects of a memory one thing, the engine
writes a link (`Tables.Link`) that the reads resolve and no fact follows:
an alias rewrites canonical names and merges rows, which no undo can split,
and a judgement must stay reversible. `SOURCE_NODE_MERGE` stays unwritten.

No threshold on the confidence the model declares (2.12.0, decision of
6 Oct, after a code audit): until then the identity statement also needed a
declared confidence of 0.9 or more. Xiong et al. ("Can LLMs Express Their
Uncertainty?", ICLR 2024, section 1 and 5.1): "LLMs tend to be highly
overconfident when verbalizing their confidence", and "the verbalized
confidence values predominantly fall within the 80% to 100% range and are
typically in multiples of 5"; their Figure 2: "the accuracy within each bin
is much lower than its corresponding confidence". A number that sits at 0.9
or above whether the statement is right or wrong decides nothing; the
criteria that do are categorical, and they stay: the relation is one of the
four identity relations, the subject is a product or an organization, both
are names, and they differ.

`replaces` is NOT an identity statement (2026-09-27): it links two distinct
entities, a successor and a predecessor (Wikidata P1365 "replaces", whose
inverse is P1366 "replaced by"; names of one item are its aliases). Measured
on a real memory's facts before release: of five `replaces` facts one was
a rename (Korely / Gordon, which "Gordon renamed_to Korely" also states) and
four were substitutions or an extraction error (Deepgram Nova-3 / Soniox,
Postgres 17 / Postgres 18, Korely Memory / Mem0, a product / its successor):
aliasing them would have answered questions about Soniox with Deepgram's
facts. `renamed_to` and `codename` were right three times out of three.

Vocabulary:
    alias map  = dict name_key(alias) -> canonical_label (6.21.0; until then
                 the alias lowercased)
    canonical  = the name after following the map (at most `MAX_HOPS` hops)
"""
from __future__ import annotations

import bisect
import unicodedata
from typing import Dict, Optional, Tuple

from .families import IDENTITY_ALIAS_DIRECTION

# Who decided an alias, in order of trust.
SOURCE_USER = "user"                    # merged by hand (panel / dashboard)
SOURCE_IDENTITY_FACT = "rename_fact"  # an explicit identity fact
SOURCE_NODE_MERGE = "node_merge"       # reserved; not wired on purpose
SOURCES = (SOURCE_USER, SOURCE_IDENTITY_FACT, SOURCE_NODE_MERGE)

# Only entities that really get renamed. People do not: two people who share a
# surname are not one person, and "Marco renamed_to Mario" is far more often
# an extraction error than a legal name change.
TYPES_THAT_RENAME = frozenset({"product", "organization"})

MAX_HOPS = 5

# A name (6.21.0, revised the same day). What the rule keeps out is what it
# always kept out: a sentence, a list, a date, a version, an address; at most
# 60 characters and 5 words, beginning with a letter. What it lets in is read
# by the categories of the Unicode Character Database (UAX #44,
# General_Category), the same for every script, instead of Python's `\w` and
# a few ASCII marks: until 6.21.0 `^[^\W\d_][\w .\-&'/+]{0,59}$` refused
# every name whose vowel signs, virama or points are combining marks (`\w`
# takes no category M), so no Devanagari, Bengali, Tamil or Thai name, no
# pointed Hebrew or vowelled Arabic one, could ever become an alias or be
# linked (the blind set of 23 Nov 2026: 54 of its 263 true pairs never
# reached the writer, every Devanagari one among them). The code points
# below are those of the Unicode of Python 3.11's `unicodedata` (14.0.0),
# from the files the Unicode Consortium publishes, checked for every code
# point by tests/test_a_name_is_a_name_in_every_script.py against the copies
# in tests/data/unicode.
MAX_NAME_CHARS = 60
MAX_NAME_WORDS = 5
# The bound of Unicode Standard Annex #15, section 13, "Stream-Safe Text
# Format" (no run of more than 30 non-starters), here on the combining marks
# that follow one letter: a letter with more marks than that is no letter of
# any script.
MAX_MARKS = 30
# The punctuation of product names the rule always took, anywhere after the
# first letter (Korely.ai, Claude-Code, AT&T, Google+, korely-ai/korely,
# O'Reilly, user_42).
_NAME_PUNCTUATION = frozenset(".-&'/+_")
# Punctuation that only joins two parts of one word, never begins or ends
# one: Unicode's MidLetter and MidNumLet (UAX #29, "Unicode Text
# Segmentation", section 4.1, Table 3 "Word_Break Property Values", rules WB6
# and WB7: no word break between two letters around them;
# WordBreakProperty-14.0.0.txt), less the colons (U+003A, U+FE13, U+FE55,
# U+FF1A: between two digits a colon is a time, "10:30") and less the dots
# that are not the full stop (U+0387, U+2024, U+2027, U+FE52, U+FF0E: a name
# with one of them looks like the name with the full stop, "Korely.ai", and
# is another string), the full stop and the apostrophe the line above already
# takes; and the katakana middle dot, which writes the words of a foreign
# name in katakana apart (W3C, "Requirements for Japanese Text Layout",
# JLReq, class cl-05 "middle dots": ニューヨーク・タイムズ). Paral·lel,
# McDonald’s, צה״ל.
_MEDIAL = frozenset("\u00b7\u055f\u05f4\u2018\u2019\uff07\u30fb")
_ZWNJ = "\u200c"
_ZWJ = "\u200d"
# Canonical_Combining_Class 9, Virama (UAX #44; `unicodedata.combining`).
_VIRAMA = 9
# Default_Ignorable_Code_Point = Yes (DerivedCoreProperties-14.0.0.txt):
# characters a reader does not see. "Default-ignorable characters are
# normally excluded from Unicode identifiers" (UAX #31, "Unicode Identifiers
# and Syntax", section 2.3): a name with one of them looks like the name
# without it and is another string. Out, all of them (the Hangul fillers too,
# which are letters to `unicodedata` and the old rule took), but for ZWNJ and
# ZWJ in the contexts of that section (`_joiner_in_context`).
_DEFAULT_IGNORABLE = (
    (0xAD, 0xAD), (0x34F, 0x34F), (0x61C, 0x61C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)

_JOINS_BOTH = (
    (0x620, 0x620), (0x626, 0x626), (0x628, 0x628), (0x62A, 0x62E), (0x633, 0x63F), (0x641, 0x647),
    (0x649, 0x64A), (0x66E, 0x66F), (0x678, 0x687), (0x69A, 0x6BF), (0x6C1, 0x6C2), (0x6CC, 0x6CC),
    (0x6CE, 0x6CE), (0x6D0, 0x6D1), (0x6FA, 0x6FC), (0x6FF, 0x6FF), (0x712, 0x714), (0x71A, 0x71D),
    (0x71F, 0x727), (0x729, 0x729), (0x72B, 0x72B), (0x72D, 0x72E), (0x74E, 0x758), (0x75C, 0x76A),
    (0x76D, 0x770), (0x772, 0x772), (0x775, 0x777), (0x77A, 0x77F), (0x7CA, 0x7EA), (0x841, 0x845),
    (0x848, 0x848), (0x84A, 0x853), (0x855, 0x855), (0x860, 0x860), (0x862, 0x865), (0x868, 0x868),
    (0x886, 0x886), (0x889, 0x88D), (0x8A0, 0x8A9), (0x8AF, 0x8B0), (0x8B3, 0x8B8), (0x8BA, 0x8C8),
    (0x1807, 0x1807), (0x1820, 0x1878), (0x1887, 0x18A8), (0x18AA, 0x18AA), (0xA840, 0xA871),
    (0x10AC0, 0x10AC4), (0x10AD3, 0x10AD6), (0x10AD8, 0x10ADC), (0x10ADE, 0x10AE0),
    (0x10AEB, 0x10AEE), (0x10B80, 0x10B80), (0x10B82, 0x10B82), (0x10B86, 0x10B88),
    (0x10B8A, 0x10B8B), (0x10B8D, 0x10B8D), (0x10B90, 0x10B90), (0x10BAD, 0x10BAE),
    (0x10D01, 0x10D21), (0x10D23, 0x10D23), (0x10F30, 0x10F32), (0x10F34, 0x10F44),
    (0x10F51, 0x10F53), (0x10F70, 0x10F73), (0x10F76, 0x10F81), (0x10FB0, 0x10FB0),
    (0x10FB2, 0x10FB3), (0x10FB8, 0x10FB8), (0x10FBB, 0x10FBC), (0x10FBE, 0x10FBF),
    (0x10FC1, 0x10FC1), (0x10FC4, 0x10FC4), (0x10FCA, 0x10FCA), (0x1E900, 0x1E943),
)

_JOINS_LEFT = (
    (0xA872, 0xA872), (0x10ACD, 0x10ACD), (0x10AD7, 0x10AD7), (0x10D00, 0x10D00), (0x10FCB, 0x10FCB),
)

_JOINS_RIGHT = (
    (0x622, 0x625), (0x627, 0x627), (0x629, 0x629), (0x62F, 0x632), (0x648, 0x648), (0x671, 0x673),
    (0x675, 0x677), (0x688, 0x699), (0x6C0, 0x6C0), (0x6C3, 0x6CB), (0x6CD, 0x6CD), (0x6CF, 0x6CF),
    (0x6D2, 0x6D3), (0x6D5, 0x6D5), (0x6EE, 0x6EF), (0x710, 0x710), (0x715, 0x719), (0x71E, 0x71E),
    (0x728, 0x728), (0x72A, 0x72A), (0x72C, 0x72C), (0x72F, 0x72F), (0x74D, 0x74D), (0x759, 0x75B),
    (0x76B, 0x76C), (0x771, 0x771), (0x773, 0x774), (0x778, 0x779), (0x840, 0x840), (0x846, 0x847),
    (0x849, 0x849), (0x854, 0x854), (0x856, 0x858), (0x867, 0x867), (0x869, 0x86A), (0x870, 0x882),
    (0x88E, 0x88E), (0x8AA, 0x8AC), (0x8AE, 0x8AE), (0x8B1, 0x8B2), (0x8B9, 0x8B9),
    (0x10AC5, 0x10AC5), (0x10AC7, 0x10AC7), (0x10AC9, 0x10ACA), (0x10ACE, 0x10AD2),
    (0x10ADD, 0x10ADD), (0x10AE1, 0x10AE1), (0x10AE4, 0x10AE4), (0x10AEF, 0x10AEF),
    (0x10B81, 0x10B81), (0x10B83, 0x10B85), (0x10B89, 0x10B89), (0x10B8C, 0x10B8C),
    (0x10B8E, 0x10B8F), (0x10B91, 0x10B91), (0x10BA9, 0x10BAC), (0x10D22, 0x10D22),
    (0x10F33, 0x10F33), (0x10F54, 0x10F54), (0x10F74, 0x10F75), (0x10FB4, 0x10FB6),
    (0x10FB9, 0x10FBA), (0x10FBD, 0x10FBD), (0x10FC2, 0x10FC3), (0x10FC9, 0x10FC9),
)
# Joining_Type Transparent outside the marks (DerivedJoiningType-14.0.0.txt:
# the transparent characters are the marks Mn and Me, the format characters,
# and this one letter). Format characters are no part of a name anyway.
_TRANSPARENT_LETTERS = frozenset((0x1E94B,))


def _in(table, c: str) -> bool:
    """Whether the code point of `c` is in a table of (first, last) ranges."""
    cp = ord(c)
    i = bisect.bisect_right(table, (cp, 0x10FFFF)) - 1
    return i >= 0 and table[i][0] <= cp <= table[i][1]


def name_key(s: Optional[str]) -> str:
    """A name as two names are compared by code: NFKC, case folded, trimmed,
    the comparison of the judge (`contradiction._n`, 3.3.0). The key of an
    alias (`alias_lower`) and of a link (`objects.key_of`) since 6.21.0:
    "Café" composed and decomposed, a Devanagari letter with its nukta
    written in one character or in two, are one name."""
    return unicodedata.normalize("NFKC", s or "").casefold().strip()


def _transparent(c: str) -> bool:
    return unicodedata.category(c) in ("Mn", "Me") or ord(c) in _TRANSPARENT_LETTERS


def _joiner_in_context(s: str, i: int) -> bool:
    """Whether the ZWNJ or ZWJ at `s[i]` is in one of the contexts UAX #31
    allows (section 2.3, "Layout and Format Control Characters"):

    A1, ZWNJ breaking a cursive connection: /$LJ $T* ZWNJ $T* $RJ/, a left- or
    dual-joining letter before, a right- or dual-joining letter after, only
    transparent characters between (Joining_Type, DerivedJoiningType-14.0.0:
    the Persian پژوهش\u200cگاه).
    A2, ZWNJ in a conjunct: /$L $M* $V $M₁* ZWNJ $M₁* $L/, a letter, a
    virama, the joiner, a letter.
    B, ZWJ in a conjunct: /$L $M* $V $M₁* ZWJ (?!$D)/, here read more
    strictly, as A2: followed by a letter (so never by a dependent vowel,
    which is a mark, nor by nothing).
    $M is a nonspacing mark (Mn), $M₁ a nonspacing mark of a combining class
    other than 0. Never between two letters of a script that does not join
    or form conjuncts: "Ac\u200dme" is no name."""
    c = s[i]
    if c == _ZWNJ:
        j = i - 1
        while j >= 0 and _transparent(s[j]):
            j -= 1
        k = i + 1
        while k < len(s) and _transparent(s[k]):
            k += 1
        if (j >= 0 and k < len(s) and (_in(_JOINS_BOTH, s[j]) or _in(_JOINS_LEFT, s[j]))
                and (_in(_JOINS_BOTH, s[k]) or _in(_JOINS_RIGHT, s[k]))):
            return True

    def m1(x: str) -> bool:
        return unicodedata.category(x) == "Mn" and unicodedata.combining(x) not in (0, _VIRAMA)

    j = i - 1
    while j >= 0 and m1(s[j]):
        j -= 1
    if j < 0 or unicodedata.combining(s[j]) != _VIRAMA:
        return False
    j -= 1
    while j >= 0 and unicodedata.category(s[j]) == "Mn":
        j -= 1
    if j < 0 or not unicodedata.category(s[j]).startswith("L"):
        return False
    k = i + 1
    while k < len(s) and m1(s[k]):
        k += 1
    return k < len(s) and unicodedata.category(s[k]).startswith("L")


def _joins_a_word(c: Optional[str]) -> bool:
    """A letter, a mark or a number: what a medial character stands between."""
    return c is not None and unicodedata.category(c)[0] in "LMN"


def _plain_name(s: str) -> bool:
    """A name with no comma: its characters, one by one (the block above)."""
    if not s or not unicodedata.category(s[0]).startswith("L") or _in(_DEFAULT_IGNORABLE, s[0]):
        return False
    marks = 0
    for i, c in enumerate(s):
        if i == 0:
            continue
        cat = unicodedata.category(c)
        prev = s[i - 1]
        nxt = s[i + 1] if i + 1 < len(s) else None
        marks = marks + 1 if cat in ("Mn", "Mc") else 0
        if _in(_DEFAULT_IGNORABLE, c):
            if c in (_ZWNJ, _ZWJ) and _joiner_in_context(s, i):
                continue
            return False
        if cat[0] in "LN" or c == " " or c in _NAME_PUNCTUATION:
            continue
        if cat in ("Mn", "Mc"):
            # A combining mark follows the letter, number or mark it
            # combines with, and no letter takes more than MAX_MARKS. An
            # enclosing mark (Me: a circle, a keycap) is a symbol's, not a
            # letter's.
            if not _joins_a_word(prev) or marks > MAX_MARKS:
                return False
            continue
        if c in _MEDIAL:
            if not (_joins_a_word(prev) and _joins_a_word(nxt)):
                return False
            continue
        return False
    return True


def _within_bounds(s: str) -> bool:
    return bool(s) and len(s) <= MAX_NAME_CHARS and len(s.split()) <= MAX_NAME_WORDS


def valid_name(s: Optional[str]) -> bool:
    """Whether `s` can be the name of an alias the engine makes by itself
    (a rename, `alias_rule_from_identity`), of a link (`objects`) or of a
    subject of a review: no comma, ever. A comma separates the items of a
    list ("pizza, pasta", "IBM, HP"), and the comma of a name ("Airbnb,
    Inc.") is taken only where a person declares the name
    (`valid_declared_name`), until it is measured where the engine decides by
    itself. Parentheses stay out: what they hold is a disambiguation, and "the
    disambiguation should not be included in the Wikidata label.
    Disambiguation information should instead be part of the description"
    (Wikidata, Help:Label, section 1.4.1, read on 23 Nov 2026)."""
    s = (s or "").strip()
    return _within_bounds(s) and "," not in s and _plain_name(s)


def valid_declared_name(s: Optional[str]) -> bool:
    """`valid_name`, and one shape more for a name a person declares (an
    alias by hand, a declared name of an end user: `new_alias_outcome` with
    `SOURCE_USER`): a name, a comma, one space, and ONE word that begins with
    a capital and holds a full stop, the abbreviated legal form of a company
    or the abbreviation of a place ("Airbnb, Inc.", "Telefónica, S.A.",
    "Washington, D.C.", Wikidata's label of Q61). A list stays out, in
    capitals or not ("IBM, HP", "NASA, ESA", "Rome, IT", "pizza, etc.",
    "Marco, CEO"), and so does a space before the comma. Letter case and the
    full stop are properties of the characters, the same in every script that
    has them; a script with no case has no such form."""
    if valid_name(s):
        return True
    s = (s or "").strip()
    if not _within_bounds(s):
        return False
    head, sep, tail = s.partition(", ")
    return (bool(sep) and "," not in head and "," not in tail and head == head.rstrip()
            and " " not in tail and _plain_name(head) and _plain_name(tail)
            and tail[0].isupper() and "." in tail)


def canonical_of(label: Optional[str], alias_map_of: Dict[str, str]) -> str:
    """The name after aliases. Follows the chain (Gordon Pro -> Gordon ->
    Korely) for at most `MAX_HOPS` hops and stops on a cycle."""
    if not label:
        return label or ""
    current_one = label.strip()
    seen: set = set()
    for _ in range(MAX_HOPS):
        k = name_key(current_one)
        if k in seen:
            break
        seen.add(k)
        following = alias_map_of.get(k)
        if following is None or name_key(following) == k:
            break
        current_one = following.strip()
    return current_one


def canonicals_of_the_triple(
    subject: Optional[str],
    predicate_canonical: Optional[str],
    obj: Optional[str],
    alias_map_of: Dict[str, str],
) -> Tuple[str, str]:
    """(subject_canonical, object_canonical) of a triple.

    A fact of IDENTITY keeps both names as written: "Orion renamed_to Vega" and
    "Vega formerly Orion" are statements ABOUT the old name, and following the
    alias would turn them into "Vega formerly Vega" (found by the MCP acceptance run,
    2026-09-27). Every other fact follows the map."""
    from .families import family_for

    if family_for(predicate_canonical) == "identity":
        return (subject or "").strip(), (obj or "").strip()
    return canonical_of(subject, alias_map_of), canonical_of(obj, alias_map_of)


def reaches(alias_map_of: Dict[str, str], start: str, a_lower: str) -> bool:
    """True when following the map from `start` reaches `a_lower` (a cycle, if
    `a_lower` is the alias about to be written). Keys as `name_key`."""
    current_one = name_key(start)
    a_lower = name_key(a_lower)
    for _ in range(MAX_HOPS + 1):
        if current_one == a_lower:
            return True
        following = alias_map_of.get(current_one)
        if following is None:
            return False
        current_one = name_key(following)
    return False


def name_with_history(label: Optional[str], canonical: Optional[str]) -> str:
    """How a label with a different canonical is shown: "Korely (then:
    Gordon)". The product speaks English."""
    lab = (label or "").strip()
    can = (canonical or "").strip()
    if not can or name_key(can) == name_key(lab):
        return lab
    return f"{can} (then: {lab})"


def alias_rule_from_identity(
    predicate_canonical: str,
    subject: str,
    obj: str,
    *,
    subject_type: Optional[str],
    confidence: Optional[float] = None,
) -> Optional[Tuple[str, str]]:
    """(alias, canonical) when the fact is an explicit identity statement that
    creates an alias on its own; otherwise None.

    "Gordon renamed_to Korely"   -> ("Gordon", "Korely").
    "Deepgram replaces Soniox"   -> None (two entities, not two names).
    "Korely named Gordon"        -> None (ambiguous: stays a manual decision).

    `confidence` is accepted and not read (2.12.0: the module docstring), so
    the callers written before keep working; the alias row still stores it.
    """
    direction = IDENTITY_ALIAS_DIRECTION.get((predicate_canonical or "").strip().lower())
    if direction is None:
        return None
    if (subject_type or "unknown") not in TYPES_THAT_RENAME:
        return None
    if not valid_name(subject) or not valid_name(obj):
        return None
    s, o = subject.strip(), obj.strip()
    if name_key(s) == name_key(o):
        return None
    new, old = (o, s) if direction == "object" else (s, o)
    return (old, new)


def new_alias_outcome(
    alias_map_of: Dict[str, str],
    alias: str,
    canonical: str,
    *,
    source: str,
    existing_source: Optional[str],
    existing_canonical: Optional[str],
) -> Tuple[str, Optional[str]]:
    """Decide what writing "alias -> canonical" does, before touching a table.

    Returns (outcome, final canonical) with outcome one of:
        invalid    not two entity names, or an unknown source
        same       the two names are already the same entity
        conflict   it would form a cycle, or overrides a decision taken by hand
        created    a new alias row
        updated    the alias existed and now points somewhere else
    Rules: the canonical follows its own chain (A -> B with B -> C becomes
    A -> C); "Korely -> Gordon" while "Gordon -> Korely" exists is a conflict
    (undo first, then redo), never a silent flip; an alias set by hand is
    never rewritten by an automatism.

    6.21.0: a name by hand (`SOURCE_USER`) is read by `valid_declared_name`,
    one the engine makes by itself by `valid_name`; two names are one when
    their `name_key` is ("Café" composed and decomposed: "same", never an
    alias of a name to itself). `alias_map_of` is keyed by `name_key`
    (`store.alias_map_of`).
    """
    if source not in SOURCES:
        return "invalid", None
    alias = (alias or "").strip()
    canonical = (canonical or "").strip()
    rule = valid_declared_name if source == SOURCE_USER else valid_name
    if not rule(alias) or not rule(canonical):
        return "invalid", None
    a_low = name_key(alias)
    if name_key(canonical) == a_low:
        return "same", None
    if reaches(alias_map_of, canonical, a_low):
        return "conflict", None
    final_canonical = canonical_of(canonical, alias_map_of)
    if name_key(final_canonical) == a_low:
        return "same", None
    if existing_canonical is not None:
        if name_key(existing_canonical) == name_key(final_canonical):
            return "same", final_canonical
        if existing_source == SOURCE_USER and source != SOURCE_USER:
            return "conflict", None
        return "updated", final_canonical
    return "created", final_canonical
