"""The documentation is one text across pages (29 Oct 2026): the README is a
landing page and links into docs/guide, whose index links every page. A page
nobody links is a page nobody reads, and a link that leads nowhere, to a file
moved or to a heading renamed, is the reader's dead end. Both are read here
from the pages themselves, never from a list written in this test."""
import re
from pathlib import Path

from tests import cloud
from tests import docs_text

ROOT = docs_text.ROOT
INDEX = ROOT / docs_text.GUIDE / "README.md"

# A Markdown link or image, `[text](target)` or `[text](target "title")`, a
# reference definition, `[name]: target`, and the HTML the README's header uses.
_LINKS = (re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)"),
          re.compile(r"^\s{0,3}\[[^\]]+\]:\s+<?(\S+?)>?(?:\s|$)", re.M),
          re.compile(r"""\b(?:href|src)\s*=\s*["']([^"']+)["']"""))
_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.M | re.S)
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)


def _prose(text: str) -> str:
    """The text outside fenced code: a `# comment` in a shell block is no
    heading, and a URL in an example is no link."""
    return _FENCE.sub("", text)


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: the text as shown (code marks, emphasis
    and link targets taken out), lower case, every character that is not a
    letter, a digit, a space, `-` or `_` dropped, each space made `-`."""
    shown = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    shown = re.sub(r"[`*]", "", shown).strip().lower()
    return re.sub(r"[^\w\- ]", "", shown).replace(" ", "-")


def _anchors(path: Path) -> set:
    """Every anchor of a page, a repeated heading numbered as GitHub numbers
    it (`-1`, `-2`)."""
    seen, out = {}, set()
    for heading in _HEADING.findall(_prose(path.read_text(encoding="utf-8"))):
        slug = _slug(heading)
        n = seen.get(slug, 0)
        out.add(slug if n == 0 else f"{slug}-{n}")
        seen[slug] = n + 1
    return out


def _relative_links(path: Path):
    text = _prose(path.read_text(encoding="utf-8"))
    for pattern in _LINKS:
        for target in pattern.findall(text):
            if not re.match(r"^[a-z][a-z0-9+.-]*:|^//", target, re.I):
                yield target


def test_the_reader_of_links_finds_them():
    """The control of the test below: without it, a pattern that matched
    nothing would pass on any page."""
    found = {t for p in docs_text.pages() for t in _relative_links(p)}
    # The guide's index links its pages, the README the licence.
    assert "install.md" in found and "LICENSE" in found
    assert any("#" in t for t in found), "no link to a heading read"
    assert _slug("Which version am I running, and how to go back") == \
        "which-version-am-i-running-and-how-to-go-back"
    assert "on-a-laptop" in _anchors(ROOT / docs_text.GUIDE / "install.md")


def test_every_page_of_the_guide_is_in_its_index():
    listed = {(INDEX.parent / t.split("#", 1)[0]).resolve() for t in _relative_links(INDEX)}
    pages = [p for p in docs_text.pages() if p.parent == INDEX.parent and p != INDEX]
    assert pages, "no page of the guide read"
    # A part of the product's own pages (core/extensions.Part), which the
    # public guide and its index do not carry.
    off = set(cloud.of_the_parts("GUIDE_PAGES", tuple))
    missing = [p.name for p in pages if p.resolve() not in listed and p.name not in off]
    assert missing == [], f"pages of docs/guide its index does not link: {missing}"


def test_every_relative_link_of_the_documentation_leads_somewhere():
    broken = []
    for page in docs_text.pages():
        for target in _relative_links(page):
            where, _, anchor = target.partition("#")
            dest = (page.parent / where).resolve() if where else page
            rel = page.relative_to(ROOT)
            if not dest.exists():
                broken.append(f"{rel}: {target} (no such file)")
            elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
                broken.append(f"{rel}: {target} (no such heading)")
    assert broken == [], "links that lead nowhere:\n" + "\n".join(broken)
