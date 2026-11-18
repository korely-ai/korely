#!/bin/sh
# Runs a command from a copy of the checkout made inside the container, never
# from the checkout itself:
#
#     docker run -v <checkout>:/src:ro -w /w -e PYTHONPATH=/w <test image> \
#         sh /src/scripts/from_a_copy.sh <command> [<argument>...]
#
# as `make test`, `make test-all`, `make migrate-check` and
# scripts/make_public_repo_check.sh run the suite and the migrations. The
# checkout is mounted read-only at /src, copied into /w, the container's own
# folder, and the command runs there. (COPY_FROM and COPY_TO move the two
# folders, for this script's test.)
#
# Why: on a Mac the checkout reaches a container through Docker Desktop's
# file sharing, and there a process whose working directory is the checkout
# can find os.getcwd() failing for a moment (FileNotFoundError, no file
# named) while any container on the machine starts or stops, whatever it
# mounts. A probe in the test image (8 Nov 2026) counted 67 such failures,
# windows of 1 to 210 ms, in 17 of 21 phases of starting other containers,
# and none with nothing starting nor while files were written. Every Python
# process the suite starts there with a relative entry on its path calls it
# (`python -c` has '', alembic.ini adds '.'; alembic five times as it
# starts), and a process that called it in such a moment died: one of the
# 133 alembic processes of test_every_downgrade_gives_back_the_schema_before_it
# in a gate of 8 Nov 2026. In the copy no process of the run has its working
# directory in the shared folder, and tar reads the checkout from the folder
# above it (`-C`), so not even the copy does. The checkout is mounted whole:
# a file mounted over a path of the shared folder is detached the same way
# when another container starts, and the container reads the file under it
# again.
#
# Left out of the copy: .git (no step reads it; the test image has no git,
# and a worktree's .git only names a folder of the host) and .claude (other
# worktrees and local settings, 700 MB in the main checkout); node_modules and
# dashboard/dist, what `npm ci` and the dashboard's build make (the suite reads
# the dashboard's sources as text, and `make dashboard-test` runs them); the
# bytecode and pytest caches. Everything else is copied, files git does not
# track included: the suite reads the tree as it is on disk. Nothing the run
# writes reaches the checkout.
set -eu
set -o pipefail
from=${COPY_FROM:-/src}
to=${COPY_TO:-/w}
up=$(dirname "$from")
name=$(basename "$from")
mkdir -p "$to"
tar -C "$up" -cf - --exclude="$name/.git" --exclude="$name/.claude" --exclude=node_modules \
    --exclude="$name/dashboard/dist" --exclude=__pycache__ --exclude=.pytest_cache "$name" \
  | tar -C "$to" --strip-components=1 -xf -
cd "$to"
exec "$@"
