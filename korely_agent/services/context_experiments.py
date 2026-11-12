"""The switches of the context block: two, both on.

Moved out of context.py on 2026-10-06 (they were 29% of the module and a
third of assemble_context's branches), so that the block the
product builds reads in one place and what was being tried reads in another.
Each one is a module-level switch that the tests flip (`monkeypatch.setattr`),
never an environment variable: an option nobody has measured, shipped as a
setting, is an option somebody turns on. Why each one exists, and what
measuring it showed, is written next to it. Both are on since 0.1.23, by a
decision of 8 Oct 2026 on measures that never read worse and cost nothing.

    DIALOGUE_ORDER         the whole memories of one exchange in the order said
    SUPERSEDED_AS_HISTORY  a memory whose facts were all superseded, kept as history

WIDE_POOL became the product's rule on 12 Oct 2026 (the pool follows the
budget, services/context.py); MEMORY_RECENCY, DIVERSITY, PLAN_CAP and
STANDING_INSTRUCTIONS were removed the same day.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

# The whole memories in the order they were said (bench-only until measured,
# 2026-10-08, an analysis of the full LoCoMo blocks). The first WHOLE
# memories are chosen by relevance and printed in that order, so two turns of
# one exchange often come backwards: in 65% of the blocks two or more of them
# are consecutive turns, and 57% of those are out of order ("Those posters
# are great!" printed above the turn that shared the poster). On: the same
# memories, each run of consecutive turns of one end user (nothing of theirs
# written in between) printed as they were said, at the rank of its best
# member; the others keep their place. Retrieved chunks kept in their
# original order read better than the same chunks by relevance (OP-RAG, Yu
# et al., arXiv:2409.01666, 2024); a coherent segment beats isolated turns
# as the unit a reader sees (SeCom, Pan et al., ICLR 2025). No line is added
# or removed, so the budget is the one of before. Measured on all LoCoMo
# against a same-day reading: 1,255 against 1,245 alone, 1,263 with the
# observation key (+18, p 0.14); never worse. On since 0.1.23 (8 Oct 2026).
# Off: relevance order.
DIALOGUE_ORDER = True


def in_dialogue_order(db: Session, hits: list, keep: int) -> list:
    """`hits` with its first `keep` reordered: each run of consecutive turns of
    one end user, in one project, printed in the order said (the event time,
    then the write order), at the rank of its best member. Two of them are
    consecutive when nothing else of that end user was written between them.
    The rest of `hits` is untouched."""
    from ..db.models import AgentMemory
    from .sentinels import within_project
    from .user_scope import within_end_users

    head = hits[:keep]
    ids = []
    for h in head:
        try:
            ids.append(UUID(str(h.get("id"))))
        except (TypeError, ValueError):
            return hits
    if len(ids) < 2:
        return hits
    rows = {str(r.id): r for r in db.query(
        AgentMemory.id, AgentMemory.tenant_id, AgentMemory.project_id, AgentMemory.end_user_id,
        AgentMemory.created_at, AgentMemory.event_time).filter(AgentMemory.id.in_(ids))}
    if len(rows) != len(ids):
        return hits
    group = {i: i for i in rows}

    def root(i):
        while group[i] != i:
            i = group[i]
        return i

    by_owner: dict = {}
    for r in rows.values():
        by_owner.setdefault((r.tenant_id, r.project_id, r.end_user_id), []).append(r)
    for (tenant_id, project_id, end_user_id), mine in by_owner.items():
        mine.sort(key=lambda r: r.created_at)
        for a, b in zip(mine, mine[1:]):
            # The project wall by its one rule (audit of 17 Oct 2026: it was
            # written out here by hand, a copy that could drift from it).
            # And the end user's by its one rule (services/user_scope.wall):
            # the turns' own end user, None the rows of none.
            between = within_end_users(within_project(db.query(AgentMemory.id),
                                                      AgentMemory.project_id, project_id),
                                       AgentMemory.end_user_id, end_user_id).filter(
                AgentMemory.tenant_id == tenant_id,
                AgentMemory.deleted_at.is_(None),
                AgentMemory.created_at > a.created_at, AgentMemory.created_at < b.created_at,
            ).limit(1).first()
            if between is None:
                group[root(str(b.id))] = root(str(a.id))

    def said(h):
        r = rows[str(h.get("id"))]
        return (r.event_time or r.created_at, r.created_at)

    out, placed = [], set()
    for h in head:
        g = root(str(h.get("id")))
        if g in placed:
            continue
        placed.add(g)
        out.extend(sorted((x for x in head if root(str(x.get("id"))) == g), key=said))
    return out + hits[keep:]


# A memory whose facts were all superseded, kept as history (bench-only until
# measured, 2026-10-08; memory_core 3.3.0 `memories_with_their_state`; the
# hosted product had agent_context._SUPERSEDED_AS_HISTORY before it ran
# this code). The block leaves such
# a memory out (rule (b) since memory_core 2.3.0, the 29 Sep conformance
# case: "Maria lives in Rome" under the current truth next to Milan). A WRONG
# closure then hides the memory with the evidence too, and one error becomes
# two: in an analysis of the full LoCoMo blocks it hid the evidence of
# 3 wrong answers (conv-49:104: the evidence ranked first, its photo caption
# the gold, hidden because "on_diet diet" was superseded later). On: the
# memory stays, its line marked "(true when said, changed later)" after its
# day (`block_line(superseded=True)`), as Graphiti keeps an invalidated
# edge readable with its end (Rasmussen et al. 2025, 2.2.3). Measured on the
# confirmation store (LoCoMo, 5 conversations): 613 against 608 (11 won, 6
# lost). On since 0.1.23 (8 Oct 2026). Off: left out.
SUPERSEDED_AS_HISTORY = True


# The wider and more varied pool of memories was the experiment WIDE_POOL
# here (2026-10-07). Measured on 12 Oct 2026 it became the product's rule:
# the search's pool follows the block's budget (services/context.py,
# `_pool_for`), with the memories past the first whole ones by maximal
# marginal relevance (services/mmr.py).


# Diversity (DIVERSITY, 2026-10-05: one version of each near-identical fact
# and line) was removed on 12 Oct 2026: the free
# screen of every block of the whole LoCoMo store lost evidence with it (1,373
# questions with all their evidence in the block against 1,377), after a
# LongMemEval read within the noise (151 against 147).


# Plans capped in the facts section (PLAN_CAP, 2026-10-05: at most three
# planned facts per block, the next facts of the ranking in the places freed)
# was removed on 12 Oct 2026 (measure it or drop it): on the 91 LongMemEval
# questions whose block it changed, three reads
# per arm with gpt-oss-120b reading and judging, 68.0 answers against 68.7
# without it (5 questions better, 8 worse, p 0.58); knowledge-update 11.7
# against 10.3, but counting 10.3 against 10.7 and temporal 11.3 against 12.7
# LoCoMo could not decide it: the evidence in the
# block, 1,376 questions against 1,377.


# Recency for the memories (MEMORY_RECENCY, 2026-10-05) was removed on 12 Oct
# 2026: past ~120 days every memory sat at the weight's
# floor and the block was the same, so no bench we have could measure it, and
# the facts already weigh their age.


# Standing instructions (STANDING_INSTRUCTIONS, 2026-10-05: the end user's
# rules, the facts of the family `instructions` that memory_core's
# `istruzioni` wrote, at the head of `stable` whatever the question) were
# removed on 12 Oct 2026, with the writer's rule and the family: on a
# procedural bench (26 conversations, 28 probes,
# gpt-oss-120b writing, reading and judging) 22 probes passed against 21 as
# shipped (3 won, 2 lost), 25 with the gold rules: a tie inside the bench's
# noise.
