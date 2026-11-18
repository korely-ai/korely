#!/usr/bin/env bash
# Render the README's install recording again: docs/assets/install.gif and
# docs/assets/install.mp4.
#
#   bash scripts/make_install_demo.sh            # before the public release
#   bash scripts/make_install_demo.sh --public   # once the repo and the image are public
#
# One command, no model key and no paid call. It records a real run of the
# README's line,
#
#   curl -fsSL https://raw.githubusercontent.com/korely-ai/korely/main/install.sh | sh
#
# pasted into zsh on this machine (scripts/record_terminal.py, a pseudo-terminal
# and the real Docker), renders it with xterm.js in Playwright and opens the
# printed address in the same browser (scripts/make_install_demo.mjs), converts
# the frames with ffmpeg, and removes the install with its own
# `./korely uninstall --yes`, which also removes the image.
#
# Before the release (the default) the line cannot reach GitHub: the repository
# and the image are not public. The run is then the same line with two
# stand-ins (demo_standins in scripts/terminal_demo_lib.sh), and nothing else
# changed:
#   - curl, first on the recorded shell's PATH, answers the addresses under
#     https://raw.githubusercontent.com/<PUBLIC_REPO>/ from a folder
#     laid out as GitHub would serve them: main/install.sh is this checkout's
#     install.sh stamped as scripts/release.sh stamps it (THIS_RELEASE and the
#     sha256 of the release files), v<release>/... the release files of this
#     checkout. Every other call goes to the real curl (the port check, /health).
#   - the image ghcr.io/<PUBLIC_REPO>:<release> is built here from this
#     checkout's HEAD with VERSION set to the release, as release.sh sets it.
#     It is on the machine before the run, so the 2.5 GB download a user waits
#     for is not in the recording.
# --public drops both: the real curl, GitHub, and the image pulled from ghcr.io.
#
# The run's login, password and API key are on screen: they are throwaway, the
# install is deleted at the end.
#
# Environment, all optional:
#   KORELY_DEMO_PORT     the port on 127.0.0.1 (default 8125), given to the run
#                        as KORELY_PORT, so the line on screen stays the README's
#   KORELY_DEMO_PROJECT  the Compose project (default korely-install-demo), as KORELY_PROJECT
#   KORELY_DEMO_STILLS   a folder to copy the two stills into (the pasted line, the printout)
#   KORELY_DEMO_KEEP=1   leave the install running (its folder is printed)
#   PLAYWRIGHT_IMAGE, FFMPEG_IMAGE   the two images used (scripts/make_demo.sh's)
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
. "$HERE/terminal_demo_lib.sh"
PUBLIC=""
case "${1:-}" in
  --public) PUBLIC=1 ;;
  "") ;;
  *) echo "usage: bash scripts/make_install_demo.sh [--public]" >&2; exit 2 ;;
esac
PORT="${KORELY_DEMO_PORT:-8125}"
PROJECT="${KORELY_DEMO_PROJECT:-korely-install-demo}"
STILLS="${KORELY_DEMO_STILLS:-}"
KEEP="${KORELY_DEMO_KEEP:-}"
PLAYWRIGHT_IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright:v1.56.0-noble}"
FFMPEG_IMAGE="${FFMPEG_IMAGE:-jrottenberg/ffmpeg:7.1-alpine320}"
ASSETS="$ROOT/docs/assets"
PUBLIC_REPO="$(demo_public_repo)"   # scripts/terminal_demo_lib.sh, from install.sh
RAW="https://raw.githubusercontent.com/$PUBLIC_REPO"
LINE="curl -fsSL $RAW/main/install.sh | sh"
REAL_CURL="$(command -v curl)"

# A short folder: the install's path is printed in the recording.
WORK="$(mktemp -d /tmp/korely.XXXXXX)"
WORK="$(cd -P "$WORK" && pwd -P)"
DEMO="$WORK/.demo"           # everything but the install
DIR="$WORK/korely"           # where the line installs: ./korely of the shell's folder
FRAMES="$DEMO/frames"
mkdir -p "$FRAMES" "$ASSETS"
BUILT=""

cleanup() {
  if [ -n "$KEEP" ]; then
    echo "The install is still running in $DIR (KORELY_DEMO_KEEP=1)." >&2
    echo "Remove it: \"$DIR/korely\" uninstall --yes && rm -rf \"$WORK\"" >&2
    return
  fi
  if [ -x "$DIR/korely" ]; then
    "$DIR/korely" uninstall --yes >/dev/null 2>&1 || echo "make_install_demo.sh: the uninstall failed, see $DIR" >&2
  fi
  # The image this script built, if the uninstall did not take it.
  if [ -n "$BUILT" ] && docker image inspect "$BUILT" >/dev/null 2>&1; then
    docker image rm "$BUILT" >/dev/null 2>&1 || echo "make_install_demo.sh: docker image rm $BUILT failed" >&2
  fi
  [ -d "$DIR" ] || rm -rf "$WORK"
}
trap cleanup EXIT

if [ -n "$PUBLIC" ]; then
  RELEASE="$("$REAL_CURL" -fsSL "$RAW/main/install.sh" | sed -n 's/^THIS_RELEASE="\(.*\)"$/\1/p')"
else
  RELEASE="$(sed -n 's/^THIS_RELEASE="\(.*\)"$/\1/p' "$ROOT/install.sh")"
fi
[ -n "$RELEASE" ] || { echo "make_install_demo.sh: no THIS_RELEASE in install.sh" >&2; exit 1; }
IMAGE="ghcr.io/$PUBLIC_REPO:$RELEASE"
# The uninstall at the end removes the image: one that was here before is
# somebody's, and is not this script's to remove.
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "make_install_demo.sh: $IMAGE is already on this machine, and the uninstall would remove it." >&2
  echo "Remove it first (docker image rm $IMAGE) if it is not in use." >&2
  exit 1
fi

if [ -z "$PUBLIC" ]; then
  demo_standins   # scripts/terminal_demo_lib.sh: sets BUILT and RUN_PATH
else
  RUN_PATH="$PATH"
fi

echo "==> Recording: $LINE" >&2
# The recorded shell gets a clean environment: no model key and nothing of
# this shell's but what Docker needs to be found.
set +e
env -i HOME="$HOME" USER="${USER:-}" PATH="$RUN_PATH" TERM=xterm-256color LANG=en_US.UTF-8 \
  ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} ${DOCKER_CONTEXT:+DOCKER_CONTEXT="$DOCKER_CONTEXT"} \
  KORELY_PORT="$PORT" KORELY_PROJECT="$PROJECT" \
  python3 "$HERE/record_terminal.py" --out "$DEMO/run.cast" --cwd "$WORK" --line "$LINE" \
    --cols 100 --rows 32 --pause-before 0.4
status=$?
set -e
[ -f "$DEMO/standin.log" ] && sed 's/^/    stand-in: /' "$DEMO/standin.log" >&2
if [ "$status" -ne 0 ]; then
  echo "make_install_demo.sh: the install exited $status; the recording is $DEMO/run.cast" >&2
  KEEP=1
  exit 1
fi

# The printout's own words, read the way a user reads them.
field() { sed -n "s/^  $1 *//p" "$DEMO/run.cast.txt" | head -n 1; }
python3 -c '
import json, sys
lines = open(sys.argv[1]).read().splitlines()[1:]
sys.stdout.write("".join(e[2] for e in map(json.loads, lines) if e[1] == "o"))
' "$DEMO/run.cast" | tr -d '\r' > "$DEMO/run.cast.txt"
OPU="$(field Login)"
OPP="$(field Password)"
BASE="$(field Dashboard)"
[ -n "$OPU" ] && [ -n "$OPP" ] && [ -n "$BASE" ] || { echo "make_install_demo.sh: no login in the printout" >&2; exit 1; }

echo "==> Rendering" >&2
demo_render "$DEMO/run.cast" "$FRAMES" BASE="$BASE" OPU="$OPU" OPP="$OPP"

echo "==> Converting" >&2
LENGTH="$(demo_convert "$FRAMES" install)"

if [ -n "$STILLS" ]; then
  mkdir -p "$STILLS"
  cp "$FRAMES/still-1.png" "$FRAMES/still-2.png" "$FRAMES/timeline.json" "$STILLS"/
  cp "$DEMO/run.cast" "$STILLS/install.cast"
fi

echo "docs/assets/install.gif  $(demo_size "$ASSETS/install.gif")"
echo "docs/assets/install.mp4  $(demo_size "$ASSETS/install.mp4")"
echo "played: ${LENGTH} s"
[ -n "$STILLS" ] && echo "stills in $STILLS"
exit 0
