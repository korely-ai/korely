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
    alias map  = dict alias_lower -> canonical_label
    canonical  = the name after following the map (at most `MAX_HOPS` hops)
"""
from __future__ import annotations

import re
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
# A name: letters, digits, spaces and the punctuation of product names
# (Korely.ai, Claude-Code, Mem0, Model Context Protocol). No
# sentences, no dates: at most 60 characters and 5 words.
_VALID_NAME = re.compile(r"^[^\W\d_][\w .\-&'/+]{0,59}$", re.UNICODE)
MAX_NAME_WORDS = 5


def valid_name(s: Optional[str]) -> bool:
    if not s:
        return False
    s = s.strip()
    return bool(_VALID_NAME.match(s)) and len(s.split()) <= MAX_NAME_WORDS


def canonical_of(label: Optional[str], alias_map_of: Dict[str, str]) -> str:
    """The name after aliases. Follows the chain (Gordon Pro -> Gordon ->
    Korely) for at most `MAX_HOPS` hops and stops on a cycle."""
    if not label:
        return label or ""
    current_one = label.strip()
    seen: set = set()
    for _ in range(MAX_HOPS):
        k = current_one.lower()
        if k in seen:
            break
        seen.add(k)
        following = alias_map_of.get(k)
        if following is None or following.strip().lower() == k:
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
    `a_lower` is the alias about to be written)."""
    current_one = (start or "").strip().lower()
    for _ in range(MAX_HOPS + 1):
        if current_one == a_lower:
            return True
        following = alias_map_of.get(current_one)
        if following is None:
            return False
        current_one = following.strip().lower()
    return False


def name_with_history(label: Optional[str], canonical: Optional[str]) -> str:
    """How a label with a different canonical is shown: "Korely (then:
    Gordon)". The product speaks English."""
    lab = (label or "").strip()
    can = (canonical or "").strip()
    if not can or can.lower() == lab.lower():
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
    if s.lower() == o.lower():
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
    """
    if source not in SOURCES:
        return "invalid", None
    alias = (alias or "").strip()
    canonical = (canonical or "").strip()
    if not valid_name(alias) or not valid_name(canonical):
        return "invalid", None
    a_low = alias.lower()
    if canonical.lower() == a_low:
        return "same", None
    if reaches(alias_map_of, canonical, a_low):
        return "conflict", None
    final_canonical = canonical_of(canonical, alias_map_of)
    if final_canonical.lower() == a_low:
        return "same", None
    if existing_canonical is not None:
        if existing_canonical.strip().lower() == final_canonical.lower():
            return "same", final_canonical
        if existing_source == SOURCE_USER and source != SOURCE_USER:
            return "conflict", None
        return "updated", final_canonical
    return "created", final_canonical
