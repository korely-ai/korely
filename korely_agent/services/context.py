"""GET /v1/context assembly — the one-call prompt-ready context block.

Deterministic retrieval + formatting (NO generation): pull the end user's
known facts (the stable profile) + the memories most relevant to the query,
pack them into a Markdown block within a token budget, return the string plus
the source ids. This is the endpoint most agent frameworks drop straight into a
system prompt.

The block comes in two parts since 2026-10-01 (the same on the hosted product,
services/agent_context.py):

    {reader note}                         ] `stable`: fixed text
                                          ┐ `volatile`: what this question
    ## Known facts                        │ brings
    - {the facts most relevant to it}     │
                                          │
    ## Relevant memories                  │
    - {memories ranked for the question}  │
                                          │
    {a part's section, when one is there} │
                                          │
    NOTE: {what could not be retrieved}   ┘

the facts chosen as before 2026-10-01 (the _FACTS_LIMIT most relevant to the
question); only the NOTE moved, from the head to the tail. A profile-first
block (the facts about the end user in a fixed order, in `stable`, agreed with
the hosted product on 2026-10-01) brought the reader fewer pieces of the
answer on LongMemEval (0.728 against 0.797 at 2,000 tokens; capped at an
eighth of the budget, 44 against 46 read; measured 2026-10-01). It stayed off,
and on 2026-10-06 it was removed with its setting, CONTEXT_PROFILE.

The end user's standing instructions in `stable`, whatever the question
(STANDING_INSTRUCTIONS, 2026-10-05), were measured on 12 Oct 2026 and
removed: 22 of 28 probes against 21, a tie.

`context` is the two joined by a blank line. A client that keeps `stable` at
the head of its system prompt and puts `volatile` in the turn sends the same
prefix on every call, and the providers' prompt caches reuse only an
identical prefix (OpenAI: "cache hits are only possible for exact prefix
matches"; Anthropic: the prefix up to a cache_control breakpoint; Gemini
implicit caching: common content at the beginning of the prompt). Nothing in
`stable` is per call: no date, no timing, no echo of the query, no count that
depends on it.

Budget split: memories (the verbatim source, highest retrieval recall) get the
larger share; the facts (the top _FACTS_LIMIT ranked for the question; with
the profile on, the profile first) share half of what the block has left
after the reader note and the NOTE, all of it when no memory follows them. A
short reader-trust note leads the block so the consuming LLM falls back to
the memories when a fact does not directly answer the question. Token
estimate is char/4 (the coarse heuristic the human RAG path uses — exact
tokenization isn't worth the dependency here), counted on every character
the block holds, headings and newlines included.

A part of the product may add one section after the memories (1.0.19,
`Section`, through services/memory_search.MemorySearch.in_the_block; the
core has none), in the room the memories leave and in at most a quarter of
the block's budget (`PART_SHARE`): every section before it, the memories
included, is the one of a block without it, byte for byte, and its lines
never push a memory out. Without a part, or with one that finds nothing,
the block is the one of before, byte for byte.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional
from uuid import UUID

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core.llm import (
    embed,
    missing_vector_level,
    TASK_QUERY,
)
from ..db.models import AgentFact, AgentMemory
from .ids import fact_public_id, mem_public_id
from .memory_engine import AgentMemoryEngine, MemorySearchUnavailable
from .memory_core import log_safe
from .memory_core.vector_search import hnsw_depth
from .sentinels import EVERY_END_USER, EVERY_PROJECT, within_project
from . import graphemes, limits, user_scope
from . import context_experiments as experiments
from .facts.temporal import end_learned, true_then
from .memory_core.presentation import (POOL, rank_with_recency, context_line, block_line,
                                       in_its_line)

logger = logging.getLogger(__name__)

# Memories are the verbatim source of truth (highest retrieval recall), so they
# get the larger share of the budget; the facts block is a compact PROFILE on top.
_FACTS_BUDGET_FRACTION = 0.5

# Precision over recall in the facts block (Liu 2024, "Lost in the Middle":
# irrelevant context degrades an LLM answer, and a long unranked list buries the
# relevant rows). The facts are already cosine-ranked by query relevance in
# _rank_facts; we keep only the top few rather than the whole profile, so an
# off-topic fact can't crowd the block or mislead the reader. Was 30 (≈ every
# fact a single conversation produces → no effective relevance filter at all).
_FACTS_LIMIT = 10

# Reader-trust calibration (Asai 2023, SELF-RAG: answer from the SUPPORTED source;
# do not abstain when support is present). Emitted for EVERY non-empty context now
# (not only when typed facts exist) — the benchmark-validated fix for the single-
# session-preference collapse (LongMemEval 2026-06-17): recommendation questions
# ("recommend / suggest / what should I") arrive with the user's relevant preference
# verbatim in the memories, but a strict factual reader treats them as an external
# lookup and replies "I don't know" (the preference context carries NO typed facts,
# so previously it got NO guidance note at all). This note tells the reader the
# user's stated preferences ARE the answer to surface, and to abstain only when the
# information is genuinely absent. GENERAL (no answer leak). Measured: preference
# 25% -> 45-60% (Groq/Gemini judges) on the saved transcripts; fresh-run validated.
# Keeps the facts-vs-memories fallback so the facts-present axes (knowledge-update)
# don't regress.
_READER_GUIDANCE = (
    "_The facts below are a compact profile of the user; the memories are the verbatim "
    "source of truth. Ground your answer in this context. If a fact does not directly "
    "answer the question, rely on the memories. If the question asks for a recommendation, "
    "a suggestion or a decision, the user's stated preferences, interests, habits and "
    "constraints ARE the answer: apply them concretely to what was asked, and when a "
    "preference concerns a related but different item, place or category, extrapolate from "
    "it instead of replying that you don't know. If the question asks for a fact, answer it "
    "from what is stated. Reply that you don't know only if the needed information is "
    "genuinely absent above; do not abstain when it is present._"
)
# Not in the note, measured 2026-10-05: a sentence saying the day in brackets is
# when a memory was said and relative times count from it. LoCoMo conversation
# 26: 119 against 121 of 152 (temporal 24 to 27, multi-hop 22 to 18; 4 won, 6
# lost, p = 0.75). The lasting fix is in the write: memory_core 2.8.0 resolves
# relative dates in the observations.
# The hybrid block is memory_core's since 2.8.0, the same rule on both
# Agents products: `presentation.block_line` shows the first WHOLE (5)
# memories of the search whole and the rest through `memory_line`, every line
# under the day it was said, read from the store (event_time, else
# created_at), never from the text. Measured here first: the hybrid block
# 2026-10-02 (LongMemEval, 50/60 against 46 all whole and 36-39 all short),
# the day on the whole ones
# 2026-10-05 (LoCoMo conversation 26, 107 to 121 of 152, p = 0.004). Our
# LongMemEval corpus of June carries the day inside its texts too, so there
# it reads twice until the corpus is written again (with the turn alone).

# How many memories the search brings to the packer: enough to fill the block
# (12 Oct 2026). memory_core's POOL (40, since 2.8.0)
# fills about 2,000 tokens: a memory line takes 30 tokens at the median and
# 41 on average (the blocks of the whole LoCoMo store, 3,080 of them), so a
# 4,000-token block shown 40 memories stayed half empty (51% of its budget at
# the median) while the evidence of list questions sat past the 40th place.
# One memory for every TOKENS_PER_MEMORY of budget, never fewer than POOL and
# at most WIDEST_POOL, the size measured: at 4,000 tokens a person, 80
# memories, the ones past the first whole ones ordered by maximal marginal
# relevance (services/mmr.py), gave +15 of 282 multi-hop questions (23 won, 8
# lost, p 0.011); at 2,000 and below the search and the block are those of
# before, byte for byte.
TOKENS_PER_MEMORY = 50
WIDEST_POOL = 80

# The block's budget, in tokens: the default and the bounds every door takes
# (/v1/context, the MCP tool, `assemble_context` itself, which clamps to
# them). Written in one place since the audit of 17 Oct 2026: the
# same 800, 50 and 8000 stood in four, and a bound moved in one of them
# would have answered differently by REST and by MCP.
DEFAULT_TOKEN_BUDGET = 800
MIN_TOKEN_BUDGET = 50
MAX_TOKEN_BUDGET = 8000

# UTF-8 bytes per token, the estimate the block is packed and measured with
# (`_estimate_tokens`, `_Packer`, `_truncate_to_budget`, all through `_cost`).
# Until 18 Oct 2026 it was four characters per token, a Latin-alphabet figure:
# a block in Chinese, Japanese or Korean passed its budget by up to three times
# (the audit of 17 Oct 2026). Measured on the Wikipedia article "Water"
# in 16 languages with o200k_base, gpt-oss's tokenizer (criterion written
# before): real tokens over the estimate, at
# worst 2.95 (zh) with characters and 1.20 with bytes; English, Italian,
# German, Spanish within 3 points of before; the lowest 0.52 (Hindi), a block
# half full. Bytes are what every script pays more for, with no table of
# alphabets.
BYTES_PER_TOKEN = 4


def _cost(text: str) -> int:
    """What a piece of the block weighs against the budget: its UTF-8 bytes."""
    return len(text.encode("utf-8")) if text else 0


def _pool_for(budget: int) -> int:
    """How many memories the block's search asks for, for a budget in tokens."""
    return max(POOL, min(WIDEST_POOL, budget // TOKENS_PER_MEMORY))


def _facts_of(tenant_id: UUID, end_user_id: Optional[str]):
    """The `scope` of memory_core's `store.states_of_the_sources` (2.8.0,
    BUG-367): the facts this block's memories can have, the tenant's, and the
    end user's when the call names one. A fact is born from a memory of its
    own end user and reconfirmed within that user's triples, so no fact
    outside can count, and the one query of the states stays on the index of
    the end user instead of the whole table."""
    from sqlalchemy import and_

    return and_(AgentFact.tenant_id == tenant_id,
                user_scope.wall(AgentFact.end_user_id, end_users_of(end_user_id)))


def end_users_of(end_user_id):
    """What the block reads its end users on (services/user_scope.wall): a
    call that names no end user reads every one of them, the rows of none
    included, so its None is said to the wall by name, EVERY_END_USER. The
    facts and the memories of one block read the same end users."""
    return EVERY_END_USER if end_user_id is None else end_user_id


# Below this budget the note isn't worth its ~tokens — emit content instead. Set
# above the note's own size (723 bytes, 180 tokens by `_cost`) + a content
# margin: at a tiny budget the note would crowd out the actual answer-bearing
# content, so we suppress it and let content win (Liu 2024 — content over
# meta-instruction when space is scarce). From here up, at every budget, the
# note's 723 bytes, separator included, are paid by both shares
# (`_Block.facts_limit`): with memories to follow, the facts may take half of
# what the note leaves, about 360 bytes more than half of the budget less the
# note (the share until 3 Nov 2026), and the memories that much less. At 400
# tokens the note takes 45% of the block and the facts may take 437 of the 875
# bytes left; at 800 (the default), 1,237 of 2,475. The split moves only when
# the facts are longer than the old share: ten facts of 130 bytes are, at 800;
# at the benchmarks' default blocks (2,000 tokens for each LoCoMo speaker,
# 4,000 for LongMemEval) the _FACTS_LIMIT ten fit in the old share unless
# their lines average more than about 330 and 730 bytes.
_GUIDANCE_MIN_BUDGET = 400

# The recommendation clause (QA-uplift 2026-06-17: "instance-transfer
# abstention", the reader answering "I don't know" when a preference concerns
# a related but different item) is part of the note and CONDITIONAL: the
# reader decides whether the question asks for a recommendation, in whatever
# language it is written. Until 2026-09-27 an English regex
# (recommend|suggest|advice|what should i ...) decided it, so the clause never
# reached a question in any other language.
# Measured 2026-09-27 on the saved LongMemEval contexts (same reader
# gemini-2.5-flash and judge, only the note changes): factual axes
# 264/316 with the regex vs 265/316 with the conditional clause; preference in
# English 53/100 vs 51/100 (no clause at all: 40/100); the same questions in
# Italian and Japanese show no significant difference between any variant
# (per-question sign counts 5-3 and 5-4).


def _estimate_tokens(text: str) -> int:
    return _cost(text) // BYTES_PER_TOKEN


_MIN_FRAGMENT_CHARS = 40  # ~10 tokens; below this a truncated fragment isn't worth keeping


def _truncate_to_budget(text: str, token_budget: int) -> str:
    """Longest prefix of `text` that fits `token_budget`, cut back to the last
    sentence boundary (``. ! ?`` or newline), falling back to a word boundary.
    Returns ``''`` when there's no room for a meaningful fragment.

    Greedy budget-packing interim for chunk-granular retrieval (Anthropic
    Contextual Retrieval, 2024): when the most-relevant memory is larger than the
    remaining budget, contribute its head (which usually holds the answer)
    instead of dropping it wholesale — the original ``break`` bug that left
    "Relevant memories" empty even when the answer was inside the top hit."""
    max_bytes = token_budget * BYTES_PER_TOKEN - 2  # leave room for the "- " line prefix
    if max_bytes < _MIN_FRAGMENT_CHARS:
        return ""
    if _cost(text) <= max_bytes:
        return text
    window = prefix_within(text, max_bytes)
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "),
              window.rfind(".\n"), window.rfind("\n"))
    if cut >= _MIN_FRAGMENT_CHARS:
        return window[:cut + 1].rstrip()
    # A fragment that ends with the ellipsis pays for it from the same bytes:
    # paid on top, it passed the room by 3 bytes (audit of 19 Oct 2026), and the
    # memory it was cut from left the block wholesale, the bug this function
    # exists for, for any text with no space in the window.
    window = prefix_within(text, max_bytes - _cost(_ELLIPSIS))
    sp = window.rfind(" ")
    if sp >= _MIN_FRAGMENT_CHARS:
        return window[:sp].rstrip() + _ELLIPSIS
    # Nothing of the text in the window (one character, as a reader sees it,
    # longer than the room): no fragment, rather than an ellipsis alone.
    return window.rstrip() + _ELLIPSIS if window.rstrip() else ""


_ELLIPSIS = "…"


def prefix_within(text: str, max_bytes: int) -> str:
    """The longest prefix of `text` within `max_bytes` UTF-8 bytes, cut
    between two characters as a reader sees them (services/graphemes.py,
    Unicode's extended grapheme clusters): never inside an emoji of joined
    code points, a flag, or a letter and its marks. Until 1.0.15 it was cut
    between two code points, and could end a piece with half of one."""
    head = text.encode("utf-8")[:max(0, max_bytes)].decode("utf-8", errors="ignore")
    return text[:graphemes.cut_back(text, len(head))]


# The parts of the context that can come back degraded, as `degraded_parts`
# names them, and what the block says to the model reading it about each. The
# same words as the hosted product (services/agent_context.py); both copies
# belong in memory_core.presentation. The NOTE closes the block
# since 2026-10-01 (it is per call, so it stays out of the stable head), hence
# "above". The profile does not depend on the question, so a missing query
# vector never touches it: only the facts ranked for the question fall back to
# recency.
FACTS, MEMORIES = "facts", "memories"
_WHAT_WENT_MISSING = {
    FACTS: ("the known facts could not be ranked against the question, so the "
            "facts listed for it above are the most recently confirmed ones and a "
            "fact that answers it may be missing"),
    MEMORIES: ("the stored memories could not be retrieved, so the block above "
               "holds no verbatim memory"),
}

# The headings of the block (see the module docstring). Shared word for word
# with the hosted product: the two hand the same block to the same models.
# The default block keeps the two headings of before 2026-10-01.
KNOWN_FACTS_HEADING = "## Known facts"
MEMORIES_HEADING = "## Relevant memories"
# Agent notices (1.0.15): the facts the reader was handed and that changed
# since, at the head of `volatile`, only for a read that names its reader
# (`reader`), after the known facts in the packing and out of the memories'
# room (`_changes_in_the_memories_room`), at most CHANGES_IN_THE_BLOCK lines,
# chosen newest first among the CHANGES_LOOKED_AT that wait; the rest are
# counted (`changed_more`). Without a reader the block is the one of before,
# byte for byte, and with one its known facts and `stable` are the same.
CHANGED_HEADING = "## Changed since you last read"
CHANGES_IN_THE_BLOCK = 3
CHANGES_LOOKED_AT = 20

# Under the memories' heading, when the block shows memories (on since 7 Oct
# 2026, the same line and place as the hosted product): a relative
# time carries its date in brackets ("next month [June 2023]", memory_core
# 2.9.0), computed from the day the memory was written, and a reader that
# recomputes it from today's date answers wrong. Measured on LoCoMo's 321
# temporal questions: a gpt-4o-mini reader 264 against 255 (p 0.049),
# gpt-oss 234 against 233 (neutral). Under the heading and not in the note:
# the note is the cached `stable` part, and a block with no memory lines (the
# search down, a small budget) needs that room for its facts.
_RESOLVED_DATES_NOTE = True
RESOLVED_DATES_LINE = (
    '_A time in square brackets after a relative expression, as in "next month [June 2023]", '
    "was already resolved from the day that memory was written: use it as written and never "
    "recompute it from the current date. The date at the start of a memory, as in "
    "[2023-05-25], is the day it was said._"
)


def _memories_heading() -> str:
    """The memories' heading, with the line on the dates under it."""
    return MEMORIES_HEADING + ("\n" + RESOLVED_DATES_LINE if _RESOLVED_DATES_NOTE else "")
_SEPARATOR = "\n\n"


_WHAT_WENT_MISSING_SHORT = {FACTS: "facts by recency, not by the question", MEMORIES: "no memories"}


def _degraded_note(parts: List[str], budget: int) -> str:
    """What the block says about the parts that are missing. The full sentence
    when it takes at most 40% of the budget, a short one otherwise: the note is
    counted in the budget like everything else (it was prepended after the
    packing, and a 60-token budget came back with 90)."""
    if not parts:
        return ""
    full = ("NOTE: the retrieval service is unavailable for this request: "
            + "; ".join(_WHAT_WENT_MISSING[p] for p in parts)
            + ". Treat the absence of detail as missing, not as absent from the record.")
    if _estimate_tokens(full) <= budget * 0.4:
        return full
    return "NOTE: partial context (" + "; ".join(_WHAT_WENT_MISSING_SHORT[p] for p in parts) + ")."


def facts_base(db: Session, tenant_id: UUID, end_user_id: Optional[str],
               agent_id: Optional[str], project_id: Any = EVERY_PROJECT,
               known_as_of: Optional[datetime] = None, exclude_sessions: bool = False):
    """The facts true now that the context may show: the tenant, the key's
    project, the end user and the agent when given. One wall for every fact
    the block shows, and for every fact a search finds (services/facts/
    search.py, 1.0.13). With `known_as_of`, the facts true then as the
    store knew them then (facts/temporal.py)."""
    base = within_project(db.query(AgentFact).filter(AgentFact.tenant_id == tenant_id,
                                                     true_then(known_as_of)),
                          AgentFact.project_id, project_id)
    base = user_scope.within_end_users(base, AgentFact.end_user_id, end_users_of(end_user_id))
    if agent_id is not None:
        base = base.filter(AgentFact.agent_id == agent_id)
    if exclude_sessions:
        # Only the facts some note states (AgentFact.session_only, 18 Oct 2026).
        base = base.filter(AgentFact.session_only.is_(False))
    return base


# The subject type of a fact about the end user itself. Extraction never gives
# it (memory_core.fact_extractor.ALLOWED_SUBJECT_TYPES); a new fact written
# with POST /v1/facts carries it when its writer sends it, and a POST that
# restates a fact already true keeps the type that fact had. The facts'
# OpenAPI description says so (api/facts.py), read from here.
SELF_SUBJECT_TYPE = "self"


def is_about_the_end_user(f: Any, end_user_id: str) -> bool:
    """The rule of GET /v1/profile for "a fact about this end user" (on both
    products): typed `self`, or its subject IS the end user's id, compared
    stripped and case-folded the same way, as written or brought to it by an
    alias (a name the end user declared: the audit of 19 Oct 2026). The
    block's rule (memory_core.presentation._is_the_user) is not this one: it
    reads the subject alone, never the type, and the handle as written, not
    case-folded (review of 4 Nov 2026)."""
    uid = (end_user_id or "").strip().lower()
    return (f.subject_type == SELF_SUBJECT_TYPE or (f.subject_label or "").strip().lower() == uid
            or (getattr(f, "subject_canonical", None) or "").strip().lower() == uid)


def _query_vector(
    tenant_id: UUID,
    agent_id: Optional[str],
    query: str,
) -> tuple:
    """(the vector of the question, whether it is missing): the ONE embedding
    call of a /v1/context request, read by the facts ranked for the question
    and by the memory search (BUG-299 on the hosted product, 2026-10-01: each
    part made its own call, two per request for the same text). An empty
    question has no vector and nothing is missing. The text is the stripped
    one, as the memory search always embedded it. `embed` returns None both
    when no provider is configured and when the call failed, and either way
    neither part can be ranked by the question."""
    q = (query or "").strip()
    if not q:
        return None, False
    from . import llm_usage

    try:
        with llm_usage.attributed(tenant_id, agent_id=agent_id):
            vector = embed(q, task=TASK_QUERY)
    except Exception as e:
        logger.warning("[agent_context] query vector failed for %s: %s", tenant_id, log_safe.stack(e))
        return None, True
    if not vector:
        logger.warning("[agent_context] no query vector for %s", tenant_id)
        return None, True
    return vector, False


def _rank_facts(
    db: Session,
    tenant_id: UUID,
    end_user_id: Optional[str],
    agent_id: Optional[str],
    query: str,
    limit: int,
    project_id: Any = EVERY_PROJECT,
    *,
    query_vector: Optional[List[float]] = None,
    known_as_of: Optional[datetime] = None,
    exclude_sessions: bool = False,
) -> tuple[List[AgentFact], bool]:
    """The facts most relevant to the query, and whether they are.

    `query_vector` is the question's vector (`_query_vector`, computed once
    for the request). The second value is False when there is none (no
    embedder, a failed call) or the ranking query failed, and the facts
    returned are the most recently confirmed ones instead. That fallback used
    to be silent: `degraded` stayed false, and a reply ranked by date read
    like one ranked by the question, so the model reading it took "no
    relevant fact" for an answer (2026-09-28). A fallback that returns
    nothing has lost nothing and is not reported."""
    base = facts_base(db, tenant_id, end_user_id, agent_id, project_id, known_as_of,
                      exclude_sessions)
    # The moment the recency weight reads: then, for a block as it was then.
    moment = known_as_of or datetime.now(timezone.utc)

    ranked_by_query = False
    try:
        if query_vector:
            ranked_by_query = True
            # Relevance, then recency (2026-09-27, memory_core.presentation —
            # the same ranking as the hosted product): pulled wider than
            # `limit` so a fresh fact at rank 25 by cosine can overtake a
            # stale one at rank 3.
            dist = AgentFact.embedding.cosine_distance(query_vector).label("d")
            pool = max(limit * 3, 30)
            # In a savepoint (BUG-300 on the hosted product, 2026-10-01): a
            # failed statement (a vector of another size, a statement
            # timeout) aborts the transaction on Postgres. The savepoint rolls
            # back that statement alone and the request's transaction goes
            # on; this used to roll back the whole of it.
            with db.begin_nested():
                # idx_agent_facts_embedding_hnsw holds every tenant's and end
                # user's facts, and the wall of `facts_base` applies after
                # the scan: the index is let bring the whole pool (BUG-320 on
                # the hosted product, 2026-10-01).
                hnsw_depth(db, pool)
                ranked = (
                    base.filter(AgentFact.embedding.isnot(None))
                    .add_columns(dist)
                    .order_by(dist, AgentFact.id)
                    .limit(pool)
                    .all()
                )
            if ranked:
                ordered = rank_with_recency(ranked, moment)
                return ordered[:limit], True
        else:
            logger.warning("[agent_context] no query vector for %s: facts by recency",
                           tenant_id)
    except Exception as e:
        ranked_by_query = False
        logger.warning("[agent_context] fact ranking failed for %s, facts by recency: %s",
                       tenant_id, log_safe.error(e))

    # Fallback: the most recently confirmed active facts. With a query vector
    # and no fact holding one yet (a vector still being retried), this is the
    # right answer rather than a degraded one: the query was understood.
    rows = (
        base.order_by(func.coalesce(AgentFact.last_confirmed_at, AgentFact.valid_from).desc(),
                      AgentFact.id)
        .limit(limit)
        .all()
    )
    return rows, ranked_by_query or not rows


class _Packer:
    """The block's characters as they are packed, each counted at the length
    it will have once joined: a part costs its text plus the blank line that
    separates it, a section its heading, a line its newline. `tokens` is
    _cost(context) // BYTES_PER_TOKEN, so keeping the count under
    BYTES_PER_TOKEN x budget keeps `tokens` under the budget. Until 2026-10-01 only the lines were counted (in
    tokens, each rounded down), never the headings and the newlines."""

    def __init__(self, budget: int):
        self.cap = budget * BYTES_PER_TOKEN
        self.used = 0

    def room(self) -> int:
        return self.cap - self.used

    def take(self, chars: int, limit: Optional[int] = None) -> bool:
        """Count `chars` if they fit under `limit` (default the whole budget)."""
        if self.used + chars > (self.cap if limit is None else limit):
            return False
        self.used += chars
        return True


def _section(heading: str, lines: List[str]) -> str:
    return heading + "".join("\n" + line for line in lines)


# ── a part's section (1.0.19) ───────────────────────────────────────────────

@dataclass(frozen=True)
class Entry:
    """One line a part's section may show: `label`, where it comes from,
    never cut, then `text`, whole or cut as a memory too long for its room
    is (`_truncate_to_budget`), each kept in its line (`in_its_line`); `id`,
    the part's own, handed back to it with the entries the block shows
    (`Section.answer`)."""
    label: str
    text: str
    id: Any


@dataclass(frozen=True)
class SectionRead:
    """What a part's section is read with: the walls of the block's
    memories (the account, the key's project, the end users the block reads,
    `end_users_of`), the key's own list of end users (None for a key of the
    whole project, services/user_scope), the agent, and the question's one
    vector (`_query_vector`), None when it is missing."""
    tenant_id: UUID
    project_id: Any
    end_user_id: Any
    end_users: Optional[list]
    agent_id: Optional[str]
    query_vector: Optional[List[float]]


#: The most of the block's budget a part's section takes, as a fraction's
#: denominator: a quarter, after the memories and in the room they leave
#: (the founder's decision of 12 Nov 2026). An earlier frame, a part's lines
#: ranked with the memories on one scale, was measured that day with four
#: rules and none kept the lines out of the questions about the memory
#: alone; this one never takes a memory's place.
PART_SHARE = 4


class Section:
    """A section a part of the product adds to the block, after the
    memories (the module's docstring), through
    services/memory_search.MemorySearch.in_the_block. The defaults add
    nothing: a section that finds no entry leaves the block as it was."""

    #: Its name in `degraded_parts`, when it could not be read.
    name = ""
    #: Its heading, with what the block says under it, before its lines.
    heading = ""
    #: What the NOTE says when it could not be read: (in full, short).
    missing = ("", "")

    def find(self, db: Session, read: SectionRead) -> List[Entry]:
        """Its entries for the question, best first, behind the read's
        walls. Raises when they could not be read: the block then has the
        rest, and its NOTE says this part is missing (`missing`). Asked in a
        savepoint, never for a block of a past moment (`known_as_of`)."""
        return []

    def answer(self, shown: List[Entry]) -> dict:
        """The fields this part adds to the block's answer for `shown`, the
        entries the block shows, in its order."""
        return {}

    def reads(self, out: dict) -> dict:
        """What the trail keeps of what `out` (the block's answer) showed of
        this part: keyword arguments of services/usage.log_v1_request."""
        return {}


def assemble_context(
    db: Session,
    tenant_id: UUID,
    *,
    query: str,
    end_user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    include_facts: bool = True,
    project_id: Any = EVERY_PROJECT,
    known_as_of: Optional[datetime] = None,
    exclude_sessions: bool = False,
    changes: Any = None,
    section: Optional[Section] = None,
    end_users: Optional[list] = None,
) -> dict:
    """The block, split as the module docstring draws it: `stable` does not
    depend on the question (the reader note), `volatile` (what the question
    brings, and the NOTE on what could not be retrieved) does. `context` is `stable` and `volatile` joined by a
    blank line, and `stable_hash` the SHA-256 of `stable`, so a client can
    tell a cached prefix from a changed one without comparing texts.

    Packing order: the reader note (reserved whenever the budget can afford
    it); then the NOTE about a degraded part, the facts ranked for the
    question (top _FACTS_LIMIT, in the facts' share, up to half of what the
    two notes leave, all of it when no memory follows them), then the
    memories (the rest), under the two headings of before 2026-10-01.

    `include_facts=False` returns memories only, the whole budget spent on
    verbatim text. It exists so the facts block can be
    ablated and measured rather than assumed: extraction costs a model call per
    write, and a layer that costs money on every write should be able to show
    what it buys.

    **What it buys, measured** (57 support-desk memories over six months, 43
    questions derived from the timelines rather than invented afterwards, one
    store, one moment, this knob the only difference):

                             ANSWERED           ANSWER WAS IN THE CONTEXT
                          facts   memories      facts   memories
        CURRENT_CHANGED   10/10      6/10       10/10     10/10
        HISTORICAL         6/7       0/7         7/7       7/7
        CURRENT_STABLE    11/12     11/12       12/12     12/12
        FREE_TEXT         14/14     11/14       14/14     14/14
        ALL               41/43     28/43       43/43     43/43

    Thirteen disagreements, all thirteen won by the facts arm.

    The right-hand column is the point. The correct answer was in the context 43
    times out of 43 in BOTH arms, so this is not a retrieval problem. In raw
    text "upgraded to Business" and "moved back to Starter" are equally present
    and equally plausible, and nothing says which one still holds. The
    memories-only reader guessed, or answered UNKNOWN with the sentence in front
    of it. The memories arm was not starved either: it receives the same number
    of memories (6 -> 6, 5 -> 5), the user's complete record.

    FREE_TEXT was the category where facts were predicted to lose, and did not.

    The limit, because it matters: one corpus, written here, containing change
    over time by construction, since that is the case under test. Where nothing
    ever changes, facts would add little and this does not show otherwise.

    `known_as_of` (11 Oct 2026): the block as the store could have built it
    then, "what did the agent know when it decided": the facts learned by
    then, true then as the store knew them then, and the memories received by
    then. Rebuilt with today's ranking, from what is stored today: a memory
    edited since shows today's text, and a memory deleted since stays out.

    `section`: a part's (`Section`, the module's docstring), read with the
    block's walls and `end_users`, the key's own list (None for a key of the
    whole project); never with `known_as_of`.
    """
    budget = max(MIN_TOKEN_BUDGET, min(int(token_budget or DEFAULT_TOKEN_BUDGET), MAX_TOKEN_BUDGET))
    found = _retrieve(db, tenant_id, query=query, end_user_id=end_user_id, agent_id=agent_id,
                      include_facts=include_facts, project_id=project_id,
                      known_as_of=known_as_of, pool=_pool_for(budget),
                      exclude_sessions=exclude_sessions)
    note = _degraded_note(found.degraded_parts, budget)

    block = _Block(budget)
    # Reserve the reader note whenever the budget can afford it. It is emitted
    # for ANY non-empty context (facts OR memories: the memory-only case IS
    # the single-session-preference failure, no typed facts, no grounding,
    # over-abstention), so `stable` does not depend on the question.
    want_guidance = budget >= _GUIDANCE_MIN_BUDGET
    if want_guidance:
        block.pack.take(_cost(_READER_GUIDANCE) + _cost(_SEPARATOR))

    # Per call from here on. The NOTE closes the block; its room comes first.
    # It always fits (before it, at most half of the budget; the long note is
    # at most 40% of it, the short one a few words): the guard only keeps the
    # block under the budget whatever the shares become.
    if note and not block.pack.take(_cost(note) + _cost(_SEPARATOR)):
        note = ""
    # Which memories go in is decided before the facts are packed: the facts'
    # share keeps room for them, and only for the ones the block will show.
    shown = _memories_for_the_block(db, found.hits, tenant_id, end_user_id, known_as_of)
    facts_share = block.facts_limit(memories_follow=bool(shown))
    fact_lines = _pack_facts(block, found.facts, end_user_id, facts_share, known_as_of)
    # `changes` (agent notices, services/facts/changes.ForARead): the
    # reader's, after the known facts and out of the memories' room
    # (`_changes_in_the_memories_room`); None: no reader, and nothing of it.
    # A read that failed (a part of it degraded, and neither facts nor
    # memories to show) tells none: it is no memory read, not even an empty
    # one (the audit of 6 Oct 2026), and its block must not look like one
    # that worked; the changes wait for the next read (review of piece 3,
    # 9 Nov 2026: a block of changes alone came without its NOTE, and the
    # MCP answered it as a success).
    failed = bool(found.degraded_parts) and not fact_lines and not shown
    told: List[dict] = []
    lines = _memory_lines(db, found.hits, shown)
    if changes is not None and not failed:
        change_lines, packed = _changes_in_the_memories_room(
            block, changes.pending, lines, further=changes.look_further,
            waiting=changes.waiting)
        # Reserved before the block is written, and only what this answer
        # reserved is in it: two answers at once never tell one change
        # twice (review of piece 2, 8 Nov 2026). A line left out only makes
        # the block shorter than its budget.
        told = changes.reserve(packed)
        if len(told) < len(packed):
            # The lines as packed (some written short), of the ones reserved.
            kept = {c["fact_id"] for c in told}
            change_lines = [line for line, c in zip(change_lines, packed) if c["fact_id"] in kept]
    else:
        change_lines = []
    mem_lines = _pack_memory_lines(block, lines)
    # A part's section, after the memories, in the room they leave
    # (`_part_after_the_memories`): asked only now, and only when its share
    # has room; a part that fails takes no room from the memories, nor counts
    # as a failed read of the memory above.
    part = _part_after_the_memories(db, block, section, SectionRead(
        tenant_id, project_id, end_users_of(end_user_id), end_users, agent_id, found.vector),
        asked=known_as_of is None and (found.vector is not None or found.vector_missing),
        vector_missing=found.vector_missing)

    out = _render(block, found.degraded_parts + part.degraded, note, guidance=want_guidance,
                  facts=fact_lines, memories=mem_lines, changes=change_lines, part=part.shown)
    if section is not None:
        out.update(section.answer(part.entries))
    if changes is not None:
        out["changed"] = changes.shown
        # What the section shows is read too, by id, at the head of the
        # block: the fact it had and the fact that holds now, which the
        # doors keep in the audit trail and mark for this reader, so a
        # change of what it was told holds now is told in its turn (review
        # of piece 3: "now" was neither marked nor recorded).
        out["sources"] = list(dict.fromkeys(_ids_of(told) + out["sources"]))
    return out


def memory_unread(out: dict) -> bool:
    """Whether the block's read of the memory failed (`degraded_parts` holds
    the facts or the memories): a part's section that could not be read
    leaves the memory's read a success (`_part_after_the_memories`)."""
    return any(p in (FACTS, MEMORIES) for p in out.get("degraded_parts") or [])


def _ids_of(changes: List[dict]) -> List[str]:
    """The `fct_` ids a section of changes shows, in its order: what each
    had, then what holds now."""
    out: List[str] = []
    for c in changes:
        out.append(c["fact_id"])
        if c.get("now"):
            out.append(c["now"]["fact_id"])
    return out


def _changes_in_the_memories_room(block: "_Block", changes: List[dict], lines: list,
                                  further: Optional[Callable[[], List[dict]]] = None,
                                  waiting: Optional[int] = None):
    """(the changes' lines, the changes packed): the changes after the
    known facts, before the memories of `lines` (`_memory_lines`), in the
    room the memories would take (`_before_the_memories`): at
    most half of it, and never what the first memory needs to be shown
    (`_first_memory_floor`, its heading and its shortest fragment), when
    memories follow; all of it when none does, or when not even the first
    would fit the room alone. So a block with `reader` has the known facts,
    and the reader note in `stable`, of the block without it, byte for byte
    (review of piece 3, 9 Nov 2026: the changes took the facts' share and a
    block could lose its facts, its `stable` and its hash), and keeps a
    memory wherever it had one.

    Until the fourth review of piece 3 (9 Nov 2026) the changes took half of
    the room and a guard packed the block again without any of them when
    that half left no memory: at 300 to 500 tokens, in a block with an end
    user's facts and memories, a long change written short in the whole
    half made the guard drop the section at every read, the short changes
    behind it with it.

    `further` (ForARead.look_further): the changes that wait after
    `changes`, asked for once by the packing (`_pack_changes`); `waiting`,
    how many wait in all, those included."""
    return _pack_changes(block, changes, _before_the_memories(block, lines), further=further,
                         waiting=waiting)


def _before_the_memories(block: "_Block", lines: list) -> int:
    """Where a section packed in the memories' room, before the memories
    of `lines`, may end: at most half of the room left, and never what the
    first memory needs to be shown (`_first_memory_floor`), when memories
    follow; all of it when none does, or when not even the first would fit
    the room alone. The rule of the agent notices (review of piece 3, 9 Nov
    2026)."""
    room = block.pack.room()
    floor = _first_memory_floor(block, lines)
    share = room if floor is None or floor > room else min(room // 2, room - floor)
    return block.pack.used + share


@dataclass
class _PartShown:
    """What a part's section put in the block: its paragraph (its heading and
    lines, or the NOTE that it could not be read), the entries shown, and
    its name when it failed."""
    shown: Optional[tuple] = None
    entries: List[Entry] = field(default_factory=list)
    degraded: List[str] = field(default_factory=list)


def _part_after_the_memories(db: Session, block: "_Block", section: Optional[Section],
                             read: SectionRead, *, asked: bool,
                             vector_missing: bool) -> _PartShown:
    """A part's section after the memories, in the room they leave and in at
    most 1/PART_SHARE of the block's budget: the part asked for its entries
    (`Section.find`, in a savepoint as the facts' ranking, so a statement
    that fails rolls back alone) only when that room holds its heading and a
    line, its entries in its order, each whole or with its text cut
    (`_entry_line`), one that does not fit passed over. A part that fails is
    said in that same room (its `missing`, in full or short), and in
    `degraded_parts`, never in the room of the memories nor in the NOTE that
    says the memory could not be read: the read of the memory worked."""
    if section is None or not asked:
        return _PartShown()
    limit = min(block.pack.cap, block.pack.used + block.pack.cap // PART_SHARE)
    heading = _cost(section.heading) + _cost(_SEPARATOR)
    if limit - block.pack.used < heading + 1 + _MIN_FRAGMENT_CHARS:
        return _PartShown()
    try:
        with db.begin_nested():
            entries = list(section.find(db, read))
    except Exception as e:  # noqa: BLE001 - a part that could not be read, said in its room
        logger.log(missing_vector_level() if vector_missing else logging.WARNING,
                   "[agent_context] %s unavailable for %s: %s", section.name, read.tenant_id,
                   log_safe.error(e))
        full, short = section.missing
        said = next((n for n in (f"NOTE: {full}.", f"NOTE: partial context ({short}).")
                     if block.pack.take(_cost(n) + _cost(_SEPARATOR), limit)), None)
        return _PartShown(shown=(said, []) if said else None, degraded=[section.name])
    out: List[str] = []
    kept: List[Entry] = []
    for entry in entries:
        overhead = 1 + (0 if out else heading)
        line = _entry_line(entry, limit - block.pack.used - overhead)
        if line is not None and block.pack.take(overhead + _cost(line), limit):
            out.append(line)
            kept.append(entry)
    return _PartShown(shown=(section.heading, out) if out else None, entries=kept)


def _entry_line(entry: Entry, most: int) -> Optional[str]:
    """`entry` as one line of the block in at most `most` bytes, "- [label]
    text", each kept in its line (`in_its_line`): the label whole, the text
    whole or cut as a memory too long for its room is
    (`_truncate_to_budget`); None when not even a fragment of the text
    fits."""
    # The label's own delimiters, and the escape, escaped as Markdown escapes
    # them (CommonMark 0.31, 2.4 "Backslash escapes"): a "]" in a file's name
    # or in a heading of somebody else's file ends no label, and a forged
    # "[from ... ]" inside one opens none (the skeptic's review, 12 Nov 2026).
    escaped = in_its_line(entry.label).replace("\\", "\\\\")
    head = "- [" + escaped.replace("[", "\\[").replace("]", "\\]") + "] "
    text = in_its_line(entry.text.strip())
    if _cost(head) + _cost(text) <= most:
        return head + text
    frag = _truncate_to_budget(text, (most - _cost(head) + 2) // BYTES_PER_TOKEN)
    return head + frag if frag else None


def _smallest_line(changes: List[dict]) -> int:
    """The fewest bytes a line of a change can take: what `change_line`
    writes for a change that no longer holds, with no words and the
    shortest of the labels of where it came from, on the day of one of
    `changes`. Any change, of these or of the ones after them, takes at
    least that much."""
    return min(_cost(change_line({**c, "was": "", "now": None, "origin": origin}))
               for c in changes for origin in _HOW)


# How the block says where a change came from (services/facts/changes.py).
_HOW = {"corrected": "corrected", "forgotten": "forgotten", "stated": "stated by a caller",
        "read_from_memory": "read from a memory"}


# What a change says in place of the words of a fact whose memory was
# forgotten (services/facts/changes.words_kept): they are never told.
FORGOTTEN_WORDS = "a fact whose memory was forgotten"


def change_line(change: dict, most: Optional[int] = None) -> Optional[str]:
    """One change in the block: what the reader had, what holds now (or that
    it no longer holds), the day the store learned it and where it came
    from, each fact as `context_line` writes it; a fact whose memory was
    forgotten without its words (`FORGOTTEN_WORDS`).

    With `most` (bytes), a line longer than that is written short, as the
    block cuts a memory too long for its room (`_truncate_to_budget`): each
    fact cut at an equal share of the room, with the ellipsis, the frame
    (what ended, the day, where it came from) never; None when a share is
    too little for a fragment (`_MIN_FRAGMENT_CHARS`). So a change of any
    length fits a block with that little room (review of the corrections,
    9 Nov 2026: a change of 1,339 bytes was never told at 800 tokens, and
    twenty long ones held the others back). Each fact written short is cut
    to show where the two begin to differ (`_written_short`), so the two
    pieces never read the same; a change where not even that fits is
    None: not told short, so not consumed (the fourth review of piece 3: two
    facts that begin the same, an address, a plan with its seats, a
    procedure, came out as two identical pieces, the change consumed, and
    the MCP has no `changed` to read them whole). REST's `changed` keeps
    the facts whole."""
    day = (change.get("known_at") or change.get("ended_at") or "")[:10]
    how = f" ({day}, {_HOW.get(change['origin'], change['origin'])})"
    was = change["was"] if change.get("was") is not None else FORGOTTEN_WORDS
    now = change["now"]["text"] if change.get("now") else None

    def written(had: str, holds: Optional[str]) -> str:
        if holds is not None:
            return f"- was: {had} -> now: {holds}{how}"
        return f"- no longer true: {had}{how}"

    line = written(was, now)
    if most is None or _cost(line) <= most:
        return line
    texts = [was] + ([now] if now is not None else [])
    share = (most - _cost(written("", "" if now is not None else None))) // len(texts)
    if not _a_fragment(share):
        return None
    differ = _where_they_differ(was, now) if now is not None else (0, 0)
    cut = [_written_short(t, share, *differ) for t in texts]
    if None in cut:
        return None
    return written(cut[0], cut[1] if now is not None else None)


def _a_fragment(share: int) -> bool:
    """Whether a share of `share` bytes holds a fragment of a fact and its
    ellipsis (`_MIN_FRAGMENT_CHARS`, as the block's memories)."""
    return share >= _MIN_FRAGMENT_CHARS + _cost(_ELLIPSIS)


def _where_they_differ(a: str, b: str):
    """(where `a` and `b` first differ, a place before it where both may be
    cut with something of each after it), in characters. The second is
    where the part of each that differs begins, on a boundary of the
    characters as a reader sees them (services/graphemes.py): two emoji
    that differ only in their last code point, or a letter and a letter
    with a mark, differ from the start of that character."""
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    start = max(0, min(n, len(a) - 1, len(b) - 1))
    while start > 0 and not (graphemes.may_cut(a, start) and graphemes.may_cut(b, start)):
        start -= 1
    return n, start


def _written_short(text: str, share: int, differs: int, start: int) -> Optional[str]:
    """`text` in at most `share` bytes, the ellipsis where it is cut: whole
    when it fits; its beginning when that reaches past `differs`, where it
    begins to differ from the other fact of its change; else as much of
    its beginning as the part from `start` leaves, then that part (cut at
    its end too, when it is long), so the two facts show what changed.
    None when nothing of the text, or of the part that differs, fits.

    A larger share never writes None where a smaller one wrote a piece:
    the part from `start` to the end of the character that differs (`need`)
    is kept first, and the beginning has what is left of half the room. The
    fifth review of piece 3 (9 Nov 2026) found 35 changes in 720 told in a
    room and not in one 1 to 8 bytes larger, most with emoji: the beginning
    took half of the share before the character that differs, and its
    cost jumps by a whole character as the room grows."""
    if _cost(text) <= share:
        return text
    e = _cost(_ELLIPSIS)
    head = prefix_within(text, share - e)
    if len(head) > differs:
        # Its trailing spaces go, unless the difference is one of them.
        return (head.rstrip() if len(head.rstrip()) > differs else head) + _ELLIPSIS
    if start == 0:
        # The part that differs begins the text: its beginning is all the
        # short form can show, and it did not reach that part.
        return None
    tail = text[start:]
    if _cost(tail) + e <= share:
        return prefix_within(text[:start], share - e - _cost(tail)).rstrip() + _ELLIPSIS + tail
    room = share - 2 * e
    upto = graphemes.cut_after(text, differs) if differs < len(text) else len(text)
    need = _cost(text[start:upto])
    if need > room:
        return None
    lead = prefix_within(text[:start], room - max(need, room // 2)).rstrip()
    rest = prefix_within(tail, room - _cost(lead))
    if len(rest.rstrip()) >= upto - start:
        rest = rest.rstrip()
    return lead + _ELLIPSIS + rest + _ELLIPSIS


def _pack_changes(block: "_Block", changes: List[dict], limit: int,
                  further: Optional[Callable[[], List[dict]]] = None,
                  waiting: Optional[int] = None):
    """At most CHANGES_IN_THE_BLOCK changes up to `limit`: (their lines, the
    changes shown), newest first. First each change in that order, whole
    when it fits, else written short (`change_line(most=)`) in its share of
    what is left: what is left divided among the places still free, the
    changes still waiting counted, those past `changes` too (`waiting`, how
    many wait in all), so the next ones fit beside it. A change
    that does not fit its share is passed over, and the others go on: a
    long change holds none of the others back. Then the ones passed over,
    in the same order, each in all that is left, alone rather than not at
    all, so a long change is told as soon as the others leave it the room.

    `further`, the changes that wait after `changes`, is asked for once,
    after the first pass, when a place is free, a change was passed over and
    the room left holds the smallest line a change can take
    (`_smallest_line`): an older change may fit there where none of
    `changes` did, and the first pass is made again over all of them,
    newest first, before the second. The block looks at the newest only,
    and changes that could not be told there, by their words (a character
    longer than a share, the fifth review of piece 3, 9 Nov 2026) or by the
    room (long ones in a small block, the sixth), kept out every older one
    for good.

    Until the fourth review of piece 3 (9 Nov 2026) a change that did not
    fit its share took all the room at once: behind each long change the
    short ones waited one more read."""
    used = block.pack.used
    waiting = max(waiting or 0, len(changes))
    kept, passed = _first_pass(block, changes, limit, waiting)
    if (further is not None and passed and len(kept) < CHANGES_IN_THE_BLOCK
            and _room_left(block, kept, limit) >= _smallest_line(changes)):
        more = further()
        if more:
            block.pack.used = used
            changes = list(changes) + more
            kept, passed = _first_pass(block, changes, limit, waiting)
    for i in passed:
        if len(kept) == CHANGES_IN_THE_BLOCK:
            break
        line = _packed_change(block, changes[i], limit, 1, first=not kept)
        if line is not None:
            kept[i] = line
    order = sorted(kept)
    return [kept[i] for i in order], [changes[i] for i in order]


def _first_pass(block: "_Block", changes: List[dict], limit: int, waiting: int):
    """({index: line} of the changes packed whole or in their share, the
    indexes of the ones passed over), newest first (`_pack_changes`). The
    places of a share count the changes that wait after this one, `waiting`
    in all: the last change looked at shares the room with the ones not
    looked at yet, and does not take it all (the sixth review of piece 3:
    the twentieth of 22 long changes took the whole room, and the short ones
    behind them were never looked for)."""
    kept: dict = {}
    passed: List[int] = []
    for i, change in enumerate(changes):
        if len(kept) == CHANGES_IN_THE_BLOCK:
            break
        places = min(CHANGES_IN_THE_BLOCK - len(kept), waiting - i)
        line = _packed_change(block, change, limit, places, first=not kept)
        if line is None:
            passed.append(i)
        else:
            kept[i] = line
    return kept, passed


def _room_left(block: "_Block", kept: dict, limit: int) -> int:
    """The bytes the section's next line may take under `limit`: the room
    left, less its line break, and the heading when it would be the first."""
    heading = 0 if kept else _cost(CHANGED_HEADING) + _cost(_SEPARATOR)
    return limit - block.pack.used - 1 - heading


def _packed_change(block: "_Block", change: dict, limit: int, places: int, *,
                   first: bool) -> Optional[str]:
    """`change`'s line, counted in the block, when it fits under `limit`:
    whole, else written short in 1/`places` of what is left; None (nothing
    counted) when not even so. The first line packed pays for the heading,
    wherever it ends up in the section: the section costs the same in any
    order."""
    before = 1 + (_cost(CHANGED_HEADING) + _cost(_SEPARATOR) if first else 0)
    line = change_line(change)
    if block.pack.take(before + _cost(line), limit):
        return line
    line = change_line(change, most=(limit - block.pack.used) // places - before)
    if line is not None and block.pack.take(before + _cost(line), limit):
        return line
    return None


@dataclass
class _Found:
    """What one request retrieves, before anything is packed."""
    facts: List[AgentFact]
    hits: list
    # Degraded, and it says which part: a part that could not be retrieved
    # the way a healthy request retrieves it. The payload carries it for the
    # developer and the block for the model reading it.
    degraded_parts: List[str]
    # The question's one vector, and whether it is missing (`_query_vector`):
    # a part's section is read with it after the memories are packed.
    vector: Optional[List[float]] = None
    vector_missing: bool = False


def _retrieve(db: Session, tenant_id: UUID, *, query: str, end_user_id: Optional[str],
              agent_id: Optional[str], include_facts: bool, project_id: Any,
              known_as_of: Optional[datetime] = None, pool: int = POOL,
              exclude_sessions: bool = False) -> _Found:
    """Everything the block can show, read first: the note about a missing
    part is then known before the packing, and its room reserved."""
    degraded_parts: List[str] = []
    # One vector for the whole request, read by the facts and the memories.
    vector, vector_missing = _query_vector(tenant_id, agent_id, query)
    facts: List[AgentFact] = []
    if include_facts:
        facts, facts_ranked = _rank_facts(db, tenant_id, end_user_id, agent_id, query,
                                          limit=_FACTS_LIMIT, project_id=project_id,
                                          query_vector=vector, known_as_of=known_as_of,
                                          exclude_sessions=exclude_sessions)
        if not facts_ranked:
            degraded_parts.append(FACTS)
    # The memories are searched before anything is packed, so the note about a
    # missing part is known and its room reserved. Degraded, and it says so.
    # Propagating would be wrong: the facts are still retrievable and a caller
    # who can have half is better served with half. Swallowing it was also
    # wrong, and was what this did: the reply looked like a complete context
    # that happened to hold no relevant memories, and the consuming model
    # answered confidently from a profile with the verbatim source missing.
    # A missing vector is what MemorySearchUnavailable says, as when the
    # search made its own call.
    try:
        if vector_missing:
            raise MemorySearchUnavailable("no query vector")
        engine = AgentMemoryEngine(db, tenant_id, project_id)
        eu = end_users_of(end_user_id)
        # The agent's memories only, as the facts above and every other read
        # with an agent_id: until 6 Oct the block took the memories of every
        # agent of the end user, the sales agent's under the support agent's
        # context (the hosted product's code audit).
        hits = engine.search(query, end_user_id=eu, agent_id=agent_id, limit=pool,
                             query_vector=vector, widest=max(pool, limits.MAX_SEARCH_RESULTS),
                             received_by=known_as_of, exclude_sessions=exclude_sessions)
        if pool > POOL:
            # A pool wider than memory_core's: the memories past the first
            # whole ones by maximal marginal relevance (`_pool_for`).
            from . import mmr
            from .memory_core.presentation import WHOLE

            hits = mmr.order(db, hits, vector, keep_first=WHOLE)
    except MemorySearchUnavailable:
        # A question past its account's share is a refusal of the caller,
        # not a failure (core/llm.missing_vector_level): the notes promised a
        # warning, and this wrote an error (audit of 20 Oct 2026).
        logger.log(missing_vector_level() if vector_missing else logging.ERROR,
                   "[agent_context] memory search unavailable for %s: facts only", tenant_id)
        hits = []
        degraded_parts.append(MEMORIES)
    return _Found(facts, hits, degraded_parts, vector, vector_missing)


class _Block:
    """The block as it is packed: its characters (`pack`) and the public ids
    of what it shows in order (`sources`). The first fact and the first
    memory pay for their heading."""

    def __init__(self, budget: int):
        self.pack = _Packer(budget)
        self.sources: List[str] = []

    def facts_limit(self, memories_follow: bool) -> int:
        """Where the facts' share ends: half of the room left when the facts
        begin, so what is packed before them (the reader note, the NOTE
        about a degraded part) is paid by the facts and the memories alike.
        The share keeps room for the memories: when none follows (the search
        found none or was down, or the block shows none of those it found),
        the facts may take the whole room.
        Until 3 Nov 2026 the share was half of the whole budget, counted from
        the start of the block, and held the reader note's 723 bytes: at 400
        tokens it left 75, an ordinary fact did not fit and the block came
        back without its facts (the documentation review of 1 Nov 2026). The
        NOTE had the same cause, corrected for it alone on 1 Oct 2026, when
        it left the degraded block EMPTY under ~550 tokens."""
        if not memories_follow:
            return self.pack.cap
        return self.pack.used + int(self.pack.room() * _FACTS_BUDGET_FRACTION)

    def before_the_first_fact(self) -> int:
        return _cost(KNOWN_FACTS_HEADING) + _cost(_SEPARATOR)

    def before_the_first_memory(self) -> int:
        return _cost(_memories_heading()) + _cost(_SEPARATOR)


def _pack_facts(block: _Block, facts: List[AgentFact], end_user_id: Optional[str],
                limit: int, known_as_of: Optional[datetime] = None) -> List[str]:
    """The facts ranked for the question, in what is left of the facts'
    share (`limit`)."""
    lines: List[str] = []
    # #1 legible first-person subject: when the fact's subject IS the end
    # user (the opaque end_user_id handle), render "The user" so the reader
    # binds it to the question's "I/my" instead of an unreadable machine
    # handle (the 06878be2-shape failure). context_line, identical on the
    # hosted product: the entity under its current name ("Globex (then:
    # Acme)"), plans marked.
    # memory_core 6.1.0: a link "the same as" reads two objects as one
    # ("Italia (written: Italy)"), and two facts it makes one triple are one
    # line with both as its sources (facts/links.py).
    from sqlalchemy.orm import object_session

    from .facts.links import one_line_each

    db = object_session(facts[0]) if facts else None
    packable = [(context_line(v, end_user_id if isinstance(end_user_id, str) else None), ids)
                for v, ids in (one_line_each(db, facts, known_as_of) if db is not None
                               else [(f, f.id) for f in facts])]
    for line, fid in packable:
        cost = 1 + _cost(line) + (0 if lines else block.before_the_first_fact())
        if not block.pack.take(cost, limit):
            continue  # skip an oversized line, keep packing the smaller ones
        lines.append(line)
        for one in (fid if isinstance(fid, list) else [fid]):
            block.sources.append(fact_public_id(one))
    return lines


def _as_id(mid: Any) -> Optional[str]:
    try:
        return str(UUID(str(mid)))
    except (TypeError, ValueError):
        return None


def _memory_rows(db: Session, hits: list) -> dict:
    """Each memory's line (memory_core 2.6.0 `memory_line`) shows its
    observation, dated, when the write gave it one, and the whole text, as
    before, when not. The observation and the times are read here, so the
    search keeps the public shape it answers with: one query, for every
    memory the search brought, since the recency weighs them before the
    filter."""
    ids = [UUID(str(h["id"])) for h in hits if h.get("id")]
    if not ids:
        return {}
    return {str(r.id): r for r in db.query(
        AgentMemory.id, AgentMemory.observation, AgentMemory.event_time,
        AgentMemory.created_at, AgentMemory.relative_times).filter(AgentMemory.id.in_(ids))}


def _memories_for_the_block(db: Session, hits: list, tenant_id: UUID,
                            end_user_id: Optional[str],
                            known_as_of: Optional[datetime] = None) -> list:
    """Which memories go in, the rule of both Agents products (memory_core
    2.3.0, `memories_for_the_context`): a text already shown is not shown
    again, a memory whose facts are all superseded is out, one that stated no
    fact stays. The same text came twice, and "Maria lives in Rome." stood
    under a block that calls the memories the verbatim source, next to "Maria
    lives in Milan" (the hosted product's audit, 2026-09-29). The states of
    all the hits come from one query (memory_core 2.8.0,
    `store.states_of_the_sources`, BUG-367), not from two per memory over the
    whole facts table. A memory with no text is not a line, so it is left
    out here too: what this returns is what the block can show."""
    from .facts.entities import TABLES as _TABLES
    from .memory_core.store import states_of_the_sources
    from .memory_core.presentation import memories_with_their_state, memories_for_the_context

    hit_ids = [UUID(i) for i in (_as_id(h.get("id")) for h in hits) if i is not None]
    # The facts that were open, and the facts that existed, as the store knew
    # them then when the block is rebuilt for a past moment.
    scope = _facts_of(tenant_id, end_user_id)
    if known_as_of is not None:
        from sqlalchemy import and_

        from .facts.temporal import known_at

        scope = and_(scope, known_at(known_as_of))
    states = states_of_the_sources(db, _TABLES, hit_ids, is_open=true_then(known_as_of),
                               scope=scope)
    state_of = lambda mid: states.get(_as_id(mid), (0, 0))  # noqa: E731
    told = _changed_by_a_caller(db, states, tenant_id, end_user_id, scope, known_as_of)
    if experiments.SUPERSEDED_AS_HISTORY:
        kept = [dict(h, _superseded=True) if superseded or _as_id(h.get("id")) in told else h
                for h, superseded in memories_with_their_state(hits, state_of, superseded_as_history=True)]
    else:
        kept = [h for h in memories_for_the_context(hits, state_of)
                if _as_id(h.get("id")) not in told]
    return [h for h in kept if (h.get("content") or "").strip()]


def _changed_by_a_caller(db: Session, states: dict, tenant_id: UUID,
                         end_user_id: Optional[str], scope: Any,
                         known_as_of: Optional[datetime]) -> set:
    """The memories of `states` (`store.states_of_the_sources`) some of whose
    facts still stand, and one of which no longer stands by a caller's word:
    corrected, forgotten, or ended by a fact a caller wrote (facts/changes
    .closed_by_a_caller, the closures agent notices call explicit). The
    block shows them as it shows a memory whose facts were all superseded,
    "(true when said, changed later)" after the day (`block_line`).

    Why: a correction (PATCH /v1/facts/{id}, korely_correct_fact) is the
    caller's word and no memory says it, so the block showed the corrected
    fact and, unmarked, the memory with the value it replaced, under a note
    that calls the memories the verbatim source of truth (pre-tester check
    of 15 Nov 2026: "renewal in November" among the facts, "her renewal is in
    October" among the memories). A closure read from a later memory has that
    memory to show, dated after it; this one has none. Mem0 removes a memory
    the new information contradicts (DELETE, Chhikara et al. 2025, section
    2.1) and Zep's context carries no episode, only the facts with their
    date ranges (Rasmussen et al. 2025, section 3, the constructor); the
    block keeps the memory, readable as history, as Graphiti keeps an
    invalidated edge (section 2.2.3), since its other facts still hold.

    A block of memories the judge read alone is the block of before byte for
    byte: only a caller's closure marks one more memory. Two queries, for
    the memories partly superseded only (those wholly superseded are marked
    already): their arrivals, then their states by the same rule as before,
    with the closure's condition added to its scope; the successor counts
    only when the block may read it (its account, the fact's project, the
    block's end users).

    A closure counts against the text it was made on: the memory's current
    text arrived by the time the store learned the end (`worker.arrival`,
    its last edit, else its creation, against `temporal.end_learned`), the
    split given_back._vouching makes for the same reason. An edit after a
    correction is later word: the retirement takes the open facts only, so
    the corrected one keeps the memory as its source, and the memory edited
    to say November stayed marked for good (review of 16 Nov 2026).

    Limits. The condition is on the fact, so a fact two of these memories
    state counts for both when one of them arrived by its closure. A past
    statement a caller posts (POST /v1/facts, `tense: past`) that ends the
    fact in place marks nothing: memory_core counts a fact closed only by
    its tense as standing (`store.still_standing`, 2.5.1), and the closure
    is no explicit one (facts/changes.py)."""
    from sqlalchemy import and_, or_

    from .facts.changes import closed_by_a_caller
    from .facts.entities import TABLES as _TABLES
    from .memory_core.store import source_condition, states_of_the_sources
    from .memory_core.worker import arrival

    partly = [UUID(k) for k, (stated, standing) in states.items() if 0 < standing < stated]
    if not partly:
        return set()

    def readable(closer):
        return and_(closer.tenant_id == tenant_id,
                    closer.project_id.is_not_distinct_from(AgentFact.project_id),
                    user_scope.wall(closer.end_user_id, end_users_of(end_user_id)))

    texts = (db.query(AgentMemory.id, AgentMemory.created_at, AgentMemory.updated_at)
             .filter(AgentMemory.id.in_(partly)).all())
    if not texts:
        return set()
    on_its_text = or_(*[and_(source_condition(_TABLES, m.id), end_learned() >= arrival(m))
                        for m in texts])
    by_a_caller = states_of_the_sources(
        db, _TABLES, partly, is_open=true_then(known_as_of),
        scope=and_(scope, closed_by_a_caller(tenant_id, readable), on_its_text))
    return {k for k, (stated, standing) in by_a_caller.items() if standing < stated}


def _memory_lines(db: Session, hits: list, shown: list) -> list:
    """(line, hit) for each memory of `shown`, in the order the block packs
    them, each line whole: the hybrid block, memory_core's rule (2.8.0,
    block_line), the first WHOLE whole, the rest as dated short lines,
    every one under the day it was said. The packing never skips a memory,
    so a memory's place in the block is its place here."""
    from .memory_core import presentation as _presentation

    rows = _memory_rows(db, hits)
    if experiments.DIALOGUE_ORDER:
        shown = experiments.in_dialogue_order(db, shown, _presentation.WHOLE)
    out = []
    for i, h in enumerate(shown):
        row = rows.get(str(h.get("id")))
        memory = {"content": h["content"].strip(),
                  "observation": row.observation if row else None,
                  "event_time": row.event_time if row else None,
                  "created_at": row.created_at if row else None,
                  # memory_core 2.9.0: the dates of its relative times, shown
                  # next to the words when written.
                  "relative_times": row.relative_times if row else None}
        out.append((block_line(memory, i, superseded=bool(h.get("_superseded"))), h))
    return out


def _first_memory_floor(block: _Block, lines: list) -> Optional[int]:
    """The fewest bytes in which `_pack_memory_lines` shows the first of
    `lines`: its heading, then the line whole or, when that is longer, cut
    to the shortest fragment `_truncate_to_budget` keeps of it (the fewest
    tokens that keep anything, found by bisection: a larger room never
    keeps less). None when there is no memory to show."""
    if not lines:
        return None
    line = lines[0][0]
    lo, hi = 1, -(-_cost(line) // BYTES_PER_TOKEN)   # at `hi` the text fits whole
    while lo < hi:
        mid = (lo + hi) // 2
        if _truncate_to_budget(line[2:], mid):
            hi = mid
        else:
            lo = mid + 1
    return 1 + block.before_the_first_memory() + min(_cost(line), lo * BYTES_PER_TOKEN)


def _pack_memory_lines(block: _Block, lines: list) -> List[str]:
    """The lines of `_memory_lines` in the rest of the budget, in order: one
    too long for the room is cut to fit (`_truncate_to_budget`), and the
    packing ends at the first that does not fit even so."""
    out: List[str] = []
    for line, h in lines:
        overhead = 1 + (0 if out else block.before_the_first_memory())
        if block.pack.room() - overhead <= 0:
            break
        if not block.pack.take(overhead + _cost(line)):
            # Greedy budget-packing: don't drop the (most-relevant) memory
            # wholesale — truncate it at a sentence boundary to fit. The loop
            # goes on: the cut can leave room, and a later, shorter memory
            # may still fit whole, or be cut in its turn; it stops when no
            # room is left or a fragment is too short to keep.
            frag = _truncate_to_budget(line[2:], (block.pack.room() - overhead) // BYTES_PER_TOKEN)
            if not frag:
                break
            line = f"- {frag}"
            if not block.pack.take(overhead + _cost(line)):
                break
        out.append(line)
        block.sources.append(mem_public_id(h["id"]))
    return out


def _render(block: _Block, degraded_parts: List[str], note: str, *, guidance: bool,
            facts: List[str], memories: List[str], changes: List[str] = (),
            part: Optional[tuple] = None) -> dict:
    """The packed lines under their headings, split in `stable` and
    `volatile` (the module docstring draws both), and the payload. `part`:
    (heading, lines) of a part's section, after the memories, or (its NOTE,
    []) when it could not be read; a block with its lines alone has the
    reader note as one with facts or memories."""
    has_content = bool(facts or memories or (part and part[1]))
    stable_parts: List[str] = []
    if guidance and has_content:
        stable_parts.append(_READER_GUIDANCE)
    volatile_parts: List[str] = []
    # The changes head `volatile` and never make content of their own: the
    # reader note, in `stable`, comes with facts or memories as before.
    if changes:
        volatile_parts.append(_section(CHANGED_HEADING, changes))
    if facts:
        volatile_parts.append(_section(KNOWN_FACTS_HEADING, facts))
    if memories:
        volatile_parts.append(_section(_memories_heading(), memories))
    if part and (has_content or part[1]):
        volatile_parts.append(_section(*part))
    if note and has_content:
        # In the block, because the model reading it needs to know, and in the
        # payload, because the developer does. Its room was reserved above.
        volatile_parts.append(note)

    stable = _SEPARATOR.join(stable_parts)
    volatile = _SEPARATOR.join(volatile_parts)
    context = _SEPARATOR.join(p for p in (stable, volatile) if p)
    return {
        "context": context,
        "tokens": _estimate_tokens(context),
        "sources": block.sources,
        "degraded": bool(degraded_parts),
        "degraded_parts": degraded_parts,
        "stable": stable,
        "volatile": volatile,
        "stable_hash": hashlib.sha256(stable.encode("utf-8")).hexdigest(),
    }
