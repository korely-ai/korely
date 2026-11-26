"""Which earlier facts the contradiction judge reads (memory_core, piece 2 of
the shared-code plan, 2026-09-28).

Until today the two Agents engines had this function twice, line for line:
`agent_facts/contradiction._stage1_candidates` in the Cloud and in
korely-agent. The copies had already diverged once in a way that mattered: the
semantic stage built its own chain of filters and left the project out, so a
fact written in project Beta closed a fact of project Alfa. It was fixed by
hand in both. Here it is written once; the engine passes only WHO may be a
candidate (`scope`, a SQL condition on its fact table), and every query below
goes through that one condition.

Recall here, precision in the judge (ConflictBank, Su et al. 2024): a
pre-filter that lets through anything plausibly about the same thing, and a
model that decides. A candidate kept costs one line in a prompt; a candidate
missed is a contradiction nobody ever notices.

The stages, in the order the judge reads them (most precise signal first):
  1a   same canonical subject, same relation or its opposite family;
  1c   same subject, a compatible object, another relation (BUG-111);
  1d   the same two entities with the relation the other way round;
  1a-bis  same relation family inside one end user, any subject surface
       (entity resolution within a bounded scope, Graphiti §2.2);
  1b   nearest facts by vector (pgvector only).

A subject named by its writer (6.5.0, `subject_named`): 1a, 1c and 1d as
they are, 1a-bis not run, 1b among the facts of the same subject only. A
candidate whose subject its writer named (6.5.0, `named`) enters 1a-bis and
1b of any new fact only about the same subject. A fact of an agent's memory
with no end user (6.7.0, `agent_scoped`, `agent_scoped_facts`): the same
rule, both ways; since 6.9.0 the engine passes the two for every memory
with no end user, an agent's or not (`owner.keeps_to_its_subject`).

Dependency: SQLAlchemy 2 only, like `store`.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence, Tuple

from sqlalchemy import and_, false, func, not_, or_

from . import log_safe as _log
from . import thresholds
from .store import (Tables, _pg, apart_from, as_before_the_write, outside_the_source,
                    without_the_same_source)
from .contradiction import arguments_swapped, compatible_objects, merge_candidates
# `cosine` lives in `vector_search` since 2.12.0; the copy of its name that stayed
# here for the code importing it from this module left with 4.0.0 (nobody did).
from .vector_search import nearest

logger = logging.getLogger(__name__)

# Stage 1b: cosine distance cutoff and cap. Recall-y by design, the judge is
# the precision gate (ConflictBank, Su et al. 2024). The cutoff depends on the
# embedding model (2.4.0): the one in use is `thresholds.current().semantic`.
# (SEMANTICA_MAX_DIST, Gemini's value kept for code that imported it, left
# with 5.2.4: nobody did.)
SEMANTIC_LIMIT = 10

# There was an object filter on Stage 1a here, in the self-hosted engine only:
# a structural candidate farther than 0.20 from the new fact was dropped before
# the judge (measured on five pairs, korely-agent cc02629, 2026-09-10). Measured
# again on 2026-09-28 (the shared-code plan, "the three differences"): a
# real replacement falls beyond it
# ("Globex uses Jira" -> "uses Linear", 0.217), unrelated objects under the
# same subject and relation span 0.125-0.233, so the cut separates nothing, and
# on the support-desk corpus it saved no question to the judge. Removed from
# both engines. A candidate kept costs one line in a prompt; a candidate
# dropped is a contradiction nobody ever notices.


class CandidatesUnread(RuntimeError):
    """A stage of the candidate search could not run (2.11.0): the write is
    not judged on the candidates the other stages found. Not "no candidates":
    a contradiction the judge never sees stays open next to the fact it
    contradicts, and nobody asks again (the same reason a failed judge is not
    a verdict, `judge.JudgementMissing`). The engine's write rolls back, and
    its failure handling decides what follows."""


def for_the_judge(
    db,
    tables: Tables,
    scope: Any,
    *,
    exclude_id: Any,
    subject_label: str,
    predicate_canonical: str,
    subject_canonical: Optional[str] = None,
    object_canonical: Optional[str] = None,
    is_open: Any = None,
    source: Any = None,
    with_recall: bool = False,
    vector=None,
    limit: int = 10,
    only_arguments: bool = False,
    subject_named: bool = False,
    named: Any = None,
    agent_scoped: bool = False,
    agent_scoped_facts: Any = None,
    subject_names: Optional[Sequence[str]] = None,
    the_person: Optional[str] = None,
) -> List[Any]:
    """The earlier facts the judge must weigh against a new one.

    `scope`        who may be a candidate (owner, project, end user): the wall.
    `is_open`      what "still true" means (Agents: `true_at()`, which also
                   counts a fact closed on a future date); default
                   `invalid_at IS NULL`.
    `source`       the memory being read (BUG-123): the facts it stated, born
                   from it or reconfirmed by it, never enter. A memory listing
                   "hobby, developer, team" does not close its own plans (rule
                   3 of `contradiction`, Graphiti §2.2.3).
    `with_recall`  Stage 1a-bis on: only when the scope is one end user, where
                   the same relation family is almost always the same entity;
                   the global scope is too broad to assume it.
    `only_arguments`  a past-tense statement closes its own twin by itself;
               what it can still make false are the facts about the same two
               entities (stages 1c and 1d only).
    `subject_named`  (6.5.0) the new fact's subject was named by whoever
               wrote it, not read by the extraction (Agents: a fact written
               directly, a correction; the boundary of `store.resolve_
               predicate(merge=False)`, BUG-202). Its candidates are the facts
               about the same canonical subject, the memory's aliases applied
               (1a, 1c, and 1b among those facts only), and the facts about the
               same two entities the other way round (1d); 1a-bis, which
               gathers any subject of the relation, is not run.
    `named`    (6.5.0) a condition on the fact table, true for the facts
               whose subject their writer named (Agents: their `model_used`).
               Such a fact enters stages 1a-bis and 1b only about the new
               fact's canonical subject: the same rule seen from the other
               side, for a new fact the extraction read. NULL is not named.
               Two facts that are not named meet as before.
    `agent_scoped`  (6.7.0) the new fact belongs to an agent's memory with
               no end user (Agents: written with an `agent_id` and no
               `user_id`, the frame of the extraction since 6.4.0, Mem0's
               `is_agent_scoped`; one rule for both, `owner.agent_scoped`):
               its stage 1b looks among the facts about
               the same canonical subject only, as for a named subject.
               Stages 1a, 1c and 1d as they are; 1a-bis does not run there
               (no end user: `with_recall` off).
    `agent_scoped_facts`  (6.7.0) a condition on the fact table, true for
               the facts of an agent's memory with no end user. Such a fact
               enters stage 1b of any new fact only about that fact's
               subject: the rule seen from the other side, so which of two
               memories was written first does not decide whether their
               facts meet. NULL is not. None (a memory with an end user,
               which holds none of them): every statement of 6.6.0.
               6.9.0: the names are 6.7.0's, the rule wider. The engine
               passes `agent_scoped` and `agent_scoped_facts` for every
               memory with no end user, with an agent or without
               (`owner.keeps_to_its_subject`, where the reason and the
               measurement are): with no end user the store cannot know the
               memory is one person's story, the reason 1b looks past the
               subject. Nothing here changed: who keeps to its subject is
               the caller's condition.

    Why (6.5.0). Stages 1a-bis and 1b look past a subject's surface because
    the extractor writes one entity several ways ("Maria", "the user", "the
    runner"). A name the writer chose is not that: it says which entity the
    fact is about. Measured on 6 Nov 2026 on a store of an agent team's own
    facts (586, 418 written directly): 108 of the 111 closures between two
    direct facts had two different subjects ("deploy Cloud 1.0.8 happened
    on 5 Nov" closed "deploy Cloud 1.0.7 happened on 2 Nov"), every one
    reachable by the semantic stage alone; replayed with the store's
    embedding model, that stage showed the judge of a direct write 9.7
    facts of other subjects on average, and the judge read its rule that a
    subject phrased differently is the same entity.
      * Ritter, Downey, Soderland, Etzioni, "It's a Contradiction, No, It's
        Not" (EMNLP 2008, ACL Anthology D08-1002, read on 7 Nov 2026): "To
        detect a contradiction, a CD system must know that A) "Mozart"
        refers to the same entity in both sentences" (§2); its candidates
        are the "contradiction sets R(x,·), where a relation R and domain
        argument x have multiple range arguments y" (§4.2), one subject x
        each, and "false contradictions arise when a single x value refers to
        multiple real-world entities" (§4.3).
      * Graphiti (Rasmussen et al. 2025, arXiv:2501.13956, §2.2.2): the
        search for the edges a new one may duplicate "is constrained to
        edges existing between the same entity pairs as the proposed new
        edge. This constraint not only prevents erroneous combinations of
        similar edges between different entities". Its invalidation
        candidates are searched in the whole graph (getzep/graphiti,
        graphiti_core/utils/maintenance/edge_operations.py,
        `resolve_extracted_edges`, `search_filter=SearchFilters()`, read at
        747b480 on 7 Nov 2026), on entities a model has already resolved
        into one node each (§2.2.1). A named subject's entity is resolved
        by its writer and by the aliases (`identity.canonical_of`, applied
        by the write); what is left to search is that entity's facts.
    The semantic stage still finds a relation of another name about the
    same subject. The same holds the other way round (6.5.0, `named`): a
    fact the extraction read, whose subject is a surface, meets a named fact only
    about that fact's subject, since the named fact is no surface; on the
    same store 3 of the 8 closures of a direct fact by an extracted one were
    judged on the extracted fact's write, between two subjects ("the user
    uses Docker" closed "a reviewer agent works read-only, without
    Docker"). Two facts
    the extraction read keep every stage: on that store all 9 closures
    between them were about one subject, and the stages exist for them.
    Why a filter on the candidates and not the [another subject] tag of
    5.8.0 (the decision of 7 Nov 2026). (1) A tag for the judge, never a
    rule that closes on its own (Lin, Mausam, Etzioni, EMNLP 2010 §3), is a
    rule about the VERDICT on the candidates shown; who is shown has always
    been decided by fixed rules (the wall of `scope`, the same source of
    rule 3, the cutoff and the cap of stage 1b, stages 1a, 1c, 1d), and this
    filter closes nothing: it leaves a line out of the prompt. (2) Whether
    two names are one entity is never decided by likeness (`identity`: "Never
    from similarity"), and the tag would ask the judge exactly that, on three
    fields, for a direct fact that has no words to read, under a question
    that tells it a subject phrased differently is the same entity; the tag,
    measured on 18-19 Oct, removed the errors on a namesake and blocked the
    right closings of an undeclared short name, and missed its pre-written
    criterion (5.8.0). (3) The cost: replayed on the store above (an
    estimate), 401 of the 418 direct writes have no candidate of their
    subject, so no call to the judge; with the tag every one pays a call with about ten lines, almost
    all of another subject. The filter decides on its own that two names are
    two things; the hand that makes them one is the alias (a declared name,
    a rename, `POST /admin/aliases`).
    The price: "Maria" and the end user's id are two subjects
    until the name is declared (`POST /v1/users/{id}/names`), and an alias
    of one agent namespace does not rename another's (`store.memory_alias_
    map`, one memory).

    Why (6.7.0). Stage 1b looks past a subject's surface for the reason
    above: inside the memory of one person the extractor writes one entity
    several ways. An agent's memory with no end user (a team's memory, an
    agent's own notes) is not one person's story but an archive of
    different things, and the reason 1a-bis is off there (the scope is too
    broad to assume one relation family is one entity) holds for 1b, which
    is wider still. Replayed on 7 Nov 2026 for free (the store's embedding
    model, the real judge's verdicts taken as given: an estimate) on the
    memory of a team of agents reloaded from 106 memories, 422 facts all
    written with an `agent_id` and no `user_id`: 21 of its 32 closures were
    wrong; 29 of the 32 pairs, 19 of the 21 wrong ones, reached the judge
    only by stage 1b; every write filled the ten places under the cutoff,
    and the distances of the wrong pairs (0.20-0.62) and of the right ones
    (0.15-0.52) overlap, so no cutoff separates them. With 1b among the
    facts of one subject: 4 wrong closures (3 by the judge, 1 made with no
    judge), 1 right, the lines the judge reads 3,740 -> 666.
      * Mem0 (mem0ai/mem0 f1791daa, mem0/memory/main.py, `add`, read on 7
        Nov 2026): the memories an addition is weighed against are the ten
        nearest by vector (`top_k=10`) under a filter of every id passed
        (`search_filters`: user_id, agent_id, run_id), so with an agent id
        and no user id (its `is_agent_scoped`) only that agent's memories
        are near. Korely keeps the wall of the project's memory with no end
        user, every agent of it (whether a team's memory is one story or
        one per agent is another decision), and narrows by subject.
      * Graphiti (Rasmussen et al. 2025, arXiv:2501.13956, §2.2.1 and
        §2.2.3): the edges a new one may invalidate are "semantically
        related existing edges", on entities a model has already resolved
        into one node each; at v0.5.2 (getzep/graphiti 0f50b74,
        graphiti_core/graphiti.py, `get_relevant_edges` on the new edge's
        source node and on its target node, read on 7 Nov 2026) they were
        the edges of those two nodes only. With no resolved entity, the
        subject the store holds after the aliases is the entity (Ritter et
        al. 2008 §4.2, above: one x per contradiction set).
    The price: two spellings of one thing ("Cloud", "Korely Cloud") are two
    subjects in such a memory until an alias in the namespace of the agent
    that writes unites them; a right closure between them is lost, as a
    wrong one is.

    `subject_names` (6.20.0) the names read as the new fact's subject: its
    group after the links between subjects of a memory with no end user
    (`objects.review_subjects`, off by default; `objects.same_names`), its
    own name among them. Wherever a stage compares a fact's subject or
    object with the new fact's subject (1a, 1c, and the rule of
    `about_the_subject` for 1b), it compares it with every name of the
    group, by the database's lower() as one name is. Nothing else changes:
    1d, the wall, the source, the cutoff and the caps are the same. None, or
    a group of one name: the statements of 6.19.0, byte for byte. The
    caller tells which candidates entered only through the group by asking
    once without it (korely-agent: facts/subjects.py), so that the closings
    they lead to are recorded on the links (`objects.record_closures`).
    Graphiti weighs an edge against the edges of the resolved nodes
    (Rasmussen et al. 2025, §2.2.1 and §2.2.3, above); the group is the
    entity a link resolved.
    """
    F = tables.Fact
    # 6.2.0: the copies an edited memory's previous text shared, weighed by
    # the write's judgement as another memory's facts (`store.judged_apart`).
    apart = apart_from(db, source)
    _scope = _the_wall(db, tables, scope, is_open=is_open, exclude_id=exclude_id, source=source,
                       apart=apart)

    def _base():
        # 3.3.0: the same relation only. Two relations that exclude each
        # other ("likes X" / "dislikes X") share their arguments, and stage
        # 1c below brings them, with no list of opposing pairs.
        return _scope(db.query(F)).filter(F.predicate_canonical == predicate_canonical)

    _sc = _subject_of(F)
    sc_new = (subject_canonical or subject_label or "").strip().lower()
    oc_new = (object_canonical or "").strip().lower()
    # In SQL the names meet with the database's lower() on both sides (2.11.0,
    # as `store.find_open`): Python's lower() is another function, and
    # on "İstanbul" the subject stages found nothing. The Python-lowered forms
    # stay for the comparisons made in Python below.
    sc_sql = _the_name(subject_label, subject_canonical)
    oc_sql = func.lower((object_canonical or "").strip())
    group = _the_group(subject_label, subject_canonical, subject_names)

    same, inverse = _same_arguments(db, F, _scope, _sc, predicate_canonical,
                                        sc_new=sc_new, oc_new=oc_new, sc_sql=sc_sql, oc_sql=oc_sql,
                                        group=group)
    if only_arguments:
        return without_the_same_source(tables, merge_candidates(same, inverse), source, apart=apart)

    # 1a: exact subject, on the CANONICAL form (after the aliases) and
    # case-insensitive: "Globex" must see the facts written when it was "Acme".
    structural = (
        _base()
        .filter(_is_the_subject(_sc, sc_sql, group))
        .order_by(*_newest_first(F))
        .limit(limit)
        .all()
    )
    # 1a-bis: the extractor writes one entity as "Maria", "the user", "the
    # runner"; the exact-subject stage misses those updates, and the old value
    # is never superseded. Independent of the embedding, so it also covers the
    # case where 1b is blind.
    # 6.5.0, 6.6.0: the subject decides who may enter the stages that look
    # past it, when the new fact's subject or the candidate's was named by its writer
    # (the docstring). Not 1a-bis at all for a named new fact: among the facts
    # of its subject it is stage 1a. 6.7.0: and when either of the two belongs
    # to an agent's memory with no end user.
    about = about_the_subject(F, subject_label, subject_canonical, subject_named=subject_named,
                              named=named, agent_scoped=agent_scoped,
                              agent_scoped_facts=agent_scoped_facts,
                              object_canonical=object_canonical, subject_names=subject_names,
                              the_person=the_person)
    recall: List[Any] = []
    if with_recall and not subject_named:
        q = _base() if about is None else _base().filter(about)
        recall = q.order_by(*_newest_first(F)).limit(limit).all()

    semantic = _semantic(db, F, _scope, vector, about=about)

    return without_the_same_source(
        tables, merge_candidates(structural, same, inverse, recall, semantic), source, apart=apart,
    )


# ---------------------------------------------------------------------------
# The steps of `for_the_judge`, one function each (5.11.1, 20 Oct 2026, the
# engine's hot spots: the same stages in the same order,
# the same statements, nothing changed in what the judge reads). Checked by
# running the function before the split and this one on the same 200
# scripted memories: same candidates, same statements.
# ---------------------------------------------------------------------------

def _the_wall(db, tables: Tables, scope: Any, *, is_open: Any, exclude_id: Any, source: Any,
              apart: Any = ()):
    """Who may enter, whatever shape the query has (with or without the
    distance column). Every query of `for_the_judge` is wrapped by this, the
    lesson of the project wall that the semantic stage once left open."""
    F = tables.Fact

    # 3.1.3: still true as when the running write began, so a fact an earlier
    # statement of the same write closed is still weighed
    # (`store.one_write`, Graphiti's episode against the graph before it).
    truth = as_before_the_write(db, F, is_open if is_open is not None else F.invalid_at.is_(None))

    def _scope(q):
        return (
            q.filter(scope)
            .filter(truth)
            .filter(F.id != exclude_id)
            # BUG-123: in the scope, so every stage (and its LIMIT) sees it.
            .filter(outside_the_source(tables, source, apart=apart))
        )

    return _scope


def _subject_of(F: Any) -> Any:
    """A fact's subject as the subject stages compare it: its canonical name
    (the memory's aliases applied at the write), else its written one, with
    the database's lower()."""
    return func.lower(func.coalesce(F.subject_canonical, F.subject_label))


def _the_name(subject_label: Optional[str], subject_canonical: Optional[str]) -> Any:
    """A new fact's subject, compared the same way: trimmed, lowered by the
    database (never by Python, `find_open`'s lesson of "İstanbul")."""
    return func.lower((subject_canonical or subject_label or "").strip())


def _the_group(subject_label: Optional[str], subject_canonical: Optional[str],
               subject_names: Optional[Sequence[str]]) -> Optional[List[Any]]:
    """6.20.0: the names of the new fact's subject's group, each compared as
    `_the_name` compares one (the database's lower()), its own name first;
    None when there is no other name, and every comparison is then the one
    of 6.19.0."""
    own = (subject_canonical or subject_label or "").strip()
    others = sorted({n.strip() for n in subject_names or () if n and n.strip()
                     and n.strip().lower() != own.lower()})
    if not others:
        return None
    return [_the_name(subject_label, subject_canonical)] + [func.lower(n) for n in others]


def _is_the_subject(column: Any, name: Any, group: Optional[List[Any]]) -> Any:
    """`column` is the new fact's subject: `== name`, or IN its group."""
    return column == name if group is None else column.in_(group)


def about_the_subject(F: Any, subject_label: Optional[str], subject_canonical: Optional[str] = None,
                      *, subject_named: bool = False, named: Any = None,
                      agent_scoped: bool = False, agent_scoped_facts: Any = None,
                      object_canonical: Optional[str] = None,
                      subject_names: Optional[Sequence[str]] = None,
                      the_person: Optional[str] = None) -> Any:
    """Who may stand next to a fact of this subject in the stages that look
    past the subject (1a-bis, 1b), as a condition on the rows of `F`, or None
    for every fact (6.5.0; public since 6.6.0, the one rule of "the
    same subject" for the write and for the late judgement of a vector).

    The rule: two facts meet there when they have the same subject as stage
    1a compares it (`_subject_of` against `_the_name`: the canonical name
    after the memory's aliases, the database's lower() on both sides), or
    when neither keeps to its subject. A fact keeps to its subject when its
    writer named it (6.5.0: `subject_named` for the new fact, `named` a
    condition true for such facts) or when it belongs to an agent's memory
    with no end user (6.7.0: `agent_scoped`, `agent_scoped_facts`; the reason
    is in `for_the_judge`). So for a fact that keeps to its subject only the
    facts of its subject; for another fact every fact that does not keep to
    its own, and those that do about its subject. The rule is the same seen
    from either fact of a pair, so the late judgement builds it on the late
    fact and applies it to the later facts. `coalesce` because a fact with
    no `model_used` is not named, and NOT of a NULL would leave it out.

    Nothing else enters the comparison, on purpose (the decision of 7 Nov
    2026): no Unicode normalization (NFKC, case folding) and no squeezing of
    inner spaces, which would also change `store.find_open`, the unique
    index of the open facts and the deduplication of every fact, a change of
    its own; no word list ("the user" is not "user": the rule of every
    alphabet, no list of words); no name inside another ("Anna" inside "Anna
    Rossi", the rule `compatible_objects` gives the objects of stages 1c and
    1d as a reason to ask, never an identity). "Straße 5" and "STRASSE 5",
    "Anna Rossi" and "Anna" are two subjects until a name is declared.
    The same comparison has a price on other alphabets and forms, the
    database's lower() being no case folding nor normalization: "ΟΔΟΣ" and
    "οδος" (a final sigma), "Café" written composed and decomposed, "Ａｃｍｅ"
    in full-width letters and "Acme" are two subjects too
    (docs/guide/limits.md); stage 1a and `store.find_open` compare them the
    same way.

    The other role (6.9.0, `object_canonical`, the new fact's object). A
    fact of a memory with no end user (`agent_scoped`, `agent_scoped_facts`)
    keeps to its ENTITY in either role, not to its subject alone: it meets a
    fact whose subject or object is its subject, or whose subject is its
    object. Graphiti's invalidation candidates are the existing edges
    touching the new edge's source node or its target node, in either
    position (getzep/graphiti 0f50b74, v0.5.2, graphiti_core/graphiti.py,
    `existing_source_edges_list` and `existing_target_edges_list`;
    search/search_utils.py `edge_similarity_search`: "n.uuid IN
    [$source_uuid, $target_uuid]", "m.uuid IN [$source_uuid, $target_uuid]";
    read on 12 Nov 2026). Found by a review of 12 Nov 2026: in a memory with
    no ids "Acme employs user", then "user works_at Globex", no longer met,
    though the person is one entity in two roles. Left out on purpose: two
    facts that only share their object ("one plan uses Linear", "another
    uses Linear"), the shape of the closures between two subjects this rule
    removed (Ritter et al., EMNLP 2008 §4.3, one x per contradiction set).
    The rule stays the same seen from either fact of a pair: "B's object is
    A's subject" read from B is "A's subject is B's object". A named fact
    (6.5.0) still keeps to its subject alone, and with no
    `agent_scoped_facts` (a memory with an end user) every statement is the
    6.6.0 one.

    `subject_names` (6.20.0, `for_the_judge`): the new fact's subject is
    every name of its group, in both roles.

    `the_person` (6.25.0): the canonical name of the memory's person when the
    caller knows the person's names (korely-agent: the end user's id, when a
    name was declared for them, `POST /v1/users/{id}/names`). Every name of
    the person is then that canonical name (the aliases of the write), so a
    fact about the person and a fact about another subject are about two
    entities, and they no longer meet in the stages that look past the
    subject: entity resolution first, then the invalidation candidates
    among the edges of the resolved entity only (Graphiti, Rasmussen et al.,
    arXiv:2501.13956, 2025, sections 2.2.1-2.2.3; getzep/graphiti v0.5.2,
    the edges touching the new edge's own nodes, as for `_the_entity`). Why
    (25 Nov 2026): on the identity bench (17 Oct 2026)
    "Marco Bianchi eats meat at every meal" closed "the user is vegetarian"
    for a user called Marco, 2-3 such closures a run; the label "[another
    subject]" for the judge (5.8.0) removed them but lost right closures
    (18 -> 15). Here the judge is not asked at all. Without a declared name
    nothing changes: an undeclared "Elena Poli" still meets the user's facts
    (the identity bench's named-in-full cases)."""
    rule = _about_the_subject(F, subject_label, subject_canonical, subject_named=subject_named,
                              named=named, agent_scoped=agent_scoped,
                              agent_scoped_facts=agent_scoped_facts,
                              object_canonical=object_canonical, subject_names=subject_names)
    if not (the_person or "").strip():
        return rule
    person = func.lower(the_person.strip())
    theirs = _subject_of(F) == person
    mine = _the_name(subject_label, subject_canonical) == person
    one_side = or_(and_(mine, theirs), and_(not_(mine), not_(theirs)))
    return one_side if rule is None else and_(rule, one_side)


def _about_the_subject(F: Any, subject_label: Optional[str], subject_canonical: Optional[str] = None,
                       *, subject_named: bool = False, named: Any = None,
                       agent_scoped: bool = False, agent_scoped_facts: Any = None,
                       object_canonical: Optional[str] = None,
                       subject_names: Optional[Sequence[str]] = None) -> Any:
    """`about_the_subject` without `the_person`: the rules of 6.5.0-6.20.0."""
    group = _the_group(subject_label, subject_canonical, subject_names)
    same = _is_the_subject(_subject_of(F), _the_name(subject_label, subject_canonical), group)
    if subject_named:
        return same
    shares = _the_entity(F, subject_label, subject_canonical, object_canonical, group=group)
    if agent_scoped:
        return shares if named is None else and_(shares, or_(same, not_(func.coalesce(named, false()))))
    if agent_scoped_facts is None:
        # A memory with an end user: the statements of 6.6.0, byte for byte.
        keeping = [not_(func.coalesce(named, false()))] if named is not None else []
        return or_(same, and_(*keeping)) if keeping else None
    rules = [or_(shares, not_(func.coalesce(agent_scoped_facts, false())))]
    if named is not None:
        rules.insert(0, or_(same, not_(func.coalesce(named, false()))))
    return and_(*rules)


def _the_entity(F: Any, subject_label: Optional[str], subject_canonical: Optional[str],
                object_canonical: Optional[str], *, group: Optional[List[Any]] = None) -> Any:
    """6.9.0: the rows that share the new fact's entity in either role (the
    other role of `about_the_subject`): their subject is its subject, their
    object is its subject, or their subject is its object. Compared as stages
    1a and 1d compare names (the canonical name, the database's lower())."""
    name = _the_name(subject_label, subject_canonical)
    roles = [_is_the_subject(_subject_of(F), name, group), _is_the_subject(_object_of(F), name, group)]
    if (object_canonical or "").strip():
        roles.append(_subject_of(F) == func.lower(object_canonical.strip()))
    return or_(*roles)


def _object_of(F: Any) -> Any:
    """A fact's object as the subject stages compare a name: its canonical
    form, else its written one, with the database's lower()."""
    return func.lower(func.coalesce(F.object_canonical, F.object_label))


def _newest_first(F: Any) -> tuple:
    """Newest first, then by id (5.2.7). The facts of one write share their
    created_at (the transaction's now()), so a LIMIT on created_at alone cut
    inside a tie in an order Postgres does not define: on the kept LoCoMo
    database 13 of the 40 subjects with more than 10 open facts had the 10th
    and 11th at the same instant, on LongMemEval 200 of 386 (measured on
    18 Oct 2026). Which candidates the judge saw could change from one
    reading to the next. The id makes the cut the same every time; it does
    not make it better (a code review)."""
    return (F.created_at.desc(), F.id.desc())


def _same_arguments(db, F: Any, _scope, _sc: Any, predicate_canonical: str, *,
                      sc_new: str, oc_new: str, sc_sql: Any, oc_sql: Any,
                      group: Optional[List[Any]] = None) -> Tuple[List[Any], List[Any]]:
    """Stages 1c and 1d, the facts about the same two entities; none without
    an object."""
    # 1c (BUG-111): same subject, a compatible object ("SSO" / "SSO support"),
    # another relation ("lacks X" / "adopted X").
    same: List[Any] = []
    # 1d: the relation the other way round ("Salesforce serves_as_crm_for
    # Globex" when the new fact is "Globex no longer uses Salesforce").
    inverse: List[Any] = []
    if oc_new:
        same = [
            c for c in (
                _scope(db.query(F))
                .filter(_is_the_subject(_sc, sc_sql, group), F.predicate_canonical != predicate_canonical)
                .order_by(*_newest_first(F))
                .limit(50)
                .all()
            )
            if compatible_objects(oc_new, c.object_canonical or c.object_label)
        ][:5]
        inverse = [
            c for c in (
                _scope(db.query(F))
                .filter(_sc == oc_sql)
                .order_by(*_newest_first(F))
                .limit(50)
                .all()
            )
            if arguments_swapped(sc_new, oc_new, c.subject_canonical or c.subject_label,
                                   c.object_canonical or c.object_label)
        ][:5]
    return same, inverse


def _semantic(db, F: Any, _scope, vector, about: Any = None) -> List[Any]:
    """Stage 1b: facts about the same thing that the extractor canonicalized
    differently ("charity 5K run | personal_best_time" vs "<user> |
    has_personal_best_time"). cosine_distance is pgvector-only; on SQLite the
    query not only errors but poisons the session, so it is gated by dialect
    instead of run-and-catch.

    `about` (6.5.0, `about_the_subject`): a condition on the candidates' subject; the
    nearest facts are looked for among those that pass it, inside the
    query, so the ten are the ten nearest of those (the HNSW scan goes on
    until it finds them, `vector_search.hnsw_depth`)."""
    semantic: List[Any] = []
    if vector is not None and _pg(db):
        threshold = thresholds.current().semantic
        try:
            # 2.11.0 (an audit of 2.10.0 and the code audit of 6 Oct):
            # in a savepoint. On Postgres a failed statement (a vector of
            # another size, a statement timeout) aborts the transaction, and
            # the `except` that said "best effort" left it aborted: the next
            # statement of the write failed with InFailedSqlTransaction
            # (reproduced). The savepoint rolls back this query
            # alone, as BUG-300 does for the context (agent_context.py).
            # 2.12.0 (BUG-320): `vector_search.nearest` sets the depth of
            # the HNSW scan first, inside this savepoint: the index holds
            # every account's facts and the memory's filters come after it,
            # so 10 asked could be fewer, or none.
            with db.begin_nested():
                q = _scope(db.query(F)).filter(F.embedding.isnot(None))
                if about is not None:
                    q = q.filter(about)
                rows = nearest(db, q, F.embedding, vector, SEMANTIC_LIMIT)
        except Exception as e:
            # And the write does not go on without it. It used to: "never
            # break a write on the semantic pre-filter" judged the new fact on
            # the other stages alone, so a contradiction only 1b finds stayed
            # open for good, the memory marked done. A stage that could not
            # run is not "no candidates" (`CandidatesUnread`): recall here,
            # precision in the judge (ConflictBank, Su et al. 2024), and a
            # candidate missed is a contradiction nobody ever notices.
            # 3.0.0: a database error's message carries the
            # statement's parameters (the end user's id among them): its
            # classes, codes and frames only (`log_safe`).
            logger.error("[memory/F3] semantic candidate retrieval failed, the write is not judged "
                         "without it: %s\n%s", _log.error(e), _log.stack(e))
            raise CandidatesUnread(f"semantic candidate retrieval failed: {_log.error(e)}") from e
        for cand, d in rows:
            if d is not None and float(d) <= threshold:
                semantic.append(cand)
    return semantic
