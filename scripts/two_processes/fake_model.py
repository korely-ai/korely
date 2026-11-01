"""An OpenAI-compatible stand-in for the writer (scripts/two_processes.sh):
answers "no facts" to every call and counts, per test end user, how many
calls named it. Each memory has its own end user, so two processes taking
the same memory show as a count of 2. No key and no network."""
import json, re, threading
from collections import Counter
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

seen, calls, lock = Counter(), Counter(), threading.Lock()
USER = re.compile(r"lt-u-\d{6}")

async def chat(request):
    body = await request.json()
    text = "\n".join(m.get("content") or "" for m in body.get("messages", [])
                     if isinstance(m.get("content"), str))
    users = set(USER.findall(text))
    with lock:
        calls["total"] += 1
        for u in users:
            seen[u] += 1
    if '"memories": [' in text:
        ids = []
        for m in re.finditer(r"mem_id[=:\s\"']+([^\s\"'<>\]]+)", text):
            if m.group(1) not in ids and not m.group(1).startswith("<"):
                ids.append(m.group(1))
        out = {"memories": [{"mem_id": i, "facts": []} for i in ids]}
    else:
        out = {"facts": []}
    return JSONResponse({
        "id": "x", "object": "chat.completion", "model": body.get("model"),
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": json.dumps(out)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105}})

async def models(request):
    return JSONResponse({"object": "list", "data": [{"id": "fake-writer", "object": "model"}]})

async def stats(request):
    with lock:
        return JSONResponse({"calls": calls["total"], "users_seen": len(seen),
                             "users_twice": sorted(u for u, n in seen.items() if n > 1)[:50],
                             "max": max(seen.values()) if seen else 0})

app = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]),
                        Route("/v1/models", models), Route("/stats", stats)])
