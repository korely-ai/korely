"""Management commands.

    python -m korely_agent.manage create-tenant --name "Acme Insurance"
    python -m korely_agent.manage issue-key --tenant <uuid> --name "prod agent"
    python -m korely_agent.manage list-keys --tenant <uuid>
    python -m korely_agent.manage revoke-key --tenant <uuid> --id <uuid>
    python -m korely_agent.manage list-tenants
    python -m korely_agent.manage create-operator --tenant <uuid> --username anna
    python -m korely_agent.manage grant-install-admin --username anna
    python -m korely_agent.manage list-operators
    python -m korely_agent.manage reset-password --username anna
    python -m korely_agent.manage status
    python -m korely_agent.manage reextract --empty
    python -m korely_agent.manage reextract --no-errors --empty
    python -m korely_agent.manage add-webhook --tenant <uuid> --url https://...
    python -m korely_agent.manage list-webhooks
    python -m korely_agent.manage delete-webhook --tenant <uuid> --id <uuid>
    python -m korely_agent.manage rotate-webhook-secret --tenant <uuid> --id <uuid>
    python -m korely_agent.manage reclassify-relations [--tenant <uuid>] [--apply]
    python -m korely_agent.manage review-vocabulary [--tenant <uuid>] [--apply]
    python -m korely_agent.manage review-objects --tenant <uuid> [--user <id>] --cap 1 --out p.json
    python -m korely_agent.manage review-objects --apply p.json
    python -m korely_agent.manage review-subjects --tenant <uuid> --dry-run
    python -m korely_agent.manage review-subjects --tenant <uuid> --cap 0.1 --out s.json
    python -m korely_agent.manage review-subjects --apply s.json
    python -m korely_agent.manage create-tenant --name "Acme" --json
    python -m korely_agent.manage erasures-export > erasures.json
    python -m korely_agent.manage erasures-replay < erasures.json
    python -m korely_agent.manage erasures-superseded < erasures-files.tar
    python -m korely_agent.manage credentials-export > credentials.json
    python -m korely_agent.manage credentials-replay < credentials.json
    python -m korely_agent.manage reseal-secrets

The hosted product exposes an unauthenticated endpoint that mints a tenant and
a key in one call: it is how a stranger gets a free key without talking to
anyone, and for that product it is the front door.

Self-hosted it would be a hole. Anyone who can reach the service could create
themselves a tenant, and the isolation between an agency's clients would be
worth nothing. Provisioning belongs to whoever operates the box, so it is a
command on the box rather than a route on the internet.

`--json`, on the commands that make or list accounts, keys and logins, prints
one JSON object (or one list) on stdout and nothing else: what install.sh reads
(29 Oct 2026). The text is for a person and may change its wording; a program
that scraped it would break on the first rephrasing, and quietly if it read
the wrong column. Refusals stay sentences on stderr with exit status 1, with or
without it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid

from .core.times import aware
from .db import AgentApiKey, AgentFact, AgentMemory, Tenant, get_sessionmaker

from .services import keys as key_service
from .services import operators as operator_service
from .services import webhooks as webhook_service
from .services.facts.temporal import true_at


def _session():
    return get_sessionmaker()()


def _writable_place(path: str):
    """`path` where this process can write it, and a note for the operator
    when it is not where they said. The image keeps /app, the working
    directory, owned by root and read-only to the service's user (the
    Dockerfile), so a bare name or a relative path (`--out p.json`) lands in
    the system's temporary folder instead, said on stderr; an absolute path
    whose folder cannot be written is refused (None, and why)."""
    folder = os.path.dirname(os.path.abspath(path)) or os.sep
    if os.access(folder, os.W_OK):
        return path, None
    if os.path.isabs(path):
        return None, (f"{path}: this process cannot write in {folder}. Name a folder it can "
                      f"({tempfile.gettempdir()}, or one mounted for it).")
    moved = os.path.join(tempfile.gettempdir(), os.path.basename(path))
    return moved, (f"{path} would be written in {folder}, which this process cannot write (the "
                   f"image keeps the application's files read-only): written to {moved} instead.")


def _uuid(value: str, what: str):
    """Parse a uuid, or explain. An operator typing an id at a prompt deserves
    a sentence, not a traceback from the standard library."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        print(f"{value!r} is not a valid {what} id", file=sys.stderr)
        return None


def _lifetime(value: str):
    """`--expires-in`: a number of days (0 expires the key at once, the most
    the API takes is key_service.MAX_LIFETIME_DAYS), or `never`. The help
    used to say "0 means never", which stopped being true when 0 came to
    mean expired (services/keys `_expiry`): an operator following it minted
    a dead key, and there was no way left to mint one that does not expire
    (the conformance check of 2026-09-29)."""
    if str(value).strip().lower() == "never":
        return NEVER
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r}: a number of days, or never")
    most = key_service.MAX_LIFETIME_DAYS
    if not 0 <= days <= most:
        raise argparse.ArgumentTypeError(f"{days}: between 0 and {most} days, or never")
    return days


NEVER = "never"


def _days(expires_in):
    """What create_api_key takes: the default when nothing was said, None for
    never, else the days."""
    if expires_in is None:
        return key_service.DEFAULT_LIFETIME_DAYS
    return None if expires_in == NEVER else expires_in


def _emit(obj) -> None:
    """The one line `--json` prints: the whole answer, on stdout."""
    print(json.dumps(obj))


def _expires(row) -> "str | None":
    """A key's end date as the text prints it, None for never."""
    return f"{row.expires_at:%Y-%m-%d}" if row.expires_at else None


def _audit(db, tenant_id, action: str, key=None, **kw) -> None:
    """A command of this file in the audit trail, as `manage` (2026-10-06,
    the audit of 6 October, row 8): creating a tenant, issuing or revoking a
    key, making an operator or resetting a password reached no trail. An act
    on a key (`key`, its row) is filed in the key's project, as the
    dashboard's and an admin key's are (api/admin, since 1.0.12): up to
    1.0.39 this file filed it in none, so a key of that project did not see
    it in GET /v1/audit (the review of 7 Nov 2026)."""
    from .services import audit

    project_id = key_service.key_project(db, key) if key is not None else None
    audit.record(db, tenant_id, action, actor="manage", project_id=project_id, **kw)


def create_tenant(name: str, expires_in: "int | str | None" = None,
                  as_json: bool = False) -> int:
    try:
        key_service.check_lifetime(_days(expires_in))
    except key_service.LifetimeRefused as e:
        print(f"{e} (`--expires-in`); no account was made", file=sys.stderr)
        return 1
    db = _session()
    try:
        t = Tenant(id=uuid.uuid4(), name=name)
        db.add(t)
        db.commit()
        # Every account gets its Default project here. The model has always
        # said "exactly one is_default", and nothing created it: a brand-new
        # install answered /admin/projects with an empty list, so the screen's
        # project switcher said "Select project" with nothing to select.
        from .services import projects as project_service

        project_service.get_or_create_default_project(db, t.id)
        _row, raw = key_service.create_api_key(
            db, tenant_id=t.id, name=f"{name} default",
            expires_in_days=_days(expires_in))
        _audit(db, t.id, "tenant_create", target_id=str(t.id), meta={"name": t.name})
        _audit(db, t.id, "key_create", key=_row, target_id=str(_row.id),
               meta={"name": _row.name})
        if as_json:
            _emit({"tenant": str(t.id), "name": t.name, "key": raw,
                   "expires": _expires(_row)})
            return 0
        print(f"tenant  {t.id}")
        print(f"name    {t.name}")
        print(f"key     {raw}")
        print(f"expires {_row.expires_at:%Y-%m-%d}" if _row.expires_at else "expires never")
        print()
        print("This key is shown once and stored only as a hash. Copy it now.")
        if _row.expires_at:
            print("It stops working on that date. Your webhooks get two weeks' "
                  "notice; `list-keys` shows the date any time.")
        return 0
    finally:
        db.close()


def issue_key(tenant_id: str, name: str, admin: bool = False,
              expires_in: "int | str | None" = None, as_json: bool = False) -> int:
    tid = _uuid(tenant_id, "tenant")
    if tid is None:
        return 1
    db = _session()
    try:
        t = db.get(Tenant, tid)
        if t is None:
            print(f"no tenant with id {tenant_id}", file=sys.stderr)
            return 1
        scopes = None
        if admin:
            # A separate credential on purpose. The keys an agent runs with
            # cannot reach /admin, so one of them leaking does not hand over
            # the ability to issue more.
            scopes = list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE]
        try:
            _row, raw = key_service.create_api_key(
                db, tenant_id=t.id, name=name, scopes=scopes, expires_in_days=_days(expires_in))
        except key_service.DuplicateKeyNameError:
            # A sentence, not a traceback (the module docstring's rule).
            print(f"this tenant already has a key called {name!r}: pick another --name",
                  file=sys.stderr)
            return 1
        except key_service.TooManyKeys as e:
            print(f"{e} (`list-keys`, then `revoke-key`)", file=sys.stderr)
            return 1
        except key_service.LifetimeRefused as e:
            print(f"{e} (`--expires-in`)", file=sys.stderr)
            return 1
        _audit(db, t.id, "key_create", key=_row, target_id=str(_row.id),
               meta={"name": _row.name, "admin": bool(admin)})
        if as_json:
            _emit({"tenant": str(t.id), "name": _row.name, "key": raw,
                   "expires": _expires(_row), "admin": bool(admin)})
            return 0
        print(raw)
        print(f"expires {_row.expires_at:%Y-%m-%d}" if _row.expires_at else "expires never",
              file=sys.stderr)
        if admin:
            print("This key can administer the account: keys, webhooks, "
                  "projects. Keep it away from the agents.", file=sys.stderr)
        return 0
    finally:
        db.close()


def list_tenants(as_json: bool = False) -> int:
    db = _session()
    try:
        rows = db.query(Tenant).order_by(Tenant.created_at).all()
        if as_json:
            _emit([{"id": str(t.id), "name": t.name, "created": f"{t.created_at:%Y-%m-%d}"}
                   for t in rows])
            return 0
        if not rows:
            print("no tenants yet: create one with `create-tenant`")
        for t in rows:
            print(f"{t.id}  {t.created_at:%Y-%m-%d}  {t.name}")
        return 0
    finally:
        db.close()


def add_webhook(tenant_id: str, url: str, events: str) -> int:
    """Register an endpoint to POST events to.

    It was written before the install served a dashboard, and until it existed
    the events were emitted for nobody: `emit_event` queried a table that
    nothing could add a row to, so the whole feature was inert in the shipped
    product. The Webhooks page does the same now, for the account you signed
    in to; this does it for any account, from a shell.
    """
    tid = _uuid(tenant_id, "tenant")
    if tid is None:
        return 1
    db = _session()
    try:
        if db.get(Tenant, tid) is None:
            print(f"no tenant with id {tenant_id}", file=sys.stderr)
            return 1
        chosen = [e.strip() for e in events.split(",") if e.strip()] if events else ["*"]
        try:
            # The same rule as POST /admin/webhooks, from the same function.
            ep = webhook_service.create_endpoint(db, tid, url=url, events=chosen)
        except webhook_service.InvalidWebhookEvents as e:
            print(str(e), file=sys.stderr)
            return 1
        except webhook_service.UnsafeWebhookUrl as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        print(f"endpoint {ep.id}")
        print(f"url      {ep.url}")
        print(f"events   {', '.join(ep.events)}")
        print(f"secret   {ep.secret}")
        print()
        _how_to_verify()
        return 0
    finally:
        db.close()


def _how_to_verify() -> None:
    print("This secret is shown once. Deliveries are signed per Standard Webhooks:")
    print("HMAC-SHA256 over `{webhook-id}.{webhook-timestamp}.{body}`, keyed by the")
    print("base64-decoded part of the secret after `whsec_`, base64, prefixed 'v1,'.")
    print("The official library does all of it: pip install standardwebhooks")
    print("(or npm install standardwebhooks), then Webhook(secret).verify(body, headers).")


def rotate_webhook_secret(tenant_id: str, endpoint_id: str) -> int:
    """A new secret for an endpoint, keeping everything else.

    For endpoints made before 0.1.11, whose secret is the whole string as
    the HMAC key: the official Standard Webhooks libraries cannot verify
    those, and since 0.2.0 they are not sent (each delivery is refused at
    every attempt, with the reason, on the Webhooks page). This is the way
    to a secret the libraries take without deleting and re-adding the
    endpoint, which would lose its id and any delivery still queued: what
    is queued goes out under the new secret. There is one secret at a time:
    deliveries fail verification until the receiver has the new one, and
    are retried meanwhile (services/webhooks.rotate_secret).
    """
    tid = _uuid(tenant_id, "tenant")
    eid = _uuid(endpoint_id, "endpoint")
    if tid is None or eid is None:
        return 1
    db = _session()
    try:
        ep = webhook_service.rotate_secret(db, tid, eid)
        if ep is None:
            print("no such endpoint for that tenant", file=sys.stderr)
            return 1
        print(f"endpoint {ep.id}")
        print(f"url      {ep.url}")
        print(f"secret   {ep.secret}")
        print()
        print("The old secret stopped working just now: give the receiver this one.")
        _how_to_verify()
        return 0
    finally:
        db.close()


def list_webhooks(tenant_id: str | None) -> int:
    db = _session()
    try:
        if tenant_id:
            tid = _uuid(tenant_id, "tenant")
            if tid is None:
                return 1
            tenants = [db.get(Tenant, tid)] if db.get(Tenant, tid) else []
        else:
            tenants = db.query(Tenant).order_by(Tenant.created_at).all()
        found = False
        for t in tenants:
            for ep in webhook_service.list_endpoints(db, t.id):
                found = True
                state = "active" if ep.active else "inactive"
                old = ("  not sent (secret of before 0.1.11): rotate-webhook-secret"
                       if webhook_service.is_legacy_secret(ep.secret) else "")
                print(f"{ep.id}  {state:8}  {t.name}  {ep.url}  [{', '.join(ep.events)}]{old}")
        if not found:
            print("no webhook endpoints: add one with `add-webhook`")
        return 0
    finally:
        db.close()


def delete_webhook(tenant_id: str, endpoint_id: str) -> int:
    tid = _uuid(tenant_id, "tenant")
    eid = _uuid(endpoint_id, "endpoint")
    if tid is None or eid is None:
        return 1
    db = _session()
    try:
        if webhook_service.delete_endpoint(db, tid, eid):
            print("deleted")
            return 0
        print("no such endpoint for that tenant", file=sys.stderr)
        return 1
    finally:
        db.close()



def list_keys(tenant_id: str) -> int:
    """The keys of one tenant, with enough to recognise one without revealing it.

    Four characters of a 256-bit secret is what Stripe shows, and it is the
    only way an operator can answer "which of these is the one on the staging
    box" without keeping a copy of the key somewhere it should not be.
    """
    tid = _uuid(tenant_id, "tenant")
    if tid is None:
        return 1
    db = _session()
    try:
        rows = (db.query(AgentApiKey)
                  .filter(AgentApiKey.tenant_id == tid)
                  .order_by(AgentApiKey.created_at)
                  .all())
        if not rows:
            print("no keys for this tenant")
            return 0
        for r in rows:
            state = key_service.state_of(r)
            if state == "revoked":
                detail = f"revoked {r.revoked_at:%Y-%m-%d}"
            elif state == "refused":
                detail = "refused: the Cloud's prefix"
            elif state == "expired":
                detail = f"expired {r.expires_at:%Y-%m-%d}"
            elif r.expires_at is not None:
                detail = f"until {r.expires_at:%Y-%m-%d}"
            else:
                detail = "never expires"
            used = r.last_used_at.strftime("%Y-%m-%d %H:%M") if r.last_used_at else "never used"
            print(f"{r.id}  {r.prefix}...{r.last_four or '????'}  {detail:<20}  {used:<17}  {r.name or ''}")
        return 0
    finally:
        db.close()


def revoke_key(tenant_id: str, key_id: str) -> int:
    """Take a key out of service.

    `revoke_api_key` has existed in the service layer since the beginning and
    nothing could reach it: the only way to stop a leaked key on a self-hosted
    box was an UPDATE typed by hand. Revocation is a soft one, so the row stays
    and the audit trail still shows which key wrote what.
    """
    tid = _uuid(tenant_id, "tenant")
    kid = _uuid(key_id, "key")
    if tid is None or kid is None:
        return 1
    db = _session()
    try:
        if key_service.revoke_api_key(db, key_id=kid, tenant_id=tid):
            _audit(db, tid, "key_revoke", key=db.get(AgentApiKey, kid), target_id=str(kid))
            print(f"revoked {kid}")
            return 0
        # Not found, not owned by this tenant, and already revoked are one
        # message on purpose: a wrong tenant id must not confirm that a key id
        # exists somewhere else.
        print("no live key with that id for this tenant", file=sys.stderr)
        return 1
    finally:
        db.close()


# ── the people who log in ───────────────────────────────────────────────────

def create_operator(tenant_id: str, username: str, install_admin: bool = False,
                    as_json: bool = False) -> int:
    """Make a login. The password is generated, shown once and stored hashed.

    There is no default password and no way to set one. `admin/admin` is how a
    self-hosted product ends up indexed, and every install that ships with one
    has somebody who meant to change it.

    The first login of an install, or one made with `--install-admin`, also
    sets what is the whole install's (the model provider, its key, the
    models) once it holds more than one account (`Operator.install_admin`)."""
    tid = _uuid(tenant_id, "tenant")
    if tid is None:
        return 1
    db = _session()
    try:
        if db.get(Tenant, tid) is None:
            print(f"no tenant with id {tenant_id}", file=sys.stderr)
            return 1
        if operator_service.by_username(db, username) is not None:
            print(f"there is already an operator called {username!r}", file=sys.stderr)
            return 1
        from .db import Operator

        first = db.query(Operator.id).first() is None
        row, password = operator_service.create(db, tenant_id=tid, username=username)
        if first or install_admin:
            row.install_admin = True
            db.commit()
        _audit(db, tid, "setting_change", target_id=row.username,
               meta={"setting": "operator", "op": "create",
                     **({"install_admin": True} if row.install_admin else {})})
        if as_json:
            _emit({"username": row.username, "password": password, "tenant": str(tid),
                   "install_admin": bool(row.install_admin)})
            return 0
        print(f"username  {row.username}")
        print(f"password  {password}")
        print()
        print("This password is shown once and stored only as a hash. Copy it now.")
        return 0
    finally:
        db.close()


def grant_install_admin(username: str, revoke: bool = False) -> int:
    """Name (or, with `revoke`, un-name) the person who sets the install's
    model provider, its key and the models when it holds several accounts."""
    db = _session()
    try:
        op = operator_service.by_username(db, username)
        if op is None:
            print(f"no operator called {username!r}", file=sys.stderr)
            return 1
        op.install_admin = not revoke
        db.commit()
        _audit(db, op.tenant_id, "setting_change", target_id=op.username,
               meta={"setting": "install_admin", "op": "revoke" if revoke else "grant"})
        print(f"{op.username} {'no longer sets' if revoke else 'now sets'} the install's model "
              f"provider, its key and the models")
        return 0
    finally:
        db.close()


def list_operators(tenant_id: str | None = None, as_json: bool = False) -> int:
    from .db import Operator
    from .services import operators as operator_service

    db = _session()
    try:
        q = db.query(Operator)
        if tenant_id:
            tid = _uuid(tenant_id, "tenant")
            if tid is None:
                return 1
            q = q.filter(Operator.tenant_id == tid)
        rows = q.order_by(Operator.created_at).all()
        if as_json:
            _emit([{"username": r.username, "tenant": str(r.tenant_id),
                    "active": r.disabled_at is None,
                    "last_login": r.last_login_at.isoformat() if r.last_login_at else None}
                   for r in rows])
            return 0
        if not rows:
            print("no operators yet: create one with `create-operator`")
        for r in rows:
            state = "disabled" if r.disabled_at else "active"
            last = r.last_login_at.strftime("%Y-%m-%d %H:%M") if r.last_login_at else "never"
            locked = ""
            locks = operator_service.locks_of(db, r.username)
            if locks:
                last_lock = max(until for _ip, until in locks)
                locked = (f"  LOCKED from {len(locks)} address"
                          f"{'es' if len(locks) > 1 else ''} until {last_lock:%H:%M}")
            print(f"{r.username:<20} {state:<9} last login {last:<17} {r.tenant_id}{locked}")
        return 0
    finally:
        db.close()


def reset_password(username: str) -> int:
    """A new password, shown once. Also ends every session that operator has:
    resetting a password because it was shared is pointless if the sessions it
    opened stay open."""
    db = _session()
    try:
        op = operator_service.by_username(db, username)
        if op is None:
            print(f"no operator called {username!r}", file=sys.stderr)
            return 1
        password = operator_service.generate_password()
        operator_service.set_password(db, op, password)
        ended = operator_service.logout_everywhere(db, op.id)
        _audit(db, op.tenant_id, "setting_change", target_id=op.username,
               meta={"setting": "operator", "op": "password", "sessions_ended": ended})
        print(f"username  {op.username}")
        print(f"password  {password}")
        if ended:
            print(f"({ended} open session(s) ended)")
        print()
        print("Shown once. Copy it now.")
        return 0
    finally:
        db.close()


def reextract(errors: bool = True, empty: bool = False, limit: int = 0) -> int:
    """Put memories back in the queue so the worker tries them again.

    The occasion was 2026-09-12: a brand-new Google account could not use the
    configured model, every extraction failed, and the memories were filed as
    processed. The swallowing is fixed, so failures now land at 'error' rather
    than 'done' - but "I have fixed the cause, now redo the work" is a thing an
    operator needs on any install, and there was no way to say it.

    --errors   memories the extraction gave up on, and the memories and facts
               whose vector gave up (services/vectors.py): without one a
               memory is never found by search and a fact never ranked by
               meaning, and until 2026-09-28 nothing here could queue them.
               On by default; `--no-errors` leaves them where they are
    --empty    memories marked done that produced no fact at all. Usually
               correct (not every sentence carries one), so this is opt-in.
    """
    from .db import AgentFact, AgentMemory
    from .services import vectors

    db = _session()
    try:
        n = 0
        if errors:
            q = db.query(AgentMemory).filter(AgentMemory.graph_sync_status == "error")
            if limit:
                q = q.limit(limit)
            rows = q.all()
            for m in rows:
                m.graph_sync_status = "pending"
            n += len(rows)
            print(f"{len(rows)} failed -> queued")
            mems, facts = vectors.requeue_given_up(db, limit=limit)
            n += mems + facts
            print(f"{mems} memories and {facts} facts without a vector -> queued")

        if empty:
            # NOT EXISTS, not NOT IN (2026-10-06): a fact written with POST
            # /v1/facts has no source memory, and `id NOT IN (..., NULL)` is
            # never true, so one such fact made this find nothing and print
            # "nothing to redo" (the audit of 6 October, row 9). A memory that
            # only reconfirmed a fact produced it too: the source rule of
            # memory_core (`store.source_condition`), correlated.
            from sqlalchemy import String, and_, cast, exists, or_

            produced = exists().where(
                AgentFact.tenant_id == AgentMemory.tenant_id,
                or_(AgentFact.source_memory_id == AgentMemory.id,
                    and_(AgentFact.observation_count > 1,
                         cast(AgentFact.source_memory_ids, String).like(
                             "%" + cast(AgentMemory.id, String) + "%"))))
            q = (db.query(AgentMemory)
                   .filter(AgentMemory.graph_sync_status == "done",
                           AgentMemory.deleted_at.is_(None),
                           ~produced))
            if limit:
                q = q.limit(limit)
            rows = q.all()
            for m in rows:
                m.graph_sync_status = "pending"
            n += len(rows)
            print(f"{len(rows)} without any fact -> queued")

        db.commit()
        if n:
            print()
            print("The worker picks them up within half a minute. `status` shows "
                  "how many are left.")
        else:
            print("nothing to redo")
        return 0
    finally:
        db.close()


def _ago(now, then) -> str:
    if then is None:
        return "never"
    secs = int((now - aware(then)).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def reclassify_relations(tenant_id: str | None = None, apply: bool = False) -> int:
    """Ask again whether each relation an account has learned holds one value.

    The occasion was I1, the blind install of 0.1.13 (2026-09-28): the
    question was asked once per relation and kept, it came back "many" for
    `subscribes_to` about once in five, and in those accounts no plan change
    ever closed the old plan, for any customer, from then on. memory_core
    2.1.0 asks it about the kind of value the relation was seen with, by a
    majority of three; a relation learned from now on gets that at its first
    write, and this is how the ones learned before get it too. Only relations
    stored with an example value are asked: without one there is only the
    general question, the one that was wrong.

    A dry run unless --apply, because it calls the model (two or three short
    calls per relation, billed to the key in force) and because what it
    changes decides which facts later close.
    """
    from .services import llm_usage, predicates

    db = _session()
    try:
        if tenant_id is not None:
            tid = _uuid(tenant_id, "tenant")
            if tid is None:
                return 1
            tenants = db.query(Tenant).filter(Tenant.id == tid).all()
            if not tenants:
                print(f"no tenant {tenant_id}; list-tenants has them", file=sys.stderr)
                return 1
        else:
            tenants = db.query(Tenant).order_by(Tenant.created_at).all()
        word = {True: "one value", False: "many values", None: "undecided"}
        changes = 0
        for t, project_id, end_user_id, where in _vocabularies(db, tenants):
            try:
                with llm_usage.attributed(t.id, stage=llm_usage.PREDICATES):
                    r = predicates.reclassify(db, t.id, apply=apply, project_id=project_id,
                                              end_user_id=end_user_id)
            except RuntimeError as e:
                # The project's region decides the writer (predicates._asked_in):
                # a Google key is not the answer to a European project's.
                print(f"{e}. Save its writer in the dashboard (Settings, Writing "
                      "model) or in .env, then run this again.", file=sys.stderr)
                db.rollback()
                return 1
            asked = len(r["changed"]) + len(r["same"]) + len(r["skipped"])
            print(f"{t.name} ({t.id}), {where}: {asked} relations")
            for canonical, old, new in r["changed"]:
                print(f"  {canonical:30s} {word[old]} -> {word[new]}"
                      f"   {'changed' if apply else 'would change'}")
            if r["skipped"]:
                print(f"  not asked, the model did not answer: {', '.join(r['skipped'])}"
                      " (run it again)")
            changes += len(r["changed"])
        if apply:
            db.commit()
            print(f"{changes} changed.")
        else:
            db.rollback()
            print(f"dry run: {changes} would change and nothing was written; --apply writes them.")
        return 0
    finally:
        db.close()


def _vocabularies(db, tenants):
    """(tenant, project_id, end_user_id, where) for every vocabulary of these
    accounts: one per end user of a project since 18 Oct 2026
    (services/predicates.end_user_vocabulary), the facts of no project and of no end
    user included when there are some."""
    from .db import AgentProject, PredicateCluster

    for t in tenants:
        keys = [k for k in db.query(PredicateCluster.project_id, PredicateCluster.end_user_id)
                .filter(PredicateCluster.tenant_id == t.id).distinct()]
        names = {p.id: p.name for p in db.query(AgentProject)
                 .filter(AgentProject.tenant_id == t.id)}
        for pid, uid in sorted(keys, key=lambda k: (k[0] is not None, names.get(k[0], ""), str(k[0]),
                                                   k[1] is not None, k[1] or "")):
            project = f"project {names.get(pid, pid)}" if pid is not None else "the facts of no project"
            user = f"end user {uid}" if uid is not None else "no end user"
            yield t, pid, uid, f"{project}, {user}"


def review_vocabulary(tenant_id: str | None = None, apply: bool = False, votes: int = 3) -> int:
    """Ask again whether each phrase an account learned as a variant of a
    relation is that relation.

    A phrase is merged into a relation the first time it is seen, by a choice
    among the nearest ones, and kept for good. On the LoCoMo bench (gpt-oss
    writer, 2,632 phrases, 1,423 under another name) some merges change what a
    fact says: "visited" under `plans_to_visit`, "mentors" under
    `receives_support_from`. memory_core 3.3.0 asks the same question again,
    with the one relation the phrase sits in; a phrase the model separates is
    born under its own name and its facts move with it. A phrase said only in
    the past is not asked: it sits in its present form's relation on purpose.

    A dry run unless --apply, because it calls the model (one short call per
    phrase, billed to the key in force) and because what it moves decides
    which facts later meet. A closure between the two sides is not reopened:
    it is printed, for somebody to read.
    """
    from .services import llm_usage, predicates

    db = _session()
    try:
        if tenant_id is not None:
            tid = _uuid(tenant_id, "tenant")
            if tid is None:
                return 1
            tenants = db.query(Tenant).filter(Tenant.id == tid).all()
            if not tenants:
                print(f"no tenant {tenant_id}; list-tenants has them", file=sys.stderr)
                return 1
        else:
            tenants = db.query(Tenant).order_by(Tenant.created_at).all()
        moved = 0
        for t, project_id, end_user_id, where in _vocabularies(db, tenants):
            try:
                with llm_usage.attributed(t.id, stage=llm_usage.PREDICATES):
                    r = predicates.review_variants(db, t.id, apply=apply, votes=votes,
                                                   project_id=project_id, end_user_id=end_user_id)
            except RuntimeError as e:
                # The project's region decides the writer (predicates._asked_in):
                # a Google key is not the answer to a European project's.
                print(f"{e}. Save its writer in the dashboard (Settings, Writing "
                      "model) or in .env, then run this again.", file=sys.stderr)
                db.rollback()
                return 1
            asked = len(r["separated"]) + len(r["same"]) + len(r["skipped"])
            print(f"{t.name} ({t.id}), {where}: {asked} phrases asked, {len(r['to_the_past'])} "
                  "said only in the past and not asked")
            for phrase, relation, facts in r["separated"]:
                print(f"  {phrase:30s} out of {relation:30s} {facts} facts"
                      f"   {'separated' if apply else 'would be separated'}")
            if r["skipped"]:
                print("  not asked, the model did not answer: "
                      + ", ".join(f"{p} ({rel})" for p, rel in r["skipped"]) + " (run it again)")
            for closed, closing in r["closings_to_reread"]:
                print(f"  to read: fact {closed} was closed by {closing}, now in another relation")
            moved += len(r["separated"])
        if apply:
            db.commit()
            print(f"{moved} separated.")
        else:
            db.rollback()
            print(f"dry run: {moved} would be separated and nothing was written; --apply writes them.")
        return 0
    finally:
        db.close()


def _apply_object_links(db, apply_file: str) -> int:
    """`review-objects --apply <file>`: exactly the links of a dry run's
    file, committed, then one trail row each (facts/objects.apply)."""
    from .services.facts import objects as obj

    with open(apply_file, encoding="utf-8") as f:
        records = json.load(f).get("proposals") or []
    results = obj.apply(db, records)
    db.commit()
    obj.audit_applied(db, results)
    for res in results:
        r = res["record"]
        print(f"  {r['name']!r:40s} = {r['same_as']!r:40s} {res['outcome']}"
              + (f" (link {res['link_id']})" if res["link_id"] else ""))
    print(f"{sum(1 for r in results if r['link_id'])} linked of {len(results)} proposed.")
    return 0


def _the_tenants(db, tenant_id: str | None):
    """One account (`--tenant`) or every one, oldest first; None after
    saying why when the id is not an account's."""
    if tenant_id is None:
        return db.query(Tenant).order_by(Tenant.created_at).all()
    tid = _uuid(tenant_id, "tenant")
    if tid is None:
        return None
    tenants = db.query(Tenant).filter(Tenant.id == tid).all()
    if not tenants:
        print(f"no tenant {tenant_id}; list-tenants has them", file=sys.stderr)
        return None
    return tenants


def review_subjects(tenant_id: str | None = None, *, every: bool = False, project: str | None = None,
                    dry_run: bool = False, cap: float | None = None, out: str | None = None,
                    apply_file: str | None = None, while_off: bool = False) -> int:
    """Ask whether two subjects of a memory with no end user (a team's, an
    agent's notes) are one thing ("Cloud di Korely" and "Korely Cloud"), and
    link them in the judge's wall; or write exactly the links a run proposed
    (memory_core 6.20.0, `objects.review_subjects`; services/facts/subjects.py).

    Three steps, so that what is written is what was read:
      `--dry-run` calls no model and costs nothing: for each wall (a project
      with facts of no end user), the pairs that would be asked, with their
      distance, the facts of each side, and the size of the questions;
      `--out <file> --cap <amount>` asks the decisions model of the
      project's region (core/llm.decisions_provider: OpenRouter, Global
      only; a European project is skipped, never asked elsewhere), two
      readings for each subject with candidates, billed under the
      operator's stage of the account it reads, until the next question
      would pass `--cap` (LLM_RATES must price the model); a provider that
      does not answer stops the run, deferred. Writes NOTHING in the
      database, and the proposals to the file: the memory's names and
      facts, the operator's to read and then delete;
      `--apply <file>` writes exactly those links, each while both names are
      still the subject of an open fact, under the wall's lock, calls no
      decisions model, leaves one row in the account's trail per link; then
      judges the open facts of each group against the earlier facts of its
      other names (`subjects.rejudge`, the judge of the project's region,
      one call per fact that has any). A link is taken back by DELETE
      /admin/object-links/{id}, which gives back what the judge closed
      through it.
    One account (`--tenant`, narrowed by `--project`) or, said so, `--all`.
    The run and the apply are refused while the switch is off
    (services/facts/subjects.SAME_SUBJECT_REVIEW) unless `--while-off`: a
    switch off by default goes on only after a measurement (CONTRIBUTING.md).
    The dry run always runs."""
    from .services import llm_usage
    from .services.facts import subjects as sub
    from .services.sentinels import EVERY_PROJECT

    if not dry_run and not sub.SAME_SUBJECT_REVIEW and not while_off:
        print("the same-subject switch is off (services/facts/subjects.SAME_SUBJECT_REVIEW): --dry-run "
              "runs, or pass --while-off to ask the model anyway", file=sys.stderr)
        return 1
    if (tenant_id is None) == (not every) and apply_file is None:
        print("say which account: --tenant <uuid>, or --all", file=sys.stderr)
        return 1
    if every and not dry_run and apply_file is None:
        # A run sends the names and facts of the accounts it reads, and up
        # to `objects.WORDS_MAX` characters of the memory's words around
        # each name, to the decisions model (`core/llm.decisions_provider`:
        # OpenRouter, and behind it the model's own provider, TypeSafe for
        # Jev). Whoever runs this installation must name those services among
        # the processors in their own privacy notice, for the people whose
        # memories these are, before that text leaves the machine: so a run
        # reads one account at a time, each by its operator's choice, never
        # every account at once.
        from .core.llm import DECISIONS_MODEL
        from .services.memory_core.objects import WORDS_MAX

        print(f"a run asks the model one account at a time (--tenant <uuid>): it sends that account's "
              f"names and facts, and up to {WORDS_MAX} characters of the memory's words around each "
              f"name, to the decisions model {DECISIONS_MODEL} through OpenRouter, services your own "
              f"privacy notice must name among its processors; --all --dry-run calls no model",
              file=sys.stderr)
        return 1
    db = _session()
    try:
        if apply_file is not None:
            return _apply_subject_links(db, apply_file, while_off=while_off)
        tenants = _the_tenants(db, tenant_id)
        if tenants is None:
            return 1
        within = EVERY_PROJECT
        if project is not None:
            within = _uuid(project, "project")
            if within is None:
                return 1
        if dry_run:
            return _subjects_dry_run(db, tenants, within)
        if out is None or cap is None:
            print("a run needs --out <file> and --cap <amount> (or --dry-run, which costs nothing)",
                  file=sys.stderr)
            return 1
        if cap <= 0:
            print("--cap must be above 0: a run with no money to spend asks nothing", file=sys.stderr)
            return 1
        out, note = _writable_place(out)
        if note:
            print(note, file=sys.stderr)
        if out is None:
            return 1
        from .config import get_settings
        from .services.usage_report import parse_rates

        card = parse_rates(get_settings().llm_rates)
        if not card:
            print("LLM_RATES has no rate card: the run's cost cannot be counted against --cap",
                  file=sys.stderr)
            return 1
        spent = _Spending(card, cap)
        proposals: list = []
        stopped = deferred = False
        for t in tenants:
            for project_id in sub.walls_with_subjects(db, t.id, project_id=within):
                where = f"project {project_id}" if project_id else "no project"
                try:
                    with llm_usage.attributed(t.id, stage=llm_usage.OPERATOR) as scope:
                        r = sub.review(db, t.id, project_id, dry_run=False,
                                       before_each=spent.before_each_of(scope))
                        spent.counted(scope)
                except sub.RegionRefused as e:
                    db.rollback()
                    print(f"{t.name} ({t.id}), {where}: not asked, {e}", file=sys.stderr)
                    continue
                db.rollback()   # nothing of a run stays in the database
                print(f"{t.name} ({t.id}), {where}: {r['subjects']} subjects, {r['asked']} asked"
                      + (" (more subjects than one run reads)" if r["more"] else ""))
                for p in r["proposals"]:
                    print(f"  {p.name!r:40s} = {p.canonical!r:40s} proposed")
                    proposals.append(sub.proposal_record(t.id, project_id, p))
                if r["unsure"]:
                    print(f"  unsure, kept apart: {len(r['unsure'])}")
                if r["deferred"]:
                    deferred = True
                    print(f"  deferred, the model gave no usable answer ({r['deferred']}): run it again",
                          file=sys.stderr)
                if r["stopped"] or r["deferred"] or spent.unpriced:
                    stopped = True
                    break
            if stopped:
                break
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"proposals": proposals}, f, ensure_ascii=False, indent=1)
        print(f"{len(proposals)} links proposed, written to {out} (it holds the memory's names: delete "
              f"it after the apply); cost {spent.total:.4f} {card.get('currency', '')}"
              + (": deferred, run it again" if deferred else "")
              + (": stopped before passing --cap" if stopped and not deferred and not spent.unpriced else "")
              + (": stopped, a model the rate card does not price" if spent.unpriced else "")
              + f". Nothing was written; --apply {out} writes exactly these.")
        return 0
    finally:
        db.close()


def _subjects_dry_run(db, tenants, within) -> int:
    """`review-subjects --dry-run`: the pairs, no model, nothing written."""
    from .services.facts import subjects as sub

    pairs = questions = characters = 0
    for t in tenants:
        for project_id in sub.walls_with_subjects(db, t.id, project_id=within):
            r = sub.review(db, t.id, project_id, dry_run=True)
            db.rollback()
            where = f"project {project_id}" if project_id else "no project"
            print(f"{t.name} ({t.id}), {where}: {r['subjects']} subjects admitted, "
                  f"{r['questions']} would be asked" + (" (more subjects than one run reads)" if r["more"] else ""))
            for p in r["pairs"]:
                d = f"{p.distance:.3f}" if p.distance is not None else "-"
                print(f"  {p.name!r:40s} ~ {p.candidate!r:40s} distance {d}, facts {p.facts} / {p.candidate_facts}")
            pairs += len(r["pairs"])
            questions += r["questions"]
            characters += r["characters"]
    print(f"dry run: {pairs} pairs in {questions} subjects, at most {2 * questions} model calls and "
          f"{characters} characters of questions (about {characters // 4} tokens, characters / 4, an "
          "estimate). No model was called and nothing was written.")
    return 0


def _apply_subject_links(db, apply_file: str, *, while_off: bool) -> int:
    """`review-subjects --apply <file>`: exactly the links of a run's file,
    committed under each wall's lock, one trail row each, then the judgement
    of each group (`subjects.rejudge`), fact by fact."""
    from uuid import UUID

    from .services import audit as audit_service, llm_usage
    from .services.facts import subjects as sub

    with open(apply_file, encoding="utf-8") as f:
        records = json.load(f).get("proposals") or []
    results = sub.apply(db, records)
    db.commit()
    for res in results:
        r = res["record"]
        print(f"  {r['name']!r:40s} = {r['same_as']!r:40s} {res['outcome']}"
              + (f" (link {res['link_id']})" if res["link_id"] else ""))
        if res["link_id"]:
            audit_service.record(db, UUID(r["account_id"]), audit_service.SETTING_CHANGE, actor="manage",
                                 project_id=UUID(r["project_id"]) if r.get("project_id") else None,
                                 target_id=res["link_id"],
                                 meta={"setting": "object_link", "op": "create", "by": "review-subjects"})
    print(f"{sum(1 for r in results if r['link_id'])} linked of {len(results)} proposed.")
    # Each group once, however many of its links the file wrote: the
    # judgement weighs every fact of the group (subjects.rejudge).
    judged: set = set()
    for res in results:
        if res["outcome"] not in ("created", "same"):
            continue
        r = res["record"]
        tid = UUID(r["account_id"])
        pid = UUID(r["project_id"]) if r.get("project_id") else None
        group = (tid, pid, tuple(sub.group_of(db, tid, pid, r["same_as"])))
        if group in judged:
            continue
        judged.add(group)
        with llm_usage.attributed(tid, stage=llm_usage.OPERATOR):
            j = sub.rejudge(db, tid, pid, r["same_as"], while_off=while_off)
        print(f"  judged again: {j['judged']} of {j['facts']} facts of {r['same_as']!r}'s names, "
              f"{j['closed']} closings through the link"
              + (f"; deferred ({j['deferred']}): --apply the same file again" if j["deferred"] else ""))
    return 0


class _Spending:
    """What a review run has spent against its `--cap` (review-objects,
    review-subjects): the cost of the calls of each account's scope
    (`llm_usage.attributed`), priced by the rate card (`usage_report.
    cost_of`), or what the provider said a call cost when that is more (its
    own currency, OpenRouter's dollars, counted at par: the higher of the
    two, so the cap stops early rather than late). A call that nothing
    prices stops the run: a model the card does not price and whose answer
    named no cost, or an answer that measured nothing (no tokens, no cost),
    which would otherwise count as free."""

    def __init__(self, card: dict, cap: float):
        self.card, self.cap = card, cap
        self.total = 0.0
        self.largest = 0.0
        self.unpriced = False
        self._seen: dict = {}

    def counted(self, scope) -> None:
        from .services.usage_report import cost_of

        for c in scope.calls[self._seen.get(id(scope), 0):]:
            if not getattr(c, "measured", True):
                self.unpriced = True
                continue
            priced = cost_of(self.card, c.model, c.prompt_tokens, c.completion_tokens)
            said = getattr(c, "cost", None)
            known = [v for v in (priced, said) if v is not None]
            if not known:
                self.unpriced = True
                continue
            self.total += max(known)
        self._seen[id(scope)] = len(scope.calls)

    def before_each_of(self, scope):
        """The `before_each` of one scope's review: counts what was spent,
        and says whether the next question may be asked."""
        before = {"total": self.total}

        def before_each() -> bool:
            self.counted(scope)
            last = self.total - before["total"]
            before["total"] = self.total
            self.largest = max(self.largest, last)
            # Strictly below: the next question is asked only while what it
            # may cost leaves the run under the cap, so `--cap 0` asks
            # nothing (the review of the rebuild, P3.8). The first
            # question's cost is not known before it: a cap below one
            # question's can be passed by that one.
            return not self.unpriced and self.total + self.largest < self.cap
        return before_each


def review_objects(tenant_id: str | None = None, *, every: bool = False, project: str | None = None,
                   user: str | None = None, cap: float | None = None, out: str | None = None,
                   apply_file: str | None = None, while_off: bool = False) -> int:
    """Ask whether two objects an end user's memory holds are one thing named
    in two languages or forms ("Italia" and "Italy", "Padova" and "Padua"),
    and link them; or write exactly the links a dry run proposed.

    memory_core 6.1.0 asks it at the write for a new object, behind a switch
    that is off (services/facts/objects.SAME_OBJECT); the objects written
    before, or with the switch off, stay two until this asks. Each object of
    an open fact, in the order the memory first named it, is offered with the
    objects of the same relation family named before it that its first fact
    reads alike to (never people); the writer of the project's region
    describes each, then answers twice, framed two ways. A union is a LINK
    (facts/links.py), never an alias: no fact is rewritten, and DELETE
    /admin/object-links/{id} takes it back exactly.

    Two steps, so that what is written is what was read (the review of
    6.1.0, P2.6):
      the dry run, `--out <file> --cap <amount>`, calls the model (three
      questions for every four objects, billed under the operator's stage,
      `llm_usage.OPERATOR`, of the account it reads) until the next question
      would pass `--cap` (in the currency of LLM_RATES, which must price the
      model), writes NOTHING in the database and the proposals to the file:
      the names and the writer's descriptions, an end user's words, the
      operator's to read and then delete;
      `--apply <file>` writes exactly those links, each while both names are
      still named by an open fact, calls no model, and leaves one row in the
      account's trail per link.
    One account (`--tenant`, narrowed by `--project` and `--user`) or, said
    so, `--all`. Refused while the switch is off unless `--while-off`: a
    switch off by default goes on only after a measurement (CONTRIBUTING.md).
    """
    from .services import llm_usage
    from .services.facts import objects as obj
    from .services.sentinels import EVERY_PROJECT

    if not obj.SAME_OBJECT and not while_off:
        print("the same-object switch is off (services/facts/objects.SAME_OBJECT): pass --while-off "
              "to run it anyway", file=sys.stderr)
        return 1
    db = _session()
    try:
        if apply_file is not None:
            return _apply_object_links(db, apply_file)
        if out is None or cap is None:
            print("a dry run needs --out <file> and --cap <amount>", file=sys.stderr)
            return 1
        if cap <= 0:
            print("--cap must be above 0: a dry run with no money to spend asks nothing", file=sys.stderr)
            return 1
        # Where the proposals can land, settled before a cent is spent.
        out, note = _writable_place(out)
        if note:
            print(note, file=sys.stderr)
        if out is None:
            return 1
        if (tenant_id is None) == (not every):
            print("say which account: --tenant <uuid>, or --all", file=sys.stderr)
            return 1
        from .config import get_settings
        from .services.usage_report import parse_rates

        card = parse_rates(get_settings().llm_rates)
        if not card:
            print("LLM_RATES has no rate card: the run's cost cannot be counted against --cap",
                  file=sys.stderr)
            return 1
        tenants = _the_tenants(db, tenant_id)
        if tenants is None:
            return 1
        within = EVERY_PROJECT
        if project is not None:
            within = _uuid(project, "project")
            if within is None:
                return 1
        spent = _Spending(card, cap)
        proposals: list = []

        stopped = False
        for t in tenants:
            for project_id, end_user_id, agent_id in obj.memories_with_objects(
                    db, t.id, project_id=within, end_user_id=user):
                try:
                    with llm_usage.attributed(t.id, agent_id=agent_id, stage=llm_usage.OPERATOR) as scope:
                        r = obj.review(db, t.id, project_id=project_id, end_user_id=end_user_id,
                                       agent_id=agent_id, before_each=spent.before_each_of(scope))
                        spent.counted(scope)
                except RuntimeError as e:
                    print(f"{e}. Save its writer in the dashboard (Settings, Writing "
                          "model) or in .env, then run this again.", file=sys.stderr)
                    db.rollback()
                    return 1
                db.rollback()   # a dry run: nothing of it stays in the database
                where = (f"project {project_id}" if project_id else "no project") + \
                    (f", end user {end_user_id}" if end_user_id else ", no end user") + \
                    (f", agent {agent_id}" if agent_id else "")
                print(f"{t.name} ({t.id}), {where}: {r['objects']} objects, {r['asked']} asked"
                      + (" (more objects than one run reads)" if r["more"] else ""))
                for p in r["proposals"]:
                    print(f"  {p.name!r:40s} = {p.canonical!r:40s} proposed")
                    proposals.append(obj.proposal_record(t.id, project_id, end_user_id, agent_id, p))
                if r["unsure"]:
                    print(f"  unsure, kept apart: {len(r['unsure'])}")
                if r["stopped"] or spent.unpriced:
                    stopped = True
                    break
            if stopped:
                break
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"proposals": proposals}, f, ensure_ascii=False, indent=1)
        print(f"dry run: {len(proposals)} links proposed, written to {out} (it holds end users' "
              f"names: delete it after the apply); cost {spent.total:.4f} {card.get('currency', '')}"
              + (": stopped before passing --cap" if stopped and not spent.unpriced else "")
              + (": stopped, a model the rate card does not price" if spent.unpriced else "")
              + f". Nothing was written; --apply {out} writes exactly these.")
        return 0
    finally:
        db.close()


def recompute_vectors(apply: bool = False, limit: int = 0) -> int:
    """Every stored vector again, with the embedding model in force.

    For an install that changes EMBEDDING_PROVIDER (or EMBEDDING_MODEL): two
    models' vectors are not comparable, and until a row has the new model's
    vector it is found by meaning badly, and a relation's centroid is not found
    at all (a new phrase then makes a duplicate of the relation). The order:
    the relations' centroids, then the facts, the memories, then the rows of a
    part of the product that have a vector (services/vectors.stored_kinds).

    A dry run unless --apply, because it computes one vector per row: billed
    on Gemini, the CPU's time on the local model (about half a second a
    memory on four cores, measured on 2026-09-29). The API can keep running: a row a user
    edits meanwhile keeps its own new vector. Stopped or interrupted, it goes
    on from where it was when run again (services/vectors.py).
    """
    from .config import get_settings
    from .core.llm import embedding_configured, embedding_model_name
    from .services import predicates, vectors

    db = _session()
    try:
        plan = vectors.to_recompute(db)
        relations = predicates.recompute_centroids(db, apply=False)["relations"]
        again = "  (left by a run that stopped)" if plan["resuming"] else ""
        print(f"model       {embedding_model_name()} ({get_settings().embedding_provider})")
        print(f"relations   {relations} centroids")
        for key, label in vectors.stored_kinds():
            print(f"{key:<11} {plan[key]}{'' if label == key else ' ' + label}{again}")
        if not apply:
            print("dry run: nothing was computed or written; --apply recomputes them.")
            return 0
        if not embedding_configured():
            print("no embedder: set GOOGLE_API_KEY, or EMBEDDING_PROVIDER=local, first",
                  file=sys.stderr)
            return 1
        if not plan["resuming"]:
            vectors.mark_stale(db)

        rel = predicates.recompute_centroids(db, apply=True)
        print(f"relations   {rel['recomputed']} recomputed, {rel['failed']} kept their old "
              f"centroid")
        if rel["stopped"]:
            print("stopped: the embedder is failing (the log says why). Run it again when "
                  "it answers; nothing is lost.", file=sys.stderr)
            return 1

        out = vectors.recompute(db, limit=limit,
                                progress=lambda kind, n: print(f"            {kind}: {n}...",
                                                               flush=True))
        for key, _label in vectors.stored_kinds():
            if key in out:
                print(f"{key:<11} {out[key]['recomputed']} recomputed, {out[key]['failed']} "
                      f"kept their old vector")
        left = vectors.to_recompute(db)
        waiting = (sum(left[key] for key, _label in vectors.stored_kinds())
                   if left["resuming"] else 0)
        stopped = any(v["stopped"] for v in out.values())
        failed = rel["failed"] or any(v["failed"] for v in out.values())
        if stopped:
            print("stopped: the embedder is failing (the log says why). Run it again when "
                  "it answers; it goes on from here.", file=sys.stderr)
        if waiting:
            print(f"{waiting} rows still have the old model's vector: run it again.")
        elif not stopped and not failed:
            # Every vector is the new model's: the start checks against this
            # name from now on (services/vectors.py).
            vectors.record_model(db, embedding_model_name())
            print(f"model       {embedding_model_name()} recorded: the service starts on "
                  f"these vectors")
        # --limit stops early on purpose: rows left then are not a failure.
        return 1 if stopped or failed or (waiting and not limit) else 0
    finally:
        db.close()


def memory_windows(apply: bool = False, batch: int = 200, limit: int = 0,
                   while_off: bool = False) -> int:
    """The windows of the memories stored before MEMORY_WINDOWS was turned on
    (1.0.33, services/memory_windows.py).

    A dry run unless --apply: what is stored and waiting, and how long the
    worker takes for it. With --apply, the windows of every live memory
    that has none, `batch` memories per transaction, each memory held while
    its windows are made, so an edit or a forget of it waits for that
    transaction and then wins (`memory_windows.fill`); and the windows that
    gave up their vector, waiting again. No vector here: the worker's job
    `windows` computes them at its own pace (memory_windows.PER_PASS a pass,
    every 30 seconds), so this command loads no model and never races the
    worker for the same rows. Stopped anywhere, it goes on from where it was
    when run again. Refused with the setting off, unless --while-off: no
    search reads the windows then, and a memory written meanwhile gets
    none."""
    from .services import memory_windows as mw
    from .worker import VECTORS_EVERY

    db = _session()
    try:
        on = mw.enabled()
        pace = max(1, int(mw.PER_PASS))
        print(f"setting     MEMORY_WINDOWS={'true' if on else 'false'}")
        print(f"window      {mw.WINDOW_WORDS} words, a new one every {mw.STRIDE_WORDS}")

        def held(n: dict) -> None:
            # The pace's ceiling: a pass of `pace` vectors, then VECTORS_EVERY
            # seconds after it ends, so the time of the passes themselves
            # (a vector's time, on this machine's CPU or Google's) comes on top.
            hours = n["pending"] / pace * VECTORS_EVERY / 3600
            print(f"windows     {n['windows']} stored, {n['pending']} waiting for their vector "
                  f"(at least {hours:.1f} h: {pace} every {VECTORS_EVERY} s at most, plus "
                  f"the time of the vectors), {n['error']} gave up; {n['short']} memories "
                  f"read need none")

        held(mw.backlog(db))
        if not apply:
            print("dry run: nothing was written; --apply makes the windows of the memories "
                  "that have none, and the worker computes their vectors.")
            return 0
        if not on and not while_off:
            print("MEMORY_WINDOWS is off: no search reads the windows, the worker computes "
                  "none, and a memory written meanwhile gets none. Turn it on first, or say "
                  "--while-off.", file=sys.stderr)
            return 1
        made = mw.fill(db, batch=batch, limit=limit,
                       progress=lambda read, n: print(f"            {read} memories read, "
                                                      f"{n} windows made...", flush=True))
        print(f"memories    {made['read']} read, {made['memories']} long enough for windows: "
              f"{made['windows']} windows made, {made['queued_again']} that gave up queued again")
        held(mw.backlog(db))
        return 0
    finally:
        db.close()


def _export_line(where: str) -> str:
    import os
    from datetime import datetime, timezone
    from pathlib import Path

    d = Path(where)
    if not d.is_dir():
        return (f"export      FAILING: {where} does not exist here "
                "(under docker-compose.yml the directory to name is /data/facts)")
    if not os.access(d, os.W_OK | os.X_OK):
        return f"export      FAILING: {where} cannot be written by this process"
    files = list(d.rglob("*.md"))
    if not files:
        return f"export      {where}, nothing written yet"
    newest = datetime.fromtimestamp(max(f.stat().st_mtime for f in files), timezone.utc)
    return (f"export      {where}, {len(files)} files, last changed "
            f"{_ago(datetime.now(timezone.utc), newest)} ago")


def _vector_lines(db) -> None:
    """`status`'s vector lines: the rows a write could not get a vector for,
    and a `recompute-vectors` run not finished."""
    # The vectors a write could not get (services/vectors.py). Silent when
    # there are none, which is a healthy install, and when there is no
    # embedder, where every row waits for one by design.
    from .core.llm import embedding_configured

    if embedding_configured():
        def without_vector(state):
            mems = (db.query(AgentMemory)
                      .filter(AgentMemory.deleted_at.is_(None),
                              AgentMemory.embedding.is_(None),
                              AgentMemory.embedding_status == state).count())
            return mems + (db.query(AgentFact)
                             .filter(AgentFact.embedding.is_(None),
                                     AgentFact.embedding_status == state).count())

        vec_waiting, vec_gave_up = without_vector("pending"), without_vector("error")
        if vec_waiting or vec_gave_up:
            line = f"vectors     {vec_waiting} waiting for a retry"
            if vec_gave_up:
                line += (f", {vec_gave_up} gave up: not found by meaning until "
                         f"`reextract` queues them")
            print(line)
        # A run of `recompute-vectors` that has not finished: those rows
        # still carry the vector of the embedding model before.
        from .services import vectors as _vectors

        left = _vectors.to_recompute(db)
        if left["resuming"]:
            print(f"vectors     {sum(left[k] for k, _l in _vectors.stored_kinds())} from the "
                  f"embedding model "
                  f"before: `recompute-vectors --apply` goes on")


def _extraction_line(db, waiting: int, oldest) -> None:
    """`status`'s extraction line: running, stuck, or not configured."""
    from datetime import datetime, timezone

    from sqlalchemy import func as sqlfunc

    last_fact = db.query(sqlfunc.max(AgentFact.created_at)).scalar()
    from .core.llm import provider_configured

    if not provider_configured():
        print("extraction  NOT CONFIGURED: no key, nothing is being mined")
    else:
        # The drain runs every ten seconds. Anything that has been waiting
        # for five minutes is not waiting, it is stuck, and the difference
        # matters more than any other number on this screen.
        now = datetime.now(timezone.utc)
        stuck = oldest is not None and (now - aware(oldest)).total_seconds() > 300
        if stuck:
            print(f"extraction  STUCK: {waiting} waiting, oldest "
                  f"{_ago(now, oldest)} ago, last fact {_ago(now, last_fact)} ago")
            print("            check `docker compose logs api` and GET /health")
        elif last_fact is None:
            print("extraction  configured, and no fact has ever been mined")
        else:
            print(f"extraction  last fact {_ago(now, last_fact)} ago")


def _embeddings_line(s) -> None:
    """`status`'s line on this machine's embedder, when it computes them."""
    # The vectors, when this machine computes them. Whether the model is
    # in the image is a fact of the files, true from this process too;
    # whether it is answering is /health's.
    if s.embedding_provider == "local":
        from .core import local_embedder

        missing = local_embedder.missing_files()
        if missing:
            print(f"embeddings  FAILING: local, and the model is not in "
                  f"{local_embedder.model_dir()}: rebuild with "
                  f"EMBEDDING_PROVIDER=local in .env")
        else:
            print(f"embeddings  local, {local_embedder.NAME}, "
                  f"{local_embedder.threads()} threads")


def _usage_lines(db) -> None:
    """`status`'s model usage of the last 30 days, by stage and by agent."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func as sqlfunc

    # What the model provider has been asked to do, and by whom. On a
    # machine the operator pays for, this is the only place the bill shows
    # up before it shows up at Google.
    from .db import AgentLlmUsage

    since = datetime.now(timezone.utc) - timedelta(days=30)
    usage = (db.query(AgentLlmUsage.stage,
                      sqlfunc.count(AgentLlmUsage.id),
                      sqlfunc.sum(AgentLlmUsage.prompt_tokens),
                      sqlfunc.sum(AgentLlmUsage.completion_tokens))
               .filter(AgentLlmUsage.ts >= since)
               .group_by(AgentLlmUsage.stage)
               .order_by(sqlfunc.sum(AgentLlmUsage.completion_tokens).desc())
               .all())
    if usage:
        print("model, last 30 days")
        for st, calls, tin, tout in usage:
            print(f"            {st:<14} {calls:>6} calls  "
                  f"{int(tin or 0):>9} in  {int(tout or 0):>9} out")
        by_agent = (db.query(AgentLlmUsage.agent_id,
                             sqlfunc.sum(AgentLlmUsage.completion_tokens))
                      .filter(AgentLlmUsage.ts >= since)
                      .group_by(AgentLlmUsage.agent_id)
                      .order_by(sqlfunc.sum(AgentLlmUsage.completion_tokens).desc())
                      .limit(5).all())
        named = [(a, t) for a, t in by_agent if a]
        if named:
            print("            by agent:  " +
                  ", ".join(f"{a} {int(t or 0)}" for a, t in named))
        elif by_agent:
            # Not a failure: agent_id is optional on a write. But the
            # per-agent bill is the reason this table exists, so say why
            # it is empty rather than printing nothing.
            print("            no agent_id on any write: pass agent_id= to "
                  "break the bill down per agent")


def status() -> int:
    """Is it working. Without opening psql.

    Everything here is a count an operator would otherwise have to write SQL
    for, plus the line that matters: whether extraction is actually running. A
    store that accepts writes and mines nothing looks identical to a healthy
    one from the outside, and that is the failure this command exists to make
    visible.

    The extraction line is read off the store, not off `provider_state()`. That
    function reports what the serving process last saw, and this command runs
    in a process of its own: it would answer "nothing has called the model yet"
    on a server that had been mining facts all morning. Evidence in the
    database is true from wherever you read it. `/health` remains the live
    answer, and is the one to put on a dashboard.
    """
    from sqlalchemy import func as sqlfunc

    from .config import get_settings

    db = _session()
    try:
        tenants = db.query(Tenant).count()
        # By the keys' own state (keys.state_of), so a key every door refuses
        # is not counted live; expired keys stay among the live ones, as the
        # documentation says (docs/guide/keys-and-limits.md).
        key_states = [key_service.state_of(k) for k in
                      db.query(AgentApiKey.prefix, AgentApiKey.revoked_at,
                               AgentApiKey.expires_at).all()]
        revoked = key_states.count("revoked")
        refused = key_states.count("refused")
        live_keys = len(key_states) - revoked - refused

        by_state = dict(
            db.query(AgentMemory.graph_sync_status, sqlfunc.count(AgentMemory.id))
              .filter(AgentMemory.deleted_at.is_(None))
              .group_by(AgentMemory.graph_sync_status)
              .all()
        )
        memories = sum(by_state.values())
        waiting = by_state.get("pending", 0) + by_state.get("processing", 0)
        errored = by_state.get("error", 0)

        live_facts = db.query(AgentFact).filter(true_at()).count()
        closed = db.query(AgentFact).filter(AgentFact.invalid_at.isnot(None)).count()

        oldest = None
        if waiting:
            oldest = (db.query(sqlfunc.min(AgentMemory.created_at))
                        .filter(AgentMemory.graph_sync_status.in_(("pending", "processing")))
                        .scalar())

        print(f"tenants     {tenants}")
        line = f"keys        {live_keys} live, {revoked} revoked"
        if refused:
            line += (f", {refused} refused ({key_service.PREFIX_LIVE}, the Cloud's prefix: "
                     f"issue {key_service.minted_prefix()} keys)")
        print(line)
        line = f"memories    {memories}"
        if waiting:
            line += f"  ({waiting} waiting"
            if oldest is not None:
                line += f", oldest {oldest:%Y-%m-%d %H:%M}"
            line += ")"
        print(line)
        if errored:
            # Loud, because nothing else in the product says it. These memories
            # are stored and searchable and will never become facts.
            print(f"            {errored} gave up extracting: those memories have no facts")
        print(f"facts       {live_facts} true now, {closed} closed")

        _vector_lines(db)

        s = get_settings()
        _extraction_line(db, waiting, oldest)

        # The Markdown export. It failed on every tick for anybody who followed
        # the example, and neither this screen nor /health said so (blind
        # install, 2026-09-28). This runs where the worker writes, so it looks
        # at the directory itself. No "stale" verdict from file ages: a pass
        # that finds nothing changed writes nothing, on purpose (git diff).
        if s.facts_export_dir:
            print(_export_line(s.facts_export_dir))

        _embeddings_line(s)
        _usage_lines(db)
        # The export has its line above (`_export_line`); a second one here
        # counted every file, not only the .md ones (the audit of 2026-10-06,
        # row 22).
        return 0
    finally:
        db.close()


def erasures_export() -> int:
    """Every erasure record, as JSON on stdout: what deploy/restore.sh keeps
    before a restore (services/erasure_replay.py)."""
    from .services import erasure_replay

    db = _session()
    try:
        data = erasure_replay.export_file(db)
    except erasure_replay.KeyUnknown as e:
        # Nothing on stdout: restore.sh stops before the database is touched.
        print(f"erasures-export: this install has no SECRET_KEY, and {e.pending} end user "
              f"erasure(s) name the person by a pseudonym made under a key no record names, "
              f"so no restore could find them again. Put back the SECRET_KEY the erasures "
              f"were made under, then export again.", file=sys.stderr)
        return 3
    finally:
        db.close()
    sys.stdout.write(erasure_replay.dumps(data))
    sys.stdout.write("\n")
    return 0


def erasures_superseded() -> int:
    """The names, one a line on stdout, of the files of erasures the newest
    holds whole (erasure_replay.superseded), from a tar of them on stdin:
    the ones deploy/backup.sh can remove; then, each on a line of its own
    as `unreadable <name>`, those that are not a file of erasures at all
    (erasure_replay.unreadable), which backup.sh sets aside. Reads no
    database; a member that is not JSON is such a file."""
    import tarfile

    from .services import erasure_replay

    files = {}
    try:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tar:
            for member in tar:
                f = tar.extractfile(member) if member.isfile() else None
                if f is None:
                    continue
                try:
                    files[member.name] = json.load(f)
                except ValueError:
                    files[member.name] = None
    except tarfile.TarError:
        print("erasures-superseded: stdin is not a tar of erasures files", file=sys.stderr)
        return 1
    for name in erasure_replay.superseded(files):
        print(name)
    for name in erasure_replay.unreadable(files):
        print(f"unreadable {name}")
    return 0


def erasures_replay() -> int:
    """After a restore: repeat the erasures read on stdin that the restored
    database does not hold."""
    import json

    from .services import erasure_replay

    try:
        records, key_check = erasure_replay.read_file(json.load(sys.stdin))
    except erasure_replay.NewerFile as e:
        print(f"erasures-replay: the file is of format {e.version}, written by a newer Korely "
              f"than this one (format {erasure_replay.FILE_VERSION}): replay it with that "
              f"version. Nothing was replayed.", file=sys.stderr)
        return 1
    except ValueError:
        print("erasures-replay: stdin is not the JSON erasures-export wrote", file=sys.stderr)
        return 1
    db = _session()
    try:
        out = erasure_replay.replay(db, records, key_check)
    except erasure_replay.KeyMismatch as e:
        why = ("this install has no SECRET_KEY" if e.keyless else
               "the file was written under another SECRET_KEY than this install's "
               "and its SECRET_KEY_FALLBACKS")
        print(f"erasures-replay: {e.pending} end user erasure(s) name the person by a "
              f"pseudonym, and {why}, so none of them can be found in the restored data. "
              f"Nothing was replayed. Put back the SECRET_KEY the erasures were made under "
              f"(the one in force when they were made), or name it in SECRET_KEY_FALLBACKS, "
              f"then run the replay again.",
              file=sys.stderr)
        return 3
    finally:
        db.close()
    print(f"{out['in_the_dump']} erasure(s) already in the restored data")
    for scope, n in sorted(out["replayed"].items()):
        print(f"{n} {scope} erasure(s) made after the dump, made again")
    for scope, n in sorted(out["nothing_to_erase"].items()):
        print(f"{n} {scope} erasure(s) with nothing left to erase in the restored data; "
              f"their records are back")
    for scope, n in sorted(out["came_back"].items()):
        print(f"{n} {scope} erasure(s) of someone who wrote again after it, in the restored "
              f"data: what they held before it is erased again, what they wrote since stays; "
              f"their records are back")
    for scope, n in sorted(out["changes"].items()):
        print(f"{n} {scope}(s) of a project or an account made after the dump, made again")
    for scope, n in sorted(out["not_made"].items()):
        # A merge into an account made after the dump: the credentials'
        # replay, whose file holds that account's login, makes it again and
        # says so (hosted/accounts._merge_again); this file holds no login.
        print(f"{n} {scope}(s) of the live database could not be made again: the account it "
              f"went into is not in the restored data. credentials-replay makes it again when "
              f"its file holds that account's login, and says so; otherwise restore a newer "
              f"dump.", file=sys.stderr)
    for scope, n in sorted(out.get("project_unknown", {}).items()):
        print(f"{n} {scope} erasure(s) of 0.1.11 or earlier whose project was not recorded "
              f"were not repeated: erasing them in every project would erase namesakes. "
              f"Erase those {scope}s again by hand, in their project; their records "
              f"are back as they were.", file=sys.stderr)
    if out["not_found"]:
        print(f"{out['not_found']} end user erasure(s) made without SECRET_KEY name nobody: "
              f"erase those people again by hand", file=sys.stderr)
    failed = out.get("failed") or []
    if failed:
        # The records' own ids, never the people's, and the errors' classes
        # and codes, never their messages (log_safe.error).
        print(f"{len(failed)} erasure record(s) could not be made again; every other record "
              f"was. Each, by its record's id, and the kind of its error:", file=sys.stderr)
        for f in failed:
            print(f"  {f['scope']} {f['id']}: {f['error']}", file=sys.stderr)
        print("Run the replay again with the same file: it repeats only what the restored data "
              "does not hold yet. If the same records fail again, keep the API stopped and "
              "report the lines above (docs/guide/operations.md, Backups).", file=sys.stderr)
        return 4
    if out["not_found"]:
        return 2
    return 2 if out["not_made"] or out.get("project_unknown") else 0


def credentials_export() -> int:
    """The state of every credential, as JSON on stdout: what deploy/restore.sh
    keeps before a restore (services/credential_replay.py)."""
    from .services import credential_replay

    db = _session()
    try:
        sys.stdout.write(credential_replay.dumps(credential_replay.export(db)))
        sys.stdout.write("\n")
        return 0
    finally:
        db.close()


def credentials_replay(unsigned_from_1_0_31: bool = False) -> int:
    """After a restore: make again the credential changes read on stdin, end
    every session and OAuth grant, and print what can sign in now.
    `unsigned_from_1_0_31` reads an unsigned file of 1.0.31, which is
    refused otherwise on an install with a SECRET_KEY
    (services/credential_replay.check)."""
    import json

    from .services import credential_replay

    try:
        live = json.load(sys.stdin)
    except ValueError:
        live = None
    if not isinstance(live, dict):
        print("credentials-replay: stdin is not the JSON credentials-export wrote", file=sys.stderr)
        return 1
    db = _session()
    try:
        try:
            out = credential_replay.replay(db, live, unsigned_from_1_0_31=unsigned_from_1_0_31)
        except credential_replay.Refused as refused:
            db.rollback()
            print(f"credentials-replay: refused, nothing changed: {refused}", file=sys.stderr)
            return refused.code
        if out["warning"]:
            print(f"credentials-replay: {out['warning']}", file=sys.stderr)
        print(f"{out['sessions_ended']} operator session(s) ended: everybody signs in again")
        print(f"{out['oauth_grants_ended']} OAuth request(s), code(s) and refresh token(s) and "
              f"{out['oauth_keys_revoked']} key(s) of connected apps ended: each app connects again")
        made = [(out["keys_revoked"], "key(s) revoked"),
                (out["keys_moved"], "key(s) back in the project they were moved to"),
                (out["keys_secret"], "key(s) back on the secret they had before the restore"),
                (out["keys_narrowed"], "key(s) without the scopes taken from them after the dump"),
                (out["passwords"], "password(s) as they were before the restore"),
                (out["operators_disabled"], "operator(s) disabled"),
                (out["install_admin_removed"], "operator(s) no longer setting the install's models"),
                (out["webhook_secrets"], "webhook secret(s) as they were before the restore"),
                (out["webhooks_off"], "webhook endpoint(s) switched off"), *out["also"]]
        print("made again since the dump: " + (", ".join(f"{n} {what}" for n, what in made if n)
                                               or "nothing"))
        if out["accounts_not_live"]:
            # Closed or merged into another since the dump. What the file
            # holds of their keys was made again above (the keys moved by a
            # merge are named under the account they went into); the rest
            # is as the dump had it (the skeptic's review of 25 Nov 2026:
            # this said "as the dump had them" of keys it had just revoked).
            print(f"{out['accounts_not_live']} account(s) of the dump were not in the database "
                  f"replaced (closed, or merged into another, since the dump): what the file "
                  f"holds of their keys is made again above, the rest of their credentials is "
                  f"as the dump had them")
        print()
        print("What can sign in now:")
        for row in credential_replay.in_force(db):
            t = row["account"]
            print(f"account   {t.id}  {t.name}")
            for k in row["keys"]:
                until = f"until {k.expires_at:%Y-%m-%d}" if k.expires_at else "never expires"
                used = f"used {k.last_used_at:%Y-%m-%d}" if k.last_used_at else "never used"
                # The project too: a key reads and writes only there, and a
                # key back in a project it was taken out of is what to spot.
                project = row["projects"].get(k.project_id, "no project: the Default one")
                print(f"  key       {k.id}  {k.prefix}...{k.last_four or '????'}  "
                      f"made {k.created_at:%Y-%m-%d}  {until}  {used}  project {project}  "
                      f"{k.name or ''}")
            for w in row["webhooks"]:
                print(f"  webhook   {w.id}  made {w.created_at:%Y-%m-%d}  {w.url}")
            for o in row["operators"]:
                last = f"last login {o.last_login_at:%Y-%m-%d}" if o.last_login_at else "never logged in"
                admin = "  sets the install's models" if o.install_admin else ""
                print(f"  operator  {o.username}  made {o.created_at:%Y-%m-%d}  {last}{admin}")
        print()
        if "accounts" not in live:
            # SKIP_ERASURES=1: there was no database to read the changes from.
            print("The database replaced was not read: a key revoked or rotated, a password "
                  "reset or a webhook secret rotated\nafter the dump is back as the dump had it. "
                  "Revoke or reset here what should not be.")
        else:
            print("Check it against what you expect: revoke or reset what should not be here.")
        return 0
    finally:
        db.close()


def reseal_secrets() -> int:
    """After a rotation of SECRET_KEY: what a fallback sealed (the screen's
    provider, key and models, an OAuth client's secret) sealed again under
    SECRET_KEY, and what no secret opens counted (provider_key.seal_again).
    Exit 2 when something opens with none of them."""
    from .services import provider_key

    db = _session()
    try:
        try:
            out = provider_key.seal_again(db)
        except provider_key.NoEncryptionKey:
            print("reseal-secrets: SECRET_KEY is not set, so nothing can be sealed", file=sys.stderr)
            return 1
        print(f"{out['sealed_again']} sealed value(s) sealed again under SECRET_KEY, "
              f"{out['current']} under it already")
        if out["unreadable"]:
            print(f"{out['unreadable']} sealed value(s) open with neither SECRET_KEY nor a "
                  "fallback, and read as unset: choose the provider on the screen again; an "
                  "OAuth client of them registers again", file=sys.stderr)
            return 2
        print("Nothing sealed needs SECRET_KEY_FALLBACKS any more; the erasures made under "
              "a fallback still need it to be found by the person's id")
        return 0
    finally:
        db.close()


def _review_parsers(sub) -> None:
    """The commands that ask a model again about what the store holds
    (relations, vocabulary, objects, subjects), each a dry run first: one
    group of `_parser`'s subcommands."""
    rc = sub.add_parser("reclassify-relations",
                        help="ask again whether each learned relation holds one value "
                             "(a dry run unless --apply)")
    rc.add_argument("--tenant", default=None, help="one account (default: all of them)")
    rc.add_argument("--apply", action="store_true", help="write the new answers")

    rvoc = sub.add_parser("review-vocabulary",
                          help="ask again whether each learned phrase is the relation it was "
                               "merged into (a dry run unless --apply)")
    rvoc.add_argument("--tenant", default=None, help="one account (default: all of them)")
    rvoc.add_argument("--apply", action="store_true", help="separate the phrases the model separates")
    rvoc.add_argument("--votes", type=int, default=3,
                      help="a phrase is separated only when a majority of this many answers say so (default 3)")

    rob = sub.add_parser("review-objects",
                         help="ask whether two objects of an end user's memory are one thing in two "
                              "languages or forms: a dry run to a file, then --apply <file>")
    rob.add_argument("--tenant", default=None, help="one account")
    rob.add_argument("--all", action="store_true", help="every account (instead of --tenant)")
    rob.add_argument("--project", default=None, help="one project of the account")
    rob.add_argument("--user", default=None, help="one end user")
    rob.add_argument("--cap", type=float, default=None,
                     help="the dry run stops before its cost would pass this (LLM_RATES' currency)")
    rob.add_argument("--out", default=None, help="the dry run's proposals, a JSON file")
    rob.add_argument("--apply", dest="apply_file", default=None,
                     help="write exactly the links of this dry run's file (no model call)")
    rob.add_argument("--while-off", action="store_true",
                     help="run it although the same-object switch is off")

    rsu = sub.add_parser("review-subjects",
                         help="ask whether two subjects of a memory with no end user are one thing: "
                              "--dry-run (free), a run to a file, then --apply <file>")
    rsu.add_argument("--tenant", default=None, help="one account")
    rsu.add_argument("--all", action="store_true", help="every account (instead of --tenant)")
    rsu.add_argument("--project", default=None, help="one project of the account")
    rsu.add_argument("--dry-run", action="store_true",
                     help="the pairs that would be asked: no model is called, nothing is written")
    rsu.add_argument("--cap", type=float, default=None,
                     help="the run stops before its cost would pass this (LLM_RATES' currency)")
    rsu.add_argument("--out", default=None, help="the run's proposals, a JSON file")
    rsu.add_argument("--apply", dest="apply_file", default=None,
                     help="write exactly the links of this run's file, then judge the groups again")
    rsu.add_argument("--while-off", action="store_true",
                     help="ask the model although the same-subject switch is off")


def _parser() -> argparse.ArgumentParser:
    """Every command and its arguments; `main` dispatches them."""
    p = argparse.ArgumentParser(prog="korely_agent.manage")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create-tenant", help="create a tenant and print its first key")
    c.add_argument("--name", required=True)
    c.add_argument("--expires-in", type=_lifetime, default=None, metavar="DAYS|never",
                   help="days until the key stops working (default 90); never for no end "
                        "date; 0 expires it at once")
    c.add_argument("--json", action="store_true", help="print one JSON object on stdout and nothing else")

    k = sub.add_parser("issue-key", help="mint another key for an existing tenant")
    k.add_argument("--tenant", required=True)
    k.add_argument("--name", default="api key")
    k.add_argument("--admin", action="store_true",
                   help="also grant administration (keys, webhooks, projects)")
    k.add_argument("--expires-in", type=_lifetime, default=None, metavar="DAYS|never",
                   help="days until it stops working (default 90); never for no end date; "
                        "0 expires it at once")
    k.add_argument("--json", action="store_true", help="print one JSON object on stdout and nothing else")

    lk = sub.add_parser("list-keys", help="list a tenant's keys")
    lk.add_argument("--tenant", required=True)

    rk = sub.add_parser("revoke-key", help="take a key out of service")
    rk.add_argument("--tenant", required=True)
    rk.add_argument("--id", required=True)

    lt = sub.add_parser("list-tenants", help="list tenants")
    lt.add_argument("--json", action="store_true", help="print one JSON list on stdout")

    sub.add_parser("status", help="counts, and whether extraction is actually running")

    rx = sub.add_parser("reextract", help="queue memories for another attempt")
    # An on/off flag. It was `store_true` with `default=True`, which is on
    # whether or not it is typed: `--empty` alone re-queued the failures too,
    # and nothing could say "only the empty ones" (2026-09-28).
    rx.add_argument("--errors", action=argparse.BooleanOptionalAction, default=True,
                    help="the ones extraction gave up on, and the vectors that "
                         "gave up (on by default; --no-errors leaves them)")
    rx.add_argument("--empty", action="store_true",
                    help="also the ones marked done that produced no fact")
    rx.add_argument("--limit", type=int, default=0)

    co = sub.add_parser("create-operator", help="create a login for the dashboard")
    co.add_argument("--tenant", required=True)
    co.add_argument("--username", required=True)
    co.add_argument("--install-admin", action="store_true",
                    help="also set the install's model provider, key and models (the first "
                         "login of an install always does)")
    co.add_argument("--json", action="store_true", help="print one JSON object on stdout and nothing else")

    ga = sub.add_parser("grant-install-admin",
                        help="name who sets the install's model provider, key and models")
    ga.add_argument("--username", required=True)
    ga.add_argument("--revoke", action="store_true")

    lo = sub.add_parser("list-operators", help="list the people who can log in")
    lo.add_argument("--tenant", default=None)
    lo.add_argument("--json", action="store_true", help="print one JSON list on stdout")

    rp = sub.add_parser("reset-password", help="new password, and end their sessions")
    rp.add_argument("--username", required=True)

    w = sub.add_parser("add-webhook", help="register an endpoint to POST events to")
    w.add_argument("--tenant", required=True)
    w.add_argument("--url", required=True)
    w.add_argument("--events", default="*",
                   help="comma-separated, or * for all (default)")

    lw = sub.add_parser("list-webhooks", help="list webhook endpoints")
    lw.add_argument("--tenant", default=None)

    dw = sub.add_parser("delete-webhook", help="remove a webhook endpoint")
    dw.add_argument("--tenant", required=True)
    dw.add_argument("--id", required=True)

    _review_parsers(sub)

    rv = sub.add_parser("recompute-vectors",
                        help="every stored vector again, with the embedding model in force "
                             "(a dry run unless --apply)")
    rv.add_argument("--apply", action="store_true", help="compute and write them")
    rv.add_argument("--limit", type=int, default=0,
                    help="at most this many facts and this many memories in this run "
                         "(default: all); run it again to go on")

    mw = sub.add_parser("memory-windows",
                        help="the windows of the memories stored before MEMORY_WINDOWS; the "
                             "worker computes their vectors (a dry run unless --apply)")
    mw.add_argument("--apply", action="store_true", help="make them")
    mw.add_argument("--batch", type=int, default=200,
                    help="memories per transaction (default 200)")
    mw.add_argument("--limit", type=int, default=0,
                    help="at most this many memories read in this run (default: all); run "
                         "it again to go on")
    mw.add_argument("--while-off", action="store_true",
                    help="run it although MEMORY_WINDOWS is off")

    sub.add_parser("erasures-export",
                   help="every erasure record as JSON on stdout (restore.sh keeps it)")
    sub.add_parser("erasures-replay",
                   help="after a restore, repeat the erasures on stdin the database does not hold")
    sub.add_parser("erasures-superseded",
                   help="of a tar of erasures files on stdin, the names the newest holds whole "
                        "(backup.sh removes them)")
    sub.add_parser("credentials-export",
                   help="the state of every key, password and webhook secret, as JSON on stdout")
    replay = sub.add_parser("credentials-replay",
                            help="after a restore, make again the credential changes on stdin and end every session")
    replay.add_argument("--unsigned-from-1.0.31", dest="unsigned_from_1_0_31", action="store_true",
                        help="read an unsigned file Korely 1.0.31 wrote for this restore; its age "
                             "and its author are not checked")
    sub.add_parser("reseal-secrets",
                   help="after a rotation of SECRET_KEY, seal again under it what a fallback sealed")

    rw = sub.add_parser("rotate-webhook-secret",
                        help="a new signing secret for an endpoint, keeping the rest")
    rw.add_argument("--tenant", required=True)
    rw.add_argument("--id", required=True)
    return p


def _the_modes_key_rules() -> None:
    """The rules on keys the mode keeps at every door, this command line's
    too: in hosted mode the Cloud's package (core/extensions.cloud) registers
    its ceiling on a key's lifetime here, as the app's start does (the review
    of 14 Nov 2026: `issue-key --expires-in never` made a key the API would
    refuse), and what a restore keeps of its accounts' logins
    (`credentials-export`, `credentials-replay`). Nothing on an install of
    one's own."""
    from .config import get_settings
    from .core import extensions

    package = extensions.cloud() if get_settings().hosted else None
    if package is not None:
        package.register_key_rules()
        package.register_restore()


def main(argv=None) -> int:
    a = _parser().parse_args(argv)
    _the_modes_key_rules()
    if a.cmd == "create-tenant":
        return create_tenant(a.name, a.expires_in, as_json=a.json)
    if a.cmd == "issue-key":
        return issue_key(a.tenant, a.name, admin=a.admin, expires_in=a.expires_in,
                         as_json=a.json)
    if a.cmd == "list-keys":
        return list_keys(a.tenant)
    if a.cmd == "revoke-key":
        return revoke_key(a.tenant, a.id)
    if a.cmd == "status":
        return status()
    if a.cmd == "reextract":
        return reextract(errors=a.errors, empty=a.empty, limit=a.limit)
    if a.cmd == "create-operator":
        return create_operator(a.tenant, a.username, install_admin=a.install_admin,
                               as_json=a.json)
    if a.cmd == "grant-install-admin":
        return grant_install_admin(a.username, revoke=a.revoke)
    if a.cmd == "list-operators":
        return list_operators(a.tenant, as_json=a.json)
    if a.cmd == "reset-password":
        return reset_password(a.username)
    if a.cmd == "add-webhook":
        return add_webhook(a.tenant, a.url, a.events)
    if a.cmd == "list-webhooks":
        return list_webhooks(a.tenant)
    if a.cmd == "delete-webhook":
        return delete_webhook(a.tenant, a.id)
    if a.cmd == "rotate-webhook-secret":
        return rotate_webhook_secret(a.tenant, a.id)
    if a.cmd == "list-tenants":
        return list_tenants(as_json=a.json)
    if a.cmd == "erasures-export":
        return erasures_export()
    if a.cmd == "erasures-replay":
        return erasures_replay()
    if a.cmd == "erasures-superseded":
        return erasures_superseded()
    if a.cmd == "credentials-export":
        return credentials_export()
    if a.cmd == "credentials-replay":
        return credentials_replay(unsigned_from_1_0_31=a.unsigned_from_1_0_31)
    if a.cmd == "reseal-secrets":
        return reseal_secrets()
    if a.cmd == "reclassify-relations":
        return reclassify_relations(a.tenant, apply=a.apply)
    if a.cmd == "review-vocabulary":
        return review_vocabulary(a.tenant, apply=a.apply, votes=a.votes)
    if a.cmd == "review-objects":
        return review_objects(a.tenant, every=a.all, project=a.project, user=a.user, cap=a.cap,
                              out=a.out, apply_file=a.apply_file, while_off=a.while_off)
    if a.cmd == "review-subjects":
        return review_subjects(a.tenant, every=a.all, project=a.project, dry_run=a.dry_run, cap=a.cap,
                               out=a.out, apply_file=a.apply_file, while_off=a.while_off)
    if a.cmd == "recompute-vectors":
        return recompute_vectors(apply=a.apply, limit=a.limit)
    if a.cmd == "memory-windows":
        return memory_windows(apply=a.apply, batch=a.batch, limit=a.limit,
                              while_off=a.while_off)
    # Unreachable: argparse requires a subcommand and every one is handled
    # above. Explicit so that adding a parser without adding a branch fails
    # here instead of quietly listing tenants.
    raise AssertionError(f"unhandled command {a.cmd!r}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
