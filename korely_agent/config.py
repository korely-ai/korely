"""Configuration.

The monolith this was extracted from carries 139 settings, because one process
served a note-taking app, a meeting recorder, a chat product and this. A client
installing the memory service should not have to reason about Deepgram service
names or free-tier inactivity windows to get it running.

So this is the whole surface, and the only value with no default is the database
URL. Everything else boots.

Precedence is the usual one: explicit argument, then environment, then default.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional, get_args

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The address of the API on the machine that runs it: the port the image's
# uvicorn listens on (Dockerfile, CMD), which docker-compose.yml and the
# shipped Caddyfile reach. Written once here for the code (audit of 17 Oct
# 2026); tests/test_the_image_and_the_code_agree.py holds the image to it.
LOCAL_BASE_URL = "http://localhost:8000"


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ── the one thing you must provide ──────────────────────────────────────
    database_url: str = Field(
        ..., description="postgresql://user:pass@host:5432/dbname"
    )

    @field_validator("database_url")
    @classmethod
    def _the_driver_we_ship(cls, v: str) -> str:
        """`postgresql://` names no driver, so SQLAlchemy picks its default.
        That default changed in SQLAlchemy 2.1 from psycopg2 to psycopg 3,
        which this image does not install: a fresh install on 2026-09-27
        pulled 2.1.1 and the API never started ("No module named
        'psycopg'"). The URL now names the driver in requirements.txt. An
        explicit driver in the URL is left alone."""
        for bare in ("postgresql://", "postgres://"):
            if v.startswith(bare):
                return "postgresql+psycopg2://" + v[len(bare):]
        return v

    # The bundled Postgres's password, apart from the URL. docker-compose.yml
    # used to splice it raw into DATABASE_URL, so a password with `@`, `#` or
    # `%` in it, which README's "anything long and random" allows, made
    # another URL: `p@ss#word` moved the host, and a `%` made alembic's
    # set_main_option raise a ValueError that printed the whole URL, password
    # included (audit of 17 Oct 2026). The compose file now passes a URL with
    # no password and this; the password goes in quoted below. A URL that
    # carries its own password is left alone.
    postgres_password: Optional[str] = Field(default=None, repr=False)

    @model_validator(mode="after")
    def _the_password_given_apart(self):
        pw = self.postgres_password
        if not pw:
            return self
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        try:
            url = make_url(self.database_url)
        except ArgumentError:
            # Its message repeats the string, and the string may hold a password.
            raise ValueError("DATABASE_URL is not a URL SQLAlchemy can read") from None
        if url.password is None:
            # SQLAlchemy quotes it the way its own parser reads it back, which
            # quote_plus does not: a space would come back as `+`.
            self.database_url = url.set(password=pw).render_as_string(hide_password=False)
        return self

    # ── the model provider ──────────────────────────────────────────────────
    # Fact extraction (the facts, the contradiction judge, the relation
    # vocabulary) is the only place this service calls a generative model, and
    # it calls it on writes, never on reads. A read embeds at most its
    # question, once (a search, a context: on this machine with
    # EMBEDDING_PROVIDER=local, a Gemini call with gemini), and the rest of it
    # is SQL (audit of 17 Oct 2026). That asymmetry is why reads cost about
    # nothing and why a client's bill tracks how much their agents learn, not
    # how often they remember.
    google_api_key: Optional[str] = None
    mistral_api_key: Optional[str] = None

    # The file's writer: Gemini on its own client, or any OpenAI-compatible
    # provider at MISTRAL_API_BASE, named mistral since it was Mistral's
    # (core/llm.get_provider has a branch for each value).
    extraction_provider: Literal["gemini", "mistral"] = "gemini"
    # gemini-2.5-flash was here until 2026-09-12, when a brand-new Google
    # account could not use it: the API lists it and then answers 404, "no
    # longer available to new users, use models/gemini-3.6-flash". An install
    # is bought by somebody whose account is new by definition, so the default
    # has to be one of those. Verified against a real key: 3.6-flash, 3.5-flash
    # and 3.1-flash-lite all answer; 2.5-flash does not.
    #
    # 3.1-flash-lite since 0.1.19 (2026-10-06): measured as the whole writer
    # (extraction, judge and vocabulary) on LoCoMo conversation 26, 129 of 152
    # like gpt-oss-120b, with an observation for every memory; about $0.002 a
    # memory against $0.0056 for 3.6-flash, whose price Google doubles on
    # 2027-01-01 (its pricing page). The writer the first login recommends
    # for the Global region (services/model_choice.py).
    extraction_model: str = "gemini-3.1-flash-lite"
    # The model that decides the relation vocabulary: is this phrase a relation
    # already known, which family is it, does it hold one value at a time
    # (services/predicates.py). Unset, it is `extraction_model`, as it always
    # was. The hosted product runs extraction and the contradiction judge on
    # Flash Lite and this on Flash, which is how its majority-of-three answer
    # was measured (2026-10-01). A model of the same provider.
    vocabulary_model: Optional[str] = None
    mistral_extraction_model: str = "mistral-small-latest"
    mistral_api_base: str = "https://api.mistral.ai/v1"

    embedding_model: str = "gemini-embedding-2-preview"

    # Who turns text into vectors. `local`, the default since 0.1.19, computes
    # them on this machine with EmbeddingGemma-300m (core/local_embedder.py):
    # no key, and no text leaving the machine for a vector, so an install that
    # writes in Europe searches in Europe too; it is also what every published
    # number was measured with. `gemini` asks Google with the key above
    # (`embedding_model`), for a machine that should not carry the model. The
    # image carries it when built with this set to local, which
    # docker-compose.yml passes to the build from .env, and the image says in
    # its own environment which one it was built for (Dockerfile). The
    # engine's distance thresholds follow the model (memory_core.thresholds).
    # EMBEDDING_MODEL is Gemini's model; a model whose thresholds nobody has
    # measured stops the service at startup (core/llm.py), and so do stored
    # vectors of another model (services/vectors.py, the start's check).
    embedding_provider: str = "local"             # local | gemini

    @model_validator(mode="after")
    def _the_vocabulary_speaks_the_providers_language(self):
        """A model NAME belongs to a provider. With EXTRACTION_PROVIDER=mistral
        (the OpenAI-compatible door: Scaleway, OVH, OpenRouter, OpenAI...),
        a VOCABULARY_MODEL carrying a Gemini name is sent BY NAME to a host
        that does not serve it, and every write that resolves a relation dies
        on 422 — found twice in one day on the EU bench (2026-10-02). Caught
        here, at start, with the message saying what to set; silently
        'working' until the first write is the failure mode this repo's boot
        checks exist to prevent."""
        vocab = (self.vocabulary_model or "").strip()
        if self.extraction_provider == "mistral" and vocab.startswith("gemini"):
            raise ValueError(
                f"VOCABULARY_MODEL={vocab!r} is a Gemini model name, but "
                f"EXTRACTION_PROVIDER=mistral sends the vocabulary to "
                f"MISTRAL_API_BASE, which will refuse it on every write. "
                f"Unset VOCABULARY_MODEL (the vocabulary then follows "
                f"MISTRAL_EXTRACTION_MODEL) or name a model that host serves.")
        return self

    @field_validator("korely_mode")
    @classmethod
    def _a_mode_we_have(cls, v: str) -> str:
        """A misspelt mode must not quietly mean the other one: `hosted`
        switches on accounts, billing and the service's limits."""
        v = (v or "").strip().lower().replace("-", "_")
        if v not in ("self_hosted", "hosted"):
            raise ValueError(f"KORELY_MODE is self_hosted or hosted, not {v!r}")
        return v

    @model_validator(mode="after")
    def _the_cloud_mode_needs_the_cloud(self):
        """KORELY_MODE=hosted on a tree without the Cloud's package (the
        public repository) is refused at start, by every process that reads
        the settings: it would run as an install of one's own while the
        operator believes it is the Cloud (core/extensions.cloud)."""
        if self.hosted:
            from .core import extensions

            if extensions.cloud() is None:
                raise ValueError(extensions.NO_CLOUD)
        return self

    @model_validator(mode="after")
    def _no_trusted_header_on_the_service(self):
        """TRUST_AUTH_HEADER makes whoever sets that header an operator. On an
        install of one's own the proxy in front of it is the operator's; the
        hosted service signs people in itself, and there one header would be
        enough to become any account. Refused at start, not ignored."""
        if self.hosted and (self.trust_auth_header or "").strip():
            raise ValueError(
                "TRUST_AUTH_HEADER cannot be set with KORELY_MODE=hosted: the "
                "service signs people in itself, and the header would let "
                "anybody who sets it act as any account.")
        # The Markdown export writes every account's facts to files. Closing
        # an account on the service deletes its rows, not files: an export
        # there would keep a closed account's facts on disk (a review of the
        # deploy, 8 Oct 2026). Refused, so the safety does not
        # depend on nobody setting the variable.
        if self.hosted and (self.facts_export_dir or "").strip():
            raise ValueError(
                "FACTS_EXPORT_DIR cannot be set with KORELY_MODE=hosted: closing "
                "an account deletes its rows, not the files the export wrote.")
        # The two escape hatches of the webhook checks are for a local test of
        # an install of one's own. On the service any account registers a
        # webhook, and either one would let it point the service at its own
        # network (the metadata endpoint, an admin port) or send events in
        # clear (12 Oct 2026, the inventory of the switches before 1.0).
        if self.hosted and (self.webhook_allow_private_urls or self.webhook_allow_http):
            raise ValueError(
                "WEBHOOK_ALLOW_PRIVATE_URLS and WEBHOOK_ALLOW_HTTP cannot be set with "
                "KORELY_MODE=hosted: any account registers webhooks there, and they would "
                "reach the service's own network or travel in clear.")
        # A proxy that carries a webhook resolves its name itself, so the
        # address the check approved is no longer the one reached
        # (services/webhooks.py, `_proxy_for`): on the service, where any
        # account registers webhooks, the proxy's policy would be the only
        # thing between an account and the service's own network (audit of
        # 17 Oct 2026). Only the variables that can carry a webhook: NO_PROXY,
        # or another scheme's proxy, carries none.
        if self.hosted:
            import urllib.request

            carrying = sorted({"http", "https", "all"} & set(urllib.request.getproxies()))
            if carrying:
                raise ValueError(
                    f"{', '.join(n.upper() + '_PROXY' for n in carrying)} cannot be set with "
                    "KORELY_MODE=hosted: a webhook would go out through the proxy, which "
                    "resolves the name itself, so the address the check approved would not "
                    "be the one reached.")
        return self

    @property
    def hosted(self) -> bool:
        """This process is the hosted service (KORELY_MODE=hosted)."""
        return self.korely_mode == "hosted"

    @field_validator("extraction_provider", mode="before")
    @classmethod
    def _the_writer_it_means(cls, v):
        """The writer a line means, as `get_provider` has always read it:
        exactly "mistral" is the OpenAI-compatible door, any other text
        (`Mistral`, `openai`, an empty `EXTRACTION_PROVIDER=`) is Gemini.
        Until 4 Nov 2026 such a line was kept as written, so `GET /health`
        and `GET /admin/provider` (`provider`) and `GET /admin/models`
        (`env_provider`) answered "openai" of an install writing with
        Gemini, and `uses.writes` said the Google key wrote nothing. A value
        that is not text is left to the `Literal`, which refuses it."""
        if not isinstance(v, str):
            return v
        # Two values: a third one added to the `Literal` stops every start
        # here, so whoever adds it also decides what this reading becomes.
        gemini, mistral = get_args(cls.model_fields["extraction_provider"].annotation)
        return mistral if v == mistral else gemini

    @field_validator("embedding_provider")
    @classmethod
    def _a_provider_we_have(cls, v: str) -> str:
        """`locale` or `Local ` must not quietly mean Gemini: the install would
        go on sending every text to Google while its operator believes it
        does not."""
        v = (v or "").strip().lower()
        if v not in ("gemini", "local"):
            raise ValueError(f"EMBEDDING_PROVIDER is gemini or local, not {v!r}")
        return v

    # A model call holds whatever is waiting on it: the worker's drain, which
    # mines the memories (WRITE_ASYNC, the default), or the request itself
    # with WRITE_ASYNC=false and on `POST /v1/facts`; embedding runs on writes
    # and on searches. Without a deadline a stalled provider does not fail, it
    # holds the connection and the open transaction behind it until something
    # else gives up first. Every model call reads it: both providers and
    # Gemini's vectors (the Gemini client had no deadline at all, and the
    # Mistral call kept a literal 60 that ignored this setting), and the
    # provider page's key test, which goes through the same client.
    # The other calls to a third party are other waits and keep a named
    # deadline next to the call (audit of 17 Oct 2026): a provider listing
    # its models to the first login (services/model_choice.py), a webhook
    # delivery (services/webhooks.py), and on the Cloud its payments, sign-in
    # and alert emails (korely_agent/hosted/).
    llm_timeout_seconds: int = 60

    # ── webhooks ────────────────────────────────────────────────────────────
    # A webhook URL is an address this server will connect to, chosen by
    # whoever holds an API key. On a machine inside your network that is a
    # request forgery primitive: the cloud metadata endpoint, an admin panel on
    # localhost, a database port. Registration refuses anything that resolves
    # to a loopback, private, link-local or reserved address, and refuses
    # plain http, unless you turn that off here for a local test.
    webhook_allow_private_urls: bool = False
    webhook_allow_http: bool = False

    # ── readable export ─────────────────────────────────────────────────────
    # Set a directory and the worker also writes the facts there as Markdown,
    # one file per end user. The database stays the source of truth and nothing
    # reads these back: deleting them loses nothing.
    #
    # It is there so you can look. Put the directory under git and `git diff`
    # shows what the agent learned this week, in a form somebody can read
    # without an API client. A hosted competitor cannot offer that.
    facts_export_dir: Optional[str] = None
    facts_export_every_seconds: int = 300

    # ── write path ──────────────────────────────────────────────────────────
    # async: /v1/memories returns as soon as the memory is durable and the facts
    # are mined by a worker. Keeps p99 writes off the LLM's latency, at the cost
    # of facts arriving a few seconds later. On by default because an agent in a
    # conversation cannot wait eight seconds.
    write_async: bool = True
    drain_limit: int = 20
    drain_stale_minutes: int = 15

    # How much each background job takes per tick. `drain_limit` above is the
    # facts drain (memories, every 10 s); these are the other two, which used to
    # be numbers typed into worker.py where nobody running this could reach
    # them. A batch job is a whole /v1/batch payload imported with extraction,
    # so a few per tick (every 30 s) is already a lot of model calls. A webhook
    # delivery can wait fifteen seconds and more on an endpoint that does not
    # answer, so the number per tick (every 15 s) bounds how long one tick runs.
    batch_drain_limit: int = 5
    webhook_drain_limit: int = 20
    # The vector drain (every 30 s, services/vectors.py): how many memories,
    # and as many facts, whose vector a write could not get are retried per
    # pass, shared among the accounts that have a vector due (each in turn).
    # One embedding call each, the cheap end of the bill (a fact whose vector
    # failed on its write is also judged once, a model call); a name declared
    # or removed re-embeds the facts that name it and asks no judge. The
    # number also bounds how fast a backlog clears the day a key is first set
    # on an install that ran without one.
    vector_drain_limit: int = 50

    # There were two flags here, `extraction_batch` and `contradiction_batch`,
    # both off, both cheaper, and both breaking the thing people come here for
    # when switched on: one made facts in a batch share an event time, which
    # loses `as_of`, and the other skipped the deterministic supersede, which
    # lost the plan change that shortcut existed to catch (the shortcut itself
    # went on 27 Sep 2026: the judge decides, memory_core).
    #
    # They are gone rather than documented, on the rule this codebase already
    # applies to the parallel drain: shipping a broken option behind a flag
    # means someone eventually turns it on. Anyone who wants them back has git.

    # There were two more here, `contradiction_provider` ("gemini" or "nli") and
    # `contradiction_nli_threshold`. "nli" meant a cross-encoder in a separate
    # ML service of the hosted product, asked before the judge; that service is
    # not part of this one, so the setting selected a branch that passed every
    # candidate through unchanged. Gone, on the same rule as the batch flags.

    # Run the background worker inside the API process. Keeps a working install
    # at two containers. Set false and run `python -m korely_agent.worker`
    # separately when the API is scaled past one replica.
    run_worker: bool = True

    # How long audit events are kept. Too short and an investigation has nothing
    # to look at; too long and the trail is itself a data-protection question.
    # 400 days is a year plus room for an annual review to run late. 0 keeps
    # them forever, which is a decision, not a default.
    audit_retention_days: int = 400
    # How long the per-call model usage rows are kept. This is the record that
    # answers "which agent spent what last quarter", so a year is the floor for
    # anything anyone charges back internally. 0 keeps them forever.
    llm_usage_retention_days: int = 400
    # How long a finished webhook delivery is kept: delivered, or failed with
    # its attempts used up. It answers "did my endpoint get last week's events";
    # a delivery still waiting for a retry is never removed on age. 0 keeps them
    # forever.
    webhook_delivery_retention_days: int = 30
    # The rate card, as JSON. Optional: without it the consumption report still
    # answers with the counts and leaves the money blank, which is the honest
    # shape when nobody has said what a token costs here.
    #
    #   LLM_RATES='{"currency":"USD","models":{
    #     "gemini-3.1-flash-lite":{"input_per_million":0.25,"output_per_million":1.50}}}'
    #
    # The example prices `extraction_model`, the writer an install has by
    # default, at Google's list price as of October 2026. It priced
    # gemini-2.5-flash, which no install calls (audit of 17 Oct 2026); the
    # same card is in .env.example and on the Usage page, and a test keeps
    # the three alike.
    #
    # Kept out of the database on purpose. A price is an assumption that changes;
    # writing it next to the tokens would make last quarter's report move when
    # the provider moves.
    llm_rates: Optional[str] = None

    # ── who may administer ──────────────────────────────────────────────────
    # How long a login lasts.
    session_hours: int = 12
    # The header your gateway sets once it has authenticated somebody, e.g.
    # `X-Forwarded-User`. Empty means off, which is the default.
    #
    # THE GATEWAY MUST STRIP THIS HEADER FROM INCOMING REQUESTS. If it does
    # not, anybody who can reach this service can set it and become any
    # operator. Loopback is not enough: the shipped Caddy faces the internet
    # and passes the header on as the client sent it (audit of 17 Oct 2026).
    # Turn it on only behind a proxy that removes it from every request and
    # sets it itself after authenticating.
    trust_auth_header: Optional[str] = None

    # Where the built dashboard lives. The image puts it at /app/static
    # (Dockerfile: WORKDIR /app, then ./static); point this somewhere else to
    # serve a page you built yourself, or at a directory that does not exist
    # to run with no screen at all.
    dashboard_dir: str = "/app/static"

    # The address clients reach this service at. Used in the MCP server's OAuth
    # metadata, which has to advertise a URL that actually resolves for the
    # caller; localhost is right only while you are the caller.
    public_base_url: str = LOCAL_BASE_URL

    # ── operations ──────────────────────────────────────────────────────────
    # Removed on 2026-09-11 because nothing read it, and back on 2026-09-12
    # because something does: the provider key set from the screen is
    # encrypted with it before it touches the database. No default. Without
    # one, saving a key refuses instead of writing it in clear text, and the
    # screen says why.
    secret_key: Optional[str] = None
    sql_echo: bool = False
    # Where this install says it is, reported by /v1/ping as `region`. A label
    # the operator sets, not something the service can check: it runs
    # wherever you put it, and /v1/ping publishes it as a label, never as
    # where the data is (audit of 19 Oct 2026).
    region: str = "eu"
    # One codebase for the install you run and the hosted service (the
    # decision of 8 Oct 2026). `self_hosted`,
    # the default, is this product as it ships; `hosted` is how Korely runs
    # the same code as its own service, and switches on what only a service
    # needs (korely_agent/hosted/). Nothing changes for an install that does
    # not set it.
    korely_mode: str = "self_hosted"

    # Rate limiting protects YOUR database and YOUR model bill; there is no
    # metering or quota here, because you are paying for your own compute.
    # Zero disables it.
    rate_limit_per_minute: int = 0


@lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    """Read configuration once, on first use.

    Deliberately lazy. The codebase this was extracted from built its settings
    object at import time (`settings = Settings()` at module scope), which meant
    that importing anything at all required a full environment: a virgin install
    crashed during `alembic upgrade` with a pydantic validation error before a
    single line of application code ran, and the error named four variables that
    exist only to support a free tier the installer does not have.

    Importing this module must stay free. Reading configuration is what costs.
    """
    return AgentSettings()  # type: ignore[call-arg]


class _LazySettings:
    """A stand-in that resolves on attribute access, not on import.

    A module-level `__getattr__` looks lazy and is not: `from config import
    settings` triggers it immediately, so every module that imports the name
    would need a reachable configuration just to be imported. That is the
    failure the laziness was supposed to prevent, reintroduced one layer up.

    Attribute access is the right moment: code that reads a setting has already
    decided to do work.
    """

    def __getattr__(self, name: str):
        return getattr(get_settings(), name)

    def __repr__(self) -> str:  # pragma: no cover
        return "<lazy AgentSettings>"


settings = _LazySettings()


def api_url() -> str:
    """Where this install's API and MCP are reached, without the trailing
    slash: PUBLIC_BASE_URL. The OAuth issuer and the MCP's metadata name it
    (api/oauth.py, mcp/server.py), and on an install of one's own the
    dashboard is served there too."""
    return (get_settings().public_base_url or "http://localhost:8000").rstrip("/")


def dashboard_url() -> str:
    """Where a person reaches the dashboard, without the trailing slash: on
    the hosted service its own domain (HOSTED_DASHBOARD_URL,
    https://agent.korely.ai), on an install of one's own the address of the
    API, which serves the screen too (PUBLIC_BASE_URL). Every link to a page
    of the dashboard is built from this (audit of 17 Oct 2026): the 401 of
    an unknown key sent a Cloud customer to /keys on the API's host, where
    no dashboard is."""
    s = get_settings()
    if s.hosted:
        from .core import extensions

        return extensions.cloud("settings").hosted_settings().dashboard_url.rstrip("/")
    return api_url()
