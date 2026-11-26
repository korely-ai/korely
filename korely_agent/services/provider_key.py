"""The model provider key, settable from the screen.

It used to live only in `.env`, read once at boot. That meant the moment a
customer bought their own Google key, somebody had to open an SSH session,
edit a file and restart the service. You do not ask that of a customer, and it
is the point where an install stops feeling like a product and starts feeling
like a favour.

Three rules.

**Encrypted at rest.** A model API key is billed to somebody by the token.
"It is only an MVP" is not an answer to why it was in a table in clear text.
Fernet, keyed off `SECRET_KEY`; without one, saving refuses rather than
writing plaintext.

**Never handed back.** The screen is told whether a key is set and its last
four characters, which is enough to recognise it and useless to anybody else.
The same convention as the API keys, for the same reason.

**The database wins, then the file.** An install that has always had its key
in `.env` keeps working and never learns this module exists.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import InstallSetting

logger = logging.getLogger(__name__)

GOOGLE_KEY = "google_api_key"


class NoEncryptionKey(RuntimeError):
    """There is no SECRET_KEY, so there is nowhere safe to put this."""


def _sealer(secret: str):
    from cryptography.fernet import Fernet

    # A Fernet key is 32 bytes, base64. SECRET_KEY is whatever the operator
    # typed, so it is stretched rather than truncated or padded.
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _fernet():
    secret = (get_settings().secret_key or "").strip()
    if not secret:
        raise NoEncryptionKey(
            "SECRET_KEY is not set, so a provider key cannot be stored safely. "
            "Put a long random string in .env and restart."
        )
    return _sealer(secret)


def encrypt_secret(raw: str) -> str:
    """`raw` sealed with this install's SECRET_KEY: the Google key below, and
    an OAuth client's secret (services/oauth.py)."""
    return _fernet().encrypt(raw.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """The text `encrypt_secret` sealed, under SECRET_KEY or, after a
    rotation, one of SECRET_KEY_FALLBACKS (MultiFernet: "attempts to decrypt
    tokens with each key in turn", cryptography.io, Fernet, "Key rotation";
    review of 15 Nov 2026); raises when none opens it. A value sealed under
    a fallback is sealed again under SECRET_KEY at its next save, or by
    `seal_again` (`manage reseal-secrets`)."""
    from cryptography.fernet import MultiFernet

    from .pseudonyms import secrets

    _fernet()  # NoEncryptionKey without SECRET_KEY, as before
    return MultiFernet([_sealer(s) for s in secrets()]).decrypt(
        token.encode("ascii")).decode("utf-8")


def sealed_again(token: str) -> Optional[str]:
    """`token` sealed again under SECRET_KEY when a fallback sealed it, or
    None when SECRET_KEY did already (MultiFernet.rotate, "re-encrypting it
    under the MultiFernet instance's primary key", cryptography.io, Fernet,
    "Key rotation"). Raises InvalidToken when no secret opens it,
    NoEncryptionKey without SECRET_KEY."""
    from cryptography.fernet import InvalidToken, MultiFernet

    from .pseudonyms import secrets

    raw = token.encode("ascii")
    try:
        _fernet().decrypt(raw)
        return None
    except InvalidToken:
        pass
    return MultiFernet([_sealer(s) for s in secrets()]).rotate(raw).decode("ascii")


def seal_again(db: Session) -> dict:
    """Every value `encrypt_secret` sealed (the install's settings: the
    provider key, the provider and models chosen on the screen; each OAuth
    client's secret) sealed again under SECRET_KEY: {"sealed_again": n,
    "current": n, "unreadable": n}. After a rotation it is what lets a
    fallback go without losing them: an OAuth client's secret is sealed only
    when it registers, so no save would ever seal it again (the skeptic's
    review of the scopes piece, 15 Nov 2026). `unreadable`: what no secret
    opens, which reads as unset already. The settings under their lock
    (`hold_install_settings`), each client under its row's, each committed
    on its own. The clients are read `STREAM_ROWS` ids at a time, after the
    last id seen (db/bounded.py): any client may register (RFC 7591), so
    their table has no bound of its own. Raises NoEncryptionKey without
    SECRET_KEY."""
    from cryptography.fernet import InvalidToken

    from ..db.bounded import STREAM_ROWS
    from ..db.models import OAuthClient

    _fernet()
    out = {"sealed_again": 0, "current": 0, "unreadable": 0}

    def again(token: str) -> Optional[str]:
        try:
            new = sealed_again(token)
        except InvalidToken:
            out["unreadable"] += 1
            return None
        out["current" if new is None else "sealed_again"] += 1
        return new

    hold_install_settings(db)
    for row in db.query(InstallSetting).order_by(InstallSetting.name).all():
        new = again(row.value_encrypted)
        if new is not None:
            row.value_encrypted = new
    db.commit()

    after = None
    while True:
        page = db.query(OAuthClient.client_id).filter(OAuthClient.secret_encrypted.isnot(None))
        if after is not None:
            page = page.filter(OAuthClient.client_id > after)
        ids = [cid for (cid,) in page.order_by(OAuthClient.client_id).limit(STREAM_ROWS)]
        db.rollback()
        for cid in ids:
            row = (db.query(OAuthClient).filter(OAuthClient.client_id == cid)
                   .with_for_update().first())
            new = again(row.secret_encrypted) if row is not None and row.secret_encrypted else None
            if new is not None:
                row.secret_encrypted = new
            db.commit()
        if len(ids) < STREAM_ROWS:
            return out
        after = ids[-1]


def set_google_key(db: Session, raw: str, *, by: Optional[str] = None) -> None:
    """Store it. Raises NoEncryptionKey rather than storing it in the clear."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("the key must not be empty")
    token = encrypt_secret(raw)

    hold_install_settings(db)
    row = db.get(InstallSetting, GOOGLE_KEY, populate_existing=True)
    if row is None:
        row = InstallSetting(name=GOOGLE_KEY, value_encrypted=token)
        db.add(row)
    else:
        row.value_encrypted = token
    row.updated_by = (by or "")[:64] or None
    row.updated_at = datetime.now(timezone.utc)
    db.commit()

    # The provider is memoized for the life of the process. Without this the
    # key is saved, the screen says so, and nothing extracts until a restart.
    from ..core.llm import reset_provider_cache

    reset_provider_cache()


def hold_install_settings(db: Session) -> None:
    """Hold the install's settings (the provider key, the writers chosen on
    the Models page) until this transaction ends, its commit or its
    rollback: every change of them takes it first. Two saves at once each
    read the row, and the second insert of a row that was missing failed
    (UniqueViolation, a 500), or the later write dropped what the earlier
    one saved; a clear that asked which regions it would leave with no
    writer could find them changed by the time it forgot them (5 Nov 2026).
    The pattern of hosted/signup.py, a lock of the transaction by name."""
    from sqlalchemy import text

    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('install_settings'))"))


def clear_google_key(db: Session) -> bool:
    hold_install_settings(db)
    row = db.get(InstallSetting, GOOGLE_KEY)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    from ..core.llm import reset_provider_cache

    reset_provider_cache()
    return True


def get_google_key(db: Session) -> Optional[str]:
    """The stored key, decrypted, or None.

    A value that will not decrypt is logged loudly and treated as absent: it
    means SECRET_KEY changed, and answering with a corrupt string would turn a
    recoverable configuration mistake into a provider error nobody can read.
    """
    row = db.get(InstallSetting, GOOGLE_KEY)
    if row is None:
        return None
    try:
        return decrypt_secret(row.value_encrypted)
    except NoEncryptionKey:
        logger.error("[provider] a key is stored but SECRET_KEY is not set: it cannot be read")
        return None
    except Exception:
        logger.error("[provider] the stored key will not decrypt. SECRET_KEY has "
                     "changed since it was saved and the old one is not in "
                     "SECRET_KEY_FALLBACKS; set it again from the screen.")
        return None


def uses() -> dict:
    """What the key in force does now, for the screen to say after a save.

    `writes`: it writes the memories of the projects in the Global region,
    which it does only as the file's writer (EXTRACTION_PROVIDER=gemini) and
    only while no writer is chosen for that region on the Models page
    (core/llm.get_provider: the screen wins, then the file). `vectors`: it
    computes the vectors, only when EMBEDDING_PROVIDER is not `local`. The
    screen said "Extraction and embeddings now go through this key" after
    every save, on an install whose vectors are local by default and whose
    first login chooses a writer (audit of 17 Oct 2026).
    """
    from ..core.llm import screen_writers
    from .regions import GLOBAL

    s = get_settings()
    return {"writes": s.extraction_provider == "gemini" and not screen_writers().get(GLOBAL),
            "vectors": s.embedding_provider != "local"}


def describe(db: Session) -> dict:
    """What the screen is allowed to know."""
    return {**_describe(db), "uses": uses()}


def _describe(db: Session) -> dict:
    row = db.get(InstallSetting, GOOGLE_KEY)
    s = get_settings()
    if row is not None:
        raw = get_google_key(db)
        return {
            "source": "screen",
            "set": raw is not None,
            "last_four": raw[-4:] if raw else None,
            "updated_at": row.updated_at,
            "updated_by": row.updated_by,
            "readable": raw is not None,
            "can_store": bool((s.secret_key or "").strip()),
        }
    if (s.google_api_key or "").strip():
        return {
            "source": "env", "set": True,
            "last_four": s.google_api_key.strip()[-4:],
            "updated_at": None, "updated_by": None, "readable": True,
            "can_store": bool((s.secret_key or "").strip()),
        }
    return {"source": None, "set": False, "last_four": None, "updated_at": None,
            "updated_by": None, "readable": False,
            "can_store": bool((s.secret_key or "").strip())}


def check(raw: str) -> dict:
    """Ask the provider whether the key works, with the cheapest call there is.

    A key you paste and cannot test is the same silent failure as everything
    else here: it looks saved, nothing extracts, and `/health` says `unknown`
    until the first write fails hours later.
    """
    raw = (raw or "").strip()
    if not raw:
        return {"ok": False, "detail": "no key given"}
    try:
        # Through the model layer and its gate, like every model call (audit
        # of 17 Oct 2026: this built a Gemini client of its own, which no
        # brake or price check of the Cloud could refuse).
        from ..core.llm import try_google_key

        try_google_key(raw)
        return {"ok": True, "detail": "the provider answered"}
    except Exception as e:
        # The provider's own words. "invalid API key" and "billing disabled"
        # are different problems and the person pasting needs to know which.
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"[:300]}
