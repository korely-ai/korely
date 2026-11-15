"""Public id encoding for the /v1 agent API.

The published contract uses opaque prefixed ids — `mem_...` for memories,
`fct_...` for facts — rather than bare UUIDs (Stripe / Mem0 convention: the
prefix tells a developer what kind of object an id refers to at a glance).
Internally everything is a UUID; these helpers encode/decode at the API edge.

Parsing accepts BOTH the prefixed form and a bare UUID, so a client that
round-trips the raw id still works.
"""
from __future__ import annotations

import uuid
from typing import Optional

from .limits import MAX_ID_CHARS, not_storable

_MEM = "mem_"
_FCT = "fct_"
_AUD = "aud_"
_JOB = "job_"


def _encode(prefix: str, u) -> str:
    return f"{prefix}{uuid.UUID(str(u)).hex}"


def _decode(prefix: str, s: Optional[str]) -> Optional[uuid.UUID]:
    """Anything that is not one of our ids comes back as None.

    The strip used to sit above the try, so a non-string argument raised
    AttributeError out of a function whose own except clause lists
    AttributeError: the intent was already "never raise", and the guard was one
    line too low. Nothing on the HTTP path could reach it, because a path
    parameter is always a string. An internal caller passing whatever it holds
    could, and would have got a 500 out of a typo.
    """
    if not isinstance(s, str) or not s:
        return None
    raw = s[len(prefix):] if s.startswith(prefix) else s
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError):
        return None


def mem_public_id(u) -> str:
    return _encode(_MEM, u)


def parse_mem_id(s: Optional[str]) -> Optional[uuid.UUID]:
    return _decode(_MEM, s)


def fact_public_id(u) -> str:
    return _encode(_FCT, u)


def parse_fact_id(s: Optional[str]) -> Optional[uuid.UUID]:
    return _decode(_FCT, s)


def audit_public_id(u) -> str:
    return _encode(_AUD, u)


def parse_audit_id(s: Optional[str]) -> Optional[uuid.UUID]:
    return _decode(_AUD, s)


def job_public_id(u) -> str:
    return _encode(_JOB, u)


def parse_job_id(s: Optional[str]) -> Optional[uuid.UUID]:
    return _decode(_JOB, s)


def public_id(prefix: str, u) -> str:
    """The public id of a row of a part of the product (core/extensions
    .Part), under the part's own prefix: the same shape as the ids above."""
    return _encode(prefix, u)


def parse_public_id(prefix: str, s: Optional[str]) -> Optional[uuid.UUID]:
    """`public_id` read back: the prefixed form or a bare UUID, None for
    anything else."""
    return _decode(prefix, s)


# ── namespace ids (user_id, agent_id) ────────────────────────────────────────
#
# 2026-09-28, the same rule as the hosted product.
# `user_id` and `agent_id` are the developer's strings, stored as given, and
# each one is also the name of something the API addresses by URL path: the
# end user in `DELETE /v1/users/<user_id>/memories` (GDPR Art. 17 erasure) and
# the agent namespace in `DELETE /v1/agents/<agent_id>`. Any string up to 255
# characters could be written, and some of them can never come back as one path
# segment: an id with a '/' was split by the router and matched nothing, so that
# end user could never be erased and that namespace never deleted.
#
# One rule, applied on every write path (POST /v1/memories, POST /v1/facts,
# POST /v1/batch and the batch worker behind it, the MCP `korely_add`): an id
# that cannot be addressed is refused when it is written, not discovered when
# somebody asks to delete it. Reads do not apply it (a query parameter carries
# any string), and the two delete routes accept any stored id, '/' included, so
# what was written before this rule stays erasable. `run_id` is not addressed by
# any path and is not subject to it.
NAMESPACE_ID_RULE = (
    f"1 to {MAX_ID_CHARS} characters, no '/', no control character or other line break, and "
    "not '.' or '..': "
    "the id names a namespace that DELETE /v1/users/<user_id>/memories and "
    "DELETE /v1/agents/<agent_id> address as one URL path segment."
)


#: What an erased end user's id becomes in the rows that stay
#: (services/pseudonyms.py), so it is no id anybody may write, confine a key
#: to or erase: in the same column as live ids, it let a key confined to 'e*'
#: read other people's erasure records, and a write or an erasure under it
#: forge or hide one (audit of 17 Oct 2026).
ERASED_PREFIX = "erased:"


def pseudonym_condition(column):
    """`is_pseudonym` as SQL on `column`: the prefix and 32 lowercase hex
    digits, the same shape."""
    import re

    return column.regexp_match("^" + re.escape(ERASED_PREFIX) + "[0-9a-f]{32}$")


def is_pseudonym(value: Optional[str]) -> bool:
    """Whether `value` is an erased end user's pseudonym, exactly as
    services/pseudonyms.pseudonym writes it: the prefix and 32 lowercase hex
    digits. What an erasure refuses to erase again and never relabels: doing
    so renamed the records that prove the earlier erasure, and GET /v1/audit
    and the replay after a restore lost them (audit of 18 Oct 2026). The shape
    and not the prefix alone: an id written before the prefix was reserved
    can begin with it, and is a live end user who must stay erasable."""
    if not value or not value.startswith(ERASED_PREFIX):
        return False
    rest = value[len(ERASED_PREFIX):]
    return len(rest) == 32 and all(c in "0123456789abcdef" for c in rest)


def namespace_id_problem(value: str) -> Optional[str]:
    """Why `value` cannot be a user_id / agent_id, or None when it can.

    Each clause is there because of a way the id stops being addressable:

    * empty: an empty path segment (`/v1/users//memories`) is not a segment,
      and clients refuse or collapse it. A read of every end user, or of the
      shared namespace, is spelled by omitting the field, not by an empty
      string; a write names someone (since 1.0.19, memory_write.checked_ids);
    * '.' and '..': RFC 3986 §5.2.4 (remove_dot_segments) deletes them while
      normalising a URL, which clients, proxies and Caddy do before the request
      reaches us, so `/v1/users/../memories` arrives as `/v1/memories`;
    * '/': it splits the segment (the defect this rule exists for);
    * control characters (Unicode category Cc), and the other line breaks,
      the line and paragraph separators (Zl, Zp: U+2028, U+2029): invisible
      in a listing, so the id cannot be read back and typed into the erase
      call, and U+0000 cannot be stored at all (Postgres text refuses it).
      By category, every alphabet's (review of the agent notices, 9 Nov
      2026: U+2028 passed where the texts said a line break did not);
    * a lone UTF-16 surrogate (category Cs): no text at all, so it cannot be
      stored either (limits.not_storable).
    """
    import unicodedata

    if value == "":
        return ("must not be empty: give an id, or leave the field out where it may be "
                "left out (a write needs a user_id or an agent_id)")
    if len(value) > MAX_ID_CHARS:
        return f"must be at most {MAX_ID_CHARS} characters"
    if value in (".", ".."):
        return ("cannot be '.' or '..': URL normalisation removes those path "
                "segments, so it could never be addressed or erased")
    if "/" in value:
        return ("cannot contain '/': it is addressed as one URL path segment "
                "(DELETE /v1/users/<user_id>/memories, DELETE /v1/agents/<agent_id>) "
                "and a '/' splits it")
    if value.startswith(ERASED_PREFIX):
        return (f"cannot begin with {ERASED_PREFIX!r}: that is how an erased end user "
                f"is named in the records that stay after an erasure")
    for ch in value:
        if unicodedata.category(ch) == "Cs":
            # Not text at all (limits.not_storable): the write rule took it
            # to the driver (the audit before 1.0.0: a lone surrogate passed the
            # one input rule).
            return f"contains {not_storable(value)}"
        if unicodedata.category(ch) == "Cc":
            return (f"cannot contain control characters (found U+{ord(ch):04X}): "
                    f"it could not be read back from a listing and typed into the erase call")
        if unicodedata.category(ch) in ("Zl", "Zp"):
            return (f"cannot contain a line break (found U+{ord(ch):04X}): it could not be "
                    f"read back from a listing and typed into the erase call")
    return None
