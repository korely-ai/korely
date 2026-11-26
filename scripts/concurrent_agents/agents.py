"""30 agents at once (scripts/concurrent_agents.sh): REST clients and MCP
clients (streamable HTTP sessions, each pinned to one process as a load
balancer with sticky sessions would), several writing for the SAME end user,
the others for their own, every write followed by a read.

usage: agents.py ROUNDS OUT.json      (env: KEY)
Agent i: i < 15 REST (each request to alternating processes), else MCP
(session on process i % 2). Users: agents 0..19 share four end users (five
agents each, REST and MCP mixed; three of them in one shared agent namespace), agents 20..29 have their own.
Memory text: "Note cm-NNNNNN: <user> likes thing-K." (K cycles over 5 values, so
agents of one user state the SAME fact at the same moment) and every 7th
"... lives in city-K." (different per agent: they contradict).
Writes the accepted ids and the latencies to OUT.json; exits non-zero when a
write or a read failed."""
import asyncio, json, os, sys, time
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROUNDS, OUT = int(sys.argv[1]), sys.argv[2]
KEY = os.environ["KEY"]
BASES = ["http://api1:8000", "http://api2:8000"]
H = {"Authorization": f"Bearer {KEY}"}
AGENTS, SHARED_USERS, REST_AGENTS = 30, 4, 15

def user_of(a):
    return f"ca-user-s{a % SHARED_USERS}" if a < 20 else f"ca-user-own{a}"

def agent_of(a):
    """Agents 0..11 of the shared users share ONE namespace per end user (three
    agents, REST and MCP, writing the same (end user, agent) at once: where the
    one-open-fact-per-triple index is raced); the others have their own."""
    return f"team-s{a % SHARED_USERS}" if a < 12 else f"agent-{a}"

def text_of(a, seq):
    tag, user = f"cm-{a * 1000 + seq:06d}", user_of(a)
    if seq % 7 == 6:
        return tag, f"Note {tag}: {user} lives in city-{(a + seq) % 6}."
    return tag, f"Note {tag}: {user} likes thing-{(seq + (0 if a < 12 else a)) % 5}."

R = {"writes": [], "reads": [], "write_ms": [], "read_ms": [], "errors": [], "mcp_ok": 0, "mcp_agents": 0}

def err(who, what):
    if len(R["errors"]) < 30:
        R["errors"].append(f"{who}: {what}")

async def timed(kind, who, coro):
    t = time.perf_counter()
    try:
        out = await coro
    except Exception as e:  # a failed call is a result, not a crash
        err(who, f"{kind}: {type(e).__name__}: {str(e)[:200]}")
        return None
    R[f"{'write' if kind == 'write' else 'read'}_ms"].append((time.perf_counter() - t) * 1000)
    return out

READS = ["search", "facts", "context"]
# Since 1.0.2 a read past its account's four places at the embedder waits its
# turn (core/threads.CALLS_PER_ACCOUNT, at most WAIT_SECONDS) instead of a 503
# search_unavailable at once (31 Oct 2026). So the client makes one attempt:
# a refusal is a failed read, counted, and fails the run.
RETRIES, BUSY = 1, [0]

async def retrying(make, busy):
    """make() -> awaitable; busy(answer) -> True when it says 'retry'."""
    for attempt in range(RETRIES):
        out = await make()
        if not busy(out):
            return out
        BUSY[0] += 1
        await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))
    return out

async def rest_agent(a, c):
    user, who = user_of(a), f"rest-{a}"
    for seq in range(ROUNDS):
        base = BASES[(a + seq) % 2]
        tag, text = text_of(a, seq)
        r = await timed("write", who, c.post(f"{base}/v1/memories", headers=H, json={
            "content": text, "user_id": user, "agent_id": agent_of(a)}))
        if r is None or r.status_code != 201:
            err(who, f"write {tag}: {None if r is None else (r.status_code, r.text[:150])}")
        else:
            R["writes"].append([tag, r.json()["id"], user])
        kind = READS[(a + seq) % 3]
        if kind == "search":
            r = await timed("read", who, retrying(lambda: c.post(f"{base}/v1/memories/search", headers=H, json={
                "query": f"what does {user} like", "user_id": user, "limit": 5}), lambda r: r.status_code == 503))
        elif kind == "facts":
            r = await timed("read", who, c.get(f"{base}/v1/facts", headers=H, params={"user_id": user}))
        else:
            r = await timed("read", who, c.get(f"{base}/v1/context", headers=H, params={
                "query": f"{user} preferences", "user_id": user}))
        if r is None or r.status_code != 200:
            err(who, f"read {kind}: {None if r is None else (r.status_code, r.text[:150])}")
        elif kind == "context" and r.json().get("degraded"):
            err(who, "read context: degraded (no vector of the question)")
        else:
            R["reads"].append(kind)

def said(res):
    return "".join(getattr(b, "text", "") for b in res.content)

async def mcp_agent(a):
    user, who, base = user_of(a), f"mcp-{a}", BASES[a % 2]
    R["mcp_agents"] += 1
    async with streamablehttp_client(f"{base}/mcp", headers=H, timeout=60) as (rd, wr, _):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            for seq in range(ROUNDS):
                tag, text = text_of(a, seq)
                res = await timed("write", who, s.call_tool("korely_add", {
                    "content": text, "user_id": user, "agent_id": agent_of(a)}))
                if res is None or res.isError:
                    err(who, f"write {tag}: {None if res is None else said(res)[:150]}")
                else:
                    R["writes"].append([tag, said(res).split()[1].rstrip("."), user])
                kind = READS[(a + seq) % 3]
                args = {"search": ("korely_search", {"query": f"what does {user} like", "user_id": user, "limit": 5}),
                        "facts": ("korely_get_facts", {"user_id": user}),
                        "context": ("korely_get_context", {"query": f"{user} preferences", "user_id": user})}[kind]
                res = await timed("read", who, retrying(lambda: s.call_tool(*args),
                                                        lambda r: r.isError and "search_unavailable" in said(r)))
                if res is None or res.isError:
                    err(who, f"read {kind}: {None if res is None else said(res)[:150]}")
                else:
                    R["reads"].append(kind)
            tools = await s.list_tools()   # the session still answers at the end
            if tools.tools:
                R["mcp_ok"] += 1

async def main():
    t = time.time()
    async with httpx.AsyncClient(timeout=120, limits=httpx.Limits(max_connections=100)) as c:
        await asyncio.gather(*[rest_agent(a, c) for a in range(REST_AGENTS)],
                             *[mcp_agent(a) for a in range(REST_AGENTS, AGENTS)])
    R["seconds"] = time.time() - t
    R["busy"] = BUSY[0]
    json.dump(R, open(OUT, "w"))
    print(f"{AGENTS} agents, {len(R['writes'])} writes, {len(R['reads'])} reads in {R['seconds']:.1f}s, "
          f"MCP sessions answering at the end: {R['mcp_ok']} of {R['mcp_agents']}")
    for e in R["errors"][:10]:
        print("  error:", e)
    sys.exit(0 if not R["errors"] else 1)

asyncio.run(main())
