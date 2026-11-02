#!/usr/bin/env bash
# Render the README's hand-drawn diagrams again: every
# docs/assets/diagrams/<name>.excalidraw to docs/assets/<name>.svg.
#
#   bash scripts/make_diagrams.sh                     # all of them
#   bash scripts/make_diagrams.sh what-runs ...       # only these
#   bash scripts/make_diagrams.sh --from-scenes       # first rebuild the .excalidraw
#                                                     # files from scripts/diagrams/scenes.mjs
#
# One command, Docker only, no key and no network but npm's. The .excalidraw
# files are the source: open one on excalidraw.com, edit it, save it over the
# file, and run this. --from-scenes overwrites them with scenes.mjs's skeletons
# instead (the way they were first made), so use one or the other.
#
# Excalidraw's own exportToSvg draws them (@excalidraw/excalidraw, pinned in
# scripts/diagrams/package-lock.json), in the Playwright image's Chromium, and
# writes Excalifont into each SVG as a data URI: GitHub shows an SVG as <img>,
# which loads no font from outside the file. scripts/diagrams/render.mjs then
# checks it: each SVG is opened as an <img> with the network off, and must look
# different once its @font-face is removed. Any failure exits non-zero.
#
# Environment, all optional:
#   KORELY_DIAGRAMS_PREVIEWS  a folder to write a PNG of each into, at the
#                             README's 880 px, on GitHub's dark background
#   PLAYWRIGHT_IMAGE          the image (scripts/make_demo.sh's)
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PLAYWRIGHT_IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright:v1.56.0-noble}"
PREVIEWS="${KORELY_DIAGRAMS_PREVIEWS:-}"
mkdir -p "$ROOT/docs/assets/diagrams"

PREVIEW_MOUNT=""
PREVIEW_ARG=""
if [ -n "$PREVIEWS" ]; then
  mkdir -p "$PREVIEWS"
  PREVIEWS="$(cd "$PREVIEWS" && pwd)"
  PREVIEW_MOUNT="-v $PREVIEWS:/previews"
  PREVIEW_ARG="--previews /previews"
fi

# The packages are installed in the container and gone with it (--ignore-scripts:
# none of them needs an install script here); scripts/diagrams is mounted
# read-only, docs/assets is where the files land, written as this user.
# shellcheck disable=SC2086
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$HERE/diagrams":/src:ro -v "$ROOT/docs/assets":/assets $PREVIEW_MOUNT \
  -w /tmp "$PLAYWRIGHT_IMAGE" \
  sh -c 'mkdir -p /tmp/w && cp /src/package.json /src/package-lock.json /src/entry.js /src/render.mjs /src/scenes.mjs /tmp/w/ \
         && cd /tmp/w && npm ci --ignore-scripts --no-audit --no-fund --silent >/dev/null \
         && node render.mjs --diagrams /assets/diagrams --assets /assets '"$PREVIEW_ARG"' "$@"' \
  sh "$@"
