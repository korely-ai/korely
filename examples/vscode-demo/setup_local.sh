#!/bin/sh
# Step 1: the store, on this machine.
#
# What docs/guide/install.md does, with the choices a laptop demo wants made for
# it: no DOMAIN, so the agents and the browser use the loopback address and no
# certificate is involved ("On a laptop" in docs/guide/install.md); a port that is not
# 8000, which a developer's laptop usually has taken; Caddy's ports moved off
# 443 and 80 for the same reason; and secrets nobody has to see.
#
#   sh setup_local.sh                       # port 8765
#   KORELY_DEMO_PORT=8900 sh setup_local.sh
#   GOOGLE_API_KEY=... sh setup_local.sh    # or paste the key in Settings later
#
# An install that already has a .env is used as it is.
set -e
cd "$(dirname "$0")/../.."

# Rewrite one KEY=value line (the same helper as scripts/timed_install.sh:
# `sed -i` means two different things on macOS and Linux).
setconf() {
  tmp=$(mktemp)
  awk -v k="$1" -v v="$2" '
    index($0, k "=") == 1 { print k "=" v; found=1; next }
    { print }
    END { if (!found) print k "=" v }
  ' .env > "$tmp" && mv "$tmp" .env
}

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  setconf POSTGRES_PASSWORD "$(openssl rand -hex 16)"
  setconf SECRET_KEY "$(openssl rand -hex 32)"
  setconf DOMAIN ""
  setconf PORT "${KORELY_DEMO_PORT:-8765}"
  setconf HTTPS_PORT 8443
  setconf HTTP_PORT 8081
  [ -n "$GOOGLE_API_KEY" ] && setconf GOOGLE_API_KEY "$GOOGLE_API_KEY"
  echo "wrote .env (port ${KORELY_DEMO_PORT:-8765}, no DOMAIN, secrets generated)"
else
  echo "using the .env already in $(pwd)"
fi

PORT=$(awk -F= '/^PORT=/{print $2}' .env)
docker compose up -d --build

printf 'waiting for the store'
i=0
until curl -fs -o /dev/null "http://127.0.0.1:${PORT}/health"; do
  i=$((i + 1))
  [ "$i" -gt 150 ] && { echo; echo "no answer after 5 minutes: docker compose logs api"; exit 1; }
  printf '.'; sleep 2
done
echo
curl -s "http://127.0.0.1:${PORT}/health"; echo
echo "the store answers on http://127.0.0.1:${PORT} (API and dashboard, this machine only)"
