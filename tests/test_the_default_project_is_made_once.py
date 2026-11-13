"""Two first calls for one account's Default project at once make one
(services/projects.get_or_create_default_project).

The unique (tenant_id, slug) index lets one writer win and the other reads
the winner's row. Made in the caller's transaction (`commit=False`, 7 Nov
2026), the insert runs in a savepoint of its own: a plain flush that lost
the race left the caller's whole transaction to be rolled back
(PendingRollbackError, a 500), and took with it whatever the caller had
written before. One session holds its insert open while the other asks;
the first ends with a commit, or with a rollback, which leaves the second
to make it.
"""
from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy.orm import Session

from korely_agent.db import AgentProject
from korely_agent.db.models import Tenant
from korely_agent.services import projects as project_service

#: How long the first session holds its insert open, so that the second's
#: meets it.
HELD = 1.0


def _account(engine, name: str):
    with Session(engine) as s:
        t = Tenant(id=uuid.uuid4(), name=name)
        s.add(t)
        s.commit()
        return t.id


def _count(engine, model, *where) -> int:
    with Session(engine) as s:
        return s.query(model).filter(*where).count()


@pytest.mark.parametrize("first_ends", ["commit", "rollback"])
@pytest.mark.parametrize("second_commits", [False, True],
                         ids=["second-in-its-transaction", "second-commits"])
def test_two_first_calls_at_once_make_one_default(db, engine, first_ends, second_commits):
    account = _account(engine, "two at once")
    inserted, failed = threading.Event(), []

    def first():
        s = Session(engine)
        try:
            project_service.get_or_create_default_project(s, account, commit=False)
            inserted.set()
            time.sleep(HELD)
            s.commit() if first_ends == "commit" else s.rollback()
        except Exception as e:  # noqa: BLE001 - said by the assertion below
            failed.append(repr(e)[:200])
            inserted.set()
        finally:
            s.close()

    thread = threading.Thread(target=first)
    thread.start()
    assert inserted.wait(10)
    second = Session(engine)
    # Something of the second caller's own, written before it asks: a lost
    # race must not take it away.
    own = uuid.uuid4()
    second.add(Tenant(id=own, name="the second caller's own row"))
    second.flush()
    try:
        got = project_service.get_or_create_default_project(second, account,
                                                            commit=second_commits)
        assert got.is_default and got.tenant_id == account
        second.commit()
    finally:
        second.close()
        thread.join(10)
    assert failed == []
    assert _count(engine, AgentProject, AgentProject.tenant_id == account) == 1
    if not second_commits:
        # In the caller's transaction, its own row stays with the Default.
        assert _count(engine, Tenant, Tenant.id == own) == 1
