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
answer); it answers None when it has nothing to say.

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

    def before_new_agent_id(self, db, tenant_id: UUID, agent_id: str) -> None:
        """Before the first write of an agent the account has never had."""
        return None

    def agent_cap(self, db, tenant_id: UUID) -> Optional[tuple[int, int]]:
        """(ceiling, used) on agent names, counted the way the ceiling counts
        them, for `cap` and `used` in GET /v1/agents; None when nothing caps
        them. The hosted plan's is per account (audit of 17 Oct 2026: the
        reply said 0, no ceiling, while `before_new_agent_id` refused at the
        plan's)."""
        return None

    def before_model_call(self, provider: str, model: str, region: Optional[str]) -> None:
        """Before every call to a model is sent (core/llm.py, `_before_the_call`):
        the hosted spend caps and brakes refuse here, by raising
        `core.llm.NotNow`. No session: the gate is inside the provider's
        call; a hook that reads the database opens a short one of its own."""
        return None

    def writer_refused(self, region: Optional[str], status: Optional[int], detail: str) -> None:
        """A region's writer refused for payment (HTTP 402), once per rest of
        the drain (services/facts/drain.py, `_provider_refused`): the
        hosted service tells its operator. `detail` is the failure's category
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

    def before_key_revoked(self, db, tenant_id: UUID, key_id: UUID) -> None:
        """Before a door of the account revokes one of its keys (POST
        /admin/keys/{id}/revoke), in the request's session, which commits
        the revocation: the hosted service refuses the last live key of an
        account it does not let make keys yet, which would leave nothing
        that reaches the account. Not the operator's own command line
        (manage.py), nor what revokes keys of apps connected over OAuth,
        which such an account cannot hold (`before_new_key`)."""
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
