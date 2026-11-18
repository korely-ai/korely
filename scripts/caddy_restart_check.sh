#!/usr/bin/env bash
# Do the requests sent while the API restarts get an answer, and does no
# write land twice? Run against the shipped Caddy, before a release that
# changes deploy/Caddyfile or the Caddy image.
#
#   bash scripts/caddy_restart_check.sh --image korely-agent:release-<version>
#
# It brings up the install's own services (docker-compose.yml with
# docker-compose.image.yml: the database, the API from the image named, and
# Caddy with this checkout's deploy/Caddyfile) as a compose project of their
# own, in a folder of their own, with no model key: a memory is stored and
# waits, mined by nobody, so no model is called and nothing is billed.
# Then, while clients write through Caddy, it restarts the API's
# container (`restart`, as a reboot or a `docker restart` does) and recreates
# it (`up -d --force-recreate`, as an upgrade does), and counts:
#
#   - Caddy's 502s for a request the API never had whole (no connection, or
#     the request or its body not sent whole: Caddy should have held it),
#     apart from those on a request that went out (the API may have had it,
#     and a proxy must not send a POST again);
#   - the writes the database holds twice: a body Caddy sent again after the
#     API had it (a double write);
#   - the writes answered 2xx and not stored.
#
# Three kinds of client, one request at a time each: POST /v1/memories with a
# small body (a usual write; --writers of them at once), POST /v1/memories
# with a large one (the same write padded with JSON whitespace to 300 KB,
# under the API's 2 MiB: past any `request_buffers` a Caddyfile may set), and
# GET /health. And two impatient writers, which wait 5 s (less than Caddy's
# 20 s hold) and send a write again when they had no answer: one with the
# same Idempotency-Key on every send, one with none. It passes with none of
# the first 502s, no write twice (the impatient writer without a key is
# shown, not judged: the guide tells such a client to send a key) and none
# missing.
# Nothing a model decides: status codes and rows.
#
# Flags:
#   --image IMAGE        the API's image (required), already on this machine
#   --caddyfile FILE     the Caddyfile to run (default: deploy/Caddyfile)
#   --caddy-image IMAGE  Caddy's image (default: the one docker-compose.yml names)
#   --project NAME       the compose project (default: korely-restart-check)
#   --rounds N           restarts, and as many recreations (default: 2)
#   --writers N          clients of small writes at once (default: 1)
#   --keep               leave the stack and its folder in place
#
# The ports are HTTPS_PORT (8743), HTTP_PORT (8380) and PORT (8323) from the
# environment, so a second install on the machine keeps its own.
set -u

HERE="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE=""
CADDYFILE="$HERE/deploy/Caddyfile"
CADDY_IMAGE=""
PROJECT="korely-restart-check"
ROUNDS=2
WRITERS=1
KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="${2:-}"; shift 2 ;;
    --caddyfile) CADDYFILE="${2:-}"; shift 2 ;;
    --caddy-image) CADDY_IMAGE="${2:-}"; shift 2 ;;
    --project) PROJECT="${2:-}"; shift 2 ;;
    --rounds) ROUNDS="${2:-}"; shift 2 ;;
    --writers) WRITERS="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,38p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done
[ -n "$IMAGE" ] || { echo "--image is required: the API's image to run" >&2; exit 2; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "no image $IMAGE on this machine" >&2; exit 2; }
[ -f "$CADDYFILE" ] || { echo "no Caddyfile at $CADDYFILE" >&2; exit 2; }
HTTPS_PORT="${HTTPS_PORT:-8743}"
HTTP_PORT="${HTTP_PORT:-8380}"
PORT="${PORT:-8323}"

# ok, bad, block, is: the acceptance scripts' verdicts.
. "$HERE/scripts/acceptance_checks.sh"

umask 077
DIR="$(mktemp -d "${TMPDIR:-/tmp}/korely-restart-check.XXXXXX")"
OUT="$DIR/out"
STOP="$DIR/stop"
mkdir -p "$DIR/deploy" "$OUT"
cp "$HERE/docker-compose.yml" "$HERE/docker-compose.image.yml" "$DIR/"
cp "$CADDYFILE" "$DIR/deploy/Caddyfile"
COMPOSE_FILES="docker-compose.yml:docker-compose.image.yml"
if [ -n "$CADDY_IMAGE" ]; then
  printf 'services:\n  caddy:\n    image: %s\n' "$CADDY_IMAGE" > "$DIR/docker-compose.caddy.yml"
  COMPOSE_FILES="$COMPOSE_FILES:docker-compose.caddy.yml"
fi
{
  echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)"
  echo "SECRET_KEY=$(openssl rand -hex 32)"
  echo "COMPOSE_FILE=$COMPOSE_FILES"
  echo "KORELY_IMAGE=$IMAGE"
  echo "PORT=$PORT"
  echo "HTTP_PORT=$HTTP_PORT"
  echo "HTTPS_PORT=$HTTPS_PORT"
} > "$DIR/.env"

# The folder's .env decides, never the shell (as ./korely does).
dc() { (cd "$DIR" && env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u PORT -u HTTP_PORT -u HTTPS_PORT \
          docker compose -p "$PROJECT" "$@"); }
finish() {
  touch "$STOP"
  wait 2>/dev/null
  if [ "$KEEP" = 1 ]; then
    echo "left in place: $DIR (project $PROJECT)"
  else
    dc down -v --remove-orphans >/dev/null 2>&1
    rm -rf "$DIR"
  fi
}
trap finish EXIT

SITE="localhost"
TLS="https://$SITE:$HTTPS_PORT"
through_caddy() { curl -sk --resolve "$SITE:$HTTPS_PORT:127.0.0.1" "$@"; }
ready() { curl -s -m 3 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"ready":true'; }
wait_ready() {  # seconds until the API says ready, at most 180
  local t0 t
  t0=$(date +%s)
  until ready; do
    t=$(( $(date +%s) - t0 ))
    [ "$t" -ge 180 ] && { echo 180; return 1; }
    sleep 0.5
  done
  echo $(( $(date +%s) - t0 ))
}

block "the stack: $IMAGE behind $(basename "$CADDYFILE")"
dc up -d >"$OUT/up.log" 2>&1 || { echo "could not start the stack:"; tail -20 "$OUT/up.log"; exit 1; }
wait_ready >/dev/null || { echo "the API never said ready"; dc logs --tail 30 api; exit 1; }
until through_caddy -o /dev/null -m 3 "$TLS/health"; do sleep 1; done
echo "  caddy $(dc exec -T caddy caddy version </dev/null | cut -d' ' -f1)"

# The key goes into a file the clients read (curl -H @file), so it is in no
# command line and no log; the folder is this user's alone, and goes at the end.
KEY=$(dc exec -T api python -m korely_agent.manage create-tenant --name "Restart check" </dev/null \
        | awk '/^key /{print $2}')
[ -n "$KEY" ] || { echo "could not make the account"; exit 1; }
printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "$KEY" > "$DIR/headers"
unset KEY
RUN="rc$(date +%H%M%S)$$"
PAD="$(head -c 300000 /dev/zero | tr '\0' ' ')"

# client NAME METHOD PATH [large]: one request at a time until $STOP, one
# line per answer: the mark, the status (000 when curl had none) and the time.
# Lines are short and appended whole, so clients can share a file.
client() {
  local name="$1" method="$2" path="$3" large="${4:-}" who="${5:-1}" i=0 mark body
  while [ ! -f "$STOP" ]; do
    i=$((i + 1))
    mark="restart check $RUN $name $who-$i"
    if [ "$method" = POST ]; then
      body="{\"content\":\"$mark\",\"user_id\":\"restart-check\"${large:+$PAD}}"
      printf '%s' "$body" > "$OUT/$name-$who.body"
      printf '%s\t%s\n' "$mark" "$(through_caddy -o /dev/null -w '%{http_code}\t%{time_total}' -m 90 \
        -X POST -H @"$DIR/headers" --data-binary @"$OUT/$name-$who.body" "$TLS$path")" >> "$OUT/$name.txt"
    else
      printf '%s\t%s\n' "$mark" "$(through_caddy -o /dev/null -w '%{http_code}\t%{time_total}' -m 90 \
        "$TLS$path")" >> "$OUT/$name.txt"
    fi
  done
}
for w in $(seq 1 "$WRITERS"); do client small POST /v1/memories "" "$w" & done
client large POST /v1/memories large &
client read GET /health &

# impatient NAME [key]: a client that waits 5 s, less than the 20 s Caddy
# holds a request, and sends a write it had no answer for again (a timeout, a
# 5xx, a 409 of a key in use), up to ten times a second apart: what an SDK's
# retry, or the hooks' next attempt, does, over HTTP/1.1 as Python's urllib
# and Node's fetch speak it: over HTTP/2 Caddy hears at once that a client
# left, over HTTP/1.1 only once it has read the body, when it sends the
# request to the API (curl alone would speak HTTP/2 here). With `key`, every
# send of one write
# carries the same Idempotency-Key (Korely 1.0.27). One line per write: the
# mark, the last status and how many sends it took.
impatient() {
  local name="$1" key="${2:-}" i=0 n code mark idem
  while [ ! -f "$STOP" ]; do
    i=$((i + 1))
    mark="restart check $RUN $name 1-$i"
    printf '{"content":"%s","user_id":"restart-check"}' "$mark" > "$OUT/$name.body"
    idem=""
    [ -n "$key" ] && idem="Idempotency-Key: \"$(openssl rand -hex 16)\""
    n=0
    while :; do
      n=$((n + 1))
      code=$(through_caddy --http1.1 -o /dev/null -w '%{http_code}' -m 5 -X POST -H @"$DIR/headers" \
               ${idem:+-H "$idem"} --data-binary @"$OUT/$name.body" "$TLS/v1/memories")
      case "$code" in 2*) break ;; esac
      [ "$n" -ge 10 ] && break
      sleep 1
    done
    printf '%s\t%s\t%s\n' "$mark" "$code" "$n" >> "$OUT/$name.txt"
  done
}
impatient impatient key &
impatient impatient-nokey &

block "restarting the API while they write"
sleep 3
for r in $(seq 1 "$ROUNDS"); do
  dc restart api >/dev/null 2>&1
  echo "  restart $r: ready again after $(wait_ready)s"
  sleep 3
  dc up -d --no-deps --force-recreate api >/dev/null 2>&1
  echo "  recreate $r: ready again after $(wait_ready)s"
  sleep 3
done
touch "$STOP"
wait

block "what the clients got"
for name in small large read impatient impatient-nokey; do
  total=$(wc -l < "$OUT/$name.txt" | tr -d ' ')
  codes=$(cut -f2 "$OUT/$name.txt" | sort | uniq -c | awk '{printf "%s%s x%s", sep, $2, $1; sep=", "}')
  slowest=$(cut -f3 "$OUT/$name.txt" | sort -n | tail -1)
  echo "  $name: $total requests ($codes), the slowest ${slowest}s"
done

block "what the database holds"
q() { dc exec -T db psql -U korely -d korely -Atc "$1" </dev/null; }
for name in small large impatient impatient-nokey; do
  stored=$(q "select count(*) from agent_memories where content like 'restart check $RUN $name %'")
  twice=$(q "select count(*) - count(distinct content) from agent_memories where content like 'restart check $RUN $name %'")
  acked=$(awk -F'\t' '$2 ~ /^2/' "$OUT/$name.txt" | wc -l | tr -d ' ')
  echo "  $name: $acked answered 2xx, $stored stored, $twice stored twice"
  # A client that sends a write again with no key makes it twice when its
  # first send was held past its timeout and then made: what the guide tells
  # such a client not to do, shown here and not a failure of the proxy.
  [ "$name" = impatient-nokey ] && continue
  is "$name: no write stored twice" 0 "$twice"
  # Every write answered 2xx is stored (a 502 may or may not be: the API
  # may have had it, which is why Caddy must not send it again).
  missing=0
  while IFS="$(printf '\t')" read -r mark code _; do
    case "$code" in 2*) [ "$(q "select count(*) from agent_memories where content = '$mark'")" = 1 ] \
                          || missing=$((missing + 1)) ;; esac
  done < "$OUT/$name.txt"
  is "$name: every write answered 2xx is stored" 0 "$missing"
done

# Each 502 is one line of Caddy's error log, and its message is Go's error
# for what failed. Two are failures no proxy may mend, because the request
# went out and a POST must not be sent again (RFC 9110 section 9.2.2: "A
# proxy MUST NOT automatically retry non-idempotent requests"): `read ...`,
# the request sent whole and its answer never came (a connection the API
# reset as it stopped), and Go's `http: server closed idle connection`, a
# kept-alive connection the API closed as the request went out on it. Any
# other is a request the API never had whole, which Caddy should have held:
# `dial ...` (no connection), `write ...` or `readfrom ...` (the request or
# its body not sent whole, as when a retry finds its body closed), or a
# message this cannot read. The check fails on each of those.
block "why Caddy answered 502"
dc logs caddy > "$OUT/caddy.log" 2>&1
grep "http.log.error" "$OUT/caddy.log" | grep '"status": 502' | awk -F'\t' '{print $4}' > "$OUT/errors.txt"
answered=$(wc -l < "$OUT/errors.txt" | tr -d ' ')
sent=$(grep -c -E '^read |^http: server closed idle connection$' "$OUT/errors.txt")
echo "  $answered answers 502, by what failed: $(awk '{print $1}' "$OUT/errors.txt" | sort | uniq -c \
  | awk '{printf "%s%s x%s", sep, $2, $1; sep=", "}')"
echo "  $sent of them on a request that went out (not sent again)"
is "no 502 for a request the API never had whole" 0 "$((answered - sent))"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ] || { printf "$FAILED_NAMES"; exit 1; }
