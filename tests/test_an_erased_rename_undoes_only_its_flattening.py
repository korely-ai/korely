"""Erasing the memory that taught a rename undoes exactly what the rename did
(audit of 20 Oct 2026).

- The undo of "Acme -> Globex" pointed back at "Acme" every older alias pointing at
  "Globex" with no fact of its own, so "GX -> Globex", declared by hand before
  the rename and never touched by it, became "GX -> Acme". Each alias now
  keeps what it was stated with (`stated_canonical`), and the undo changes
  only the rows whose stated chain went through the rule
  (`store.restore_the_chain`).
- The erasure left derived copies, the vectors: a kept fact embedded as "Globex uses Slack" while the alias stood went back to
  "Acme" with the vector of the erased name, still 'done'. A fact whose
  canonical names change waits for a vector of its current text.
"""
import uuid
from datetime import datetime, timezone

import pytest

from korely_agent.core.llm import EMBEDDING_DIM
from korely_agent.db.models import AgentEntityAlias, AgentFact, AgentMemory
from korely_agent.services import keys as key_service
from korely_agent.services.ids import mem_public_id
from tests.conftest import seeded_project_id, stub_extraction


U = "collaudo+g4-alias@korely.ai"


@pytest.fixture
def keyed(monkeypatch):
    from korely_agent.config import get_settings

    monkeypatch.setenv("SECRET_KEY", "a-long-random-string-for-the-tests")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app

    monkeypatch.setenv("RUN_WORKER", "false")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _head(db, tenant, pid, scopes=None):
    kw = {"scopes": scopes} if scopes else {}
    _r, full = key_service.create_api_key(db, tenant_id=tenant.id, name=f"k-{uuid.uuid4().hex[:6]}",
                                          project_id=pid, expires_in_days=None, **kw)
    return {"Authorization": f"Bearer {full}"}


def _admin(db, tenant, pid):
    return _head(db, tenant, pid, list(key_service.DEFAULT_SCOPES) + [key_service.ADMIN_SCOPE])


def _mem(db, tenant, pid, text):
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=U,
                    content=text, embedding=[0.1] * EMBEDDING_DIM, graph_sync_status="done")
    db.add(m)
    db.commit()
    return m.id


def _fact(db, tenant, pid, mid, s, pc, o, *, embedded=False):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=U,
                  subject_label=s, subject_canonical=s, predicate_raw=pc.replace("_", " "),
                  predicate_canonical=pc, object_label=o, object_canonical=o,
                  source_memory_id=mid, source_memory_ids=[str(mid)],
                  valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc))
    if embedded:
        f.embedding = [0.2] * EMBEDDING_DIM
        f.embedding_status = "done"
    db.add(f)
    db.commit()
    return f.id


def _memory(tenant, pid):
    from korely_agent.services.facts import entities

    return entities.memory_of(tenant.id, pid, U, None)


def _canonicalize(db, tenant, pid):
    from korely_agent.services.facts import entities
    from korely_agent.services.memory_core import store

    store.recanonicalize(db, entities.TABLES, _memory(tenant, pid))
    db.commit()


def _rename(db, tenant, pid, old, new):
    """A memory saying "<old> is now called <new>", its identity fact and the
    alias it teaches, as `writing._alias_from_identity` writes it."""
    from korely_agent.services.facts import entities
    from korely_agent.services.memory_core import store
    from korely_agent.services.memory_core.identity import SOURCE_IDENTITY_FACT

    mid = _mem(db, tenant, pid, f"{old} is now called {new}.")
    f = _fact(db, tenant, pid, mid, old, "renamed_to", new)
    store.add_alias(db, entities.TABLES, _memory(tenant, pid), old, new,
                            source=SOURCE_IDENTITY_FACT, fact_id=f)
    db.commit()
    return mid


def _declare(db, tenant, pid, alias, canonical):
    from korely_agent.services.facts import entities

    row, outcome = entities.declare_alias(db, tenant.id, pid, U, None, alias, canonical)
    assert row is not None, outcome
    db.commit()


def _aliases(db, tenant):
    db.expire_all()
    return sorted((a.alias_lower, a.canonical_label, a.source) for a in db.query(AgentEntityAlias)
                  .filter(AgentEntityAlias.tenant_id == tenant.id, AgentEntityAlias.end_user_id == U))


def _erase(db, tenant, pid, client, mid):
    r = client.delete(f"/v1/memories/{mem_public_id(mid)}", params={"erase": "true"},
                      headers=_head(db, tenant, pid))
    assert r.status_code == 200, r.text
    db.expire_all()


def test_an_alias_declared_by_hand_before_the_rename_stays(keyed, db, tenant, client):
    """The audit's path: POST /admin/aliases "GX -> Globex", a kept fact by
    GX, then a memory teaching "Acme -> Globex", erased. GX still is Globex,
    and nothing kept says "Acme", a name only the erased memory stated."""
    pid = seeded_project_id(db, tenant.id)
    r = client.post("/admin/aliases", headers=_admin(db, tenant, pid),
                    json={"alias": "GX", "canonical": "Globex", "user_id": U})
    assert r.status_code == 201, r.text
    kept = _fact(db, tenant, pid, _mem(db, tenant, pid, "We buy from GX."), U, "buys_from", "GX")
    _canonicalize(db, tenant, pid)
    erased = _rename(db, tenant, pid, "Acme", "Globex")
    assert _aliases(db, tenant) == [("acme", "Globex", "rename_fact"), ("gx", "Globex", "user")]

    _erase(db, tenant, pid, client, erased)

    assert _aliases(db, tenant) == [("gx", "Globex", "user")]
    assert db.get(AgentFact, kept).object_canonical == "Globex"
    facts = client.get("/v1/facts", headers=_head(db, tenant, pid), params={"user_id": U}).text
    assert "Acme" not in facts


def test_the_alias_the_rename_flattened_goes_back(keyed, db, tenant, client):
    """The run-3 property, kept: "GX -> Acme", flattened to "GX -> Globex" by
    the rename, points at "Acme" again once the rename is erased."""
    pid = seeded_project_id(db, tenant.id)
    _declare(db, tenant, pid, "GX", "Acme")
    kept = _fact(db, tenant, pid, _mem(db, tenant, pid, "We buy from GX."), U, "buys_from", "GX")
    _canonicalize(db, tenant, pid)
    erased = _rename(db, tenant, pid, "Acme", "Globex")
    assert _aliases(db, tenant) == [("acme", "Globex", "rename_fact"), ("gx", "Globex", "user")]
    assert db.get(AgentFact, kept).object_canonical == "Globex"

    _erase(db, tenant, pid, client, erased)

    assert _aliases(db, tenant) == [("gx", "Acme", "user")]
    assert db.get(AgentFact, kept).object_canonical == "Acme"


def test_an_alias_resolved_through_the_rename_goes_back_to_what_was_stated(keyed, db, tenant, client):
    """"GX -> Acme" declared while "Acme -> Globex" stands is stored pointing
    at "Globex" (the chain resolved on the write); without the rename it is
    what was declared, "Acme", whatever its age."""
    pid = seeded_project_id(db, tenant.id)
    erased = _rename(db, tenant, pid, "Acme", "Globex")
    _declare(db, tenant, pid, "GX", "Acme")
    assert _aliases(db, tenant) == [("acme", "Globex", "rename_fact"), ("gx", "Globex", "user")]

    _erase(db, tenant, pid, client, erased)

    assert _aliases(db, tenant) == [("gx", "Acme", "user")]


def test_a_rename_of_the_declared_name_is_undone_too(keyed, db, tenant, client):
    """By design a later rename of the target flattens a declared alias
    ("GX -> Globex", then "Globex -> Acme": GX is Acme); its erasure puts the
    declared row back as it was declared."""
    pid = seeded_project_id(db, tenant.id)
    _declare(db, tenant, pid, "GX", "Globex")
    erased = _rename(db, tenant, pid, "Globex", "Acme")
    assert _aliases(db, tenant) == [("globex", "Acme", "rename_fact"), ("gx", "Acme", "user")]

    _erase(db, tenant, pid, client, erased)

    assert _aliases(db, tenant) == [("gx", "Globex", "user")]


@pytest.mark.parametrize("which", ["first", "second"])
def test_a_chain_of_two_renames_loses_only_the_erased_link(keyed, db, tenant, client, which):
    """"GX -> Acme", then "Acme -> Globex", then "Globex -> Initech": every
    row points at Initech. Erasing one rename takes only that link out of
    every chain: the first, and GX is Acme again while Globex stays Initech;
    the second, and GX and Acme are Globex."""
    pid = seeded_project_id(db, tenant.id)
    _declare(db, tenant, pid, "GX", "Acme")
    first = _rename(db, tenant, pid, "Acme", "Globex")
    second = _rename(db, tenant, pid, "Globex", "Initech")
    assert _aliases(db, tenant) == [("acme", "Initech", "rename_fact"),
                                    ("globex", "Initech", "rename_fact"),
                                    ("gx", "Initech", "user")]

    _erase(db, tenant, pid, client, first if which == "first" else second)

    if which == "first":
        assert _aliases(db, tenant) == [("globex", "Initech", "rename_fact"), ("gx", "Acme", "user")]
    else:
        assert _aliases(db, tenant) == [("acme", "Globex", "rename_fact"), ("gx", "Globex", "user")]


def test_a_row_written_before_the_column_is_left_as_it_is(keyed, db, tenant, client):
    """A row from before `stated_canonical` (NULL) holds its canonical as it
    is: which flattening rewrote it was never recorded, so the undo does not
    guess and leaves it."""
    pid = seeded_project_id(db, tenant.id)
    _declare(db, tenant, pid, "GX", "Acme")
    erased = _rename(db, tenant, pid, "Acme", "Globex")
    db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant.id).update(
        {AgentEntityAlias.stated_canonical: None})
    db.commit()

    _erase(db, tenant, pid, client, erased)

    assert _aliases(db, tenant) == [("gx", "Globex", "user")]


def test_removing_a_rename_from_the_dashboard_puts_its_chain_back(keyed, db, tenant, client):
    """DELETE /admin/aliases is the same undo (`store.remove_alias`): the
    rows the rename flattened point where they were stated."""
    pid = seeded_project_id(db, tenant.id)
    _declare(db, tenant, pid, "GX", "Acme")
    _declare(db, tenant, pid, "GXX", "Globex")
    _rename(db, tenant, pid, "Acme", "Globex")
    rename = db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant.id,
                                               AgentEntityAlias.alias_lower == "acme").one()
    r = client.delete(f"/admin/aliases/{rename.id}", headers=_admin(db, tenant, pid))
    assert r.status_code == 200, r.text

    assert _aliases(db, tenant) == [("gx", "Acme", "user"), ("gxx", "Globex", "user")]


def test_a_fact_whose_names_change_waits_for_a_new_vector(keyed, db, tenant, client):
    """Embedded as "Acme uses Slack", renamed to "Globex uses Slack", and back
    to "Acme" when the rename is erased: each time the vector of the old text
    goes and the fact waits for the drain (`vector_state.cleared`). A fact
    whose names stay keeps its vector."""
    pid = seeded_project_id(db, tenant.id)
    kept_mem = _mem(db, tenant, pid, "Acme uses Slack. Anna likes tea.")
    kept = _fact(db, tenant, pid, kept_mem, "Acme", "uses", "Slack", embedded=True)
    other = _fact(db, tenant, pid, kept_mem, "Anna", "likes", "tea", embedded=True)

    erased = _rename(db, tenant, pid, "Acme", "Globex")
    db.expire_all()
    f = db.get(AgentFact, kept)
    assert f.subject_canonical == "Globex"
    assert f.embedding is None and f.embedding_status == "pending" and f.embedding_attempts == 0
    assert db.get(AgentFact, other).embedding_status == "done"

    # The drain computed the vector of "Globex uses Slack" meanwhile.
    f.embedding, f.embedding_status = [0.3] * EMBEDDING_DIM, "done"
    db.commit()

    _erase(db, tenant, pid, client, erased)

    f = db.get(AgentFact, kept)
    assert f.subject_canonical == "Acme"
    assert f.embedding is None and f.embedding_status == "pending"
    o = db.get(AgentFact, other)
    assert o.embedding is not None and o.embedding_status == "done"


def test_the_real_write_path_leaves_no_vector_of_the_erased_name(keyed, db, tenant, client, monkeypatch):
    """The audit's reproduction (V2), through the engine's own write
    (`extract_and_persist_facts` -> `write_fact`, the identity alias made by
    `_alias_from_identity`), extraction and judge stubbed: the kept fact,
    embedded on "Globex uses Slack", has no vector after the erasure, and is
    waiting for the vector of "Acme uses Slack"."""
    from korely_agent.services import vectors
    from korely_agent.services.facts import contradiction
    from korely_agent.services.facts import service as fsvc
    from korely_agent.services.memory_core.fact_extractor import ExtractedFact

    embedded = []
    monkeypatch.setattr(fsvc, "fact_vector", lambda text: embedded.append(text) or [0.1] * EMBEDDING_DIM)
    monkeypatch.setattr(fsvc, "detect_and_invalidate", lambda *a, **k: [])
    monkeypatch.setattr(contradiction, "detect_and_invalidate_batch", lambda *a, **k: {})
    monkeypatch.setattr(contradiction, "judge_after_the_vector", lambda *a, **k: {})
    by_text = {
        "Acme is now called Globex.": [
            ExtractedFact("Acme", "renamed_to", "Globex", 0.9, subject_type="organization")],
        "Acme uses Slack.": [ExtractedFact("Acme", "uses", "Slack", 0.9, subject_type="organization")],
    }
    stub_extraction(monkeypatch, fsvc, lambda content, **kw: by_text[content])

    def write(text):
        m = AgentMemory(tenant_id=tenant.id, project_id=pid, end_user_id=U, content=text,
                        graph_sync_status="pending", embedding=[0.1] * EMBEDDING_DIM)
        db.add(m)
        db.commit()
        db.refresh(m)
        fsvc.extract_and_persist_facts(db, tenant.id, memory=m)
        db.commit()
        return m.id

    pid = seeded_project_id(db, tenant.id)
    erased = write("Acme is now called Globex.")
    embedded.clear()
    write("Acme uses Slack.")
    assert embedded == ["Globex uses Slack"]

    _erase(db, tenant, pid, client, erased)

    f = db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id, AgentFact.end_user_id == U).one()
    assert (f.subject_canonical, f.object_canonical) == ("Acme", "Slack")
    assert f.embedding is None and f.embedding_status == "pending"
    # What the vectors drain will embed: the fact's current text.
    assert vectors._fact_text(f) == "Acme uses Slack"


# ── the erasure's derived copies, the labels (audit of 27 Oct 2026) ──────────
# A restatement only adds its memory to the fact's sources; the labels stay
# the first writer's. The erased memory taught "Acme -> Globex" and wrote
# "Globex hired Bob"; the kept one said "Acme hired Bob".

@pytest.mark.parametrize("forgotten_first", [False, True], ids=["live", "forgotten-first"])
def test_a_kept_fact_first_written_by_the_erased_rename_is_written_again(db, tenant, keyed,
                                                                          forgotten_first):
    """Erased live, or forgotten first (DELETE /v1/memories/{id}, the
    dashboard's Remove, then the erasure): that forget re-pointed the fact's
    `source_memory_id` to the kept memory, and until the audit before
    1.0.0 the erasure no longer saw it as the erased memory's."""
    from korely_agent.services import facts as agent_facts

    pid = seeded_project_id(db, tenant.id)
    m1 = _rename(db, tenant, pid, "Acme", "Globex")
    m2 = _mem(db, tenant, pid, "Acme hired Bob.")
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=U,
                  subject_label="Globex", subject_canonical="Globex", predicate_raw="hired",
                  predicate_canonical="hired", object_label="Bob", object_canonical="Bob",
                  source_memory_id=m1, source_memory_ids=[str(m1), str(m2)],
                  observation_count=2, valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    if forgotten_first:
        assert agent_facts.forget_memory(db, tenant.id, m1, project_id=pid)
    agent_facts.erase_memory(db, tenant.id, m1, project_id=pid)
    db.expire_all()
    said = {(x.subject_label, x.subject_canonical) for x in db.query(AgentFact)
            .filter(AgentFact.tenant_id == tenant.id)}
    assert not any("Globex" in s for pair in said for s in pair), said
    assert db.get(AgentMemory, m2).graph_sync_status == "pending", "the kept memory writes it again"


def test_a_forgotten_renames_erasure_leaves_the_facts_born_before_it(db, tenant, keyed):
    """The facts the forget could have re-pointed are the erased memory's
    candidates only from when it was written: "Acme hired Carl", stated
    before the rename by a kept memory, carries the old name and is not the
    erased memory's; it stays, and its memory is not read again."""
    from korely_agent.services import facts as agent_facts

    pid = seeded_project_id(db, tenant.id)
    m0 = _mem(db, tenant, pid, "Acme hired Carl.")
    _fact(db, tenant, pid, m0, "Acme", "hired", "Carl")
    m1 = _rename(db, tenant, pid, "Acme", "Globex")
    assert agent_facts.forget_memory(db, tenant.id, m1, project_id=pid)
    agent_facts.erase_memory(db, tenant.id, m1, project_id=pid)
    db.expire_all()
    said = {(x.subject_label, x.subject_canonical, x.object_label) for x in db.query(AgentFact)
            .filter(AgentFact.tenant_id == tenant.id)}
    assert said == {("Acme", "Acme", "Carl")}, said
    assert db.get(AgentMemory, m0).graph_sync_status == "done"


# ── the first writer is kept, never inferred (review of the audit before 1.0.0) ─
# A forget or an edit re-points `source_memory_id` to the kept memory; the
# erasure reads the first writer from `first_memory_id`, which the database
# sets at the insert (db/triggers.py). Inferred from the labels, it deleted
# facts the erased memory never wrote.

def _restated(db, tenant, pid, first, kept, s, o):
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=U,
                  subject_label=s, subject_canonical=s, predicate_raw="hired",
                  predicate_canonical="hired", object_label=o, object_canonical=o,
                  source_memory_id=first, source_memory_ids=[str(first), str(kept)],
                  observation_count=2, valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    return f.id


def test_a_forgotten_renames_erasure_leaves_the_facts_other_memories_wrote(db, tenant, keyed):
    """Probe A: the rename forgotten, then "Acme hired Carol" said and
    forgotten (its fact closed), "Acme hired Dave" said and kept. Erasing
    the rename leaves both facts, and Dave's memory is not read again."""
    from korely_agent.services import facts as agent_facts

    pid = seeded_project_id(db, tenant.id)
    m1 = _rename(db, tenant, pid, "Acme", "Globex")
    assert agent_facts.forget_memory(db, tenant.id, m1, project_id=pid)
    m3 = _mem(db, tenant, pid, "Acme hired Carol.")
    carol = _fact(db, tenant, pid, m3, "Acme", "hired", "Carol")
    assert agent_facts.forget_memory(db, tenant.id, m3, project_id=pid)
    m4 = _mem(db, tenant, pid, "Acme hired Dave.")
    dave = _fact(db, tenant, pid, m4, "Acme", "hired", "Dave")
    agent_facts.erase_memory(db, tenant.id, m1, project_id=pid)
    db.expire_all()
    assert db.get(AgentFact, carol) is not None, "a forgotten memory's history was deleted"
    assert db.get(AgentFact, dave).invalid_at is None
    assert db.get(AgentMemory, m4).graph_sync_status == "done"


def test_a_renames_erasure_leaves_a_fact_a_forget_moved_to_it(db, tenant, keyed):
    """Probe A's other half, the one the code of ba43445 failed: "Acme hired
    Dave" written first by M0 and restated by the rename memory and by M4;
    M0 forgotten, which moved the fact's `source_memory_id` to the rename
    memory. The rename's erasure took it as its own (first writer read from
    `source_memory_id`), deleted it and read M4 again. Its first writer is
    M0: it loses the erased source and stays, with M4."""
    from korely_agent.services import facts as agent_facts

    pid = seeded_project_id(db, tenant.id)
    m0 = _mem(db, tenant, pid, "Acme hired Dave.")
    m4 = _mem(db, tenant, pid, "Acme hired Dave, as I said.")
    m1 = _rename(db, tenant, pid, "Acme", "Globex")
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=U,
                  subject_label="Acme", subject_canonical="Globex", predicate_raw="hired",
                  predicate_canonical="hired", object_label="Dave", object_canonical="Dave",
                  source_memory_id=m0, source_memory_ids=[str(m0), str(m1), str(m4)],
                  observation_count=3, valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    assert agent_facts.forget_memory(db, tenant.id, m0, project_id=pid)
    db.expire_all()
    assert db.get(AgentFact, f.id).source_memory_id == m1, "the forget moved it to the rename"
    agent_facts.erase_memory(db, tenant.id, m1, project_id=pid)
    db.expire_all()
    kept = db.get(AgentFact, f.id)
    assert kept is not None and kept.invalid_at is None, "a fact M0 wrote was deleted"
    assert kept.source_memory_ids == [str(m4)]
    assert db.get(AgentMemory, m4).graph_sync_status == "done"


def test_a_correction_made_after_the_forget_stays(db, tenant, keyed):
    """Probe B: "Globex hired Bob" (the rename's, restated by "Acme hired
    Bob"), the rename forgotten, then the fact corrected to Robert: a new
    fact, the old one closed by it (services/facts.correct_fact). Erasing
    the rename takes the closed fact, which carries its name, keeps the
    correction, and does not read the kept memory again, which would write
    the corrected value back."""
    from korely_agent.services import facts as agent_facts

    pid = seeded_project_id(db, tenant.id)
    m1 = _rename(db, tenant, pid, "Acme", "Globex")
    m2 = _mem(db, tenant, pid, "Acme hired Bob.")
    old = _restated(db, tenant, pid, m1, m2, "Globex", "Bob")
    assert agent_facts.forget_memory(db, tenant.id, m1, project_id=pid)
    db.expire_all()
    before = db.get(AgentFact, old)
    assert before.source_memory_id == m2, "the forget moved it to the kept memory"
    fixed = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=pid, end_user_id=U,
                      subject_label="Globex", subject_canonical="Globex", predicate_raw="hired",
                      predicate_canonical="hired", object_label="Robert",
                      object_canonical="Robert", source_memory_id=m2,
                      source_memory_ids=[str(m2)], model_used="correction",
                      valid_from=datetime.now(timezone.utc))
    db.add(fixed)
    db.flush()
    before.invalid_at = datetime.now(timezone.utc)
    before.invalidated_by_id = fixed.id
    db.commit()
    agent_facts.erase_memory(db, tenant.id, m1, project_id=pid)
    db.expire_all()
    assert db.get(AgentFact, old) is None, "the closed fact keeps the erased memory's words"
    kept = db.get(AgentFact, fixed.id)
    assert kept is not None and kept.invalid_at is None, "the correction was undone"
    assert db.get(AgentMemory, m2).graph_sync_status == "done"


def test_an_edited_renames_erasure_rewrites_the_kept_facts_it_wrote_first(db, tenant, keyed):
    """The rename memory edited so it no longer states the rename (the
    edit's retirement, `store.retire_the_previous_version`, moves the
    restated fact to the kept memory), then erased: the fact it wrote first
    goes and the kept memory writes it again (review of the audit before
    1.0.0)."""
    from korely_agent.services import facts as agent_facts
    from korely_agent.services.facts import entities
    from korely_agent.services.facts.temporal import not_ended
    from korely_agent.services.memory_core import store

    pid = seeded_project_id(db, tenant.id)
    m1 = _rename(db, tenant, pid, "Acme", "Globex")
    m2 = _mem(db, tenant, pid, "Acme hired Bob.")
    f = _restated(db, tenant, pid, m1, m2, "Globex", "Bob")
    db.get(AgentMemory, m1).content = "Nothing about Acme."
    store.retire_the_previous_version(
        db, entities.TABLES, m1, set(), when=datetime.now(timezone.utc), is_open=not_ended(),
        scope=entities.memory_scope(tenant.id, pid, U))
    db.commit()
    db.expire_all()
    assert db.get(AgentFact, f).source_memory_id == m2, "the edit moved it to the kept memory"
    agent_facts.erase_memory(db, tenant.id, m1, project_id=pid)
    db.expire_all()
    said = {(x.subject_label, x.subject_canonical) for x in db.query(AgentFact)
            .filter(AgentFact.tenant_id == tenant.id)}
    assert not any("Globex" in s for pair in said for s in pair), said
    assert db.get(AgentMemory, m2).graph_sync_status == "pending"


def test_the_facts_written_again_are_found_and_deleted_by_the_database(db, tenant, keyed,
                                                                         monkeypatch):
    """Review of the audit before 1.0.0: the facts written again were loaded whole,
    vectors included, deleted one statement each, cut silently at
    MAX_FIRST_WRITTEN, and locked after the memories' order. Now: no row of
    a fact comes back, one DELETE, every one of them, the facts locked
    before the memories are queued."""
    from sqlalchemy import event

    from korely_agent.services import facts as agent_facts
    from korely_agent.services.facts import erasure

    pid = seeded_project_id(db, tenant.id)
    m1 = _rename(db, tenant, pid, "Acme", "Globex")
    m2 = _mem(db, tenant, pid, "Acme hired Bob, Carl and Dora.")
    for who in ("Bob", "Carl", "Dora"):
        _restated(db, tenant, pid, m1, m2, "Globex", who)
    monkeypatch.setattr(erasure, "MAX_FIRST_WRITTEN", 2, raising=False)
    seen = []

    def record(conn, cursor, statement, params, context, executemany):
        seen.append(" ".join(statement.split()))

    real = erasure._written_again

    def watched(*a, **k):
        bind = db.get_bind()
        event.listen(bind, "before_cursor_execute", record)
        try:
            return real(*a, **k)
        finally:
            event.remove(bind, "before_cursor_execute", record)

    monkeypatch.setattr(erasure, "_written_again", watched)
    agent_facts.erase_memory(db, tenant.id, m1, project_id=pid)
    db.expire_all()
    assert not db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id,
                                          AgentFact.subject_label == "Globex").count()
    assert not [s for s in seen if s.startswith("SELECT") and "agent_facts.embedding" in s]
    assert len([s for s in seen if s.startswith("DELETE FROM agent_facts")]) == 1, seen
    facts_locked = next(i for i, s in enumerate(seen) if "FOR UPDATE" in s and "agent_facts" in s)
    queued = next(i for i, s in enumerate(seen) if s.startswith("UPDATE agent_memories"))
    assert facts_locked < queued, seen
