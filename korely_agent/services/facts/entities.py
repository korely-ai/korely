"""Entity aliases, reconfirmation and memory scope for the agent facts (2026-09-27).

The rules are written once in `services/memory_core/`, the facts engine
(shared with an earlier product until 11 Oct 2026, only here since).
This module only says which tables and what a memory is on a self-hosted
install: tenant + project + end user + agent namespace, the key facts were
already deduplicated on.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy import and_, cast, func, literal, or_, select, tuple_
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from ...db.models import (AgentEntityAlias, AgentFact, AgentFactClosureLink, AgentMemory, AgentObjectLink,
                          PredicateAlias, PredicateCluster)
from .. import vector_state
from ..memory_core import store as _store
from ..sentinels import EVERY_PROJECT, within_project

logger = logging.getLogger(__name__)

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
    # memory_core 6.20.0 (`objects.review_subjects`, off): the closings the
    # judge made only through a link between two subjects, given back when
    # the link goes (`objects.remove_link`, facts/subjects.remove).
    ClosureLink=AgentFactClosureLink,
)

#: The scope of a link (`AgentObjectLink.scope`, memory_core 6.20.0): two
#: objects of one memory, read by its reads ...
MEMORY_LINKS = "memory"
#: ... or two subjects of a memory with no end user, in the judge's wall
#: (`wall_of`), read by the judge only (facts/subjects.py).
WALL_LINKS = "wall"


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


def kept_to_their_subject():
    """The facts of a memory with no end user, as a condition on the facts
    table (memory_core 6.9.0): `memory_core.owner.keeps_to_its_subject_rows`
    on this install's column. The judge weighs such a fact, by meaning,
    against the facts of its own subject only, and weighs it against another
    fact by meaning only about that fact's subject (`candidates.for_the_judge(
    agent_scoped=, agent_scoped_facts=)`, the names of 6.7.0, which narrowed an
    agent's memory alone, `scoped_by_an_agent`)."""
    from ..memory_core.owner import keeps_to_its_subject_rows

    return keeps_to_its_subject_rows(AgentFact.end_user_id)


def scoped_by_an_agent():
    """The facts of an agent's memory with no end user, as a condition on the
    facts table (memory_core 6.7.0): `memory_core.owner.agent_scoped_rows`,
    the rule the extraction frames a memory by (`owner.agent_scoped`, a blank
    `agent_id` being no agent), on this install's columns. The changes
    section reads it (`changes.py`). The judge narrowed its candidates by it
    from 6.7.0 to 6.8.1; since 6.9.0 by `kept_to_their_subject`, every
    memory with no end user."""
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
            # 6.20.0: a memory's links, never the wall's links of its subjects.
            L.scope == MEMORY_LINKS,
        ),
        relations=vocab.relations,
        variants=vocab.variants,
        new_relation=vocab.new_relation,
        # memory_core 6.25.0: an agent's memory of an end user reads the
        # names declared for that end user in their own memory.
        person_names=(persons_names(tenant_id, project_id, end_user_id)
                      if end_user_id is not None and agent_id is not None else None),
    )


#: The `source` of an alias somebody declared (`declare_alias`: the end
#: user's name, `POST /v1/users/{id}/names`, and the dashboard's alias,
#: `POST /admin/aliases`), as against one the engine learned from a memory
#: ("Acme is now called Globex", `identity.SOURCE_*`).
DECLARED = "user"


def persons_names(tenant_id: UUID, project_id: Optional[UUID], end_user_id: str):
    """The names declared for an end user, as a condition on the alias table
    (memory_core 6.25.0, `store.Memory.person_names`): the rows somebody
    declared (`DECLARED`) in the end user's own memory (no agent) whose
    canonical is the end user's id, the rows `POST /v1/users/{id}/names`
    writes (memory_core 6.24.0) and a dashboard alias to the id. Only
    declared rows: a rename the engine learned from a memory is that memory's
    own, and its facts are not followed elsewhere (the review of 25 Nov 2026).
    Found on 31 Oct 2026, on the Cloud's 1.0.0: two agents of one end user wrote
    "Marta ... penicillin" under the user_id and under "Marta", two open
    facts, because a declared name held only in the memory it was declared in."""
    A = AgentEntityAlias
    return and_(A.tenant_id == tenant_id, _equal(A.project_id, project_id),
                A.end_user_id == end_user_id, A.agent_id.is_(None),
                A.canonical_label == end_user_id, A.source == DECLARED)


def declared_person(db: Session, tenant_id: UUID, project_id: Optional[UUID],
                    end_user_id: Optional[str]) -> Optional[str]:
    """The end user's id when a name was declared for them in this project
    (a declared row whose canonical is the id, in any agent namespace of
    theirs), else None: what the judge's candidates take as `the_person`
    (memory_core 6.25.0, `candidates.about_the_subject`; off,
    contradiction._DECLARED_PERSON). One indexed read."""
    if end_user_id is None:
        return None
    A = AgentEntityAlias
    hit = (db.query(A.id).filter(A.tenant_id == tenant_id, _equal(A.project_id, project_id),
                                 A.end_user_id == end_user_id, A.canonical_label == end_user_id,
                                 A.source == DECLARED)
           .first())
    return end_user_id if hit is not None else None


def is_a_persons_name(row: Any) -> bool:
    """Whether an alias row is one of `persons_names` (read on the row as
    it is: a caller that changes the row asks before and after)."""
    return (row is not None and row.agent_id is None and row.end_user_id is not None
            and row.canonical_label == row.end_user_id and row.source == DECLARED)


def agents_of_the_person(db: Session, tenant_id: UUID, project_id: Optional[UUID],
                         end_user_id: str) -> List[str]:
    """The agent namespaces that hold facts of an end user in a project:
    the memories whose names change when a name of the person is declared
    or removed. At most `_AGENTS_MAX`, the first by id; past it a warning
    with the counts only, and the others keep their names until their next
    write (never a silent cut)."""
    rows = (db.query(AgentFact.agent_id).filter(
                AgentFact.tenant_id == tenant_id, _equal(AgentFact.project_id, project_id),
                AgentFact.end_user_id == end_user_id, AgentFact.agent_id.isnot(None))
            .distinct().order_by(AgentFact.agent_id).limit(_AGENTS_MAX + 1).all())
    agents = [r[0] for r in rows]
    if len(agents) > _AGENTS_MAX:
        logger.warning("[entities] a name of an end user changed and more than %d agent namespaces "
                       "hold their facts: the first %d follow it now, the others at their next write",
                       _AGENTS_MAX, _AGENTS_MAX)
        agents = agents[:_AGENTS_MAX]
    return agents


#: How many agent namespaces of one end user a name's declaration recanonicalizes.
_AGENTS_MAX = 10_000


def after_a_persons_name(db: Session, tenant_id: UUID, project_id: Optional[UUID], end_user_id: str) -> int:
    """After a name of a person was declared or removed (`is_a_persons_name`):
    the facts of every agent namespace of that person take the names the
    map now gives (`store.recanonicalize`, as the person's own memory's
    facts already did). Returns how many memories were read."""
    agents = agents_of_the_person(db, tenant_id, project_id, end_user_id)
    for agent in agents:
        _store.recanonicalize(db, TABLES, memory_of(tenant_id, project_id, end_user_id, agent))
    return len(agents)


def wall_of(tenant_id: UUID, project_id: Optional[UUID]) -> _store.Memory:
    """The judge's wall of a memory with no end user, as one memory of the
    engine (memory_core 6.20.0, `objects.review_subjects`): the facts of the
    account and the project with no end user, of every agent (`memory_scope`,
    the candidates of the judge), the aliases there, and the links of scope
    `WALL_LINKS` between their subjects, written with no end user and no
    agent. A link of the wall is read by the judge only (facts/subjects.py):
    the reads of a memory never follow it (`memory_of` leaves it out)."""
    from ..predicates import end_user_vocabulary

    vocab = end_user_vocabulary(tenant_id, project_id, None)
    A, L = AgentEntityAlias, AgentObjectLink
    return _store.Memory(
        facts=memory_scope(tenant_id, project_id, None),
        alias=and_(A.tenant_id == tenant_id, _equal(A.project_id, project_id), A.end_user_id.is_(None)),
        new_alias=dict(tenant_id=tenant_id, project_id=project_id, end_user_id=None, agent_id=None),
        links=and_(L.tenant_id == tenant_id, _equal(L.project_id, project_id), L.end_user_id.is_(None),
                   L.agent_id.is_(None), L.scope == WALL_LINKS),
        new_link={"scope": WALL_LINKS},
        relations=vocab.relations,
        variants=vocab.variants,
        new_relation=vocab.new_relation,
    )


def memory_of_link(tenant_id: UUID, project_id: Optional[UUID], row: Any) -> _store.Memory:
    """The memory a link row belongs to, by its scope, in `project_id` (a
    project's links moving into another, services/projects.py)."""
    if getattr(row, "scope", MEMORY_LINKS) == WALL_LINKS:
        return wall_of(tenant_id, project_id)
    return memory_of(tenant_id, project_id, row.end_user_id, row.agent_id)


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
    # memory_core 6.21.0: an alias row's key is `identity.name_key`, or the
    # alias lowercased for a row written before (`store.alias_keys`).
    scope = [A.tenant_id == tenant_id, _store.alias_key_condition(A.alias_lower, name)]
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
    # memory_core 6.25.0: a name declared for a person in their own memory
    # (`persons_names`) is the person in each agent namespace of theirs too,
    # unless that agent's memory holds the name itself (its own alias wins, as
    # in `store.memory_alias_map`).
    person = [A.tenant_id == tenant_id, _store.alias_key_condition(A.alias_lower, name),
              A.agent_id.is_(None), A.end_user_id.isnot(None), A.canonical_label == A.end_user_id,
              A.source == DECLARED]
    if project_id is not EVERY_PROJECT:
        person.append(_equal(A.project_id, project_id))
    if not all_end_users:
        person.append(_equal(A.end_user_id, end_user_id))
    the_person = select(_memory_key(A)[0], A.end_user_id, its_own).where(*person)
    agents_own = select(*_memory_key(A)).where(*scope, A.agent_id.isnot(None))
    in_an_agent = and_(F.agent_id.isnot(None),
                       tuple_(_memory_key(F)[0], F.end_user_id, column).in_(the_person),
                       tuple_(*_memory_key(F)).not_in(agents_own))
    return or_(and_(column == key, tuple_(*_memory_key(F)).not_in(elsewhere)),
               tuple_(*_memory_key(F), column).in_(as_its_own),
               in_an_agent)


#: Names an account may have declared (`source` "user": the dashboard's
#: alias and an end user's declared name), like keys.MAX_API_KEYS a count per
#: account. Delete one to make room (`DELETE /admin/aliases/{id}`).
MAX_DECLARED_NAMES = 1000


class TooManyNames(ValueError):
    """The account holds MAX_DECLARED_NAMES declared names."""


def _room_for_a_name(db: Session, mem, tenant_id: UUID, alias: str) -> None:
    """Refuse a NEW declared name past the cap: one declaration of the
    account at a time from the count to the commit (services/caps). A name
    the memory has already is no new one."""
    from .. import caps

    A = AgentEntityAlias

    def held():
        if db.query(A.id).filter(mem.alias, _store.alias_key_condition(A.alias_lower, alias)).first():
            return None
        return db.query(A).filter(A.tenant_id == tenant_id, A.source == "user").count()

    caps.room_for_one_more(
        db, "entity-aliases", tenant_id, held, MAX_DECLARED_NAMES,
        lambda n: TooManyNames(f"this account has declared {n} names, the most it may have is "
                               f"{MAX_DECLARED_NAMES}: delete one first"))


def _memory_key(T):
    """The memory (project, end user, agent namespace) of a row, as a key that
    is never NULL, so rows compare with `=` and a set: a NULL becomes the nil
    UUID or "", which no id can be (an end user or agent id has at least one
    character, `ids`)."""
    nil = cast(literal("00000000-0000-0000-0000-000000000000"), PG_UUID(as_uuid=True))
    return (func.coalesce(T.project_id, nil), func.coalesce(T.end_user_id, ""),
            func.coalesce(T.agent_id, ""))


def declare_alias(db: Session, tenant_id: UUID, project_id: Optional[UUID],
                   end_user_id: Optional[str], agent_id: Optional[str], alias: str, canonical: str,
                   *, canonical_is_an_end_user: bool = False):
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
    # memory_core 6.25.0: the person's names before the change, read before
    # the row is touched: a name declared for the person and pointed by hand
    # at somebody else is no longer theirs, and the agents' facts must leave
    # it too (the review of 25 Nov 2026); a chain the change flattens can
    # make another row a name of the person.
    before = names_of_the_person(db, tenant_id, project_id, end_user_id, agent_id)
    row, outcome = _store.add_alias(db, TABLES,
                                      memory_of(tenant_id, project_id, end_user_id, agent_id),
                                      alias, canonical, source="user",
                                      # memory_core 6.24.0: a declared name's canonical is
                                      # the end user's id, read as an id, not as a name.
                                      canonical_is_an_end_user=canonical_is_an_end_user)
    if row is not None and row.project_not_recorded:
        # Declared again in its project: it is that project's now, no
        # longer a name the upgrade could not place (e7c4b2d9a6f1).
        row.project_not_recorded = False
    if outcome in _store.ALIAS_WRITTEN:
        # memory_core 6.25.0: a name of the person holds in every agent
        # namespace of that person, whose facts follow it now.
        db.flush()
        if names_of_the_person(db, tenant_id, project_id, end_user_id, agent_id) != before:
            after_a_persons_name(db, tenant_id, project_id, end_user_id)
    return row, outcome


def names_of_the_person(db: Session, tenant_id: UUID, project_id: Optional[UUID],
                        end_user_id: Optional[str], agent_id: Optional[str]) -> Optional[frozenset]:
    """The keys of the person's declared names (`persons_names`), for a
    change made in the person's own memory; None for any other memory, whose
    aliases no other memory reads. Bounded by MAX_DECLARED_NAMES."""
    if end_user_id is None or agent_id is not None:
        return None
    A = AgentEntityAlias
    rows = db.query(A.alias_lower).filter(persons_names(tenant_id, project_id, end_user_id)).all()
    return frozenset(_store.alias_keys(r[0])[0] for r in rows)


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
