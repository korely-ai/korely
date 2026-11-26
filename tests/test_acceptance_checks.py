"""One copy of the acceptance verdicts, in English (audit of 17 Oct
2026).

acceptance.sh and acceptance_survives.sh each defined ok, bad, is and has,
the two copies had drifted, and their messages were Italian ("atteso ...,
ottenuto ..."). Both now source scripts/acceptance_checks.sh."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKS = ROOT / "scripts" / "acceptance_checks.sh"


def _bash(script):
    return subprocess.run(["bash", "-c", f'. "{CHECKS}"\n{script}'], capture_output=True,
                          text=True, check=True).stdout


def test_the_verdicts_and_their_messages():
    out = _bash('is "a status" 200 500; has "a body" needle hay; hasnt "no leak" key "a key"\n'
                'nomatch "no id" "[0-9]+" "id 42"; ok "fine"\n'
                'printf "%s %s\\n" "$PASS" "$FAIL"; printf "$FAILED_NAMES"')
    assert "expected 200, got 500" in out
    assert "missing: needle" in out
    assert "present, and should not be: key" in out
    assert "matches, and should not: [0-9]+" in out
    assert "1 4\n" in out
    assert "    a status\n    a body\n    no leak\n    no id\n" in out


def test_both_scripts_source_the_one_copy():
    for name in ("acceptance.sh", "acceptance_survives.sh"):
        text = (ROOT / "scripts" / name).read_text()
        assert '. "$HERE/scripts/acceptance_checks.sh"' in text, name
        own = re.findall(r"^(ok|bad|block|is|has|hasnt|nomatch)\s*\(\)", text, re.M)
        assert own == [], f"{name} defines its own {own}"


# ── a check that needs what the install lacks is not run, and said so ───────
#
# Up to 1.0.28 the checks of what a model extracted failed on every run
# without a model key, the same eight failures on 1.0.20 to 1.0.28, and a
# real failure hid among them. The
# seven checks of acceptance.sh that a run with no writer and no model key
# does not run, by the names the release logs printed them with: six of the
# eight failures, and "April does not read Basic", which passed on an empty
# reading. Of the other two, one asked a key to be set where none was; the
# other is the browser's.
NEEDS_A_WRITER = ("today reads Basic", "April reads Priority", "April does not read Basic",
                  "extraction is in there", "a fact to forget", "a fact to correct",
                  "categories are predicate families")
ACCEPTANCE = ROOT / "scripts" / "acceptance.sh"
SURVIVES = ROOT / "scripts" / "acceptance_survives.sh"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _run(script):
    out = subprocess.run(["bash", "-c", f'. "{CHECKS}"\n{script}'], capture_output=True, text=True)
    return out.returncode, ANSI.sub("", out.stdout)


def _function(path, name):
    """The text of a shell function the script defines, to run it here."""
    text = path.read_text()
    start = text.index(f"{name}() {{")
    return text[start:text.index("\n}\n", start) + 3]


def test_a_check_that_needs_what_the_install_lacks_is_neither_passed_nor_failed():
    code, out = _run('block "a block"; unmet "needs a model key"\n'
                     'has "a" needle hay; hasnt "b" needle hay; is "c" 1 1\n'
                     'block "the next"; ok "d"; unmet "needs a model key" bad "e" why; ok "f"\n'
                     'printf "%s %s %s\\n" "$PASS" "$FAIL" "$NOT_RUN"')
    assert code == 0
    assert "2 0 4\n" in out, out
    for name in "abce":
        assert f"  not run {name}: needs a model key\n" in out
    assert "  ok   d\n" in out and "  ok   f\n" in out
    assert "FAIL" not in out


def test_the_verdict_fails_only_for_a_failure_and_names_what_was_not_run():
    code, out = _run('ok "x"; unmet "needs a model key" ok "y"; verdict "it does what it says"')
    assert code == 0
    assert "1 passed, 0 failed, 1 not run\n" in out
    assert "not run:\n    y (needs a model key)\n" in out
    assert "1 checks were not run: this run does not show that it does what it says.\n" in out
    assert "\nit does what it says.\n" not in out

    code, out = _run('unmet "needs a model key" ok "y"; bad "z" why; verdict "it does what it says"')
    assert code == 1
    assert "0 passed, 1 failed, 1 not run\n" in out
    assert "not run:\n    y (needs a model key)\nfailed:\n    z\n" in out

    code, out = _run('ok "x"; verdict "it does what it says"')
    assert code == 0 and out.endswith("1 passed, 0 failed, 0 not run\nit does what it says.\n")


def _writer_decision():
    """acceptance.sh from `a_model_key_given` to the end of `with_a_writer`:
    the key given, what GET /admin/models answers, the check of the two."""
    text = ACCEPTANCE.read_text()
    start = text.index("a_model_key_given() {")
    end = text.index("with_a_writer() {", start)
    return text[start:text.index("\n}\n", end) + 3]


SECRET = "ZZSECRETNEVERPRINTED42"


def test_without_a_writer_the_checks_of_extracted_facts_are_not_run(tmp_path):
    """`with_a_writer` as acceptance.sh defines it, on what GET /admin/models
    answers and on whether whoever runs it gave a model key: none and no
    key, not run; none with a key, the install fails and so do they;
    configured, run; an answer that says neither, run, so that an install
    that cannot write fails them. A key given in the install's .env is its
    last line written, and its value is never printed."""
    unset = '{"configured":false,"source":null}'
    cases = (
        # (answer, key in the environment, .env lines, writer, a key given,
        #  verdict of the two checks, passed failed not run)
        # An install's .env always holds other keys, never a model's.
        (unset, "", ["SECRET_KEY=0a1b", "GOOGLE_API_KEY=", f"# MISTRAL_API_KEY={SECRET}"],
         "none", "no", "not run", "1 0 2"),
        (unset, "", [f"MISTRAL_API_KEY={SECRET}", "MISTRAL_API_KEY="], "none", "no", "not run", "1 0 2"),
        (unset, SECRET, ["GOOGLE_API_KEY="], "none", "yes", "FAIL", "1 3 0"),
        (unset, "", ["GOOGLE_API_KEY=", f"GOOGLE_API_KEY={SECRET}"], "none", "yes", "FAIL", "1 3 0"),
        (unset, "", ["MISTRAL_API_KEY=", f"MISTRAL_API_KEY={SECRET}"], "none", "yes", "FAIL", "1 3 0"),
        ('{"configured":true,"source":"env"}', SECRET, [], "configured", "yes", "FAIL", "2 2 0"),
        ('{"configured":true,"source":"env"}', "", [], "configured", "no", "FAIL", "1 2 0"),
        ("<html>502</html>", "", [], "unknown", "no", "FAIL", "1 2 0"),
        ("<html>502</html>", SECRET, [], "unknown", "yes", "FAIL", "1 3 0"),
    )
    for n, (answer, key, env, writer, given, verdict, counts) in enumerate(cases):
        install = tmp_path / str(n)
        install.mkdir()
        (install / ".env").write_text("".join(line + "\n" for line in env))
        code, out = _run(f"ANSWER='{answer}'; DIR='{install}'; GOOGLE_API_KEY='{key}'\n"
                         'body() { printf "%s" "$ANSWER"; }\n'
                         f"{_writer_decision()}\n"
                         'echo "writer=$WRITER"; block "the moat"; with_a_writer\n'
                         'has "today reads Basic" Basic "{}"\n'
                         'block "S2"; with_a_writer has "extraction is in there" x "{}"; has "by stage" a a\n'
                         'printf "%s %s %s\\n" "$PASS" "$FAIL" "$NOT_RUN"')
        assert f"a model that writes: {writer} (a model key given: {given})\n" in out, (n, out)
        assert f"writer={writer}\n" in out, (n, out)
        assert f"  {verdict} today reads Basic" in out and f"  {verdict} extraction is in there" in out, (n, out)
        assert f"{counts}\n" in out, (n, out)
        said = {"yes": "ok   " if writer == "configured" else "FAIL ", "no": None}[given]
        assert ("the model key given reaches the install" in out) == (given == "yes"), (n, out)
        assert said is None or f"  {said}the install says a model writes" in out, (n, out)
        assert SECRET not in out, n


def test_a_run_with_no_writer_and_no_key_does_not_run_the_seven():
    """The blocks of acceptance.sh as they are, on an install that answers
    nothing and has no writer, no model key given: exactly the seven are
    not run, nothing passes or fails, nothing waits for a fact."""
    text = ACCEPTANCE.read_text()

    def between(a, b):
        start = text.index(a)
        return text[start:text.index(b, start)]

    moat = between('block "the moat:', 'block "S1: administration')
    usage = between('with_a_writer has "extraction is in there"', "\n") + "\n"
    forgetting = between('block "S3: forgetting', 'has "request activity"')
    helper = _function(ACCEPTANCE, "with_a_writer")
    code, out = _run("WRITER=none; KEY_GIVEN=no\n"
                     'body() { :; }; code() { printf 000; }; sleep() { echo "slept"; }\n'
                     f"{helper}\n{moat}\n{usage}{forgetting}\n"
                     'printf "%s %s %s\\n" "$PASS" "$FAIL" "$NOT_RUN"')
    assert code == 0, out
    assert out.endswith("0 0 7\n"), out
    names = re.findall(r"^  not run (.+): needs a model key$", out, re.M)
    assert sorted(names) == sorted(NEEDS_A_WRITER), names
    assert "slept" not in out


def _checks_by_block(path, gate):
    """(block, check name, gated) for every check of a script, gated when the
    check is given to `gate` or follows a bare `gate` in its block."""
    found, block, blanket = [], None, False
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("block "):
            block, blanket = line, False
        elif line == gate:
            blanket = True
        m = re.match(rf'^(?:({re.escape(gate)})\s+)?(?:is|has|hasnt|nomatch|ok|bad)\s+"([^"]+)"', line)
        if m:
            found.append((block, m.group(2), blanket or m.group(1) is not None))
    return found


def test_acceptance_gates_the_checks_of_extracted_facts_and_no_other_block():
    found = _checks_by_block(ACCEPTANCE, "with_a_writer")
    gated = {name for _b, name, g in found if g}
    assert set(NEEDS_A_WRITER) <= gated, set(NEEDS_A_WRITER) - gated
    # A gate does not spread: a gated check is in a block of one of the six.
    theirs = {b for b, name, _g in found if name in NEEDS_A_WRITER}
    assert {b for b, _n, g in found if g} <= theirs
    assert len(gated) < len(found) / 4, "most of the acceptance needs no model"


def test_a_refused_provider_key_is_checked_against_what_the_install_had():
    """After a refused save the install keeps what it had, a key or none.
    It asked for `"set":true` and failed on every install with no model
    key. The line as acceptance.sh has it, on what GET /admin/provider said
    before (PROV) and says after: the same, ok; another, or nothing read
    before, a failure."""
    text = ACCEPTANCE.read_text()
    start = text.index("HAD=$(printf '%s' \"$PROV\"")
    end = text.index('has "and the install still runs on the one it had"', start)
    line = text[start:text.index("\n\n", end)]
    assert '"set":true' not in line
    on, off = '{"set":true,"source":"screen","last_call":null}', '{"set":false,"source":null,"last_call":null}'
    for before, after, verdict in ((on, on, "ok  "), (on, off, "FAIL"), (off, off, "ok  "),
                                   (off, on, "FAIL"), ("", off, "FAIL"), ("", on, "FAIL")):
        _code, out = _run(f"PROV='{before}'; AFTER='{after}'\n"
                          'body() { printf "%s" "$AFTER"; }\n'
                          f"{line}\n")
        assert f"  {verdict} and the install still runs on the one it had" in out, (before, after, out)
    assert not any(name == "and the install still runs on the one it had" and g
                   for _b, name, g in _checks_by_block(ACCEPTANCE, "with_a_writer"))


def test_survives_says_what_it_did_not_run_without_a_model_key():
    found = {name: g for _b, name, g in _checks_by_block(SURVIVES, "with_a_model_key")}
    for name in ("the facts survived", "April still reads April", "and the facts"):
        assert found.get(name) is True, name
    helper = _function(SURVIVES, "with_a_model_key")
    code, out = _run(f'{helper}\nGOOGLE_API_KEY= ; with_a_model_key is "and the facts" 0 0\n'
                     'printf "%s %s\\n" "$PASS" "$NOT_RUN"')
    assert "  not run and the facts: needs a model key\n" in out and "0 1\n" in out
    # The block a key alone can run says, without one, that it did not run.
    text = SURVIVES.read_text()
    after = text[text.index('block "the same dump where SECRET_KEY is another one"'):]
    assert "\nelse\n" in after[:after.index("\nfi\n") + 4]
    assert "not_run " in after[after.index("\nelse\n"):after.index("\nfi\n")]
    assert text.count("verdict ") == 1 and "printf '%d passed" not in text
