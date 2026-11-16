"""The acceptance run leaves nothing of its own acting after it, and no
credential in a file (functional test of 1.0.0, 28 Oct 2026).

- scripts/acceptance.sh registered a webhook endpoint at
  https://example.com/korely-acceptance-nope, subscribed to every event, to
  test a delivery that fails, and never removed it: the install went on
  sending its real deliveries there after the run.
- The browser check printed the body of the sign-in's answer, the live
  session token, and acceptance.sh kept what it printed in
  /tmp/korely-browser.txt, a fixed path any account on the machine reads.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCEPTANCE = (ROOT / "scripts" / "acceptance.sh").read_text()


def _code(text: str) -> list:
    return [l for l in text.splitlines() if not l.lstrip().startswith(("#", "//"))]


def test_every_webhook_endpoint_it_registers_is_removed():
    lines = _code(ACCEPTANCE)
    made = {}
    for i, line in enumerate(lines):
        m = re.match(r'\s*(\w+)=\$\(body POST "\$API_URL/admin/webhooks" ', line)
        if m:
            # The id is read from the answer on the next line.
            read = re.compile(r"\s*(\w+)=\$\((?:echo|printf '%s') \"\$" + m.group(1) + r"\"")
            made[m.group(1)] = next(read.match(l).group(1) for l in lines[i + 1:i + 3]
                                    if read.match(l))
    assert made, "the parser found no registration: it is broken, not the script"
    for answer, idvar in made.items():
        assert any(f'code DELETE "$API_URL/admin/webhooks/${idvar}"' in l for l in lines), \
            f"the endpoint in ${idvar} ({answer}) is never removed"


def test_the_test_endpoint_is_removed_when_the_run_stops_early():
    body = ACCEPTANCE[ACCEPTANCE.index("cleanup() {"):]
    body = body[:body.index("\n}\n")]
    assert 'code DELETE "$API_URL/admin/webhooks/$THID"' in body


def test_no_output_goes_to_a_fixed_path_in_tmp():
    offenders = [l for l in _code(ACCEPTANCE) if re.search(r"/tmp/korely-[\w.-]+", l)]
    assert offenders == [], offenders


def test_the_browser_check_never_prints_the_sessions_answer():
    src = (ROOT / "scripts" / "check_modals_in_a_browser.mjs").read_text()
    watch = src[src.index('page.on("response"'):]
    watch = watch[:watch.index("\n});")]
    assert "if (!r.ok())" in watch, "the body is read only for a refusal"
    assert watch.index("if (!r.ok())") < watch.index("r.text()")
    for name in ("check_dashboard_in_a_browser.mjs", "check_modals_in_a_browser.mjs"):
        text = "\n".join(_code((ROOT / "scripts" / name).read_text()))
        assert "writeFile" not in text and "/tmp/korely" not in text, name
