"""The reads of the facts: the read shape of one fact, the facts of one
memory and its history, and the deterministic retrieval of GET /v1/facts and
the MCP's korely_get_facts (no model call).

Out of service.py since the audit of 17 Oct 2026 (with the drain, that file
was 1,651 lines). Nothing here writes; service.py imports it and serves its
names as before (`facts.service.retrieve_agent_facts`, `facts.serialize_fact`).
Every read takes `project_id` by name with no default (service.py says why).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...db.models import AgentFact
from .. import user_scope
from ..ids import fact_public_id, mem_public_id
from ..limits import MAX_FACT_NAME_CHARS, MAX_PAGE
from ..memory_core.families import FAMILY_ORDER, family_of_the_fact
# Sentinel: "no end_user filter" (all of the account's facts), distinct from
# end_user_id=None which means "the shared / bot-level facts".
# One sentinel for the whole codebase. See services/sentinels.py for what
# the two separate ones cost.
from ..sentinels import EVERY_END_USER as _ALL
from ..sentinels import EndUsers, within_project
from .entities import facts_in_project
from .temporal import end_known_by, known_at, known_true_at, true_at

def quoted(names) -> str:
    """Names as a description lists them: `a`, `b`."""
    return ", ".join(f"`{n}`" for n in names)


#: The `predicate_family` filter as every door's document says it (GET
#: /v1/facts, GET /admin/facts, MCP korely_get_facts): REST, the admin and
#: MCP validated the filter by one rule (api/facts.names_problem) and named
#: the families in none (review of 3 Nov 2026). The families are read from
#: FAMILY_ORDER, the list `retrieve_agent_facts` below checks a name against,
#: and the bound from the one limits.name_problem holds a name to; the rest
#: is that function's and names_problem's rule in words.
#: tests/test_openapi_fact_fields.py reads the matching clauses back from
#: `retrieve_agent_facts`, which every door calls, and
#: tests/test_every_read_input_by_the_rule.py the refusals on each door.
FAMILY_FILTER = (f"Filter to one family: one of {quoted(FAMILY_ORDER)} (case and surrounding "
                 "spaces ignored). A name that is not a family matches no fact: an empty "
                 "result, not an error. An empty value is no filter; one made only of "
                 "spaces is no family, and matches no fact. A name longer than "
                 f"{MAX_FACT_NAME_CHARS} characters, or with a NUL byte, is refused as an "
                 "invalid request (a 422 on REST), as every name a read of the facts narrows on.")


def serialize_fact(fact: AgentFact, known_as_of: Optional[datetime] = None) -> dict:
    """Full read shape for GET /v1/facts and GET /v1/memories/:id.

    `known_as_of`: the fact as the store knew it then (11 Oct 2026). An end
    the store learned afterwards is not shown: that day it was open.
    `closed_known_at` says when the end shown was learned. Its sources, their
    count and its last confirmation are the ones of then too (`_as_known`)."""
    ended = fact.invalid_at is not None and (known_as_of is None
                                             or end_known_by(fact, known_as_of))
    learned_end = (fact.closed_known_at or fact.created_at) if ended else None
    sources = [u for u in (_to_uuid(m) for m in (fact.source_memory_ids or [])) if u]
    confirmed = fact.last_confirmed_at or fact.valid_from
    count = int(fact.observation_count or 1)
    if known_as_of is not None:
        sources, count, confirmed = _as_known(fact, sources, count, confirmed, known_as_of)
    # memory_core 6.1.0: a link "the same as" shows two objects of a memory
    # under one name; each fact keeps its own words (facts/links.py).
    from sqlalchemy.orm import object_session

    from .links import resolve

    names = resolve(object_session(fact), fact, known_as_of)
    return {
        "id": fact_public_id(fact.id),
        "subject": fact.subject_label,
        "subject_type": fact.subject_type,
        "predicate": fact.predicate_canonical,
        "predicate_raw": fact.predicate_raw,
        "object": fact.object_label,
        "object_is_literal": fact.object_is_literal,
        "predicate_family": family_of_the_fact(fact.predicate_family, fact.predicate_canonical),
        "confidence": round(float(fact.confidence), 3) if fact.confidence is not None else None,
        "user_id": fact.end_user_id,
        "agent_id": fact.agent_id,
        "valid_from": fact.valid_from.isoformat() if fact.valid_from else None,
        "invalid_at": fact.invalid_at.isoformat() if ended else None,
        "invalidated_by": (fact_public_id(fact.invalidated_by_id)
                           if ended and fact.invalidated_by_id else None),
        "source_memory_id": mem_public_id(fact.source_memory_id) if fact.source_memory_id else None,
        "created_at": fact.created_at.isoformat() if fact.created_at else None,
        # 2026-10-11 — additive: when the store learned the fact had ended
        # (`created_at` is when it learned the fact). Null while it is open.
        "closed_known_at": learned_end.isoformat() if learned_end else None,
        # 2026-09-27 — additive; the SDKs ignore unknown keys. `subject_canonical`
        # is the entity's current name after aliases ("Globex" for a fact
        # written as "Acme"); `subject` keeps history.
        # 6.1.0: after a link too, `subject_link_id` / `object_link_id` the
        # link that resolved them (DELETE /admin/object-links/{id}).
        "subject_canonical": names["subject_canonical"],
        "object_canonical": names["object_canonical"],
        "subject_link_id": names["subject_link_id"],
        "object_link_id": names["object_link_id"],
        "tense": fact.tense or "current",
        "last_confirmed_at": confirmed.isoformat() if confirmed else None,
        "observation_count": count,
        "source_memory_ids": [mem_public_id(u) for u in sources],
    }


def _as_known(fact: AgentFact, sources: List[UUID], count: int,
              confirmed: Optional[datetime], known: datetime):
    """(source memories, observation count, last confirmation) of `fact` as
    the store knew them at `known` (audit of 18 Oct 2026). A reconfirmation
    changes the row in place (memory_core `store.reconfirm`) and keeps
    no history, so until 18 Oct 2026 a read with `known_as_of` showed today's
    three: counts and memory ids the store did not have then, and a
    confirmation dated after the moment asked.

    What can be told from the store: a memory written after `known` had
    not said anything by then, so it is no source then; the count is the
    sources left, plus the restatements that came with no memory (a triple
    posted directly), which carry no date and are counted as they are now;
    the last confirmation is never after `known`, nor after the latest
    saying of a source left (when the memory says it happened, else when it
    was written: the date `writing.write_fact` gives a confirmation).
    What cannot: a source a later forget removed from the fact (its id is no
    longer on the row) is not shown."""
    from sqlalchemy.orm import object_session

    from ...core.times import aware
    from ...db.models import AgentMemory

    known = aware(known)
    db = object_session(fact)
    if sources and db is not None:
        said = {mid: aware(ev or made) for mid, ev, made in db.query(
            AgentMemory.id, AgentMemory.event_time, AgentMemory.created_at).filter(
                AgentMemory.tenant_id == fact.tenant_id, AgentMemory.id.in_(sources),
                AgentMemory.created_at <= known)}
        unsourced = max(0, count - len(sources))
        sources = [u for u in sources if u in said]
        count = max(1, len(sources) + unsourced)
        if said and confirmed is not None:
            confirmed = min(aware(confirmed), max(said.values()))
    if confirmed is not None:
        confirmed = min(aware(confirmed), known)
    return sources, count, confirmed


def _to_uuid(v):
    try:
        return v if isinstance(v, UUID) else UUID(str(v))
    except (TypeError, ValueError):
        return None


def load_fact(db: Session, tenant_id: UUID, fact_uuid: UUID,
              *, project_id: Any) -> Optional[AgentFact]:
    """One fact, or None: the fact a forget (erasure.forget_fact) or a
    correction (service.correct_fact) acts on, in service.py until the
    1.0.27 gate.

    `project_id` has no default (audit of 17 Oct 2026, as every read and
    erasure of the facts): leaving it out meant "every project in this
    account", which is only ever right for the dashboard, which administers
    the account, and says so with EVERY_PROJECT. Anything reached by an API
    key passes one, because a key is confined to one project and a key that
    can close somebody else's fact is the wall with a door in it.

    The sentinel exists so that `project_id=None`, which is a real value meaning
    "the unfiled project", cannot be confused with "did not say". It is the
    one of services/sentinels.py, as everywhere else: service.py kept a
    second one of its own until 2026-10-06, and the shared one passed here
    reached the database as a value ("can't adapt type '_EveryProject'", the
    audit of 6 October, row 16).
    """
    q = db.query(AgentFact).filter(
        AgentFact.tenant_id == tenant_id, AgentFact.id == fact_uuid)
    return within_project(q, AgentFact.project_id, project_id).first()


def facts_for_memory(db: Session, tenant_id: UUID, memory_uuid: UUID,
                     *, project_id: Any, true_now: bool = False) -> List[AgentFact]:
    """The facts one memory asserted, newest first: born from it, or
    reconfirmed by it (2026-09-27, `source_memory_ids`). All of them, active
    and superseded, which is what its history shows; with `true_now` only the
    ones true today (`true_at`: not closed, or closed on a future date)."""
    from .entities import TABLES
    from ..memory_core.store import source_condition

    q = db.query(AgentFact).filter(AgentFact.tenant_id == tenant_id,
                                   source_condition(TABLES, memory_uuid))
    if true_now:
        q = q.filter(true_at())
    rows = facts_in_project(q, project_id).order_by(AgentFact.valid_from.desc(), AgentFact.id).all()
    from .links import prefetch

    prefetch(db, rows)   # 6.1.0: the links, once, for `serialize_fact`
    return rows


def retrieve_agent_facts(
    db: Session,
    tenant_id: UUID,
    *,
    end_user_id: Any = _ALL,
    agent_id: Optional[str] = None,
    subject: Optional[str] = None,
    entity: Optional[str] = None,
    predicate: Optional[str] = None,
    predicate_family: Optional[str] = None,
    include_invalidated: bool = False,
    as_of: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
    project_id: Any,
    known_as_of: Optional[datetime] = None,
    exclude_sessions: bool = False,
) -> tuple[List[AgentFact], int]:
    """Deterministic fact retrieval (no model call). Filters are AND-combined.

    `as_of` returns point-in-time validity (what was true then) and overrides
    include_invalidated. Otherwise active facts only unless include_invalidated.

    `known_as_of` (11 Oct 2026): as the store knew it then. Only the facts
    learned by then, each with the ends learned by then, true at `as_of`, or
    at `known_as_of` itself when no `as_of` is given (what the store believed
    that day about that day). With include_invalidated and no `as_of`: every
    fact learned by then, ended or not (temporal.py has the two clocks).
    """
    limit = max(1, min(int(limit or 50), MAX_PAGE))
    offset = max(0, int(offset or 0))

    q = db.query(AgentFact).filter(AgentFact.tenant_id == tenant_id)
    q = facts_in_project(q, project_id)

    # The one end-user wall (services/user_scope.within_end_users): _ALL is
    # every end user, None the rows of none, EndUsers a confined key's list.
    q = user_scope.within_end_users(q, AgentFact.end_user_id, end_user_id)
    if agent_id is not None:
        q = q.filter(AgentFact.agent_id == agent_id)
    if exclude_sessions:
        # Only the facts some note states (AgentFact.session_only, 18 Oct 2026).
        q = q.filter(AgentFact.session_only.is_(False))

    # Temporal scope.
    if known_as_of is not None:
        q = q.filter(known_at(known_as_of))
        if as_of is not None or not include_invalidated:
            q = q.filter(known_true_at(known_as_of, as_of))
    elif as_of is not None:
        q = q.filter(true_at(as_of))
    elif not include_invalidated:
        # "Now" is a moment like any other. It used to be written as "has no end
        # date", which is a different question and gave a different answer the
        # moment an end date could be in the future.
        q = q.filter(true_at())

    # 2026-09-27: subject and entity on the CANONICAL name after the aliases
    # ("Acme" finds the facts about Globex once Acme is known to be Globex);
    # equality on lower(), never LIKE, so `%` in a name is a character.
    from sqlalchemy import or_
    from . import entities as _ent
    from ..memory_core.store import family_filter

    _SC = func.lower(func.coalesce(AgentFact.subject_canonical, AgentFact.subject_label))
    _OC = func.lower(func.coalesce(AgentFact.object_canonical, AgentFact.object_label))
    # Each fact's name is read in the fact's own memory (one map for every
    # end user gave one end user's facts): `entities.name_condition`.
    # A confined read resolves names over its project, like a read of every
    # end user: the rows are already only its end users' (the filter above).
    every = end_user_id is _ALL or isinstance(end_user_id, EndUsers)
    alias_filters = dict(
        project_id=project_id,
        end_user_id=None if every else end_user_id,
        all_end_users=every,
        agent_id=agent_id,
    )
    # memory_core 6.1.0: and every name a link of the fact's memory reads as
    # the one asked (facts/links.py).
    from .links import names_condition, prefetch

    # The links as the store knew them at `known_as_of`, as the names the
    # same response shows (`serialize_fact`, `prefetch` below).
    linked = dict(alias_filters, known_as_of=known_as_of)
    if subject:
        q = q.filter(or_(_ent.name_condition(db, tenant_id, subject, _SC, **alias_filters),
                         names_condition(db, tenant_id, subject, _SC, **linked)))
    if entity:
        q = q.filter(or_(_ent.name_condition(db, tenant_id, entity, _SC, **alias_filters),
                         _ent.name_condition(db, tenant_id, entity, _OC, **alias_filters),
                         names_condition(db, tenant_id, entity, _SC, **linked),
                         names_condition(db, tenant_id, entity, _OC, **linked)))
    if predicate:
        q = q.filter(_ent.predicate_condition(tenant_id, predicate))
    if predicate_family:
        fam = predicate_family.strip().lower()
        if fam not in FAMILY_ORDER:
            return [], 0  # unknown family → empty
        q = q.filter(family_filter(AgentFact, fam))

    total = q.count()
    if as_of is not None or known_as_of is not None:
        order = (AgentFact.valid_from.desc(), AgentFact.id)
    else:
        # Now: the freshest confirmation first.
        order = (
            func.coalesce(AgentFact.last_confirmed_at, AgentFact.valid_from).desc(),
            AgentFact.valid_from.desc(),
            AgentFact.id,
        )
    rows = (
        q.order_by(*order)
        .offset(offset)
        .limit(limit)
        .all()
    )
    # The links of the page's memories, read once for `serialize_fact`.
    prefetch(db, rows, known_as_of)
    return rows, total


def memory_timeline(db: Session, tenant_id: UUID, memory,
                    *, project_id: Any) -> tuple[List[dict], List[AgentFact]]:
    """What one memory taught the store, in order, and the facts it names (a
    read records which, for the audit trail).

    Lives here rather than in a router because two doors ask for it: `/v1` for
    a program, behind its key's project, and `/admin` for a person. The admin
    one was a second implementation returning a different shape, and the
    screen that consumed it crashed on the difference; then `/v1` kept a copy
    of its own until 2026-10-06 (the code audit, row 15).
    """
    from ..ids import fact_public_id

    events = [{"event": "created", "at": memory.created_at.isoformat()
               if memory.created_at else None}]
    if memory.updated_at and memory.created_at and memory.updated_at != memory.created_at:
        events.append({"event": "updated", "at": memory.updated_at.isoformat()})
    # Dated by the store's clock, when it learned (CONTRIBUTING.md, two clocks):
    # until 18 Oct 2026 by the world's, `valid_from` and `invalid_at`, so a
    # memory written today about 2020 showed its fact extracted six years
    # before the memory, and an end set ahead showed as an event still to
    # come (audit of 18 Oct 2026). A fact is learned from this memory no
    # earlier than the memory was written (one it restated was learned
    # before, from another); its end when the store learned it
    # (`closed_known_at`, else as serialize_fact reads it), never before the
    # fact itself was learned.
    from ...core.times import aware

    written = aware(memory.created_at)
    facts = facts_for_memory(db, tenant_id, memory.id, project_id=project_id)
    for f in facts:
        learned = max((t for t in (aware(f.created_at), written) if t is not None), default=None)
        line = f"{f.subject_label} {f.predicate_canonical} {f.object_label}"
        events.append({
            "event": "fact_extracted",
            "at": learned.isoformat() if learned else None,
            "fact": line,
            "fact_id": fact_public_id(f.id),
        })
        if f.invalid_at:
            ended = max((t for t in (aware(f.closed_known_at), aware(f.created_at))
                         if t is not None), default=None)
            # The closed fact is this memory's own, and is named as the
            # extraction is: the event carried its id only, so the screen
            # read "Superseded a fact" with no fact under it, as if this
            # memory had superseded another (functional test of 28 Oct 2026).
            events.append({"event": "fact_invalidated",
                           "at": ended.isoformat() if ended else None,
                           "fact": line,
                           "fact_id": fact_public_id(f.id)})
    if memory.deleted_at:
        events.append({"event": "deleted", "at": memory.deleted_at.isoformat()})
    # Chronological; anything without a timestamp sorts last.
    events.sort(key=lambda e: e["at"] or "9999-12-31")
    return events, facts
