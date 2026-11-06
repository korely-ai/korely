"""Near-duplicates on the write: the same claim in other words (memory_core
2.8.0, cantiere 11). OFF by default.

Step 2 of `writing.write_fact` reconfirms the SAME canonical triple. A
restatement in other words ("plans outdoor wedding ceremony", later "plans
outdoor wedding") became a second open row: 559 of the 4,574 open facts of
a LongMemEval tenant (12.2%) at cosine 0.93, a quarter of the events
(measured on 5 Oct 2026). The context block then shows the same thing several
times, the counts of a profile scatter over rows, and the judge sometimes
closes one with the other as if the user had changed their mind.

Graphiti (Rasmussen et al. 2025, arXiv:2501.13956, section 2.2.2) deduplicates
a new edge with a search "constrained to edges existing between the same
entity pairs as the proposed new edge", a constraint that "prevents erroneous
combinations of similar edges between different entities", and a model then
says whether the new edge expresses "the same factual information"
(appendix 6.1.4). Here the scope is the same subject and the same relation in
the same memory, and no model is asked: the guards below keep two facts apart
whenever their words say they may differ, and the vector decides only between
restatements. The vector alone does not separate them: "owns 3 bikes" and
"owns 4 bikes" sit at cosine 0.975, an update the judge must see.

Containment as in Broder (1997, "On the resemblance and containment of
documents", section 2): c(A, B) = |S(A) & S(B)| / |S(A)|, on the set of
tokens of a canonical form that ignores punctuation and capitalization; here
the tokens are words (shingles of size 1), and c(A, B) = 1 when every word of
A is in B.

The guards, all of them (a proposal, read on 224 pairs by
one reader: an estimate, not a measurement):
  1. the same memory, the kept fact true now (`is_open`), and not a fact the
     same source stated: the facts of one memory never act on each other
     (BUG-123, `store.outside_the_source`); a list of alternatives is not
     a restatement;
  2. the new fact is current or planned (a past one has its own path);
  3. the same canonical subject and the same canonical relation;
  4. neither in the identity or instructions family (structure, not wording);
  5. the same numbers in the two objects, as a set ("3 bikes" and "4 bikes"
     stay two, and the judge closes the old one as before); no table of
     numbers written in words, which would cover one language;
  6. the capitalized words of one object are all among those of the other
     ("Hawaii" and "Oahu" stay two); in a script without case this guard
     stops nothing;
  7. the new object's words all in the kept one (c(new, kept) = 1): cosine at
     least `Twin.content`; neither containing the other: at least
     `Twin.different`;
  8. two events that happened (both current, family events) fuse only on the
     same known day: otherwise it happened twice;
  9. a new object that adds words to the kept one (c(kept, new) = 1 and not
     the reverse) is a refinement, written and judged as before (the
     first version; keeping the finer wording is a choice still to be measured).

OFF: `write_fact(twins=False)` is the default, and a model without a
measured `Thresholds.twin` (Gemini, the 8-bit model of the desktop) keeps it
off. Before it is turned on for a product: the measurement and a map of the
merged wordings per source, so that forgetting the memory whose words the row
carries relabels the row (section 6): the right to be forgotten is the detail a
customer checks.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any, List, Optional, Set, Tuple

from sqlalchemy import func

from .store import Memory, Tables, outside_the_source
from .thresholds import Twin
from .vector_search import nearest

# Words of any script, as `extraction.comparable` cuts them.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_DIGITS = re.compile(r"\d+", re.UNICODE)

FAMILIES_LEFT_OUT = ("identity",)

# Candidates read per write on Postgres, nearest first: same subject and same
# relation in one memory are few, and the nearest is the one that can pass.
LIMIT = 20


def _form(text: Optional[str]) -> str:
    return unicodedata.normalize("NFKC", text or "")


def words(text: Optional[str]) -> Set[str]:
    """The words of a text, case folded (Broder's canonical form)."""
    return set(_WORD.findall(_form(text).casefold()))


def numbers(text: Optional[str]) -> Set[str]:
    """The numbers written with digits, each digit by its value, in any
    script ("٣" is "3")."""
    return {
        "".join(str(unicodedata.decimal(c, c)) for c in run)
        for run in _DIGITS.findall(_form(text))
    }


def capitalized(text: Optional[str]) -> Set[str]:
    """The words that start with a capital letter, case folded."""
    return {w.casefold() for w in _WORD.findall(_form(text)) if w[:1].isupper()}


def _day(v: Any) -> Optional[date]:
    if not isinstance(v, datetime):
        return None
    if v.tzinfo is not None:
        v = v.astimezone(timezone.utc)
    return v.date()


def same_claim(new: Optional[str], kept: Optional[str],
                        distance: Optional[float], thresholds: Twin) -> bool:
    """Whether the object `new` restates `kept`, at this cosine distance
    between the two facts' vectors (guards 5, 6, 7 and 9 of the module)."""
    if distance is None:
        return False
    if numbers(new) != numbers(kept):
        return False
    mn, mt = capitalized(new), capitalized(kept)
    if not (mn <= mt or mt <= mn):
        return False
    pn, pt = words(new), words(kept)
    if not pn or not pt:
        return False
    if pn <= pt:
        return distance <= thresholds.content
    if pt <= pn:
        return False
    return distance <= thresholds.different


def _same_day_if_events(fact: Any, kept: Any, family: Optional[str]) -> bool:
    """Guard 8: two events that happened fuse only on the same known day."""
    if family != "events":
        return True
    if (fact.tense or "current") != "current" or (kept.tense or "current") != "current":
        return True
    a, b = _day(fact.valid_from), _day(kept.valid_from)
    return a is not None and a == b


def find_twin(
    db,
    tables: Tables,
    mem: Memory,
    fact: Any,
    *,
    family: Optional[str],
    is_open: Any,
    thresholds: Twin,
    source: Any = None,
) -> Optional[Any]:
    """The open fact of this memory that `fact` (just written, with its
    vector) restates, the nearest that passes every guard; None otherwise."""
    vector = getattr(fact, "embedding", None)
    if vector is None or family in FAMILIES_LEFT_OUT:
        return None
    if (fact.tense or "current") not in ("current", "planned"):
        return None
    F = tables.Fact
    subject = (fact.subject_canonical or fact.subject_label or "").strip()
    q = (
        db.query(F)
        .filter(
            mem.facts,
            is_open,
            F.id != fact.id,
            # The database's lower() on both sides (2.11.0, as
            # `store.find_open`): Python's is another function.
            func.lower(func.coalesce(F.subject_canonical, F.subject_label)) == func.lower(subject),
            F.predicate_canonical == fact.predicate_canonical,
            F.embedding.isnot(None),
        )
        .filter(outside_the_source(tables, source))
    )
    # 2.12.0 (BUG-320): `vector_search.nearest`, with the depth of the
    # HNSW scan on PostgreSQL (the index holds every account's facts, the
    # memory's filters come after it); on SQLite (the tests) the distance in
    # Python, as before.
    pairs: List[Tuple[Any, Optional[float]]] = nearest(db, q, F.embedding, vector, LIMIT)
    obj = fact.object_canonical or fact.object_label
    for c, d in pairs:
        if not _same_day_if_events(fact, c, family):
            continue
        if same_claim(obj, c.object_canonical or c.object_label, d, thresholds):
            return c
    return None
