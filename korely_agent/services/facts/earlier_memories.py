"""The memories the same end user wrote just before the one being read, which
the extraction reads beside it (memory_core 2.6.0). Moved out of
services/facts/service.py on 26 Nov 2026, as it was there.
"""
from __future__ import annotations

from typing import List

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from ...db.models import AgentMemory
from .. import graphemes
from ..sentinels import within_project

# The extraction reads the memories the same end user wrote just before this
# one (memory_core 2.6.0, 2026-10-02): "she", "the second one", "that plan"
# are resolved against them, and no fact is extracted from them. How many and
# how long is memory_core's rule, the same in both engines (MAX_PREVIOUS,
# MAX_PREVIOUS_CHARS); this reads no more. Since 2.6.1 ten of 300
# characters, as Mem0 gives its extractor (mem0ai/mem0 abb81c8,
# storage.get_last_messages, prompts.PAST_MESSAGE_TRUNCATION_LIMIT).
from ..memory_core.fact_extractor import MAX_PREVIOUS_CHARS, MAX_PREVIOUS

PREVIOUS_MEMORIES, PREVIOUS_MEMORY_CHARS = MAX_PREVIOUS, MAX_PREVIOUS_CHARS


def _cut(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= max_chars else graphemes.first(text, max_chars).rstrip() + "…"


def previous_memories(db: Session, memory, n: int = PREVIOUS_MEMORIES,
                      max_chars: int = PREVIOUS_MEMORY_CHARS) -> List[str]:
    """The texts of the `n` memories the same end user wrote before `memory`,
    oldest first, each cut to `max_chars`: the same tenant, project, end user
    and agent namespace, never a deleted one. "Before" is event time (the
    write's `timestamp`), then the order of writing, so the turns of one
    conversation, which share its time, keep their order. Without an end user
    there is no person whose words they would explain, and the list is
    empty."""
    if not getattr(memory, "end_user_id", None):
        return []
    when = func.coalesce(AgentMemory.event_time, AgentMemory.created_at)
    mine = memory.event_time or memory.created_at
    q = db.query(AgentMemory.content).filter(
        AgentMemory.tenant_id == memory.tenant_id,
        AgentMemory.end_user_id == memory.end_user_id,
        AgentMemory.deleted_at.is_(None),
        AgentMemory.id != memory.id,
        tuple_(when, AgentMemory.created_at) < tuple_(mine, memory.created_at),
    )
    q = within_project(q, AgentMemory.project_id, memory.project_id)
    q = (q.filter(AgentMemory.agent_id.is_(None)) if memory.agent_id is None
         else q.filter(AgentMemory.agent_id == memory.agent_id))
    rows = q.order_by(when.desc(), AgentMemory.created_at.desc()).limit(n).all()
    return [_cut(r.content, max_chars) for r in reversed(rows)]
