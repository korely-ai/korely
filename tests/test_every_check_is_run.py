"""A check nobody runs catches nothing (audit of 17 Oct 2026).

check_response_shapes.py, check_dashboard_in_a_browser.mjs (through
acceptance_browser.sh) and check_modals_in_a_browser.mjs were each written
after a real release shipped a blank page or a modal that could not be used,
and none of them was in a release step, a test or a workflow. Every check in
scripts/ is now called by one of the things that do run: the Makefile, the
release and acceptance scripts, the workflows."""
import importlib.util
import re
from pathlib import Path

from tests import cloud

ROOT = Path(__file__).resolve().parent.parent

# What runs: on a release (release.sh, acceptance.sh and what it calls), on a
# tag (the workflows, through the Makefile).
RUNNERS = [ROOT / "Makefile", ROOT / "scripts" / "release.sh",
           ROOT / "scripts" / "acceptance.sh", ROOT / "scripts" / "acceptance_browser.sh",
           ROOT / "scripts" / "acceptance_survives.sh",
           *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
           # The release checks of a part of the product (core/extensions.Part).
           *(ROOT / p for p in cloud.of_the_parts("RUNNERS", tuple))]

# Not a release check: the second half of a measurement against a model, run
# on its rows by whoever runs that measure, which costs money and is never
# automatic (the measurement scripts are not in the public repository).
COMPANIONS_OF_A_MEASURE = {"check_store_state.py"}


def _calls(text: str, name: str) -> bool:
    # A call, not a mention in a comment.
    lines = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    return re.search(rf"(^|[/\s\"']){re.escape(name)}\b", "\n".join(lines), re.M) is not None


def test_every_check_is_called_by_something_that_runs():
    checks = sorted(p.name for p in (ROOT / "scripts").iterdir()
                    if p.name.startswith("check_") and p.suffix in (".py", ".sh", ".mjs"))
    assert "check_response_shapes.py" in checks
    texts = {r.name: r.read_text() for r in RUNNERS}
    unrun = [c for c in checks if c not in COMPANIONS_OF_A_MEASURE
             and not any(_calls(t, c) for t in texts.values())]
    assert unrun == [], f"checks that nothing runs: {unrun}"


def test_the_browser_checks_are_part_of_the_acceptance():
    acceptance = (ROOT / "scripts" / "acceptance.sh").read_text()
    assert _calls(acceptance, "acceptance_browser.sh")
    assert _calls(acceptance, "check_response_shapes.py")


def test_the_shapes_check_still_finds_the_screens_calls():
    """It exits 2 when its parser finds no typed GET at all; the acceptance
    would then report a broken parser on release day. Read here, on the
    dashboard as it is, without a server."""
    spec = importlib.util.spec_from_file_location(
        "check_response_shapes", ROOT / "scripts" / "check_response_shapes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src = ROOT / "dashboard" / "src"
    found = mod.wanted(src, mod.interfaces(src))
    assert found, "the parser finds no typed GET in the dashboard"
    assert all(path.startswith("/admin/") for path, _, _ in found)
