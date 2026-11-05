"""Deleting a project without `erase` reads what moves, not what it moves into
(audit of 20 Oct 2026).

The move ran in the shared API process and read, in one transaction: every
open fact of the deleted project whole, vector included, with one read of its
twin each (`_fold_duplicates`); every relation of the default project with its
centroid and every phrase there, of every end user, even when the deleted
project was empty (`predicates.merge_vocabulary`); and the whole memory again,
vectors included, once per alias it moved (`store.add_alias` ->
`recanonicalize`). Measured by the verifier: about 42 KiB of peak memory per
open fact moved and 41 KiB per relation of the default project.

Now: the colliding pairs come from one read of ids and only their rows are
loaded, without the vector (`store.open_twins`); the vocabulary merges
in five statements decided by the database; each memory that took an alias is
recanonicalized once, without its vectors. And the rows the move leaves are
the ones the old code left: the second half of this file runs both on the
same data and compares every row.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from korely_agent.core.embedding_model import DIMENSIONS
from korely_agent.db.models import (AgentEntityAlias, AgentFact, AgentProject, PredicateAlias,
                                    PredicateCluster, Tenant)
from korely_agent.services import predicates
from korely_agent.services import projects as project_service
from korely_agent.services.facts import entities as _ent
from korely_agent.services.memory_core import store as _store


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _vec(k: int) -> list:
    v = [0.0] * DIMENSIONS
    v[k % DIMENSIONS] = 1.0
    v[(k * 7 + 3) % DIMENSIONS] += 0.5
    return v


class _Statements:
    """Every statement the database is sent while the block runs."""

    def __init__(self, db):
        self.bind = db.get_bind()
        self.seen: list = []

    def _take(self, conn, cursor, statement, params, context, executemany):
        self.seen.append(" ".join(statement.split()))

    def __enter__(self):
        event.listen(self.bind, "before_cursor_execute", self._take)
        return self

    def __exit__(self, *exc):
        event.remove(self.bind, "before_cursor_execute", self._take)

    def vectors(self):
        """The statements that read a fact's vector (not `embedding_status`
        and the other columns named after it)."""
        return [s for s in self.seen if re.search(r"agent_facts\.embedding(?![_a-z])", s.lower())]

    def selects(self, table):
        return [s for s in self.seen if s.upper().startswith("SELECT") and f" FROM {table} " in f"{s} "]


def _fact(db, tenant_id, project_id, s, p, o, *, eu="u1", agent=None, days=0, sources=(),
          conf=0.9, tense="current", invalid_days=None, session_only=False, family=None,
          invalidated_by=None, k=0):
    f = AgentFact(
        id=uuid.uuid4(), tenant_id=tenant_id, project_id=project_id, end_user_id=eu,
        agent_id=agent, subject_label=s, subject_canonical=s, predicate_raw=p,
        predicate_canonical=p, object_label=o, object_canonical=o, tense=tense,
        valid_from=NOW - timedelta(days=400 - days),
        invalid_at=(NOW - timedelta(days=invalid_days)) if invalid_days is not None else None,
        invalidated_by_id=invalidated_by, confidence=conf,
        source_memory_ids=list(sources) or None, observation_count=max(1, len(sources)),
        last_confirmed_at=NOW - timedelta(days=100 - days), session_only=session_only,
        predicate_family=family, created_at=NOW - timedelta(days=300 - days),
        embedding=_vec(k), embedding_status="done")
    db.add(f)
    db.flush()
    return f


def _alias(db, tenant_id, project_id, alias, canonical, *, eu="u1", agent=None, days=0,
           source="user", fact=None):
    db.add(AgentEntityAlias(
        tenant_id=tenant_id, project_id=project_id, end_user_id=eu, agent_id=agent,
        alias_lower=alias.lower(), alias_label=alias, canonical_label=canonical,
        since=NOW - timedelta(days=60 - days), source=source, confidence=1.0,
        source_fact_id=fact.id if fact is not None else None,
        created_at=NOW - timedelta(days=60 - days)))
    db.flush()


def _relation(db, tenant_id, project_id, eu, canonical, *phrases, k=0, functional=None):
    c = PredicateCluster(id=uuid.uuid4(), tenant_id=tenant_id, project_id=project_id,
                         end_user_id=eu, canonical=canonical, centroid=_vec(1000 + k),
                         is_functional=functional, family="other",
                         example_object=f"value {k}", member_count=k)
    db.add(c)
    db.flush()
    db.add_all([PredicateAlias(tenant_id=tenant_id, project_id=project_id, end_user_id=eu,
                               surface=p, cluster_id=c.id) for p in (canonical, *phrases)])
    db.flush()
    return c


# ---------------------------------------------------------------------------
# what a move reads
# ---------------------------------------------------------------------------

def _no_vector_and_no_whole_vocabulary(st):
    assert not [s for s in st.seen if "centroid" in s.lower()], "a centroid was read"
    assert st.vectors() == [], "a fact's vector was read"
    assert st.selects("predicate_clusters") == [], "relations were read into the process"
    assert st.selects("predicate_aliases") == [], "phrases were read into the process"


class TestWhatTheMoveReads:
    def test_an_empty_project_reads_nothing_of_the_default_project(self, db, tenant):
        default = project_service.get_or_create_default_project(db, tenant.id)
        for u in range(10):
            for r in range(3):
                _relation(db, tenant.id, default.id, f"user-{u}", f"relation_{r}", f"phrase {r}",
                          k=u * 3 + r)
            _fact(db, tenant.id, default.id, f"user-{u}", "likes", "tea", eu=f"user-{u}", k=u)
        db.commit()
        empty = project_service.create_project(db, tenant_id=tenant.id, name="Empty")
        db.expunge_all()
        empty = db.get(type(empty), empty.id)

        with _Statements(db) as st:
            out = project_service.delete_project(db, tenant.id, empty)

        assert (out["facts"], out["facts_merged"], out["relations_moved"]) == (0, 0, 0)
        _no_vector_and_no_whole_vocabulary(st)
        # The one row that cuts the facts into blocks (projects._fold_duplicates,
        # audit of 27 Oct 2026) and the one read of the pairs.
        assert len(st.selects("agent_facts")) <= 2, "more than the one read of the pairs"
        assert db.query(PredicateCluster).filter(PredicateCluster.tenant_id == tenant.id).count() == 30

    def test_open_facts_are_paired_in_one_read_without_their_vectors(self, db, tenant):
        default = project_service.get_or_create_default_project(db, tenant.id)
        gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
        for i in range(60):
            _fact(db, tenant.id, gamma.id, "c4821", "likes", f"thing {i}", days=i, k=i)
        for i in range(0, 60, 6):        # ten of them already open in the default project
            _fact(db, tenant.id, default.id, "c4821", "likes", f"THING {i}", days=100 + i, k=500 + i)
        _relation(db, tenant.id, gamma.id, "u1", "likes", "enjoys", k=1)
        _relation(db, tenant.id, default.id, "u1", "likes", k=2)
        db.commit()
        db.expunge_all()
        gamma = db.get(type(gamma), gamma.id)

        with _Statements(db) as st:
            out = project_service.delete_project(db, tenant.id, gamma)

        assert out["facts_merged"] == 10 and out["facts"] == 60, "the older rows, Gamma's, are kept"
        _no_vector_and_no_whole_vocabulary(st)
        # The block's cut (one row), one read of the pairs and one of the ten
        # pairs' rows, never one per fact.
        assert len(st.selects("agent_facts")) == 3, st.selects("agent_facts")
        db.expire_all()
        open_ = db.query(AgentFact).filter(AgentFact.tenant_id == tenant.id,
                                           AgentFact.invalid_at.is_(None)).all()
        assert len(open_) == 60 and all(f.project_id == default.id for f in open_)
        assert all(f.embedding is not None for f in open_), "a vector was lost on the way"

    def test_each_memory_is_recanonicalized_once_whatever_the_aliases(self, db, tenant, monkeypatch):
        default = project_service.get_or_create_default_project(db, tenant.id)
        gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
        for i in range(6):
            _alias(db, tenant.id, gamma.id, f"Name {i}", f"Person {i}", days=i)
            _fact(db, tenant.id, default.id, f"Name {i}", "likes", "tea", k=i)
        _alias(db, tenant.id, gamma.id, "Bot A", "Bot", eu="u2", agent="bot")
        _alias(db, tenant.id, gamma.id, "Bot B", "Bot", eu="u2", agent="bot", days=1)
        db.commit()
        calls = []
        real = _store.recanonicalize

        def counted(db_, tables, mem, **kw):
            calls.append(mem)
            return real(db_, tables, mem, **kw)

        monkeypatch.setattr(_store, "recanonicalize", counted)
        with _Statements(db) as st:
            out = project_service.delete_project(db, tenant.id, gamma)

        assert (out["aliases_moved"], out["aliases_not_moved"]) == (8, 0)
        assert len(calls) == 2, "one recanonicalization per memory that took an alias"
        assert st.vectors() == [], "a fact's vector was read"
        db.expire_all()
        assert sorted(f.subject_canonical for f in db.query(AgentFact)
                      .filter(AgentFact.tenant_id == tenant.id)) == [f"Person {i}" for i in range(6)]


def test_merge_vocabulary_alone_sends_five_statements_and_reads_no_row(db, tenant):
    default = project_service.get_or_create_default_project(db, tenant.id)
    gamma = project_service.create_project(db, tenant_id=tenant.id, name="Gamma")
    for u in range(5):
        _relation(db, tenant.id, default.id, f"user-{u}", "subscribes_to", "is on plan", k=u)
    _relation(db, tenant.id, gamma.id, "user-1", "subscribes_to", "has plan", k=9)
    db.commit()
    with _Statements(db) as st:
        moved = predicates.merge_vocabulary(db, source=(tenant.id, gamma.id),
                                            target=(tenant.id, default.id), move_the_rest=True)
    assert moved == 0
    assert len(st.seen) == 5 and not [s for s in st.seen if s.upper().startswith("SELECT")], st.seen


# ---------------------------------------------------------------------------
# the same rows as before
# ---------------------------------------------------------------------------
# The three helpers as they were at origin/main 1521b37 (0.2.1-dev), copied
# verbatim but for the imports: the reference the new ones are compared with.

def _old_merge_vocabulary(db, *, source, target, move_the_rest):
    from sqlalchemy import and_

    def of(scope, M):
        tenant_id, project_id = scope
        return and_(M.tenant_id == tenant_id, M.project_id.is_not_distinct_from(project_id))

    to_tenant, to_project = target
    theirs = {(c.end_user_id, c.canonical): c.id
              for c in db.query(PredicateCluster).filter(of(target, PredicateCluster))}
    their_phrases = {(a.end_user_id, a.surface)
                     for a in db.query(PredicateAlias).filter(of(target, PredicateAlias))}
    for alias in db.query(PredicateAlias).filter(of(source, PredicateAlias)).all():
        if (alias.end_user_id, alias.surface) in their_phrases:
            db.delete(alias)
    db.flush()
    moved = 0
    for cluster in db.query(PredicateCluster).filter(of(source, PredicateCluster)).all():
        twin = theirs.get((cluster.end_user_id, cluster.canonical))
        phrases = db.query(PredicateAlias).filter(PredicateAlias.cluster_id == cluster.id)
        where = {PredicateAlias.tenant_id: to_tenant, PredicateAlias.project_id: to_project}
        if twin is not None:
            phrases.update({**where, PredicateAlias.cluster_id: twin}, synchronize_session=False)
            db.delete(cluster)
        elif move_the_rest:
            cluster.tenant_id, cluster.project_id = to_tenant, to_project
            phrases.update(where, synchronize_session=False)
            moved += 1
    db.flush()
    return moved


def _old_move_vocabulary(db, tenant_id, from_id, to_id):
    return _old_merge_vocabulary(db, source=(tenant_id, from_id), target=(tenant_id, to_id),
                                 move_the_rest=True)


def _old_move_aliases(db, tenant_id, from_id, to_id):
    A = AgentEntityAlias
    rows = (db.query(A)
              .filter(A.tenant_id == tenant_id, A.project_id == from_id)
              .order_by(A.since.asc(), A.created_at.asc())
              .all())
    moved = not_moved = 0
    for r in rows:
        mem = _ent.memory_of(tenant_id, to_id, r.end_user_id, r.agent_id)
        there = db.query(A).filter(mem.alias, A.alias_lower == r.alias_lower).first()
        if there is not None:
            same = there.canonical_label.strip().lower() == r.canonical_label.strip().lower()
            moved += int(same)
            not_moved += int(not same)
        else:
            new, outcome = _store.add_alias(
                db, _ent.TABLES, mem, r.alias_label, r.canonical_label,
                source=r.source, confidence=r.confidence, fact_id=r.source_fact_id)
            if outcome == "created":
                new.since = r.since
                moved += 1
            elif outcome == "same":
                moved += 1
            else:
                not_moved += 1
        db.delete(r)
    db.flush()
    return moved, not_moved


def _old_fold_duplicates(db, tenant_id, from_id, to_id):
    from korely_agent.core.times import aware

    def age(f):
        return (aware(f.created_at) or datetime.min.replace(tzinfo=timezone.utc), str(f.id))

    folded = 0
    for f in (db.query(AgentFact)
                .filter(AgentFact.tenant_id == tenant_id, AgentFact.project_id == from_id,
                        AgentFact.invalid_at.is_(None))
                .all()):
        twin = _store.find_open(db, _ent.TABLES, _ent.memory_of(tenant_id, to_id, f.end_user_id, f.agent_id),
                                  f.subject_canonical, f.predicate_canonical, f.object_canonical)
        if twin is None:
            continue
        keep, loser = sorted((f, twin), key=age)
        _store.merge_into(db, _ent.TABLES, keep, loser)
        folded += 1
    if folded:
        db.flush()
    return folded


def _build(db, name):
    """One account with a default project and Gamma, the same rows every time
    but for the ids: duplicates to fold (either side older, a plan and the same
    thing done, another agent's, the facts of no end user, a closed fact
    pointing at a loser), aliases that collapse the default project's facts
    (one, and a chain of two in the same memory), a name decided differently,
    a cycle, the same decision; relations with twins, without, a phrase the
    default project resolves, the relations of no end user."""
    t = Tenant(id=uuid.uuid4(), name=name)
    db.add(t)
    db.commit()
    D = project_service.get_or_create_default_project(db, t.id).id
    G = project_service.create_project(db, tenant_id=t.id, name="Gamma").id
    T = t.id
    # u1, no agent: duplicates.
    _fact(db, T, D, "c4821", "subscribes_to", "Priority Support plan", days=5, sources=["m1"], conf=0.8, k=1)
    loser = _fact(db, T, G, "c4821", "subscribes_to", "priority support plan", days=8, sources=["m2"],
                  conf=0.95, session_only=True, family="ownership", k=2)
    _fact(db, T, D, "c4821", "subscribes_to", "Basic plan", days=1, sources=["m0"], invalid_days=30,
          invalidated_by=loser.id, k=3)
    _fact(db, T, D, "c4821", "lives_in", "Rome", days=9, sources=["m3"], k=4)
    _fact(db, T, G, "c4821", "lives_in", "Rome", days=2, sources=["m4", "m3"], session_only=True, k=5)
    _fact(db, T, G, "c4821", "visits", "Japan", days=1, tense="planned", sources=["m5"], k=6)
    _fact(db, T, D, "c4821", "visits", "Japan", days=7, sources=["m6"], k=7)
    _fact(db, T, G, "c4821", "works_at", "Initech", days=0, invalid_days=1, k=8)
    _fact(db, T, D, "c4821", "works_at", "Initech", days=3, k=9)
    # u1: what the moved aliases collapse in the default project.
    _fact(db, T, D, "Acme", "uses", "Salesforce", days=2, sources=["m7"], k=10)
    _fact(db, T, D, "Globex", "uses", "Salesforce", days=6, sources=["m8"], conf=0.99, k=11)
    gp = _fact(db, T, D, "Gordon Pro", "uses", "FastAPI", days=1, sources=["a"], k=12)
    _fact(db, T, D, "Gordon", "uses", "FastAPI", days=4, sources=["b"], k=13)
    _fact(db, T, D, "Korely", "uses", "FastAPI", days=6, sources=["c"], k=14)
    _fact(db, T, G, "Mary", "likes", "tea", days=7, sources=["m9"], k=15)
    _fact(db, T, D, "Maria", "likes", "tea", days=2, sources=["m10"], k=16)
    _alias(db, T, G, "Acme", "Globex", days=0)
    _alias(db, T, G, "Gordon Pro", "Gordon", days=1, fact=gp)
    _alias(db, T, G, "Gordon", "Korely", days=2)
    _alias(db, T, G, "Mary", "Maria", days=3, source="rename_fact")
    _alias(db, T, G, "Acme Corp", "Globex", days=4)
    _alias(db, T, D, "Acme Corp", "Globex", days=0)
    # u1, agent bot; and the facts of no end user.
    _fact(db, T, G, "c4821", "subscribes_to", "Priority Support plan", agent="bot", days=9, k=17)
    _fact(db, T, D, "c4821", "subscribes_to", "Priority Support plan", agent="bot", days=8, k=18)
    _fact(db, T, G, "Team", "uses", "Slack", eu=None, days=3, sources=["s1"], k=19)
    _fact(db, T, D, "Team", "uses", "Slack", eu=None, days=4, sources=["s2"], k=20)
    # u2: a name decided otherwise, a cycle.
    _alias(db, T, D, "Mary", "Marie", eu="u2")
    _alias(db, T, G, "Mary", "Maria", eu="u2", days=5)
    _alias(db, T, D, "Bob", "Robert", eu="u2")
    _alias(db, T, G, "Robert", "Bob", eu="u2", days=6)
    _fact(db, T, G, "Bob", "likes", "chess", eu="u2", days=5, k=21)
    # The vocabularies.
    _relation(db, T, D, "u1", "subscribes_to", "is on plan", k=1, functional=True)
    _relation(db, T, G, "u1", "subscribes_to", "is on plan", "has plan", k=2)
    _relation(db, T, G, "u1", "lives_in", "resides in", k=3, functional=True)
    _relation(db, T, D, None, "uses", k=4)
    _relation(db, T, G, None, "uses", "utilizes", k=5)
    _relation(db, T, G, "u3", "likes", k=6)
    _relation(db, T, D, "u2", "likes", k=7)
    _relation(db, T, G, "u2", "plays", "likes", k=8)
    db.commit()
    return t


def _snapshot(db, tenant_id):
    db.expire_all()
    names = {p.id: p.name for p in db.query(AgentProject).filter(AgentProject.tenant_id == tenant_id)}
    facts = db.query(AgentFact).filter(AgentFact.tenant_id == tenant_id).all()

    def key(f):
        return (names.get(f.project_id), f.end_user_id, f.agent_id, f.subject_label,
                f.predicate_raw, f.object_label, f.tense, f.created_at)

    by_id = {f.id: key(f) for f in facts}
    fact_rows = sorted([(key(f), f.subject_canonical, f.object_canonical, f.predicate_canonical,
                         f.invalid_at, by_id.get(f.invalidated_by_id), f.source_memory_ids,
                         f.observation_count, f.confidence, f.last_confirmed_at, f.session_only,
                         f.predicate_family, f.valid_from,
                         None if f.embedding is None else [float(x) for x in f.embedding])
                        for f in facts], key=repr)
    alias_rows = sorted([(names.get(a.project_id), a.end_user_id, a.agent_id, a.alias_lower,
                          a.alias_label, a.canonical_label, a.since, a.source, a.confidence,
                          by_id.get(a.source_fact_id))
                         for a in db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant_id)],
                        key=repr)
    clusters = db.query(PredicateCluster).filter(PredicateCluster.tenant_id == tenant_id).all()
    rel_rows = sorted([
        (names.get(c.project_id), c.end_user_id or "", c.canonical, c.is_functional, c.family,
         c.example_object, c.member_count, [float(x) for x in c.centroid],
         sorted((names.get(a.project_id), a.end_user_id or "", a.surface)
                for a in db.query(PredicateAlias).filter(PredicateAlias.cluster_id == c.id)))
        for c in clusters], key=repr)
    stray = db.query(PredicateAlias).filter(PredicateAlias.tenant_id == tenant_id).count()
    return {"facts": fact_rows, "aliases": alias_rows, "relations": rel_rows, "phrases": stray}


def test_the_move_leaves_the_rows_the_old_code_left(db, monkeypatch):
    before_t = _build(db, "Before")
    after_t = _build(db, "After")

    with monkeypatch.context() as m:
        m.setattr(project_service, "_fold_duplicates", _old_fold_duplicates)
        m.setattr(project_service, "_move_aliases", _old_move_aliases)
        m.setattr(project_service, "_move_vocabulary", _old_move_vocabulary)
        g = db.query(AgentProject).filter(AgentProject.tenant_id == before_t.id,
                                          AgentProject.name == "Gamma").one()
        out_before = project_service.delete_project(db, before_t.id, g)
    g = db.query(AgentProject).filter(AgentProject.tenant_id == after_t.id,
                                      AgentProject.name == "Gamma").one()
    out_after = project_service.delete_project(db, after_t.id, g)

    assert out_after == out_before
    assert out_after["facts_merged"] == 5 and out_after["relations_moved"] == 3
    assert (out_after["aliases_moved"], out_after["aliases_not_moved"]) == (5, 2)
    before, after = _snapshot(db, before_t.id), _snapshot(db, after_t.id)
    for part in ("facts", "aliases", "relations", "phrases"):
        assert after[part] == before[part], part
    # And the fixture did exercise what it is there for.
    open_uses = [r for r in after["facts"] if r[0][4] == "uses" and r[4] is None and r[0][1] == "u1"]
    assert sorted(r[1] for r in open_uses) == ["Globex", "Korely"], open_uses
