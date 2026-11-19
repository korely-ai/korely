"""N memories, each for its own end user, sent round-robin to both processes,
CONC at a time (scripts/two_processes.sh). Exits non-zero unless every one is
accepted."""
import asyncio, os, sys, time, httpx
N, CONC = int(sys.argv[1]), int(sys.argv[2])
KEY = os.environ["KEY"]
BASES = ["http://api1:8000", "http://api2:8000"]
H = {"Authorization": f"Bearer {KEY}"}
codes = {}
async def one(c, i, sem):
    async with sem:
        r = await c.post(f"{BASES[i % 2]}/v1/memories", headers=H, json={
            "content": f"Test note {i}: lt-u-{i:06d} prefers the window seat.",
            "user_id": f"lt-u-{i:06d}"})
        codes[r.status_code] = codes.get(r.status_code, 0) + 1
        if r.status_code >= 300 and codes[r.status_code] < 4:
            print(r.status_code, r.text[:300])
async def main():
    sem = asyncio.Semaphore(CONC); t = time.time()
    async with httpx.AsyncClient(timeout=60) as c:
        await asyncio.gather(*(one(c, i, sem) for i in range(N)))
    print(f"sent {N} in {time.time()-t:.1f}s, answers {codes}")
    sys.exit(0 if codes == {201: N} else 1)
asyncio.run(main())
