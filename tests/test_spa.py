"""Serving the screen from the same machine as the store.

Two ways this goes wrong, and they pull in opposite directions. Without a
fallback, reloading any page but the first gives a 404, because the routes
after the first one exist only in the browser. With a fallback that catches
everything, a typo in an API path answers with an HTML page and a 200, and
whoever is debugging spends an afternoon on it. The third is the oldest one in
the book: `..` in a URL.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from korely_agent.api import spa


@pytest.fixture
def built(tmp_path):
    """A directory shaped like a Vite build."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>Korely</title>")
    (tmp_path / "assets" / "index-abc.js").write_text("console.log(1)")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("not for the internet")
    return tmp_path


@pytest.fixture
def client(built):
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/facts")
    def facts():
        return {"facts": []}

    assert spa.mount(app, str(built)) is True
    return TestClient(app)


class TestTheScreen:
    def test_the_root_is_the_page(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "Korely" in r.text

    def test_assets_are_served_as_themselves(self, client):
        r = client.get("/assets/index-abc.js")
        assert r.status_code == 200
        assert "console.log" in r.text

    def test_a_route_that_only_exists_in_the_browser(self, client):
        """`/memories` is not a server route and never will be. Reloading it
        has to give the page, or the back button breaks."""
        r = client.get("/memories")
        assert r.status_code == 200
        assert "Korely" in r.text

    def test_a_deep_browser_route_too(self, client):
        assert "Korely" in client.get("/facts/fct_abc/history").text


class TestItDoesNotSwallowTheApi:
    def test_the_real_routes_still_answer(self, client):
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/v1/facts").json() == {"facts": []}

    def test_openapi_is_json_not_the_page(self, client):
        """It is a real route, registered before the catch-all. If the ordering
        ever changed, this would come back as HTML with a 200."""
        r = client.get("/openapi.json")
        assert r.headers["content-type"].startswith("application/json")

    # `/openapi.json` is deliberately absent from this list: on the real app it
    # is a route, and the point of the prefix list is that the catch-all does
    # not answer for it either way.
    @pytest.mark.parametrize("path", [
        "/v1/nonexistent", "/v1/memories/typo", "/admin/nope",
        "/docs/x", "/mcp/anything", "/health/typo",
    ])
    def test_a_typo_under_an_api_prefix_is_a_404_not_a_page(self, client, path):
        """A page with a 200 on it, where JSON was expected, is an afternoon."""
        r = client.get(path)
        assert r.status_code == 404, f"{path} answered {r.status_code}"
        assert "<!doctype" not in r.text.lower()


class TestAnApiPathThatIsNotHereIsA404WhateverTheMethod:
    """The fallback answers GET. While it matched every path, a POST to an
    API path this product does not have (the hosted DELETE /v1/account, POST
    /v1/agents/init) matched its path and not its method, and answered 405,
    as if the route existed (found by the SDK's contract check, 2026-10-06)."""

    @pytest.mark.parametrize("method,path", [
        ("DELETE", "/v1/account"), ("POST", "/v1/agents/init"),
        ("PUT", "/v1/nonexistent"), ("PATCH", "/admin/nope"), ("POST", "/health/typo"),
    ])
    def test_404(self, client, method, path):
        r = client.request(method, path)
        assert r.status_code == 404, f"{method} {path} answered {r.status_code}"

    def test_a_real_route_with_a_wrong_method_keeps_its_405(self, client):
        assert client.delete("/v1/facts").status_code == 405

    def test_the_screen_still_answers_get_only(self, client):
        assert client.post("/memories").status_code == 405


class TestPathTraversal:
    """Tested on the function, not through the client.

    The first version of this sent `/../secret.txt` and friends over HTTP and
    passed with the check replaced by `".." not in path`, because the test
    client collapses the path before the application ever sees it. A test that
    goes green against the broken version is not a test. `_safe` is where the
    decision is made, so that is where the question gets asked.
    """

    def test_the_ordinary_case_resolves(self, built):
        assert spa._safe(built, "index.html") == built / "index.html"
        assert spa._safe(built, "assets/index-abc.js") == built / "assets" / "index-abc.js"

    @pytest.mark.parametrize("path", [
        "../secret.txt",
        "assets/../../secret.txt",
        "./../../secret.txt",
        "a/b/c/../../../../secret.txt",
        "/../secret.txt",
    ])
    def test_nothing_escapes_the_directory(self, built, path):
        assert spa._safe(built, path) is None, f"{path!r} reached outside"

    def test_an_absolute_path_does_not_win(self, built):
        """`root / "/etc/passwd"` is `/etc/passwd` in pathlib, which is the
        surprise that makes this worth its own line."""
        assert spa._safe(built, "/etc/passwd") is None
        assert spa._safe(built, str(built.parent / "secret.txt")) is None

    def test_a_symlink_out_of_the_directory_is_refused(self, built):
        """The case that separates resolving from looking for "..".

        A build output can contain a symlink, deliberately or by accident, and
        its path holds no `..` at all. `resolve()` follows it and the answer is
        a file outside the directory; a check that greps the request path sees
        nothing wrong. This is the reason the check is written the way it is,
        and the reason this test exists at all: the first version of this class
        stayed green with the resolving replaced by a string search.
        """
        outside = built.parent / "secret.txt"
        link = built / "sneaky.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("no symlinks here")
        assert spa._safe(built, "sneaky.txt") is None

    def test_a_directory_is_not_a_file(self, built):
        assert spa._safe(built, "assets") is None

    def test_something_that_is_not_there(self, built):
        assert spa._safe(built, "nope.js") is None

    def test_and_over_http_as_well(self, client):
        """Belt and braces. These are normalised by the client before they
        arrive, which is why the real check is above."""
        for path in ("/../secret.txt", "/assets/../../secret.txt",
                     "/%2e%2e/secret.txt"):
            assert "not for the internet" not in client.get(path).text


class TestWithoutABuild:
    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        """The API is the product; the screen is a convenience on top of it.
        Somebody running from source without Node gets a working service."""
        app = FastAPI()

        @app.get("/health")
        def health():
            return {"status": "ok"}

        assert spa.mount(app, str(tmp_path / "nothing-here")) is False
        c = TestClient(app)
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/").status_code == 404

    def test_a_directory_with_no_index_counts_as_missing(self, tmp_path):
        (tmp_path / "assets").mkdir()
        app = FastAPI()
        assert spa.mount(app, str(tmp_path)) is False


class TestTheReadmeKnowsThereIsAScreen:
    def test_it_does_not_say_there_is_none(self):
        """Step 6 of the install opens the screen, and the section on health
        went on to say there was no dashboard on a box you host: written the
        day before the image started serving one, and never read again. An
        operator believing it looks for the provider's state on the command
        line only, when Settings shows it next to the key. Read in the whole
        documentation, the README and docs/guide, where the section on health
        lives since 29 Oct 2026."""
        from pathlib import Path
        import re

        from tests import docs_text

        root = Path(__file__).resolve().parents[1]
        assert "COPY --from=dashboard" in (root / "Dockerfile").read_text(), (
            "the image no longer carries the screen: this test and the documentation both change")
        denial = re.search(r"[^.\n]*\bno (dashboard|screen)\b[^.\n]*", docs_text.text(),
                           re.IGNORECASE)
        assert denial is None, f"the documentation says: {denial.group(0).strip()!r}"


class TestNoOtherSiteFramesTheScreen:
    """The consent page gives an app a key to a project's memories with one
    click on Allow, and the application sent no framing protection of its
    own: only the self-hosted Caddyfile did, so behind any other proxy, the
    Cloud's among them, any site could frame it under a decoy (audit of 18
    Oct 2026). Every page of the screen says so itself now."""

    @pytest.mark.parametrize("path", ["/oauth/consent", "/login", "/", "/assets/index-abc.js"])
    def test_the_screen_refuses_to_be_framed(self, client, path):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
