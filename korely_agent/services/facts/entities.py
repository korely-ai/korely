"""Entity aliases, reconfirmation and memory scope for the agent facts (2026-09-27).

The rules are written once in `services/memory_core/`, the facts engine
(shared with an earlier product until 11 Oct 2026, only here since).
This module only says which tables and what a memory is on a self-hosted
install: tenant + project + end user + agent namespace, the key facts were
already deduplicated on.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy import and_, cast, func, literal, or_, select, tuple_
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from ...db.models import (AgentEntityAlias, AgentFact, AgentMemory, AgentObjectLink, PredicateAlias,
                          PredicateCluster)
from .. import vector_state
from ..memory_core import store as _store
from ..sentinels import EVERY_PROJECT, within_project

TABLES = _store.Tables(
    Fact=AgentFact,
    Alias=AgentEntityAlias,
    source="source_memory_id",
    sources="source_memory_ids",
    alias_fact="source_fact_id",
    Relation=PredicateCluster,
    Variant=PredicateAlias,
    vector="embedding",
    alias_stated="stated_canonical",
    # A fact's vector is computed on its canonical names (facts.service.
    # fact_text): when an alias changes them, the vector waits for the new
    # text, as a memory's does when its text changes (vector_state.renamed;
    # the vectors drain computes it, audit of 20 Oct 2026). The fact was
    # judged when it was written, so the drain does not judge it again (audit
    # of 27 Oct 2026: the revectoring of a rename spent on the judge, uncounted).
    text_changed=vector_state.renamed,
    # memory_core 6.1.0 (`objects`, off): two objects of one memory the
    # writer judged one thing ("Italy" and "Italia"), a link the reads
    # resolve (facts/links.py) and no write follows.
    Link=AgentObjectLink,
)


# The facts whose words the caller chose, not the extraction, by the
# `model_used` they are written with: a fact written directly
# (`write_fact_direct`, POST /v1/facts, "direct") and a correction whose
# subject is the caller's (`correct_fact`, PATCH /v1/facts/{id},
# "correction": the correction names the subject, or corrects a fact that
# was already the caller's). A correction that gives only the relation or the
# object of an extracted fact keeps the extraction's `model_used`, and its
# words are the extraction's (memory_core 6.6.0). The facts named here: their
# relation keeps its name (`predicates.resolve_relation(merge=False)`, the
# rule memory_core 3.3.0 wrote in `store.resolve_predicate(merge=)`), their
# object is not offered as another object (objects.py), and their subject is
# judged against its own facts only, and meets another subject's fact never
# (memory_core 6.5.0 and 6.6.0, `candidates.for_the_judge(subject_named=,
# named=)`). The price of a correction that keeps the extraction's
# provenance: its inherited relation is resolved with `merge=True`, as the
# extraction's words are, and its object could be offered as another object
# when `objects.SAME_OBJECT` is on (it is off); giving `subject` makes the
# correction the caller's in full.
NAMED_BY_THE_CALLER = ("direct", "correction")


def named_by_the_caller(model_used: Optional[str]) -> bool:
    """Whether a fact written with `model_used` was named by its caller."""
    return model_used in NAMED_BY_THE_CALLER


#: The same boundary as a condition on the facts table, for the engine's
#: candidates (`candidates.for_the_judge(named=)`): a fact whose subject its
#: caller named enters the judgement of another subject's fact never.
NAMED_FACTS = AgentFact.model_used.in_(NAMED_BY_THE_CALLER)


def scoped_by_an_agent():
    """The facts of an agent's memory with no end user, as a condition on the
    facts table (memory_core 6.7.0): `memory_core.owner.agent_scoped_rows`,
    the rule the extraction frames a memory by (`owner.agent_scoped`, a blank
    `agent_id` being no agent), on this install's columns. Its judge weighs
    such a fact, by meaning, against the facts of its own subject only, and
    weighs it against another fact by meaning only about that fact's subject
    (`candidates.for_the_judge(agent_scoped=, agent_scoped_facts=)`, where the
    reason is)."""
    from ..memory_core.owner import agent_scoped_rows

    return agent_scoped_rows(AgentFact.end_user_id, AgentFact.agent_id)


def _equal(col, value):
    return col.is_(None) if value is None else col == value


def facts_in_project(q, project_id):
    """The wall (`sentinels.within_project`), on a query over facts.
    EVERY_PROJECT is the operator looking at their own account from the
    dashboard; anything reached with an API key passes that key's project."""
    return within_project(q, AgentFact.project_id, project_id)


def memories_in_project(q, project_id):
    """The wall (`sentinels.within_project`), on a query over memories."""
    return within_project(q, AgentMemory.project_id, project_id)


def memory_scope(tenant_id: UUID, project_id: Optional[UUID], end_user_id: Optional[str]):
    """Who can stand next to a memory's facts: the account, the project and
    the end user, of any agent (two agents of one end user speak of the same
    person; the project is the wall between two customers of one account).
    The candidates of the contradiction judge (`candidates.for_the_judge`)
    and the facts an edited memory retires (`store.retire_the_previous_
    version`, memory_core 2.12.0: without it every write read the whole
    facts table). The hosted product had the same rule."""
    return and_(
        AgentFact.tenant_id == tenant_id,
        _equal(AgentFact.project_id, project_id),
        _equal(AgentFact.end_user_id, end_user_id),
    )


def memory_of(
    tenant_id: UUID,
    project_id: Optional[UUID],
    end_user_id: Optional[str],
    agent_id: Optional[str],
) -> _store.Memory:
    """The memory a fact is deduplicated, reconfirmed and aliased in, with
    the relation vocabulary of its end user (services/predicates.py
    `end_user_vocabulary`, the one the write resolves through: the agent namespace has
    none of its own). Until 18 Oct 2026 the vocabulary here was the whole
    account's, a filter nothing in the product read and a trap for whoever
    reused it (audit of 18 Oct 2026)."""
    from ..predicates import end_user_vocabulary

    vocab = end_user_vocabulary(tenant_id, project_id, end_user_id)
    F, A, L = AgentFact, AgentEntityAlias, AgentObjectLink
    return _store.Memory(
        facts=and_(
            F.tenant_id == tenant_id,
            _equal(F.project_id, project_id),
            _equal(F.end_user_id, end_user_id),
            _equal(F.agent_id, agent_id),
        ),
        alias=and_(
            A.tenant_id == tenant_id,
            _equal(A.project_id, project_id),
            _equal(A.end_user_id, end_user_id),
            _equal(A.agent_id, agent_id),
        ),
        new_alias=dict(
            tenant_id=tenant_id, project_id=project_id,
            end_user_id=end_user_id, agent_id=agent_id,
        ),
        links=and_(
            L.tenant_id == tenant_id,
            _equal(L.project_id, project_id),
            _equal(L.end_user_id, end_user_id),
            _equal(L.agent_id, agent_id),
        ),
        relations=vocab.relations,
        variants=vocab.variants,
        new_relation=vocab.new_relation,
    )


def name_condition(
    db: Session,
    tenant_id: UUID,
    name: str,
    column,
    *,
    project_id: Any = EVERY_PROJECT,
    end_user_id: Any = None,
    all_end_users: bool = False,
    agent_id: Optional[str] = None,
):
    """SQL: one side of a fact, `column` (its canonical name, lowercase), is
    `name` as the fact's OWN memory names it. Who reads need not know where
    an alias was born to ask about "Acme".

    An alias is a statement of one memory (`memory_of`: account, project, end
    user, agent namespace): Maria's "Acme is now Globex" does not rename
    Luca's Acme. So the name is resolved memory by memory: in each memory
    that holds `name` as an alias, with that memory's map (`identity.canonical_of`,
    its chain included); in every other memory it stays as written. The
    condition is the union: the facts of each memory under what that memory
    means by the name.

    Until 6 Oct a read resolved the name once, with ONE map built from the
    aliases of every memory in its scope (a dict keyed by the alias): with
    two end users holding the same alias for two entities one row won, so
    the answer held one end user's facts; and a memory without the alias lost
    its facts about the name, read under another memory's canonical. Across
    end users (GET /v1/facts and korely_get_facts without `user_id`) and
    across the agent namespaces of one end user. The hosted product had the
    same code (its code audit of 6 Oct 2026).

    The scope of the aliases is the read's: the account, the project when the
    caller is confined to one, the end user unless `all_end_users`, the
    agent namespace when given.

    One condition, however many memories hold the alias (audit of 27 Oct
    2026: it was a
    query per memory and two literal branches per memory, 4,000 memories
    cost a read 4,005 statements): the alias row of THE FACT'S OWN memory is
    looked up by the database, correlated with the fact, as
    `predicate_condition` does for the vocabulary. The rows of a memory
    are kept flat (`store.add_alias` points whoever pointed at an
    alias to its canonical), so one hop is what `identity.canonical_of` would
    reach by following the chain."""
    A = AgentEntityAlias
    F = AgentFact
    key = (name or "").strip().lower()
    scope = [A.tenant_id == tenant_id, A.alias_lower == key]
    if project_id is not EVERY_PROJECT:
        scope.append(_equal(A.project_id, project_id))
    if not all_end_users:
        scope.append(_equal(A.end_user_id, end_user_id))
    if agent_id is not None:
        scope.append(A.agent_id == agent_id)
    its_own = func.lower(func.trim(A.canonical_label))
    # The fact's memory holds the name as another entity ...
    elsewhere = select(*_memory_key(A)).where(*scope, its_own != key)
    # ... and the fact is about that entity.
    as_its_own = select(*_memory_key(A), its_own).where(*scope, its_own != key)
    # Uncorrelated row-value IN: the database builds each subquery once and
    # hashes it. A subquery correlated with the fact by IS NOT DISTINCT FROM
    # (null-safe, so neither indexable nor hashable) scanned the alias rows
    # once per fact: 14.7 s at 4,000 memories and 20,000 facts (review of
    # the audit of 27 Oct 2026).
    return or_(and_(column == key, tuple_(*_memory_key(F)).not_in(elsewhere)),
               tuple_(*_memory_key(F), column).in_(as_its_own))


#: Names an account may have declared (`source` "user": the dashboard's
#: alias and an end user's declared name), like keys.MAX_API_KEYS a count per
#: account. Delete one to make room (`DELETE /admin/aliases/{id}`).
MAX_DECLARED_NAMES = 1000


class TooManyNames(ValueError):
    """The account holds MAX_DECLARED_NAMES declared names."""


def _room_for_a_name(db: Session, mem, tenant_id: UUID, alias: str) -> None:
    """Refuse a NEW declared name past the cap. One declaration of the account
    at a time from the count to the commit (a transaction's lock, released by
    the commit or the rollback)."""
    from sqlalchemy import text

    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"entity-aliases:{tenant_id}"})
    A = AgentEntityAlias
    if db.query(A.id).filter(mem.alias, A.alias_lower == (alias or "").strip().lower()).first():
        return
    held = db.query(A).filter(A.tenant_id == tenant_id, A.source == "user").count()
    if held >= MAX_DECLARED_NAMES:
        db.rollback()
        raise TooManyNames(f"this account has declared {held} names, the most it may have is "
                           f"{MAX_DECLARED_NAMES}: delete one first")


def _memory_key(T):
    """The memory (project, end user, agent namespace) of a row, as a key that
    is never NULL, so rows compare with `=` and a set: a NULL becomes the nil
    UUID or "", which no id can be (an end user or agent id has at least one
    character, `ids`)."""
    nil = cast(literal("00000000-0000-0000-0000-000000000000"), PG_UUID(as_uuid=True))
    return (func.coalesce(T.project_id, nil), func.coalesce(T.end_user_id, ""),
            func.coalesce(T.agent_id, ""))


def declare_alias(db: Session, tenant_id: UUID, project_id: Optional[UUID],
                   end_user_id: Optional[str], agent_id: Optional[str], alias: str, canonical: str):
    """`alias` is `canonical` in one memory, by somebody's declaration (source
    "user"): the dashboard's alias (`POST /admin/aliases`) and an end user's
    declared name (`POST /v1/users/{id}/names`), by the one rule every alias
    obeys (`store.add_alias`: no cycle, the chain flattened, the
    memory's facts recanonicalized). Returns (row, outcome) as that rule.

    An account holds at most MAX_DECLARED_NAMES declared names: a name read
    walks a memory's aliases, and a key that declared one per end user made
    every read of the account heavier (audit of 27 Oct 2026). Past it, TooManyNames;
    a name already declared in the memory answers as before."""
    _room_for_a_name(db, memory_of(tenant_id, project_id, end_user_id, agent_id), tenant_id, alias)
    row, outcome = _store.add_alias(db, TABLES,
                                      memory_of(tenant_id, project_id, end_user_id, agent_id),
                                      alias, canonical, source="user")
    if row is not None and row.project_not_recorded:
        # Declared again in its project: it is that project's now, no
        # longer a name the upgrade could not place (e7c4b2d9a6f1).
        row.project_not_recorded = False
    return row, outcome


def predicate_form(predicate: str) -> str:
    """The form a write stores a phrase under (`predicate_forms.canonicalize_predicate`)."""
    from ..memory_core.predicate_forms import canonicalize_predicate

    return canonicalize_predicate(predicate)


def predicate_condition(tenant_id: UUID, predicate: str):
    """SQL on AgentFact: `subscribes_to` finds what is stored as `on_plan` once
    the vocabulary learned they are one relation. A fact matches when it is
    stored under the phrase's own form, or under a relation that the phrase
    names in the vocabulary of THAT fact's end user and project (one per end
    user since 18 Oct 2026): a read of several end users resolves the phrase
    in each one's vocabulary, never one end user's learning applied to
    another's facts."""
    surface = predicate_form(predicate)
    F, C, V = AgentFact, PredicateCluster, PredicateAlias
    in_its_vocabulary = (
        select(C.id).join(V, V.cluster_id == C.id)
        .where(C.tenant_id == tenant_id, V.surface == surface,
               C.canonical == F.predicate_canonical,
               C.project_id.is_not_distinct_from(F.project_id),
               C.end_user_id.is_not_distinct_from(F.end_user_id))
        .correlate(F).exists()
    )
    return or_(F.predicate_canonical == surface, in_its_vocabulary)
