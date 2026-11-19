#!/bin/sh
# `make test-all`, inside the test image, on the database TEST_DATABASE_URL
# names (the Makefile's TEST_DB).
#
# WORKERS unset or empty: the whole suite in one process, as it always ran.
# The tests workflow (.github/workflows/tests.yml: a public repository's
# tags, a private one's Sunday run, by hand) runs this way;
# scripts/release.sh runs WORKERS=4 (since 13 Nov 2026).
#
# WORKERS=N: the suite in N processes (pytest-xdist), each on a database of
# its own next to TEST_DB, made and dropped by the suite (tests/conftest.py,
# `own_database`); then the tests marked `serial` (pyproject.toml), which need
# the machine to themselves, in one process on TEST_DB. Both passes always run
# and both must pass: the exit status is the first pass's failure, else the
# second's. A serial pass with nothing to run (pytest's 5) is not a failure.
#
# WORKERS reaches here from the environment, never pasted into a command line,
# and anything but a whole number of processes is refused.
#
# A Ctrl-C (or `docker stop`, which `make test-all` has send the same SIGINT)
# stops the run: the container's init (`docker run --init`) hands the signal
# to every process of the run (TINI_KILL_PROCESS_GROUP), pytest's and its
# workers' included, which end as on a Ctrl-C at a terminal. Here it is only
# remembered: the serial pass does not start after a stopped parallel one,
# and the exit status is the signal's. Tried on 7 Nov 2026, a run in two
# processes stopped after half a minute: 130, no serial pass, no database
# left behind.
set -u
stopped=
trap 'stopped=130' INT
trap 'stopped=143' TERM

if [ -z "${WORKERS:-}" ]; then
	exec python -m pytest -q
fi

case $WORKERS in
	*[!0-9]* | 0*)
		echo "refused: WORKERS=\"$WORKERS\" must be a whole number of processes, 1 or more" >&2
		exit 2 ;;
esac

# One test at a time, not in runs: xdist's default (`--dist load`) hands each
# worker runs of consecutive tests, a quarter of what is left per worker, and
# the slowest tests of the suite sit side by side (two of about a minute in
# tests/test_no_read_loads_a_whole_table.py, the 44 s of
# test_no_read_builds_the_wall_by_hand.py next to them), so one worker went on
# alone with them for 3 to 7 minutes after the others had finished (runs of
# 6 Nov 2026; `--dist worksteal`, which splits the suite in equal runs and lets
# a worker that runs out take tests from another's queue, still left 3.5).
# With --maxschedchunk 1 a worker is given the next test when it is ready for
# it (pytest-xdist's docs), so the slow ones go to different workers.
python -m pytest -q -n "$WORKERS" --maxschedchunk 1 -m "not serial"
parallel=$?
if [ -n "$stopped" ]; then
	echo "test-all with $WORKERS workers: stopped; the serial pass does not run" >&2
	exit "$stopped"
fi
python -m pytest -q -m serial
alone=$?
[ -n "$stopped" ] && exit "$stopped"
[ "$alone" -eq 5 ] && alone=0
echo "test-all with $WORKERS workers: the parallel pass exited $parallel, the serial pass $alone"
[ "$parallel" -ne 0 ] && exit "$parallel"
exit "$alone"
