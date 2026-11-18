"""the family each fact was shown under, saved once as data

Revision ID: 8d4f1a2c6e35
Revises: 7b3e1d9a4c26
Create Date: 2026-10-08 13:30:00

memory_core 3.3.0, the rule of 8 Oct 2026: "there must be no list of words
in the code". A fact's family is decided by the learned
vocabulary when its relation is born, and stored on the relation and on the
fact (`predicate_family`). Facts written before that column was filled had
none, and until 3.3.0 they were shown under the family of a static map of 49
English predicates (`families.PREDICATE_TO_FAMILY`), read as a fallback by
`family_of_the_fact` and `family_filter`. The map leaves the code; this
migration saves those families once, so no row changes the family it is
shown under:

  1. from the tenant's learned relation of the same canonical name, when it
     has a family;
  2. else the family the row was shown under until today: the old map, kept
     here as historical data and never read by the running code;
  3. the rest stay NULL and read "other", as today.

On the LoCoMo bench stores of 7-8 Oct every fact already had its family (0
NULL of 5,935): this is for installs that wrote facts before the column was
filled. The downgrade removes nothing: which rows were empty is no longer
known, and a family saved equal to the one shown changes no reading.
"""
import sqlalchemy as sa  # noqa: F401
from alembic import op

revision = "8d4f1a2c6e35"
down_revision = "7b3e1d9a4c26"
branch_labels = None
depends_on = None

# The map of memory_core <= 3.2.0, as historical data (predicate, family).
_HISTORICAL_MAP = (
    ("likes", "preferences"),
    ("dislikes", "preferences"),
    ("prefers", "preferences"),
    ("favorite", "preferences"),
    ("enjoys", "preferences"),
    ("hates", "preferences"),
    ("loves", "preferences"),
    ("interested_in", "preferences"),
    ("knows", "people"),
    ("friend_of", "people"),
    ("colleague_of", "people"),
    ("married_to", "people"),
    ("divorced_from", "people"),
    ("parent_of", "people"),
    ("child_of", "people"),
    ("sibling_of", "people"),
    ("mentor_of", "people"),
    ("lives_in", "places"),
    ("moved_from", "places"),
    ("born_in", "places"),
    ("studied_in", "places"),
    ("visited", "places"),
    ("from", "places"),
    ("works_at", "work"),
    ("left_company", "work"),
    ("founded", "work"),
    ("ceo_of", "work"),
    ("role", "work"),
    ("title", "work"),
    ("studied", "work"),
    ("studies", "work"),
    ("employed_by", "work"),
    ("owns", "ownership"),
    ("has", "ownership"),
    ("uses", "ownership"),
    ("subscribes_to", "ownership"),
    ("member_of", "ownership"),
    ("allergic_to", "health"),
    ("diagnosed_with", "health"),
    ("takes_medication", "health"),
    ("costs", "financial"),
    ("priced_at", "financial"),
    ("earns", "financial"),
    ("spends", "financial"),
    ("completed_on", "events"),
    ("started_on", "events"),
    ("scheduled_for", "events"),
    ("birthday", "events"),
    ("anniversary", "events"),
)


_VALUES = ", ".join(f"('{p}', '{f}')" for p, f in _HISTORICAL_MAP)


def upgrade() -> None:
    # 1. From the tenant's learned relation.
    op.execute("""
        UPDATE agent_facts AS f SET predicate_family = c.family
        FROM predicate_clusters AS c
        WHERE f.predicate_family IS NULL AND c.family IS NOT NULL
          AND c.tenant_id = f.tenant_id AND c.canonical = f.predicate_canonical
    """)
    # 2. The family each remaining row was shown under until today.
    op.execute(f"""
        UPDATE agent_facts AS f SET predicate_family = v.family
        FROM (VALUES {_VALUES}) AS v(predicate, family)
        WHERE f.predicate_family IS NULL AND f.predicate_canonical = v.predicate
    """)


def downgrade() -> None:
    pass
