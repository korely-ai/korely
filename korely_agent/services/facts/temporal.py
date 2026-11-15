"""What it means for a fact to be true, at a moment.

Its own module because it has no dependencies and everything depends on it:
putting it next to the code that writes facts made a circle the first time
the contradiction pass needed it.
"""
from __future__ import annotations

from sqlalchemy import and_, case, func, or_

from ...db.models import AgentFact


def true_at(when=None, fact=None):
    """The condition for "this fact is true", at a moment.

    One expression, because there were twelve and one of them was different.
    `as_of` asked the right question — began before then, and had not ended by
    then — while every read that meant "now" asked a narrower one: has it no end
    date at all. The two agreed for as long as an end date could only be in the
    past, and stopped agreeing the day `forget` learned to take a date.

    A tester closed a fact in the year 3026 and got two answers to the same
    question on the same day: the default read said it was gone, `as_of` today
    said it was there. Closing a fact on a future date is the obvious use of
    that parameter — a contract that ends on 31 December, a subscription with a
    known last day — so this is not a laboratory case.

    `when=None` means now, resolved at query time. `fact`: an alias of
    AgentFact the condition is about, when a query reads two rows of the table
    (services/facts/changes.py); the table itself when None.
    """
    from datetime import datetime as _d, timezone as _tz

    F = AgentFact if fact is None else fact
    moment = when or _d.now(_tz.utc)
    return and_(
        or_(F.valid_from.is_(None), F.valid_from <= moment),
        or_(F.invalid_at.is_(None), F.invalid_at > moment),
    )


def not_ended(when=None):
    """The condition for "this fact has not ended yet": no end, or an end
    after `when` (now when None, resolved at query time). Wider than
    `true_at`: a fact dated ahead has not begun, and has not ended either.

    What a retraction asks for, never a read. A memory forgotten or edited
    must end every fact it alone stated, the ones dated ahead included: asked
    "true now", a fact dated ahead (a write's timestamp in the future, or a
    date in the text up to a day ahead, memory_core `fact_time`) was left open
    and came true at its date although its memory no longer said it
    (2026-10-06 for a forget, the audit of 17 Oct 2026 for an edit)."""
    from datetime import datetime as _d, timezone as _tz

    moment = when or _d.now(_tz.utc)
    return or_(AgentFact.invalid_at.is_(None), AgentFact.invalid_at > moment)


def true_now(fact: AgentFact, now) -> bool:
    """Python, one row: not closed at `now`, no end or an end still to come
    (the twin of `not_ended`)."""
    from ...core.times import aware

    return fact.invalid_at is None or aware(fact.invalid_at) > now


# ── what the store knew, at a moment (`known_as_of`, 11 Oct 2026) ─────────
#
# Two clocks, as in Graphiti (Rasmussen et al. 2025, section 2.2.3): when a
# fact was true in the world (`valid_from`, `invalid_at`: `as_of` asks this
# one) and when the store knew it (`created_at`, when it was learned, and
# `closed_known_at`, when its end was learned: `known_as_of` asks this one).
# A move made in September and told in October: `as_of` 20 September answers
# the new address, what was true that day as the store knows it now;
# `known_as_of` 20 September answers the old one, what the store knew that day.

def end_as_known(known):
    """SQL: the fact's end date as the store knew it at `known`: its
    `invalid_at` when that end was learned by then, none otherwise. A closed
    row with no `closed_known_at` (none should be left after the migration)
    reads as closed since it was learned."""
    return case(
        (func.coalesce(AgentFact.closed_known_at, AgentFact.created_at) <= known,
         AgentFact.invalid_at),
        else_=None,
    )


def known_at(known):
    """SQL: the store had learned the fact by `known`."""
    return AgentFact.created_at <= known


def known_true_at(known, when=None):
    """The condition for "this fact is true at `when`, as the store knew it
    at `known`" (`when` defaults to `known`: what the store believed that day
    about that day). Learned by `known`, begun by `when`, and not ended by
    `when` as far as the store knew at `known`."""
    moment = when or known
    end = end_as_known(known)
    return and_(
        known_at(known),
        or_(AgentFact.valid_from.is_(None), AgentFact.valid_from <= moment),
        or_(end.is_(None), end > moment),
    )


def true_then(known=None, when=None):
    """`true_at(when)` when `known` is None, `known_true_at(known, when)`
    otherwise: the one call the reads make."""
    return true_at(when) if known is None else known_true_at(known, when)


def end_known_by(fact, known) -> bool:
    """Python, one row: whether the store knew of the fact's end at `known`
    (the twin of `end_as_known`)."""
    from ...core.times import aware

    if fact.invalid_at is None:
        return False
    learned = fact.closed_known_at or fact.created_at
    return learned is not None and aware(learned) <= known


def parse_moment(s: str):
    """An `as_of` / `valid_from` / `at` string as an aware datetime.

    ISO date (`2026-06-01`, midnight) or datetime, `Z` accepted; a value with no
    offset is UTC. Raises ValueError on anything else.

    One function because REST and the MCP read the same parameter and read it
    differently: the MCP kept a value with no offset naive, and a naive
    datetime compared with a timestamptz column is interpreted in the database
    session's time zone, so `as_of=2026-06-01` could answer for a different
    instant on the two surfaces (2026-09-28, the same fix as the hosted
    product).
    """
    from datetime import date as _date, datetime as _dt

    from ...core.times import aware

    raw = (s or "").strip().replace("Z", "+00:00")
    try:
        dt = _dt.fromisoformat(raw)
    except ValueError:
        d = _date.fromisoformat(raw)  # ValueError propagates: not a date either
        dt = _dt(d.year, d.month, d.day)
    return aware(dt)
