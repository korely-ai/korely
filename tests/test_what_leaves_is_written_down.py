"""What leaves the machine with a write, said where an operator reads it
(audit of 17 Oct 2026): the README's list and the dashboard's texts named the
memory's text alone. The extraction also sends the end user's id and up to
`PREVIOUS_MEMORIES` earlier memories of the same person, so a region change
sends texts written in the other region too. These tests tie the words to the
numbers the code uses."""
from pathlib import Path

from korely_agent.services.facts.service import PREVIOUS_MEMORIES, PREVIOUS_MEMORY_CHARS

from tests import docs_text

ROOT = Path(__file__).resolve().parent.parent
WORDS = {10: "ten"}


def _flat(path: str) -> str:
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def test_the_readme_lists_the_end_users_id_and_earlier_memories():
    docs = docs_text.flat()
    assert "with the end user's id as your agent sent it" in docs
    assert (f"up to {WORDS[PREVIOUS_MEMORIES]} of the same end user's earlier memories"
            in docs), "the documentation's number is not the code's"
    assert f"{PREVIOUS_MEMORY_CHARS} characters each" in docs


def test_no_text_promises_that_a_region_change_rereads_nothing():
    for path, text in (("the documentation", docs_text.flat()),
                       ("RegionSelect.tsx", _flat("dashboard/src/components/RegionSelect.tsx"))):
        assert "nothing already written is read again" not in text, path


def test_no_text_says_a_memory_leaves_once():
    assert "Every memory is read once by a model" not in _flat("dashboard/src/pages/SetupPage.tsx")


def test_the_readme_names_every_personal_datum_of_the_audit_trail():
    """The trail keeps the operator's username on each dashboard action and the
    client address of each sign-in (api/admin.py, `_audit` and the login), not
    only the end user's id (audit of 17 Oct 2026)."""
    docs = docs_text.flat()
    assert "The one personal datum it keeps is the end user's id" not in docs
    assert "the username of the operator behind each action" in docs
    assert "client address of each dashboard sign-in" in docs


def test_the_readme_says_the_screens_writer_wins_over_the_file():
    """The README said MISTRAL_API_BASE sends the writing to a server of your
    own; a provider saved on the screen wins over .env, so every memory still
    went to it (audit of 17 Oct 2026). The way back is the Settings button."""
    docs = docs_text.flat()
    button = "Use what .env says instead"
    assert button in docs and button in _flat("dashboard/src/pages/SettingsPage.tsx")
    assert "a provider saved on the screen wins over `.env`" in docs
    assert button in _flat("scripts/choose_models.py")
