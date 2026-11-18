"""The release image is built natively for each platform (audit of 17 Oct
2026). Built on one amd64 runner, the arm64 half ran under QEMU and took most
of a 17-minute job, out of GitHub Free's 2,000 minutes a month; its `gha`
cache was written on every run and hit by none."""
from pathlib import Path

import yaml

from tests.github_expressions import event, matrix, value

ROOT = Path(__file__).resolve().parent.parent
WF = yaml.safe_load((ROOT / ".github" / "workflows" / "image.yml").read_text())


def _uses(job):
    return [s.get("uses", "") for s in job["steps"]]


def test_no_platform_is_emulated():
    for name, job in WF["jobs"].items():
        assert not any("setup-qemu-action" in u for u in _uses(job)), name


def test_each_platform_is_built_on_a_runner_of_its_architecture():
    """Read in a public repository, which builds both platforms (a private
    one builds amd64 alone: tests/test_the_minutes_of_a_private_repository.py)."""
    job = WF["jobs"]["image"]
    ctx = event("push", private=False)
    runners = {r["platform"]: value(job["runs-on"], {"matrix": r, **ctx})
               for r in matrix(job, ctx)}
    assert set(runners) == {"amd64", "arm64"}
    assert runners["arm64"].endswith("-arm") and not runners["amd64"].endswith("-arm")
    build = next(s for s in job["steps"] if "build-push-action" in s.get("uses", ""))
    assert build["with"]["platforms"] == "linux/${{ matrix.platform }}"
    # The cache nobody read is not written either.
    assert "cache-to" not in build["with"] and "cache-from" not in build["with"]


def test_the_release_tags_carry_the_platforms_built():
    job = WF["jobs"]["manifest"]
    assert job["needs"] == "image"
    step = next(s for s in job["steps"] if "imagetools create" in s.get("run", ""))
    script = step["run"]
    assert 'for p in $PLATFORMS; do set -- "$@" "$TAG-$p"; done' in script
    assert 'imagetools create -t "$TAG" -t "$LATEST" "$@"' in script
    # Each platform is checked on the index the tags point at.
    assert 'for p in $PLATFORMS; do' in script.split("imagetools inspect", 1)[1]
    assert 'grep -q "Platform: *linux/$p\\$"' in script
    assert value(step["env"]["PLATFORMS"], event("push", private=False)) == "amd64 arm64"
