"""Whose memory a write belongs to: one rule for the extraction and the judge (memory_core 6.7.0).

The extraction frames a memory by it since 6.4.0 (`fact_extractor._owner`):
with an end user, theirs; with an agent and no end user, the agent's own (a
team's memory, an agent's notes: Mem0's `is_agent_scoped`, mem0ai/mem0
0724291, mem0/memory/main.py); with neither, one person's. The judge narrowed
its candidates by meaning by the same rule from 6.7.0 to 6.8.1
(`candidates.for_the_judge(agent_scoped=, agent_scoped_facts=)`, where the
reason is), and since 6.9.0 by a wider one, written here too:
`keeps_to_its_subject`, every memory with no end user, with an agent or
without. The two rules live in this one module, so the extraction and the
judge each read theirs from one place (a review of 7 Nov 2026).

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


def keeps_to_its_subject(end_user_id: Optional[str]) -> bool:
    """6.9.0: whether a fact of a write with this end user is weighed by the
    judge, by meaning (stage 1b) and through the late judgement of a vector,
    only against facts of its own entity (its subject, in either role:
    `candidates.about_the_subject`): every write with no end user, whatever
    its `agent_id` (approved on 12 Nov 2026). Until 6.8.1 only an agent's
    memory (`agent_scoped`, 6.7.0, by its subject alone); a memory with no
    ids at all met facts of any subject by meaning.

    Why. Stage 1b looks past a subject's surface because in ONE person's
    memory the extractor writes one entity several ways ("Maria", "the
    user", "the runner"); with no end user the store cannot know it is one
    person's, and the reason stage 1a-bis is off there ("the global scope is
    too broad to assume one relation family is one entity",
    `candidates.for_the_judge`) holds for 1b, which is wider still. Ritter,
    Downey, Soderland, Etzioni, EMNLP 2008 (ACL Anthology D08-1002, read on 12
    Nov 2026): the candidates of a contradiction are the sets "R(x,·)" of one
    domain argument x (§4.2), and "false contradictions arise when a single x
    value refers to multiple real-world entities" (§4.3); Graphiti
    (Rasmussen et al. 2025, arXiv:2501.13956, §2.2.1, §2.2.3) invalidates
    among edges of entities already resolved into one node each, and the
    subject after the memory's aliases is that entity here. Measured cause
    (12 Nov 2026, Gemini 3.1 Flash-Lite, the memory of a team of agents
    written with no ids, 43 memories, three runs, every closure labelled by
    reading both memories): of the wrong closures, 7 of 19 joined two
    different subjects met by meaning ("a rule of who enters the candidates"
    closed by "Mem0's rule on whose messages make facts"), every one through
    stage 1b; a rule written into the judge's question did not remove them
    (measured the same day: 16 of 40 against 19 of 41, inside the noise).
    The same memory written with an `agent_id`, which 6.7.0 already judges
    subject by subject, had none of them.

    The price, as for 6.7.0: two names of one thing ("Cloud", "Korely Cloud",
    or a person's name and "user", the subject the extraction gives the
    first person of a write with no ids) are two subjects until an alias
    unites them (a declared name, a rename, `POST /admin/aliases`); so are
    one name in two Unicode forms the database's lower() does not unite (a
    final sigma, a composed and a decomposed accent, full-width letters,
    `candidates.about_the_subject`). A right closure between them is lost, as
    a wrong one is. A fact still meets its entity in the other role ("Acme
    employs user", "user works at Globex": `candidates.about_the_subject`).
    Measured after (12 Nov 2026, the same memory, three runs per version):
    wrong closures 19 of 41 -> 12 of 30, none between two subjects; outdated
    facts closed 21 -> 18, the 3 left open one outdated version that no fact
    of its own subject says changed. With an end user nothing changes,
    statement for statement (LongMemEval and LoCoMo always write with one)."""
    return end_user_id is None


def keeps_to_its_subject_rows(end_user_id: Any) -> Any:
    """`keeps_to_its_subject` as a condition on the column of the stored
    rows (the judge's candidates and the late judgement of a vector): no end
    user. Never NULL."""
    return end_user_id.is_(None)


@functools.lru_cache(maxsize=None)
def whitespace() -> str:
    """Every character `str.strip()` removes, from this Python's Unicode
    database (29 on Python 3.11); computed once per process, on the first
    condition built (about 0.1 s)."""
    return "".join(c for c in map(chr, range(sys.maxunicode + 1)) if c.isspace())
