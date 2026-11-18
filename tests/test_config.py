"""Configuration surface.

The reason these tests exist is the number in `test_surface_stays_small`. The
codebase this was extracted from reached 139 settings, and the way it got there
was one reasonable addition at a time, each of which made sense on its own. A
client installing this has to understand every knob we expose, so the count is a
product decision, not an implementation detail, and it deserves a test that
forces a conversation when it moves.
"""
import pytest
from pydantic import ValidationError

from korely_agent.config import AgentSettings


def _in_a_clean_process(code: str) -> str:
    """Run `code` with no DATABASE_URL, in its own interpreter. Returns stdout.

    stderr is folded in on failure so a broken subprocess reports what broke
    instead of an empty string and an assertion nobody can read.
    """
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DATABASE_URL", "TEST_DATABASE_URL"))}
    env["PYTHONPATH"] = os.getcwd()
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, timeout=60)
    if r.returncode != 0:
        return f"subprocess failed ({r.returncode}): {r.stderr.strip()[-400:]}"
    return r.stdout.strip()



DB = "postgresql://u:p@localhost:5432/korely"


class TestBoot:
    def test_boots_with_only_a_database_url(self):
        """Everything else must have a working default.

        A first install should be `DATABASE_URL=... docker compose up`, not a
        scavenger hunt through the source for required variables. The monolith
        demanded four values that only exist because it also sells a free tier.
        """
        s = AgentSettings(database_url=DB)
        assert s.extraction_provider == "gemini"
        assert s.write_async is True

    def test_database_url_is_the_only_required_value(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(ValidationError) as e:
            AgentSettings(_env_file=None)
        missing = {err["loc"][0] for err in e.value.errors() if err["type"] == "missing"}
        assert missing == {"database_url"}, f"unexpected required fields: {missing}"


class TestProviderDetection:
    """Whether extraction can run, asked of `core.llm.provider_configured`,
    the function the product reads (the settings' own `has_llm_provider`
    saw only the file and nothing read it; gone 2026-10-06)."""

    def _configured(self, monkeypatch, settings, google_key):
        from korely_agent.core import llm

        monkeypatch.setattr(llm, "get_settings", lambda: settings)
        monkeypatch.setattr(llm, "screen_writers", lambda: {})
        monkeypatch.setattr(llm, "current_google_key", lambda: google_key)
        return llm.provider_configured()

    def test_no_key_means_no_extraction(self, monkeypatch):
        s = AgentSettings(database_url=DB, google_api_key=None)
        assert self._configured(monkeypatch, s, None) is False

    def test_gemini_key_enables_it(self, monkeypatch):
        s = AgentSettings(database_url=DB, google_api_key="x")
        assert self._configured(monkeypatch, s, "x") is True

    def test_mistral_provider_reads_the_mistral_key(self, monkeypatch):
        """A Gemini key must not satisfy a Mistral provider.

        Getting this backwards means the boot check passes and extraction fails
        on every write instead.
        """
        s = AgentSettings(
            database_url=DB, extraction_provider="mistral",
            google_api_key="gemini-key", mistral_api_key=None,
        )
        assert self._configured(monkeypatch, s, "gemini-key") is False
        s2 = s.model_copy(update={"mistral_api_key": "m"})
        assert self._configured(monkeypatch, s2, "gemini-key") is True

    @pytest.mark.parametrize("value", ["openai", "mistal", "", "Mistral", " mistral ", "gemini"])
    def test_a_line_that_is_not_exactly_mistral_is_the_gemini_writer(self, value):
        """As `get_provider` has always read the line, and now said as
        such: until 4 Nov 2026 the setting kept `openai` while the install
        wrote with Gemini. Refusing such a line at start would stop installs
        that boot today, a decision of its own not taken here."""
        assert AgentSettings(database_url=DB, extraction_provider=value).extraction_provider == "gemini"

    def test_exactly_mistral_is_the_openai_compatible_writer(self):
        assert AgentSettings(database_url=DB, extraction_provider="mistral").extraction_provider == "mistral"


class TestDefaultsThatEncodeADecision:
    def test_the_batch_flags_are_gone(self):
        """They were off, cheaper, and each one broke the thing people come here
        for when switched on: one made facts in a batch share an event time,
        losing `as_of`; the other skipped the deterministic supersede, losing
        the plan change the shortcut exists to catch.

        Asserted as absent rather than as False, because False is what they
        were, and the rule this codebase applies elsewhere is that a broken
        option behind a flag is one somebody eventually turns on.
        """
        from korely_agent.config import AgentSettings

        fields = AgentSettings.model_fields
        for gone in ("extraction_batch", "extraction_batch_max",
                     "contradiction_batch"):
            assert gone not in fields, f"{gone} came back"

    def test_the_nli_settings_are_gone(self):
        """`contradiction_provider="nli"` selected a pre-filter whose client was
        always None here (the cross-encoder lived in an ML service of the
        hosted product), so it and `contradiction_nli_threshold` changed
        nothing at all. A setting that does nothing is read by somebody who
        then believes it did something."""
        fields = AgentSettings.model_fields
        for gone in ("contradiction_provider", "contradiction_nli_threshold"):
            assert gone not in fields, f"{gone} came back"

        from korely_agent.services.facts import contradiction

        assert not hasattr(contradiction, "_nli_prefilter")

    def test_rate_limiting_is_off(self):
        """Self-hosted means you pay for your own compute.

        There is no metering here and no quota to enforce. The limiter exists to
        protect the operator's own database and model bill, so it is opt-in.
        """
        assert AgentSettings(database_url=DB).rate_limit_per_minute == 0


def test_surface_stays_small():
    # 31 since 2026-09-28: two settings that did nothing went (the NLI pair),
    # three numbers the worker had typed in came in (batch and webhook drain
    # limits, webhook delivery retention). 32 the same day: the vector drain
    # (services/vectors.py) is a job of its own and gets its limit the way the
    # other drains got theirs, rather than borrowing the facts drain's, whose
    # unit is a whole extraction, roughly four hundred times an embedding (core/llm.py).
    # 33 on 2026-09-29: EMBEDDING_PROVIDER, the decision to compute
    # the vectors on the machine (core/local_embedder.py). One setting and not
    # three: where the model lives is the image's to decide, and the thread
    # count follows the CPUs the container is given.
    # 34 on 2026-10-01: VOCABULARY_MODEL, agreed so a measure
    # can run the way the hosted product runs (extraction and judge on Flash
    # Lite, the relation vocabulary on Flash). Unset it is EXTRACTION_MODEL,
    # so an install that never sets it behaves exactly as before.
    # 35 the same evening: HYBRID_SEARCH, off by default, the hosted product's
    # AGENTS_HYBRID_SEARCH: the word side of the search stays in the code and
    # out of the ranking until a measure of name-and-code questions shows it
    # better than the cosine (on conversational questions it was not); removed
    # on 2026-10-12 (35 now).
    # 36 the same evening: CONTEXT_PROFILE, the profile-first /v1/context
    # block; measured, it lost, and on 2026-10-06 it went (35 again).
    # 36 on 2026-10-08: KORELY_MODE, one codebase for the install
    # and the hosted service; everything the service adds goes under
    # korely_agent/hosted/ and is read only with it on.
    # 36 on 2026-10-17: POSTGRES_PASSWORD, read by the compose file before and
    # now by the application too, which quotes it into DATABASE_URL: spliced
    # raw by the compose file, a password with `@`, `#` or `%` broke the URL
    # (audit of 17 Oct 2026). Not a new knob for the operator: the same line
    # of .env it always was.
    # 37 on 2026-11-08: READ_MARKS_RETENTION_DAYS, the window of the agent
    # notices' read marks (services/read_marks.py), 90 days by the decision of
    # that day: one more place an end user's id is kept, and every
    # such window is a line the operator can move, as the audit trail's.
    # 38 on 2026-11-15: SECRET_KEY_FALLBACKS, the SECRET_KEYs of before, read
    # and never written with, so a rotation of SECRET_KEY keeps the erasures'
    # proofs and what was sealed (services/pseudonyms.secrets; Django's
    # setting of the same name).
    # 39 on 2026-11-17: MEMORY_WINDOWS, off by default, the switch of the
    # windows of long memories (services/memory_windows.py): a change of
    # what a search and the context block answer, so the founder's to turn
    # on after a measure, never a default. Their pace is a constant
    # (memory_windows.PER_PASS), not one more setting.
    n = len(AgentSettings.model_fields)
    assert n <= 39, (
        f"{n} settings. The monolith this came from had 139 and got there one "
        f"reasonable addition at a time. If this needs to grow, decide it "
        f"deliberately and move the ceiling in the same commit."
    )


class TestTheExampleFileNamesRealSettings:
    """`.env.example` is where an operator learns what can be set, so every
    name in it has to be one something reads."""

    # Read by docker-compose.yml and Caddy, not by the application.
    # (POSTGRES_PASSWORD was here until 17 Oct 2026: the application reads it.)
    # FORWARDED_ALLOW_IPS is read from the environment by main.create_app
    # (core/proxies.py), as uvicorn read it until 1.0.22, not by Settings.
    # COMPOSE_PROJECT_NAME and COMPOSE_FILE are Compose's own, KORELY_IMAGE
    # docker-compose.image.yml's: what install.sh writes (29 Oct 2026).
    FOR_COMPOSE = {"DOMAIN", "ACME_CA", "PORT",
                   "HTTPS_PORT", "HTTP_PORT", "FORWARDED_ALLOW_IPS", "API_MEMORY_LIMIT",
                   "COMPOSE_PROJECT_NAME", "COMPOSE_FILE", "KORELY_IMAGE"}

    def _named(self):
        import pathlib
        import re

        text = (pathlib.Path(__file__).resolve().parents[1] / ".env.example").read_text()
        return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))

    def test_every_name_is_a_setting(self):
        settings = {f.upper() for f in AgentSettings.model_fields}
        unknown = self._named() - settings - self.FOR_COMPOSE
        assert unknown == set(), f".env.example names what nothing reads: {unknown}"

    def test_the_worker_limits_and_retentions_are_there(self):
        """They are settings so an operator can move them; an operator finds
        out that they exist here."""
        want = {"DRAIN_LIMIT", "BATCH_DRAIN_LIMIT", "WEBHOOK_DRAIN_LIMIT",
                "VECTOR_DRAIN_LIMIT", "AUDIT_RETENTION_DAYS", "LLM_USAGE_RETENTION_DAYS",
                "WEBHOOK_DELIVERY_RETENTION_DAYS", "READ_MARKS_RETENTION_DAYS"}
        assert want - self._named() == set()


class TestImportIsFree:
    def test_importing_the_module_needs_no_environment(self):
        """Importing configuration must not read configuration.

        This is the failure that crashed the install spike on 2026-09-10. The
        monolith builds its settings object at module scope, so `import app.db`
        transitively constructed it, and a virgin machine died during
        `alembic upgrade` with a validation error naming four variables that
        only exist to support a free tier. The application never started, so no
        log line explained what a new operator was supposed to do.

        Anything that only fails when a value is actually needed can be
        diagnosed. Anything that fails at import cannot.
        """
        out = _in_a_clean_process(
            "import korely_agent.config as c\n"
            "print('yes' if c.AgentSettings is not None else 'no')\n"
        )
        assert out == "yes", out

    def test_reading_settings_without_config_still_fails_loudly(self):
        """Lazy must not mean forgiving: the error still has to arrive.

        In a subprocess, and that is the point. What is being checked is a
        property of importing the module, which is a property of a process. The
        previous version checked it by removing korely_agent from sys.modules
        and importing it again in the running interpreter, and putting that back
        afterwards turns out to be impossible: `get_settings` is memoized per
        module object and `from ..config import settings` binds the proxy
        instance by value, so any module imported before the purge keeps asking
        the throwaway module's cache for the rest of the run. The symptom is a
        test elsewhere that sets an environment variable, reads the old value,
        and passes when run on its own. A subprocess costs a fifth of a second
        and cannot leak.
        """
        code = (
            "import korely_agent.config as c\n"
            "try:\n"
            "    c.get_settings()\n"
            "except Exception as e:\n"
            "    print(type(e).__name__)\n"
            "else:\n"
            "    print('NO ERROR')\n"
        )
        out = _in_a_clean_process(code)
        assert out == "ValidationError", out

    def test_importing_the_settings_name_is_also_free(self):
        """`from config import settings` must not read configuration either.

        A module-level __getattr__ satisfies the previous test and still fails
        this one, because importing the name calls it. Every module in the
        service imports `settings`, so getting this wrong makes the whole
        package unimportable without a database.
        """
        out = _in_a_clean_process(
            "from korely_agent.config import settings\n"
            "print(repr(settings))\n"
        )
        assert out == "<lazy AgentSettings>", out

    def test_importing_the_application_is_free(self):
        """The one that actually broke an install.

        The entrypoint runs `alembic upgrade head` before uvicorn, and alembic
        imports the models, which import config. With settings built at import
        time a virgin install died there, naming variables that exist only to
        support a hosted free tier the installer does not have.
        """
        out = _in_a_clean_process(
            "import korely_agent.db.models\n"
            "import korely_agent.services.rate_limit\n"
            "print('imported')\n"
        )
        assert out == "imported", out


class TestTheDatabaseDriverIsTheOneWeShip:
    """SQLAlchemy 2.1 changed the default driver of `postgresql://` to psycopg 3,
    which the image does not install (fresh install, 2026-09-27)."""

    def test_a_bare_url_names_psycopg2(self, monkeypatch):
        from korely_agent.config import AgentSettings

        for bare in ("postgresql://u:p@h:5432/k", "postgres://u:p@h:5432/k"):
            monkeypatch.setenv("DATABASE_URL", bare)
            assert AgentSettings().database_url == "postgresql+psycopg2://u:p@h:5432/k"

    def test_an_explicit_driver_is_left_alone(self, monkeypatch):
        from korely_agent.config import AgentSettings

        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@h:5432/k")
        assert AgentSettings().database_url == "postgresql+psycopg://u:p@h:5432/k"


class TestTheVocabularySpeaksTheProvidersLanguage:
    """The boot guard for the trap the EU bench hit twice in one day
    (2026-10-02): a Gemini model name sent by name through the
    OpenAI-compatible door dies on 422 at every write that resolves a
    relation. The service must refuse to start, saying what to set."""

    def test_a_gemini_vocabulary_on_the_compatible_door_stops_the_start(self):
        with pytest.raises(ValidationError) as e:
            AgentSettings(database_url=DB, extraction_provider="mistral",
                          mistral_api_key="k", vocabulary_model="gemini-3.6-flash")
        assert "VOCABULARY_MODEL" in str(e.value) and "mistral" in str(e.value)

    def test_the_providers_own_names_pass(self):
        s = AgentSettings(database_url=DB, extraction_provider="mistral",
                          mistral_api_key="k", vocabulary_model="mistral-small-3.2-24b-instruct-2506")
        assert s.vocabulary_model.startswith("mistral")
        assert AgentSettings(database_url=DB, extraction_provider="mistral",
                             mistral_api_key="k").vocabulary_model is None

    def test_on_gemini_everything_stays_as_before(self):
        s = AgentSettings(database_url=DB, vocabulary_model="gemini-3.6-flash")
        assert s.vocabulary_model == "gemini-3.6-flash"


def test_no_setting_is_declared_twice():
    """In a class body the last of two declarations wins without a word: the
    hosted product declared its writer model under a name it already used for
    another model, and the second silently replaced the first (found on
    2026-10-06, two duplicates found at once). Read off the source, because by
    the time the class exists the first declaration is gone."""
    import ast
    import inspect
    from collections import Counter

    from korely_agent import config

    tree = ast.parse(inspect.getsource(config))
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        names = Counter(stmt.target.id for stmt in cls.body
                        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name))
        twice = sorted(n for n, c in names.items() if c > 1)
        assert not twice, f"{cls.name} declares {', '.join(twice)} more than once"


def _access_record(method, path, status=200):
    import logging

    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                             '%s - "%s %s HTTP/%s" %d',
                             ("172.18.0.4:51234", method, path, "1.1", status), None)


def test_the_access_log_has_no_query_string():
    """The question and the end user of GET /v1/context?query=...&user_id=...
    stay out of the container's log (2026-10-06); the path and the status
    stay in."""
    from korely_agent.main import _LoggableAccessLine

    record = _access_record("GET", "/v1/context?query=where+does+maria+live&user_id=c1001")
    assert _LoggableAccessLine().filter(record)
    line = record.getMessage()
    # Outside a request no route table names the first segment: none stays.
    assert '"GET /... HTTP' in line and "maria" not in line and "c1001" not in line
    assert line.endswith("200")


# ── what a path carries (11 Oct 2026, the legal review) ─────────────────────
#
# DELETE /v1/users/{id}/memories names the end user in its path, and the
# container's log kept it, where an erasure never reaches. The log writes the
# template of the route that took the request instead.

def _access_line(app, method, path):
    """The access line as uvicorn writes it: while the response starts, in the
    request's context, from its scope (the path quoted, the query string
    after it), through the filter the product installs."""
    from urllib.parse import quote

    from fastapi.testclient import TestClient

    from korely_agent.main import _LoggableAccessLine

    lines = []

    async def server(scope, receive, send):
        async def send_and_log(message):
            if message["type"] == "http.response.start":
                query = scope["query_string"].decode()
                record = _access_record(method, quote(scope["path"]) + ("?" + query if query else ""),
                                        message["status"])
                assert _LoggableAccessLine().filter(record)
                lines.append(record.getMessage())
            await send(message)
        await app(scope, receive, send_and_log)

    TestClient(server, raise_server_exceptions=False, follow_redirects=False).request(method, path)
    assert len(lines) == 1
    return lines[0]


@pytest.fixture(scope="module")
def product():
    from korely_agent.main import create_app

    return create_app()


@pytest.mark.parametrize("method,path,kept", [
    ("DELETE", "/v1/users/maria@example.com/memories", "/v1/users/{end_user}/memories"),
    ("DELETE", "/v1/users/team%2Fmaria/memories", "/v1/users/{end_user}/memories"),
    ("DELETE", "/v1/agents/sales%2Fmaria", "/v1/agents/{agent_id}"),
    ("GET", "/v1/memories/mem_1?user_id=maria", "/v1/memories/{memory_id}"),
    # the route Starlette picks: the fixed one, which takes the method
    ("POST", "/v1/memories/search", "/v1/memories/search"),
    # the route of the path without the method, the 405
    ("PUT", "/v1/memories/mem_1", "/v1/memories/{memory_id}"),
    ("GET", "/v1/users?user_id=maria", "/v1/users"),
    # no route took it: a 404, and the redirect of a path one slash away
    ("GET", "/v1/nowhere", "/v1/..."),
    ("DELETE", "/v1/users/maria/memories/", "/v1/..."),
    ("DELETE", "/v1/users/maria", "/v1/..."),
])
def test_the_log_keeps_the_route_not_what_the_path_carries(product, method, path, kept):
    line = _access_line(product, method, path)
    assert f'"{method} {kept} HTTP/1.1"' in line, line
    assert "maria" not in line


@pytest.mark.cloud
def test_the_hosted_alert_secret_stays_out_of_the_log():
    """The providers' billing alert carries the service's secret in its path."""
    from fastapi import FastAPI

    from korely_agent.hosted import api_billing
    from korely_agent.main import _RememberTheRequest

    app = FastAPI()
    app.include_router(api_billing.public)
    app.add_middleware(_RememberTheRequest)
    line = _access_line(app, "POST", "/hosted/ops/provider-alert/scaleway/s3cret")
    assert "/hosted/ops/provider-alert/{provider}/{secret}" in line and "s3cret" not in line


def test_the_screen_keeps_its_own_files_and_nothing_of_any_other_path(tmp_path):
    """The dashboard's own files are written as the file served; quoted, so
    a file's name cannot forge a line. Any other path the catch-all takes is
    the route's name, `/{full_path}`: it may be a mis-built API path that
    carries an end user, even in its first segment (audit of 27 Oct
    2026)."""
    from fastapi import FastAPI

    from korely_agent.api import spa
    from korely_agent.main import _RememberTheRequest, loggable_path

    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "index-1.js").write_text("1")
    app = FastAPI()
    app.include_router(spa.build_router(tmp_path))
    app.add_middleware(_RememberTheRequest)
    assert '"GET /assets/index-1.js HTTP/1.1"' in _access_line(app, "GET", "/assets/index-1.js")
    assert '"GET /index.html HTTP/1.1"' in _access_line(app, "GET", "/index.html")
    assert '"GET / HTTP/1.1"' in _access_line(app, "GET", "/")
    assert '"GET /{full_path} HTTP/1.1"' in _access_line(app, "GET", "/graph?user=maria")
    line = _access_line(app, "GET", "/%0Aforged")
    assert "\n" not in line and "forged" not in line
    for method in ("GET", "DELETE"):
        for path in ("/maria@example.com/memories", "/maria@example.com",
                     "/users/maria@example.com/memories",
                     "/api/v1/users/maria@example.com/memories",
                     "/V1/users/maria@example.com/memories", "/assets/maria@example.com/x"):
            line = _access_line(app, method, path)
            assert "maria" not in line and f'"{method} /{{full_path}} HTTP/1.1"' in line, path
    # A base URL ending in a slash gives a doubled one; an HTTP client may
    # read it as a host, so it is built from the route directly.
    route = spa.build_router(tmp_path).routes[0]
    for raw, param in (("//maria%40example.com", "/maria@example.com"),
                       ("//v1/users/maria%40example.com/memories", "/v1/users/maria@example.com/memories")):
        scope = {"route": route, "path_params": {"full_path": param}}
        assert loggable_path(raw, scope) == "/{full_path}"
        # served a file of its folder: the file, never the path that reached it
        scope[spa.OWN_FILE] = "assets/index-1.js"
        assert loggable_path(raw, scope) == "/assets/index-1.js"
    # A path no route took keeps no segment no route begins with.
    assert loggable_path("/maria%40example.com/memories", {}) == "/..."
    assert loggable_path("//maria%40example.com", {}) == "/..."


def test_no_route_writes_what_the_client_put_in_the_path(product, tmp_path):
    """The guard of the access line: every route of the product (the Cloud's
    and the screen's included) with a client's string in each place its path
    has one, and paths no route took whose first segment is the client's.
    A route whose convertor writes its value (`loggable`) fails here unless
    the value is a file the dashboard's folder really has; a path no route
    took keeps only a first segment some route begins with (audit of 27
    Oct 2026: `/<end user>/memories` and
    `//<end user>` kept the end user)."""
    from types import SimpleNamespace
    from urllib.parse import quote

    from korely_agent.api import spa
    from korely_agent.api.errors import every_route
    from korely_agent.core import extensions
    from korely_agent.main import loggable_path

    (tmp_path / "index.html").write_text("<html></html>")
    # The Cloud's routes too, where the tree carries its package.
    cloud = extensions.cloud()
    routers = cloud.routers() if cloud is not None else []
    routes = list(every_route([*product.routes, *(r for router in routers for r in router.routes),
                               *spa.build_router(tmp_path).routes]))
    app = SimpleNamespace(routes=routes)
    leaks = []
    for route in routes:
        convertors = getattr(route, "param_convertors", None) or {}
        if not convertors:
            continue
        params = {name: "maria@example.com/memories" for name in convertors}
        path = quote(route.path_format.format(**params))
        shown = loggable_path(path, {"route": route, "path_params": params, "app": app})
        if "maria" in shown:
            leaks.append(f"{route.path_format} -> {shown}")
    for path in ("/maria@example.com", "/maria@example.com/memories", "//maria@example.com",
                 "//maria@example.com/memories", "/v1/users/maria@example.com/memories"):
        shown = loggable_path(quote(path), {"app": app})
        if "maria" in shown:
            leaks.append(f"no route: {path} -> {shown}")
    assert not leaks, "the access line writes a client's string:\n" + "\n".join(leaks)
    # What the route table names stays readable: its first segment.
    assert loggable_path("/v1/nowhere/maria", {"app": app}) == "/v1/..."
    assert loggable_path("/health", {"app": app}) == "/health"
    # and the address the MCP documents, which the mount serves as its /mcp
    assert loggable_path("/agent/mcp/x/maria", {"app": app}) == "/agent/..."


def test_the_error_line_names_no_end_user(caplog):
    """The line of an unexpected error writes the path too, and so does the
    access line of its 500, written after the exception left the app."""
    import logging

    from korely_agent.main import create_app

    app = create_app()

    def boom(end_user: str):
        raise RuntimeError("boom")

    app.add_api_route("/v1/boom/{end_user:path}", boom, methods=["GET"])
    with caplog.at_level(logging.ERROR, logger="korely_agent.main"):
        line = _access_line(app, "GET", "/v1/boom/maria@example.com")
    assert '"GET /v1/boom/{end_user} HTTP/1.1" 500' in line
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "korely_agent.main"]
    # The route's template, then the stack without messages (log_safe.stack).
    assert any(rec.startswith("[api] unhandled error on /v1/boom/{end_user} stack") for rec in lines)
    assert not any("maria" in rec for rec in lines)


def test_no_text_says_the_shipped_stack_makes_the_trusted_header_safe():
    """.env.example and config.py said the compose file's loopback binding
    was the condition for TRUST_AUTH_HEADER; the shipped Caddy faces the
    internet and passes the header through (audit of 17 Oct 2026)."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    for name in (".env.example", "korely_agent/config.py"):
        text = " ".join((root / name).read_text().split())
        assert "which is how docker-compose.yml already binds it" not in text, name
        assert "binds the API to loopback so that only the proxy" not in text, name
        assert "passes the header on as the client sent it" in text, name


def test_uvicorns_error_line_keeps_the_stack_and_not_the_message(caplog):
    """uvicorn writes "Exception in ASGI application" with the traceback of an
    unhandled error, and a database error's message carries the row: an end
    user's id, an alias's text (audit of 17 Oct 2026)."""
    import logging

    from korely_agent import main

    main._quiet_access_log()
    log = logging.getLogger("uvicorn.error")
    try:
        raise RuntimeError("DETAIL: Key (end_user_id)=(maria@example.com) already exists.")
    except RuntimeError:
        with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
            log.exception("Exception in ASGI application")
    assert "Exception in ASGI application" in caplog.text
    assert "maria@example.com" not in caplog.text
    assert "test_config.py" in caplog.text, "the stack went too"


# ── every duration starts with every value it started with (8 Nov 2026) ──
#
# Piece 1 of agent notices put bounds on every duration, and they stopped the
# start of installs that start today: AUDIT_RETENTION_DAYS=-1 ("for ever",
# the purge skips 0 or less), 0 for SESSION_HOURS or DRAIN_STALE_MINUTES
# (which the code reads as one hour, one minute). Decided on 8 Nov 2026: every
# value an install starts with starts, with the meaning it has; a value no
# clock holds is held where it meets one (tests/test_durations_meet_the_clock.py).

STARTS_TODAY = [("AUDIT_RETENTION_DAYS", -1), ("AUDIT_RETENTION_DAYS", 40_000),
                ("AUDIT_RETENTION_DAYS", 3_000_000), ("LLM_USAGE_RETENTION_DAYS", -1),
                ("WEBHOOK_DELIVERY_RETENTION_DAYS", -1), ("READ_MARKS_RETENTION_DAYS", -1),
                ("READ_MARKS_RETENTION_DAYS", 3_000_000), ("SESSION_HOURS", 0),
                ("LLM_TIMEOUT_SECONDS", 0), ("DRAIN_STALE_MINUTES", 0),
                ("FACTS_EXPORT_EVERY_SECONDS", 0)]


@pytest.mark.parametrize("env, value", STARTS_TODAY, ids=[f"{e}={v}" for e, v in STARTS_TODAY])
def test_every_value_an_install_starts_with_starts(env, value, monkeypatch):
    monkeypatch.setenv(env, str(value))
    assert getattr(AgentSettings(database_url=DB), env.lower()) == value


def test_the_clouds_session_hours_start_at_zero_too(monkeypatch):
    from tests import cloud

    if not cloud.HERE:
        return
    from korely_agent.hosted.settings import HostedSettings

    env = (HostedSettings.model_config.get("env_prefix") or "") + "SESSION_HOURS"
    monkeypatch.setenv(env, "0")
    assert HostedSettings().session_hours == 0
