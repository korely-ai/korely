"""What the hosted service adds to the core, as hooks (one codebase, 8 Oct 2026).

The core calls each hook at its own point. On an install of one's own nothing
is registered and every hook does nothing, so the core behaves as it always
has. The hosted package (korely_agent/hosted/, KORELY_MODE=hosted) registers
its own when the app starts: the monthly quota before a write, the plan's
rate limits, the spend cap before a model call.

A rule that keeps one account away from another's is not a hook. It belongs
to the mode and lives in the core (KORELY_MODE=hosted refuses the
install-wide settings, the password login, the trusted header), so it holds
even when the hosted package registered nothing: closed by default. A hook may
open what the mode closes for a principal the service trusts, never the other
way round.

A hook refuses by raising the HTTP error it wants the caller to see (the
model gate's hook raises `core.llm.NotNow` instead: no request is there to
answer); it answers None when it has nothing to say. One answers instead of
raising: `webhook_destination_problem` returns the reason (a string) or None,
and the webhooks' check raises its own UnsafeWebhookUrl with that reason,
which a registration answers 400 and a delivery records as its failure.

The Cloud's package is optional (the decision of 28 Oct 2026: the
public repository holds the engine and the Self-hosted product, the Cloud's
sign-in and payments stay closed). `cloud()` below is the one place outside
korely_agent/hosted/ that loads it (tests/test_module_boundaries.py): a tree
without the package runs the product as an install of one's own, and refuses
KORELY_MODE=hosted at start (config.Settings) instead of running as the
install while saying it is the Cloud.
"""
from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any, Callable, Optional
from uuid import UUID

CLOUD_PACKAGE = "korely_agent.hosted"

# Said when KORELY_MODE=hosted meets a tree without the Cloud's package.
NO_CLOUD = ("KORELY_MODE=hosted is the mode of the Korely Cloud, whose package "
            "(korely_agent/hosted/) this install does not carry: unset KORELY_MODE, "
            "or set KORELY_MODE=self_hosted, to run it as an install of your own")


def cloud(part: Optional[str] = None) -> Optional[ModuleType]:
    """The Cloud's package (korely_agent/hosted/), or its module `part`, when
    this tree carries it; None when it does not (the public repository). A
    module the package itself fails to import raises as ever: only the
    package's absence is None."""
    try:
        package = importlib.import_module(CLOUD_PACKAGE)
    except ModuleNotFoundError as e:
        if e.name != CLOUD_PACKAGE:
            raise
        return None
    if getattr(package, "__file__", None) is None:
        # A folder with no __init__.py imports as a namespace package: the
        # empty korely_agent/hosted/ (its __pycache__ alone) git leaves behind
        # when a checkout moves from the private tree to the public one. No
        # package is there.
        return None
    return importlib.import_module(f"{CLOUD_PACKAGE}.{part}") if part else package


# Every table of the Cloud's is named with this prefix (korely_agent/hosted/
# models.py; tests/test_module_boundaries.py holds it, and that no table of
# the core's has it). Its migrations are in the one chain every install
# applies, so a tree without the package has the tables and not their models.
CLOUD_TABLE_PREFIX = "hosted_"


def cloud_tables_without_models(names) -> set:
    """Of the tables `names` (a database's), the Cloud's ones when this tree
    does not carry the Cloud's package: there the migrations made them and no
    model declares them (alembic/env.py, the comparison with the models).
    Empty wherever the package is: its models declare every one."""
    if cloud() is not None:
        return set()
    return {n for n in names if n.startswith(CLOUD_TABLE_PREFIX)}


class _Hooks:
    """The defaults: each one does nothing. A registered hook is set on the
    instance and shadows the default; `reset` takes them all away."""

    def before_write(self, db, tenant_id: UUID, contents: list[str]) -> None:
        """Before memories are stored: the hosted quota refuses here."""
        return None

    def before_query(self, db, tenant_id: UUID) -> None:
        """Before a read that counts (search, context)."""
        return None

    def query_not_answered(self, tenant_id: UUID) -> None:
        """A read `before_query` counted that ended in 503
        `search_unavailable`, not answered: the hosted quota gives its query
        back (core/auth.give_back_the_read). No session: the read's may be
        broken; a hook that writes opens a short one of its own. Never raises."""
        return None

    def rate_limits(self, db, tenant_id: UUID) -> Optional[Any]:
        """The account's ceilings, or None for the install's own."""
        return None

    def before_new_agent_id(self, db, tenant_id: UUID, agent_id: str, project_id) -> None:
        """Before the first write of an agent the project has never had: an
        agent is a name in a project, as every read and erasure of one is
        (DELETE /v1/agents/{agent_id} reaches the key's project only)."""
        return None

    def before_part_write(self, db, tenant_id: UUID, part: str, **what: Any) -> Optional[Any]:
        """Before a part of the product (`Part`, named `part`) stores what
        an account gave it, with what it says of it (`what`): where a plan's
        caps on that part would refuse, and what they decide of what is
        stored (the part says which value it reads; None: nothing). Nothing
        caps it on an install of one's own."""
        return None

    def agent_cap(self, db, tenant_id: UUID) -> Optional[tuple[int, int]]:
        """(ceiling, used) on agents, counted the way the ceiling counts
        them, for `cap` and `used` in GET /v1/agents; None when nothing caps
        them. The hosted plan's is per account, one agent per project and
        name (audit of 17 Oct 2026: the reply said 0, no ceiling, while
        `before_new_agent_id` refused at the plan's)."""
        return None

    def before_model_call(self, provider: str, model: str, region: Optional[str]) -> None:
        """Before every call to a model is sent (core/llm.py, `_before_the_call`):
        the hosted spend caps and brakes refuse here, by raising
        `core.llm.NotNow`. No session: the gate is inside the provider's
        call; a hook that reads the database opens a short one of its own."""
        return None

    def writer_refused(self, region: Optional[str], status: Optional[int], detail: str) -> None:
        """A region's writer refused in a way that waits for a person (HTTP
        401, 402 or 403, services/facts/drain.py `waits_for_a_person`), once
        per rest of the drain (`_provider_refused`): the hosted service tells
        its operator. Until 10 Nov 2026 a 402 alone. `detail` is the failure's category
        (`core.llm.failure_kind`), never the provider's words, and a hook
        still never writes it into a log line or an email (audit of 17 Oct
        2026). No session; never raises."""
        return None

    def purge(self, db) -> int:
        """Once an hour, with the worker's own purge (worker._purge): what the
        hosted service keeps for a while and then deletes (hosted/cli.cleanup).
        Returns how many rows went."""
        return 0

    def can_change_install_settings(self, db, principal) -> Optional[bool]:
        """Whether this principal may change the install-wide provider and
        models. None leaves it to the mode: the operator of an install of
        one's own may, an account of the hosted service may not."""
        return None

    def before_writer_saved(self, db, provider: str) -> None:
        """Before PUT /admin/models/{provider} checks and stores a writer:
        the hosted service refuses a provider its published list of
        sub-processors does not name."""
        return None

    def before_new_key(self, db, tenant_id: UUID) -> None:
        """Before a door of the account makes it a key (POST /admin/keys, an
        app's OAuth consent, api/oauth.approve): the hosted service refuses
        one to an account it does not let make keys yet. Not the operator's
        own command line (manage.py), which no account reaches."""
        return None

    def longest_key_lifetime(self) -> Optional[int]:
        """The most days a key made from now on may last, or None: the
        install's own ceiling (services/keys.MAX_LIFETIME_DAYS), "never"
        included. Read where every key is made (services/keys.create_api_key),
        and by GET /admin/keys/lifetimes, which the screen offers from. A key
        already made keeps its date."""
        return None

    def before_key_revoked(self, db, tenant_id: UUID, key_id: UUID) -> None:
        """Before a door of the account revokes one of its keys (POST
        /admin/keys/{id}/revoke), in the request's session, which commits
        the revocation: the hosted service refuses the last live key of an
        account it does not let make keys yet, which would leave nothing
        that reaches the account. Not the operator's own command line
        (manage.py), nor what revokes keys of apps connected over OAuth,
        which such an account cannot hold (`before_new_key`)."""
        return None

    def screen_sources(self) -> dict:
        """What the hosted service's own screens (its sign-in) load from
        outside the screen's origin, by Content-Security-Policy directive:
        {directive: (source, ...)}, added to the screen's policy
        (api/spa.screen_headers). {} here: the screen of an install of one's
        own loads nothing from elsewhere."""
        return {}

    def webhook_destination_problem(self, host: str, addresses: list) -> Optional[str]:
        """Why a webhook may not go to `host` at `addresses` (what it resolved
        to), or None: checked with the public-address rule, at registration
        and at every delivery (services/webhooks.checked_destination), and
        whatever the install's switches for local tests say. The hosted
        service refuses its own addresses."""
        return None


hooks = _Hooks()


def register(name: str, fn: Callable) -> None:
    """Put `fn` in place of the default hook `name`. A name the core does not
    call is refused, so a typo cannot register a hook that never runs."""
    if name.startswith("_") or not callable(getattr(_Hooks, name, None)):
        raise ValueError(f"no hook called {name!r}")
    setattr(hooks, name, fn)


def reset() -> None:
    """Every hook back to its default (tests, and an app built twice)."""
    hooks.__dict__.clear()


# ── the parts of the product the Cloud's package carries (5 Nov 2026) ────────
#
# The founder's decision of 4 Nov 2026: some of the product is the Cloud's
# alone (a paid feature) and never reaches the public repository. Such a part
# lives in the Cloud's package, and the core reaches it only through the
# methods of `Part` below, each of which does nothing by default, so the core
# of a tree without the package is the product as it was before the part
# existed.
#
# Two kinds of seam (decided on 5 Nov 2026: a paid part is served by the
# Cloud alone, never by an install of one's own built from the private
# tree):
#
# - what the package carries wherever it is (`parts()`): its tables and their
#   chain of migrations, the places an erasure reaches, the stores of an agent
#   namespace, a project's rows, the kinds of vectors, the erasure records'
#   replays, the trail's categories, the fields it adds to the erasure's and
#   the trail's answers. Rows that exist are erased and moved as every other,
#   in either mode;
# - what it serves (`mounted()`, only with KORELY_MODE=hosted): its routes,
#   its start, its sections of /health, its jobs of the worker, its upload
#   doors, its search;
# - and, of that, what it serves to one account (`served(tenant_id)`, the
#   founder's decision of 13 Nov 2026: a part may be tried on the Cloud by
#   the accounts it names before any customer): its routes answer that
#   account (any other meets them as routes that do not exist,
#   main._mount_routes), its search, its section of the context block and
#   its upload doors' larger ceiling are that account's, and what it holds
#   counts in that account's limits. Rows of an account it does not serve
#   are kept, erased and moved as every other.


class Part:
    """A part of the product the Cloud's package carries: what it adds to
    each place of the core that offers a seam. The defaults add nothing.
    Each method says whether the core asks it of every part the package
    carries (`parts`) or of the parts the mode serves (`mounted`)."""

    #: Its name, as the logs and `before_part_write` say it.
    name = ""

    def routers(self) -> tuple:
        """Mounted. Its FastAPI routers, mounted with the core's; a route at
        the path and method of a core's route takes that route's place for
        an account it serves (main._mount_routes)."""
        return ()

    def accounts(self) -> Optional[frozenset]:
        """Mounted. The accounts this part is served to, as ids (UUID), or
        None, the default: every account the mode serves. A part served to
        a list is one nobody else may tell exists: its routes match only a
        caller it serves (main._mount_routes), and every schema one per
        server (the API's document, the MCP's tools for a caller it does not
        serve) is the core's without it."""
        return None

    def serves(self, tenant_id) -> bool:
        """Whether this part is served to the account `tenant_id`
        (`served`), by `accounts`."""
        listed = self.accounts()
        if listed is None:
            return True
        try:
            return (tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))) in listed
        except ValueError:
            return False

    def at_start(self, settings) -> None:
        """Mounted. Once, as a process that serves or works starts
        (main.create_app, worker.main), before anything else runs there."""
        return None

    def health(self, settings) -> dict:
        """Mounted. Its sections of /health, by name: each a dict with a
        `state`; `failing` makes the whole `degraded`."""
        return {}

    def jobs(self) -> tuple:
        """Mounted. Its jobs of the worker (worker.run_forever): (name,
        job(db) -> dict, seconds between passes, the keys of the job's answer
        worth a log line)."""
        return ()

    def upload_doors(self) -> tuple:
        """Mounted. Its routes a file is uploaded to
        (services/limits.UploadDoor)."""
        return ()

    def screen_sources(self) -> dict:
        """Mounted. What its pages of the screen load from outside the
        screen's origin, by Content-Security-Policy directive: {directive:
        (source, ...)}, added to the screen's policy only where the part is
        mounted (api/spa.screen_headers)."""
        return {}

    def search(self):
        """Mounted. The one search with this part's results in it (a
        subclass of services/memory_search.MemorySearch), or None."""
        return None

    def places(self) -> tuple:
        """Carried. Its places that hold an end user's rows
        (services/end_user_places.Place), erased with the person."""
        return ()

    def stores(self) -> tuple:
        """Carried. (name, model) of its tables that hold content behind a
        memory's walls (tenant_id, project_id, end_user_id, agent_id): an
        agent namespace is found in them (services/usage.has_agent_id) and a
        deleted project's rows move or go with them (services/projects)."""
        return ()

    def project_deleting(self, db, tenant_id, project) -> None:
        """Carried. Before a project is deleted or its rows moved: its rows
        locked in the order its own writes take them (services/projects)."""
        return None

    def account_closing(self, db, tenant_id) -> None:
        """Carried. Before an account is erased with its rows (the close of
        an account on the Cloud), in the erasure's transaction: what of its
        rows must outlive the cascade, written where no key to the account
        reaches it, for a step that runs once the erasure has committed."""
        return None

    def project_folding(self, db, tenant_id, from_id, to_id) -> dict:
        """Carried. Before a deleted project's rows move into the default
        project: those that would collide with the default project's folded,
        as counts (services/projects)."""
        return {}

    def vector_kinds(self) -> tuple:
        """Carried. (name, services/vectors.Kind) of its rows with a vector,
        recomputed and compared with the memories' and the facts' when the
        embedding model changes. Its own drain is its job's."""
        return ()

    def actions(self) -> dict:
        """Carried. {endpoint: category} of the trail for its own endpoints
        (services/usage._ACTION_FOR); an endpoint named nowhere is a read."""
        return {}

    def replays(self) -> dict:
        """Carried. {scope: replay(db, tenant_id, record) -> audit id or
        None} for the erasure records of its scopes
        (services/erasure_replay)."""
        return {}

    def tables(self) -> frozenset:
        """Carried. The tables its own chain of migrations makes
        (`migrations`): the core's chain and comparison leave them out
        (alembic/env.py)."""
        return frozenset()

    def migrations(self) -> Optional[str]:
        """Carried. The alembic.ini of its own chain of migrations, with its
        own version table, applied after the core's (db/chains.py)."""
        return None

    def extend(self, name: str, model):
        """Carried. The answer model `name` of the core (`extend` below),
        with the fields this part adds, or `model` as it is."""
        return model


_parts: Optional[tuple] = None


def parts() -> tuple:
    """The parts of the product the Cloud's package carries, in its order;
    () on a tree without the package (the public repository). Read once."""
    global _parts
    if _parts is None:
        package = cloud()
        _parts = tuple(package.parts()) if package is not None else ()
    return _parts


def mounted() -> tuple:
    """The parts the mode serves: every part the package carries with
    KORELY_MODE=hosted, none otherwise (an install of one's own, whichever
    tree it was built from). Read at each call: a test, or a process, may
    build an app of each mode."""
    from ..config import get_settings

    return parts() if get_settings().hosted else ()


def served(tenant_id) -> tuple:
    """The parts the mode serves to the account `tenant_id` (`Part.serves`):
    the one door every place that answers an account asks (its routes, its
    search and context block, its upload doors, its limits). None for no
    account. Read at each call, as `mounted`."""
    if tenant_id is None:
        return ()
    return tuple(part for part in mounted() if part.serves(tenant_id))


#: Where main._ThePartsCallers leaves, in a request's scope, the names of the
#: parts served to a list of accounts that serve its caller.
SERVED = "korely.parts_served"
#: Where `PartRoutes.matches` leaves the route of the part that matched.
_ITS_ROUTE = "korely.parts_route"


class PartRoutes:
    """The routes of a part served to a list of accounts
    (core/extensions.Part.accounts), as one entry of the app's routes:
    they match a request only when the part serves its caller (`SERVED`,
    which main._ThePartsCallers writes before the routing). For any other
    caller, signed in or not, they do not exist: the router answers what a
    path no route has answers, a 404 with its body, with no 405 and `Allow`
    for a method they lack, no redirect for a slash, no body read, no key's
    last use and no rate limit counted, since nothing of them runs. Not an
    APIRoute, so the API's document leaves them out, as it leaves a route
    out that does not exist, and shows the core's routes they take the
    place of (main._mount_routes). The repository's walk of the
    routes (api/errors.every_route) goes into them, as into a router."""

    def __init__(self, part, routes: list) -> None:
        self.part = part
        self.routes = routes

    def owns(self, scope) -> bool:
        """Whether one of the routes is at the request's path, or at the
        path one slash away the router would redirect to, whatever the
        method and the caller."""
        from starlette.routing import Match

        path = scope.get("path") or ""
        twin = path.rstrip("/") if path.endswith("/") else path + "/"
        return any(route.matches({**scope, "path": p})[0] is not Match.NONE
                   for p in (path, twin) for route in self.routes)

    def matches(self, scope):
        from starlette.routing import Match

        if self.part.name not in scope.get(SERVED, ()):
            return Match.NONE, {}
        partial = None
        for route in self.routes:
            match, child = route.matches(scope)
            if match is Match.FULL:
                return match, {**child, _ITS_ROUTE: route}
            if match is Match.PARTIAL and partial is None:
                partial = {**child, _ITS_ROUTE: route}
        return (Match.PARTIAL, partial) if partial is not None else (Match.NONE, {})

    async def handle(self, scope, receive, send) -> None:
        await scope[_ITS_ROUTE].handle(scope, receive, send)

    def url_path_for(self, name: str, /, **params):
        from starlette.routing import NoMatchFound

        for route in self.routes:
            try:
                return route.url_path_for(name, **params)
            except NoMatchFound:
                pass
        raise NoMatchFound(name, params)

    async def __call__(self, scope, receive, send) -> None:
        await self.handle(scope, receive, send)


def part_tables() -> frozenset:
    """The tables the parts' own chains of migrations make
    (`Part.tables`): the core's chain and comparison leave them out."""
    return frozenset(t for part in parts() for t in part.tables())


def extend(name: str, model):
    """`model` with the fields each part the package carries adds to it
    (`Part.extend`): an answer of the core that a part's rows show in too.
    The core's own on a tree without the package."""
    for part in parts():
        model = part.extend(name, model)
    return model
