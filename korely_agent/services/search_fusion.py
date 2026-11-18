"""Rankings of different kinds merged into one list, by rank (1.0.13).

A search that answers with more than one kind of thing (the memories and
the facts: services/facts/search.py) ranks each kind by its own score, the
cosine between the question's one vector and the stored text's, and merges
the rankings here.

**Why by rank and not by score.** Both kinds are scored by the same
function, but not on the same kind of text: a fact's vector is computed on
"subject predicate object" with its current names (services/facts/
service.fact_text), a memory's on what somebody wrote, sentences, often many
of them. A cosine depends on the text as much as on the question, and how a
fact's compares with a memory's for the same question was not measured. So
the raw cosines of two kinds are not put in one order: each ranking is kept
in its own, and the rankings are merged by reciprocal rank fusion, which
reads the ranks only. A hit at rank r of its ranking weighs 1 / (RRF_K + r),
RRF_K = 60 (Cormack, Clarke, Büttcher, "Reciprocal Rank Fusion outperforms
Condorcet and individual Rank Learning Methods", SIGIR 2009, where k = 60
"was fixed during a pilot investigation" and its choice is said not to be
critical).

Rankings with no item in common, which rankings of different kinds are,
make the fusion an alternation by rank: the first of the first ranking, the
first of the second, the second of the first, and so on, the earlier ranking
first at equal rank. That is the honest reading of what RRF does here; it
says nothing of which kind answers better, which is not measured. Each hit
keeps its own score, so a caller can still read it; the scores of two kinds
are not to be compared with each other.
"""
from __future__ import annotations

from typing import List, Sequence

#: Reciprocal rank fusion's constant: k = 60 (Cormack, Clarke, Büttcher,
#: SIGIR 2009; the module's docstring says what it does here).
RRF_K = 60


def fused(rankings: Sequence[List[dict]], limit: int) -> List[dict]:
    """The rankings in one, by reciprocal rank fusion (the module's
    docstring), at most `limit`: by 1 / (RRF_K + rank), the earlier ranking
    first at equal weight, and within a ranking in its own order, so a
    single ranking comes back exactly as it was given."""
    ranked = [(1.0 / (RRF_K + rank), kind, rank, h)
              for kind, hits in enumerate(rankings)
              for rank, h in enumerate(hits, 1)]
    ranked.sort(key=lambda x: (-x[0], x[1], x[2]))
    return [h for _w, _k, _r, h in ranked[:max(1, int(limit))]]
