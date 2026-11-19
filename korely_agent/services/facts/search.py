"""The facts in the search (1.0.13): POST /v1/memories/search and the MCP's
korely_search answer, when asked (`types`), with the facts closest in meaning
to the question, alone or in one list with the memories, each hit saying its
kind.

Until 1.0.12 a search read the memories only. A fact has a vector of its own,
computed on "subject predicate object" with its current names
(service.fact_text), which the context block ranks its facts by and the
contradiction judge finds its candidates with; no search read it, so a fact
written with POST /v1/facts, which has no memory, was found by none. On the
store of an agent team's own memory (6 Nov 2026), asked eight times "is
there a decision about X?", a search of the memories found the decision 2
times and the context block 8 (a small sample).

**What a search asks for.** `types`, a list of the kinds it searches, each
once: `memories` (the default, alone), `facts`, or both. The order they are
listed in does not matter: the kinds are always searched and merged in the
store's order, KINDS, which is the order that breaks a tie of rank.

**What a search of the facts reads.** The facts true now behind the block's
walls (context.facts_base): the account, the key's project, the end user
asked or a confined key's list, the agent asked, and the facts some note
states when the sessions are left out; and, as the memories' search does, a
confined key's list as a second wall (`AgentMemoryEngine.end_users`). A fact
no longer true is never found, nor one still waiting for its vector (GET
/v1/facts lists it; a search finds it once its vector is computed). The
walls are the engine's, read from the one engine the memories are searched
with, so the two kinds of one search cannot be read behind two walls.

**How it ranks them.** By the cosine of the question's one vector
(`AgentMemoryEngine.question_vector`, one call for the whole search) and the
fact's, best first, ties by id, as the memories' search ranks the memories;
not by the block's recency weight (memory_core.presentation.rank_with_recency),
which serves a block that shows ten facts and their history, not a search
that says how close each hit is. With more than one kind each is ranked by
its own cosine and the rankings are merged by rank
(services/search_fusion.py, which says why not by score); the `limit` best of
each kind are asked for, and the first `limit` of the fusion returned. So
with `limit: 1` and both kinds, the first memory is the answer whenever
there is one, however close the first fact is.

**What a search with the facts cannot be narrowed by.** `run_id` and
`metadata` narrow the memories: a fact has no metadata, and no read of the
facts is narrowed by a run (GET /v1/facts and the block are not), its
`run_id` being its first memory's whatever memories said it since.
`min_score` is the memories' floor, on a memory's cosine; the facts have no
floor of their own, since how a fact's cosine compares with a memory's was
not measured. A search that asks for the facts and any of the three is
refused (`combination_problem`, a 422 on REST and the tool's refusal on the
MCP) rather than answered as if it had narrowed the facts: a floor that left
the facts unfloored would break what a floor promises, an empty list when
nothing is close.

A search of the memories alone (`types: ["memories"]`, the default) is the
one there was before this module, call for call.
"""
from __future__ import annotations

import logging
import typing
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence

from ...db.models import AgentFact
from .. import limits, user_scope
from ..ids import fact_public_id, mem_public_id
from ..memory_core import log_safe
from ..memory_core.vector_search import hnsw_depth
from ..search_fusion import fused
from ..sentinels import EVERY_END_USER, EndUsers

logger = logging.getLogger(__name__)

#: The kind of each hit (a result's `type`): one declaration for the hits and
#: the answers that show them.
Hit = Literal["memory", "fact"]
MEMORY, FACT = typing.get_args(Hit)
#: A kind a search may be asked for (each value of `types`, on REST's list
#: and in korely_search's text alike: one declaration for both doors), in
#: the store's order, the one the merged list breaks a tie of rank by.
Kind = Literal["memories", "facts"]
KINDS = typing.get_args(Kind)
MEMORIES, FACTS = KINDS
#: What a search searches when it does not say: the memories only, as before
#: 1.0.13. A caller that only used memories would otherwise get `fct_` ids in
#: its results, in place of memories at the top, the day it upgrades.
DEFAULT_TYPES = (MEMORIES,)
#: How korely_search's one text names several kinds: `memories,facts`. A
#: tool's argument is one value (tests/test_every_input_by_one_rule.py).
KIND_SEPARATOR = ","


def types_problem(types: Sequence[str], kinds: Sequence[str] = KINDS) -> Optional[str]:
    """`types` as both doors read it: at least one kind, each a kind of
    `kinds` (KINDS, or the core's and a part of the product's:
    services/memory_search.MemorySearch.kinds), each once. None when it is
    one."""
    if not types:
        return f"types must name at least one of {', '.join(kinds)}"
    unknown = next((t for t in types if t not in kinds), None)
    if unknown is not None:
        return f"types names {unknown!r}, which is not one of {', '.join(kinds)}"
    twice = next((t for t in kinds if list(types).count(t) > 1), None)
    if twice is not None:
        return f"types names {twice} twice"
    return None


def kinds_in(text: str) -> List[str]:
    """korely_search's `types`: the kinds of its one text, `memories`,
    `facts` or `memories,facts`, each part as written once its spaces are
    stripped (`types_problem` reads them)."""
    return [part.strip() for part in (text or "").split(KIND_SEPARATOR)]


def combination_problem(types: Sequence[str], *, run_id: Optional[str] = None,
                        metadata: Optional[dict] = None,
                        min_score: Optional[float] = None) -> Optional[str]:
    """A filter of the memories alone asked of a search with the facts: each
    door refuses it (the module's docstring) rather than answer as if it had
    narrowed the facts. None otherwise."""
    if FACTS not in types:
        return None
    if run_id is not None:
        return ("run_id narrows the memories and types has facts: no read of the facts is "
                "narrowed by a run; leave facts out of types, or run_id out")
    if metadata:
        return ("metadata narrows the memories and types has facts: a fact has no metadata; "
                "leave facts out of types, or metadata out")
    if min_score is not None:
        return ("min_score floors the memories and types has facts: the facts have no "
                "floor, their score is not on a memory's scale; leave facts out of types, "
                "or min_score out")
    return None


def search_facts(engine, *, query_vector: list, end_user_id: Any = EVERY_END_USER,
                 agent_id: Optional[str] = None, exclude_sessions: bool = False,
                 limit: int = limits.DEFAULT_SEARCH_RESULTS) -> List[dict]:
    """The facts true now with a vector closest to `query_vector`, best
    first, behind the walls of `engine` (an AgentMemoryEngine: its account,
    project and confined end users) and of the read (`end_user_id`, by
    services/user_scope.wall; `agent_id`; `exclude_sessions`); at most
    `limit`. Each hit has its kind, its fact as GET /v1/facts shows it, and
    the fact's line as the MCP's tools print it as its `content`. Raises
    MemorySearchUnavailable when the database cannot answer, as the
    memories' search does: the query, the names a fact shows (its memory's
    links, a read of their own) and what is made of them alike."""
    from ..context import facts_base
    from ..memory_core.presentation import mcp_line
    from ..memory_engine import MemorySearchUnavailable, search_pool
    from .links import prefetch
    from .reads import serialize_fact

    limit = max(1, int(limit))
    db = engine.db
    try:
        q = facts_base(db, engine.tenant_id, end_user_id, agent_id, engine.project_id,
                       exclude_sessions=exclude_sessions)
        if engine.end_users is not None:
            q = user_scope.within_end_users(q, AgentFact.end_user_id, EndUsers(engine.end_users))
        dist = AgentFact.embedding.cosine_distance(query_vector).label("dist")
        pool = search_pool(limit)
        # idx_agent_facts_embedding_hnsw holds every account's facts and the
        # walls apply after the scan: the index is let bring the whole pool,
        # as for the memories (memory_core.vector_search.hnsw_depth).
        hnsw_depth(db, pool)
        rows = (q.filter(AgentFact.embedding.isnot(None)).add_columns(dist)
                .order_by(dist, AgentFact.id).limit(pool).all())[:limit]
        prefetch(db, [f for f, _d in rows])
        out = []
        for f, d in rows:
            shown = serialize_fact(f)
            out.append({"type": FACT, "id": f.id, "score": round(1.0 - float(d), 4),
                        "content": mcp_line(shown), "fact": shown,
                        "end_user_id": f.end_user_id, "agent_id": f.agent_id, "metadata": {}})
    except Exception as e:  # noqa: BLE001 - not an empty result: a search that could not run
        logger.warning("[agent_facts] fact search failed for %s: %s", engine.tenant_id,
                       log_safe.stack(e))
        raise MemorySearchUnavailable() from e
    return out


def search(engine, query: str, *, types: Sequence[str] = DEFAULT_TYPES,
           end_user_id: Any = EVERY_END_USER, agent_id: Optional[str] = None,
           run_id: Optional[str] = None, metadata: Optional[dict] = None,
           limit: int = limits.DEFAULT_SEARCH_RESULTS, min_score: Optional[float] = None,
           exclude_sessions: bool = False, kinds: Sequence[str] = KINDS,
           more: Optional[Mapping[str, Callable[[list], List[dict]]]] = None) -> List[dict]:
    """The hits closest to `query`, of the kinds `types` names, behind the
    walls of `engine` (the caller's AgentMemoryEngine) and of the read; at
    most `limit`, each with its `type`, its own cosine as `score` and its
    whole text as `content` (a memory's, or a fact's line). `types` of the
    memories alone is exactly the memories' search as it was. `kinds` and
    `more` are a part of the product's (services/memory_search.py): every
    kind `types` may name, in the order the rankings are merged (the core's
    first), and the ranking of each kind past the core's, asked with the
    question's one vector. Raises ValueError for what `types_problem` and
    `combination_problem` refuse, MemorySearchUnavailable as the memories'
    search does."""
    problem = (types_problem(types, kinds)
               or combination_problem(types, run_id=run_id, metadata=metadata,
                                      min_score=min_score))
    if problem:
        raise ValueError(problem)
    kinds = [k for k in kinds if k in types]
    memories_kw = dict(end_user_id=end_user_id, agent_id=agent_id, run_id=run_id,
                       metadata=metadata, limit=limit, min_score=min_score,
                       exclude_sessions=exclude_sessions)
    if kinds == [MEMORIES]:
        return [{**h, "type": MEMORY} for h in engine.search(query, **memories_kw)]
    if not (query or "").strip():
        return []
    vector = engine.question_vector(query, agent_id)
    # Each kind's ranking, with the one vector of the question.
    ranking: Dict[str, Callable[[], List[dict]]] = {
        MEMORIES: lambda: [{**h, "type": MEMORY} for h in
                           engine.search(query, query_vector=vector, **memories_kw)],
        FACTS: lambda: search_facts(engine, query_vector=vector, end_user_id=end_user_id,
                                    agent_id=agent_id, exclude_sessions=exclude_sessions,
                                    limit=limit),
        **{kind: (lambda rank=rank: rank(vector)) for kind, rank in (more or {}).items()},
    }
    return fused([ranking[k]() for k in kinds], limit)


def public_id(hit: dict) -> str:
    """A hit's public id: `mem_` for a memory, `fct_` for a fact."""
    return (fact_public_id if hit.get("type") == FACT else mem_public_id)(hit["id"])


def reads(hits: List[dict], types: Sequence[str]) -> dict:
    """What the trail keeps of a search (`log_v1_request`'s arguments): the
    ids of the memories and of the facts it showed, each kind only when it
    was asked for, so a search of the memories alone is kept as it was."""
    out = {}
    if MEMORIES in types:
        out["memories_read"] = [mem_public_id(h["id"]) for h in hits if h.get("type") == MEMORY]
    if FACTS in types:
        out["facts_read"] = [fact_public_id(h["id"]) for h in hits if h.get("type") == FACT]
    return out
