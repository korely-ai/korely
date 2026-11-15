"""Serving the screen from the same machine as the store.

The page is static files produced by the dashboard build and copied into the
image. The application serves them rather than the proxy, so the install stays
two containers plus a proxy and there is one artefact to deploy.

The whole point of this file is the fallback. A single-page application owns
its own routes: `/memories`, `/facts/abc` and everything else exist only in the
browser, and asking the server for one has to answer with `index.html` or a
reload of any page but the first gives a 404. The mistake that fallback invites
is answering `index.html` to *everything*, which turns a typo in an API path
into an HTML page with a 200 on it, and a client parsing JSON into a puzzle.
So the fallback refuses the API prefixes by name.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from starlette.convertors import Convertor, register_url_convertor

logger = logging.getLogger(__name__)

# Everything the server owns. A path under one of these is never the screen's,
# so it gets the 404 it deserves instead of a page.
# OAuth's doors (api/oauth.py) are the server's too: a GET on /token used to
# get the dashboard's page with a 200 (found on 16 Oct 2026).
API_PREFIXES = ("v1/", "admin/", "hosted/", "mcp", "agent/mcp", "health", "docs", "redoc", "openapi.json",
                ".well-known/", "authorize", "token", "register", "revoke")


# The key the screen sets in the request's scope when it serves a file of the
# dashboard's folder: that file, by its place in the folder (`assets/x.js`,
# "" for the root). The access line writes it in place of the path the client
# sent (main.loggable_path), which is never written.
OWN_FILE = "korely_screen_file"


class _ScreenPath(Convertor):
    """A path the screen may own: anything but the API prefixes, matched in
    the route itself (2026-10-06). The fallback answers GET only, and while it
    matched every path a POST or a DELETE to an API path that does not exist
    here (the hosted product's DELETE /v1/account, POST /v1/agents/init)
    matched its path and not its method: 405, as if the route existed, where
    the client needed 404. Now no route matches such a path, the store
    answers 404, and a real route called with a wrong method keeps its 405."""

    regex = "(?!(?:" + "|".join(p.replace(".", r"\.") for p in API_PREFIXES) + ")).*"
    # The parameter the access log writes as the file the screen served
    # (OWN_FILE), when it served one of its folder's; otherwise it becomes its
    # name, as every other parameter does (main.loggable_path).
    loggable = True

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("screen", _ScreenPath())


def _safe(root: Path, relative: str) -> Optional[Path]:
    """Resolve a request path inside `root`, or None.

    `..` in a URL is the oldest way to read a file that was never meant to be
    served. Resolving both sides and comparing is the check that actually
    holds, rather than looking for the string.
    """
    try:
        candidate = (root / relative.lstrip("/")).resolve()
    except (OSError, ValueError):
        return None
    if candidate == root or root in candidate.parents:
        return candidate if candidate.is_file() else None
    return None


# No other site may show the screen inside a frame of its own (audit of 18
# Oct 2026): the OAuth consent page hands an app a key to a project's
# memories with one click on Allow, and a page framed under a decoy is how
# that click is stolen. Only the self-hosted Caddyfile sent it; the Cloud's
# proxy is not in this repository, so the application says it itself, on
# every file of the screen. X-Frame-Options for older browsers, CSP
# frame-ancestors for the rest (CSP Level 2; RFC 7034).
NOT_FRAMED = {"X-Frame-Options": "DENY"}

# What the screen's pages may run and show, in the order the header writes
# it (CSP Level 3, w3.org/TR/CSP3; review of 15 Nov 2026): scripts of its
# own origin only, the `blob:` worker the graph's layout starts
# (graphology-layout-forceatlas2/worker), frames of its own origin, no
# plugin, no <base> that points elsewhere, and framed by nobody. What a
# page loads from outside the origin is added by whoever needs it, and
# only where it is served (`screen_headers`: the extensions' hook and the
# parts mounted here declare it). Directives not named (styles, images,
# fonts, connections) stay open: an allowlist of scripts and frames, not a
# nonce-based strict policy (web.dev/articles/strict-csp), and the screen
# makes no request for a script elsewhere.
SCREEN_POLICY = (
    ("script-src", ("'self'",)),
    ("worker-src", ("'self'", "blob:")),
    ("frame-src", ("'self'",)),
    ("object-src", ("'none'",)),
    ("base-uri", ("'self'",)),
    ("frame-ancestors", ("'none'",)),
)

# Where a page of the screen sends the address it is at: the origin alone
# to another site, the whole address to its own (the browsers' default,
# developer.chrome.com/blog/referrer-policy-new-chrome-default). A path of
# the screen can carry an end user (`/users/<id>`); and a script of another
# origin that the page loads with an API key restricted to the dashboard's
# address is refused without a referrer (docs.cloud.google.com/api-keys/
# docs/add-restrictions-api-keys), which `no-referrer`, the Caddyfile's for
# every answer until 1.0.27, made of every request.
SCREEN_REFERRER = "strict-origin-when-cross-origin"
#: Every other answer of the install says `no-referrer`
#: (main._RememberTheRequest), unless it says a policy of its own.
EVERY_OTHER_ANSWER = (b"referrer-policy", b"no-referrer")


def screen_headers() -> dict:
    """The headers of every file of the screen: NOT_FRAMED, SCREEN_REFERRER
    and the Content-Security-Policy, SCREEN_POLICY with the sources from
    outside the origin that the Cloud's own screens (extensions.hooks.
    screen_sources) and the parts of the product mounted here
    (Part.screen_sources) declare, each in its directive. A source for a
    directive the policy does not have is a mistake of the code, refused."""
    from ..core import extensions

    added: dict = {name: [] for name, _own in SCREEN_POLICY}
    for more in (extensions.hooks.screen_sources(),
                 *(part.screen_sources() for part in extensions.mounted())):
        for directive, sources in more.items():
            if directive not in added:
                raise ValueError(f"the screen's policy has no {directive!r}")
            added[directive].extend(sources)
    policy = "; ".join(f"{name} {' '.join(dict.fromkeys((*own, *added[name])))}"
                       for name, own in SCREEN_POLICY)
    return {**NOT_FRAMED, "Content-Security-Policy": policy,
            "Referrer-Policy": SCREEN_REFERRER}


def build_router(static_dir: Path) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    index = static_dir / "index.html"

    @router.get("/{full_path:screen}")
    def screen(full_path: str, request: Request):
        if full_path.startswith(API_PREFIXES):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        hit = _safe(static_dir, full_path) if full_path else None
        if hit is not None:
            # A file of the dashboard's own: the access line writes the file,
            # not the path that reached it (main.loggable_path). Any other path
            # this catch-all takes can be a mis-built API path that carries an
            # end user, `/<id>/memories`, and is written as the route's name.
            request.scope[OWN_FILE] = hit.relative_to(static_dir).as_posix()
            return FileResponse(hit, headers=screen_headers())
        if not full_path:
            request.scope[OWN_FILE] = ""
        return FileResponse(index, headers=screen_headers())

    return router


def mount(app, static_dir: str) -> bool:
    """Attach the screen, if there is one. Returns whether it was attached.

    A missing directory is not an error: the API is the product and the screen
    is a convenience on top of it. Somebody running from source without Node
    gets a working service and a line in the log saying where the page would
    have been.
    """
    # The directory is DASHBOARD_DIR's, whose default is the image's
    # /app/static: no second copy of it here (audit of 17 Oct 2026).
    root = Path(static_dir).resolve()
    if not (root / "index.html").is_file():
        logger.info("[dashboard] no dashboard at %s: the API is up, there is no screen.", root)
        return False
    app.include_router(build_router(root))
    logger.info("[dashboard] served from %s", root)
    return True
