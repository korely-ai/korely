#!/usr/bin/env bash
# Does an install that already holds data survive?
#
#   GOOGLE_API_KEY=... bash scripts/acceptance_survives.sh
#
# `acceptance.sh` proves a fresh install works. It has now done so about twenty
# times, and in none of them was there anything in the database beforehand.
# That leaves the two things most likely to go wrong on somebody else's
# machine, months after we stopped watching:
#
#   an upgrade, where a migration meets a populated table for the first time.
#     The product this was extracted from took production down in a boot loop
#     on exactly that, in June, on a migration that referenced a column which
#     did not exist. It passed its tests, because the tests build the schema
#     from the models and never run the migrations.
#
#   a restore, which deploy/restore.sh's own comment says to practise before
#     you need it, and which nobody has ever run.
#
# So this installs an OLD version, fills it, and then does both.
#
# Flags:
#   --from REF   the version to start at (default: v0.1.20, see FROM_REF)
#   --keep       leave the stack up
set -u

# The version to start from must still build today. b3fa78b (11 Sep) did
# not: its requirements were ranges, and SQLAlchemy 2.1 made psycopg 3 the
# default driver for postgresql://, so its API died on "No module named
# psycopg" before the first migration (8 Oct 2026). From 0.1.18 the image
# installs the lock. 0.1.20 still has a migration to meet (cached_tokens).
FROM_REF="${FROM_REF:-v0.1.20}"
KEEP=0
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DIR="${TMPDIR:-/tmp}/korely-survives"

while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM_REF="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# ok, bad, block, is, has, and the counters: acceptance.sh's.
. "$HERE/scripts/acceptance_checks.sh"

API="http://127.0.0.1:${PORT:-8124}"
say()  { printf '%s\n' "$1"; }
dc()   { (cd "$DIR" && docker compose "$@"); }
psqlq(){ (cd "$DIR" && docker compose exec -T db psql -U korely -d korely -tAc "$1" 2>/dev/null | tr -d ' \r'); }

cleanup() { [ "$KEEP" = "0" ] && dc down -v >/dev/null 2>&1; }
trap cleanup EXIT

# ─────────────────────────────────────────────── the version they installed ──
block "installing $FROM_REF, the version they already have"
[ -f "$DIR/docker-compose.yml" ] && dc down -v >/dev/null 2>&1
rm -rf "$DIR"; mkdir -p "$DIR"
git -C "$HERE" archive "$FROM_REF" | tar -x -C "$DIR" || { echo "no such ref: $FROM_REF" >&2; exit 2; }
say "  $(git -C "$HERE" log -1 --format='%h %s' "$FROM_REF" | cut -c1-72)"

# The .env the README asks for, SECRET_KEY included (without one the screen
# cannot store a model key, and the restore below is about that key).
grep -v '^SECRET_KEY=' "$HERE/.env.example" > "$DIR/.env"
PGPASS=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')
{
  echo "SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo "DATABASE_URL=postgresql://korely:${PGPASS}@db:5432/korely"
  echo "POSTGRES_PASSWORD=${PGPASS}"
  echo "PORT=${PORT:-8124}"
  echo "HTTP_PORT=${HTTP_PORT:-8181}"
  echo "HTTPS_PORT=${HTTPS_PORT:-8544}"
  [ -n "${GOOGLE_API_KEY:-}" ] && echo "GOOGLE_API_KEY=${GOOGLE_API_KEY}"
} >> "$DIR/.env"

dc up -d --build >/dev/null 2>&1 || { echo "the old version would not start" >&2; exit 1; }
DEADLINE=$(( $(date +%s) + 240 ))
until curl -fsS -o /dev/null --max-time 5 "$API/health" 2>/dev/null; do
  [ "$(date +%s)" -gt "$DEADLINE" ] && { echo "the old version never answered"; dc logs --tail 20 api; exit 1; }
  sleep 2
done
ok "it is up and answering"
BEFORE_MIGRATION=$(psqlq "SELECT version_num FROM alembic_version;")
say "  at migration $BEFORE_MIGRATION"

# ──────────────────────────────────────────────────────── fill it with data ──
block "a few months of use"
OUT=$(dc exec -T api python -m korely_agent.manage create-tenant --name "Acme" 2>&1)
KEY=$(echo "$OUT" | awk '/^key /{print $2}')
TENANT=$(echo "$OUT" | awk '/^tenant /{print $2}')
[ -n "$KEY" ] || { echo "no key from the old version: $OUT" >&2; exit 1; }

curl -fsS -X POST "$API/v1/memories" -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"content":"Customer 4821 is on the Priority Support plan.","user_id":"c4821","timestamp":"2026-03-15"}' >/dev/null
curl -fsS -X POST "$API/v1/memories" -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"content":"Customer 4821 moved to the Basic plan.","user_id":"c4821","timestamp":"2026-06-20"}' >/dev/null

if [ -n "${GOOGLE_API_KEY:-}" ]; then
  # Every memory digested before counting, not just the first fact: counted
  # at the first fact, the second memory's fact landed during the upgrade and
  # "the facts survived" read 1 against 2 (8 Oct 2026).
  DEADLINE=$(( $(date +%s) + 180 ))
  until [ "$(psqlq "SELECT count(*) FROM agent_memories WHERE graph_sync_status IN ('pending','processing');")" = "0" ] \
        && [ "$(psqlq 'SELECT count(*) FROM agent_facts;')" != "0" ]; do
    [ "$(date +%s)" -gt "$DEADLINE" ] && break
    sleep 3
  done
fi
MEM_BEFORE=$(psqlq "SELECT count(*) FROM agent_memories;")
FACT_BEFORE=$(psqlq "SELECT count(*) FROM agent_facts;")
say "  $MEM_BEFORE memories, $FACT_BEFORE facts, one key"
is "there is something to lose" "2" "$MEM_BEFORE"

# ───────────────────────────────────────────────────────────── the upgrade ──
block "upgrading to today, in place, the way the runbook says"
# The working tree, not HEAD. `git archive HEAD` was here first, and it made
# this test useless: a deliberately broken migration stayed uncommitted, never
# reached the image, and the run went green. A test of what you are about to
# ship has to use what you have, which is also what acceptance.sh does.
#
# The migrations then run inside the entrypoint, which is where they will run
# on the customer's machine at two in the morning.
rsync -a --delete-after \
  --exclude=node_modules --exclude=dist --exclude=.git --exclude=.env \
  --exclude=backups --exclude=install.env --exclude='install-report-*' \
  "$HERE"/ "$DIR"/
UP_LOG=$(dc up -d --build 2>&1)
DEADLINE=$(( $(date +%s) + 300 ))
UP=0
until curl -fsS -o /dev/null --max-time 5 "$API/health" 2>/dev/null; do
  if [ "$(date +%s)" -gt "$DEADLINE" ]; then break; fi
  sleep 2
done
curl -fsS -o /dev/null --max-time 5 "$API/health" 2>/dev/null && UP=1
if [ "$UP" = "1" ]; then
  ok "it came back up"
else
  bad "it came back up" "$(dc logs --tail 25 api 2>&1 | tail -8 | tr '\n' ' ')"
fi

# At the head of the code it now runs, read from that code: a release with no
# new migration leaves the database where it was, and that is right (0.1.24 to
# 0.1.25, 9 Oct 2026: "still at" its head was reported as a failure).
AFTER_MIGRATION=$(psqlq "SELECT version_num FROM alembic_version;")
TARGET_HEAD=$(dc exec -T api alembic heads 2>/dev/null | awk '{print $1}' | tr -d '\r' | head -1)
if [ -n "$AFTER_MIGRATION" ] && [ "$AFTER_MIGRATION" = "$TARGET_HEAD" ]; then
  if [ "$AFTER_MIGRATION" != "$BEFORE_MIGRATION" ]; then
    ok "the migrations ran ($BEFORE_MIGRATION -> $AFTER_MIGRATION)"
  else
    ok "the database is at this code's head ($AFTER_MIGRATION, nothing new to run)"
  fi
else
  bad "the migrations ran" "at ${AFTER_MIGRATION:-nothing}, the code's head is ${TARGET_HEAD:-unknown}"
fi

is "the memories survived" "$MEM_BEFORE" "$(psqlq 'SELECT count(*) FROM agent_memories;')"
is "the facts survived"    "$FACT_BEFORE" "$(psqlq 'SELECT count(*) FROM agent_facts;')"
is "the key still works"   "200" "$(curl -s -o /dev/null -w '%{http_code}' "$API/v1/ping" -H "Authorization: Bearer $KEY")"
# The one that matters: the moat still answers after the schema moved.
if [ "${FACT_BEFORE:-0}" != "0" ]; then
  has "April still reads April" "Priority" \
      "$(curl -s "$API/v1/facts?user_id=c4821&as_of=2026-04-01" -H "Authorization: Bearer $KEY")"
fi
# And the things today added exist on a database that predates them.
is "the new columns are usable" "200" "$(curl -s -o /dev/null -w '%{http_code}' "$API/admin/keys/lifetimes" -H "Authorization: Bearer $(dc exec -T api python -m korely_agent.manage issue-key --tenant "$TENANT" --name op --admin 2>/dev/null | tr -d '\r')")"

# ────────────────────────────────────────────────────────────── the restore ──
block "a backup, and the restore nobody has ever run"
DUMP=$( (cd "$DIR" && BACKUP_DIR=./backups sh deploy/backup.sh 2>&1) | tail -1 | awk '{print $1}')
if [ -f "$DIR/$DUMP" ] || [ -f "$DUMP" ]; then
  ok "the backup wrote a file"
else
  bad "the backup wrote a file" "$DUMP"
fi

# Destroy something specific, so "it worked" is not just "nothing happened".
psqlq "DELETE FROM agent_memories;" >/dev/null
is "and now the data is gone" "0" "$(psqlq 'SELECT count(*) FROM agent_memories;')"

RESTORE_OUT=$( (cd "$DIR" && FORCE=1 sh deploy/restore.sh "$DUMP" 2>&1) | tail -3 )
DEADLINE=$(( $(date +%s) + 120 ))
until curl -fsS -o /dev/null --max-time 5 "$API/health" 2>/dev/null; do
  [ "$(date +%s)" -gt "$DEADLINE" ] && break
  sleep 2
done
is "the restore brought the memories back" "$MEM_BEFORE" "$(psqlq 'SELECT count(*) FROM agent_memories;')"
is "and the facts"                         "$FACT_BEFORE" "$(psqlq 'SELECT count(*) FROM agent_facts;')"
is "and the service is serving"            "200" "$(curl -s -o /dev/null -w '%{http_code}' "$API/health")"
is "and the key still works"               "200" "$(curl -s -o /dev/null -w '%{http_code}' "$API/v1/ping" -H "Authorization: Bearer $KEY")"

# ──────────────────────────────────────── the same dump, another SECRET_KEY ──
# The README's promise (Backups): a dump restored on another machine needs the
# same SECRET_KEY, "or the model key cannot be decrypted and is pasted again in
# Settings, which is the whole damage". Until 8 Oct 2026 only a unit test said
# so (tests/test_provider_key.py). Here on the install: the key pasted from
# the screen, then the .env of a machine with another SECRET_KEY and no model
# key in the file, then what still works and what has to be pasted again. The
# paste asks the provider once, its cheapest call, so it needs GOOGLE_API_KEY.
if [ -n "${GOOGLE_API_KEY:-}" ]; then
  block "the same dump where SECRET_KEY is another one"
  ADMIN=$(dc exec -T api python -m korely_agent.manage issue-key --tenant "$TENANT" --name "op-secret" --admin 2>/dev/null | tr -d '\r')
  is "the model key is pasted from the screen" "200" \
     "$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$API/admin/provider" -H "Authorization: Bearer $ADMIN" \
        -H 'Content-Type: application/json' -d "{\"key\":\"$GOOGLE_API_KEY\"}")"
  has "and read back as the screen's" '"source":"screen","set":true' \
      "$(curl -s "$API/admin/provider" -H "Authorization: Bearer $ADMIN")"

  grep -v -e '^SECRET_KEY=' -e '^GOOGLE_API_KEY=' "$DIR/.env" > "$DIR/.env.other"
  echo "SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> "$DIR/.env.other"
  mv "$DIR/.env.other" "$DIR/.env"
  dc up -d api >/dev/null 2>&1
  DEADLINE=$(( $(date +%s) + 120 ))
  until curl -fsS -o /dev/null --max-time 5 "$API/health" 2>/dev/null; do
    [ "$(date +%s)" -gt "$DEADLINE" ] && break
    sleep 2
  done
  PROV=$(curl -s "$API/admin/provider" -H "Authorization: Bearer $ADMIN")
  has "the stored model key is not readable there, and says so" '"set":false' "$PROV"
  has "and still says it came from the screen"                  '"source":"screen"' "$PROV"
  has "the install says no model is configured"                 '"not configured"' "$(curl -s "$API/health")"
  is  "the agents' keys still work"   "200" "$(curl -s -o /dev/null -w '%{http_code}' "$API/v1/ping" -H "Authorization: Bearer $KEY")"
  is  "the memories are all there"    "$MEM_BEFORE" "$(psqlq 'SELECT count(*) FROM agent_memories;')"
  has "and April still reads April"   "Priority" \
      "$(curl -s "$API/v1/facts?user_id=c4821&as_of=2026-04-01" -H "Authorization: Bearer $KEY")"
  is  "pasting it again is the whole repair" "200" \
     "$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$API/admin/provider" -H "Authorization: Bearer $ADMIN" \
        -H 'Content-Type: application/json' -d "{\"key\":\"$GOOGLE_API_KEY\"}")"
  has "and it reads again" '"set":true' "$(curl -s "$API/admin/provider" -H "Authorization: Bearer $ADMIN")"
fi

printf '\n────────────────────────────────────────────\n'
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then printf 'failed:\n'; printf "$FAILED_NAMES"; exit 1; fi
printf 'an install that already holds data survives both.\n'
