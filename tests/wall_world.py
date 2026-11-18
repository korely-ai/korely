"""The world of the end-user wall's leak matrix, and its doors (T2, piece
P0, the shared level's specification, 4.2). Read by
tests/test_the_end_user_wall_leaks_nothing.py, which asks every door with
every key, and tests/test_no_read_builds_the_wall_by_hand.py, which watches
the SQL those same doors send.

The world: two accounts, X and Y; in X two projects, P1 and P2. In P1 one
row of each kind (memory, fact, trail event, alias, object link, batch job)
for Anna, Bruno, `team:hr`, `team:sales`, no end user, and the pseudonym an
erasure left of Carla, who was erased for real, through its door, before
anything is asked. P2 and Y hold an `anna` and a no-end-user row of their
own: the same ids, other walls. Every row says the same sentence, word for
word the question the doors are asked, and has the same vector as that
question, a hair apart so the order is fixed: a wall that gives way puts
the bait at the top, where it cannot go unnoticed.

Each row carries its place's marker in its text and its own ids and agent
id, and an answer shows a place when any string in it carries one. An end
user's id names a place only where one place has it: `anna` is in P1, P2
and Y on purpose, since the walls between them are what is tested there,
and those rows are told apart by their markers, agent ids and row ids,
which every answer that carries the row carries too. A listing that names
the id alone (GET /v1/users) is held by its counts, which the kept answers
of the matrix hold.

`SEES` has one row per key and one column per place. A design for the
shared level (T2: a shared place) adds a column, and each key's
expectation for it, and nothing else here changes.

A part of the product the Cloud's package carries (core/extensions.Part)
adds its own rows to every place and to Carla's, its own check that a
place is untouched, and its own doors, under tests/hosted_parts.WALL_PARTS
(`PARTS`); none in the public repository. Its doors join the tables below
(`PART_DOORS`, never `DOORS`: they are served only in the part's mode).
"""
from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from korely_agent.db.models import (AgentEntityAlias, AgentFact, AgentMemory, AgentObjectLink,
                                    AuditEvent, PredicateCluster, Tenant)
from korely_agent.services import keys as key_service
from tests import cloud

BAIT = "Where does the office keep the spare key to the archive room?"

# (account, project, end user) of each place; ERASED is filled in with the
# pseudonym of Carla once she is erased.
ERASED = object()
PLACES = {
    "anna": ("X", "P1", "anna"),
    "bruno": ("X", "P1", "bruno"),
    "hr": ("X", "P1", "team:hr"),
    "sales": ("X", "P1", "team:sales"),
    "nobody": ("X", "P1", None),
    "erased": ("X", "P1", ERASED),
    "p2_anna": ("X", "P2", "anna"),
    "p2_nobody": ("X", "P2", None),
    "y_anna": ("Y", "P1", "anna"),
    "y_nobody": ("Y", "P1", None),
}
COLUMNS = tuple(PLACES) + ("carla",)

# The keys: (account, project, end_users, scopes). `e_star`'s prefix is the
# first letter of every pseudonym (`erased:`): a confined key never reads
# an erased end user's rows, whatever its prefix matches.
_RW = ("memories:read", "memories:write")
KEYS = {
    "whole": ("X", "P1", None, _RW),
    "anna_hr": ("X", "P1", ["anna", "team:hr"], _RW),
    "bruno_sales": ("X", "P1", ["bruno", "team:sales"], _RW),
    "team_star": ("X", "P1", ["team:*"], _RW),
    "e_star": ("X", "P1", ["e*"], _RW),
    "whole_p2": ("X", "P2", None, _RW),
    "whole_y": ("Y", "P1", None, _RW),
    "admin": ("X", "P1", None, _RW + ("admin",)),
}

# What each key may read, place by place (1: it may). Carla was erased: no
# key reads anything of hers, under her id or any other.
SEES = {
    #               anna bruno hr sales nobody erased p2_anna p2_nobody y_anna y_nobody carla
    "whole":       (1,   1,    1, 1,    1,     1,     0,      0,        0,     0,       0),
    "anna_hr":     (1,   0,    1, 0,    0,     0,     0,      0,        0,     0,       0),
    "bruno_sales": (0,   1,    0, 1,    0,     0,     0,      0,        0,     0,       0),
    "team_star":   (0,   0,    1, 1,    0,     0,     0,      0,        0,     0,       0),
    "e_star":      (0,   0,    0, 0,    0,     0,     0,      0,        0,     0,       0),
    "whole_p2":    (0,   0,    0, 0,    0,     0,     1,      1,        0,     0,       0),
    "whole_y":     (0,   0,    0, 0,    0,     0,     0,      0,        1,     1,       0),
    "admin":       (1,   1,    1, 1,    1,     1,     0,      0,        0,     0,       0),
}
# The operator's routes (/admin) read the whole account, every project of
# it (EVERY_PROJECT, services/sentinels.py), by design: the admin key reads
# what the dashboard reads. The other keys are refused there.
SEES_ACCOUNT = {"admin": (1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0)}

# The user_ids every door is asked with: none, each end user of P1, the
# erased one by her old id.
ASKED = (None, "anna", "bruno", "team:hr", "team:sales", "carla")
# A tool's refusal, the MCP's 4xx: its exception, kept with its words.
REFUSED = "refused"


def allowed(table, key):
    return {c for c, bit in zip(COLUMNS, table[key]) if bit}


# The doors that read a person's trail: by the id asked and the pseudonyms an
# erasure left of it (services/pseudonyms.as_asked).
AS_A_PERSON = ("audit", "admin_audit")


def places_of(world, door, user, confined=None) -> set:
    """The places a read that asks for `user` may show: that end user's;
    on a door that reads a person's trail, the place of the pseudonym an
    erasure left of them (Carla's, `erased`); and on a door that reads the
    rows of no end user beside one end user's (`READS_THE_SHARED`), asked
    with a key of the whole project (`confined` None, its list otherwise),
    the places of no end user too (services/user_scope.with_the_shared: a
    confined key never reads them)."""
    from korely_agent.services.pseudonyms import pseudonym

    names = {user}
    if door in AS_A_PERSON:
        names.add(pseudonym(world.tenants["X"].id, user))
    if door in READS_THE_SHARED and confined is None:
        names.add(None)
    return {p for p, u in world.users.items() if u in names}


def marker(place):
    return f"[MK:{place}]"


def _vector(i):
    from korely_agent.core.llm import EMBEDDING_DIM

    v = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    v[1 + i] = 0.001 * (i + 1)
    return v


def query_vector():
    from korely_agent.core.llm import EMBEDDING_DIM

    return [1.0] + [0.0] * (EMBEDDING_DIM - 1)


def no_model(monkeypatch):
    """No model, one vector for every question, no facts mined."""
    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-tests")
    monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda: None)
    monkeypatch.setattr("korely_agent.core.llm.embed", lambda *a, **k: query_vector())
    monkeypatch.setattr("korely_agent.services.memory_engine.embed", lambda *a, **k: query_vector())
    monkeypatch.setattr("korely_agent.services.context.embed", lambda *a, **k: query_vector())
    monkeypatch.setattr("korely_agent.services.facts.extract_and_persist_facts",
                        lambda *a, **k: [])


@contextmanager
def a_client(db, monkeypatch):
    from fastapi.testclient import TestClient
    from korely_agent import config
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    config.get_settings.cache_clear()
    try:
        app = create_app()
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()
    finally:
        config.get_settings.cache_clear()


class World:
    """The two accounts, their projects, rows and keys, and how to tell
    which place a string of an answer comes from. Its accounts are new
    ones, so several worlds live side by side in one database."""

    def __init__(self, db, client):
        from korely_agent.core.auth import key_project
        from korely_agent.services import projects as project_service
        from korely_agent.services.ids import fact_public_id, mem_public_id
        from korely_agent.services.memory_engine import AgentMemoryEngine
        from korely_agent.services.pseudonyms import pseudonym

        self.db, self.client = db, client
        self.tenants = {}
        for name in ("X", "Y"):
            t = Tenant(id=uuid.uuid4(), name=f"Account {name}")
            db.add(t)
            db.commit()
            self.tenants[name] = t
        self.projects = {}
        for name, t in self.tenants.items():
            self.projects[(name, "P1")] = project_service.get_or_create_default_project(db, t.id).id
        self.projects[("X", "P2")] = project_service.create_project(
            db, tenant_id=self.tenants["X"].id, name="Second").id
        db.commit()

        self.raw, self.head = {}, {}
        for name, (acc, proj, end_users, scopes) in KEYS.items():
            _row, full = key_service.create_api_key(
                db, tenant_id=self.tenants[acc].id, name=f"k-{name}", scopes=list(scopes),
                project_id=self.projects[(acc, proj)], end_users=end_users)
            self.raw[name] = full
            self.head[name] = {"Authorization": f"Bearer {full}"}
        assert key_project(db, key_service.verify_api_key(db, self.raw["whole_p2"])) == \
            self.projects[("X", "P2")]

        # Carla, erased for real before anything is asked: her rows go, and
        # what an erasure keeps of her carries her pseudonym.
        x = self.tenants["X"]
        p1 = self.projects[("X", "P1")]
        carla = AgentMemoryEngine(db, x.id, p1).add(f"{BAIT} {marker('carla')}",
                                                    end_user_id="carla", agent_id="ag-carla")
        carla_fact = self._fact(x.id, p1, "carla", "carla", carla.id, len(PLACES))
        db.add(carla_fact)
        db.commit()
        self.carla_ids = {mem_public_id(carla.id), str(carla.id), fact_public_id(carla_fact.id),
                          str(carla_fact.id)}
        self.carla_mem, self.carla_fact = mem_public_id(carla.id), fact_public_id(carla_fact.id)
        for part in PARTS:
            self.carla_ids |= part.carla(self, x.id, p1)
        r = client.delete("/v1/users/carla/memories", headers=self.head["whole"])
        assert r.status_code == 200, r.text
        self.erased = pseudonym(x.id, "carla")
        assert self.erased and self.erased.startswith("erased:")

        # The rows, each place a second apart: every listing's order is
        # the same on every run.
        t0 = datetime.now(timezone.utc) - timedelta(days=1)
        self.users, self.mems, self.facts = {}, {}, {}
        self.strings = {}
        for i, (place, (acc, proj, user)) in enumerate(PLACES.items()):
            user = self.erased if user is ERASED else user
            self.users[place] = user
            tid, pid = self.tenants[acc].id, self.projects[(acc, proj)]
            at = t0 + timedelta(seconds=i)
            m = AgentMemory(id=uuid.uuid4(), tenant_id=tid, project_id=pid, end_user_id=user,
                            agent_id=f"ag-{place}", content=f"{BAIT} {marker(place)}",
                            meta={"m": "1"}, embedding=_vector(i), created_at=at)
            db.add(m)
            db.flush()
            f = self._fact(tid, pid, user, place, m.id, i)
            f.created_at = at
            db.add(f)
            db.flush()
            self.mems[place], self.facts[place] = m.id, f.id
            db.add(AuditEvent(tenant_id=tid, project_id=pid, actor="rest", action="write",
                              end_user_id=user, target_id=mem_public_id(m.id), ts=at))
            db.add(AgentEntityAlias(tenant_id=tid, project_id=pid, end_user_id=user,
                                    agent_id=f"ag-{place}", alias_lower=f"alias-{place}",
                                    alias_label=f"alias-{place}",
                                    canonical_label=f"canonical {marker(place)}", since=at))
            db.add(AgentObjectLink(tenant_id=tid, project_id=pid, end_user_id=user,
                                   agent_id=f"ag-{place}", name_label=f"name-{place}",
                                   canonical_label=f"object {marker(place)}", family="other",
                                   decided_by="review", source_memory_id=m.id, created_at=at))
            db.add(PredicateCluster(tenant_id=tid, project_id=pid, end_user_id=user,
                                    canonical=f"rel-{place}", created_at=at))
            self.strings[place] = {mem_public_id(m.id), str(m.id), fact_public_id(f.id),
                                   str(f.id), f"ag-{place}", f"subject-{place}",
                                   f"alias-{place}", f"name-{place}", f"rel-{place}"}
            for part in PARTS:
                self.strings[place] |= part.place(self, i, place, tid, pid, user, at)
        db.commit()
        self.strings["carla"] = self.carla_ids | {"ag-carla", "subject-carla", "carla"}
        # An end user's id names a place when only that place has it.
        counts = Counter(u for u in self.users.values() if u is not None)
        for place, user in self.users.items():
            if user is not None and counts[user] == 1:
                self.strings[place].add(user)
        self.jobs = self._jobs()

    def _jobs(self) -> dict:
        """One batch job per place, queued by the whole key of its project
        (the erased one excepted: no write names a pseudonym), and one by
        each confined key that can write, for its first end user: job ->
        (place, the key that queued it)."""
        jobs = {}
        whole_of = {("X", "P1"): "whole", ("X", "P2"): "whole_p2", ("Y", "P1"): "whole_y"}
        queued = [(place, whole_of[(acc, proj)], user)
                  for place, (acc, proj, user) in PLACES.items() if user is not ERASED]
        queued += [({"anna": "anna", "bruno": "bruno", "team:hr": "hr"}[ids[0]], k, ids[0])
                   for k, (_a, _p, ids, _s) in KEYS.items()
                   if ids is not None and ids[0] in ("anna", "bruno", "team:hr")]
        for place, key, user in queued:
            item = {"content": f"{BAIT} {marker(place)}", "agent_id": f"ag-{place}"}
            if user is not None:
                item["user_id"] = user
            r = self.client.post("/v1/batch", headers=self.head[key], json={"memories": [item]})
            assert r.status_code == 202, r.text
            jobs[r.json()["id"]] = (place, key)
            self.strings[place].add(r.json()["id"])
        return jobs

    @staticmethod
    def _fact(tid, pid, user, place, memory_id, i):
        return AgentFact(id=uuid.uuid4(), tenant_id=tid, project_id=pid, end_user_id=user,
                         agent_id=f"ag-{place}", subject_label=f"subject-{place}",
                         subject_type="unknown", predicate_raw="keeps", predicate_canonical="keeps",
                         object_label=f"{BAIT} {marker(place)}", object_is_literal=True,
                         confidence=0.9, source_memory_id=memory_id, embedding=_vector(i))

    def places_in(self, payload, asked=None) -> list:
        """Every place a string of `payload` comes from, once per string, in
        the order the answer gives them. `asked`, what the caller sent (a
        user_id, an id), is not a place when an answer repeats it: it is
        the caller's own word; several words of the caller's come as a tuple."""
        out = []
        echo = set(asked) if isinstance(asked, tuple) else {asked} - {None}

        def walk(v):
            if isinstance(v, dict):
                for k, x in v.items():
                    walk(k)
                    walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x)
            elif isinstance(v, str):
                for place in COLUMNS:
                    names = self.strings[place] - echo
                    if marker(place) in v or v in names:
                        out.append(place)
                    elif any(len(s) > 8 and s in v for s in names):
                        out.append(place)
        walk(payload)
        return out

    def numbers_in(self, payload) -> list:
        """The counts of an answer (`total`, a listing's per-row counts), by
        the field that holds them, in the answer's order."""
        out = []

        def walk(v, name=""):
            if isinstance(v, dict):
                for k, x in v.items():
                    walk(x, k)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x, name)
            elif isinstance(v, int) and not isinstance(v, bool):
                out.append([name, v])
        walk(payload)
        return out

    def untouched(self, place) -> bool:
        """Whether the place's rows are as the world wrote them: the memory
        there and live with its words, the fact true with its words, its
        alias, object link and vocabulary in place with theirs."""
        db = self.db
        db.expire_all()
        tid = self.tenants[PLACES[place][0]].id
        m = db.get(AgentMemory, self.mems[place])
        f = db.get(AgentFact, self.facts[place])
        aliases = db.query(AgentEntityAlias).filter(
            AgentEntityAlias.agent_id == f"ag-{place}", AgentEntityAlias.tenant_id == tid,
            AgentEntityAlias.canonical_label == f"canonical {marker(place)}").count()
        links = db.query(AgentObjectLink).filter(
            AgentObjectLink.agent_id == f"ag-{place}", AgentObjectLink.tenant_id == tid,
            AgentObjectLink.canonical_label == f"object {marker(place)}").count()
        vocabulary = db.query(PredicateCluster).filter(
            PredicateCluster.tenant_id == tid, PredicateCluster.canonical == f"rel-{place}").count()
        return (m is not None and m.deleted_at is None and marker(place) in m.content
                and f is not None and f.invalid_at is None and marker(place) in f.object_label
                and (aliases, links, vocabulary) == (1, 1, 1)
                and all(part.untouched(self, place) for part in PARTS))


def mcp(world, key, tool, **kw):
    from mcp.server.fastmcp.exceptions import ToolError

    from korely_agent.mcp import server as mcp_server

    async def go():
        await mcp_server.KorelyAgentKeyVerifier().verify_token(world.raw[key])
        # The tool as this key's account has it (a part's own korely_search
        # for an account the part serves: mcp/server.callers_tool).
        return await mcp_server._get_agent_mcp().callers_tool(tool).fn(**kw)
    try:
        return 200, asyncio.run(go())
    except ToolError as e:
        return REFUSED, str(e)


def _json(r):
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


def _q(**kw):
    return {k: v for k, v in kw.items() if v is not None}


# ── the doors that read, asked (client, world, key, user_id) ───────────────

def _get(path, **fixed):
    def door(c, w, k, u):
        return _json(c.get(path, headers=w.head[k], params=_q(user_id=u, **fixed)))
    return door


def _search(**extra):
    def door(c, w, k, u):
        return _json(c.post("/v1/memories/search", headers=w.head[k],
                            json=_q(query=BAIT, user_id=u, limit=50, **extra)))
    return door


def _tool(name, **fixed):
    def door(c, w, k, u):
        return mcp(w, k, name, **_q(user_id=u, **fixed))
    return door


ASKS_A_USER = {
    "search": _search(),
    "search_metadata": _search(metadata={"m": "1"}),
    # 1.0.13: the facts alone, and both kinds with their whole text.
    "search_facts": _search(types=["facts"]),
    "search_memories_and_facts": _search(types=["memories", "facts"], full_text=True),
    "list": _get("/v1/memories", limit=200),
    "facts": _get("/v1/facts", limit=200),
    "facts_closed_too": _get("/v1/facts", limit=200, include_invalidated="true"),
    "facts_by_entity": _get("/v1/facts", limit=200, entity="subject-anna"),
    "facts_by_subject": _get("/v1/facts", limit=200, subject="alias-bruno"),
    "profile": _get("/v1/profile"),
    "context": _get("/v1/context", query=BAIT),
    "events": _get("/v1/events", limit=200),
    "audit": _get("/v1/audit", limit=500),
    "mcp_search": _tool("korely_search", query=BAIT, limit=50),
    "mcp_search_facts": _tool("korely_search", query=BAIT, limit=50, types="facts"),
    "mcp_search_memories_and_facts": _tool("korely_search", query=BAIT, limit=50,
                                           types="memories,facts", full_text=True),
    "mcp_context": _tool("korely_get_context", query=BAIT),
    "mcp_facts": _tool("korely_get_facts", limit=200),
    "mcp_facts_by_entity": _tool("korely_get_facts", limit=200, entity="subject-anna"),
}
ASKS_NONE = {
    "users": lambda c, w, k, u: _json(c.get("/v1/users", headers=w.head[k], params={"limit": 200})),
    "agents": lambda c, w, k, u: _json(c.get("/v1/agents", headers=w.head[k],
                                             params={"limit": 200})),
}
OPERATORS = {
    "admin_aliases": _get("/admin/aliases"),
    "admin_links": _get("/admin/object-links"),
    "admin_audit": _get("/admin/audit"),
    "admin_memories": _get("/admin/memories"),
    "admin_search": _get("/admin/memories/search", query=BAIT, limit=50),
    "admin_facts": _get("/admin/facts"),
}


# ── the doors that take an id: (client, world, key, place) -> answer ───────
# Each is asked with the id of every place's row; a place's own row is what
# the key may touch, another's is a 404 (REST) or the tool's refusal (MCP),
# never a 403, and stays as it was.

def _mem(w, place):
    from korely_agent.services.ids import mem_public_id
    return w.carla_mem if place == "carla" else mem_public_id(w.mems[place])


def _fid(w, place):
    from korely_agent.services.ids import fact_public_id
    return w.carla_fact if place == "carla" else fact_public_id(w.facts[place])


READ_BY_ID = {
    "memory": lambda c, w, k, p: _json(c.get(f"/v1/memories/{_mem(w, p)}", headers=w.head[k])),
    "history": lambda c, w, k, p: _json(c.get(f"/v1/memories/{_mem(w, p)}/history",
                                              headers=w.head[k])),
}
WRITE_BY_ID = {
    "memory_patch": lambda c, w, k, p: _json(c.patch(f"/v1/memories/{_mem(w, p)}",
                                                     headers=w.head[k],
                                                     json={"content": "overwritten"})),
    "memory_delete": lambda c, w, k, p: _json(c.delete(f"/v1/memories/{_mem(w, p)}",
                                                       headers=w.head[k])),
    "fact_patch": lambda c, w, k, p: _json(c.patch(f"/v1/facts/{_fid(w, p)}", headers=w.head[k],
                                                   json={"object": "changed"})),
    "fact_forget": lambda c, w, k, p: _json(c.post(f"/v1/facts/{_fid(w, p)}/forget",
                                                   headers=w.head[k])),
    "mcp_correct_fact": lambda c, w, k, p: mcp(w, k, "korely_correct_fact", fact_id=_fid(w, p),
                                               object="changed"),
    "mcp_forget_memory": lambda c, w, k, p: mcp(w, k, "korely_forget_memory",
                                                memory_id=_mem(w, p)),
}
ASKED_BY_ID = {**{d: (lambda w, p: _mem(w, p)) for d in
                  ("memory", "history", "memory_patch", "memory_delete", "mcp_forget_memory")},
               **{d: (lambda w, p: _fid(w, p)) for d in
                  ("fact_patch", "fact_forget", "mcp_correct_fact")}}


def _batch(c, w, k, job):
    return _json(c.get(f"/v1/batch/{job}", headers=w.head[k]))


def batch_sees(key, queued_by) -> bool:
    """A job is read by the keys of its project that are not confined, and
    by the confined key that queued it (GET /v1/batch/{job_id})."""
    acc, proj, ids, _s = KEYS[key]
    q_acc, q_proj, _i, _s = KEYS[queued_by]
    return (acc, proj) == (q_acc, q_proj) and (ids is None or key == queued_by)


# ── the doors that write for an end user or an agent: (c, w, k, name) ──────
# Every one is asked with each end user of P1 (and Carla), or each place's
# agent; a key confined to others is refused (403, or the tool's refusal),
# and every place the key may not read stays as it was.

FOR_A_USER = {
    "user_names": lambda c, w, k, u: _json(c.post(f"/v1/users/{u}/names", headers=w.head[k],
                                                  json={"name": f"Another {u}"})),
    "user_erase": lambda c, w, k, u: _json(c.delete(f"/v1/users/{u}/memories",
                                                    headers=w.head[k])),
    "mcp_add": lambda c, w, k, u: mcp(w, k, "korely_add", content=f"A note for {u}",
                                      user_id=u),
    "memory_write": lambda c, w, k, u: _json(c.post("/v1/memories", headers=w.head[k],
                                                    json={"content": f"A note for {u}",
                                                          "user_id": u})),
    "fact_write": lambda c, w, k, u: _json(c.post("/v1/facts", headers=w.head[k],
                                                  json={"subject": "the office",
                                                        "predicate": "keeps",
                                                        "object": f"a key for {u}",
                                                        "user_id": u})),
    "batch_write": lambda c, w, k, u: _json(c.post("/v1/batch", headers=w.head[k],
                                                   json={"memories": [{"content": f"For {u}",
                                                                       "user_id": u}]})),
}
USERS_ASKED = ("anna", "bruno", "team:hr", "team:sales", "carla")
# An erasure changes the keys confined to the person (their list takes the
# pseudonym, and a key left with nobody is revoked:
# end_user_places._pseudonymise_lists): each end user is erased in a world
# of its own.
ONE_WORLD_EACH = ("user_erase",)
FOR_AN_AGENT = {
    "agent_delete": lambda c, w, k, a: _json(c.delete(f"/v1/agents/{a}", headers=w.head[k])),
}

READS = sorted(ASKS_A_USER) + sorted(ASKS_NONE) + sorted(OPERATORS) + sorted(READ_BY_ID) \
    + ["batch"]
WRITES = sorted(WRITE_BY_ID) + sorted(FOR_A_USER) + sorted(FOR_AN_AGENT)
DOORS = READS + WRITES

# The parts' doors (`PARTS`), in the tables above, and how a door of theirs
# that answers its own way refuses (a 200 whose every item is refused), does
# not find (a 200 that names nothing) or answers a row the key may touch:
# (world, key, asked, status, payload) -> bool.
# The parts' worlds: each adds its rows (`carla`, `place`), says whether a
# place's are untouched (`untouched`) and brings its doors (`doors`, by the
# table they join), the doors' own refusals and not-founds (`refused_as`,
# `not_found_as`) and those whose control asks each key's own places to
# show (`shows_its_own`). Read here, once the doors they build on are.
PARTS = cloud.of_the_parts("WALL_PARTS", tuple)
_TABLES = {"ASKS_A_USER": ASKS_A_USER, "ASKS_NONE": ASKS_NONE, "OPERATORS": OPERATORS,
           "READ_BY_ID": READ_BY_ID, "WRITE_BY_ID": WRITE_BY_ID, "FOR_A_USER": FOR_A_USER,
           "FOR_AN_AGENT": FOR_AN_AGENT}
PART_DOORS: list = []
REFUSED_AS: dict = {}
NOT_FOUND_AS: dict = {}
OWN_AS: dict = {}
SHOWS_ITS_OWN: list = []
#: The doors whose read for one end user also has the rows of no end user,
#: for a key of the whole project (`places_of`): a part's, by its own rule.
READS_THE_SHARED: list = []
for _part in PARTS:
    for _table, _doors in _part.doors.items():
        assert not set(_doors) & set(_TABLES[_table]), _doors
        _TABLES[_table].update(_doors)
        PART_DOORS += sorted(_doors)
        if _table in ("WRITE_BY_ID", "FOR_A_USER", "FOR_AN_AGENT"):
            WRITES += sorted(_doors)
    ASKED_BY_ID.update(_part.asked_by_id)
    REFUSED_AS.update(_part.refused_as)
    NOT_FOUND_AS.update(_part.not_found_as)
    OWN_AS.update(_part.own_as)
    SHOWS_ITS_OWN += list(_part.shows_its_own)
    READS_THE_SHARED += list(getattr(_part, "reads_the_shared", ()))


def ask(db, client, door):
    """Every key through `door`: ({(key, asked): (status, payload, what was
    sent, the world it was asked in)}, {key: the places it may not read
    that the door changed}). A door that writes is asked in a world of its
    own for each key, so what one key did is not what the next one finds;
    a door that reads, in one world for all of them."""
    out, changed = {}, {}
    world = None
    for key in KEYS:
        if door in WRITES or world is None:
            world = World(db, client)
        w = world

        def put(name, answer, sent):
            out[(key, name)] = (answer[0], answer[1], sent, w)

        if door in ASKS_A_USER or door in OPERATORS:
            fn = ASKS_A_USER.get(door) or OPERATORS[door]
            for user in ASKED:
                put(user, fn(client, w, key, user), user)
        elif door in ASKS_NONE:
            put(None, ASKS_NONE[door](client, w, key, None), None)
        elif door in READ_BY_ID or door in WRITE_BY_ID:
            fn = READ_BY_ID.get(door) or WRITE_BY_ID[door]
            for place in COLUMNS:
                put(place, fn(client, w, key, place), ASKED_BY_ID[door](w, place))
        elif door == "batch":
            for job, (place, queued_by) in sorted(w.jobs.items(), key=lambda j: j[1]):
                put(f"{place}@{queued_by}", _batch(client, w, key, job), job)
        elif door in FOR_A_USER:
            for user in USERS_ASKED:
                if door in ONE_WORLD_EACH and user != USERS_ASKED[0]:
                    changed.setdefault(key, []).extend(_changed(w, key))
                    w = World(db, client)
                put(user, FOR_A_USER[door](client, w, key, user), user)
        else:
            for place in COLUMNS:
                agent = f"ag-{place}"
                put(place, FOR_AN_AGENT[door](client, w, key, agent), agent)
        if door in WRITES:
            changed[key] = sorted(changed.get(key, []) + _changed(w, key))
    return out, changed


def _changed(w, key) -> list:
    return [p for p in PLACES if p not in allowed(SEES, key) and not w.untouched(p)]
