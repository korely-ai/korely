"""Keys that see some end users of their project, not all of them.

Added on 16 Oct 2026. A key sees its whole project (core/auth.key_project),
and some must see less: in one project, an operator's own agents read the
notes under `team` and `repo:*` and never the ones under `private`; a
customer's support agent must not read the notes
of the HR agent's users. A key, or an OAuth grant at consent, may carry
`end_users`: exact ids, and prefixes that end in `*` (`repo:*`). No list is
the whole project, as before.

The rules, one for every door (REST, MCP, the context block):

- a read that names a user_id outside the list is refused (403);
- a read that names none sees the listed end users only, and never the rows
  with no end user: the shared memory is everybody's, and a confined key is
  not everybody;
- a write names a user_id inside the list; when the list is a single exact
  id, a write that names none goes to it;
- a row of another end user, asked for by its id, is not found (404), as a
  row of another project is: telling the two apart says which ids exist;
- a confined key never holds `admin`: a key that can mint keys could hand
  itself a wider one (services/keys.py).

A `*` is a prefix, at the end only, matched with LIKE after escaping `%`, `_`
and the escape character, so those are characters of an id, never patterns.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .ids import ERASED_PREFIX, namespace_id_problem
from .sentinels import EndUsers

WILDCARD = "*"
# How many ids and prefixes one key may list. A list longer than this is a
# project of its own.
MAX_ENTRIES = 50


class OutOfScope(PermissionError):
    """A user_id this key may not read or write (None: a write that named
    none, from a key whose list has no single id to default to)."""

    def __init__(self, user_id: Optional[str]):
        self.user_id = user_id
        super().__init__(user_id)


def checked(values) -> Optional[List[str]]:
    """The list as it is stored, or None for a whole-project key. Raises
    ValueError with the reason, in the words the API gives back."""
    if values is None:
        return None
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ValueError("users is a list of user_ids")
    out: List[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("users holds user_ids, as strings")
        value = value.strip()
        if value == WILDCARD:
            raise ValueError("'*' alone is every end user: leave the list out for the whole project")
        literal = value[:-1] if value.endswith(WILDCARD) else value
        if WILDCARD in literal:
            raise ValueError(f"{value!r}: '*' is allowed at the end only")
        problem = namespace_id_problem(literal)
        if problem:
            raise ValueError(f"{value!r}: {problem}")
        if value not in out:
            out.append(value)
    if not out:
        raise ValueError("an empty list would see nobody: leave it out for the whole project")
    if len(out) > MAX_ENTRIES:
        raise ValueError(f"at most {MAX_ENTRIES} user_ids and prefixes")
    return out


def allows(users: Optional[Sequence[str]], user_id: Optional[str]) -> bool:
    """Whether a key with this list may touch `user_id`. No list: any.
    No user_id: only a whole-project key (the shared memory)."""
    if users is None:
        return True
    if user_id is None or user_id.startswith(ERASED_PREFIX):
        # An erased end user's pseudonym is nobody a confined key holds.
        return False
    for pattern in users:
        if pattern.endswith(WILDCARD):
            if user_id.startswith(pattern[:-1]):
                return True
        elif user_id == pattern:
            return True
    return False


def default_user(users: Optional[Sequence[str]]) -> Optional[str]:
    """Where a write that names no user_id goes: the only id of a list that
    holds exactly one exact id; None otherwise."""
    if users is not None and len(users) == 1 and not users[0].endswith(WILDCARD):
        return users[0]
    return None


def for_read(users: Optional[Sequence[str]], user_id: Optional[str], every):
    """What a read filters its end users on: the user_id asked for, checked;
    without one, `every` (the caller's EVERY_END_USER) for a whole-project
    key and EndUsers(the list) for a confined one."""
    if users is None:
        return user_id if user_id is not None else every
    if user_id is not None:
        if not allows(users, user_id):
            raise OutOfScope(user_id)
        return user_id
    return EndUsers(users)


def for_write(users: Optional[Sequence[str]], user_id: Optional[str]) -> Optional[str]:
    """The end user a write goes to: the one named, checked; without one,
    the list's single id, or OutOfScope(None) when it has none."""
    if users is None:
        return user_id
    if user_id is None:
        user_id = default_user(users)
        if user_id is None:
            raise OutOfScope(None)
        return user_id
    if not allows(users, user_id):
        raise OutOfScope(user_id)
    return user_id


def condition(column, users: Sequence[str]):
    """The SQL condition of a confined read on an end-user column."""
    from sqlalchemy import and_, false, or_

    parts = []
    for pattern in users:
        if pattern.endswith(WILDCARD):
            literal = (pattern[:-1].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
            parts.append(column.like(literal + "%", escape="\\"))
        else:
            parts.append(column == pattern)
    if not parts:
        return false()
    # Never the rows an erasure left: their end user is a pseudonym, nobody
    # this key was confined to, whatever a prefix happens to match.
    return and_(or_(*parts), ~column.like(ERASED_PREFIX + "%"))
