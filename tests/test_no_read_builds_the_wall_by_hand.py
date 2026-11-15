"""No read builds the end-user wall by hand (T2, piece P0, the shared
level's specification, 4.1).

The filter a read puts on an end-user column is written once,
services/user_scope.wall (and within_end_users, which applies it), as the
project's is in services/sentinels.within_project. Until 6 Nov 2026 it was
written out in seven places that did not agree on what None meant; a shared
level changes exactly this rule, and seven copies are seven chances to leave
one open.

The guard watches the SQL every door of the leak matrix sends
(tests/wall_world.py: every /v1 door, the MCP tools, the operator's
listings, the reads with no door), as the database gets it. It sees the
common shapes of a filter on an end-user column, and the ones the control
below tries: a comparison (`=`, `IS`, `IN`, `LIKE`, `IS NOT DISTINCT FROM`,
an operator of one's own with `.op()`), on the column, on a `coalesce` or a
tuple of it, on a `literal_column` that names it, from `filter_by`, a column
kept in a variable or reached by `getattr` or `__table__.c`; a function of
the column used as a condition; a `text()` anywhere in a WHERE, HAVING or
ON, and a statement of text that names the column. Where each comparison is
built is recorded when it is built, so a function of another module named
`wall` is not the wall. It does not see every shape SQL allows: an
identifier spelled with escapes inside a whole statement of text, for one,
is not read. What holds the wall is the matrix, which reads the data every
door gives back, with this guard beside it.

A condition not built by services/user_scope.py fails unless it is
declared below, by its exact SQL, the three functions that build it and ask
it, and how many times one statement carries it, with the reason. The same
condition built again elsewhere, or once more in a statement that carried
it once, fails; the same condition built on another line of the same
functions, in a statement of its own, is not told apart.

Its reach is the matrix's: a read no door of the matrix reaches is not
watched (the matrix lists its doors).
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.sql import elements, operators, visitors
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.selectable import Join, ScalarSelect, SelectBase, Subquery

from tests import cloud
from tests import wall_world as ww

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = str(ROOT / "korely_agent") + "/"
THE_WALL = "korely_agent/services/user_scope.py"
COLUMN = "end_user_id"

ERASURE = ("an erasure, or the end of an agent's namespace: it works on the end users it "
           "erases, by definition (services/end_user_places.py lists every place it reaches)")
PSEUDONYM = ("the trail, writing a row about an end user, looks whether that person was "
             "erased, to write the pseudonym instead (services/pseudonyms.as_logged)")
PERIMETER = ("the write's perimeter: the facts, names and vocabulary of the memory in hand, "
             "the end user of the row being written, judged or forgotten (a forgotten "
             "memory gives back what it closed: facts/erasure._give_back), never what a "
             "caller may read")

# The reads that filter on an end user without the wall, each for a reason
# of its own. Not leaks (the matrix holds every answer they give), but the
# places a shared level (T2) has to change with the wall, since each
# decides what a reader sees of an end user:
T2_MUST_TOUCH = {
    "trail": ("GET /v1/audit and the operator's trail, by the person asked: the id and the "
              "pseudonyms an erasure left of it (services/pseudonyms.as_asked), after "
              "`reading` put up the key's wall"),
    "names": ("a fact's names read in the fact's own memory (facts/entities.name_condition, "
              "facts/links.names_condition, the `entity` and `subject` filters of GET "
              "/v1/facts, and the links' prefetch for the names a fact shows): the alias or "
              "link of the same end user as the fact"),
    "declared": ("POST /v1/users/{end_user}/names reads the names already declared in that "
                 "end user's memory (entities.memory_of) before declaring one"),
    "users": ("GET /v1/users lists the end users that hold rows: the rows of no end user "
              "are nobody's (api/memories._activity); a shared place would be listed there"),
    "users_not_erased": ("GET /v1/users lists the end users the holding places name "
                         "(end_user_places.held_selects), never the pseudonym an erasure "
                         "left, which is nobody (ids.pseudonym_condition)"),
    "export": ("the Markdown export lists who has facts, to write each end user's file; no "
               "door, the operator's FACTS_EXPORT_DIR (worker.py)"),
}
# And one meaning of the wall no door reaches yet: None, the memories of no
# end user and nothing else (memory_engine._apply_end_user). Every door that
# names no user_id reads every end user (EVERY_END_USER) or a confined
# key's list, so a wall that read None as every end user passes the matrix.
# The first T2 door that reads the shared memory as a place of its own goes
# into the matrix with it.


def _at(*where):
    return tuple(f"korely_agent/{w}" for w in where)


# (the condition as the database gets it, where it is built: the function
# that builds it, what asks it and what asks that, how many times one
# statement carries it) -> why. Generated from what the doors send, then
# each reason read and written by hand.
DECLARED = {
    ('audit_events.end_user_id IN (?)',
     _at('api/admin.py:list_audit'), 1): T2_MUST_TOUCH["trail"],
    ('audit_events.end_user_id IN (?)',
     _at('api/audit.py:list_audit'), 1): T2_MUST_TOUCH["trail"],
    # One scan of each since 1.0.23: what is live counted with FILTER, every
    # end user the rows name listed (list_users, seeded).
    ('agent_facts.end_user_id IS NOT NULL',
     _at('api/memories.py:_activity', 'api/memories.py:list_users'), 1): T2_MUST_TOUCH["users"],
    ('agent_memories.end_user_id IS NOT NULL',
     _at('api/memories.py:_activity', 'api/memories.py:list_users'), 1): T2_MUST_TOUCH["users"],
    # And the end users every other holding place names (1.0.23), behind the
    # key's wall (user_scope), never a pseudonym.
    **{(f'{table}.end_user_id {cond}',
        _at('services/end_user_places.py:select', 'services/end_user_places.py:held_selects',
            'api/memories.py:list_users'), 1): T2_MUST_TOUCH[why]
       for table in ("agent_read_marks", "agent_entity_aliases", "agent_object_links",
                     "predicate_clusters", "predicate_aliases", "agent_forget_audit")
       for cond, why in (("IS NOT NULL", "users"), ("!~ ?", "users_not_erased"))},
    ('agent_facts.end_user_id IS NOT NULL',
     _at('services/export_md.py:_end_users'), 1): T2_MUST_TOUCH["export"],
    ('agent_entity_aliases.end_user_id = ?',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:memory_of', 'api/facts.py:declare_name'), 1): T2_MUST_TOUCH["declared"],
    ('agent_entity_aliases.end_user_id = ?',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts'), 2): T2_MUST_TOUCH["names"],
    ('agent_entity_aliases.end_user_id = ?',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts'), 4): T2_MUST_TOUCH["names"],
    (('((coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?)) NOT IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND agent_entity_aliases.end_user_id = ? AND '
     'lower(trim(agent_entity_aliases.canonical_label)) != ?))'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'api/facts.py:list_facts'), 1): T2_MUST_TOUCH["names"],
    (('((coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?)) NOT IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND agent_entity_aliases.end_user_id = ? AND '
     'lower(trim(agent_entity_aliases.canonical_label)) != ?))'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'api/facts.py:list_facts'), 2): T2_MUST_TOUCH["names"],
    (('((coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?)) NOT IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND lower(trim(agent_entity_aliases.canonical_label)) != ?))'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'api/facts.py:list_facts'), 1): T2_MUST_TOUCH["names"],
    (('((coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?)) NOT IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND lower(trim(agent_entity_aliases.canonical_label)) != ?))'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'api/facts.py:list_facts'), 2): T2_MUST_TOUCH["names"],
    (('(coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?), '
     'lower(coalesce(agent_facts.object_canonical, agent_facts.object_label))) IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6, '
     'lower(trim(agent_entity_aliases.canonical_label)) AS lower_1 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND agent_entity_aliases.end_user_id = ? AND '
     'lower(trim(agent_entity_aliases.canonical_label)) != ?)'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'api/facts.py:list_facts'), 1): T2_MUST_TOUCH["names"],
    (('(coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?), '
     'lower(coalesce(agent_facts.object_canonical, agent_facts.object_label))) IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6, '
     'lower(trim(agent_entity_aliases.canonical_label)) AS lower_1 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND lower(trim(agent_entity_aliases.canonical_label)) != ?)'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'api/facts.py:list_facts'), 1): T2_MUST_TOUCH["names"],
    (('(coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?), '
     'lower(coalesce(agent_facts.subject_canonical, agent_facts.subject_label))) IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6, '
     'lower(trim(agent_entity_aliases.canonical_label)) AS lower_1 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND agent_entity_aliases.end_user_id = ? AND '
     'lower(trim(agent_entity_aliases.canonical_label)) != ?)'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'api/facts.py:list_facts'), 1): T2_MUST_TOUCH["names"],
    (('(coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?), '
     'lower(coalesce(agent_facts.subject_canonical, agent_facts.subject_label))) IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6, '
     'lower(trim(agent_entity_aliases.canonical_label)) AS lower_1 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND lower(trim(agent_entity_aliases.canonical_label)) != ?)'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'api/facts.py:list_facts'), 1): T2_MUST_TOUCH["names"],
    (('((coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?)) NOT IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND agent_entity_aliases.end_user_id = ? AND '
     'lower(trim(agent_entity_aliases.canonical_label)) != ?))'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'mcp/server.py:run'), 2): T2_MUST_TOUCH["names"],
    (('((coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?)) NOT IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND lower(trim(agent_entity_aliases.canonical_label)) != ?))'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'mcp/server.py:run'), 2): T2_MUST_TOUCH["names"],
    (('(coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?), '
     'lower(coalesce(agent_facts.object_canonical, agent_facts.object_label))) IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6, '
     'lower(trim(agent_entity_aliases.canonical_label)) AS lower_1 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND agent_entity_aliases.end_user_id = ? AND '
     'lower(trim(agent_entity_aliases.canonical_label)) != ?)'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'mcp/server.py:run'), 1): T2_MUST_TOUCH["names"],
    (('(coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?), '
     'lower(coalesce(agent_facts.object_canonical, agent_facts.object_label))) IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6, '
     'lower(trim(agent_entity_aliases.canonical_label)) AS lower_1 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND lower(trim(agent_entity_aliases.canonical_label)) != ?)'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'mcp/server.py:run'), 1): T2_MUST_TOUCH["names"],
    (('(coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?), '
     'lower(coalesce(agent_facts.subject_canonical, agent_facts.subject_label))) IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6, '
     'lower(trim(agent_entity_aliases.canonical_label)) AS lower_1 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND agent_entity_aliases.end_user_id = ? AND '
     'lower(trim(agent_entity_aliases.canonical_label)) != ?)'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'mcp/server.py:run'), 1): T2_MUST_TOUCH["names"],
    (('(coalesce(agent_facts.project_id, CAST(? AS UUID)), '
     'coalesce(agent_facts.end_user_id, ?), coalesce(agent_facts.agent_id, ?), '
     'lower(coalesce(agent_facts.subject_canonical, agent_facts.subject_label))) IN '
     '(SELECT coalesce(agent_entity_aliases.project_id, CAST(? AS UUID)) AS '
     'coalesce_3, coalesce(agent_entity_aliases.end_user_id, ?) AS coalesce_4, '
     'coalesce(agent_entity_aliases.agent_id, ?) AS coalesce_6, '
     'lower(trim(agent_entity_aliases.canonical_label)) AS lower_1 FROM '
     'agent_entity_aliases WHERE agent_entity_aliases.tenant_id = ?::UUID AND '
     'agent_entity_aliases.alias_lower = ? AND agent_entity_aliases.project_id = '
     '?::UUID AND lower(trim(agent_entity_aliases.canonical_label)) != ?)'),
     _at('services/facts/entities.py:name_condition', 'services/facts/reads.py:retrieve_agent_facts', 'mcp/server.py:run'), 1): T2_MUST_TOUCH["names"],
    ('agent_object_links.end_user_id = ?',
     _at('services/facts/links.py:_equal', 'services/facts/links.py:_of', 'services/facts/links.py:_load'), 1): T2_MUST_TOUCH["names"],
    ('agent_object_links.end_user_id = ?',
     _at('services/facts/links.py:_equal', 'services/facts/links.py:_of', 'services/facts/links.py:_load'), 2): T2_MUST_TOUCH["names"],
    ('agent_object_links.end_user_id = ?',
     _at('services/facts/links.py:_equal', 'services/facts/links.py:_of', 'services/facts/links.py:_load'), 5): T2_MUST_TOUCH["names"],
    ('agent_object_links.end_user_id = ?',
     _at('services/facts/links.py:_equal', 'services/facts/links.py:_of', 'services/facts/links.py:_load'), 6): T2_MUST_TOUCH["names"],
    ('agent_object_links.end_user_id IS NULL',
     _at('services/facts/links.py:_equal', 'services/facts/links.py:_of', 'services/facts/links.py:_load'), 1): T2_MUST_TOUCH["names"],
    ('agent_object_links.end_user_id IS NULL',
     _at('services/facts/links.py:_equal', 'services/facts/links.py:_of', 'services/facts/links.py:_load'), 2): T2_MUST_TOUCH["names"],
    ('agent_object_links.end_user_id = ?',
     _at('services/facts/links.py:_equal', 'services/facts/links.py:names_condition', 'services/facts/reads.py:retrieve_agent_facts'), 1): T2_MUST_TOUCH["names"],
    ('agent_facts.end_user_id = ?',
     _at('services/end_user_places.py:lives_elsewhere', 'services/pseudonyms.py:_counts_here', 'services/pseudonyms.py:_record_for'), 1): PSEUDONYM,
    ('agent_memories.end_user_id = ?',
     _at('services/end_user_places.py:lives_elsewhere', 'services/pseudonyms.py:_counts_here', 'services/pseudonyms.py:_record_for'), 1): PSEUDONYM,
    ('agent_facts.end_user_id = ?',
     _at('services/end_user_places.py:lives_in', 'services/pseudonyms.py:_counts_here', 'services/pseudonyms.py:_record_for'), 1): PSEUDONYM,
    ('agent_memories.end_user_id = ?',
     _at('services/end_user_places.py:lives_in', 'services/pseudonyms.py:_counts_here', 'services/pseudonyms.py:_record_for'), 1): PSEUDONYM,
    ('agent_forget_audit.end_user_id IN (?)',
     _at('services/pseudonyms.py:_record_for', 'services/pseudonyms.py:as_logged', 'services/audit.py:_settle_all'), 1): PSEUDONYM,
    ('agent_forget_audit.end_user_id IN (?)',
     _at('services/pseudonyms.py:_record_for', 'services/pseudonyms.py:as_logged', 'services/audit.py:_row'), 1): PSEUDONYM,
    ('agent_facts.end_user_id IN (?)',
     _at('services/end_user_places.py:_living_elsewhere', 'services/end_user_places.py:_pseudonymise_trail', 'services/pseudonyms.py:rename'), 1): ERASURE,
    ('agent_memories.end_user_id IN (?)',
     _at('services/end_user_places.py:_living_elsewhere', 'services/end_user_places.py:_pseudonymise_trail', 'services/pseudonyms.py:rename'), 1): ERASURE,
    ('agent_forget_audit.end_user_id IN (?)',
     _at('services/end_user_places.py:_pseudonymise_records', 'services/pseudonyms.py:rename', 'services/pseudonyms.py:after_an_erasure'), 1): ERASURE,
    ('audit_events.end_user_id IN (?)',
     _at('services/end_user_places.py:_pseudonymise_trail', 'services/pseudonyms.py:rename', 'services/pseudonyms.py:after_an_erasure'), 1): ERASURE,
    ('agent_memories.end_user_id = ?',
     _at('services/end_user_places.py:_rows', 'services/end_user_places.py:_lock_memories', 'services/end_user_places.py:erase_person'), 1): ERASURE,
    ('agent_facts.end_user_id = ?',
     _at('services/end_user_places.py:_rows', 'services/end_user_places.py:erase', 'services/end_user_places.py:erase_person'), 1): ERASURE,
    ('agent_memories.end_user_id = ?',
     _at('services/end_user_places.py:_rows', 'services/end_user_places.py:erase', 'services/end_user_places.py:erase_person'), 1): ERASURE,
    ('agent_object_links.end_user_id = ?',
     _at('services/end_user_places.py:_rows', 'services/end_user_places.py:erase', 'services/end_user_places.py:erase_person'), 1): ERASURE,
    # The read marks of the agent notices (1.0.15): the erased person's go,
    # and those read under their id.
    ('agent_read_marks.end_user_id = ?',
     _at('services/end_user_places.py:_erase_marks', 'services/end_user_places.py:erase_person', 'services/facts/erasure.py:forget_end_user'), 1): ERASURE,
    ('agent_entity_aliases.end_user_id = ?',
     _at('services/end_user_places.py:aliases_behind', 'services/end_user_places.py:_erase_aliases', 'services/end_user_places.py:erase_person'), 1): ERASURE,
    ('agent_entity_aliases.end_user_id IN (?)',
     _at('services/end_user_places.py:unplaced_names', 'services/end_user_places.py:erase_person', 'services/facts/erasure.py:forget_end_user'), 1): ERASURE,
    ('agent_facts.end_user_id IN (?)',
     _at('services/end_user_places.py:unplaced_names', 'services/end_user_places.py:erase_person', 'services/facts/erasure.py:forget_end_user'), 1): ERASURE,
    ('agent_memories.end_user_id IN (?)',
     _at('services/end_user_places.py:unplaced_names', 'services/end_user_places.py:erase_person', 'services/facts/erasure.py:forget_end_user'), 1): ERASURE,
    ('predicate_clusters.end_user_id = ?',
     _at('services/end_user_places.py:vocabulary_behind', 'services/end_user_places.py:_erase_vocabulary', 'services/end_user_places.py:erase_person'), 1): ERASURE,
    ('predicate_clusters.end_user_id IS NOT DISTINCT FROM ?',
     _at('services/facts/erasure.py:_erase_examples', 'services/facts/erasure.py:delete_agent', 'api/memories.py:delete_agent_namespace'), 1): ERASURE,
    ('predicate_clusters.end_user_id IS NOT DISTINCT FROM NULL',
     _at('services/facts/erasure.py:_erase_examples', 'services/facts/erasure.py:delete_agent', 'api/memories.py:delete_agent_namespace'), 1): ERASURE,
    ('predicate_clusters.end_user_id = ?',
     _at('services/facts/erasure.py:forget_end_user', 'api/memories.py:delete_user_memories'), 2): ERASURE,
    ('agent_entity_aliases.end_user_id = ?',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:memory_of', 'services/facts/entities.py:declare_alias'), 1): PERIMETER,
    ('agent_facts.end_user_id = ?',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:memory_of', 'services/facts/entities.py:declare_alias'), 1): PERIMETER,
    ('agent_entity_aliases.end_user_id = ?',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:memory_of', 'services/facts/service.py:_persist_here'), 1): PERIMETER,
    ('agent_entity_aliases.end_user_id IS NULL',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:memory_of', 'services/facts/service.py:_persist_here'), 1): PERIMETER,
    ('agent_facts.end_user_id = ?',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:memory_of', 'services/facts/service.py:_persist_here'), 1): PERIMETER,
    ('agent_facts.end_user_id IS NULL',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:memory_of', 'services/facts/service.py:_persist_here'), 1): PERIMETER,
    ('agent_facts.end_user_id = ?',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:memory_scope', 'services/facts/contradiction.py:_stage1_candidates'), 1): PERIMETER,
    ('agent_facts.end_user_id IS NULL',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:memory_scope', 'services/facts/contradiction.py:_stage1_candidates'), 1): PERIMETER,
    ('agent_facts.end_user_id = ?',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:memory_scope', 'services/facts/erasure.py:_give_back'), 1): PERIMETER,
    ('agent_facts.end_user_id IS NULL',
     _at('services/facts/entities.py:_equal', 'services/facts/entities.py:memory_scope', 'services/facts/erasure.py:_give_back'), 1): PERIMETER,
    ('predicate_aliases.end_user_id IS NOT DISTINCT FROM ?',
     _at('services/predicates.py:end_user_vocabulary', 'services/predicates.py:resolve_relation', 'services/facts/service.py:_persist_here'), 1): PERIMETER,
    ('predicate_aliases.end_user_id IS NOT DISTINCT FROM NULL',
     _at('services/predicates.py:end_user_vocabulary', 'services/predicates.py:resolve_relation', 'services/facts/service.py:_persist_here'), 1): PERIMETER,
    ('predicate_clusters.end_user_id IS NOT DISTINCT FROM ?',
     _at('services/predicates.py:end_user_vocabulary', 'services/predicates.py:resolve_relation', 'services/facts/service.py:_persist_here'), 1): PERIMETER,
    ('predicate_clusters.end_user_id IS NOT DISTINCT FROM NULL',
     _at('services/predicates.py:end_user_vocabulary', 'services/predicates.py:resolve_relation', 'services/facts/service.py:_persist_here'), 1): PERIMETER,
}
# A part of the product's (tests/hosted_parts; none in the public tree):
# its places a shared level changes with the wall, and its declarations,
# written with the reasons above and given by where they are sent. Those
# the core's doors send to its rows wherever the package carries it (an
# erasure reaches them in either mode) are declared here; those its own
# doors send, where its mode serves them (tests/wall_world.PART_DOORS),
# and those its own tests watch, its tests declare (PART_DECLARED).
T2_MUST_TOUCH.update(cloud.of_the_parts("WALL_T2_MUST_TOUCH"))
PART_DECLARED = cloud.of_the_parts("WALL_DECLARED", lambda: (lambda **reasons: {}))(
    ERASURE=ERASURE, PSEUDONYM=PSEUDONYM, PERIMETER=PERIMETER, T2_MUST_TOUCH=T2_MUST_TOUCH,
    at=_at)
DECLARED.update(PART_DECLARED.get("everywhere", {}))


# How many functions of the package, from the one that builds a condition
# outwards, name where it is built: the builder, what asks it, and what asks
# that (`entities._equal`, for `memory_of`, for the names' door).
DEPTH = 3


def _site():
    """Where the code that asked is: the innermost functions of this
    package on the stack, a comprehension read as the function it is in."""
    out = []
    f = sys._getframe(2)
    while f is not None and len(out) < DEPTH:
        name = f.f_code.co_filename
        if name.startswith(PACKAGE) and not f.f_code.co_name.startswith("<"):
            rel = Path(name).resolve().relative_to(ROOT).as_posix()
            out.append(f"{rel}:{f.f_code.co_name}")
        f = f.f_back
    return tuple(out)


def _built_by_the_wall(at) -> bool:
    return bool(at) and at[0].startswith(THE_WALL + ":")


def _names_the_column(n) -> bool:
    """A column `end_user_id`, or a `literal_column` whose last part,
    unquoted, is that name (`literal_column("agent_memories.end_user_id")`)."""
    if not isinstance(n, elements.ColumnClause):
        return False
    if n.name == COLUMN:
        return True
    return bool(n.is_literal) and n.name.rsplit(".", 1)[-1].strip().strip('"') == COLUMN


def _below(e):
    """`e` and what is in it, a subquery left out: a statement of its own,
    read on its own."""
    todo = [e]
    while todo:
        n = todo.pop()
        yield n
        if isinstance(n, (SelectBase, ScalarSelect, Subquery)) and n is not e:
            continue
        todo.extend(n.get_children())


def _touches(e) -> bool:
    return any(_names_the_column(n) for n in _below(e))


def _a_condition(op) -> bool:
    """A comparison, or an operator of one's own (`.op("=")`), which says
    nothing of what it compares."""
    return operators.is_comparison(op) or isinstance(op, operators.custom_op)


def _criteria(e) -> list:
    """What filters rows in `e`: a WHERE, a HAVING, a join's ON."""
    out = []
    if isinstance(e, (SelectBase, UpdateBase)) and getattr(e, "whereclause", None) is not None:
        out.append(e.whereclause)
    out += list(getattr(e, "_having_criteria", ()) or ())
    if isinstance(e, Join) and e.onclause is not None:
        out.append(e.onclause)
    return out


def _sql(e) -> str:
    text = str(e.compile(dialect=postgresql.dialect()))
    text = re.sub(r"%\(\w+\)s", "?", text)
    return " ".join(re.sub(r"__\[POSTCOMPILE_\w+\]", "?", text).split())


class Watched(set):
    """The conditions not built by the wall, each (its SQL, where it was
    built, how many times one statement carried it), and how many were
    the wall's."""
    walled = 0


@pytest.fixture
def watched(monkeypatch):
    """Every condition on the column, with where it was built, in every
    statement sent."""
    made = elements.BinaryExpression.__init__

    def init(self, left, right, operator, *a, **k):
        made(self, left, right, operator, *a, **k)
        if _a_condition(operator) and (_touches(left) or _touches(right)):
            self._built_at = _site()

    monkeypatch.setattr(elements.BinaryExpression, "__init__", init)
    found = Watched()

    def before_execute(conn, clause, multiparams, params, execution_options):
        here = Counter()
        if isinstance(clause, elements.TextClause) and COLUMN in clause.text:
            here[(" ".join(clause.text.split()), _site())] += 1
        for e in visitors.iterate(clause):
            if isinstance(e, elements.BinaryExpression) and _a_condition(e.operator) \
                    and _touches(e):
                at = getattr(e, "_built_at", ("built before the watch",))
                if _built_by_the_wall(at):
                    found.walled += 1
                else:
                    here[(_sql(e), at)] += 1
            for root in _criteria(e):
                for n in _beside_the_conditions(root):
                    here[(_sql(n), ("a function or a text in a condition", *_site()))] += 1
        found.update(k + (n,) for k, n in here.items())

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if getattr(context, "compiled", None) is None and COLUMN in statement:
            found.add((" ".join(statement.split()), _site(), 1))

    event.listen(Engine, "before_execute", before_execute)
    event.listen(Engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield found
    finally:
        event.remove(Engine, "before_execute", before_execute)
        event.remove(Engine, "before_cursor_execute", before_cursor_execute)


def _beside_the_conditions(root):
    """In a WHERE, HAVING or ON: a `text()` (whatever it names), and a
    function of the column that is not inside a comparison already read."""
    todo = [root]
    while todo:
        n = todo.pop()
        if isinstance(n, (SelectBase, ScalarSelect, Subquery)):
            continue
        if isinstance(n, elements.BinaryExpression) and _a_condition(n.operator) and _touches(n):
            continue
        if isinstance(n, elements.TextClause):
            yield n
            continue
        if isinstance(n, FunctionElement) and _touches(n):
            yield n
            continue
        todo.extend(n.get_children())


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    ww.no_model(monkeypatch)


@pytest.fixture
def client(db, monkeypatch):
    with ww.a_client(db, monkeypatch) as c:
        yield c


def test_the_guard_sees_every_shape_of_a_hand_built_wall(db, tenant, watched):
    """The control: each shape the guard is said to see is seen, a filter
    once more in a statement is seen, and the wall's is not reported."""
    from sqlalchemy import func, literal_column, text, tuple_
    from sqlalchemy.sql import operators as ops

    from korely_agent.db.models import AgentMemory
    from korely_agent.services import user_scope

    def wall(q, u):
        # A function of another module named like the wall is not the wall.
        return q.filter(AgentMemory.end_user_id == u)

    col = AgentMemory.end_user_id
    q = db.query(AgentMemory.id)
    shapes = {
        "a column in a variable": lambda: q.filter(col == "anna"),
        "getattr": lambda: q.filter(getattr(AgentMemory, "end_user_id").is_(None)),
        "filter_by": lambda: q.filter_by(end_user_id="anna"),
        "the table's column": lambda: q.filter(AgentMemory.__table__.c["end_user_id"] != "x"),
        "a tuple": lambda: q.filter(tuple_(AgentMemory.end_user_id, AgentMemory.agent_id)
                                    .in_([("anna", "a")])),
        "coalesce": lambda: q.filter(func.coalesce(AgentMemory.end_user_id, "") == "anna"),
        "an operator called": lambda: q.filter(ops.eq(AgentMemory.end_user_id, "anna")),
        "a local wall": lambda: wall(q, "anna"),
        "text": lambda: q.filter(text("agent_memories.end_user_id = :u")).params(u="anna"),
        "an operator of one's own": lambda: q.filter(AgentMemory.end_user_id.op("=")("anna")),
        "a literal column": lambda: q.filter(literal_column("agent_memories.end_user_id")
                                             == "anna"),
        "a function as the condition": lambda: q.filter(func.texteq(AgentMemory.end_user_id,
                                                                    "anna")),
        "an escaped name in a text": lambda: q.filter(
            text('agent_memories.U&"end\\005fuser_id" = :x')).params(x="anna"),
    }
    for name, build in shapes.items():
        watched.clear()
        build().all()
        assert len(watched) == 1, (name, watched)
    # Once more in the same statement is another count, so another entry.
    watched.clear()
    q.filter(col == "anna").all()
    once = set(watched)
    watched.clear()
    q.filter(col == "anna", col == "bruno").all()
    assert {n for *_k, n in watched} == {2} and {n for *_k, n in once} == {1}
    watched.clear()
    walled = watched.walled
    user_scope.within_end_users(q, AgentMemory.end_user_id, "anna").all()
    assert watched.walled > walled and not watched


def test_every_end_user_filter_the_doors_send_is_the_walls(client, db, watched):
    the_doors_send_only_the_walls(client, db, watched, ww.DOORS, DECLARED)


def the_doors_send_only_the_walls(client, db, watched, doors, declared):
    from tests.test_the_end_user_wall_leaks_nothing import services

    for door in doors:
        ww.ask(db, client, door)
    services(ww.World(db, client), db)
    import os
    if os.environ.get("KORELY_PRINT_WATCHED"):
        print("\n".join(map(repr, sorted(watched))))
    assert watched.walled > 100, "the control: the wall's own conditions are seen"
    undeclared = sorted(f for f in watched if f not in declared)
    assert undeclared == [], ("an end-user filter built outside services/user_scope.py: use "
                              "user_scope.within_end_users, or declare why it is not a "
                              "reader's wall", undeclared)
    assert sorted(set(declared) - watched) == [], "a declaration that nothing sends any more"
