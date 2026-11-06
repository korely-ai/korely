"""The load of scripts/soak.sh: what an agent like Hermes does all day.

USERS end users; every SECONDS seconds one of them gets a memory written, a
search and a context read, as an agent does around each turn of a
conversation. Runs until killed; a request that fails is counted, and the
counts are printed every minute (one line, for the soak's log).
"""
import os, random, sys, time
import httpx

BASE, KEY = os.environ["BASE"], os.environ["KEY"]
USERS, SECONDS = int(sys.argv[1]), float(sys.argv[2])
H = {"Authorization": f"Bearer {KEY}"}
counts = {"write": 0, "search": 0, "context": 0, "failed": 0}
started, last = time.time(), time.time()
topics = ["prefers the window seat", "moved to the Basic plan", "works on the billing team",
          "asked for invoices by email", "is allergic to peanuts", "reads in Italian"]
with httpx.Client(timeout=60) as c:
    i = 0
    while True:
        u = f"soak-u-{random.randrange(USERS):04d}"
        try:
            r1 = c.post(f"{BASE}/v1/memories", headers=H, json={
                "content": f"Turn {i}: {u} {random.choice(topics)}.", "user_id": u})
            r2 = c.post(f"{BASE}/v1/memories/search", headers=H, json={"query": random.choice(topics), "user_id": u})
            r3 = c.get(f"{BASE}/v1/context", headers=H, params={"query": "what do we know", "user_id": u})
            for kind, r in (("write", r1), ("search", r2), ("context", r3)):
                counts[kind if r.status_code < 300 else "failed"] += 1
        except httpx.HTTPError:
            counts["failed"] += 1
        i += 1
        if time.time() - last >= 60:
            last = time.time()
            print(f"{int(last - started)}s {counts}", flush=True)
        time.sleep(SECONDS)
