"""Where install.sh sends somebody, there is something to read.

A folder install.sh made, asked for another --version, stopped with "This
script installs; upgrading is in the README (Upgrading)", and the README had
no such section: the upgrade of such a folder was written nowhere (row of
the review, 9 Nov 2026). The steps are now in docs/guide/operations.md,
"Upgrading", and the message gives its address. This holds every pointer of
the script's own lines (comments aside) to a document: an address of the
public repository's main with a section, or a section of the README named
in brackets.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
PUBLIC = re.search(r'^PUBLIC_REPO="([^"]+)"$', INSTALL, re.M).group(1)


def _lines() -> list[str]:
    """install.sh's lines that run or print, its comments left out."""
    return [ln for ln in INSTALL.splitlines() if not ln.lstrip().startswith("#")]


def _slug(heading: str) -> str:
    """The anchor GitHub gives a Markdown heading: lower case, everything but
    letters, digits, spaces, `-` and `_` dropped, spaces to `-`."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)  # a `#` in a code block is no heading
    return {_slug(m.group(1)) for m in re.finditer(r"^#{1,6} (.+)$", text, re.M)}


def _pointers() -> list[tuple[str, str]]:
    """(file, anchor) of every pointer in install.sh's own lines."""
    found = []
    address = re.compile(r"https://github\.com/(?:\$PUBLIC_REPO|" + re.escape(PUBLIC)
                         + r")/blob/main/([\w./-]+\.md)#([\w-]+)")
    in_the_readme = re.compile(r"\bREADME \(([^)]+)\)")
    for line in _lines():
        found += [(m.group(1), m.group(2)) for m in address.finditer(line)]
        found += [("README.md", _slug(m.group(1))) for m in in_the_readme.finditer(line)]
    return found


def test_every_pointer_names_a_section_that_exists():
    pointers = _pointers()
    assert pointers, "install.sh points to no document: this test reads nothing"
    for name, anchor in pointers:
        path = ROOT / name
        assert path.is_file(), f"install.sh points to {name}, which this tree does not have"
        assert anchor in _anchors(path), f"install.sh points to {name}#{anchor}: no such heading"


def test_a_folder_asked_for_another_version_is_sent_to_its_upgrade():
    refusal = next(ln for ln in _lines() if "--version asks for $VERSION_ASKED" in ln)
    assert "docs/guide/operations.md#upgrading" in refusal
    section = (ROOT / "docs" / "guide" / "operations.md").read_text(encoding="utf-8") \
        .split("\n## Upgrading\n", 1)[1].split("\n## ", 1)[0]
    assert "### An install made by install.sh" in section
    for what in ("KORELY_IMAGE=", "release=", ".korely-install", "./korely start", "pg_dump"):
        assert what in section, what
