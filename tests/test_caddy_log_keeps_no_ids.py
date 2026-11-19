"""The shipped Caddy's logs keep no end user (audit of 17 Oct 2026): a path
carries one (`/v1/users/<id>/memories`, `/<id>/memories`) and an erasure
never reaches a log. Cutting the address to its first segments, in every
spelling found (audits of 18 and 19 Oct 2026), still kept an id that was
itself a kept segment (audit of 27 Oct 2026), so Caddy
writes no address at all: the request's `uri` and a redirect's `Location`
are deleted in both logs (Caddy's own and the access log), checked on
caddy 2.11.7 on 27 Oct 2026. The API's own access line writes each path by
its route (main.loggable_path). The Referer of a request is an address too,
the page's that asked, path and query whole: a page of the dashboard sends
it to its own origin (`strict-origin-when-cross-origin`, api/spa.py), so it
is deleted in both logs as well."""
import re
from pathlib import Path

import pytest

CADDYFILE = Path(__file__).resolve().parent.parent / "deploy" / "Caddyfile"
FIELDS = ("request>uri", "resp_headers>Location", "request>headers>Referer")


def _filters():
    """The body of each `format filter { ... }` block of the Caddyfile."""
    text = CADDYFILE.read_text()
    blocks = []
    for start in (m.end() for m in re.finditer(r"format filter \{", text)):
        depth, i = 1, start
        while depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        blocks.append(text[start:i - 1])
    return blocks


def test_both_logs_filter():
    assert len(_filters()) == 2, "Caddy's own log and the access log, each with its filter"


@pytest.mark.parametrize("field", FIELDS)
def test_no_log_keeps_any_part_of_the_address(field):
    for block in _filters():
        rules = [line.split(None, 1)[1] for line in map(str.strip, block.splitlines())
                 if line.split(None, 1)[:1] == [field]]
        assert rules == ["delete"], f"{field} is deleted, nothing of it kept: {rules}"


def test_the_billing_alert_secret_header_is_dropped_from_both_logs():
    """Caddy writes the request's headers, Authorization and Cookie as
    REDACTED, every other one whole: the Cloud's billing alert secret too."""
    for block in _filters():
        assert "request>headers>X-Korely-Alert-Secret delete" in block
