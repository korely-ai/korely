"""The leak matrix: every /v1 door, the MCP tools and the operator's
listings, with every key, over a store where the same bait sits in every
place a key may not read (T2, piece P0, the shared level's specification,
4.2). The world, the keys, the doors and `SEES` are in tests/wall_world.py.

What must hold, on every door and for every key and everything asked:

- a place the key may not read never shows (`SEES`), in an answer or in an
  error body;
- a `user_id` outside a confined key's list is a 403 (the tool's refusal on
  the MCP);
- a row of another place, read or written by its id, is a 404 (the tool's
  refusal on the MCP), never a 403, and a door that writes leaves every
  place the key may not read as it was;
- a batch job is read by the keys of its project that are not confined and
  by the confined key that queued it, and is a 404 for every other.

Every answer is also kept, place by place in the answer's order, with its
counts, in tests/data/end_user_wall_answers.json, captured on the code
before the end-user wall was written once (origin/main at a16f30c2, the
code of 1.0.11): each door answers as it did. To capture it again, on a
change meant to change an answer and that says so in the CHANGELOG:
`KORELY_WRITE_WALL_ANSWERS=1` with this file, on the code before the change.

A tree that carries a part of the product (core/extensions.Part, the
Cloud's package) answers with its rows too: its answers, where they are not
the ones above, are kept in the file tests/hosted_parts.WALL_ANSWERS names,
by mode, and written there; the file above is the public tree's alone.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests import cloud
from tests import wall_world as ww

ANSWERS = Path(__file__).resolve().parent / "data" / "end_user_wall_answers.json"
# This tree's answers where a part of the product makes them others.
PART_ANSWERS = cloud.of_the_parts("WALL_ANSWERS", lambda: None)
# The doors whose control asks each key's own places to show.
_SHOWS_ITS_OWN = ("search", "search_facts", "search_memories_and_facts", "list", "facts",
                  "context", "mcp_search", "mcp_search_facts", "mcp_search_memories_and_facts",
                  "mcp_facts", "audit", *ww.SHOWS_ITS_OWN)


def test_the_matrix_has_a_column_per_place_and_a_row_per_key():
    assert set(ww.SEES) == set(ww.KEYS)
    rows = list(ww.SEES.values()) + list(ww.SEES_ACCOUNT.values())
    assert all(len(row) == len(ww.COLUMNS) for row in rows)


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    ww.no_model(monkeypatch)


@pytest.fixture
def client(db, monkeypatch):
    with ww.a_client(db, monkeypatch) as c:
        yield c


def _refusal(door, w, key, asked, status, payload) -> bool:
    if door in ww.REFUSED_AS:
        return ww.REFUSED_AS[door](w, key, asked, status, payload)
    return status in (403, ww.REFUSED)


def _not_found(door, w, key, asked, status, payload) -> bool:
    if door in ww.NOT_FOUND_AS:
        return ww.NOT_FOUND_AS[door](w, key, asked, status, payload)
    return status == (ww.REFUSED if door.startswith("mcp_") else 404)


def leaks(door, answers, changed) -> list:
    from korely_agent.services import user_scope

    found = [f"{door} {key}: changed {places}, which it may not read"
             for key, places in changed.items() if places]
    for (key, asked), (status, payload, sent, w) in answers.items():
        confined = ww.KEYS[key][2]
        if door in ww.OPERATORS:
            if key not in ww.SEES_ACCOUNT:
                if status != 403:
                    found.append(f"{door} {key}: {status}, not 403, on an operator's route")
                continue
            may = ww.allowed(ww.SEES_ACCOUNT, key)
        elif door == "batch":
            place, queued_by = asked.split("@")
            sees = ww.batch_sees(key, queued_by)
            may = {place} if sees else set()
            if status != (200 if sees else 404):
                found.append(f"batch {key} job of {asked}: {status}")
        else:
            may = ww.allowed(ww.SEES, key)
        if (door in ww.ASKS_A_USER or door in ww.OPERATORS) and asked is not None:
            # A read for one end user shows that end user's places only (and
            # the shared ones where the door's rule says: ww.places_of).
            may &= ww.places_of(w, door, asked, confined)
        shown = set(w.places_in(payload, sent)) - may
        if shown:
            found.append(f"{door} {key} {asked!r}: shows {sorted(shown)} ({status})")
        if door in ww.READ_BY_ID or door in ww.WRITE_BY_ID:
            if door in ww.OWN_AS:
                ok = ww.OWN_AS[door](w, key, asked, status, payload)
            else:
                ok = (200 <= status < 300) if isinstance(status, int) else status == 200
            if asked in may and not ok:
                found.append(f"{door} {key} {asked}: {status} on its own row")
            if asked not in may and not _not_found(door, w, key, asked, status, payload):
                found.append(f"{door} {key} {asked}: {status}, not a 404")
        elif door in ww.ASKS_A_USER or door in ww.FOR_A_USER:
            if asked is not None and confined is not None \
                    and not user_scope.allows(confined, asked) \
                    and not _refusal(door, w, key, asked, status, payload):
                found.append(f"{door} {key} user_id={asked!r}: {status}, not refused")
        elif door in ww.FOR_AN_AGENT and confined is not None and status != 403:
            found.append(f"{door} {key} {asked}: {status}, not 403")
    return found


def _kept(answers):
    return {f"{key}|{asked}": [status, w.places_in(payload, sent), w.numbers_in(payload)]
            for (key, asked), (status, payload, sent, w) in sorted(answers.items(), key=str)}


def _read(path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _mode() -> str:
    from korely_agent.config import get_settings

    return "hosted" if get_settings().hosted else "self-hosted"


def _before() -> dict:
    """The answers kept: the public tree's, and this tree's own over them."""
    kept = _read(ANSWERS)
    if PART_ANSWERS is not None:
        kept.update(_read(PART_ANSWERS).get(_mode(), {}))
    return kept


def _keep(name, value):
    """With KORELY_WRITE_WALL_ANSWERS, the answers are written, not read:
    in a tree with a part, into the part's file, and only where they are not
    the public tree's."""
    if not os.environ.get("KORELY_WRITE_WALL_ANSWERS"):
        return
    if PART_ANSWERS is None:
        kept = _read(ANSWERS)
        kept[name] = value
        ANSWERS.parent.mkdir(exist_ok=True)
        ANSWERS.write_text(json.dumps(kept, indent=1, sort_keys=True) + "\n")
        return
    kept = _read(PART_ANSWERS)
    mode = kept.setdefault(_mode(), {})
    if _read(ANSWERS).get(name) == value:
        mode.pop(name, None)
    else:
        mode[name] = value
    PART_ANSWERS.write_text(json.dumps(kept, indent=1, sort_keys=True) + "\n")


@pytest.mark.parametrize("door", ww.DOORS)
def test_the_door_leaks_nothing_and_answers_as_before(client, db, door):
    the_door_leaks_nothing_and_answers_as_before(client, db, door)


def the_door_leaks_nothing_and_answers_as_before(client, db, door):
    answers, changed = ww.ask(db, client, door)
    if door in _SHOWS_ITS_OWN:
        # The control: each key's own places do show, or the test proves
        # nothing.
        for key in ww.KEYS:
            if ww.allowed(ww.SEES, key):
                status, payload, sent, w = answers[(key, None)]
                assert set(w.places_in(payload)), (door, key, status, payload)
    assert leaks(door, answers, changed) == []
    kept = _kept(answers)
    _keep(door, kept)
    assert kept == _before()[door]


def test_the_reads_with_no_door_answer_as_before(client, db):
    """The reads with no /v1 door, called as their callers call them: the
    Markdown export (worker.py, after an erasure), the facts the review of
    the objects reads (manage review-objects), the dialogue order of a
    search's hits (services/context.py)."""
    w = ww.World(db, client)
    got = services(w, db)
    _keep("_services", got)
    assert got == _before()["_services"]


def services(world, db) -> dict:
    from korely_agent.services import export_md
    from korely_agent.services.context_experiments import in_dialogue_order
    from korely_agent.services.facts.objects import memories_with_objects
    from korely_agent.services.sentinels import EVERY_PROJECT

    x = world.tenants["X"].id
    out = {}
    for user in ("anna", "bruno", None, world.erased):
        name = "erased" if user == world.erased else str(user)
        for live in (True, False):
            rows = list(export_md._facts_of(db, x, user, live=live))
            out[f"export|{name}|{live}"] = world.places_in([r.object_label for r in rows])
        for project in ("P1", "P2", "every"):
            pid = EVERY_PROJECT if project == "every" else world.projects[("X", project)]
            rows = memories_with_objects(db, x, project_id=pid, end_user_id=user)
            out[f"objects|{name}|{project}"] = sorted(
                [project if p == pid else str(p == world.projects[("X", "P1")]),
                 world.places_in([a])] for p, _u, a in rows)
    users = list(export_md._end_users(db, x))
    out["export_users"] = ["erased" if u == world.erased else str(u) for u in users]
    out["dialogue_order"] = _dialogue_order(world, db, in_dialogue_order)
    return out


def _dialogue_order(world, db, in_dialogue_order) -> list:
    """Two turns of one end user are consecutive when nothing else of that
    end user, in that project, was written between them: Anna's three turns
    have a write of no end user between the last two and stay one run. The
    two writes of no end user make no run (1.0.22: a memory with no end user
    is not known to be one person's) and keep their ranks."""
    import uuid
    from datetime import timedelta

    from korely_agent.db.models import AgentMemory

    x, p1 = world.tenants["X"].id, world.projects[("X", "P1")]
    first = {p: db.get(AgentMemory, world.mems[p]) for p in ("anna", "nobody")}
    t0 = max(m.created_at for m in first.values())
    for m in first.values():
        m.created_at = t0
    # The ids as the engine's hits carry them (AgentMemoryEngine.search).
    names = {str(m.id): p for p, m in first.items()}
    for name, user, seconds in (("anna+0.5", "anna", 0.5), ("nobody+1", None, 1),
                                ("anna+3", "anna", 3)):
        m = AgentMemory(id=uuid.uuid4(), tenant_id=x, project_id=p1, end_user_id=user,
                        content=name, created_at=t0 + timedelta(seconds=seconds))
        db.add(m)
        names[str(m.id)] = name
    db.commit()
    ranked = ("anna", "nobody", "anna+3", "nobody+1", "anna+0.5")
    hits = [{"id": i} for i in sorted(names, key=lambda i: ranked.index(names[i]))]
    return [names[h["id"]] for h in in_dialogue_order(db, hits, len(hits))]
