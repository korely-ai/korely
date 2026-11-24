"""Quality gate on extracted facts (memory_core, BUG-110).

Real rows from a real memory, 2026-09-26, all active:

    Gordon owns 22                      Gordon owns 9
    Gordon uses images/Gordon Robot.png Gordon uses Gordon_chat_session_id
    Gordon knowledge_graph true         Gordon owns FREE_TIER_ID

Lines of technical notes (sprint recaps, analyses) read as facts about the
world. None of them is something a person or an agent needs to remember: a
bare count without a unit, a file path, a code identifier, a boolean.

This gate is deliberately narrow. It refuses SHAPES that are never a value
in any language (a path, a code identifier, a literal of a programming
language, a template placeholder), never topics and never words: no list of
English words decides here (the rule of 2026-09-27). "costs 20 EUR",
"limit 16000 characters", "born_in 1986" all pass.

A number is a value together with what it counts. BONIE (Saha, Pal, Mausam,
ACL 2017) extracts numerical facts as a number with its unit, and reads the
relation from the unit when the sentence does not name it ("square
kilometers" means area): the unit is what carries the meaning. A bare "22"
carries none, so it is refused unless it is a year (a date, checked against
the text for BUG-112). Whether a number deserves a unit is decided upstream
by the model, which the extractor prompt asks to write it
(`extraction.RULES_OBJECTS`); this is the net for the ones that come out
without.
"""
from __future__ import annotations

import re
from typing import Optional

_BARE_NUMBER = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
# Media and document files are never product names: "Gordon Robot.png" is a
# file wherever it appears.
_MEDIA_EXTENSION = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|pdf|csv|zip|log|mp3|mp4|wav|docx?|xlsx?)$", re.IGNORECASE)
# Code and config extensions ARE product names often enough ("Node.js",
# "d3.js", "Next.js"): they mark a path only together with a separator.
_CODE_EXTENSION = re.compile(r"\.(md|txt|json|ya?ml|py|ts|tsx|js|jsx|css|html|sql|env|sh|toml|ini|lock)$", re.IGNORECASE)
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
# snake_case / SCREAMING_CASE identifiers of code: at least one underscore,
# no spaces. "works_at" as an OBJECT is not a value, and neither is
# "FREE_TIER_ID" or "Gordon_chat_session_id".
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+$")
# Literals of programming languages (JSON, JavaScript, Python), not words,
# in the case each language writes them (3.3.0, the 6 Oct audit): the
# comparison ignored case, and "Nan", a person's name, was dropped as a
# boolean. JSON true/false/null, JavaScript undefined/NaN, Python
# True/False/None, nan as a dataframe prints it, and SQL and YAML's
# TRUE/FALSE/NULL/Null (the review of 7 Oct evening).
_CODE_LITERALS = frozenset({"true", "false", "null", "undefined", "NaN", "nan", "True", "False", "None",
                                  "TRUE", "FALSE", "NULL", "Null"})
_PLACEHOLDER = re.compile(r"^(?:<[^>]*>|\{[^}]*\}|\[[^\]]*\]|\.\.\.)$")

MAX_OBJECT_CHARS = 120


# A date written with its parts in any order and separator ("05/12/2024",
# "12.05.2024", "2024/12/05"): a value, never a path (3.3.0, the 6 Oct
# audit: "05/12/2024" has two slashes and no space and was dropped as a path).
_NUMERIC_DATE = re.compile(r"^\s*(\d{1,4})[-/.](\d{1,2})[-/.](\d{1,4})\s*$")


def _year_of_the_date(s: str) -> Optional[str]:
    """The four-digit year of a numeric date, first or last, or None."""
    m = _NUMERIC_DATE.match(s)
    if not m:
        return None
    for part in (m.group(1), m.group(3)):
        if len(part) == 4:
            return part
    return None


def _is_a_path(s: str) -> bool:
    """A file or directory path. A URL is a value (a website is a fine fact),
    one slash between words is prose ("TCP/IP", "iOS/Android", "24/7"), and a
    date is a date ("05/12/2024", 3.3.0)."""
    if _URL.match(s):
        return False
    if _NUMERIC_DATE.match(s):
        return False
    if _MEDIA_EXTENSION.search(s):
        return True
    has_separator = "/" in s or "\\" in s
    if has_separator and _CODE_EXTENSION.search(s):
        return True
    if s.startswith(("./", "../", "~/", "/")) or "\\" in s:
        return True
    return " " not in s and s.count("/") >= 2


_DATE_AS_VALUE = re.compile(r"^\s*((?:19|20)\d{2})(?:[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?\s*$")


def drop_reason(predicate_canonical: str, subject: str, obj: str, text: Optional[str] = None) -> Optional[str]:
    """Why this triple must not become a fact, or None when it may.

    The reason is a short English tag, logged by the engines so the drops can
    be counted instead of guessed. With `text` (the source text), a date used
    as a VALUE whose year is not written there is refused as invented: the MCP
    acceptance run (2026-09-27) saw "tested_on 2024-06" come out of a note whose only
    time word was "giugno".
    """
    s = (subject or "").strip()
    o = (obj or "").strip()
    if not s or not o:
        return "empty"
    if text is not None:
        m = _DATE_AS_VALUE.match(o)
        year = m.group(1) if m else _year_of_the_date(o)
        if year and not re.search(rf"(?<!\d){year}(?!\d)", text):
            return "object_is_an_invented_date"
    # An argument must be an entity or a concept that can stand in a relation
    # (TextRunner, Banko et al., IJCAI 2007, §3.2: "(29, dropped, instruments)"
    # is a tuple whose arguments are not well-formed; OPIEC-Clean, Gashteovski
    # et al., AKBC 2019, §4.2, keeps only arguments it recognizes as entities).
    # The shapes refused below (a path, a code identifier, a placeholder, a
    # code literal, a subject ending in _id/_key/_token) are ours, from BUG-110
    # (the real memory's rows, 26 Sep 2026: "Gordon uses images/Gordon
    # Robot.png", "Gordon owns FREE_TIER_ID"), not from a paper.
    for arg, name in ((s, "subject"), (o, "object")):
        if _is_a_path(arg):
            return f"{name}_is_a_path"
        if _IDENTIFIER.match(arg) and name == "object":
            return "object_is_a_code_identifier"
        if _PLACEHOLDER.match(arg):
            return f"{name}_is_a_placeholder"
    if _IDENTIFIER.match(s) and s.lower().endswith(("_id", "_key", "_token")):
        return "subject_is_a_code_identifier"
    if o in _CODE_LITERALS:
        return "object_is_a_bare_boolean"
    # A year is a date, not a count ("founded_in 2019"); whether it is written
    # in the text was checked above when the text is known.
    is_a_year = bool(re.fullmatch(r"(?:19|20)\d{2}", o))
    if _BARE_NUMBER.match(o) and not is_a_year:
        return "object_is_a_bare_number"
    if len(o) > MAX_OBJECT_CHARS:
        return "object_is_a_sentence"
    return None
