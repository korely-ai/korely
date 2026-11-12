"""What the tests share about the Cloud's optional package (korely_agent/hosted/).

The public repository has the engine and the Self-hosted product, not the
Cloud's sign-in and payments (the decision of 28 Oct 2026). A test
that needs the package is marked `cloud` (tests/conftest.py skips it where the
package is not); a test file of the package itself is tests/test_hosted_*.py
and goes with it. Two more things a test of the core may need from here:
the modes it can build, and its lists of named places with the Cloud's
entries left out where the Cloud's files are not.
"""
import pytest

from korely_agent.core import extensions

# This tree carries the Cloud's package.
HERE = extensions.cloud() is not None

# The modes a test parametrized over both builds; the Cloud's skips where the
# package is not, since KORELY_MODE=hosted is refused there.
MODES = ("self-hosted", pytest.param("hosted", marks=pytest.mark.cloud))

# The Cloud's folders: korely_agent/hosted/ and dashboard/src/hosted/, as the
# guards that read the source name them, from the repository's root or from
# the package's or the screen's own (`hosted/spending.py`).
ROOTS = ("korely_agent/", "dashboard/src/")
FOLDER = "hosted/"


def is_the_clouds(path) -> bool:
    path = str(path)
    for root in ROOTS:
        if path.startswith(root):
            path = path[len(root):]
            break
    return path.startswith(FOLDER)


def in_this_tree(entries):
    """`entries` (paths, or tuples whose first item is a path) without those
    in the Cloud's folders when this tree does not carry them: a guard that
    names a place of the Cloud's still holds the core's places in a tree
    without it, and the Cloud's ones wherever they are."""
    def path(e):
        return e[0] if isinstance(e, tuple) else e
    kept = [e for e in entries if HERE or not is_the_clouds(path(e))]
    return type(entries)(kept) if not isinstance(entries, dict) else {
        k: entries[k] for k in kept}
