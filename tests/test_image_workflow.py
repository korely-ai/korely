"""The release image is built natively for each platform (audit of 17 Oct
2026). Built on one amd64 runner, the arm64 half ran under QEMU and took most
of a 17-minute job, out of GitHub Free's 2,000 minutes a month; its `gha`
cache was written on every run and hit by none."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = yaml.safe_load((ROOT / ".github" / "workflows" / "image.yml").read_text())


def _uses(job):
    return [s.get("uses", "") for s in job["steps"]]


def test_no_platform_is_emulated():
    for name, job in WF["jobs"].items():
        assert not any("setup-qemu-action" in u for u in _uses(job)), name


def test_each_platform_is_built_on_a_runner_of_its_architecture():
    job = WF["jobs"]["image"]
    matrix = job["strategy"]["matrix"]
    runners = {i["platform"]: i["runner"] for i in matrix["include"]}
    assert set(matrix["platform"]) == {"amd64", "arm64"}
    assert runners["arm64"].endswith("-arm") and not runners["amd64"].endswith("-arm")
    assert job["runs-on"] == "${{ matrix.runner }}"
    build = next(s for s in job["steps"] if "build-push-action" in s.get("uses", ""))
    assert build["with"]["platforms"] == "linux/${{ matrix.platform }}"
    # The cache nobody read is not written either.
    assert "cache-to" not in build["with"] and "cache-from" not in build["with"]


def test_the_release_tags_carry_both_platforms():
    job = WF["jobs"]["manifest"]
    assert job["needs"] == "image"
    script = "\n".join(s.get("run", "") for s in job["steps"])
    assert 'imagetools create -t "$TAG" -t "$LATEST" "$TAG-amd64" "$TAG-arm64"' in script
    assert "linux/amd64 linux/arm64" in script
