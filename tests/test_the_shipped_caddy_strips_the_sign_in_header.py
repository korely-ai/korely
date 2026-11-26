"""The Caddy this stack ships removes, from every request, the header
TRUST_AUTH_HEADER names (the review of 5 Nov 2026): with it on, the API signs
in whoever that header names (core/auth._operator_from_trusted_header), and
up to 1.0.39 this Caddy, which faces the internet, passed the header on as
the client sent it, so whoever sent it became any operator.

Read as text, as the other tests of the Caddyfile read it. Checked with the
image docker-compose.yml names (caddy 2.11.9) on 26 Nov 2026: `caddy adapt`
writes the line as a `headers` handler that deletes the named header from
the request, before `reverse_proxy`, and `caddy validate` takes the file."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = (ROOT / "deploy" / "Caddyfile").read_text()
COMPOSE = (ROOT / "docker-compose.yml").read_text()


def _site_block() -> str:
    start = CADDYFILE.index("{$DOMAIN:localhost} {")
    return CADDYFILE[start:]


def test_the_site_removes_the_named_header_at_the_door():
    lines = [line.strip() for line in _site_block().splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert "request_header -{$TRUST_AUTH_HEADER:X-Forwarded-User}" in lines, lines
    # At the site's top, so every request passes it: not inside a matcher,
    # a handle or the reverse_proxy block.
    assert re.search(r"\n\trequest_header -\{\$TRUST_AUTH_HEADER:X-Forwarded-User\}\n",
                     _site_block())


def test_compose_hands_caddy_the_header_s_name():
    caddy = COMPOSE[COMPOSE.index("\n  caddy:"):]
    caddy = caddy[:caddy.index("\n  facts-dir:")]
    assert "TRUST_AUTH_HEADER: ${TRUST_AUTH_HEADER:-X-Forwarded-User}" in caddy, caddy


def test_the_example_config_says_what_the_shipped_caddy_does():
    example = (ROOT / ".env.example").read_text()
    block = example[:example.index("# TRUST_AUTH_HEADER=")]
    block = block[block.rindex("# SSO,"):]
    assert "passes the header on as the client sent it" not in block, block
    assert "removes the header named here from every request" in block, block
