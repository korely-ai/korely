"""A request sent while the API restarts is held by the shipped Caddy, not
answered 502, and Caddy sends again only a request whose connection could not
be made.

Seen on 15 Nov 2026, at a deploy: a POST that arrived while the API was down
was held 5.8 s and answered 502 "invalid Read on closed Body", while a GET was
held and served. And the shipped Caddyfile held nothing at all: measured with
scripts/caddy_restart_check.sh on caddy 2.11.9, two restarts and two
recreations of the API gave 3,235 answers 502 out of 4,231 (small writes,
300 KB writes and reads alike). The run is the proof and is too heavy for the
suite (an install, its images, four restarts); this holds what it measured,
in every reverse_proxy the Caddyfile has.

Caddy retries a failed connection for every method; a request whose
connection was made is retried only if it meets `lb_retry_match`, by
default a GET (https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#lb_retry_match).
The body survives the failed connection only from Caddy 2.11.4
(caddyserver/caddy#7547; before, the transport closed it, #7546), hence the
image's version, named by its digest."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = (ROOT / "deploy" / "Caddyfile").read_text()
COMPOSE = (ROOT / "docker-compose.yml").read_text()
# The first release that keeps an unread body across a failed connection.
KEEPS_THE_BODY = (2, 11, 4)


def _lines(text):
    """The Caddyfile's directives, comments and blank lines left out."""
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _proxies():
    """Each reverse_proxy of the Caddyfile, wherever it is (a site, a handle,
    a route), as the lines of its block, nested blocks included."""
    lines = _lines(CADDYFILE)
    found = []
    for i, line in enumerate(lines):
        if line.split()[0] != "reverse_proxy":
            continue
        assert line.endswith("{"), f"a reverse_proxy with no block holds nothing: {line!r}"
        depth, block = 1, []
        for inner in lines[i + 1:]:
            depth += inner.count("{") - inner.count("}")
            if depth == 0:
                break
            block.append(inner)
        found.append(block)
    assert found, "the Caddyfile proxies to the API"
    return found


def _directive(block, name):
    found = [line.split()[1:] for line in block if line.split()[0] == name]
    assert len(found) == 1, f"{name} once in each reverse_proxy block: {found}"
    return found[0]


def _seconds(duration):
    value, unit = re.fullmatch(r"(\d+)(ms|s)", duration).groups()
    return int(value) / (1000 if unit == "ms" else 1)


def test_a_restart_is_waited_for():
    """Up to lb_try_duration, a try every lb_try_interval (0 would spin the
    CPU while the API is down, the docs warn)."""
    for block in _proxies():
        duration = _seconds(_directive(block, "lb_try_duration")[0])
        interval = _seconds(_directive(block, "lb_try_interval")[0])
        assert 0 < interval < duration
        # Past the slowest restart measured, 11 s from the command to ready (the
        # longest a request was held, 10.3 s), and short of the 30 s the SDKs
        # wait by default, so a client hears Caddy's 502 and not its own timeout.
        assert 11 < duration < 30


def test_only_a_connection_never_made_is_tried_again():
    """Caddy sends a request again only when no connection to the API could
    be made: never one that went out, which the API may have had (a write
    would be made twice, and a request that brought the API down would be
    sent to it again by Caddy; Go's transport under it may still send, once,
    a GET, HEAD, OPTIONS or TRACE, or a request with no body and an
    Idempotency-Key, that failed on a kept-alive connection the API had
    closed: a write with its body, never). One condition, written on its
    line, that no request meets: a block of matchers (`lb_retry_match {
    method POST }`) would let some through."""
    for block in _proxies():
        assert _directive(block, "lb_retry_match") == ["expression", "false"]


def test_the_image_keeps_the_body_across_a_failed_connection():
    """The version, and the digest that makes it that version: a tag can be
    moved, and `caddy:2.11.90-alpine` reads as a version past 2.11.4 whether
    or not it exists."""
    service = COMPOSE.split("\n  caddy:\n", 1)[1].split("\n\n", 1)[0]
    image = re.search(r"^    image: caddy:(\d+)\.(\d+)\.(\d+)-alpine@sha256:([0-9a-f]{64})$",
                      service, re.M)
    assert image, "the Caddy image is named by its version and its digest"
    assert tuple(map(int, image.groups()[:3])) >= KEEPS_THE_BODY
    # The index of caddy:2.11.9-alpine (every platform), read from the
    # registry on 16 Nov 2026; a new pin is read the same way and written here.
    assert image.group(0).endswith(
        "caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0")


def test_no_copy_of_the_body_is_kept():
    """request_buffers, anywhere: it adds nothing on the image's Caddy and
    costs up to its size per request; on Caddy 2.10 and earlier a failed
    connection gives the copy back to the shared pool while the retry still
    reads it, and on 2.8.4 a write answered 201 was stored with another
    request's body (4 of 794, scripts/caddy_restart_check.sh --writers 8)."""
    assert all(line.split()[0] != "request_buffers" for line in _lines(CADDYFILE))


def test_what_stays_as_it_was():
    """The body's ceiling, the MCP's stream and the client's address."""
    assert re.search(r"request_body \{\n\t\tmax_size 80MB\n\t\}", CADDYFILE)
    for block in _proxies():
        assert _directive(block, "flush_interval") == ["-1"]
        headers = [line.split()[1:] for line in block if line.split()[0] == "header_up"]
        assert ["X-Forwarded-For", "{client_ip}"] in headers
        assert ["X-Forwarded-Proto", "{scheme}"] in headers
