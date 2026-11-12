"""How a memory is written, the same at every door (audit of 17 Oct 2026).

POST /v1/memories, the MCP korely_add and the batch worker each wrote a memory
by hand: the input rules, the key's end users, the hosted service's two hooks,
the engine's write, the inline extraction, the audit row and the webhook, in a
different order and with different holes. Only MCP refused a NUL byte in
`run_id`: REST answered it with a 500, and a batch job carrying it failed on
that item for every attempt, the rows after it never imported and no error a
caller could fix. services/limits.py already held the rules; the doors had
stopped asking it the same questions.

So the write is here, in three steps, and each door takes the ones it needs:

- `checked`: every rule a memory is held to, and the end user it goes to;
- `admit`: the hosted service's ceilings (the agent names, the monthly
  writes); POST /v1/facts passes through it too, and POST /v1/batch once for
  the whole batch, when its job is queued;
- `store`: the engine's write, the facts inline when WRITE_ASYNC is off, the
  audit row and the `memory.created` webhook.

`write` is the three, for a door that writes one memory now (REST, MCP). The
batch API checks and admits at the door and its worker checks again and
stores, minutes later, with no key.

A refusal is the HTTPException REST answers, as the hooks raise theirs
(core/extensions.py) and core/auth.writing raises the 403: REST sends it as it
is, MCP turns it into its `error: <code>, <message>` line (mcp/server.refusal),
the batch worker into the item's error (`reason`). Each door keeps its own
answer shape; the decision is made once.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Sequence
from uuid import UUID, uuid4

from contextlib import contextmanager

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from . import limits, user_scope
from .clients import client_name
from .ids import mem_public_id, namespace_id_problem


# What kind of text a memory is (AgentMemory.kind, 18 Oct 2026): a note, the
# default and NULL on the row, or the log of a work session. None and "note"
# are the same answer at the door.
SESSION = "session"
KINDS = (None, "note", SESSION)


def session_kind(kind: Optional[str]) -> Optional[str]:
    """The row's value for a kind the door accepted: "session", or NULL."""
    return SESSION if kind == SESSION else None


@dataclass(frozen=True)
class MemoryWrite:
    """A memory write that passed every rule: its text stripped, its event
    time parsed, its end user resolved for the key that sent it, the name of
    the tool that wrote it read as services/clients.py reads it."""

    content: str
    end_user_id: Optional[str]
    agent_id: Optional[str]
    run_id: Optional[str]
    metadata: Optional[dict]
    event_time: Optional[datetime]
    client: Optional[str]
    # NULL a note, "session" a work session (`session_kind`).
    kind: Optional[str] = None

    def as_item(self) -> dict:
        """The item a batch job queues: what the worker reads back minutes
        later, the event time normalized so a bad one is never found there."""
        item = {"content": self.content, "user_id": self.end_user_id,
                "agent_id": self.agent_id, "run_id": self.run_id,
                "metadata": self.metadata,
                "timestamp": self.event_time.isoformat() if self.event_time else None}
        if self.client:
            item["client"] = self.client
        if self.kind:
            item["kind"] = self.kind
        return item


def _invalid(where: str, problem: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                         detail=f"{where}{problem}")


def require_event_time(s: Optional[str], field: str = "timestamp") -> Optional[datetime]:
    """The optional `timestamp` of a memory write, parsed, or a 422.

    `timestamp` is the event time: the facts extracted from the memory inherit
    it as valid_from, which is what makes an `as_of` query answer what was true
    then rather than what was written then. It is the feature the store exists
    for.

    So an unparseable value cannot be ignored. Ignoring it silently substitutes
    "now" for the date the caller asked for, the write succeeds, and every
    point-in-time query about that period is quietly wrong with nothing
    anywhere saying why. A 422 costs the caller one corrected request.

    Absent is different from unparseable and stays absent: no timestamp (or a
    blank one) means the caller did not claim to know one, and now is the
    right default.

    One rule and one sentence for every door that takes it: POST /v1/memories,
    each item of POST /v1/batch (`field` names it, `memories[3].timestamp`) and
    the MCP korely_add, which answers the same sentence as a tool error. The
    parser is services.facts.parse_event_time, the one the rest of the code
    uses. Here, not in api/_bodies.py where it was born, since the audit of
    17 Oct 2026: the write path is a service and a service imports no door.
    """
    from .facts import parse_event_time

    if not s or not s.strip():
        return None
    dt = parse_event_time(s)
    if dt is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(f"{field} {s!r} is not an ISO 8601 date or datetime "
                    "(e.g. '2026-03-01' or '2026-03-01T14:30:00Z')"),
        )
    # Readable is not storable: "0001-01-01T00:00:00+14:00" is the year 0 in
    # UTC, stored, then unreadable, and the drain stopped on it for every
    # account (audit of 20 Oct 2026). A later one is now already
    # (parse_event_time).
    problem = limits.moment_problem(field, dt)
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
    return dt


def checked_ids(*, user_id: Optional[str], agent_id: Optional[str], run_id: Optional[str],
                end_users: Optional[Sequence[str]] = None, where: str = "") -> Optional[str]:
    """The ids of a write, memory or fact: `user_id` and `agent_id` by the
    namespace rule (ids.NAMESPACE_ID_RULE), `run_id` bounded and without a
    NUL (422); then the end user the write goes to, inside a confined key's
    list (403, user_scope.for_write). Returns that end user. `where` prefixes
    every sentence with the place of the row (`memories[3].`)."""
    for field, value in (("user_id", user_id), ("agent_id", agent_id)):
        problem = namespace_id_problem(value) if value is not None else None
        if problem:
            raise _invalid(where, f"{field} {problem}")
    problem = limits.id_problem("run_id", run_id)
    if problem:
        raise _invalid(where, problem)
    try:
        return user_scope.for_write(end_users, user_id)
    except user_scope.OutOfScope as e:
        from ..core.auth import user_refusal

        refused = user_refusal(e.user_id)
        if where:
            refused.detail = f"{where}user_id: {refused.detail}"
        raise refused from None


def checked(content: Optional[str], *, user_id: Optional[str] = None,
            agent_id: Optional[str] = None, run_id: Optional[str] = None,
            metadata: Any = None, timestamp: Optional[str] = None,
            client: Optional[str] = None, end_users: Optional[Sequence[str]] = None,
            where: str = "", kind: Optional[str] = None) -> MemoryWrite:
    """Every rule a memory write is held to, in one order: the text, the
    metadata and the event time (422), then the ids and the key's end users
    (`checked_ids`). Raises before anything is read from the database or
    written; `end_users` None is a key of the whole project (or the batch
    worker, whose rows were placed at the door)."""
    problem = limits.content_problem(content) or limits.metadata_problem(metadata)
    if problem:
        raise _invalid(where, problem)
    # The rule and the sentence of every door: unreadable is a refusal,
    # never a silent "now".
    event_time = require_event_time(timestamp, f"{where}timestamp")
    if kind not in KINDS:
        raise _invalid(where, f"kind must be one of {', '.join(sorted(k for k in KINDS if k))}")
    user = checked_ids(user_id=user_id, agent_id=agent_id, run_id=run_id,
                       end_users=end_users, where=where)
    return MemoryWrite(content=(content or "").strip(), end_user_id=user, agent_id=agent_id,
                       run_id=run_id, metadata=metadata, event_time=event_time,
                       client=client_name(client), kind=session_kind(kind))


def admit(db: Session, tenant_id: UUID, contents: Sequence[str],
          agent_ids: Iterable[Optional[str]] = (), *, endpoint: Optional[str] = None,
          **row) -> None:
    """The hosted service's cap on agent names, then its monthly write quota
    (korely_agent/hosted/quotas.py, KORELY_MODE=hosted), for the writes about
    to be made: refused whole, or counted when the write commits. On an
    install of one's own both hooks do nothing: you pay for your own
    database and your own model calls. A refusal is in the trail
    (`_kept_in_the_trail`)."""
    from ..core.extensions import hooks

    with _kept_in_the_trail(db, tenant_id, endpoint, row):
        for agent_id in sorted({a for a in agent_ids if a}):
            hooks.before_new_agent_id(db, tenant_id, agent_id)
        hooks.before_write(db, tenant_id, list(contents))


def before_write(db: Session, tenant_id: UUID, contents: Sequence[str], *,
                 endpoint: Optional[str] = None, **row) -> None:
    """The monthly write quota alone, for a write that names no new agent
    (an edit, a correction); a refusal in the trail, as `admit`'s."""
    from ..core.extensions import hooks

    with _kept_in_the_trail(db, tenant_id, endpoint, row):
        hooks.before_write(db, tenant_id, list(contents))


@contextmanager
def _kept_in_the_trail(db: Session, tenant_id: UUID, endpoint: Optional[str], row: dict):
    """A refusal of the Cloud's quota or agent cap kept in the trail as the
    call `endpoint`, `error` with its token in `meta.code`, with `row` (the
    key, the end user, the target: services/usage.log_v1_request), as every
    other refusal of the same doors (api/errors.refused): the 429s of the
    month's writes and of the plan's agent names left no row (audit of 27 Oct
    2026)."""
    try:
        yield
    except HTTPException as e:
        if endpoint is not None:
            from .usage import log_v1_request

            detail = e.detail if isinstance(e.detail, dict) else {}
            code = detail.get("code") or getattr(e, "code", None) or "refused"
            log_v1_request(db, tenant_id, endpoint, result="error",
                           meta={**(row.pop("meta", None) or {}), "code": code}, **row)
        raise


def store(db: Session, tenant_id: UUID, project_id, w: MemoryWrite, *,
          end_users: Optional[Sequence[str]] = None, memory_id: Optional[UUID] = None,
          actor: str = "rest", api_key_id: Optional[UUID] = None,
          audit_meta: Optional[dict] = None):
    """Write `w` and say so: the memory, its facts inline when WRITE_ASYNC is
    off (`[]` otherwise: the drain extracts them), one audit row and one
    `memory.created` webhook. Returns (the memory row, the facts).

    The event time goes on the row (agent_memories.event_time), never into
    the developer's metadata: the worker that extracts later, or retries a
    failed inline extraction, reads it from there and dates the facts by it
    (valid_from, bi-temporal)."""
    from ..config import get_settings
    from . import facts as agent_facts
    from .memory_engine import AgentMemoryEngine
    from .usage import log_v1_request

    inline = not get_settings().write_async
    engine = AgentMemoryEngine(db, tenant_id, project_id, end_users=end_users)
    # The webhook is enqueued before the commit that writes the memory, so
    # the two commit together: enqueued after, with its own commit, a failure
    # between the two kept the memory and lost its event (audit of 17 Oct
    # 2026). Hence the id chosen here.
    memory_id = memory_id or uuid4()
    _announce(db, engine, memory_id, w)
    m = engine.add(
        w.content,
        end_user_id=w.end_user_id,
        agent_id=w.agent_id,
        run_id=w.run_id,
        metadata=w.metadata,
        event_time=w.event_time,
        memory_id=memory_id,
        extracting_now=inline,
        client=w.client,
        kind=w.kind,
    )
    # What a write that waits for its extraction answers with, taken before
    # it: an erasure that lands during the model call deletes the row, and a
    # rollback (the lost claim's) expires the object, which then cannot be
    # loaded again: reading `m` below was a 500 on a write that had worked
    # (found by tests/test_forget_during_extraction.py, Cognee #3766's shape).
    as_stored = _as_stored(m) if inline else None
    facts = agent_facts.extract_inline(db, tenant_id, m, valid_from=w.event_time) if inline else []
    if inline and _is_gone(m):
        m = as_stored
    # Who, which end user, which memory, in which project (GET /v1/audit
    # reads by project): a REST call's project is its key's, the MCP and the
    # worker have no key row and pass theirs.
    meta = dict(audit_meta or {})
    if w.client:
        meta["client"] = w.client
    log_v1_request(db, tenant_id, "memories:add", actor=actor, api_key_id=api_key_id,
                   project_id=project_id, end_user_id=m.end_user_id,
                   target_id=mem_public_id(m.id), meta=meta or None)
    return m, facts


def _as_stored(m):
    """The columns of a memory row as they are now, in an object that needs
    no session: what the write answers with if the row is erased before the
    answer is made."""
    from types import SimpleNamespace

    from sqlalchemy import inspect
    from sqlalchemy.exc import NoInspectionAvailable

    try:
        loaded = inspect(m).dict
    except NoInspectionAvailable:  # not a row (a stand-in): nothing to lose
        return m
    return SimpleNamespace(**{k: v for k, v in loaded.items() if not k.startswith("_sa_")})


def _is_gone(m) -> bool:
    """Whether the row of `m` has been deleted since it was written (an
    erasure from another request, during the extraction)."""
    from sqlalchemy.orm.exc import ObjectDeletedError

    if not hasattr(m, "_sa_instance_state"):
        return False
    try:
        m.end_user_id  # loads the object again if a rollback expired it
    except ObjectDeletedError:
        return True
    return False


def _announce(db: Session, engine, memory_id: UUID, w: MemoryWrite) -> None:
    """The one `memory.created` webhook of a write, whatever the door: the
    only place a memory write emits it. Enqueued for the memory
    `engine.add(..., memory_id=memory_id)` is about to write, before the
    commit that writes it, so the two commit together (`emit_event`). The
    project is the engine's, the one the row is stamped with; the row cannot
    be read for it yet."""
    from . import webhooks
    from .sentinels import EVERY_PROJECT

    project = None if engine.project_id is EVERY_PROJECT else engine.project_id
    webhooks.emit_event(db, engine.tenant_id, "memory.created", {
        "id": mem_public_id(memory_id), "user_id": w.end_user_id, "agent_id": w.agent_id,
    }, project)


def write(db: Session, tenant_id: UUID, project_id, content: Optional[str], *,
          user_id: Optional[str] = None, agent_id: Optional[str] = None,
          run_id: Optional[str] = None, metadata: Any = None,
          timestamp: Optional[str] = None, client: Optional[str] = None,
          end_users: Optional[Sequence[str]] = None, actor: str = "rest",
          api_key_id: Optional[UUID] = None, kind: Optional[str] = None):
    """One memory, written now: `checked`, `admit`, `store`. Returns (the
    memory row, the facts extracted inline)."""
    from ..core.auth import OutOfScopeRefusal

    try:
        w = checked(content, user_id=user_id, agent_id=agent_id, run_id=run_id,
                    metadata=metadata, timestamp=timestamp, client=client,
                    end_users=end_users, kind=kind)
    except OutOfScopeRefusal as e:
        # A write for an end user outside the key's list is an attempt on
        # that person's memory: in the trail, as every door's refusal
        # (api/errors.refused; audit of 19 Oct 2026), for REST and MCP alike.
        # A malformed write names nothing yet and writes no row.
        from .usage import log_v1_request

        log_v1_request(db, tenant_id, "memories:add", actor=actor, api_key_id=api_key_id,
                       project_id=project_id, end_user_id=e.user_id, result="error",
                       meta={"code": e.code})
        raise
    admit(db, tenant_id, [w.content], [w.agent_id], endpoint="memories:add", actor=actor,
          api_key_id=api_key_id, project_id=project_id, end_user_id=w.end_user_id)
    return store(db, tenant_id, project_id, w, end_users=end_users, actor=actor,
                 api_key_id=api_key_id)


def reason(e: HTTPException) -> str:
    """A refusal as one sentence, for a door that answers with text (the
    batch job's per-item errors)."""
    detail = e.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("code") or "refused")
    return str(detail)
