"""The workflows run third-party actions by commit, and Dependabot watches
them (audit of 17 Oct 2026).

A tag such as `@v4` can be moved onto other code by whoever controls the
action's repository, and image.yml runs it with the token that publishes the
images. A full commit cannot be moved; the tag stays as a comment, which is
what Dependabot reads to propose the next one."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def test_every_action_is_pinned_to_a_commit():
    assert WORKFLOWS, "no workflows found"
    seen = 0
    for wf in WORKFLOWS:
        for n, line in enumerate(wf.read_text().splitlines(), 1):
            m = re.match(r"^\s*-?\s*uses:\s*(\S+)(.*)$", line)
            if not m:
                continue
            seen += 1
            ref, rest = m.groups()
            if ref.startswith("./"):
                continue  # an action of this repository
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), (
                f"{wf.name}:{n}: {ref} is not pinned to a full commit")
            assert re.fullmatch(r"\s+#\s+v\d+(\.\d+)*\s*", rest), (
                f"{wf.name}:{n}: the tag the commit stands for goes in a comment")
    assert seen


def test_dependabot_watches_the_actions_and_both_dependency_files():
    config = (ROOT / ".github" / "dependabot.yml").read_text()
    for ecosystem, directory in (("github-actions", "/"), ("pip", "/requirements"), ("npm", "/dashboard")):
        assert re.search(rf"package-ecosystem:\s*{ecosystem}\s*\n\s*directory:\s*{directory}\s*\n",
                         config), f"Dependabot does not watch {ecosystem} in {directory}"
