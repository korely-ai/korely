"""The Unicode data the tests read travels with its licence, as the Swagger UI
files do (tests/test_api_page_offline.py): the Unicode License v3 asks that
its copyright and permission notice appear with every copy of the data files.
Until 1.0.37 the files kept only their own header (a copyright line and a link
to the terms of use), and NOTICE did not name them.

Both texts are pinned whole: a licence cut short, a copyright line taken out,
or a NOTICE that says something else about the licence fails here."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNICODE = ROOT / "tests" / "data" / "unicode"

# sha256 of https://www.unicode.org/license.txt as read on 24 Nov 2026, the
# file tests/data/unicode/LICENSE holds byte for byte.
LICENCE_SHA256 = "e7a93b009565cfce55919a381437ac4db883e9da2126fa28b91d12732bc53d96"

# NOTICE's section on this data, its spaces and line ends folded.
NOTICE_SECTION = (
    "Third-party: Unicode Character Database (test data) "
    "The tests read three data files of the Unicode Character Database, version "
    "14.0.0, from https://www.unicode.org/Public/14.0.0/ucd/, in tests/data/unicode/: "
    "SentenceBreakTest-14.0.0.txt and DerivedJoiningType-14.0.0.txt as published, "
    "and DefaultIgnorable-14.0.0.txt, the Default_Ignorable_Code_Point lines of "
    "DerivedCoreProperties-14.0.0.txt with that file's header. Copyright 1991-2026 "
    "Unicode, Inc., licensed under the Unicode License v3; its copyright and "
    "permission notice is in tests/data/unicode/LICENSE, next to the files, as "
    "Unicode publishes it at https://www.unicode.org/license.txt."
)


def _section(notice: str, title: str) -> str:
    """The section of NOTICE that starts at `title`, up to the next
    "Third-party:" or the end, its whitespace folded to single spaces."""
    start = notice.index(title)
    end = notice.find("\nThird-party:", start + len(title))
    return " ".join(notice[start:end if end != -1 else None].split())


def test_the_permission_notice_is_next_to_the_files_whole():
    assert hashlib.sha256((UNICODE / "LICENSE").read_bytes()).hexdigest() == LICENCE_SHA256


def test_notice_says_what_the_folder_holds_and_under_which_licence():
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert _section(notice, "Third-party: Unicode Character Database") == NOTICE_SECTION
    files = sorted(p.name for p in UNICODE.glob("*.txt"))
    assert files, "the tests read this folder"
    assert [f for f in files if f not in NOTICE_SECTION] == []
