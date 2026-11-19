#!/bin/sh
# The test Postgres's own settings, applied by `make db-up`:
#     sh dev/test_db_settings.sh <the compose command of the test database>
#
# Postgres's default of 100 connections does not hold a run in several
# processes beside the others on this machine: one process of the suite keeps
# about a dozen open (its engine's pool and the application's two), and one
# run in 4 processes beside two in one took 80 of the 100 (6 Nov 2026). The
# server allows 300, and shared_buffers grows from the default 128MB to 256MB
# for the busier server, a small part of what Docker has: the processes of the
# suites need the rest.
#
# Set with ALTER SYSTEM, which the server keeps in its own data directory, and
# not with `command:` in dev/docker-compose.test.yml: every checkout on the
# machine (the worktrees, the other sessions) shares this one container, and
# `docker compose up` recreates a container whose compose file has changed, so
# two checkouts with different files would recreate it in the middle of each
# other's runs, back and forth, until every one had the same file.
#
# Both settings take effect at the server's next start, and this never starts
# it again: a restart in the middle of another run cuts its connections, and
# nothing here can tell whether a run is under way. It used to restart when no
# other session was connected, and a suite in its first tests, before any of
# them reaches the database, has none: its next tests found the database
# shutting down (review of 7 Nov 2026, 2 tries out of 2). So it says what the
# next start will bring and how to start it, for whoever knows the machine is
# idle, and goes on.
set -eu
COMPOSE=$*
CONNECTIONS=300
BUFFERS=256MB

q() { $COMPOSE exec -T db psql -U korely -d postgres -tAc "$1"; }

[ "$(q 'SHOW max_connections')" = "$CONNECTIONS" ] && \
	[ "$(q 'SHOW shared_buffers')" = "$BUFFERS" ] && exit 0

q "ALTER SYSTEM SET max_connections = $CONNECTIONS" >/dev/null
q "ALTER SYSTEM SET shared_buffers = '$BUFFERS'" >/dev/null
echo "test database: max_connections=$CONNECTIONS and shared_buffers=$BUFFERS are set for its next" \
	"start. When no suite on this machine is using it (no korely-agent-dev container in" \
	"\`docker ps\`), start it again with: $COMPOSE restart db" >&2
