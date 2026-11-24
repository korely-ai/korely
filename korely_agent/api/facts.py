"""/v1 facts API (Sprint 18 B) — the typed knowledge-graph moat over the agent store.

GET /v1/facts   — deterministic SQL retrieval (no model call): subject / entity /
                  predicate / predicate_family / include_invalidated / as_of /
                  known_as_of.
POST /v1/facts  — write a triple directly (skips extraction); with a model key the
                  vocabulary and the contradiction check still ask the model.

Bi-temporal: every fact has valid_from / invalid_at; `as_of` returns point-in-time
state ("what was true on 2026-06-01"), scoped per end user.

And the second clock (11 Oct 2026): `known_as_of` returns the facts as the
store knew them at a moment ("what did we know on 2026-06-01"): learned by
then (`created_at`), with the ends learned by then (`closed_known_at`).
services/facts/temporal.py has the two clocks.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from ._bodies import FROM_THE_N8N_NODE_0_1_0, Body, NamespaceId, its_parameters_only
from .errors import (EMBEDDER_BUSY, SERVICE_PAUSED, V1_KEY_ROUTE_ERRORS, CodedHTTPException,
                     not_a_model_that_failed, refused, v1_errors)
from sqlalchemy.orm import Session

from ..core.auth import (OutOfScopeRefusal, count_the_read, require_scopes,
                         require_read_with_quota,
                         enforce_rate_limit, key_end_users, key_project, reading, sees_row)
from ..db import get_db, AgentApiKey, AgentFact
from ..services import facts as agent_facts
from ..services import limits, memory_write, read_marks
from ..services.context import SELF_SUBJECT_TYPE
from ..services.sentinels import EVERY_END_USER as _ALL
from ..services.facts.reads import FAMILY_FILTER, quoted
from ..services.facts.temporal import parse_moment
from ..services.ids import NAMESPACE_ID_RULE, fact_public_id
from ..services.memory_core.fact_extractor import ALLOWED_SUBJECT_TYPES
from ..services.memory_core.families import FAMILY_ORDER
from ..services.usage import log_v1_request

router = APIRouter(prefix="/v1", tags=["v1"], dependencies=[Depends(enforce_rate_limit)],
                   responses=V1_KEY_ROUTE_ERRORS)

_read_key = require_read_with_quota("memories:read")
_write_key = require_scopes("memories:write")


def parse_as_of(s: Optional[str], field: str = "as_of") -> Optional[datetime]:
    # Blank means absent, and whitespace is blank. `as_of=` is how several HTTP
    # clients serialize "no value", so refusing it would break callers who never
    # asked for a point-in-time query; and refusing "" while accepting "  " is
    # the kind of difference nobody can predict from the documentation.
    # Unreadable is a different thing from absent and still raises: this is the
    # moat query, and quietly answering about today when somebody asked about
    # March is the failure mode this endpoint exists to have not.
    if not s or not s.strip():
        return None
    # The parser the MCP reads `as_of` with too (temporal.parse_moment): a
    # value with no offset is UTC on both doors.
    try:
        moment = parse_moment(s)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            # Named after the field the caller actually sent. The same
            # parser serves `as_of` and `at`, and saying "as_of" to
            # somebody who sent `at` sends them looking for a field that is
            # not in their request.
            detail=f"{field} must be an ISO date (2026-06-01) or datetime",
        )
    # A fact's `valid_from` of the year 0 in UTC was stored and could not be
    # read back: every read of that end user's facts answered 500 (audit of
    # 20 Oct 2026). The reads' dates are held to the same years, one rule.
    problem = limits.moment_problem(field, moment)
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
    return moment


def names_problem(*, user_id=None, agent_id=None, subject=None, entity=None, predicate=None,
                  predicate_family=None, reader=None) -> Optional[str]:
    """The first problem of what a read of the facts narrows on, by the rule
    of every door (services/limits.py), REST and MCP's korely_get_facts
    alike: the ids by `id_problem`, the names by `name_problem` and the
    column each is compared with (`entity` meets the object side too). And
    the `reader` it names (agent notices), which narrows nothing and is an
    agent's name by the rule of a written `agent_id` (read_marks.
    reader_problem)."""
    return (limits.id_problem("user_id", user_id) or limits.id_problem("agent_id", agent_id)
            or read_marks.reader_problem(reader)
            or limits.name_problem("subject", subject)
            or limits.name_problem("entity", entity, limits.MAX_FACT_OBJECT_CHARS)
            or limits.name_problem("predicate", predicate)
            or limits.name_problem("predicate_family", predicate_family))


_KNOWN_AS_OF = ("ISO date/datetime: the facts as the store knew them then: learned by "
                "then, with the ends learned by then, true at `as_of` (default: that same "
                "moment). A move made in September and told in October: `as_of` September "
                "returns the new address, `known_as_of` September the old one. A moment "
                f"{limits.MOMENTS_STORED}; outside it, or unreadable, a 422.")


# The names the engine writes, read from it and never copied here: the spec
# described a family with `location` and `role`, which are not families (review
# of 1 Nov 2026). A new fact's subject type is stored as its writer sent it,
# any value the column holds (facts.write_fact_direct,
# memory_core.writing._new_row); a write of a triple already true reconfirms
# that row and keeps its type (memory_core.writing, step 2: _already_true),
# so a `self` sent then is not stored, and both descriptions say so (review of
# 3 Nov 2026). Extraction gives the engine's types, and `self` is the one GET
# /v1/profile reads (services/context.is_about_the_end_user), so the spec
# names both and says that any other is possible. The context block tells the
# end user by the subject alone (memory_core.presentation.context_line) and
# never reads the type, so `self` is described by the one route that reads it
# (review of 4 Nov 2026).
_FAMILY = f"The relation's family: one of {quoted(FAMILY_ORDER)}."
_SUBJECT_TYPE_CHARS = AgentFact.__table__.c.subject_type.type.length
_EXTRACTED_TYPES = quoted(sorted(ALLOWED_SUBJECT_TYPES))
_SELF_TYPE = (f"`{SELF_SUBJECT_TYPE}` (lower case, matched exactly): GET /v1/profile counts a "
              "fact so typed as one about the end user and lists it first")
_SELF_NOT_IN_THE_BLOCK = ("The context block of GET /v1/context does not read the type: it "
                          "tells the end user by the subject")
_TYPE_KEPT = ("A write that restates a fact already true keeps the type the fact was "
              "first stored with")
_SUBJECT_TYPE = (f"What the subject is, as the write that stored the fact gave it: "
                 f"extraction gives one of {_EXTRACTED_TYPES}; a POST /v1/facts may have "
                 f"sent any type of up to {_SUBJECT_TYPE_CHARS} characters. {_TYPE_KEPT}. "
                 f"{_SELF_TYPE}. {_SELF_NOT_IN_THE_BLOCK}.")
_SUBJECT_TYPE_SENT = (f"What the subject is: any type of up to {_SUBJECT_TYPE_CHARS} "
                      "characters, stored as sent on a new fact (an empty one as `unknown`). "
                      f"{_TYPE_KEPT}, and POST answers that fact's id. Extraction gives one "
                      f"of {_EXTRACTED_TYPES}. {_SELF_TYPE}, as it does a fact whose subject "
                      f"is the end user's id. {_SELF_NOT_IN_THE_BLOCK}.")


def moments_asked(as_of: Optional[str], known_as_of: Optional[str]) -> Optional[dict]:
    """What the audit row keeps of the moments a read asked about."""
    out = {k: v for k, v in (("as_of", as_of), ("known_as_of", known_as_of)) if v}
    return out or None


class Fact(BaseModel):
    """A fact as a read answers it. A field added in a later release still
    passes (`extra` is allowed)."""
    # Typed since 0.1.27, so the OpenAPI document lists its fields
    # (services.facts.serialize_fact builds it): it was a free object, and the
    # site's spec could not show `closed_known_at` (A, 11 Oct 2026). The class
    # docstring is the schema's public description, so the history is here.
    model_config = ConfigDict(extra="allow")

    id: str = Field(description="The fact's `fct_` id.")
    subject: str = Field(description="The subject as it was written.")
    subject_type: Optional[str] = Field(default=None, description=_SUBJECT_TYPE)
    predicate: str = Field(description="The relation, canonical: `lives_in`.")
    predicate_raw: str = Field(description="The relation as it was extracted.")
    object: str = Field(description="The object as it was written.")
    object_is_literal: bool = Field(description="True for a value (a date, an amount), false for an entity.")
    predicate_family: Optional[str] = Field(default=None, description=_FAMILY)
    confidence: Optional[float] = Field(
        default=None, description="0 to 1: the extraction model's own estimate for a fact read "
                                  "from a memory, what POST /v1/facts was sent (0.9 by default) "
                                  "for one written directly; a correction keeps the fact's, and two "
                                  "facts a rename makes one keep the higher. A "
                                  "label, not a probability that the fact is true, and no read "
                                  "filters on it: a model's declared confidence sits near 1 "
                                  "even when it is wrong.")
    user_id: Optional[str] = Field(default=None, description="The end user the fact belongs to.")
    agent_id: Optional[str] = Field(default=None, description="The agent namespace it was written under.")
    valid_from: Optional[str] = Field(
        default=None, description="When it became true in the world: an ISO date/datetime "
                                  f"{limits.MOMENTS_STORED}; outside it, or unreadable, a 422.")
    invalid_at: Optional[str] = Field(
        default=None, description="When it stopped being true. Null while it holds, and, read with "
                                  "`known_as_of`, while the store had not learned the end yet.")
    invalidated_by: Optional[str] = Field(default=None, description="The `fct_` id of the fact that closed it.")
    source_memory_id: Optional[str] = Field(
        default=None, description="The memory it was extracted from; null on a row a caller wrote "
                                  "(POST /v1/facts, or a correction since 1.0.27).")
    created_at: Optional[str] = Field(default=None, description="When the store learned the fact.")
    closed_known_at: Optional[str] = Field(
        default=None, description="When the store learned the fact had ended. Null while it is open.")
    subject_canonical: str = Field(description="The subject's current name, after its aliases and its "
                                               "object link.")
    object_canonical: str = Field(description="The object's current name, after its aliases and its "
                                              "object link.")
    subject_link_id: Optional[str] = Field(
        default=None, description="The object link that shows the subject under `subject_canonical`: "
                                  "two names of one thing the writer judged the same (memory_core 6.1.0, "
                                  "off by default). Null when none does.")
    object_link_id: Optional[str] = Field(
        default=None, description="The same, for the object.")
    tense: str = Field(description="What the text said: current, past or planned.")
    last_confirmed_at: Optional[str] = Field(default=None, description="The last time a memory said it again.")
    observation_count: int = Field(description="How many memories said it.")
    source_memory_ids: list[str] = Field(description="Every memory that said it.")


class FactsResponse(BaseModel):
    facts: list[Fact]
    total: int


class ProfileResponse(BaseModel):
    user_id: str
    as_of: Optional[str]
    # 2026-10-11 — additive: the profile as the store knew it then.
    known_as_of: Optional[str] = None
    facts: list[Fact]
    by_family: dict[str, list[Fact]]
    total: int
    truncated: bool  # true when total > len(facts) — the profile is capped at 200


class FactWriteResponse(BaseModel):
    """The write shape of POST /v1/facts (and the facts[] inline on a memory
    write): the new fact + the ids it superseded."""
    id: str
    subject: str
    predicate: str
    object: str
    predicate_family: Optional[str] = Field(default=None, description=_FAMILY)
    valid_from: Optional[str] = None
    invalidated: list[str] = []
    # 2026-09-27 — additive. `tense` is what the text said (current | past |
    # planned); a past statement closes the fact it restates, and then
    # `invalid_at` is set. `observation_count` > 1 means the write restated a
    # fact the store already held (reconfirmed, not duplicated).
    tense: Optional[str] = "current"
    invalid_at: Optional[str] = None
    observation_count: int = 1
    # 2026-10-07 — additive: whose fact it is, as GET /v1/facts says.
    user_id: Optional[str] = None
    agent_id: Optional[str] = None


class ForgetBody(Body):
    """Closing a fact. Everything optional: the common case is a bare POST."""
    at: Optional[str] = Field(
        default=None,
        description="ISO date/datetime it STOPPED being true. Defaults to now. "
                    "This is the date of the change, not the date you noticed "
                    "it: set it to when the world changed and `as_of` before "
                    "that still returns the fact. Equal to the fact's `valid_from` "
                    "it retracts the fact (true at no moment, kept in the history); "
                    "earlier is a 422. With no `at`, a fact dated ahead ends at its "
                    "own start, never before it.",
    )


class ForgetResponse(BaseModel):
    id: str
    status: str          # "forgotten" | "already_forgotten"
    invalid_at: Optional[str] = None
    audit_id: Optional[str] = None


class CorrectBody(Body):
    """Superseding a fact with corrected values. At least one field."""
    subject: Optional[str] = Field(default=None, min_length=1, max_length=limits.MAX_FACT_NAME_CHARS)
    predicate: Optional[str] = Field(default=None, min_length=1, max_length=limits.MAX_FACT_NAME_CHARS)
    object: Optional[str] = Field(default=None, min_length=1, max_length=limits.MAX_FACT_OBJECT_CHARS)


class FactBody(Body):
    subject: str = Field(min_length=1, max_length=limits.MAX_FACT_NAME_CHARS)
    predicate: str = Field(min_length=1, max_length=limits.MAX_FACT_NAME_CHARS)
    object: str = Field(min_length=1, max_length=limits.MAX_FACT_OBJECT_CHARS)
    user_id: Optional[NamespaceId] = Field(
        default=None, max_length=limits.MAX_ID_CHARS,
        description="End user this fact is about. " + memory_write.IDENTITY_RULE
                    + NAMESPACE_ID_RULE)
    agent_id: Optional[NamespaceId] = Field(
        default=None, max_length=limits.MAX_ID_CHARS,
        description="Agent namespace: the agent writing. " + memory_write.IDENTITY_RULE
                    + NAMESPACE_ID_RULE)
    run_id: Optional[str] = Field(default=None, max_length=limits.MAX_ID_CHARS)
    subject_type: str = Field(default="unknown", max_length=_SUBJECT_TYPE_CHARS,
                              description=_SUBJECT_TYPE_SENT)
    object_is_literal: bool = False
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    valid_from: Optional[str] = None
    # 2026-09-27 — additive. The time of the fact is SAID by the writer, as
    # the extractor says it for a memory (Graphiti §2.2.3): the store no longer
    # guesses it from the verb form ("worked_at"), which was a list of English
    # past forms. "past" closes the open fact it restates.
    tense: Literal["current", "past", "planned"] = Field(
        default="current",
        description="current | past | planned. A past fact closes the open fact it restates.",
    )


@router.get("/facts", response_model=FactsResponse)
def list_facts(
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
    _only_its_own: None = Depends(FROM_THE_N8N_NODE_0_1_0),
    user_id: Optional[str] = Query(default=None, max_length=limits.MAX_ID_CHARS, description="Filter to one end user."),
    agent_id: Optional[str] = Query(default=None),
    subject: Optional[str] = Query(default=None, description="Match the subject side."),
    entity: Optional[str] = Query(default=None, description="Match subject OR object side."),
    predicate: Optional[str] = Query(default=None),
    predicate_family: Optional[str] = Query(default=None, description=FAMILY_FILTER),
    include_invalidated: bool = Query(default=False),
    as_of: Optional[str] = Query(default=None, description="ISO date/datetime: point-in-time validity, "
                                 f"{limits.MOMENTS_STORED}; outside it, or unreadable, a 422."),
    known_as_of: Optional[str] = Query(default=None, description=_KNOWN_AS_OF),
    limit: int = Query(default=50, ge=1, le=limits.MAX_PAGE),
    offset: int = Query(default=0, ge=0, le=limits.MAX_OFFSET),
    exclude_sessions: bool = Query(
        default=False,
        description="Leave out the facts only work sessions state (memories written with "
                    "`kind: session`, what a coding assistant's hooks save). Default false."),
    reader: Optional[str] = Query(default=None, max_length=limits.MAX_ID_CHARS,
                                  description=read_marks.READER),
) -> FactsResponse:
    """The typed facts, a page at a time, filtered by end user, agent,
    subject (`subject`), either side (`entity`) and relation (`predicate`,
    `predicate_family`). A database read: no model is asked.

    By default the facts true now. `as_of` gives the facts true at a date,
    `known_as_of` the facts as the store knew them at a moment, and
    `include_invalidated=true` adds the facts that ended, each with its
    `invalid_at` and the `invalidated_by` that closed it.
    """
    # The ids and the names it narrows on by every door's rule: a NUL byte
    # reached the driver and came back as a 500, in the ids until the audit
    # of 17 Oct 2026 and in `subject` and `entity` until that of 18 Oct.
    problem = names_problem(user_id=user_id, agent_id=agent_id, subject=subject, entity=entity,
                            predicate=predicate, predicate_family=predicate_family, reader=reader)
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
    eu: Any = reading(key, user_id, _ALL, db=db, endpoint="facts:list")
    known = parse_as_of(known_as_of, "known_as_of")
    moment = parse_as_of(as_of)
    # Counted once the request is known to be one it answers.
    count_the_read(db, key)
    project = key_project(db, key)
    rows, total = agent_facts.retrieve_agent_facts(
        db, key.tenant_id,
        end_user_id=eu,
        agent_id=agent_id,
        subject=subject,
        entity=entity,
        predicate=predicate,
        predicate_family=predicate_family,
        include_invalidated=include_invalidated,
        as_of=moment,
        known_as_of=known,
        project_id=project,
        limit=limit,
        offset=offset,
        exclude_sessions=exclude_sessions,
    )
    log_v1_request(db, key.tenant_id, "facts:list", api_key_id=key.id,
                   end_user_id=user_id, facts_read=[f.id for f in rows],
                   meta=moments_asked(as_of, known_as_of))
    # Which facts a reader that named itself was handed as true (agent
    # notices): nothing without `reader`, nor for a read of the past.
    read_marks.mark(key.tenant_id, project, reader, [f.id for f in rows], as_of=moment,
                    known_as_of=known, include_invalidated=include_invalidated)
    return FactsResponse(facts=[agent_facts.serialize_fact(f, known) for f in rows], total=total)


_MODEL_UNAVAILABLE = (
    "`model_unavailable`: a model the write needs (the one that resolves relations, or the "
    "contradiction check) gave no usable answer. Nothing was written; retry. A new fact whose "
    "own embedding fails is written anyway, and its vector is computed later by the worker. "
    + SERVICE_PAUSED + " Or " + EMBEDDER_BUSY
)


def model_unavailable(db: Session) -> HTTPException:
    """The 503 of a fact write a model gave no usable answer for
    (agent_facts.MODEL_FAILURES). Nothing was written: the session is rolled
    back here, before the answer.

    It used to cover only the vocabulary, and only on POST. A contradiction
    check that failed was read as "nothing contradicts" and the fact was
    written next to the one it may close; on PATCH the vocabulary's refusal
    was a 500 (2026-09-28, the same 503).

    When the service's own gate kept the call from going out (the Cloud's
    daily stop, a brake) it is `service_paused` with a `Retry-After`: a retry
    cannot succeed until the stop lifts (audit of 27 Oct 2026). When this
    machine's embedder refused the relation's vector (busy, or loading its
    model at the start) it is `unavailable` with a `Retry-After` of seconds
    (since 1.0.27): nothing failed (`errors.not_a_model_that_failed`)."""
    db.rollback()
    kept_back = not_a_model_that_failed()
    if kept_back is not None:
        return kept_back
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "model_unavailable",
                "message": "A model the write needs did not give a usable answer. Nothing was "
                           "written; retry the request."},
    )


@router.post("/facts", response_model=FactWriteResponse, status_code=status.HTTP_201_CREATED,
             responses=v1_errors(503, descriptions={503: _MODEL_UNAVAILABLE}))
def create_fact(
    body: FactBody,
    key: AgentApiKey = Depends(_write_key),
    db: Session = Depends(get_db),
) -> dict:
    """Write a fact directly: a subject, a relation and an object, with no
    memory behind it and no extraction. Whose memory it goes to is required,
    as for a memory: a `user_id` or an `agent_id` (422 `identity_required`
    with neither, since 1.0.19).

    It is not a write without a model. With a model key configured, the
    relation goes through the store's vocabulary (a model may be asked whether
    a new relation is one already known, and whether it holds one value at a
    time), and a `current` fact goes through the contradiction check: when
    open facts of the same subject could conflict with it, a model judges
    them and may close one, named in `invalidated`. The fact's words reach
    that model's provider and the calls are billed as any other. When one of
    those models gives no usable answer nothing is written: 503
    `model_unavailable`, retry.

    With no model key, no model is asked and no judge closes anything. A
    `past` fact closes the open fact it restates either way.

    To end a fact yourself, with the date it stopped being true, use
    `POST /v1/facts/{fact_id}/forget`, which asks no model.
    """
    # The gate of every write (services/memory_write.py): the ids by the
    # memory's rules (a NUL byte in run_id was a 500 here, audit of 17 Oct
    # 2026), the end user inside a confined key's list (403), then the hosted
    # service's cap on agent names and its monthly write quota; on an install
    # of one's own both hooks do nothing. A fact written directly is one
    # write: the judge reads it.
    try:
        user = memory_write.checked_ids(user_id=body.user_id, agent_id=body.agent_id,
                                        run_id=body.run_id, end_users=key_end_users(key),
                                        identity=True)
    except OutOfScopeRefusal as e:
        # Another person's memory, for a confined key: in the trail.
        raise refused(db, key, "facts:create", e, end_user_id=e.user_id) from None
    memory_write.admit(db, key.tenant_id, [""], [body.agent_id], endpoint="facts:create",
                       project_id=key_project(db, key), api_key_id=key.id, end_user_id=user)
    # Named after the field that was sent: the parser said "as_of" whichever
    # field was wrong, so a bad `valid_from` sent the caller looking for a
    # parameter they had not used (2026-09-28).
    valid_from = parse_as_of(body.valid_from, "valid_from") if body.valid_from else None
    try:
        result = _write_direct(db, key, body, valid_from, user)
    except agent_facts.MODEL_FAILURES:
        # The vocabulary could not ask whether this relation is one it already
        # knows, or the contradiction check gave no verdict. Writing anyway
        # would bind the phrase to the wrong relation forever, or keep open a
        # fact the new one may close; refusing is recoverable, the client
        # retries. (The new fact's own vector is not a reason: it is retried
        # by the worker, services/vectors.py.)
        raise refused(db, key, "facts:create", model_unavailable(db), end_user_id=user)
    log_v1_request(db, key.tenant_id, "facts:create", api_key_id=key.id,
                   end_user_id=user, target_id=_fact_id_of(result))
    return result


def _write_direct(db: Session, key: AgentApiKey, body: "FactBody", valid_from, user):
    return agent_facts.write_fact_direct(
        db, key.tenant_id,
        subject=body.subject.strip(),
        predicate=body.predicate.strip(),
        obj=body.object.strip(),
        end_user_id=user,
        agent_id=body.agent_id,
        run_id=body.run_id,
        subject_type=body.subject_type,
        object_is_literal=body.object_is_literal,
        confidence=body.confidence,
        valid_from=valid_from,
        project_id=key_project(db, key),
        tense=body.tense,
    )


# The docstrings of these routes are their descriptions in /docs. This one
# called closing facts by hand "the configuration with no model in the write
# path at all", and the write route had none, while POST /v1/facts asks the
# model whenever a key is configured (audit of 17 Oct 2026).
@router.post("/facts/{fact_id}/forget", response_model=ForgetResponse,
             responses=v1_errors(404))
def forget_fact(
    fact_id: str,
    body: Optional[ForgetBody] = None,
    key: AgentApiKey = Depends(_write_key),
    db: Session = Depends(get_db),
) -> dict:
    """Close a fact: it stops being current, and stays in history.

    The half of the contract that was missing. Facts could be read and written
    over `/v1` and only closed from the dashboard, so an agent could assert
    what it had learned and never retract it. Everything it got wrong stayed
    true forever unless a model happened to notice the contradiction.

    With this, an agent that knows a fact is finished says so, and nothing has
    to infer it. This call asks no model. Writing a fact is another matter:
    `POST /v1/facts` asks the model for the vocabulary and the contradiction
    check whenever a model key is configured, and that judge may close an
    earlier fact on its own. Only an install with no model key writes facts
    with no model.

    `at` is the date it stopped being true, not the date you noticed. Reading
    `as_of` a date before it still returns the fact, which is the whole point of
    keeping history instead of deleting rows.

    Idempotent: forgetting a fact that is already closed changes nothing and
    says so.
    """
    from ..services.ids import parse_fact_id

    fid = parse_fact_id(fact_id)
    # Each refusal in the trail as `error` (api/errors.refused): the fact
    # asked for, never another end user's name for a confined key.
    if fid is None:
        raise refused(db, key, "facts:forget", no_such_fact())
    if not _visible(db, key, fid):
        raise refused(db, key, "facts:forget", no_such_fact(), target_id=fact_public_id(fid))
    try:
        out = agent_facts.forget_fact(
            db, key.tenant_id, fid,
            project_id=key_project(db, key),
            at=parse_as_of(body.at, "at") if (body and body.at) else None,
        )
    except agent_facts.ClosedBeforeItBegan as e:
        raise refused(db, key, "facts:forget", HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_request", "message": str(e)}),
            end_user_id=whose(db, fid), target_id=fact_public_id(fid))
    if out is None:
        # Not found and belongs-to-another-project share one answer, the same
        # way reading a stranger's memory id does: telling them apart tells
        # whoever is probing which ids are real.
        raise refused(db, key, "facts:forget", no_such_fact(), target_id=fact_public_id(fid))
    # The status in the trail, as the MCP tool keeps it: ids and labels only.
    log_v1_request(db, key.tenant_id, "facts:forget", api_key_id=key.id,
                   end_user_id=whose(db, fid), target_id=fact_public_id(fid),
                   meta={"status": out["status"]})
    return out


@router.patch("/facts/{fact_id}", response_model=FactWriteResponse,
              responses=v1_errors(404, 409, 503, descriptions={
                  409: "`fact_not_current`: the fact is history (superseded, forgotten or ended); "
                       "`current_fact_id` is the fact in force to correct instead, null when "
                       "nothing took the fact's place, and the message says the same in words.",
                  503: _MODEL_UNAVAILABLE}))
def correct_fact(
    fact_id: str,
    body: CorrectBody,
    key: AgentApiKey = Depends(_write_key),
    db: Session = Depends(get_db),
) -> dict:
    """Supersede a fact with a corrected one. Both stay readable.

    Not a mutation: the old row keeps its dates and gains an `invalidated_by`
    pointing at the new one, so `as_of` before the correction still returns what
    you believed then. A store that edits a row in place cannot answer "what did
    we think in June", and answering that is the product.

    The corrected fact is your statement, as a fact you post is: it names no
    memory (`source_memory_id` null), so editing or forgetting the memory of
    the fact it corrected leaves it true (since 1.0.27). It is made of that
    fact's words, so erasing that memory (`erase=true`) deletes it, as
    before. A value a memory already states as true is that memory's row,
    said again.
    """
    from ..services.ids import parse_fact_id

    fields = agent_facts.correction_fields(body.subject, body.predicate, body.object)
    if fields is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_request",
             "message": "Give at least one of subject, predicate or object."})
    fid = parse_fact_id(fact_id)
    if fid is None:
        raise refused(db, key, "facts:correct", no_such_fact())
    if not _visible(db, key, fid):
        raise refused(db, key, "facts:correct", no_such_fact(), target_id=fact_public_id(fid))
    # A correction is a write for the hosted quota, counted when it commits
    # (a 404, 409 or 503 rolls back and costs nothing).
    memory_write.before_write(db, key.tenant_id, [""], endpoint="facts:correct",
                              api_key_id=key.id, target_id=fact_public_id(fid))
    # The write shape, whose `invalidated` names the fact(s) this correction
    # superseded, as POST /v1/facts does (it answered `[]` until 2026-09-28).
    try:
        out = agent_facts.correct_fact(db, key.tenant_id, fid, **fields,
                                       project_id=key_project(db, key), write_shape=True)
    except agent_facts.FactNotCurrent as e:
        # Only the truth as it stands is corrected (7 Oct 2026): a correction
        # of history wrote the new value and closed the fact that holds now.
        # `current_fact_id`: the fact to correct instead, for a client that
        # retries on it without reading the message (null when nothing
        # superseded it), the same field as the hosted product's.
        raise refused(db, key, "facts:correct", not_current_refusal(e),
                      end_user_id=whose(db, fid), target_id=fact_public_id(fid))
    except agent_facts.MODEL_FAILURES:
        raise refused(db, key, "facts:correct", model_unavailable(db),
                      end_user_id=whose(db, fid), target_id=fact_public_id(fid))
    if out is None:
        raise refused(db, key, "facts:correct", no_such_fact(), target_id=fact_public_id(fid))
    # How many facts the correction closed, as the MCP tool keeps it.
    log_v1_request(db, key.tenant_id, "facts:correct", api_key_id=key.id,
                   end_user_id=whose(db, fid), target_id=fact_public_id(fid),
                   meta={"facts_closed": len(out.get("invalidated") or [])})
    return out


def not_current_refusal(e) -> HTTPException:
    """The 409 of a refused correction, one shape on /v1 and on /admin:
    `fact_not_current`, its words, and `current_fact_id`. /admin answered a
    sentence with the code `conflict` until the audit of 17 Oct 2026, so a
    client written for the /v1 refusal could not read the screen's."""
    return HTTPException(status.HTTP_409_CONFLICT, {
        "code": "fact_not_current",
        "message": not_current_message(e),
        "current_fact_id": fact_public_id(e.current_id) if e.current_id else None})


def not_current_message(e) -> str:
    """The words of a refused correction, the hosted product's: which fact to
    correct instead, or how to state the value that holds now."""
    return ("This fact is no longer current, so it cannot be corrected." +
            (f" It was superseded by {fact_public_id(e.current_id)}: correct that one instead."
             if e.current_id else " Write the value that holds now with POST /v1/facts."))


def no_such_fact() -> HTTPException:
    """The 404 of a fact this key cannot reach: unknown, another project's or,
    for a confined key, another end user's, one answer for all three."""
    return HTTPException(status.HTTP_404_NOT_FOUND,
                         {"code": "not_found", "message": "No fact with that id."})


def _visible(db: Session, key, fid) -> bool:
    """Whether a confined key may act on this fact (services/user_scope.py):
    another end user's fact is not found, as another project's is. Every
    whole-project key sees it, and the project's wall is the service's."""
    return key_end_users(key) is None or sees_row(key, whose(db, fid))


def whose(db: Session, fid) -> Optional[str]:
    """The end user of a fact this key just acted on, for its audit row. Asked
    after the action, which already proved the fact is in the key's project."""
    from ..db import AgentFact

    return db.query(AgentFact.end_user_id).filter(AgentFact.id == fid).scalar()


def _fact_id_of(written) -> Optional[str]:
    """The public id of the fact a direct write produced (the write shape)."""
    return (written.get("id") if isinstance(written, dict)
            else getattr(written, "id", None))


class NameBody(Body):
    name: str = Field(min_length=1, max_length=limits.MAX_FACT_NAME_CHARS,
                      description="Another name of the end user, as the memories write it: "
                                  "a full name (\"Maria Rossi\") for the id `maria`.")
    agent_id: Optional[NamespaceId] = Field(
        default=None, description="The agent namespace whose memory the name is declared in; "
                                  "none for the end user's own memory.")


class NameResponse(BaseModel):
    user_id: str
    name: str
    agent_id: Optional[str] = None
    since: Optional[datetime] = None


@router.post("/users/{end_user:path}/names", response_model=NameResponse,
             status_code=status.HTTP_201_CREATED,
             responses=v1_errors(409, descriptions={409: (
                 "`conflict`: the name is already declared for somebody else in this "
                 "end user's memory (the same name declared again for the same end user "
                 "answers 201, as the first time); `too_many_names`: the account has "
                 "declared the most names it may have.")}))
def declare_name(
    end_user: str,
    body: NameBody,
    key: AgentApiKey = Depends(_write_key),
    db: Session = Depends(get_db),
) -> dict:
    """Declare that this end user is also called `name`.

    The store never merges two names of a person by itself: two people can
    share a name, and a merge it guessed wrong would close one person's facts
    with another's. This is the declaration that does it. From now on a fact
    written about `name` in this end user's memory is a fact about the end
    user, and the facts already written about `name` are read so at once
    (their written words stay as they were). It is the alias the dashboard
    makes (`POST /admin/aliases`), for the key's project and this end user.

    `end_user` is the `user_id` exactly as written, percent-encoded as one
    path segment. Declaring the same name again answers the same. 409 when
    `name` is already declared for somebody else in this memory, 422 when it
    is not a name.

    There is no undo here: `DELETE /admin/aliases/{id}` removes the alias,
    and the facts take back the names their words give; two open facts it
    merged stay one, and a plan it closed stays closed."""
    from ..services.facts import entities as _ent

    from ..services.memory_core import store as _store
    from ..services.memory_core.identity import canonical_of, name_key

    try:
        user = memory_write.checked_ids(user_id=end_user, agent_id=body.agent_id, run_id=None,
                                        end_users=key_end_users(key))
    except OutOfScopeRefusal as e:
        # Another person's memory, for a confined key: in the trail.
        raise refused(db, key, "names:declare", e, end_user_id=e.user_id) from None
    project = key_project(db, key)
    # A name declared for somebody else in this memory stays theirs: the
    # dashboard's alias may move it (the latest decision by hand wins there),
    # an end user's key may not.
    alias_map = _store.memory_alias_map(db, _ent.TABLES, _ent.memory_of(key.tenant_id, project, user,
                                                                    body.agent_id))
    taken = HTTPException(status.HTTP_409_CONFLICT,
                          "this name is already declared for somebody else in this memory")
    if name_key(body.name) in alias_map and \
            name_key(canonical_of(body.name.strip(), alias_map)) != name_key(user):
        raise refused(db, key, "names:declare", taken, end_user_id=user)
    # A name declared re-embeds every fact that names it, so it is a write
    # for the hosted quota, counted when it commits (audit of 27 Oct 2026).
    # A refusal of the
    # quota is in the trail as every other door's (memory_write.before_write).
    memory_write.before_write(db, key.tenant_id, [""], endpoint="names:declare",
                              api_key_id=key.id, end_user_id=user)
    try:
        row, outcome = _ent.declare_alias(db, key.tenant_id, project, user, body.agent_id,
                                         body.name, user)
    except _ent.TooManyNames as e:
        raise refused(db, key, "names:declare",
                      CodedHTTPException(status.HTTP_409_CONFLICT, str(e), code="too_many_names"),
                      end_user_id=user)
    if outcome == "conflict":
        raise refused(db, key, "names:declare", taken, end_user_id=user)
    if outcome == "invalid" or row is None:
        raise refused(db, key, "names:declare", HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "name must be a name, and not the user_id itself"), end_user_id=user)
    db.commit()
    db.refresh(row)
    # The alias it made, by its id, as POST /admin/aliases names its own
    # (audit of 19 Oct 2026: the row named no target).
    log_v1_request(db, key.tenant_id, "names:declare", api_key_id=key.id, end_user_id=user,
                   target_id=str(row.id))
    return {"user_id": user, "name": row.alias_label, "agent_id": row.agent_id, "since": row.since}


@router.get("/profile", response_model=ProfileResponse)
def get_profile(
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
    _only_its_own: None = Depends(its_parameters_only),
    user_id: str = Query(..., min_length=1, max_length=limits.MAX_ID_CHARS, description="End user to build the profile for."),
    agent_id: Optional[str] = Query(default=None, description="Filter to one agent namespace."),
    as_of: Optional[str] = Query(default=None, description="ISO date/datetime: the profile as it was on "
                                 f"that date, {limits.MOMENTS_STORED}; outside it, or unreadable, a 422."),
    known_as_of: Optional[str] = Query(default=None, description=_KNOWN_AS_OF),
    exclude_sessions: bool = Query(
        default=False,
        description="Leave out the facts only work sessions state (memories written with "
                    "`kind: session`, what a coding assistant's hooks save). Default false."),
) -> ProfileResponse:
    """The assembled profile of one end user: the active typed facts known about
    them, ``self``-facts first, grouped by predicate family. Deterministic (no
    model call). ``user_id`` is required: a profile is per-end-user (this is
    what distinguishes it from the flat ``GET /v1/facts`` list). ``as_of`` gives
    the point-in-time profile ("what was true on 2026-03-01"), ``known_as_of``
    the profile as the store knew it then ("what we knew on 2026-03-01")."""
    # The ids by every door's rule: a NUL byte in either was a 500 (audit of
    # 18 Oct 2026).
    problem = names_problem(user_id=user_id, agent_id=agent_id)
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
    known = parse_as_of(known_as_of, "known_as_of")
    moment = parse_as_of(as_of)
    reading(key, user_id, db=db, endpoint="profile")  # a confined key builds its own end users' profiles only (403)
    count_the_read(db, key)  # once the request is one it answers
    rows, total = agent_facts.retrieve_agent_facts(
        db, key.tenant_id,
        end_user_id=user_id,
        agent_id=agent_id,
        as_of=moment,
        known_as_of=known,
        project_id=key_project(db, key),
        limit=200,
        offset=0,
        exclude_sessions=exclude_sessions,
    )
    # The end user's OWN facts first: typed `self`, or whose subject IS this
    # end user (extraction labels "<user> lives_in Berlin" as a person, but
    # its subject equals the user_id). The rule of the context's profile,
    # read on the rows as it reads them (`is_about_the_end_user`): it was
    # written a second time here, on the serialized subject. Stable, so the
    # valid_from-desc order holds within each bucket.
    from ..services.context import is_about_the_end_user

    rows = sorted(rows, key=lambda f: 0 if is_about_the_end_user(f, user_id) else 1)
    serial = [agent_facts.serialize_fact(f, known) for f in rows]
    by_family: dict[str, list[dict]] = {}
    for s in serial:
        by_family.setdefault(s.get("predicate_family") or "other", []).append(s)
    log_v1_request(db, key.tenant_id, "profile", api_key_id=key.id,
                   end_user_id=user_id, facts_read=[f.id for f in rows],
                   meta=moments_asked(as_of, known_as_of))
    return ProfileResponse(
        user_id=user_id, as_of=as_of, known_as_of=known_as_of, facts=serial, by_family=by_family,
        total=total, truncated=total > len(serial),
    )
