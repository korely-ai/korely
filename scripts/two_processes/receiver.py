"""The webhook endpoint of scripts/two_processes.sh: counts deliveries per
event and memory id."""
import json, threading
from collections import Counter
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

got, lock = Counter(), threading.Lock()

async def hook(request):
    body = await request.json()
    key = json.dumps(body.get("data", body), sort_keys=True)[:400]
    mid = (body.get("data") or {}).get("id") or (body.get("data") or {}).get("memory_id") or key
    with lock:
        got[(body.get("type") or body.get("event"), mid)] += 1
    return Response(status_code=204)

async def stats(request):
    with lock:
        return JSONResponse({"deliveries": sum(got.values()), "distinct": len(got),
                             "twice": [list(k) for k, n in got.items() if n > 1][:20]})

app = Starlette(routes=[Route("/hook", hook, methods=["POST"]), Route("/stats", stats)])
