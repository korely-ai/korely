"""What every extractor asks the model beyond the triple, and how the answer is read (memory_core).

The three extractors (personal notes, Agents memories, self-hosted Agents) have
different prompts for good reasons: a personal vault has one owner, an agent
store has end users. What must NOT differ is how they read TIME, NAMES and
VALUES, because the rest of the engine (supersede, aliases, recency) trusts
those fields the same way everywhere. So the rules are one text, appended by
each extractor, and the fields are read by one function.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Optional, Tuple

from .quality import drop_reason
from .fact_time import date_from_text, effective_tense

# JSON fields every extractor adds to each triple.
JSON_FIELDS = (
    '"tense": "<one of: current|past|planned>",\n'
    '"valid_from": "<YYYY-MM-DD, YYYY-MM or YYYY ONLY if the text states when this became true, else \\"\\">",\n'
    '"evidence": "<the words of the text that state this fact, copied exactly>"'
)

# Every rule below describes WHAT to recognise, never which words signal it
# (the rule of 2026-09-27: a list of trigger words, even inside a prompt,
# covers one language and breaks on the next). Graphiti (Rasmussen 2025,
# arXiv:2501.13956 §2.2.3) is the reference: the model reads time and identity
# from the context of the episode.
RULES_TENSE = """\
TENSE (mandatory):
- Write the relation itself in the PRESENT tense, whatever tense the text
  uses, and say WHEN it holds in "tense". The same relation stated in the past
  and stated today must be the same predicate, so that the memory can see that
  one closes the other.
- "current": the text presents the fact as true now.
- "past": the text presents the fact as having been true and no longer true,
  including text that describes an earlier state of something. A past fact is
  still worth emitting: the memory keeps history, marked as past.
- "planned": the text presents the fact as an intention, a proposal, a
  recommendation or something still to do. Never emit a plan as "current"."""

# The tense of an agent's memory with no end user (6.4.0, approved on 7 Nov
# 2026), and only there for now: the prompts of an end user's memory,
# and of a memory that names neither, keep RULES_TENSE byte for byte
# (fact_extractor, `*_WITHOUT_END_USER`).
# A team's memory is mostly procedures, rules in force and decisions with a
# lasting result, often told in the past tense ("the founder approved it on
# 6 November"). Read with "past" as above, 38 of the 168 facts of a team of
# agents came out past (7 Nov 2026), each closed from its birth (`writing`,
# a past fact is history), 32 with no fact that replaced them: among them
# the founder's approval of a charter and his revocation of a key, whose
# results still stand.
# Friedrich, Palmer, Pinkal (ACL 2016, "Situation entity types", Figure 1
# and section 2, after Smith 2003) keep apart an EVENT ("John won the race")
# from a STATE and from the General Statives, GENERIC and GENERALIZING
# sentences (habituals, "John cycles to work"), which "reflect regularities
# in the world": a procedure, a rule in force or a habit is a regularity
# that holds, not a happening that is over. Graphiti (graphiti_core/prompts/
# extract_edges.py, DATETIME RULES, read on 7 Nov 2026) sets an end only
# "If a change/termination is expressed", and a fact that is "ongoing" has
# only its start. So the result of something that happened and still stands
# is current from the day it happened, and "past" needs the text to say the
# state ended. What to recognise, never which words signal it (the rule of
# 2026-09-27); the examples in parentheses are kinds of fact, not words to
# look for.
TENSE_WITHOUT_END_USER = (
    ('- "past": the text presents the fact as having been true and no longer true,\n'
     '  including text that describes an earlier state of something. A past fact is\n'
     '  still worth emitting: the memory keeps history, marked as past.',
     '- "current" also for what holds as a standing rule: a procedure, a rule or a\n'
     '  decision in force, a habit, how something is done. And for something that\n'
     '  happened and whose result still stands (an approval given, an access\n'
     '  revoked, a version released): the result is "current", with valid_from the\n'
     '  day the text gives for it.\n'
     '- "past": ONLY when the text says that the state itself ended: it stopped, it\n'
     '  no longer holds, something else took its place. Telling something in the\n'
     '  past tense does not make it past. A past fact is still worth emitting: the\n'
     '  memory keeps history, marked as past.'),
)

RULES_DATE = """\
DATE (valid_from): fill it ONLY when the text says WHEN the fact became true
("since March 2026", "a marzo 2026", "in 2024", "from 2026-06-01"). Use the most
precise form the text supports (YYYY-MM-DD, YYYY-MM or YYYY). If the text gives
no date, or only a relative one ("last week", "yesterday"), leave it "". Never
guess a date."""

RULES_IDENTITY = """\
IDENTITY / RENAMES (mandatory): when the text states that an entity changed its
name, or that two names designate the same entity, emit ONE triplet that uses
exactly one of these predicates, with both names exactly as written:
- renamed_to (subject = OLD name, object = NEW name)
- formerly or codename (subject = CURRENT name, object = OTHER name)
- also_known_as (subject = main name, object = alternative name)
Use tense "current" and confidence 0.9+. Do not use these predicates for a
change of role, status or kind (a company becoming public, a person becoming
CTO): those are ordinary facts about the entity, not a new name. And do not
use them when a DIFFERENT entity takes another's place (a new provider, a
newer version, one product chosen instead of another): that is "replaces",
an ordinary fact between two entities that both keep their own name."""

RULES_OBJECTS = """\
VALUES ARE NOT NOISE (mandatory):
- An enumeration of options, providers or integrations ("OAuth: Google,
  GitHub, Facebook", "supports Notion, Trello, Asana") is NOT likes/dislikes.
  Emit likes/dislikes only for an explicitly stated preference.
- NEVER emit as object: a file path or file name (images/logo.png, src/app.py),
  a code identifier (FREE_TIER_ID, user_chat_session_id), a bare true/false, or
  a number without its unit or what it counts ("owns 22"). Always write a
  number together with its unit or what it counts, also when the text leaves
  it implicit and the context makes it clear ("costs 20 EUR", "has 12
  employees", "limit 16000 characters")."""

# The words a fact was read from (memory_core 2.5.0, 2026-10-01). The judge of
# contradictions reads triples, and a triple loses what decides many cases:
# "Luca also works part-time at Google" became `Luca works_at Google`, and the
# judge closed `Luca works_at Apple` (BUG-200). Ritter, Downey, Soderland,
# Etzioni (EMNLP 2008, "It's a Contradiction, No, it's Not") found that on
# functional relations with different values only 1.2% of the pairs were
# genuine contradictions, and that telling them apart needs the context of the
# sentence; the evidence sentence of fact verification (FEVER, Thorne et al.,
# NAACL 2018) is the same idea. The rule says WHAT to keep, never which words
# signal it (the rule of 2026-09-27).
RULES_EVIDENCE = """\
EVIDENCE (mandatory): in "evidence" copy the words of the text that state the
fact, exactly as they are written (same language, same spelling, nothing
added), at most one sentence. Keep the part that says how this fact stands
next to what was true before, because the triple alone loses it. If several
places state the fact, copy the clearest one."""

RULES_ALL = "\n\n".join((RULES_TENSE, RULES_DATE, RULES_IDENTITY, RULES_OBJECTS, RULES_EVIDENCE))

# An attribute is a relation with a name (3.2.0, 2026-10-07). "I am
# vegetarian." never became a fact: the model answers (user, is, vegetarian)
# and `fact_extractor.read_facts` drops every predicate under three letters (a
# guard meant for "a", "of", "in"), so every attribute said with the verb to
# be (a diet, a role, a status, a condition) was lost, in every language (the
# customer audit of the Cloud, 7 Oct; LoCoMo shows the same,
# "I'm lactose intolerant" read as `allergic_to lactose`). Accepting `is`
# would put different attributes under one relation and the judge would
# confuse them. A statement is "a property with at least one value"
# (Wikidata, Help:Statements: Marie Curie, occupation, physicist): the
# property names the attribute, never "is". The rule describes what to
# recognise, never which words signal it (the rule of 2026-09-27); its
# example is an invented shape of the output. Measured on 7 Oct on a bench
# (37 sentences in English, Italian and Spanish, read one by one for meaning;
# Gemini 3.1 Flash-Lite): off 13/37,
# with the first sentence of the rule 31/37, with the negation sentences
# 33/37, no sentence worse than off. Left: a bare negation ("I'm not
# vegetarian", "not allergic to anything") still gives a fact; "Sono di
# Napoli" read as lives_in, and "My sister is a doctor" on the user, wrong
# off as well. Off by default (`attributes` in the extractors) until the
# sequences of the bench (the judge on a moment's state next to a stable
# one) are measured: the prompts stay byte for byte.
RULES_ATTRIBUTES = """\
ATTRIBUTES: when the text says what the subject IS (a trait, a diet, a
condition, a role, a status), name the attribute in the predicate and put its
value in the object, as for any other relation (shape: subject
has_<attribute> value, e.g. has_diet vegetarian, has_role nurse). Never a bare
copula as the predicate. When the text says the subject is NO LONGER
something, emit that attribute with that value and tense "past"; when it only
says the subject is NOT something, emit nothing. Never a negated value as the
object ("not vegetarian", "nothing")."""

# A change of state is the new state (3.1.0, 2026-10-06). An acceptance run
# of the Self-hosted with gpt-oss-120b as the writer, twice 12/13: "Globex
# moved to the Starter plan." became an event in the past
# (`changes_plan_to_starter_plan`, then `moves_to`, tense past) instead of the
# new state, so the old plan stayed open and the block still said Business;
# with Gemini 3.1 Flash-Lite it did not happen. TimeML (Pustejovsky et al.,
# IWCS-5 2003, the EVENT classes) keeps a STATE apart from the OCCURRENCE
# that changes it, and Graphiti (Rasmussen et al. 2025, arXiv:2501.13956
# §2.2.3) invalidates the old edge when a new one contradicts it: the
# extractor writes the new state, current, and the judge closes the old one.
# The rule describes what to recognise, never which words signal it (the
# rule of 2026-09-27); its example is an invented shape of the output, like the
# examples of RULES_OBSERVATION and of the relative times, never a sentence
# of a bench or of an acceptance run. 3.1.1: the first wording also said that "a
# relation never contains its value", and with gpt-oss-120b it pushed the
# unit out of a count ("12 employees" became the bare number 12, dropped by
# the guard on bare numbers) while the plan change stayed an event: an
# acceptance run, 11/13 against 12/13. That sentence is gone, and the shape of
# the output shows what the words alone did not. Off by default (`states` in
# the extractors), so the measured prompts stay byte for byte until it is
# measured.
RULES_STATES = """\
CHANGE OF STATE: when the text says that something passed from one value to
another (a plan, a provider, a tool, a place, a job, a status), emit the state
itself with its NEW value: the relation of that state, the new value as the
object, tense "current", and valid_from when the text says when. The memory
compares it with the old value and closes that one. The passage is not a fact
of its own: never emit it as an event, in the past or in the present.
Shape of the output, in any language:
  "Acme switched its CRM to Salesforce in March 2026" ->
    {"subject": "Acme", "predicate": "uses_crm", "object": "Salesforce",
     "tense": "current", "valid_from": "2026-03"}"""

# Standing instructions (2.8.0, REGOLE_ISTRUZIONI: the end user's rules as
# `instructs` facts of the family `instructions`, shown by /v1/context
# whatever the question) left with 5.0.0: measured on 12 Oct 2026 on a
# procedural bench, 22 of 28 probes against 21, a tie; removed.

# Longer than one sentence is not what was asked; cut at a word, marked.
MAX_EVIDENCE = 300

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def read_tense(item: dict, text: str, *, now: Optional[datetime] = None) -> Tuple[str, Optional[datetime]]:
    """(tense, valid_from) of one extracted triple. `valid_from` is None unless
    the text states the date (see `fact_time.date_from_text`)."""
    tense = effective_tense(item.get("tense") if isinstance(item, dict) else None)
    raw = item.get("valid_from") if isinstance(item, dict) else None
    since = date_from_text(raw if isinstance(raw, str) else None, text, now=now)
    return tense, since


def comparable(text: Optional[str]) -> str:
    """The words of a text, any script, case folded, one space apart: what is
    compared to decide whether the evidence was copied. Punctuation, quotes
    and spacing a model may change while copying do not decide it."""
    return " ".join(_WORD.findall(unicodedata.normalize("NFKC", text or "").casefold()))


def read_evidence(item: dict, text: str, *, comparable_text: Optional[str] = None) -> Optional[str]:
    """The words the fact was read from, or None. Kept only when they occur in
    the source text (`comparable`): a paraphrase is the model's reading,
    not what was said, and a judge given the model's reading decides on it.
    None changes nothing: the judge reads the triple alone, as before 2.5.0.
    `comparable_text`: `comparable(text)`, computed once per text by
    a caller reading several facts."""
    raw = item.get("evidence") if isinstance(item, dict) else None
    if not isinstance(raw, str):
        return None
    phrase = " ".join(raw.split())
    key = comparable(phrase)
    if not key:
        return None
    source = comparable_text if comparable_text is not None else comparable(text)
    if key not in source:
        return None
    if len(phrase) > MAX_EVIDENCE:
        phrase = phrase[:MAX_EVIDENCE].rsplit(" ", 1)[0] + " ..."
    return phrase


def drop(predicate_canonical: str, subject: str, obj: str, text: Optional[str] = None) -> Optional[str]:
    """Why a triple must be dropped before it becomes a fact (BUG-110), or None.
    Pass the source `text` so an invented date can be recognised."""
    return drop_reason(predicate_canonical, subject, obj, text)
