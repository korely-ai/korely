"""The schema: 24 tables. One `tenants`, 13 `agent_*`, three that
support them (two for the learned predicate vocabulary, one audit trail),
three for the people who log in to the screen (operators, their sessions,
the sign-in attempts), two for OAuth (the apps and their grants) and two for
the install's own settings and state.

The store is scoped at two levels, and keeping them straight matters because the
public API and the database disagree on the word "user":

    tenant_id     who owns this memory space. In a hosted setup that is an
                  account; in a self-hosted one it is usually one of your own
                  clients. Never exposed over the REST API: it is derived from
                  the API key.

    end_user_id   whose memory this is, from the agent's point of view. This is
                  what the REST API calls `user_id`, and what an SDK caller
                  passes as `user_id="customer-4821"`. An opaque string chosen
                  by the caller, not a foreign key.

The monolith this came from called the first one `user_id` too, which meant two
different things wore the same name one layer apart. Renamed here, where nothing
depends on it yet.

Everything cascades from `tenants`: deleting a tenant removes their memories,
facts, keys, audit rows and counters in one statement. That is the mechanism
behind erasure requests, so it is load-bearing rather than tidy.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import deferred

from ..core.embedding_model import DIMENSIONS as EMBEDDING_DIM
from .base import Base


class Tenant(Base):
    """Who owns a memory space.

    In the hosted product this is a customer account. Self-hosted it is more
    often one of your own clients: an agency running this for five customers
    gets five rows here, and the isolation between them is a foreign key rather
    than a convention someone has to remember in every query.

    Deliberately almost empty. There is no email, no password, no subscription
    and no plan, because authentication belongs to whatever is already in front
    of this service and billing does not exist when you pay for your own
    compute. A name and an id are what the rest of the schema needs.
    """

    __tablename__ = "tenants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # No `deleted_at`: an account is deleted for real (hosted/closing.py), and
    # the column, never written nor read, made a soft delete look possible
    # (audit of 17 Oct 2026; dropped by c3f5a7e9b1d4).
    # The ids of the accounts moved into this one (hosted/claim.move), as
    # strings, oldest first; NULL: none. The pseudonyms an erasure left in
    # their rows are keyed to the account they were made in
    # (services/pseudonyms.py), and cannot be keyed again without the end
    # user's id, so the lookups by id try these too (audit of 19 Oct 2026).
    merged_tenant_ids = Column(JSON, nullable=True)

    __table_args__ = (
        Index("idx_tenants_name", "name"),
    )


class AgentApiKey(Base):
    """API key authentication for the REST surface.

    Mem0-style: a SHA-256 hash of the key, a human-readable prefix
    ('kor_self_' for what this install mints), scopes, and a tier.

    The tier is the hosted service's plan name, and here it is a label and
    nothing more. Every key this install mints gets the default, `hobby`, and
    no limit reads it: no agent cap, no write or query quota, and the rate
    limit is per account and set by the operator. It stays because `/v1/ping`
    and `/admin/keys` have always carried it, and a field that disappears from
    a reply breaks whoever parses it. tests/test_memories_api.py
    (TestTheOperatorOwnsTheCapacity) holds a `hobby` key past the hosted caps.
    """
    __tablename__ = "agent_api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    key_hash = Column(String(64), nullable=False, unique=True)  # SHA-256 hex
    prefix = Column(String(16), nullable=False)  # 'kor_self_'; older rows 'kor_live_'
    # Last 4 chars of the full key, for display only (e.g. kor_self_••••O5fP).
    # Revealing 4 chars of a 256-bit random secret is a negligible leak and is
    # the Stripe/Mem0 convention for letting a user recognize a key in a list.
    last_four = Column(String(4), nullable=True)
    name = Column(String(128), nullable=True)
    tier = Column(String(32), nullable=False, server_default="hobby")  # a label: see the docstring
    # Stored as Postgres JSONB physically (per migration); typed as JSON at the
    # ORM layer, as it always was. Nothing in the codebase uses JSONB-specific
    # operators on this column.
    scopes = Column(JSON, nullable=False, server_default=text("'[\"memories:read\", \"memories:write\"]'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    # When it stops working. NULL means never, which is a choice somebody made
    # rather than the absence of one.
    #
    # A key that expires in silence is worse than a key that never expires: the
    # agent stops at three in the morning and nothing says why. So the expiry is
    # only half of this feature; the other half is `key.expiring`, emitted to
    # the account's webhooks before the day arrives, and the state being visible
    # in every listing.
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # When the warning went out, so it goes out once rather than every tick of
    # the worker. A notification that repeats every ten seconds is a
    # notification everybody filters.
    expiry_warned_at = Column(DateTime(timezone=True), nullable=True)
    # The secret a rotation replaced, and when it stops working
    # (POST /admin/keys/{id}/rotate): the agents that hold it have that long
    # to take the new one. NULL for a key never rotated, or whose rotation
    # ended the old secret at once. Revoking the key ends both.
    previous_key_hash = Column(String(64), nullable=True)
    previous_expires_at = Column(DateTime(timezone=True), nullable=True)
    # When its current secret was made, if not at its creation (a rotation,
    # an OAuth refresh): the lifetime a rotation keeps is counted from here.
    rotated_at = Column(DateTime(timezone=True), nullable=True)
    # The project this key belongs to; NULL
    # resolves to the account's Default project at read time. FK SET NULL so
    # deleting/archiving a project never deletes a developer's live key.
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="SET NULL"), nullable=True)
    # The OAuth client this key was issued to (services/oauth.py): ChatGPT,
    # Claude. One row per connected app, its secret renewed at every refresh;
    # NULL for a key a person minted. Its expiry is an hour that renews by
    # itself, so the expiry warnings leave it out.
    oauth_client_id = Column(String(64), ForeignKey("oauth_clients.client_id", ondelete="SET NULL"),
                             nullable=True)
    # The end users this key may read and write, inside its project
    # (services/user_scope.py, 16 Oct 2026): exact ids
    # and prefixes ending in `*`. NULL: the whole project, as before.
    end_users = Column(JSON, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "tier IN ('hobby', 'developer', 'team', 'scale')",
            name="ck_agent_api_keys_tier",
        ),
        Index("idx_agent_api_keys_tenant", "tenant_id"),
        # Name mirrors the Postgres migration that created it. Declared on the
        # model too, so the test schema (made from the models) enforces it and
        # a duplicate name surfaces as a handled 409, not a production-only
        # 500. (NULL names are distinct in a unique index, so several unnamed
        # keys are fine.)
        Index("uq_agent_api_keys_tenant_name", "tenant_id", "name", unique=True),
        Index("idx_agent_api_keys_project", "project_id"),
        Index("uq_agent_api_keys_previous_hash", "previous_key_hash", unique=True),
    )


class OAuthClient(Base):
    """A client that registered itself for OAuth (RFC 7591 dynamic client
    registration): ChatGPT, Claude (services/oauth.py). `info` is the SDK's
    OAuthClientInformationFull without the secret; a confidential client's
    secret is kept encrypted (`services.provider_key.encrypt_secret`), a
    public one (PKCE only) has none."""
    __tablename__ = "oauth_clients"

    client_id = Column(String(64), primary_key=True)
    info = Column(JSON, nullable=False)
    secret_encrypted = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # The network it registered from (services/rate_limit.network_of): /register
    # answers anyone, and the registrations of one network in an hour are
    # counted (services/oauth.py, `too_many`).
    network = Column(String(64), nullable=True)

    __table_args__ = (Index("idx_oauth_clients_network", "network", "created_at"),)


class OAuthGrant(Base):
    """One step of an OAuth authorization (services/oauth.py): a `request`
    waiting for a person's consent, an authorization `code`, or a `refresh`
    token. Codes and refresh tokens are kept as SHA-256 only, as the keys
    are; a request is addressed by its id. `used_at` makes each one single
    use (a refresh token rotates: RFC 9700 section 4.14, MCP 2025-11-25)."""
    __tablename__ = "oauth_grants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind = Column(String(16), nullable=False)  # request | code | refresh
    token_hash = Column(String(64), nullable=True, unique=True)
    client_id = Column(String(64), ForeignKey("oauth_clients.client_id", ondelete="CASCADE"),
                       nullable=False)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="CASCADE"),
                        nullable=True)
    key_id = Column(UUID(as_uuid=True), ForeignKey("agent_api_keys.id", ondelete="CASCADE"),
                    nullable=True)
    scopes = Column(JSON, nullable=False)
    resource = Column(String(512), nullable=True)
    redirect_uri = Column(Text, nullable=True)
    redirect_uri_explicit = Column(Boolean, nullable=False, server_default=text("true"))
    code_challenge = Column(String(128), nullable=True)
    state = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # A request's network, counted like the clients' (`too_many`).
    network = Column(String(64), nullable=True)
    # The end users the person allowed at consent (services/user_scope.py);
    # carried from the request to the code and to the key. NULL: all of them.
    end_users = Column(JSON, nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('request', 'code', 'refresh')", name="ck_oauth_grants_kind"),
        Index("idx_oauth_grants_key", "key_id"),
        Index("idx_oauth_grants_network", "network", "created_at"),
    )


class AgentBatchJob(Base):
    """Async batch import for /v1/batch (migrations from other stores).

    POST /v1/batch accepts up to 500 memory objects; this row holds the payload
    and the processing result. A scheduler-drained worker imports each item via
    the same engine.add + fact extraction as a single POST, so a migrated
    memory gets the full intelligence pipeline. Mirrors the BackgroundJobOutbox
    outbox pattern (status + attempts + next_retry_at) so it survives restarts.
    """
    __tablename__ = "agent_batch_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    # The project of the key that submitted the batch. It has to live on the row
    # rather than travel as an argument: the import happens later, in a worker,
    # long after the request that carried the key is gone. Without it every
    # imported memory would land unwalled, which is the quietest way to lose an
    # isolation guarantee: one endpoint out of nineteen, on the path people use
    # exactly once, when they migrate their data in.
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="SET NULL"), nullable=True)
    # The key that queued it, for the same reason: the worker writes long
    # after the request, and it writes only while that key could still write
    # there (services/batch._key_still_writes). Until the audit of 18 Oct 2026
    # a key revoked, expired or disconnected as an OAuth app was refused on
    # every route at once and every job it had queued went on importing.
    # NULL: queued before the column (migration a4c8e2f6b9d1), imported as
    # before. SET NULL as audit_events.api_key_id; a key row is deleted only
    # with its account, which takes the job with it, or by the purge of the
    # keys revoked past the trail's retention, which never takes a key with a
    # job not finished (services/keys.purge_revoked). Indexed, for that purge.
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("agent_api_keys.id", ondelete="SET NULL"),
                        nullable=True, index=True)
    status = Column(String(20), nullable=False, server_default="pending", index=True)  # pending|processing|completed|failed
    received = Column(Integer, nullable=False, server_default="0")
    imported = Column(Integer, nullable=False, server_default="0")
    failed = Column(Integer, nullable=False, server_default="0")
    # The list of memory objects, up to the batch's ceiling (tens of MB):
    # deferred, so loading the job never brings it (db/bounded.py). Every
    # GET /v1/batch/{id} and every claim of the worker read it whole for the
    # counts beside it (audit of 20 Oct 2026);
    # an item is read alone (batch._item_now), a scrub one locked job at a time.
    payload = deferred(Column(JSON, nullable=False))
    errors = Column(JSON, nullable=True)               # [{index, error}]
    created_memory_ids = Column(JSON, nullable=True)   # [mem_ ids]
    attempts = Column(Integer, nullable=False, server_default="0")
    max_attempts = Column(Integer, nullable=False, server_default="3")
    last_error = Column(Text, nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, onupdate=func.now())
    # The key of the process whose worker holds the claim (1.0.40,
    # services/holder.py), as `AgentMemory.graph_sync_claimed_by`: a job
    # 'processing' whose key no session of the database holds was left by a
    # process that has ended, and goes back to the queue at once instead of
    # after `drain_stale_minutes` (services/batch.reclaim_interrupted). Set by
    # every claim (NULL without a key: the clock decides, as before), read
    # only while the job is 'processing'.
    claimed_by = Column(Integer, nullable=True)

    __table_args__ = (
        Index("idx_agent_batch_status", "status"),
        # Created by migration c1a7f3e94d20 with the column and never declared
        # here, so the models said one thing and every install another: the
        # next autogenerate would have written a migration dropping it.
        # tests/test_migrations.py runs `alembic check` so the two agree.
        Index("idx_agent_batch_project", "project_id"),
    )


class AgentIdempotencyKey(Base):
    """An `Idempotency-Key` a write was sent with, and what that write made
    (1.0.27; services/idempotency.py, the IETF draft
    draft-ietf-httpapi-idempotency-key-header-08).

    One row per account, project and key, written in the transaction that
    writes the memory (POST /v1/memories) or queues the job (POST
    /v1/batch), so the two commit together or not at all: the foreign key
    to what it made is checked at the commit (DEFERRABLE INITIALLY
    DEFERRED), and a row with no write behind it cannot be committed. No
    text: the key's SHA-256, the SHA-256 of the checked request
    (`fingerprint`), the id it made and two times. Kept 24 hours from the
    write (`expires_at`), the expiry the docs publish (§2.3), then purged
    by the worker. It goes with what it made (ON DELETE CASCADE: an end
    user's erasure deletes their memories, and their keys with them), with
    the erasure of any row of the job it made (services/batch._scrub), its
    project and its account.
    """

    __tablename__ = "agent_idempotency_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    # The key's project: NULL only on an install with no Default project,
    # where its memories have none either (keys.key_project).
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="CASCADE"),
                        nullable=True)
    key_hash = Column(String(64), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    # What the write made: one of the two, by its route.
    memory_id = Column(UUID(as_uuid=True),
                       ForeignKey("agent_memories.id", ondelete="CASCADE",
                                  deferrable=True, initially="DEFERRED"),
                       nullable=True)
    batch_job_id = Column(UUID(as_uuid=True),
                          ForeignKey("agent_batch_jobs.id", ondelete="CASCADE",
                                     deferrable=True, initially="DEFERRED"),
                          nullable=True)
    # The database's clock, as `expires_at` is.
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # One key per account and project. NULLS NOT DISTINCT: a project of
        # NULL is one project, as in the read marks' unique index.
        Index("uq_agent_idempotency_keys_key", "tenant_id", "project_id", "key_hash",
              unique=True, postgresql_nulls_not_distinct=True),
        # The purge's question.
        Index("idx_agent_idempotency_keys_expires", "expires_at"),
        # The cascades from a deleted memory and a purged job.
        Index("idx_agent_idempotency_keys_memory", "memory_id"),
        Index("idx_agent_idempotency_keys_job", "batch_job_id"),
        CheckConstraint("(memory_id IS NULL) <> (batch_job_id IS NULL)",
                        name="ck_agent_idempotency_keys_one_target"),
    )


class AgentFact(Base):
    """Typed (subject, predicate, object) facts for the AGENT
    memory store, the Layer-3 moat exposed over /v1/facts.

    Open-vocabulary LLM extraction → canonical predicate → bi-temporal
    validity (valid_from / invalid_at / invalidated_by_id) → two-stage
    contradiction check, the rules of `services/memory_core`. Facts here are
    scoped per end user (`end_user_id`, Mem0 namespace) and sourced from
    `agent_memories`.

    `source_memory_id` is nullable: a fact written directly via
    POST /v1/facts (the developer already has the structured form) has no
    originating memory.
    """
    __tablename__ = "agent_facts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Owning developer account.
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    # Mem0-style namespaces (strings, not FKs, to match the published contract).
    end_user_id = Column(String(255), nullable=True, index=True)
    agent_id = Column(String(255), nullable=True)
    run_id = Column(String(255), nullable=True)

    subject_label = Column(String(200), nullable=False)
    subject_type = Column(
        String(20), nullable=False, default="unknown", server_default="unknown",
        comment="Graphiti node type: self|person|organization|product|place|concept|event|unknown",
    )
    predicate_raw = Column(String(200), nullable=False)
    predicate_canonical = Column(String(200), nullable=False)
    object_label = Column(String(500), nullable=False)
    object_is_literal = Column(Boolean, nullable=False, default=False, server_default="false")
    confidence = Column(Float, nullable=False, default=0.7, server_default="0.7")

    source_memory_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_memories.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    # The memory that wrote the fact first: `source_memory_id` as it was
    # inserted, set by the database (db/triggers.py) and never moved. A
    # forget or an edit re-points `source_memory_id` to a memory that keeps
    # the fact (facts/erasure._lose_the_source, memory_core store.
    # retire_the_previous_version), and the erasure of the first writer
    # then no longer knew which kept facts carried its words (the audit
    # before 1.0.0). Never set by
    # the code; NULL once that memory is deleted.
    first_memory_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agent_memories.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    model_used = Column(String(100), nullable=True)

    # Bi-temporal validity (A-MEM / Graphiti pattern). invalid_at NULL = active.
    # A fact written without a start begins now on the APPLICATION's clock, the
    # clock `true_at()` reads "now" on (13 Oct 2026): with the database's
    # `now()` (the start of its transaction, on its own clock) a fact written a
    # moment before could read as not begun yet whenever the database's clock
    # was ahead, and the judge did not see it (one test failed once in about
    # twenty runs). The server default stays, for rows written in SQL.
    valid_from = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        server_default=func.now(), nullable=False)
    invalid_at = Column(DateTime(timezone=True), nullable=True)
    invalidated_by_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_facts.id", ondelete="SET NULL"), nullable=True,
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, onupdate=func.now())
    # When the store learned the fact had ended, as `created_at` is when it
    # learned the fact (`known_as_of`, 11 Oct 2026). Written by the database
    # itself, whoever closes the fact (db/triggers.py); never set by the code.
    closed_known_at = Column(DateTime(timezone=True), nullable=True)

    # Semantic retrieval embedding of "subject predicate object" (768D, same
    # space as agent_memories.embedding for reuse). Deferred: a row loads
    # without it, which a list of 768 floats made some forty times heavier
    # (audit of 20 Oct 2026: the export and a project's move read every fact whole,
    # 36 to 43 KiB a fact). A read that ranks by it names it (db/bounded.py).
    embedding = deferred(Column(Vector(EMBEDDING_DIM), nullable=True))
    embedding_status = Column(String(20), nullable=False, default="pending", server_default="pending")
    # 2026-09-28: a vector that did not come back is retried by the worker
    # (services/vectors.py): the calls made so far for this text, and when the
    # next one may be made (NULL: as soon as the drain passes).
    embedding_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    embedding_next_at = Column(DateTime(timezone=True), nullable=True)
    # The fact was judged when it was written and its vector waits only
    # because its canonical names changed (a name declared or removed): the
    # vector drain stores the vector and asks no judge again (audit of 27
    # Oct 2026, services/vector_state.renamed). False for a vector that failed on write.
    judged_before_vector = Column(Boolean, nullable=False, default=False, server_default="false")
    # Org layer . INERT in M1; the per-project isolation key
    # at L2 (and the dimension added to the contradiction Stage-1 lookup).
    # SET NULL, not CASCADE. Deleting a project is deleting a folder, not
    # deleting what people remembered inside it. When L2 starts setting this
    # column, removing a project's data has to be an explicit operation that
    # says how many rows it is about to destroy, never a foreign key side
    # effect behind a button.
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="SET NULL"), nullable=True)

    # 2026-09-27 — aliases, reconfirmation, tense (the rules are in
    # services/memory_core).
    # The CANONICAL name after `agent_entity_aliases`: "Acme" stays in
    # subject_label (as written), "Globex" goes here once Acme is known to be
    # Globex. Dedup, contradiction and reads by subject work on these.
    subject_canonical = Column(
        String(200), nullable=True,
        default=lambda ctx: ctx.get_current_parameters().get("subject_label"),
    )
    object_canonical = Column(
        String(500), nullable=True,
        default=lambda ctx: ctx.get_current_parameters().get("object_label"),
    )
    # current | past | planned, read from the memory's text.
    tense = Column(String(10), nullable=False, default="current", server_default="current")
    # Reconfirmation: the same triple restated by another memory raises these
    # instead of being returned in silence.
    last_confirmed_at = Column(DateTime(timezone=True), nullable=True)
    observation_count = Column(Integer, nullable=False, default=1, server_default="1")
    # Every memory that asserted the fact (UUID strings); forgetting one
    # detaches it, and the fact only drops out when none is left.
    source_memory_ids = Column(JSON, nullable=True)
    # True while every memory that stated the fact is a work session
    # (AgentMemory.kind): born from a session it is true, a note that states
    # it again makes it false, and it is counted again from the sources left
    # when a note's is detached (a forget, an erasure, an edit that no longer
    # states it: facts/erasure.recount_session_only). What `exclude_sessions`
    # leaves out of the fact reads (18 Oct 2026).
    session_only = Column(Boolean, nullable=False, default=False, server_default="false")
    # BUG-109: the relation's family as the learned vocabulary decided it.
    predicate_family = Column(String(20), nullable=True)

    __table_args__ = (
        # Contradiction Stage 1 + subject/entity filters.
        Index("idx_agent_facts_tenant_enduser_subject", "tenant_id", "end_user_id", "subject_label"),
        Index("idx_agent_facts_tenant_enduser", "tenant_id", "end_user_id"),
        Index("idx_agent_facts_predicate", "tenant_id", "predicate_canonical"),
        Index("idx_agent_facts_source_memory", "source_memory_id"),
        Index("idx_agent_facts_project", "tenant_id", "project_id"),
        # One open fact per triple, arbitrated by the database rather than by a
        # read. The dedup in the service is a read followed by a write, and
        # under concurrency every worker reads "nothing there" and every worker
        # writes: five identical writes at once left five identical open facts.
        #
        # Declared here as well as in the migration on purpose: the test schema
        # is built by create_all() and never sees a migration, so a constraint
        # that lives only in alembic is a constraint no test can check. That is
        # how a bad migration took production down once already.
        #
        # NULLS NOT DISTINCT is the half that matters. project_id, end_user_id
        # and agent_id are often NULL, and by default Postgres treats two NULLs
        # as different, so a plain unique index would miss duplicates in exactly
        # the commonest case. Needs Postgres 15+.
        #
        # 2026-09-27: on the CANONICAL names, case-insensitive (migration
        # f27a0c9e1b01). "Maria" and "maria", or "Acme" and "Globex" once Acme
        # is known to be Globex, are one entity.
        Index("uq_agent_facts_one_open_per_triple",
              "tenant_id", "project_id", "end_user_id", "agent_id",
              text("lower(subject_canonical)"), "predicate_canonical", text("lower(object_canonical)"),
              unique=True,
              postgresql_nulls_not_distinct=True,
              postgresql_where=text("invalid_at IS NULL")),
        # The facts carry embeddings too, and /v1/context ranks them by cosine
        # distance on every read. Same scan, same index.
        Index("idx_agent_facts_embedding_hnsw", "embedding",
              postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
        # What the vector drain reads (services/vectors.py): the rows still
        # waiting for a vector, by when they are due.
        Index("idx_agent_facts_vector_waiting", "embedding_next_at",
              postgresql_where=text("embedding_status = 'pending'")),
    )



# When the store learned a fact had ended, and which memory wrote it first:
# filled by the database (db/triggers.py).
from . import triggers as _triggers  # noqa: E402

_triggers.install(AgentFact.__table__)

class AgentEntityAlias(Base):
    """Entity aliases for the agent memory (2026-09-27).

    The rule of `memory_core.identity`: a former name points to the current one, dated,
    with who decided it. Created by itself only from an explicit identity
    statement ("Acme is now called Globex"), never from similarity; the
    operator adds or removes one from the admin API.

    One memory = tenant + project + end user + agent namespace: the key facts
    are deduplicated on. A rename stated by end user A does not rename anything
    in end user B's facts, and erasing an end user erases their aliases.
    """
    __tablename__ = "agent_entity_aliases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="CASCADE"), nullable=True)
    end_user_id = Column(String(255), nullable=True)
    agent_id = Column(String(255), nullable=True)
    alias_lower = Column(String(200), nullable=False)
    alias_label = Column(String(200), nullable=False)
    canonical_label = Column(String(200), nullable=False)
    # The canonical as the alias was stated, before any chain flattening
    # rewrote `canonical_label` (memory_core `store.add_alias`):
    # "GX -> Acme" stays "Acme" here after "Acme -> Globex" makes the row
    # point at "Globex". Undoing an alias puts back exactly the rows its
    # flattening changed (`store.remove_alias`; audit of 20 Oct
    # 2026). NULL on a row written before the column: its canonical as it is.
    stated_canonical = Column(String(200), nullable=True)
    since = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    source = Column(String(20), nullable=False, default="user", server_default="user")
    confidence = Column(Float, nullable=False, default=1.0, server_default="1.0")
    source_fact_id = Column(UUID(as_uuid=True), ForeignKey("agent_facts.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # A name 0.2.0 filed under the Default project (made with no project up
    # to 0.1.40) whose person's project the upgrade could not tell
    # (migration e7c4b2d9a6f1): it stays in the Default project, and an
    # erasure of its id that leaves the id in no other project takes it
    # (services/end_user_places.unplaced_names).
    project_not_recorded = Column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = (
        Index(
            "uq_agent_entity_aliases_alias_per_memoria",
            "tenant_id", "project_id", "end_user_id", "agent_id", "alias_lower",
            unique=True, postgresql_nulls_not_distinct=True,
        ),
    )


class AgentObjectLink(Base):
    """"The same as": two objects of one memory that the writer judged to be
    one thing named in two languages or forms ("Italy" and "Italia"),
    memory_core 6.1.0 (`objects`, off by default).

    NOT an alias (AgentEntityAlias): an alias rewrites the canonical names
    of the facts and makes two facts one row; a link writes no fact. Both
    objects keep their facts, labels, dates, tense, evidence and sources;
    the reads resolve the link (services/facts/links.py) and show the two as
    one, under `canonical_label`. Deleting the row is the undo, and the
    facts are then exactly as they were before it.

    One memory = tenant + project + end user + agent namespace, as an alias.
    `name_label` (the object linked, named after the other) is unique in its
    memory, compared lowercased by the database. `reason` holds the writer's
    descriptions of the two objects and its two verdicts, words derived from
    the memories': erasing either memory takes the row (facts/erasure.py,
    and the foreign keys below).
    """
    __tablename__ = "agent_object_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="CASCADE"), nullable=True)
    end_user_id = Column(String(255), nullable=True)
    agent_id = Column(String(255), nullable=True)
    # The object linked, as its facts name it (their canonical name) ...
    name_label = Column(String(200), nullable=False)
    # ... and the object it is the same thing as, the name the reads show.
    canonical_label = Column(String(200), nullable=False)
    # The relation family it was decided in (the facts compared were of it):
    # the reads apply it to the facts of that family only. "Florence" the
    # city is not an aunt called Florence (the review of the rebuild, P2.1).
    family = Column(String(20), nullable=False)
    # Who decided: "writer" (at the write, `objects.unite_new_objects`) or
    # "review" (`manage review-objects --apply`).
    decided_by = Column(String(20), nullable=False)
    # The memory whose words named `name_label`, and the fact (and its
    # memory) that named `canonical_label` when the writer compared them.
    source_memory_id = Column(UUID(as_uuid=True), ForeignKey("agent_memories.id", ondelete="CASCADE"),
                              nullable=True)
    source_fact_id = Column(UUID(as_uuid=True), ForeignKey("agent_facts.id", ondelete="SET NULL"),
                            nullable=True)
    canonical_memory_id = Column(UUID(as_uuid=True), ForeignKey("agent_memories.id", ondelete="CASCADE"),
                                 nullable=True)
    # {"model", "descriptions": {"name", "canonical"}, "readings": [...]}.
    reason = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # memory_core 6.20.0: whose link it is. "memory" (the default, every
    # link until then): two objects of one memory, read by the reads of that
    # memory. "wall": two SUBJECTS of a memory with no end user, decided by
    # `manage review-subjects` (off by default), in the judge's wall (the
    # account, the project, no end user, every agent: `entities.wall_of`),
    # with `end_user_id` and `agent_id` NULL; the judge weighs a fact of one
    # name against the other's, and no read follows it. Its own value, not
    # an empty `agent_id`, which is already the memory with no agent
    # (`entities.memory_of`).
    scope = Column(String(10), nullable=False, server_default="memory")

    __table_args__ = (
        Index(
            "uq_agent_object_links_name_per_memory",
            "tenant_id", "project_id", "end_user_id", "agent_id", "scope", text("lower(name_label)"),
            unique=True, postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("scope IN ('memory', 'wall')", name="ck_agent_object_links_scope"),
        Index("idx_agent_object_links_source_memory", "source_memory_id"),
        Index("idx_agent_object_links_canonical_memory", "canonical_memory_id"),
        Index("idx_agent_object_links_source_fact", "source_fact_id"),
    )


class AgentFactClosureLink(Base):
    """A closing the judge made only through a link between two subjects
    (memory_core 6.20.0, `objects.record_closures`): the fact closed and the
    link that let the judge weigh it. Removing the link gives the closing
    back first (`objects.remove_link`, the one door; facts/subjects.remove),
    then these rows go, then the link.

    `fact_id` ON DELETE CASCADE: a fact erased takes its records. `link_id`
    ON DELETE NO ACTION, a constraint that is not deferred and is checked
    at the end of each statement (PostgreSQL documentation, Constraints,
    Foreign Keys, and SET CONSTRAINTS; RESTRICT is the one checked at once):
    a statement that deletes a link and its closed facts together (an
    account's or a project's erasure, by cascade) passes, and one that
    deletes a link with closings still recorded on it, the door skipped,
    fails (tests/test_same_subject_link_undo_gives_back.py). Ids only, and
    the account's, as every table of an account's data."""
    __tablename__ = "agent_fact_closure_links"

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
                       index=True)
    fact_id = Column(UUID(as_uuid=True), ForeignKey("agent_facts.id", ondelete="CASCADE"),
                     primary_key=True)
    link_id = Column(UUID(as_uuid=True), ForeignKey("agent_object_links.id", ondelete="NO ACTION"),
                     primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_agent_fact_closure_links_link", "link_id"),
    )


def _key_check_of(ctx):
    """`AgentForgetAudit.key_check` at the insert: the key check when the
    record names its end user by pseudonym, else None."""
    from ..services.ids import is_pseudonym
    from ..services.pseudonyms import key_check

    return key_check() if is_pseudonym(ctx.get_current_parameters().get("end_user_id")) else None


class AgentForgetAudit(Base):
    """GDPR Art. 17 audit stub for agent-store forget events.

    Created on DELETE /v1/memories/:id, DELETE /v1/users/:tenant_id/memories,
    DELETE /v1/agents/:agent_id, forgetting or correcting a fact, and
    DELETE /admin/projects/:id?erase=true. The row id IS the `audit_id` returned in the
    API response, so a developer can prove to their own end user "you were
    forgotten on YYYY-MM-DD, N memories + M facts dropped." Counts and
    references, never the erased content. The end_user_id is the
    developer's handle for a person, personal data (GDPR Art. 4(5); this
    said "not personal data" until the legal review of 11 Oct 2026): once the
    end user is erased, the rows about them hold a keyed pseudonym instead
    (services/pseudonyms.py, 0.1.27), which still makes the erasure provable
    for that person (Art. 5(2)) and no longer says who it was.
    """
    __tablename__ = "agent_forget_audit"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    end_user_id = Column(String(255), nullable=True, index=True)
    # Org layer . scope='project' rows set this at L2; FK
    # SET NULL so deleting a project never wipes the audit trail.
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="SET NULL"), nullable=True, index=True)
    # The project the record was written in, which `project_id` stops saying
    # once a project's deletion files the record under the default project
    # (services/projects.delete_project): for 'user' and 'agent' the project
    # the erasure reached (NULL: every project of the account), for 'memory'
    # and 'fact' the row's, for 'project' the default one (the erased project
    # is `target_id`). A restore repeats an erasure there
    # (services/erasure_replay.py; audit of 19 Oct 2026: it looked in the
    # default project, found nobody or erased a namesake, and the erased
    # person came back in clear). Taken from `project_id` at the insert, so
    # every writer fills it, and never moved: no foreign key, so a project's
    # deletion cannot null it either.
    erased_in_project_id = Column(
        UUID(as_uuid=True), nullable=True,
        default=lambda ctx: ctx.get_current_parameters().get("project_id"),
    )
    # 'memory' | 'user' | 'fact' | 'agent' | 'project', and 'move': a
    # project deleted without erasing it, its rows moved to the default one
    # (`project_id`), which a restore makes again in its place among the
    # erasures (services/erasure_replay.py; audit of 20 Oct 2026: an erasure made
    # after the move reached the moved rows, and its replay on a dump older
    # than the move did not). And 'past' (1.0.14): a past statement of a
    # fact posted by hand (POST /v1/facts with tense 'past') on a fact the
    # store held, the caller's word that it is over, which is no forget: a
    # deletion never gives that fact back, as for 'fact'
    # (services/facts/given_back.record_by_hand). The replay of erasures
    # repeats neither.
    scope = Column(String(20), nullable=False)
    # memory/fact id, agent_id, erased or moved project's id; for 'user',
    # NULL for an end user's own erasure, the erased project's id for each
    # end user a project's erasure erased (services/projects._file_each_end_user).
    target_id = Column(String(255), nullable=True)
    # The check of the pseudonym key `end_user_id` was made under
    # (pseudonyms.key_check), for a record that names its end user by
    # pseudonym: a restore under another key refuses instead of finding
    # nobody (audit of 20 Oct 2026: the only check was the export's, made
    # under the key in force then). Filled at the insert, as the default below,
    # so every writer fills it.
    key_check = Column(String(32), nullable=True, default=lambda ctx: _key_check_of(ctx))
    memories_forgotten = Column(Integer, nullable=False, server_default="0")
    facts_invalidated = Column(Integer, nullable=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # A deletion reads the records of the facts it would give back
    # (facts/given_back._said_by_hand: a fact forgotten or corrected by hand
    # is never given back), by account and fact id: without it, every record
    # of the account (alembic a7c4e9f2d1b6).
    __table_args__ = (Index("idx_agent_forget_audit_tenant_target", "tenant_id", "target_id"),)


class AgentFactEndedBy(Base):
    """A memory whose own words say a fact is over (since 1.0.14), written
    by that memory's write, when they say it: a past statement of it that
    lands on a row the store held (memory_core `writing._the_past`: it
    closes the open row it restates, or adds to the closed row it is
    history of), a fact that statement's judgement closed
    (`writing._judge_the_past`), or an edit of the fact's own memory that no
    longer states it (`store.retire_the_previous_version`). None of them
    names a new fact, and the row keeps the pointer an earlier judge gave it
    (`invalidated_by_id`): a deletion reads these to know who still says the
    fact is over (facts/given_back.py). Every such memory has its row, not
    only the first: two memories that say it ("I no longer live in Paris",
    "Paris is not my home any more"), however they reach the worker, each
    keep it closed while they are live. A row is taken away when a later
    version of the memory no longer says it (facts/previous_version.py), and
    goes with the fact or the memory, by the foreign keys; a forgotten
    memory keeps it, and is read as no longer saying it from its forget.
    Ids only: no word of the memory or the fact."""
    __tablename__ = "agent_fact_ended_by"

    fact_id = Column(UUID(as_uuid=True), ForeignKey("agent_facts.id", ondelete="CASCADE"),
                     primary_key=True)
    memory_id = Column(UUID(as_uuid=True), ForeignKey("agent_memories.id", ondelete="CASCADE"),
                       primary_key=True, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)


class AgentMemory(Base):
    """The agent memory store, SEPARATE from the personal vault.

    same login, separate store. Memories created
    by a developer's agents via /v1 live here, NEVER mixed with the human-app
    RichNotes (the personal vault). One account (tenant_id) holds memories for
    many end users (end_user_id, Mem0-style), apps (agent_id), sessions
    (run_id). The same RAG engine runs over it (embedding + entity graph), but
    the data is physically separate so the two worlds never tangle.
    """
    __tablename__ = "agent_memories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The developer / owning account.
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    # Mem0-style namespaces (strings, not FKs, to match the published contract).
    end_user_id = Column(String(255), nullable=True)  # the developer's end user
    agent_id = Column(String(255), nullable=True)      # the developer's application
    run_id = Column(String(255), nullable=True)        # a session
    # The tool that wrote it (services/clients.py): the X-Korely-Client
    # header, or the name an MCP client gave when it connected. A label, not
    # a namespace: no read is scoped by it and no cap counts it.
    client = Column(String(64), nullable=True)
    # What kind of text it is: NULL a note (what somebody said or decided),
    # "session" the log of a work session a coding assistant's hooks saved
    # (18 Oct 2026). Measured on the
    # session bench of 17 Oct: 71% of a session's facts are working
    # artefacts (commands, paths, quoted values). A read may leave the
    # sessions out (`exclude_sessions`), their facts with them.
    kind = Column(String(16), nullable=True)

    content = Column(Text, nullable=False)
    meta = Column("metadata", JSON, nullable=True)  # arbitrary developer metadata
    # When the memory's events occurred: the write's `timestamp`, parsed. Its
    # facts inherit it as valid_from, which is what makes `as_of` answer what
    # was true then rather than what was written then (Graphiti: valid time,
    # not transaction time). NULL when the write gave none: the facts date
    # from their extraction. Every write path stores it here and the worker
    # reads only this; until 2026-09-28 it lived in `metadata` as
    # `_event_time`, where the developer read it back and a synchronous write
    # never put it (migration 5e1d9a7c3b28).
    event_time = Column(DateTime(timezone=True), nullable=True)
    # One short sentence about the end user that the extraction writes next
    # to the facts (memory_core 2.6, 2026-10-02): what the memory says about
    # the person, dated when the text gives a date. /v1/context shows it in
    # place of the whole text, which stays here, untouched, as the source.
    # NULL until a write of this release extracts it, and when the memory
    # says nothing about the person.
    observation = Column(Text, nullable=True)
    # memory_core 2.9.0: the memory's relative times and the dates the code
    # computed from its day ({"v", "anchor", "times"}), with the
    # relative-times rule on; None when not asked.
    relative_times = Column(JSON, nullable=True)

    # Deferred, as the facts' (db/bounded.py).
    embedding = deferred(Column(Vector(EMBEDDING_DIM), nullable=True))
    embedding_status = Column(String(20), nullable=False, server_default="pending")
    # 2026-09-28: the same retry as the facts (services/vectors.py).
    embedding_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    embedding_next_at = Column(DateTime(timezone=True), nullable=True)
    # graph: entity/relationship extraction status for this memory.
    graph_sync_status = Column(String(20), nullable=False, server_default="pending")
    # When the facts drain last claimed the row ('processing'): the clock its
    # reaper reads to take back a claim a dead worker left behind. The reaper
    # read `updated_at` until 2026-09-28, which the claim moved (below).
    graph_sync_claimed_at = Column(DateTime(timezone=True), nullable=True)
    # The key of the process that holds the claim (1.0.22, services/holder.py):
    # a key no session of the database holds is a process's that has ended,
    # and its claim goes back to the queue at once instead of after
    # `drain_stale_minutes`. NULL when the claim is not stamped (an inline
    # write, a release before 1.0.22, the instant between a claim and its
    # stamp): then the clock decides, as before. The database clears it
    # whenever the claim changes (db/triggers.py), so a stamp is always the
    # current claim's.
    graph_sync_claimed_by = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # The developer's, and moved by an edit only: the API returns it, the
    # history of a memory reads its "updated" event off it, and PATCH checks
    # `expected_updated_at` against it. The one path that edits sets it
    # (`AgentMemoryEngine.update`). No `onupdate`: until 2026-09-28 it had
    # one, which fires on every write to the row, and the worker writes the
    # row for its own bookkeeping (the facts drain's claim and status, the
    # reaper, the vectors, `manage reextract`, a deleted project's move), so
    # every memory processed in the background read as edited by nobody, and
    # a caller holding the version it had been given got 409 `stale_write`.
    # NULL until the first edit, as it always was.
    updated_at = Column(DateTime(timezone=True), nullable=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    # Org layer . INERT in M1 (writes never set it yet);
    # becomes the per-project isolation key at L2. FK CASCADE so a project delete
    # cascades its memories (load-bearing at L2).
    # SET NULL, not CASCADE. Deleting a project is deleting a folder, not
    # deleting what people remembered inside it. When L2 starts setting this
    # column, removing a project's data has to be an explicit operation that
    # says how many rows it is about to destroy, never a foreign key side
    # effect behind a button.
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        Index("idx_agent_mem_tenant_enduser", "tenant_id", "end_user_id"),
        Index("idx_agent_mem_tenant", "tenant_id"),
        Index("idx_agent_mem_graph_pending", "graph_sync_status"),
        # HNSW, or a semantic search is a sequential scan over every vector.
        # Measured on 50,000 rows: 287ms without, 7.8ms with.
        Index("idx_agent_memories_embedding_hnsw", "embedding",
              postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
        Index("idx_agent_mem_project", "tenant_id", "project_id"),
        Index("idx_agent_mem_vector_waiting", "embedding_next_at",
              postgresql_where=text("embedding_status = 'pending'")),
    )


# The stamp of a claim is the current claim's: cleared by the database
# whenever the claim changes (db/triggers.py).
_triggers.install_claimed_by(AgentMemory.__table__)


class AgentMemoryWindow(Base):
    """One window of a long memory's text, by where it begins and ends in
    the memory's current text, and its vector (1.0.33, services/
    memory_windows.py, written only with MEMORY_WINDOWS on): a search ranks
    the memory by the best of its own vector and its windows'.

    No copy of the text: a window is two offsets into `agent_memories.
    content`, and its vector is computed from that text by the drain
    (services/vectors.py, the memories' states and ladder). The vector is
    derived from the memory's words, so it goes with every change of them:
    ON DELETE CASCADE for every erasure that deletes the memory's row, and
    deleted by the edit, the forget and the other paths that keep the row
    (memory_windows.drop)."""
    __tablename__ = "agent_memory_windows"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    memory_id = Column(UUID(as_uuid=True), ForeignKey("agent_memories.id", ondelete="CASCADE"),
                       nullable=False)
    ordinal = Column(Integer, nullable=False)
    # Where the window is in the memory's text, in characters (Python's code
    # points, as the text is stored): text[start_char:end_char].
    start_char = Column(Integer, nullable=False)
    end_char = Column(Integer, nullable=False)
    # A memory's vector and its job (services/vector_state.py's states and
    # ladder): deferred, as theirs (db/bounded.py).
    embedding = deferred(Column(Vector(EMBEDDING_DIM), nullable=True))
    embedding_status = Column(String(20), nullable=False, server_default="pending")
    embedding_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    embedding_next_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("uq_agent_memory_windows_ordinal", "memory_id", "ordinal", unique=True),
        Index("idx_agent_memory_windows_tenant", "tenant_id"),
        Index("idx_agent_memory_windows_embedding_hnsw", "embedding",
              postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
        Index("idx_agent_memory_windows_vector_waiting", "embedding_next_at",
              postgresql_where=text("embedding_status = 'pending'")),
    )


class AgentProject(Base):
    """Projects / org layer above agents (L1, milestone 1).

    A developer-owned workspace that groups API keys (and, at L2/phase 2,
    isolates memories + facts). In M1 it is ORG-ONLY: a folder for keys + a
    staging/prod label + the audit/GDPR scope. It is the data-isolation wall on
    memories/facts is deferred to L2. The inert project_id columns on
    agent_memories/agent_facts are pre-placed now so L2 is purely additive.
    One account (tenant_id) holds many projects; exactly one is_default = the
    backfill + auto-create target. Deleting a project re-points its keys to
    Default (FK SET NULL), so a live key is never deleted from under a developer.
    """
    __tablename__ = "agent_projects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(128), nullable=False)        # 'Production' | 'Staging' | 'Default'
    slug = Column(String(64), nullable=False)          # lowercased, agent_id-safe
    description = Column(Text, nullable=True)
    is_default = Column(Boolean, nullable=False, default=False, server_default="false")
    # Where the project's memories are read by a model: "eu" or "global"
    # (services/regions.py, 8 Oct 2026). NULL follows the install's default
    # writer, which is every project of an install that never chose: the
    # behaviour of before, one writer for all.
    region = Column(String(16), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())

    __table_args__ = (
        Index("idx_agent_projects_tenant", "tenant_id"),
        # Declared on the model so the test schema enforces a duplicate
        # name or slug as a 409.
        Index("uq_agent_projects_tenant_name", "tenant_id", "name", unique=True),
        Index("uq_agent_projects_tenant_slug", "tenant_id", "slug", unique=True),
        CheckConstraint("region IS NULL OR region IN ('eu', 'global')",
                        name="ck_agent_projects_region"),
        # NOTE: "at most one is_default per user" is a Postgres PARTIAL unique
        # index (WHERE is_default), created in the migration only; the service
        # layer also enforces a single default on writes.
    )


class AgentRateLimit(Base):
    """Fixed-window rate-limit counters for the /v1 API.

    One row per (account, window_type, bucket). `bucket` is the truncated
    timestamp key; only `minute` is written. The /v1 and /admin dependencies
    increment it and answer 429 past RATE_LIMIT_PER_MINUTE, which is off
    unless the operator sets it and is the same for every key: the tier plays
    no part (services/rate_limit.py). Postgres-backed, no Redis.
    """
    __tablename__ = "agent_rate_limits"

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    window_type = Column(String(8), primary_key=True)  # 'minute' | 'hour' | 'day'
    bucket = Column(String(20), primary_key=True)
    count = Column(Integer, nullable=False, server_default="0")


class AgentWebhookDelivery(Base):
    """One outbound delivery attempt (the outbox).

    Enqueued pending by emit_event; a scheduler worker POSTs it with backoff.
    Mirrors BackgroundJobOutbox: status + attempts + next_retry_at.
    """
    __tablename__ = "agent_webhook_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_id = Column(UUID(as_uuid=True), ForeignKey("agent_webhook_endpoints.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(50), nullable=False)
    payload = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, server_default="pending", index=True)  # pending|delivered|failed
    attempts = Column(Integer, nullable=False, server_default="0")
    max_attempts = Column(Integer, nullable=False, server_default="5")
    response_status = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True, index=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    # The end user the event names (`data.user_id`) was erased while this
    # was still to be sent: it goes out with the id its receiver knows, and
    # takes their pseudonym, or loses the id without SECRET_KEY, once it is
    # finished (services/end_user_places.rename_in_deliveries, audit of 27 Oct
    # 2026: a lookup by time at the finish missed one enqueued while the
    # erasure was open, and every one without a key).
    erased_user = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, onupdate=func.now())

    __table_args__ = (
        # The listing reads the newest failure per endpoint, and the purge reads
        # by age. Without this both sort the whole table: measured on 200,000
        # deliveries the listing was a parallel sort of every row in the tenant
        # to take 500 of them. Fast today, O(n log n) in a table that until now
        # nothing ever trimmed.
        Index("idx_agent_webhook_deliveries_tenant_recent",
              "tenant_id", "created_at"),
    )

class AgentWebhookEndpoint(Base):
    """A developer-configured outbound webhook target.

    Korely POSTs events (memory.created, fact.invalidated, key.expiring,
    key.expired; services/webhooks.VALID_EVENTS) to
    `url`, signed HMAC-SHA256 with `secret` (Standard Webhooks scheme). The
    secret is stored in plaintext because we need it to SIGN outgoing payloads
    (unlike API keys, which are hashed because we only ever VERIFY them).
    """
    __tablename__ = "agent_webhook_endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    url = Column(String(1000), nullable=False)
    secret = Column(String(100), nullable=False)        # whsec_...
    events = Column(JSON, nullable=False)               # ["memory.created", ...] or ["*"]
    active = Column(Boolean, nullable=False, server_default="true")
    failure_count = Column(Integer, nullable=False, server_default="0")
    last_delivery_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, onupdate=func.now())
    # The project whose events this endpoint receives (8 Oct 2026, the hosted
    # product's r64 of 7 Oct). NULL: every project of the account, as before.
    # Set: that project's events and the account's, so a staging receiver no
    # longer gets production's end users. A deleted project takes its
    # endpoints with it: SET NULL would turn one into an every-project endpoint.
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="CASCADE"),
                        nullable=True, index=True)


class PredicateCluster(Base):
    """One relation, however the extractor happened to phrase it.

    The store learns its vocabulary from its own traffic instead of being given
    one. A tenant in insurance ends up with `covered_under` and `claim_status`;
    a tenant in recruiting ends up with something else; neither had to be
    anticipated, and neither pollutes the other.

    One vocabulary per end user of a project (18 Oct 2026: every end user is
    a thing of its own, every project is a thing of its own). It
    was one per tenant until that morning, then one per project. A relation
    carries values of the facts it was learned from (`example_object`, the
    `centroid` made of it) and decisions taken on them (`is_functional`,
    `family`, the canonical name coined from the first phrase), and all of it
    went into the prompts and the judgements of every other end user of the
    scope: an EU project's value into a Global project's prompt (the code
    audit of 17 Oct 2026), one end user's value and vote into another's
    (audit of 18 Oct 2026, the shared vocabulary). Now each end user's
    vocabulary is learned from that end user's facts alone and goes with them
    at an erasure. `project_id` NULL is the vocabulary of the facts of no
    project; `end_user_id` NULL that of the facts of no end user (the shared
    facts of a project).

    `is_functional` is owl:FunctionalProperty: the relation holds one value at a
    time, so a new value supersedes the old one. NULL means undecided, and
    undecided means do not supersede: two facts standing is a recoverable state,
    a true fact deleted is not.
    """

    __tablename__ = "predicate_clusters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="CASCADE"),
                        nullable=True, index=True)
    end_user_id = Column(String(255), nullable=True)
    canonical = Column(String(200), nullable=False)
    # Deferred, as the facts' vector (db/bounded.py): a project's move read
    # every relation of the default project with it (audit of 20 Oct 2026).
    centroid = deferred(Column(Vector(EMBEDDING_DIM), nullable=True))
    is_functional = Column(Boolean, nullable=True)
    # 2026-09-27 (BUG-109): the family of the relation for the panels and the
    # filters, asked once per new relation the static map does not know.
    # memory_core.store writes it (the hosted product had the same column).
    family = Column(String(20), nullable=True)
    # The object the relation was first seen with: shown to the model next to
    # the relation's name ("subscribes_to (as in: ... Business plan)") and to
    # the judge next to the single-value tag, because cardinality is typed by
    # the value (Lin, Mausam, Etzioni, EMNLP 2010 §2). The hosted product had
    # the same column.
    example_object = Column(String(500), nullable=True)
    member_count = Column(Integer, nullable=False, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("uq_predicate_clusters_user_canonical", "tenant_id", "project_id", "end_user_id",
              "canonical", unique=True, postgresql_nulls_not_distinct=True),
    )


class PredicateAlias(Base):
    """A surface phrase already resolved to a cluster.

    The point of the table is that resolving costs a model call, and the same
    phrase arrives thousands of times. Asked once, then free: a store that has
    been running a while spends nothing here, and a fresh one pays only for the
    relations it has genuinely never seen.
    """

    __tablename__ = "predicate_aliases"

    # The phrase belongs to a cluster, so to that cluster's end user and
    # project: one phrase per end user of a project (the unique index below),
    # the same phrase free for another (18 Oct 2026; it was the primary key
    # with the tenant, then one per project).
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="CASCADE"),
                        nullable=True, index=True)
    end_user_id = Column(String(255), nullable=True)
    surface = Column(String(200), primary_key=True)
    cluster_id = Column(UUID(as_uuid=True), ForeignKey("predicate_clusters.id", ondelete="CASCADE"),
                        primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("uq_predicate_aliases_user_surface", "tenant_id", "project_id", "end_user_id",
              "surface", unique=True, postgresql_nulls_not_distinct=True),
    )


class AuditEvent(Base):
    """Who did what, when. Append-only.

    A buyer in a regulated sector asks for this before they ask what the product
    does, and the answer has to be a table they can query and export rather than
    a line in a brochure. The service this was extracted from called a telemetry
    helper at twenty-two places and wrote nothing, because the helper referenced
    a table that only existed in the other product and the call was wrapped in a
    try/except. Code that appears to keep records and does not is worse in a
    review than code that never claimed to.

    **No content, ever.** Not the memory text, not the fact's object, not a
    query string. An audit row that copies the data makes erasure a lie: delete
    the memory and the log still holds it, and a feature bought for compliance
    becomes the thing that breaks it. References and counts only.
    """

    __tablename__ = "audit_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ts = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    # Which key acted. SET NULL rather than CASCADE: revoking a key must not
    # erase the record of what it did, which is the whole point of keeping one.
    # Indexed (1.0.40): the purge of revoked keys (services/keys.purge_revoked)
    # sets it NULL key by key, and without the index each key read the trail.
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("agent_api_keys.id", ondelete="SET NULL"),
                        nullable=True, index=True)
    # The project the call was made in: the project of the key, as
    # `key_project` resolves it, stamped when the row is written. GET
    # /v1/audit reads only its key's project. Until 2026-09-28 there was no
    # such column and that route listed every project's end user ids, memory
    # ids and actions to any key of the account (migration b64f02d8e9a1).
    # NULL: produced with no key (before the MCP recorded one), or on an
    # install with no default project. The operator's screen reads them all.
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="SET NULL"), nullable=True)

    actor = Column(String(16), nullable=False)      # rest | mcp | manage | worker | dashboard | oauth
    action = Column(String(32), nullable=False)     # see services/audit.py
    # ok | error; an error's token (forbidden, not_found, ...) is meta.code
    # (api/errors.refused). `denied` was named here and never written (audit
    # of 27 Oct 2026).
    result = Column(String(8), nullable=False, server_default="ok")

    # The caller's handle for an end user: personal data (GDPR Art. 4(5);
    # this said "not a person" until the legal review of 11 Oct 2026). Kept
    # with the rest of the row for AUDIT_RETENTION_DAYS (400 by default); once
    # the end user is erased, as a keyed pseudonym (services/pseudonyms.py).
    end_user_id = Column(String(255), nullable=True, index=True)
    # The row acted on, by id. Never its content.
    target_id = Column(String(255), nullable=True)
    # Counts and shapes: {"facts": 3, "invalidated": 1}. Never text.
    meta = Column(JSON, nullable=True)
    # What a read returned, by public id: {"memories": [...], "facts": [...]},
    # at most 100 of each, the counts in `meta` (memories_read, facts_read).
    # With `as_of` it rebuilds what an agent knew when it decided (bussola 2e,
    # 2026-10-01, the same column as the hosted product). Ids, never content.
    read_ids = Column(JSON, nullable=True)

    ip = Column(String(64), nullable=True)

    __table_args__ = (
        # The two questions people actually ask: what happened on this account
        # recently, and everything about one end user (which is what an access
        # request looks like).
        Index("idx_audit_tenant_ts", "tenant_id", "ts"),
        Index("idx_audit_tenant_enduser_ts", "tenant_id", "end_user_id", "ts"),
        # What a key reads: its own project's events, newest first.
        Index("idx_audit_tenant_project_ts", "tenant_id", "project_id", "ts"),
    )


class AgentReadMark(Base):
    """Which fact a named reader was handed as true, and when (agent
    notices, 1.0.15; services/read_marks.py).

    One row per account, project, reader and fact, written only by a read
    that names its reader (`reader` on GET /v1/context, GET /v1/facts and
    their MCP twins) and reads what is true now. The reader is the caller's
    word for itself, the rule of an `agent_id`, never authenticated, and it
    filters nothing. No text: the fact's id, the reader's name, the end user
    the fact is about and three times.

    Kept READ_MARKS_RETENTION_DAYS (90) after the last read, and a row whose
    change was shown (`notified_at`) goes at the next purge. It goes with
    its fact (ON DELETE CASCADE), its project and its account, and with its
    end user's erasure (services/end_user_places.py, `read_marks`): deleted,
    never kept under a pseudonym, since it is no record of what was done,
    only of what to tell a reader.
    """

    __tablename__ = "agent_read_marks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    # The project of the read, which is the fact's: NULL only on an install
    # with no Default project, where facts have none either (keys.key_project).
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="CASCADE"),
                        nullable=True)
    reader = Column(String(255), nullable=False)
    fact_id = Column(UUID(as_uuid=True), ForeignKey("agent_facts.id", ondelete="CASCADE"),
                     nullable=False)
    # The fact's end user, copied from the fact, never the request's: an
    # erasure finds the row by it without reading the facts.
    end_user_id = Column(String(255), nullable=True)
    # The database's clock, the one `closed_known_at` is written on.
    first_read_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_read_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # When a change of the fact was shown to the reader: nothing sets it in
    # 1.0.15 (the notices come later), the purge already reads it.
    notified_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One row per reader and fact. NULLS NOT DISTINCT: a project of NULL
        # is one project, as in the facts' own unique index.
        Index("uq_agent_read_marks_reader_fact", "tenant_id", "project_id", "reader", "fact_id",
              unique=True, postgresql_nulls_not_distinct=True),
        # What a reader read, newest first: the notices' question.
        Index("idx_agent_read_marks_reader_read", "tenant_id", "project_id", "reader",
              "last_read_at"),
        # The cascade from a deleted fact.
        Index("idx_agent_read_marks_fact", "fact_id"),
    )


class AgentLlmUsage(Base):
    """What each model call cost, and which agent caused it.

    On the hosted product this number is the vendor's margin and stays hidden.
    Self-hosted the model bill arrives at the operator's own account, and
    "which of my agents burned four hundred euros last month" is their
    question, with nobody else able to answer it. Until this table existed the
    counts were computed on every call inside `core/llm.py` and thrown away.

    **Tokens, not money.** A token count is a fact and stays true. A price is an
    assumption that goes stale the next time a provider changes a rate or the
    operator negotiates one, and a stale price written into a database is wrong
    silently, forever. Cost is computed at read time from a rate card the
    operator can edit.

    One row per call rather than a daily rollup: the useful question is usually
    "why was that week expensive", and the answer is a memory, a stage and a
    model. A rollup can be a GROUP BY over this; this cannot be recovered from a
    rollup.
    """

    __tablename__ = "agent_llm_usage"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ts = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)

    # extraction | contradiction | predicates | objects | embedding | operator | unknown
    stage = Column(String(24), nullable=False)
    provider = Column(String(24), nullable=False)   # gemini | mistral
    model = Column(String(100), nullable=False)
    # Who answered and where (8 Oct 2026, the one codebase): `provider` is
    # the client that made the call, and every OpenAI-compatible host goes
    # through "mistral"; the vendor (scaleway, openrouter, google...) and the
    # region of the writer tell them apart, for a cap on one vendor's spend.
    # NULL on rows written before, and on the vectors.
    vendor = Column(String(24), nullable=True)
    region = Column(String(16), nullable=True)

    prompt_tokens = Column(Integer, nullable=False, server_default="0")
    # Thinking tokens are billed as output and are counted here. Leaving them
    # out understates exactly the models most likely to produce them, which is
    # the opposite of the point of this table.
    completion_tokens = Column(Integer, nullable=False, server_default="0")
    # The part of `prompt_tokens` the provider served from its prompt cache
    # (2026-10-07, as the hosted product): Scaleway caches prefixes by itself
    # and bills them less, Gemini too. Counted, not priced: the report prices
    # them at the input rate, a ceiling, until a provider publishes its rate.
    cached_tokens = Column(Integer, nullable=False, server_default="0")

    # Attribution. A string, not a foreign key: a namespace the caller chose,
    # the same one on memories and facts. No end user: a cost does not need
    # to know whose. The rows named one until the audit of 19 Oct 2026, and
    # an erasure took it off afterwards, so a row written while the erasure
    # was open kept the erased id in clear for 400 days (migration
    # d4b7e2a9c8f1 drops the column).
    agent_id = Column(String(255), nullable=True, index=True)
    # What was being processed. SET NULL rather than CASCADE: erasing a memory
    # must not erase the record that it cost money, or the month stops adding up.
    memory_id = Column(UUID(as_uuid=True), ForeignKey("agent_memories.id", ondelete="SET NULL"),
                       nullable=True)
    # The project the call worked for (1.0.41, migration a3d7f9c1e5b2): the
    # memory's, the fact's, or the one project a request reads. NULL on rows
    # written before, on a call of the whole account (the vocabulary of its
    # relations, an operator's run, a read across projects), and on a call
    # whose project was deleted while it ran. A project deleted later hands
    # its rows to the Default project, as it hands its memories
    # (projects._move_what_is_left); SET NULL is only the constraint's last
    # word. The usage report's `by_project` groups by it, its rows adding up
    # to the account's totals.
    project_id = Column(UUID(as_uuid=True), ForeignKey("agent_projects.id", ondelete="SET NULL"),
                        nullable=True)

    __table_args__ = (
        # The three reports: the month for this account, and the month broken
        # down by agent and by project.
        Index("idx_llm_usage_tenant_ts", "tenant_id", "ts"),
        Index("idx_llm_usage_tenant_agent_ts", "tenant_id", "agent_id", "ts"),
        Index("idx_llm_usage_tenant_project_ts", "tenant_id", "project_id", "ts"),
    )


class Operator(Base):
    """A person who logs in to administer one account.

    Distinct from an API key on purpose. A key is what a program runs with and
    belongs in an environment variable; an operator is a human being with a
    password, and the two want different lifetimes, different revocation and
    different audit lines.

    **No default credentials.** There is no `admin/admin` here and there never
    will be: a self-hosted product with a known password is a product that ends
    up indexed. `create-operator` generates one, prints it once and stores only
    the hash, exactly as `create-tenant` does with the key.

    Scoped to a tenant, like a key, because an install can hold several of an
    agency's own clients and each wants its own people.
    """

    __tablename__ = "operators"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    # Unique across the install: somebody typing a username at a login box has
    # not said which tenant they belong to, and asking them would be asking
    # them to know something they have no way of knowing.
    username = Column(String(64), nullable=False, unique=True)
    # scrypt, not SHA-256. A key is 256 bits of randomness and a digest is
    # enough; a password is a word somebody can remember, and a digest of that
    # is a lookup table away from plaintext.
    password_hash = Column(String(255), nullable=False)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    disabled_at = Column(DateTime(timezone=True), nullable=True)
    # The person who sets what is the whole install's (the model provider, its
    # key, the models), when the install holds more than one account (audit of
    # 17 Oct 2026: any account's administrator could switch them for every
    # other). Set from the command line only (`manage grant-install-admin`);
    # the first operator of an install gets it.
    install_admin = Column(Boolean, nullable=False, server_default="false")
    # The wrong passwords and the locks are LoginAttempt's, per address: they
    # were two columns here until 2026-10-06, and a lock on the operator let
    # anybody who knew the name keep the operator out.


class LoginAttempt(Base):
    """Wrong passwords at the operator's login, per address and username
    (2026-10-06, the audit of 6 October, row 7).

    A password box with no ceiling is one anybody can grind, so ten wrong
    answers lock it; per address, because a lock on the account let anybody
    who knew the operator's name keep the operator out from anywhere. The
    failures of one address over every username are counted too, before any
    hash is computed (services/operators.py). A row and not a cache, as the
    lock always was: a lockout you can clear by waiting for a deploy is not
    one. Unknown usernames have rows too: they count for the address.
    """

    __tablename__ = "login_attempts"

    ip = Column(String(64), primary_key=True)
    username = Column(String(64), primary_key=True)
    failures = Column(Integer, nullable=False, server_default="0")
    window_start = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    locked_until = Column(DateTime(timezone=True), nullable=True)


class OperatorSession(Base):
    """One login. Opaque token, stored as a hash, with an end date.

    Not a JWT. A signed token cannot be taken back before it expires, and the
    thing an operator most wants on the day a laptop goes missing is to be able
    to take it back. A row can be deleted.
    """

    __tablename__ = "operator_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operator_id = Column(UUID(as_uuid=True), ForeignKey("operators.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    # SHA-256 of a 256-bit random token: high entropy, so a digest is the right
    # shape here, unlike the password above.
    token_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    # No `last_seen_at`: it cost a write and a commit on every request of the
    # dashboard and nothing read it (audit of 17 Oct 2026; dropped by
    # c3f5a7e9b1d4).
    # What asked for it, for the line in the audit that says where a session
    # came from. Never used to authorise anything.
    user_agent = Column(String(255), nullable=True)
    ip = Column(String(64), nullable=True)


class InstallSetting(Base):
    """Settings an operator changes from the screen instead of from a file.

    The provider key is the one that matters and the reason this exists. It
    used to live only in `.env`, read once at boot, which meant that the moment
    a customer bought their own Google key somebody had to open an SSH session,
    edit a file and restart the service. That is not something you ask of a
    customer, and it is the moment an install stops feeling like a product.

    Install-wide rather than per tenant. One company, one machine, one model
    account is the shape of the thing being sold; a per-tenant key is a real
    need for an agency running several clients on one box, and it is additive
    when somebody asks for it.

    Values are encrypted at rest. A model API key is something the customer
    pays for by the token, and "it is only an MVP" is not an answer to why it
    was sitting in a table in clear text.
    """

    __tablename__ = "install_settings"

    name = Column(String(64), primary_key=True)
    # Fernet, keyed off SECRET_KEY. Never returned to a client: the screen is
    # told whether a value is set and what its last four characters are, which
    # is enough to recognise it and useless to anybody else.
    value_encrypted = Column(Text, nullable=False)
    # For "set on 3 September by anna", which is the question an audit asks.
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                        onupdate=func.now())
    updated_by = Column(String(64), nullable=True)


class InstallState(Base):
    """What the install knows about itself, in clear text. Nothing secret goes
    here: secrets are InstallSetting's, encrypted.

    One row so far, `embedding_model`: the model that computed the stored
    vectors (2026-10-06). The default embedder changed in 0.1.19, from Gemini
    to EmbeddingGemma on the machine, and both give 768 numbers, so nothing
    fails when one model's questions search the other's vectors; the start
    compares this with the model in force and stops on a difference
    (services/vectors.py), and `manage recompute-vectors` writes it when every
    vector has moved to the new one.
    """

    __tablename__ = "install_state"

    name = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                        onupdate=func.now())
