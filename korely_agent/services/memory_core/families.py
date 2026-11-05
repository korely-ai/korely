"""Predicate family taxonomy (memory_core, shared by every engine).

Facts are grouped by HUMAN-READABLE category (Preferences, People, Places,
Work, etc.) rather than by raw `predicate_canonical` (`likes`, `dislikes`,
`lives_in`, `works_at`, ...): the user manages memory as objects they
can read, not as a database schema.

Grounding:
    - Huang, Gutierrez, Kamana, MacNeil, "Memory Sandbox: Transparent and
      Interactive Memory Management for Conversational Agents", UIST 2023
      Adjunct, arXiv:2308.01542: memories shown as data objects the user
      can view, edit and delete.
    - Apple's App Privacy Report (iOS 15.2+): a human-readable category
      taxonomy instead of raw identifiers.
    (A "Zhang et al., DIS '24" reference that stood here could not be found
    and was replaced in the 2026-09-27 audit.)

The family of a relation is the learned vocabulary's: the model is asked
once, when a relation is born, which family it belongs to (BUG-109,
2026-09-27), and the answer is stored on the relation and on every fact
(`predicate_family`); readers use `family_of_the_fact`. `other` is what is
left when even the model says none fits.

No word lists (3.3.0, the rule of 8 Oct 2026: "in the code we
must not have any list of words"). The static map below holds only
the predicates of our own schema, the names the extraction prompt itself
asks for: the identity predicates the alias rule reads (and `instructs`, for
the standing instructions, until 5.0.0 removed them). Until
3.3.0 it also held 49 English predicates ("likes" -> preferences, "works_at"
-> work ...), read as a fallback for facts written before the family was
stored; a migration stored those facts' families once, as data, and the
map left the code.
"""
from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Family taxonomy
# ---------------------------------------------------------------------------

PreFamily = str  # alias for clarity in type hints

PREDICATE_TO_FAMILY: dict[str, PreFamily] = {
    # ── IDENTITY / RENAMES (2026-09-27) ─────────────────────────────────
    # Statements about WHAT an entity IS CALLED. They describe the name, not
    # the world: that is why they feed the entity aliases (`agent_entity_aliases`,
    # through `identity`) instead of staying inert triples in `other`. `replaces` is NOT here (2026-09-27): it links two different
    # entities, a successor and a predecessor (Wikidata P1365), not two names
    # of one. On a real memory's facts it was a rename 1 time in 5
    # ("Deepgram Nova-3 replaces Soniox", "Postgres 17 replaces Postgres 18",
    # "Korely Memory replaces Mem0" are replacements). Its family is decided
    # by the learned vocabulary, as for every new predicate.
    "renamed_to": "identity",
    "formerly": "identity",
    "codename": "identity",
    "also_known_as": "identity",
}

# The identity predicates that make an alias ON THEIR OWN (2026-09-26):
# the sentence says outright that two names are one entity, and in which
# direction. `named` is NOT here: "Korely named Gordon" can mean "Korely was
# called Gordon" or "Korely gave something the name Gordon", so it stays a
# manual proposal. Value = which of the two is the NEW name.
IDENTITY_ALIAS_DIRECTION: dict[str, str] = {
    "renamed_to": "object",       # Gordon renamed_to Korely  -> Korely is the new one
    "formerly": "subject",        # Korely formerly Gordon    -> Korely is the new one
    "codename": "subject",        # Korely.ai codename Gordon -> Korely.ai is the new one
    "also_known_as": "subject",   # Korely also_known_as Gordon
}


# Family display order (most common first — Apple Privacy pattern)
FAMILY_ORDER: list[PreFamily] = [
    "preferences",
    "people",
    "places",
    "work",
    "ownership",
    "health",
    "financial",
    "events",
    "identity",
    "other",
]


# Human-readable labels (i18n-ready: caller can override per-locale)
FAMILY_LABELS_EN: dict[PreFamily, str] = {
    "preferences": "Preferences",
    "people": "People",
    "places": "Places",
    "work": "Work",
    "ownership": "Ownership",
    "health": "Health",
    "financial": "Financial",
    "events": "Events",
    "identity": "Identity",
    "other": "Other",
}



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def family_for(predicate_canonical: Optional[str]) -> PreFamily:
    """Return the family for a canonical predicate. Unknown → 'other'."""
    if not predicate_canonical:
        return "other"
    return PREDICATE_TO_FAMILY.get(predicate_canonical.lower().strip(), "other")


def all_families() -> list[PreFamily]:
    """List of all known families in display order."""
    return list(FAMILY_ORDER)


def family_label(family: PreFamily) -> str:
    """Human-readable label for a family, in English (the app's language).

    An Italian table stood next to this one until 2026-09-27: one language
    out of all, never read (the MCP and the API ask for English, the
    frontend has its own labels), removed in the audit.
    """
    return FAMILY_LABELS_EN.get(family, family.title())


def predicates_in_family(family: PreFamily) -> list[str]:
    """Inverse lookup: all canonical predicates in a given family."""
    return [p for p, f in PREDICATE_TO_FAMILY.items() if f == family]


def family_of_the_fact(predicate_family: Optional[str], predicate_canonical: Optional[str]) -> PreFamily:
    """The family a fact is SHOWN and FILTERED under.

    The family stored on the fact (decided once per learned relation, see
    `vocabulary.py`) wins; a fact written before the column existed, or whose
    relation was never classified, falls back to the static map. One function,
    so the panel, the MCP table and the REST filters never disagree.
    """
    fam = (predicate_family or "").strip().lower()
    if fam in FAMILY_ORDER:
        return fam
    return family_for(predicate_canonical)


# Opposing predicates are NOT a list in this file (3.3.0). Until then five
# English pairs lived here (likes/dislikes, married_to/divorced_from,
# works_at/left_company, lives_in/moved_from, friend_of/estranged_from) and
# widened the judge's first stage of candidates. Every pair shares subject and
# object, which is the candidate stage of BUG-111 (2026-09-27): "Gordon lacks
# Temporal Decay" next to "Gordon adopt Temporal Decay", "Claude uses Gordon"
# next to "Claude unused_in Gordon". The pairs are endless, the arguments are
# not: same subject, a compatible object, another relation
# (`contradiction.compatible_objects`, `candidates.for_the_judge` stage 1c),
# and the judge decides whether the two relations exclude each other.


# Relation cardinality is NOT a list in this file.
#
# An earlier version (korely-agent, 2026-09-10) kept ~25 predicates here marked
# as single-valued. It was hand-written feature engineering, which the
# canonicalization literature warns against directly (CESI, WWW 2018: "manual
# feature engineering is expensive and often sub-optimal"), and it could not
# work: an insurer writes `covered_under` and a recruiter writes
# `screened_for`, and the relations that matter to a client are the ones nobody
# thought to list.
#
# Cardinality lives per memory in the learned vocabulary
# (`predicate_clusters.is_functional`), classified once by the model and
# cached. See `vocabulary.py`.
