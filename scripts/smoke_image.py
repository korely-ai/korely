#!/usr/bin/env python3
"""The published image, started as an install starts it, checked from outside
(2026-10-06; .github/workflows/image-smoke.yml runs it on each release).

Until then the image a customer pulls from GHCR was built and pushed and never
started by anything (the code audit of 6 October). Checked here:

  * /health answers 200 and says the version the image promises;
  * a key of a fresh tenant writes a memory (201);
  * with the local embedder, the default image: the memory is found again by
    its meaning, by POST /v1/memories/search and in GET /v1/context, with no
    external key at all (EmbeddingGemma computes the vectors in the image);
  * with the Gemini embedder and no key, as on a CI runner: the write is still
    accepted, and the search says it cannot run (503 `search_unavailable`)
    instead of an empty 200, which is what the image does without a key.

    python3 scripts/smoke_image.py --url http://127.0.0.1:8000 \\
        --key kor_self_... --version 0.1.19 --embedder local

Stdlib only: it runs on a bare runner. Exit 1 on the first check that fails,
saying which.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER = "smoke-maria"
MEMORY = "Maria moved to Rome in March and works as a nurse at the Gemelli hospital."
QUESTION = "Where does Maria live?"


def call(url: str, method: str = "GET", key: str = "", body=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def check(ok: bool, what: str, detail="") -> None:
    print(("ok   " if ok else "BAD  ") + what + (f": {detail}" if detail and not ok else ""))
    if not ok:
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--embedder", choices=("local", "gemini"), required=True)
    a = ap.parse_args()
    base = a.url.rstrip("/")

    status, health = call(f"{base}/health")
    check(status == 200, "/health answers 200", f"{status} {health}")
    check(health.get("version") == a.version, f"the image says version {a.version}",
          health.get("version"))

    status, written = call(f"{base}/v1/memories", "POST", a.key, {"content": MEMORY, "user_id": USER})
    check(status == 201, "a memory is written", f"{status} {written}")

    search = {"query": QUESTION, "user_id": USER}
    if a.embedder == "gemini":
        status, body = call(f"{base}/v1/memories/search", "POST", a.key, search)
        check(status == 503 and body.get("code") == "search_unavailable",
              "without a key the search says it cannot run, never an empty 200", f"{status} {body}")
        return 0

    # The local embedder computes the vector on the write; a vector the write
    # could not compute is retried by the drain, so give it a minute.
    found = []
    for _ in range(30):
        status, body = call(f"{base}/v1/memories/search", "POST", a.key, search)
        found = [m for m in (body.get("results") or []) if m.get("snippet") == MEMORY]
        if status == 200 and found:
            break
        time.sleep(2)
    check(bool(found), "the memory is found again by its meaning", f"{status} {body}")

    q = urllib.parse.urlencode({"query": QUESTION, "user_id": USER})
    status, ctx = call(f"{base}/v1/context?{q}", "GET", a.key)
    check(status == 200 and MEMORY in (ctx.get("context") or ""),
          "/v1/context hands it to the agent", f"{status} {ctx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
