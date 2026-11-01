"""One thing named in two languages or forms: the product's half (memory_core 6.1.0 `objects`).

"lives in Italia" and "lives in Italy" were two objects: the extraction keeps
the memory's own words, by design. With the switch on, a write's new objects
are offered, before its facts are written, to the writer of the project's
region with the objects of the same memory they read alike to (open facts,
the same relation family, never people), each with its context (the fact and
the memory's words). The writer describes each, then answers twice, framed
two ways; two "same" write a LINK (`agent_object_links`), never an alias:
no fact is rewritten, merged or reconfirmed, the reads show the two objects
as one (facts/links.py), and DELETE /admin/object-links/{id} takes it back,
leaving the memory exactly as before. The rules and their papers are in
memory_core/objects.py.

What this module gives the engine: the memory (`entities.memory_of`), the
model (`predicates.writers_ask`: the vocabulary's model, the writer of the
region the write runs in, `regions.writing_for`; never the other region's),
the vector (`service.fact_vector` of the fact's text, the space the stored
facts' vectors are in), the words of the memories that stated the candidates
(never a forgotten memory's), what "open" means (`temporal.true_at`), a lock
per memory (an advisory lock of the transaction) and the id of the memory
being written, which the link keeps so that erasing that memory takes it
back (erasure.py).

Only what the extraction read is asked: a fact written directly or a
correction names its own object, as it names its own relation (BUG-202,
`predicates.resolve_relation(merge=False)`).

SAME_OBJECT, off: a switch of the engine off by default goes on only after a
measurement (CONTRIBUTING.md); the condition set on 30 Oct
2026 is 0 wrong unions on at least 100 hard negatives on both regions'
writers. Measured on 30 Oct 2026 on scripts/bench_same_object.json with
scripts/measure_same_object.py (seven new personas without filler, 138 hard
negatives with no disambiguator in their own sentence, 42 positives stated in
another fact than the stored one, beside the 29 Oct items), Wilson 95%:

                                     Gemini 3.1 Flash-Lite   gpt-oss-120b (Scaleway)
    hard negatives, wrong unions     1/138 [0.1%, 4.0%]      0/138 [0.0%, 2.7%]
      company / parent / product     0/32                    0/32
      exonym                         0/7                     0/7
      New-World homonym              1/63                    0/63
      part of / contains             0/20                    0/20
      names that look alike          0/16                    0/16
    29 Oct negatives (with hints)    0/24                    1/24
    positives in another fact,
      missed                         1/42 [0.4%, 12.3%]      2/42 [1.3%, 15.8%]
    cost of the run (798 calls)      EUR 0.32                EUR 0.12

The wrong union left on Gemini is "Carthagène" (French, which names Cartagena
in Spain; Cartagena de Indias is "Carthagène des Indes") with a memory's
Cartagena de Indias, described as the Colombian city; on gpt-oss "Principality
of Monaco" with the 29 Oct Italian memory's "Monaco" (Munich, "Erasmus at the
Technische Universität"), the held object described as the principality.
128 of the 138 hard negatives had their false friend in the shortlist. Not 0
on both: off. Before the descriptions were asked in
two questions (`objects._described`) gpt-oss united three exonyms of Monaco
with Munich (3/7); with the describe prompt that spoke of "where it is"
rather than an encyclopedia's identification Gemini united AWS with Amazon.
Cost when on: two to four calls for a write that names a new object with
candidates, about EUR 0.0004 a call on Gemini and EUR 0.00015 on Scaleway, a
second each.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import text as _sql
from sqlalchemy.orm import Session

from ...db.models import AgentFact, AgentMemory
from ..memory_core import objects as _objects
from ..sentinels import EVERY_PROJECT, within_project

# The switch (a module-level constant the tests flip, never an environment
# variable: services/context_experiments.py says why).
SAME_OBJECT = False

_NOT_ASKED = ("direct", "correction")


def words_of(db: Session, tenant_id: UUID, fact_ids: Sequence[Any]) -> Dict[Any, str]:
    """The text of the memory each fact was read from, by fact id: one read,
    bounded by the ids (a write's shortlists), never a forgotten memory's
    text (its words are no longer the memory's)."""
    ids = [i for i in fact_ids if i is not None]
    if not ids:
        return {}
    rows = (db.query(AgentFact.id, AgentMemory.content)
              .join(AgentMemory, AgentMemory.id == AgentFact.source_memory_id)
              .filter(AgentFact.tenant_id == tenant_id, AgentFact.id.in_(ids),
                      AgentMemory.tenant_id == tenant_id, AgentMemory.deleted_at.is_(None))
              .limit(len(ids)).all())
    return {r.id: r.content for r in rows}


def mentions_of(db: Session, tenant_id: UUID, memory, extracted: Sequence, text: Optional[str]) -> list:
    """The objects a write names, each in its context: the fact (subject,
    relation as written, the relation's family) and the memory's words that
    state it (the extractor's `evidence`, else the memory's text around the
    name). A literal value is not a thing to unite. The family is the
    vocabulary's, resolved now as the write resolves it (the same call, whose
    answer is cached for the fact's own write)."""
    from ..memory_core.fact_time import effective_tense
    from ..predicates import resolve_relation

    out = []
    for ef in extracted:
        if getattr(ef, "object_is_literal", False) or not (ef.object or "").strip():
            continue
        rel = resolve_relation(db, tenant_id, ef.predicate_raw, object_label=ef.object or "",
                               tense=effective_tense(getattr(ef, "tense", "current")), merge=True,
                               project_id=memory.project_id, end_user_id=memory.end_user_id)
        words = getattr(ef, "evidence", None) or _objects.words_around(text, ef.object)
        out.append(_objects.Mention(name=ef.object, subject=ef.subject, relation=ef.predicate_raw,
                                    family=rel.family, words=words))
    return out


def lock_of(db: Session, tenant_id: UUID, project_id, end_user_id, agent_id) -> Callable[[], None]:
    """The lock of one memory's object links, held to the transaction's end:
    two writes of one end user at once (the parallel drain) read and link
    its new objects one after the other (the review's P3.10)."""
    key = f"object-links:{tenant_id}:{project_id}:{end_user_id}:{agent_id}"

    def lock() -> None:
        db.execute(_sql("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
    return lock


def uniter(db: Session, tenant_id: UUID, memory, *, model_name: Optional[str],
           text: Optional[str]) -> Optional[Callable[[Sequence], dict]]:
    """The `objects` hook of `worker.write_memory` for this memory's write,
    or None when the switch is off or the facts are not the extraction's."""
    if not SAME_OBJECT or model_name in _NOT_ASKED:
        return None

    def unite(extracted: Sequence) -> dict:
        from .. import regions
        from ..predicates import writers_ask
        from .entities import TABLES, memory_of
        from .service import fact_text, fact_vector
        from .temporal import true_at

        with regions.writing_for(db, tenant_id, memory.project_id):
            ask = writers_ask()
            mentions = mentions_of(db, tenant_id, memory, extracted, text)

            def embed(m):
                return fact_vector(fact_text(m.subject, m.subject, m.relation, m.name, m.name))

            return _objects.unite_new_objects(
                db, TABLES, memory_of(tenant_id, memory.project_id, memory.end_user_id, memory.agent_id),
                mentions, ask=ask, embed=embed,
                words_of=lambda ids: words_of(db, tenant_id, ids), is_open=true_at(),
                lock=lock_of(db, tenant_id, memory.project_id, memory.end_user_id, memory.agent_id),
                memory_id=memory.id, strict=True, severe=True,
            )
    return unite


# ---------------------------------------------------------------------------
# manage review-objects
# ---------------------------------------------------------------------------

def review(db: Session, tenant_id: UUID, *, project_id: Optional[UUID], end_user_id: Optional[str],
           agent_id: Optional[str], before_each: Optional[Callable[[], bool]] = None) -> dict:
    """`manage review-objects` for one memory (an end user's facts in one
    project and agent namespace): the objects it already holds, asked as a
    write would ask them (memory_core `objects.review_objects`), of the
    writer of the project's region (`predicates.asked_in`: RuntimeError when
    that region has none, nothing asked of the other), the cost rows under
    the operator's stage (`llm_usage.OPERATOR`). Writes nothing: the result's
    `proposals` are what `apply` writes."""
    from .. import llm_usage
    from ..predicates import asked_in
    from .entities import TABLES, memory_of
    from .temporal import true_at

    with asked_in(db, tenant_id, project_id, stage=llm_usage.OPERATOR) as ask:
        return _objects.review_objects(
            db, TABLES, memory_of(tenant_id, project_id, end_user_id, agent_id),
            ask=ask, words_of=lambda ids: words_of(db, tenant_id, ids), is_open=true_at(),
            before_each=before_each)


def proposal_record(tenant_id: UUID, project_id, end_user_id, agent_id, p: _objects.Proposal) -> dict:
    """A proposal as the dry run's file keeps it: the memory, the two names,
    the ids, the reason. The file holds an end user's names and the writer's
    descriptions of them: the operator's, to read and then delete."""
    s = lambda v: str(v) if v is not None else None  # noqa: E731
    return {"account_id": str(tenant_id), "project_id": s(project_id), "user_id": end_user_id,
            "agent_id": agent_id, "name": p.name, "same_as": p.canonical, "family": p.family,
            "source_memory_id": s(p.source_memory_id), "source_fact_id": s(p.source_fact_id),
            "canonical_memory_id": s(p.canonical_memory_id), "reason": p.reason}


def apply(db: Session, records: Sequence[dict]) -> List[dict]:
    """Write exactly the links of a dry run's `records` (`proposal_record`),
    each in its memory, while both names are still named by an open fact
    there (memory_core `objects.apply_proposals`); no model is called.
    Returns one result
    per record ({"record", "outcome", "link_id"}). The caller commits, then
    writes the trail (`audit_applied`)."""
    from .entities import TABLES, memory_of
    from .temporal import true_at

    def uuid_or_none(v):
        return UUID(v) if v else None

    out = []
    for r in records:
        tenant_id, project_id = UUID(r["account_id"]), uuid_or_none(r.get("project_id"))
        p = _objects.Proposal(name=r["name"], canonical=r["same_as"],
                              source_memory_id=uuid_or_none(r.get("source_memory_id")),
                              source_fact_id=uuid_or_none(r.get("source_fact_id")),
                              canonical_memory_id=uuid_or_none(r.get("canonical_memory_id")),
                              reason=r.get("reason") or {}, family=r.get("family") or "other")
        mem = memory_of(tenant_id, project_id, r.get("user_id"), r.get("agent_id"))
        [(_p, row, outcome)] = _objects.apply_proposals(db, TABLES, mem, [p], is_open=true_at())
        out.append({"record": r, "outcome": outcome, "link_id": str(row.id) if row is not None else None})
    return out


def audit_applied(db: Session, results: Sequence[dict]) -> int:
    """One row in the account's trail for each link `apply` wrote, after the
    commit (the trail is written in its own session: a row before the commit
    would stand for a link a rollback took back). Ids and labels only."""
    from .. import audit as audit_service

    n = 0
    for res in results:
        if not res.get("link_id"):
            continue
        r = res["record"]
        audit_service.record(db, UUID(r["account_id"]), audit_service.SETTING_CHANGE, actor="manage",
                             project_id=UUID(r["project_id"]) if r.get("project_id") else None,
                             end_user_id=r.get("user_id"), target_id=res["link_id"],
                             meta={"setting": "object_link", "op": "create", "by": "review-objects"})
        n += 1
    return n


def memories_with_objects(db: Session, tenant_id: UUID, *, project_id: Any = EVERY_PROJECT,
                          end_user_id: Optional[str] = None) -> list:
    """(project_id, end_user_id, agent_id) of every memory of the account
    whose facts have a vector, behind `project_id`, of `end_user_id` when
    given: the memories `review` can read. One grouped read, bounded by the
    memories an account can hold."""
    q = (db.query(AgentFact.project_id, AgentFact.end_user_id, AgentFact.agent_id)
           .filter(AgentFact.tenant_id == tenant_id, AgentFact.embedding.isnot(None)))
    q = within_project(q, AgentFact.project_id, project_id)
    if end_user_id is not None:
        q = q.filter(AgentFact.end_user_id == end_user_id)
    rows = (q.group_by(AgentFact.project_id, AgentFact.end_user_id, AgentFact.agent_id)
              .limit(_MEMORIES_MAX).all())
    return [tuple(r) for r in rows]


# How many memories of one account one run of `manage review-objects` reads.
_MEMORIES_MAX = 100_000
