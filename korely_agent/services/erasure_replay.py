"""The erasures made after a dump, carried across its restore (deploy/restore.sh).

A restore brings back whatever the dump held, including the people erased
after it was taken, and the erasure records go back with the database. The
README told the operator to read them first with GET /v1/audit?action=erase
and repeat them, which could not be followed (audit of 17 Oct 2026): an erased
end user is there only as a keyed pseudonym (services/pseudonyms.py), the list
mixes in the soft forgets of facts, and a key reads one project.

So restore.sh does it: `export` writes every erasure record of the live
database before the restore, and `replay`, after it, repeats the ones the dump
does not hold, finding each erased end user among the restored ones by the
same pseudonym, and puts back the original record, so the `audit_id` the
erasure answered with still proves it. The end user is looked for wherever an
erasure reaches, the items of queued /v1/batch jobs too, and an erased memory
the dump holds only as such an item has that item scrubbed (audit of 19 Oct
2026: both counted as nothing to erase, and the worker imported them
again). Erasures of an end user, of an agent
namespace, of a project and of one memory; a fact forgotten or corrected is a
change of what is true, not an erasure of a person, and the dump's version of
it stands.

One memory has two records that look the same, scope 'memory': its forget
and its erasure (DELETE /v1/memories/{id}?erase=true writes the forget's).
The live database tells them apart: after an erasure the row is gone, after
a forget it is there, so `export` marks `erased` the records whose memory the
live database no longer holds, and the replay erases those again, row, text,
vector and facts, instead of forgetting them (audit of 18 Oct 2026: it only
forgot, and the text and the facts came back). A record written before this
is told apart the same way. A memory gone with a whole end user, agent or
project is marked too, and erasing it once more is what that erasure did.

The file holds pseudonyms, agent namespaces and ids, and for the erasure of
one memory the end user id it named: personal data, written owner-only by
restore.sh and deleted after the replay.

The file is `{"key_check": ..., "records": [...]}`: the records, and the
check of the pseudonym key they were read under (pseudonyms.key_check). An
erased end user is found only by the pseudonym computed under this install's
SECRET_KEY, so under another key, or none, every one of them matched nobody,
was counted as nothing to erase, and restore.sh started the API with them
back (audit of 19 Oct 2026). The replay now refuses, before it erases anything, when
the file holds end users erased by pseudonym and the key here is not the
one it names, or there is none here: `KeyMismatch`. A file written before
it, the bare list of records, replays as it did, with no key to compare,
except that without SECRET_KEY here those end users are refused the same
way instead of counted as nothing to erase.

An erasure of an end user or an agent namespace is repeated in the project
it was made in (`erased_in_project_id`), never in the one its record is filed
under now: deleting a project without `erase` files its records under the
default project, and the replay looked there, so the person the dump brought
back in the deleted project stayed in clear, or a different end user who
shares the id in the default project was erased in their place (audit of 19
Oct 2026). When that project is not in the restored data, nothing of the
erasure is there either. A file written before it carries only the project
the record was filed under, and replays as it did.

Erasing a project files, beside its own record, one scope 'user' record per
end user it erased, under their pseudonym and naming the project in
`target_id` (services/projects._file_each_end_user; audit of 19 Oct
2026). They are parts of the project's erasure, not erasures of their own:
the project's record replays the whole project, which finds those people by
its rows and needs no key, and the parts go back as they were, counted
nowhere (the project's erasure is counted once) and never replayed one by
one. The records the replayed erasure files for the same people are dropped,
since the originals go back.

What the live database did after the dump that changes where an erasure
reaches is made again too, in its place among them (audit of 20 Oct
2026):

- a project deleted without `erase` moved its rows to the default project,
  and an erasure made there afterwards, through one of its moved keys,
  reached them; on a dump older than the move they were in the deleted
  project, which the replay never searched, and the person came back in
  clear.
  The deletion files a record of the move (scope 'move',
  projects.delete_project), and the replay makes the move again where the
  file has it, before the erasures that came after it;
- on the Cloud, an account merged into another by a claim (hosted/claim.
  move; the live account lists it in `merged_tenant_ids`) is merged again
  first, and an account closed (hosted/closing.close, its proof in
  `hosted_erasures`, outside the cascade) is closed again: a dump older than
  either brought the account back, its keys working, and the erasures made
  in the account it was merged into found nobody.

Each record carries the check of the pseudonym key it was made under
(`AgentForgetAudit.key_check`): the file's own check was computed at the
export, under the key in force then, so a key changed between an erasure and
the restore passed it, and the erased person was counted nothing to erase
(audit of 20 Oct 2026: a mismatched SECRET_KEY failed open). An export from an
install with no SECRET_KEY now carries the
records' own checks, which the replay compares with its key; one whose
records have none is refused at the export (`KeyUnknown`), since no key
could replay it (it was refused under every key, the right one included).
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime
from typing import Iterable, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from ..db.models import AgentForgetAudit, AgentMemory, AgentProject, Tenant
from .memory_core import log_safe
from .sentinels import EVERY_PROJECT

logger = logging.getLogger(__name__)

SCOPES = ("user", "agent", "project", "memory")
# `erased_in_project_id` of an end user's or agent's erasure of 0.1.1-0.1.11
# whose project the trail could not tell (migration f6a3d9c1b8e2): neither a
# project nor the whole account. The replay repeats it nowhere and counts it
# (`project_unknown`): repeated in every project of the account it erased a
# namesake there (audit of 27 Oct 2026), and the operator erases the person
# again where they were.
PROJECT_NOT_RECORDED = UUID(int=0)
# What else the replay makes again: a project's move, a Cloud account's
# merge and close (the module's docstring).
CHANGES = ("move", "merge", "close")


class KeyMismatch(Exception):
    """The replay cannot find the end users the file names by pseudonym:
    the file was written under another pseudonym key than this install's,
    or this install has none. `pending` counts those end users' records."""

    def __init__(self, pending: int, *, keyless: bool):
        super().__init__(pending)
        self.pending = pending
        self.keyless = keyless


_UNCHECKED = object()  # a file written before the key check: nothing to compare


class KeyUnknown(Exception):
    """`export_file` on an install with no SECRET_KEY whose records name end
    users by pseudonym and carry no check of the key that made them: no key
    could replay the file. `pending` counts them."""

    def __init__(self, pending: int):
        super().__init__(pending)
        self.pending = pending


def export_file(db: Session) -> dict:
    """What `manage erasures-export` writes: the records, each with the check
    of the key it was made under, and the check of the key in force now.
    KeyUnknown when neither says which key made a pseudonym."""
    from .ids import is_pseudonym
    from .pseudonyms import key_check

    records = export(db)
    here = key_check()
    if here is None:
        unknown = sum(1 for r in records if r.get("scope") == "user"
                      and is_pseudonym(r.get("end_user_id")) and not r.get("key_check"))
        if unknown:
            raise KeyUnknown(unknown)
    return {"key_check": here, "records": records}


def read_file(data) -> tuple:
    """(records, key_check) from what `export_file` wrote, or from the bare
    list of records written before it, whose check is `_UNCHECKED`.
    ValueError for anything else."""
    if isinstance(data, list):
        return data, _UNCHECKED
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return data["records"], data.get("key_check")
    raise ValueError("not an erasures file")


def export(db: Session) -> List[dict]:
    """Every erasure record of the database, and every change the replay
    makes again (`CHANGES`), as JSON-ready dicts: the merges first, whose
    time no row keeps and which every later erasure in the account they
    went into assumes, then the rest oldest first."""
    from .ids import parse_mem_id

    rows = (db.query(AgentForgetAudit)
              .filter(AgentForgetAudit.scope.in_(SCOPES + ("move",)))
              .order_by(AgentForgetAudit.created_at.asc(), AgentForgetAudit.id.asc()).all())
    timed = []
    for r in rows:
        rec = {"id": str(r.id), "account": str(r.tenant_id),
               "project_id": str(r.project_id) if r.project_id else None,
               "erased_in_project_id": (str(r.erased_in_project_id)
                                        if r.erased_in_project_id else None),
               "scope": r.scope, "end_user_id": r.end_user_id, "target_id": r.target_id,
               "key_check": r.key_check,
               "memories_forgotten": int(r.memories_forgotten or 0),
               "facts_invalidated": int(r.facts_invalidated or 0),
               "created_at": r.created_at.isoformat() if r.created_at else None}
        if r.scope == "memory":
            mid = parse_mem_id(r.target_id)
            rec["erased"] = mid is not None and db.get(AgentMemory, mid) is None
        timed.append(rec)
    # The Cloud's merges and closes (hosted/replay.py); a tree without the
    # Cloud's package has none to export.
    from ..core import extensions

    cloud = extensions.cloud("replay")
    merges, closes = cloud.exported(db) if cloud is not None else ([], [])
    timed.extend(closes)
    timed.sort(key=lambda rec: (rec.get("created_at") or "", rec["id"]))
    return merges + timed


def _end_users(db: Session, tenant_id: UUID, project_id: Optional[UUID]) -> set:
    """Every end user the restored data names in clear, wherever an erasure
    of one reaches: every place of the one list of them
    (services/end_user_places.py), behind the project the erasure was made
    in; a pseudonym an earlier erasure left is nobody to find.

    Until the audit of 19 Oct 2026 only the memories and the facts were
    read, so an end user the dump held only as a queued item (or only in the
    trail) counted as nothing to erase: the worker imported them again, and
    what an erasure renames kept their id in clear. Until the audit of 20
    Oct 2026 the list here was a list of its own, without the consents'
    codes and the lists the trail recorded."""
    from .end_user_places import found

    return found(db, tenant_id, project_id if project_id is not None else EVERY_PROJECT)


def _erased_end_user(db: Session, rec: dict, tenant_id: UUID,
                     project_id: Optional[UUID]) -> Optional[str]:
    """The restored end user an erasure record names: by its pseudonym, or by
    the id itself on a record written before pseudonyms (0.1.27)."""
    from .pseudonyms import keyed_under, pseudonym

    named = rec.get("end_user_id")
    if not named:
        return None
    # Each end user's pseudonym under this account and under the accounts
    # moved into it, whose records keep the pseudonym of the account they
    # were made in (audit of 19 Oct 2026).
    accounts = keyed_under(db, tenant_id)
    for eu in _end_users(db, tenant_id, project_id):
        if eu == named or any(pseudonym(a, eu) == named for a in accounts):
            return eu
    return None


def _erased_in(rec: dict) -> Optional[UUID]:
    """The project the erasure was made in, None for every project of the
    account; from a file written before `erased_in_project_id`, the project
    the record was filed under."""
    key = "erased_in_project_id" if "erased_in_project_id" in rec else "project_id"
    return UUID(rec[key]) if rec.get(key) else None


def _not_recorded(rec: dict) -> bool:
    """An end user's or agent's erasure whose project is not recorded
    (`PROJECT_NOT_RECORDED`), logged by id."""
    if rec.get("scope") not in ("user", "agent") or _erased_in(rec) != PROJECT_NOT_RECORDED:
        return False
    logger.warning("[erasures] %s erasure %s: its project was not recorded; not repeated",
                   rec.get("scope"), rec["id"])
    return True


def _put_back(db: Session, rec: dict, made: Optional[str]) -> None:
    """The original record in place of the one the replay wrote: the same id,
    date and counts, so the proof the erasure answered with holds."""
    from .ids import parse_audit_id

    if made:
        new_id = parse_audit_id(made)
        if new_id is not None:
            db.query(AgentForgetAudit).filter(AgentForgetAudit.id == new_id).delete(
                synchronize_session=False)
    project_id = UUID(rec["project_id"]) if rec.get("project_id") else None
    if project_id is not None and db.get(AgentProject, project_id) is None:
        project_id = None
    db.add(AgentForgetAudit(
        id=UUID(rec["id"]), tenant_id=UUID(rec["account"]), project_id=project_id,
        erased_in_project_id=_erased_in(rec), key_check=rec.get("key_check"),
        scope=rec["scope"], end_user_id=rec.get("end_user_id"), target_id=rec.get("target_id"),
        memories_forgotten=int(rec.get("memories_forgotten") or 0),
        facts_invalidated=int(rec.get("facts_invalidated") or 0),
        created_at=datetime.fromisoformat(rec["created_at"]) if rec.get("created_at") else None))
    db.commit()


def _parts_of_projects(records: List[dict]) -> dict:
    """{record id: the erased project's id} for the end users' records a
    project's erasure filed (scope 'user', the project in `target_id`) whose
    project's record is in the same file."""
    projects = {(rec.get("account"), rec.get("target_id")) for rec in records
                if rec.get("scope") == "project" and rec.get("target_id")}
    return {rec["id"]: rec["target_id"] for rec in records
            if rec.get("scope") == "user" and rec.get("target_id")
            and (rec.get("account"), rec["target_id"]) in projects}


def _drop_the_parts_made_again(db: Session, tenant_id: UUID, project_id: str,
                               records: List[dict], parts: dict) -> None:
    """After a project's erasure is replayed: the end users' records it
    filed for the people the file's own records of that project name, which
    go back as they were (`_put_back`) and stand for them. Not committed:
    `_put_back` commits with the project's record."""
    named = [rec.get("end_user_id") for rec in records
             if parts.get(rec["id"]) == project_id and rec.get("end_user_id")]
    if not named:
        return
    ids = [UUID(rec["id"]) for rec in records]
    (db.query(AgentForgetAudit)
       .filter(AgentForgetAudit.tenant_id == tenant_id, AgentForgetAudit.scope == "user",
               AgentForgetAudit.target_id == project_id,
               AgentForgetAudit.end_user_id.in_(named),
               AgentForgetAudit.id.notin_(ids))
       .delete(synchronize_session=False))


def _refuse_another_key(db: Session, records: List[dict], key_check) -> None:
    """KeyMismatch, before anything is erased, when the records name end
    users by pseudonym that the dump does not already hold and this
    install's pseudonym key cannot be the one they were read under: there is
    none here, or the file's check names another. Erasures of agents,
    projects and memories need no key and do not count, nor do the end
    users' records of a project erased in the file (`_parts_of_projects`),
    whose project's replay finds them by its rows."""
    from .ids import is_pseudonym
    from .pseudonyms import key_check as here

    parts = _parts_of_projects(records)
    pending = [rec for rec in records
               if rec.get("scope") == "user" and is_pseudonym(rec.get("end_user_id"))
               and rec["id"] not in parts
               and db.get(AgentForgetAudit, UUID(rec["id"])) is None]
    if not pending:
        return
    mine = here()
    if mine is None:
        raise KeyMismatch(len(pending), keyless=True)

    def made_under(rec):
        # The record's own check, the key its pseudonym was made under; for
        # a record that has none (made before the audit of 20 Oct 2026 on an
        # install that had no key at the upgrade), the file's.
        return rec.get("key_check") or key_check

    other = [rec for rec in pending
             if made_under(rec) is not _UNCHECKED and made_under(rec) != mine]
    if other:
        raise KeyMismatch(len(other), keyless=False)


def _project_of(db: Session, tenant_id, rec: dict):
    """The record's project when it is this account's, else None."""
    try:
        project = db.get(AgentProject, UUID(rec.get("target_id") or ""))
    except ValueError:
        return None
    return project if project is not None and project.tenant_id == tenant_id else None


def _repeat(db: Session, rec: dict, records: List[dict], parts: dict, out: dict) -> None:
    """`replay` of one record: made again, counted in `out`, its original
    record put back (committed), or nothing when the restored data holds
    it."""
    from . import batch
    from .facts import service as facts
    from .ids import parse_mem_id
    from .projects import delete_project

    def counted(key, scope):
        if scope in CHANGES:
            # A move, a merge, a close: made again, or nothing of it in the
            # restored data; never counted as an erasure.
            key = {"replayed": "changes", "nothing_to_erase": "changes_absent"}.get(key, key)
        out[key][scope] = out[key].get(scope, 0) + 1

    scope = rec.get("scope")
    if scope in ("merge", "close"):
        # The Cloud's (hosted/replay.py). A tree without the Cloud's package
        # cannot make them, and says so: `not_made`, never a silent skip.
        from ..core import extensions

        cloud = extensions.cloud("replay")
        if cloud is None:
            counted("not_made", scope)
            return
        done = (cloud.merge_again if scope == "merge" else cloud.close_again)(db, rec)
        if done == "in_the_dump":
            out["in_the_dump"] += 1
        else:
            counted(done, scope)
        return
    if scope not in SCOPES + CHANGES:
        return
    if rec["id"] in parts:
        # An end user's part of a project's erasure in this file: the
        # project's record erases them (before or after this one, by the
        # file's order), and is the one counted.
        if db.get(AgentForgetAudit, UUID(rec["id"])) is None:
            _put_back(db, rec, None)
        return
    held = db.get(AgentForgetAudit, UUID(rec["id"])) is not None
    if (held or db.get(Tenant, UUID(rec["account"])) is not None) and _not_recorded(rec):
        # Repeated nowhere, and counted every time, a replay run again
        # included: the operator erases the person by hand.
        counted("project_unknown", scope)
        if not held:
            _put_back(db, rec, None)
        return
    if held:
        out["in_the_dump"] += 1
        return
    tenant_id = UUID(rec["account"])
    if db.get(Tenant, tenant_id) is None:
        counted("nothing_to_erase", scope)
        return
    made = None
    if scope in ("user", "agent"):
        if scope == "user" and not rec.get("end_user_id"):
            # Erased without SECRET_KEY: nothing says who it was.
            out["not_found"] += 1
            _put_back(db, rec, None)
            return
        # Where the erasure was made, never where its record was filed
        # since (`_erased_in`). A project the restored data does not hold
        # holds nothing of it: the record's project now, the default one,
        # holds another person who shares the id, if anyone.
        reached = _erased_in(rec)
        project = db.get(AgentProject, reached) if reached is not None else None
        gone = reached is not None and (project is None or project.tenant_id != tenant_id)
        if gone or (scope == "agent" and not rec.get("target_id")):
            counted("nothing_to_erase", scope)
            _put_back(db, rec, None)
            return
        within = reached if reached is not None else EVERY_PROJECT
        if scope == "user":
            eu = _erased_end_user(db, rec, tenant_id, reached)
            if eu is None:
                counted("nothing_to_erase", scope)
                _put_back(db, rec, None)
                return
            made = facts.forget_end_user(db, tenant_id, eu, project_id=within)["audit_id"]
        else:
            made = facts.delete_agent(db, tenant_id, rec["target_id"],
                                      project_id=within)["audit_id"]
    elif scope == "memory":
        mid = parse_mem_id(rec.get("target_id"))
        m = db.get(AgentMemory, mid) if mid else None
        if m is None and mid is not None and rec.get("erased"):
            # Not imported yet when the dump was taken: the dump holds it
            # only as the item of a queued /v1/batch job, which would
            # import it again under the very id erased (audit of 19 Oct 2026).
            # The item is what there is to erase; the erasure's own
            # function scrubs it.
            if batch.erase_memory_item(db, tenant_id, mid, project_id=EVERY_PROJECT):
                db.commit()
                counted("replayed", scope)
            else:
                counted("nothing_to_erase", scope)
            _put_back(db, rec, None)
            return
        # An erasure erases the row a forget had only hidden, too.
        if m is None or m.tenant_id != tenant_id or (
                m.deleted_at is not None and not rec.get("erased")):
            counted("nothing_to_erase", scope)
            _put_back(db, rec, None)
            return
        act = facts.erase_memory if rec.get("erased") else facts.forget_memory
        made = (act(db, tenant_id, mid, project_id=EVERY_PROJECT) or {}).get("audit_id")
    elif scope == "project":
        project = _project_of(db, tenant_id, rec)
        if project is None:
            counted("nothing_to_erase", scope)
            _put_back(db, rec, None)
            return
        # Across regions too: the restore repeats a deletion made before
        # it, which met the rule of the region then (projects.
        # _keep_the_region), and an erasure is repeated whatever the
        # regions are now, or the person comes back in clear.
        made = delete_project(db, tenant_id, project, erase=True,
                              across_regions=True).get("audit_id")
        _drop_the_parts_made_again(db, tenant_id, rec["target_id"], records, parts)
    elif scope == "move":
        # A project deleted without `erase` after the dump: its rows go
        # to the default project again, where the erasures made after
        # the move reached them.
        project = _project_of(db, tenant_id, rec)
        if project is None or project.is_default:
            counted("nothing_to_erase", scope)
            _put_back(db, rec, None)
            return
        delete_project(db, tenant_id, project, erase=False, across_regions=True)
        (db.query(AgentForgetAudit)
           .filter(AgentForgetAudit.tenant_id == tenant_id, AgentForgetAudit.scope == "move",
                   AgentForgetAudit.target_id == rec["target_id"],
                   AgentForgetAudit.id != UUID(rec["id"]))
           .delete(synchronize_session=False))
    counted("replayed", scope)
    _put_back(db, rec, made)


def replay(db: Session, records: Iterable[dict], key_check=_UNCHECKED) -> dict:
    """Repeat the erasures the database does not hold. Returns counts:
    `in_the_dump` (already there), `replayed` and `nothing_to_erase` (the
    tenant, the project or the end user is not in the restored data) by
    scope, `not_found`: end users erased without SECRET_KEY, whose record
    names nobody, and `project_unknown` by scope: erasures of 0.1.1-0.1.11
    whose project is not recorded (`PROJECT_NOT_RECORDED`), not repeated.
    The moves, merges and closes (`CHANGES`) are counted
    apart: `changes` made again, `changes_absent` with nothing of them in
    the restored data, `not_made` a merge into an account it does not hold,
    or a merge or a close on a tree without the Cloud's package, which cannot
    make either.
    `failed`: the records whose repeat raised, each `{id, scope, error}`
    (the record's id, never the person's; the error's classes and codes,
    never its message: log_safe.error), rolled back and left for a run
    again; the others are made all the same. `key_check`: the file's
    (`read_file`); KeyMismatch, with nothing erased, when its end users
    cannot be found under this key."""
    records = list(records)
    _refuse_another_key(db, records, key_check)
    parts = _parts_of_projects(records)
    out = {"in_the_dump": 0, "replayed": {}, "nothing_to_erase": {}, "not_found": 0,
           "project_unknown": {}, "changes": {}, "changes_absent": {}, "not_made": {},
           "failed": []}

    for rec in records:
        # Each record on its own: it ends with its own commit (`_put_back`),
        # and a record whose repeat raises is rolled back, counted by its id
        # and left for a run again, which repeats only what the restored
        # data does not hold yet (a repeat that raised after its erasure
        # committed finds nothing left to erase, and puts its record back).
        # One that raised stopped the run, and every run again stopped at
        # it, so the erasures filed after it, other accounts' too, were
        # never made again (the audit before 1.0.0). Logged by the record's id
        # and its scope, and the exception's classes only: its message can
        # quote a value.
        counts = copy.deepcopy({k: v for k, v in out.items() if k != "failed"})
        try:
            _repeat(db, rec, records, parts, out)
        except Exception as e:  # noqa: BLE001 - counted, logged, and the run goes on
            db.rollback()
            out.update(counts)
            why = log_safe.error(e)
            out["failed"].append({"id": str(rec.get("id")), "scope": rec.get("scope"),
                                  "error": why})
            logger.error("[erasures] %s record %s could not be made again: %s",
                         rec.get("scope"), rec.get("id"), why)
    return out


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)
