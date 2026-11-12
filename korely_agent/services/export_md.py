"""The facts, also as Markdown you can open.

The database is the source of truth and this is derived from it. Nothing reads
these files back; deleting them loses nothing and the next pass rewrites them.

Why it exists. This runs on somebody else's server, and the one thing a hosted
competitor cannot offer is letting you look. A folder of readable files can be
put under git, and then `git diff` shows exactly what the agent learned this
week, in a form a compliance officer can read without an API client and without
asking us anything.

Two decisions worth stating, because both could sensibly have gone the other
way:

**A superseded fact stays in the file, with the date it was closed.** Dropping
it would make the file agree with a store that overwrites, which is the thing
this product exists not to be. The file has to be able to tell the story the
`as_of` query tells.

**The output is deterministic.** Same facts, same bytes: a fixed sort and no
generated-at header. A file that changes on every pass makes `git diff` useless,
which would defeat the only reason to write files at all.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import and_, exists
from sqlalchemy.orm import Session

from ..db import bounded
from ..db.models import AgentFact, Tenant
from . import user_scope

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
# A name kept as the id itself: what _UNSAFE leaves, in lowercase only.
_PLAIN = re.compile(r"[a-z0-9._-]+")
# The facts with no end user. Its leading `_` is stripped from every id, so
# no end user's id is ever this name.
_NO_USER = "_no-user"


def safe_name(end_user_id: Optional[str]) -> str:
    """A filename that cannot escape the directory it is written into, and
    that no other end user's id gives.

    `end_user_id` is whatever the caller's agent put in a JSON field. It can be
    "../../etc/cron.d/x", a leading dot, a Windows device name, or four hundred
    characters of unicode. Everything outside a conservative set becomes an
    underscore and the result is truncated.

    Flattening alone is not one file per end user: "repo:a" and "repo_a",
    "anna@example.com" and "anna_example.com" became one name, both were written to
    it in turn on every pass, and one of them never appeared (audit of 17 Oct
    2026). So only an id that is already a plain name (lowercase letters,
    digits, `.`, `_`, `-`, nothing flattened, stripped or cut) is the name;
    any other gets `~` and a digest of the whole id after its flattened form.
    `~` is never in a plain name, so the two kinds cannot meet, and the
    lowercase rule keeps "Anna" and "anna" apart on a filesystem that does not
    tell case apart (a folder on a Mac, the README's `./facts`). Derived from
    the id alone, so a name never changes because another end user appears.

    The original is still on the first line of the file, so nothing is lost by
    the flattening.
    """
    if end_user_id is None:
        return _NO_USER
    # Normalise first: a composed character that survives the filter and a
    # decomposed one that does not would otherwise be two names for one user.
    raw = unicodedata.normalize("NFKC", end_user_id.strip())
    # Leading and trailing dots and underscores go: "." and ".." are
    # directories, and a leading dot hides the file.
    name = _UNSAFE.sub("_", raw)[:120].strip("._")
    if name == end_user_id and _PLAIN.fullmatch(name):
        return name
    digest = hashlib.sha256(end_user_id.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{name or _NO_USER}~{digest}"


def _row(f: AgentFact) -> str:
    closed = f.invalid_at.date().isoformat() if f.invalid_at else ""
    cells = [
        f.subject_label or "",
        f.predicate_canonical or "",
        f.object_label or "",
        f.valid_from.date().isoformat() if f.valid_from else "",
        closed,
    ]
    # A pipe in a value would end the cell and shift every column after it.
    return "| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |"


def _in_order(f) -> tuple:
    """A fact's place in its file: the order `_facts_of` asks the database
    for, the day it began, then its words, then its id."""
    return (f.valid_from.isoformat() if f.valid_from else "",
            (f.subject_label or ""), (f.predicate_canonical or ""),
            (f.object_label or ""), str(f.id))


def render(end_user_id: Optional[str], facts: Iterable[AgentFact]) -> str:
    """One user's facts as Markdown. Deterministic for a given set of facts."""
    rows = sorted(facts, key=_in_order)
    return _document(end_user_id, [f for f in rows if f.invalid_at is None],
                     [f for f in rows if f.invalid_at is not None])


def _document(end_user_id: Optional[str], live: Iterable, closed: Iterable) -> str:
    """The file of one end user from their facts true now and their facts
    closed, each already in order: only the lines are held, never the rows."""
    return _lines(end_user_id, [_row(f) for f in live], [_row(f) for f in closed])


def _lines(end_user_id: Optional[str], live_lines: list, closed_lines: list) -> str:
    out = [f"# {end_user_id or '(no user id)'}", ""]
    head = ["| subject | predicate | object | since | until |",
            "| --- | --- | --- | --- | --- |"]

    out += ["## What is true now", ""]
    out += head + live_lines if live_lines else ["_nothing._"]
    out += [""]

    if closed_lines:
        out += ["## What was true before", "",
                "Kept, not deleted. `until` is the day a later fact replaced it.",
                ""]
        out += head + closed_lines + [""]
    return "\n".join(out) + "\n"


# What a file shows of a fact, and nothing more: never the vector, which a
# whole row brought back as a list of 768 floats, some forty times the text
# (audit of 20 Oct 2026).
_COLUMNS = (AgentFact.id, AgentFact.subject_label, AgentFact.predicate_canonical,
            AgentFact.object_label, AgentFact.valid_from, AgentFact.invalid_at)
# The order `render` sorts in, asked of the database: by code point
# (COLLATE "C", the order of Python's strings), the id last.
_ORDER = (AgentFact.valid_from, AgentFact.subject_label.collate("C"),
          AgentFact.predicate_canonical.collate("C"), AgentFact.object_label.collate("C"),
          AgentFact.id)


def _of(tenant_id: UUID, end_user_id: Optional[str]):
    """The facts of one file: one end user's, or (None) the facts of no end
    user, `_NO_USER`'s file. The end-user wall by its one rule
    (services/user_scope.wall), where None is the rows of none."""
    return and_(AgentFact.tenant_id == tenant_id, user_scope.wall(AgentFact.end_user_id, end_user_id))


def _facts_of(db: Session, tenant_id: UUID, end_user_id: Optional[str], *, live: bool):
    """One end user's facts true now (`live`) or closed, in their file's
    order, streamed (`bounded.stream`): the columns the file shows, a batch
    at a time."""
    ended = AgentFact.invalid_at.is_(None) if live else AgentFact.invalid_at.isnot(None)
    return bounded.stream(db.query(*_COLUMNS).filter(_of(tenant_id, end_user_id), ended)
                          .order_by(*_ORDER))


def _end_users(db: Session, tenant_id: UUID):
    """Every end user the tenant's facts name, the facts of no end user
    first, a page at a time (by code point, from the last one seen): a
    tenant's end users are not all held at once either."""
    if db.query(AgentFact.id).filter(_of(tenant_id, None)).first() is not None:
        yield None
    who = AgentFact.end_user_id.collate("C")
    last = None
    while True:
        q = db.query(who).filter(AgentFact.tenant_id == tenant_id,
                                 AgentFact.end_user_id.isnot(None))
        if last is not None:
            q = q.filter(who > last)
        page = [eu for (eu,) in q.distinct().order_by(who).limit(bounded.STREAM_ROWS).all()]
        yield from page
        if len(page) < bounded.STREAM_ROWS:
            return
        last = page[-1]


# One export at a time in the process: an erasure's (after_an_erasure, on
# the request that erased) and the worker's pass each walked a whole tenant,
# and nothing kept two of them from running at once (audit of 20 Oct 2026).
# Taken through core/threads.taken: a request waits for it as long as for a row's
# lock, never for a whole pass (audit of 27 Oct 2026).
_ONE_AT_A_TIME = threading.Lock()
# The tenants whose files are owed: each export marks its tenant here before
# it waits for the lock, and whoever holds the lock writes every tenant
# marked before giving it back. A request that gave up waiting leaves its
# mark, so the export under way writes its files when it ends, never a whole
# cadence later with an erased person's file on disk (review of the fixes
# of the audit of 27 Oct 2026). Held around a set. Both are this process's: across processes
# each file a walk writes is read again from the store after the write
# (`_as_the_store_holds_it`, the audit before 1.0.0).
_owed: set = set()
_owed_lock = threading.Lock()


def export_tenant(db: Session, tenant_id: UUID, root: Path) -> int:
    """Write one file per end user of this tenant. Returns files written.

    Files are written under `<root>/<tenant_id>/`, never shared between tenants:
    the isolation the API enforces would be worth nothing if the export put two
    customers' facts in one folder.

    Only files whose content changed are written, so an unchanged pass touches
    no mtimes and produces no git noise.

    The file of an end user with no fact left goes: an erasure deletes the
    rows, and the file was their readable copy (audit of 17 Oct 2026: it
    outlived DELETE /v1/users/{id}/memories, which answers "permanent").
    """
    from ..core.threads import taken

    owed = (tenant_id, str(root))
    with _owed_lock:
        _owed.add(owed)
    with taken(_ONE_AT_A_TIME) as mine:
        if not mine:
            # A request, while another export runs: that export writes this
            # tenant's files before it gives the lock back (`_owed`).
            logger.info("[export] another export is under way; it writes tenant %s's files "
                        "when it ends", tenant_id)
            return 0
        written = 0
        while True:
            with _owed_lock:
                if not _owed:
                    return written
                nxt = owed if owed in _owed else next(iter(_owed))
                _owed.discard(nxt)
            try:
                written += _export_tenant(db, nxt[0], Path(nxt[1]) / str(nxt[0]))
            except BaseException:
                with _owed_lock:
                    _owed.add(nxt)      # still owed: the next export writes it
                raise


def _file_of(db: Session, tenant_id: UUID, end_user_id: Optional[str]) -> Optional[str]:
    """The file of one end user as the store holds it now, or None when they
    have no fact, live or closed: an erased person has no file."""
    live = [_row(f) for f in _facts_of(db, tenant_id, end_user_id, live=True)]
    closed = [_row(f) for f in _facts_of(db, tenant_id, end_user_id, live=False)]
    if not live and not closed:
        return None
    return _lines(end_user_id, live, closed)


# How many times a walk writes one file before it leaves it as last written
# (the next pass writes what came after).
_WRITES_PER_FILE = 3


def _as_the_store_holds_it(db: Session, tenant_id: UUID, end_user_id: Optional[str],
                           path: Path) -> tuple:
    """One end user's file brought in line with the store: (whether it is
    there now, how many times it was written).

    What a walk wrote is read again from the store after the write, and the
    file follows: rewritten, or removed when the person has no fact left. The
    lock and the owed marks of `export_tenant` order the exports of one
    process only; a walk in another one (RUN_WORKER=false with a worker
    apart, several workers, API replicas sharing FACTS_EXPORT_DIR) read the
    store before an erasure committed and wrote the erased person's file
    after the erasure's own export had removed it, and kept it until the
    next pass (the audit before 1.0.0). Read again after the write, the store answers
    with every commit made before it: either the erasure committed by then,
    and the file goes here, or it commits later, and its own export, after
    its commit, finds the file this wrote. The database orders the two,
    whichever process each runs in."""
    body = _file_of(db, tenant_id, end_user_id)
    written = 0
    while body is not None:
        if path.exists() and path.read_text(encoding="utf-8") == body:
            return True, written
        if written == _WRITES_PER_FILE:
            # A person written to all along, with facts: the last write
            # stays, rather than no file for someone who has facts (review
            # of the audit before 1.0.0), and the next pass writes what came after. The
            # person's erasure reads as no fact on the read after a write
            # and takes the file; one memory's erasure among such writes
            # may stay in it until that pass.
            return True, written
        # Write to a neighbour and rename: a reader that opens the file
        # while this runs sees the old version or the new one, never half.
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
        written += 1
        body = _file_of(db, tenant_id, end_user_id)
    # No fact left: no file.
    path.unlink(missing_ok=True)
    return False, written


def _export_tenant(db: Session, tenant_id: UUID, base: Path) -> int:
    base.mkdir(parents=True, exist_ok=True)
    keep = set()
    written = 0
    for eu in _end_users(db, tenant_id):
        name = f"{safe_name(eu)}.md"
        try:
            there, n = _as_the_store_holds_it(db, tenant_id, eu, base / name)
            written += n
            if there:
                keep.add(name)
        except OSError as e:
            # A full disk or a read-only mount must not stop fact extraction.
            # The export is derived; the store is not. The tenant's folder and
            # the system's reason, never the file: its name is the end user's
            # id, and the container's log is kept where no erasure reaches
            # (audit of 17 Oct 2026). The file it had stays.
            keep.add(name)
            reason = the_systems_reason(e)
            logger.error("[export] could not write a file in %s: %s", base, reason)
    _remove_all_but(base, keep)
    return written


def the_systems_reason(e: OSError) -> str:
    """An OSError as the system names it, without its message: the message
    carries the file's path, and the file is named after the end user. For
    the log lines here and for the export's state on /health (worker._export),
    which answers callers on the machine and its network with no key."""
    return (f"{type(e).__name__} errno {e.errno} ({os.strerror(e.errno)})" if e.errno
            else type(e).__name__)


def _remove_all_but(base: Path, keep: set) -> int:
    """Delete the export's own files in `base` (`*.md`, and a `.md.tmp` a pass
    left behind) that are not in `keep`; nothing else in the folder (a `.git`,
    a README of the operator's). Returns how many."""
    gone = 0
    if not base.is_dir():
        return 0
    for path in base.iterdir():
        try:
            if not path.is_file() or path.name in keep:
                continue
        except OSError as e:
            # A folder that can be listed and not searched: the error's
            # message is this file's path, named after its end user, and it
            # reached /health (audit of 18 Oct 2026). The next file is tried.
            reason = the_systems_reason(e)
            logger.error("[export] could not read a file in %s: %s", base, reason)
            continue
        if path.name.endswith(".md") or path.name.endswith(".md.tmp"):
            try:
                path.unlink()
                gone += 1
            except OSError as e:
                reason = the_systems_reason(e)
                logger.error("[export] could not remove a file in %s: %s", base, reason)
    return gone


def _is_tenant_dir(path: Path) -> bool:
    try:
        UUID(path.name)
    except ValueError:
        return False
    return path.is_dir()


def export_all(db: Session, root: Optional[str]) -> dict:
    """Worker entry point. Does nothing at all when no directory is configured.
    A tenant's folder with no fact left in the store (every end user erased,
    the account deleted) loses its files too."""
    if not root:
        return {}
    # The accounts with a fact: one row per account, from their own table.
    tenants = [t for (t,) in db.query(Tenant.id).filter(
        exists().where(AgentFact.tenant_id == Tenant.id)).all()]
    total = 0
    for t in tenants:
        total += export_tenant(db, t, Path(root))
    having = {str(t) for t in tenants}
    base = Path(root)
    if base.is_dir():
        for folder in base.iterdir():
            if _is_tenant_dir(folder) and folder.name not in having:
                _remove_all_but(folder, set())
    return {"processed": total} if total else {}


def after_an_erasure(db: Session, tenant_id: UUID) -> None:
    """Bring this tenant's files in line with the store at once, after an
    erasure committed, instead of at the next pass: the erased end user's file
    goes, the others are rewritten. While another export holds the lock past
    a request's wait, that export writes them before it ends (`_owed`).
    Best effort, and nothing at all when no directory is configured."""
    from ..config import get_settings

    root = get_settings().facts_export_dir
    if not root:
        return
    try:
        export_tenant(db, tenant_id, Path(root))
    except OSError as e:
        reason = the_systems_reason(e)
        logger.error("[export] could not update the files after an erasure: %s", reason)
