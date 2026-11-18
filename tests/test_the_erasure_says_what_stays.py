"""What DELETE /v1/users/{end_user}/memories says it takes and what it says
stays are what it does (5 Nov 2026: its first line said every other row the
store keeps of the person went, its paragraph below that the audit row
stays with a pseudonym). A place the erasure deletes from holds what was
said or learned of the person; a place that keeps its rows and gives them
the pseudonym (services/end_user_places, `pseudonymise` and no `erase`)
records what was done, and the document names each of them."""
import inspect

from korely_agent.api.memories import ERASE_USER, delete_user_memories
from korely_agent.services.end_user_places import PLACES

#: Each place that keeps its rows, as the document names it.
NAMED_AS = {
    "erasure_records": "the record of this erasure",
    "audit_events": "the rest of the access trail",
    "webhooks_delivered": "the webhooks already delivered",
    "confined_keys": "a key or a grant is confined to",
}


def _document() -> str:
    return " ".join(inspect.getdoc(delete_user_memories).split("\f", 1)[0].split())


def test_the_first_line_claims_only_what_the_erasure_deletes():
    first = _document().split(". ", 1)[0]
    assert ERASE_USER in first
    assert "every other row the store keeps" not in first


def test_every_place_that_keeps_its_rows_is_named_as_staying():
    kept = {p.name for p in PLACES if p.pseudonymise is not None and p.erase is None}
    assert kept == set(NAMED_AS), sorted(kept ^ set(NAMED_AS))
    document = _document().lower()
    missing = [name for name, words in NAMED_AS.items() if words not in document]
    assert missing == [], missing
