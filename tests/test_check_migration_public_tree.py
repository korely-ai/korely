"""scripts/check_migration.py in the public repository, which has no hosted/
package: the migrations create the hosted service's tables on every install
(one chain), and the models of a tree without that package do not declare
them. Found by the tests workflow of the first public tag (1 Nov 2026): the
check called them drift, and a tree that carries the package must still be
compared in full."""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_migration  # noqa: E402

TABLES = {"tenants", "agent_memories", "hosted_accounts", "hosted_brakes"}


def test_without_the_package_the_hosted_tables_are_not_drift():
    assert check_migration.left_to_the_hosted_package(TABLES, hosted_present=False) == {
        "tenants", "agent_memories"}


def test_with_the_package_nothing_is_set_aside():
    assert check_migration.left_to_the_hosted_package(TABLES, hosted_present=True) == TABLES


def test_only_the_hosted_prefix_is_set_aside():
    # A table of the product that merely mentions hosted stays compared.
    assert "memories_hosted" in check_migration.left_to_the_hosted_package(
        {"memories_hosted"}, hosted_present=False)


def test_this_tree_decides_by_the_package_it_has():
    # In the private tree the package is there; the public copy deletes it.
    here = importlib.util.find_spec("korely_agent.hosted") is not None
    assert here == (Path(__file__).resolve().parents[1] / "korely_agent" / "hosted").is_dir()
