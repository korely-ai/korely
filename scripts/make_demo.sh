#!/usr/bin/env bash
# Render the README's demo again: docs/assets/demo.gif and docs/assets/demo.mp4.
#
#   bash scripts/make_demo.sh
#
# One command, Docker only, no model key and no paid call. It installs this
# checkout with install.sh (its HEAD: commit a dashboard change first), writes
# the README's story through /v1 with no model (scripts/make_demo.mjs: two
# memories, two facts, the old one closed on the day it stopped being true),
# records the dashboard frame by frame with Playwright, converts the frames
# with ffmpeg, and removes the install, its volumes and its image.
#
# Environment, all optional:
#   KORELY_DEMO_PORT     the port on 127.0.0.1 (default 8124)
#   KORELY_DEMO_PROJECT  the Compose project (default korely-demo)
#   KORELY_DEMO_STILLS   a folder to copy the three key frames into (PNG)
#   KORELY_DEMO_KEEP=1   leave the install running (its folder is printed)
#   PLAYWRIGHT_IMAGE, FFMPEG_IMAGE   the two images used
#
# Never touches another Compose project: the cleanup is
# `docker compose down -v --rmi local` inside the folder install.sh made.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PORT="${KORELY_DEMO_PORT:-8124}"
PROJECT="${KORELY_DEMO_PROJECT:-korely-demo}"
STILLS="${KORELY_DEMO_STILLS:-}"
KEEP="${KORELY_DEMO_KEEP:-}"
PLAYWRIGHT_IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright:v1.56.0-noble}"
# Published for linux/amd64 only: on an arm64 machine Docker emulates it.
FFMPEG_IMAGE="${FFMPEG_IMAGE:-jrottenberg/ffmpeg:7.1-alpine320}"
ASSETS="$ROOT/docs/assets"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/korely-demo.XXXXXX")"
DIR="$WORK/install"
FRAMES="$WORK/frames"
mkdir -p "$FRAMES" "$ASSETS"

cleanup() {
  if [ -n "$KEEP" ]; then
    echo "The install is still running in $DIR (KORELY_DEMO_KEEP=1)." >&2
    echo "Remove it: cd \"$DIR\" && docker compose down -v --rmi local && rm -rf \"$WORK\"" >&2
    return
  fi
  if [ -f "$DIR/docker-compose.yml" ]; then
    (cd "$DIR" && docker compose down -v --rmi local >/dev/null 2>&1) || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "==> Installing this checkout on 127.0.0.1:$PORT (no model key)" >&2
# The model key is never read from this shell: install.sh takes it from
# KORELY_PROVIDER_KEY alone, and here that is cleared.
env -u KORELY_PROVIDER_KEY sh "$ROOT/install.sh" --from-local "$ROOT" --dir "$DIR" \
  --project "$PROJECT" --port "$PORT" --save-credentials >/dev/null

field() { awk -v k="$1" 'index($0, k) == 1 { print $NF }' "$DIR/credentials.txt"; }
OPU="$(field login)"
OPP="$(field password)"
KEY="$(field 'api key')"
[ -n "$OPU" ] && [ -n "$OPP" ] && [ -n "$KEY" ] || { echo "make_demo.sh: no credentials in $DIR" >&2; exit 1; }
BASE="http://127.0.0.1:$PORT"

echo "==> Writing the story and recording the dashboard" >&2
# Playwright from scripts/package-lock.json, installed in the container and
# gone with it; scripts/ is mounted read-only. The credentials go in as
# environment variables, never on a command line `ps` shows.
export BASE OPU OPP KEY
docker run --rm --network host \
  -v "$HERE":/src:ro -v "$FRAMES":/out \
  -e BASE -e OPU -e OPP -e KEY -e OUT=/out \
  -w /tmp "$PLAYWRIGHT_IMAGE" \
  sh -c 'cp /src/package.json /src/package-lock.json /src/make_demo.mjs /tmp/ \
         && npm ci --silent >/dev/null 2>&1 \
         && node /tmp/make_demo.mjs'

echo "==> Converting" >&2
ff() { docker run --rm --platform linux/amd64 -v "$FRAMES":/f:ro -v "$ASSETS":/o -w /f "$FFMPEG_IMAGE" \
         -hide_banner -loglevel error -y -f concat -safe 0 -i frames.txt "$@"; }
# The frames are 1760x1040 (the 880x520 frame at 2x): the MP4 keeps them, for
# sharp text on a high-density screen. Cut at the length the frames add up to:
# the fps filter otherwise stretches the last frame by the one before it.
LENGTH="$(awk '$1 == "duration" { s += $2 } END { printf "%.3f", s }' "$FRAMES/frames.txt")"
ff -vf "fps=30,format=yuv420p" -t "$LENGTH" -c:v libx264 -preset slow -crf 20 -tune stillimage \
   -movflags +faststart /o/demo.mp4
# The GIF at README width, 1:1. A held frame is one GIF frame with its delay
# (-fps_mode vfr), and no dithering: the screen is flat colours and text, and
# dithering only adds noise that changes between frames.
ff -filter_complex "scale=880:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=full[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
   -fps_mode vfr /o/demo.gif

if [ -n "$STILLS" ]; then
  mkdir -p "$STILLS"
  cp "$FRAMES"/still-1.png "$FRAMES"/still-2.png "$FRAMES"/still-3.png "$STILLS"/
fi

size() { wc -c < "$1" | awk '{ printf "%.2f MB", $1 / 1048576 }'; }
echo "docs/assets/demo.gif  $(size "$ASSETS/demo.gif")"
echo "docs/assets/demo.mp4  $(size "$ASSETS/demo.mp4")"
[ -n "$STILLS" ] && echo "stills in $STILLS"
exit 0
