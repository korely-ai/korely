"""The browser check keeps the operator's password off every command line
(audit of 17 Oct 2026).

Spliced into the container's `sh -c` between single quotes, a password with an
apostrophe broke the run and one with `'; cmd` ran a command in the container;
wave 2 moved the credentials into environment variables. They still reached
two command lines `ps` shows to every account on the test machine: the docker
client's (`-e OPP=<password>`) and node's (its arguments), and acceptance.sh
handed the password to acceptance_browser.sh as an argument too. Played with a
docker that writes down what it was given."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "it's'; touch /tmp/owned; echo '"
KEY = "an-install-key-for-the-test"


def _run(tmp_path, args, extra_env):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS"\nprintf "%s" "$OPP" > "$SEEN"\n'
        'printf "%s" "$KORELY_API_KEY" > "$SEEN.key"\n')
    (fake / "docker").chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}",
               ARGS=str(tmp_path / "args"), SEEN=str(tmp_path / "seen"), **extra_env)
    r = subprocess.run(["bash", str(ROOT / "scripts" / "acceptance_browser.sh"), *args],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return ((tmp_path / "args").read_text(), (tmp_path / "seen").read_text(),
            (tmp_path / "seen.key").read_text())


def test_the_password_reaches_the_container_and_no_command_line(tmp_path):
    argv, seen, key = _run(tmp_path, ["http://127.0.0.1", "operator"],
                           {"OPERATOR_PASSWORD": PASSWORD, "WRITER": "none",
                            "KORELY_API_KEY": KEY})
    assert seen == PASSWORD, "the container gets the password byte for byte"
    assert "touch /tmp/owned" not in argv and "it's" not in argv
    assert "\n-e\nOPP\n" in argv, "docker takes the value from the environment"
    # The key the modals check writes with (its agent drawer), the same way.
    assert key == KEY and KEY not in argv and "\n-e\nKORELY_API_KEY\n" in argv


def test_the_acceptance_passes_it_in_the_environment():
    acceptance = (ROOT / "scripts" / "acceptance.sh").read_text()
    call = next(l for l in acceptance.splitlines()
                if "acceptance_browser.sh" in l and not l.lstrip().startswith("#"))
    # Its output kept in a variable, not in /tmp/korely-browser.txt
    # (tests/test_acceptance_leaves_nothing_behind.py).
    assert call.lstrip().startswith("if OUT=$(OPERATOR_PASSWORD="), call
    assert "PASSWORD" not in call.split("acceptance_browser.sh", 1)[1], call


def test_the_dashboard_check_reads_the_environment():
    """check_modals_in_a_browser.mjs already did; the dashboard check read
    its arguments, so the inner shell had to put the password on node's."""
    src = (ROOT / "scripts" / "check_dashboard_in_a_browser.mjs").read_text()
    assert "process.env.OPP" in src and "process.env.OPU" in src
    inner = (ROOT / "scripts" / "acceptance_browser.sh").read_text()
    assert '"$OPP"' not in inner.split("docker run", 1)[1]


def test_the_logic_of_the_browser_checks_runs_with_the_dashboards_tests():
    """Only a real browser on a real install ran browser_sign_in.mjs, once a
    release, and a check switched off in it passed every test here (review
    of 15 Nov 2026). Its fake page runs in `make dashboard-test`, which
    `make test-all` runs, in the node image that target already uses (this
    image has no node)."""
    makefile = (ROOT / "Makefile").read_text()
    recipe = makefile.split("\ndashboard-test:\n", 1)[1].split("\n\n", 1)[0]
    assert "-v $(CURDIR)/scripts:/scripts:ro" in recipe
    assert "node /scripts/browser_checks_on_a_fake_page.mjs" in recipe
    assert "test-all: image dashboard-test" in makefile
    fake = (ROOT / "scripts" / "browser_checks_on_a_fake_page.mjs").read_text()
    assert 'from "./browser_sign_in.mjs"' in fake
    module = (ROOT / "scripts" / "browser_sign_in.mjs").read_text()
    assert "import " not in module, "the module needs no package, so node runs it bare"
    check = (ROOT / "scripts" / "check_dashboard_in_a_browser.mjs").read_text()
    assert "await visitPages(page, " in check, "the real check visits the pages the fake ones do"
