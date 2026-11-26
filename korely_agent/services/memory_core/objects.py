"""One thing named in two languages or forms is one object (memory_core 6.1.0, off).

The extraction keeps the memory's own words (the GROUNDING rule of
`extraction`, against invented values), so a note in Italian writes
"lives in Italia" and one in English "lives in Italy": two objects, two facts
that never meet, a search for one that misses the other. Until 6.1.0 only an
explicit identity statement (`identity.alias_rule_from_identity`, "Acme is
now called Globex") or an alias declared by hand united them.

This is entity resolution in an agent memory, as Graphiti does it for its
nodes (Rasmussen et al., "Zep: A Temporal Knowledge Graph Architecture for
Agent Memory", arXiv:2501.13956, 2025, section 2.2.1): "the system embeds
each entity name ... This embedding enables the retrieval of similar nodes
through cosine similarity search across existing graph entity nodes ...
These candidate nodes, together with the episode context, are then processed
through an LLM using our entity resolution prompt"; and its prompt (appendix
6.1.2) says "duplicate nodes may have different names".

THE UNION IS A LINK, NEVER AN ALIAS (the decision of 30 Oct 2026:
the writer decides, automatically, and every union is reversible without
loss). An alias (`store.add_alias`) rewrites the canonical names of the
memory's facts, and at the write the same canonical triple is reconfirmed
(`writing.write_fact`, step 2): no row is written, and the new fact's label,
start, tense and evidence are gone; `store.recanonicalize` merges two open
duplicates and deletes the younger row (`store.merge_into`). Nothing of that
can be undone (an independent review of 6.1.0 as first written, P1.2: a
wrong union of Munich with the Principality of Monaco swallowed a move). A
link ("the same as", `Tables.Link`) writes NO fact and changes no decision
of a write: the facts are stored exactly as with the switch off, each with
its own words, and the reads resolve the link (the product's
`services/facts/links.py`): the two objects read as one, under the name the
memory held first, each fact keeping its own label, and only in the facts of
the relation family the link was decided in (`add_link(family=)`): "Florence"
linked to "Firenze" among the places is not an aunt called Florence (the
review of the rebuild, P2.1). `remove_link` deletes
the row, and the memory is exactly as it was before the union.

The steps:

  1. A NEW object only: one no fact of the memory names yet (as object or
     subject, compared lowercased by the database on both sides), no alias
     and no link of the memory names, in a relation family that is neither
     "identity" (a rename's two names are a statement about names) nor
     "people": two people who share a first name are not one person, and a
     nickname is not a translation (`identity.TYPES_THAT_RENAME`: people do
     not get renamed by the engine; the review's P2.3). An object the memory
     already holds was decided when it first came, as a relation phrase is
     decided once (`store.resolve_predicate`).
  2. A shortlist by vectors among the OPEN facts of the same memory in the
     SAME relation family (the "blocking" step of entity resolution:
     Papadakis, Skoutas, Thanos, Palpanas, "Blocking and Filtering
     Techniques for Entity Resolution: A Survey", ACM Computing Surveys
     53(2), 2020, section 2: "Blocking trades slightly lower effectiveness
     for significantly higher efficiency. Its goal is to reduce the number
     of performed comparisons, while missing as few matches as possible"),
     on the HNSW index with its depth set (`vector_search.nearest`,
     BUG-320: the memory's filters apply after the index is scanned). The
     vector is the mention IN ITS CONTEXT, the text of the fact the engine
     stores it in (Botha, Shan, Gillick, "Entity Linking in 100 Languages",
     EMNLP 2020, figure 1: the mention encoder reads "context immediately
     left of the mention ..., the mention span ..., and context immediately
     right"), one multilingual model for every language (their "single
     entity retrieval model that covers 100+ languages"; here
     EmbeddingGemma, local). A candidate whose memory was forgotten has no
     words left to read it by, and is not offered. Objects already linked
     are one candidate, under the name the memory held first.
  3. DESCRIBE, then compare (`describe_question`). The writer first writes,
     for every mention of the question (the new ones and their candidates),
     what the name refers to there: what kind of thing, what it belongs to
     or where it is. Logeswaran et al., "Zero-Shot Entity Linking by Reading
     Entity Descriptions", ACL 2019, link a mention by reading it with the
     entity's DESCRIPTION, section 4.2: "By jointly encoding the entity
     description and the mention in context with a Transformer, they can
     attend to each other at every layer", and section 6.1: "The
     significant gap between Full-Transformer and the other variants shows
     the importance of allowing fine-grained comparisons between the two
     inputs". A memory has no descriptions of its objects; the first call
     writes them from each fact and the memory's words, and the comparison
     reads them beside the contexts. The new mentions and the objects the
     memory holds are described in two questions, never side by side
     (`_described`). They are kept as the union's reason.
  4. Two readings with different framings, a union only when both say
     "same". The first SELECTS among the candidates of each new mention
     (Wang et al., "Match, Compare, or Select? An Investigation of Large
     Language Models for Entity Matching", COLING 2025, arXiv:2405.16884,
     section 1: "an average 16.02% improvement in F1 over the current
     matching strategy"; section 4.3, finding 3: "the selecting strategy
     costs less than half of the matching strategy"), nearest first,
     because selection suffers "position bias, with the selection accuracy
     varying significantly as the position of the matching record increases
     in the candidate list" (section 3.4): `SHORTLIST` candidates. The
     second MATCHES the chosen pair alone, framed the other way round: the
     object the memory holds first, then the new one, and the question is
     whether they are two different things (`pair_question`). Their ComEM
     combines two strategies in two stages (section 1); Peeters, Steiner,
     Bizer, "Entity Matching using Large Language Models", EDBT 2025,
     arXiv:2310.11244, section 3: "there is no prompt design that performs
     best for most models", so the two readings do not share a prompt and a
     bias of one framing is not read twice. All the new objects of a write
     go in one question per step (Fan et al., "Cost-Effective In-Context
     Learning for Entity Resolution: A Design Space Exploration", ICDE 2024,
     arXiv:2312.03987, section VI-B: "Batch prompting can not only bring
     4x-7x cost saving, but also achieve higher and more stable matching
     accuracy than standard prompting").
  5. The answers are categorical: "same", "different" or "unsure" (never a
     probability: CONTRIBUTING.md, Xiong et al., ICLR 2024). "different" and
     "unsure" leave two objects. A failed call, an answer that cannot be
     read, a vector that could not be computed are never "different": the
     write rolls back and is tried again (`store.ResolutionUnavailable`,
     `store.ResolutionUnusable`), because an object left apart once is
     never asked again.

The prompts are the same for both regions' writers; whether they fit each
was measured on both (scripts/measure_same_object.py, Peeters et al. above).

What the engine passes: the memory, the mentions of a write with their
words, `ask(prompt, think)`, `embed(mention)` (the vector of the fact text the
mention would be stored in), `words_of(fact ids)` (the text of the memories
that stated the candidates' facts, never a forgotten one's), `is_open` (what
"true now" means), `lock()` (serializes the check and the link per memory,
against the parallel drain) and the id of the memory being written, which
the link keeps so that erasing that memory takes the link back.

The link table (`Tables.Link`) has the columns `name_label`,
`canonical_label`, `family` (the relation family it was decided in, the only
one the reads apply it to), `decided_by`, `source_memory_id`, `source_fact_id`,
`canonical_memory_id`, `reason`, `created_at`, and the memory's columns
(`Memory.new_alias`); `Memory.links` selects one memory's rows.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from sqlalchemy import exists, func, or_
from sqlalchemy.orm import defer
from sqlalchemy.orm.exc import StaleDataError

from . import identity as _id
from . import log_safe as _log
from . import store as _store
from . import vocabulary as _vocabulary
from .model_json import read_object

logger = logging.getLogger(__name__)

# How many candidates one new object is offered, nearest first: a short ranked
# list (Wang et al. 2025, section 3.4: selection loses accuracy as the right
# candidate sits lower in the list; their ComEM keeps the top 4, section 4.1).
SHORTLIST = 4

# How many of the memory's nearest facts the shortlist is read from: several
# facts can name one object, and the shortlist keeps distinct objects. A
# bounded read (LIMIT), on the vector index with its depth (`vector_search`).
NEAREST_FACTS = 40

# The memory's words shown for a mention, at most: a window around the name.
WORDS_MAX = 240

# The families never asked (step 1 of the module's docstring).
NEVER_ASKED = frozenset({"identity", "people"})

# The verdicts: tokens of the JSON the model writes, never words read in a
# sentence (CONTRIBUTING.md: a refusal is a token or a field).
SAME = "same"
DIFFERENT = "different"
UNSURE = "unsure"

# Who decided a link (`Tables.Link.decided_by`).
BY_THE_WRITER = "writer"
BY_A_REVIEW = "review"

Answer = Union[int, str]   # 0-based candidate index ("same"), DIFFERENT or UNSURE


@dataclass(frozen=True)
class Mention:
    """A thing named in a fact, with the context it was named in."""
    name: str
    subject: str
    relation: str
    family: Optional[str] = None
    words: Optional[str] = None     # the memory's words that stated the fact
    description: Optional[str] = None   # the writer's, step 3


@dataclass(frozen=True)
class Candidate:
    """An object the memory already holds, in one fact it is named in."""
    name: str                       # the name the memory holds it under (a link's canonical)
    mention: Mention                # the fact it was found in, with its words
    fact_id: Any = None
    source_id: Any = None           # the memory that stated that fact
    distance: Optional[float] = None


@dataclass(frozen=True)
class Decision:
    """The answer for one new mention, with what the writer said: the reason
    a link keeps (`Tables.Link.reason`)."""
    answer: Answer
    readings: Tuple[dict, ...] = ()
    description: Optional[str] = None       # of the new mention
    chosen: Optional[str] = None            # of the candidate chosen, when one was


# ---------------------------------------------------------------------------
# the text of the questions
# ---------------------------------------------------------------------------

def one_line(text: Optional[str], limit: int = WORDS_MAX) -> str:
    """A text on one line, at most `limit` characters (the spotlighting of
    `contradiction.example_on_one_line`: untrusted text kept apart from the
    instructions, Hines et al., arXiv:2403.14720, 2024, section III)."""
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


def words_around(text: Optional[str], name: Optional[str], limit: int = WORDS_MAX) -> Optional[str]:
    """The memory's words around the first place it says `name` (case
    folded, any script), at most `limit` characters; its first words when the
    name is not written in it as such; None for no text. A window on the
    fact's own name, never a search for words of a list."""
    t = " ".join((text or "").split())
    if not t:
        return None
    if len(t) <= limit:
        return t
    at = t.casefold().find((name or "").strip().casefold()) if (name or "").strip() else -1
    if at < 0:
        return t[:limit].rstrip() + "…"
    start = max(0, at - limit // 2)
    end = min(len(t), start + limit)
    start = max(0, end - limit)
    return ("…" if start > 0 else "") + t[start:end].strip() + ("…" if end < len(t) else "")


def _fact_line(m: Mention) -> str:
    rel = (m.relation or "").replace("_", " ").strip()
    family = f" (family: {m.family})" if m.family and m.family != "other" else ""
    return f"{one_line(m.subject, 200)} | {one_line(rel, 200)} | {one_line(m.name, 200)}{family}"


def _context(m: Mention, indent: str, *, description: bool = False) -> str:
    lines = [f"{indent}fact: {_fact_line(m)}"]
    if m.words:
        lines.append(f'{indent}memory\'s words: "{one_line(m.words)}"')
    if description and m.description:
        lines.append(f'{indent}description: "{one_line(m.description, 300)}"')
    return "\n".join(lines)


_ABOUT = ("A memory about one person or agent is being written; its facts keep the "
          "memory's own words, in any language.")

# Wikidata keeps "part of" (P361), "located in the administrative territorial
# entity" (P131) and "parent organization" (P749) as relations between two
# items, never as two names of one item (the reading of identity.py for
# "replaces", P1365); "subsidiary" (P355) and "manufacturer" (P176) likewise.
# The bench of 29 Oct 2026, one reading: a place inside a country united with
# it, 1 time on Gemini and 2 on gpt-oss without this sentence, 1 and 1 with
# it; on 30 Oct a division of a company (AWS) united with it on Gemini
# before "a division, brand or product of" was said.
_PARTS = ("A thing is not the same as what it is part of, located in, owned by, a "
          "division, brand or product of, or named after: those are two things.")

# The sentences the two readings share with the review of subjects (6.20.0,
# `decide_by_choice`), written once; the questions of 6.1.0 are the same
# text, byte for byte.
_FORMS = ("The same thing can be named in another language or script, in a shorter or "
          "longer form, or by an abbreviation. Names that look alike, or translate one "
          "another, are still different things when their contexts point to different "
          "things: the name alone never decides. ")
_TWO_THINGS = ("Two places, organizations or things that share a name or translate each "
               "other's name are two things when their contexts locate or define them "
               "differently. ")
_ONE_THING = (" They are one thing when both contexts point to the same real-world thing, "
              "named in two languages, scripts or forms.")
# The direction that loses nothing, as the vocabulary's "Prefer null when
# unsure" (store.resolve_predicate).
_UNSURE = 'Answer "unsure" when the contexts do not settle it.'


def describe_question(mentions: Sequence[Mention]) -> str:
    """Step 3: what each name refers to, in its context (Logeswaran et al.
    2019: the comparison reads an entity's description). One question for
    every mention of a write (Fan et al. 2024: batched)."""
    blocks = "\n\n".join(
        f'Mention {i}: "{one_line(m.name, 200)}"\n{_context(m, "  ")}'
        for i, m in enumerate(mentions, 1))
    return (
        f"{_ABOUT} Below are names it uses, each in the fact it is in and the memory's "
        "words that stated it.\n\n"
        "For each mention, identify the very thing the name refers to there, as an "
        "encyclopedia entry would: what kind of thing it is and, when it is part of, "
        "located in, a division, brand or product of, or owned by a larger thing, which "
        "one. Use the fact, the memory's words and what the name means in the language "
        "they are written in. Identify the thing itself, not what the person did with it "
        "or when. When the context does not establish which thing it is, say so.\n\n"
        + blocks + "\n\n"
        'Answer JSON only: {"descriptions": [{"mention": <number>, "description": '
        '"<one sentence>"}]}, one entry for each mention.'
    )


def select_question(items: Sequence[Tuple[Mention, Sequence[Candidate]]]) -> str:
    """Step 4, the first reading: for each new mention, which of its
    candidates (nearest first, Wang et al. 2025: select) is the same thing,
    if any; every mention with its context and its description."""
    blocks = []
    for i, (m, cands) in enumerate(items, 1):
        options = "\n".join(
            f'    {k}. "{one_line(c.name, 200)}"\n{_context(c.mention, "       ", description=True)}'
            for k, c in enumerate(cands, 1)
        )
        blocks.append(
            f'New mention {i}: "{one_line(m.name, 200)}"\n{_context(m, "  ", description=True)}\n'
            f"  Already in this memory:\n{options}"
        )
    return (
        f"{_ABOUT} Each new mention below names something the memory has not named "
        "before; under it are things the same memory already names, found because their "
        "facts read alike. Each comes with its fact, the memory's words and a description "
        "of what it refers to there.\n\n"
        "For each new mention, decide whether it refers to the very same real-world thing "
        "as one of the things listed under it. Decide from the contexts and the "
        "descriptions, as a person reading both would. " + _FORMS
        + _PARTS + "\n\n"
        + "\n\n".join(blocks) + "\n\n"
        'Answer JSON only: {"answers": [{"mention": <number>, "verdict": "same" or '
        '"different" or "unsure", "same_as": <the number of the listed thing when '
        'the verdict is "same", else null>}]}, one entry for each new mention.\n'
        + _UNSURE
    )


def pair_question(pairs: Sequence[Tuple[Candidate, Mention]]) -> str:
    """Step 4, the second reading: each pair alone, the object the memory
    holds first, framed as "are these two different things?" (Wang et al.
    2025: match; Peeters et al. 2025: no prompt design is best for every
    model, so this one is not the first reading's)."""
    blocks = "\n\n".join(
        f'Pair {i}:\n  A: "{one_line(c.name, 200)}"\n{_context(c.mention, "     ", description=True)}\n'
        f'  B: "{one_line(m.name, 200)}"\n{_context(m, "     ", description=True)}'
        for i, (c, m) in enumerate(pairs, 1))
    return (
        f"{_ABOUT} In each pair below, A is a thing the memory already names and B a name "
        "it has just used, each with its fact, the memory's words and a description of "
        "what it refers to there.\n\n"
        "For each pair, are A and B two different things? " + _TWO_THINGS
        + _PARTS + _ONE_THING + "\n\n"
        + blocks + "\n\n"
        'Answer JSON only: {"pairs": [{"pair": <number>, "verdict": "different" or "same" '
        'or "unsure"}]}, one entry for each pair. Answer "unsure" when the contexts do not '
        "settle it."
    )


# ---------------------------------------------------------------------------
# reading the answers
# ---------------------------------------------------------------------------

def _number(v: Any) -> Optional[int]:
    """An int, or a string of ASCII digits; never a boolean."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isascii() and v.strip().isdigit():
        return int(v.strip())
    return None


_MISSING = object()
_CONTRADICTORY = object()


def _entries(json_text: Optional[str], key: str, number: str, fields: Sequence[str]) -> Dict[int, Any]:
    """{entry number: (its fields)}; an entry given twice differently is
    `_CONTRADICTORY`."""
    items = read_object(json_text)
    entries = items.get(key) if isinstance(items, dict) else None
    out: Dict[int, Any] = {}
    if isinstance(entries, list):
        for e in entries:
            if not isinstance(e, dict):
                continue
            k = _number(e.get(number))
            if k is None:
                continue
            got = tuple(e.get(f) for f in fields)
            out[k] = got if out.get(k, got) == got else _CONTRADICTORY
    return out


def _verdict(v: Any) -> Optional[str]:
    return v.strip().lower() if isinstance(v, str) else None


def read_descriptions(json_text: Optional[str], n: int, *, severe: bool = False) -> List[Optional[str]]:
    """One description per mention, in order. A mention left out, or
    described twice differently, or not as a text, is no answer: with
    `severe` `vocabulary.UnreadableAnswer`, otherwise None (and a mention
    with no description is compared "unsure", which unites nothing)."""
    got = _entries(json_text, "descriptions", "mention", ("description",))
    out: List[Optional[str]] = []
    for i in range(1, n + 1):
        e = got.get(i, _MISSING)
        d = e[0] if isinstance(e, tuple) else None
        if isinstance(d, str) and d.strip():
            out.append(d.strip())
        elif severe:
            raise _vocabulary.UnreadableAnswer(
                f"same-object description {i} of {n} missing: {_vocabulary._excerpt(json_text, 'descriptions')}")
        else:
            out.append(None)
    return out


def read_same_object(json_text: Optional[str], sizes: Sequence[int], *,
                     severe: bool = False) -> List[Answer]:
    """The first reading: one answer per new mention, in order: the 0-based
    index of its candidate (verdict "same"), DIFFERENT or UNSURE. `sizes[i]`
    is how many candidates mention i+1 was offered.

    The verdict is the categorical field that decides (CONTRIBUTING.md);
    `same_as` only says which candidate, and a "same" without a candidate in
    range is no answer. A boolean is not a number (`vocabulary.
    read_same_relation`: True == 1 in Python, and uniting is the direction
    that cannot be taken on a malformed answer); a number written as a
    string of digits is that number (a JSON shape, not a word).

    `severe`: an answer that is none of those the question allows (no
    object, no entry for a mention, another verdict, a "same" with no
    candidate in range, a mention answered twice differently) raises
    `vocabulary.UnreadableAnswer`; otherwise it is "unsure", which unites
    nothing."""
    got = _entries(json_text, "answers", "mention", ("verdict", "same_as"))
    out: List[Answer] = []
    for i, n in enumerate(sizes, 1):
        e = got.get(i, _MISSING)
        verdict, which = e if isinstance(e, tuple) else (None, None)
        verdict = _verdict(verdict)
        k = _number(which)
        if verdict == SAME and k is not None and 1 <= k <= n:
            out.append(k - 1)
        elif verdict == DIFFERENT:
            out.append(DIFFERENT)
        elif verdict == UNSURE:
            out.append(UNSURE)
        elif severe:
            raise _vocabulary.UnreadableAnswer(
                f"same-object answer for mention {i} is not same 1..{n}, different or unsure: "
                f"{_vocabulary._excerpt(json_text, 'answers')}")
        else:
            out.append(UNSURE)
    return out


def read_pairs(json_text: Optional[str], n: int, *, severe: bool = False) -> List[str]:
    """The second reading: SAME, DIFFERENT or UNSURE for each pair, in order;
    anything else is no answer (`severe`) or UNSURE."""
    got = _entries(json_text, "pairs", "pair", ("verdict",))
    out: List[str] = []
    for i in range(1, n + 1):
        e = got.get(i, _MISSING)
        v = _verdict(e[0]) if isinstance(e, tuple) else None
        if v in (SAME, DIFFERENT, UNSURE):
            out.append(v)
        elif severe:
            raise _vocabulary.UnreadableAnswer(
                f"same-object pair {i} of {n} is not same, different or unsure: "
                f"{_vocabulary._excerpt(json_text, 'pairs')}")
        else:
            out.append(UNSURE)
    return out


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

def _described(ask, items: Sequence[Tuple[Mention, Sequence[Candidate]]], think: bool,
               severe: bool) -> List[Tuple[Mention, List[Candidate]]]:
    """The items with every mention described (step 3), each distinct
    context once: the new mentions in one question, the objects the memory
    holds in another. A description is never written beside the name it will
    be compared with: asked together, an Italian memory's "Monaco"
    (Munich, "Erasmus at the Ludwig-Maximilians-Universität") was described
    as the principality next to "Principauté de Monaco", and the two readings
    then united them (gpt-oss-120b, the bench of 30 Oct 2026; Logeswaran et
    al. 2019 read an entity's description written without the mention)."""
    def describe(mentions: List[Mention]) -> Dict[Any, str]:
        order: List[Mention] = []
        index: Dict[Any, int] = {}
        for m in mentions:
            k = (m.name, m.subject, m.relation, m.family, m.words)
            if k not in index:
                index[k] = len(order)
                order.append(m)
        texts = _vocabulary.ask_and_read(
            ask, describe_question(order), think,
            lambda text, severe: read_descriptions(text, len(order), severe=severe), severe=severe)
        return {k: texts[i] for k, i in index.items()}

    def key(m: Mention):
        return (m.name, m.subject, m.relation, m.family, m.words)

    new = describe([m for m, _ in items])
    held = describe([c.mention for _, cs in items for c in cs])
    return [(replace(m, description=new[key(m)]),
             [replace(c, mention=replace(c.mention, description=held[key(c.mention)])) for c in cs])
            for m, cs in items]


def decide(
    ask: Callable[[str, bool], Optional[str]],
    items: Sequence[Tuple[Mention, Sequence[Candidate]]],
    *,
    think: bool = False,
    severe: bool = False,
) -> List[Decision]:
    """The decision for each item (the module's steps 3 and 4): the
    descriptions, the selection among the candidates, and the chosen pairs
    alone, framed the other way round; a union (an int answer) only when
    both readings say "same", else UNSURE when they disagree. An unusable
    answer is asked again first (`vocabulary.ask_and_read`); `ask` raising,
    or `UnreadableAnswer` with `severe`, propagates: nothing was decided."""
    items = list(items)
    if not items:
        return []
    items = _described(ask, items, think, severe)
    sizes = [len(c) for _, c in items]
    first = _vocabulary.ask_and_read(
        ask, select_question(items), think,
        lambda text, severe: read_same_object(text, sizes, severe=severe), severe=severe)
    out = [Decision(answer=a, readings=({"framing": "select", "verdict": a if isinstance(a, str) else SAME,
                                         "of": len(cs)},),
                    description=m.description,
                    chosen=cs[a].mention.description if isinstance(a, int) else None)
           for (m, cs), a in zip(items, first)]
    chosen = [i for i, a in enumerate(first) if isinstance(a, int)]
    if not chosen:
        return out
    pairs = [(items[i][1][first[i]], items[i][0]) for i in chosen]
    second = _vocabulary.ask_and_read(
        ask, pair_question(pairs), think,
        lambda text, severe: read_pairs(text, len(pairs), severe=severe), severe=severe)
    for i, b in zip(chosen, second):
        d = out[i]
        readings = d.readings + ({"framing": "pair", "verdict": b},)
        out[i] = replace(d, readings=readings, answer=d.answer if b == SAME else UNSURE)
    return out


def reason_of(d: Decision, *, model: Optional[str] = None) -> dict:
    """What a link keeps of the decision: the two descriptions and the two
    readings (the review's P3.8). Words derived from the memories: erased
    with the link."""
    return {"model": model, "descriptions": {"name": d.description, "canonical": d.chosen},
            "readings": [dict(r) for r in d.readings]}


# ---------------------------------------------------------------------------
# the links
# ---------------------------------------------------------------------------

def key_of(name: Optional[str]) -> str:
    """How a name is compared in Python, on both sides of every comparison
    made here (the database compares with its own lower()): since 6.21.0
    `identity.name_key`, NFKC and case folded, the key of the aliases and of
    the judge's names, so a name written in two Unicode forms is one key."""
    return _id.name_key(name)


def family_of(family: Optional[str]) -> str:
    """A family as a link stores it and the reads look it up: the
    vocabulary's name, lowercased, "other" for none."""
    return (family or "other").strip().lower() or "other"


def link_rows(db, tables: _store.Tables, mem: _store.Memory, *, known_as_of: Optional[datetime] = None,
              family: Optional[str] = None) -> list:
    """The memory's links, as the store knew them at `known_as_of` (created
    by then; a link removed leaves no row, as an alias), of one relation
    `family` when given. Bounded by the memory: at most one row per object
    it names."""
    L = tables.Link
    if L is None or mem.links is None:
        return []
    q = db.query(L).filter(mem.links)
    if known_as_of is not None:
        q = q.filter(L.created_at <= known_as_of)
    if family is not None:
        q = q.filter(L.family == family_of(family))
    return q.all()


def link_map(rows: Sequence[Any]) -> Dict[str, str]:
    """name key -> the name it is the same as, the shape of an alias map, so
    that a chain is followed by the walk an alias is (`identity.canonical_of`)."""
    return {key_of(r.name_label): r.canonical_label for r in rows}


def root_of(name: Optional[str], links: Dict[str, str]) -> str:
    """The name the reads show for `name`: the end of its links' chain, at
    most `identity.MAX_HOPS` hops (a review can link an object that others
    are linked to)."""
    return _id.canonical_of(name, links) if name else (name or "")


def same_names(name: Optional[str], links: Dict[str, str]) -> set:
    """The keys of every name of the memory read as `name` (its root's
    whole group), `name`'s own included."""
    root = key_of(root_of(name, links))
    names = {key_of(name), root}
    for k in list(links):
        if key_of(root_of(k, links)) == root:
            names.add(k)
    return names


def link_outcome(db, tables: _store.Tables, mem: _store.Memory, name: str, canonical: str, *,
                 family: Optional[str]) -> str:
    """What writing "`name` is `canonical`" in `family` would be, before
    anything is written: "created" (it may be); "same" (already read as one
    there); "taken" (`name` is already linked in the memory, in any family,
    or decided by an alias: never rewritten); "invalid" (not two names an
    alias could hold, `identity.valid_name`); "conflict" (`canonical` is
    read as `name` in that family: a cycle). The rule of `add_link` and of a
    project's links moving into another (the product's `projects`)."""
    if tables.Link is None or mem.links is None:
        return "invalid"
    if not _id.valid_name(name) or not _id.valid_name(canonical):
        return "invalid"
    if key_of(name) in _store.memory_alias_map(db, tables, mem):
        return "taken"
    links = link_map(link_rows(db, tables, mem, family=family))
    root = root_of(canonical, links)
    if key_of(root) == key_of(name):
        return "conflict" if key_of(canonical) != key_of(name) else "invalid"
    if key_of(name) in links:
        return "same" if key_of(root_of(name, links)) == key_of(root) else "taken"
    if key_of(name) in link_map(link_rows(db, tables, mem)):
        return "taken"
    return "created"


def add_link(db, tables: _store.Tables, mem: _store.Memory, name: str, canonical: str, *,
             decided_by: str, family: Optional[str], source_memory_id: Any = None,
             source_fact_id: Any = None, canonical_memory_id: Any = None,
             reason: Optional[dict] = None) -> Tuple[Any, str]:
    """"`name` is the same thing as `canonical`" in the memory, for the facts
    of one relation `family`: the one the writer decided it in, the only one
    the reads apply it to ("Florence" the city is not an aunt called
    Florence, whose facts are in "people"; the review of the rebuild, P2.1).
    Writes the link row only: no fact is read or changed. Returns (row,
    outcome), outcome as `link_outcome`."""
    outcome = link_outcome(db, tables, mem, name, canonical, family=family)
    if outcome != "created":
        return None, outcome
    row = tables.Link(name_label=name.strip()[:200], canonical_label=canonical.strip()[:200],
                      family=family_of(family), decided_by=decided_by, source_memory_id=source_memory_id,
                      source_fact_id=source_fact_id, canonical_memory_id=canonical_memory_id,
                      reason=reason, **{**mem.new_alias, **mem.new_link})
    db.add(row)
    db.flush()
    # The row and the sources, never the names (CONTRIBUTING.md).
    logger.info("[memory] object link created: row %s, fact %s, memory %s (%s)",
                row.id, source_fact_id, source_memory_id, decided_by)
    return row, "created"


class LinkHoldsClosures(RuntimeError):
    """A link the judge closed facts through (6.20.0, `Tables.ClosureLink`)
    asked to go with nobody to give them back: never removed so."""


def remove_link(db, tables: _store.Tables, row, *,
                give_back: Optional[Callable[[List[Any]], int]] = None) -> int:
    """The undo, the one door a link leaves by: the row goes, and the memory
    is as it was before the link.

    An object link (6.1.0) never touched a fact, so nothing else changes.
    A link between two subjects (6.20.0, `review_subjects`) let the judge
    weigh the facts of one name against the other's, and the closings it
    made only so are recorded on it (`Tables.ClosureLink`, `record_closures`):
    `give_back(closed fact ids)` gives them back first (the engine's rule for
    a fact whose closer no longer stands; korely-agent: facts/given_back.py,
    a new open row from where the fact was closed, the closed row kept as
    the store knew it, Graphiti's two clocks, Rasmussen et al. 2025, section
    2.2.3), then their records go, then the row. A link that holds closings
    and no `give_back` raises `LinkHoldsClosures`, and nothing is removed;
    the database refuses the same shortcut to any statement that deletes the
    row without the records (korely-agent: the foreign key of
    `agent_fact_closure_links`, ON DELETE NO ACTION). Returns how many facts
    were given back."""
    given = 0
    C = tables.ClosureLink
    if C is not None:
        closed = [i for (i,) in db.query(C.fact_id).filter(C.link_id == row.id).all()]
        if closed:
            if give_back is None:
                raise LinkHoldsClosures(f"link {row.id} holds {len(closed)} closings to give back")
            given = int(give_back(closed) or 0)
            db.query(C).filter(C.link_id == row.id).delete(synchronize_session=False)
    db.delete(row)
    db.flush()
    logger.info("[memory] object link removed: row %s, %d closings given back", row.id, given)
    return given


def record_closures(db, tables: _store.Tables, closed: Dict[Any, Sequence[Any]],
                    columns: Optional[Dict[str, Any]] = None) -> int:
    """{closed fact id: the links it was closed through}: one record each
    (`Tables.ClosureLink`), with the engine's `columns` (korely-agent: the
    account), what `remove_link` gives back. Written in the caller's
    transaction, with the closing. Returns how many."""
    C = tables.ClosureLink
    if C is None:
        return 0
    n = 0
    for fact_id, link_ids in closed.items():
        for link_id in dict.fromkeys(link_ids):
            db.add(C(fact_id=fact_id, link_id=link_id, **(columns or {})))
            n += 1
    if n:
        db.flush()
    return n


# ---------------------------------------------------------------------------
# the database steps of a write
# ---------------------------------------------------------------------------

def _named(db, tables: _store.Tables, mem: _store.Memory, name: str) -> bool:
    """Whether a fact of the memory names `name`, as object or subject
    canonical, the database's lower() on both sides (the review's P3.9:
    Python's lower() is another function, "İstanbul")."""
    F = tables.Fact
    key = func.lower(func.trim(name or ""))
    return bool(db.query(exists().where(
        mem.facts, or_(func.lower(F.object_canonical) == key,
                       func.lower(F.subject_canonical) == key))).scalar())


def new_mentions(db, tables: _store.Tables, mem: _store.Memory,
                 mentions: Sequence[Mention]) -> List[Mention]:
    """The mentions of a write that name something new to the memory (step 1
    of the module's docstring), once each, in the order of the write."""
    alias_map = _store.memory_alias_map(db, tables, mem)
    links = link_map(link_rows(db, tables, mem))
    linked = set(links) | {key_of(v) for v in links.values()}
    out: List[Mention] = []
    seen = set()
    for m in mentions:
        key = key_of(m.name)
        if not key or key in seen or (m.family or "other") in NEVER_ASKED or not _id.valid_name(m.name):
            continue
        seen.add(key)
        if key in alias_map or key in linked or _named(db, tables, mem, m.name):
            continue
        out.append(m)
    return out


def nearest_objects(db, tables: _store.Tables, mem: _store.Memory, vec: Any, mention: Mention, *,
                    is_open: Any, before: Any = None) -> List[Candidate]:
    """The memory's objects nearest to a mention's vector, at most
    `SHORTLIST`, nearest first, one fact each (the nearest that names it):
    read from the `NEAREST_FACTS` nearest OPEN facts with a vector in the
    mention's relation family (step 2), on the HNSW index with its depth set
    (`vector_search.nearest`, BUG-320), never the mention's own name, a
    literal value or a fact of a family never asked. Objects already linked
    are one candidate, under the name the memory read them as.
    `before`: only facts written before that instant (`review_objects`: an
    object is offered the ones the memory held before it). Postgres with
    pgvector only; elsewhere there is no shortlist."""
    from .vector_search import nearest

    F = tables.Fact
    col = getattr(F, tables.vector) if tables.vector else None
    if col is None or vec is None or not _store._pg(db):
        return []
    q = (db.query(F).options(defer(col))
         .filter(mem.facts, is_open, col.isnot(None),
                 _store.family_filter(F, mention.family or "other"),
                 func.lower(F.object_canonical) != func.lower(func.trim(mention.name or ""))))
    if hasattr(F, "object_is_literal"):
        q = q.filter(F.object_is_literal.is_(False))
    if before is not None:
        q = q.filter(F.created_at < before)
    links = link_map(link_rows(db, tables, mem, family=mention.family))
    out: List[Candidate] = []
    seen = {key_of(root_of(mention.name, links))}
    for r, d in nearest(db, q, col, vec, NEAREST_FACTS):
        if not _id.valid_name(r.object_canonical):
            continue
        root = root_of(r.object_canonical.strip(), links)
        if key_of(root) in seen:
            continue
        seen.add(key_of(root))
        out.append(Candidate(
            name=root,
            mention=Mention(name=r.object_label, subject=r.subject_label, relation=r.predicate_raw,
                            family=r.predicate_family or mention.family),
            fact_id=r.id, source_id=getattr(r, tables.source), distance=d,
        ))
        if len(out) >= SHORTLIST:
            break
    return out


def _with_words(items, words_of) -> list:
    """Each candidate with the memory's words that stated it; one whose
    memory was forgotten (no words come back) is not offered, and an item
    left with none is not asked."""
    ids = [c.fact_id for _, cs in items for c in cs]
    texts = words_of(ids) or {}
    out = []
    for m, cs in items:
        kept = [replace(c, mention=replace(c.mention, words=words_around(texts[c.fact_id], c.mention.name)))
                for c in cs if texts.get(c.fact_id)]
        if kept:
            out.append((m, kept))
    return out


def unite_new_objects(
    db,
    tables: _store.Tables,
    mem: _store.Memory,
    mentions: Sequence[Mention],
    *,
    ask: Optional[Callable[[str, bool], Optional[str]]],
    embed: Optional[Callable[[Mention], Optional[list]]],
    words_of: Callable[[Sequence[Any]], Dict[Any, str]],
    is_open: Any,
    lock: Optional[Callable[[], None]] = None,
    memory_id: Any = None,
    model: Optional[str] = None,
    strict: bool = True,
    severe: bool = True,
    think: bool = False,
) -> dict:
    """Link each new object of a write to the object of the memory it is
    the same thing as, when the writer says so twice (the module's
    docstring). Called before the write's facts are written, in its
    transaction; it writes link rows only, and the facts are then written
    exactly as without it.

    `lock()` is taken once there is a new object to ask about, and the
    objects are read again under it: two writes of one memory at once (the
    parallel drain) do not both decide the same name (the review's P3.10).

    `strict`: a vector or a model call that fails, or a vector that cannot
    be computed, raises `store.ResolutionUnavailable`, an answer that cannot
    be used (with `severe`) `store.ResolutionUnusable`, and the write rolls
    back and is tried again: a failure is never "different", which would
    leave the object apart for good (it is not asked again once a fact
    names it). Without `strict` nothing is linked and the write goes on.

    Returns the counts: new, asked, linked, apart ("different"), unsure."""
    out = {"new": 0, "asked": 0, "linked": 0, "apart": 0, "unsure": 0}
    if ask is None or embed is None or tables.Link is None or not _store._pg(db):
        return out
    fresh = new_mentions(db, tables, mem, mentions)
    if fresh and lock is not None:
        lock()
        fresh = new_mentions(db, tables, mem, fresh)
    out["new"] = len(fresh)
    try:
        items: List[Tuple[Mention, List[Candidate]]] = []
        for m in fresh:
            vec = embed(m)
            if vec is None:
                raise _store.ResolutionUnavailable("no vector for a new object")
            with db.begin_nested():
                cands = nearest_objects(db, tables, mem, vec, m, is_open=is_open)
            if cands:
                items.append((m, cands))
        items = _with_words(items, words_of) if items else []
        if not items:
            return out
        decisions = decide(ask, items, think=think, severe=severe)
    except StaleDataError:
        raise   # the memory erased meanwhile: `worker.write_memory` decides (5.12.2)
    except _vocabulary.UnreadableAnswer as e:
        logger.error("[memory] same-object answer unusable: %s", _log.error(e))
        if strict:
            raise _store.ResolutionUnusable(f"same-object answer unusable: {_log.error(e)}") from e
        return out
    except Exception as e:  # noqa: BLE001 - a failed call is not an answer
        logger.error("[memory] same-object question failed: %s", _log.error(e))
        if strict:
            if isinstance(e, _store.ResolutionUnavailable):
                raise
            raise _store.ResolutionUnavailable(f"could not ask whether objects are the same: {_log.error(e)}") from e
        return out
    out["asked"] = len(items)
    for (m, cands), d in zip(items, decisions):
        if d.answer == DIFFERENT:
            out["apart"] += 1
            continue
        if not isinstance(d.answer, int):
            out["unsure"] += 1
            continue
        c = cands[d.answer]
        with db.begin_nested():
            _row, outcome = add_link(
                db, tables, mem, m.name, c.name, decided_by=BY_THE_WRITER, family=m.family,
                source_memory_id=memory_id, source_fact_id=c.fact_id, canonical_memory_id=c.source_id,
                reason=reason_of(d, model=model))
        out["linked" if outcome == "created" else "unsure"] += 1
    # Counts only: the names are the end user's words (CONTRIBUTING.md).
    logger.info("[memory] same object: %s", out)
    return out


# ---------------------------------------------------------------------------
# the objects a memory already holds (manage review-objects)
# ---------------------------------------------------------------------------

# At most this many objects of one memory are reviewed in one run, oldest
# first (a bounded read; the run says when there were more), and this many
# are asked in one question (Fan et al. 2024, batched).
REVIEW_MAX = 2000
REVIEW_BATCH = 4


@dataclass
class Proposal:
    """A link a review proposes: what `apply_proposals` writes, and nothing
    else (the review's P2.6: the dry run and the apply cannot differ)."""
    name: str
    canonical: str
    source_memory_id: Any           # the memory of the later object's first fact
    source_fact_id: Any             # the fact of the object it is the same as
    canonical_memory_id: Any
    reason: dict = field(default_factory=dict)
    family: str = "other"           # the relation family it was decided in


def review_objects(
    db,
    tables: _store.Tables,
    mem: _store.Memory,
    *,
    ask: Callable[[str, bool], Optional[str]],
    words_of: Callable[[Sequence[Any]], Dict[Any, str]],
    is_open: Any,
    model: Optional[str] = None,
    severe: bool = True,
    think: bool = False,
    limit: int = REVIEW_MAX,
    batch: int = REVIEW_BATCH,
    before_each: Optional[Callable[[], bool]] = None,
) -> dict:
    """The question of `unite_new_objects` for the objects a memory already
    holds: each object of an open fact, in the order the memory first named
    it, with the objects named before it that its first open fact reads
    alike to. Writes nothing: the "same" twice are `Proposal`s, which
    `apply_proposals` writes. An object with an alias or a link is not
    asked. `before_each()` is called before each question and False stops
    the run (the operator's spending cap). A failed or unusable answer
    raises.

    Returns {"objects", "asked", "more" (objects past `limit`), "stopped",
    "proposals": [Proposal], "apart": [...], "unsure": [...]}."""
    F = tables.Fact
    col = getattr(F, tables.vector) if tables.vector else None
    out: Dict[str, Any] = {"objects": 0, "asked": 0, "more": False, "stopped": False, "proposals": [],
                           "apart": [], "unsure": []}
    if col is None or tables.Link is None or not _store._pg(db):
        return out
    q = (db.query(F.id, F.subject_label, F.predicate_raw, F.object_label, F.object_canonical,
                  F.predicate_family, getattr(F, tables.source), F.created_at)
         .filter(mem.facts, is_open, col.isnot(None)))
    if hasattr(F, "object_is_literal"):
        q = q.filter(F.object_is_literal.is_(False))
    first: Dict[str, Any] = {}
    alias_map = _store.memory_alias_map(db, tables, mem)
    links = link_map(link_rows(db, tables, mem))
    linked = set(links) | {key_of(v) for v in links.values()}
    # The oldest fact of each object: read in pages of the facts' order, so
    # that a memory of many facts is never loaded whole.
    page, offset = 1000, 0
    while len(first) <= limit:
        rows = q.order_by(F.created_at, F.id).offset(offset).limit(page).all()
        if not rows:
            break
        offset += len(rows)
        for r in rows:
            key = key_of(r.object_canonical)
            if (key and key not in first and key not in alias_map and key not in linked
                    and (r.predicate_family or "other") not in NEVER_ASKED
                    and _id.valid_name(r.object_canonical)):
                first[key] = r
    names = list(first.values())
    out["more"] = len(names) > limit
    names = names[:limit]
    out["objects"] = len(names)
    items: List[Tuple[Mention, List[Candidate], Any]] = []
    for r in names[1:]:
        vec = db.query(col).filter(F.id == r.id).scalar()
        m = Mention(name=r.object_canonical.strip(), subject=r.subject_label, relation=r.predicate_raw,
                    family=r.predicate_family)
        with db.begin_nested():
            cands = nearest_objects(db, tables, mem, vec, m, is_open=is_open, before=r.created_at)
        if cands:
            items.append((m, cands, r))
    texts = words_of([c.fact_id for _, cs, _ in items for c in cs] + [r.id for _, _, r in items]) or {}
    ready = []
    for m, cs, r in items:
        kept = [replace(c, mention=replace(c.mention, words=words_around(texts[c.fact_id], c.mention.name)))
                for c in cs if texts.get(c.fact_id)]
        if kept and texts.get(r.id):
            ready.append((replace(m, words=words_around(texts.get(r.id), r.object_label)), kept, r))
    for i in range(0, len(ready), max(1, batch)):
        if before_each is not None and not before_each():
            out["stopped"] = True
            break
        chunk = ready[i:i + max(1, batch)]
        decisions = decide(ask, [(m, cs) for m, cs, _ in chunk], think=think, severe=severe)
        out["asked"] += len(chunk)
        for (m, cs, r), d in zip(chunk, decisions):
            if not isinstance(d.answer, int):
                out["apart" if d.answer == DIFFERENT else "unsure"].append(m.name)
                continue
            c = cs[d.answer]
            out["proposals"].append(Proposal(
                name=m.name, canonical=c.name, source_memory_id=r[6], source_fact_id=c.fact_id,
                canonical_memory_id=c.source_id, reason=reason_of(d, model=model), family=family_of(m.family)))
    logger.info("[memory] objects reviewed: %d objects, %d asked, %d proposed, %d apart, %d unsure",
                out["objects"], out["asked"], len(out["proposals"]), len(out["apart"]), len(out["unsure"]))
    return out


def apply_proposals(db, tables: _store.Tables, mem: _store.Memory, proposals: Sequence[Proposal], *,
                    is_open: Any, names_of: Optional[Callable[[Any], Any]] = None,
                    decided_by: str = BY_A_REVIEW) -> List[Tuple[Proposal, Any, str]]:
    """Write exactly the links of `proposals` (a review's dry run), each
    only while both names are still named by an open fact of the memory.
    No model is called. Returns [(proposal, row or None, outcome)], outcome
    as `add_link`, or "gone" for a name no open fact names any more. Two
    applies or an apply and a write at once: the one that loses the unique
    name of the memory (no lock is held here) is "taken", and the others are
    still written (the review of the rebuild, P3.7). The caller commits.
    `names_of(Fact)`: the column the names are read in, lowercased by the
    database (6.20.0: the subject of `review_subjects`); the object's when
    None."""
    from sqlalchemy.exc import IntegrityError

    F = tables.Fact
    named_in = names_of(F) if names_of is not None else func.lower(F.object_canonical)

    def named(names) -> bool:
        return bool(db.query(exists().where(
            mem.facts, is_open,
            named_in.in_([func.lower(func.trim(n)) for n in names]))).scalar())

    out = []
    for p in proposals:
        links = link_map(link_rows(db, tables, mem, family=p.family))
        group = [n for n in [p.canonical, *links] if key_of(root_of(n, links)) == key_of(root_of(p.canonical, links))]
        if not (named([p.name]) and named(group)):
            out.append((p, None, "gone"))
            continue
        try:
            with db.begin_nested():
                row, outcome = add_link(db, tables, mem, p.name, p.canonical, decided_by=decided_by,
                                        family=p.family, source_memory_id=p.source_memory_id,
                                        source_fact_id=p.source_fact_id,
                                        canonical_memory_id=p.canonical_memory_id, reason=p.reason)
        except IntegrityError:
            row, outcome = None, "taken"
        out.append((p, row, outcome))
    return out


# ---------------------------------------------------------------------------
# the subjects a memory with no end user already holds (6.20.0, off;
# manage review-subjects)
# ---------------------------------------------------------------------------
#
# A memory with no end user (an agent's notes, a team's memory) weighs a fact
# only against the facts of its own subject (6.7.0 and 6.9.0,
# `candidates.about_the_subject`): the memory is an archive of different
# things, and a name is the only entity the store holds (Ritter et al.,
# EMNLP 2008, section 4.2, one x per contradiction set). The price, said in
# `candidates.for_the_judge`: "two spellings of one thing ("Cloud", "Korely
# Cloud") are two subjects in such a memory until an alias ... unites them".
# Measured on 22 Nov 2026 on the export of a team's memory (1,537 facts, 700
# subjects): one product under four subjects, and four version facts left
# open on three of them while the fourth moved on.
#
# The review below asks, once for each pair, whether two subjects the
# memory already holds are one thing, and a "same" twice is a LINK between
# the two names in the judge's wall (korely-agent: `agent_object_links` with
# scope 'wall'), never an alias: no fact is rewritten, and the reads do not
# follow it (a later piece). The judge then weighs a fact of one name
# against the facts of the other (`candidates.for_the_judge(subject_names=)`),
# and every closing made only so is recorded on the link
# (`record_closures`), which `remove_link` gives back before the link goes.
#
#   1. Who enters. Admitted types, not forbidden ones: a subject some open
#      fact types as one of `identity.TYPES_THAT_RENAME` (product,
#      organization: the entities that really get renamed, the decision of
#      26 Sep 2026), and that NO open fact types `PERSON` (two people who
#      share a name are not one person; a nickname is not a translation).
#      `identity.valid_name` on both names. The extractor's type is a field
#      of the fact, never a word read in a name.
#   2. The guard of numbers, on every pair, before the shortlist
#      (`numbers_of`): two names whose numbers differ in value are never
#      offered to each other ("Cloud 1.0.30" is not "Cloud", 二号楼 is not
#      三号楼), read before any normalization, for every script.
#   3. Blocking by vectors (Papadakis et al. 2020, above): each subject, in
#      the order the memory first named it, against the admitted subjects
#      named before it, so every pair is seen once and a short form born
#      first ("Cloud", 7 Nov) is offered to the longer one born after it.
#      The shortlist is read from the nearest open facts to each of the
#      subject's first `FACTS_SHOWN` open facts (each fact one key, as
#      redundancy-positive blocking puts a record in several blocks, the
#      survey's section 3), at most `SHORTLIST` subjects, nearest first.
#   4. The evidence of each side: its first `FACTS_SHOWN` open facts and the
#      memory's words around its name (`WORDS_MAX`). With the facts the
#      model sees the neighbours of both names (the same versions, the same
#      server): collective entity resolution, Bhattacharya and Getoor,
#      "Collective Entity Resolution in Relational Data", ACM TKDD 1(1),
#      2007, which resolves references by their related references as well
#      as by their attributes. Without the facts a short form would have no
#      tie beyond its name.
#   5. Two readings (Wang et al. 2025, above: select, then match the chosen
#      pair framed the other way round), for a model that answers with a
#      choice (`decide_by_choice`): a union only when the first chooses a
#      candidate and the second, the pair alone, says "same". "none",
#      "unsure", a disagreement, an answer outside the choices: no proposal.
#      The model's probability is never read (Xiong et al., ICLR 2024).
#   6. A dry run calls no model: the pairs that would be asked, with their
#      distance, the facts of each side and the size of the questions.
#   7. A call that fails or an answer that cannot be used stops the run
#      (`deferred`): what was decided before it stays proposed, nothing is
#      written, and the next run asks again. No lock is held while the model
#      answers: every read comes first, and the engine's transaction ends
#      before each question (`release`); only `apply_proposals` writes, in
#      the caller's short transaction.

#: Who may enter on both sides of a pair (step 1).
SUBJECT_TYPES = _id.TYPES_THAT_RENAME
#: The type that keeps a name out, from one open fact (step 1).
PERSON = "person"
#: The open facts of a side shown to the model and used as blocking keys.
FACTS_SHOWN = 8
#: Who decided a link between two subjects (`Tables.Link.decided_by`).
BY_A_SUBJECT_REVIEW = "subject_review"
#: The family a link between two subjects is stored with: a subject is in no
#: relation family, and the reads apply such a link nowhere (6.20.0).
SUBJECT_FAMILY = "other"
#: The first reading's two answers that are not a candidate.
NONE = "none"


def numbers_of(name: Optional[str]) -> Tuple[float, ...]:
    """The numbers written in a name, by VALUE, in order (step 2 of the
    review): a run of decimal digits of any script is one integer read
    positionally, and any other character with a numeric value (二, Ⅱ, ፩,
    ½) is its own value. The Unicode Character Database gives every
    character its Numeric_Type and Numeric_Value (UAX #44): Numeric_Type
    Decimal is a digit of a positional decimal system, category Nd
    (`unicodedata.decimal`), and every numeric character has a value
    (`unicodedata.numeric`). Read on the name as written, before any
    normalization, so "١٥" and "15" and full-width digits are one number,
    and no list of digits or of scripts is written here. Numbers written
    as words ("two") and Roman numerals in Latin letters ("Rocky II") are
    letters, not numbers: the model reads them (a limit, docs/guide/
    limits.md)."""
    import unicodedata

    out: List[float] = []
    run = ""
    for ch in name or "":
        d = unicodedata.decimal(ch, None)
        if d is not None:
            run += str(d)
            continue
        if run:
            out.append(int(run))
            run = ""
        v = unicodedata.numeric(ch, None)
        if v is not None:
            out.append(v)
    if run:
        out.append(int(run))
    return tuple(out)


def same_numbers(a: Optional[str], b: Optional[str]) -> bool:
    """Whether two names may be offered to each other by the guard of
    numbers: the same values in the same order (`numbers_of`)."""
    return numbers_of(a) == numbers_of(b)


@dataclass(frozen=True)
class Side:
    """One subject of the review, with its evidence (step 4)."""
    name: str                       # the name the memory holds it under (a link's root, for a candidate)
    key: str                        # the subject as the judge compares it (`key_of`)
    facts: Tuple[str, ...] = ()     # "subject | relation | object", oldest first
    words: Optional[str] = None     # the memory's words around the name
    fact_id: Any = None             # its first open fact
    source_id: Any = None           # the memory that stated that fact
    distance: Optional[float] = None


@dataclass(frozen=True)
class Pair:
    """A pair the review would ask (the dry run's line)."""
    name: str
    candidate: str
    distance: Optional[float]
    facts: int
    candidate_facts: int


# The sentence on short forms (the design of 23 Nov 2026, written after the
# wrong answers of 12 Nov on initials with no tie: "MS" / "Microsoft"), and
# the rule measured on 12 Nov (Jev: 0 wrong of 38 different, 28 of 29 true
# pairs), here with the side's facts beside its words. Neither is a list of
# words: they say what evidence decides.
_SHARED_NAME = (' A shared name alone never decides: answer "same" only when B\'s words or facts '
                "carry a tie to A beyond the name (a relationship, a role, a place or an attribute "
                "that A's words or facts also state, or a fuller or translated form of the same name "
                'with a context that agrees). When B\'s words and facts give nothing beyond the name, '
                'the answer is "unsure".')
_SHORT_FORM = (" When one name is a shorter form of the other (fewer words, initials, or the other "
               "name with something added), answer \"same\" only when the memory's words or facts of "
               "the shorter one tie it to the longer one beyond the shared name: the longer name, its "
               'owner, or a fact that only that thing could hold. Otherwise the answer is "unsure".')

SUBJECT_SELECT = (
    f"{_ABOUT} B is a name the memory uses, with its open facts and the memory's words that "
    "used it; under it are names the same memory used before it, found because their facts "
    "read alike, each with its facts and words.\n\n"
    "Decide whether B refers to the very same real-world thing as one of the names listed. "
    "Decide from the facts and the words, as a person reading both would. " + _FORMS
    + _PARTS + _SHARED_NAME + _SHORT_FORM + " " + _UNSURE
)

SUBJECT_PAIR = (
    f"{_ABOUT} A is a name the memory already uses and B a name it used later, each with its "
    "open facts and the memory's words that used it.\n\n"
    "Are A and B two different things? " + _TWO_THINGS + _PARTS + _ONE_THING
    + _SHARED_NAME + _SHORT_FORM + " " + _UNSURE
)


def _side_state(s: Side) -> dict:
    return {"name": one_line(s.name, 200), "facts": [one_line(f, 300) for f in s.facts],
            "memory_words": one_line(s.words) if s.words else ""}


def select_state(s: Side, cands: Sequence[Side]) -> Tuple[dict, dict]:
    """The first reading for a choice model: (state, criteria). The
    candidates are numbered nearest first; the choices are their numbers,
    `NONE` and `UNSURE`."""
    state = {"B": _side_state(s),
             "names_used_before": {str(k): _side_state(c) for k, c in enumerate(cands, 1)}}
    criteria = {str(k): f"B is the very same real-world thing as name {k}" for k in range(1, len(cands) + 1)}
    criteria[NONE] = "B is a different thing from every name listed"
    criteria[UNSURE] = "the facts and the words do not settle it"
    return state, criteria


def pair_state(held: Side, s: Side) -> Tuple[dict, dict]:
    """The second reading: the pair alone, the name the memory held first as
    A (`pair_question`'s framing)."""
    return ({"A": _side_state(held), "B": _side_state(s)},
            {DIFFERENT: "A and B are two different things",
             SAME: "A and B are one thing, named in two forms",
             UNSURE: "the facts and the words do not settle it"})


def _chosen(choose, state: dict, instructions: str, criteria: dict, severe: bool) -> str:
    """One reading: the choice, a key of `criteria`; anything else is no
    answer (`severe`: `vocabulary.UnreadableAnswer`) or UNSURE."""
    got = choose(state, instructions, criteria)
    v = got.strip().lower() if isinstance(got, str) else None
    if v in criteria:
        return v
    if severe:
        raise _vocabulary.UnreadableAnswer(f"the choice is none of {len(criteria)} allowed")
    return UNSURE


def decide_by_choice(
    choose: Callable[[dict, str, dict], Optional[str]],
    items: Sequence[Tuple[Side, Sequence[Side]]],
    *,
    severe: bool = False,
) -> List[Decision]:
    """The decision for each subject (step 5 of the review), for a model
    that answers with one choice among named criteria (`choose(state,
    instructions, criteria)`: OpenRouter's decisions API, TypeSafe Jev). The
    first reading selects among the candidates (`SUBJECT_SELECT`), the
    second matches the chosen pair alone, framed the other way round
    (`SUBJECT_PAIR`); an int answer only when the first chose a candidate
    and the second says "same". "none" is DIFFERENT, a disagreement or
    "unsure" UNSURE. `choose` raising, or an unusable answer with `severe`,
    propagates: nothing was decided."""
    out: List[Decision] = []
    for s, cands in items:
        state, criteria = select_state(s, cands)
        first = _chosen(choose, state, SUBJECT_SELECT, criteria, severe)
        readings: Tuple[dict, ...] = ({"framing": "select", "verdict": first, "of": len(cands)},)
        if not first.isdigit():
            out.append(Decision(answer=DIFFERENT if first == NONE else UNSURE, readings=readings))
            continue
        k = int(first) - 1
        state, criteria = pair_state(cands[k], s)
        second = _chosen(choose, state, SUBJECT_PAIR, criteria, severe)
        readings += ({"framing": "pair", "verdict": second},)
        out.append(Decision(answer=k if second == SAME else UNSURE, readings=readings))
    return out


def question_size(s: Side, cands: Sequence[Side]) -> int:
    """The characters the two readings of one subject would send at most
    (the dry run's measure of a run's size): the first, and the second on
    its largest candidate."""
    import json as _json

    first = len(SUBJECT_SELECT) + len(_json.dumps(select_state(s, cands), ensure_ascii=False))
    second = max((len(SUBJECT_PAIR) + len(_json.dumps(pair_state(c, s), ensure_ascii=False))
                  for c in cands), default=0)
    return first + second


def subject_key(F: Any) -> Any:
    """A fact's subject as the judge compares it (`candidates._subject_of`:
    the canonical name, else the written one, the database's lower())."""
    from .candidates import _subject_of

    return _subject_of(F)


def admitted_subjects(db, tables: _store.Tables, mem: _store.Memory, *, is_open: Any,
                      limit: int = REVIEW_MAX) -> List[Tuple[str, str, Any]]:
    """The subjects of the memory's open facts that may enter the review
    (step 1), in the order the memory first named them: [(key, name,
    first_at)], at most `limit` + 1 (the caller says when there were more).
    One grouped read (Postgres: `bool_or`)."""
    from sqlalchemy import and_, false, not_

    F = tables.Fact
    key = subject_key(F)
    admitted = func.bool_or(F.subject_type.in_(sorted(SUBJECT_TYPES)))
    person = func.coalesce(func.bool_or(F.subject_type == PERSON), false())
    first = func.min(F.created_at)
    rows = (db.query(key.label("k"), func.min(func.coalesce(F.subject_canonical, F.subject_label)).label("n"),
                     first.label("first"))
              .filter(mem.facts, is_open)
              .group_by(key)
              .having(and_(func.coalesce(admitted, false()), not_(person)))
              .order_by(first, key)
              .limit(limit + 1).all())
    return [(r.k, r.n.strip(), r.first) for r in rows if r.k and _id.valid_name(r.n)]


def subject_side(db, tables: _store.Tables, mem: _store.Memory, key: str, name: str, *,
                 is_open: Any) -> Tuple[Side, List[Any]]:
    """A subject's evidence (step 4): its first `FACTS_SHOWN` open facts,
    oldest first, and their vectors (the blocking keys of step 3)."""
    F = tables.Fact
    col = getattr(F, tables.vector) if tables.vector else None
    cols = [F.id, F.subject_label, F.predicate_raw, F.object_label, getattr(F, tables.source)]
    if col is not None:
        cols.append(col)
    rows = (db.query(*cols).filter(mem.facts, is_open, subject_key(F) == key)
              .order_by(F.created_at, F.id).limit(FACTS_SHOWN).all())
    facts = tuple(f"{r[1]} | {(r[2] or '').replace('_', ' ')} | {r[3]}" for r in rows)
    vectors = [r[5] for r in rows if col is not None and r[5] is not None]
    first = rows[0] if rows else None
    return (Side(name=name, key=key, facts=facts, fact_id=first[0] if first else None,
                 source_id=first[4] if first else None), vectors)


def nearest_subjects(db, tables: _store.Tables, mem: _store.Memory, vectors: Sequence[Any], *,
                     among: Dict[str, str], is_open: Any) -> List[Tuple[str, float]]:
    """The subjects of `among` ({key: name}: the admitted subjects named
    before, the guard of numbers passed) nearest to any of `vectors`, at
    most `SHORTLIST`, nearest first: [(key, distance)]. Read from the
    `NEAREST_FACTS` nearest open facts to each vector, on the HNSW index
    with its depth set (`vector_search.nearest`). Postgres with pgvector
    only."""
    from .vector_search import nearest

    F = tables.Fact
    col = getattr(F, tables.vector) if tables.vector else None
    if col is None or not vectors or not among or not _store._pg(db):
        return []
    key = subject_key(F)
    best: Dict[str, float] = {}
    for vec in vectors:
        q = db.query(key.label("k")).filter(mem.facts, is_open, col.isnot(None), key.in_(sorted(among)))
        with db.begin_nested():
            found = nearest(db, q, col, vec, NEAREST_FACTS)
        for k, d in found:
            if d is not None and (k not in best or d < best[k]):
                best[k] = d
    return sorted(best.items(), key=lambda kd: (kd[1], kd[0]))[:SHORTLIST]


def review_subjects(
    db,
    tables: _store.Tables,
    mem: _store.Memory,
    *,
    words_of: Callable[[Sequence[Any]], Dict[Any, str]],
    is_open: Any,
    choose: Optional[Callable[[dict, str, dict], Optional[str]]] = None,
    dry_run: bool = False,
    model: Optional[str] = None,
    limit: int = REVIEW_MAX,
    before_each: Optional[Callable[[], bool]] = None,
    release: Optional[Callable[[], None]] = None,
) -> dict:
    """The review of the subjects a memory with no end user already holds
    (the section's steps): each admitted subject, in the order the memory
    first named it, against the admitted subjects named before it that its
    facts read alike to. Writes nothing: the "same" twice are `Proposal`s,
    which `apply_proposals(names_of=subject_key)` writes.

    `dry_run`: no model is called (`choose` is not read): the result's
    `pairs` are the pairs that would be asked, `questions` how many
    subjects, and `characters` the size of their questions at most
    (`question_size`). `before_each()` is called before each subject is
    asked and False stops the run (the operator's spending cap). A call
    that fails or an answer that cannot be used stops it too, `deferred`
    (step 7). `release()` is called before each question, after every read
    of the run: the engine ends its transaction there (korely-agent: a
    rollback, the run writes nothing), so that no lock of a read, not even
    the ACCESS SHARE of a table, is held while the model answers.

    Returns {"subjects", "more", "questions", "characters", "pairs",
    "asked", "stopped", "deferred", "proposals", "apart", "unsure"}."""
    out: Dict[str, Any] = {"subjects": 0, "more": False, "questions": 0, "characters": 0, "pairs": [],
                           "asked": 0, "stopped": False, "deferred": None, "proposals": [],
                           "apart": [], "unsure": []}
    if tables.Link is None or not _store._pg(db) or (not dry_run and choose is None):
        return out
    subjects = admitted_subjects(db, tables, mem, is_open=is_open, limit=limit)
    out["more"] = len(subjects) > limit
    subjects = subjects[:limit]
    out["subjects"] = len(subjects)
    alias_map = _store.memory_alias_map(db, tables, mem)
    links = link_map(link_rows(db, tables, mem))
    names = {k: n for k, n, _ in subjects}
    before: List[str] = []
    items: List[Tuple[Side, List[Side]]] = []
    for k, n, _first in subjects:
        own = key_of(root_of(n, links))
        among = {c: names[c] for c in before
                 if key_of(root_of(names[c], links)) != own and same_numbers(n, names[c])}
        before.append(k)
        if key_of(n) in alias_map or key_of(n) in links:
            continue    # an alias decided it, or it is linked already: never asked again
        side, vectors = subject_side(db, tables, mem, k, n, is_open=is_open)
        near = nearest_subjects(db, tables, mem, vectors, among=among, is_open=is_open)
        cands: List[Side] = []
        seen = set()
        for c, d in near:
            root = root_of(names[c], links)
            if key_of(root) in seen:
                continue
            seen.add(key_of(root))
            held, _v = subject_side(db, tables, mem, c, names[c], is_open=is_open)
            cands.append(replace(held, name=root, distance=d))
        if cands:
            items.append((side, cands))
    texts = words_of([s.fact_id for s, cs in items] + [c.fact_id for _, cs in items for c in cs]) or {}

    def worded(s: Side) -> Side:
        return replace(s, words=words_around(texts.get(s.fact_id), s.name)) if texts.get(s.fact_id) else s

    items = [(worded(s), [worded(c) for c in cs]) for s, cs in items]
    out["questions"] = len(items)
    for s, cs in items:
        out["characters"] += question_size(s, cs)
        out["pairs"].extend(Pair(name=s.name, candidate=c.name, distance=c.distance, facts=len(s.facts),
                                 candidate_facts=len(c.facts)) for c in cs)
    if dry_run:
        logger.info("[memory] subjects reviewed dry: %d subjects, %d questions, %d pairs",
                    out["subjects"], out["questions"], len(out["pairs"]))
        return out
    for s, cs in items:
        if before_each is not None and not before_each():
            out["stopped"] = True
            break
        if release is not None:
            release()
        try:
            [d] = decide_by_choice(choose, [(s, cs)], severe=True)
        except Exception as e:  # noqa: BLE001 - a failed call or answer is no verdict: the run waits
            out["deferred"] = _log.error(e)
            logger.error("[memory] subject review deferred, the model gave no usable answer: %s",
                         _log.error(e))
            break
        out["asked"] += 1
        if not isinstance(d.answer, int):
            out["apart" if d.answer == DIFFERENT else "unsure"].append(s.name)
            continue
        c = cs[d.answer]
        out["proposals"].append(Proposal(
            name=s.name, canonical=c.name, source_memory_id=s.source_id, source_fact_id=c.fact_id,
            canonical_memory_id=c.source_id, reason=reason_of(d, model=model), family=SUBJECT_FAMILY))
    logger.info("[memory] subjects reviewed: %d subjects, %d asked, %d proposed, %d apart, %d unsure%s",
                out["subjects"], out["asked"], len(out["proposals"]), len(out["apart"]), len(out["unsure"]),
                ", deferred" if out["deferred"] else "")
    return out
