"""The `Idempotency-Key` of a write: a request sent again is made once.

The header of the IETF draft "The Idempotency-Key HTTP Header Field"
(draft-ietf-httpapi-idempotency-key-header-08, 22 Oct 2026), honoured on the
doors that create, the ones `ROUTES` names: POST /v1/memories and POST
/v1/batch. A client that retries after a timeout sends the same key, and the
write it may already have made is not made, nor counted, again. Without the
header nothing changes: two identical requests are two writes, as with Mem0's
`infer=False` and with Supermemory without an `id`.

What each section of the draft asks, and where it is done here:

- §2.1, the value is a String (RFC 9651 §3.3.3): `key_of` reads the quoted
  form, and the bare value Stripe's clients send
  (https://docs.stripe.com/api/idempotent_requests) as the same key; 1 to 255
  characters, Stripe's ceiling. Anything else is a 400
  `idempotency_key_invalid`.
- §2.2 and §5, uniqueness: the key is the account's and the project's, a
  "unique composite key" of the client's key and what the resource knows of
  the client. The same key in another account or project is another key; a
  key of the project is the same key for every API key of it, as Stripe's
  is the account's.
- §2.3, expiry: `LIFETIME`, 24 hours from the write, published in
  docs/guide/api.md; then the key is taken as new.
- §2.4, the fingerprint: the SHA-256 of the route and of the request as it
  was checked (services/memory_write.MemoryWrite.as_item: the text stripped,
  the end user placed for the key, the event time read), so a confined key
  that sends another key's request names another write.
- §2.6, a retry of a request made: "a response consistent with the request
  being completed", with the header `Idempotent-Replayed: true` (Stripe's
  name). Stripe answers the first response, kept; the answer is not kept
  here, so the table holds no text of anybody's, and a retry is answered
  with what the request itself holds and what it made, never with what
  others wrote since: the memory with the text and ids sent, its id, status
  and times, and no facts (api/memories._made_before); the job with its id,
  status and count (api/batch._queued_before). What it made, gone since or
  out of the key's reach, is a 404 `idempotent_target_gone`, and nothing is
  written again.
- §2.7: the same key with another request, 422 `idempotency_key_reused`; a
  retry while the first is still being made, 409 `idempotency_key_in_use`,
  which needs no correction (retry it).

Made once, and for sure: the key's row is written in the transaction that
makes the write (`hold`, before the quota is counted; the write's own
commit), so a write refused or failed takes its key with it, and a retry
makes it; its foreign key to what it made is checked at that commit. While
that transaction is open, its holder has the key's lock
(`pg_try_advisory_xact_lock`): a retry that cannot take it is the 409, never
a wait.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from ..db.models import AgentIdempotencyKey

HEADER = "Idempotency-Key"
#: The header of an answer made from a request made before (Stripe's name).
REPLAYED = "Idempotent-Replayed"
#: How long a key holds (§2.3), from its write.
LIFETIME = timedelta(hours=24)
MAX_KEY_CHARS = 255

#: The doors that honour the header, and the column of what each makes.
ROUTES = {
    "POST /v1/memories": AgentIdempotencyKey.memory_id,
    "POST /v1/batch": AgentIdempotencyKey.batch_job_id,
}

HEADER_RULE = (
    "Makes a retry of this request safe (IETF draft-ietf-httpapi-idempotency-key-header-08): "
    "the same key with the same request answers what the first made, and nothing is "
    "written or counted again (`Idempotent-Replayed: true`); the same key with another "
    "request is a 422 `idempotency_key_reused`; a retry while the first is being made is a "
    "409 `idempotency_key_in_use`. A key is the project's and holds 24 hours from its "
    f"write. 1 to {MAX_KEY_CHARS} characters, quoted (`\"...\"`) or bare. Without it, "
    "every request is a new write.")


#: What the statuses a door that honours the header can add mean there
#: (api/errors.v1_errors).
RESPONSES = {
    400: f"`idempotency_key_invalid`: the `{HEADER}` header is not 1 to {MAX_KEY_CHARS} "
         "printable ASCII characters, quoted or bare.",
    404: "`idempotent_target_gone`: the request with this `Idempotency-Key` was made, and "
         "what it made is not there for this key (deleted since, or out of its reach); "
         "nothing was written again.",
    409: "`idempotency_key_in_use`: a request with this `Idempotency-Key` is still being made; "
         "retry it as it is.",
    # Beside the route's own 422s (api/errors.idempotent_door).
    422: "`idempotency_key_reused`: this `Idempotency-Key` was sent with another request in "
         "its 24 hours; nothing was written. Send a new key for a new request.",
}

#: The response header of an answer made from a request made before, as the
#: document declares it on the door's 201 or 202.
REPLAYED_HEADER = {
    "description": "`true` on an answer made from the request made before with this "
                   "`Idempotency-Key`: nothing was written or counted again. Absent "
                   "otherwise.",
    "schema": {"type": "string", "enum": ["true"]},
}


@dataclass(frozen=True)
class Held:
    """What `hold` found: the id the request's write is to take, or, for a
    request made before (`again`), the id of what it made."""

    target: UUID
    again: bool


def _refusal(code: int, token: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": token, "message": message})


def _invalid() -> HTTPException:
    return _refusal(status.HTTP_400_BAD_REQUEST, "idempotency_key_invalid",
                    f"The {HEADER} header must be 1 to {MAX_KEY_CHARS} printable ASCII "
                    f"characters, as a quoted string (\"...\") or bare.")


def _sf_string(value: str) -> Optional[str]:
    """A String of RFC 9651 (§4.2.5): the characters between the quotes, the
    only escapes \\" and \\\\; None for anything else, or anything after it."""
    out = []
    i = 1
    while i < len(value):
        c = value[i]
        if c == "\\":
            if value[i + 1:i + 2] not in ('"', "\\"):
                return None
            out.append(value[i + 1])
            i += 2
            continue
        if c == '"':
            return "".join(out) if not value[i + 1:].strip(" \t") else None
        if not 0x20 <= ord(c) <= 0x7E:
            return None
        out.append(c)
        i += 1
    return None


def key_of(raw: Optional[str]) -> Optional[str]:
    """The key of the header as sent, None when it was not sent; a 400 for
    one that is not a key (§2.1)."""
    if raw is None:
        return None
    value = raw.strip(" \t")
    if value.startswith('"'):
        key = _sf_string(value)
    else:
        key = value if all(0x21 <= ord(c) <= 0x7E for c in value) else None
    if not key or len(key) > MAX_KEY_CHARS:
        raise _invalid()
    return key


def _digest(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def fingerprint(route: str, request: Any) -> str:
    """The SHA-256 of the route and of the request as checked (§2.4): the
    same for the same write, whatever the order of a JSON object's keys."""
    return _digest(json.dumps([route, request], sort_keys=True, ensure_ascii=False,
                              separators=(",", ":"), default=str))


def _lock_key(tenant_id, project_id, key_hash: str) -> int:
    digest = hashlib.sha256(f"korely-idempotency:{tenant_id}:{project_id}:{key_hash}"
                            .encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def hold(db: Session, tenant_id, project_id, raw: Optional[str], route: str,
         request: Any) -> Optional[Held]:
    """The key of `raw` for the request about to be written, in `db`'s
    transaction, the one that will make the write: None without a key
    (nothing changes); `again` when the request was made already, within
    its 24 hours. Raises 400, 409 or 422 (the module's docstring). Call it
    once the request is checked and before the quota is counted, so that a
    retry counts nothing."""
    key = key_of(raw)
    if key is None:
        return None
    key_hash, print_ = _digest(key), fingerprint(route, request)
    taken = db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                       {"k": _lock_key(tenant_id, project_id, key_hash)}).scalar()
    if not taken:
        raise _refusal(status.HTTP_409_CONFLICT, "idempotency_key_in_use",
                       f"A request with this {HEADER} is still being made. Retry it as it is.")
    t = AgentIdempotencyKey
    scope = (t.tenant_id == tenant_id, t.project_id.is_not_distinct_from(project_id),
             t.key_hash == key_hash)
    row = db.execute(select(t.fingerprint, t.memory_id, t.batch_job_id,
                            (t.expires_at > func.now()).label("live"))
                     .where(*scope).limit(1)).first()
    if row is not None and row.live:
        if row.fingerprint != print_:
            raise _refusal(status.HTTP_422_UNPROCESSABLE_CONTENT, "idempotency_key_reused",
                           f"This {HEADER} was sent with another request in the last 24 "
                           f"hours. Send a new key for a new request.")
        return Held(target=row.memory_id or row.batch_job_id, again=True)
    if row is not None:  # past its 24 hours: a new key
        db.execute(delete(t).where(*scope))
    target = uuid4()
    db.execute(insert(t).values(id=uuid4(), tenant_id=tenant_id, project_id=project_id,
                                key_hash=key_hash, fingerprint=print_,
                                expires_at=func.now() + LIFETIME,
                                **{ROUTES[route].key: target}))
    return Held(target=target, again=False)


def target_gone() -> HTTPException:
    """The answer to a retry whose write was made and whose result this key
    cannot read: deleted since, or a job another key confined to an end user
    queued (api/batch._queued_before). It names no id and claims no
    deletion: the id is another key's, and the job is there for that key
    (code review finding, 15 Nov 2026). Nothing is written again."""
    return _refusal(status.HTTP_404_NOT_FOUND, "idempotent_target_gone",
                    f"The request with this {HEADER} was made, and what it made is not there "
                    f"for this key: deleted since, or out of its reach. Nothing was written "
                    f"again.")


def purge_expired(db: Session) -> int:
    """The keys past their 24 hours, deleted; a count. The worker's purge."""
    t = AgentIdempotencyKey
    n = db.execute(delete(t).where(t.expires_at <= func.now())).rowcount
    db.commit()
    return int(n or 0)
