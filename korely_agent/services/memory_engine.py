"""AgentMemoryEngine — the agent memory store.

Operates on `agent_memories`. One developer account holds memories for many
end users (end_user_id), apps (agent_id), sessions (run_id).

Scoping invariant: EVERY query is filtered by the owning account `tenant_id`.
`end_user_id` is an optional sub-namespace inside the account:
  - omitted (the _ALL sentinel) → all of the account's agent memory (dashboard
    browse, or the developer testing).
  - a value (including None) → exactly that end user (None → the shared,
    bot-level memory with end_user_id IS NULL).
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import String, and_, cast, func, literal, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from ..core.times import aware
from ..core.llm import (
    EmbeddingFailed,
    embed,
    embedding_configured,
    forget_failure_here,
    missing_vector_level,
    not_now_here,
    TASK_DOCUMENT,
    TASK_QUERY,
)
from ..db import AgentMemory
from . import limits
from . import vector_state as vectors  # the row's job, below the drain (vector_state.py)
from .clients import client_name
from .memory_write import SESSION

logger = logging.getLogger(__name__)

# The search brings max(4 x limit, 60) candidates by meaning, at most 200,
# and the vector index is let bring them all: Mem0's pool, the same for both
# Agents engines (2026-10-01).
POOL_FACTOR, POOL_MIN, POOL_MAX = 4, 60, 200

# What `search` is, for a measure that has to say which engine it measured.
# The word side fused with it
# (HYBRID_SEARCH) was removed on 12 Oct 2026, measured worse and five times
# slower.
SEARCH_ENGINE = "cosine, pool max(4k, 60) <= 200, HNSW ef_search at the pool; no floor"


# How much of a memory's text its one vector is computed from: its first
# 8,000 characters. The local model reads at most 2,048 tokens of them anyway
# (core/embedding_model.MAX_TOKENS), so the tail of a longer memory is not in
# its vector and a search finds it by its beginning; its facts are mined from
# the whole text. Cut here only, for the write and the vector drain alike
# (audit of 17 Oct 2026: the drain cut it twice, in services/vectors.py too).
VECTOR_CHARS = 8000

# The stored types a metadata filter compares with: scalars only.
_SCALARS = ("string", "number", "boolean")


def json_text(value) -> str:
    """The text of a JSON scalar as PostgreSQL's `->>` reads a stored one: a
    string is itself, a boolean `true` or `false`, a number its JSON literal.
    Python's `str()` says "True" and "None", which no stored value reads as."""
    return value if isinstance(value, str) else json.dumps(value)


def metadata_matches(key: str, value):
    """SQL: the memory's metadata holds `key` with a value the filter `value`
    matches (`metadata` of POST /v1/memories/search).

      * values are compared as text with the stored value, as the contract
        says, a boolean or a number read with JSON's text: "pro" matches
        "pro", true matches true, and "true" or "5" match a stored true or 5,
        as they always did;
      * a number also matches a stored number equal to it, compared as
        numbers (5 and 5.0, 1e20 and 100000000000000000000);
      * null matches a stored null and nothing else;
      * scalars only: a stored object or array never matches, and a filter
        value that is one is refused by the route (api/_bodies.MetadataFilter).

    Until 6 Oct the text was Python's `str()`: true became "True" and matched
    nothing, null matched the string "None", and 5 missed a stored 5.0. The
    hosted product had the same code (code audit of 6 Oct 2026). The column is
    `json`, not `jsonb`: `json_typeof` for the type,
    `->>` for the text, and a number compared as JSONB, whose equality reads
    numbers as numbers and never raises, where a cast of the text would on a
    row holding a word."""
    node = AgentMemory.meta[key]
    kind = func.json_typeof(node)
    if value is None:
        return kind == "null"
    same_text = and_(kind.in_(_SCALARS), node.as_string() == json_text(value))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        same_number = cast(node, JSONB) == cast(literal(json.dumps(value), String), JSONB)
        return or_(same_text, and_(kind == "number", same_number))
    return same_text


def _pool(limit: int) -> int:
    return min(POOL_MAX, max(POOL_FACTOR * limit, POOL_MIN))


# Sentinel: "no end_user filter" (all of the account's agent memory), distinct
# from end_user_id=None which means "the shared / bot-level memory".
# One sentinel for the whole codebase. See services/sentinels.py for what
# the two separate ones cost.
from .sentinels import EVERY_END_USER as _ALL  # noqa: E402
from .sentinels import EVERY_PROJECT, EndUsers, within_project  # noqa: E402
from . import user_scope  # noqa: E402
from .memory_core import log_safe  # noqa: E402
from .memory_core.vector_search import hnsw_depth  # noqa: E402
from .facts.temporal import true_at

# Sentinel for get_all/search: scope to the DEFAULT namespace (rows whose
# agent_id IS NULL) — distinct from agent_id=None which means "no agent filter".
NO_AGENT = object()

# The largest page get_all returns, limits.MAX_PAGE: the `le=` of GET
# /v1/memories and GET /v1/events read the same number, so the two cannot
# drift. It was a literal 100 here while both routes accepted 200 (the
# published maximum), so `limit=200` quietly answered half a page
# (2026-09-28).
from .limits import MAX_PAGE  # noqa: E402


class MemorySearchUnavailable(Exception):
    """Retrieval engine failed transiently (embedding service / DB). The API
    maps it to 503 instead of leaking a 500 stack trace. `paused`: the
    service's own gate kept the question's vector from being computed (the
    Cloud's daily stop, a brake), which a retry cannot change until it lifts:
    the doors answer `service_paused` (api/errors.py), not `search_unavailable`."""

    def __init__(self, *args, paused: bool = False) -> None:
        super().__init__(*args)
        self.paused = paused


class StaleVersion(Exception):
    """An edit pinned to a version (`expected_updated_at`) found the memory at
    another one: nothing was written. The API answers 409 `stale_write`."""


def _instant(value) -> Optional[datetime]:
    """An ISO 8601 string or a datetime as an aware instant (a value with no
    offset is UTC, as the columns are); None when it cannot be read."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return aware(value)


def version_problem(expected: Optional[str]) -> Optional[str]:
    """Why PATCH's `expected_updated_at` cannot be compared, or None.

    Unreadable or empty, it is the caller's mistake, a 422, never 409
    `stale_write`: a client that retries on a stale write read the memory
    again and sent the same bad value back, for ever, and an empty one
    turned the check off without a word (audit of 17 Oct 2026). Absent
    (None) is an edit without the check."""
    if expected is None:
        return None
    read = _instant(expected)
    if read is None:
        return ("expected_updated_at is not an ISO 8601 instant: send back the "
                "`updated_at` (or, before the first edit, `created_at`) the API returned, "
                "or leave it out")
    return limits.moment_problem("expected_updated_at", read)


def is_another_version(expected: str, m: AgentMemory) -> bool:
    """True when `expected` is not the memory's current version: its
    `updated_at`, or its `created_at` before the first edit, compared as
    instants to the microsecond (the offset it is written with does not
    matter, the instant does). The client sends back the string the API
    returned, which reads back to the stored instant exactly; anything else,
    a rounded value or an unreadable one, is another version, and the safe
    answer to it is the refusal, never the overwrite."""
    current = _instant(m.updated_at or m.created_at)
    read = _instant(expected)
    return current is None or read is None or read != current


# Internal sync states → one developer-facing word. Extraction runs after the
# write returns, so without this a developer polls blind: they write, read back
# an empty fact list, and cannot tell "not finished" from "nothing to extract".
_PUBLIC_STATUS = {
    "pending": "processing",
    "processing": "processing",
    "done": "ready",
    "error": "error",
}


def _public_status(m: AgentMemory) -> str:
    """Whether this memory's facts are queryable yet.

    ``ready`` means extraction finished, so get_facts() reflects it. ``error``
    means extraction failed and will not retry on its own: the memory is stored
    and searchable, only its typed facts are missing.
    """
    return _PUBLIC_STATUS.get((m.graph_sync_status or "").lower(), "processing")


def _status_condition(public_status: str):
    """The SQL twin of `_public_status`: rows that read as `public_status`.

    Built from the same table, so the filter and the field agree. It is an IN
    on the raw column, which `idx_agent_mem_graph_pending` serves: the column is
    NOT NULL (server_default 'pending') and only the four lowercase values of
    `_PUBLIC_STATUS` are ever written (the write paths, the drain, `manage`),
    so the IN is exact. A `lower()` or a NOT IN would give the index up for
    nothing.
    """
    internal = [k for k, v in _PUBLIC_STATUS.items() if v == public_status]
    return AgentMemory.graph_sync_status.in_(internal)


def memory_vector(text: str):
    """The vector of a memory's text, or None when no embedder is configured
    (no Google key: a configuration, the memory is stored without one).

    Raises `EmbeddingFailed` when an embedder is configured and the call gave
    no vector: `embed` answers None for both, and only here is the difference
    decided, because the write and the drain (services/vectors.py) have to
    treat them differently. One function for both, so the vector a memory
    gets later is computed exactly as the one it would have got on the write.
    """
    emb = embed(text[:VECTOR_CHARS], task=TASK_DOCUMENT)
    if emb is None and embedding_configured():
        raise EmbeddingFailed("the embedding call gave no vector (the log and /health say why)")
    return emb


def serialize_memory(m: AgentMemory, *, score: Optional[float] = None) -> dict:
    out = {
        "id": str(m.id),
        "content": m.content,
        "end_user_id": m.end_user_id,
        "agent_id": m.agent_id,
        "run_id": m.run_id,
        "client": m.client,
        "kind": m.kind or "note",
        "metadata": m.meta if isinstance(m.meta, dict) else {},
        "status": _public_status(m),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
    if score is not None:
        out["score"] = round(float(score), 4)
    return out


def _claimed_now(m: AgentMemory) -> None:
    """A memory its writer extracts before answering, claimed in the same
    commit that writes it, as the drain claims (memory_core `claim_pending`): until
    2026-10-06 it was written 'pending' and the drain, every ten seconds in
    the same process, took it during the model call and extracted it a second
    time (the audit of 6 October, row 1): paid twice, judged twice, and the
    second extraction closing the facts of the first that it did not repeat.
    A writer that dies on the way leaves it to the reaper, which gives a
    'processing' row back after `drain_stale_minutes`."""
    m.graph_sync_status = "processing"
    m.graph_sync_claimed_at = datetime.now(timezone.utc)


class AgentMemoryEngine:
    """Every read and write of a memory goes through here.

    `project_id` is the isolation wall. An API key belongs to one project and
    passes it here, and from that point the key cannot see or touch a memory
    that belongs to another project. EVERY_PROJECT is the caller that is not a
    key: the dashboard, which an operator logs into and which is meant to see
    the whole account.

    The wall is one filter in `_base()` rather than a condition at each of the
    nineteen endpoints, because a wall with nineteen gates is a wall with
    nineteen chances to leave one open. Anything that does not go through
    `_base()` is a hole, which is why `_get_row` goes through it too.
    """

    def __init__(self, db: Session, tenant_id: UUID, project_id=EVERY_PROJECT, end_users=None):
        self.db = db
        self.tenant_id = tenant_id
        self.project_id = project_id
        # The end users of a confined key (services/user_scope.py, 16 Oct
        # 2026), a second wall in the same place as the project's: every read
        # and every row asked for by its id goes through `_walled()`. None:
        # the whole project.
        self.end_users = end_users

    # ── helpers ───────────────────────────────────────────────────────────
    def _walled(self):
        """The account and the project, forgotten memories included: the wall
        without the "still live" filter. Only the lifecycle read (history)
        wants a forgotten memory; every other read goes through `_base()`."""
        q = self.db.query(AgentMemory).filter(AgentMemory.tenant_id == self.tenant_id)
        if self.end_users is not None:
            q = q.filter(user_scope.condition(AgentMemory.end_user_id, self.end_users))
        return self._apply_project(q)

    def _base(self):
        return self._walled().filter(AgentMemory.deleted_at.is_(None))

    def _apply_project(self, q):
        return within_project(q, AgentMemory.project_id, self.project_id)

    @staticmethod
    def _apply_run(q, run_id):
        """Scope to one run/session. We accept run_id on write, so refusing it on
        read was an asymmetry: the column was there and unreachable."""
        return q if run_id is None else q.filter(AgentMemory.run_id == run_id)

    @staticmethod
    def _apply_sessions(q, exclude_sessions: bool):
        """Leave the work sessions out (AgentMemory.kind, 18 Oct 2026): what a
        coding assistant's hooks saved, when the read wants what was said."""
        return q.filter(AgentMemory.kind.is_distinct_from(SESSION)) if exclude_sessions else q

    @staticmethod
    def _apply_metadata(q, metadata):
        """AND-filter on developer metadata, one condition per key
        (`metadata_matches`).

        (The original note here explained a SQLite test suite. There is none:
        this repository tests on the Postgres it ships on, because the codebase
        it was extracted from tested on SQLite and shipped a migration that
        crashed production on first boot.)
        """
        if not metadata:
            return q
        # Bounded here too, for every caller (audit of 18 Oct 2026): each
        # key is a condition the database plans, and their cost grew faster
        # than their number. The route refuses it first with a 422.
        problem = limits.metadata_filter_problem(metadata)
        if problem:
            raise ValueError(problem)
        for k, v in metadata.items():
            q = q.filter(metadata_matches(str(k), v))
        return q

    @staticmethod
    def _apply_end_user(q, end_user_id):
        if end_user_id is _ALL:
            return q
        if isinstance(end_user_id, EndUsers):
            return q.filter(user_scope.condition(AgentMemory.end_user_id, end_user_id.patterns))
        if end_user_id is None:
            return q.filter(AgentMemory.end_user_id.is_(None))
        return q.filter(AgentMemory.end_user_id == end_user_id)

    @staticmethod
    def _apply_agent(q, agent_id):
        # Additive AND filter; None means "no agent filter" (all agents).
        if agent_id is None:
            return q
        # NO_AGENT scopes to the default namespace (rows with no agent_id).
        if agent_id is NO_AGENT:
            return q.filter(AgentMemory.agent_id.is_(None))
        return q.filter(AgentMemory.agent_id == agent_id)

    # ── write ─────────────────────────────────────────────────────────────
    def add(
        self,
        content: str,
        *,
        end_user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        run_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        event_time: Optional[datetime] = None,
        memory_id: Optional[UUID] = None,
        extracting_now: bool = False,
        client: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> AgentMemory:
        """Store one memory. `event_time` is the write's `timestamp`, already
        parsed: kept on the row (`agent_memories.event_time`), never in
        `metadata`, so the worker that mines the facts later (an asynchronous
        write, or the retry of an inline extraction that failed) dates them
        the way the write asked for. `memory_id` is for a caller that must be
        able to tell, on a retry, whether this write already happened (the
        batch import derives it from the job and the item). `extracting_now`:
        the caller extracts it before answering (`facts.extract_inline`), so
        the row is born claimed and the drain leaves it alone. `client`: the
        tool that wrote it (services/clients.py), a label and nothing more."""
        if not user_scope.allows(self.end_users, end_user_id):
            # The routes check first (core/auth.writing); this is the wall's
            # own refusal, so a door that forgot cannot write past it.
            raise user_scope.OutOfScope(end_user_id)
        text = (content or "").strip()
        if not text:
            raise ValueError("content must not be empty")
        m = AgentMemory(
            **({"id": memory_id} if memory_id is not None else {}),
            tenant_id=self.tenant_id,
            # Stamped from the key that wrote it, never from the request body:
            # a caller that could name its own project could write into
            # somebody else's.
            project_id=None if self.project_id is EVERY_PROJECT else self.project_id,
            end_user_id=end_user_id,
            agent_id=agent_id,
            run_id=run_id,
            client=client_name(client),
            kind=kind,
            content=text,
            meta=metadata,
            event_time=event_time,
        )
        if extracting_now:
            _claimed_now(m)
        # Embed inline so the memory is searchable immediately. A call that
        # gives no vector leaves it waiting for the worker (`_vector_of`); the
        # write succeeds either way.
        #
        # No memory_id in the usage scope: the row has not been flushed yet,
        # so it has no id. It costs nothing to lose, because the expensive
        # calls for this memory (extraction, the judge) happen later in
        # extract_and_persist_facts, where the id is known.
        self._vector_of(m, text, agent_id=agent_id)
        self.db.add(m)
        self.db.commit()
        self.db.refresh(m)
        return m

    def update(self, memory_id: str, content: str, *,
               extracting_now: bool = False, version: Optional[str] = None) -> Optional[dict]:
        """Replace a memory's text: its vector, its facts' extraction, the
        developer's `updated_at`. None when there is no such memory, or it
        was forgotten before the edit could be written.

        `version` is PATCH's `expected_updated_at`: the edit is written only
        if the memory is at that version, to the microsecond
        (`is_another_version`), and still at it when the write commits. The
        check reads the row; the write is a compare-and-set on the
        `updated_at` it read, in the write's own transaction, so an edit
        committed in between leaves it no row, and StaleVersion is raised
        with nothing written. Optimistic concurrency, the read without locks
        and the validation at the write (Kung and Robinson, ACM TODS 6(2),
        1981), like the facts drain's claim. Until 6 Oct the route compared
        the versions within one second and then wrote whatever had happened
        since: two edits in the same second both passed, the second
        overwriting the first (the hosted product's code audit). The vector is
        computed before the compare-and-set, so the
        row is held only from there to the commit, never while the embedder
        answers."""
        m = self._get_row(memory_id)
        if m is None:
            return None
        text = (content or "").strip()
        if not text:
            raise ValueError("content must not be empty")
        if version is not None:
            # The row as it is now, not as this session read it earlier.
            self.db.refresh(m)
            if is_another_version(version, m):
                raise StaleVersion(str(m.id))
        read = m.updated_at
        unchanged = (text == m.content and m.embedding is not None
                     and m.embedding_status == vectors.DONE)
        if not unchanged:
            # The same text keeps its vector: it is the vector of that text.
            self._vector_of(m, text, agent_id=m.agent_id, memory_id=m.id)
        # The write claims the row, still live, in its own transaction, with
        # or without a version: an edit without one wrote by primary key
        # alone, so a forget committed while the vector was computed got the
        # edit's text on the forgotten row, and the edit answered 200 about a
        # memory a GET then called not found (audit of 17 Oct 2026). With a
        # version it must also still be the one read.
        live = [AgentMemory.id == m.id, AgentMemory.deleted_at.is_(None)]
        if version is not None:
            live.append(AgentMemory.updated_at.is_(None) if read is None
                        else AgentMemory.updated_at == read)
        written = (self.db.query(AgentMemory).filter(*live)
                   .update({AgentMemory.updated_at: func.now()}, synchronize_session=False))
        if not written:
            self.db.rollback()
            if version is not None:
                raise StaleVersion(str(m.id))
            return None
        m.content = text
        if extracting_now:
            _claimed_now(m)
        else:
            m.graph_sync_status = "pending"  # re-extract entities on next pass
            # No claim left on it (memory_core 2.11.0): a drain extracting
            # the old text finds its claim gone and discards that write.
            m.graph_sync_claimed_at = None
        # An edit, the one write that moves the developer's `updated_at`
        # (models.AgentMemory says why nothing else does). The database's
        # clock, like `created_at`.
        m.updated_at = func.now()
        self.db.commit()
        self.db.refresh(m)
        return serialize_memory(m)

    def _vector_of(self, m: AgentMemory, text: str, *, agent_id=None,
                   memory_id=None) -> None:
        """The vector of a memory's text, on the write (2026-09-28). Does not
        commit.

        The old vector goes first. `update` kept it when the re-embed failed,
        still marked done: the memory said the new thing and was found by
        what it no longer said, and nothing would ever re-embed it. Now a
        memory has the vector of its text or none. A call that gives none
        leaves it `pending` with the call counted, and the worker retries it
        (services/vectors.py, bounded: `error` after the last attempt, which
        `manage reextract` queues again). Search reads only memories with a
        vector, so until then the memory is not found by meaning, rather than
        found by the wrong one. The write succeeds either way.
        """
        from . import llm_usage

        vectors.cleared(m)
        try:
            with llm_usage.attributed(self.tenant_id, agent_id=agent_id,
                                      memory_id=memory_id):
                emb = memory_vector(text)
        except Exception as e:  # noqa: BLE001 - the write must not fail on its vector
            logger.warning("[agent_memory] memory %s: no vector, the worker retries it: %s",
                           memory_id or "(new)", log_safe.error(e))
            vectors.missed(m)
            return
        if emb:
            m.embedding = emb
            m.embedding_status = vectors.DONE

    def delete(self, memory_id: str) -> bool:
        m = self._get_row(memory_id)
        if m is None:
            return False
        from datetime import datetime, timezone

        m.deleted_at = datetime.now(timezone.utc)
        self.db.commit()
        return True

    # ── read ──────────────────────────────────────────────────────────────
    def _get_row(self, memory_id: str) -> Optional[AgentMemory]:
        try:
            mid = UUID(str(memory_id))
        except (ValueError, TypeError, AttributeError):
            return None
        return self._base().filter(AgentMemory.id == mid).first()

    def get(self, memory_id: str) -> Optional[dict]:
        m = self._get_row(memory_id)
        return serialize_memory(m) if m else None

    def get_row_with_history(self, memory_id: str) -> Optional[AgentMemory]:
        """One memory for its lifecycle timeline (GET /v1/memories/{id}/history):
        a forgotten one too, since "deleted" is an event of the timeline, but
        behind the project wall like every other read. The route used to query
        by account alone, so a key read another project's timeline, and its
        200 against a 404 told it which ids exist there (2026-09-28, as on the
        hosted product)."""
        try:
            mid = UUID(str(memory_id))
        except (ValueError, TypeError, AttributeError):
            return None
        return self._walled().filter(AgentMemory.id == mid).first()

    def _family_subquery(self, family: str):
        """Subquery of memory ids that produced an active fact in `family`,
        with the rule the facts, the profile and the context use
        (`store.family_filter`): the family stored on the fact wins, the
        static map serves a fact without one. This used the static map alone
        until 2026-10-06, so a learned relation was "preferences" in the Facts
        page and nowhere under that filter here (the audit of 6 October, row
        11)."""
        from ..db import AgentFact
        from .memory_core.store import family_filter

        return self.db.query(AgentFact.source_memory_id).filter(
            AgentFact.tenant_id == self.tenant_id,
            true_at(),
            AgentFact.source_memory_id.isnot(None),
            family_filter(AgentFact, family),
        )

    def families_of(self, memory_ids: list) -> dict[str, list[str]]:
        """The categories each memory shows: the families of the live facts
        it produced, by the rule `_family_subquery` filters on, so a memory
        listed under a category chip shows that category. One query for a
        page. The Memories page had a Categories column that nothing filled
        (audit of 17 Oct 2026)."""
        from ..db import AgentFact
        from .memory_core.families import FAMILY_ORDER, family_of_the_fact

        ids = [UUID(str(i)) for i in memory_ids]
        if not ids:
            return {}
        rows = (
            self.db.query(AgentFact.source_memory_id, AgentFact.predicate_family,
                          AgentFact.predicate_canonical)
            .filter(AgentFact.tenant_id == self.tenant_id, true_at(),
                    AgentFact.source_memory_id.in_(ids))
            .distinct().all()
        )
        found: dict[str, set] = {}
        for mid, family, canonical in rows:
            found.setdefault(str(mid), set()).add(family_of_the_fact(family, canonical))
        return {mid: sorted(fams, key=FAMILY_ORDER.index) for mid, fams in found.items()}

    def get_all(
        self, *, end_user_id: Any = _ALL, agent_id: Optional[str] = None,
        run_id: Optional[str] = None, metadata: Optional[dict] = None,
        family: Optional[str] = None, created_after: Any = None,
        status: Optional[str] = None,
        limit: int = 20, offset: int = 0, exclude_sessions: bool = False,
    ) -> tuple[list[dict], int]:
        limit = max(1, min(int(limit or 20), MAX_PAGE))
        offset = max(0, int(offset or 0))
        q = self._apply_sessions(self._apply_metadata(
            self._apply_run(
                self._apply_agent(self._apply_end_user(self._base(), end_user_id), agent_id),
                run_id,
            ),
            metadata,
        ), exclude_sessions)
        if created_after is not None:
            q = q.filter(AgentMemory.created_at >= created_after)
        if status is not None:
            # In the query, before the LIMIT: filtering the page afterwards
            # answered `?status=error&limit=50` with the errors among the 50
            # newest writes, an empty list when the error was the 51st.
            q = q.filter(_status_condition(status))
        if family:
            q = q.filter(AgentMemory.id.in_(self._family_subquery(family)))
        total = q.count()
        rows = (
            q.order_by(AgentMemory.created_at.desc(), AgentMemory.id.desc())
            .offset(offset).limit(limit).all()
        )
        return [serialize_memory(m) for m in rows], total

    def count(self, *, end_user_id: Any = _ALL, status: Optional[str] = None) -> int:
        """How many live memories match, behind the same wall as every read.

        A SELECT count(*), no rows loaded. With `status="processing"` it is the
        in-flight backlog of the key's project, which /v1/events reports and a
        batch import waits on.
        """
        q = self._apply_end_user(self._base(), end_user_id)
        if status is not None:
            q = q.filter(_status_condition(status))
        return q.count()

    def search(
        self, query: str, *, end_user_id: Any = _ALL, agent_id: Optional[str] = None,
        run_id: Optional[str] = None, metadata: Optional[dict] = None,
        limit: int = limits.DEFAULT_SEARCH_RESULTS, query_vector: Optional[list] = None,
        widest: int = limits.MAX_SEARCH_RESULTS,
        min_score: Optional[float] = None, received_by: Optional[datetime] = None,
        exclude_sessions: bool = False,
    ) -> list[dict]:
        """`query_vector`: the vector of `query` when the caller has it
        already (GET /v1/context computes it once for the facts and the
        memories, as the hosted product does since BUG-299); no call is made
        then. `widest`: the most results one search returns
        (limits.MAX_SEARCH_RESULTS, what the API allows); the block of a large budget asks for more
        (services/context.py, `_pool_for`).

        `min_score` (2026-10-07, chosen for both products): a
        memory whose score, the cosine as the caller reads it (rounded as
        `serialize_memory` rounds it), is below it is dropped before the fusion,
        found by meaning or by words: Mem0's rule. None is no floor, as
        before. Fewer than `limit` results can come back.

        `received_by` (11 Oct 2026, the context rebuilt with `known_as_of`):
        the memories the store had received by then only."""
        q = (query or "").strip()
        if not q:
            return []
        limit = max(1, min(int(limit or limits.DEFAULT_SEARCH_RESULTS), widest))
        # Before the try below, which turns every failure into a 503: a filter
        # past its bounds is the caller's error (`_apply_metadata`).
        problem = limits.metadata_filter_problem(metadata)
        if problem:
            raise ValueError(problem)
        from . import llm_usage

        # The thread's last refusal belongs to an earlier request on a reused
        # thread, never to this search (`not_now_here`).
        forget_failure_here()
        try:
            if query_vector is not None:
                emb = query_vector
            else:
                with llm_usage.attributed(self.tenant_id, agent_id=agent_id):
                    emb = embed(q, task=TASK_QUERY)
            if not emb:
                # Not an empty result. `embed` returns None when there is no
                # provider configured or when the call failed, and in both cases
                # semantic search cannot run. Returning [] would answer "you
                # have no memories about this", which is a statement about the
                # caller's data made out of a failure of ours, with HTTP 200 on
                # it. An expired key would turn every search into a confident
                # "nothing known" until somebody noticed, and nobody notices a
                # 200.
                #
                # Same shape as the extractor returning [] for both "no facts in
                # this text" and "no provider", which is how the hosted product
                # came to report memories as processed while extracting nothing.
                raise MemorySearchUnavailable(
                    "no embedding available: check the model provider",
                    paused=not_now_here(),
                )
            dist = AgentMemory.embedding.cosine_distance(emb).label("dist")
            base = self._apply_metadata(
                self._apply_run(
                    self._apply_agent(
                        self._apply_end_user(
                            self._base().filter(AgentMemory.embedding.isnot(None)), end_user_id
                        ),
                        agent_id,
                    ),
                    run_id,
                ),
                metadata,
            )
            base = self._apply_sessions(base, exclude_sessions)
            if received_by is not None:
                base = base.filter(AgentMemory.created_at <= received_by)
            # By meaning, the cosine, ties by id. The word side fused with it
            # by rank (HYBRID_SEARCH, 2026-10-01 to 12 Oct) was off by default
            # and measured worse: removed, and the order is the cosine's, as
            # it always was with the setting off.
            pool = _pool(limit)
            self._let_the_vector_index_bring(pool)
            by_meaning = base.add_columns(dist).order_by(dist, AgentMemory.id).limit(pool).all()
        except MemorySearchUnavailable:
            # A question past its account's share is a refusal, not a failure
            # (core/llm.missing_vector_level; audit of 20 Oct 2026).
            logger.log(missing_vector_level(), "[agent_memory] search unavailable for %s: "
                       "no embedding", self.tenant_id)
            raise
        except Exception as e:
            logger.warning("[agent_memory] search failed for %s: %s", self.tenant_id,
                           log_safe.stack(e))
            raise MemorySearchUnavailable() from e

        if min_score is not None:
            # The score as the caller reads it, rounded as it is returned.
            by_meaning = [(m, d) for (m, d) in by_meaning
                          if round(1.0 - float(d), 4) >= min_score]
        return [serialize_memory(m, score=1.0 - float(d)) for (m, d) in by_meaning[:limit]]

    def _let_the_vector_index_bring(self, pool: int) -> None:
        # pgvector's HNSW scan returns at most hnsw.ef_search rows BEFORE the
        # WHERE (tenant, end user, project): the index is let bring the whole
        # pool (2026-10-01). memory_core's rule since 3.0.0
        # (`vector_search.hnsw_depth`), the same in the hosted product.
        hnsw_depth(self.db, pool)
