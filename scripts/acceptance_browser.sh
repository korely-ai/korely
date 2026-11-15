#!/usr/bin/env bash
# Drive the dashboard with a real browser.
#
#   OPERATOR_PASSWORD=<password> WRITER=none bash scripts/acceptance_browser.sh https://your-host operator
#
# WRITER says whether the install has a model that writes: "configured", or
# "none", when the dashboard's first screen comes after the login and the
# checks press its Skip for now, as a person does (browser_sign_in.mjs).
# acceptance.sh asks the install (GET /admin/models).
#
# The password may also come third on the command line, where `ps` shows it to
# every account on the machine; the environment keeps it to yours.
#
# Separate from acceptance.sh because it needs a browser, and acceptance.sh
# must run anywhere curl runs. It is not optional in spirit: the rest of the
# suite verified that the page is served and that every endpoint it names
# exists, and on 2026-09-12 both were true while the login was broken and
# three pages rendered blank. curl cannot find that.
set -eu

BASE="${1:?usage: OPERATOR_PASSWORD=<password> acceptance_browser.sh <base-url> <username>}"
OPU="${2:?}"
OPP="${3:-${OPERATOR_PASSWORD:?no password: OPERATOR_PASSWORD=<password>, or a third argument}}"
WRITER="${WRITER:?WRITER=configured or WRITER=none: whether the install has a model that writes}"
IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright:v1.56.0-noble}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Playwright from scripts/package-lock.json, the one place its version is
# written, installed in the container and gone with it. The directory is
# mounted read-only so nothing is ever installed into the repository.
#
# Two checks, one install: every page after a real login
# (check_dashboard_in_a_browser.mjs), then the modals, which a repair once
# froze so that their fields could be neither filled in nor closed
# (check_modals_in_a_browser.mjs). acceptance.sh runs this; until 17 Oct 2026
# nothing did, and neither caught anything (audit of 17 Oct 2026).
# The credentials go in as environment variables, never spliced into the
# command line the container's shell parses. `-e NAME` without a value takes
# it from this environment: `-e OPP="$PASS"` and the node arguments put the
# password on two command lines `ps` shows to every account on the test
# machine (audit of 17 Oct 2026).
export BASE OPU OPP WRITER
docker run --rm --network host \
  -v "$HERE":/src:ro \
  -e BASE -e OPU -e OPP -e WRITER \
  -w /tmp "$IMAGE" \
  sh -c 'cp /src/package.json /src/package-lock.json /src/check_dashboard_in_a_browser.mjs \
            /src/check_modals_in_a_browser.mjs /src/browser_sign_in.mjs /tmp/ \
         && npm ci --silent >/dev/null 2>&1 \
         && node /tmp/check_dashboard_in_a_browser.mjs \
         && node /tmp/check_modals_in_a_browser.mjs'
