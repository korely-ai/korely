#!/bin/sh
# Migrate, then serve. Refuse to serve if migrating fails.
#
# `set -e` is the whole point: the codebase this came from ran the upgrade and
# started the server regardless, so a failed migration produced a running
# service on a half-built schema, which is worse than a container that will not
# start. A crash loop is a clear signal. Silent corruption is not.
set -e

if [ -z "$DATABASE_URL" ]; then
  echo "DATABASE_URL is not set. Under docker-compose.yml it is built from" >&2
  echo "POSTGRES_PASSWORD in your .env, so the thing to set is that one." >&2
  exit 1
fi

# The compose file falls back to a literal `change-me` when POSTGRES_PASSWORD is
# empty, which is how an install ends up on a password that is written down in
# public. Postgres is not published outside the compose network, so this is not
# an open door; it is a default that nobody chose, on a machine that is on the
# internet, and the cost of refusing is one line in a .env file.
# The password travels apart from the URL under compose (POSTGRES_PASSWORD,
# audit of 17 Oct 2026), so both are looked at.
case "$DATABASE_URL|$POSTGRES_PASSWORD" in
  *:change-me@*|*"|change-me")
    echo "POSTGRES_PASSWORD is not set, so the database would come up on the" >&2
    echo "default password. Put something in it before the first start:" >&2
    echo "  openssl rand -hex 16" >&2
    echo "Changing it later does not rotate anything, it locks you out." >&2
    exit 1
    ;;
esac

# The address clients reach this install at, for the OAuth and MCP metadata
# and the 401s that point at them (RFC 9728 section 3.3 and RFC 8414 section
# 3.3: what is advertised must be the URL the client used). It defaulted to
# http://localhost:8000 on every install, so ChatGPT and Claude were sent to
# localhost and the connection the 0.1.37 notes called "nothing to do" could
# not be completed (audit of 17 Oct 2026). With DOMAIN or HTTPS_PORT set (the
# shipped compose passes both, as Caddy gets them, since 18 Oct 2026: before,
# HTTPS_PORT came only from a .env that set it) and no
# PUBLIC_BASE_URL, it is the address Caddy serves: https, the name (Caddy's own
# default, localhost, when DOMAIN is empty: `{$DOMAIN:localhost}` in the
# Caddyfile), and the port only when it is not 443. The release acceptance run of
# 0.1.40 found an install without DOMAIN still advertising localhost:8000.
if [ -z "$PUBLIC_BASE_URL" ] && { [ -n "$DOMAIN" ] || [ -n "$HTTPS_PORT" ]; }; then
  SITE="${DOMAIN:-localhost}"
  case "${HTTPS_PORT:-443}" in
    443) PUBLIC_BASE_URL="https://$SITE" ;;
    *)   PUBLIC_BASE_URL="https://$SITE:$HTTPS_PORT" ;;
  esac
  export PUBLIC_BASE_URL
fi

# Its own words on stderr: a command run through it (restore.sh's
# `manage erasures-export`) must print on stdout nothing but its own output.
# On stdout they went into the erasures file, the replay refused it, and the
# restore stopped with the erased people back (the acceptance run of 0.1.40).
echo "Applying migrations..." >&2
alembic upgrade head
# Then the chains of the parts of the product the Cloud's package carries,
# each with its own version table (korely_agent/db/chains.py); none on an
# install of one's own.
# The list is read apart and its failure stops the start: inside `$( )`
# `set -e` does not reach, and a list that failed was no chain at all.
if ! chains=$(python -m korely_agent.db.chains --list); then
  echo "The chains of migrations could not be listed (python -m korely_agent.db.chains --list failed): not serving on a database they may not have reached." >&2
  exit 1
fi
for chain in $chains; do
  alembic -c "$chain" upgrade head
done
echo "Migrations applied." >&2

exec "$@"
