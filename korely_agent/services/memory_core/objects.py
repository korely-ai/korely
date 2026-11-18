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
        "descriptions, as a person reading both would. The same thing can be named in "
        "another language or script, in a shorter or longer form, or by an abbreviation. "
        "Names that look alike, or translate one another, are still different things when "
        "their contexts point to different things: the name alone never decides. "
        + _PARTS + "\n\n"
        + "\n\n".join(blocks) + "\n\n"
        'Answer JSON only: {"answers": [{"mention": <number>, "verdict": "same" or '
        '"different" or "unsure", "same_as": <the number of the listed thing when '
        'the verdict is "same", else null>}]}, one entry for each new mention.\n'
        # The direction that loses nothing, as the vocabulary's "Prefer null
        # when unsure" (store.resolve_predicate).
        'Answer "unsure" when the contexts do not settle it.'
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
        "For each pair, are A and B two different things? Two places, organizations or "
        "things that share a name or translate each other's name are two things when "
        "their contexts locate or define them differently. "
        + _PARTS + " They are one thing when both contexts point to the same real-world "
        "thing, named in two languages, scripts or forms.\n\n"
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
    made here (the database compares with its own lower())."""
    return (name or "").strip().lower()


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
                      reason=reason, **mem.new_alias)
    db.add(row)
    db.flush()
    # The row and the sources, never the names (CONTRIBUTING.md).
    logger.info("[memory] object link created: row %s, fact %s, memory %s (%s)",
                row.id, source_fact_id, source_memory_id, decided_by)
    return row, "created"


def remove_link(db, tables: _store.Tables, row) -> None:
    """The undo: the row goes, and nothing else changes; the facts were
    never touched by it, so the memory is as it was before the link."""
    db.delete(row)
    db.flush()
    logger.info("[memory] object link removed: row %s", row.id)


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
                    is_open: Any) -> List[Tuple[Proposal, Any, str]]:
    """Write exactly the links of `proposals` (a review's dry run), each
    only while both names are still named by an open fact of the memory.
    No model is called. Returns [(proposal, row or None, outcome)], outcome
    as `add_link`, or "gone" for a name no open fact names any more. Two
    applies or an apply and a write at once: the one that loses the unique
    name of the memory (no lock is held here) is "taken", and the others are
    still written (the review of the rebuild, P3.7). The caller commits."""
    from sqlalchemy.exc import IntegrityError

    F = tables.Fact

    def named(names) -> bool:
        return bool(db.query(exists().where(
            mem.facts, is_open,
            func.lower(F.object_canonical).in_([func.lower(func.trim(n)) for n in names]))).scalar())

    out = []
    for p in proposals:
        links = link_map(link_rows(db, tables, mem, family=p.family))
        group = [n for n in [p.canonical, *links] if key_of(root_of(n, links)) == key_of(root_of(p.canonical, links))]
        if not (named([p.name]) and named(group)):
            out.append((p, None, "gone"))
            continue
        try:
            with db.begin_nested():
                row, outcome = add_link(db, tables, mem, p.name, p.canonical, decided_by=BY_A_REVIEW,
                                        family=p.family, source_memory_id=p.source_memory_id,
                                        source_fact_id=p.source_fact_id,
                                        canonical_memory_id=p.canonical_memory_id, reason=p.reason)
        except IntegrityError:
            row, outcome = None, "taken"
        out.append((p, row, outcome))
    return out
