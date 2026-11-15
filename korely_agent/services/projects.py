"""Agent Projects service (the projects layer of an account).

A Project groups a developer's API keys (staging vs production, multiple
products under one account). In M1 it is org-only; the inert project_id columns
on agent_memories/agent_facts make L2 full isolation purely additive later.

Mirrors api_key_service conventions: unique (tenant_id, name) + (tenant_id, slug)
enforced by the DB → IntegrityError mapped to a domain error → 409. Exactly one
is_default project per account (Postgres partial-unique index + this layer).
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import AgentProject
from ..core.times import aware

from . import regions
from .integrity import violated

_DEFAULT_NAME = "Default"
_DEFAULT_SLUG = "default"


# The indexes that mean "this name or slug is taken". Anything else that the
# database refuses is a different problem and must not be reported as this one:
# tenant_id is a foreign key, so a tenant that does not exist raises the same
# IntegrityError, and answering that with 409 "a project with that name already
# exists" sends whoever mistyped an id looking for a collision that is not there.
_NAME_INDEXES = ("uq_agent_projects_tenant_name", "uq_agent_projects_tenant_slug")


#: Projects an account may hold, archived ones included: an archive is undone
#: with /unarchive and holds its memories, as an expired key still counts
#: until it is revoked (keys.MAX_API_KEYS); a deleted project does not. A
#: count per account, like the keys, the declared names and the webhook
#: endpoints, so the project list, the Projects page and the Cloud's claim,
#: which read an account's projects whole, grow with a number the account
#: cannot raise (before 1.0.0, the leftover of the whole-read guard of the
#: audit of 27 Oct 2026).
#: An account over it (a claim keeps every project of both accounts) keeps
#: them all and makes no new one until it deletes down to it.
MAX_PROJECTS = 100


class DuplicateProjectError(Exception):
    """A project with that name (or derived slug) already exists for the user.
    The router maps this to 409, exactly like DuplicateKeyNameError."""
    code = "project_name_taken"


class ProjectRefused(ValueError):
    """A change to a project refused as it stands: 409, with `code` telling the
    screen which of its sentences to show. The screen had one for each of
    these (ProjectsPage `friendlyError`) and the API sent none of the codes,
    so every refusal read "Something went wrong" (code review of 29 Sep 2026)."""
    code = "conflict"


class CannotArchiveDefault(ProjectRefused):
    code = "cannot_archive_default"


class CannotDeleteDefault(ProjectRefused):
    code = "cannot_delete_default"


class ProjectHasLiveKeys(ProjectRefused):
    code = "project_has_live_keys"


class ProjectRegionDiffers(ProjectRefused):
    """A deletion that would move the project's live keys, or what it holds,
    into a default project of another region (`regions.kept_when_moved`):
    refused, nothing deleted (`delete_project`)."""
    code = "project_region_differs"


class TooManyProjects(ProjectRefused):
    """The account holds MAX_PROJECTS projects."""
    code = "too_many_projects"


class ProjectArchived(ProjectRefused):
    """A key put in an archived project: the twin of ProjectHasLiveKeys, which
    refuses the archive while a live key writes there (audit of 17 Oct 2026)."""
    code = "project_archived"


#: The longest name and slug a project holds: the columns' own lengths
#: (db/models.AgentProject), read here so the door that takes a name
#: (api/admin.ProjectNew, ProjectPatch), the slug made from it and the
#: claim that renames (hosted/claim.py) are one rule. The slug was cut by
#: the database instead: a name of 65 to 128 characters, which the door
#: accepted, made a slug the column refused, and the create answered 500
#: (QA of 14 Nov 2026).
NAME_MAX: int = AgentProject.__table__.c.name.type.length
SLUG_MAX: int = AgentProject.__table__.c.slug.type.length


def _within(slug: str, room: int) -> str:
    """`slug` cut to `room` characters, never ending on a dash."""
    return slug[:room].rstrip("-")


def slugify(name: str) -> str:
    """Lowercase, agent_id-safe slug: non-alphanumerics → '-', collapsed and
    trimmed, at most SLUG_MAX characters. Empty input → 'project'."""
    s = _within(re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-"), SLUG_MAX)
    return s or "project"


def _region_of(region: Optional[str]) -> Optional[str]:
    """The region a project is stored with (`regions.checked`). None follows
    the install's default writer, on an install of one's own; on the Cloud
    it is Global, the service's default (decided 6 Oct 2026; the Terms
    and the Privacy Policy say so), stored as such: the service's default
    writer is whichever provider the superadmin saved last, and a customer's
    project must not move between eu and global with it (hosted/signup.py;
    audit of 18 Oct 2026). The Default project of a Cloud
    account is made Global where the account is made (hosted/signup.create,
    hosted/accounts.sign_in)."""
    from ..config import get_settings

    r = regions.checked(region)
    if r is None and get_settings().hosted:
        return regions.HOSTED_DEFAULT
    return r


def _live_keys(db: Session, tenant_id, project_id):
    """The count of the project's live keys, as a query (a caller may narrow
    it): what authentication accepts, not revoked, not expired. An OAuth
    app's key is live until it is revoked: its `expires_at` is the hour of
    the access token, and the app's refresh makes it current again, so
    between two refreshes it counted as dead, the project was archived and
    the app went on writing into it (audit of 18 Oct 2026; the batch worker
    reads a key the same way). A key every door refuses (a `kor_live_` one
    on an install of one's own, keys.refused_here) writes nowhere and is
    not live. Counted by the database, the rule of `refused_here` with it
    (audit of 27 Oct 2026: every live key of the project was loaded to be
    counted). Read by the archive (`archive_project`) and the deletion
    (`_keep_the_region`)."""
    from datetime import datetime, timezone

    from sqlalchemy import or_

    from ..db import AgentApiKey
    from .keys import PREFIX_LIVE, refused_here

    q = db.query(func.count(AgentApiKey.id)).filter(
        AgentApiKey.tenant_id == tenant_id,
        AgentApiKey.project_id == project_id,
        AgentApiKey.revoked_at.is_(None),
        or_(AgentApiKey.oauth_client_id.isnot(None),
            AgentApiKey.expires_at.is_(None),
            AgentApiKey.expires_at > datetime.now(timezone.utc)))
    if refused_here(PREFIX_LIVE):
        q = q.filter(~AgentApiKey.prefix.startswith(PREFIX_LIVE, autoescape=True))
    return q


def get_default_project(db: Session, tenant_id) -> Optional[AgentProject]:
    return (
        db.query(AgentProject)
        .filter(AgentProject.tenant_id == tenant_id, AgentProject.is_default.is_(True))
        .first()
    )


def get_or_create_default_project(db: Session, tenant_id, *,
                                  commit: bool = True) -> AgentProject:
    """Return the account's Default project, creating it if absent. Safe under
    concurrent first-calls: the unique (tenant_id, slug) index lets one writer
    win; the loser catches IntegrityError and re-reads (same pattern as the key
    bootstrap).

    `commit=False`: made in the caller's transaction (the insert in a
    savepoint of its own, for the same race), so that a refusal after it
    rolls it back with the rest, and the caller's commit keeps it: a caller
    that needs the Default before checks of its own that may refuse leaves
    no project behind when it refuses. A part of the product's route
    (core/extensions.Part.routers) that refused what it was given after
    making the Default had left one in an account that had none (the review
    of 7 Nov 2026).

    Made with the region of any project made without one (`_region_of`):
    Global on the Cloud, stored as such, and NULL, the install's default
    writer, on an install of one's own. It was NULL everywhere, and on the
    Cloud every caller that made one this way (a key minted for an account
    with no project, the projects list, a project's deletion) left it
    following the provider the superadmin saved last (audit of 19 Oct 2026)."""
    existing = get_default_project(db, tenant_id)
    if existing is not None:
        return existing
    row = AgentProject(
        id=uuid.uuid4(), tenant_id=tenant_id, name=_DEFAULT_NAME, slug=_DEFAULT_SLUG,
        is_default=True, region=_region_of(None),
    )
    try:
        if commit:
            db.add(row)
            db.commit()
        else:
            with db.begin_nested():
                db.add(row)
    except IntegrityError:
        if commit:
            db.rollback()
        again = (
            db.query(AgentProject)
            .filter(AgentProject.tenant_id == tenant_id, AgentProject.slug == _DEFAULT_SLUG)
            .first()
        ) or get_default_project(db, tenant_id)
        if again is None:
            raise
        return again
    db.refresh(row)
    return row


def _unique_slug(db: Session, tenant_id, base: str) -> str:
    """A slug not yet used by this account (append -2, -3, … like the key
    bootstrap's 'Default N' loop), within SLUG_MAX with its suffix: the base
    gives up the suffix's room."""
    slug = base
    n = 2
    while (
        db.query(AgentProject.id)
        .filter(AgentProject.tenant_id == tenant_id, AgentProject.slug == slug)
        .first()
        is not None
    ):
        tail = f"-{n}"
        slug = _within(base, SLUG_MAX - len(tail)) + tail
        n += 1
    return slug


def create_project(
    db: Session, *, tenant_id, name: str, description: Optional[str] = None,
    region: Optional[str] = None,
) -> AgentProject:
    """Create a non-default project. Raises DuplicateProjectError (→409) on a
    name clash. Slug auto-derived from the name and de-duplicated. `region`
    (services/regions.py) None follows the install's default writer; on the
    Cloud it is Global (`_region_of`). Raises TooManyProjects (→409) past
    MAX_PROJECTS."""
    name = (name or "").strip()
    _room_for_a_project(db, tenant_id)
    slug = _unique_slug(db, tenant_id, slugify(name))
    row = AgentProject(
        id=uuid.uuid4(), tenant_id=tenant_id, name=name, slug=slug,
        description=(description or None), is_default=False,
        region=_region_of(region),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        if violated(e, *_NAME_INDEXES):
            raise DuplicateProjectError(name) from e
        raise
    db.refresh(row)
    return row


def _room_for_a_project(db: Session, tenant_id) -> None:
    """Refuse the project past MAX_PROJECTS. One create of the account at a
    time from the count to the commit (a transaction's lock, released by the
    commit or the rollback), as for keys (keys._room_for_a_key): two creates
    that counted together would both go in. The Default project is never
    refused (`get_or_create_default_project`): every key falls back on it."""
    from sqlalchemy import text

    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"projects:{tenant_id}"})
    held = (db.query(func.count(AgentProject.id))
              .filter(AgentProject.tenant_id == tenant_id).scalar() or 0)
    if held >= MAX_PROJECTS:
        db.rollback()
        raise TooManyProjects(f"this account holds {held} projects, archived ones included, the "
                              f"most it may hold is {MAX_PROJECTS}: delete one first")


_KEEP = object()


def rename_project(
    db: Session, project: AgentProject, *, name: Optional[str] = None,
    description: Optional[str] = None, region=_KEEP,
) -> AgentProject:
    """Update name/description, and the region when given (None: the
    install's default writer on an install of one's own, Global on the Cloud:
    `_region_of`). Slug is NOT changed on rename (stable handle).
    A new region applies from the next write; no fact is extracted again
    (services/regions.py). Raises DuplicateProjectError (→409) on a name clash."""
    if name is not None:
        project.name = name.strip()
    if description is not None:
        project.description = description or None
    if region is not _KEEP:
        project.region = _region_of(region)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        if violated(e, *_NAME_INDEXES):
            raise DuplicateProjectError(name or project.name) from e
        raise
    db.refresh(project)
    return project


# ── the rest of the CRUD, which the CLI never needed and a screen does ──────

def list_projects(db: Session, tenant_id, *, include_archived: bool = False) -> list:
    """This account's projects, the default one first, then by name."""
    q = db.query(AgentProject).filter(AgentProject.tenant_id == tenant_id)
    if not include_archived:
        q = q.filter(AgentProject.archived_at.is_(None))
    return q.order_by(AgentProject.is_default.desc(), AgentProject.name).all()


def get_project(db: Session, tenant_id, project_id, *,
                lock: bool = False) -> Optional[AgentProject]:
    """One project, or None. Scoped to the tenant in the query rather than
    checked afterwards: a lookup by id alone would find somebody else's row and
    leave the ownership check to whoever remembers to write it.

    `lock`: the row is held until the caller's transaction ends, read again
    as it is once any other holder has committed. Archiving takes it, and so
    does every door that puts a key in a project (POST /admin/keys, its move,
    the OAuth consent and code exchange), each in the transaction that
    commits the key: one waits for the other and finds what it did, so a key
    never lands in a project archived meanwhile, nor an archive go through
    over a key put there meanwhile. Each side read the other in a plain
    SELECT before (audit of 19 Oct 2026: archiving raced a live key). FOR
    NO KEY UPDATE (`key_share`): the memories and facts written into the
    project check their foreign key with a KEY SHARE lock on this row, and
    do not wait on it."""
    q = (db.query(AgentProject)
           .filter(AgentProject.tenant_id == tenant_id, AgentProject.id == project_id))
    if lock:
        q = q.with_for_update(key_share=True).populate_existing()
    return q.first()


def archive_project(db: Session, project: AgentProject) -> AgentProject:
    """Hide it without losing it. The default project cannot be archived: it is
    where keys land when their own project is deleted, so archiving it would
    leave the next delete with nowhere to put them."""
    if project.is_default:
        raise CannotArchiveDefault("the default project cannot be archived")
    # An archived project is hidden from the screen's lists, and a live key in
    # it goes on writing there: an agent filling a project nobody can see. The
    # screen already said "move or revoke this project's live keys first";
    # nothing checked it (code review of 29 Sep 2026).
    from datetime import datetime, timezone

    # Under the project's lock, the one every door that puts a key in it
    # takes (`get_project(lock=True)`): a key being put in commits first and
    # is counted, one that comes later finds the project archived.
    get_project(db, project.tenant_id, project.id, lock=True)
    live = int(_live_keys(db, project.tenant_id, project.id).scalar() or 0)
    if live:
        raise ProjectHasLiveKeys(
            f"{live} live key{'s write' if live != 1 else ' writes'} to this project: "
            "move or revoke them first, then archive it")
    if project.archived_at is None:
        project.archived_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(project)
    return project


def unarchive_project(db: Session, project: AgentProject) -> AgentProject:
    if project.archived_at is not None:
        project.archived_at = None
        db.commit()
        db.refresh(project)
    return project


def _move_vocabulary(db: Session, tenant_id, from_id, to_id) -> int:
    """Carry a deleted project's relations into the default project, with the
    facts that use them. The vocabulary is one per end user of a project (18
    Oct 2026: services/predicates.end_user_vocabulary), and the facts move with their end
    user, so each end user's relations join the same end user's vocabulary in
    the default project, never another end user's: deleting a project without
    erasing it is the operator asking for its data to be the default
    project's, and each end user stays a thing of their own. A relation the
    end user already has there stays its own, and the moved phrases for it
    join it unless that end user already resolves the phrase there; any other
    relation moves whole, its example a value of the facts that moved with it.
    Returns how many moved whole. Does not commit. Erased, the project's
    relations go with its row (ON DELETE CASCADE)."""
    from .predicates import merge_vocabulary

    return merge_vocabulary(db, source=(tenant_id, from_id), target=(tenant_id, to_id),
                            move_the_rest=True)


def _move_aliases(db: Session, tenant_id, from_id, to_id) -> tuple[int, int]:
    """Carry a deleted project's entity aliases into the default project, with
    the facts they name. Returns (moved, not moved). Does not commit.

    They used to go by the foreign key (ON DELETE CASCADE) with the project
    row, while the facts moved: the facts arrived with the canonical names the
    aliases had given them ("Maria" for a fact written as "Mary") and the
    aliases did not, so a read by the old name found nothing and the next
    write by it started a second entity next to the first (2026-09-28).

    Each one joins its new memory (the same end user and agent namespace, in
    the default project) by the rule every alias obeys there, the shared
    `memory_core.store.add_alias`: no cycle, the chain flattened,
    and that memory's facts recanonicalized, the default project's own facts
    by the old name included, as if the alias had been made there. Who
    decided it and since when come with it.

    A name the default project has already decided stays decided, whoever
    decided either: a move brings no newer information than what is there,
    and the default project's facts were written under its decision. The same
    decision counts as moved (the two memories agreed); a different one, or
    one that would close a cycle there, is not moved, and the reply says how
    many.

    Each memory that took an alias is recanonicalized once, after the last
    one (audit of 20 Oct 2026): it was once per alias, a full read of
    the memory each time (`add_alias(recanonicalize_now=False)`).

    Read `_MOVE_CHUNK` aliases at a time, their columns only, and each
    chunk's rows deleted, flushed and let go before the next (audit of 27
    Oct 2026: every alias of the
    project was loaded whole at once and kept in the session to the end)."""
    from ..db import AgentEntityAlias as A
    from .facts import entities as _ent
    from .memory_core import store as _store

    columns = (A.id, A.end_user_id, A.agent_id, A.alias_lower, A.alias_label,
               A.canonical_label, A.source, A.confidence, A.source_fact_id, A.since)
    moved = not_moved = 0
    touched: dict = {}
    while True:
        # The chunk moved is deleted below, so the next read starts after it.
        rows = (db.query(*columns)
                  .filter(A.tenant_id == tenant_id, A.project_id == from_id)
                  .order_by(A.since.asc(), A.created_at.asc(), A.id.asc())
                  .limit(_MOVE_CHUNK).all())
        if not rows:
            break
        for r in rows:
            mem = _ent.memory_of(tenant_id, to_id, r.end_user_id, r.agent_id)
            there = (db.query(A.canonical_label)
                       .filter(mem.alias, A.alias_lower == r.alias_lower).first())
            if there is not None:
                same = there.canonical_label.strip().lower() == r.canonical_label.strip().lower()
                moved += int(same)
                not_moved += int(not same)
                continue
            new, outcome = _store.add_alias(
                db, _ent.TABLES, mem, r.alias_label, r.canonical_label,
                source=r.source, confidence=r.confidence, fact_id=r.source_fact_id,
                recanonicalize_now=False)
            if outcome in _store.ALIAS_WRITTEN:
                touched[(r.end_user_id, r.agent_id)] = mem
            if outcome == "created":
                new.since = r.since
                moved += 1
            elif outcome == "same":
                moved += 1          # the default project already reads them as one
            else:
                not_moved += 1      # a cycle there, or not two entity names
        # The originals go: what they said now lives in the default project,
        # or was not taken there. The project row's cascade would take them.
        db.query(A).filter(A.id.in_([r.id for r in rows])).delete(synchronize_session=False)
        db.flush()
        for obj in [o for o in db.identity_map.values() if isinstance(o, A)]:
            db.expunge(obj)
    for mem in touched.values():
        _store.recanonicalize(db, _ent.TABLES, mem)
    return moved, not_moved


_MOVE_CHUNK = 500


def _move_links(db: Session, tenant_id, from_id, to_id) -> int:
    """Carry a deleted project's object links (memory_core 6.1.0, "the same
    as", facts/links.py) into the default project, with the facts they name.
    Each joins its new memory (the same end user and agent namespace) by the
    rule every link is written by, `objects.link_outcome`: one whose name
    that memory has decided already (a link or an alias), or that would close
    a cycle there ("Italy -> Italia" moving where "Italia -> Italy" stands;
    the review of the rebuild, P3.4), stays out, as an alias the default
    project has decided does (`_move_aliases`): the default project's
    decision stands, and no fact depends on a link. The row moves whole
    (its id, reason and date). Read `_MOVE_CHUNK` at a time, oldest first.
    Returns how many moved. Does not commit."""
    from ..db import AgentObjectLink as L
    from .facts import entities as _ent
    from .memory_core import objects as _objects

    moved = 0
    while True:
        rows = (db.query(L).filter(L.tenant_id == tenant_id, L.project_id == from_id)
                  .order_by(L.created_at, L.id).limit(_MOVE_CHUNK).all())
        if not rows:
            break
        for r in rows:
            mem = _ent.memory_of(tenant_id, to_id, r.end_user_id, r.agent_id)
            if _objects.link_outcome(db, _ent.TABLES, mem, r.name_label, r.canonical_label,
                                     family=r.family) == "created":
                r.project_id = to_id
                moved += 1
            else:
                db.delete(r)
            db.flush()
    return moved


def _fold_duplicates(db: Session, tenant_id, from_id, to_id) -> int:
    """Before a deleted project's facts move into the default one: an open fact
    whose canonical triple is already open there, in the same memory (same end
    user, same agent), is a restatement of it, and the two merge.

    Until now the move hit the unique index on the open triple
    (`uq_agent_facts_one_open_per_triple`) and the delete answered 500, with
    nothing moved: found in the code review of 29 Sep 2026, the same thing the
    hosted product fixed in its merge of duplicates. The rule is memory_core's
    for every duplicate (`merge_into`): sources united, the latest
    confirmation, whoever pointed at
    the loser points at the keeper, and the loser is deleted, not
    invalidated, because nothing stopped being true. The older row is kept.
    Only a fact with no end date can collide: the index is on
    `invalid_at IS NULL`, and a fact closed on a future date moves as it is.
    Returns how many merged.

    The pairs come from one read of ids (`store.open_twins`), and only
    the facts of a pair are loaded, without their vector, `_FOLD_CHUNK` pairs
    at a time (audit of 20 Oct 2026): every open fact of the project was
    read whole, vector included, with one read of its twin each.
    """
    from datetime import datetime, timezone

    from sqlalchemy import and_

    from ..db import AgentFact as F
    from .facts import entities as _ent
    from .memory_core import store as _store

    def age(f):
        return (aware(f.created_at) or datetime.min.replace(tzinfo=timezone.utc), str(f.id))

    folded = 0
    # A twin merged away or closed is no longer open there, and a later fact
    # does not meet it, as a `find_open` after the merge would not.
    gone: set = set()
    # The project's facts `_FOLD_CHUNK` at a time by id (keyset), and the
    # pairs of each block only (audit of 27 Oct 2026: the pairs of the whole
    # project were one list). A fact a block merges away is that block's.
    # Each block is bounded by the id of its last fact, the one row read to
    # cut it (OFFSET), never the block's ids.
    last = None
    while True:
        q = db.query(F.id).filter(F.tenant_id == tenant_id, F.project_id == from_id)
        if last is not None:
            q = q.filter(F.id > last)
        bound = q.order_by(F.id.asc()).offset(_FOLD_CHUNK - 1).limit(1).scalar()
        block = [F.id > last] if last is not None else []
        if bound is not None:
            block.append(F.id <= bound)
        folded += _fold_block(db, gone, _store.open_twins(
            db, _ent.TABLES, and_(F.tenant_id == tenant_id, F.project_id == from_id, *block),
            lambda f, t: and_(t.tenant_id == f.tenant_id, t.project_id == to_id,
                              t.end_user_id.is_not_distinct_from(f.end_user_id),
                              t.agent_id.is_not_distinct_from(f.agent_id))), age)
        if bound is None:
            break
        last = bound
    return folded


def _fold_block(db: Session, gone: set, chunk: list, age) -> int:
    """`_fold_duplicates` for the pairs of one block of facts, their rows
    loaded without their vector."""
    from sqlalchemy.orm import defer

    from ..db import AgentFact as F
    from .facts import entities as _ent
    from .memory_core import store as _store

    folded = 0
    ids = {x for pair in chunk for x in pair}
    if not ids:
        return 0
    rows = {r.id: r for r in db.query(F).options(defer(F.embedding))
            .filter(F.id.in_(ids)).limit(len(ids))}
    for f_id, t_id in chunk:
        if t_id in gone:
            continue
        f, twin = rows[f_id], rows[t_id]
        keep, loser = sorted((f, twin), key=age)
        if _store.merge_into(db, _ent.TABLES, keep, loser) is not twin:
            gone.add(t_id)
        folded += 1
    # Flushed, the block's rows are clean and the session's identity map
    # (weak references) lets them go with `rows`.
    db.flush()
    return folded


_FOLD_CHUNK = 500


def _hold_the_jobs(db: Session, tenant_id, project: AgentProject) -> None:
    """`delete_project`'s first lock: the project's /v1/batch jobs, in the
    order `batch.erase_items` takes them. The worker holds an item's job row
    from the moment it reads the item until the item's memory commits
    (`batch._item_now`), so that memory is written before anything below reads
    or deletes the project's rows, and goes with them; the worker's next read
    then finds the job erased, or moved. Without it, an erasure that landed
    while an item's vector was computed deleted the project's memories before
    the item's existed, and the move at the end filed it under the default
    project with its end user's id in clear (audit of 18 Oct 2026)."""
    from ..db import AgentBatchJob

    from ..db.bounded import lock_rows

    lock_rows(db, db.query(AgentBatchJob.id)
                    .filter(AgentBatchJob.tenant_id == tenant_id,
                            AgentBatchJob.project_id == project.id)
                    .order_by(AgentBatchJob.id))


def _stored(db: Session) -> tuple:
    """(label, model) of what an erasure of the project takes and a deletion
    moves: the memories, the facts, and a part of the product's rows
    (core/extensions.Part.stores) where the database has their table
    (db/chains.absent_tables)."""
    from ..core import extensions
    from ..db import AgentFact, AgentMemory
    from ..db.chains import absent_tables

    gone = absent_tables(db)
    return (("memories", AgentMemory), ("facts", AgentFact),
            *(store for part in extensions.parts() for store in part.stores()
              if store[1].__tablename__ not in gone))


def _parts_here(db: Session) -> tuple:
    """The parts of the product whose tables the database has: a part's
    whose chain never ran holds no row to lock, fold or move."""
    from ..core import extensions
    from ..db.chains import absent_tables

    gone = absent_tables(db)
    return tuple(part for part in extensions.parts() if not set(part.tables()) & gone)


def _hold_the_parts(db: Session, tenant_id, project: AgentProject) -> None:
    """The rows of a part of the product the Cloud's package carries, locked
    in the order its own writes take them (core/extensions.Part
    .project_deleting); nothing on an install of one's own."""
    for part in _parts_here(db):
        part.project_deleting(db, tenant_id, project)


def _fold_twins(db: Session, tenant_id, from_id, to_id, out: dict) -> None:
    """What the default project already holds, folded before the move: the
    open facts (`_fold_duplicates`), and a part's rows the same way
    (core/extensions.Part.project_folding), counted in `out`."""
    out["facts_merged"] = _fold_duplicates(db, tenant_id, from_id, to_id)
    for part in _parts_here(db):
        out.update(part.project_folding(db, tenant_id, from_id, to_id))


def _hold_the_project(db: Session, project: AgentProject) -> None:
    """Lock the project's own row. Every row written into the project checks
    its foreign key with a share lock on this row, which this lock excludes:
    a write already under way commits first and is seen by what follows; one
    that starts later waits, and fails on the deleted project when this
    commits. Without it, a write committed while the project went was moved
    to the default project by the erasure (an OAuth app's too, which had the
    person's consent for this project only), or kept with no project at all
    by the foreign key's SET NULL, where no key reads it (audit of 18 Oct
    2026).

    Taken after the project's memories are locked when erasing: a fact being
    written checks its memory before its project (the order of its foreign
    keys), so the other order would deadlock with the fact drain finishing
    one of those memories: the drain holding the memory, waiting for the
    project, and this holding the project, waiting for the memory."""
    (db.query(AgentProject.id)
       .filter(AgentProject.id == project.id)
       .with_for_update().all())


def _hand_over_keys(db: Session, tenant_id, project: AgentProject,
                    fallback: AgentProject, out: dict) -> None:
    """`delete_project`'s keys: the OAuth apps' revoked, every other one moved
    to the default project. Counted in `out`."""
    from ..db import AgentApiKey

    # The keys of apps connected over OAuth are revoked, not moved: the person
    # consented to this project only, and moved into the default one the app
    # read and wrote memories it was never allowed to (audit of 17 Oct 2026).
    # Revoking the key ends the grant: the app connects again.
    from datetime import datetime, timezone

    # One statement, never the keys loaded (audit of 27 Oct 2026: the apps'
    # keys are not under keys.MAX_API_KEYS). "fetch": a key of the request
    # already in the session takes the revocation too, or its later flush
    # would not know of it.
    out["apps_disconnected"] = int(
        db.query(AgentApiKey)
          .filter(AgentApiKey.tenant_id == tenant_id, AgentApiKey.project_id == project.id,
                  AgentApiKey.oauth_client_id.isnot(None), AgentApiKey.revoked_at.is_(None))
          .update({AgentApiKey.revoked_at: datetime.now(timezone.utc)},
                  synchronize_session="fetch") or 0)
    # The other keys always move: a live key must not disappear from under an
    # agent that is running, whatever happens to the data.
    out["keys"] = int((db.query(AgentApiKey)
                         .filter(AgentApiKey.tenant_id == tenant_id,
                                 AgentApiKey.project_id == project.id,
                                 AgentApiKey.oauth_client_id.is_(None))
                         .update({AgentApiKey.project_id: fallback.id},
                                 synchronize_session=False)) or 0)


def _keep_the_region(db: Session, tenant_id, project: AgentProject, fallback: AgentProject,
                     *, erase: bool, refuse: bool = True) -> Optional[str]:
    """`delete_project`'s rule of the region: the region what moves is
    written in from now on, the default project's (`regions.of_project`);
    with `refuse`, ProjectRegionDiffers, the transaction rolled back, when
    something would move into another region (`regions.kept_when_moved`).

    What moves and is written or read by the default project's writer: the
    live keys that are not an app's (an app's are revoked, `_hand_over_keys`),
    whose next writes go there, and, when not erasing, every memory, fact
    and batch job the project holds: a memory still in the queue is
    extracted there, and the judge of the default project's next writes
    reads the facts. A project with none of them moves nothing a writer
    reads, and goes. Counted under the project's lock (nothing arrives
    after the count) and after the erasure, which revokes the keys it
    leaves with nobody (`keys_revoked`); refused, that erasure is rolled
    back with the rest.

    Refused rather than asked: the region of a project is a promise made to
    the people whose memories it holds (the Privacy Policy names the
    provider of each region), and the two ways to go on are each made on
    purpose, where the change shows: give the two projects the same region
    (PATCH /admin/projects/{id}; a new region applies from the next write),
    or revoke the project's keys and erase it."""
    from ..db import AgentApiKey, AgentBatchJob, AgentFact, AgentMemory

    # Both regions read under a lock, and read again: the project's row is
    # held (`_hold_the_project`), the default project's is taken FOR SHARE
    # here, so a change of its region (PATCH) waits for this deletion's
    # commit, or, committed first, is the one read here (the review of 12
    # Nov 2026: changed between this check and the commit, the check passed
    # on the old region and the answer said it). The objects the caller
    # holds may have been read before either lock.
    db.flush()
    db.refresh(project)
    db.refresh(fallback, with_for_update={"read": True})
    region = regions.of_project(db, tenant_id, fallback.id)
    if not refuse or regions.kept_when_moved(db, tenant_id, project, fallback):
        return region
    # Live as authentication reads it (`_live_keys`), an app's left out.
    keys = int(_live_keys(db, tenant_id, project.id)
               .filter(AgentApiKey.oauth_client_id.is_(None)).scalar() or 0)
    counted = [(keys, "live API key", "live API keys")]
    if not erase:
        counted += [(int(db.query(func.count(model.id))
                         .filter(model.tenant_id == tenant_id, model.project_id == project.id)
                         .scalar() or 0), one, many)
                    for model, one, many in ((AgentMemory, "memory", "memories"),
                                             (AgentFact, "fact", "facts"),
                                             (AgentBatchJob, "batch job", "batch jobs"))]
    moving = [f"{n} {one if n == 1 else many}" for n, one, many in counted if n]
    if not moving:
        return region
    message = (
        f'Not {"erased" if erase else "deleted"}: "{project.name}" writes in '
        f"{_written_in(db, tenant_id, project)} and the Default project in "
        f"{_written_in(db, tenant_id, fallback)}. Its {', '.join(moving)} would move into "
        f"Default and be written there from now on, and a deletion never changes a project's "
        f"region. " + (
            "Revoke its keys first, or give the Default project the same region" if erase else
            "Give the two the same region first") +
        ' (Edit on the Projects page, or PATCH /admin/projects/{id} with "region")'
        + ("." if erase else ", or revoke its keys and erase it (?erase=true)."))
    db.rollback()
    raise ProjectRegionDiffers(message)


def _written_in(db: Session, tenant_id, project: AgentProject) -> str:
    """A project's region in a sentence: its writer's region now, and
    whether it follows the install's default writer, which can change."""
    from ..config import get_settings

    region = regions.of_project(db, tenant_id, project.id)
    said = regions.LABELS.get(region, "no region with a writer") if region else "no region yet"
    if project.region is None and not get_settings().hosted:
        return f"{said}, following this install's default writer"
    return said


def _live_confined_keys(db: Session, tenant_id, project: AgentProject) -> int:
    """The project's live keys confined to end users: the erasure revokes
    each one whose every end user it erased or only named, since it would
    see nobody (end_user_places.rename_in_lists), and says how many
    (`keys_revoked`, audit of 27 Oct 2026)."""
    from ..db import AgentApiKey

    db.flush()
    return int(db.query(func.count(AgentApiKey.id))
               .filter(AgentApiKey.tenant_id == tenant_id, AgentApiKey.project_id == project.id,
                       AgentApiKey.end_users.isnot(None), AgentApiKey.revoked_at.is_(None))
               .scalar() or 0)


def _erase_rows(db: Session, tenant_id, project: AgentProject,
                fallback: AgentProject, out: dict):
    """`delete_project(erase=True)`'s erasure: the project's end users
    pseudonymised, its facts, memories and batch jobs deleted, what they left
    in the vocabulary erased, counted in `out`; returns its audit row, added
    to the session."""
    from ..db import AgentForgetAudit, AgentMemory

    # The project's memories locked first: a write under way commits its
    # facts before the count below, which is then true (audit of 17 Oct
    # 2026). Then the project itself, so nothing new arrives
    # (`_hold_the_project`); its batch jobs are already held (`_hold_the_jobs`).
    from ..db.bounded import lock_rows

    lock_rows(db, db.query(AgentMemory.id).filter(AgentMemory.tenant_id == tenant_id,
                                                  AgentMemory.project_id == project.id))
    _hold_the_project(db, project)
    # Every end user the project held is erased in it: what stays of them
    # here (the access trail, the erasure records, the delivered webhooks,
    # the lists of the keys confined to them, while those keys are still the
    # project's) takes their pseudonym (services/pseudonyms.py); the cost
    # rows never name an end user (0.2.0). Read before the rows that name them go, and
    # after the locks, so a row written meanwhile names its end user here too.
    # Who the project held, from the one list of where a person can be
    # (services/end_user_places.py): its memories, facts, batch items,
    # aliases, vocabulary and erasure records. An id only its trail, its
    # keys' lists or its deliveries name (a read of it, refused or empty)
    # was never one of its people: those rows of the project take the
    # pseudonym, and nothing files an erasure of that person (audit of 20
    # Oct 2026: a key of the project
    # could get any end user of the account recorded as erased, and from then
    # on written as a pseudonym across the account).
    from . import end_user_places as places
    from .pseudonyms import after_a_projects_erasure

    end_users = places.held_in(db, tenant_id, project.id)
    only_named = places.named_in(db, tenant_id, project.id)
    agents = places.agents_in(db, tenant_id, project.id)
    # All of them at once, under two locks (the project's and the rows of
    # no project's), never one per id (audit of 27 Oct 2026).
    live_confined = _live_confined_keys(db, tenant_id, project)
    after_a_projects_erasure(db, tenant_id, project.id, held=end_users, named=only_named)
    out["keys_revoked"] = live_confined - _live_confined_keys(db, tenant_id, project)
    # Every row the project holds, facts before memories (a fact written
    # straight to /v1/facts has no source memory, so no cascade from one).
    # Its people's names 0.2.0 filed under the Default project are in it
    # since the upgrade (migration e7c4b2d9a6f1), and go with it; the ones
    # the upgrade could not place go for each who lives nowhere else.
    people = [(places.END_USER, u) for u in end_users] + [(places.AGENT, a) for a in agents]
    gone = places.erase_project(db, tenant_id, project.id, people=people)
    out["facts_deleted"] = gone.get("facts", 0)
    out["memories_deleted"] = gone.get("memories", 0)
    out["batches_deleted"] = gone.get("batch_items", 0)
    from .facts.erasure import parts_deleted

    out.update(parts_deleted(gone, tenant_id))
    # What the erased facts left in the account's vocabulary (an example
    # value, a centroid made of it), as the erasure of an end user or an
    # agent takes it away (audit of 6 Oct; this door was missed, audit of
    # 17 Oct 2026).
    from .facts.service import erase_what_it_leaves

    erase_what_it_leaves(db, tenant_id, project_id=project.id)
    # Filed straight under the default project rather than moved there by
    # the loop below: the same place, without depending on the order. The
    # counts in the columns every erasure row uses (forget_end_user files
    # its deletions there too).
    erasure = AgentForgetAudit(
        tenant_id=tenant_id,
        end_user_id=None,
        project_id=fallback.id,
        scope="project",
        target_id=str(project.id),
        memories_forgotten=out["memories_deleted"],
        facts_invalidated=out["facts_deleted"],
    )
    db.add(erasure)
    _file_each_end_user(db, tenant_id, project, fallback, end_users)
    return erasure


def _file_each_end_user(db: Session, tenant_id, project: AgentProject,
                        fallback: AgentProject, end_users: list) -> None:
    """The erasure of a project is the erasure of each of its end users (a
    decision after the audit of 19 Oct 2026), and each gets the record
    an end user's erasure files (facts.erasure.forget_end_user): scope
    'user', their pseudonym, made in the project (`erased_in_project_id`).

    Everything that asks whether an end user was erased reads those records:
    the trail's `pseudonyms.as_logged`, the webhooks' `erased_since`, the
    restore's replay. With only the project's record (scope 'project', no
    end user) a trail row written after the erasure, by a call in flight
    when it committed or by a later read through a key it moved to the
    default project, kept the erased id in clear, next to that person's
    pseudonymised rows (the pseudonyms as logged ignored a project's erasure).

    Filed under the default project with the project's own record, and
    naming the erased project in `target_id`, which tells them from an
    end user's own erasure (whose `target_id` is NULL) and ties them to the
    project's record, where the counts are: these count nothing, so a sum of
    the records' counts reads the project's deletions once, and the project's
    record is still the one `audit_id` names. Without SECRET_KEY there is no
    pseudonym to name anyone by, and nothing is filed: the proof is that
    `audit_id`, as for an end user erased without it."""
    from ..db import AgentForgetAudit
    from .pseudonyms import pseudonym

    for end_user in end_users:
        p = pseudonym(tenant_id, end_user)
        if p is None:
            continue
        db.add(AgentForgetAudit(
            tenant_id=tenant_id,
            end_user_id=p,
            project_id=fallback.id,
            erased_in_project_id=project.id,
            scope="user",
            target_id=str(project.id),
            memories_forgotten=0,
            facts_invalidated=0,
        ))


def _move_deliveries(db: Session, tenant_id, project: AgentProject,
                     fallback: AgentProject) -> int:
    """The webhook deliveries about a project deleted without erasing it
    say the default project from now on (`payload.project_id`), as the rows
    they are about do: an erasure there reaches them by that project, and a
    delivery that kept the deleted one kept the erased end user's id in
    clear for its 30 days (audit of 27 Oct 2026). In the database, one
    statement; Postgres only, as the deliveries' JSON is."""
    from sqlalchemy import JSON, Text, cast, literal_column
    from sqlalchemy.dialects.postgresql import JSONB

    from ..db import AgentWebhookDelivery as D

    if db.get_bind().dialect.name != "postgresql":
        return 0
    moved = func.jsonb_set(cast(D.payload, JSONB), literal_column("'{project_id}'::text[]"),
                           func.to_jsonb(cast(str(fallback.id), Text)))
    return int(db.query(D)
               .filter(D.tenant_id == tenant_id,
                       D.payload["project_id"].as_string() == str(project.id))
               .update({D.payload: cast(moved, JSON)}, synchronize_session=False) or 0)


def _file_the_move(db: Session, tenant_id, project: AgentProject,
                   fallback: AgentProject) -> None:
    """`delete_project`'s record of a move, among the erasures: an erasure
    made in the default project after it, through a key it moved, reaches
    the rows it moved, and a restore of a dump older than the move makes the
    move again first, where the file has it (services/erasure_replay.py;
    audit of 20 Oct 2026: the replay looked only in the default project, and
    the person came back in clear in the restored one). No end user, no counts: nothing
    was erased."""
    from ..db import AgentForgetAudit

    db.add(AgentForgetAudit(tenant_id=tenant_id, end_user_id=None, project_id=fallback.id,
                            scope="move", target_id=str(project.id),
                            memories_forgotten=0, facts_invalidated=0))


def _move_what_is_left(db: Session, tenant_id, project: AgentProject,
                       fallback: AgentProject, out: dict, *, erase: bool) -> None:
    """`delete_project`'s rows of `project` moved into `fallback`, the
    default project, counted in `out`: what an erasure left, or all of it."""
    from ..db import AgentBatchJob, AgentForgetAudit, AuditEvent

    # Move whatever is left, erased or not: otherwise the SET NULL constraint
    # would leave the rows with no project while the keys have a concrete one,
    # and the two would never meet again. The audit trail too (2026-09-28,
    # since its rows carry a project): the keys that produced it now read the
    # default project's. After an erasure no memory or fact is left to move,
    # the project row being held, and none is ever filed elsewhere. An
    # erasure record moves its `project_id` only: `erased_in_project_id`
    # keeps the project the erasure was made in, where a restore repeats it
    # (services/erasure_replay.py; audit of 19 Oct 2026).
    stored = _stored(db)
    tables = (*stored, ("audit", AgentForgetAudit), ("batches", AgentBatchJob),
              ("audit_events", AuditEvent))
    if erase:
        tables = tuple(t for t in tables if t not in stored)
    for label, model in tables:
        out[label] = int((db.query(model)
                            .filter(model.tenant_id == tenant_id,
                                    model.project_id == project.id)
                            .update({model.project_id: fallback.id},
                                    synchronize_session=False)) or 0)
    if erase:
        return
    _move_deliveries(db, tenant_id, project, fallback)
    # The aliases, after the facts they name are in place. Erased, they go
    # with the project row (ON DELETE CASCADE), as the docstring says.
    out["aliases_moved"], out["aliases_not_moved"] = _move_aliases(
        db, tenant_id, project.id, fallback.id)
    out["object_links_moved"] = _move_links(db, tenant_id, project.id, fallback.id)
    out["relations_moved"] = _move_vocabulary(db, tenant_id, project.id, fallback.id)
    _file_the_move(db, tenant_id, project, fallback)


def delete_project(db: Session, tenant_id, project: AgentProject,
                   *, erase: bool = False, across_regions: bool = False) -> dict:
    """Delete a project. Everything it held moves to the default one, of
    the same region or nothing is deleted (`_keep_the_region`, skipped by
    `across_regions`, the restore's replay only).

    `erase=True` erases it instead: every memory and every fact the project
    held is physically deleted (the forgotten memories and the closed facts
    too, their text included), with the project's entity aliases and its
    /v1/batch jobs, and a row in the erasure audit. Asked for, never the
    default.

    Deleted, not flagged, since 2026-09-28. It used to mark the memories
    deleted and close the facts bi-temporally, and every one of those rows,
    text and all, stayed in the table and in the history a key reads with
    `include_invalidated=true`: a soft forget answering to the name of an
    erasure, on the product whose README says "erasure that erases" and
    whose end-user erasure (`facts.service.forget_end_user`) has deleted
    physically since 2026-09-08. One meaning for the word, the legal one
    (GDPR Art. 17). Forgetting without erasing stays available where it is
    documented as such: one memory (DELETE /v1/memories/{id}) and one fact.

    The batch jobs go because each one's payload is a copy of the memories
    it imported, and a job still queued would have imported the erased
    memories again, into the default project, after the erasure. The aliases
    go by their foreign key (ON DELETE CASCADE) with the project row. The
    access trail (`audit_events`: who touched which end user's data, never
    the content) moves to the default project as it does for every other
    erasure.

    The audit row is `agent_forget_audit` with scope 'project', filed like
    the other erasures (forget_end_user, delete_agent): counts, never content,
    and its id comes back as `audit_id`. Its project is the default one, where
    the keys and the rest of the deleted project's audit trail now live; the
    erased project is named in `target_id`, because its own row is gone. The
    docstring promised the row from the start and none was written until
    2026-09-28, which made this the one erasure nobody could show had
    happened. Beside it, one scope 'user' record per end user it erased,
    under their pseudonym (`_file_each_end_user`, audit of 19 Oct 2026): erasing a
    project counts as erasing each of them.

    Moving is the default because of which mistake can be undone. Move when
    they wanted erasure and they can still erase; erase when they wanted to keep
    and nothing brings it back. The irreversible road must not be the one you
    take by accident, and a confirmation box is not enough to tell the two
    apart.

    Keys, memories, facts, entity aliases (`_move_aliases`, since
    2026-09-28), erasure audit rows and queued batch jobs: all of it, in one
    transaction. Refuses to delete the default project for the same
    reason it refuses to archive it.

    THE DATA HAS TO MOVE WITH THE KEYS, and this used to move only the keys.

    The foreign key is ON DELETE SET NULL, which was the right shape while a
    project was a label: nothing was lost, the rows just stopped being filed
    under anything. Since the project became a wall, NULL is not "unfiled", it
    is an address, and the keys were being sent to a different one. `key_project`
    resolves a key with no project to the default project's concrete id, so
    after a delete the key looked for that id and the rows carried NULL. They
    never met again.

    Found by a tester who deleted a project and then tried to erase what was in
    it:

        delete_all(user_id="g-user")
        -> memories_deleted=0, facts_deleted=0, audit_id=...

    while one memory and three facts were still in the database, readable by
    nobody and deletable by nobody. Not a leak: nothing became visible to
    another project. Worse in a different direction, for a product whose README
    says "erasure that erases" and offers the audit trail as the shape of an
    access request. A pipeline built on that API would have recorded the
    erasure as done.
    """
    if project.is_default:
        raise CannotDeleteDefault("the default project cannot be deleted")

    fallback = get_or_create_default_project(db, tenant_id, commit=False)
    out = {"keys": 0, "memories": 0, "facts": 0, "audit": 0, "batches": 0,
           "audit_events": 0, "aliases_moved": 0, "aliases_not_moved": 0,
           "memories_deleted": 0, "facts_deleted": 0,
           "batches_deleted": 0, "keys_revoked": 0,
           "facts_merged": 0, "audit_id": None}

    # The deadlines of an erasure (core/threads.longer_deadlines): this
    # request waits for the writes under way into the project to commit, and
    # the writes that start later give up after a request's lock wait (503,
    # retry) instead of holding a thread and a connection for its length
    # (audit of 20 Oct 2026: a lock wait had no deadline).
    from ..core.threads import longer_deadlines

    longer_deadlines(db)
    # Serialised with the writes into the project: its batch jobs, a part
    # of the product's rows (core/extensions.Part.project_deleting), then
    # (in `_erase_rows`, or here) the project row itself.
    _hold_the_jobs(db, tenant_id, project)
    _hold_the_parts(db, tenant_id, project)
    erasure = (_erase_rows(db, tenant_id, project, fallback, out) if erase
               else _hold_the_project(db, project))
    out["region"] = _keep_the_region(db, tenant_id, project, fallback, erase=erase,
                                     refuse=not across_regions)
    if not erase:
        # A fact the default project already holds open would break the unique
        # index on the move; it merges into its twin first. After an erasure
        # there are no facts left to collide.
        _fold_twins(db, tenant_id, project.id, fallback.id, out)
    # The keys after the erasure, which rewrites the lists of the keys
    # confined to the erased end users while they are still the project's.
    _hand_over_keys(db, tenant_id, project, fallback, out)

    _move_what_is_left(db, tenant_id, project, fallback, out, erase=erase)
    db.delete(project)
    db.commit()
    if erasure is not None:
        from .end_user_places import after_the_commit
        from .ids import audit_public_id

        out["audit_id"] = audit_public_id(erasure.id)
        after_the_commit(db, tenant_id)
    return out
