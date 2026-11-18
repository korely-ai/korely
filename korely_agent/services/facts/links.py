"""How the reads see the links "the same as" (memory_core 6.1.0 `objects`, off by default).

A link (`agent_object_links`) says that two objects of one memory are one
thing named in two languages or forms ("Italy" and "Italia"), for the facts of
the relation family it was decided in, and only those: "Florence" linked to
"Firenze" among the memory's places leaves an aunt called Florence, a fact of
the family "people", as she was written, and a family never asked
(`objects.NEVER_ASKED`) is never read through a link. It is written
by the writer's judgement (facts/objects.py) and changes NO fact: the rows
keep their labels, canonical names, dates, tense, evidence and sources, as
the switch-off engine writes them. The reads resolve it here:

- GET /v1/facts, GET /v1/profile, korely_get_facts and the dashboard's
  facts (`serialize_fact`): `subject_canonical` and `object_canonical` are
  the name the memory read the object as first (the link's canonical, its
  chain followed), `subject` and `object` stay each fact's own words, and
  `object_link_id` / `subject_link_id` name the link that resolved them (the
  one DELETE /admin/object-links/{id} takes back);
- `entity=` and `subject=` find the facts of every name linked to the one
  asked, in the fact's own memory, as an alias is read memory by memory
  (`entities.name_condition`);
- `/v1/context` shows the linked facts under the one name and says one
  thing once: two facts that read the same triple through a link are one
  line, with both facts among its sources;
- with `known_as_of`, only the links the store held then (created by then),
  in the filter and the names alike:
  what the store knew that day; `as_of` is a moment of the world, and a link
  is about names, not a fact that begins or ends.

The memory search (POST /v1/memories/search, korely_search) returns the
memories' own text, which keeps its own words: there is no name to resolve
there.

Undo: deleting the row. Nothing else was changed by it, so the facts are
then exactly as before the link, read as two objects again.

An alias is not a link: a declared alias (POST /admin/aliases, POST
/v1/users/{id}/names) and a rename's alias rewrite the facts' canonical
names and merge two open duplicates into one row (memory_core
`store.add_alias`, `store.recanonicalize`), and DELETE /admin/aliases/{id}
cannot split the row they merged. That behaviour is unchanged.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import and_, false, func, literal, or_
from sqlalchemy.orm import Session

from ...db.models import AgentFact, AgentObjectLink
from ..memory_core import objects as _objects
from ..sentinels import EVERY_PROJECT

_CACHE = "korely.object_links"

MemoryKey = Tuple[Any, Any, Optional[str], Optional[str]]


def memory_key(row: Any) -> MemoryKey:
    """The memory a fact or a link is in: account, project, end user, agent."""
    return (row.tenant_id, row.project_id, row.end_user_id, row.agent_id)


def _equal(col, value):
    return col.is_(None) if value is None else col == value


def _of(key: MemoryKey):
    t, p, u, a = key
    L = AgentObjectLink
    return and_(L.tenant_id == t, _equal(L.project_id, p), _equal(L.end_user_id, u), _equal(L.agent_id, a))


# How many memories one statement reads the links of.
_MEMORIES = 200


def _load(db: Session, keys: Iterable[MemoryKey], known_as_of: Optional[datetime]) -> Dict[MemoryKey, list]:
    """The links of each memory of `keys`, one statement per `_MEMORIES`."""
    keys = list(dict.fromkeys(keys))
    out: Dict[MemoryKey, list] = {k: [] for k in keys}
    L = AgentObjectLink
    for i in range(0, len(keys), _MEMORIES):
        q = db.query(L).filter(or_(*[_of(k) for k in keys[i:i + _MEMORIES]]))
        if known_as_of is not None:
            q = q.filter(L.created_at <= known_as_of)
        for r in q.all():
            out.setdefault(memory_key(r), []).append(r)
    return out


def _by_family(rows: Iterable[Any]) -> Dict[str, Tuple[Dict[str, str], Dict[str, Any]]]:
    """{family: (its link map, its rows by name)}: a link is read in its own
    family only."""
    grouped: Dict[str, list] = {}
    for r in rows:
        grouped.setdefault(_objects.family_of(r.family), []).append(r)
    return {fam: (_objects.link_map(rs), {_objects.key_of(r.name_label): r for r in rs})
            for fam, rs in grouped.items()}


def family_of_the_fact(fact: Any) -> str:
    """The family a read looks a fact's links up in: the family stored on
    the fact, else the static map's (`families.family_of_the_fact`, the rule
    of `store.family_filter`, which chose the facts the link was decided on)."""
    from ..memory_core.families import family_of_the_fact as _family

    return _objects.family_of(_family(fact.predicate_family, fact.predicate_canonical))


def prefetch(db: Session, facts: Iterable[Any], known_as_of: Optional[datetime] = None) -> None:
    """Read the links of the memories of `facts` once, for the reads that
    follow in this session (`resolve`): a page of facts costs one statement,
    not one per fact."""
    facts = list(facts)
    if not facts:
        return
    cache = db.info.setdefault(_CACHE, {})
    keys = [k for k in {memory_key(f) for f in facts} if (k, known_as_of) not in cache]
    for k, rows in _load(db, keys, known_as_of).items():
        cache[(k, known_as_of)] = _by_family(rows)


def forget_prefetched(db: Session) -> None:
    """After a link is written or removed in this session."""
    db.info.pop(_CACHE, None)


def _maps(db: Optional[Session], fact: Any, known_as_of: Optional[datetime]):
    """The links of the fact's memory in the fact's family: ({}, {}) for a
    family never asked, which no link may change."""
    family = family_of_the_fact(fact)
    if family in _objects.NEVER_ASKED or db is None:
        return {}, {}
    key = memory_key(fact)
    cache = db.info.get(_CACHE, {})
    got = cache.get((key, known_as_of))
    if got is None:
        got = _by_family(_load(db, [key], known_as_of).get(key, []))
    return got.get(family) or ({}, {})


def resolve(db: Optional[Session], fact: Any, known_as_of: Optional[datetime] = None) -> dict:
    """The names a read shows for `fact`: {"subject_canonical",
    "object_canonical", "subject_link_id", "object_link_id"}; the stored
    canonicals and no link ids when no link of its memory names them."""
    links, rows = _maps(db, fact, known_as_of)
    out = {}
    for side in ("subject", "object"):
        stored = getattr(fact, f"{side}_canonical") or getattr(fact, f"{side}_label")
        root = _objects.root_of(stored, links) if links else stored
        row = rows.get(_objects.key_of(stored)) if links else None
        out[f"{side}_canonical"] = root
        out[f"{side}_link_id"] = str(row.id) if row is not None and _objects.key_of(root) != _objects.key_of(stored) else None
    return out


def view(db: Optional[Session], fact: Any, known_as_of: Optional[datetime] = None) -> dict:
    """A fact as `presentation.context_line` reads it, the names resolved."""
    names = resolve(db, fact, known_as_of)
    return {"subject_label": fact.subject_label, "object_label": fact.object_label,
            "predicate_raw": fact.predicate_raw, "predicate_canonical": fact.predicate_canonical,
            "predicate_family": fact.predicate_family,
            "valid_from": fact.valid_from, "tense": fact.tense, "invalid_at": fact.invalid_at, **names}


def one_line_each(db: Session, facts: List[Any], known_as_of: Optional[datetime] = None) -> List[Tuple[dict, list]]:
    """The facts of a context block, each with its view, two facts that read
    the same triple through a link given once (the first, by rank), with the
    ids of both: [(view, [fact ids])]. Without links each fact is its own."""
    prefetch(db, facts, known_as_of)
    out: List[Tuple[dict, list]] = []
    at: Dict[tuple, Tuple[int, bool]] = {}
    for f in facts:
        v = view(db, f, known_as_of)
        linked = bool(v["subject_link_id"] or v["object_link_id"])
        k = (memory_key(f), _objects.key_of(v["subject_canonical"]), f.predicate_canonical,
             _objects.key_of(v["object_canonical"]), f.tense or "current", f.invalid_at is None)
        # Only two lines a link made the same are folded: two facts the store
        # keeps apart without one stay two lines, as before 6.1.0.
        if k in at and (linked or at[k][1]):
            out[at[k][0]][1].append(f.id)
            continue
        at.setdefault(k, (len(out), linked))
        out.append((v, [f.id]))
    return out


def names_condition(db: Session, tenant_id: UUID, name: str, column, *, project_id: Any = EVERY_PROJECT,
                    end_user_id: Any = None, all_end_users: bool = False, agent_id: Optional[str] = None,
                    known_as_of: Optional[datetime] = None):
    """SQL on AgentFact: `column` (a lowercased name of the fact) is a name
    linked to `name` in the fact's own memory and in the fact's own family,
    as `entities.name_condition` reads an alias. The links that name it, in
    the read's scope (the account, the project when the caller is confined
    to one, the end user unless `all_end_users`, the agent namespace when
    given), as the store knew them at `known_as_of`, the moment the same
    response's names are read at (`serialize_fact`); then each of their
    memories' links of that family, for the whole group of names read as
    one. The names meet lowercased by the database on both sides."""
    L = AgentObjectLink
    key = func.lower(func.trim(literal(name or "")))
    scope = [L.tenant_id == tenant_id,
             or_(func.lower(L.name_label) == key, func.lower(L.canonical_label) == key)]
    if project_id is not EVERY_PROJECT:
        scope.append(_equal(L.project_id, project_id))
    if not all_end_users:
        scope.append(_equal(L.end_user_id, end_user_id))
    if agent_id is not None:
        scope.append(L.agent_id == agent_id)
    if known_as_of is not None:
        scope.append(L.created_at <= known_as_of)
    keys = [memory_key(r) for r in db.query(L.tenant_id, L.project_id, L.end_user_id, L.agent_id)
            .filter(*scope).distinct()]
    if not keys:
        return false()
    from ..memory_core.store import family_filter

    F = AgentFact
    each = []
    for k, rows in _load(db, keys, known_as_of).items():
        for family, (links, _rows) in _by_family(rows).items():
            if family in _objects.NEVER_ASKED:
                continue
            root = _objects.key_of(_objects.root_of(name, links))
            labels = {n for r in _rows.values() for n in (r.name_label, r.canonical_label)
                      if _objects.key_of(_objects.root_of(n, links)) == root}
            if not labels:
                continue
            labels.add(name.strip())
            _t, p, u, a = k
            each.append(and_(_equal(F.project_id, p), _equal(F.end_user_id, u), _equal(F.agent_id, a),
                             family_filter(F, family),
                             column.in_([func.lower(literal(n)) for n in sorted(labels)])))
    return or_(*each) if each else false()
