"""Logging in, for the people who run the box.

An API key is what a program runs with. This is a person with a password, and
the two want different things: a password has to survive being memorable, a
session has to be revocable before it expires, and a login box has to survive
somebody pointing a script at it.

Three rules the rest of this file exists to keep.

**No default credentials.** `create` generates a password, returns it once and
stores only the hash. There is no `admin/admin` fallback and no environment
variable that sets one, because a self-hosted product with a known password
ends up indexed.

**scrypt, not a digest.** `services/keys.py` hashes API keys with SHA-256 and
says why: 256 bits of randomness needs no stretching. A password is a word
somebody can remember, and the same reasoning inverts.

**A wrong password costs time.** Ten failures lock the account for fifteen
minutes, counted in the row so a restart does not clear it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import UUID

from sqlalchemy import case, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..core import threads
from ..core.times import at_most_a_century, aware, database_now
from ..db import LoginAttempt, Operator, OperatorSession
from .rate_limit import network_of

logger = logging.getLogger(__name__)

# scrypt parameters. n=2**14 is about a tenth of a second on the hardware this
# runs on, which is the right order for a login: unnoticeable to a person,
# ruinous to a script.
_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 64

# The login's three ceilings (2026-10-06, the audit of 6 October, row 7). Every
# attempt costs a scrypt of 16 MiB, unknown usernames included (the time must
# not tell them apart), and nothing bounded them: a few dozen requests a
# second filled a small server's CPU and memory for everybody, and ten wrong
# passwords locked the operator out for whoever knew the name.
#   - ten wrong passwords for one username from one address lock that pair
#     for fifteen minutes, and only that pair;
#   - thirty from one address in fifteen minutes, whatever the usernames,
#     refuse that address before any hash is computed;
#   - at most four hashes at once in this process; the rest are refused at
#     once, so a crowd of addresses cannot queue the CPU.
# An address is its network, as for every other ceiling on a caller without
# a key (rate_limit.network_of): an IPv6 caller is handed a whole /64, and
# keyed on the exact address it took a fresh one for every attempt, so
# neither of the first two ever fired and it could hold the four hashes by
# itself (audit of 19 Oct 2026: the login ceiling was per exact IPv6
# address).
_MAX_ATTEMPTS = 10
_LOCK_MINUTES = 15
_WINDOW = timedelta(minutes=15)
_MAX_PER_ADDRESS = 30
_HASHING_AT_ONCE = 4
# The process's places for a hash (core/threads.Places, one key for the
# whole process: a login has no account yet), refused at once past them.
_HASHING = "login-hash"
_hashing = threads.Places(_HASHING_AT_ONCE, refusal=lambda n: TooBusy())

TOKEN_BYTES = 32
# A prefix, like the API keys have. Not security: it means a log line, a
# support ticket or a paste into the wrong box is recognisable for what it is.
SESSION_PREFIX = "kor_sess_"


class LockedOut(Exception):
    """Too many wrong passwords. Carries when it ends."""

    def __init__(self, until: datetime):
        super().__init__("too many attempts")
        self.until = until


class TooBusy(Exception):
    """As many passwords as the process hashes at once are being checked."""


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check. Any malformed stored value is a no, not a crash."""
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"),
                            salt=base64.b64decode(salt_b64),
                            n=int(n), r=int(r), p=int(p),
                            dklen=len(base64.b64decode(dk_b64)))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


def generate_password() -> str:
    """Long enough that nobody is tempted to improve on it, and made of
    characters that survive being read down a telephone."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(24))


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── operators ───────────────────────────────────────────────────────────────

def create(db: Session, *, tenant_id: UUID, username: str,
           password: Optional[str] = None) -> Tuple[Operator, str]:
    """Create an operator and return it with the password, once."""
    username = (username or "").strip().lower()
    if not username:
        raise ValueError("username must not be empty")
    raw = password or generate_password()
    row = Operator(id=uuid.uuid4(), tenant_id=tenant_id, username=username,
                   password_hash=hash_password(raw))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, raw


def set_password(db: Session, operator: Operator, password: str) -> None:
    """A new password, and the locks on the old one lifted, from every address."""
    operator.password_hash = hash_password(password)
    db.query(LoginAttempt).filter(LoginAttempt.username == operator.username).delete(
        synchronize_session=False)
    db.commit()


def by_username(db: Session, username: str) -> Optional[Operator]:
    return (db.query(Operator)
              .filter(Operator.username == (username or "").strip().lower())
              .first())


# ── sessions ────────────────────────────────────────────────────────────────

def login(db: Session, *, username: str, password: str, hours: int = 12,
          user_agent: Optional[str] = None, ip: Optional[str] = None
          ) -> Optional[Tuple[OperatorSession, str]]:
    """Check a password and open a session. None means "no".

    One answer for an unknown username, a wrong password and a disabled
    account: telling them apart turns the login box into a way of finding out
    which usernames exist. Raises LockedOut (this address and username, or
    this address) and TooBusy before any hash is computed.
    """
    name = (username or "").strip().lower()[:64]
    where = (network_of(ip) or "unknown")[:64]
    now = datetime.now(timezone.utc)
    _refuse_if_locked(db, name, where, now)

    with _hashing.held(_HASHING):
        op = by_username(db, name)
        if op is None:
            # Spend the time anyway. A login that returns instantly for unknown
            # names and slowly for real ones is a list of real names.
            hash_password(password or "")
            ok = False
        else:
            ok = op.disabled_at is None and verify_password(password or "", op.password_hash)

    if not ok:
        _failed(db, name, where, now)
        return None

    db.query(LoginAttempt).filter(LoginAttempt.ip == where,
                                  LoginAttempt.username == name).delete(synchronize_session=False)
    return open_session(db, op, hours=hours, user_agent=user_agent, ip=ip)


def open_session(db: Session, op: Operator, *, hours: int = 12,
                 user_agent: Optional[str] = None, ip: Optional[str] = None
                 ) -> Tuple[OperatorSession, str]:
    """A session for an operator whose identity was already checked: by the
    password above, or, on the hosted service, by a sign-in provider
    (korely_agent/hosted/). Returns the row and the token, once."""
    now = datetime.now(timezone.utc)
    # By the database's clock, which the restore's file is dated by
    # (services/credential_replay.newest_moment), never this process's.
    op.last_login_at = database_now(db)
    raw = SESSION_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    row = OperatorSession(
        id=uuid.uuid4(), operator_id=op.id, token_hash=_hash_token(raw),
        expires_at=now + timedelta(hours=at_most_a_century("session_hours", max(1, int(hours)),
                                                             24)),
        user_agent=(user_agent or "")[:255] or None, ip=(ip or "")[:64] or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, raw


def _refuse_if_locked(db: Session, name: str, where: str, now: datetime) -> None:
    row = db.get(LoginAttempt, (where, name))
    if row is not None and row.locked_until is not None and aware(row.locked_until) > now:
        raise LockedOut(aware(row.locked_until))
    recent = (db.query(func.coalesce(func.sum(LoginAttempt.failures), 0))
                .filter(LoginAttempt.ip == where, LoginAttempt.window_start > now - _WINDOW)
                .scalar())
    if int(recent or 0) >= _MAX_PER_ADDRESS:
        raise LockedOut(now + _WINDOW)


def _failed(db: Session, name: str, where: str, now: datetime) -> None:
    """One more wrong answer for this pair, counted in the statement: two
    attempts at once cannot both read the same count."""
    T = LoginAttempt.__table__
    expired = T.c.window_start <= now - _WINDOW
    failures = db.execute(
        pg_insert(LoginAttempt)
        .values(ip=where, username=name, failures=1, window_start=now)
        .on_conflict_do_update(
            index_elements=[T.c.ip, T.c.username],
            set_={"failures": case((expired, 1), else_=T.c.failures + 1),
                  "window_start": case((expired, now), else_=T.c.window_start)})
        .returning(T.c.failures)
    ).scalar_one()
    if failures >= _MAX_ATTEMPTS:
        until = now + timedelta(minutes=_LOCK_MINUTES)
        db.query(LoginAttempt).filter(LoginAttempt.ip == where,
                                      LoginAttempt.username == name).update(
            {"locked_until": until}, synchronize_session=False)
        logger.warning("[operators] %s locked out from %s until %s", name, where, until)
    db.commit()


def locks_of(db: Session, username: str) -> list:
    """The addresses this username is locked from now, and until when."""
    now = datetime.now(timezone.utc)
    return [(r.ip, aware(r.locked_until)) for r in
            db.query(LoginAttempt).filter(LoginAttempt.username == username,
                                          LoginAttempt.locked_until > now).all()]


def resolve(db: Session, raw_token: str) -> Optional[Operator]:
    """The operator behind a session token, or None.

    An expired session is deleted on the way past rather than left to the
    purge: the row has no further use and leaving it is one more place a token
    hash sits around.
    """
    if not raw_token:
        return None
    row = (db.query(OperatorSession)
             .filter(OperatorSession.token_hash == _hash_token(raw_token))
             .first())
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    if aware(row.expires_at) <= now:
        db.delete(row)
        db.commit()
        return None
    op = db.get(Operator, row.operator_id)
    if op is None or op.disabled_at is not None:
        return None
    return op


def logout(db: Session, raw_token: str) -> Optional[Operator]:
    """End the session of this token: the operator it was, for the trail,
    or None when there was no such session."""
    row = (db.query(OperatorSession)
             .filter(OperatorSession.token_hash == _hash_token(raw_token))
             .first())
    if row is None:
        return None
    op = db.get(Operator, row.operator_id)
    db.delete(row)
    db.commit()
    return op


def logout_everywhere(db: Session, operator_id: UUID) -> int:
    n = (db.query(OperatorSession)
           .filter(OperatorSession.operator_id == operator_id)
           .delete(synchronize_session=False))
    db.commit()
    return int(n or 0)


def purge_expired(db: Session) -> int:
    """Sessions past their end, and login attempts a day old with no lock in
    force (their window and any lock ended long ago)."""
    now = datetime.now(timezone.utc)
    n = (db.query(OperatorSession)
           .filter(OperatorSession.expires_at < now)
           .delete(synchronize_session=False))
    n += (db.query(LoginAttempt)
            .filter(LoginAttempt.window_start < now - timedelta(days=1),
                    (LoginAttempt.locked_until.is_(None)) | (LoginAttempt.locked_until < now))
            .delete(synchronize_session=False))
    if n:
        db.commit()
    return int(n or 0)
