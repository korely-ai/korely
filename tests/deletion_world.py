"""A small world for the tests that back the deletion claims (tests/test_deletion_*.py).

One scripted writer stands in for every model call (`core.llm.get_provider`):
it records EVERY prompt it is sent, answers the extraction with the facts a
test declares for a memory's text, and the judge by closing what a test calls
stale. One deterministic embedder stands in for the vectors. No model is
called, no network is reached.

The writer's reading of a memory is a plain table, text -> facts, filled by
the test (`World.says`): the test decides what the model "understood", so it
can assert that what the model never gets to see, or leaves behind, is what
the product promised.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text

from korely_agent.core.llm import EMBEDDING_DIM, LLMResult

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def vector(s: str) -> List[float]:
    h = hashlib.sha256(s.encode()).digest()
    return [((h[i % 32] / 255.0) - 0.5) for i in range(EMBEDDING_DIM)]


@dataclass
class Fact:
    subject: str
    predicate: str
    obj: str
    tense: str = "current"
    # The date the model reads in the text ("YYYY-MM-DD"), or none.
    valid_from: str = ""


class Writer:
    """The scripted model. `prompts` is everything it was ever sent."""

    name = "scripted"

    def __init__(self):
        self.prompts: List[str] = []
        self.reads: Dict[str, List[Fact]] = {}
        # Words in an EXISTING-facts line that make the judge close that fact.
        self.stale: Tuple[str, ...] = ()
        # One-shot hooks, run while a model call is in flight, by the kind of
        # the call: "extraction", "judge" or "vocabulary". A test uses one to
        # do something else (a forget, an erasure, from another session)
        # at the moment the model is being asked, which is how a write in
        # flight meets a deletion deterministically.
        self.hooks: Dict[str, object] = {}
        self.calls: List[str] = []

    def _hook(self, kind: str):
        self.calls.append(kind)
        fn = self.hooks.pop(kind, None)
        if fn is not None:
            fn()

    # the extraction prompt ends with "Memory (date: ...):\n<text>\n"
    def _memory_of(self, p: str) -> Optional[str]:
        if "You extract knowledge-graph facts" not in p:
            return None
        return re.split(r"\nMemory(?: \(date: [^)]*\))?:\n", p)[-1].strip()

    def complete(self, messages, **kw):
        p = messages[0]["content"]
        self.prompts.append(p)
        mem = self._memory_of(p)
        if mem is not None:
            self._hook("extraction")
            facts = [{"subject": f.subject, "subject_type": "person", "predicate": f.predicate,
                      "object": f.obj, "object_is_literal": False, "tense": f.tense,
                      "valid_from": f.valid_from, "confidence": 0.9, "evidence": mem}
                     for f in self.reads.get(mem, [])]
            return LLMResult(text=json.dumps({"facts": facts, "observation": None}))
        if "NEW facts and their candidate EXISTING facts:" in p or "EXISTING facts:" in p:
            self._hook("judge")
        elif '"family"' in p or "many_values" in p or '{"past": false}' in p or "same_as" in p:
            self._hook("vocabulary")
        if "NEW facts and their candidate EXISTING facts:" in p:
            # The batched judge: one block per new fact, its candidates under it.
            body = p.split("NEW facts and their candidate EXISTING facts:", 1)[1]
            results = []
            for block in re.split(r"\n(?=\[new_id=)", body.strip()):
                m = re.match(r"\[new_id=([^\]]+)\]", block)
                if not m:
                    continue
                ids = [UUID_RE.search(line).group(0) for line in block.splitlines()
                       if line.strip().startswith("- id=")
                       and any(x in line.split("[single")[0] for x in self.stale)]
                results.append({"new_id": m.group(1), "contradictions": [
                    {"existing_id": i, "reason": "stale"} for i in ids]})
            return LLMResult(text=json.dumps({"results": results}))
        if "EXISTING facts:" in p:
            existing = p.split("EXISTING facts:", 1)[1].split("NEW fact", 1)[0]
            ids = [UUID_RE.search(line).group(0) for line in existing.splitlines()
                   if UUID_RE.search(line) and any(s in line for s in self.stale)]
            return LLMResult(text='{"contradictions": [%s]}' % ", ".join(
                '{"existing_id": "%s", "reason": "stale"}' % i for i in ids))
        if '"family"' in p:
            return LLMResult(text='{"family": "other"}')
        if "many_values" in p:
            return LLMResult(text='{"many_values": false}')
        if '{"past": false}' in p:
            return LLMResult(text='{"past": false}')
        return LLMResult(text='{"same_as": null}')

    def seen(self, needle: str, since: int = 0) -> List[int]:
        """The indexes of the prompts from `since` on that hold `needle`."""
        return [i for i, p in enumerate(self.prompts) if i >= since and needle in p]


def dump_rows_with(db, needle: str) -> List[str]:
    """Every row, of every table, whose text form holds `needle`: "table: id"."""
    from korely_agent.db import Base

    out = []
    db.rollback()
    for t in Base.metadata.sorted_tables:
        rows = db.execute(text(f'SELECT t::text FROM "{t.name}" AS t WHERE t::text ILIKE :n'),
                          {"n": f"%{needle}%"}).fetchall()
        out += [f"{t.name}: {r[0][:80]}" for r in rows]
    return out


# ── the fixture: a product that runs, with the scripted writer ──────────────

from tests import cloud
import pytest  # noqa: E402


class World(Writer):
    """The scripted writer, plus the product around it: a client with a key
    of the account's default project (`use` changes the project), the
    database session and the account."""

    def __init__(self, db, tenant, client):
        super().__init__()
        self.db, self.tenant, self.client = db, tenant, client

    def use(self, project_id=None, name=None):
        """Write and read with a new key bound to `project_id` (the default
        project when None)."""
        from korely_agent.services import keys as key_service

        _r, key = key_service.create_api_key(self.db, tenant_id=self.tenant.id,
                                             name=name or f"k-{uuid.uuid4().hex[:6]}",
                                             project_id=project_id)
        self.client.headers["Authorization"] = f"Bearer {key}"

    def write(self, content, user="u1", agent=None, **extra):
        body = {"content": content, "user_id": user, **extra}
        if agent:
            body["agent_id"] = agent
        r = self.client.post("/v1/memories", json=body)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def drain(self, limit=50):
        from korely_agent.services.facts import service as S

        out = S.process_pending_memories(self.db, limit=limit)
        # A group that outlasts the round ends in its own thread
        # (core/threads.Lanes): wait for it, as the next round would.
        from korely_agent.services.facts import drain
        from tests.thread_helpers import wait_idle

        wait_idle(drain._draining, 60)
        return out


@pytest.fixture
def world(db, tenant, monkeypatch):
    from fastapi.testclient import TestClient

    from korely_agent.config import get_settings
    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("WRITE_ASYNC", "false")  # the facts come back before the answer
    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        w = World(db, tenant, c)
        monkeypatch.setattr("korely_agent.core.llm.get_provider", lambda *a, **k: w)
        monkeypatch.setattr("korely_agent.core.llm.current_google_key", lambda: "k")
        # `embed` is imported by name in several modules: every copy is replaced.
        import sys

        from korely_agent.core import llm

        real = llm.embed
        for mod in list(sys.modules.values()):
            if getattr(mod, "__name__", "").startswith("korely_agent") \
                    and getattr(mod, "embed", None) is real:
                monkeypatch.setattr(mod, "embed", lambda t, **kw: vector(t))
        w.use()
        yield w
    app.dependency_overrides.clear()


# ── the orphans: a row that points at a row that is gone ────────────────────

def orphans(db) -> List[str]:
    """Every row that points at a row that is not there (or not live), by
    what the schema says and by what it only says in a JSON list or in the
    meaning of a row. Empty when the store is whole. Each line names the kind
    and the table, never a row's words."""
    db.rollback()
    q = lambda sql: [tuple(r) for r in db.execute(text(sql)).fetchall()]  # noqa: E731
    out: List[str] = []

    # A fact without its source memory: the id in `source_memory_id`, and
    # every id of the JSON list `source_memory_ids`.
    for (n,) in q("SELECT count(*) FROM agent_facts f WHERE f.source_memory_id IS NOT NULL AND NOT EXISTS "
                  "(SELECT 1 FROM agent_memories m WHERE m.id = f.source_memory_id)"):
        out += ["fact whose source_memory_id is gone"] * n
    # A fact posted by hand holds JSON null there (memory_core writes None).
    for (n,) in q("SELECT count(*) FROM agent_facts f, json_array_elements_text("
                  "CASE WHEN json_typeof(f.source_memory_ids) = 'array' THEN f.source_memory_ids "
                  "ELSE '[]'::json END) AS s "
                  "WHERE f.source_memory_ids IS NOT NULL AND NOT EXISTS "
                  "(SELECT 1 FROM agent_memories m WHERE m.id::text = s)"):
        out += ["fact whose source_memory_ids names a memory that is gone"] * n
    # A fact that is still true with no live memory behind it (every source
    # forgotten): a fact written by hand has no source and is not this.
    for (n,) in q("SELECT count(*) FROM agent_facts f WHERE f.invalid_at IS NULL AND "
                  "f.source_memory_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM agent_memories m "
                  "WHERE m.deleted_at IS NULL AND (m.id = f.source_memory_id OR "
                  "(f.source_memory_ids IS NOT NULL AND f.source_memory_ids::text LIKE '%' || m.id::text || '%')))"):
        out += ["open fact whose every source memory is forgotten"] * n
    # A supersession pointing at a fact that is gone.
    for (n,) in q("SELECT count(*) FROM agent_facts f WHERE f.invalidated_by_id IS NOT NULL AND NOT EXISTS "
                  "(SELECT 1 FROM agent_facts g WHERE g.id = f.invalidated_by_id)"):
        out += ["fact superseded by a fact that is gone"] * n
    # An alias learned from a fact: the fact is gone when the link is NULL on
    # a row the writer made (a user's own alias has no fact).
    for (n,) in q("SELECT count(*) FROM agent_entity_aliases a WHERE a.source <> 'user' AND "
                  "a.source_fact_id IS NULL"):
        out += ["alias whose fact is gone"] * n
    for (n,) in q("SELECT count(*) FROM agent_entity_aliases a WHERE a.source_fact_id IS NOT NULL AND "
                  "NOT EXISTS (SELECT 1 FROM agent_facts f WHERE f.id = a.source_fact_id)"):
        out += ["alias pointing at a fact that is gone"] * n
    # An object link: its memories (cascade) and the fact it was decided on.
    for (n,) in q("SELECT count(*) FROM agent_object_links l WHERE l.source_fact_id IS NULL"):
        out += ["object link whose fact is gone"] * n
    for (n,) in q("SELECT count(*) FROM agent_object_links l WHERE (l.source_memory_id IS NOT NULL AND NOT EXISTS "
                  "(SELECT 1 FROM agent_memories m WHERE m.id = l.source_memory_id)) OR "
                  "(l.canonical_memory_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM agent_memories m "
                  "WHERE m.id = l.canonical_memory_id))"):
        out += ["object link whose memory is gone"] * n
    # The vocabulary and its vectors: a centroid (a vector) with no
    # example it was computed from; an example no fact of that end user states.
    for (n,) in q("SELECT count(*) FROM predicate_clusters c WHERE c.centroid IS NOT NULL AND "
                  "c.example_object IS NULL"):
        out += ["centroid with no example (a vector without its row)"] * n
    for (n,) in q("SELECT count(*) FROM predicate_clusters c WHERE c.example_object IS NOT NULL AND NOT EXISTS "
                  "(SELECT 1 FROM agent_facts f WHERE f.tenant_id = c.tenant_id AND "
                  "f.end_user_id IS NOT DISTINCT FROM c.end_user_id AND f.object_label = c.example_object)"):
        out += ["relation example that no fact states any more"] * n
    for (n,) in q("SELECT count(*) FROM predicate_aliases a WHERE NOT EXISTS "
                  "(SELECT 1 FROM predicate_clusters c WHERE c.id = a.cluster_id)"):
        out += ["predicate alias of a relation that is gone"] * n
    # A part of the product's rows without the rows they belong to
    # (core/extensions.Part).
    for orphans_of_a_part in cloud.of_the_parts("ORPHANS", tuple):
        out += orphans_of_a_part(q)
    return out
