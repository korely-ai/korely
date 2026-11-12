"""The learned predicate vocabulary: questions to the model and how to read the answers (memory_core, BUG-109).

The extractor writes open relation phrases, which is what makes the store
useful on a domain it has never seen: an insurer's `covered_under` and
`claim_status` are not in anybody's fixed vocabulary. The cost is that the same
relation comes back phrased differently every time. Over one unchanged sentence
the self-hosted product observed `has_plan`, `uses_plan`, `subscribes_to`,
`plan`, `uses` and `moved_to`, and contradiction detection matches candidates
on the canonical predicate, so a plan change written twice was stored as two
facts that never met. An earlier product's memory had the same disease in
another form: two thirds of 2015 facts in `other` (BUG-109).

This is a named problem: canonicalizing an Open KB (Galarraga et al., CIKM
2014; CESI, WWW 2018). Both cluster relation phrases rather than fixing a
vocabulary. Embedding distance alone does not work, measured in korely-agent
(2026-09-10): `uses_plan`/`has_plan` sit at 0.347 and must merge while
`lives_in`/`works_at` sit at 0.293 and must not. So embeddings narrow the field
to a shortlist and the model answers whether the new phrase names one of them.
Asked once per unseen phrase per memory, then cached forever.

This module holds the parts that must be identical everywhere: the three
questions (same relation? one value at a time? which family?) and the readers
of their answers. The measurements that chose every line of these prompts are
in korely-agent `services/predicates.py`; do not reword a prompt without
measuring it again.

What each engine does with them (the table, the embedding, the call) lives in
the engine, because each has its own database and its own model client.

2026-09-28 — each reader has a strict form (`severe=True`): an answer that is
none of those its question allows (empty, not JSON, the key missing, a value
of the wrong kind or out of range) raises `UnreadableAnswer` instead of
being read as "a new relation", "undecided", "other" or "not past". Those
readings are decisions and `store.resolve_predicate` caches them for good,
so an unusable answer read that way was a wrong decision nobody would ask
again: the asymmetry already removed for a failed call and for the
contradiction judge (a failed call is retried, an unusable answer is not a
result). The lenient form stays the
default for the callers written against it.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Sequence

from . import log_safe as _log
from .contradiction import example_on_one_line
from .families import FAMILY_ORDER
# A model's JSON, read as the extractor and the judge read it: one reader for
# the folder since 2.11.0 (`model_json`), and the vocabulary's own since
# 5.2.2.
from .model_json import read_object

logger = logging.getLogger(__name__)


class UnreadableAnswer(ValueError):
    """The model answered, and the answer is none of those the question
    allows. Raised only by the strict readers (`severe=True`)."""


# An unusable answer is asked again, at most this many times, before it is
# raised (2026-09-29, memory_core 2.2.0). With `severe` one unusable answer
# failed the whole write and the memory kept no facts: 3 writes in ~450 on the
# LongMemEval run of 29 Sep (family "interests", a word of the description, not
# a name; a JSON answer repeated with text around it), and the majority of three
# votes made it three times as likely.
RETRIES_UNREADABLE = 2


def ask_and_read(
    ask: Callable[[str, bool], Optional[str]],
    question: str,
    think: bool,
    read: Callable[..., object],
    *,
    severe: bool = False,
    retries: int = RETRIES_UNREADABLE,
):
    """`read(ask(question, think), severe=severe)`, asking again when the
    answer is unusable. The next asks think: the cheap call first, the stronger
    one only when the cheap answer cannot be used (the cascade of FrugalGPT,
    Chen, Zaharia, Zou 2023, arXiv:2305.05176, the chat's Flash/Pro choice). An
    unusable answer is never a result: after `retries` more asks the last
    `UnreadableAnswer` propagates, and nothing is cached. The lenient
    readers never raise, so without `severe` this is one ask, as before."""
    for attempt in range(retries + 1):
        try:
            return read(ask(question, think or attempt > 0), severe=severe)
        except UnreadableAnswer as e:
            if attempt >= retries:
                raise
            logger.warning("[vocabulary] unusable answer, asking again with thinking (%d/%d): %s",
                           attempt + 1, retries, e)
    raise AssertionError("unreachable")  # pragma: no cover


_MISSING = object()

# How many nearest clusters the model is asked to choose between. Enough to hold
# the right answer, short enough that the prompt stays cheap and the model does
# not start reaching.
SHORTLIST = 5

# Beyond a distance the clusters are not worth showing: including everything
# would make the shortlist noise rather than a shortlist. Deliberately loose,
# because the measurement says the boundary is not here, the model decides.
# The cutoff depends on the embedding model (2.4.0): `thresholds.current().shortlist`.
# (SHORTLIST_MAX_DIST, Gemini's value kept for code that imported it, left
# with 5.2.4: nobody did.)


def text_for_embedding(surface: str, object_label: str = "") -> str:
    """Side information: the argument. "has plan Business plan" is a better
    question than "has_plan", even though neither is sufficient alone."""
    return f"{(surface or '').replace('_', ' ')} {object_label or ''}".strip()


# ---------------------------------------------------------------------------
# 1. Is this new phrase one of the relations this memory already knows?
# ---------------------------------------------------------------------------

def _option(c) -> str:
    """A known relation, with the object it was first seen with when known:
    "subscribes_to (as in: ... Business plan)". Found by the MCP acceptance run
    (2026-09-27): offered bare, `subscribes_to` looked like "uses Salesforce"
    ("subscribes to Salesforce" reads fine), and the model merged a CRM into a
    subscription plan. The argument is the side information CESI relies on."""
    if isinstance(c, (tuple, list)):
        name, example = c[0], (c[1] if len(c) > 1 else None)
    else:
        name, example = c, None
    example = example_on_one_line(example)  # 5.6.0: one line, cut (contradiction)
    return f"{name} (as in: ... {example})" if example else f"{name}"


# 3.2.0 (2026-10-06): the kinds of the arguments are part of the relation.
# An acceptance run of 3.1.2 on the Self-hosted (gpt-oss-120b) resolved
# "subscribes to (as in: ... Slack)" into the known `on_plan` (as in: ...
# Business plan), single-valued, so Slack read as a plan, and `based_in` of a
# company into `lives_in`; on LoCoMo conversation 26 (6 Oct) a
# relation named after one instrument, single-valued on a frequency, absorbed
# the next instrument, and the judge closed one with the other. Lin, Mausam,
# Etzioni (EMNLP 2010, §3
# "Type Restrictions", read on 6 Oct): <George Washington, was born in,
# America> and <..., born in, Virginia> "both use the same sense of 'was born
# in' but refer to different semantic relations - one that takes a country in
# arg2, and the other that takes a state", and §1: "we need to consider typed
# relations where the relations analyzed are constrained to have specific
# argument types". The question shows the value on both sides ("as in: ...");
# the subject is shown on neither (a known relation keeps an example object,
# never an example subject), so for the subject the model judges the kind the
# phrase itself takes ("lives in": a person). Two examples, never a list: the
# model decides. It pushes towards null, the direction that loses nothing
# ("Prefer null when unsure" below).
ARGUMENT_TYPES = (
    "They are the same relation only when their subjects are of the same kind "
    "and so are their values: where a company is based and where a person "
    "lives are different relations, and so are a subscription to a service "
    "and an account's pricing plan."
)


def same_relation_question(surface: str, object_label: str, candidates: Sequence) -> str:
    """The question put to the model. Asked WITHOUT thinking: measured in
    korely-agent, thinking off is better on merging, unchanged on false merges
    (0/10) and fourteen times faster. `candidates` are canonical names or
    (canonical, example object) pairs. Since 3.2.0 it says that the kinds of
    the subject and of the value belong to the relation (ARGUMENT_TYPES)."""
    object_label = example_on_one_line(object_label)  # 5.7.0: one line, cut (contradiction)
    options = "\n".join(f"{i}. {_option(c)}" for i, c in enumerate(candidates, 1))
    # BUG-365 (2.8.0): "visited" was merged into "plans_to_visit" (42 facts on
    # a bench), "attended" into "plans_to_attend" (40): a done event
    # stored as an intention. TimeML (Pustejovsky et al. 2003, section 3.2 and
    # the EVENT classes) keeps them apart: an OCCURRENCE is asserted, while an
    # I_STATE or I_ACTION ("intend", "want", "try") introduces a possible world
    # in which the event is not asserted (a MODAL subordination); and a STATE
    # (a relation that lasts) is not an OCCURRENCE (a single event). The
    # sentence only pushes towards keeping relations apart, the direction that
    # loses nothing (as "Prefer null when unsure" below).
    return (
        "Two relation phrases name the SAME relation when one could replace the "
        "other in a sentence without changing what is asserted. "
        "A plan, a wish or a need to do something and doing it are different "
        "relations (\"plans to visit\" and \"visited\" are not the same), and so "
        "are a lasting relation and a single event (\"visits\" a place "
        "regularly, \"visited\" it once). "
        + ARGUMENT_TYPES + "\n\n"
        f'New phrase: "{surface.replace("_", " ")}"'
        + (f' (as in: ... {object_label})' if object_label else "")
        + "\n\nKnown relations:\n" + options + "\n\n"
        "Answer JSON only: {\"same_as\": <number>} to reuse one, or "
        "{\"same_as\": null} when the new phrase names a different relation.\n"
        # Measured: this line is why ten runs produced zero false merges. It may
        # also be why the median run merges little. See korely-agent predicates.py.
        "Prefer null when unsure: merging two different relations loses "
        "information permanently, keeping them apart only costs a duplicate."
    )


def read_same_relation(json_text: Optional[str], n_candidates: int, *, severe: bool = False) -> Optional[int]:
    """0-based index of the chosen candidate, or None for "a new relation".

    `not isinstance(n, bool)` is not defensive noise: in Python True is an int
    equal to 1, so a model that answers {"same_as": true} would index the first
    option and merge two relations that were never compared. Merging is the
    irreversible direction, and this would take it on a malformed answer.

    `severe`: only {"same_as": null} is "a new relation"; any other answer
    that does not name one of the options raises `UnreadableAnswer`.
    """
    items = _json(json_text)
    n = items.get("same_as", _MISSING) if isinstance(items, dict) else _MISSING
    if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= n_candidates:
        return n - 1
    if severe and n is not None:
        raise UnreadableAnswer(
            f"same-relation answer is neither null nor an option 1..{n_candidates}: {_excerpt(json_text, 'same_as')}"
        )
    return None


# ---------------------------------------------------------------------------
# 2. Does the relation hold one value at a time (owl:FunctionalProperty)?
# ---------------------------------------------------------------------------

def single_value_question(surface: str, object_label: str = "") -> str:
    """Asked WITH thinking, and only here. Measured on eighteen relations with
    known answers: 18/18 with it, 16/18 without, and the two it loses are
    `on_plan` and `signed_up_for`. A subscription plan classified multi-valued
    means the judge is never told a customer is on one plan at a time, and the
    store answers that one customer is on two plans at once.

    TYPED when the relation was seen with a value (2026-09-28, I1 from the
    blind installs of korely-agent 0.1.13: 4 fresh installs in 21 left two
    plans open, every time because this answer had come back "many"). Asked
    about the relation in general, "subscribes to ... Priority Support plan"
    came back "many" 3 times in 10 (a customer subscribes to many services);
    asked about values of the same kind as the one seen, 0 in 60, while
    "subscribes to ... weekly product newsletter" stayed "many" 20 in 20.
    Functionality is a property of
    the relation AND the kind of its value: "published in" holds one year but
    many languages (Lin, Mausam, Etzioni, EMNLP 2010 §3), the same rule the
    judge's [single-valued for values like: X] tag already applies. The
    example in the question is the paper's, not one of the measured cases.

    ASKED AS REPLACEMENT (2.5.0, 2026-10-01). "Can one subject hold more than
    one value at the same time?" has a true answer of yes for a job (a second
    job, a consultancy), so the model said "many" for employment: measured on
    30 Sep (10 runs, the questions with today's date and with a replacement,
    $0.56), works_at Globex 4 wrong in 10, employed_by, works_for and
    reports_to 10 in 10, 34 of 40 in all. The judge then never received the
    tag, and a change of job left both employers true. The tag is a hint for
    the judge, never a rule that closes on its own (contradiction, Ritter
    2008, Lin 2010), so the question is now asked the way the judge uses the
    answer: does a new value of this kind USUALLY replace the old one? 0 wrong
    in 40 on the four; plans, residence and spouse still right; the
    many-valued controls stay many, except "requires a responsible person",
    "one" 8 times in 10. The exception, the second job, the second
    requirement, is the judge's to see, and from 2.5.0 the judge reads the
    words the new fact was read from (contradiction rule 4). Without a value
    the question is unchanged.

    A FEELING OF ONE MOMENT (3.2.0, 2026-10-06). On LoCoMo conversation 26
    written by gpt-oss-120b (6 Oct) `feels` came back
    single-valued, decided on a mood as its example (a mood that the next
    mood replaces), and 10 of the 20 closures of the write were one feeling
    closed by the next one, weeks later. One of them took out of every block
    the memory that answered a question about the person's family, and the
    reader's answer turned wrong. In the write with the dates off the same
    relation was many-valued: the answer moves from one write to the next.
    (LoCoMo is CC BY-NC: its texts stay out of this repository.)
    TimeML (Pustejovsky et al. 2003; Annotation Guidelines 1.2.1, Sauri et
    al. 2006, §2.1.3 "Class", read on 6 Oct) keeps what happens, an
    OCCURRENCE ("something that happens or occurs in the world"), apart from
    a STATE ("circumstances in which something obtains or holds true"), and
    marks up a state only when the text anchors it to a time or changes it.
    A feeling or a reaction told in a conversation is bound to its moment:
    what happened then, or a state that held then, never a lasting state that
    the next feeling ends. So a feeling of another moment is another value,
    not a replacement: one example among the many-valued ones, in their style
    (the kind of value, Lin 2010 §3). A lasting affection is still many-valued
    ("love" is a STATE in the 2003 paper, and `likes` is many-valued above);
    a mood the words say has ended ("not sad anymore") is the judge's
    exception, read in the words (contradiction rule 4)."""
    object_label = example_on_one_line(object_label)  # 5.7.0: one line, cut (contradiction)
    rel = surface.replace("_", " ")
    # Relations named as examples of each answer: the same kind of measured
    # exception to "no word lists in prompts" as the extraction prompts'
    # (fact_extractor, above PROMPT_SINGLE). They illustrate the two kinds of
    # value (Lin, Mausam, Etzioni, EMNLP 2010 §3); the model answers for the
    # relation it is shown, and the answer is a vote of three (SINGLE_VALUE_VOTES).
    # Their removal has not been measured: a change starts with that measure.
    examples = (
        "many_values TRUE:  likes (a person likes many things), owns, knows,\n"
        "                   speaks, tagged_with, member_of, feels (a feeling or\n"
        "                   a reaction of one moment happened then, it is not a\n"
        "                   lasting state: a later one does not end it)\n"
        "many_values FALSE: subscription plan (one plan at a time), city of\n"
        "                   residence, current employer, date of birth, marital\n"
        "                   status, account tier, current status\n\n"
    )
    if object_label:
        return (
            f'Seen as: "... {rel} {object_label}"\n\n'
            f'When a subject gets a NEW value of the same kind as "{object_label}" '
            f'for the relation "{rel}", does the new value USUALLY REPLACE the old one '
            "(the old one stops being true), or do the values usually hold together?\n\n"
            "Judge the kind of value, not the relation in general: \"published in\" "
            "holds one YEAR (a new year replaces the old one) but many LANGUAGES (a new "
            "language adds to the others).\n"
            "Judge the usual case, not every possible exception: an exception is decided "
            "later by someone who reads both values.\n\n"
            + examples +
            'Answer JSON only: {"many_values": false} if the new value usually replaces '
            'the old one, {"many_values": true} if they usually hold together.'
        )
    return (
        "Can ONE subject hold MORE THAN ONE value for this relation AT THE SAME "
        f'TIME?\n\nRelation: "{rel}"\n\n'
        + examples +
        "Ask yourself: if the subject got a NEW value for this relation, would "
        "the old one still be true? If no, answer false.\n"
        'Answer JSON only: {"many_values": true} or {"many_values": false}.'
    )


def read_single_value(json_text: Optional[str], *, severe: bool = False) -> Optional[bool]:
    """is_functional: True = one value at a time. None = undecided, and
    undecided means NEVER supersede: two facts standing is recoverable, a true
    fact closed is not.

    `severe`: the question allows true or false only. Anything else raises
    `UnreadableAnswer`: "undecided" is stored on the relation and never
    asked again, so it must come from the model, not from an answer we could
    not read."""
    items = _json(json_text)
    many = items.get("many_values") if isinstance(items, dict) else None
    if isinstance(many, bool):
        return not many
    if severe:
        raise UnreadableAnswer(f"many_values answer is not true or false: {_excerpt(json_text, 'many_values')}")
    return None


# The answer is kept for the memory, for good: a wrong one is paid on every
# later write of the relation. So it is decided by majority of three answers
# (self-consistency, Wang et al., ICLR 2023: sample several answers, keep the
# most frequent), stopping at two when the first two agree, the common case.
SINGLE_VALUE_VOTES = 3


def decide_single_value(
    ask: Callable[[str, bool], Optional[str]],
    surface: str,
    object_label: str = "",
    *,
    severe: bool = False,
    votes: int = SINGLE_VALUE_VOTES,
) -> Optional[bool]:
    """is_functional by majority of `votes` answers (see SINGLE_VALUE_VOTES).
    None when no answer reaches the majority: undecided, never supersede.
    `ask` raising, or `UnreadableAnswer` with `severe`, propagates: a
    failed or unusable answer is not a vote (the caller does not cache). An
    unusable answer is asked again first (`ask_and_read`), so it takes three
    in a row to lose the vote, not one."""
    question = single_value_question(surface, object_label)
    majority = votes // 2 + 1
    count: Dict[Optional[bool], int] = {}
    for _ in range(votes):
        v = ask_and_read(ask, question, True, read_single_value, severe=severe)
        count[v] = count.get(v, 0) + 1
        if v is not None and count[v] >= majority:
            if len(count) > 1:
                # 3.0.0: the relation's name (the vocabulary's
                # word), never the object it was asked with (an end user's
                # value): its length.
                logger.info("[vocabulary] %r (object %s): answers split %s, kept %s",
                            surface, _log.length(object_label), count, v)
            return v
    logger.warning("[vocabulary] %r (object %s): no majority in %s, undecided",
                   surface, _log.length(object_label), count)
    return None


# ---------------------------------------------------------------------------
# 3. Which family does a relation the static map does not know belong to?
# ---------------------------------------------------------------------------

# `identity` is never offered: it is reached only through the predicates of
# the schema (families.PREDICATE_TO_FAMILY). So was `instructions` (2.8.0)
# until 5.0.0 removed it.
_CHOOSABLE_FAMILIES: List[str] = [f for f in FAMILY_ORDER if f != "identity"]

# Each family defined in words, never by relation names (3.3.0, the rule of
# 8 Oct: no word lists in the code). Until then
# the lines named relations ("knows, married_to, colleague_of, manages", "has,
# owns, uses, runs on ..."): a list the model could copy as a lookup. The
# model decides the family from what the relation means.
_FAMILIES_DESCRIPTION = (
    "preferences: how someone feels about things they could choose: tastes, interests, aversions\n"
    "people: how one person stands toward another: family, friendship, work ties, acquaintance\n"
    "places: where a person or a thing is, comes from, or goes\n"
    "work: a person's occupation: job, role, employer, studies, projects\n"
    "ownership: what a subject possesses or relies on: belongings, tools, services, dependencies\n"
    "health: the body and the mind: conditions, allergies, medication, fitness\n"
    "financial: money: prices, costs, revenue, salaries, plans and billing\n"
    "events: something tied to a date: deadlines, launches, meetings, milestones, a status over time\n"
    "other: none of the above fits"
)


def family_question(surface: str, object_label: str = "") -> str:
    """Asked once per NEW relation the static map does not know, without
    thinking (a classification into nine labels, like the resolution call)."""
    object_label = example_on_one_line(object_label)  # 5.7.0: one line, cut (contradiction)
    example = f' (as in: "... {surface.replace("_", " ")} {object_label}")' if object_label else ""
    return (
        "Classify a relation between two things into ONE family.\n\n"
        f'Relation: "{surface.replace("_", " ")}"{example}\n\n'
        f"Families:\n{_FAMILIES_DESCRIPTION}\n\n"
        'Answer JSON only: {"family": "<one family name from the list>"}.'
    )


def read_family(json_text: Optional[str], *, severe: bool = False) -> Optional[str]:
    """The family, or None when the answer is not one of the names offered.

    `severe`: a name that was not offered raises `UnreadableAnswer` instead
    of None (which the caller stores as "other", for good). "other" itself is
    one of the names offered, and a genuine answer."""
    items = _json(json_text)
    fam = items.get("family") if isinstance(items, dict) else None
    if isinstance(fam, str) and fam.strip().lower() in _CHOOSABLE_FAMILIES:
        return fam.strip().lower()
    if severe:
        raise UnreadableAnswer(f"family answer is not one of the names offered: {_excerpt(json_text, 'family')}")
    return None


# ---------------------------------------------------------------------------
# 4. Does the phrase carry the past inside it? Which is its present form?
# ---------------------------------------------------------------------------

def phrase_tense_question(surface: str, object_label: str = "") -> str:
    """The relation without its tense, so that "used" (past) and "uses"
    (today) are one relation and the past statement can close the present one.

    CESI (Vashishth 2018, §4.1) uses morphological normalization, tense
    removal included, as side information for canonicalizing relation
    phrases; here the model does it, so it works in every language and needs
    no dictionary of verb forms (the rule of 2026-09-27). Graphiti (Rasmussen
    2025, arXiv:2501.13956 §2.2.3) keeps the time on the fact, not in the
    phrase. Asked in two places, each time once per phrase: when an unseen
    phrase arrives with tense "past" (the extractor is asked to write the
    present and does not always), and when repairing facts written before the
    extractor emitted a tense. Without thinking: a reading of one phrase, like
    the family question."""
    object_label = example_on_one_line(object_label)  # 5.7.0: one line, cut (contradiction)
    example = f' (as in: "... {surface.replace("_", " ")} {object_label}")' if object_label else ""
    return (
        "A relation phrase from a fact.\n\n"
        f'Relation: "{surface.replace("_", " ")}"{example}\n\n'
        "Does the phrase ITSELF state that the relation held before and no longer "
        "holds? A phrase that describes a present state with a participle is not "
        "past. If it is past, write the same relation in the present tense, in "
        "the same language and style.\n"
        'Answer JSON only: {"past": false} or {"past": true, "present": "<phrase>"}.'
    )


def read_phrase_tense(json_text: Optional[str], *, severe: bool = False) -> Optional[str]:
    """The present-tense phrase when the answer says "past", else None. A
    malformed answer is None: the fact stays as it is (recoverable).

    `severe`: only {"past": false} is None; {"past": true} without a present
    form, or any other answer, raises `UnreadableAnswer`. Inside
    `store.resolve_predicate` "not past" is not recoverable: the phrase is
    then resolved and cached as it is."""
    items = _json(json_text)
    past = items.get("past") if isinstance(items, dict) else None
    if past is True:
        present = items.get("present")
        if isinstance(present, str) and present.strip():
            return present.strip()
    elif past is False:
        return None
    if severe:
        raise UnreadableAnswer(f"past-tense answer is not one the question allows: {_excerpt(json_text, 'past')}")
    return None


# ---------------------------------------------------------------------------

def _excerpt(text: Optional[str], key: Optional[str] = None) -> str:
    """An answer for the error, by its shape (3.0.0; `log_safe`):
    the error goes to the logs, and the answer may repeat the example value
    it was asked with, an end user's. Until 3.0.0 its first 80 characters."""
    if not text or not str(text).strip():
        return "(empty)"
    return _log.answer(text, read_object(str(text)), key)


def _json(text: Optional[str]):
    """The JSON object in a model answer, as every reader of this folder reads
    one (`model_json.read_object`): the whole text, a ```json fence
    removed, or the one object written in it, once or repeated identically
    (2026-09-29, LongMemEval on dev: 'Formatting:** * `{"many_values": false}`
    {"many_values": false}' was read as no answer and the memory lost its
    facts). Two different objects are no answer. {} when there is none.

    5.2.2: until then a chain of its own `json.loads` (whole text, each ```
    block, first "{" to last "}"), which on the same text could answer
    differently from the extractor's and the judge's reader: an object in a
    ``` block and a different one outside it was the block's here, no answer
    there (a code review; CONTRIBUTING.md: never a bare json.loads on a
    model's answer)."""
    value = read_object(text)
    return value if isinstance(value, dict) else {}
