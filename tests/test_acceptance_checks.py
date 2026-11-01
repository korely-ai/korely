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
