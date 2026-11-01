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
the profile on, the profile first) share half of the budget. A short
reader-trust note leads the block so the consuming LLM falls back to the
memories when a fact does not directly answer the question. Token estimate is
char/4 (the coarse heuristic the human RAG path uses — exact tokenization isn't
worth the dependency here), counted on every character the block holds,
headings and newlines included.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, List, Optional
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
from .sentinels import EVERY_END_USER, EVERY_PROJECT, EndUsers, within_project
from . import limits, user_scope
from . import context_experiments as experiments
from .facts.temporal import true_then
from .memory_core.presentation import POOL, rank_with_recency, context_line, block_line

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

    if end_user_id is None:
        return AgentFact.tenant_id == tenant_id
    if isinstance(end_user_id, EndUsers):
        # A key confined to some end users (services/user_scope.py).
        return and_(AgentFact.tenant_id == tenant_id,
                    user_scope.condition(AgentFact.end_user_id, end_user_id.patterns))
    return and_(AgentFact.tenant_id == tenant_id, AgentFact.end_user_id == end_user_id)


# Below this budget the note isn't worth its ~tokens — emit content instead. Set
# above the note's own size (~150 tok) + a content margin: at a tiny budget the
# ~150-token note would crowd out the actual answer-bearing content, so we suppress
# it and let content win (Liu 2024 — content over meta-instruction when space is
# scarce). Realistic budgets (bench 2000; customers 800-8000) clear it easily.
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
    window = _prefix_within(text, max_bytes)
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "),
              window.rfind(".\n"), window.rfind("\n"))
    if cut >= _MIN_FRAGMENT_CHARS:
        return window[:cut + 1].rstrip()
    # A fragment that ends with the ellipsis pays for it from the same bytes:
    # paid on top, it passed the room by 3 bytes (audit of 19 Oct 2026), and the
    # memory it was cut from left the block wholesale, the bug this function
    # exists for, for any text with no space in the window.
    window = _prefix_within(text, max_bytes - _cost(_ELLIPSIS))
    sp = window.rfind(" ")
    if sp >= _MIN_FRAGMENT_CHARS:
        return window[:sp].rstrip() + _ELLIPSIS
    return window.rstrip() + _ELLIPSIS


_ELLIPSIS = "…"


def _prefix_within(text: str, max_bytes: int) -> str:
    """The longest prefix of `text` within `max_bytes` UTF-8 bytes, cut on a
    character boundary."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


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


def _facts_base(db: Session, tenant_id: UUID, end_user_id: Optional[str],
                agent_id: Optional[str], project_id: Any = EVERY_PROJECT,
                known_as_of: Optional[datetime] = None, exclude_sessions: bool = False):
    """The facts true now that the context may show: the tenant, the key's
    project, the end user and the agent when given. One wall for every fact
    the block shows. With `known_as_of`, the facts true then as the store
    knew them then (facts/temporal.py)."""
    base = within_project(db.query(AgentFact).filter(AgentFact.tenant_id == tenant_id,
                                                     true_then(known_as_of)),
                          AgentFact.project_id, project_id)
    if isinstance(end_user_id, EndUsers):
        base = base.filter(user_scope.condition(AgentFact.end_user_id, end_user_id.patterns))
    elif end_user_id is not None:
        base = base.filter(AgentFact.end_user_id == end_user_id)
    if agent_id is not None:
        base = base.filter(AgentFact.agent_id == agent_id)
    if exclude_sessions:
        # Only the facts some note states (AgentFact.session_only, 18 Oct 2026).
        base = base.filter(AgentFact.session_only.is_(False))
    return base


def is_about_the_end_user(f: Any, end_user_id: str) -> bool:
    """The rule of GET /v1/profile for "a fact about this end user" (on both
    products): typed `self`, or its subject IS the end user's id, compared
    stripped and case-folded the same way, as written or brought to it by an
    alias (a name the end user declared: the audit of 19 Oct 2026;
    memory_core.presentation, the same rule for the block)."""
    uid = (end_user_id or "").strip().lower()
    return (f.subject_type == "self" or (f.subject_label or "").strip().lower() == uid
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
    base = _facts_base(db, tenant_id, end_user_id, agent_id, project_id, known_as_of,
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
                # user's facts, and the wall of `_facts_base` applies after
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
) -> dict:
    """The block, split as the module docstring draws it: `stable` does not
    depend on the question (the reader note), `volatile` (what the question
    brings, and the NOTE on what could not be retrieved) does. `context` is `stable` and `volatile` joined by a
    blank line, and `stable_hash` the SHA-256 of `stable`, so a client can
    tell a cached prefix from a changed one without comparing texts.

    Packing order: the reader note (reserved whenever the budget can afford
    it); then the NOTE about a degraded part, the facts
    ranked for the question (top _FACTS_LIMIT, in the facts' share, up to half
    of the budget), then the memories (the rest), under the two headings of
    before 2026-10-01.

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
    # The NOTE's room comes out of the memories' share, not the facts': with
    # the memories down that share is empty anyway, and the block stays under
    # the budget. Counted against `facts_cap` it left no room for a single
    # fact under ~550 tokens, and the degraded block came back EMPTY (found
    # porting this code from the hosted product, which has the same line
    # since 955e222, 2026-10-01).
    facts_limit = block.facts_cap + (_cost(note) + _cost(_SEPARATOR) if note else 0)
    fact_lines = _pack_facts(block, found.facts, end_user_id, facts_limit, known_as_of)
    mem_lines = _pack_memories(db, block, found.hits, tenant_id, end_user_id,
                               known_as_of=known_as_of)

    return _render(block, found.degraded_parts, note,
                   guidance=want_guidance, facts=fact_lines, memories=mem_lines)


@dataclass
class _Found:
    """What one request retrieves, before anything is packed."""
    facts: List[AgentFact]
    hits: list
    # Degraded, and it says which part: a part that could not be retrieved
    # the way a healthy request retrieves it. The payload carries it for the
    # developer and the block for the model reading it.
    degraded_parts: List[str]


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
        eu = end_user_id if end_user_id is not None else EVERY_END_USER
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
    return _Found(facts, hits, degraded_parts)


class _Block:
    """The block as it is packed: its characters (`pack`) and the public ids
    of what it shows in order (`sources`). The first fact and the first
    memory pay for their heading."""

    def __init__(self, budget: int):
        self.pack = _Packer(budget)
        self.facts_cap = int(budget * _FACTS_BUDGET_FRACTION) * BYTES_PER_TOKEN
        self.sources: List[str] = []

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
    whole facts table."""
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
    if experiments.SUPERSEDED_AS_HISTORY:
        return [dict(h, _superseded=True) if superseded else h
                for h, superseded in memories_with_their_state(hits, state_of, superseded_as_history=True)]
    return memories_for_the_context(hits, state_of)


def _pack_memories(db: Session, block: _Block, hits: list, tenant_id: UUID,
                   end_user_id: Optional[str],
                   known_as_of: Optional[datetime] = None) -> List[str]:
    """The memories fill the rest of the budget."""
    from .memory_core import presentation as _presentation

    rows = _memory_rows(db, hits)
    hits = _memories_for_the_block(db, hits, tenant_id, end_user_id, known_as_of)
    if experiments.DIALOGUE_ORDER:
        hits = experiments.in_dialogue_order(db, hits, _presentation.WHOLE)
    lines: List[str] = []
    for h in hits:
        content = (h.get("content") or "").strip()
        if not content:
            continue
        overhead = 1 + (0 if lines else block.before_the_first_memory())
        if block.pack.room() - overhead <= 0:
            break
        row = rows.get(str(h.get("id")))
        # The hybrid block, memory_core's rule (2.8.0, block_line): the
        # first WHOLE whole, the rest as dated short lines, every one under
        # the day it was said.
        memory = {"content": content,
                  "observation": row.observation if row else None,
                  "event_time": row.event_time if row else None,
                  "created_at": row.created_at if row else None,
                  # memory_core 2.9.0: the dates of its relative times, shown
                  # next to the words when written.
                  "relative_times": row.relative_times if row else None}
        line = block_line(memory, len(lines), superseded=bool(h.get("_superseded")))
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
        lines.append(line)
        block.sources.append(mem_public_id(h["id"]))
    return lines


def _render(block: _Block, degraded_parts: List[str], note: str, *, guidance: bool,
            facts: List[str], memories: List[str]) -> dict:
    """The packed lines under their headings, split in `stable` and
    `volatile` (the module docstring draws both), and the payload."""
    has_content = bool(facts or memories)
    stable_parts: List[str] = []
    if guidance and has_content:
        stable_parts.append(_READER_GUIDANCE)
    volatile_parts: List[str] = []
    if facts:
        volatile_parts.append(_section(KNOWN_FACTS_HEADING, facts))
    if memories:
        volatile_parts.append(_section(_memories_heading(), memories))
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
