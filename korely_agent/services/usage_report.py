"""What the model provider has been asked to do, and what it costs.

The rows come from `agent_llm_usage`, which records tokens and never money. A
rate card is an assumption the operator owns: providers change prices, and
somebody who negotiates a discount or moves to another model must be able to
say so without the past being rewritten. So the tokens are a fact in the
database and the cost is arithmetic done at read time.

When no rate card is configured the report still answers, with the counts and
`cost` left null. A model that appears in the usage and not in the card is
named in `models_without_a_rate` rather than being priced at zero, because a
bill that silently omits a model is worse than one that says it cannot price it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import String, and_, cast, func, literal, not_, or_
from sqlalchemy.orm import Session

from ..core.embedding_model import NAME as LOCAL_MODEL
from ..db import AgentLlmUsage
from . import audit
from .facts.temporal import true_at
from .sentinels import EVERY_PROJECT, within_project


logger = logging.getLogger(__name__)


#: The agents the report lists by name, the ones with the most output tokens;
#: the rest are one `other_agents` entry.
MAX_AGENTS_LISTED = 50


def parse_rates(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """The rate card, or None. A malformed one is None and a loud log line.

    Shape:

        {"currency": "USD",
         "models": {"gemini-3.1-flash-lite": {"input_per_million": 0.25,
                                              "output_per_million": 1.50}}}
    """
    if not raw or not raw.strip():
        return None
    # Loud: a rate card that silently fails to parse produces a report full
    # of nulls that looks like "nothing was spent". Where the JSON breaks is
    # the parser's line and column, never its message (CONTRIBUTING.md, logs): the
    # one exception the log test allowed here is gone (audit of 17 Oct 2026).
    try:
        card = json.loads(raw)
    except json.JSONDecodeError as e:
        line, column = e.lineno, e.colno
        logger.error("[usage] LLM_RATES is not a valid rate card, costs will be blank: "
                     "not JSON (it breaks at line %d, column %d)", line, column)
        return None
    models = card.get("models") if isinstance(card, dict) else None
    if not isinstance(models, dict):
        logger.error("[usage] LLM_RATES is not a valid rate card, costs will be blank: "
                     "no \"models\" object")
        return None
    return {"currency": str(card.get("currency") or "EUR"), "models": models}


def cost_of(card: Optional[Dict[str, Any]], model: str,
          prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    if not card:
        return None
    if model == LOCAL_MODEL and model not in card["models"]:
        # Computed on this machine (EMBEDDING_PROVIDER=local): no provider bills
        # it, so it is not a model the card forgot. Zero, unless the operator
        # gives it a rate of their own (the CPU is theirs to price).
        return 0.0
    rate = card["models"].get(model)
    if not isinstance(rate, dict):
        return None
    try:
        return round(
            (prompt_tokens / 1_000_000.0) * float(rate.get("input_per_million", 0))
            + (completion_tokens / 1_000_000.0) * float(rate.get("output_per_million", 0)),
            6,
        )
    except (TypeError, ValueError):
        return None


def report(db: Session, tenant_id: UUID, *, days: int = 30,
           rates_raw: Optional[str] = None) -> Dict[str, Any]:
    """Consumption for one account over a window, grouped three ways."""
    days = max(1, min(int(days or 30), 400))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    card = parse_rates(rates_raw)

    # Cost is per model, because that is the only grouping where a rate exists.
    # The per-stage and per-agent costs are then summed from the per-model rows
    # rather than guessed from an average.
    per_model_cost: Dict[str, Optional[float]] = {}
    by_model = []
    unpriced = []

    def _sums():
        return (func.count(AgentLlmUsage.id),
                func.coalesce(func.sum(AgentLlmUsage.prompt_tokens), 0),
                func.coalesce(func.sum(AgentLlmUsage.completion_tokens), 0),
                func.coalesce(func.sum(AgentLlmUsage.cached_tokens), 0))

    # One row per model: the models an install is set to use.
    for model, calls, tin, tout, cached in (
            db.query(AgentLlmUsage.model, *_sums())
              .filter(AgentLlmUsage.tenant_id == tenant_id, AgentLlmUsage.ts >= since)
              .group_by(AgentLlmUsage.model).all()):
        c = cost_of(card, model or "", int(tin), int(tout))
        per_model_cost[model or ""] = c
        if card and c is None:
            unpriced.append(model or "")
        by_model.append({"model": model or "", "calls": int(calls),
                         "prompt_tokens": int(tin), "completion_tokens": int(tout),
                         "cached_tokens": int(cached), "cost": c})

    def _cost_for(rows) -> Optional[float]:
        """Sum only over rows whose model has a rate. Returns None when none has
        one, rather than 0, which would read as "free"."""
        if not card:
            return None
        total = 0.0
        seen = False
        for model, _calls, tin, tout in rows:
            c = cost_of(card, model or "", int(tin), int(tout))
            if c is not None:
                total += c
                seen = True
        return round(total, 6) if seen else None

    def _folded(rows, key_of):
        """Rows of (group, model, calls, in, out, cached) folded into one
        entry per group, its cost the sum over its models' real rates."""
        folded: Dict[Any, Dict[str, Any]] = {}
        for row in rows:
            key, model, calls, tin, tout, cached = row
            slot = folded.setdefault(key_of(key), {"calls": 0, "prompt_tokens": 0,
                                                   "completion_tokens": 0, "cached_tokens": 0,
                                                   "models": []})
            slot["calls"] += int(calls)
            slot["prompt_tokens"] += int(tin)
            slot["completion_tokens"] += int(tout)
            slot["cached_tokens"] += int(cached)
            slot["models"].append((model, None, int(tin), int(tout)))
        for slot in folded.values():
            slot["cost"] = _cost_for(slot.pop("models"))
        return folded

    def _grouped_with_cost(column, label, *, top: Optional[int] = None):
        """One entry per value of `column`, by one query that groups by the
        column and the model (the split that prices it). With `top`, only the
        `top` values with the most completion tokens: a column a key holder
        chooses (the agent id) has no bound of its own. Returns (entries, the
        rest folded into one entry or None, how many values it holds)."""
        window = and_(AgentLlmUsage.tenant_id == tenant_id, AgentLlmUsage.ts >= since)
        wanted, named = window, []
        if top is not None:
            named = [k for (k,) in (
                db.query(column).filter(window, column.isnot(None)).group_by(column)
                  .order_by(func.sum(AgentLlmUsage.completion_tokens).desc(), column.asc())
                  .limit(top).all())]
            # The calls with no value are always listed by name (null), never
            # folded into the rest, whose count of agents would leave them out.
            wanted = and_(window, or_(column.in_(named), column.is_(None)))
        rows = (db.query(column, AgentLlmUsage.model, *_sums())
                  .filter(wanted).group_by(column, AgentLlmUsage.model).all())
        out = [{label: key, **slot} for key, slot in _folded(rows, lambda k: k).items()]
        out.sort(key=lambda r: r["completion_tokens"], reverse=True)
        rest, held = None, 0
        if top is not None:
            left = and_(window, column.isnot(None), not_(column.in_(named)))
            held = int(db.query(func.count(func.distinct(column))).filter(left).scalar() or 0)
            if held:
                more = (db.query(literal(None), AgentLlmUsage.model, *_sums())
                          .filter(left).group_by(AgentLlmUsage.model).all())
                rest = next(iter(_folded(more, lambda k: None).values()), None)
        return out, rest, held

    by_stage, _no_rest, _none = _grouped_with_cost(AgentLlmUsage.stage, "stage")
    by_agent, other_agents, other_count = _grouped_with_cost(
        AgentLlmUsage.agent_id, "agent_id", top=MAX_AGENTS_LISTED)

    totals_calls = sum(r["calls"] for r in by_model)
    totals_in = sum(r["prompt_tokens"] for r in by_model)
    totals_out = sum(r["completion_tokens"] for r in by_model)
    totals_cached = sum(r["cached_tokens"] for r in by_model)
    totals_cost = None
    if card:
        priced = [r["cost"] for r in by_model if r["cost"] is not None]
        totals_cost = round(sum(priced), 6) if priced else None

    # An agent_id is optional on a write, and the per-agent bill is the reason
    # this exists. Saying how many calls arrived without one is the difference
    # between an empty breakdown and a wrong one.
    unattributed = int(db.query(func.count(AgentLlmUsage.id))
                         .filter(AgentLlmUsage.tenant_id == tenant_id, AgentLlmUsage.ts >= since,
                                 or_(AgentLlmUsage.agent_id.is_(None), AgentLlmUsage.agent_id == ""))
                         .scalar() or 0)

    return {
        "since": since.isoformat(),
        "days": days,
        "currency": card["currency"] if card else None,
        "rates_configured": bool(card),
        "models_without_a_rate": sorted(set(unpriced)),
        "totals": {"calls": totals_calls, "prompt_tokens": totals_in,
                   "completion_tokens": totals_out, "cached_tokens": totals_cached,
                   "cost": totals_cost},
        "by_stage": by_stage,
        "by_agent": by_agent,
        # The agents past the `MAX_AGENTS_LISTED` with the most output,
        # folded into one entry (null when there are none): the names are a
        # string a write key chooses, so the list has a bound of its own.
        "other_agents": ({"agents": other_count, **other_agents} if other_agents else None),
        "by_model": sorted(by_model, key=lambda r: r["completion_tokens"], reverse=True),
        "calls_without_an_agent_id": unattributed,
    }


# ── request activity, off the audit trail ───────────────────────────────────
#
# The dashboard's Requests page. The audit trail already records every call, so
# this is a grouping of it rather than a second counter that could disagree
# with the first.

# What the page counts, by verb: the calls that read the memory and the ones
# that change it, whoever made them, but the screen's own reads
# (`is_a_request`). The administration (a key made, moved or
# revoked, a tenant, a setting, a login) is the Audit log's and not a request.
# Until 2026-10-06 every verb that was not a write counted as a read, and on
# the day the trail began recording the administration a login showed on the
# Home page as a retrieval event. Every verb is in one of the three sets
# (tests/test_requests_are_calls_on_the_memory.py): a new one makes somebody
# decide where it goes.
_READ_ACTIONS = {audit.READ}
_WRITE_ACTIONS = {audit.WRITE, audit.FACT_WRITE, audit.FACT_INVALIDATE, audit.ERASE, audit.FORGET}
ADMINISTRATION = {audit.KEY_CREATE, audit.KEY_REVOKE, audit.TENANT_CREATE,
                  audit.KEY_CHANGE, audit.SETTING_CHANGE, audit.LOGIN, audit.LOGOUT}
REQUEST_ACTIONS = _READ_ACTIONS | _WRITE_ACTIONS

# Who the screen is in the trail (services/usage._by): an operator signed in
# to the dashboard. Its reads are the operator looking at the memory, kept in
# the Audit log as every read of personal data is, and they are not a
# request: the plan does not count them (the doors of /v1 and the MCP do,
# core/auth.count_the_read), and counted here each page opened grew the
# numbers, so an account nobody's agent had called showed 12 reads, and the
# Home page and the Requests page, read a few clicks apart, never agreed
# (QA of 14 Nov 2026: 0 against 12 on an empty account, 519 against 531).
_THE_SCREEN = "dashboard"


def calls():
    """How many calls the rows of a group are, as one SQL count: the rows of
    one call share `meta.call` (services/usage.log_v1_call: a list of the
    screen filed once per end user it showed), every other row is a call of
    its own. An admin key's list of 50 end users counted 50 reads (review
    of 14 Nov 2026)."""
    from ..db import AuditEvent

    return func.count(func.distinct(func.coalesce(AuditEvent.meta["call"].as_string(),
                                                  cast(AuditEvent.id, String))))


def is_a_request():
    """The rows of the trail that are requests, as one SQL condition: a verb
    on the memory, except a read by the screen. Every view of the requests
    (the summary, the timeline, the list) reads through it."""
    from ..db import AuditEvent

    return and_(AuditEvent.action.in_(REQUEST_ACTIONS),
                not_(and_(AuditEvent.action == audit.READ, AuditEvent.actor == _THE_SCREEN)))


def _window_start(days: int) -> datetime:
    """Where a window of `days` days begins: midnight UTC of the first of
    them, today counted. The summary began `days` days back to the second
    and the timeline at midnight, so the total and the chart beside it could
    differ by most of a day's calls."""
    return (datetime.now(timezone.utc) - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)


def kind_of(action: Optional[str]) -> str:
    """`write` or `read`: the one classification of a call, for the summary,
    the timeline and every row the screen lists (audit of 17 Oct 2026: the
    page classified rows by the endpoint's suffix and showed a fact
    forgotten as a read, next to a summary that counted it as a write)."""
    return "write" if (action or "") in _WRITE_ACTIONS else "read"


def requests_summary(db: Session, tenant_id: UUID, *, days: int = 30,
                     project_id=EVERY_PROJECT) -> Dict[str, Any]:
    """Reads, writes, and which endpoints they went to, in one project
    (`project_id`, as `sentinels.within_project` reads it) or the whole
    account.

    Shaped for the screen that reads it: `{by_endpoint, writes, reads, total}`.
    It used to return `{by_action, refused}`, which is a perfectly good shape
    that nothing consumed, and the page crashed on the fields that were not
    there.
    """
    from ..db import AuditEvent

    days = max(1, min(int(days or 30), 400))
    since = _window_start(days)

    # Counted by the database, by verb, outcome and endpoint: a row per group,
    # never per call. Every row of the window came back and was counted
    # here, some 0.8 KiB each in the one API process, with a trail kept 400
    # days (audit of 20 Oct 2026); requests_daily already grouped.
    endpoint = AuditEvent.meta["endpoint"].as_string()
    refused_here = AuditEvent.result != "ok"
    rows = (within_project(
                db.query(AuditEvent.action, refused_here, endpoint, calls())
                  .filter(AuditEvent.tenant_id == tenant_id, AuditEvent.ts >= since,
                          is_a_request()),
                AuditEvent.project_id, project_id)
              .group_by(AuditEvent.action, refused_here, endpoint)
              .all())

    writes = reads = refused = 0
    per_endpoint: Dict[str, Dict[str, Any]] = {}
    for action, was_refused, named, n in rows:
        n = int(n)
        is_write = kind_of(action) == "write"
        if is_write:
            writes += n
        else:
            reads += n
        if was_refused:
            refused += n
        name = named or (action or "unknown")
        slot = per_endpoint.setdefault(
            name, {"endpoint": name, "count": 0, "kind": "write" if is_write else "read"})
        slot["count"] += n

    return {
        "since": since.isoformat(), "days": days,
        "total": writes + reads, "writes": writes, "reads": reads,
        # Anything that was refused. A screen that only shows successes cannot
        # show the afternoon somebody's key was wrong.
        "refused": refused,
        "by_endpoint": sorted(per_endpoint.values(), key=lambda r: (-r["count"], r["endpoint"]))[:20],
    }


def requests_daily(db: Session, tenant_id: UUID, *, days: int = 30,
                   project_id=EVERY_PROJECT) -> Dict[str, Any]:
    """One row per day, zero-filled, split into writes and reads, in one
    project or the whole account, as `requests_summary`.

    A chart with holes in it reads as an outage, so the quiet days say zero
    rather than going missing.
    """
    from ..db import AuditEvent

    days = max(1, min(int(days or 30), 400))
    since = _window_start(days)

    # The day in UTC, the one the series below is filled in: `date(ts)` alone
    # is the date in the database session's time zone, and on a Postgres that
    # is not set to UTC the last hours of each day fell on the next date and
    # today's calls were missing from the chart (audit of 27 Oct 2026).
    day = func.date(func.timezone("UTC", AuditEvent.ts))
    rows = (within_project(
                db.query(day, AuditEvent.action, calls())
                  .filter(AuditEvent.tenant_id == tenant_id, AuditEvent.ts >= since,
                          is_a_request()),
                AuditEvent.project_id, project_id)
              .group_by(day, AuditEvent.action)
              .all())

    buckets: Dict[str, Dict[str, int]] = {}
    for d, action, n in rows:
        slot = buckets.setdefault(str(d), {"writes": 0, "reads": 0})
        if kind_of(action) == "write":
            slot["writes"] += int(n)
        else:
            slot["reads"] += int(n)

    series = []
    for i in range(days):
        d = (since + timedelta(days=i)).date().isoformat()
        got = buckets.get(d, {"writes": 0, "reads": 0})
        series.append({"date": d, "writes": got["writes"], "reads": got["reads"]})
    # `buckets`, singular, because that is the name the screen reads. It also
    # carried `series` for a while: two names for one list is how a field comes
    # to disagree with itself, and it broke a check that counted dates.
    return {"since": since.isoformat(), "days": days, "buckets": series,
            "total_writes": sum(b["writes"] for b in series),
            "total_reads": sum(b["reads"] for b in series)}


def memory_categories(db: Session, tenant_id: UUID,
                      project_id=EVERY_PROJECT) -> List[Dict[str, Any]]:
    """A category is a predicate family, and its count is how many memories
    produced a live fact in it, in one project or the whole account, as
    `requests_summary`.

    Mapped in Python, not in SQL: a fact's family is the one stored on it
    when its relation was classified, or the static map's for a fact that
    has none (`families.family_of_the_fact`), and a CASE with the whole map in
    the query would make the vocabulary a schema migration. So Python maps
    the distinct (stored family, relation) pairs, as many as the vocabulary
    has, and the database counts the memories per family over them. Every
    live fact came back and was mapped here, one row each in the one API
    process (audit of 20 Oct 2026).
    """
    from sqlalchemy import String, column, values

    from ..db import AgentFact
    from .memory_core.families import family_of_the_fact

    live = (AgentFact.tenant_id == tenant_id, true_at(), AgentFact.source_memory_id.isnot(None))
    # The family stored on the fact wins, as on every page that shows one
    # (`families.family_of_the_fact`): with the static map alone, a learned
    # relation was "other" here and "preferences" in the Facts page (the
    # audit of 2026-10-06, row 11).
    pairs = (within_project(db.query(AgentFact.predicate_family, AgentFact.predicate_canonical)
                              .filter(*live), AgentFact.project_id, project_id)
               .distinct().all())
    if not pairs:
        return []
    families = values(column("stored", String), column("predicate", String),
                      column("family", String), name="families").data(
        [(stored, predicate, family_of_the_fact(stored, predicate) or "other")
         for stored, predicate in pairs])
    counted = (within_project(
                   db.query(families.c.family, func.count(func.distinct(AgentFact.source_memory_id)))
                     .select_from(AgentFact)
                     .join(families, and_(
                         AgentFact.predicate_family.is_not_distinct_from(families.c.stored),
                         AgentFact.predicate_canonical == families.c.predicate))
                     .filter(*live),
                   AgentFact.project_id, project_id)
                 .group_by(families.c.family)
                 .all())
    seen = {fam: int(n) for fam, n in counted}
    # `family` and `count` are what the screen reads; `category` and `memories`
    # were what this returned, which is the same information under names
    # nothing was looking for.
    return sorted(
        ({"family": fam, "count": n, "category": fam, "memories": n}
         for fam, n in seen.items()),
        key=lambda r: (-r["count"], r["family"]),
    )
