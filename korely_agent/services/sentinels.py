"""Values that mean "no filter", shared so there is only one of each.

There were two. `services/memory_engine.py` and `services/facts/service.py` each
defined `_ALL = object()`, and the two are not interchangeable: passing one
where the other is expected does not raise anything useful. It is not
recognised as the sentinel, so it travels on as an ordinary value and the
database says `can't adapt type 'object'` several layers later.

`mcp/server.py` already imported one of them under an alias to keep the two
apart at the call site, which is a symptom being managed rather than a cause
being removed. This module is the cause being removed.
"""
from __future__ import annotations


class _EveryEndUser:
    """No end-user filter: every one of them.

    A class with a name rather than a bare `object()`, so a traceback that
    reaches it says what it was instead of `<object object at 0x...>`.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "EVERY_END_USER"

    def __bool__(self) -> bool:
        # Never truthy by accident: `if end_user_id:` reads as "was one given",
        # and this is the value that means no.
        return False


EVERY_END_USER = _EveryEndUser()



class EndUsers:
    """Some end users only: the ones a key confined to them may read
    (services/user_scope.py, 16 Oct 2026). Wherever a
    read takes EVERY_END_USER, it takes this too, and filters on the ids and
    prefixes it holds.

    Never truthy, like the value above: `if end_user_id:` asks whether one
    end user was named, and this names none."""

    __slots__ = ("patterns",)

    def __init__(self, patterns):
        self.patterns = tuple(patterns)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EndUsers({list(self.patterns)!r})"

    def __bool__(self) -> bool:
        return False


class _EveryProject:
    """No project filter: every project in this account.

    An API key belongs to exactly one project and sees only that project's
    memories and facts. This is the value for the callers that are not an API
    key: the dashboard an operator logs into, which is meant to see across the
    whole account, and the CLI.

    It is deliberately not spelled `None`. `None` is a real project value
    elsewhere in this schema (a key whose project was deleted), so a caller
    that forgot to pass anything and a caller that meant "no wall" would be
    indistinguishable, and the one that gets it wrong silently is the one that
    removes the wall.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "EVERY_PROJECT"

    def __bool__(self) -> bool:
        return False


EVERY_PROJECT = _EveryProject()


def within_project(q, column, project_id):
    """The project wall, one rule for every read and write: EVERY_PROJECT puts
    up none (the operator's screen, an account-wide job); None is the default
    project, the rows that have no project; anything else is that project.
    Written out in twelve places until 2026-10-06 (the code audit, row 15),
    which is how copies come to disagree."""
    if project_id is EVERY_PROJECT:
        return q
    return q.filter(column.is_(None)) if project_id is None else q.filter(column == project_id)
