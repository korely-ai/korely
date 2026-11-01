"""Every request path the dashboard builds with a value encodes it as one
path segment, through one helper (lib/agentsApi.apiPath, encodeURIComponent)
(audit of 20 Oct 2026: an id unencoded in an admin request's path).

The ids were interpolated raw: a link `/memories?open=..%2Fusers%2F<id>%2F
memories` gave the memory drawer the id `../users/<id>/memories`, the browser
resolved `/admin/memories/../users/<id>/memories` to another route, and the
drawer's Delete sent the account-wide erasure of that end user. This scans
dashboard/src: a template string that starts with an API path and holds a
`${...}` is written with the tag, or the test fails. No node is needed.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dashboard" / "src"

# A template literal whose text starts with one of the API's prefixes and
# interpolates a value, and what precedes its backtick (the tag, if any).
_API_TEMPLATE = re.compile(r"(\w*)`(/(?:admin|v1|hosted|authorize|token|register|revoke)\b[^`]*\$\{[^`]*)`")
# A request path built by concatenation instead of a template.
_CONCATENATED = re.compile(r"[\"'](/(?:admin|v1|hosted)/[^\"']*)[\"']\s*\+")


def _sources():
    return sorted(p for p in SRC.rglob("*") if p.suffix in (".ts", ".tsx"))


def test_every_api_path_with_a_value_goes_through_the_helper():
    raw = []
    for path in _sources():
        for m in _API_TEMPLATE.finditer(path.read_text()):
            if m.group(1) != "apiPath":
                raw.append(f"{path.relative_to(ROOT)}: `{m.group(2)}`")
    assert raw == [], raw


def test_no_api_path_is_concatenated():
    found = [f"{p.relative_to(ROOT)}: {m.group(1)}" for p in _sources()
             for m in _CONCATENATED.finditer(p.read_text())]
    assert found == [], found


def test_the_helper_encodes_each_value_and_refuses_a_dot_segment():
    text = (SRC / "lib" / "agentsApi.ts").read_text()
    body = text[text.index("export function apiPath"):]
    body = body[:body.index("\n}\n")]
    assert "encodeURIComponent(value)" in body
    assert 'value === "."' in body and 'value === ".."' in body
    # And it is used: the walk above found the paths it should.
    assert len(re.findall(r"apiPath`", text)) >= 18


def test_the_drawer_opens_only_a_memorys_id():
    page = (SRC / "pages" / "MemoriesPage.tsx").read_text()
    assert 'useState<string | null>(params.get("open"))' not in page
    assert "/^mem_" in page
