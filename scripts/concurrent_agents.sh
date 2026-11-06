#!/usr/bin/env bash
# 30 agents at once against two Korely processes on one Postgres: is any write
# lost, doubled or corrupted?
#
#   bash scripts/concurrent_agents.sh            # 30 agents, 20 rounds each
#   bash scripts/concurrent_agents.sh 40         # 40 rounds each
#
# The competitors' most reported failure is memory lost or corrupted when
# several agents write at once (several Claude Code windows on one store).
# scripts/two_processes.sh measured 2 processes and one client; this is the
# crowd:
#   - 30 clients: 15 REST and 15 MCP (streamable HTTP sessions, each kept on
#     one process as a load balancer with sticky sessions would);
#   - 20 of them write for four SHARED end users (five agents each, REST and
#     MCP mixed, stating the same fact at the same moment, and every seventh
#     memory a single-valued "lives in" that contradicts the others'); 10 have
#     their own end user;
#   - every write is followed by a read (search, get_facts, get_context).
# The stand-in model (concurrent_agents/fake_model.py) writes a deterministic
# fact per memory and answers the contradiction judge and the other questions
# the engine asks. No key, nothing leaves the machine, nothing is spent.
#
# It passes when: every write was accepted; every memory is stored once with
# its vector; every memory reached the writer once; no open fact repeats for
# the same (end user, agent namespace, subject, predicate, object) (the same triple in two
# agents' namespaces is two facts by design); no statement lost its fact;
# neither log has an ERROR or traceback; both processes run; every MCP session
# answers. The stack has its own compose project and ports 18011/18012, and
# the database lives in memory; everything is removed at the end.
#
# Environment, all optional: KORELY_IMAGE (built from this tree when unset),
# KORELY_CODE_FROM_TREE=1 (mount this tree's code over the image's).
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ROUNDS="${1:-20}"
DIR="$HERE/concurrent_agents"
FILES=(-f "$DIR/compose.yml")
if [ "${KORELY_CODE_FROM_TREE:-}" = 1 ]; then
  FILES+=(-f "$HERE/two_processes/code-from-tree.yml")
fi
if [ -z "${KORELY_IMAGE:-}" ]; then
  KORELY_IMAGE=korely-agent:concurrent-agents
  echo "building $KORELY_IMAGE from this tree..."
  docker build -q -t "$KORELY_IMAGE" "$ROOT" >/dev/null
fi
export KORELY_IMAGE KORELY_TREE="$ROOT"
OUT="$(mktemp -d)"
dc() { docker compose "${FILES[@]}" "$@"; }
sql() { dc exec -T db psql -U korely -d korely -tAc "$1"; }
inside() { docker run --rm --network korely-concurrent-agents_default -v "$DIR:/t:ro" -v "$OUT:/out" "$@"; }
cleanup() { [ "${KEEP:-}" = 1 ] && return; dc down -v >/dev/null 2>&1 || true; rm -rf "$OUT"; }
trap cleanup EXIT
fail() { echo "FAIL: $*"; for s in api1 api2; do echo "--- $s"; dc logs --no-color "$s" 2>&1 | tail -20; done; exit 1; }

cleanup
OUT="$(mktemp -d)"
dc up -d >/dev/null 2>&1
for _ in $(seq 1 60); do
  a=$(curl -s -o /dev/null -w '%{http_code}' localhost:18011/health || true)
  b=$(curl -s -o /dev/null -w '%{http_code}' localhost:18012/health || true)
  [ "$a" = 200 ] && [ "$b" = 200 ] && break
  sleep 3
done
[ "$a" = 200 ] && [ "$b" = 200 ] || fail "both processes should answer /health; they answered $a and $b"
echo "ok: both processes started together on a new database"

out=$(dc exec -T api1 python -m korely_agent.manage create-tenant --name "Concurrent agents")
KEY=$(echo "$out" | awk '/^key/{print $2}')

# The load. A failed write or read is reported by the checks below, so keep going.
inside -e KEY="$KEY" --entrypoint python "$KORELY_IMAGE" /t/agents.py "$ROUNDS" /out/results.json || true
[ -s "$OUT/results.json" ] || fail "the agents did not finish"

for _ in $(seq 1 240); do
  left=$(sql "SELECT count(*) FROM agent_memories WHERE embedding_status = 'pending' OR graph_sync_status = 'pending'")
  [ "$left" = 0 ] && break
  sleep 5
done
[ "$left" = 0 ] || echo "note: $left memories still waiting after 20 minutes"

inside --entrypoint python "$KORELY_IMAGE" -c "
import httpx; print(httpx.get('http://writer:9000/stats').text)" > "$OUT/stats.json"

sql "
WITH st AS (
  SELECT m.id, m.agent_id, (regexp_match(m.content, 'Note (cm-[0-9]{6}): (.+?) (likes|lives in) ((?:thing|city)-[0-9]+)\.'))[2] AS usr,
         CASE (regexp_match(m.content, 'Note (cm-[0-9]{6}): (.+?) (likes|lives in) ((?:thing|city)-[0-9]+)\.'))[3] WHEN 'likes' THEN 'likes' ELSE 'lives_in' END AS pred,
         (regexp_match(m.content, 'Note (cm-[0-9]{6}): (.+?) (likes|lives in) ((?:thing|city)-[0-9]+)\.'))[4] AS obj
  FROM agent_memories m),
ln AS (SELECT end_user_id, count(*) FILTER (WHERE invalid_at IS NULL) AS open
       FROM agent_facts WHERE predicate_raw = 'lives_in' GROUP BY end_user_id)
SELECT json_build_object(
  'sent', $ROUNDS * 30,
  'tags', (SELECT coalesce(json_agg(substring(content from 'cm-[0-9]{6}')), '[]'::json) FROM agent_memories),
  'no_vector', (SELECT count(*) FROM agent_memories WHERE embedding IS NULL),
  'pending', $left,
  'fact_dupes', (SELECT count(*) FROM (SELECT 1 FROM agent_facts WHERE invalid_at IS NULL
                 GROUP BY end_user_id, agent_id, lower(subject_canonical), predicate_canonical, lower(object_canonical) HAVING count(*) > 1) d),
  'statements', (SELECT count(*) FROM st),
  'facts_lost', (SELECT count(*) FROM st WHERE NOT EXISTS (SELECT 1 FROM agent_facts f
                 WHERE f.end_user_id = st.usr AND f.agent_id IS NOT DISTINCT FROM st.agent_id AND f.predicate_raw = st.pred AND f.object_label = st.obj)),
  'lives_users', (SELECT count(*) FROM ln), 'lives_open_many', (SELECT count(*) FROM ln WHERE open > 1),
  'facts', (SELECT count(*) FROM agent_facts), 'closed', (SELECT count(*) FROM agent_facts WHERE invalid_at IS NOT NULL))" > "$OUT/db.json"

echo "--- open facts that repeat (sample):"
sql "SELECT end_user_id, agent_id, predicate_raw, object_label, count(*), string_agg(substr(id::text,1,8) || '@' || to_char(created_at,'SS.MS') || ' m=' || substr(coalesce(source_memory_id::text,'-'),1,8), ', ' ORDER BY created_at)
     FROM agent_facts WHERE invalid_at IS NULL GROUP BY end_user_id, agent_id, lower(subject_canonical), predicate_canonical, predicate_raw, lower(object_canonical), object_label HAVING count(*) > 1 LIMIT 5"
running=$(dc ps --status running --services | grep -c '^api' || true)
errors=$(for s in api1 api2; do dc logs --no-color "$s" 2>&1; done | grep -c -E '\| ERROR|Traceback' || true)
python3 "$DIR/check.py" "$OUT/results.json" "$OUT/stats.json" "$OUT/db.json" "$running" "$errors" || fail "see above"
