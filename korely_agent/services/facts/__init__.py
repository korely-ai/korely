"""Agent-store Layer 3 — typed facts for the /v1 memory product.

The moat: every agent memory is mined for (subject, predicate, object) facts
with bi-temporal validity and contradiction detection, scoped per end user.
The rules are the facts engine's, `services/memory_core/`; this package
holds the tables and the orchestration around it:

    extractor.py     — direct LLM extraction, end_user-anchored (Mem0, Chhikara et al. 2025)
    contradiction.py — two-stage contradiction on agent_facts (ConflictBank Su 2024)
    service.py       — orchestration (extract+persist+contradict+embed), direct write, corrections
    reads.py         — retrieval, a fact's read shape, a memory's facts and history
    drain.py         — the deferred writes, claimed and written in the background
    erasure.py       — forgetting a memory or one fact, erasing an end user or an agent namespace
    temporal.py      — what it means for a fact to be true (or not ended) at a moment

The extraction prompt is agent-tuned (subject = the end user, not a vault
owner). `canonicalize_predicate`, the predicate families and the
contradiction's opposing families and prompt come from memory_core, which
until 11 Oct 2026 an earlier product shared (decided 2026-06-12: shared
logic, separate storage).
"""
from .service import (
    MODEL_FAILURES,
    ClosedBeforeItBegan,
    FactNotCurrent,
    RELATION_SAYS_NOTHING,
    RelationSaysNothing,
    check_relation,
    ReplacesAnotherMemory,
    RestatedFactClosed,
    extract_and_persist_facts,
    extract_inline,
    retrieve_agent_facts,
    write_fact_direct,
    serialize_fact,
    facts_for_memory,
    erase_memory,
    forget_memory,
    forget_end_user,
    delete_agent,
    forget_fact,
    correct_fact,
    correction_fields,
    memory_timeline,
    parse_event_time,
    event_time_of,
)

__all__ = [
    "ClosedBeforeItBegan",   # caught by api/facts.py as agent_facts.ClosedBeforeItBegan
    "FactNotCurrent",        # a correction of history: 409 fact_not_current, nothing written
    "MODEL_FAILURES",        # caught by api/facts.py and api/admin.py: a 503, nothing written
    "RELATION_SAYS_NOTHING",  # the message of the 422 relation_says_nothing
    "RelationSaysNothing",   # a relation of one or two letters: 422, nothing written
    "check_relation",        # the doors' check, before the write's quota
    "ReplacesAnotherMemory", # POST /v1/facts `replaces` another's fact: 422, nothing written
    "RestatedFactClosed",    # the fact a write restates closed meanwhile: its own 409 words
    "correct_fact",
    "correction_fields",     # the fields of a correction, for /v1 and the operator's screen
    "delete_agent",
    "event_time_of",         # the memory's event time: the drain and a synchronous PATCH
    "extract_and_persist_facts",
    "extract_inline",
    "facts_for_memory",
    "forget_end_user",
    "forget_fact",
    "erase_memory",
    "forget_memory",
    "memory_timeline",       # one memory's history, for /v1 and the operator's screen
    "parse_event_time",      # every write path's `timestamp`: memories, batch, MCP
    "retrieve_agent_facts",
    "serialize_fact",
    "write_fact_direct",
]
