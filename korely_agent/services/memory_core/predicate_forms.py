"""Surface-level predicate normalization (memory_core, shared by every engine).

The model writes a relation phrase ("is fond of", "works with", "lavora con");
this module gives it one snake_case surface form, so that the same phrase
written twice is the same key. It does nothing else.

What this is NOT:
    * Not a synonym resolver and not a lemmatizer: "likes" and "loves",
      "prefers" and "likes", "worked_with" and "works_with" stay distinct
      here. Merging different phrasings of one relation is the learned
      per-memory vocabulary's job (`vocabulary.py`), with the model and the
      type of the values, in every language: CESI (Vashishth et al., WWW
      2018, sections 7.3.1 and 8.1.1) beats hand-made morphological
      normalization, and Lin et al. (EMNLP 2010, section 3) type a relation
      by its arguments.
    * No word lists (the rule of 8 Oct 2026: "in the code we must
      not have any list of words"). Until 3.3.0 two English lists lived
      here, 25 auxiliary prefixes and 70 synonyms, and some of their merges
      were wrong: a preference excludes an alternative and a taste does not
      ("prefers" is not "likes"), a colleague is not an employer
      ("works_with" is not "works_at"), a company is not a resident
      ("based_in" became "lives_in"), a daughter is not a possession ("has"
      became "owns"). Measured before they went: a confirmation run
      (8 Oct, LoCoMo conversations 0-4, gpt-oss): 610 answers against 616
      with the lists, inside the reader's noise, closures 65 to 20.

The contract: the same raw input gives the same canonical form, in any script.
"""
from __future__ import annotations

import re
import unicodedata


def _form_in_any_script(s: str) -> str:
    """A predicate with letters outside ASCII (3.3.0, a finding of the 6 Oct
    audit). The pipeline below keeps [a-z0-9] only: "lavora_già" became
    "lavora_gi", and a predicate in Chinese, Japanese, Arabic or Russian
    became "" and then "unknown", so its fact was lost (CONTRIBUTING.md: a
    rule holds for every alphabet). Here the words of any script are kept, case folded
    (NFKC, as `extraction.comparable`), joined by underscores. A cased
    word of one or two letters is a
    function word or a copula ("è", "de") and reads as "unknown", as "of" or
    "a" do below; a script without case (Chinese, Japanese, Thai) has words
    of one or two characters that carry meaning, so it is kept."""
    t = unicodedata.normalize("NFKC", s).casefold()
    # Letters, marks and digits of any script are the word; the rest
    # separates. `\w` leaves out the combining marks (the vowel signs of
    # Devanagari and Thai, the dot of a Turkish "i"), which split one word in
    # two: "काम" became "क_म" (the review of 7 Oct evening).
    t = "".join(ch if unicodedata.category(ch)[0] in "LMN" else "_" for ch in t)
    t = re.sub(r"_+", "_", t).strip("_")
    if not t:
        return "unknown"
    # A relation phrase begins with a verb (ReVerb, Fader, Soderland, Etzioni,
    # EMNLP 2011, §3.1, Fig. 1), so "a", "of", "in" are no relation, and a bare
    # "is" is uninformative (ibid. §1, Table 2). The cut at two characters is
    # our proxy for both (1 Jun 2026: degenerate answers of the model), not the
    # paper's test, which is the part of speech: Table 2 also calls "has",
    # "made", "took" uninformative and they pass here, and a two-letter verb of
    # another language ("ha", "es") does not.
    if len(t) <= 2 and t.upper() != t.lower():
        return "unknown"
    return t


# The width of the vocabulary's columns (String(200)) and the cap the fact
# row already applies (writing.write_fact). NFKC and casefold can make a
# form longer than what was written ("ß" casefolds to "ss"): 150 of them gave
# 300 characters, the insert raised DataError, only IntegrityError was caught,
# and the write answered 500, or in the drain the whole memory went to error
# (a code review, 17 Oct 2026). A form past it is cut at a word
# boundary of the canonical form (an underscore), never inside a word.
MAX_PREDICATE = 200


def _within_the_column(form: str) -> str:
    """The canonical form within MAX_PREDICATE: cut at the last underscore
    that fits, or hard at the width when one word alone is longer."""
    if len(form) <= MAX_PREDICATE:
        return form
    cut = form[:MAX_PREDICATE]
    if "_" in cut:
        cut = cut[:cut.rindex("_")]
    return cut.strip("_") or "unknown"


def canonicalize_predicate(raw: str) -> str:
    """Return the canonical snake_case form of ``raw``.

    Pipeline: lowercase; trim and collapse whitespace; anything that is not a
    letter or a digit becomes an underscore. A predicate with letters outside
    ASCII takes `_form_in_any_script` (3.3.0). Empty input, or a
    form of one or two characters ("a", "of", "in": they assert nothing),
    returns the literal ``"unknown"``, so the column NOT NULL constraint holds
    even on a degenerate answer of the model.

    3.3.0: no prefixes are stripped and no synonyms are merged (the module
    docstring): the phrase keeps the form the model wrote ("loves",
    "is_fond_of", "works_with", "based_in", "has")."""
    if not raw or not raw.strip():
        return "unknown"

    # 3.3.0: letters or digits outside ASCII take the path of every script.
    if any(ch.isalnum() and not ch.isascii() for ch in raw):
        return _within_the_column(_form_in_any_script(raw))

    s = raw.lower().strip()
    # collapse whitespace to single space first so the regex doesn't
    # produce double-underscores
    s = re.sub(r"\s+", " ", s)
    # then replace any non-alphanumeric with underscore
    s = re.sub(r"[^a-z0-9]+", "_", s)
    # collapse repeated underscores + strip leading/trailing ones
    s = re.sub(r"_+", "_", s).strip("_")
    if not s or len(s) <= 2:
        return "unknown"
    return _within_the_column(s)
