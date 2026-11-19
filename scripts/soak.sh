#!/usr/bin/env bash
# Days of agent-like load on one Korely process: does its memory or its disk
# grow, and does it come back from restarts?
#
#   bash scripts/soak.sh                       # 48 hours, a restart every 12
#   bash scripts/soak.sh 3m 1m                 # a short check of the script
#
# Arguments: how long (s, m or h; default 48h) and how often the API restarts
# (default 12h); KORELY_SOAK_USERS end users (50) and KORELY_SOAK_SECONDS
# between turns (1). Each turn writes a memory, searches and reads a context,
# as an agent does around a turn of a conversation (scripts/soak/load.py). The
# writer is the stand-in of scripts/two_processes/: no key, nothing spent,
# no fact written, so this measures the process, the queues and the disk, not
# the facts.
#
# Every minute it writes a line to $OUT/samples.csv: seconds, the API's
# memory, the database's size, the memories and facts waiting, and the
# load's counts. At the end it prints the growth per hour of the API's memory
# and per day of the database, and the load's last counts (a few requests
# fail around each restart, by design), and FAIL when the API died other than
# by a restart, when
# memories were still waiting ten minutes after the load stopped, or when
# its log has an ERROR or a traceback. It stops early when the Mac's disk
# falls under 4 GB free.
#
# Environment, all optional:
#   KORELY_IMAGE            the image to run; built from this tree when unset
#   KORELY_CODE_FROM_TREE   1: this tree's code over KORELY_IMAGE's
#   KORELY_SOAK_OUT         where samples.csv and the logs go (./soak-results)
#   KEEP                    1: leave the stack up at the end
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DIR="$HERE/soak"
secs() { case "$1" in *h) echo $(( ${1%h} * 3600 ));; *m) echo $(( ${1%m} * 60 ));; *s) echo "${1%s}";; *) echo "$1";; esac; }
DURATION=$(secs "${1:-48h}")
EVERY=$(secs "${2:-12h}")
USERS="${KORELY_SOAK_USERS:-50}"
PAUSE="${KORELY_SOAK_SECONDS:-1}"
OUT="${KORELY_SOAK_OUT:-$PWD/soak-results}"
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
FILES=(-f "$DIR/compose.yml")
[ "${KORELY_CODE_FROM_TREE:-}" = 1 ] && FILES+=(-f "$DIR/code-from-tree.yml")
if [ -z "${KORELY_IMAGE:-}" ]; then
  KORELY_IMAGE=korely-agent:soak
  echo "building $KORELY_IMAGE from this tree..."
  docker build -q -t "$KORELY_IMAGE" "$ROOT" >/dev/null || { echo "FAIL: the image did not build"; exit 1; }
fi
export KORELY_IMAGE KORELY_TREE="$ROOT"
dc() { docker compose "${FILES[@]}" "$@"; }
sql() { dc exec -T db psql -U korely -d korely -tAc "$1" 2>/dev/null; }
# The Mac's data volume when there is one, else / (a Linux server). A pipe's
# status is its last command's, so `df a | awk || df b` never fell back, and
# on Linux the check read nothing and never stopped (31 Oct 2026).
free_gb() { local p=/; [ -d /System/Volumes/Data ] && p=/System/Volumes/Data; df -k "$p" | tail -1 | awk '{print int($4/1048576)}'; }
LOAD=korely-soak-load
cleanup() {
  docker rm -f "$LOAD" >/dev/null 2>&1
  [ "${KEEP:-0}" = 1 ] || dc down -v >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

dc down -v >/dev/null 2>&1
dc up -d >/dev/null 2>&1 || { echo "FAIL: the stack did not start"; exit 1; }
for _ in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' localhost:18021/health)" = 200 ] && break
  sleep 3
done
KEY=$(dc exec -T api python -m korely_agent.manage create-tenant --name Soak | awk '/^key/{print $2}')
[ -n "$KEY" ] || { echo "FAIL: no key"; exit 1; }

docker run -d --name "$LOAD" --network korely-soak_default -v "$DIR:/t:ro" \
  -e BASE=http://api:8000 -e KEY="$KEY" --entrypoint python "$KORELY_IMAGE" \
  -u /t/load.py "$USERS" "$PAUSE" >/dev/null

echo "seconds,api_mib,db_mib,memories,waiting_facts,waiting_vectors,load" > "$OUT/samples.csv"
start=$(date +%s); next_restart=$((start + EVERY)); restarts=""; died=0
while :; do
  now=$(date +%s); el=$((now - start))
  [ "$el" -ge "$DURATION" ] && break
  if [ "$(free_gb)" -lt 4 ]; then echo "stopped: the disk is under 4 GB free"; break; fi
  if [ "$now" -ge "$next_restart" ]; then
    dc logs --no-color api >> "$OUT/api.log" 2>&1
    dc restart api >/dev/null 2>&1; restarts="$restarts $el"; next_restart=$((now + EVERY))
  fi
  state=$(docker inspect -f '{{.State.Running}}' korely-soak-api-1 2>/dev/null)
  [ "$state" = true ] || { died=1; echo "the API is not running at ${el}s"; break; }
  mem=$(docker stats --no-stream --format '{{.MemUsage}}' korely-soak-api-1 | awk '{v=$1; if (v ~ /GiB/) {sub(/GiB/,"",v); v*=1024} else {sub(/MiB/,"",v)} print int(v)}')
  db=$(sql "SELECT pg_database_size('korely')/1048576")
  row=$(sql "SELECT count(*), count(*) FILTER (WHERE graph_sync_status IN ('pending','processing')), count(*) FILTER (WHERE embedding_status = 'pending') FROM agent_memories" | tr '|' ',')
  load=$(docker logs --tail 1 "$LOAD" 2>&1 | tr ',' ';')
  echo "$el,$mem,$db,$row,\"$load\"" >> "$OUT/samples.csv"
  sleep 60
done

docker stop "$LOAD" >/dev/null 2>&1
docker logs "$LOAD" > "$OUT/load.log" 2>&1
waiting=1
for _ in $(seq 1 60); do
  waiting=$(sql "SELECT count(*) FROM agent_memories WHERE graph_sync_status IN ('pending','processing') OR embedding_status = 'pending'")
  [ "$waiting" = 0 ] && break
  sleep 10
done
dc logs --no-color api >> "$OUT/api.log" 2>&1
errors=$(grep -c -E '\| ERROR|Traceback' "$OUT/api.log" || true)

python3 - "$OUT/samples.csv" "$restarts" "$died" "$waiting" "$errors" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
restarts = [int(x) for x in sys.argv[2].split()]
died, waiting, errors = int(sys.argv[3]), int(sys.argv[4] or 0), int(sys.argv[5])
if len(rows) < 2:
    print("FAIL: fewer than two samples"); sys.exit(1)
first, last = rows[0], rows[-1]
hours = max((int(last["seconds"]) - int(first["seconds"])) / 3600, 1e-9)
api = [int(r["api_mib"]) for r in rows if r["api_mib"]]
db = [int(r["db_mib"]) for r in rows if r["db_mib"]]
# The API's memory after its warm-up: from the tenth minute, or the first sample.
warm = api[min(10, len(api) - 1)]
print(f"ran {hours:.2f} h, {len(rows)} samples, restarts at {restarts or 'none'} s")
print(f"api memory: warm {warm} MiB, last {api[-1]} MiB, max {max(api)} MiB, "
      f"{(api[-1] - warm) / hours:+.1f} MiB/h")
print(f"database: {db[0]} -> {db[-1]} MiB, {(db[-1] - db[0]) / hours * 24:+.1f} MiB/day")
print(f"memories: {last['memories']}; waiting at the end: {waiting}")
print(f"last load line: {last['load']}")
fails = []
if died: fails.append("the API died other than by a restart")
if waiting: fails.append(f"{waiting} memories still waiting ten minutes after the load stopped")
if errors: fails.append(f"{errors} ERROR lines or tracebacks in the API log")
print("FAIL: " + "; ".join(fails) if fails else "PASS")
sys.exit(1 if fails else 0)
PY
