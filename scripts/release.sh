#!/usr/bin/env bash
# Cut a release, or refuse to.
#
# The first tag this repository ever had, v0.1.0, served `"version": "unknown"`.
# The fix that put the VERSION file into the image landed two commits after it,
# so the one tag the README told people to go back to was the one build that
# could not say what it was. A tester found it by doing exactly what the README
# says.
#
# That is the second time in two rounds that a repair existed and did not exist
# at the point somebody picks it up. A tag is not a label on a commit, it is a
# promise about an artefact, so this builds the artefact and asks it.
#
#     scripts/release.sh 0.1.1
set -euo pipefail

want="${1:-}"
here="$(cd "$(dirname "$0")/.." && pwd)"
cd "$here"

if [ -z "$want" ] || ! [[ "$want" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: scripts/release.sh <x.y.z>" >&2
  exit 2
fi
# The public repository, as install.sh holds it (PUBLIC_REPO): its CI pushes
# the image ghcr.io/<PUBLIC_REPO>, the one install.sh pulls, and the Mac
# download goes on its release.
public="$(sed -n 's/^PUBLIC_REPO="\(.*\)"$/\1/p' install.sh)"
if [ -z "$public" ]; then
  echo "install.sh names no PUBLIC_REPO: no address to print for the image and the Mac download." >&2
  exit 1
fi
if git rev-parse "v$want" >/dev/null 2>&1; then
  echo "v$want already exists. A tag is a promise; it does not move." >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty: the tag would not describe what you tested." >&2
  git status --short >&2
  exit 1
fi
# The CHANGELOG's newest entry is this release's, under its own heading. This
# script changes VERSION, pyproject.toml and install.sh, never the CHANGELOG's
# text, and v1.0.17 was tagged with "## 1.0.17 (Unreleased)" on top: the
# public copy carried it, and only the security reading before the push saw it.
newest="$(grep -m1 '^## ' CHANGELOG.md || true)"
if [ "$newest" != "## $want" ]; then
  echo "CHANGELOG.md's newest entry is \"$newest\", not \"## $want\": write the release's heading (no \"(Unreleased)\") and commit it first." >&2
  exit 1
fi

echo "$want" > VERSION
python3 - "$want" <<'PY'
import pathlib, re, sys
want = sys.argv[1]
p = pathlib.Path("pyproject.toml"); s = p.read_text()
p.write_text(re.sub(r'^version = "[^"]+"', f'version = "{want}"', s, count=1, flags=re.M))
PY
# The release install.sh installs, and the sha256 of the files it downloads,
# as they are in this commit (the tag's). The -dev bump below leaves them, so
# the copy on main keeps installing this release until the next tag.
python3 scripts/stamp_installer.py release "$want"

echo "== the sources agree =="
# `make image` first: the test image is named after the lock it installs, and
# `korely-agent-dev` alone is whichever one was built last.
make -s image
docker run --rm -v "$here":/w -w /w korely-agent-dev python scripts/check_the_version.py

echo "== the tests pass =="
# In 4 processes, then the tests marked `serial` in one (scripts/test_all.sh):
# every test runs once and both passes must pass. The parallel run gives the
# same result test by test (tests/test_the_suite_runs_in_several_processes.py,
# and every gate since 7 Nov 2026 runs it so); 4 is the Makefile's measured
# choice (CLAUDE.md). The tests workflow runs the whole suite in one process,
# on another machine: the second check, independent of this one. It runs on
# every tag of the public repository; in the private one, whose Actions
# minutes are counted, a tag skips it, and it runs every Sunday night and by
# hand (.github/workflows/tests.yml, since 17 Nov 2026). So in the private
# repository this run is the one the tag rests on.
#
# On a database of the release's own, named here whatever TEST_DB the shell
# has, made by `make test-db` and dropped at the end, however the script ends
# (`make test-db-drop`). The serial pass runs on TEST_DB itself, and on the
# shared korely_test, the Makefile's default, it met a table another
# branch had left there: 3,438 errors (13 Nov 2026). The workers' databases,
# <TEST_DB>_gw<n>, the suite drops itself.
TEST_DB="korely_test_release_${want//./_}_$$"
export TEST_DB
trap 'make -s test-db-drop || echo "the release left its test database: make test-db-drop TEST_DB=$TEST_DB" >&2' EXIT
echo "on a test database of its own, $TEST_DB"
make test-all WORKERS=4

echo "== the image says the same number =="
./scripts/check_the_image_knows_its_version.sh "korely-agent:release-$want"

echo "== the image installed exactly the lock =="
make -s image-check IMAGE="korely-agent:release-$want"

# docs/DEPENDENCIES.md is the file handed to a security review, read off this
# image and the dashboard lockfile. Nothing checked it before a tag: 0.1.20
# was tagged with the dashboard of React Router 6 still listed after the move
# to 7 (found on 7 Oct). `make licences-write` regenerates it.
echo "== the licences file describes what ships =="
docker run --rm --entrypoint python -v "$here":/w -w /w "korely-agent:release-$want" \
  scripts/check_dependency_licences.py --self-test
docker run --rm --entrypoint python -v "$here":/w -w /w "korely-agent:release-$want" \
  scripts/check_dependency_licences.py

# The checks of the parts of the product the Cloud's tree alone carries, on
# the same image (scripts/release_cloud_checks.sh); the public repository has
# none.
if [ -f scripts/release_cloud_checks.sh ]; then
  sh scripts/release_cloud_checks.sh "korely-agent:release-$want"
fi

git add VERSION pyproject.toml install.sh
git commit -m "release $want" >/dev/null
git tag -a "v$want" -m "korely-agent $want"

# After the tag, the checkout stops being the release. Saying so is the
# difference between `cat VERSION` answering a question and answering the same
# thing in two situations the README puts side by side.
next="$(python3 -c "
import sys
a, b, c = '$want'.split('.')
print(f'{a}.{b}.{int(c) + 1}-dev')")"
echo "$next" > VERSION
python3 - "$next" <<'PY'
import pathlib, re, sys
p = pathlib.Path("pyproject.toml"); s = p.read_text()
p.write_text(re.sub(r'^version = "[^"]+"', f'version = "{sys.argv[1]}"', s, count=1, flags=re.M))
PY
git add VERSION pyproject.toml
git commit -m "open $next" >/dev/null

echo
echo "tagged v$want; the checkout is now $next"
echo "push it with:  git push origin main --tags"
echo
echo "then, once CI has pushed ghcr.io/$public:$want, pin install.sh to its digest:"
echo "  docker buildx imagetools inspect ghcr.io/$public:$want   # the Digest: line"
echo "  python3 scripts/stamp_installer.py digest sha256:<that digest>"
echo "  git commit -am \"install.sh: the digest of $want\" && git push origin main"
echo "(until then install.sh pulls $want by its tag, and says so)"
echo
# The Mac download (29 Oct 2026): Korely Installer.app, which bundles the
# install.sh of the tag (the script checks it), signed with the Developer ID
# and notarized. Built on a Mac whose keychain has the identity and, for the
# notarization, the notarytool profile (KORELY_NOTARY_PROFILE, default korely:
# `xcrun notarytool store-credentials korely --apple-id <id> --team-id <team>`,
# once, by the account holder). Without the profile it prints the commands.
echo "and the Mac download, on a Mac with the Developer ID, attached to the GitHub release:"
echo "  scripts/make_mac_installer.sh $want          # signs, notarizes, staples dist/Korely-Installer-$want.dmg"
echo "  gh release upload v$want dist/Korely-Installer-$want.dmg --repo $public"
echo "(the README links https://github.com/$public/releases/latest/download/Korely-Installer.dmg:"
echo " upload a copy under that name too, with --clobber, so the link follows the newest release)"
