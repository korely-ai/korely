"""A private repository's Actions minutes are counted, a public one's are
free (17 Nov 2026): on a release tag a private repository builds, joins and
starts only the image a deploy runs, `local` on amd64, and skips the whole
suite, which runs there every Sunday night and by hand; a public repository
runs all of it on every tag, as before. A private release took about 114
billed minutes (v1.0.30: the suite's 100, the images' 12, the smoke's 2),
and takes about 5 with this rule. The rule is read from the event
(`github.event.repository.private`), never from an owner's name, since the
public copy holds the same files. The workflows' own expressions are
evaluated here (tests/github_expressions.py)."""
import re
from pathlib import Path

import pytest
import yaml

from tests.github_expressions import event, evaluate, matrix, on, truthy, value

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
TESTS = yaml.safe_load((WORKFLOWS / "tests.yml").read_text())
IMAGE = yaml.safe_load((WORKFLOWS / "image.yml").read_text())
SMOKE = yaml.safe_load((WORKFLOWS / "image-smoke.yml").read_text())


def _runs(job, name, private):
    return truthy(evaluate(job["if"], event(name, private)))


@pytest.mark.parametrize("job", sorted(TESTS["jobs"]))
def test_a_tag_runs_the_suite_in_a_public_repository_and_skips_it_in_a_private_one(job):
    """Every job of the workflow: a skipped job bills no minute."""
    jobs = TESTS["jobs"]
    assert _runs(jobs[job], "push", private=False)
    assert not _runs(jobs[job], "push", private=True)
    # Sunday's run is the private repository's; the public one has its tags.
    assert _runs(jobs[job], "schedule", private=True)
    assert not _runs(jobs[job], "schedule", private=False)
    # By hand, anywhere.
    assert _runs(jobs[job], "workflow_dispatch", private=True)
    assert _runs(jobs[job], "workflow_dispatch", private=False)


def test_the_suite_runs_on_the_tags_every_sunday_and_by_hand():
    triggers = on(TESTS)
    assert triggers["push"] == {"tags": ["v*.*.*"]}
    assert "workflow_dispatch" in triggers
    # The whole line, not only the day: "*/5 * * * 0" is a Sunday too, and
    # would bill about 100 minutes every five minutes of it.
    assert [s["cron"] for s in triggers["schedule"]] == ["0 22 * * 0"]


def _built(private):
    job = IMAGE["jobs"]["image"]
    rows = matrix(job, event("push", private))
    return sorted((r["embedder"], r["platform"],
                   value(job["runs-on"], {"matrix": r, **event("push", private)}))
                  for r in rows)


def test_a_private_repository_builds_the_one_image_a_deploy_runs():
    assert _built(private=True) == [("local", "amd64", "ubuntu-latest")]
    assert _built(private=False) == [
        ("gemini", "amd64", "ubuntu-latest"), ("gemini", "arm64", "ubuntu-24.04-arm"),
        ("local", "amd64", "ubuntu-latest"), ("local", "arm64", "ubuntu-24.04-arm")]


def _joined(private):
    job = IMAGE["jobs"]["manifest"]
    ctx = event("push", private)
    step = next(s for s in job["steps"] if "imagetools create" in s.get("run", ""))
    return ({r["embedder"] for r in matrix(job, ctx)},
            set(value(step["env"]["PLATFORMS"], ctx).split()))


@pytest.mark.parametrize("private", [True, False])
def test_the_release_tags_are_put_on_what_was_built_and_the_smoke_starts_it(private):
    built = _built(private)
    embedders, platforms = _joined(private)
    assert embedders == {e for e, _p, _r in built}
    assert platforms == {p for _e, p, _r in built}
    smoked = {r["embedder"] for r in matrix(SMOKE["jobs"]["smoke"], event("workflow_run", private))}
    assert smoked == embedders
    expected = ({"local"}, {"amd64"}) if private else ({"gemini", "local"}, {"amd64", "arm64"})
    assert (embedders, platforms) == expected


def _smoke_runs(name, conclusion):
    ctx = event(name, True)
    ctx["github"]["event"]["workflow_run"] = {"conclusion": conclusion}
    return truthy(evaluate(SMOKE["jobs"]["smoke"]["if"], ctx))


def test_the_smoke_starts_only_after_an_image_built_or_by_hand():
    """A deploy waits for the smoke: it must never start on an image whose
    build failed, nor on a run that was cancelled."""
    assert _smoke_runs("workflow_run", "success")
    for conclusion in ("failure", "cancelled", "skipped", None):
        assert not _smoke_runs("workflow_run", conclusion), conclusion
    assert _smoke_runs("workflow_dispatch", None)


def test_the_smoke_starts_the_image_of_its_embedder():
    """`local` is the release's plain tag `X.Y.Z`, `gemini` its `-gemini` one
    (image.yml): the smoke must start the image its row names."""
    step = next(s for s in SMOKE["jobs"]["smoke"]["steps"]
                if "suffix=" in s.get("run", ""))
    lines = [line.strip() for line in step["run"].splitlines()]
    assert 'suffix=""' in lines
    assert '[ "${{ matrix.embedder }}" = gemini ] && suffix="-gemini"' in lines
    assert not any("local" in line and "suffix=" in line for line in lines)


def test_no_workflow_decides_by_an_owners_name():
    """The rule's one fact is the event's; a comparison with the repository's
    or its owner's name would be wrong in the public copy, which holds these
    same files."""
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        text = wf.read_text()
        assert not re.search(r"github\.repository(_owner)?\s*[!=]=", text), wf.name
        assert not re.search(r"[!=]=\s*'[^']*/[^']*'", text), wf.name
        for m in re.finditer(r"[\w.]+\.(?:private|visibility)\b", text):
            assert m.group(0) == "github.event.repository.private", f"{wf.name}: {m.group(0)}"


def test_the_evaluator_reads_github_s_meaning():
    """What the tests above rest on: `&&` and `||` give an operand, a
    missing property is null, strings compare ignoring case."""
    assert evaluate("${{ true && 'a' || 'b' }}", {}) == "a"
    assert evaluate("${{ false && 'a' || 'b' }}", {}) == "b"
    assert evaluate("github.event.nothing.here", {"github": {}}) is None
    assert evaluate("'ARM64' == 'arm64'", {}) is True
    assert evaluate("!github.event.repository.private", event("push", None)) is True
    with pytest.raises(ValueError):
        evaluate("contains(github.ref, 'v')", {})
