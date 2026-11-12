"""/v1 memory API, authenticated with this install's API keys (`kor_self_`).

The programmatic surface a developer's agents call. Operates on the agent
memory store (separate from the personal vault). Public namespace param is
`user_id` (the developer's END user, Mem0-style); internally that is the
`end_user_id` column. `agent_id` / `run_id` are sub-namespaces.

Public ids are prefixed (`mem_...`) per the published contract. Writes return
the facts extracted from the memory inline (`facts[]`); reads echo them too.

Reads need scope memories:read; writes need memories:write.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from ._bodies import Body, MetadataFilter, NamespaceId
from .facts import Fact
from .errors import SERVICE_PAUSED, V1_KEY_ROUTE_ERRORS, refused, service_paused, v1_errors

from sqlalchemy.orm import Session

from ..config import settings
from ..core.extensions import hooks
from ..core.threads import BUSY_RETRY_SECONDS, CALLS_PER_ACCOUNT, WAIT_SECONDS, on_a_request_thread
from ..core.auth import (a_turn_for_its_question, count_the_read, give_back_the_read,
                         ready_to_wait, require_scopes, require_read_with_quota,
                         enforce_rate_limit, key_end_users, key_project, reading, writing)
from ..db import get_db, AgentApiKey
from ..services import facts as agent_facts
from ..services import limits, memory_write
from ..services.clients import HEADER as CLIENT_HEADER
from ..services.ids import NAMESPACE_ID_RULE, mem_public_id, parse_mem_id
from ..services.facts import search as fact_search
from ..services.usage import log_v1_request
from ..services import user_scope
from ..services.facts.temporal import true_at
from ..services.sentinels import EndUsers, within_project
from ..services.memory_engine import (
    AgentMemoryEngine,
    MemorySearchUnavailable,
    StaleVersion,
    serialize_memory,
    version_problem,
)
from ..services.sentinels import EVERY_END_USER as _ALL

def _validate_content(v: str) -> str:
    """Strip + reject blank → clean 422 (never a 500). Done at the endpoint
    with HTTPException so it doesn't hit the app's RequestValidationError
    handler (which can't serialize a validator-raised ValueError). The rule
    is every door's (services/limits.py): a NUL byte arrived as a 500, the
    only one in a battery of 280,000 characters, emoji, invisible characters,
    Japanese and a string shaped like SQL."""
    problem = limits.content_problem(v)
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
    return (v or "").strip()


def validate_filters(**ids) -> None:
    """The ids a read narrows on, by the rule of every door
    (limits.id_problem): a NUL byte in one reached the driver and came back
    as a 500, where MCP refused it (audit of 17 Oct 2026)."""
    for field, value in ids.items():
        problem = limits.id_problem(field, value)
        if problem:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)


# Rate limit (F5) applies to every /v1 memory endpoint via a router dependency.
router = APIRouter(prefix="/v1", tags=["v1"], dependencies=[Depends(enforce_rate_limit)],
                   responses=V1_KEY_ROUTE_ERRORS)

_read_key = require_read_with_quota("memories:read")
_write_key = require_scopes("memories:write")


# ── schemas ───────────────────────────────────────────────────────────────
class AddBody(Body):
    content: str = Field(min_length=1, max_length=limits.MAX_CONTENT_CHARS)
    user_id: Optional[NamespaceId] = Field(
        default=None, max_length=limits.MAX_ID_CHARS,
        description="The end user this memory belongs to. " + NAMESPACE_ID_RULE)
    agent_id: Optional[NamespaceId] = Field(
        default=None, max_length=limits.MAX_ID_CHARS, description="Agent namespace. " + NAMESPACE_ID_RULE)
    run_id: Optional[str] = Field(default=None, max_length=limits.MAX_ID_CHARS)
    metadata: Optional[dict] = None
    kind: Literal["note", "session"] = Field(
        default="note",
        description="What the text is: `note` (the default), what somebody said or decided; "
                    "`session`, the log of a work session (what a coding assistant's hooks "
                    "save). A read may leave the sessions out, their facts with them "
                    "(`exclude_sessions`).")
    timestamp: Optional[str] = Field(
        default=None,
        description="ISO date/datetime the memory's events occurred (defaults to now). "
                    "Facts extracted inherit it as valid_from, so `as_of` point-in-time "
                    "queries reflect when things were true, not when they were ingested. "
                    f"A moment {limits.MOMENTS_STORED}; outside it, or unreadable, a 422.",
    )


class UpdateBody(Body):
    content: str = Field(min_length=1, max_length=limits.MAX_CONTENT_CHARS)
    expected_updated_at: Optional[str] = Field(
        default=None,
        description="Optimistic concurrency: the `updated_at` you read (`created_at` while `updated_at` "
                    "is null), sent back as the API returned it. The edit is written only if the memory "
                    "is still at that version, to the microsecond, when it is written; otherwise 409 "
                    "`stale_write` and nothing changes. A value that is not an ISO 8601 instant, the "
                    "empty string included, is a 422 and nothing changes; leave the field out for an "
                    "edit without the check.",
    )


class SearchBody(Body):
    query: str = Field(min_length=1, max_length=limits.MAX_QUERY_CHARS)
    user_id: Optional[str] = Field(default=None, max_length=limits.MAX_ID_CHARS)
    agent_id: Optional[str] = Field(default=None, max_length=limits.MAX_ID_CHARS)
    run_id: Optional[str] = Field(default=None, max_length=limits.MAX_ID_CHARS,
                                  description="Scope to one run/session.")
    metadata: MetadataFilter = Field(
        default=None,
        description="Filter on the metadata you stored at write time, e.g. "
                    "{\"tier\": \"pro\"}. Keys are ANDed. A value matches the stored value "
                    "compared as text (true, false and numbers as JSON writes them, so "
                    "\"5\" matches 5), a number also matches an equal number (5 and 5.0), "
                    "and null matches a stored null. Strings, numbers, booleans and null "
                    "only: an object or an array is a 422. At most "
                    f"{limits.MAX_METADATA_FILTER_KEYS} keys and "
                    f"{limits.MAX_METADATA_BYTES:,} bytes, what stored metadata may hold: "
                    "past either it is a 422.",
    )
    limit: int = Field(default=limits.DEFAULT_SEARCH_RESULTS, ge=1, le=limits.MAX_SEARCH_RESULTS)
    exclude_sessions: bool = Field(default=False, description="Leave out the work sessions (memories written with `kind: session`, what a "
                    "coding assistant's hooks save), for a read that wants what was said "
                    "and decided. Default false: everything.")
    min_score: Optional[float] = Field(
        default=None, ge=limits.MIN_SCORE_MIN, le=limits.MIN_SCORE_MAX, strict=True,
        # What the parameter does and its bounds; the value measured for
        # it, and on what, is in the CHANGELOG entry that brought it (audit
        # of 27 Oct 2026: the public spec quoted it as guidance).
        description="Leave out the memories whose `score` is below this, 0 to 1 (a cosine "
                    "similarity, compared as the response shows it). Absent: no floor, every "
                    "nearest memory comes back. Fewer than `limit` results can come back, none "
                    "when nothing in the store is that close. A higher floor drops more of the "
                    "weak matches and, past some value, true ones too: the right value depends "
                    "on your texts and on the embedding model.")
    types: list[fact_search.Kind] = Field(
        default=list(fact_search.DEFAULT_TYPES), min_length=1,
        max_length=len(fact_search.KINDS),
        description="What to search, a list of kinds, each once: `memories` (the default, "
                    "alone) the memories, as before 1.0.13; `facts` the facts true now (those "
                    "GET /v1/facts lists, the ones written with POST /v1/facts included; one "
                    "still waiting for its vector is listed there and found once it has it), "
                    "by the cosine of their own vector. With both, each kind is ranked by its "
                    "own score and the rankings are merged by rank (reciprocal rank fusion, "
                    "k = 60: the first memory, the first fact, the second memory...), not by "
                    "score: how a fact's cosine compares with a memory's was not measured. "
                    "The order of the list does not matter; at equal rank a memory comes "
                    "first, so with `limit: 1` the best memory is the answer whenever there "
                    "is one. When the request names `types`, each result says its `type`. "
                    "`run_id`, `metadata` and `min_score` narrow the memories only: with "
                    "`facts` in the list each of them is a 422.")
    full_text: bool = Field(
        default=False,
        description="Also return each result's whole text as `content`: a memory's as it is "
                    "stored, a fact's line (`subject · predicate · object`, its dates). "
                    f"`snippet` stays its first {limits.SNIPPET_CHARS} characters. Default "
                    "false: no `content` in the results. A memory can hold "
                    f"{limits.MAX_CONTENT_CHARS:,} characters, so a page of {limits.MAX_SEARCH_RESULTS} "
                    "whole memories can be large.")

class Memory(BaseModel):
    id: str
    content: str
    user_id: Optional[str]   # the end user (engine end_user_id)
    agent_id: Optional[str]
    run_id: Optional[str]
    client: Optional[str] = Field(
        default=None,
        description="The tool that wrote it: the X-Korely-Client header of the write, or the "
                    "name an MCP client gave when it connected. A label, not a namespace: no "
                    "read is scoped by it and the agent cap does not count it.")
    kind: str = Field(
        default="note",
        description="`note`, or `session` for the log of a work session (`kind` of the write).")
    metadata: dict
    status: str = Field(
        default="processing",
        # No webhook says that extraction finished: this named a
        # `fact_extracted` one, which no code sends and POST /admin/webhooks
        # refuses (audit of 17 Oct 2026).
        description="Whether this memory's typed facts are queryable yet: "
                    "'processing', 'ready', or 'error'. Extraction runs after the "
                    "write returns, so a read taken immediately can legitimately "
                    "find no facts. Poll GET /v1/events or read the memory again: "
                    "no webhook says that extraction finished (`memory.created` "
                    "is sent when the memory is stored, before its facts).",
    )
    created_at: Optional[str]
    updated_at: Optional[str]


class MemoryWithFacts(Memory):
    facts: list[dict] = []


class UserErasureResponse(BaseModel):
    """DELETE /v1/users/{end_user}/memories: what was physically deleted.
    Field order is the order on the wire."""
    user_id: str = Field(description="The end user that was erased.")
    memories_deleted: int = Field(description="Memories physically deleted.")
    facts_deleted: int = Field(description="Facts physically deleted.")
    erasure: str = Field(description="Always `permanent`.")
    audit_id: str = Field(description="The `aud_` id of the audit row, which keeps the counts "
                                      "and a keyed pseudonym of your end user id, never the "
                                      "erased content.")


class SearchResult(BaseModel):
    """One result of a search. `type`, `content` and `fact` are in it only
    when the request asks for what they say (`types`, `full_text`, a fact
    found): a result is otherwise what it was before 1.0.13."""
    id: str = Field(description="The memory's `mem_` id, or the fact's `fct_` id.")
    score: float = Field(description=(
        "The cosine similarity of the question's vector and the result's, rounded to four "
        "places: from -1 to 1, about 0 for a text unrelated to the question, and slightly "
        "negative for some. A search returns the nearest results up to `limit` whether or "
        "not anything is close: a result is not an answer, and without `min_score` nothing "
        "is left out for being far. A fact's score and a memory's are not to be compared."))
    snippet: str = Field(description=f"The first {limits.SNIPPET_CHARS} characters of the "
                                     f"memory, or of the fact's line.")
    user_id: Optional[str] = Field(description="The end user of the memory or of the fact; "
                                               "null for none.")
    agent_id: Optional[str] = Field(description="The agent namespace of the memory or of the "
                                                "fact; null for none.")
    metadata: dict = Field(description="The metadata stored with the memory; a fact has "
                                       "none, `{}`.")
    type: Optional[Literal["memory", "fact"]] = Field(
        default=None, description="`memory` or `fact`: in every result when the request "
                                  "names `types`, absent otherwise.")
    content: Optional[str] = Field(
        default=None, description="The whole text: the memory's as it is stored, or the "
                                  "fact's line. Only with `full_text: true`.")
    fact: Optional[Fact] = Field(
        default=None, description="A fact's result: the fact as GET /v1/facts shows it. "
                                  "Absent for a memory.")


class SearchResponse(BaseModel):
    results: list[SearchResult]


class MemoryList(BaseModel):
    memories: list[Memory]
    total: int


class HistoryEvent(BaseModel):
    event: str            # created | updated | fact_extracted | fact_invalidated | deleted
    at: Optional[str]
    fact: Optional[str] = None       # "subject predicate object" for fact events
    fact_id: Optional[str] = None


class MemoryHistory(BaseModel):
    id: str
    events: list[HistoryEvent]


class UserScope(BaseModel):
    user_id: str
    memories: int
    facts: int
    last_active: Optional[str]


class UsersResponse(BaseModel):
    users: list[UserScope]
    total: int


class AgentScope(BaseModel):
    agent_id: str
    memories: int
    facts: int
    last_active: Optional[str]


class AgentsResponse(BaseModel):
    agents: list[AgentScope]
    total: int      # distinct agent namespaces of this key's view
    # What the ceiling on new agent names is and how much of it is taken,
    # from `hooks.agent_cap`: an SDK reads these two to know whether a new
    # name will be accepted. An install of one's own sets no ceiling (0); the
    # hosted plans cap the names of the whole account, and this said 0 there
    # too, with `used` counted in the key's project (audit of 17 Oct 2026).
    cap: int = Field(description="The most agent names a write may bring in: 0 when nothing caps "
                                 "them, as on an install of one's own. A write under a new name "
                                 "past it is refused.")
    used: int = Field(description="The agent names `cap` counts, over the set it caps (the whole "
                                  "account where a plan caps them); with no ceiling, the names "
                                  "this key sees, which `total` counts.")


def public_memory(mem: dict) -> dict:
    """Engine dict → public shape: end_user_id→user_id, raw uuid→mem_ id.
    The dashboard's /admin/memories routes answer it too: they answered the
    engine's dict, and the screen, written to this shape, found no
    `user_id` and no `facts` (audit of 17 Oct 2026)."""
    out = dict(mem)
    out["user_id"] = out.pop("end_user_id", None)
    out["id"] = mem_public_id(out["id"])
    return out


def _engine(db: Session, key) -> AgentMemoryEngine:
    """The memory engine of this key: its project, and its end users when it
    is confined to some (services/user_scope.py). Built here and nowhere else
    in this module, so no route can forget the second wall."""
    return AgentMemoryEngine(db, key.tenant_id, key_project(db, key), end_users=key_end_users(key))


def _require_mem_uuid(db: Session, key, endpoint: str, memory_id: str):
    """Parse a public mem_ id (or bare uuid) → uuid str, or 404, kept in the
    trail as the call `endpoint` (api/errors.refused; audit of 19 Oct 2026).
    With no target: what does not parse is not an id, and the trail keeps
    ids only."""
    u = parse_mem_id(memory_id)
    if u is None:
        raise refused(db, key, endpoint, HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"))
    return str(u)


# ── write ─────────────────────────────────────────────────────────────────
@router.post("/memories", response_model=MemoryWithFacts, status_code=status.HTTP_201_CREATED)
def add_memory(
    body: AddBody,
    key: AgentApiKey = Depends(_write_key),
    db: Session = Depends(get_db),
    x_korely_client: Optional[str] = Header(
        default=None, alias=CLIENT_HEADER,
        description="The tool writing, e.g. claude-code: kept on the memory as `client`. "
                    "Not a namespace and not counted in the agent cap."),
) -> MemoryWithFacts:
    """Store a memory for an end user (`user_id`), an agent (`agent_id`) or a
    run (`run_id`).

    The call returns once the memory is stored. Its typed facts are extracted
    by a model after that, so the reply says `status: processing` with
    `facts: []`; a server run with `WRITE_ASYNC=false` extracts before
    answering and returns the facts inline. Poll `GET /v1/events` or read the
    memory again to see them.

    `timestamp` is when the events happened, not when you send them: the
    facts take it as `valid_from`, so reads with `as_of` answer about the
    right date. An unreadable one is a 422, never a silent "now".
    """
    # The write of every door (services/memory_write.py): the rules, this
    # key's end users (403 outside its list; its single id when it names
    # none), the hosted service's cap on agent names and its monthly quota,
    # the write, the facts inline when WRITE_ASYNC is off, the audit row and
    # the webhook. It was written out here by hand, and had drifted from
    # MCP's: a NUL byte in run_id was a 500 here (audit of 17 Oct 2026).
    m, facts = memory_write.write(
        db, key.tenant_id, key_project(db, key), body.content,
        user_id=body.user_id, agent_id=body.agent_id, run_id=body.run_id,
        metadata=body.metadata, timestamp=body.timestamp, client=x_korely_client,
        end_users=key_end_users(key), api_key_id=key.id, kind=body.kind)
    return MemoryWithFacts(**public_memory(serialize_memory(m)), facts=facts)


@router.patch("/memories/{memory_id}", response_model=MemoryWithFacts,
              responses=v1_errors(404, 409))
def update_memory(
    memory_id: str,
    body: UpdateBody,
    key: AgentApiKey = Depends(_write_key),
    db: Session = Depends(get_db),
) -> MemoryWithFacts:
    """Replace a memory's text.

    Its facts are extracted again from the new text and go through the same
    contradiction check as a new write: a value that changed supersedes the
    fact the old text stated, which stays in history (`as_of` before the edit
    still returns it). Like a new write, the reply says `processing` with
    `facts: []` until the extraction has run. The text the memory already
    has, once extracted, is not an edit: nothing is written, extracted or
    closed, and the reply is the memory as it is.

    `expected_updated_at` makes the edit conditional on the version you read:
    409 `stale_write`, and nothing changes, when the memory moved since; 422
    when it is not an instant (an empty string included).
    """
    content = _validate_content(body.content)
    # A version that cannot be read is a 422 before anything is looked up:
    # answered 409 `stale_write`, a client retrying on it looped for ever,
    # and '' switched the check off (audit of 17 Oct 2026).
    problem = version_problem(body.expected_updated_at)
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
    mid = _require_mem_uuid(db, key, "memories:update", memory_id)
    # New text is extracted again: a write for the hosted quota (counted when
    # the update commits, so a 404 or a 409 costs nothing).
    # A refusal of the quota is in the trail as every other (memory_write.admit).
    memory_write.before_write(db, key.tenant_id, [content], endpoint="memories:update",
                              api_key_id=key.id, target_id=mem_public_id(mid))
    engine = _engine(db, key)
    # Optimistic concurrency (409 stale_write) when the caller pins a version:
    # the exact version, held by the write itself up to its commit
    # (AgentMemoryEngine.update).
    try:
        updated = engine.update(mid, content, extracting_now=not settings.write_async,
                                version=body.expected_updated_at)
    except StaleVersion:
        # In the trail as `error`, with the memory and its end user (api/
        # errors.refused; audit of 18 Oct 2026): the memory is in
        # the key's reach, the version only was not.
        stale = engine._get_row(mid)
        raise refused(db, key, "memories:update", HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "stale_write",
                    "message": "expected_updated_at does not match the current record."},
        ), end_user_id=stale.end_user_id if stale is not None else None,
            target_id=mem_public_id(mid))
    if updated is None:
        raise refused(db, key, "memories:update", HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"),
            target_id=mem_public_id(mid))
    # Re-run fact extraction on the new content — the documented contract ("PATCH
    # re-runs extraction; facts stay in sync"). The freshly extracted facts go
    # through the SAME contradiction pipeline as add(): a changed value (e.g.
    # "€30" -> "€45") supersedes the prior fact from this memory (its invalid_at is
    # set, so get_facts() returns the current value); unchanged facts are deduped.
    #
    # When, as on POST: WRITE_ASYNC=true (the default) leaves it to the worker,
    # since engine.update left the memory 'pending', and answers `processing`
    # with facts:[]; WRITE_ASYNC=false extracts inline. Until 2026-09-28 an edit
    # extracted inline whatever the setting said, holding the request for the
    # model call a write is spared. Both modes date the facts by the memory's
    # event time (`agent_memories.event_time`, the timestamp of the write that
    # made it), as the worker does: an inline edit used to date them "now", so
    # the same edit gave a different history in each mode.
    row = engine._get_row(mid)
    facts: list[dict] = []
    if row is not None and not settings.write_async and row.graph_sync_status != "done":
        facts = agent_facts.extract_inline(db, key.tenant_id, row,
                                           valid_from=agent_facts.event_time_of(row))
        # After the extraction, as POST answers: `ready` once it is.
        updated = serialize_memory(row)
    # Who and what: the end user the memory is about and the memory itself
    # (bussola 2e; this wrote neither until 2026-10-01).
    log_v1_request(db, key.tenant_id, "memories:update", api_key_id=key.id,
                   end_user_id=row.end_user_id if row is not None else None,
                   target_id=mem_public_id(mid))
    return MemoryWithFacts(**public_memory(updated), facts=facts)


@router.delete("/memories/{memory_id}", status_code=status.HTTP_200_OK,
               responses=v1_errors(404))
def delete_memory(
    memory_id: str,
    erase: bool = Query(default=False, description=(
        "Erase it for good: the row, its text, observation and vector, and the facts it "
        "alone stated go from the database, with its item in a /v1/batch payload and what "
        "its facts taught the project's vocabulary and aliases. A memory forgotten earlier "
        "can be erased too. Without it the memory is forgotten: it drops out of every "
        "read, its facts stop being true, and the row stays.")),
    key: AgentApiKey = Depends(_write_key),
    db: Session = Depends(get_db),
) -> dict:
    """Forget one memory, or erase it with `erase=true`.

    Forgetting drops the memory out of every read and ends the facts it
    stated, which stay in history, closed. The row, with its text and vector,
    stays in the database, and no call brings the memory back.

    `erase=true` deletes it for good: the row, its text and vector, the
    facts only it stated, its item in a `/v1/batch` payload, and the aliases
    and vocabulary examples its facts alone taught. A memory forgotten
    earlier is erased the same way (a forget answers 404 for it). Either way
    an audit row keeps the counts, never the content. On a Self-hosted
    install, `restore.sh` makes the erasure again after a restore of a backup
    taken before it (`manage erasures-export` before and `erasures-replay`
    after); a restore done another way does not. To erase everything of one
    end user, use `DELETE /v1/users/{end_user}/memories`.
    """
    # Erasing one memory arrived on the audit of 17 Oct 2026: a forgotten
    # memory kept its text, observation and vector for ever, and only a whole
    # end user, agent or project could be erased.
    from uuid import UUID as _UUID

    mid = _require_mem_uuid(db, key, "memories:delete", memory_id)
    # The end user, read before the memory goes and behind the project wall:
    # a memory of another project must not put its end user in this trail.
    # A forgotten memory too, for an erasure: it can be erased after its
    # forget (audit of 18 Oct 2026: it answered 404).
    engine = _engine(db, key)
    before = engine.get_row_with_history(mid) if erase else engine._get_row(mid)
    if before is None and key_end_users(key) is not None:
        # Another end user's memory, for a confined key: not found, before
        # the forget underneath (walled by project only) can reach it.
        raise refused(db, key, "memories:delete", HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"),
            target_id=mem_public_id(mid))
    # Read before anything goes: after an erasure the row is not there to read.
    end_user_id = before.end_user_id if before is not None else None
    # The forget and the erasure in one transaction (`erase_memory`): an
    # erasure that failed after its forget had committed left the memory
    # forgotten, and erase=true answered 404 to the retry.
    act = agent_facts.erase_memory if erase else agent_facts.forget_memory
    result = act(db, key.tenant_id, _UUID(mid), project_id=key_project(db, key))
    # A deletion that found nothing is in the trail as what it was, `error`
    # with its code, as every refused /v1 call (api/errors.refused): it said
    # `ok` until the audit of 17 Oct 2026, then `error` with no code, the
    # one /v1 404 a filter on `meta.code` missed (audit of 20 Oct 2026).
    meta = {"erase": True} if erase else None
    if result is None:
        raise refused(db, key, "memories:delete", HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"),
            end_user_id=end_user_id, target_id=mem_public_id(mid), meta=meta)
    # The counts too, as the MCP tool keeps them: numbers, never content.
    counts = {k: result[k] for k in ("facts_invalidated", "facts_deleted") if k in result}
    if result.get("facts_reopened"):
        # A count only (31 Oct 2026): the facts the deletion made true again,
        # each a new row (services/facts/given_back.py).
        counts["facts_reopened"] = int(result["facts_reopened"])
    log_v1_request(db, key.tenant_id, "memories:delete", api_key_id=key.id,
                   end_user_id=end_user_id, target_id=mem_public_id(mid),
                   meta={**(meta or {}), **counts})
    return result


# `:path`, not the default converter (2026-09-28): the default stops at '/',
# and an end user id holding one ("acme/42") was writable, so that end user
# could never be erased. The converter takes the rest of the path up to the
# final `/memories`, so every id already stored is reachable, whatever it
# holds; new ids with a '/' are refused on write (services.ids.NAMESPACE_ID_RULE).
# The published path is unchanged: FastAPI documents it as `{end_user}`.
@router.delete("/users/{end_user:path}/memories", status_code=status.HTTP_200_OK,
               response_model=UserErasureResponse)
def delete_user_memories(
    end_user: str,
    key: AgentApiKey = Depends(_write_key),
    db: Session = Depends(get_db),
) -> dict:
    """ERASE everything about one end user in one call: every memory and fact.

    Physical deletion, not a flag: after this call the content is gone from the
    database and cannot be read back by any parameter, `include_invalidated`
    included. Unrecoverable by design, because that is what erasure means.

    `end_user` is the `user_id` exactly as written, percent-encoded as one path
    segment (`acme/42` is `acme%2F42`). Any stored id is accepted here,
    including one written before new ids were required to be a single segment.

    The response counts rows physically deleted: `memories_deleted` and
    `facts_deleted` (until 0.1.42 also under their first names,
    `memories_forgotten` and `facts_invalidated`, removed in 0.2.0).

    The audit row survives, carrying counts and a keyed pseudonym of your
    `end_user_id`, none of the erased content, so you can evidence the erasure
    to your own regulator: `GET /v1/audit?user_id=<the same id>` finds it. The
    rest of the access trail takes the pseudonym too, a webhook already
    delivered takes the pseudonym, and the cost rows of the model calls never
    name an end user.

    Within the key's project: the same id in another project of the account
    stays there (it may be another customer's person). To erase it in every
    project at once, the account's administrator calls
    `DELETE /admin/users/{end_user}/memories`.

    For a single memory, `DELETE /v1/memories/{memory_id}` forgets it (out of
    every read, the row kept, and no call brings it back) or, with
    `erase=true`, erases it."""
    # By the rule of every id (limits.id_problem): a NUL byte, which no stored
    # id can hold, reached the driver and was a 500 (audit of 18 Oct 2026).
    validate_filters(end_user=end_user)
    writing(key, end_user, db=db, endpoint="users:delete")  # a confined key erases only its own end users (403)
    from ..services.ids import ERASED_PREFIX, is_pseudonym

    if is_pseudonym(end_user):
        # A pseudonym is what an erasure left: erasing it would only rename the
        # records that prove the erasure (audit of 17 Oct 2026). The shape, not
        # the prefix: an id written before the prefix was reserved stays
        # erasable (audit of 18 Oct 2026).
        raise refused(db, key, "users:delete", HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_request",
             "message": f"{ERASED_PREFIX!r} names an end user already erased; nothing to erase."}),
            end_user_id=end_user)
    result = agent_facts.forget_end_user(db, key.tenant_id, end_user,
                                        project_id=key_project(db, key))
    # Written after the erasure: with the pseudonym, as the rows it left.
    from ..services.pseudonyms import pseudonym

    log_v1_request(db, key.tenant_id, "users:delete", api_key_id=key.id,
                   end_user_id=pseudonym(key.tenant_id, end_user),
                   meta={"memories": result.get("memories_deleted"),
                         "facts": result.get("facts_deleted")})
    return result


# ── read ──────────────────────────────────────────────────────────────────
#: When search answers 503 `search_unavailable`, from the embedder's bounds
#: (core/threads, core/local_embedder._Turns): since 0.2.1 an account's
#: places there are shared by its searches and the vectors of its writes,
#: and a request also gives up its turn after a wait (audit of 27 Oct 2026:
#: this said "four searches", 0.2.0's rule). Since 31 Oct 2026 a search
#: past its account's places waits for one instead of being refused
#: (core/auth.a_turn_for_its_question; the finding of
#: scripts/concurrent_agents.sh): refused only once it has waited.
SEARCH_UNAVAILABLE = (
    "`search_unavailable`: the query could not be embedded, so semantic search could not "
    "run. Either the embedding provider did not answer, or, with the local embedder, the "
    f"search waited {WAIT_SECONDS:.0f} s for one of its account's {CALLS_PER_ACCOUNT} places "
    "there, per process (its searches, its contexts and the vectors of its writes share "
    "them, over REST and MCP; a search past them waits its turn), or "
    f"{WAIT_SECONDS:.0f} s more for its turn there. Not an empty result; retry after "
    "`Retry-After`. It takes no query of the month.")


# `response_model_exclude_unset`: a result carries `type`, `content` and
# `fact` only when it was given them (`search_result`), so a search that asks
# for neither answers as it did before 1.0.13, byte for byte.
@router.post("/memories/search", response_model=SearchResponse,
             response_model_exclude_unset=True,
             responses=v1_errors(503, descriptions={
                 503: SEARCH_UNAVAILABLE + " " + SERVICE_PAUSED}))
async def search_memories(
    body: SearchBody,
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
) -> SearchResponse:
    """The memories closest in meaning to `query`, best first; with `types`,
    the facts too, or them only.

    `score` is the cosine similarity of the vectors and `snippet` the first
    280 characters of the memory, or of the fact's line; `full_text: true`
    adds the whole text as `content`. Narrow it with `user_id`, `agent_id`,
    `run_id` and the `metadata` stored at write time; `min_score` leaves out
    the weak matches. A forgotten memory is never returned, nor a fact that
    is no longer true. With `types: ["memories", "facts"]` the two kinds
    are each ranked by their own score and merged by rank, not by score;
    each result then says its `type`, and a fact's carries the fact as GET
    /v1/facts shows it.

    503 `search_unavailable` when the query could not be embedded (see the
    503 below): that is not an empty result, retry. A search that could not
    run takes no query of the month.
    \f
    Async since 31 Oct 2026, its work on request threads before and after
    it waits on the loop for its turn at the embedder
    (core/auth.a_turn_for_its_question): past its account's places a search
    waits instead of being refused, and is refused for anything else before
    it waits. The facts since 1.0.13 (services/facts/search.py).
    """
    def checked():
        # The question by MCP's rule (limits.query_problem): a blank one was a
        # 200 with no results here and a refusal there (audit of 17 Oct 2026).
        problem = limits.query_problem(body.query)
        if problem:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
        validate_filters(user_id=body.user_id, agent_id=body.agent_id, run_id=body.run_id)
        # A kind named twice, or a filter of the memories alone asked of a
        # search with the facts, refused before the turn at the embedder as
        # every other refusal.
        problem = (fact_search.types_problem(body.types)
                   or fact_search.combination_problem(body.types, run_id=body.run_id,
                                                      metadata=body.metadata,
                                                      min_score=body.min_score))
        if problem:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
        eu: Any = reading(key, body.user_id, _ALL, db=db, endpoint="memories:search")
        count_the_read(db, key)  # once the request is one it answers (audit of 17 Oct 2026)
        return eu, ready_to_wait(db, key)

    def searched(eu: Any) -> SearchResponse:
        engine = _engine(db, key)
        try:
            hits = fact_search.search(engine, body.query, types=body.types, end_user_id=eu,
                                      agent_id=body.agent_id, run_id=body.run_id,
                                      metadata=body.metadata, limit=body.limit,
                                      min_score=body.min_score,
                                      exclude_sessions=body.exclude_sessions)
        except MemorySearchUnavailable as e:
            # Not answered: the query counted above is given back, a
            # service stop's as a busy embedder's.
            give_back_the_read(key.tenant_id)
            # The attempt is in the trail, as `error` (audit of 18 Oct 2026).
            if e.paused:
                raise refused(db, key, "memories:search", service_paused(),
                              end_user_id=body.user_id)
            # With when to ask again (31 Oct 2026): the same wait as every 503
            # of a busy resource (core/threads.BUSY_RETRY_SECONDS).
            raise refused(db, key, "memories:search", HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "search_unavailable",
                    "message": "Memory search is temporarily unavailable. Please retry.",
                },
                headers={"Retry-After": str(BUSY_RETRY_SECONDS)},
            ), end_user_id=body.user_id)
        # What the search gave back, by id, and whose memory it searched (bussola
        # 2e): the trail says what the agent was shown, never the query.
        log_v1_request(db, key.tenant_id, "memories:search", api_key_id=key.id,
                       end_user_id=body.user_id, **fact_search.reads(hits, body.types))
        return SearchResponse(results=[search_result(h, body) for h in hits])

    eu, account = await on_a_request_thread(checked)
    async with a_turn_for_its_question(account):
        return await on_a_request_thread(searched, eu)


def search_result(h: dict, body: SearchBody) -> SearchResult:
    """One hit of the search (services/facts/search.search) as a result: the
    six fields of before 1.0.13, and only what `body` asks for besides
    (`type` when it names `types`, `content` with `full_text`) or what the
    hit is (`fact`), the fields the route's answer leaves out when unset."""
    text = h.get("content") or ""
    extra: dict = {}
    if "types" in body.model_fields_set:
        extra["type"] = h["type"]
    if body.full_text:
        extra["content"] = text
    if h.get("type") == fact_search.FACT:
        extra["fact"] = h["fact"]
    return SearchResult(id=fact_search.public_id(h), score=h.get("score", 0.0),
                        snippet=text[:limits.SNIPPET_CHARS], user_id=h.get("end_user_id"),
                        agent_id=h.get("agent_id"), metadata=h.get("metadata") or {}, **extra)


@router.get("/memories", response_model=MemoryList)
def list_memories(
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
    user_id: Optional[str] = Query(default=None, max_length=limits.MAX_ID_CHARS, description="Filter to one end user."),
    agent_id: Optional[str] = Query(default=None, description="Filter to one agent namespace."),
    run_id: Optional[str] = Query(default=None, max_length=limits.MAX_ID_CHARS, description="Filter to one run/session."),
    limit: int = Query(default=50, ge=1, le=limits.MAX_PAGE),  # docs: default 50, max 200 (matches /v1/facts)
    offset: int = Query(default=0, ge=0, le=limits.MAX_OFFSET),
    exclude_sessions: bool = Query(default=False, description="Leave out the work sessions (memories written with `kind: session`, what a "
                    "coding assistant's hooks save), for a read that wants what was said "
                    "and decided. Default false: everything."),
) -> MemoryList:
    """The memories of this key's project, newest first, a page at a time
    (`limit`, `offset`); `total` counts them all. Narrow it with `user_id`,
    `agent_id` and `run_id`. Forgotten memories are not listed.

    Each memory's `status` says whether its facts are ready; the facts are on
    `GET /v1/memories/{memory_id}` and `GET /v1/facts`.
    """
    validate_filters(user_id=user_id, agent_id=agent_id, run_id=run_id)
    engine = _engine(db, key)
    eu: Any = reading(key, user_id, _ALL, db=db, endpoint="memories:list")
    count_the_read(db, key)  # once the request is one it answers
    rows, total = engine.get_all(end_user_id=eu, agent_id=agent_id, run_id=run_id,
                                 limit=limit, offset=offset, exclude_sessions=exclude_sessions)
    log_v1_request(db, key.tenant_id, "memories:list", api_key_id=key.id,
                   end_user_id=eu if isinstance(eu, str) else None, meta={"rows": total},
                   memories_read=[mem_public_id(r["id"]) for r in rows])
    return MemoryList(memories=[Memory(**public_memory(r)) for r in rows], total=total)


class MemoryEvent(BaseModel):
    memory_id: str
    user_id: Optional[str]
    agent_id: Optional[str]
    status: str
    created_at: Optional[str]


class EventsResponse(BaseModel):
    events: list[MemoryEvent]
    processing: int = Field(
        description="Writes of this key's project (of `user_id`, when given) still in flight, "
                    "counted over all of them. Independent of `status` and `limit`.")


@router.get("/events", response_model=EventsResponse)
def list_events(
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
    user_id: Optional[str] = Query(default=None, max_length=limits.MAX_ID_CHARS, description="Filter to one end user."),
    memory_status: Optional[str] = Query(
        default=None, alias="status", pattern="^(processing|ready|error)$",
        description="Filter to one processing state.",
    ),
    limit: int = Query(default=50, ge=1, le=limits.MAX_PAGE),
) -> EventsResponse:
    """Which writes have finished being processed.

    A write returns as soon as the memory is stored, then fact extraction runs
    behind it. Without this endpoint a developer polls blind: they cannot tell
    "extraction has not finished" from "there was nothing to extract", which is
    the first confusing thing anyone hits when building on us.

    `events` are the newest writes, newest first; `status` filters them before
    `limit` is applied, so `?status=error` returns the most recent errors
    however many ready writes came after them.

    `processing` in the response is how many writes of this key's project (of
    `user_id`, when given) are still in flight, counted over all of them, so a
    batch import can wait on one number instead of walking every id. It ignores
    `status` and `limit`.

    No webhook announces that a write's extraction finished: `memory.created`
    is sent when the memory is stored, before its facts, and
    `GET /admin/webhooks/events` lists the events there are. Waiting for the
    facts is polling this, or reading the memory again.
    """
    # It said "prefer the `fact_extracted` webhook", an event no code sends
    # and an endpoint cannot subscribe to (audit of 17 Oct 2026).
    validate_filters(user_id=user_id)
    engine = _engine(db, key)
    eu: Any = reading(key, user_id, _ALL, db=db, endpoint="events:list")
    count_the_read(db, key)  # once the request is one it answers
    # 2026-09-28. The status filter used to run on the page after the LIMIT,
    # and `processing` was counted among the newest rows only (200 asked, 100
    # returned by get_all's cap): a backlog older than that read as zero, which
    # is the one number a batch import waits on.
    rows, _ = engine.get_all(end_user_id=eu, status=memory_status, limit=limit)
    events = [
        MemoryEvent(
            memory_id=mem_public_id(r["id"]),
            user_id=r.get("end_user_id"),
            agent_id=r.get("agent_id"),
            status=r.get("status", "processing"),
            created_at=r.get("created_at"),
        )
        for r in rows
    ]
    in_flight = engine.count(end_user_id=eu, status="processing")
    # Whose memories and which, as memories:list records them: it wrote the
    # label alone, so "who was shown this person's memories" missed every
    # poll of the events (audit of 18 Oct 2026).
    log_v1_request(db, key.tenant_id, "events:list", api_key_id=key.id,
                   end_user_id=eu if isinstance(eu, str) else None,
                   memories_read=[e.memory_id for e in events])
    return EventsResponse(events=events, processing=in_flight)


@router.get("/memories/{memory_id}", response_model=MemoryWithFacts,
            responses=v1_errors(404))
def get_memory(
    memory_id: str,
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
) -> MemoryWithFacts:
    """One memory, with the facts it asserts that are true now. The ones a
    later fact superseded are its history: GET /v1/memories/{id}/history."""
    from uuid import UUID as _UUID

    mid = _require_mem_uuid(db, key, "memories:get", memory_id)
    count_the_read(db, key)  # once the request is one it answers
    engine = _engine(db, key)
    mem = engine.get(mid)
    if mem is None:
        # The attempt is kept, as `error`, never as a read that happened
        # (audit of 17 Oct 2026), by the helper every refusal uses since.
        raise refused(db, key, "memories:get", HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"),
            target_id=mem_public_id(mid))
    # The facts true now, as the hosted service answers and as the SDK reads
    # them: a memory's superseded facts came back too, so "Maria lives in
    # Rome" sat next to "Maria lives in Milan" on a read that looks like the
    # current state (the hosted product's audit against its docs, 2026-09-29).
    # What the memory used to assert is its history, GET .../history.
    rows = agent_facts.facts_for_memory(db, key.tenant_id, _UUID(mid),
                                        project_id=key_project(db, key), true_now=True)
    facts = [agent_facts.serialize_fact(f) for f in rows]
    # The memory, its end user and the facts shown with it (bussola 2e).
    log_v1_request(db, key.tenant_id, "memories:get", api_key_id=key.id,
                   end_user_id=mem.get("end_user_id"), target_id=mem_public_id(mid),
                   memories_read=[mem_public_id(mid)], facts_read=[f.id for f in rows])
    return MemoryWithFacts(**public_memory(mem), facts=facts)


@router.get("/memories/{memory_id}/history", response_model=MemoryHistory,
            responses=v1_errors(404))
def memory_history(
    memory_id: str,
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
) -> MemoryHistory:
    """The lifecycle timeline of one memory: created / updated / deleted, plus
    every typed fact extracted from it (with the moment it was learned and, if
    superseded, invalidated). Richer than a plain add/update/delete log: it
    shows what the memory *taught* the store over time."""
    mid = _require_mem_uuid(db, key, "memories:history", memory_id)
    count_the_read(db, key)  # once the request is one it answers
    # Behind the project wall, as every other memory read (404 outside the
    # key's project); forgotten memories included, for their `deleted` event.
    # It queried by account alone until 2026-09-28: a key read another
    # project's timeline, and the 200 against a 404 confirmed the id.
    mem = _engine(db, key).get_row_with_history(mid)
    if mem is None:
        # In the trail as `error`, as memories:get (audit of 18 Oct 2026).
        raise refused(db, key, "memories:history", HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found"),
            target_id=mem_public_id(mid))

    # The same timeline as the operator's screen (facts.memory_timeline), the
    # facts behind this key's project.
    timeline, shown = agent_facts.memory_timeline(db, key.tenant_id, mem,
                                                  project_id=key_project(db, key))
    events = [HistoryEvent(**e) for e in timeline]
    log_v1_request(db, key.tenant_id, "memories:history", api_key_id=key.id,
                   end_user_id=mem.end_user_id, target_id=mem_public_id(mem.id),
                   memories_read=[mem.id], facts_read=[f.id for f in shown])
    return MemoryHistory(id=mem_public_id(mem.id), events=events)


def _activity(db: Session, tenant_id, project_id, column: str, *, limit: int, offset: int,
              agent_id: Optional[str] = None, seeded: bool = False, end_users=None) -> tuple:
    """Live memories and facts per value of `column` ("end_user_id" or
    "agent_id"), behind the project wall, the most recently active first,
    one page of it and how many there are: ([(value, {"memories", "facts",
    "last"})], total). One aggregation for /v1/users and /v1/agents, which had
    a copy each until 2026-10-06 (the code audit, row 15). Ties on the last
    activity go by the value, so offset pages over them repeat nothing and
    skip nothing. `seeded`: every value the project's rows carry is listed,
    a value with nothing live with zeros, last.

    Grouped, ordered, cut and counted by the database (db/bounded.py): every
    group came back, was sorted here and the page cut after, so a page of ten
    held every end user of the project in the one API process (audit of 20
    Oct 2026)."""
    from sqlalchemy import func as _func
    from sqlalchemy import literal, null, select, union_all
    from sqlalchemy import Integer, DateTime

    from ..db import AgentFact, AgentMemory

    parts = []
    # Active only, on both sides: a memory not forgotten, a fact true now.
    for model, live, slot in ((AgentMemory, AgentMemory.deleted_at.is_(None), "memories"),
                              (AgentFact, true_at(), "facts")):
        col = getattr(model, column)

        def scoped(q, model=model, col=col):
            q = within_project(q.where(model.tenant_id == tenant_id, col.isnot(None)),
                               model.project_id, project_id)
            if agent_id is not None:
                q = q.where(model.agent_id == agent_id)
            if end_users is not None:
                # A confined key counts its own end users' rows only.
                q = user_scope.within_end_users(q, model.end_user_id, EndUsers(end_users))
            return q

        n = _func.count(model.id)
        zero = literal(0, Integer)
        parts.append(scoped(select(col.label("value"),
                                   (n if slot == "memories" else zero).label("memories"),
                                   (n if slot == "facts" else zero).label("facts"),
                                   _func.max(model.created_at).label("last"))
                            .where(live)).group_by(col))
        if seeded:
            parts.append(scoped(select(col.label("value"), zero.label("memories"), zero.label("facts"),
                                       null().cast(DateTime(timezone=True)).label("last")))
                         .group_by(col))
    every = union_all(*parts).subquery()
    last = _func.max(every.c.last)
    grouped = (select(every.c.value, _func.sum(every.c.memories), _func.sum(every.c.facts), last)
               .group_by(every.c.value))
    page = db.execute(grouped.order_by(last.desc().nulls_last(), every.c.value.collate("C").desc())
                      .limit(limit).offset(offset)).all()
    total = db.execute(select(_func.count(_func.distinct(every.c.value)))).scalar() or 0
    return ([(value, {"memories": int(m or 0), "facts": int(f or 0), "last": at})
             for value, m, f, at in page], int(total))


def _iso(v):
    # Postgres returns a datetime from max(); a value that is already a
    # string goes through as it is, so .isoformat() never blows up.
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


@router.get("/users", response_model=UsersResponse)
def list_users(
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
    agent_id: Optional[str] = Query(default=None, description="Filter to one agent namespace."),
    limit: int = Query(default=50, ge=1, le=limits.MAX_PAGE),
    offset: int = Query(default=0, ge=0, le=limits.MAX_OFFSET),
) -> UsersResponse:
    """List the developer's end users (the distinct ``user_id`` namespaces that
    hold memories or facts), with per-user memory + fact counts and last-active
    time. Both stores are read (memories AND direct fact writes), so a
    fact-only end user is still visible. The default (null) namespace is omitted."""
    validate_filters(agent_id=agent_id)
    # The project wall, on the listing too. Memories and facts were behind it
    # from the start and these two aggregates were not, so a key confined to one
    # project still read back every end user id in the account. Not content,
    # identifiers, which for a product that sells the shape of a subject access
    # request is not a comforting distinction.
    count_the_read(db, key)  # once the request is one it answers
    items, total = _activity(db, key.tenant_id, key_project(db, key), "end_user_id",
                             limit=limit, offset=offset, agent_id=agent_id,
                             end_users=key_end_users(key))
    users = [
        UserScope(user_id=eu, memories=d["memories"], facts=d["facts"],
                  last_active=_iso(d["last"]))
        for eu, d in items
    ]
    # How many end users the page showed. Their ids are not kept: read_ids
    # holds memories and facts, and an end user's id in clear there would
    # outlive their erasure, which renames only the end user column
    # (services/pseudonyms.py). Audit of 18 Oct 2026.
    log_v1_request(db, key.tenant_id, "users:list", api_key_id=key.id,
                   meta={"rows": len(users), "total": total})
    return UsersResponse(users=users, total=total)


# `:path` for the same reason as the erase route above: an agent_id holding a
# '/' was writable and then unreachable here, so it could never be deleted.
@router.delete("/agents/{agent_id:path}", status_code=status.HTTP_200_OK,
               responses=v1_errors(404))
def delete_agent_namespace(
    agent_id: str,
    key: AgentApiKey = Depends(_write_key),
    db: Session = Depends(get_db),
) -> dict:
    """Hard-delete an agent namespace: purge every memory + fact written under this
    ``agent_id``, so a typo or throwaway ``agent_id`` stops appearing in
    ``GET /v1/agents``. Soft-forgetting its data is not enough for that: the
    listing counts any row carrying the namespace, forgotten ones included.
    Returns the purge counts + an audit id. 404 if the namespace was never used
    in this project.

    ``agent_id`` is percent-encoded as one path segment (``a/b`` is ``a%2Fb``);
    any stored id is accepted, including one written before new ids were
    required to be a single segment."""
    from ..services.usage import has_agent_id

    # By the rule of every id, as the erasure of an end user asks it: the
    # refusals below keep the namespace asked for in the trail, which must
    # be an id (limits.id_problem: no NUL, bounded).
    validate_filters(agent_id=agent_id)
    # Scoped to this key's project, like the delete underneath it. Account-wide
    # here meant two wrong answers at once: a key in one project asking about a
    # name that only exists in another got past the check and then deleted
    # nothing, and the 404 it did not get was itself the confirmation that the
    # name exists somewhere in the account. Not found is the honest answer, and
    # it matches what reading a stranger's memory id already does.
    if key_end_users(key) is not None:
        # An agent's namespace spans end users: erasing it is for a key of
        # the whole project.
        raise refused(db, key, "agents:delete", HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="a key confined to some end users cannot delete an agent "
                   "namespace, which spans every end user"), target_id=agent_id)
    proj = key_project(db, key)
    if not has_agent_id(db, key.tenant_id, agent_id, project_id=proj):
        # Each refusal in the trail (api/errors.refused; audit of 19 Oct 2026),
        # with the namespace asked for, as the erasure names it.
        raise refused(db, key, "agents:delete", HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found",
                    "message": f"No agent namespace '{agent_id}' in this project."},
        ), target_id=agent_id)
    result = agent_facts.delete_agent(db, key.tenant_id, agent_id, project_id=proj)
    # The namespace erased, as the erasure's own record names it
    # (AgentForgetAudit.target_id): the row said which call, not what it
    # erased (audit of 17 Oct 2026).
    log_v1_request(db, key.tenant_id, "agents:delete", api_key_id=key.id, target_id=agent_id,
                   meta={"memories": result.get("memories_deleted"),
                         "facts": result.get("facts_deleted"),
                         # Another agent's facts the namespace's had closed,
                         # true again (services/facts/given_back.py).
                         **({"facts_reopened": result["facts_reopened"]}
                            if result.get("facts_reopened") else {})})
    return result


@router.get("/agents", response_model=AgentsResponse)
def list_agents(
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=limits.MAX_PAGE),
    offset: int = Query(default=0, ge=0, le=limits.MAX_OFFSET),
) -> AgentsResponse:
    """List the agent namespaces this developer has written under (the distinct
    non-null ``agent_id`` values), each with its active memory + fact counts and
    last-active time, plus ``cap``, the most agent names a write may bring in
    (0 when nothing caps them, as on an install of one's own), and ``used``,
    how many of them the cap counts.

    The namespace set counts memories AND direct fact writes, including
    soft-deleted / invalidated rows; the per-agent ``memories``/``facts`` counts
    reflect only active items, so an agent whose data was all forgotten still
    appears, with zero counts, until ``DELETE /v1/agents/{agent_id}`` purges it.
    The default (null) namespace is never counted, and omitted."""
    # Same wall as /v1/users. A key confined to Beta used to read back the agent
    # names of Alpha and of Default, which is the account's shape leaking
    # through an endpoint whose own page promises the opposite. Seeded with
    # every namespace, so a fully forgotten agent still surfaces with zeros.
    count_the_read(db, key)  # once the request is one it answers
    proj = key_project(db, key)
    confined = key_end_users(key)
    # A confined key sees the agents of its own end users' rows, never the
    # names seeded from the whole project.
    items, total = _activity(db, key.tenant_id, proj, "agent_id", limit=limit, offset=offset,
                             seeded=confined is None, end_users=confined)
    agents = [
        AgentScope(agent_id=aid, memories=d["memories"], facts=d["facts"],
                   last_active=_iso(d["last"]))
        for aid, d in items
    ]
    log_v1_request(db, key.tenant_id, "agents:list", api_key_id=key.id)
    # The ceiling the writes are held to, counted as they count it
    # (`hooks.before_new_agent_id` refuses past it). None: no ceiling, which
    # is 0, the same convention the rate limiter uses, and the names this key
    # sees, `total`: counted from the project's seed, a confined key, which
    # is given no seed, said `used: 0` next to the agents it listed (audit of
    # 17 Oct 2026).
    capped = hooks.agent_cap(db, key.tenant_id)
    cap, used = capped if capped is not None else (0, total)
    return AgentsResponse(agents=agents, total=total, cap=cap, used=used)
