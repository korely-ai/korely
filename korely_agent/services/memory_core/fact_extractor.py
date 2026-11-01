"""Fact extraction for the Agents engine (memory_core).

The Agents cloud and the self-hosted product extract facts from a memory the
same way, and since 9 Oct 2026 they are one codebase. Until 2026-09-27 each
carried its own copy (the Cloud's `agent_facts/extractor.py`, korely-agent
`services/facts/extractor.py`), and the copies had drifted apart (241 lines). The self-hosted one had been fixed on 2026-09-11/12
for two failures the cloud never received (BUG-116): a 1024-token ceiling that
thinking tokens could fill before the JSON closed, and a failed or unreadable
model reply returned as "no facts", so the memory was marked processed and
never retried. The cloud had meanwhile received the quality gate that reads the
source text, which the self-hosted never got. Both are here now, once.

The engine passes one thing: `call(prompt, max_tokens) -> text`, the model
call with its own provider and usage tracking. Choosing the provider stays in
the engine, because billing and settings differ.

Grounding (an entity argument must occur in the memory): FActScore (Min et
al., EMNLP 2023) and SAFE (Wei et al., 2024) score a fact by whether its
source supports it. Batched extraction echoes a per-memory id so facts map
back to their memory (batch prompting, Cheng et al., 2023, arXiv:2301.08721).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import log_safe as _log
from . import relative_time as _relative_time
from .extraction import (JSON_FIELDS, RULES_ATTRIBUTES, RULES_STATES, RULES_ALL, comparable, read_evidence,
                         read_tense, drop)
from .model_json import read_answer
from .predicate_forms import canonicalize_predicate

logger = logging.getLogger(__name__)

# (prompt, max_tokens) -> the model's text. Raises when the provider fails.
Call = Callable[[str, int], str]


class ExtractionFailed(RuntimeError):
    """The model was asked, and its answer could not be used.

    Not the same as "the text held no facts": that is a result and ends with
    the memory marked done. A failed call, an empty reply or JSON cut off by
    the token ceiling means nobody looked, and marking that done tells the
    caller the memory was processed when it was not. Raising routes it into
    the failure handling both engines already have (the memory goes to
    'error', where it is counted and can be re-driven).
    """


class ExtractionProviderUnavailable(RuntimeError):
    """No model provider could be selected for fact extraction (never looked)."""


# Generous on purpose: thinking tokens are spent from the output budget, and
# 1024 was observed truncating the JSON of a two-line memory (korely-agent,
# 2026-09-11). The cost is the output emitted, not the output allowed.
MAX_TOKENS = 8192
# The text of one extraction call: a window (2.12.0). A longer memory is read
# in several windows (`windows`), never cut.
MAX_CONTENT_CHARS = 6000
# Facts per window, so per 6,000 characters read (2.12.0: it was per memory,
# when a memory was one window). A memory of 16,000 characters, three
# windows, can give up to 24 facts once the duplicates are merged. The prompts
# ask for "at most 8, highest-signal first" and the reader keeps the first 8.
# The number is ours and was never measured (set on 1 Jun 2026 as
# "defensive against runaway outputs", when the answer had 512 tokens). Taking
# the most salient memories of an exchange is a published pattern (Mem0,
# Chhikara et al., arXiv:2504.19413, §2.1: "a set of salient memories"), not
# a published value.
MAX_FACTS = 8
# How much of the end of a window the next one reads again (2.12.0): more than
# the longest evidence a fact keeps (`extraction.MAX_EVIDENCE`, 300, one
# sentence), so a sentence cut by the end of a window is whole at the start
# of the next one.
OVERLAP_CHARS = 500

_TENSE_FIELDS = JSON_FIELDS.replace("\n", "\n      ") + ","
_TENSE_FIELDS_BATCH = JSON_FIELDS.replace("\n", "\n          ") + ","

# Why the subject rule is phrased on the text and not on identity. A tester
# wrote two sentences about the same end user:
#
#     "Elena Poli works at Fabbrica Blu."        ->  k1 · works_at · ...
#     "Elena Poli now works at Officina Rossa."  ->  Elena Poli · works_at · ...
#
# Same person, two subjects, both facts stay open. Asking the model whether a
# named person IS the end user has no answer in the text (measured on six
# users: heads three times, tails three times). Asking what form the label has
# in the text is observable and answered the same way every time, which is what
# matters: two answers make two people. Measured: 1 broken pair of 4 and 2
# ways of choosing on 6 users before, 0 and 1 after.
_SUBJECT_RULE = """\
- DECIDE THIS FROM THE TEXT, NEVER FROM A GUESS ABOUT WHO SOMEBODY IS. You
  cannot tell whether "Elena Poli" is the end user or a colleague, and neither
  can we. What matters is not being right about that, it is answering the same
  way every time, because two answers make two people:
    * the text refers to the end user WITHOUT a proper name — first person, "the
      account holder", "the customer" — or names them by an identifier-like
      label ("Customer 7003", "Account 88", "Developer 9", "the holder of
      account 12"): subject = "{END_USER}".
    * the text gives a PROPER NAME for a person ("Elena Poli", "Marco Sala"):
      use that name as the subject, every time, even if they might be the end
      user. A name is stable across writes; a decision about identity is not.
  Getting this inconsistent does not produce a wrong fact, it produces a second
  person: the same person written twice lands under two subjects, neither
  supersedes the other, and both stay true forever."""

# The relation names in the two prompts below ("likes, prefers, works_at,
# lives_in ..." in the predicate field and rule) are a measured exception to
# the rule of no word lists in prompts (CONTRIBUTING.md), kept on purpose: they are
# examples of the FORM a predicate takes (short, snake_case), not a lookup the
# engine decides with. Without them (an experiment on memory_core 5.1.0)
# conversation 26 of LoCoMo went 129 against 134 with Gemini writing (5 won, 10
# lost) and
# 135 against 133 with gpt-oss, and the relation names scattered: closed facts
# +13% and +21%, distinct relations +25% and +12% (measured on
# 12 Oct 2026). Not adopted; a new attempt starts from that measure.
PROMPT_SINGLE = """\
You extract knowledge-graph facts from a short memory stored by a software
product about one of its end users.

Output JSON ONLY in this exact schema:
{
  "facts": [
    {
      "subject": "<entity name, or the end-user handle for facts about the end user themselves>",
      "subject_type": "<one of: person|organization|product|place|concept|event>",
      "predicate": "<verb or relation, lowercase snake_case, e.g. likes, prefers, works_at, lives_in, owns, costs>",
      "object": "<entity name or short literal value like a date or price>",
      "object_is_literal": false,
      {TENSE_FIELDS}
      "confidence": 0.0
    }
  ]
}

CONTEXT:
- This memory belongs to the end user identified as: "{END_USER}".
- If a fact is about the end user themselves (first person, or an implicit
  preference/trait/attribute with no other named subject), set
  subject = "{END_USER}" and subject_type = "person".
- If a fact names a DIFFERENT entity (a company, product, place, another
  person), use that entity as the subject with the correct subject_type
  (organization / product / place / person / concept / event).
{SUBJECT_RULE}

RULES:
- One fact per discrete statement. Do NOT invent facts the text does not state.
- GROUNDING (mandatory): emit a fact ONLY if the memory text DIRECTLY states that
  this subject has this relation to this object. Do NOT infer a relation from mere
  co-occurrence or surrounding context — e.g. do NOT link a person to a place just
  because both are mentioned in the memory. The object must be something the text
  actually asserts about the subject, using the memory's own words.
- predicate: a single short verb or verb-preposition phrase, lowercase,
  snake_case (likes, dislikes, prefers, works_at, lives_in, married_to, owns,
  uses, subscribes_to, costs, allergic_to, ...). NEVER single- or two-letter
  predicates ("a", "of", "in") — skip the fact instead.
- object_is_literal=true for dates, numbers, prices, or free-text values that
  are not named entities.
- confidence is your 0-1 estimate: 0.9 for explicit facts, 0.6 for inferred;
  below 0.5 means SKIP the fact.
- Output at most 8 facts, highest-signal first. If none, return {"facts": []}.

{CORE_RULES}

Memory:
{CONTENT}
"""

# The relation names here are the same measured exception as in PROMPT_SINGLE.
PROMPT_BATCH = """\
You extract knowledge-graph facts from SEVERAL short memories stored by a software
product about ONE of its end users. Each memory is delimited and tagged with a mem_id.

Output JSON ONLY in this exact schema:
{
  "memories": [
    {
      "mem_id": "<the exact mem_id of the memory these facts came from>",
      "facts": [
        {
          "subject": "<entity name, or the end-user handle for facts about the end user themselves>",
          "subject_type": "<one of: person|organization|product|place|concept|event>",
          "predicate": "<verb or relation, lowercase snake_case, e.g. likes, prefers, works_at, lives_in, owns, costs>",
          "object": "<entity name or short literal value like a date or price>",
          "object_is_literal": false,
          {TENSE_FIELDS_BATCH}
          "confidence": 0.0
        }
      ]
    }
  ]
}

CONTEXT:
- All memories below belong to the end user identified as: "{END_USER}".
- If a fact is about the end user themselves (first person, or an implicit
  preference/trait/attribute with no other named subject), set
  subject = "{END_USER}" and subject_type = "person".
- If a fact names a DIFFERENT entity (a company, product, place, another
  person), use that entity as the subject with the correct subject_type.
{SUBJECT_RULE}

RULES:
- Process EACH memory independently. A fact extracted from one memory must come
  ONLY from that memory's text — never carry an entity from one memory into another.
- Include EVERY mem_id in "memories", even if its "facts" array is empty.
- One fact per discrete statement. Do NOT invent facts the text does not state.
- GROUNDING (mandatory): emit a fact ONLY if THAT memory's text DIRECTLY states that
  this subject has this relation to this object. Do NOT infer from mere co-occurrence.
- predicate: a single short verb or verb-preposition phrase, lowercase, snake_case
  (likes, dislikes, prefers, works_at, lives_in, owns, uses, costs, allergic_to, ...).
  NEVER single- or two-letter predicates — skip the fact instead.
- object_is_literal=true for dates, numbers, prices, or free-text values.
- confidence is your 0-1 estimate: 0.9 explicit, 0.6 inferred; below 0.5 SKIP.
- At most 8 facts per memory, highest-signal first.

{CORE_RULES}

Memories:
{BLOCKS}
"""

ALLOWED_SUBJECT_TYPES = frozenset(
    {"person", "organization", "product", "place", "concept", "event", "unknown"}
)


@dataclass
class ExtractedFact:
    subject: str
    predicate_raw: str
    object: str
    # The model's own 0-1 estimate, stored with the fact and shown to the
    # developer; 0.0 when the model gave none. Never a reason to keep or drop
    # a fact (2.11.0): see `is_valid`.
    confidence: float
    object_is_literal: bool = False
    subject_type: str = "unknown"
    # current | past | planned, and the date the text says the fact became
    # true (None = the memory's own time).
    tense: str = "current"
    valid_from: Optional[datetime] = None
    # The words of the memory the fact was read from, when the model copied
    # them (2.5.0, extraction.read_evidence): the judge reads them next to
    # the triple. Never stored: a forgotten memory leaves no copy behind.
    evidence: Optional[str] = None
    # 3.3.0: the single day the fact's own words date, when they hold one
    # relative time the code dated (`the_day_it_happened`). Computed, never
    # written by the model. The write path begins the fact that day only when
    # the fact HAPPENED (`writing.write_fact(day_of_the_words=...)`).
    day_of_the_words: Optional[datetime] = None

    def is_valid(self) -> bool:
        """A fact needs its three parts. Until 2.11.0 it also needed a
        declared confidence of 0.5 or more, and a missing one counted as 0, so
        a fact the model wrote without the field was dropped. The confidence a
        model states is not a threshold: measured, verbalized confidence sits
        between 0.8 and 1.0 whether the answer is right or wrong, and barely
        separates the two (Xiong et al., "Can LLMs Express Their Uncertainty?",
        ICLR 2024; CONTRIBUTING.md, "the confidence a model states is not a
        threshold"). Whether a fact is emitted at all is the model's decision,
        asked in the prompt; the grounding in the memory's own words
        (`keep_grounded`) is ours."""
        return bool(
            self.subject and self.subject.strip()
            and self.predicate_raw and self.predicate_raw.strip()
            and self.object and self.object.strip()
        )


def _text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


# Words of any script, four characters or more. Until 2026-09-27 this was
# [a-z0-9]{4,}: a word in Cyrillic, Greek, Arabic or Hebrew produced no token
# at all, so an object not written verbatim in the text ("компания Ромашка"
# when the text says "Ромашка наняла Марию") was dropped as ungrounded, while
# the same case in English was kept. The match is still on whole words: an
# inflected form ("Москве" for "Москва") does not match in any language. CJK
# text has no spaces between words, so there the check stays the verbatim one.
_WORD = re.compile(r"[^\W_]{4,}", re.UNICODE)


def _words(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def argument_grounded(value: str, source_lower: str, source_words: set,
                       others: Sequence[str] = ()) -> bool:
    """An entity argument is grounded when it occurs in the memory: verbatim,
    or through one of its words of four characters or more that is its own.

    5.2.0 (15 Oct 2026): a word is not its own when it is
    BORROWED, that is when it belongs to `others`, another argument of the same
    memory's facts written verbatim in the memory, which names something else
    (it has a word this one does not). The VS Code demo, run end to end on 15
    Oct: reading "Customer 4821 moved to the Basic plan." the writer took the
    old plan from the earlier memories the prompt shows only for references,
    and "Priority Support plan" was kept because "plan" occurs, in "Basic
    plan". A statement is attributable to its source when the source supports
    it, not a word of another statement (Rashkin et al., "Measuring
    Attribution in Natural Language Generation Models", Computational
    Linguistics 49(4), 2023, the AIS definition: "According to S, P").
    "компания Ромашка" grounded by "Ромашка" stays: the other argument
    "Ромашка" names nothing this one does not. No list of words: only the
    memory's own arguments."""
    v = (value or "").strip().lower()
    if not v:
        return False
    if v in source_lower:
        return True
    mine = set(_WORD.findall(v))
    found = [w for w in mine if w in source_words]
    if not found:
        return False
    owners = []
    for a in others:
        a = (a or "").strip().lower()
        if a and a != v and a in source_lower:
            words = set(_WORD.findall(a))
            if words - mine:
                owners.append(words)
    return any(not any(w in p for p in owners) for w in found)


def read_facts(items, text: str = "", *, user: Optional[str] = None) -> List[ExtractedFact]:
    """The model's fact list, validated. Tense, date and the quality gate are
    the rules every engine shares (extraction.py), and the gate reads the
    source text (an identifier the text itself states is not a code path).
    `user` (3.3.0, `fixed_prefix`): a subject or object written as the
    rules' name for the end user (END_USER) is that handle, BEFORE the gate,
    which would read "END_USER" as a code identifier and drop the fact (the
    review of 7 Oct evening: "Maria mentors END_USER" was lost).

    Anything but a list raises ExtractionFailed (2.11.0). It returned [] until
    then, so an answer without its list (`{}`, `{"fact": ...}`, a list of
    the wrong name) read as "the text holds no facts": the memory was marked
    done, and on an edited memory `worker.write_memory(retire=)` then
    closed every fact of its previous version. An answer nobody can read is
    never "no facts" (CONTRIBUTING.md; BUG-116)."""
    if not isinstance(items, list):
        raise ExtractionFailed(f"the reply has no list of facts (got {type(items).__name__})")
    out: List[ExtractedFact] = []
    text_c = comparable(text)
    for item in items:
        if not isinstance(item, dict):
            continue
        subject = _text(item.get("subject"))
        predicate = _text(item.get("predicate"))
        obj = _text(item.get("object"))
        if user:
            subject = user if subject == END_USER_NAME else subject
            obj = user if obj == END_USER_NAME else obj
        if not subject or not predicate or not obj:
            continue
        # One or two letters is a function word or a copula ("is", "of", "è");
        # in a script without case (Chinese, Japanese, Thai) two characters are
        # a word ("住在", "喜欢"): kept (3.3.0, the review of 7 Oct). An
        # ASCII predicate is judged exactly as before.
        if len(predicate.replace("_", "")) < 3 and (
                predicate.upper() != predicate.lower() or not any(ch.isalpha() for ch in predicate)):
            continue
        st = _text(item.get("subject_type")).lower() or "unknown"
        if st not in ALLOWED_SUBJECT_TYPES:
            st = "unknown"
        # Stored as the model said it, 0.0 when absent or not a number: a
        # label, never a filter (2.11.0, `ExtractedFact.is_valid`).
        try:
            conf = float(item.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        reason = drop(canonicalize_predicate(predicate), subject, obj, text or None)
        if reason:
            # 3.0.0: the reason and the sizes, never the triple,
            # which is the memory's content (`log_safe`).
            logger.info("[memory_core] dropped a triple (subject %s, relation %s, object %s): %s",
                        _log.length(subject), _log.length(predicate), _log.length(obj), reason)
            continue
        tense, valid_from = read_tense(item, text)
        out.append(ExtractedFact(
            subject=subject, predicate_raw=predicate, object=obj,
            confidence=max(0.0, min(1.0, conf)),
            object_is_literal=bool(item.get("object_is_literal", False)),
            subject_type=st, tense=tense, valid_from=valid_from,
            evidence=read_evidence(item, text, comparable_text=text_c),
        ))
    return out


def keep_grounded(facts: List[ExtractedFact], snippet: str, anchor: str) -> List[ExtractedFact]:
    """Drop a triple whose entity object, or named subject, does not occur in
    THIS memory: an extractor can join entities from disjoint parts of a text
    into a relation the text never states ("Rachel lives_near Tampa" when
    Tampa was the user's trip). The end-user anchor need not appear verbatim,
    as subject or, since 3.3.0, as object: "Maria mentors me" was dropped
    because the handle is not in the text (the review of 7 Oct evening);
    literal objects are normalized and not gated. In the batched path it is
    also the safety net for a fact attributed to the wrong memory."""
    source_lower = snippet.lower()
    source_words = _words(snippet)
    anchor_lower = anchor.strip().lower()
    # 5.2.0: every argument of this memory's facts, for the borrowed word.
    others = [a for f in facts for a in (f.subject, f.object) if a]
    kept: List[ExtractedFact] = []
    for f in facts:
        subj = f.subject.strip().lower()
        subj_ok = subj in (anchor_lower, "user") or argument_grounded(f.subject, source_lower,
                                                                       source_words, others)
        obj_ok = (f.object_is_literal or f.object.strip().lower() in (anchor_lower, "user")
                  or argument_grounded(f.object, source_lower, source_words, others))
        if subj_ok and obj_ok:
            kept.append(f)
        else:
            logger.debug("[memory_core] dropped an ungrounded triple (subject %s, object %s, %s)",
                         _log.length(f.subject), _log.length(f.object),
                         "subject not in the text" if not subj_ok else "object not in the text")
    return [f for f in kept if f.is_valid()][:MAX_FACTS]


def read_json(call: Call, prompt: str, max_tokens: int, extract_json=None):
    """One model call, read as JSON: the object the schema asks for, or a
    bare list (a model in JSON mode sometimes drops the wrapper and answers
    `[]` or the list itself; that is an answer, not a failure). Raises
    ExtractionFailed when the call fails, the reply is empty or it is neither
    an object nor a list: none of those is "no facts".

    Read with the rule of `model_json` (2.11.0: the whole text, else the
    one object written in it, once or repeated identically), then with
    `extract_json`, the engine's own reader, when it passes one: it may return
    the value or the JSON text to parse. Until 2.11.0 the first step was a
    bare `json.loads` and the engines passed an extractor that cut from the
    first brace to the end, so `{"facts": []}\\n}` (one brace too many,
    gemini-3.1-flash-lite) failed both and ended 'error'."""
    try:
        raw = (call(prompt, max_tokens) or "").strip()
    except Exception as e:
        # 3.0.0: the error's class and codes, not its message
        # (`log_safe.error`), here and in the exception the engines log.
        logger.error("[memory_core] the model call failed, so this memory is NOT processed: %s",
                     _log.error(e))
        raise ExtractionFailed(f"the model call failed: {_log.error(e)}") from e
    if not raw:
        logger.error("[memory_core] the model returned nothing, so this memory is NOT processed")
        raise ExtractionFailed("the model returned nothing")
    data = read_answer(raw, extract_json)
    if not isinstance(data, (dict, list)):
        # 3.0.0: the reply's shape, never its start: the reply of
        # an extractor is the memory's words.
        logger.error(
            "[memory_core] could not read the model's reply as JSON. A reply "
            "that ends mid-object usually means the token ceiling was hit: %s",
            _log.answer(raw, data),
        )
        raise ExtractionFailed("model reply was not usable JSON")
    return data


# ---------------------------------------------------------------------------
# 2.12.0: a long memory is read in windows, never cut
# ---------------------------------------------------------------------------
#
# Until 2.12.0 every extractor read the first MAX_CONTENT_CHARS (6,000)
# characters of a memory and dropped the rest in silence, while the API
# accepts 16,000 (`limits.MAX_CONTENT_CHARS`): what was said after the 6,000th
# character was never read, and the memory ended 'ready' (code audit of 6 Oct).
# The decision of 6 Oct: read it all, in windows, one call each.
# The window keeps the size of one call: Liu et al. ("Lost in the Middle",
# TACL 2024) measured that "performance is often highest when relevant
# information occurs at the beginning or end of the input context, and
# significantly degrades when models must access relevant information in
# the middle of long contexts, even for explicitly long-context models", so
# one longer prompt is not the same reading as more prompts of the size the
# engine was measured on.
#
# The cuts follow the text's own structure, from the strongest to the
# weakest: a blank line (a paragraph), a line break, a space after a
# punctuation mark (Unicode category P*, any script), a space, a punctuation
# mark with no space after it (scripts written without spaces), and only
# then the character count. No list of words or abbreviations.
#
# Cost, said plainly: one extraction call per window. Up to 6,000
# characters, one call as before, with the same prompt byte for byte;
# 12,000 characters, three calls (the windows overlap by
# OVERLAP_CHARS); 16,000, the API's maximum, three. The facts of the
# windows are merged, the same triple once, and a window that fails fails
# the whole memory: never half its facts written and the memory 'ready'.

_PARAGRAPH = re.compile(r"\n[^\S\n]*\n")


def _punctuation(c: str) -> bool:
    return unicodedata.category(c).startswith("P")


def _cut(text: str, start: int, end: int) -> int:
    """Where a window that starts at `start` and may reach `end` ends: just
    after the last structural boundary of its second half, the strongest
    kind first (the comment above); `end` when there is none."""
    at = start + (end - start) // 2
    dash = text[at:end]
    paragraphs = list(_PARAGRAPH.finditer(dash))
    if paragraphs:
        return at + paragraphs[-1].end()
    row = dash.rfind("\n")
    if row >= 0:
        return at + row + 1
    spaces = [i for i, c in enumerate(dash) if c.isspace()]
    after_a_mark = [i for i in spaces if i > 0 and _punctuation(dash[i - 1])]
    if after_a_mark:
        return at + after_a_mark[-1] + 1
    if spaces:
        return at + spaces[-1] + 1
    marks = [i for i in range(1, len(dash)) if _punctuation(dash[i - 1])]
    if marks:
        return at + marks[-1]
    return end


def _resume(text: str, at: int, end: int) -> int:
    """Where the next window starts: just after the first space or
    punctuation mark at or after `at`, so it does not start in the middle of
    a word; `at` when there is none before `end`."""
    for i in range(at, end):
        c = text[i]
        if c.isspace() or _punctuation(c):
            return i + 1
    return at


def windows(text: Optional[str], *, width: int = MAX_CONTENT_CHARS,
             overlap: int = OVERLAP_CHARS) -> List[str]:
    """The text in windows of at most `width` characters, each ending at a
    structural boundary, each starting again up to `overlap`
    characters before the end of the one before. One window, the text
    itself, when it fits: the prompt of a short memory does not change.
    [] for a text with nothing in it."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= width:
        return [text]
    overlap = max(0, min(int(overlap), width // 3))
    out: List[str] = []
    start, n = 0, len(text)
    while start < n:
        end = min(n, start + width)
        if end < n:
            end = _cut(text, start, end)
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(start + 1, _resume(text, max(start + 1, end - overlap), end))
    return out


def _fact_key(f: "ExtractedFact") -> Tuple[str, str, str, str]:
    return (f.subject.strip().lower(), canonicalize_predicate(f.predicate_raw),
            f.object.strip().lower(), f.tense or "current")


def _without_duplicates(facts: Sequence["ExtractedFact"]) -> List["ExtractedFact"]:
    """The facts of the windows of one memory, the same triple once (the
    first, with its words): two windows that overlap read the same sentence.
    The write would reconfirm the second within the same memory, a source
    that is already there; merged here, it is not written twice."""
    seen: set = set()
    out: List[ExtractedFact] = []
    for f in facts:
        k = _fact_key(f)
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def _anchor_text(end_user_id: Optional[str]) -> str:
    return (end_user_id or "user").strip() or "user"


# 3.3.0, work item 8 (the prefix of the extraction prompt).
# A provider's prompt cache reuses only an identical prefix: Scaleway caches
# automatically and bills cached input at a discount (Generative APIs FAQ,
# read on 7 Oct 2026), Gemini 2.5 and later do the same implicitly. Until
# 3.3.0 the end user's handle sat in the rules about twenty lines in, so two
# end users never shared more than the first ~250 tokens of about 3,750. With
# `fixed_prefix` the rules name the end user END_USER and the handle is
# bound once, at the end, next to the parts that change with every call (the
# memories before, the day, the memory itself): everything before it is the
# same text for every end user of every customer. Off by default: off, the
# prompts are those of before, byte for byte. Measured with `cached_tokens`
# (both products log it since 7 Oct) before it is turned on.
END_USER_NAME = "END_USER"
_BINDING = (f'{END_USER_NAME} in the rules above is the end user "{{END_USER}}": write that '
           f'handle wherever they say {END_USER_NAME}.\n\n')
_FIRST_VARIABLE_PART = ("{PREVIOUS}", "Memory:\n{CONTENT}", "Memories:\n{BLOCKS}")


def _with_the_user(template: str, anchor: str, fixed_prefix: bool = False) -> str:
    """The template with the end user's handle: in place (until 3.3.0, and
    with `fixed_prefix` off), or bound once before the first part that
    changes with every call (`fixed_prefix`)."""
    if not fixed_prefix:
        return template.replace("{END_USER}", anchor)
    fixed = template.replace('"{END_USER}"', END_USER_NAME)
    if "{END_USER}" in fixed:
        raise ValueError("memory_core: {END_USER} outside quotes in an extraction prompt: the fixed prefix "
                         "would carry the handle")
    for mark in _FIRST_VARIABLE_PART:
        if mark in fixed:
            return fixed.replace(mark, _BINDING + mark, 1).replace("{END_USER}", anchor)
    raise ValueError("memory_core: no variable part in an extraction prompt to bind the end user before")


def _core_rules(states: bool = False, attributes: bool = False) -> str:
    """The rules every extractor shares (extraction.RULES_ALL) and, on
    request (each off by default, so the prompts stay those of 2.8.0 byte for
    byte until measured): `states` (3.1.0), a change of state is the new state
    (extraction.RULES_STATES); `attributes` (3.2.0), an attribute named in the
    predicate (extraction.RULES_ATTRIBUTES). The rule on the turns of a
    conversation (2.9.0, `turni`) left with 4.0.0: measured without gain."""
    rules = RULES_ALL
    if states:
        rules += "\n\n" + RULES_STATES
    if attributes:  # 3.2.0: an attribute named in the predicate (extraction.RULES_ATTRIBUTES)
        rules += "\n\n" + RULES_ATTRIBUTES
    return rules


# 5.5.0 (audit of 18 Oct 2026, finding 2): every extraction prompt ended its
# confidence line with "below 0.5 SKIP", a threshold on the confidence the
# model states, which the engine stopped using in 2.11.0 (`is_valid`):
# verbalized confidence sits between 0.8 and 1.0 whether the answer is right
# or wrong (Xiong et al., "Can LLMs Express Their Uncertainty?", ICLR 2024).
# With `without_threshold` the clause goes and the confidence stays an
# estimate stored with the fact; whether a fact is emitted is the GROUNDING
# rule's question. Off by default: off, the prompts are those of before, byte
# for byte, until measured.
_THRESHOLDS = (
    (" 0.9 for explicit facts, 0.6 for inferred;\n  below 0.5 means SKIP the fact.",
     " 0.9 for explicit facts, 0.6 for inferred."),
    (" 0.9 explicit, 0.6 inferred; below 0.5 SKIP.", " 0.9 explicit, 0.6 inferred."),
)


def _without_threshold(template: str, without_threshold: bool) -> str:
    """The template without its confidence threshold (`without_threshold`), or as it is."""
    if not without_threshold:
        return template
    present = [s for s in _THRESHOLDS if s[0] in template]
    if len(present) != 1:
        raise RuntimeError("memory_core: no single confidence clause in an extraction prompt")
    return _with_markers(template, present)


def extract(call: Call, content: str, end_user_id: Optional[str], extract_json=None, *,
           states: bool = False, attributes: bool = False,
           fixed_prefix: bool = False,
           without_threshold: bool = False) -> List[ExtractedFact]:
    """Facts of one memory. [] when the text holds none; ExtractionFailed when
    the model could not be read. A memory longer than one window is read in
    windows (2.12.0, `windows`): one call each, the facts merged,
    ExtractionFailed when any window fails."""
    anchor = _anchor_text(end_user_id)
    facts: List[ExtractedFact] = []
    for snippet in windows(content):
        facts.extend(_extract_window(call, snippet, anchor, extract_json, states, attributes,
                                      fixed_prefix, without_threshold))
    return _without_duplicates(facts)


def _extract_window(call: Call, snippet: str, anchor: str, extract_json,
                     states: bool = False, attributes: bool = False,
                     fixed_prefix: bool = False, without_threshold: bool = False) -> List[ExtractedFact]:
    """One extraction call on one window (the body of `extract` until 2.12.0)."""
    prompt = _with_the_user(
        _without_threshold(PROMPT_SINGLE, without_threshold)
        .replace("{SUBJECT_RULE}", _SUBJECT_RULE)
        .replace("{TENSE_FIELDS}", _TENSE_FIELDS)
        .replace("{CORE_RULES}", _core_rules(states, attributes)),
        anchor, fixed_prefix,
    ).replace("{CONTENT}", snippet)
    data = read_json(call, prompt, MAX_TOKENS, extract_json)
    # An object without its "facts" list is no answer: `read_facts` raises
    # (2.11.0). `{"facts": []}` is the answer "no facts".
    items = data if isinstance(data, list) else data.get("facts")
    return keep_grounded(read_facts(items, snippet, user=anchor if fixed_prefix else None), snippet, anchor)


def _with_text(items: Sequence[Tuple[str, str]]) -> Tuple[Dict[str, str], List[str]]:
    """The memories that have text: {mem_id: text} and their order. The whole
    text (2.12.0): a memory longer than one window does not go in a batched
    prompt (`_long_ones`)."""
    snippets: Dict[str, str] = {}
    ordered: List[str] = []
    for mem_id, content in items:
        snip = (content or "").strip()
        if snip:
            snippets[str(mem_id)] = snip
            ordered.append(str(mem_id))
    return snippets, ordered


def _long_ones(snippets: Dict[str, str], ordered: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(the memories that fit one window, those that do not). A long memory
    is read in windows, one call each (`extract`): in a batched prompt it
    would be cut, or would take the room of the whole batch (2.12.0)."""
    short_ones = [m for m in ordered if len(snippets[m]) <= MAX_CONTENT_CHARS]
    return short_ones, [m for m in ordered if len(snippets[m]) > MAX_CONTENT_CHARS]


# 1.2.0 to 3.3.1: `estrai_lotto` and `estrai_lotto_risposte`, the batched
# extraction without the observation. No engine called them (the two
# products write one memory per call), only tests did: gone with 4.0.0
# (12 Oct 2026). PROMPT_BATCH stays as the base of
# PROMPT_BATCH_OBSERVATION, and `extract_batch_with_observation` keeps the
# batched path, with the same contract (a memory nobody answered for is
# absent).


# ---------------------------------------------------------------------------
# 2.6.0: the observation, and the memories before (new functions, opt-in)
# ---------------------------------------------------------------------------
#
# Mem0 (Chhikara et al., 2025, arXiv:2504.19413, section 2.1) extracts with the
# conversation summary and the last m=10 messages in the prompt, and stores
# short natural-language memories about the user; LongMemEval (Wu et al., ICLR
# 2025, section 5) finds that facts about the user indexed next to each round
# raise recall, and that time-aware reading needs the date of each round.
# Measured on our engine (LongMemEval run A, 2026-10-01): the search finds the
# evidence and the context loses it, because whole turns fill the budget and
# general-knowledge facts taken from assistant answers crowd it. So the write
# asks, in the SAME call, for one short sentence about the end user per
# memory, and reads the memories before it to resolve pronouns. Everything
# above, prompts included, stays as it was, byte for byte: an engine moves to
# these functions when it chooses to.

MAX_OBSERVATION_CHARS = 400
# Mem0's own numbers (mem0 `storage.get_last_messages(limit=10)`,
# `PAST_MESSAGE_TRUNCATION_LIMIT = 300`, read in its code, 2.6.1).
MAX_PREVIOUS = 10
MAX_PREVIOUS_CHARS = 300

RULES_OBSERVATION = """\
- observation: ONE sentence, at most 40 words, that says what this memory tells
  about the end user, in the third person ("The user ..."). Keep the names,
  numbers, places, prices and dates the memory states, exactly as written.
  When the memory is something said to the user, tell it around the user
  ("The user was recommended ...", "The user was told ...") with its
  specifics. Turn a relative time ("last Sunday", "two weeks ago") into a date
  when the date of the memory is given. You may read the earlier memories to
  understand who or what a pronoun means; everything you write must still come
  from this memory. An empty string when the memory holds nothing about the
  user or their world.
- Facts are about the end user and about the people, organizations, products,
  places and events of their life or work. Do NOT extract general knowledge the
  text only explains (a definition, a historical or scientific fact, how
  something works, who wrote a work) unless the end user did something with it
  (read it, bought it, chose it, plans it): then the fact is about the end user
  ("{END_USER}" reads "Python Data Science Handbook")."""

_OBSERVATION_FIELD = '"observation": "<one sentence about the end user, or empty>",'


def _observation_rules(times: bool = False, faithful: bool = False) -> str:
    """RULES_OBSERVATION and, on request (each switch off by default, so the
    prompt is the 2.6.0 one byte for byte unless it is asked for):
      - `times` (2.9.0): the relative times described for the code to date
        (relative_time.RULES_RELATIVE_TIMES), and the observation keeps
        them in the memory's words (TIMES_IN_THE_TEXT_SENTENCE);
      - `faithful` (3.1.0): the observation says only what the memory says, with
        the caption of a shared image and the referent of a pronoun
        (_FAITHFUL_OBSERVATION).
    The 2.8.0 switches that asked the model to write the dates itself
    (`date_assolute`, the comment above _RELATIVE_DATES_SENTENCE) and to write the
    end user's standing instructions (`istruzioni`) left with 4.0.0 and 5.0.0."""
    if times:
        rules = RULES_OBSERVATION_TIMES
    else:
        rules = RULES_OBSERVATION
    if faithful:
        rules = _with_markers(rules, _FAITHFUL_OBSERVATION)
    if times:
        rules += "\n\n" + _relative_time.RULES_RELATIVE_TIMES
    return rules


def _with_markers(text: str, replacements: Sequence[Tuple[str, str]]) -> str:
    """The 2.6.0 prompts are the old ones with a few insertions, so the shared
    rules cannot drift between the two; a marker that is gone fails at import,
    never silently at run time."""
    for old, new in replacements:
        if text.count(old) != 1:
            raise RuntimeError(f"memory_core: prompt marker not found exactly once: {old!r}")
        text = text.replace(old, new, 1)
    return text


# 2.8.0: relative times (LoCoMo conversation 26, 5 Oct: 13 of the
# 31 wrong answers were the reader's dates, and the stored observations kept
# "yesterday", "last Saturday", "two weekends ago" as written, so the reader
# counted them from today). TimeML (Pustejovsky et al. 2003, TIMEX3) treats an
# underspecified expression ("Monday", "next month", "two days ago") as a
# function of an anchor, the document's creation time: "last week" is the
# week before the anchor's. The anchor of a memory is its date, which the
# prompt already gives. The rule says what kind of expression to resolve,
# never which words signal it.
# 2.8.0 to 3.3.1 also had `date_assolute` (FRASE_DATE_ASSOLUTE in place of the
# sentence below: the model writes every relative time as a date). It
# converted 1 of 40 relative times (LoCoMo conversation 26, 5 Oct),
# the dates computed by code below replaced it, and switched on in the
# Self-hosted it failed every write, because this module refused it together
# with `times` (korely-agent 5510f07): gone with 4.0.0 (12 Oct 2026).
_RELATIVE_DATES_SENTENCE = (
    "Turn a relative time (\"last Sunday\", \"two weeks ago\") into a date\n"
    "  when the date of the memory is given."
)

# 2.9.0: relative times dated by code (`relative_time`, a proposal of
# 5 Oct, korely-agent f673ab3). The model describes each relative time in
# "relative_times" and the code dates it from the memory's day. SCATE (Su,
# Yu, Howard, Bethard 2025, arXiv:2507.06450, Table 2): the same model scores
# 0.49 when it writes the operation and code executes it, 0.38 when it writes
# the interval; TReMu (Ge et al. 2025, arXiv:2502.01630, Table 5, GPT-4o) and
# PREMem (Kim et al. 2025, arXiv:2509.10852, section 3.1) date the memory when
# it is written (71.50 against 41.67 overall in TReMu; up to -16.4% without the
# step in PREMem's Table 4). So the observation keeps the memory's own words,
# and the block puts the computed date next to them
# (`presentation.memory_line`): the sentence that asked the model to turn
# them into a date is replaced, never stacked with the new paragraph.
TIMES_IN_THE_TEXT_SENTENCE = (
    "Keep a relative time in the words of the memory: the code dates it\n"
    "  from relative_times."
)
RULES_OBSERVATION_TIMES = _with_markers(RULES_OBSERVATION, [
    (_RELATIVE_DATES_SENTENCE, TIMES_IN_THE_TEXT_SENTENCE),
])

# A faithful observation (3.1.0, 2026-10-06). Two acceptance runs with the real model
# and the whole LoCoMo found the observation adding what the memory
# does not say. (1) The rule asked for every observation "in the third person
# ("The user ...")", so a memory about someone else gained a link to the end
# user: "Globex moved to the Starter plan" became "The user's organization,
# Globex, ...", a notice to a customer became "The user was informed that ...".
# (2) It lost what a shared image shows and whom a pronoun points to: "[shared
# an image: <caption>]" became a vague action, "we just did it yesterday"
# became "did the activity yesterday" (about 10% of the reader's errors on
# LoCoMo). The observation is a summary that must stay entailed by its source
# (the faithfulness of summaries, Maynez et al., ACL 2020: hallucinated
# content is what the source does not support), and the referent of an
# anaphora is resolved from the context before it (coreference). Off by
# default (`faithful`), so the measured prompts stay byte for byte until it is
# measured on a bench. What to recognise, never which words signal it.
# 3.1.2, after a test (40 LoCoMo memories the reader got wrong with
# the evidence in the block, gpt-oss-120b, one extraction per side, about
# 0.04 EUR): the caption helped (3 better, 0 worse on 20 photos; 26% of the
# caption's words kept against 15%), the referent sentence did not (0 better,
# 1 worse on 20), and twice "last week" became a vaguer time. So the referent
# sentence is gone and the time keeps its precision. Not significant yet
# (p 0.25): an indication, measured again before it is switched on.
_FAITHFUL_OBSERVATION = (
    ('about the end user, in the third person ("The user ...").',
     'about the end user and their world. Name the end user ("The user ...") only\n'
     '  for what the memory says about them, by them or to them: a memory about\n'
     '  someone or something else keeps its own subject, and never gains a link to\n'
     '  the end user that it does not state.'),
    ("  specifics. ",
     "  specifics. When the memory shares an image or a file, keep what its caption\n"
     "  or description shows. Keep every time the memory names as precise as it is\n"
     "  said: never a vaguer time in its place. "),
)

# 2.8.0 to 3.2.0: REGOLE_EVENTI, the events said in passing written as facts of
# their own (TimeML OCCURRENCE vs STATE). Measured in the 3.3.0 pack (8 Oct,
# LoCoMo conversation 26): worse with both writers (gpt-oss 128 to 125 answers
# with closures from 5 to 16; Gemini 135 to 127), never switched on in either
# product. Removed from the code (a "clean environment", 8 Oct).

PROMPT_SINGLE_OBSERVATION = _with_markers(PROMPT_SINGLE, [
    ('{\n  "facts": [', '{\n  ' + _OBSERVATION_FIELD + '\n  "facts": ['),
    ("{CORE_RULES}", "{OBSERVATION_RULES}\n\n{CORE_RULES}"),
    ("Memory:\n{CONTENT}", "{PREVIOUS}Memory{MEMORY_DATE}:\n{CONTENT}"),
])

PROMPT_BATCH_OBSERVATION = _with_markers(PROMPT_BATCH, [
    ('"mem_id": "<the exact mem_id of the memory these facts came from>",\n      "facts": [',
     '"mem_id": "<the exact mem_id of the memory these facts came from>",\n      '
     + _OBSERVATION_FIELD + '\n      "facts": ['),
    ("{CORE_RULES}", "{OBSERVATION_RULES}\n\n{CORE_RULES}"),
    ("Memories:\n{BLOCKS}", "{PREVIOUS}Memories:\n{BLOCKS}"),
])

# 2.9.0: the field the RELATIVE TIMES paragraph asks for, next to the
# observation, in the single and in the batched prompt; only with `times`, so
# the prompts of 2.8.0 stay as they were.
_TIMES_FIELD = '"relative_times": [<one object per relative time, see RELATIVE TIMES; [] if none>],'

PROMPT_SINGLE_OBSERVATION_TIMES = _with_markers(PROMPT_SINGLE_OBSERVATION, [
    (_OBSERVATION_FIELD + '\n  "facts": [', _OBSERVATION_FIELD + '\n  ' + _TIMES_FIELD + '\n  "facts": ['),
])

PROMPT_BATCH_OBSERVATION_TIMES = _with_markers(PROMPT_BATCH_OBSERVATION, [
    (_OBSERVATION_FIELD + '\n      "facts": [',
     _OBSERVATION_FIELD + '\n      ' + _TIMES_FIELD + '\n      "facts": ['),
])

# Free text (the memories) goes in with one pass over the template, so a
# memory that happens to contain "{CONTENT}" or "{END_USER}" stays as written.
_FREE_PLACEHOLDER = re.compile(r"\{(PREVIOUS|CONTENT|BLOCKS)\}")


def _with_texts(template: str, **values: str) -> str:
    return _FREE_PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), template)


@dataclass
class Extraction:
    """What one memory yields in the 2.6.0 mode. `observation`: the short
    sentence about the end user; "" when the model said the memory holds
    nothing about them; None when the reply had no such field.

    `times` (2.9.0, only with `times=True`): the memory's relative times read
    and dated from its day, the value of its JSON column
    (`relative_time.annotate`: {"v", "anchor", "times"}, "times" [] when the
    model listed none). None when they were not asked, when the reply had no
    "relative_times" field (nobody answered: not the same as "none"), or when
    the memory's day is not known."""
    facts: List[ExtractedFact]
    observation: Optional[str] = None
    times: Optional[dict] = None


def the_day_it_happened(facts: List[ExtractedFact], times: Optional[dict]) -> List[ExtractedFact]:
    """The facts of one memory, each with the single day its own words date
    (3.3.0, a proposal of the 2026-10-06 quality reading, with
    its limits of 7 Oct).

    A fact takes `day_of_the_words` when all of these hold:
      1. it is current: a past fact is a state that ENDED (`extraction.
         RULES_TENSE`), and a time in its words usually dates the end ("I
         worked at Acme until I quit yesterday"), never its start (the
         independent review of 7 Oct evening); a plan is not yet;
      2. its own words (`evidence`) hold exactly one of the memory's relative
         times, whole words, and its value is a single day (not a week, a
         month, a range or an approximate count);
      3. that day is not after the memory's day.
    The write path uses the day only for a current fact of the events family
    (`writing.write_fact`), and only when the engine passes it (a switch,
    off until measured, with the events rule of 2.8.0). A state that is still
    true is never moved, because a time in the same sentence may date
    something else ("I was just remembering yesterday how much I love
    volunteering": yesterday dates the remembering). Everything else keeps
    the rule of before: the start is the memory's time.
    The day is computed by the code from the memory's day
    (`relative_time.resolve`), so the guard on invented dates
    (`fact_time.date_from_text`, which keeps the model from writing dates) does
    not apply: nothing here was written by the model. Graphiti (Rasmussen et
    al. 2025, arXiv:2501.13956, section 2.2.3) reads t_valid of an edge from
    the episode's text relative to its reference time; this is the same,
    with the relative time resolved by code."""
    if not facts or not times:
        return facts
    day = times.get("anchor") if isinstance(times.get("anchor"), str) else None
    times_stated = [(comparable(t.get("text")), t) for t in (times.get("times") or [])
                   if isinstance(t, dict) and comparable(t.get("text"))]
    if not times_stated:
        return facts
    out: List[ExtractedFact] = []
    for f in facts:
        if f.tense == "current" and f.evidence and f.day_of_the_words is None:
            words = comparable(f.evidence)
            # Whole words in a spaced script, a substring where words are not
            # spaced ("昨天" in "我昨天去了"), as `relative_time` matches them.
            inside = [t for key, t in times_stated if _relative_time._contains(words, key)]
            if len(inside) == 1:
                t = inside[0]
                start = t.get("start")
                if (t.get("granularity") == "day" and isinstance(start, str) and start == t.get("end")
                        and not t.get("mod") and (day is None or start <= day)):
                    try:
                        f = replace(f, day_of_the_words=datetime.fromisoformat(start).replace(tzinfo=timezone.utc))
                    except ValueError:
                        pass
        out.append(f)
    return out


def _observation(value) -> Optional[str]:
    if value is None:
        return None
    return " ".join(_text(value).split())[:MAX_OBSERVATION_CHARS]


def _times_of_the_answer(entry, day: Optional[str], text: str) -> Optional[dict]:
    """The "relative_times" of one memory's answer (2.9.0), read with
    `relative_time.read` and dated from the memory's day `day`
    (YYYY-MM-DD) by `relative_time.annotate`; an object whose words are not in
    the memory's text is left out, as the evidence is. The objects left out
    go to the log at info level, each with its reason (the same proposal)."""
    if not isinstance(entry, dict) or entry.get("relative_times") is None:
        return None
    if not isinstance(entry.get("relative_times"), list):
        # Not an answer either: "none" would be a claim nobody made. Its type,
        # not its value (3.0.0).
        logger.info("[memory_core] relative_times is a %s, not a list: left out",
                    _log.type_name(entry.get("relative_times")))
        return None
    day = _text(day)
    if not day:
        logger.info("[memory_core] relative times not dated: the memory's day is not known")
        return None
    try:
        column, dropped = _relative_time.annotate(entry.get("relative_times"), day, text=text)
    except TypeError:
        logger.warning("[memory_core] relative times not dated: the memory's day %r is not YYYY-MM-DD", day)
        return None
    for item, reason in dropped:
        # 3.0.0: the reason without the values it quotes and the
        # fields of the object, never its words ("text" is the memory's).
        logger.info("[memory_core] relative time left out (%s): %s", _log.without_values(reason),
                    _log.fields(item, _relative_time.SCHEMA["properties"]))
    return column


def _previous_section(previous: Sequence[str]) -> str:
    """The memories before, oldest first, at most MAX_PREVIOUS and each cut at
    MAX_PREVIOUS_CHARS: for pronouns, like Mem0's last m messages."""
    rows = [" ".join(_text(p).split())[:MAX_PREVIOUS_CHARS] for p in list(previous or [])]
    rows = [r for r in rows if r][-MAX_PREVIOUS:]
    if not rows:
        return ""
    body = "\n".join(f"- {r}" for r in rows)
    return (
        "Earlier memories of the same end user, oldest first. Read them only to "
        "understand pronouns and references in what follows; take no fact and no "
        "observation from them:\n" + body + "\n\n"
    )


def extract_with_observation(
    call: Call,
    content: str,
    end_user_id: Optional[str],
    extract_json=None,
    *,
    previous: Sequence[str] = (),
    memory_date: Optional[str] = None,
    times: bool = False,
    states: bool = False,
    attributes: bool = False,
    faithful: bool = False,
    fixed_prefix: bool = False,
    without_threshold: bool = False,
) -> Extraction:
    """`extract` plus the observation (2.6.0). `previous`: the texts of the
    end user's memories before this one, oldest first; `memory_date`: the memory's
    day (YYYY-MM-DD), for relative times. `times` (2.9.0, off by
    default): the model also describes each relative time of the memory
    ("relative_times") and they come back dated from `memory_date` in
    `Extraction.times`; the facts' `valid_from` does not take those days (the
    guard on invented dates, `fact_time.date_from_text`, stays as it is).

    A memory longer than one window (2.12.0) is read in windows, one call
    each, with the same `previous` and the same day: the facts merged, the
    observations of the windows one after the other, the relative times
    together (None when any window did not answer for them), and
    ExtractionFailed when any window fails."""
    pieces = windows(content)
    if not pieces:
        return Extraction([], None)
    anchor = _anchor_text(end_user_id)
    parts = [
        _extract_window_with_observation(
            call, snippet, anchor, extract_json, previous=previous, memory_date=memory_date,
            times=times, states=states, attributes=attributes, faithful=faithful, fixed_prefix=fixed_prefix,
            without_threshold=without_threshold,
        )
        for snippet in pieces
    ]
    if len(parts) == 1:
        return parts[0]
    return _merge_extractions(parts, times=times)


def _merge_extractions(parts: Sequence[Extraction], *, times: bool) -> Extraction:
    """The windows of one memory as one `Extraction` (2.12.0)."""
    facts = _without_duplicates([f for p in parts for f in p.facts])
    phrases: List[str] = []
    for p in parts:
        if p.observation and p.observation not in phrases:
            phrases.append(p.observation)
    if phrases:
        observation: Optional[str] = " ".join(phrases)
    elif any(p.observation is not None for p in parts):
        observation = ""
    else:
        observation = None
    column = None
    if times and all(p.times is not None for p in parts):
        column = dict(parts[0].times)
        seen: set = set()
        together = []
        for p in parts:
            for t in p.times.get("times") or []:
                key = comparable(t.get("text"))
                if key in seen:
                    continue
                seen.add(key)
                together.append(t)
        column["times"] = together
    return Extraction(facts, observation, column)


def _extract_window_with_observation(
    call: Call, snippet: str, anchor: str, extract_json, *, previous: Sequence[str],
    memory_date: Optional[str], times: bool,
    states: bool = False, faithful: bool = False, attributes: bool = False, fixed_prefix: bool = False,
    without_threshold: bool = False,
) -> Extraction:
    """One call of `extract_with_observation` on one window (its body until
    2.12.0)."""
    template = _with_the_user(
        _without_threshold(PROMPT_SINGLE_OBSERVATION_TIMES if times else PROMPT_SINGLE_OBSERVATION,
                      without_threshold)
        .replace("{SUBJECT_RULE}", _SUBJECT_RULE)
        .replace("{TENSE_FIELDS}", _TENSE_FIELDS)
        .replace("{OBSERVATION_RULES}", _observation_rules(times, faithful))
        .replace("{CORE_RULES}", _core_rules(states, attributes))
        .replace("{MEMORY_DATE}", f" (date: {_text(memory_date)})" if _text(memory_date) else ""),
        anchor, fixed_prefix,
    )
    prompt = _with_texts(template, PREVIOUS=_previous_section(previous), CONTENT=snippet)
    answer = read_json(call, prompt, MAX_TOKENS, extract_json)
    if isinstance(answer, list):
        items, obs = answer, None
    else:
        items, obs = answer.get("facts"), answer.get("observation")
    column = _times_of_the_answer(answer, memory_date, snippet) if times else None
    return Extraction(the_day_it_happened(keep_grounded(read_facts(items, snippet, user=anchor if fixed_prefix else None),
                                              snippet, anchor), column),
                    _observation(obs), column)


def extract_batch_with_observation(
    call: Call,
    items: Sequence[Tuple[str, str]],
    end_user_id: Optional[str],
    extract_json=None,
    *,
    previous: Sequence[str] = (),
    date: Optional[Dict[str, str]] = None,
    times: bool = False,
    states: bool = False,
    attributes: bool = False,
    faithful: bool = False,
    fixed_prefix: bool = False,
    without_threshold: bool = False,
) -> Dict[str, Extraction]:
    """The facts and the observation of several memories of ONE end user in
    one call (2.6.0). Each memory's facts are grounded against its own text;
    an id the model invents is dropped. A memory the model left out of
    `memories`, or answered without its `facts` list, is ABSENT from the
    result (the contract of 1.2.0: the prompt asks for every mem_id, so a
    missing one is a memory nobody answered for, and the caller asks for it
    alone); an entry with its mem_id and an empty `facts` list is the answer
    "no facts". ExtractionFailed on an unreadable reply. `date` maps a mem_id
    to its day. `times` (2.9.0, off): each memory's relative times, dated
    from its own day, as in `extract_with_observation`. A memory longer than
    one window (2.12.0) is left out of the batch and of the result: the
    caller asks for it alone, and `extract_with_observation` reads it in
    windows. No engine calls it today (both products write one memory per
    call): kept in 4.0.0 as the lever for a batched write."""
    snippets, ordered = _with_text(items)
    ordered = _long_ones(snippets, ordered)[0]
    if not ordered:
        return {}
    date = {str(k): _text(v) for k, v in (date or {}).items() if _text(v)}
    if len(ordered) == 1:
        only = ordered[0]
        return {only: extract_with_observation(call, snippets[only], end_user_id, extract_json,
                                              previous=previous, memory_date=date.get(only),
                                              times=times,
                                              states=states, attributes=attributes, faithful=faithful,
                                              fixed_prefix=fixed_prefix,
                                              without_threshold=without_threshold)}
    anchor = _anchor_text(end_user_id)
    blocks = "\n\n".join(
        f"[mem_id={mid}" + (f" date={date[mid]}" if mid in date else "") + f"]\n{snippets[mid]}"
        for mid in ordered
    )
    template = _with_the_user(
        _without_threshold(PROMPT_BATCH_OBSERVATION_TIMES if times else PROMPT_BATCH_OBSERVATION,
                      without_threshold)
        .replace("{SUBJECT_RULE}", _SUBJECT_RULE)
        .replace("{TENSE_FIELDS_BATCH}", _TENSE_FIELDS_BATCH)
        .replace("{OBSERVATION_RULES}", _observation_rules(times, faithful))
        .replace("{CORE_RULES}", _core_rules(states, attributes)),
        anchor, fixed_prefix,
    )
    prompt = _with_texts(template, PREVIOUS=_previous_section(previous), BLOCKS=blocks)
    # Scales with the batch (up to 8 facts of ~30 tokens and ~80 tokens of
    # observation per memory), never below the single-memory ceiling.
    max_tokens = max(MAX_TOKENS, 256 + 400 * len(ordered))
    answer = read_json(call, prompt, max_tokens, extract_json)
    mems = answer if isinstance(answer, list) else answer.get("memories")
    if not isinstance(mems, list):
        raise ExtractionFailed("batched reply without a 'memories' list")
    out: Dict[str, Extraction] = {}
    for entry in mems:
        if not isinstance(entry, dict):
            continue
        mid = _text(entry.get("mem_id"))
        if mid not in snippets or not isinstance(entry.get("facts"), list):
            continue
        column = _times_of_the_answer(entry, date.get(mid), snippets[mid]) if times else None
        out[mid] = Extraction(
            the_day_it_happened(keep_grounded(read_facts(entry.get("facts"), snippets[mid],
                                                  user=anchor if fixed_prefix else None),
                                      snippets[mid], anchor), column),
            _observation(entry.get("observation")),
            column,
        )
    missing = [mid for mid in ordered if mid not in out]
    if missing:
        logger.warning("[memory_core] the batched reply left out %d of %d memories: not answered for",
                       len(missing), len(ordered))
    return out
