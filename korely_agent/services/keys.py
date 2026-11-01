"""Agent API key issuance + verification.

Best-practice key handling (OWASP ASVS + the Stripe/GitHub/OpenAI model):

1. Generate from a CSPRNG: secrets.token_urlsafe(32) = 256 bits of entropy.
2. Prefix the key (kor_self_ here, kor_live_ on the hosted service) so it is
   human-recognizable, says which of the two it belongs to, and is
   detectable by secret scanners (GitHub push protection) if leaked.
3. Store ONLY the SHA-256 hash, never the plaintext. A fast hash is correct
   here: the input is 256-bit random, so it is not brute-forceable (unlike a
   low-entropy password, which would need bcrypt/argon2).
4. Show the full key exactly once at creation; afterwards only a masked
   fragment (last 4) is recoverable.
5. Verify in constant time (hmac.compare_digest) to avoid timing side channels.
6. last_used_at on every use, revoked_at for instant soft revocation.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.times import aware
from ..db import AgentApiKey
from ..db.bounded import STREAM_ROWS
from .integrity import violated
from .sentinels import EVERY_PROJECT, within_project


#: Keys an account may hold at once that were not minted by a connected app
#: (OAuth): not revoked, expired or not. Like webhooks.MAX_ENDPOINTS, a count
#: per account, so the key list and every place that walks an account's keys
#: grow with a number the plan's customer cannot raise. Revoke one to make
#: room. A connected app's keys live an hour and are revoked at the next
#: refresh (services/oauth.py).
MAX_API_KEYS = 100


class TooManyKeys(ValueError):
    """The account holds MAX_API_KEYS keys that are not revoked."""


class DuplicateKeyNameError(Exception):
    """Raised when a user already has a (live or revoked) key with that name.
    The DB enforces unique (tenant_id, name); the router maps this to 409."""


# `kor_live_` belongs to the hosted service at api.korely.ai. A key minted by
# an install you run yourself says so in its first nine characters.
#
# The reason is one incident, and it is the reason 0.1.11 exists: a self-hosted
# key was put into a client that had no address, the client used its default,
# and the memory reached our servers before the 401 came back. The refusal was
# correct and it was too late. Two keys that look identical cannot be told
# apart by eye, in a log, or in a screenshot pasted into a chat.
#
# A fixed marker rather than the install's own name, which was the other idea
# on the table: what has to be prevented is "this must never be sent to
# Korely", and that is one bit. Putting the install name in would also catch a
# key from staging sent to production, but both of those are machines the same
# customer runs, so nothing leaves their control, and a variable-length prefix
# is a thing every client then has to parse.
PREFIX_LIVE = "kor_live_"     # hosted service
PREFIX_SELF = "kor_self_"     # an install somebody runs themselves
# `kor_test_` was a third, which nothing minted (audit of 17 Oct 2026); the
# shape below still reads it, for a key minted with it before 0.1.41.
_VALID_PREFIXES = {PREFIX_LIVE, PREFIX_SELF}

# What an install of one's own mints; the Cloud (KORELY_MODE=hosted, the same
# code) mints PREFIX_LIVE, and `minted_prefix` below is the one to ask (this
# said "what THIS product mints" until the audit of 17 Oct 2026). From
# 0.1.3 to 0.1.42 an install of one's own still let in the `kor_live_` keys it
# had minted before 0.1.3; since 0.2.0, as the CHANGELOG said from 0.1.41,
# `kor_live_` is the Cloud's alone, at every door (`verify_api_key`): a
# self-hosted key never looks like one of the Cloud's.
PREFIX_DEFAULT = PREFIX_SELF


def minted_prefix() -> str:
    """The prefix of the keys this process mints: `kor_self_` on an install of
    one's own, `kor_live_` on the hosted service (KORELY_MODE=hosted), the
    prefix the SDKs read to send a key with no address to api.korely.ai."""
    from ..config import get_settings

    return PREFIX_LIVE if get_settings().hosted else PREFIX_DEFAULT


def refused_here(prefix_or_key: str) -> bool:
    """Whether this process refuses a key for its prefix alone: a `kor_live_`
    key on an install of one's own, since 0.2.0. One rule for the door
    (`verify_api_key`) and the listings (`state_of`)."""
    from ..config import get_settings

    return (prefix_or_key or "").startswith(PREFIX_LIVE) and not get_settings().hosted

# Default least-privilege scope set for a new key.
DEFAULT_SCOPES = ["memories:read", "memories:write"]

# Every scope that means anything. A key used to accept whatever string it was
# handed, so asking for `read` (which is what somebody guesses when the list is
# written down nowhere) minted a key that authenticated fine and answered 403
# to every endpoint. A credential that works enough to look correct and is
# refused everywhere is worse than one that was never issued, because the
# person debugging it looks at the endpoint rather than at the key.
KNOWN_SCOPES = ["memories:read", "memories:write", "admin"]


class BadEndUsers(ValueError):
    """An `end_users` list a key cannot hold (services/user_scope.checked), or
    one next to `admin`: a key that mints keys could hand itself a wider one."""


class UnknownScope(ValueError):
    """A scope nobody serves. Carries the list, because that is the fix."""

    def __init__(self, bad):
        self.bad = list(bad)
        super().__init__(
            f"unknown scope(s): {', '.join(sorted(self.bad))}. "
            f"Valid scopes are: {', '.join(KNOWN_SCOPES)}"
        )

# Deliberately not in DEFAULT_SCOPES. A key that can mint keys makes revocation
# meaningless: the first thing a leaked one does is issue itself a second, and
# turning off the first changes nothing. Administration is a separate
# credential, and until the dashboard has its own login it is a separate key.
ADMIN_SCOPE = "admin"


def _checked(scopes):
    bad = [x for x in scopes if x not in KNOWN_SCOPES]
    if bad:
        raise UnknownScope(bad)
    return list(scopes)

# How long a key lasts when nobody says. Ninety days rather than forever: a
# credential with no end date is one nobody ever gets round to rotating, and
# this is the default somebody inherits without reading the documentation.
# `expires_in_days=None` still means never, for the cases that need it.
DEFAULT_LIFETIME_DAYS = 90
# The choices the screen offers. Not enforced here: an operator scripting
# against this can pass any number they like, up to the most below.
LIFETIME_CHOICES = (30, 60, 90, 180, None)
# The longest lifetime in days the API and the CLI take (ten years; longer
# is "never"). One number here: it was written in api/admin.py and in
# manage.py separately (audit of 17 Oct 2026).
MAX_LIFETIME_DAYS = 3650


def _expiry(days: Optional[int]) -> Optional[datetime]:
    """Days into an expiry date. `None` is never; `0` is already expired.

    `0` used to mean never too, which is the most permissive of the three
    readings it could have had and the opposite of what it looks like. The trap
    is a script: `days = max(0, deadline - today)` on a date already past mints
    an immortal key instead of refusing, and nothing anywhere says so.

    Now there is one way to write never (`None`, which is what the screen sends
    for "Never expires") and `0` means what it reads like. It also gives an
    operator the thing there was no way to do: expire a key on command, without
    revoking it, which is a different event and worth being able to test.
    """
    if days is None:
        return None
    days = int(days)
    if days == 0:
        return datetime.now(timezone.utc)
    if days < 0:
        raise ValueError("expires_in_days cannot be negative")
    return datetime.now(timezone.utc) + timedelta(days=days)


def state_of(row: AgentApiKey, now: Optional[datetime] = None) -> str:
    """live | revoked | refused | expired. One place, so a listing and an
    error agree: the keys page, `manage list-keys`, `manage status` and the
    restore's list of the keys in force read it.

    `refused`: a `kor_live_` key on an install of one's own, which every door
    refuses since 0.2.0 (`refused_here`) and the listings called live until
    the audit of 19 Oct 2026. Before `expired`, as the door checks the prefix
    first and its refusal says so."""
    now = now or datetime.now(timezone.utc)
    if row.revoked_at is not None:
        return "revoked"
    if refused_here(row.prefix):
        return "refused"
    if row.expires_at is not None and aware(row.expires_at) <= now:
        return "expired"
    return "live"


@dataclass(frozen=True)
class GeneratedKey:
    """The one-time result of minting a key. `full_key` is returned to the
    caller exactly once and never persisted; only `key_hash` is stored."""

    full_key: str
    key_hash: str
    prefix: str
    last_four: str


def hash_key(full_key: str) -> str:
    """SHA-256 hex digest of the full key. The DB stores only this."""
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


def generate_api_key(prefix: Optional[str] = None) -> GeneratedKey:
    """Mint a new key. The secret is 256 bits from the OS CSPRNG. No prefix:
    the one this process mints (`minted_prefix`)."""
    prefix = prefix or minted_prefix()
    if prefix not in _VALID_PREFIXES:
        raise ValueError(f"invalid key prefix: {prefix!r}")
    secret = secrets.token_urlsafe(32)  # ~43 url-safe chars, 256 bits
    full_key = f"{prefix}{secret}"
    return GeneratedKey(
        full_key=full_key,
        key_hash=hash_key(full_key),
        prefix=prefix,
        last_four=full_key[-4:],
    )


def create_api_key(
    db: Session,
    *,
    tenant_id,
    name: Optional[str] = None,
    prefix: Optional[str] = None,
    tier: str = "hobby",
    scopes: Optional[list[str]] = None,
    expires_in_days: Optional[int] = DEFAULT_LIFETIME_DAYS,
    project_id=None,
    expires_at: Optional[datetime] = None,
    oauth_client_id: Optional[str] = None,
    end_users: Optional[list[str]] = None,
) -> tuple[AgentApiKey, str]:
    """Create + persist a key row. Returns (row, full_key). The full_key is the
    ONLY time the plaintext exists outside the client; the caller must surface
    it once and then drop it. The row stores only the hash.

    `project_id` binds the key to a project. Leave it out and the key is bound
    to the account's default project, by id, here.

    It used to be stored as NULL and resolved to the default at read time, and
    the difference sounds like nothing. It is not. Three places mint keys, and
    two of them passed no project: `create-tenant`, which makes the first key of
    every install, the one the README tells you to give your agents, and
    `issue-key`. A key stored with NULL follows whichever project becomes
    default later, instead of staying where it was put, and it is missing from
    every count that joins on a project id.

    Found by a tester who noticed the Projects page saying "Default: 1 key"
    while the database held two, and worked out that the page counts concrete
    ids and the key list groups NULL under Default as well.

    Defaulting here rather than at the three call sites is the whole point: a
    fourth one will be written eventually, and it will be right without anyone
    remembering this.

    `expires_at` sets the moment itself (an OAuth access token's hour, where
    days are too coarse) and wins over `expires_in_days`; `oauth_client_id`
    marks the key of a connected app (services/oauth.py). `end_users` confines
    it to those end users of its project (services/user_scope.py); None is
    the whole project.
    """
    from . import user_scope

    try:
        confined = user_scope.checked(end_users)
    except ValueError as e:
        raise BadEndUsers(str(e)) from e
    final_scopes = _checked(scopes) if scopes is not None else list(DEFAULT_SCOPES)
    if confined is not None and ADMIN_SCOPE in final_scopes:
        raise BadEndUsers("a key confined to some end users cannot hold 'admin'")
    if project_id is None:
        from . import projects as _projects

        project_id = _projects.get_or_create_default_project(db, tenant_id).id
    if oauth_client_id is None:
        _room_for_a_key(db, tenant_id)
    gen = generate_api_key(prefix)
    row = AgentApiKey(
        tenant_id=tenant_id,
        key_hash=gen.key_hash,
        prefix=gen.prefix,
        last_four=gen.last_four,
        name=name,
        tier=tier,
        scopes=final_scopes,
        expires_at=expires_at if expires_at is not None else _expiry(expires_in_days),
        project_id=project_id,
        oauth_client_id=oauth_client_id,
        end_users=confined,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        # Only the unique (tenant_id, name) index is a duplicate name. The same
        # IntegrityError is also what a missing tenant raises, because
        # tenant_id is a foreign key, and answering that with "a key with that
        # name already exists" is a false statement about a name nobody used,
        # sent in response to a different mistake. Whoever mistyped a tenant id
        # then goes looking for a name collision that is not there.
        if violated(e, _NAME_INDEX):
            raise DuplicateKeyNameError(name or "") from e
        raise
    db.refresh(row)
    # A hash this process found unknown before (it cannot be guessed, but a
    # key could be restored from a backup) never outlives its minting here.
    _forget_unknown(gen.key_hash)
    return row, gen.full_key


def _room_for_a_key(db: Session, tenant_id) -> None:
    """Refuse the key past MAX_API_KEYS. One create of the account at a time
    from the count to the commit (a transaction's lock, released by the commit
    or the rollback), as for webhook endpoints: two creates that counted
    together would both go in."""
    from sqlalchemy import text

    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"api-keys:{tenant_id}"})
    held = (db.query(AgentApiKey)
              .filter(AgentApiKey.tenant_id == tenant_id, AgentApiKey.revoked_at.is_(None),
                      AgentApiKey.oauth_client_id.is_(None))
              .count())
    if held >= MAX_API_KEYS:
        db.rollback()
        raise TooManyKeys(f"this account holds {held} keys that are not revoked, the most it may "
                          f"hold is {MAX_API_KEYS}: revoke one first")


_NAME_INDEX = "uq_agent_api_keys_tenant_name"


# The shape of every key this code has minted, since its first version
# (`generate_api_key`: a prefix and secrets.token_urlsafe(32), 43 URL-safe
# characters). A string without it is no key of any install: it gets the answer
# of an unknown key without a query. The hosted product's logs of 2026-10-05:
# 5,224 requests in nine minutes from one address, an analyzer calling every
# method of the SDK with fake values (`/v1/facts/[object Object]`), each 401 a
# database round trip, and the pool saturated. The prefix still never decides
# whether a key gets in (core/auth.verify_api_key): all three are accepted here
# and the hash decides.
_KEY_SHAPE = re.compile(r"kor_(?:live|self|test)_[A-Za-z0-9_-]{43}")


def has_the_shape_of_a_key(presented: str) -> bool:
    """Whether a string could be a key of any install: a prefix and 43 URL-safe
    characters. Says nothing of whether one is stored."""
    return bool(presented) and bool(_KEY_SHAPE.fullmatch(presented))

# A well-formed key the table does not have, remembered for an hour (per
# process, at most 10,000, oldest out first): the same wrong key asked again
# does not reach the database. Only "no such key": a revoked or expired key is a
# row and is always read, so a revocation never waits for this cache. A key
# minted here is taken out of it at once (`create_api_key`). The same rule as the
# hosted product (its commit e21edc8).
_UNKNOWN_TTL_S = 3600.0
_UNKNOWN_MAX = 10_000
_unknown: "OrderedDict[str, float]" = OrderedDict()
_unknown_lock = threading.Lock()


def _known_to_be_unknown(key_hash: str) -> bool:
    with _unknown_lock:
        seen = _unknown.get(key_hash)
        if seen is None:
            return False
        if time.monotonic() - seen > _UNKNOWN_TTL_S:
            del _unknown[key_hash]
            return False
        return True


def _remember_unknown(key_hash: str) -> None:
    with _unknown_lock:
        _unknown[key_hash] = time.monotonic()
        _unknown.move_to_end(key_hash)
        while len(_unknown) > _UNKNOWN_MAX:
            _unknown.popitem(last=False)


def _forget_unknown(key_hash: str) -> None:
    with _unknown_lock:
        _unknown.pop(key_hash, None)


def verify_api_key(db: Session, presented_key: str) -> Optional[AgentApiKey]:
    """Verify a presented key. Returns the live (non-revoked) AgentApiKey row
    and stamps last_used_at, or None if the key is unknown or revoked.

    A string without the shape of a key, and a key already found unknown in the
    last hour, are None without a query (`_KEY_SHAPE`, `_unknown`).

    The lookup is by indexed key_hash. We still run a constant-time compare on
    the hash so a future non-indexed path can't reintroduce a timing leak.
    """
    if not presented_key or not _KEY_SHAPE.fullmatch(presented_key):
        return None
    if refused_here(presented_key):
        # 0.2.0: `kor_live_` is the Cloud's alone (the comment on
        # PREFIX_DEFAULT); the refusal's words say whose it is
        # (core/auth.refusal_for), and the listings say `refused` (`state_of`).
        return None
    presented_hash = hash_key(presented_key)
    if _known_to_be_unknown(presented_hash):
        return None
    row = (
        db.query(AgentApiKey)
        .filter(AgentApiKey.key_hash == presented_hash)
        .first()
    )
    if row is None:
        _remember_unknown(presented_hash)
        return None
    # Defense in depth: even though the SQL equality already matched, compare
    # in constant time so we never branch on partial hash equality.
    if not hmac.compare_digest(row.key_hash, presented_hash):
        return None
    if row.revoked_at is not None:
        return None
    # Expired keys answer exactly like revoked and unknown ones. Telling them
    # apart to an unauthenticated caller says which guesses were close; the
    # owner finds out which it was from `list-keys` or the screen, where the
    # state is spelled out.
    if row.expires_at is not None and aware(row.expires_at) <= datetime.now(timezone.utc):
        return None
    # Best-effort last_used_at stamp: a transient write failure here must not
    # fail an otherwise-valid authentication. The key is already verified.
    # Never waited for: a row another transaction holds (the account's own
    # project deletion or erasure moving its keys, or another check of the
    # same key stamping it now) is left as it is, SKIP LOCKED. Waiting held
    # one of the four admission threads every account's key check shares
    # for a request's lock wait (audit of 27 Oct 2026).
    try:
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import set_committed_value

        now = datetime.now(timezone.utc)
        free = (select(AgentApiKey.id).where(AgentApiKey.id == row.id)
                .with_for_update(skip_locked=True, key_share=True).scalar_subquery())
        stamped = (db.query(AgentApiKey).filter(AgentApiKey.id == free)
                     .update({AgentApiKey.last_used_at: now}, synchronize_session=False))
        db.commit()
        if stamped:
            set_committed_value(row, "last_used_at", now)
    except Exception:
        db.rollback()
    return row


def revoke_api_key(db: Session, *, key_id, tenant_id) -> bool:
    """Soft-revoke a key the user owns. Returns True if a live key was revoked,
    False if not found / not owned / already revoked. Never hard-deletes."""
    row = (
        db.query(AgentApiKey)
        .filter(AgentApiKey.id == key_id, AgentApiKey.tenant_id == tenant_id)
        .first()
    )
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return True


class ConnectedAppKey(ValueError):
    """The key of an app connected over OAuth: the person consented to one
    project, and only a new consent gives the app another."""


def move_key(db: Session, *, key_id, tenant_id, project_id) -> bool:
    """Put a key in a different project. Returns False if it is not this
    account's key. `project_id` of None puts it back in no project at all,
    which is what a key created before projects existed looks like.
    ConnectedAppKey for an OAuth app's key (audit of 17 Oct 2026)."""
    row = (
        db.query(AgentApiKey)
        .filter(AgentApiKey.id == key_id, AgentApiKey.tenant_id == tenant_id)
        .first()
    )
    if row is None:
        return False
    if row.oauth_client_id is not None and row.project_id != project_id:
        raise ConnectedAppKey(str(key_id))
    row.project_id = project_id
    db.commit()
    return True


# ── the half that makes an expiry safe ──────────────────────────────────────

# How long before the day arrives. Two weeks is enough time for somebody to
# notice, decide, and do something about it without it being urgent.
WARN_DAYS = 14


def reissue_secret(db: Session, row: AgentApiKey, *, expires_at: Optional[datetime]) -> str:
    """A new secret for the same key row, the old one dead at once; returns
    the new full key (shown once, as at minting). For an OAuth client's key
    (services/oauth.py), renewed at every refresh: one row per connected app
    in the listing, not one per hour."""
    gen = generate_api_key(row.prefix)
    row.key_hash = gen.key_hash
    row.last_four = gen.last_four
    row.expires_at = expires_at
    row.expiry_warned_at = None
    db.commit()
    _forget_unknown(gen.key_hash)
    return gen.full_key


def warn_about_expiring(db: Session, *, warn_days: int = WARN_DAYS) -> dict:
    """Tell the account about keys that are about to stop working.

    Without this, an expiry is an outage with a timer on it: the agent stops at
    three in the morning and nothing anywhere says why. With it, the same
    webhook machinery that carries `fact.invalidated` carries two weeks of
    notice, and the row remembers that it was sent so the notice does not
    repeat every ten seconds until everybody filters it.

    Also emits `key.expired` once, on the day it happens, because "it stopped
    an hour ago" is the message somebody actually needs at that moment.
    """
    from . import webhooks as webhook_service

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=max(1, int(warn_days)))
    # A key this process refuses for its prefix alone (`refused_here`) gets
    # no notice, and is left out in SQL: skipped in Python it was never
    # stamped, and a block of them at the front would starve the others.
    from ..config import get_settings

    own = [] if get_settings().hosted else [~AgentApiKey.prefix.startswith(PREFIX_LIVE, autoescape=True)]
    rows = (
        db.query(AgentApiKey)
        .filter(AgentApiKey.revoked_at.is_(None), *own,
                AgentApiKey.expires_at.isnot(None),
                AgentApiKey.expires_at <= horizon,
                # Never told, or told it was expiring and expired since: the
                # second event is due too. `expiry_warned_at` is when the last
                # one went, so once `key.expired` went it is past the expiry
                # and the key is done (audit of 17 Oct 2026: a key warned two
                # weeks ahead, the default for every 90-day key, never got
                # its `key.expired`).
                or_(AgentApiKey.expiry_warned_at.is_(None),
                    and_(AgentApiKey.expiry_warned_at < AgentApiKey.expires_at,
                         AgentApiKey.expires_at <= now)),
                # An OAuth client's key expires every hour and renews itself.
                AgentApiKey.oauth_client_id.is_(None))
        # A block a round, the next round takes the next one: the keys of
        # every account that expire together were one list (audit of 27 Oct
        # 2026).
        .order_by(AgentApiKey.expires_at, AgentApiKey.id)
        .limit(STREAM_ROWS)
        .all()
    )
    warned = expired = 0
    for row in rows:
        if refused_here(row.prefix):
            # Refused at every door already (a `kor_live_` key on an install
            # of one's own, since 0.2.0): no notice says it is about to stop,
            # nor that it stopped at its expiry (audit of 20 Oct 2026).
            continue
        at = aware(row.expires_at)
        event = "key.expired" if at <= now else "key.expiring"
        # The notice and the row's memory of it commit together. The row was
        # stamped and committed first, so an enqueue that failed after it
        # was never tried again and the notice the README promises was lost
        # (audit of 17 Oct 2026). A failed enqueue leaves the row unstamped,
        # for the next round.
        if webhook_service.emit_event(
            db, row.tenant_id, event,
            {
                "key_id": str(row.id),
                "name": row.name,
                "last_four": row.last_four,
                "expires_at": at.isoformat(),
                "days_left": max(0, (at - now).days),
            },
            # The key's project: an endpoint bound to it hears about its keys.
            project_id=row.project_id,
        ) is None:
            continue
        row.expiry_warned_at = now
        db.commit()
        if event == "key.expired":
            expired += 1
        else:
            warned += 1
    return {"warned": warned, "expired": expired} if (warned or expired) else {}


def key_project(db: Session, key) -> Optional[UUID]:
    """The project an API key is confined to.

    A key carries its project directly. The fallback exists for one case: the
    foreign key is ON DELETE SET NULL, so deleting a project leaves its keys
    with no project rather than deleting them, which is the right call because
    a live key must never disappear from under somebody's running agent.

    Such a key falls back to this account's Default project, never to "see
    everything". The direction of a mistake matters here: a key that sees too
    little makes an agent fail loudly and somebody fixes it in a minute, and a
    key that sees too much is a data leak nobody reports because nothing looks
    broken.

    None comes back only on an install where no Default project exists at all.
    Memories and facts on such an install also have no project, so the filter
    still matches them and the account keeps working. The migration that
    introduced this wall backfills both sides together, precisely so the two
    cannot end up disagreeing.
    """
    if getattr(key, "project_id", None) is not None:
        return key.project_id
    return _default_project_id(db, key.tenant_id)


def _default_project_id(db: Session, tenant_id) -> Optional[UUID]:
    """The id `key_project` gives a key with no project: the account's
    Default project, or None on an install with none."""
    from ..db.models import AgentProject

    return (db.query(AgentProject.id)
              .filter(AgentProject.tenant_id == tenant_id, AgentProject.is_default.is_(True))
              .scalar())


def within_key_project(db: Session, q, column, tenant_id, project_id):
    """The project wall for rows that carry a key's project (the keys
    themselves, and the OAuth codes whose exchange mints one): the rows
    `key_project` places in `project_id`. A key with no project reads and
    writes the Default project, so it is one of the Default project's keys
    here too, never of no project at all (audit of 19 Oct 2026: an erasure in
    the Default project skipped such a key, which kept the erased id in clear
    and stayed live). EVERY_PROJECT and None as in `sentinels.within_project`;
    another project's erasure leaves such a key alone."""
    if project_id is EVERY_PROJECT or project_id is None:
        return within_project(q, column, project_id)
    if project_id == _default_project_id(db, tenant_id):
        return q.filter(or_(column == project_id, column.is_(None)))
    return q.filter(column == project_id)
