"""Maximal marginal relevance over the stored vectors of memories.

The order of the memories a large block shows past its first whole ones
(services/context.py, `_pool_for`; since 12 Oct 2026): each
next one is the memory most like the question and least like the memories
already placed (Carbonell and Goldstein, SIGIR 1998), so seven phrasings of
one plan stop taking the lines the question's other items need. Until 12 Oct
it was the bench-only experiment WIDE_POOL of context_experiments.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

# The weight of the likeness to the question against the likeness to the
# memories already placed: 0.7, the value read on LoCoMo (12 Oct 2026: +15 of
# 282 multi-hop questions at 4,000 tokens a person, p 0.011).
LAMBDA = 0.7


def _unit(v):
    import numpy as np

    if v is None:
        return None
    v = np.asarray(v, dtype=float)
    n = float(np.linalg.norm(v))
    return v / n if n else None


def order(db: Session, hits: list, query_vector, keep_first: int, lam: float = LAMBDA) -> list:
    """`hits` (the search's, best first) with the first `keep_first` where
    they are and the others by maximal marginal relevance: each next one the
    memory with the best lam * likeness to the question - (1 - lam) * its
    likeness to the closest memory already placed. A tie keeps the search's
    order; a memory with no stored vector follows the ones that have one."""
    from ..db.models import AgentMemory

    q = _unit(query_vector)
    if q is None or len(hits) <= keep_first + 1:
        return hits
    ids = []
    for h in hits:
        try:
            ids.append(UUID(str(h.get("id"))))
        except (TypeError, ValueError):
            continue
    vecs = {str(i): _unit(v) for i, v in
            db.query(AgentMemory.id, AgentMemory.embedding).filter(AgentMemory.id.in_(ids))}
    out = list(hits[:keep_first])
    placed = [v for v in (vecs.get(str(h.get("id"))) for h in out) if v is not None]
    rest = [(h, vecs.get(str(h.get("id")))) for h in hits[keep_first:]]
    left = [(h, v) for h, v in rest if v is not None]
    while left:
        def score(k):
            v = left[k][1]
            near = max((float(v @ w) for w in placed), default=0.0)
            return lam * float(q @ v) - (1 - lam) * near
        best = max(range(len(left)), key=score)
        h, v = left.pop(best)
        out.append(h)
        placed.append(v)
    return out + [h for h, v in rest if v is None]
