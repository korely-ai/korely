"""One keyset paginator (db/bounded.by_key) for the reads that walk rows a
block at a time: end_user_places.rename_in_lists had written the same loop
three times by hand, for the keys, the grants and the trail (the audit of
27 Oct 2026)."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from korely_agent.db.bounded import by_key
from korely_agent.db.models import AgentProject

PLACES = Path(__file__).resolve().parents[1] / "korely_agent" / "services" / "end_user_places.py"


def test_the_blocks_come_by_key_and_never_past_their_size(db, tenant):
    for i in range(7):
        db.add(AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name=f"p{i}", slug=f"p{i}"))
    db.commit()
    q = db.query(AgentProject).filter(AgentProject.tenant_id == tenant.id)
    blocks = list(by_key(q, AgentProject.id, 3))
    every = [p.id for b in blocks for p in b]
    n = q.count()
    assert n >= 7 and [len(b) for b in blocks] == [3] * (n // 3) + ([n % 3] if n % 3 else [])
    assert len(every) == n
    assert every == sorted(every), "each block starts after the last key of the one before"


def test_a_locked_block_is_read_for_update(db, tenant):
    db.add(AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="x", slug="x"))
    db.commit()
    seen = []
    from sqlalchemy import event

    def statement(conn, cursor, sql, *_a):
        seen.append(sql)

    event.listen(db.get_bind(), "before_cursor_execute", statement)
    try:
        list(by_key(db.query(AgentProject).filter(AgentProject.tenant_id == tenant.id),
                    AgentProject.id, 10, lock=True))
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", statement)
    assert seen and all("FOR UPDATE" in s for s in seen if "agent_projects" in s)


def test_the_erasures_lists_walk_their_rows_through_the_one_paginator():
    source = PLACES.read_text(encoding="utf-8")
    assert source.count("by_key(") == 3
    assert not re.search(r"\.id > after", source), "a keyset loop written by hand"


def test_an_erasure_reads_the_grants_that_list_its_end_user_for_update(db, tenant):
    """The grants of a consent that list the erased end user are rewritten
    under their lock (FOR UPDATE, by_key's `lock`), so a consent being
    finished meanwhile waits and does not write its list back: the review
    of 26 Nov 2026 took the lock away and nothing failed."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import event

    from korely_agent.db.models import OAuthClient, OAuthGrant
    from korely_agent.services import projects
    from korely_agent.services.facts import erasure

    pid = projects.get_or_create_default_project(db, tenant.id).id
    db.add(OAuthClient(client_id="zz-lock", info={"client_name": "app"}))
    db.flush()
    grant = OAuthGrant(kind="code", client_id="zz-lock", tenant_id=tenant.id, project_id=pid,
                       scopes=["memories:read"],
                       expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                       end_users=["erased-anna", "kept-bob"])
    db.add(grant)
    db.commit()
    gid = grant.id
    seen = []

    def statement(conn, cursor, sql, *_a):
        if "FROM oauth_grants" in sql and sql.lstrip().upper().startswith("SELECT"):
            seen.append(sql)

    event.listen(db.get_bind(), "before_cursor_execute", statement)
    try:
        erasure.forget_end_user(db, tenant.id, "erased-anna", project_id=pid)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", statement)
    db.expire_all()
    listed = db.get(OAuthGrant, gid).end_users
    assert "erased-anna" not in listed and "kept-bob" in listed
    assert seen and all("FOR UPDATE" in s for s in seen), seen
