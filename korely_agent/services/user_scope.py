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

The end-user wall itself, the filter a read puts on an end-user column, is
written once, here (`within_end_users`, `wall`), as the project's is in
services/sentinels.within_project. Until 6 Nov 2026 it was written out in
seven places that did not agree on what `None` meant (the shared level's
specification, 2.2): the memories' engine and the facts' reads read it as
the rows with no end user, the context block's facts as every end user.
Both meanings stay, and each caller now says which one it means, by name:
EVERY_END_USER for every end user, None for the rows of none.
tests/test_no_read_builds_the_wall_by_hand.py watches the SQL every door of
the leak matrix sends, and fails on a condition on an end-user column built
anywhere else that it does not declare, by the exact condition and where it
is built.

Reads that filter on an end user without this wall, for a reason of their
own, and that a shared level has to change with it (the guard declares
them under T2_MUST_TOUCH):

- the trail by the person asked, the id and its pseudonyms
  (api/audit.list_audit, api/admin.list_audit: pseudonyms.as_asked);
- a fact's names read in the fact's own memory, the `entity` and `subject`
  filters of the facts (facts/entities.name_condition,
  facts/links.names_condition, links' prefetch);
- the names already declared in an end user's memory, read by
  POST /v1/users/{end_user}/names (facts/entities.memory_of);
- the end users that hold rows, listed by GET /v1/users
  (api/memories._activity) and by the Markdown export (export_md._end_users).

And None, the rows of no end user and nothing else, is a meaning no door
asks of the memories' engine yet (every door that names no user_id reads
every end user, or a confined key's list): the first door of a shared level
that reads the shared memory goes into the leak matrix with it.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .ids import ERASED_PREFIX, namespace_id_problem
from .sentinels import EVERY_END_USER, AndTheShared, EndUsers

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


def wall(column, end_users):
    """The end-user wall on `column`, as a condition.

    - EVERY_END_USER: no wall, every end user and the rows of none (`true()`,
      which and_() leaves out, so a condition built with it reads as one
      without it; a None there would read as SQL NULL and match nothing);
    - EndUsers(...): a confined key's list (`condition`), never the rows of
      no end user nor an erased one's;
    - None: the rows of no end user, the shared memory;
    - AndTheShared(x): end user x's rows and the rows of none
      (`with_the_shared`);
    - anything else: that end user.

    A read for which "no user_id" means every end user passes
    EVERY_END_USER, never None: None is a value of the column."""
    from sqlalchemy import true

    if end_users is EVERY_END_USER:
        return true()
    if isinstance(end_users, EndUsers):
        return condition(column, end_users.patterns)
    if end_users is None:
        return column.is_(None)
    if isinstance(end_users, AndTheShared):
        from sqlalchemy import or_

        return or_(column == end_users.one, column.is_(None))
    return column == end_users


def with_the_shared(users: Optional[Sequence[str]], end_users):
    """What a read that shows a part's rows kept for the whole project
    filters its end users on: `end_users`, the read's own wall (`for_read`'s
    answer), with the rows of no end user added to one end user's when the
    key reads the whole project (`users` None). The founder's decision of
    12 Nov 2026 (AndTheShared): a key of the whole project reads those rows
    anyway, by naming no user_id, so they reach an end user's read too; a
    confined key never reads the rows of none (the rules above), and its
    reads stay as they are. A read that names no end user already reads
    every one, the rows of none included."""
    if users is None and isinstance(end_users, str):
        return AndTheShared(end_users)
    return end_users


def within_end_users(q, column, end_users):
    """`q` behind the end-user wall (`wall`): the one rule for every read,
    a Query or a select. EVERY_END_USER leaves `q` as it is."""
    return q if end_users is EVERY_END_USER else q.filter(wall(column, end_users))


def condition(column, users: Sequence[str]):
    """The SQL condition of a confined read on an end-user column. Reads
    take it through `wall`, with EndUsers(users)."""
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
