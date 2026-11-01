#!/usr/bin/env bash
# Two Korely processes on one Postgres, under load: does any memory get
# written twice?
#
#   bash scripts/two_processes.sh                 # 2000 memories, 32 at a time
#   bash scripts/two_processes.sh 5000 64
#
# Both processes run the worker (RUN_WORKER's default) and start at the same
# moment on a new database, so both run the migrations at once. Then N
# memories, each for its own end user, go half to one process and half to the
# other, and the run waits for every vector and every fact extraction. It
# passes when:
#   - both processes started, and neither died;
#   - every memory was accepted, has its vector, and reached the writer once;
#   - every memory.created webhook was delivered once;
#   - neither log has an ERROR or a traceback.
# It also shows that an MCP session opened on one process is unknown to the
# other: a load balancer in front must keep each MCP client on one process.
#
# The writer is a stand-in (scripts/two_processes/fake_model.py) that answers
# "no facts" and counts: no key, nothing leaves the machine, nothing is spent.
# The database lives in memory and everything is removed at the end.
#
# Environment, all optional:
#   KORELY_IMAGE            the image to run; built from this tree when unset
#   KORELY_CODE_FROM_TREE   1: mount this tree's code over KORELY_IMAGE's
#                           (two_processes/code-from-tree.yml)
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
N="${1:-2000}"
CONC="${2:-32}"
DIR="$HERE/two_processes"
FILES=(-f "$DIR/compose.yml")
if [ "${KORELY_CODE_FROM_TREE:-}" = 1 ]; then
  FILES+=(-f "$DIR/code-from-tree.yml")
fi
if [ -z "${KORELY_IMAGE:-}" ]; then
  KORELY_IMAGE=korely-agent:two-processes
  echo "building $KORELY_IMAGE from this tree..."
  docker build -q -t "$KORELY_IMAGE" "$ROOT" >/dev/null
fi
export KORELY_IMAGE KORELY_TREE="$ROOT"
dc() { docker compose "${FILES[@]}" "$@"; }
sql() { dc exec -T db psql -U korely -d korely -tAc "$1"; }
inside() { docker run --rm --network korely-two-processes_default -v "$DIR:/t:ro" "$@"; }
cleanup() { dc down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT
fail() { echo "FAIL: $*"; for s in api1 api2; do echo "--- $s"; dc logs --no-color "$s" 2>&1 | tail -20; done; exit 1; }

cleanup
dc up -d >/dev/null 2>&1
for _ in $(seq 1 60); do
  a=$(curl -s -o /dev/null -w '%{http_code}' localhost:18001/health || true)
  b=$(curl -s -o /dev/null -w '%{http_code}' localhost:18002/health || true)
  [ "$a" = 200 ] && [ "$b" = 200 ] && break
  sleep 3
done
[ "$a" = 200 ] && [ "$b" = 200 ] || fail "both processes should answer /health; they answered $a and $b"
echo "ok: both processes started together on a new database"

out=$(dc exec -T api1 python -m korely_agent.manage create-tenant --name "Two processes")
KEY=$(echo "$out" | awk '/^key/{print $2}')
TENANT=$(echo "$out" | awk '/^tenant/{print $2}')
dc exec -T api1 python -m korely_agent.manage add-webhook --tenant "$TENANT" \
  --url http://hooks:9100/hook --events memory.created >/dev/null

inside -e KEY="$KEY" --entrypoint python "$KORELY_IMAGE" /t/load.py "$N" "$CONC" \
  || fail "not every memory was accepted"

for _ in $(seq 1 240); do
  left=$(sql "SELECT count(*) FROM agent_memories WHERE embedding_status = 'pending' OR graph_sync_status = 'pending'")
  [ "$left" = 0 ] && break
  sleep 5
done
[ "$left" = 0 ] || fail "$left memories still waiting after 20 minutes"
sleep 20   # the last webhooks: the worker sends them every 15 s

stats=$(inside --entrypoint python "$KORELY_IMAGE" -c "
import httpx, json
print(json.dumps({'writer': httpx.get('http://writer:9000/stats').json(),
                  'hooks': httpx.get('http://hooks:9100/stats').json()}))")
no_vector=$(sql "SELECT count(*) FROM agent_memories WHERE embedding IS NULL")
running=$(dc ps --status running --services | grep -c '^api' || true)
errors=$(for s in api1 api2; do dc logs --no-color "$s" 2>&1; done | grep -c -E '\| ERROR|Traceback' || true)
python3 - "$N" "$stats" "$no_vector" "$running" "$errors" <<'PY' || fail "see above"
import json, sys
n, s, no_vector, running, errors = int(sys.argv[1]), json.loads(sys.argv[2]), *map(int, sys.argv[3:])
w, h = s["writer"], s["hooks"]
checks = [
    (running == 2, f"processes still running: {running} of 2"),
    (no_vector == 0, f"memories without a vector: {no_vector}"),
    (w["users_seen"] == n and w["max"] == 1,
     f"memories that reached the writer: {w['users_seen']} of {n}, the most times one did: {w['max']}"),
    (h["distinct"] == n and not h["twice"],
     f"memory.created webhooks: {h['distinct']} of {n}, delivered more than once: {len(h['twice'])}"),
    (errors == 0, f"ERROR lines or tracebacks in the two logs: {errors}"),
]
for ok, say in checks:
    print(("ok:   " if ok else "FAIL: ") + say)
sys.exit(0 if all(ok for ok, _ in checks) else 1)
PY

init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"two-processes","version":"1"}}}'
H=(-H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
sid=$(curl -s -D - -o /dev/null "${H[@]}" -X POST localhost:18001/mcp -d "$init" \
  | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}' | tr -d '\r')
other=$(curl -s -o /dev/null -w '%{http_code}' "${H[@]}" -H "Mcp-Session-Id: $sid" \
  -H 'MCP-Protocol-Version: 2025-06-18' -X POST localhost:18002/mcp \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
echo "note: an MCP session opened on one process answers $other on the other (keep each MCP client on one process)"
echo "PASS: $N memories over two processes, each written once"
