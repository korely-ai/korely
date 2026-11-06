"""The end-of-run checks of scripts/concurrent_agents.sh. usage: check.py RESULTS.json STATS.json DB.json RUNNING ERRORS
(DB.json: what the shell read from Postgres.) Prints one line per check and
the numbers; exit 0 only when every check passes."""
import json, sys
from statistics import quantiles

res, st, db = (json.load(open(p)) for p in sys.argv[1:4])
running, errors = int(sys.argv[4]), int(sys.argv[5])
w, r = res["writes"], res["reads"]
def pct(xs, q):
    return quantiles(xs, n=100)[q - 1] if len(xs) > 1 else (xs[0] if xs else 0)
tags = [t for t, _, _ in w]
bad_w = [e for e in res["errors"] if ": write " in e]
bad_r = [e for e in res["errors"] if ": read " in e]
db["tags_twice"] = len(db["tags"]) - len(set(db["tags"]))
db["missing_tags"] = len(set(tags) - set(db["tags"]))
checks = [
    (not bad_w and len(w) == db["sent"], f"writes accepted: {len(w)} of {db['sent']}" + (f"; first error: {bad_w[0]}" if bad_w else "")),
    (not bad_r, f"reads answered: {len(r)} of {db['sent']}" + (f"; first error: {bad_r[0]}" if bad_r else "")),
    (len({i for _, i, _ in w}) == len(w), f"distinct memory ids returned: {len({i for _, i, _ in w})} of {len(w)}"),
    (len(db["tags"]) == len(w) and db["tags_twice"] == 0 and db["missing_tags"] == 0,
     f"memories stored: {len(db['tags'])} of {len(w)}; stored twice: {db['tags_twice']}; accepted but not stored: {db['missing_tags']}"),
    (db["no_vector"] == 0 and db["pending"] == 0, f"memories without a vector: {db['no_vector']}; still pending: {db['pending']}"),
    (st["memories_seen"] == len(w) and st["most_times_one_memory"] == 1 and not set(tags) - set(st["seen"]),
     f"memories that reached the writer: {st['memories_seen']} of {len(w)}, the most times one did: {st['most_times_one_memory']}"),
    (db["fact_dupes"] == 0, f"open facts repeated for the same (end user, agent namespace, subject, predicate, object): {db['fact_dupes']}"),
    (db["facts_lost"] == 0, f"likes/lives_in statements with no fact at all (open or closed): {db['facts_lost']} of {db['statements']}"),
    (not st["unknown_prompts"], f"model calls the stand-in did not recognise: {len(st['unknown_prompts'])}"),
    (running == 2, f"processes still running: {running} of 2"),
    (errors == 0, f"ERROR lines or tracebacks in the two logs: {errors}"),
    (res["mcp_ok"] == res["mcp_agents"], f"MCP sessions that answered at the end: {res['mcp_ok']} of {res['mcp_agents']}"),
]
for ok, say in checks:
    print(("ok:   " if ok else "FAIL: ") + say)
print(f"note: lives_in is single-valued; end users with more than one open lives_in fact: {db['lives_open_many']} of {db['lives_users']}"
      " (informational: agent namespaces are separate, so they do not close each other)")
print(f"note: reads the embedder refused as busy (503 search_unavailable; one attempt, so each is also a failed read): {res['busy']}")
print(f"note: judge calls {st['calls'].get('judge', 0)}, extraction calls {st['calls'].get('extraction', 0)}, facts rows {db['facts']}, closed {db['closed']}")
for k in ("write", "read"):
    ms = res[f"{k}_ms"]
    print(f"{k}s: {len(ms)}  p50 {pct(ms, 50):.0f} ms  p95 {pct(ms, 95):.0f} ms  max {max(ms or [0]):.0f} ms")
ok = all(c[0] for c in checks)
print(f"{'PASS' if ok else 'FAIL'}: 30 agents (15 REST, 15 MCP), {len(w)} writes, {len(r)} reads, {res['seconds']:.0f} s under load, 2 processes on one Postgres")
sys.exit(0 if ok else 1)
