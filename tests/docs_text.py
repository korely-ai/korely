"""The documentation an operator reads, in one place (29 Oct 2026).

The README became a landing page and its long sections moved, word for word,
to docs/guide/*.md. The tests that hold "the docs" to what the code does read
README.md alone and, once the sentences had moved, either failed or, for a
sentence that must not be there, passed on a page that no longer held it.
They read this instead: the README and every page of docs/guide, as one text.
A page added to docs/guide is read without touching any test.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

README = "README.md"
GUIDE = "docs/guide"


def pages() -> list:
    """The README first, then every page of the guide, its index included."""
    return [ROOT / README, *sorted((ROOT / GUIDE).glob("*.md"))]


def page(rel: str) -> str:
    """One page, for a test about where a section lives (a table, a recipe)."""
    return (ROOT / rel).read_text(encoding="utf-8")


def text() -> str:
    """The whole documentation, page after page, with a blank line between
    two pages so that no sentence runs into the next page's first one."""
    return "\n\n".join(p.read_text(encoding="utf-8") for p in pages())


def flat() -> str:
    """text() with every run of whitespace made one space, so that a sentence
    reads the same wherever its lines break."""
    return " ".join(text().split())
