"""Which constraint actually failed.

`IntegrityError` is one exception for every rule the database enforces, so a
handler that assumes which one fired turns an unrelated mistake into a confident
lie. Twice in this codebase the assumption was "a duplicate name", and a foreign
key on a tenant that does not exist raises the same exception: mistype a tenant
id and you are told a name is taken that nobody has used, then go looking for a
collision that is not there.

So the question is asked rather than assumed, from the driver's structured
diagnostics where they exist. The message text is not a contract; the constraint
name is.
"""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError


def violated(err: IntegrityError, *constraints: str) -> bool:
    """True when this error is one of the named constraints, and only then.

    Defaults to False, deliberately: mislabelling some other violation as the
    one you were expecting is the failure this exists to end, so an error whose
    constraint cannot be identified is re-raised as itself rather than dressed
    up as a domain error.
    """
    diag = getattr(getattr(err, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name:
        return name in constraints
    text = str(getattr(err, "orig", err))
    return any(c in text for c in constraints)
