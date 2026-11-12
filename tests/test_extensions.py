"""The hooks the hosted service adds to the core: on an install of one's own each does nothing.

korely_agent/core/extensions.py. Nothing is registered unless the hosted
package registers it, and then the core behaves as it always has: every
default answers None and refuses nothing.
"""
import uuid

import pytest

from korely_agent.core import extensions


@pytest.fixture(autouse=True)
def _clean():
    extensions.reset()
    yield
    extensions.reset()


T = uuid.uuid4()


def test_before_write_does_nothing():
    assert extensions.hooks.before_write(None, T, ["I moved to Lisbon."]) is None


def test_before_query_does_nothing():
    assert extensions.hooks.before_query(None, T) is None


def test_rate_limits_leave_the_installs_own():
    assert extensions.hooks.rate_limits(None, T) is None


def test_before_new_agent_id_does_nothing():
    assert extensions.hooks.before_new_agent_id(None, T, "support-bot") is None


def test_agent_cap_says_no_ceiling():
    assert extensions.hooks.agent_cap(None, T) is None


def test_before_model_call_does_nothing():
    assert extensions.hooks.before_model_call("google", "gemini-3.1-flash-lite", "global") is None


def test_install_settings_are_left_to_the_mode():
    assert extensions.hooks.can_change_install_settings(None, object()) is None


def test_any_writer_may_be_saved_on_an_install_of_ones_own():
    assert extensions.hooks.before_writer_saved(None, "mistral") is None


def test_any_key_may_be_made_on_an_install_of_ones_own():
    assert extensions.hooks.before_new_key(None, T) is None


def test_a_registered_hook_runs_and_reset_takes_it_away():
    extensions.register("before_query", lambda db, tenant_id: "refused")
    assert extensions.hooks.before_query(None, T) == "refused"
    extensions.reset()
    assert extensions.hooks.before_query(None, T) is None


def test_an_app_built_on_an_install_of_ones_own_drops_the_hooks(monkeypatch):
    """A process that built a hosted app before (the tests do) builds the
    next one with every hook back to its default."""
    from korely_agent import config
    from korely_agent.main import create_app

    extensions.register("before_query", lambda db, tenant_id: "refused")
    monkeypatch.delenv("KORELY_MODE", raising=False)
    monkeypatch.setenv("RUN_WORKER", "false")
    config.get_settings.cache_clear()
    create_app()
    assert extensions.hooks.before_query(None, T) is None


@pytest.mark.parametrize("name", ["before_wirte", "_Hooks", "__init__", "hooks"])
def test_a_name_the_core_never_calls_is_refused(name):
    with pytest.raises(ValueError, match="no hook"):
        extensions.register(name, lambda *a: None)


# The Cloud's package is optional (the decision of 28 Oct 2026):
# `cloud()` is where the core finds it, and a tree without it refuses the
# Cloud's mode at start. `sys.modules[name] = None` is how Python says a
# module is not there: the import raises ModuleNotFoundError for that name.
@pytest.fixture
def without_the_cloud(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, extensions.CLOUD_PACKAGE, None)


def test_a_tree_without_the_cloud_has_none(without_the_cloud):
    assert extensions.cloud() is None
    assert extensions.cloud("replay") is None


def test_a_tree_without_the_cloud_has_its_tables_and_not_their_models(without_the_cloud):
    names = ["tenants", "agent_memories", "hosted_accounts", "hosted_erasures"]
    assert extensions.cloud_tables_without_models(names) == {"hosted_accounts",
                                                             "hosted_erasures"}


@pytest.mark.cloud
def test_the_cloud_is_found_where_the_tree_has_it():
    assert extensions.cloud() is not None
    assert extensions.cloud("replay").__name__ == f"{extensions.CLOUD_PACKAGE}.replay"
    assert extensions.cloud_tables_without_models(["hosted_accounts"]) == set()


def test_the_cloud_mode_without_the_cloud_refuses_to_start(without_the_cloud, monkeypatch):
    """Never an install of one's own that says it is the Cloud: every process
    reads the settings first, and they refuse."""
    from korely_agent import config

    monkeypatch.setenv("KORELY_MODE", "hosted")
    config.get_settings.cache_clear()
    try:
        with pytest.raises(Exception, match="KORELY_MODE=hosted is the mode of the Korely Cloud"):
            config.get_settings()
        from korely_agent.main import create_app

        with pytest.raises(Exception, match="this install does not carry"):
            create_app()
    finally:
        monkeypatch.delenv("KORELY_MODE")
        config.get_settings.cache_clear()


def test_an_install_of_its_own_starts_without_the_cloud(without_the_cloud, monkeypatch):
    from korely_agent import config

    monkeypatch.delenv("KORELY_MODE", raising=False)
    monkeypatch.setenv("RUN_WORKER", "false")
    config.get_settings.cache_clear()
    try:
        assert not config.get_settings().hosted
        from korely_agent.main import create_app

        create_app()
    finally:
        config.get_settings.cache_clear()


def test_a_missing_module_inside_the_cloud_is_not_its_absence(monkeypatch):
    """Only the package's own absence is None: a dependency the package fails
    to import (google-auth, say) raises, never a Cloud quietly switched off."""
    import importlib

    def fails(name, *a, **kw):
        raise ModuleNotFoundError("No module named 'google'", name="google")

    monkeypatch.setattr(importlib, "import_module", fails)
    with pytest.raises(ModuleNotFoundError):
        extensions.cloud()


def _a_cloud_package(monkeypatch):
    """A package in the place of the Cloud's, whatever this tree carries: a
    module with a file, as a real package has."""
    import sys
    import types

    package = types.ModuleType(extensions.CLOUD_PACKAGE)
    package.__file__ = "hosted/__init__.py"
    monkeypatch.setitem(sys.modules, extensions.CLOUD_PACKAGE, package)
    return package


def test_a_part_that_fails_on_a_missing_dependency_raises(monkeypatch):
    """The package is there, one of its modules needs a dependency the image
    lacks: cloud("<part>") raises that error, never None."""
    import importlib

    package = _a_cloud_package(monkeypatch)
    real = importlib.import_module

    def only_the_part_fails(name, *a, **kw):
        if name == f"{extensions.CLOUD_PACKAGE}.firebase":
            raise ModuleNotFoundError("No module named 'google'", name="google")
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", only_the_part_fails)
    assert extensions.cloud() is package
    with pytest.raises(ModuleNotFoundError) as e:
        extensions.cloud("firebase")
    assert e.value.name == "google"


@pytest.fixture
def an_empty_cloud_folder(monkeypatch, tmp_path):
    """korely_agent/hosted/ with its __pycache__ alone, as git leaves it when
    a checkout moves from the private tree to the public one: Python imports
    such a folder as a namespace package, a module with no file."""
    import importlib.machinery
    import importlib.util
    import sys

    (tmp_path / "hosted" / "__pycache__").mkdir(parents=True)
    spec = importlib.machinery.PathFinder.find_spec(extensions.CLOUD_PACKAGE, [str(tmp_path)])
    assert spec is not None and spec.origin in (None, "namespace"), spec
    namespace = importlib.util.module_from_spec(spec)
    assert getattr(namespace, "__file__", None) is None
    monkeypatch.setitem(sys.modules, extensions.CLOUD_PACKAGE, namespace)


def test_an_empty_cloud_folder_is_no_cloud(an_empty_cloud_folder):
    assert extensions.cloud() is None
    assert extensions.cloud("models") is None
    assert extensions.cloud_tables_without_models(["hosted_accounts"]) == {"hosted_accounts"}


def test_an_empty_cloud_folder_still_refuses_the_cloud_mode(an_empty_cloud_folder, monkeypatch):
    from korely_agent import config

    monkeypatch.setenv("KORELY_MODE", "hosted")
    config.get_settings.cache_clear()
    try:
        with pytest.raises(Exception, match="this install does not carry"):
            config.get_settings()
    finally:
        monkeypatch.delenv("KORELY_MODE")
        config.get_settings.cache_clear()


def test_any_key_may_be_revoked_on_an_install_of_ones_own():
    assert extensions.hooks.before_key_revoked(None, T, T) is None
