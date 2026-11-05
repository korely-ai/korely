"""/v1/context API (Sprint 18 C) — one-call prompt-ready context block.

GET /v1/context?query=...&user_id=...&token_budget=800 → assembles the end
user's known facts + the memories most relevant to the query into a Markdown
string under a token budget. Deterministic retrieval + formatting, no
generation. Drop the returned `context` straight into a system prompt, or
(2026-10-01) `stable` in the system prompt and `volatile` in the turn, so the
provider's prompt cache reuses the part that does not change.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import (a_turn_for_its_question, count_the_read, enforce_rate_limit,
                         key_project, reading, ready_to_wait, require_read_with_quota)
from ..core.threads import on_a_request_thread
from ..db import get_db, AgentApiKey
from ..services import limits
from ..services.context import (DEFAULT_TOKEN_BUDGET, MAX_TOKEN_BUDGET, MIN_TOKEN_BUDGET,
                                assemble_context)
from ..services.usage import log_v1_request
from .errors import V1_KEY_ROUTE_ERRORS

router = APIRouter(prefix="/v1", tags=["v1"], dependencies=[Depends(enforce_rate_limit)],
                   responses=V1_KEY_ROUTE_ERRORS)

_read_key = require_read_with_quota("memories:read")


class ContextResponse(BaseModel):
    context: str
    tokens: int
    sources: list[str]
    # True when part of the block could not be retrieved the way a healthy
    # request retrieves it. A partial context that looks complete is worse than
    # an error, because the agent reading it has no way to know it is answering
    # from half.
    degraded: bool = False
    # Which part, when `degraded` (2026-09-28, additive): "memories" (the
    # verbatim memories could not be retrieved; the block holds facts only) and
    # "facts" (the question could not be embedded, so the facts are the most
    # recently confirmed ones rather than the most relevant). Until then only
    # the first was reported, and the second fell back to recency in silence.
    # The block closes with a NOTE saying the same to the model (2026-10-01:
    # it opened it until then).
    degraded_parts: list[str] = Field(default_factory=list)
    # The block in two parts (2026-10-01, additive, as the hosted product):
    # `context` == `stable` + a blank line + `volatile`, either side possibly
    # empty. services/context.py draws the layout.
    stable: str = Field(
        default="",
        description="The head of `context` that does not depend on the question: the reader "
                    "note, the same text from one call to the next. Put it in your system "
                    "prompt, where the model provider's prompt cache can reuse it.")
    volatile: str = Field(
        default="",
        description="The rest of `context`: the facts and the memories for this question and, "
                    "when part of the retrieval failed, a closing NOTE. Put it in the turn.")
    stable_hash: str = Field(
        default="",
        description="SHA-256 (hex) of `stable`. The same value means the same prefix, so a "
                    "cached system prompt is still valid.")


@router.get("/context", response_model=ContextResponse)
async def get_context(
    key: AgentApiKey = Depends(_read_key),
    db: Session = Depends(get_db),
    query: str = Query(..., min_length=1, max_length=limits.MAX_QUERY_CHARS),
    user_id: Optional[str] = Query(default=None, max_length=limits.MAX_ID_CHARS, description="End user to build context for."),
    agent_id: Optional[str] = Query(default=None, max_length=limits.MAX_ID_CHARS,
                                    description="One agent's namespace: its facts and its memories only, "
                                                "as every other read with an agent_id."),
    # What the parameter does and its bounds: the measurements behind the
    # recommendation, each with its block and its date, are the README's
    # (audit of 27 Oct 2026: the public spec quoted them as guidance).
    token_budget: int = Query(default=DEFAULT_TOKEN_BUDGET, ge=MIN_TOKEN_BUDGET, le=MAX_TOKEN_BUDGET,
                              description=f"Tokens the block may take, {MIN_TOKEN_BUDGET} to "
                                          f"{MAX_TOKEN_BUDGET}. Default {DEFAULT_TOKEN_BUDGET}; "
                                          "we recommend 4000 for an agent that answers questions "
                                          "about the past. A larger budget lets more facts and "
                                          "memories into the block (above 2000 tokens, more "
                                          "memories, ordered so that near duplicates do not crowd "
                                          "it), and is a larger prompt: keep it low where latency "
                                          "matters, as in a voice agent."),
    known_as_of: Optional[str] = Query(
        default=None,
        description="ISO date/datetime: the block as the store could have built it then: the "
                    "facts it had learned by then, true then as it knew them, and the memories "
                    "it had received by then. Rebuilt with today's ranking; a memory edited "
                    "since shows today's text, one deleted since stays out."),
    exclude_sessions: bool = Query(
        default=False,
        description="Leave out the work sessions (memories written with `kind: session`, what "
                    "a coding assistant's hooks save) and the facts only they state, for a block "
                    "about what was said and decided. Default false: everything."),
) -> ContextResponse:
    """A block for the prompt: what the store knows about the end user, and
    the facts and memories most relevant to `query`, as Markdown within
    `token_budget` tokens.

    No text is generated: retrieval and layout, the question only embedded to
    rank. `context` is
    the whole block; `stable` (the same from one call to the next) and
    `volatile` (this question's part) are its two halves, for a model
    provider's prompt cache. `degraded` is true when part of it could not be
    retrieved, `degraded_parts` says which, and the block then ends with a
    note saying so to the model.
    \f
    Async since 31 Oct 2026, its work on request threads before and after
    it waits on the loop for its turn at the embedder
    (core/auth.a_turn_for_its_question): past the wait the block is built
    without the question's vector, `degraded`, as when the embedder refuses
    it, and a request refused for anything else is refused before it waits.
    """
    from .facts import moments_asked, parse_as_of

    def checked():
        # The ids by the rule of every read (limits.id_problem), as MCP's
        # korely_get_context asks it: `user_id=` built the block of an end user
        # named '' (audit of 17 Oct 2026).
        # The question too (limits.query_problem), as korely_get_context asks
        # it: a blank one or one with a NUL byte was a 200 with an empty block
        # here and a refusal there (audit of 18 Oct 2026).
        problem = (limits.query_problem(query) or limits.id_problem("user_id", user_id)
                   or limits.id_problem("agent_id", agent_id))
        if problem:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
        # One end user, or every one (None), or a confined key's own
        # (services/user_scope.py); 403 for an end user outside its list.
        end_user = reading(key, user_id, None, db=db, endpoint="context")
        known = parse_as_of(known_as_of, "known_as_of")
        # Counted once the request is known to be one it answers.
        count_the_read(db, key)
        return end_user, known, ready_to_wait(db, key)

    def built(end_user, known) -> ContextResponse:
        out = assemble_context(
            db, key.tenant_id,
            query=query,
            end_user_id=end_user,
            agent_id=agent_id,
            token_budget=token_budget,
            project_id=key_project(db, key),
            known_as_of=known,
            exclude_sessions=exclude_sessions,
        )
        # What the agent was handed, by id (bussola 2e): the facts and the
        # memories that made it into the block, whose user it was for. With
        # `as_of` they answer "what did the agent know when it decided".
        log_v1_request(db, key.tenant_id, "context", api_key_id=key.id, end_user_id=user_id,
                       memories_read=[s for s in out["sources"] if s.startswith("mem_")],
                       facts_read=[s for s in out["sources"] if s.startswith("fct_")],
                       meta=moments_asked(None, known_as_of))
        return ContextResponse(**out)

    end_user, known, account = await on_a_request_thread(checked)
    async with a_turn_for_its_question(account):
        return await on_a_request_thread(built, end_user, known)
