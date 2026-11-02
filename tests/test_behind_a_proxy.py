"""What the application believes about how it is being reached.

TLS terminates at Caddy, so the request that arrives here is plain HTTP and the
only thing saying otherwise is `X-Forwarded-Proto`. If the app does not read it,
every absolute URL it generates says http, and the one that matters is a
redirect: FastAPI sends /mcp to /mcp/, and an MCP client following a
`Location: http://...` resends `Authorization: Bearer kor_live_...` over an
unencrypted connection. The credential is on the wire in clear text before
anything redirects it back.

Found by installing the thing from a clean clone and looking at the headers,
which is the only way this kind of defect shows up: every test in the suite ran
without a proxy in front, so every test saw the truth.
"""
import pathlib
import re

import pytest
from fastapi.testclient import TestClient



@pytest.fixture
def proxied(db, monkeypatch):
    """The app wrapped in the same middleware uvicorn installs for --proxy-headers."""
    monkeypatch.setenv("RUN_WORKER", "false")
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from korely_agent.db import get_db
    from korely_agent.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts="*")
    with TestClient(wrapped, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


class TestThereIsNoRedirectLeftToDowngrade:
    """The stronger version of the property this file was opened for.

    The defect here was a redirect from `/mcp` to `/mcp/` that said
    `Location: http://...` behind a TLS proxy, so a client following it resent
    `Authorization: Bearer kor_live_...` in clear text. Reading
    `X-Forwarded-Proto` fixed the scheme.

    Then a second defect turned up on a real install: with the screen mounted,
    which is every install, a bare `/mcp` never redirected at all. It hit the
    screen's catch-all, which answers GET and nothing else, and an MCP client
    got 405. Both spellings are now the same request, answered in place, so
    there is no `Location` header on this path for anything to downgrade. That
    closes the original hole by removing it rather than by correcting it.
    """

    def test_the_bare_address_is_answered_and_not_redirected(self, proxied):
        r = proxied.post(
            "/mcp",
            headers={"X-Forwarded-Proto": "https",
                     "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}},
            follow_redirects=False,
        )
        assert r.status_code not in (301, 302, 307, 308), (
            f"a redirect is back on /mcp (Location: "
            f"{r.headers.get('location')!r}); it used to carry the API key "
            f"over http behind a proxy"
        )
        assert r.status_code != 405, "the address in our own documentation answers 405"


class TestTheServerIsToldToReadThem:
    """The middleware above only runs if uvicorn is started with the flags.

    Asserted against the Dockerfile because that is where the defect lived: the
    application code was always correct, and shipped a downgraded redirect for
    as long as the image existed.
    """

    def test_the_image_starts_uvicorn_with_proxy_headers(self):
        src = (pathlib.Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
        cmd = re.search(r"^CMD \[.*?\]", src, re.S | re.M)
        assert cmd, "no CMD in the Dockerfile"
        line = cmd.group(0)
        assert "--proxy-headers" in line, (
            "uvicorn ignores X-Forwarded-Proto without this, and builds http:// "
            "redirects behind a TLS proxy"
        )
        # Trusted through the environment uvicorn reads when the flag is
        # absent, so that one line of .env narrows it (audit of 17 Oct 2026).
        assert "--forwarded-allow-ips" not in line, (
            "a flag in CMD wins over FORWARDED_ALLOW_IPS, and .env could not narrow it")
        env = re.search(r"^ENV FORWARDED_ALLOW_IPS=(\S+)\s*$", src, re.M)
        assert env and env.group(1) == "*", (
            "uvicorn trusts only 127.0.0.1 by default, and the proxy reaches "
            "this container from the compose network"
        )

    def test_uvicorn_reads_the_variable_when_the_flag_is_absent(self, monkeypatch):
        from uvicorn.config import Config

        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.7")
        assert Config("korely_agent.main:app", proxy_headers=True).forwarded_allow_ips == "10.0.0.7"

    def test_caddy_actually_sends_the_header(self):
        caddyfile = (pathlib.Path(__file__).resolve().parents[1]
                     / "deploy" / "Caddyfile").read_text()
        assert "X-Forwarded-Proto" in caddyfile


class TestTheClientDoesNotChooseItsAddress:
    """With FORWARDED_ALLOW_IPS=* uvicorn takes the leftmost entry of
    X-Forwarded-For as the client: a proxy that appends to the header lets
    the client choose the address the login lock, the LOGIN audit row and the
    per-network caps read (audit of 17 Oct 2026). Caddy's default already
    replaces it when no proxy is trusted; the Caddyfile says so in writing,
    and the README tells whoever replaces Caddy what to do."""

    ROOT = pathlib.Path(__file__).resolve().parents[1]

    def test_caddy_writes_the_header_with_the_connections_address(self):
        caddyfile = (self.ROOT / "deploy" / "Caddyfile").read_text()
        proxy = re.search(r"reverse_proxy api:8000 \{(.*?)\n\t\}", caddyfile, re.S)
        assert proxy, "no reverse_proxy block for the API"
        assert re.search(r"^\s*header_up X-Forwarded-For \{client_ip\}\s*$", proxy.group(1), re.M), (
            "the header the API reads the client from must be written by Caddy, not passed on"
        )
        # A trusted proxy, if one is ever added, has its header read from the
        # right: Caddy's default reads it from the left, the client's end.
        assert re.search(r"^\s*trusted_proxies_strict\s*$", caddyfile, re.M)

    def test_whoever_replaces_caddy_is_told(self):
        """Since the README became a landing page (29 Oct 2026) the section
        is in docs/guide/operations.md, one heading level up."""
        from tests import docs_text

        operations = docs_text.page("docs/guide/operations.md")
        assert "\n## Your own proxy in front\n" in operations
        section = operations.split("\n## Your own proxy in front\n", 1)[1].split("\n## ", 1)[0]
        assert "proxy_set_header X-Forwarded-For $remote_addr" in section
        assert "FORWARDED_ALLOW_IPS=<your proxy's address>" in section
        assert "Your own proxy in front" in (self.ROOT / "Dockerfile").read_text()
