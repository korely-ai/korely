"""Whose memory a write belongs to: one rule for the extraction and the judge (memory_core 6.7.0).

The extraction frames a memory by it since 6.4.0 (`fact_extractor._owner`):
with an end user, theirs; with an agent and no end user, the agent's own (a
team's memory, an agent's notes: Mem0's `is_agent_scoped`, mem0ai/mem0
0724291, mem0/memory/main.py); with neither, one person's. The judge narrows
its candidates by meaning by it since 6.7.0 (`candidates.for_the_judge(
agent_scoped=, agent_scoped_facts=)`, where the reason is). One rule in one
place, so no memory is framed as an agent's by one and judged as a person's
by the other (a review of 7 Nov 2026).

An end user is there when the write names one: `end_user_id` is not None,
whatever it holds (an id of spaces is an end user's namespace, 6.4.0). An
agent is there when `agent_id` holds something besides whitespace: a blank id
names nobody, and the extraction binds no blank handle in a prompt (a review
of 7 Nov 2026). Whitespace is Python's own, the characters
`str.strip()` removes (`str.isspace`: every alphabet's spaces, the
ideographic one included), read from the Unicode database of the running
Python and never from a list written here.

Dependency: the standard library; the SQL form imports SQLAlchemy 2 when it
is built, so the extraction, which reads only the Python form, stays pure
logic.
"""
from __future__ import annotations

import functools
import sys
from typing import Any, Optional


def agent_of(agent_id: Optional[str]) -> Optional[str]:
    """The agent a write names, stripped, or None when it names none."""
    return (agent_id or "").strip() or None


def agent_scoped(end_user_id: Optional[str], agent_id: Optional[str]) -> bool:
    """Whether a write with these ids belongs to an agent's memory with no
    end user: no end user, and an agent (`agent_of`)."""
    return end_user_id is None and agent_of(agent_id) is not None


def agent_scoped_rows(end_user_id: Any, agent_id: Any) -> Any:
    """`agent_scoped` as a condition on two columns of the stored rows (the
    judge's candidates and the late judgement of a vector): no end user, and
    an agent id that a `btrim` of every whitespace character leaves
    non-empty, so the database and `agent_scoped` say the same of every id.
    A NULL agent id gives NULL, which the engine reads as no (`coalesce`,
    `candidates.about_the_subject`). `btrim` removes characters in a UTF8
    database; in a SQL_ASCII one it works on bytes, and on an id written
    with other characters than ASCII the two forms may then disagree
    (docs/guide/limits.md)."""
    from sqlalchemy import and_, func

    return and_(end_user_id.is_(None), func.btrim(agent_id, whitespace()) != "")


@functools.lru_cache(maxsize=None)
def whitespace() -> str:
    """Every character `str.strip()` removes, from this Python's Unicode
    database (29 on Python 3.11); computed once per process, on the first
    condition built (about 0.1 s)."""
    return "".join(c for c in map(chr, range(sys.maxunicode + 1)) if c.isspace())
